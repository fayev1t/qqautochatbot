"""GetRecentThoughtsTool —— 按需取回自己最近若干拍写下的程序注释。

设计动机（2026-08-03）：她每一拍的判读本来就在落库——`agent.decision_emitted`
的 ``payload.program`` 存的是完整源码，注释在里面，append-only、永不删除
（`loop.py` 决策落库处）。缺的从来不是记录，是**没有任何东西读它**。反思要成立
就需要这批素材：注释是她当时**真实**的想法，不是事后重构；把它和后来实际发生
的事摆在一起，才谈得上"与当时的判断差在哪"。

2026-08-14 起当拍源码会作为 ``<action>`` 进入时间线。本工具不再承担
「找回上一拍程序」的兜底，只提供跨多拍抽取注释的便利：返回值不含源码
本身，也不含那些拍的工具结果。主动调用仍会留下 ``<tool>`` 终态。

已知残余风险，照实记在这里：注释里若写了草稿措辞（"# 准备说：……"），取回来
之后仍可能被照抄。这条靠 `planner.md` 的既有纪律约束（落笔依据是当下时间线，
不是先前想好的那套），本工具不做内容过滤——过滤等于让工具判断哪句是草稿，
那是模型的活。

scope 隔离：只取当前 scope 自己的决策事件，与 search_history 同口径。
"""

from __future__ import annotations

import io
import tokenize
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "get_recent_thoughts.md")

DEFAULT_LIMIT = 20
MAX_LIMIT = 30
DEFAULT_WITHIN_HOURS = 6
MAX_WITHIN_HOURS = 24
# 单拍注释合并后的上限。超出截断——这里和 reflect 的取舍相反：注释是素材不是
# 结论，丢掉尾部不会让她误把半句话当成自己的完整想法。
MAX_NOTES_CHARS = 400


class GetRecentThoughtsTool(BaseTool):
    """实现 Tool 协议。GUEST：读自己写过的东西，不涉及任何人的信息。"""

    name = "get_recent_thoughts"
    program_kind = "effect"
    max_call_sites = 4
    description = (
        "取回当前 scope 最近若干拍程序里的注释文本，按时间正序返回。"
        "用于回想自己先前几拍分别在想什么；不含程序源码本身，"
        "也不含任何查询结果或动作记录。"
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "description": (
                    f"最多返回多少拍，取值 1–{MAX_LIMIT}，缺省 {DEFAULT_LIMIT}。"
                    "无注释的拍不占名额。"
                ),
            },
            "within_hours": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_WITHIN_HOURS,
                "description": (
                    f"只看最近这么多小时内的拍，取值 1–{MAX_WITHIN_HOURS}，"
                    f"缺省 {DEFAULT_WITHIN_HOURS}。"
                ),
            },
        },
        "required": [],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "returned": {"type": "integer"},
            "ticks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "at": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "required": ["at", "notes"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["returned", "ticks"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> Any:
        if fail := await self.enforce_access(context):
            return fail

        limit, fail = _bounded_int(
            arguments.get("limit"), DEFAULT_LIMIT, 1, MAX_LIMIT, "limit"
        )
        if fail:
            return fail
        within_hours, fail = _bounded_int(
            arguments.get("within_hours"),
            DEFAULT_WITHIN_HOURS,
            1,
            MAX_WITHIN_HOURS,
            "within_hours",
        )
        if fail:
            return fail

        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        if not scope_key or session_factory is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "get_recent_thoughts unavailable: missing scope/session context",
            )
        try:
            scope, group_id, user_id = parse_scope_key(str(scope_key))
        except ValueError:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"invalid scope_key: {scope_key!r}",
                reason_code="scope_key_invalid",
            )

        since = china_now() - timedelta(hours=within_hours)
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.type == "agent.decision_emitted")
            .where(AgentEvent.scope == scope)
            .where(AgentEvent.occurred_at >= since)
        )
        if group_id is not None:
            stmt = stmt.where(AgentEvent.group_id == group_id)
        if user_id is not None:
            stmt = stmt.where(AgentEvent.user_id == user_id)
        # 倒序多取一些再在应用层过滤——无注释的拍不该占用 limit 名额，
        # 而"有没有注释"要解析完源码才知道，SQL 侧没法先滤掉。
        stmt = stmt.order_by(
            AgentEvent.occurred_at.desc(), AgentEvent.event_id.desc()
        ).limit(limit * 3)

        async with session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()

        ticks: list[dict[str, str]] = []
        for row in rows:
            if len(ticks) >= limit:
                break
            notes = _extract_comments((row.payload or {}).get("program"))
            if not notes:
                continue
            ticks.append(
                {"at": row.occurred_at.isoformat(timespec="seconds"), "notes": notes}
            )
        # 查询按倒序取，返回给模型时翻回时间正序，与 timeline 阅读方向一致。
        ticks.reverse()
        return ToolOutcome.success({"returned": len(ticks), "ticks": ticks})


def _bounded_int(
    raw: Any, default: int, low: int, high: int, field: str
) -> tuple[int, ToolOutcome | None]:
    """可选整数参数的取值与边界检查。**返回**失败，不 raise。"""
    if raw is None:
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default, ToolOutcome.failure(
            "invalid_arguments",
            f"{field} must be an integer",
            reason_code=f"{field}_not_int",
        )
    if not (low <= raw <= high):
        return default, ToolOutcome.failure(
            "invalid_arguments",
            f"{field} must be within [{low}, {high}], got {raw}",
            reason_code=f"{field}_out_of_range",
        )
    return raw, None


def _extract_comments(source: Any) -> str:
    """从一段程序源码里取出全部注释文本，按行合并。

    用 ``tokenize`` 而不是逐行找 ``#``：字符串字面量里的 ``#`` 不是注释，
    朴素扫描会把 f-string 里的井号当成注释切出来。源码可能来自**预检未通过**
    的那一拍（decision_emitted 在执行之前就写了），此时 tokenize 会抛错——
    整拍跳过，不做降级扫描：语法都不成立的源码里切出来的"注释"不可信。
    """
    if not isinstance(source, str) or not source.strip():
        return ""
    lines: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            text = token.string.lstrip("#").strip()
            if text:
                lines.append(text)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return ""
    if not lines:
        return ""
    merged = "\n".join(lines)
    if len(merged) > MAX_NOTES_CHARS:
        merged = merged[:MAX_NOTES_CHARS] + "…"
    return merged
