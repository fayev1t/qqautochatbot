"""Projector — build DecisionContext from the agent_events stream.

Contract: 开发文档/v2.0 —— 任务与决策契约.md §2.1、§8、§11；主线 Part 3 §2-§3

Strategy:
- Fetch the newest agent_visible events for this scope (count-limited window).
- Fold `agent.task_note_written` latest-wins into the single `<task>` note.
- Pair `agent.tool_called` with `agent.tool_result | agent.tool_failed`
  into ToolResultView。所属 decision 尚无 program terminal 时，半截调用
  折成 pending / 「已调用」；收口后的半截才是 interrupted。
  视图**只**用于渲染 timeline 的 <tool> 行——2026-07-02 起不再有独立的
  pending_tool_results 区，
  工具结果在 timeline 单点呈现（旧的双重渲染是复读诱饵）。
- Build the timeline from messages / notices / tool-call pairs / replies /
  agent-visible runtime hints. Task-note and tool-result events are folded
  upstream and do NOT produce timeline rows of their own.
- Render `agent.decision_emitted.payload.program` as ``<action>`` plus
  the full source. The later ``<program_result>`` row is a separate event.

Folding and rendering are split into pure staticmethods so unit tests
can drive them without a DB.

Renderers emit compact **line-grammar** rows（行文法，2026-08-03 起替换 XML
元素/属性渲染；不变量与安全模型见主线 Part 3 §2）。Each renderer:
- 只保留 XML 的承重基因：一切动态文本经 ``_esc_text``（`&`/`<`/`>`）转义，
  一切渲染器结构（行头 ``<msg>``/``<t>``/``<tool>``…、行内段 ``[img …>``…）
  以 ``<`` 开头——假行头/假段标记在**字符层**不可伪造。第二层防线是换行
  处理：多行容忍位缩进续行、单行字段位压平，动态内容到不了列 0。
- 行头短字段（名字/头衔/摘要）另做定界净化（半角→全角，`_head_field` /
  `_quote_excerpt`），行头文法内不残留用户可控定界字符。
- Rows carry **no timestamp of their own**（时间流契约 2026-07-26）：
  信封层用 ``render_timeline_stream`` 给相邻同秒的行共享一个 ``<t>``
  时刻头——时间是最外层结构，模型对每个事件的第一感知是"何时"。时刻头
  无闭合标签，同秒追加是纯追加；同日只渲染时分秒，跨日带完整日期，
  时区全局固定 Asia/Shanghai（envelope.md 约定，不逐节点渲染）。
- Walks OneBot V11 segments structurally (at / reply / image / face /
  poke / record / video / share / forward / ...) instead of dumping the
  raw CQ-code string — see `_render_segments` for the per-type contract.
  reply 段不再在正文渲染：上提为消息行头的 ``回复#ID(作者)「摘要」`` 标记。
- Serializes dict/list values as JSON (not Python repr).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import CHINA_TIMEZONE
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.decision import (
    DecisionContext,
    ImageRef,
    TimelineItem,
    ToolResultView,
)
from qqbot.services.agent_loop.event_writer import parse_scope_key

logger = get_logger(__name__)

# 待办便签事件（2026-08-21，渲染格式表 §一②）。与 tools/task.py 的
# TASK_NOTE_EVENT_TYPE 同值；这里不 import 那边——投影层不该依赖工具包。
TASK_NOTE_EVENT_TYPE = "agent.task_note_written"

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class _EventSnapshot:
    """Minimal row representation: avoids leaking the SQLAlchemy ORM into
    folding logic and lets tests build fixtures with plain dataclasses."""

    event_id: str
    occurred_at: datetime
    origin: str
    type: str
    scope: str
    group_id: int | None
    user_id: int | None
    visibility: str
    correlation_id: str | None
    causation_id: str | None
    payload: dict


def _snapshot_from_row(row: AgentEvent) -> _EventSnapshot:
    # asyncpg 把 TIMESTAMPTZ 列硬编码返回 UTC tzinfo（与 PG session
    # timezone 设置无关）。但人类可读输出必须用北京时间——这是项目契约：
    # 写入侧 china_now() 已是 +08:00，读出侧必须 normalize 回去，否则
    # timeline 渲染给 LLM 时会出现 "+00:00" 这种和数据库语义不一致的尾巴，
    # LLM 容易被它带歪（"现在凌晨1点，用户应该睡了" 实际是早上 9 点）。
    occurred_at = row.occurred_at
    if occurred_at is not None and occurred_at.tzinfo is not None:
        occurred_at = occurred_at.astimezone(CHINA_TIMEZONE)
    return _EventSnapshot(
        event_id=row.event_id,
        occurred_at=occurred_at,
        origin=row.origin,
        type=row.type,
        scope=row.scope,
        group_id=row.group_id,
        user_id=row.user_id,
        visibility=row.visibility,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        payload=dict(row.payload or {}),
    )


def _recap_boundary(recap: _EventSnapshot) -> tuple[datetime, str]:
    """recap 的覆盖边界 (occurred_at, event_id)。载荷缺字段/损坏时退化以
    recap 行自身为界——其之前的事件本就在窗口之外（记忆系统契约 §3.1）。"""
    payload = recap.payload or {}
    boundary_id = payload.get("covers_until_event_id")
    raw_at = payload.get("covers_until_occurred_at")
    boundary_at: datetime | None = None
    if isinstance(raw_at, str):
        try:
            boundary_at = datetime.fromisoformat(raw_at)
        except ValueError:
            boundary_at = None
    if not isinstance(boundary_id, str) or boundary_at is None:
        return recap.occurred_at, recap.event_id
    return boundary_at, boundary_id


class Projector:
    # 单条 tool_result 渲染上限：超过即截断尾部并加 <truncated/>。websearch
    # 等工具的 results 列表很容易爆掉 prompt token，必须兜底。
    # 2026-07-02 从 2048 上调：timeline 的 <tool> 行现在是工具结果的
    # **唯一**出口（pending-tool-results 区已删除——它曾是不截断的全量渲染，
    # 模型看长结果全靠它），不上调会让长 websearch 结果的可见部分缩水。
    MAX_TOOL_RESULT_CHARS = 6144

    # <task> 便签正文的渲染上限。与 tools/task.py MAX_TASK_CHARS 同值——
    # 工具侧已经拒绝超长写入，这里只是防御历史行与手工写入的库数据。
    MAX_TASK_NOTE_CHARS = 600

    # ─── 窗口锚定滞回（2026-07-12，前缀缓存契约）───
    # OpenAI 系 API 的自动前缀缓存要求前缀**逐字节一致**。若裁剪恒取"尾部
    # 正好 max 条"，活跃群每来一条消息窗口起点就前移一行，timeline 的缓存
    # 前缀每拍从起点断掉。改为锚定+滞回：起点钉在上一拍的首行（anchor），
    # 窗口放任增长到 max + SLACK 条才一次性前移回 max 条——起点每 SLACK 条
    # 新行才跳一次，其间各拍共享整段 timeline 前缀。锚失效（掉出取数窗 /
    # 重启丢内存态）时退回朴素裁剪并重新锚定，只多一次缓存 miss。
    TIMELINE_TRIM_SLACK = 30

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        max_items: int = 400,
        max_timeline_items: int = 100,
    ) -> None:
        self._session_factory = session_factory
        # 拉取 fetch 上限保持较大，给 fold_tool_results / 时间线折叠喂够事件；
        # 真正塞给 LLM 的 timeline 在 project() 里再裁到 max_timeline_items 条。
        # 2026-07-27 300→400：记忆系统不变式③（≥ 压缩触发阈值 250 × 1.5），
        # tick 侧未覆盖计数在阈值处不被截断、recap 正常时都在取数窗内。
        self._max_items = max_items
        self._max_timeline_items = max_timeline_items
        # scope_key → 上一拍渲染的 timeline 首行 event_id（窗口锚定滞回，
        # 见类常量注释）。纯内存态：重启即空，首拍走朴素裁剪重新锚定，
        # 代价只是一次缓存 miss，不落库。
        self._timeline_anchors: dict[str, str] = {}
        # 记忆压缩触顶探针（记忆系统契约 §4.2）：build_context 每拍投影后
        # 回调 (scope_key, 最新 recap 之后的事件数)。压缩器在通知入口校验
        # 阈值；启动/空闲期没有另一路扫描触发。未装配时零开销。
        self._uncovered_notifier: Callable[[str, int], None] | None = None

    def set_uncovered_notifier(
        self, notifier: Callable[[str, int], None] | None
    ) -> None:
        """装配/卸下记忆压缩探针（回调需廉价；异常由 build_context 兜住）。"""
        self._uncovered_notifier = notifier

    async def build_context(
        self,
        *,
        scope_key: str,
        correlation_id: str,
        tick_seq: int,
        now: datetime,
        bot_user_id: str | None = None,
    ) -> DecisionContext:
        scope, group_id, _ = parse_scope_key(scope_key)
        # 取数窗只按条数收敛（2026-07-27 去除 24h 时间回溯）：模型要能看到
        # 任意早的"最近 N 条"，安静 scope 的旧对话不因时间流逝而消失，退场
        # 唯一路径是被更新的事件挤出条数窗。窗口头部因此只在裁剪重锚时移动，
        # 前缀缓存反而更稳（原 cutoff 按小时取整的动机随之消失）。
        # 逻辑下界（记忆系统契约 §3.1）：最新 runtime.context_compacted
        # （含自身）——更早的事件已折叠进它携带的滚动摘要，不再投影。
        recap: _EventSnapshot | None = None
        recap_boundary: tuple[datetime, str] | None = None
        if scope == "group" and group_id is not None:
            recap = await self._fetch_latest_recap(group_id)
            recap = await self._overlay_group_memory(recap, group_id)
        if recap is not None:
            recap_boundary = _recap_boundary(recap)

        # ``now`` 不只是渲染时钟，也是本拍严格的投影快照上界。消息可能在
        # loop 捕获 now 之后、这条 SELECT 真正执行之前入库；若不设上界，
        # 它会混进本拍 context，却又排在 occurred_at=now 的
        # decision_emitted 之后，下一拍看起来像是本拍已经处理过。明确截到
        # now 后，这类消息留给其自己的 wake/tick 消费。
        events = await self._fetch(
            scope,
            group_id,
            now,
            lower_bound=recap_boundary[0] if recap_boundary else None,
        )
        if recap is not None:
            events = Projector.apply_recap_window(events, recap)
        # 记忆压缩触顶探针：报告"最新摘要之后"的事件数（≈未覆盖数，饱和
        # 于 max_items）。阈值由压缩器入口校验；异常绝不允许影响 tick。
        if self._uncovered_notifier is not None:
            try:
                uncovered = len(events) - (1 if recap is not None else 0)
                self._uncovered_notifier(scope_key, uncovered)
            except Exception as exc:
                logger.warning(
                    "[projection] uncovered notifier failed for {}: {}",
                    scope_key,
                    exc,
                )
        # bot_role 单独一次 SQL 查 —— runtime.bot_role_observed 可能远早于
        # 取数窗（比如启动 sweep 跑过一次就再没变，早被后续事件挤出条数
        # LIMIT），不应受取数窗影响。
        bot_role: str | None = None
        if scope == "group" and group_id is not None:
            bot_role = await self._fetch_latest_bot_role(group_id, bot_user_id)
        ctx = self.project(
            events,
            scope_key=scope_key,
            correlation_id=correlation_id,
            tick_seq=tick_seq,
            now=now,
            max_timeline_items=self._max_timeline_items,
            bot_user_id=bot_user_id,
            bot_role=bot_role,
            timeline_anchor=self._timeline_anchors.get(scope_key),
            pinned_event_id=recap.event_id if recap is not None else None,
        )
        # 记录本拍窗口锚：timeline 首行 = 下一拍的裁剪起点候选。钉住的
        # recap 行不做锚——它不参与裁剪计数，锚在它身上会让滞回每拍退回
        # 朴素裁剪。
        if ctx.timeline:
            for item in ctx.timeline:
                if recap is not None and item.event_id == recap.event_id:
                    continue
                self._timeline_anchors[scope_key] = item.event_id
                break
        # 便签跨窗口补全：窗口折叠只看最近 max_items 条，一段写下之后长期
        # 不改的便签会被水群挤出去、于是凭空消失。2026-08-21 之前这靠
        # agent_tasks 读模型表兜底；便签坍缩后不再有读模型（事件系统设计
        # §7.3），改用一条不受取数窗约束的 LIMIT 1 查询——与 bot_role /
        # <recall> 同一手法，且比原来的 CQRS 双写更省：没有可变表要维护。
        # 查询与窗口同上界，所以它的结果恒 ⊇ 窗口折叠值，可直接覆盖。
        # 失败时整段降级（保留窗口折叠结果），绝不让 tick 因补全失败而崩。
        ctx = await self._augment_with_task_note(ctx, scope_key, now)
        # 表情包收藏夹注入：查 agent_memes 挂到 ctx.saved_memes，llm_planner
        # 渲染成表情包收藏节（meme 工具凭 hash 前缀操作收藏的选图目录）。同样
        # best-effort 降级——查不到收藏夹只影响本 tick 发不了表情包。
        ctx = await self._augment_with_saved_memes(ctx, scope_key)
        # _augment_with_pending_reply 已于 2026-07-24 删除（待办#19），它服务的
        # 整套 reply / ReplyTask 又于 2026-08-17 随提案-裁决流水线删除。Planner
        # 信封没有独立状态区：一切都在 timeline 上。
        return ctx

    async def _augment_with_task_note(
        self, ctx: DecisionContext, scope_key: str, upper_bound: datetime
    ) -> DecisionContext:
        try:
            note = await self._fetch_latest_task_note(scope_key, upper_bound)
        except Exception as exc:  # 查询失败 → 降级为纯窗口折叠
            logger.warning(
                "[projection] load task note failed for {}: {}", scope_key, exc
            )
            return ctx
        if note == ctx.task_note:
            return ctx
        from dataclasses import replace

        return replace(ctx, task_note=note)

    async def _fetch_latest_task_note(
        self, scope_key: str, upper_bound: datetime
    ) -> str | None:
        """查该 scope 最新一条 ``agent.task_note_written`` 的正文。

        不走 ``_fetch`` 的条数取数窗：便签可能几百条消息之前写下、此后一直
        没改，硬依赖取数窗会让它凭空消失——那正是 2026-08-21 之前要靠
        ``agent_tasks`` 读模型表解决的问题。上界与窗口一致（``context.now``），
        所以本查询结果恒 ⊇ 窗口折叠值。

        空正文是**合法值**，语义是"便签已清空"，返回 ``None``（与"从没写过"
        同一渲染结果：整节不出现）。
        """
        from sqlalchemy import desc

        scope, group_id, user_id = parse_scope_key(scope_key)
        stmt = (
            select(AgentEvent.payload)
            .where(AgentEvent.type == TASK_NOTE_EVENT_TYPE)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.occurred_at <= upper_bound)
        )
        if group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        if user_id is not None:
            stmt = stmt.where(AgentEvent.user_id == user_id)
        stmt = stmt.order_by(
            desc(AgentEvent.occurred_at), desc(AgentEvent.event_id)
        ).limit(1)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            payload = result.scalars().first()
        return _task_note_of(payload)

    async def _augment_with_saved_memes(
        self, ctx: DecisionContext, scope_key: str
    ) -> DecisionContext:
        """收藏夹注入：全局 agent_memes → ctx.saved_memes。

        收藏夹全 bot 一份、所有聊天 scope 共用（事件系统设计 §11.3 例外，
        见 meme_store 模块 docstring），查询不带 scope 过滤；scope_key 只用来
        判断"有没有聊天面"——system scope 没有（meme 工具的
        allowed_scopes 也不含它），跳过查询省一次 SQL。查询失败整段降级
        （本 tick 不渲染表情包收藏节，模型只是暂时"想不起收藏"），绝不让
        tick 崩。
        """
        if not scope_key.startswith(("group:", "private:")):
            return ctx
        try:
            from qqbot.services.agent_loop.meme_store import load_saved_memes

            memes = await load_saved_memes(self._session_factory)
        except Exception as exc:  # 读表失败 → 降级为无收藏夹
            logger.warning(
                "[projection] load saved memes failed for {}: {}",
                scope_key,
                exc,
            )
            return ctx
        if not memes:
            return ctx
        from dataclasses import replace

        return replace(ctx, saved_memes=memes)

    async def _fetch_latest_bot_role(
        self,
        group_id: int,
        bot_user_id: str | None,
    ) -> str | None:
        """查该群最新一条 runtime.bot_role_observed。

        不走 _fetch 的条数取数窗与 agent_visible 过滤：
        - 窗口：bot 角色可能很久不变，sweep 后几个月才有 group_admin 事件触发
          下一次写入，这条老事件早被后续事件挤出 LIMIT，硬依赖取数窗会让
          bot_role 凭空消失。
        - visibility：runtime.bot_role_observed 默认 agent_visible，但即使未来
          调成 runtime_only 也应能取到——这是事实数据，不是给 LLM 的渲染数据。

        ``bot_user_id`` 用来在多账号场景下只取本 bot 自己的 baseline；为 None
        时不过滤 self_id（单 bot 场景 / 启动初期 bot_registry 还空）。
        """
        from sqlalchemy import desc

        stmt = (
            select(AgentEvent)
            .where(AgentEvent.type == "runtime.bot_role_observed")
            .where(AgentEvent.scope == "group")
            .where(AgentEvent.group_id == group_id)
            .order_by(desc(AgentEvent.occurred_at))
            .limit(10)  # 取最近 10 条，应用层按 self_id 过滤后取首条
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        for row in rows:
            payload = row.payload or {}
            self_id = payload.get("self_id")
            if bot_user_id is None or self_id is None or str(self_id) == bot_user_id:
                role = payload.get("role")
                if isinstance(role, str) and role.strip():
                    return role.strip().lower()
        return None

    async def _fetch_latest_recap(self, group_id: int) -> _EventSnapshot | None:
        """查该群最新一条 runtime.context_compacted（滚动记忆载体）。

        与 bot_role 同理走独立查询、不受取数 LIMIT 约束：积压超过
        max_items 时（压缩器暂时追不上），记忆也不能凭空消失
        （记忆系统契约 §3.2 保底）。"""
        from sqlalchemy import desc

        stmt = (
            select(AgentEvent)
            .where(AgentEvent.type == "runtime.context_compacted")
            .where(AgentEvent.scope == "group")
            .where(AgentEvent.group_id == group_id)
            .order_by(desc(AgentEvent.occurred_at), desc(AgentEvent.event_id))
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return _snapshot_from_row(rows[0]) if rows else None

    async def _overlay_group_memory(
        self, recap: _EventSnapshot | None, group_id: int
    ) -> _EventSnapshot | None:
        """读侧 ``group_memories`` 覆盖摘要正文；失败或空行回退事件 payload。"""
        if recap is None:
            return None
        try:
            from qqbot.services.group_memory_store import load_group_memory

            content = await load_group_memory(self._session_factory, group_id)
        except Exception as exc:
            logger.warning(
                "[projection] load group_memories failed group_id={}: {}",
                group_id,
                exc,
            )
            return recap
        if not isinstance(content, str) or not content.strip():
            return recap
        from dataclasses import replace

        payload = dict(recap.payload or {})
        payload["summary"] = content
        return replace(recap, payload=payload)

    async def _fetch(
        self,
        scope: str,
        group_id: int | None,
        upper_bound: datetime,
        lower_bound: datetime | None = None,
    ) -> list[_EventSnapshot]:
        # 无时间回溯下界（2026-07-27 去除 24h 回溯）：窗口只按 LIMIT 收敛，
        # (scope, group_id, occurred_at) 索引倒序扫，表再大也只摸最新
        # max_items 行。lower_bound 是唯一的逻辑下界：最新 recap 的覆盖
        # 边界（粗滤——同时刻残留由 apply_recap_window 按 event_id 精滤）。
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.visibility == "agent_visible")
            .where(AgentEvent.occurred_at <= upper_bound)
        )
        if lower_bound is not None:
            stmt = stmt.where(AgentEvent.occurred_at >= lower_bound)
        if scope == "group" and group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        # event_id 是 ULID；同一 occurred_at 下用它做稳定次序，reverse 后仍是
        # 正向时间序，避免时间戳相同时窗口边缘与行位置随机抖动。
        stmt = stmt.order_by(
            AgentEvent.occurred_at.desc(), AgentEvent.event_id.desc()
        ).limit(self._max_items)

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        # Reverse to chronological order for downstream folding.
        return [_snapshot_from_row(row) for row in reversed(rows)]

    # ─── Pure projection: testable without DB ───

    @staticmethod
    def apply_recap_window(
        events: Sequence[_EventSnapshot], recap: _EventSnapshot
    ) -> list[_EventSnapshot]:
        """窗口下界精滤（记忆系统契约 §3.1/§3.2）：丢弃全序 ≤ 覆盖边界的
        事件（含更老的 recap 代次），recap 自身保底在场——积压超过取数
        LIMIT 时它会缺席取数结果，此时前插（记忆永不消失）。"""
        boundary = _recap_boundary(recap)
        kept = [ev for ev in events if (ev.occurred_at, ev.event_id) > boundary]
        if all(ev.event_id != recap.event_id for ev in kept):
            kept.insert(0, recap)
        return kept

    @staticmethod
    def project(
        events: Sequence[_EventSnapshot],
        *,
        scope_key: str,
        correlation_id: str,
        tick_seq: int,
        now: datetime,
        max_timeline_items: int | None = None,
        bot_user_id: str | None = None,
        bot_role: str | None = None,
        timeline_anchor: str | None = None,
        pinned_event_id: str | None = None,
    ) -> DecisionContext:
        task_note = Projector.fold_task_note(events)
        # tool_views 只喂给 timeline 渲染（<tool> 行按两态折叠）；不再
        # 另出 pending_tool_results 区——同一调用双重渲染曾是复读的直接诱饵。
        tool_views = Projector.fold_tool_results(events)
        timeline = Projector.build_timeline(
            events, tool_views=tool_views, bot_user_id=bot_user_id
        )
        # 裁到尾部 max_timeline_items 条 —— fetch 上限给得宽是为了 fold 任务/
        # 工具结果时能看到足够长的事件链，但塞给 LLM 的不必那么多。
        # timeline_anchor（上一拍窗口首行）有效时起点滞回钉住，见
        # _trim_timeline。
        if max_timeline_items is not None:
            # recap 行钉住（记忆系统契约 §3.2）：摘出裁剪再前插——既不占
            # max_timeline_items 行预算，也永不被裁掉（借 MaiBot 的
            # count_in_context=False 思路：记忆不得挤占真实对话的窗口配额）。
            pinned: TimelineItem | None = None
            if pinned_event_id is not None:
                pinned = next(
                    (item for item in timeline if item.event_id == pinned_event_id),
                    None,
                )
            if pinned is not None:
                rest = [item for item in timeline if item is not pinned]
                rest = Projector._trim_timeline(
                    rest, max_timeline_items, timeline_anchor
                )
                timeline = [pinned, *rest]
            else:
                timeline = Projector._trim_timeline(
                    timeline, max_timeline_items, timeline_anchor
                )
        # 如果 caller 没单独传 bot_role（pure project() 测试常常如此），尝试从
        # 事件列表里 fold 一次——支持纯函数测试不需要 DB 也能验证 fold 逻辑。
        if bot_role is None:
            bot_role = Projector.fold_bot_role(events, bot_user_id=bot_user_id)
        # 类型上 DecisionContext.bot_role 是 Literal[...]，但跑期我们对未知值
        # 一律 None（防止 LLM 拿到"垃圾角色字符串"做判断）。
        normalized_role: str | None = None
        if isinstance(bot_role, str):
            low = bot_role.strip().lower()
            if low in _BOT_ROLES:
                normalized_role = low
        return DecisionContext(
            scope_key=scope_key,
            correlation_id=correlation_id,
            tick_seq=tick_seq,
            now=now,
            timeline=timeline,
            task_note=task_note,
            bot_user_id=bot_user_id,
            bot_role=normalized_role,  # type: ignore[arg-type]
        )

    @staticmethod
    def _trim_timeline(
        timeline: list[TimelineItem],
        max_items: int,
        anchor: str | None,
    ) -> list[TimelineItem]:
        """尾部裁剪 + 窗口锚定滞回（前缀缓存契约，见类常量注释）。

        朴素裁剪 = 保留尾部 ``max_items`` 行。给了 ``anchor``（上一拍窗口
        首行的 event_id）且它仍在窗内、锚起的行数未超
        ``max_items + TIMELINE_TRIM_SLACK`` 时，起点钉在锚上不动——各拍共享
        同一窗口起点，timeline 前缀逐字节稳定；超出滞回带或锚已失效（掉出
        取数窗 / 重启）则退回朴素裁剪，由 caller 重新锚定。
        """
        if max_items <= 0:
            return []
        naive = max(0, len(timeline) - max_items)
        if naive == 0:
            return timeline  # 不足预算（或起点已是首行），整段保留
        if anchor:
            # 锚只可能在朴素起点或更早（窗口只会向前追加）；更新的"锚"说明
            # 状态异常（如配置变更），忽略之走朴素裁剪。
            for idx in range(naive + 1):
                if timeline[idx].event_id != anchor:
                    continue
                kept_rows = len(timeline) - idx
                if kept_rows <= max_items + Projector.TIMELINE_TRIM_SLACK:
                    return timeline[idx:]
                break  # 超出滞回带：一次性前移回朴素起点
        return timeline[naive:]

    # fold_unseen_message_ids / `<message unseen="true">` 的第一拍判定
    # **2026-08-02 删除**（2026-07-06 引入、07-24 删除、07-28 复活，这是第
    # 三次翻转；勿再复活，除非带着下面没被覆盖的新论据）。
    #
    # 删除时明知系统里不再有任何"这几条是本拍第一次看到"的显式信号：
    # decision_emitted 与 idle_decision 都不投影，`<my-thought>` 行位置判据
    # 已随 2026-08-01 reasoning 回显删除退役，`<current unread="N">` 聚合
    # 计数 07-28 复活本机制时一并删掉。idle 拍因此在时间线上零痕迹。
    #
    # 论据：① 07-24 删除时的两条理由从未被推翻，07-28 只是权衡压过——
    # 二值标签造成锚定（模型只在带标签的行里找发言理由），且一次性（一条
    # 消息只享有一拍的"值得看"，那拍观望就永久降级）；② 前缀缓存代价当时
    # 是知情接受的：逐拍翻转把密集聊天的命中率从 65-68% 压到 38-43%，而
    # 密集聊天正是它想服务的场景；③ 08-01 planner.md 重构后 `# 决策要求`
    # 的"回看"条目已在政策层承担同一职责，不再需要投影层的结构性标记。
    #
    # 连带说明：agent.decision_emitted.occurred_at 仍回填为本拍投影时刻
    # （loop._tick），理由已不再是水位线，而是与同拍其他决策产物
    # （tool_called / idle_decision / task_*）保持同刻——见
    # 事件系统设计.md §时间戳约束。别顺手改回写入时刻。

    @staticmethod
    def fold_bot_role(
        events: Iterable[_EventSnapshot],
        *,
        bot_user_id: str | None = None,
    ) -> str | None:
        """Pure fold: 从事件序列里取最新一条 runtime.bot_role_observed.role。

        多账号过滤：payload.self_id 必须等于 bot_user_id；bot_user_id 为 None
        时不过滤（单 bot 部署）。事件被假设为升序排列（与 _fetch 返回一致），
        因此最后一条匹配即为"最新"。
        """
        latest_role: str | None = None
        for ev in events:
            if ev.type != "runtime.bot_role_observed":
                continue
            payload = ev.payload or {}
            self_id = payload.get("self_id")
            if (
                bot_user_id is not None
                and self_id is not None
                and str(self_id) != bot_user_id
            ):
                continue
            role = payload.get("role")
            if isinstance(role, str) and role.strip():
                latest_role = role.strip().lower()
        return latest_role

    # ─── Folding helpers ───

    @staticmethod
    def fold_task_note(events: Iterable[_EventSnapshot]) -> str | None:
        """窗口内最新一条 ``agent.task_note_written`` 的正文（latest-wins）。

        纯函数（无 DB），便于单测；生产路径另有
        ``_fetch_latest_task_note`` 兜住窗口外的旧便签。事件流本身有序，
        所以直接取最后一条命中即可，不比较时刻。

        空正文（模型传了 ``content=""``）语义是"便签已清空"，返回 None——与
        "从没写过"同一渲染结果：整节不出现。清空是一次**真实的覆写**，它必须
        压掉更早那版有内容的便签，所以不能写成"跳过空值继续往前找"。

        2026-08-21 取代 ``fold_tasks``（渲染格式表 §一②）：没有 ID、没有状态
        机、没有父子层级、没有在途调用集合，因而也没有 done/failed 要过滤。
        """
        latest: str | None = None
        for ev in events:
            if ev.type != TASK_NOTE_EVENT_TYPE:
                continue
            latest = _task_note_of(ev.payload)
        return latest

    @staticmethod
    def fold_tool_results(
        events: Iterable[_EventSnapshot],
    ) -> list[ToolResultView]:
        """Pair every tool call with its terminal result/failure.

        所属 decision 尚无 program terminal 时，无终态的 ``tool_called`` 折成
        ``pending``（渲染「已调用」）。窗口内已有 program terminal、调用本身
        仍无 terminal，才防御性折成 ``interrupted`` / ``uncertain``。
        """
        closed_decisions = {
            str(ev.causation_id)
            for ev in events
            if ev.type in ("agent.program_completed", "agent.program_failed")
            and ev.causation_id
        }
        calls: dict[str, dict] = {}
        for ev in events:
            if ev.type == "agent.tool_called":
                tc_id = ev.payload.get("tool_call_id")
                if not tc_id:
                    continue
                decision_id = str(ev.causation_id or "")
                in_flight = decision_id and decision_id not in closed_decisions
                if in_flight:
                    error_kind = "pending"
                    error_message = None
                    error_extra = None
                else:
                    error_kind = "interrupted"
                    error_message = (
                        "tool call has no terminal; delivery state is uncertain"
                    )
                    error_extra = {"status": "uncertain"}
                calls[tc_id] = {
                    "tool_call_id": tc_id,
                    "tool_name": ev.payload.get("tool_name", ""),
                    "arguments": dict(ev.payload.get("arguments") or {}),
                    "result": None,
                    "error_kind": error_kind,
                    "error_message": error_message,
                    "error_extra": error_extra,
                }
            elif ev.type == "agent.tool_result":
                tc_id = ev.payload.get("tool_call_id")
                if tc_id in calls:
                    calls[tc_id]["result"] = ev.payload.get("result")
                    calls[tc_id]["error_kind"] = None
                    calls[tc_id]["error_message"] = None
                    calls[tc_id]["error_extra"] = None
            elif ev.type == "agent.tool_failed":
                tc_id = ev.payload.get("tool_call_id")
                if tc_id in calls:
                    calls[tc_id]["error_kind"] = (
                        ev.payload.get("error_kind") or "unknown"
                    )
                    calls[tc_id]["error_message"] = ev.payload.get("error_message")
                    calls[tc_id]["error_extra"] = _extract_error_extra(ev.payload)
        return [ToolResultView(**d) for d in calls.values()]

    @staticmethod
    def build_timeline(
        events: Sequence[_EventSnapshot],
        *,
        tool_views: Sequence[ToolResultView],
        bot_user_id: str | None = None,
    ) -> list[TimelineItem]:
        """``bot_user_id`` 用于给行内出现的本账号 QQ 号打 ``*`` 后缀（服务端
        标注，Part 3 §2.2）；None 时不标（纯函数测试 / 启动初期），此时
        reply 标记的 ``*`` 仍由 from_self 服务端事实兜底。"""
        tool_view_by_id = {tv.tool_call_id: tv for tv in tool_views}
        # 预扫一遍构建 reply 段引用所需的索引（被回复消息摘要 + 用户名映射），
        # 让单条消息渲染时无需再遍历全部事件。
        excerpt_by_msg_id = _build_excerpt_index(events)
        name_by_user_id = _build_user_name_index(events)
        # author_by_msg_id：被回复消息的作者（_AuthorRef）。reply 段渲染时据此
        # 标 from_name/from_qq/from_self 三个独立属性，让 LLM 一眼看清"是 B 在
        # 引用某人"而不是"某人在发言"——这是 addressee 误判（把别人引用你当成
        # 你说话）的根因修复。覆盖外部消息 + bot 自己已投递的发言（后者标
        # from_self="true"，无需比对 bot_qq 即知"别人引用的是你自己"）。
        author_by_msg_id = _build_author_index(events)
        # 过期完成事件的渲染守卫（reply/ReplyTask 2026-08-17 已删除，此处只
        # 服务存量行）：写入侧当年已在 scope 锁内复核，这里是投影侧的第二道
        # 防线——更低 revision 的 completed、以及 cancelled 任务上迟到的
        # completed 不渲染。
        reply_task_guard = _build_reply_task_guard(events)

        items: list[TimelineItem] = []
        for ev in events:
            if ev.type in (
                "agent.program_completed",
                "agent.program_failed",
            ):
                rendered = Projector._render_program(ev)
                if rendered is not None:
                    items.append(
                        TimelineItem(
                            event_id=ev.event_id,
                            occurred_at=ev.occurred_at,
                            kind="program",
                            render=rendered,
                        )
                    )
                continue
            if ev.type.startswith("agent.task_"):
                # 便签折进顶部 <task> 单栏，不进时间线（渲染格式表 §一②：
                # 反思要历史，便签只要现状）。
                #
                # 2026-08-21 起 `<task_closed>` 行型一并删除：它渲染的是
                # agent.task_state_changed(done|failed)，而状态机本身没了。
                # 库里的存量 task_created / task_state_changed /
                # task_progress_noted 行落到这条分支上静默消隐——它们描述的是
                # 一套已经不存在的结构，逐字渲染出来只会让她照着一个没有的
                # 工具形态去用 task()。
                continue
            if ev.type in ("agent.tool_result", "agent.tool_failed"):
                # rendered alongside the matching tool_called row。
                # send_messages 也不例外（2026-07-31 实施后调整，维护者拍板）：
                # 调用行的 <args> + <result> 逐条回执就是发言记录，不派生
                # 第二行发言记录——同一句话两处渲染是复读诱饵。终态
                # receipts 仍被 _build_author_index 消费（别人引用 bot 时标
                # from_self）。
                continue
            if ev.type in (
                "agent.reply_task_upserted",
                "agent.reply_task_cancelled",
            ):
                # 领域事件消隐：同一次授权已由它的 <tool>reply 行
                # 完整呈现（<args> 授权原文 + <result> 调度事实），再渲染一遍
                # 就是双重渲染。它们仍是 reply_task 折叠的数据源，只是不进
                # timeline。
                continue
            if ev.type == "agent.reflection_written":
                # 2026-08-21 时间线化：不再消隐折成 `## 反思` 一节，而是逐版
                # 留在时间线上。latest-wins 全量覆写会让历史各版彻底消失，
                # 模型看不到自己认识是怎么变过来的；那套折叠器交给 task 便签。
                #
                # 与 2026-08-01 删除的 <my-thought> 的边界（勿在此扩容）：那次
                # 删的是**程序注释**逐拍原样回灌，现在仍然如此（注释只经
                # get_recent_thoughts 主动读回）。这里铺开的是 reflect 显式
                # 写下、有意留给将来的结论，低频且有字数上限，不是逐拍笔记。
                rendered = Projector._render_reflection(ev)
                if rendered is not None:
                    items.append(
                        TimelineItem(
                            event_id=ev.event_id,
                            occurred_at=ev.occurred_at,
                            kind="reflection",
                            render=rendered,
                        )
                    )
                continue
            if ev.type == "agent.invalid_action":
                # 注册层拦截（2026-08-21，渲染格式表 §一⑦）。它**回灌被拒源码**：
                # 模型看得见自己写错的那一段才谈得上自纠正，这推翻了 2026-08-11
                # 「不回灌」的旧决议。取代已废止的 runtime.llm_invalid_output。
                rendered = Projector._render_invalid_action(ev)
                if rendered is not None:
                    items.append(
                        TimelineItem(
                            event_id=ev.event_id,
                            occurred_at=ev.occurred_at,
                            kind="invalid_action",
                            render=rendered,
                        )
                    )
                continue
            if ev.type == "agent.background_noted":
                # 每日群聊背景（2026-08-21，渲染格式表 §一①）：原信封头部的
                # 折叠快照下沉为时间线事实事件，于是"群叫这个名字"也有了发生
                # 时刻与先后。写入方是 daily_background，不是任何一拍。
                rendered = Projector._render_background(ev)
                if rendered is not None:
                    items.append(
                        TimelineItem(
                            event_id=ev.event_id,
                            occurred_at=ev.occurred_at,
                            kind="background",
                            render=rendered,
                        )
                    )
                continue
            if ev.type == "agent.decision_emitted":
                rendered = Projector._render_decision(ev)
                if rendered is not None:
                    items.append(
                        TimelineItem(
                            event_id=ev.event_id,
                            occurred_at=ev.occurred_at,
                            kind="program",
                            render=rendered,
                        )
                    )
                continue
            if ev.type in (
                "agent.reply_emitted",
                "agent.reply_delivered",
                "agent.reply_failed",
                "agent.idle_decision",
            ):
                # reply_emitted/delivered/failed 是历史链路；现役发送事实为
                # runtime.reply_flushed。这里继续跳过旧库遗留事件。idle_decision
                # 是纯运营事件，仍消隐。
                continue

            if ev.type == "agent.tool_called":
                tc_id = ev.payload.get("tool_call_id")
                tv = tool_view_by_id.get(tc_id)
                # 工具调用行一律不折叠：每一次调用及其终态都是 Planner 回看
                # 自己做过什么的时间线记录。
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="tool_call",
                        render=Projector._render_tool_call(ev, tv),
                        related_event_ids=[],
                    )
                )
            elif ev.type.startswith("external.message."):
                render, images = Projector._render_message(
                    ev,
                    excerpt_by_msg_id,
                    name_by_user_id,
                    author_by_msg_id,
                    bot_user_id=bot_user_id,
                )
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="message",
                        render=render,
                        images=images,
                    )
                )
            elif ev.type.startswith("external.notice."):
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="notice",
                        render=Projector._render_notice(
                            ev, name_by_user_id, bot_user_id=bot_user_id
                        ),
                    )
                )
            elif ev.type.startswith("external.request."):
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="request",
                        render=Projector._render_request(ev),
                    )
                )
            elif ev.type == "runtime.reply_flushed":
                # 旧链路历史事件（2026-07-31 起新链路不写 flushed；现役发言
                # 记录是 send_messages 的 <tool> 行）。
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="my_reply",
                        render=Projector._render_reply_flushed(ev),
                    )
                )
            elif ev.type == "runtime.reply_task_completed":
                if _completed_is_stale(ev, reply_task_guard):
                    continue
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="reply_task_completed",
                        render=Projector._render_reply_task_completed(ev),
                    )
                )
            elif ev.type == "runtime.context_compacted":
                # 滚动记忆摘要行（记忆系统契约 §3.3）：专门渲染，不落
                # runtime JSON 兜底——那会把 recall_cues 等内部字段一起
                # 倾倒进 prompt。
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="system_hint",
                        render=Projector._render_context_recap(ev),
                    )
                )
            elif ev.type == "runtime.event_ingest_failed":
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="system_hint",
                        render=Projector._render_ingest_failure(ev),
                    )
                )
            elif ev.type.startswith("runtime.") and ev.visibility == "agent_visible":
                items.append(
                    TimelineItem(
                        event_id=ev.event_id,
                        occurred_at=ev.occurred_at,
                        kind="system_hint",
                        render=Projector._render_runtime(ev),
                    )
                )
            # silently drop anything else
        return items

    # ─── Renderers ───

    @staticmethod
    def _render_message(
        ev: _EventSnapshot,
        excerpt_by_msg_id: dict[str, str],
        name_by_user_id: dict[str, str],
        author_by_msg_id: "dict[str, _AuthorRef] | None" = None,
        *,
        bot_user_id: str | None = None,
    ) -> tuple[str, list[ImageRef]]:
        """消息行：``<msg>名字(QQ[/身份][/匿名][/「头衔」]) #消息ID
        [回复#ID(作者)「摘要」]: 正文``。

        行头（``<msg>`` 到第一个 ``: ``）是渲染器领地：名字/头衔经
        ``_head_field`` 定界净化，reply 段从正文**上提**为行头标记（行文法
        §5.2——被引内容属于作者、新文本属于发送者，位置上先于正文更不易
        误认）。正文 = 其余段的混排，时刻由外层 ``<t>`` 头承载。缺哪个字段
        省哪个（=未知），不造占位。
        """
        sender = ev.payload.get("sender") or {}
        name = sender.get("card") or sender.get("nickname")
        qq = sender.get("user_id") or ev.user_id
        # 匿名群消息（OneBot 标准字段；napcat 不支持匿名、恒缺失）：发送者
        # 顶着匿名马甲，名字退到匿名昵称，括号槽标「匿名」让 LLM 知道这
        # 名字不是真实群成员身份。anonymous.flag 是禁言凭证，只入库不渲染
        # （凭证不经 LLM，与 request.flag 同策略）。
        anonymous = ev.payload.get("anonymous")
        if not isinstance(anonymous, dict):
            anonymous = None
        if not name and anonymous:
            anon_name = anonymous.get("name")
            if anon_name:
                name = str(anon_name)
        msg_id = ev.payload.get("onebot_message_id") or ""

        segments = ev.payload.get("segments") or []
        # reply 段上提：正文渲染前摘出，行头标记与正文分开构造。
        reply_segs = [
            seg
            for seg in segments
            if isinstance(seg, dict) and seg.get("type") == "reply"
        ]
        rest_segs = [
            seg
            for seg in segments
            if not (isinstance(seg, dict) and seg.get("type") == "reply")
        ]
        body, images = _render_segments(
            rest_segs,
            excerpt_by_msg_id,
            name_by_user_id,
            author_by_msg_id,
            bot_user_id=bot_user_id,
        )
        # raw_message 兜底：mapper 上游异常时 segments 可能为空但 raw_message 还在
        if not body and not reply_segs:
            raw = ev.payload.get("raw_message", "")
            if raw:
                body = _ml_text(str(raw))

        # 括号槽：QQ 号（本账号打 *）/ 身份（仅 管理员|群主）/ 匿名 / 头衔。
        # member 是绝大多数，逐条渲染纯耗 token；缺省语义（普通成员或未知）
        # 由 envelope.md 写死，无歧义。
        slots: list[str] = []
        if qq is not None:
            slots.append(_qq_disp(qq, bot_user_id))
        role = str(sender.get("role") or "").strip().lower()
        if role == "owner":
            slots.append("群主")
        elif role == "admin":
            slots.append("管理员")
        if anonymous:
            slots.append("匿名")
        title = str(sender.get("title") or "").strip()
        if title:
            slots.append(f"「{_head_field(title)}」")
        name_part = _head_field(str(name)) if name else ""
        if slots:
            name_part += f"({'/'.join(slots)})"

        head = f"<msg>{name_part}"
        # #消息ID：与工具参数 message_id / 出站 reply 段 data.id 同域。
        if msg_id:
            head += (" " if name_part else "") + f"#{_head_field(str(msg_id))}"
        for seg in reply_segs:
            marker = _render_reply_marker(
                seg,
                excerpt_by_msg_id,
                author_by_msg_id,
                bot_user_id=bot_user_id,
            )
            if marker:
                head += f" {marker}"
        return (f"{head}: {body}" if body else f"{head}:"), images

    @staticmethod
    def _render_notice(
        ev: _EventSnapshot,
        name_by_user_id: dict[str, str] | None = None,
        *,
        bot_user_id: str | None = None,
    ) -> str:
        """通知行：``<notice>kind 模板句``（Part 3 §3.1）。

        kind 保留 OneBot 原始枚举词作锚，正文是逐 kind 的模板句——比属性堆
        可读且更省。人物一律 ``名(QQ)`` 形态（近期消息反查名字，查不到只渲
        染号；本账号打 ``*``）。mapper 已入库的 kind 专属明细（禁言秒数/
        新旧名片/文件名/表情统计/被撤回消息 ID…）全部进句子，不丢。模板拼
        不出（未识别 kind / 关键字段全缺）→ 载荷 JSON 兜底，事实不消失。
        """
        names = name_by_user_id or {}
        kind = ev.type.replace("external.notice.", "")
        sentence = _notice_sentence(kind, ev, names, bot_user_id=bot_user_id)
        if sentence is None:
            sentence = _esc_text(_safe_json(ev.payload or {}))
        return f"<notice>{_esc_text(kind)} {sentence}"

    @staticmethod
    def _render_request(ev: _EventSnapshot) -> str:
        """渲染 external.request.*。

        2026-07-03 拆分后实际会渲染的只有入群申请（``external.request.group.add``，
        scope=group 进目标群 timeline）：群内 LLM 看到后可提醒，管理员明确授权后
        调 respond_to_group_join_request 回执 napcat（EventIngest契约.md §2）。
        好友申请 / 邀请入群是 runtime_only（自动审批），永远不会走到这里；渲染
        逻辑仍按 type 前缀泛化，不对 kind 特判。

        关键：渲染必须带 event_id —— LLM 调工具时回填它，工具用 event_id 反查
        事件 payload 里的 flag，这样 napcat 的 flag 凭证不经过 LLM 复述，避免
        长串照抄出错。comment 是申请人填的验证留言（提醒/决策的主要依据）。
        """
        parts = [f"<join_request>ev:{_head_field(str(ev.event_id))}"]
        if ev.user_id is not None:
            parts.append(f"申请人({ev.user_id})")
        comment = ev.payload.get("comment")
        if comment:
            parts.append(f"留言{_quote_excerpt(str(comment), limit=200)}")
        # group_id 恒为当前群（scope=group 进目标群 timeline），不渲染——
        # Part 3 §3.1 裁定的冗余属性删除。
        return " ".join(parts)

    @staticmethod
    def _render_tool_call(ev: _EventSnapshot, tv: ToolResultView | None) -> str:
        """工具行（Part 3 §3.2）：

        ``<tool>名 完成|失败[ kind k=v …]`` + 缩进的
        ``参数`` / ``结果``（成功，超限加 ``（截断）``）/ ``原因``（失败）行。
        发起时刻由外层 ``<t>`` 时刻头承载。

        ``send_messages`` 特例（承接 2026-07-31「一次发送只渲染一处」与
        2026-08-01 人话渲染两裁定）：参数/结果不渲 JSON，改为逐气泡一行
        ``「内容」→回执``——回执三态 ``→#消息ID``（送达）/``→失败``/``→存疑``，
        失败行头是 ``失败 <error_kind> [status=…]``。终态回执缺失（旧事件/
        形状不识）时退回通用 JSON 行，事实不消失。
        """
        name = str(ev.payload.get("tool_name", "?"))
        args = ev.payload.get("arguments", {})
        args_line = f"  参数 {_esc_text(_safe_json(args))}"
        if tv is None:
            tv = ToolResultView(
                tool_call_id=str(ev.payload.get("tool_call_id") or ""),
                tool_name=name,
                arguments=dict(args) if isinstance(args, dict) else {},
                result=None,
                error_kind="interrupted",
                error_message=(
                    "tool call has no terminal; delivery state is uncertain"
                ),
                error_extra={"status": "uncertain"},
            )

        if name == "send_messages":
            special = _render_send_messages_call(args, tv)
            if special is not None:
                return special

        if tv.error_kind == "pending":
            return f"<tool>{_esc_text(name)} 已调用\n{args_line}"

        if tv.error_kind is None:
            result_json = _safe_json(tv.result)
            truncated = ""
            if len(result_json) > Projector.MAX_TOOL_RESULT_CHARS:
                result_json = result_json[: Projector.MAX_TOOL_RESULT_CHARS]
                truncated = "（截断）"
            return (
                f"<tool>{_esc_text(name)} 完成\n{args_line}\n"
                f"  结果 {_esc_text(result_json)}{truncated}"
            )
        head_extra = _error_head_suffix(tv.error_kind, tv.error_extra)
        lines = [f"<tool>{_esc_text(name)} 失败{head_extra}", args_line]
        if tv.error_message:
            lines.append(f"  原因 {_ml_text(str(tv.error_message))}")
        return "\n".join(lines)

    @staticmethod
    def _render_decision(ev: _EventSnapshot) -> str | None:
        """``agent.decision_emitted`` → ``<action>`` 行块（渲染格式表 §五3）。

        三行结构，后两行各自可缺——两层正交，一拍可以只有一层::

            <action> ev:01K3P822…          ← 这条事件自己的 ID
            execute_program: 8f3c4e5a6b7c  ← 裁决层，作用于代码资产
            next_action {1a2b3c4d5e6f}:    ← 动作层，本拍新起草的代码
              <源码缩进两格>

        ``ev:`` 供 ``<program_result>`` 回指，让模型看得出某次运行是哪一拍
        下的令。``next_action`` 的 ``{hash}`` 是这段源码作为**代码资产**的身份
        （``sha256(源码)[:12]``）——写下的程序不会当拍执行，要执行得由后一拍
        写 ``execute_program(program_hash=…)`` 指名，抄不到这个 hash 就永远
        执行不了自己写下的任何东西。

        两个值域不可互推：``ev:`` 命名"发生过的事"，``{hash}`` 命名"不可变
        的代码"。没写下动作层代码的拍（空程序、纯裁决）payload 里没有 hash，
        整个 ``next_action`` 块不出现。

        **``execute_program:`` 读的是 payload 里单独存的目标 hash，不是从源码
        里扒的**——落库解耦（防套娃）保证存下来的 program 正文里绝不含调度
        指令，所以那一行必须另有出处。
        """
        payload = ev.payload or {}
        if "program" not in payload:
            return None
        lines = [f"<action> ev:{_head_field(str(ev.event_id))}"]

        commit_hash = payload.get("commit_program_hash")
        if isinstance(commit_hash, str) and commit_hash:
            lines.append(f"execute_program: {_head_field(commit_hash)}")

        program_hash = payload.get("program_hash")
        raw = payload.get("program")
        source = raw if isinstance(raw, str) else str(raw)
        if isinstance(program_hash, str) and program_hash and source.strip():
            lines.append(f"next_action {{{_head_field(program_hash)}}}:")
            lines.append(f"  {_ml_text(source)}")
        elif source.strip():
            # 历史事件：2026-08-21 之前写的决策没有 program_hash 键。正文还在，
            # 照旧渲染出来，只是没有可指名的资产身份。
            lines.append("next_action:")
            lines.append(f"  {_ml_text(source)}")
        elif len(lines) == 1:
            # 两层都空 = 停止符。留一行说明，否则时间线上只剩一个孤零零的 ev:。
            lines.append("（空程序）")
        return "\n".join(lines)

    @staticmethod
    def _render_program(ev: _EventSnapshot) -> str | None:
        """Program terminal → 一行 ``<program_result>``（渲染格式表 §五5）。

        ``<program_result> {hash} ev:{调度事件} status:ok|failed``，正文另起
        ``result:`` 或 ``reason:`` 缩进行。**hash 与 ev: 缺一不可**：
        2026-08-21 取消 ``already_executed`` 后同一份资产可以合法并发跑多次，
        只凭 hash 分不出是哪一次运行，``(调度事件, 资产 hash)`` 才唯一确定一
        次。并发派发下同一屏可以有多段程序在跑，靠位置更对不上号。

        ``ev:`` 取 payload.dispatch_event_id，缺失时退回 decision_id /
        causation_id（历史事件）。程序源码永不出现在本行——它在写下它的那条
        ``<action>`` 上，此处只报结果。
        """
        payload = ev.payload or {}
        anchor = _decision_anchor(ev)
        hash_part = _program_hash_part(ev)
        if ev.type == "agent.program_completed":
            head = f"<program_result>{hash_part}{anchor} status:ok"
            has_result = bool(payload.get("has_result"))
            if not has_result:
                return head
            result_json = _safe_json(payload.get("result"))
            truncated = ""
            if len(result_json) > Projector.MAX_TOOL_RESULT_CHARS:
                result_json = result_json[: Projector.MAX_TOOL_RESULT_CHARS]
                truncated = "（截断）"
            return f"{head}\nresult: {_esc_text(result_json)}{truncated}"

        error_kind = str(payload.get("error_kind") or "unknown")
        extra = _extract_program_error_extra(payload)
        head = (
            f"<program_result>{hash_part}{anchor} status:failed"
            f"{_error_head_suffix(error_kind, extra)}"
        )
        message = payload.get("error_message")
        if isinstance(message, str) and message:
            return f"{head}\nreason: {_ml_text(message)}"
        return head

    @staticmethod
    def _render_invalid_action(ev: _EventSnapshot) -> str | None:
        """``agent.invalid_action`` → ``<invalid_action>`` 行块。

        两个子槽：``reason:`` 是真实静态 kind 加定位，``raw_text:`` 是被拒源码
        全文（缩进两格）。回灌源码是本行存在的理由——只说"你写错了"而不给出
        错在哪一段，模型无从改起。

        ``reason`` 走 ``_inline_text``（压平 + 转义）而不是 ``_head_field``：
        它不是行头，没有"首个 `: ` 即定界"的解析需求，把冒号和括号打成全角
        只会让模型读到的错误说明失真。源码逐行走 ``_esc_text``——那本就是模型
        写的自由文本，可能含列 0 的 ``<`` 与 ``&``，不转义就能伪造行标记。
        """
        payload = ev.payload or {}
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            kind = payload.get("error_kind")
            reason = str(kind) if kind else "invalid_action"
        parts = ["<invalid_action>", f"reason: {_inline_text(reason.strip())}"]
        raw_text = payload.get("raw_text")
        if isinstance(raw_text, str) and raw_text.strip():
            normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
            parts.append("raw_text:")
            parts.extend(f"  {_esc_text(line)}" for line in normalized.split("\n"))
        return "\n".join(parts)

    @staticmethod
    def _render_background(ev: _EventSnapshot) -> str | None:
        """``agent.background_noted`` → ``<background>`` 时间线行（2026-08-21）。

        头行是裸标记，五个字段各占一条两空格缩进的续行（``key: value``）。
        缺字段整行不出——按信封通则一，缺失读作"未知"，写 ``group_role: null``
        会被读成"角色是 null"。全部字段都缺时返回 None，不渲染空壳。

        安全：``group_name`` 与 ``self_group_nick_name`` 是**用户可控**的
        （群主改群名、管理员改本账号名片）。走 ``_head_field``——与 ``<msg>``
        行头里的人名同一套净化：压平换行防伪造列 0 行，半角 ``[ ]`` 中和成全角
        防伪造内联段。``group_id`` 是整数、``group_role`` 出自闭集、日期由本
        进程生成，都不经外部文本。
        """
        payload = ev.payload or {}
        lines: list[str] = []

        name = payload.get("group_name")
        if isinstance(name, str) and name.strip():
            lines.append(f"  group_name: {_head_field(name)}")

        group_id = payload.get("group_id")
        if isinstance(group_id, int) or (
            isinstance(group_id, str) and group_id.strip().isdigit()
        ):
            lines.append(f"  group_id: {int(group_id)}")

        nick = payload.get("self_group_nick_name")
        if isinstance(nick, str) and nick.strip():
            lines.append(f"  self_group_nick_name: {_head_field(nick)}")

        role = payload.get("group_role")
        if isinstance(role, str) and role.strip().lower() in _BOT_ROLES:
            lines.append(f"  group_role: {role.strip().lower()}")

        date = payload.get("date")
        if isinstance(date, str) and date.strip():
            weekday = payload.get("weekday")
            stamp = _head_field(date)
            if isinstance(weekday, str) and weekday.strip():
                stamp = f"{stamp} {_head_field(weekday)}"
            lines.append(f"  date: {stamp}")

        if not lines:
            return None
        return "\n".join(["<background>", *lines])

    @staticmethod
    def _render_reflection(ev: _EventSnapshot) -> str | None:
        """``agent.reflection_written`` → ``<reflection>`` 时间线行（2026-08-21）。

        时刻由外层 ``<t>`` 头承载，行内不再自带写入时刻——它现在是时间线上
        一条普通事实，"多久以前想的"由时刻头与 ``<now>`` 相减即得，和别的
        行一个算法。正文走多行容忍位净化（换行 + 两空格缩进），保证动态内容
        到不了列 0。

        正文为空时返回 None（不渲染空行）。
        """
        text = (ev.payload or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return f"<reflection>\n  {_ml_text(text.strip())}"

    @staticmethod
    def _render_context_recap(ev: _EventSnapshot) -> str:
        """回忆行（runtime.context_compacted，Part 3 §3.1）：头行 = 覆盖区间
        + 条数，缩进正文 = 摘要全文 + 从属脚注；不渲染 recall_cues /
        内部字段。钉住/不占窗口预算等投影行为在裁剪层，不在此处。"""
        payload = ev.payload or {}
        summary = str(payload.get("summary") or "").strip()
        head = "<recall>"
        # 头字段虽由可信的 compactor 写入，仍按"一切动态文本先净化"的总则
        # 走单行净化，不让任何 payload 值裸进列 0 行头。
        frm = _inline_text(str(payload.get("covers_from_occurred_at") or "")[:16])
        until = _inline_text(str(payload.get("covers_until_occurred_at") or "")[:16])
        if frm and until:
            head += f"{frm.replace('T', ' ')} 起至 {until.replace('T', ' ')}"
        count = payload.get("dropped_event_count")
        if isinstance(count, int):
            head += f" 共{count}条"
        footnote = (
            "（回忆由更早对话压缩而来，仅供参考；与当前对话冲突时，以当前对话为准。）"
        )
        return f"{head}\n  {_ml_text(summary)}\n  {footnote}"

    @staticmethod
    def _render_runtime(ev: _EventSnapshot) -> str:
        kind = ev.type.replace("runtime.", "")
        payload = ev.payload or {}
        if not payload:
            return f"<system>{_esc_text(kind)}"
        return f"<system>{_esc_text(kind)} {_esc_text(_safe_json(payload))}"

    @staticmethod
    def _render_ingest_failure(ev: _EventSnapshot) -> str:
        """Render only the safe summary, never the raw NapCat audit payload."""
        payload = ev.payload or {}
        parts = ["<system>event_ingest_failed"]

        source_type = payload.get("source_event_type")
        if source_type:
            parts.append(f" source={_inline_text(str(source_type))}")

        sender = payload.get("sender")
        if isinstance(sender, dict):
            name = sender.get("card") or sender.get("nickname")
            qq = sender.get("user_id") or ev.user_id
            actor = _head_field(str(name)) if name else ""
            if qq is not None:
                actor += f"({_head_field(str(qq))})"
            if actor:
                parts.append(f" actor={actor}")

        message_id = payload.get("source_message_id")
        if message_id:
            parts.append(f" #{_head_field(str(message_id))}")

        failures = payload.get("failures")
        if isinstance(failures, list) and failures:
            labels: list[str] = []
            for failure in failures:
                if not isinstance(failure, dict):
                    continue
                stage = str(failure.get("stage") or "unknown")
                code = str(failure.get("error_code") or "unknown")
                labels.append(f"{_inline_text(stage)}/{_inline_text(code)}")
            if labels:
                parts.append(f" failures={','.join(labels)}")
            first = failures[0] if isinstance(failures[0], dict) else {}
            reason = first.get("reason")
            if reason:
                parts.append(f" reason={_quote_excerpt(str(reason), limit=80)}")

        raw_message = payload.get("raw_message")
        if raw_message:
            parts.append(f" text={_quote_excerpt(str(raw_message), limit=120)}")
        return "".join(parts)

    @staticmethod
    def _render_reply_task_completed(ev: _EventSnapshot) -> str:
        """runtime.reply_task_completed → ``<wait_ended>`` 极简行。

        只陈述"这段等待结束了"这一件事；没有授权 ID、unseen、consumed 或
        expires_at——命名刻意不表达任何发言权限（v2.0/30-工具设计/发言链路
        设计.md §1）。2026-08-01 起连 analysis 正文也没有了：这一行的信息量本来
        就该低到只是一次叫醒，该说什么去读它上面的时间线。
        """
        payload = ev.payload or {}
        head = f"<wait_ended>{_head_field(str(payload.get('reply_task_id') or ''))}"
        revision = payload.get("revision")
        if revision:
            head += f" r{_head_field(str(revision))}"
        return head

    @staticmethod
    def _render_reply_flushed(ev: _EventSnapshot) -> str:
        """旧链路 runtime.reply_flushed → ``<legacy_reply>`` 行块（仅历史兼容渲染）。

        现役发言不产生本行：一次发送的记录就是它的 ``<tool>send_messages``
        行（气泡 + 结果回执）。
        """
        payload = ev.payload or {}
        head = "<legacy_reply>"
        task_id = payload.get("reply_task_id")
        if task_id:
            head += f"{_head_field(str(task_id))} "
        head += _esc_text(str(payload.get("status") or "unknown"))
        parts = [head]
        for item in payload.get("sent_messages") or []:
            if not isinstance(item, dict):
                continue
            line = _render_bubble_line(item, receipt=item)
            if line is not None:
                parts.append(f"  {line}")
        reason = payload.get("reason")
        if reason:
            parts.append(f"  原因 {_ml_text(str(reason))}")
        return "\n".join(parts)


# ─── 时间流渲染（时间流契约 2026-07-26）───


def render_timeline_stream(items: Sequence[TimelineItem]) -> list[str]:
    """timeline 行 → ``<t>`` 时刻头 + 事件行的行序列（信封层唯一入口）。

    时间是最外层结构：事件行从属于其上方最近的 ``<t>`` 时刻头、自身不带
    任何时间字段——模型对每个事件的第一感知是"何时"。相邻且同秒
    （timespec=seconds，与旧行内 time= 同精度）的行共享同一时刻头：同拍
    派发的工具批次、同秒消息 burst 自然聚簇为"这一刻发生了这些"。

    时刻头无闭合标签（Part 3 §3.1）：首个时刻头与跨日的时刻头带完整日期
    ``<t>YYYY-MM-DD HH:MM:SS``，同日内只 ``<t>HH:MM:SS``；时区全局固定
    Asia/Shanghai（envelope.md 约定），不逐节点渲染。前缀缓存：同秒追加
    从旧 XML 的"重写 ``</time>`` 闭合位置"变为**纯追加**，无任何重写点。

    信封组装层必须经由本函数渲染 timeline（历史上 Planner 与 Replyer 两个
    组装层靠它保证逐字节同构；2026-07-31 删除 Replyer 后只剩 Planner 一个
    消费者，单一入口的约定保留；memory_compactor 同样走这里）。
    """
    parts: list[str] = []
    open_when: str | None = None
    open_date: str | None = None
    for item in items:
        when = item.occurred_at.isoformat(timespec="seconds")
        if when != open_when:
            date_part, _, time_part = when.partition("T")
            # 去掉 isoformat 的时区尾巴（+08:00）——全局约定承载。
            clock = time_part[:8]
            if date_part != open_date:
                parts.append(f"<t>{date_part} {clock}")
                open_date = date_part
            else:
                parts.append(f"<t>{clock}")
            open_when = when
        parts.append(item.render)
    return parts


# ─── 转义/净化 + JSON helpers（Part 3 §2.1）───
#
# 双层防御：第一层字符级——一切动态文本经 _esc_text，渲染器结构一律 "<"
# 开头，假结构在字符层不成立；第二层行级——多行容忍位缩进续行、单行位
# 压平，动态内容到不了列 0。行头短字段另做定界净化（_head_field /
# _quote_excerpt，半角→全角，不可逆的显示层替换——被换字符在显示名里
# 出现率低、替换后形近，换来行头文法内无用户可控定界字符）。


def _task_note_of(payload: object) -> str | None:
    """``agent.task_note_written`` 载荷 → 便签正文，空/缺失一律 None。

    窗口折叠与库查询共用一个解析，两条路径不会对同一条事件给出不同答案。
    """
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    content = content.strip()
    return content or None


# bot 在群里的身份闭集。跑期对闭集外的值一律降级为 None——不让来路不明的
# 角色字符串进信封（<background> 行与 DecisionContext.bot_role 共用本集合）。
_BOT_ROLES = frozenset({"owner", "admin", "member"})


def _esc_text(s: str) -> str:
    """结构字符转义：``& < >``。**不含** ``[ ]``。

    JSON 序列化结果（工具参数/结果、载荷兜底）也走这里，而 JSON 数组本来就用
    方括号——把它一并转义会把每个数组打成 ``&lsqb;…&rsqb;``，模型每拍都要读
    这些行。方括号的转义因此按位置收窄，见 ``_esc_inline``。
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_inline(s: str) -> str:
    """**正文位**转义：``& < >`` 再加内联段定界符 ``[ ]``（2026-08-21）。

    内联段（``[img …]`` / ``[@ …]`` / ``[card …]``…）只出现在消息正文与引用摘要
    里，方括号也只在那两处具有结构含义。用户直接打一行
    ``[img aabbccddeeff : 无害图片]`` 就能伪造一张图——这是可注入面，不是风格
    问题（渲染格式表 §五2 实施前置条件）。

    转义形态沿用正文位既有的实体风格（``&lt;`` 同族），不用全角替换：正文是
    用户原话，替换字符会悄悄改写他说的内容，而实体至少是可还原的标注。
    行头短字段走另一条路（``_head_field`` 的全角定界净化），那里字段短、
    可读性优先。
    """
    return _esc_text(s).replace("[", "&lsqb;").replace("]", "&rsqb;")


def _ml_text(s: str) -> str:
    """多行容忍位的正文净化：字符级转义 + 换行归一为「换行+两空格缩进」。

    缩进续行从属于所在行（Part 3 §2.1），保证动态内容永远到不了列 0。"""
    normalized = s.replace("\r\n", "\n").replace("\r", "\n")
    return _esc_text(normalized).replace("\n", "\n  ")


def _seg_field(s: str) -> str:
    """内联段 ``[…]`` 内部的单行字段位：压平 + 转义（含 ``[ ]``）。

    段内自由文本（图片描述、文件名、表情名、卡片标题…）里出现一个 ``]``
    就能提前闭合本段，后半截变成正文——与正文位同一类可注入面，用同一套
    转义堵住。
    """
    return _esc_inline(_flatten(s))


def _ml_inline(s: str) -> str:
    """``_ml_text`` 的正文位版本：额外转义 ``[ ]``。消息正文段专用。"""
    normalized = s.replace("\r\n", "\n").replace("\r", "\n")
    return _esc_inline(normalized).replace("\n", "\n  ")


def _flatten(s: str) -> str:
    """单行字段位：空白（含换行）压成单空格。"""
    return " ".join(s.split())


def _inline_text(s: str) -> str:
    """单行动态字段：先压平空白，再做结构字符转义。"""
    return _esc_text(_flatten(s))


_HEAD_FIELD_TABLE = str.maketrans(
    {
        "(": "（",
        ")": "）",
        "/": "／",
        ":": "：",
        "#": "＃",
        "@": "＠",
        # 2026-08-21：内联段改用 [ ] 定界后，行头里的方括号同样要中和——
        # 名字叫 "张三[img aabbccddeeff : x]" 时行头会长出一张假图。
        "[": "［",
        "]": "］",
    }
)

_EXCERPT_TABLE = str.maketrans(
    {":": "：", "「": "『", "」": "』", "[": "［", "]": "］"}
)


def _head_field(s: str) -> str:
    """行头短字段（名字/头衔/各类 ID）净化：压平 + 转义 + N1 定界净化
    （半角 ( ) / : # @ → 全角）。行头的解析终点是第一个 ``: ``，净化后
    头内不残留用户可控的定界字符。"""
    return _esc_text(_flatten(s)).translate(_HEAD_FIELD_TABLE)


def _program_hash_part(ev: _EventSnapshot) -> str:
    """program terminal → `` <hash12>``；缺失时返回空串。

    与 ``_decision_anchor`` 合起来构成一次运行的唯一身份。历史事件（2026-08-21
    之前写的 terminal）没有这个键，退化为只有 ``ev:`` 的旧形态，不报错。
    """
    program_hash = (ev.payload or {}).get("program_hash")
    if not isinstance(program_hash, str) or not program_hash:
        return ""
    return f" {_head_field(program_hash)}"


def _decision_anchor(ev: _EventSnapshot) -> str:
    """program terminal → `` ev:<调度事件ID>``；查不到来源时返回空串。

    优先取 ``payload.dispatch_event_id``（下达 execute_program 的那一拍），
    退回 ``payload.decision_id`` / ``causation_id``——空程序在自己那一拍收口，
    没有调度事件，回指的就是它自己。
    """
    payload = ev.payload or {}
    anchor_id = (
        payload.get("dispatch_event_id")
        or payload.get("decision_id")
        or ev.causation_id
    )
    if not anchor_id:
        return ""
    return f" ev:{_head_field(str(anchor_id))}"


def _quote_excerpt(s: str, *, limit: int = 40) -> str:
    """摘要/留言的 ``「…」`` 引用位：压平 + 截断 + 转义 + N2 净化
    （半角冒号→全角、内层「」→『』、方括号→全角——冒号防提前终止行头，
    引号防提前闭合引用位，方括号防伪造内联段）。

    2026-08-21 加入方括号：内联段改用 ``[…]`` 定界后，引用摘要里若留下半角
    方括号，一条被引用的消息就能在摘要位置伪造出一张图。全角化同时中和了
    ``_segment_gloss`` 自己产出的 ``[图片]`` 一类占位——这是有意的：摘要里
    不存在真的内联段，全都该是全角。"""
    flat = _clip(_flatten(s), limit)
    return f"「{_esc_text(flat).translate(_EXCERPT_TABLE)}」"


def _qq_disp(qq: object, bot_user_id: str | None) -> str:
    """QQ 号显示：等于本账号时缀 ``*``（服务端标注，Part 3 §2.2）。"""
    text = str(qq)
    if bot_user_id and text == str(bot_user_id):
        return f"{text}*"
    return text


def _person(
    qq: object,
    names: dict[str, str],
    bot_user_id: str | None,
) -> str:
    """人物显示：``名(QQ)``（近期消息可反查到名字）或 ``(QQ)``。"""
    disp = _qq_disp(qq, bot_user_id)
    name = names.get(str(qq))
    if name:
        return f"{_head_field(str(name))}({disp})"
    return f"({disp})"


_HEX_HASH_RE = re.compile(r"[0-9a-fA-F]{12,64}")


def _hash12(value: object) -> str:
    """图片 sha256 的 12 位展示前缀（Part 3 §2.2；工具按前缀唯一匹配）。

    hash 位也是动态文本——失败路径的气泡参数、上游消息段都可能携带任意
    字符串——必须先验形：合法十六进制才按前缀截取，否则按单行动态字段
    净化后截断透出，防止伪造值把字面 ``<`` 或换行带进结构位。"""
    text = str(value)
    if _HEX_HASH_RE.fullmatch(text):
        return text[:12]
    return _inline_text(text[:24])


def _record_seconds(d: dict) -> int | None:
    """OneBot record 段的时长（秒）。取不到返回 None，**不编数字**。

    napcat 上报字段名不统一（``duration`` / ``seconds`` / ``time``），逐个试；
    非正整数视为未知。转录不在本函数职责内——语音转录尚未实现（2026-08-21）。
    """
    for key in ("duration", "seconds", "time"):
        raw = d.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            secs = int(float(raw))
        except (TypeError, ValueError):
            continue
        if secs > 0:
            return secs
    return None


def _human_size(value: object) -> str | None:
    """字节数 → 人性化大小（512B / 3.4KB / 2.0MB / 1.1GB）。非数字原样
    透传（上游给什么显示什么），空白 → None。"""
    text = str(value).strip()
    if not text:
        return None
    try:
        n = float(text)
    except ValueError:
        return _inline_text(text)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)}B"
            return f"{n:.1f}{unit}"
        n /= 1024
    return None  # pragma: no cover - unreachable


