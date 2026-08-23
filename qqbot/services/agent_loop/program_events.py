"""Append-only event protocol for program execution and crash recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop.event_writer import (
    AgentEventWrite,
    parse_scope_key,
    write_agent_event,
    write_agent_events,
)
from qqbot.services.agent_loop.tool_registry import ToolOutcome
from qqbot.services.event_gateway.registry import issue_event_id

if TYPE_CHECKING:
    from datetime import datetime

SessionFactory = Callable[[], AsyncSession]

_TOOL_TERMINALS = frozenset({"agent.tool_result", "agent.tool_failed"})
# 收口只认工具调用（2026-08-17 提案-裁决流水线）：``agent.decision_emitted``
# 不再参与——决策事件和时间线上任何一条事件同级，写进去就是一段文本，没有
# 状态、没有生命周期、不需要收口。"这条执行过没有"永远是对 append-only 事件流
# 的一次查询（有没有以它为因的 program terminal），不是被维护的标志位。
_RECOVERY_TYPES = tuple({"agent.tool_called", *_TOOL_TERMINALS})
_PROGRAM_TERMINALS = frozenset({"agent.program_completed", "agent.program_failed"})


@dataclass(frozen=True)
class EffectCallHandle:
    tool_call_id: str
    called_event_id: str
    decision_id: str
    tool_name: str
    call_site: str
    occurrence: int


@dataclass(frozen=True)
class RecoveryReport:
    tool_calls_closed: int = 0


@dataclass(frozen=True)
class ProgramAsset:
    """``execute_program(program_hash=…)`` 指名的那份代码资产。

    ``event_id`` / ``correlation_id`` 是这份源码**落库那一拍**的出处：被执行
    程序沿用它们，因为那些 ``tool_called`` / terminal 归属程序的出处，不归属
    按下执行键的这一拍。同一份资产被多次提案时取最新的一条出处。
    """

    event_id: str
    correlation_id: str
    program: str
    program_sha256: str
    program_hash: str


async def begin_effect_call(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    decision_id: str,
    tool_name: str,
    arguments: dict,
    triggered_by_event_id: str | None,
    bot_role: str | None,
    call_site: str,
    occurrence: int,
    occurred_at: datetime | None = None,
) -> EffectCallHandle:
    """Transaction 1: persist intent before any external side effect."""
    tool_call_id = new_event_id()
    called_event_id = issue_event_id()
    payload = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "arguments": arguments,
        "triggered_by_event_id": triggered_by_event_id,
        "bot_role": bot_role,
        "program_call_site": call_site,
        "program_occurrence": occurrence,
    }
    writes = [
        AgentEventWrite(
            event_type="agent.tool_called",
            causation_id=decision_id,
            payload=payload,
            occurred_at=occurred_at,
            event_id=called_event_id,
        )
    ]
    # 这里曾在 task_id 非空时伴生一条 agent.task_state_changed(pending→running)，
    # 把调用"挂"到任务上。随任务坍缩为单栏便签一并删除（2026-08-21）：便签没有
    # 状态机，也就没有可迁移的状态。
    await write_agent_events(
        session_factory,
        scope_key=scope_key,
        correlation_id=correlation_id,
        events=writes,
    )
    return EffectCallHandle(
        tool_call_id=tool_call_id,
        called_event_id=called_event_id,
        decision_id=decision_id,
        tool_name=tool_name,
        call_site=call_site,
        occurrence=occurrence,
    )


async def finish_effect_call(
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    handle: EffectCallHandle,
    outcome: ToolOutcome,
) -> str:
    """Transaction 2: generated domain events plus exactly one terminal."""
    writes = [
        AgentEventWrite(
            event_type=event.event_type,
            causation_id=handle.called_event_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        for event in outcome.emitted_events
    ]
    writes.append(_tool_terminal_write(handle, outcome))
    event_ids = await write_agent_events(
        session_factory,
        scope_key=scope_key,
        correlation_id=correlation_id,
        events=writes,
    )
    return event_ids[-1]


def uncertain_outcome(
    *,
    error_kind: str,
    error_message: str,
    status: str = "uncertain",
    **extra: Any,
) -> ToolOutcome:
    return ToolOutcome.failure(
        error_kind,
        error_message,
        status=status,
        **extra,
    )


def _tool_terminal_write(
    handle: EffectCallHandle, outcome: ToolOutcome
) -> AgentEventWrite:
    common = {
        "tool_call_id": handle.tool_call_id,
        "tool_name": handle.tool_name,
    }
    if outcome.ok:
        return AgentEventWrite(
            event_type="agent.tool_result",
            causation_id=handle.called_event_id,
            payload={**common, "result": outcome.result},
        )
    payload = {
        **common,
        "error_kind": outcome.error_kind or "internal_tool_error",
        "error_message": outcome.error_message or "tool call failed",
    }
    if isinstance(outcome.extra, dict):
        payload.update(outcome.extra)
    return AgentEventWrite(
        event_type="agent.tool_failed",
        causation_id=handle.called_event_id,
        payload=payload,
    )


async def write_program_completed(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    decision_id: str,
    program_sha256: str,
    duration_ms: int,
    query_calls: list[str],
    effect_call_ids: list[str],
    result: Any,
    has_result: bool,
    program_hash: str | None = None,
    dispatch_event_id: str | None = None,
) -> str:
    return await write_agent_event(
        session_factory,
        event_type="agent.program_completed",
        scope_key=scope_key,
        correlation_id=correlation_id,
        causation_id=decision_id,
        payload={
            "decision_id": decision_id,
            "program_sha256": program_sha256,
            # 一次运行的身份 = (调度事件, 资产 hash)。取消 already_executed 后
            # 同一份资产可以合法并发跑多次，只凭 hash 分不出是哪一次；
            # dispatch_event_id 是下达 execute_program 的那条决策事件。
            # 空程序在自己那一拍收口，没有调度事件，两者都是 None。
            "program_hash": program_hash,
            "dispatch_event_id": dispatch_event_id,
            "duration_ms": duration_ms,
            "query_calls": query_calls,
            "effect_call_ids": effect_call_ids,
            "result": result,
            "has_result": has_result,
        },
    )


async def write_program_failed(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    decision_id: str,
    program_sha256: str,
    duration_ms: int,
    query_calls: list[str],
    effect_call_ids: list[str],
    error_kind: str,
    error_message: str,
    failed_call: dict[str, Any] | None = None,
    program_hash: str | None = None,
    dispatch_event_id: str | None = None,
    **details: Any,
) -> str:
    payload: dict[str, Any] = {
        "decision_id": decision_id,
        "program_sha256": program_sha256,
        # 见 write_program_completed：一次运行 = (调度事件, 资产 hash)。
        "program_hash": program_hash,
        "dispatch_event_id": dispatch_event_id,
        "duration_ms": duration_ms,
        "query_calls": query_calls,
        "effect_call_ids": effect_call_ids,
        "error_kind": error_kind,
        "error_message": str(error_message)[:1000],
        "failed_call": failed_call,
    }
    payload.update({key: value for key, value in details.items() if value is not None})
    return await write_agent_event(
        session_factory,
        event_type="agent.program_failed",
        scope_key=scope_key,
        correlation_id=correlation_id,
        causation_id=decision_id,
        payload=payload,
    )


async def recover_interrupted_programs(
    session_factory: SessionFactory,
    *,
    scope_key: str,
) -> RecoveryReport:
    """Close every pre-existing half tool call in one scope; never replay.

    只收口 ``agent.tool_called``——那是真正发生过外部副作用、投递状态存疑的
    地方。决策事件不收口（见 ``_RECOVERY_TYPES`` 上的说明）；进程异常关闭后
    已开跑程序的整体收束由后续统一方案处理，不在这里。
    """
    rows = await _load_recovery_rows(session_factory, scope_key=scope_key)
    terminal_causes = {
        str(row.causation_id)
        for row in rows
        if row.type in _TOOL_TERMINALS and row.causation_id
    }
    calls = [row for row in rows if row.type == "agent.tool_called"]

    tool_calls_closed = 0
    for row in calls:
        if str(row.event_id) in terminal_causes:
            continue
        payload = row.payload or {}
        await write_agent_event(
            session_factory,
            event_type="agent.tool_failed",
            scope_key=scope_key,
            correlation_id=str(row.correlation_id or new_event_id()),
            causation_id=str(row.event_id),
            payload={
                "tool_call_id": payload.get("tool_call_id"),
                "tool_name": payload.get("tool_name"),
                "error_kind": "interrupted",
                "error_message": (
                    "process stopped before this effect produced a terminal; "
                    "delivery state is uncertain and the call was not replayed"
                ),
                "status": "uncertain",
            },
        )
        tool_calls_closed += 1

    return RecoveryReport(tool_calls_closed)


async def load_program_asset(
    session_factory: SessionFactory,
    *,
    scope_key: str,
    program_hash: str,
) -> tuple[ProgramAsset | None, str | None]:
    """按 12 位 hash 取本 scope 资产库里的一份代码资产。

    返回 ``(asset, error_kind)``——``error_kind`` 为 None 时 ``asset`` 必然非
    None。只有一种拒绝：

    - ``program_not_found``：本 scope 的资产库里没有这个 hash。

    **资产库不是一张表**，而是对 append-only 事件流的一次查询：本 scope 里
    ``payload.program_hash`` 等于该值的 ``agent.decision_emitted``。写入侧只在
    这一拍确实留下了动作层代码时才写 ``program_hash``（loop.py），因此空程序与
    纯裁决拍不进资产库、指名不到——旧的 ``decision_not_a_proposal`` 由此并入
    ``program_not_found``。

    **没有 already_executed 守卫**（2026-08-21）。同源码必然同 hash，这是内容
    寻址的应有之义：一份资产可以反复调度，调度几次跑几次，重复副作用由模型读
    ``<program_result>`` 自行判断。此前那条查询（存不存在以决策为因的
    ``tool_called`` 或 program terminal）连同它挡住的两件事一并撤销：

    1. 并发派发下"程序还在跑的那几十秒里被再裁决一次"——现在不拦，暴露窗口
       实际只到首次 ``tool_called`` 落库为止，此后 ``<tool>已调用`` 即提供在途
       可见性；
    2. 崩溃重启后重复副作用——半截 ``tool_called`` 仍被收成 uncertain，但不再
       有任何东西阻止模型把同一 hash 再调度一次。这是资产语义的直接代价，
       已在契约里登记（任务与决策契约 §12、待办 #22）。

    同一 hash 有多条提案时取**最新**一条：源码逐字相同，取谁都一样，取最新是为
    让沿用的 correlation_id 贴近当前这段对话。
    """
    scope, group_id, user_id = parse_scope_key(scope_key)
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.type == "agent.decision_emitted")
        .where(AgentEvent.scope == scope)
        .where(AgentEvent.payload["program_hash"].astext == program_hash)
        .order_by(AgentEvent.event_id.desc())
        .limit(1)
    )
    if scope == "group":
        stmt = stmt.where(AgentEvent.group_id == group_id)
    elif scope == "private":
        stmt = stmt.where(AgentEvent.user_id == user_id)
    async with session_factory() as session:
        row = (await session.execute(stmt)).scalars().first()
    if row is None:
        return None, "program_not_found"
    payload = row.payload if isinstance(row.payload, dict) else {}
    program = payload.get("program")
    if not isinstance(program, str) or not program.strip():
        return None, "program_not_found"
    return (
        ProgramAsset(
            event_id=str(row.event_id),
            correlation_id=str(row.correlation_id or ""),
            program=program,
            program_sha256=str(payload.get("program_sha256") or ""),
            program_hash=program_hash,
        ),
        None,
    )


async def _load_recovery_rows(
    session_factory: SessionFactory, scope_key: str
) -> list[AgentEvent]:
    scope, group_id, user_id = parse_scope_key(scope_key)
    stmt = (
        select(AgentEvent)
        .where(AgentEvent.scope == scope)
        .where(AgentEvent.type.in_(_RECOVERY_TYPES))
    )
    if scope == "group":
        stmt = stmt.where(AgentEvent.group_id == group_id)
    elif scope == "private":
        stmt = stmt.where(AgentEvent.user_id == user_id)
    async with session_factory() as session:
        result = await session.execute(stmt)
        return list(result.scalars().all())


__all__ = [
    "EffectCallHandle",
    "ProgramAsset",
    "RecoveryReport",
    "begin_effect_call",
    "finish_effect_call",
    "load_program_asset",
    "recover_interrupted_programs",
    "uncertain_outcome",
    "write_program_completed",
    "write_program_failed",
]
