"""MemoryCompactor — 滚动折叠式场景记忆的压缩器。

把滚出条数窗的旧事件折叠进 `runtime.context_compacted` 事件携带的滚动摘要
（旧摘要 + 新滚出片段 → LLM → 新摘要完整替换，latest-wins）。摘要事件同时
是压缩进度游标：payload.covers_until_event_id 之前（含）的事件已被覆盖。

由投影的触顶通知单向驱动；worker 启动与空闲期不扫描数据库。投影以最新
recap 为窗口下界，重启后继续复用已落盘摘要与覆盖游标。

单批折叠条数有硬上限：超出的更早积压**整段跳过、不进摘要**，游标一步
跨过去。记忆只覆盖"开始记之后"的历史，输入长度与积压规模脱钩。

失败语义：LLM 调用失败 / 输出不可解析 → 不写事件、游标不动，等待后续
事件再次触顶；绝不在启动或定时扫描时主动补压。每次触顶只做一次 LLM
merge、写一代完整替换摘要（投影只认最新一条 recap）。

契约：开发文档/v2.0/20-横切契约/记忆系统契约.md
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterator

from sqlalchemy import and_, or_, select

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_env_value
from qqbot.core.time import china_now, normalize_china_time
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.event_writer import (
    SessionFactory,
    parse_scope_key,
    write_runtime_event,
)
from qqbot.services.agent_loop.projection import (
    Projector,
    _snapshot_from_row,
    render_timeline_stream,
)
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    should_snapshot,
    write_snapshot,
)

logger = get_logger(__name__)

RECAP_EVENT_TYPE = "runtime.context_compacted"

# 压缩器算法 / prompt 代次，写入 payload.compactor_version 供回放与演进区分。
# v2：主输出契约由 JSON 改为标签块，旧 JSON 仅作兼容降级。
COMPACTOR_VERSION = 2

# 单批折叠上限 = 稳态批量（TRIGGER − KEEP）的这个倍数。留一整批余量吸收
# 探针过冲与突发，稳态下恒不触发；超出即判定为异常积压并整段跳过。
MAX_FOLD_BATCH_MULTIPLIER = 2

_TRUTHY = {"1", "true", "yes", "on"}


def memory_compaction_enabled() -> bool:
    """总开关（S2 由 supervisor 依此决定是否启动压缩器）。"""
    raw = (get_env_value("MEMORY_COMPACTION_ENABLED") or "").strip().lower()
    return raw in _TRUTHY


def load_scope_allowlist() -> frozenset[str] | None:
    """MEMORY_COMPACTION_SCOPES 灰度白名单（记忆系统契约 §8 S3）。

    逗号分隔的 scope_key（"group:123"）或裸群号（"123"，归一化为
    group:123）；空/未设 = 不限制（所有 group，仍受总开关管）。压缩器
    构造时读定——改动需重启，换来 notify 热路径零 env 读。"""
    raw = (get_env_value("MEMORY_COMPACTION_SCOPES") or "").strip()
    if not raw:
        return None
    normalized: set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        normalized.add(f"group:{part}" if part.isdigit() else part)
    return frozenset(normalized) or None


def _int_env(key: str, default: int) -> int:
    raw = get_env_value(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("[memory] env {} 非整数（{!r}），使用缺省 {}", key, raw, default)
        return default


@dataclass(frozen=True)
class CompactionConfig:
    """契约 §6 参数表（env 读取，运行期可调）。"""

    summary_max_chars: int
    trigger_events: int
    keep_events: int

    @staticmethod
    def load() -> "CompactionConfig":
        return CompactionConfig(
            summary_max_chars=_int_env("MEMORY_SUMMARY_MAX_CHARS", 1200),
            trigger_events=_int_env("MEMORY_COMPACTION_TRIGGER_EVENTS", 250),
            keep_events=_int_env("MEMORY_COMPACTION_KEEP_EVENTS", 150),
        )


@dataclass(frozen=True)
class _Cursor:
    """从最新 recap 折出的压缩进度游标（覆盖边界 + 旧摘要底稿）。"""

    event_id: str
    occurred_at: datetime
    summary: str
    folded_revision: int


@dataclass(frozen=True)
class CompactionOutcome:
    """单次 compact_scope 的结果（供日志 / 测试断言）。"""

    scope_key: str
    rounds: int
    event_ids: tuple[str, ...]
    skipped_reason: str | None


# 输出主格式（v2）：标签块。散文体摘要里引号/换行常见，
# 要求纯文本模型对 JSON 完美转义并不现实——生产首日
# deepseek-v4-flash 即因正文内未转义引号整轮解析失败。标签格式
# 对二者免疫；JSON 保留为兼容降级。允许模型在标签名后多打空格、
# 改变大小写或误包代码围栏，但重复摘要块属歧义输出，不猜测。
_TAG_FLAGS = re.DOTALL | re.IGNORECASE
_SUMMARY_OPEN_RE = re.compile(r"<summary\s*>", re.IGNORECASE)
_SUMMARY_CLOSE_RE = re.compile(r"</summary\s*>", re.IGNORECASE)
_SUMMARY_TAG_RE = re.compile(r"<summary\s*>\s*(.*?)\s*</summary\s*>", _TAG_FLAGS)
_CUES_OPEN_RE = re.compile(r"<recall-cues\s*>", re.IGNORECASE)
_CUES_CLOSE_RE = re.compile(r"</recall-cues\s*>", re.IGNORECASE)
_CUES_TAG_RE = re.compile(r"<recall-cues\s*>\s*(.*?)\s*</recall-cues\s*>", _TAG_FLAGS)
_CUE_PREFIX_RE = re.compile(r"^(?:[-*•]\s*|\d+[.)、]\s*)")


def parse_compaction_output(text: str) -> tuple[str, list[str]] | None:
    """解析 LLM 输出为 (summary, recall_cues)；不可解析返回 None。

    防御链：标签主格式 → JSON 兼容（裸 → 剥围栏 → 大括号切片，契约
    §5.2）。解析失败整轮放弃、不修补不猜测——宁可没记忆，不写脏记忆。"""
    # 一旦出现 summary 标签边界，就按新协议严格解析。若边界
    # 缺失/重复，不再从同一份歧义输出中猜选某个 JSON 片段。
    if _SUMMARY_OPEN_RE.search(text) or _SUMMARY_CLOSE_RE.search(text):
        return _parse_tagged(text)
    for candidate in _json_candidates(text):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        cues_raw = data.get("recall_cues")
        cues: list[str] = []
        if isinstance(cues_raw, list):
            cues = [c.strip() for c in cues_raw if isinstance(c, str) and c.strip()][:5]
        return summary.strip(), cues
    return None


def _match_single_tag_block(
    text: str,
    open_pattern: re.Pattern[str],
    close_pattern: re.Pattern[str],
    block_pattern: re.Pattern[str],
) -> re.Match[str] | None:
    """只在开/闭边界各一个且顺序合法时返回块匹配。"""
    open_count = sum(1 for _ in open_pattern.finditer(text))
    close_count = sum(1 for _ in close_pattern.finditer(text))
    if open_count != 1 or close_count != 1:
        return None
    return block_pattern.search(text)


def _parse_tagged(text: str) -> tuple[str, list[str]] | None:
    summary_match = _match_single_tag_block(
        text, _SUMMARY_OPEN_RE, _SUMMARY_CLOSE_RE, _SUMMARY_TAG_RE
    )
    if summary_match is None:
        return None
    summary = summary_match.group(1).strip()
    if not summary:
        return None
    cues: list[str] = []
    has_cue_boundary = bool(_CUES_OPEN_RE.search(text) or _CUES_CLOSE_RE.search(text))
    if not has_cue_boundary:
        return summary, cues
    cues_match = _match_single_tag_block(
        text, _CUES_OPEN_RE, _CUES_CLOSE_RE, _CUES_TAG_RE
    )
    if cues_match is None:
        return None
    for line in cues_match.group(1).splitlines():
        cue = _CUE_PREFIX_RE.sub("", line.strip()).strip()
        if cue:
            cues.append(cue)
    return summary, cues[:5]


def _json_candidates(text: str) -> Iterator[str]:
    stripped = text.strip()
    yield stripped
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        yield body.rsplit("```", 1)[0].strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        yield stripped[start : end + 1]


def truncate_at_sentence(text: str, max_chars: int) -> str:
    """摘要超预算时句边界硬截断（找不到过半处的句读才裸切）。"""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    best = max(cut.rfind(ch) for ch in "。！？!?；;\n")
    if best >= max_chars // 2:
        return cut[: best + 1]
    return cut


def _extract_text(message: Any) -> str:
    """langchain BaseMessage.content 可能是 str 或 list[dict]，拍平成 str
    （与 meme_caption._extract_text 同语义的本地副本，避免横向 import）。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, dict) and "text" in chunk:
                parts.append(str(chunk["text"]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "".join(parts)
    return str(content)


class MemoryCompactor:
    """每 scope 互斥的滚动摘要压缩器（group scope 专属）。

    依赖注入：``session_factory``（每次新开独立事务）与 ``llm_factory``
    （缺省 ``create_llm(role="memory")``，路由未配置该 role 时回落 default，
    见 LLM 路由契约 §3）——契约测试全离线跑。"""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        llm_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._session_factory = session_factory
        # None = 走出口网关 invoke；测试注入 factory 则本地 ainvoke。
        self._llm_factory = llm_factory
        # 灰度白名单构造时读定（None=不限制）；改 env 需重启（同阈值）。
        self._scope_allowlist = load_scope_allowlist()
        self._trigger_events = CompactionConfig.load().trigger_events
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = False

    # ─── worker 生命周期（S2 由 supervisor 启停）───

    def start(self) -> None:
        if self._task is not None:
            return
        # 启动只挂起 worker；不扫描、不唤醒。只有投影探针报告真正触顶后
        # notify() 才允许进入 merge，重启本身绝不产生 LLM 请求。
        self._task = asyncio.create_task(self._run(), name="memory_compactor")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    def notify(self, scope_key: str, uncovered_events: int) -> None:
        """只接受已触顶的投影通知；幂等、无 SQL。

        阈值在 worker 边界再次校验，避免调用方误 poke 导致启动/定时任务
        意外触发 merge。拿锁后仍会查库复核真实未覆盖数。
        """
        if not scope_key.startswith("group:"):
            return
        if uncovered_events < self._trigger_events:
            return
        if self._scope_allowlist is not None and scope_key not in self._scope_allowlist:
            return
        self._pending.add(scope_key)
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopped:
            await self._wake.wait()
            self._wake.clear()
            if self._stopped:
                break
            pending = set(self._pending)
            self._pending.clear()
            for scope_key in sorted(pending):
                try:
                    await self.compact_scope(scope_key)
                except Exception as exc:
                    logger.warning(
                        "[memory] {} 压缩异常（等待后续触顶）: {}", scope_key, exc
                    )

    # ─── 单 scope 压缩主流程 ───

    async def compact_scope(
        self, scope_key: str, *, now: datetime | None = None
    ) -> CompactionOutcome:
        """复核 → 批选取 → 一次 LLM merge → 写一代完整 recap。"""
        if not scope_key.startswith("group:"):
            return CompactionOutcome(scope_key, 0, (), "scope_not_supported")
        if self._scope_allowlist is not None and scope_key not in self._scope_allowlist:
            return CompactionOutcome(scope_key, 0, (), "scope_not_enabled")
        lock = self._locks.setdefault(scope_key, asyncio.Lock())
        if lock.locked():
            return CompactionOutcome(scope_key, 0, (), "already_running")
        async with lock:
            return await self._compact_locked(scope_key, now or china_now())

    async def _compact_locked(self, scope_key: str, now: datetime) -> CompactionOutcome:
        cfg = CompactionConfig.load()
        scope, group_id, _ = parse_scope_key(scope_key)
        cursor = await self._load_cursor(scope, group_id)
        keys = await self._fetch_uncovered_keys(scope, group_id, cursor, now)
        if len(keys) < cfg.trigger_events:
            return CompactionOutcome(scope_key, 0, (), "below_trigger")
        foldable = keys[: len(keys) - cfg.keep_events]
        batch_size = cfg.trigger_events - cfg.keep_events
        if batch_size <= 0 or not foldable:
            logger.warning(
                "[memory] {} keep({}) ≥ trigger({}) / 未覆盖数({})，配置违反不变式②",
                scope_key,
                cfg.keep_events,
                cfg.trigger_events,
                len(keys),
            )
            return CompactionOutcome(scope_key, 0, (), "config_invalid")
        # 定量批：只折最靠近窗口的一段，更早的积压整段跳过（契约 §4.3）。
        # 上界不动——游标照旧推进到"恰留 KEEP 条"处，一步跨过被跳过的积压，
        # 下一拍即回稳态。否则积压规模直接决定单次输入长度：首次开启 /
        # 停机 / 上一轮失败攒下的历史会一次性灌进单次调用撑爆上下文，而
        # 失败又不推进游标 → 积压更大 → 永久卡死（2026-07-27 生产实况）。
        batch = foldable[-(batch_size * MAX_FOLD_BATCH_MULTIPLIER) :]
        skipped = len(foldable) - len(batch)
        start_id, start_at = batch[0]
        boundary_id, boundary_at = batch[-1]
        snaps = await self._fetch_slice_rows(
            scope, group_id, start_id, start_at, boundary_id, boundary_at
        )
        if not snaps:
            return CompactionOutcome(scope_key, 0, (), "empty_slice")
        rendered = self._render_slice(scope_key, snaps, now)
        if self._llm_factory is None:
            llm = None
        else:
            llm = await self._llm_factory()
            if llm is None:
                logger.warning(
                    "[memory] {} LLM 未配置（role=memory 且无 default 候选）",
                    scope_key,
                )
                return CompactionOutcome(scope_key, 0, (), "llm_unavailable")
        result = await self._summarize(
            scope_key=scope_key,
            llm=llm,
            system_prompt=self._load_system_prompt(),
            cursor=cursor,
            rendered=rendered,
            count=len(snaps),
            covers_from=normalize_china_time(snaps[0].occurred_at),
            covers_until=normalize_china_time(snaps[-1].occurred_at),
            max_chars=cfg.summary_max_chars,
        )
        if result is None:
            return CompactionOutcome(scope_key, 0, (), "llm_failed")
        summary, cues = result
        revision = (cursor.folded_revision if cursor else 0) + 1
        event_id = await self._write_recap(
            scope_key=scope_key,
            summary=summary,
            cues=cues,
            snaps=snaps,
            revision=revision,
            skipped=skipped,
            correlation_id=new_event_id(),
        )
        logger.info(
            "[memory] {} 第 {} 代摘要落盘：折叠 {} 条，{} 字",
            scope_key,
            revision,
            len(snaps),
            len(summary),
        )
        if skipped:
            # 这批历史永久不进记忆（append-only，无补压通道）——该出现的
            # 场合只有首次开启与积压异常，日常刷屏说明该调 TRIGGER/KEEP。
            logger.warning(
                "[memory] {} 跳过 {} 条更早积压（超单批上限 {}），不进摘要",
                scope_key,
                skipped,
                batch_size * MAX_FOLD_BATCH_MULTIPLIER,
            )
        return CompactionOutcome(scope_key, 1, (event_id,), None)

    # ─── 查询 ───

    async def _load_cursor(self, scope: str, group_id: int | None) -> _Cursor | None:
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.type == RECAP_EVENT_TYPE)
        )
        if scope == "group" and group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        stmt = stmt.order_by(
            AgentEvent.occurred_at.desc(), AgentEvent.event_id.desc()
        ).limit(1)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        if not rows:
            return None
        row = rows[0]
        payload = dict(row.payload or {})
        raw_id = payload.get("covers_until_event_id")
        occurred: datetime | None = None
        raw_at = payload.get("covers_until_occurred_at")
        if isinstance(raw_at, str):
            try:
                occurred = datetime.fromisoformat(raw_at)
            except ValueError:
                occurred = None
        if not isinstance(raw_id, str) or occurred is None:
            # 载荷损坏：退化锚在 recap 行自身——其之前的事件本就在窗口之外。
            logger.warning(
                "[memory] recap {} 载荷缺 covers_until，退化锚在事件自身",
                row.event_id,
            )
            raw_id, occurred = row.event_id, row.occurred_at
        summary = payload.get("summary")
        revision = payload.get("folded_revision")
        return _Cursor(
            event_id=raw_id,
            occurred_at=occurred,
            summary=summary if isinstance(summary, str) else "",
            folded_revision=revision if isinstance(revision, int) else 0,
        )

    def _uncovered_filters(self, stmt: Any, cursor: _Cursor | None) -> Any:
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    AgentEvent.occurred_at > cursor.occurred_at,
                    and_(
                        AgentEvent.occurred_at == cursor.occurred_at,
                        AgentEvent.event_id > cursor.event_id,
                    ),
                )
            )
        return stmt

    async def _fetch_uncovered_keys(
        self,
        scope: str,
        group_id: int | None,
        cursor: _Cursor | None,
        now: datetime,
    ) -> list[tuple[str, datetime]]:
        stmt = (
            select(AgentEvent.event_id, AgentEvent.occurred_at)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.visibility == "agent_visible")
            .where(AgentEvent.type != RECAP_EVENT_TYPE)
            .where(AgentEvent.occurred_at <= now)
        )
        if scope == "group" and group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        stmt = self._uncovered_filters(stmt, cursor)
        stmt = stmt.order_by(
            AgentEvent.occurred_at.asc(), AgentEvent.event_id.asc()
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.all()
        return [(row[0], row[1]) for row in rows]

    async def _fetch_slice_rows(
        self,
        scope: str,
        group_id: int | None,
        start_id: str,
        start_at: datetime,
        boundary_id: str,
        boundary_at: datetime,
    ) -> list[Any]:
        """取折叠区间 [start, boundary] 的完整事件行（全序闭区间）。

        下界取自定量批首条而非游标——游标与 start 之间的积压本轮整段
        跳过，SQL 必须同步收窄，否则仍会把全部积压捞回来。"""
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.visibility == "agent_visible")
            .where(AgentEvent.type != RECAP_EVENT_TYPE)
            .where(
                or_(
                    AgentEvent.occurred_at > start_at,
                    and_(
                        AgentEvent.occurred_at == start_at,
                        AgentEvent.event_id >= start_id,
                    ),
                )
            )
            .where(
                or_(
                    AgentEvent.occurred_at < boundary_at,
                    and_(
                        AgentEvent.occurred_at == boundary_at,
                        AgentEvent.event_id <= boundary_id,
                    ),
                )
            )
        )
        if scope == "group" and group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        stmt = stmt.order_by(
            AgentEvent.occurred_at.asc(), AgentEvent.event_id.asc()
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_snapshot_from_row(row) for row in rows]

    # ─── 渲染与预算 ───

    def _render_slice(self, scope_key: str, snaps: list[Any], now: datetime) -> str:
        """区间事件走投影同一套折叠/渲染管线——压缩器读到的文本与 Planner
        所见同构（契约 §4.4）。max_timeline_items=None：折叠输入不裁行。"""
        ctx = Projector.project(
            snaps,
            scope_key=scope_key,
            correlation_id="memory-compaction",
            tick_seq=0,
            now=now,
            max_timeline_items=None,
        )
        # render_timeline_stream 返回行列表（Planner/Replyer 在信封侧自行
        # join）；这里拼成单块文本——预算按字数比较、直接进 user 信封。
        return "\n".join(render_timeline_stream(ctx.timeline))

    def _load_system_prompt(self) -> str:
        from qqbot.services.prompt_assembler import assemble

        return assemble("memory")

    # ─── LLM 折叠 ───

    def _build_user_text(
        self,
        *,
        cursor: _Cursor | None,
        rendered: str,
        count: int,
        covers_from: datetime,
        covers_until: datetime,
        max_chars: int,
    ) -> str:
        parts = ["<memory-compaction-input>"]
        if cursor is not None and cursor.summary:
            parts.append(f'<previous-summary revision="{cursor.folded_revision}">')
            parts.append(cursor.summary)
            parts.append("</previous-summary>")
        else:
            parts.append('<previous-summary empty="true"/>')
        parts.append(
            f'<events-to-fold count="{count}" '
            f'from="{covers_from.isoformat()}" '
            f'until="{covers_until.isoformat()}">'
        )
        parts.append(rendered)
        parts.append("</events-to-fold>")
        parts.append(f'<budget max_summary_chars="{max_chars}"/>')
        parts.append("</memory-compaction-input>")
        return "\n".join(parts)

    async def _summarize(
        self,
        *,
        scope_key: str,
        llm: Any,
        system_prompt: str,
        cursor: _Cursor | None,
        rendered: str,
        count: int,
        covers_from: datetime,
        covers_until: datetime,
        max_chars: int,
    ) -> tuple[str, list[str]] | None:
        """每次触顶只调用一次；超预算时句边界硬截断。

        调用失败 / 不可解析返回 None，不写摘要、不推进游标。
        """
        user_text = self._build_user_text(
            cursor=cursor,
            rendered=rendered,
            count=count,
            covers_from=covers_from,
            covers_until=covers_until,
            max_chars=max_chars,
        )
        parsed = await self._invoke_once(llm, system_prompt, user_text, scope_key)
        if parsed is None:
            return None
        summary, cues = parsed
        if len(summary) > max_chars:
            logger.warning(
                "[memory] {} 摘要 {} 字仍超上限 {}，句边界硬截断",
                scope_key,
                len(summary),
                max_chars,
            )
            summary = truncate_at_sentence(summary, max_chars)
        return summary, cues

    async def _invoke_once(
        self,
        llm: Any,
        system_prompt: str,
        user_text: str,
        scope_key: str,
    ) -> tuple[str, list[str]] | None:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_text),
        ]
        snapshot: PromptSnapshot | None = None
        if should_snapshot(scope_key):
            snapshot = PromptSnapshot(
                kind="memory_compaction",
                scope_key=scope_key,
                model=getattr(llm, "model_name", None) or getattr(llm, "model", None),
                system_prompt=system_prompt,
                user_text=user_text,
            )
        started = time.monotonic()
        try:
            if llm is None:
                from qqbot.services.event_gateway.outbound import invoke

                invoked = await invoke(
                    "memory", messages, extra={"scope_key": scope_key}
                )
                if not invoked.ok:
                    raise RuntimeError(invoked.error or "llm_unavailable")
                raw = invoked.raw
                text = invoked.text.strip()
            else:
                raw = await llm.ainvoke(messages)
                text = _extract_text(raw).strip()
        except Exception as exc:
            logger.warning(
                "[memory] {} LLM 调用失败: {}: {}",
                scope_key,
                type(exc).__name__,
                exc,
            )
            if snapshot is not None:
                snapshot.add_attempt(
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
                snapshot.outcome = "call_error"
                write_snapshot(snapshot)
            return None
        parsed = parse_compaction_output(text)
        if snapshot is not None:
            snapshot.add_attempt(
                latency_ms=int((time.monotonic() - started) * 1000),
                response_text=text,
                usage=extract_usage(raw),
            )
            snapshot.outcome = "ok" if parsed is not None else "parse_error"
            write_snapshot(snapshot)
        if parsed is None:
            logger.warning(
                "[memory] {} LLM 输出不可解析（前 200 字）: {!r}",
                scope_key,
                text[:200],
            )
        return parsed

    # ─── 事件写入 ───

    async def _write_recap(
        self,
        *,
        scope_key: str,
        summary: str,
        cues: list[str],
        snaps: list[Any],
        revision: int,
        skipped: int,
        correlation_id: str,
    ) -> str:
        covers_until_at = normalize_china_time(snaps[-1].occurred_at)
        payload = {
            "summary": summary,
            "recall_cues": cues,
            "covers_until_event_id": snaps[-1].event_id,
            "covers_until_occurred_at": covers_until_at.isoformat(),
            "covers_from_occurred_at": normalize_china_time(
                snaps[0].occurred_at
            ).isoformat(),
            "dropped_event_count": len(snaps),
            "skipped_event_count": skipped,
            "folded_revision": revision,
            "compactor_version": COMPACTOR_VERSION,
        }
        event_id = await write_runtime_event(
            self._session_factory,
            event_type=RECAP_EVENT_TYPE,
            scope_key=scope_key,
            visibility="agent_visible",
            # causation 指向覆盖边界事件——"因为窗口推进到了它才压缩"。
            correlation_id=correlation_id,
            causation_id=snaps[-1].event_id,
            payload=payload,
            # 回填到接缝处：渲染位置正好落在已折叠历史与保留尾巴之间
            # （边界语义以 covers_until_event_id 全序位置为准，契约 §2.2）。
            occurred_at=covers_until_at + timedelta(milliseconds=1),
        )
        if scope_key.startswith("group:"):
            from qqbot.services.group_memory_store import upsert_group_memory

            try:
                group_id = int(scope_key.split(":", 1)[1])
            except (TypeError, ValueError):
                group_id = None
            if group_id is not None:
                await upsert_group_memory(
                    self._session_factory,
                    group_id=group_id,
                    content=summary,
                )
        return event_id