def _safe_json(value) -> str:
    """JSON 序列化，不可序列化的对象用 str() 兜底，避免 prompt 渲染崩溃。

    json.dumps 产物内的换行必然是 ``\\n`` 字面；str() 兜底可能带真换行，
    压平后返回，保证本函数产物永远单行（调用方只再做字符级转义）。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return _flatten(str(value))


# ─── send_messages 气泡渲染（Part 3 §3.2 特例）───
#
# 动机（2026-08-01 人话渲染裁定的延伸）：自己说过的话在 timeline 上的唯一
# 形态就是这一行块，若渲染成 JSON 参数文本，同一份 prompt 里「我」的语言
# 是转义结构体、「他人」的语言是话，模型对"我刚才是什么语气"几乎无感——
# 线上实测表现为跨拍复用同一句式。行文法把气泡与逐条回执并进同一行块：
# 记录仍只有这一处，不派生第二行（不与 2026-07-31「一次发送只渲染一处」
# 裁定冲突）。


def _spoken_bubble_text(content: object) -> str | None:
    """**迁移前**的 chat 气泡段数组 → 单条人话文本（2026-08-14 前的事件）。

    形状不识 → None（调用方整体退回 JSON 通用行）。text 段转义后换行缩进；
    at/回复/表情用行文法记号，与消息体同一套内联段词汇（2026-08-21 起
    ``[@ (QQ)]`` / ``[face ID]``）。新形状走 ``_domain_bubble_text``。"""
    if not isinstance(content, list) or not content:
        return None
    parts: list[str] = []
    for seg in content:
        if not isinstance(seg, dict):
            return None
        data = seg.get("data")
        data = data if isinstance(data, dict) else {}
        seg_type = seg.get("type")
        if seg_type == "text":
            parts.append(_ml_inline(str(data.get("text", ""))))
        elif seg_type == "at":
            parts.append(f"[@ ({_seg_field(str(data.get('qq', '')))})]")
        elif seg_type == "reply":
            parts.append(f"回复#{_head_field(str(data.get('id', '')))}")
        elif seg_type == "face":
            parts.append(f"[face {_seg_field(str(data.get('id', '')))}]")
        else:
            return None
    return "".join(parts)


def _domain_bubble_text(bubble: dict) -> str | None:
    """领域形状 chat 气泡（2026-08-14 起）→ 单条人话文本。

    渲染顺序与 ``outbound_messages.build_chat_content`` 的段顺序一致
    （reply → at → text → face），记号沿用行文法：``回复#ID`` / ``[@ (QQ)]`` /
    ``[face ID]``。四个键全缺 → None，调用方退回 JSON 通用行。"""
    parts: list[str] = []
    # 参数气泡（`agent.tool_called`）渲染的是模型原样写的值，未经 validate_messages
    # 归一：schema 允许 reply/at/face 写成整数，这里不能只认字符串。
    reply = bubble.get("reply")
    if isinstance(reply, (str, int)) and not isinstance(reply, bool):
        reply = str(reply).strip()
        if reply:
            parts.append(f"回复#{_head_field(reply)}")
    at = bubble.get("at")
    for qq in at if isinstance(at, list) else ([at] if at is not None else []):
        parts.append(f"[@ ({_seg_field(str(qq))})]")
    text = bubble.get("text")
    if isinstance(text, str) and text:
        parts.append(_ml_inline(text))
    face = bubble.get("face")
    for fid in face if isinstance(face, list) else ([face] if face is not None else []):
        parts.append(f"[face {_seg_field(str(fid))}]")
    return "".join(parts) if parts else None


def _render_bubble_line(bubble: object, receipt: object = None) -> str | None:
    """单气泡 → ``「内容」[→回执]`` 一行。气泡形状不识 → None。

    两种 chat 形状都要认：2026-08-14 之后是领域键（text/reply/at/face），
    之前是 OneBot 段数组（``content``）。旧形状**不能**只保留一段时间——
    ``agent_events`` 只增不改，早于迁移的发言行会被永久重复投影，两条路径
    都是现役代码。

    回执（可选）三态：sent+message_id → ``→#ID``；failed → ``→失败``；
    uncertain → ``→存疑``；其余取值原样透出（历史形状兜底）。"""
    if not isinstance(bubble, dict):
        return None
    if bubble.get("kind") == "meme" or "meme" in bubble:
        image_hash = _nonempty_str(bubble.get("image_hash") or bubble.get("meme"))
        body = f"<meme {_hash12(image_hash)}>" if image_hash else "<meme>"
    else:
        if "content" in bubble:
            spoken = _spoken_bubble_text(bubble.get("content"))
        else:
            spoken = _domain_bubble_text(bubble)
        if spoken is None:
            return None
        body = f"「{spoken}」"
    if not isinstance(receipt, dict):
        return body
    status = str(receipt.get("status") or "")
    if status == "sent":
        mid = receipt.get("message_id")
        return f"{body}→#{_esc_text(str(mid))}" if mid is not None else (f"{body}→存疑")
    if status == "failed":
        return f"{body}→失败"
    if status == "uncertain":
        return f"{body}→存疑"
    if status:
        return f"{body}→{_esc_text(status)}"
    return body


def _render_send_messages_call(args: object, tv: "ToolResultView | None") -> str | None:
    """``<tool>send_messages`` 行块：头行 + 逐气泡行（终态带回执）。

    任一环节形状不识（旧事件 / 空 messages / 回执缺失且参数不识）→ None，
    调用方退回通用 JSON 渲染，事实不消失。"""
    arg_bubbles: list | None = None
    if isinstance(args, dict) and isinstance(args.get("messages"), list):
        arg_bubbles = args["messages"] or None

    if tv is not None and tv.error_kind == "pending":
        if not arg_bubbles:
            return None
        lines = ["<tool>send_messages 已调用"]
        for bubble in arg_bubbles:
            line = _render_bubble_line(bubble)
            if line is None:
                return None
            lines.append(f"  {line}")
        return "\n".join(lines)

    if tv is None:
        if not arg_bubbles:
            return None
        lines = ["<tool>send_messages 失败 interrupted status=uncertain"]
        for bubble in arg_bubbles:
            line = _render_bubble_line(bubble)
            if line is None:
                return None
            lines.append(f"  {line}")
        lines.append("  原因 调用没有终态，投递状态存疑")
        return "\n".join(lines)

    if tv.error_kind is None:
        result = tv.result if isinstance(tv.result, dict) else {}
        receipts = result.get("sent_messages")
        if not isinstance(receipts, list) or not receipts:
            return None
        lines = ["<tool>send_messages 完成"]
        for receipt in receipts:
            line = _render_bubble_line(receipt, receipt=receipt)
            if line is None:
                return None
            lines.append(f"  {line}")
        return "\n".join(lines)

    extra = tv.error_extra or {}
    head_extra = {key: value for key, value in extra.items() if key != "sent_messages"}
    head = "<tool>send_messages 失败" + _error_head_suffix(
        tv.error_kind,
        head_extra,
    )
    receipts = extra.get("sent_messages")
    lines = [head]
    if isinstance(receipts, list) and receipts:
        for receipt in receipts:
            line = _render_bubble_line(receipt, receipt=receipt)
            if line is None:
                return None
            lines.append(f"  {line}")
    elif arg_bubbles:
        # 发送前即失败（校验/收藏缺失…）：无回执，渲染参数气泡供回看。
        for bubble in arg_bubbles:
            line = _render_bubble_line(bubble)
            if line is None:
                return None
            lines.append(f"  {line}")
    else:
        return None
    if tv.error_message:
        lines.append(f"  原因 {_ml_text(str(tv.error_message))}")
    return "\n".join(lines)


# agent.tool_failed.payload 顶层的"信封字段"——不属于结构化失败附加信息（extra）。
# 工具执行层把 payload 拼成 {tool_call_id, tool_name, error_kind,
# error_message, **outcome.extra}，fold_tool_results 据此把其余键收进
# ToolResultView.error_extra，再由 _render_error_element 透给 LLM。
# ``task_id`` 保留在集合里：新写入已不带它（2026-08-21 任务坍缩），但库里的
# 存量失败行还有，不过滤会让老行在信封里多长出一个 task_id=... 的 k=v。
_TOOL_FAILED_ENVELOPE_KEYS = frozenset(
    {"tool_call_id", "tool_name", "task_id", "error_kind", "error_message"}
)


def _extract_error_extra(payload: dict) -> dict | None:
    """从 tool_failed.payload 顶层收出结构化失败附加字段（ToolOutcome.extra
    平铺进来的那些：required_tier / actual_tier / retcode / action ...），
    剔除信封字段与 None 值。全空则返回 None。"""
    extra = {
        k: v
        for k, v in (payload or {}).items()
        if k not in _TOOL_FAILED_ENVELOPE_KEYS and v is not None
    }
    return extra or None


# program terminal 载荷里**已经被行头渲染过**的键。漏一个就会在失败行尾
# 多出一个 k=v 重复同一份信息（2026-08-21：program_hash / dispatch_event_id
# 随资产语义新增，当时忘了加进来——行头已有 `hash ev:X`，尾部再来一遍）。
# 新增任何进 program terminal 载荷的结构字段时，必须同步这个集合。
_PROGRAM_FAILED_ENVELOPE_KEYS = frozenset(
    {
        "decision_id",
        "program_sha256",
        "program_hash",
        "dispatch_event_id",
        "duration_ms",
        "query_calls",
        "effect_call_ids",
        "error_kind",
        "error_message",
    }
)


def _extract_program_error_extra(payload: dict) -> dict | None:
    extra = {
        key: value
        for key, value in payload.items()
        if key not in _PROGRAM_FAILED_ENVELOPE_KEYS and value is not None
    }
    return extra or None


def _is_safe_attr_key(key: object) -> bool:
    """extra 键能否安全当 XML 属性名：仅允许 ASCII 字母/数字/下划线且非数字开头。

    extra 键全是代码字面量（required_tier / retcode / ...），本无注入风险；这里
    只是防御未来某个工具塞进奇怪键名破坏 <error> 标签。不合规的键静默跳过。"""
    if not isinstance(key, str) or not key:
        return False
    if not (key[0].isascii() and (key[0].isalpha() or key[0] == "_")):
        return False
    return all(c.isascii() and (c.isalnum() or c == "_") for c in key)


def _error_head_suffix(
    error_kind: str | None,
    error_extra: dict | None = None,
) -> str:
    """工具失败行头的尾缀：`` kind k=v …``。人类可读原因由调用方另起
    ``原因`` 缩进行。timeline ``<tool>`` 行的失败渲染唯一入口。

    ``error_extra``（required_tier / actual_tier / required_bot_role /
    actual_bot_role / retcode / action / allowed_scopes ...）是工具失败时
    ``ToolOutcome.extra`` 平铺进 ``tool_failed.payload`` 的结构化字段，逐个
    以 ``k=v`` 透出：标量原样、列表/字典 JSON 编码，让 LLM 精确解释"差在
    哪一级权限 / napcat 具体报了什么"。键做标识符白名单过滤
    （``_is_safe_attr_key``）防结构注入；单值超 200 字截断，避免个别工具塞
    大对象撑爆 prompt。"""
    parts = [_esc_text(str(error_kind or "unknown"))]
    for key, value in (error_extra or {}).items():
        if value is None or not _is_safe_attr_key(key):
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            rendered = str(value)
        else:
            rendered = _safe_json(value)
        if len(rendered) > 200:
            rendered = rendered[:200] + "…"
        parts.append(f"{key}={_esc_text(_flatten(rendered))}")
    return " " + " ".join(parts)


# ─── Segment-level rendering ───


def _render_reply_marker(
    seg: dict,
    excerpt_by_msg_id: dict[str, str],
    author_by_msg_id: "dict[str, _AuthorRef] | None",
    *,
    bot_user_id: str | None = None,
) -> str:
    """reply 段 → 行头 ``回复#ID(作者)「摘要」`` 标记（Part 3 §3.1）。

    作者位/摘要位各自可省（=未知）。作者信息量与旧 from_name/from_qq/
    from_self 三属性一一对应：``名(QQ)`` / ``(QQ)``，本账号缀 ``*``——这是
    "别人引用我 ≠ 我在发言"的关键；没有它，LLM 看到被引用内容内联在画面
    里，容易当成对方刚说的话。

    取值优先级（2026-07-22 出窗引用黑洞修复）：ingest 富化的 segment 顶层
    quoted 键 > 投影窗口内索引。quoted 在消息到达时由适配器已解析的
    event.reply 固化（EventIngest契约 §4），不随窗口滚动丢失；旧库事件
    无 quoted，仍靠窗口索引兜底。逐字段回退：quoted 缺个别子键时该字段仍
    可由索引补上。from_self 是服务端事实标注，bot_user_id 缺失时仍有效。
    """
    d = seg.get("data") or {}
    rid = str(d.get("id", "")).strip()
    if not rid:
        return "回复#?"
    marker = f"回复#{_head_field(rid)}"
    quoted = seg.get("quoted")
    if not isinstance(quoted, dict):
        quoted = {}
    author = (author_by_msg_id or {}).get(rid)
    from_name = _nonempty_str(quoted.get("sender_name")) or (
        author.name if author else None
    )
    from_qq = _nonempty_str(quoted.get("sender_qq")) or (
        author.user_id if author else None
    )
    from_self = quoted.get("from_self") is True or (author.is_self if author else False)
    if from_qq:
        disp = _qq_disp(from_qq, bot_user_id)
        if from_self and not disp.endswith("*"):
            disp += "*"
        author_str = f"{_head_field(from_name)}({disp})" if from_name else f"({disp})"
        marker += f"({author_str})"
    elif from_name:
        star = "(*)" if from_self else ""
        marker += f"({_head_field(from_name)}{star})"
    elif from_self:
        marker += "((*))"
    excerpt = _gloss_segments(quoted.get("segments") or []) or excerpt_by_msg_id.get(
        rid
    )
    if excerpt:
        marker += _quote_excerpt(excerpt)
    return marker


def _render_segments(
    segments: Iterable,
    excerpt_by_msg_id: dict[str, str],
    name_by_user_id: dict[str, str],
    author_by_msg_id: "dict[str, _AuthorRef] | None" = None,
    *,
    bot_user_id: str | None = None,
) -> tuple[str, list[ImageRef]]:
    """把 OneBot V11 段数组翻译成行文法内联段 + 收集已落盘的 ImageRef。

    支持的段类型 → 形态（一律"缺失=未知/不适用"，语义与 envelope.md 的
    "内联段"一一对应，两处必须同步改）。

    2026-08-21 起内联段定界符由 ``<…>`` 改为 ``[…]``（渲染格式表 §五2）。
    与之配套：正文位走 ``_ml_inline`` / 段内字段位走 ``_seg_field``，两者都
    在 ``& < >`` 之外**额外转义 ``[ ]``**——否则用户手打
    ``[img aabbccddeeff : 无害图片]`` 就能凭空造出一张图。旧形态靠 ``<`` 已在
    转义集里才安全，换定界符必须同时换转义集，这是可注入面不是风格问题。
      text     → 原文（转义 + 换行缩进续行）
      at       → ``[@ 名字(QQ)]`` / ``[@ (QQ)]`` / ``[@ 全体]``（QQ 与出站段
                 data.qq 同域，模型可直抄；本账号缀 *）
      reply    → 正常路径已在 _render_message 上提为行头标记；此处兜底
                 内联渲染同一标记（防未上提的调用方）
      image    → ``[img hash12 照片|贴图 : 描述或外显文案]``
                 hash 为 12 位展示前缀（§7，工具按前缀唯一匹配）。
                 kind：napcat data.sub_type 0→照片，1→贴图，或
                 data.emoji_id 存在→贴图（商城表情——napcat 接收侧 mface
                 一律折成 image 段到达）；判断不出则不渲染。
                 正文位描述优先取 desc（ingest 期 VLM 客观转录——纯文本
                 模型看图的**唯一**途径，未描述成功则退 summary 外显文案，
                 模型知道有图但看不到内容，可调 look_at_image 补看）。
      face     → ``[face N : 名]``（QQ 原生黄豆表情；名取 napcat
                 data.raw.faceText，LLM 背不出表情 id 表）
      mface    → ``[face : 名]``（商城表情；无 id）
      record   → ``[voice Ns]`` / ``[voice]``（只给时长，**不转录**）
      video    → ``<视频>``            (同上)
      file     → ``[file 名 (大小) id:X]``（大小人性化；id 是 napcat 文件
                 凭证，供未来的文件下载类工具回填）
      poke     → ``<拍一拍 目标(QQ)>``（napcat 群内拍一拍段不带目标 →
                 ``<拍一拍>``）
      dice     → ``<骰子 N>``   (掷骰子结果 1-6)
      rps      → ``<猜拳 石头|剪刀|布>`` (1/2/3 直接渲染词)
      markdown → ``<markdown>正文``（超 _MAX_MARKDOWN_CHARS 截断加 "…"）
      forward  → ``<聊天记录 id:X>``
      json     → ark 卡片，走 _render_card_segment 解析出
                 ``[card app 「外显」 标题 描述 url]``；解析不出任何字段才
                 回退 ``[card 原始json]``
      share    → 同卡片渲染（OneBot 标准段；napcat 不产生，兼容保留）
      xml      → ``[card 原始xml]``（napcat 收发均不产生，兼容保留）
      其他     → ``<未识别段 类型>``

    image segment 的富化字段 (file_hash / local_path / mime / downloaded /
    description) 由 event_ingest/media.py 写在 segment 顶层（不在 data 内），见
    EventIngest契约.md §5.1。downloaded=true 且 local_path 存在的图片才会进
    images 列表 —— 2026-07-28 起没有任何 prompt 装配路径消费它（Planner/Replyer
    已不看像素），保留是因为它仍是"这条消息带了哪些已落盘图片"的结构化记录。
    """
    parts: list[str] = []
    images: list[ImageRef] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            # 正文位：额外转义 [ ]，否则用户直接打 [img …] 就能伪造内联段。
            parts.append(_ml_inline(str(d.get("text", ""))))
        elif t == "at":
            qq = str(d.get("qq", "")).strip()
            if qq == "all":
                parts.append("[@ 全体]")
            elif qq:
                disp = _qq_disp(qq, bot_user_id)
                nm = name_by_user_id.get(qq)
                if nm:
                    parts.append(f"[@ {_head_field(nm)}({disp})]")
                else:
                    parts.append(f"[@ ({disp})]")
            else:
                parts.append("[@ (?)]")
        elif t == "reply":
            parts.append(
                _render_reply_marker(
                    seg,
                    excerpt_by_msg_id,
                    author_by_msg_id,
                    bot_user_id=bot_user_id,
                )
            )
        elif t == "image":
            inner = "[img"
            file_hash = seg.get("file_hash")
            if file_hash:
                inner += f" {_hash12(file_hash)}"
                # ImageRef 的收集条件只看 downloaded + local_path，与有没有
                # description 无关（描述失败的图仍是"这条消息带了这张图"）。
                if seg.get("downloaded") and seg.get("local_path"):
                    images.append(
                        ImageRef(
                            file_hash=str(file_hash),
                            local_path=str(seg["local_path"]),
                            mime=str(seg.get("mime") or "image/png"),
                        )
                    )
            kind = _image_kind(d)
            if kind:
                inner += " 照片" if kind == "photo" else " 贴图"
            # 描述位：desc（VLM 客观转录，写时已按 MAX_DESCRIPTION_CHARS
            # 截断，这里再兜一道上界防历史脏数据）优先，缺则 summary 外显。
            description = str(seg.get("description") or "").strip()
            summary = str(d.get("summary") or "").strip()
            if description:
                inner += " : " + _esc_inline(
                    _clip(_flatten(description), _MAX_IMAGE_DESC_CHARS)
                )
            elif summary:
                inner += f" : {_seg_field(_clip(summary, 50))}"
            parts.append(inner + "]")
        elif t == "face":
            fid = str(d.get("id", "")).strip()
            fname = _face_name(d)
            if fid and fname:
                parts.append(f"[face {_seg_field(fid)} : {_seg_field(fname)}]")
            elif fid:
                parts.append(f"[face {_seg_field(fid)}]")
            else:
                parts.append("[face]")
        elif t == "mface":
            # 商城/魔法表情（动图贴纸）。summary 是人类可读释义（如 "羡慕"），
            # 是 LLM 唯一能理解的语义；没有 id，缺 summary 时退化为 [face]。
            summary = str(d.get("summary", "")).strip()
            if summary:
                parts.append(f"[face : {_seg_field(summary)}]")
            else:
                parts.append("[face]")
        elif t == "record":
            # 语音只渲染时长，**不转录**（2026-08-21 维护者裁定：这轮只改形态）。
            # envelope.md 里 [voice 时长 : 转录文本] 的转录位同样未实现。
            secs = _record_seconds(d)
            parts.append(f"[voice {secs}s]" if secs is not None else "[voice]")
        elif t == "video":
            parts.append("[video]")
        elif t == "file":
            inner = "[file"
            fname = str(d.get("name", "") or d.get("file", "")).strip()
            if fname:
                inner += f" {_seg_field(fname)}"
            fsize = d.get("file_size")
            if fsize is not None:
                human = _human_size(fsize)
                if human:
                    inner += f" ({human})"
            # file_id 是 napcat 侧的文件句柄，工具要用来取文件——不能省。
            file_id = d.get("file_id")
            if file_id is not None and str(file_id).strip():
                inner += f" id:{_seg_field(str(file_id))}"
            parts.append(inner + "]")
        elif t == "poke":
            target = d.get("qq") or d.get("user_id")
            if target:
                disp = _qq_disp(str(target), bot_user_id)
                parts.append(f"[poke 目标({disp})]")
            else:
                parts.append("[poke]")
        elif t == "dice":
            val = str(d.get("result", "") or d.get("value", "")).strip()
            parts.append(f"[dice {_seg_field(val)}]" if val else "[dice]")
        elif t == "rps":
            # 猜拳：napcat result 1=石头 2=剪刀 3=布，直接渲染词。
            val = str(d.get("result", "") or d.get("value", "")).strip()
            word = {"1": "石头", "2": "剪刀", "3": "布"}.get(val, val)
            parts.append(f"[rps {_seg_field(word)}]" if word else "[rps]")
        elif t == "markdown":
            content = str(d.get("content") or "").strip()
            if content:
                parts.append(
                    f"[markdown]{_ml_inline(_clip(content, _MAX_MARKDOWN_CHARS))}"
                )
            else:
                parts.append("[markdown]")
        elif t == "forward":
            fid = str(d.get("id", "")).strip()
            if fid:
                parts.append(f"[forward id:{_seg_field(fid)}]")
            else:
                parts.append("[forward]")
        elif t == "json":
            parts.append(_render_card_segment(d))
        elif t == "share":
            # OneBot 标准 share 段（napcat 不产生，兼容其他实现）。字段
            # 语义与 ark 卡片对齐：content → desc。
            card = {}
            title = _nonempty_str(d.get("title"))
            if title:
                card["title"] = title
            desc = _nonempty_str(d.get("content"))
            if desc:
                card["desc"] = desc
            url = _nonempty_str(d.get("url"))
            if url:
                card["url"] = url
            parts.append(_card_line(card, fallback="原始share"))
        elif t == "xml":
            parts.append("[card 原始xml]")
        else:
            parts.append(f"[unknown {_seg_field(str(t or 'unknown'))}]")
    return "".join(parts), images


# markdown 段正文渲染上限：官方机器人可能发整页 md，塞满 prompt 不值。
_MAX_MARKDOWN_CHARS = 500
# 图片客观描述的渲染上界。与 image_description.MAX_DESCRIPTION_CHARS 同值但不
# import —— 那是写入端的截断，这里是渲染端对任意来源（含历史事件）的兜底。
_MAX_IMAGE_DESC_CHARS = 1200


def _clip(s: str, limit: int) -> str:
    """超长截断加 "…"。属性值通用，保证单个字段不会撑爆 prompt。"""
    return s if len(s) <= limit else s[:limit] + "…"


def _image_kind(d: dict) -> str | None:
    """推断图片语义类别，返回 "photo" / "sticker" / None（判断不出）。

    依据（NapCatQQ rawToOb11Converters 实测行为）：
    - data.emoji_id 存在 → 商城表情（napcat 把 marketFace 折成 image 段上报，
      带 emoji_id / emoji_package_id / key）→ sticker；
    - data.sub_type 是 NTQQ PicSubType：0=KNORMAL 普通图片 → photo，
      1=KCUSTOM 自定义表情/表情包 → sticker；
    - 其余取值（2=KHOT 等罕见类型）与缺失一律 None——宁可不标也不猜错，
      "缺失=未知"是本渲染器的属性总语义。
    """
    if d.get("emoji_id"):
        return "sticker"
    sub = d.get("sub_type")
    if sub is None:
        return None
    s = str(sub).strip()
    if s == "0":
        return "photo"
    if s == "1":
        return "sticker"
    return None


def _face_name(d: dict) -> str | None:
    """从 napcat face 段的 data.raw.faceText 取表情释义（如 "[微笑]"）。

    faceText 在部分老版本里带 QQ 输入法风格的 "/" 前缀（"/微笑"），去掉；
    raw 缺失 / faceText 为空 → None（渲染方退回只有 id 的形态）。
    """
    raw = d.get("raw")
    if not isinstance(raw, dict):
        return None
    text = raw.get("faceText")
    if not isinstance(text, str):
        return None
    cleaned = text.strip().lstrip("/").strip()
    return cleaned or None


def _parse_ark_card(d: dict) -> dict[str, str] | None:
    """解析 json（ark）段的语义字段，供 XML 渲染与 excerpt 摘要两处复用。

    返回只含非空值的 dict（键：app / summary / title / desc / url），
    啥都解析不出（data 非法 JSON / 空对象）→ None。字段语义：
    - app     ark 应用标识（如 com.tencent.structmsg 链接分享、
              com.tencent.miniapp_01 小程序），LLM 可据此判断卡片种类
    - summary QQ 自己的外显文案（ark 顶层 prompt，如 "[QQ小程序]哔哩哔哩"），
              最稳的一句话摘要
    - title / desc  卡片标题与描述（meta.* 内首个命中的 title/desc；小程序
              卡片里 title 常是应用名、desc 是内容标题，照实透传不加工）
    - url     卡片跳转链接（qqdocurl > jumpUrl > url > musicUrl 择先）
    """
    raw = d.get("data")
    obj = None
    if isinstance(raw, dict):
        obj = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
        except Exception:
            obj = None
    if not isinstance(obj, dict):
        return None

    card: dict[str, str] = {}
    app = obj.get("app")
    if isinstance(app, str) and app.strip():
        card["app"] = app.strip()
    prompt = obj.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        card["summary"] = prompt.strip()
    meta = obj.get("meta")
    if isinstance(meta, dict):
        for detail in meta.values():
            if not isinstance(detail, dict):
                continue
            if "title" not in card:
                title = _nonempty_str(detail.get("title"))
                if title:
                    card["title"] = title
            if "desc" not in card:
                desc = _nonempty_str(detail.get("desc"))
                if desc:
                    card["desc"] = desc
            if "url" not in card:
                url = (
                    _nonempty_str(detail.get("qqdocurl"))
                    or _nonempty_str(detail.get("jumpUrl"))
                    or _nonempty_str(detail.get("url"))
                    or _nonempty_str(detail.get("musicUrl"))
                )
                if url:
                    card["url"] = url
    return card or None


def _card_line(card: dict[str, str], *, fallback: str) -> str:
    """卡片语义字段 → ``[card app 「外显」 标题 描述 url]``。

    字段有则出、按固定顺序空格连排；外显文案（QQ 自己的单行 summary）
    包 ``「」`` 与自由文本字段区分。全空 → ``[card <fallback>]``（表示
    "未解析的原始段格式"，内容未知）。
    """
    parts: list[str] = []
    for key, limit in (
        ("app", 60),
        ("summary", 100),
        ("title", 100),
        ("desc", 200),
        ("url", 300),
    ):
        value = card.get(key)
        if not value:
            continue
        clipped = _clip(_flatten(value), limit)
        if key == "summary":
            parts.append(_quote_excerpt(value, limit=limit))
        else:
            parts.append(_esc_inline(clipped))
    if not parts:
        return f"[card {fallback}]"
    return f"[card {' '.join(parts)}]"


def _render_card_segment(d: dict) -> str:
    """json（ark）段 → ``<卡片 …>``。napcat 接收侧一切富卡片——链接分享、
    B 站/小程序、公众号文章、位置、群推荐——都以 json 段到达，历史上渲染
    成裸类型占位等于把"别人分享了什么"整个丢掉。

    字段语义见 :func:`_parse_ark_card`（全部可缺省，缺失=该字段解析不到）。
    解析不出任何字段 → 回退 ``<卡片 原始json>``。
    """
    card = _parse_ark_card(d)
    return _card_line(card or {}, fallback="原始json")


def _nonempty_str(value) -> str | None:
    """str 且去空白后非空才返回；其他类型（数字/dict）不硬转——ark 字段
    类型不受我们控制，硬转出 "{'x': 1}" 这种属性值比缺失更有歧义。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _notice_sentence(
    kind: str,
    ev: _EventSnapshot,
    names: dict[str, str],
    *,
    bot_user_id: str | None = None,
) -> str | None:
    """按 notice kind 生成模板句（Part 3 §3.1）。

    mapper 已存储的明细字段（禁言秒数 / 新旧名片 / 文件名与大小 / 拍一拍
    文案 / 表情统计 / 被撤回消息 ID / 荣誉类型…）全部进句子——被撤回的
    message_id 尤其关键：没有它 LLM 只知道"有人撤回了"，不知道撤的是哪条，
    会继续引用已撤回的内容。人物 = ``名(QQ)``（近期消息反查），本账号缀
    ``*``。拼不出（关键字段全缺 / 未识别 kind）→ None，调用方走载荷 JSON
    兜底。
    """
    payload = ev.payload or {}
    user = _person(ev.user_id, names, bot_user_id) if ev.user_id is not None else None
    op_id = payload.get("operator_id")
    op = _person(op_id, names, bot_user_id) if op_id else None
    target_id = payload.get("target_id")
    target = _person(target_id, names, bot_user_id) if target_id else None
    sub = str(payload.get("sub_type") or "").strip()
    mid = payload.get("onebot_message_id")
    mid_ref = f"消息#{_head_field(str(mid))}" if mid else None

    if kind == "group_increase":
        if user is None:
            return None
        if sub == "invite":
            return f"{user} 被 {op} 邀请入群" if op else f"{user} 被邀请入群"
        return f"{user} 入群，由 {op} 通过" if op else f"{user} 入群"
    if kind == "group_decrease":
        if sub == "kick_me":
            return f"本账号被 {op} 移出" if op else "本账号被移出"
        if user is None:
            return None
        if sub == "kick":
            return f"{user} 被 {op} 移出" if op else f"{user} 被移出"
        return f"{user} 退群"
    if kind == "group_recall":
        what = mid_ref or "一条消息"
        if op and user and op_id is not None and ev.user_id is not None:
            if str(op_id) == str(ev.user_id):
                return f"{user} 撤回了自己的{what}"
            return f"{op} 撤回了 {user} 的{what}"
        if op:
            return f"{op} 撤回了{what}"
        if user:
            return f"{user} 的{what}被撤回"
        return f"{what}被撤回"
    if kind == "friend_recall":
        return f"对方撤回了{mid_ref or '一条消息'}"
    if kind == "poke":
        if user is None:
            return None
        action = _flatten(str(payload.get("action") or "").strip()) or "拍了拍"
        suffix = _flatten(str(payload.get("action_suffix") or "").strip())
        if target:
            return f"{user} {_esc_text(action)} {target}{_esc_text(suffix)}"
        return f"{user} {_esc_text(action)}"
    if kind == "group_admin":
        if user is None:
            return None
        if sub == "unset":
            return f"{user} 被取消管理员"
        return f"{user} 被设为管理员"
    if kind == "group_ban":
        whole = ev.user_id is None or str(ev.user_id) == "0"
        if sub == "lift_ban":
            if whole:
                return f"{op} 关闭了全员禁言" if op else "全员禁言被关闭"
            if user is None:
                return None
            return f"{op} 解除了 {user} 的禁言" if op else f"{user} 被解除禁言"
        if whole:
            return f"{op} 开启了全员禁言" if op else "全员禁言被开启"
        if user is None:
            return None
        duration = payload.get("duration")
        try:
            seconds = int(str(duration).strip())
        except (TypeError, ValueError):
            seconds = 0
        span = f" {seconds}秒" if seconds > 0 else ""
        return f"{op} 将 {user} 禁言{span}" if op else f"{user} 被禁言{span}"
    if kind == "group_card":
        if user is None:
            return None
        old = payload.get("card_old")
        new = payload.get("card_new")
        # 空串=名片被清空，与缺失（未知）不同——空串也渲染成「」。
        if old is None and new is None:
            return f"{user} 修改了群名片"
        old_part = _quote_excerpt(str(old)) if old is not None else "「?」"
        new_part = _quote_excerpt(str(new)) if new is not None else "「?」"
        return f"{user} 群名片 {old_part}→{new_part}"
    if kind == "group_upload":
        if user is None:
            return None
        file_info = payload.get("file") or {}
        if not isinstance(file_info, dict):
            file_info = {}
        fname = _nonempty_str(file_info.get("name"))
        size = file_info.get("size")
        human = _human_size(size) if size is not None else None
        sentence = f"{user} 上传了"
        if fname:
            sentence += f" {_esc_text(_clip(_flatten(fname), 100))}"
        else:
            sentence += "文件"
        if human:
            sentence += f" ({human})"
        return sentence
    if kind == "essence":
        if mid_ref is None:
            return None
        verb = "移出精华" if sub == "delete" else "设为精华"
        return f"{op} 将{mid_ref}{verb}" if op else f"{mid_ref}被{verb}"
    if kind == "emoji_like":
        label = _emoji_likes_label(payload.get("likes") or [])
        if user is None or label is None:
            return None
        if mid_ref:
            return f"{user} 对{mid_ref}回应 {_esc_text(label)}"
        return f"{user} 作出表情回应 {_esc_text(label)}"
    if kind == "honor":
        honor_type = _nonempty_str(payload.get("honor_type"))
        if user is None or honor_type is None:
            return None
        return f"{user} 获得群荣誉 {_inline_text(honor_type)}"
    if kind == "lucky_king":
        king = target or user
        if king is None:
            return None
        return f"{king} 成为红包运气王"
    if kind == "friend_add":
        if user is None:
            return None
        return f"新增好友 {user}"
    if kind == "input_status":
        return "对方正在输入"
    if kind == "bot_offline":
        return "本账号掉线"
    return None


