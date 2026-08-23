"""SearchHistoryTool — v2 自带的历史事件检索工具。

设计动机（任务与决策契约 §动态记忆检索；拓扑 README §5.3 关联）：

  Projector 每 tick 只把尾部 100 条 timeline 喂给 LLM——这是控制 prompt
  长度的硬上限。当 LLM 需要更早的上下文（"前天某某说了啥"），它通过这个
  工具按需检索而不是被动等被裁掉的事件回流。

三种过滤方式同时支持（逻辑 AND）：
  1. 锚点（anchor）：anchor_event_id，查询只看 anchor 之前发生的事件
     （ULID 字典序天然=时间序）。2026-08-21 起没有 task_id 这条间接路径——
     任务坍缩为单栏便签后 task_id 值域消失（渲染格式表 §一②）。
  2. 时间窗：start_time / end_time（ISO8601 字符串）
  3. 关键字：query 用 pg_trgm word_similarity（`<%` 算子）对 search_text
     （STORED GENERATED 列，见 models/agent_event.py）做模糊相似匹配，
     按相似度倒序取 limit 条，不要求逐字子串命中。2026-07-23 重做前是
     ILIKE payload->>'raw_message'：既没走已建好的 GIN trgm 索引（表达式
     对不上索引列，全表扫描），中文口语转述也基本命不中子串。

scope 隔离：group 按 group_id 过滤，private 按 user_id 过滤（parse_scope_key
解出的三元组按 scope 只用其中一个）。2026-07-23 前 private 分支漏了这道
过滤，只是当时 PrivateAgentLoop 从未实例化，没被线上触发。

返回结构复用 Projector 渲染器，与正向 timeline 完全同构。

错误策略：
  - scope_key 缺失 / 非法 → return ToolOutcome.failure(invalid_arguments)（不 raise）
  - 时间参数无法解析 → 加 warning，不报错
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, literal, select

from qqbot.core.logging import get_logger
from qqbot.core.time import normalize_china_time
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.agent_loop.projection import Projector, _snapshot_from_row
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome

logger = get_logger(__name__)

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50

_USAGE_PROMPT = load_sibling_md(__file__, "search_history.md")


class SearchHistoryTool(BaseTool):
    """实现 Tool 协议。session_factory 从 run() 的 context 进（ProgramExecutor
    统一注入），无构造依赖 —— 与 websearch / send_messages 同构。
    """

    name = "search_history"
    program_kind = "effect"
    max_call_sites = 4
    description = (
        "检索当前 scope 中未包含在近期时间线窗口内的历史事件。过滤条件可组合："
        "anchor_event_id 提供锚点，start_time/end_time 提供时间范围，"
        "query 对消息文本执行模糊相似度匹配。结果使用与时间线相同的行文法。"
    )
    usage_prompt = _USAGE_PROMPT
    # required_permission / required_bot_role 用 BaseTool 默认值（GUEST /
    # 不限 bot 角色）：查历史属于内部知识检索，任何群员都能让小奏查，无需管理员。
    arguments_schema = {
        "type": "object",
        "properties": {
            "anchor_event_id": {
                "type": "string",
                "description": (
                    "仅返回严格早于该 event_id 的事件；event_id 为按时间可排序的 ULID。"
                ),
            },
            "start_time": {
                "type": "string",
                "description": "ISO8601 格式的起始时间，包含边界。",
            },
            "end_time": {
                "type": "string",
                "description": "ISO8601 格式的结束时间，包含边界。",
            },
            "query": {
                "type": "string",
                "description": (
                    "用于匹配消息文本的模糊关键词；采用 trigram 相似度，不要求"
                    "精确子串匹配。"
                ),
            },
            "limit": {
                "type": "integer",
                "description": f"最大返回条数，上限为 {_MAX_LIMIT}。",
                "default": _DEFAULT_LIMIT,
            },
        },
    }
    result_schema = {
        "type": "object",
        "properties": {
            "matched": {"type": "integer"},
            "anchor_event_id": {"type": ["string", "null"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "occurred_at": {"type": "string"},
                        "kind": {"type": "string"},
                        "render": {"type": "string"},
                    },
                    "required": ["event_id", "occurred_at", "kind", "render"],
                    "additionalProperties": False,
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["matched", "anchor_event_id", "items", "warnings"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        # GUEST + 不限 scope：enforce_access 实为 no-op，但仍统一保留首行调用。
        if fail := await self.enforce_access(context):
            return fail

        scope_key = context.get("scope_key")
        if not scope_key or not isinstance(scope_key, str):
            # 由 ProgramExecutor 注入。理论上 system / group:N / private:N 总有一个。
            return ToolOutcome.failure(
                "invalid_arguments",
                "search_history requires scope_key from caller context",
            )
        # session_factory 同样由 ProgramExecutor 在 context 里注入。registry
        # 每次函数调用都会新建工具实例，落到 self 上供 _query /
        # _resolve_task_anchor 复用，不会跨调用共享可变状态。
        self._session_factory = context.get("session_factory")

        try:
            scope, group_id, user_id = parse_scope_key(scope_key)
        except ValueError as exc:
            return ToolOutcome.failure(
                "invalid_arguments", f"invalid scope_key {scope_key!r}: {exc}"
            )

        warnings: list[str] = []

        anchor_event_id = _coerce_str(arguments.get("anchor_event_id"))
        start_time = _coerce_str(arguments.get("start_time"))
        end_time = _coerce_str(arguments.get("end_time"))
        query = _coerce_str(arguments.get("query"))
        raw_limit = arguments.get("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else _DEFAULT_LIMIT
        except (TypeError, ValueError):
            limit = _DEFAULT_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))

        start_dt = _parse_time(start_time) if start_time else None
        end_dt = _parse_time(end_time) if end_time else None
        if start_time and start_dt is None:
            warnings.append(f"start_time {start_time!r} unparseable; ignored")
        if end_time and end_dt is None:
            warnings.append(f"end_time {end_time!r} unparseable; ignored")

        rows = await self._query(
            scope=scope,
            group_id=group_id,
            user_id=user_id,
            anchor_event_id=anchor_event_id,
            start_dt=start_dt,
            end_dt=end_dt,
            query=query,
            limit=limit,
        )

        snapshots = [_snapshot_from_row(r) for r in rows]
        # 复用 Projector 的折叠逻辑构造 tool_views，让 tool_call 渲染能拼上结果
        tool_views = Projector.fold_tool_results(snapshots)
        items = Projector.build_timeline(snapshots, tool_views=tool_views)

        return ToolOutcome.success(
            {
                "matched": len(items),
                "anchor_event_id": anchor_event_id,
                "items": [
                    {
                        "event_id": it.event_id,
                        "occurred_at": it.occurred_at.isoformat(),
                        "kind": it.kind,
                        "render": it.render,
                    }
                    for it in items
                ],
                "warnings": warnings,
            }
        )

    async def _query(
        self,
        *,
        scope: str,
        group_id: int | None,
        user_id: int | None,
        anchor_event_id: str | None,
        start_dt: datetime | None,
        end_dt: datetime | None,
        query: str | None,
        limit: int,
    ) -> list[AgentEvent]:
        stmt = _build_query_stmt(
            scope=scope,
            group_id=group_id,
            user_id=user_id,
            anchor_event_id=anchor_event_id,
            start_dt=start_dt,
            end_dt=end_dt,
            query=query,
            limit=limit,
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()
        # 投影按时间正序，与正向 timeline 一致；query 命中时 DB 侧是按相似度
        # 排序取的 limit，不能再假设“已经是 occurred_at 倒序”，统一在这重排。
        return sorted(rows, key=lambda r: r.occurred_at)


def _build_query_stmt(
    *,
    scope: str,
    group_id: int | None,
    user_id: int | None,
    anchor_event_id: str | None,
    start_dt: datetime | None,
    end_dt: datetime | None,
    query: str | None,
    limit: int,
) -> Select:
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.scope == scope)
        .where(AgentEvent.visibility == "agent_visible")
    )
    if scope == "group" and group_id is not None:
        stmt = stmt.where(AgentEvent.group_id == group_id)
    if scope == "private" and user_id is not None:
        stmt = stmt.where(AgentEvent.user_id == user_id)
    if anchor_event_id:
        stmt = stmt.where(AgentEvent.event_id < anchor_event_id)
    if start_dt is not None:
        stmt = stmt.where(AgentEvent.occurred_at >= start_dt)
    if end_dt is not None:
        stmt = stmt.where(AgentEvent.occurred_at <= end_dt)
    if query:
        # pg_trgm word_similarity：`<%` 是 gin_trgm_ops 索引真正支持的算子
        # （走 agent_events_search_trgm_idx），语义是"query 是否与 search_text
        # 中某个连续片段模糊相似"，阈值由会话级 GUC
        # pg_trgm.word_similarity_threshold 控制（PG 默认 0.6，要调松紧在
        # DB 层改，不在这写死）。排序另外显式调 word_similarity() 算分——
        # ORDER BY 本身不吃索引，索引只加速上面这行 WHERE。
        similarity = func.word_similarity(query, AgentEvent.search_text)
        stmt = stmt.where(literal(query).op("<%")(AgentEvent.search_text))
        stmt = stmt.order_by(similarity.desc())
    else:
        stmt = stmt.order_by(AgentEvent.occurred_at.desc())
    return stmt.limit(limit)


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_time(s: str) -> datetime | None:
    """ISO8601 → tz-aware datetime（统一到 Asia/Shanghai）。失败返回 None。"""
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return normalize_china_time(dt)