def _emoji_likes_label(likes) -> str | None:
    """把 emoji_like 的 likes 数组压成一个可读属性值，如 "👍×2,face:66×1"。

    每项 "表情×人数"，逗号分隔；表情的两种形态（napcat 的 emoji_id 两义）：
    - unicode 表情：emoji_id 是十进制 codepoint（128077 → 👍），直接给字符——
      LLM 读 "👍" 比读 "128077" 无歧义得多；
    - QQ 黄豆表情：emoji_id 是小整数 face id，渲染 "face:<id>"（与消息里
      <face face_id=.../> 同一 id 空间）。
    条目上限 5，防御异常 payload 撑爆属性。全部无效 → None（不渲染 likes=）。
    """
    parts: list[str] = []
    for item in list(likes)[:5]:
        if not isinstance(item, dict):
            continue
        symbol = _emoji_symbol(item.get("emoji_id"))
        if symbol is None:
            continue
        count = item.get("count")
        if count is not None and str(count).strip():
            parts.append(f"{symbol}×{count}")
        else:
            parts.append(symbol)
    return ",".join(parts) or None


def _emoji_symbol(emoji_id) -> str | None:
    """emoji_id → 显示符号。≥0x2000 视作 unicode codepoint 转字符（QQ 的
    unicode 类回应都在 emoji 区段，远高于黄豆 face id 的几百量级；排除
    surrogate 区），小整数按 QQ face id 渲染 "face:N"，非数字原样截断透传。"""
    if emoji_id is None:
        return None
    s = str(emoji_id).strip()
    if not s:
        return None
    try:
        value = int(s)
    except ValueError:
        return _clip(s, 20)
    if 0x2000 <= value <= 0x10FFFF and not (0xD800 <= value <= 0xDFFF):
        try:
            return chr(value)
        except ValueError:
            pass
    return f"face:{value}"


def _bracket(s: str) -> str:
    """gloss 用的 "[语义]" 占位包装：没带 "[" 前缀的才包（napcat 的 summary /
    faceText 常自带方括号，如 "[动画表情]"；商城表情名 "贴贴" 则是裸文本）。"""
    return s if s.startswith("[") else f"[{s}]"


def _segment_gloss(seg: dict) -> str | None:
    """单个 segment 的纯文本摘要，用于 reply 段的 excerpt。

    与 ``_render_segments`` 的 XML 渲染**同源取语义字段**——消息体里渲得出
    的信息（图片/商城表情 summary、face 名、ark 卡片外显文案、markdown 正文、
    文件名……），被回复时的 excerpt 里也要看得到；否则"回复一张表情包 / 一个
    B 站卡片"会退化成 [image] / [json] 类型占位，回复链语义断掉。两处新增
    段类型时必须同步改。

    样式对齐 QQ 会话列表习惯：非文本段用 "[语义]" 占位、@ 用 "@号"。
    返回 None = 该段对摘要无贡献（嵌套 reply 标记本身）。

    这里的方括号是**半角**，但本函数的产物必然经 ``_quote_excerpt`` 落进
    ``「…」`` 引用位，那一步会把 ``[ ]`` 一并转成全角 ``［ ］``。信封的不变式
    因此成立：**半角 ``[`` 开头的一定是渲染器写的真内联段**，摘要里的占位与
    用户原话都到不了那个形态。若将来有新调用方绕开 ``_quote_excerpt``，必须
    自己补上等价的中和，否则摘要能伪造内联段。
    """
    t = seg.get("type")
    d = seg.get("data") or {}
    if t == "text":
        return str(d.get("text", ""))
    if t == "image":
        summary = str(d.get("summary") or "").strip()
        if summary:
            return _bracket(summary)
        return "[表情]" if _image_kind(d) == "sticker" else "[图片]"
    if t == "face":
        name = _face_name(d)
        return _bracket(name) if name else "[表情]"
    if t == "mface":
        summary = str(d.get("summary") or "").strip()
        return _bracket(summary) if summary else "[表情]"
    if t == "markdown":
        content = str(d.get("content") or "").strip()
        return content or "[markdown]"
    if t == "json":
        card = _parse_ark_card(d) or {}
        # prompt（QQ 外显文案）通常已是 "[QQ小程序]哔哩哔哩" 形态，原样用；
        # 没有 prompt 时退 title，标上 [卡片] 出处。
        if card.get("summary"):
            return card["summary"]
        if card.get("title"):
            return f"[卡片]{card['title']}"
        return "[卡片]"
    if t == "share":
        title = str(d.get("title") or "").strip()
        return f"[分享]{title}" if title else "[分享]"
    if t == "xml":
        return "[卡片]"
    if t == "file":
        fname = str(d.get("name", "") or d.get("file", "")).strip()
        return f"[文件]{fname}" if fname else "[文件]"
    if t == "record":
        return "[语音]"
    if t == "video":
        return "[视频]"
    if t == "at":
        qq = str(d.get("qq", "")).strip()
        if qq == "all":
            return "@全体成员"
        return f"@{qq}" if qq else "@?"
    if t == "dice":
        val = str(d.get("result", "") or d.get("value", "")).strip()
        return f"[骰子:{val}]" if val else "[骰子]"
    if t == "rps":
        val = str(d.get("result", "") or d.get("value", "")).strip()
        return f"[猜拳:{val}]" if val else "[猜拳]"
    if t == "poke":
        return "[戳一戳]"
    if t == "forward":
        return "[聊天记录]"
    if t == "reply":
        # 被回复消息自己又引用了别人——嵌套引用标记不进摘要，摘要只描述
        # 这条消息"说了什么"。
        return None
    return f"[{t}]" if t else None


def _gloss_segments(segments: Iterable) -> str:
    """段数组 → 单行摘要（前 40 字，超长加 "…"）。

    逐段取 ``_segment_gloss``：文本段取原文，富媒体段取与消息体渲染同源的
    语义占位（不是裸类型列表）。空白规整成单空格（摘要是单行属性值，不该
    带换行）。窗口内 excerpt 索引与 ingest 富化的 quoted.segments 共用这
    一条渲染路径，保证两个来源的 excerpt 形态逐字节同规格。
    """
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        gloss = _segment_gloss(seg)
        if gloss:
            parts.append(gloss)
    excerpt = " ".join("".join(parts).split())
    if len(excerpt) > 40:
        excerpt = excerpt[:40] + "…"
    return excerpt


def _build_reply_task_guard(
    events: Iterable[_EventSnapshot],
) -> dict[str, dict]:
    """reply_task_id → {"max_revision": int, "cancelled": bool} 预扫索引。

    供 ``_completed_is_stale`` 判定过期完成事件（§1.5）：窗口内可见的最高
    upsert revision 与 cancel 事实。窗口外的更早历史不参与——守卫是防御层，
    写入侧的 scope 锁复核才是主防线。
    """
    guard: dict[str, dict] = {}
    for ev in events:
        if ev.type == "agent.reply_task_upserted":
            task_id = str((ev.payload or {}).get("reply_task_id") or "")
            if not task_id:
                continue
            revision = (ev.payload or {}).get("revision")
            entry = guard.setdefault(task_id, {"max_revision": 0, "cancelled": False})
            if isinstance(revision, int) and revision > entry["max_revision"]:
                entry["max_revision"] = revision
        elif ev.type == "agent.reply_task_cancelled":
            task_id = str((ev.payload or {}).get("reply_task_id") or "")
            if not task_id:
                continue
            entry = guard.setdefault(task_id, {"max_revision": 0, "cancelled": False})
            entry["cancelled"] = True
    return guard


def _completed_is_stale(ev: _EventSnapshot, guard: dict[str, dict]) -> bool:
    """过期完成事件判定：revision 低于窗口内最高 upsert，或任务已 cancel。"""
    payload = ev.payload or {}
    task_id = str(payload.get("reply_task_id") or "")
    entry = guard.get(task_id)
    if entry is None:
        return False
    if entry["cancelled"]:
        return True
    revision = payload.get("revision")
    return (
        isinstance(revision, int)
        and entry["max_revision"] > 0
        and revision < entry["max_revision"]
    )


def _build_excerpt_index(events: Iterable[_EventSnapshot]) -> dict[str, str]:
    """timeline 内 onebot_message_id → 摘要（前 40 字）。

    用于渲染 reply 段时给 LLM 提供"被回复消息说了啥"的上下文。命中不到
    （消息在取数窗口外或被 napcat 抛弃了）时由 reply 段自身的
    quoted 富化兜底；两头都没有才渲染裸 reply.to_message_id。
    """
    out: dict[str, str] = {}
    for ev in events:
        if not ev.type.startswith("external.message."):
            continue
        mid = ev.payload.get("onebot_message_id")
        if not mid:
            continue
        excerpt = _gloss_segments(ev.payload.get("segments") or [])
        if excerpt:
            out[str(mid)] = excerpt
    return out


def _build_user_name_index(
    events: Iterable[_EventSnapshot],
) -> dict[str, str]:
    """user_id → 最近一次出现的 card/nickname。用于给 at 段添加名字。"""
    out: dict[str, str] = {}
    for ev in events:
        if not ev.type.startswith("external.message."):
            continue
        sender = ev.payload.get("sender") or {}
        uid = sender.get("user_id") or ev.user_id
        if uid is None:
            continue
        name = sender.get("card") or sender.get("nickname")
        if name:
            out[str(uid)] = str(name)
    return out


@dataclass(frozen=True)
class _AuthorRef:
    """被回复消息的作者信息，供 reply 段拆成独立属性渲染。

    - ``name`` / ``user_id``：作者名（card/nickname 择先）与 QQ 号，任一可为
      None（=未知，渲染时省略对应属性，不造 "?" 占位）。
    - ``is_self``：该消息是 bot 自己发出的（来自 send_message 工具的
      tool_result）。这是**服务端事实标注**，独立于当 tick 是否拿得到
      bot_user_id——渲染成 `from_self="true"`，取代旧的 `from="我(...)"`
      魔法名字。
    """

    name: str | None
    user_id: str | None
    is_self: bool = False


def _build_author_index(
    events: Iterable[_EventSnapshot],
) -> dict[str, _AuthorRef]:
    """onebot_message_id → :class:`_AuthorRef`。用于 reply 段标
    from_name / from_qq / from_self 三个独立属性。

    覆盖两类来源：
    - 外部消息：作者 = sender 的 card/nickname + user_id。别人引用某人时，
      LLM 据此判断被引用的是谁（含群主自己）——而不是误以为那人在发言。
    - bot 自己发出的消息：``is_self=True``。现役来源是 ``send_messages``
      终态里 confirmed-sent 的逐条 receipt（tool_result 的 result 或
      tool_failed 平铺 payload 里的 ``sent_messages``，partial 时只收
      status="sent" 项）；历史来源是旧链路 runtime.reply_flushed 的成功
      sent item。别人引用 bot 时渲染 `from_self="true"`。

    单值 ``result.message_id`` 分支只作 append-only 历史兼容：旧
    send_message 与旧 meme.send/send_meme 的 tool_result 仍可恢复
    from_self；现役 reply tool_result 不含 message_id，被门槛自然滤掉。
    名字元组里的 ``meme`` 同样是**历史名**（2026-07-25 改名
    ``meme_collection`` 且无 send 动作）。
    """
    out: dict[str, _AuthorRef] = {}
    # 收集发言工具调用 id（现役 send_messages + 历史名）。
    send_call_ids: set[str] = set()
    for ev in events:
        if ev.type == "agent.tool_called" and (
            (ev.payload or {}).get("tool_name")
            in ("send_messages", "send_message", "meme", "send_meme", "reply")
        ):
            tc_id = (ev.payload or {}).get("tool_call_id")
            if tc_id is not None:
                send_call_ids.add(str(tc_id))

    def _harvest_sent_items(source: dict) -> None:
        for item in source.get("sent_messages") or []:
            if not isinstance(item, dict) or item.get("status") != "sent":
                continue
            mid = item.get("message_id")
            if mid is None:
                continue
            self_id = item.get("self_id")
            out[str(mid)] = _AuthorRef(
                name=None,
                user_id=str(self_id) if self_id else None,
                is_self=True,
            )

    for ev in events:
        if ev.type == "runtime.reply_flushed":
            _harvest_sent_items(ev.payload or {})
        # bot 自己经工具发出的消息：从终态取 message_id + self_id。
        # tool_result → receipts 在 result（send_messages）或单值
        # message_id（历史工具）；tool_failed → partial 的 receipts 经
        # extra 平铺在 payload 顶层。
        if ev.type in ("agent.tool_result", "agent.tool_failed"):
            payload = ev.payload or {}
            tc_id = payload.get("tool_call_id")
            if tc_id is not None and str(tc_id) in send_call_ids:
                if ev.type == "agent.tool_result":
                    result = payload.get("result") or {}
                    if isinstance(result, dict):
                        _harvest_sent_items(result)
                        mid = result.get("message_id")
                        if mid is not None:
                            self_id = result.get("self_id")
                            out[str(mid)] = _AuthorRef(
                                name=None,
                                user_id=str(self_id) if self_id else None,
                                is_self=True,
                            )
                else:
                    _harvest_sent_items(payload)
        # 外部消息作者。
        if not ev.type.startswith("external.message."):
            continue
        mid = ev.payload.get("onebot_message_id")
        if not mid:
            continue
        sender = ev.payload.get("sender") or {}
        name = sender.get("card") or sender.get("nickname")
        qq = sender.get("user_id") or ev.user_id
        out[str(mid)] = _AuthorRef(
            name=str(name) if name else None,
            user_id=str(qq) if qq is not None else None,
        )
    return out
