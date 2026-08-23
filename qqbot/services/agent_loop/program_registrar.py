"""注册层适配：一次模型响应变成代码资产或非法行动。

2026-08-21（渲染格式表 §一⑦）把 AST 预检从 tick 体里搬出来。本次按流水线
提案把**调用点**收到 ``channel=model`` 适配器：本模块只产出
``PartialSystemEvent``（外加 loop 派发要用的 ``PreflightResult``），发号与
落库是注册器的事。

``register_model_response`` 仍给无网关的降级路径（契约测试 / 未接线进程）
用：有入口网关时由 ``EventIngest._adapt_model`` 调 ``adapt_program``。

契约：事件流渲染格式表.md §一⑦；提案 ③⑥⑪。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.event_writer import write_agent_event
from qqbot.services.agent_loop.program_ast import (
    MAX_SOURCE_CHARS,
    PreflightResult,
    ProgramErrorInfo,
    ProgramPreflightError,
    preflight,
)
from qqbot.services.event_ingest.system_event import PartialSystemEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)

SessionFactory = Callable[[], "AsyncSession"]

DECISION_EVENT_TYPE = "agent.decision_emitted"
INVALID_ACTION_EVENT_TYPE = "agent.invalid_action"


@dataclass(frozen=True, slots=True)
class AdaptedProgram:
    """预检产物：还没发号的 partial + loop 需要的 prepared。"""

    partial: PartialSystemEvent
    accepted: bool
    program: str
    program_sha256: str
    prepared: PreflightResult | None = None
    error: ProgramErrorInfo | None = None
    left_asset: bool = False


@dataclass(frozen=True, slots=True)
class RegisteredResponse:
    """一次模型响应的登记结果。

    ``accepted`` 为真时 ``prepared`` 必非空、``event_id`` 指向那条
    ``agent.decision_emitted``；为假时 ``error`` 必非空、``event_id`` 指向那条
    ``agent.invalid_action``。两种情形都**恰好**在时间线上留下一条事件。
    """

    event_id: str
    accepted: bool
    program: str
    program_sha256: str
    prepared: PreflightResult | None = None
    error: ProgramErrorInfo | None = None
    left_asset: bool = False


def bounded_program_source(value: Any) -> str:
    source = value if isinstance(value, str) else str(value)
    return source[:MAX_SOURCE_CHARS]


def program_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _scope_parts(scope_key: str) -> tuple[str, int | None, int | None]:
    if scope_key == "system":
        return "system", None, None
    if scope_key.startswith("group:"):
        return "group", int(scope_key.split(":", 1)[1]), None
    if scope_key.startswith("private:"):
        return "private", None, int(scope_key.split(":", 1)[1])
    return "system", None, None


def _reason_sentence(info: ProgramErrorInfo) -> str:
    position = ""
    if info.line is not None:
        position = f" line={info.line}"
        if info.column is not None:
            position += f" column={info.column}"
    return f"{info.error_kind}{position}: {info.message}"


def adapt_program(
    *,
    raw_program: Any,
    registry: ToolRegistry,
    scope: str,
    scope_key: str,
    correlation_id: str,
    tick_seq: int,
) -> AdaptedProgram:
    """纯适配：源码 → decision_emitted / invalid_action 的 partial。不写库。"""
    source = bounded_program_source(raw_program)
    digest = program_sha256(source)
    event_scope, group_id, user_id = _scope_parts(scope_key)
    if event_scope not in ("system", "group", "private"):
        event_scope = "system"
    try:
        prepared = preflight(source, registry, scope)
    except ProgramPreflightError as exc:
        payload: dict[str, Any] = {
            "reason": _reason_sentence(exc.info),
            "error_kind": exc.info.error_kind,
            "raw_text": source,
            "tick_seq": tick_seq,
        }
        if exc.info.line is not None:
            payload["line"] = exc.info.line
        if exc.info.column is not None:
            payload["column"] = exc.info.column
        for key, value in (exc.info.details or {}).items():
            payload.setdefault(key, value)
        logger.info(
            "[registrar {}] invalid action: {} ({})",
            scope_key,
            exc.info.error_kind,
            exc.info.message,
        )
        return AdaptedProgram(
            partial=PartialSystemEvent(
                origin="agent",
                type=INVALID_ACTION_EVENT_TYPE,
                scope=event_scope,  # type: ignore[arg-type]
                group_id=group_id,
                user_id=user_id,
                visibility="agent_visible",
                payload=payload,
                raw=None,
                idempotency_key=None,
                correlation_id=correlation_id,
            ),
            accepted=False,
            program=source,
            program_sha256=digest,
            error=exc.info,
        )

    stored_program = prepared.source
    left_asset = bool(prepared.call_sites or prepared.has_return)
    decision_payload: dict[str, Any] = {
        "program": stored_program,
        "program_sha256": prepared.program_sha256,
        "tick_seq": tick_seq,
    }
    if left_asset:
        decision_payload["program_hash"] = prepared.program_hash
    if prepared.commit_program_hash is not None:
        decision_payload["commit_program_hash"] = prepared.commit_program_hash
    return AdaptedProgram(
        partial=PartialSystemEvent(
            origin="agent",
            type=DECISION_EVENT_TYPE,
            scope=event_scope,  # type: ignore[arg-type]
            group_id=group_id,
            user_id=user_id,
            visibility="agent_visible",
            payload=decision_payload,
            raw=None,
            idempotency_key=None,
            correlation_id=correlation_id,
        ),
        accepted=True,
        program=stored_program,
        program_sha256=prepared.program_sha256,
        prepared=prepared,
        left_asset=left_asset,
    )


async def register_model_response(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    scope_key: str,
    scope: str,
    correlation_id: str,
    raw_program: Any,
    registry: ToolRegistry,
    tick_seq: int,
    now: datetime | None = None,
) -> RegisteredResponse:
    """无网关时的降级登记：预检后经 event_writer 直写。

    生产路径由入口网关 ``channel=model`` 适配器调用 ``adapt_program``。
    """
    adapted = adapt_program(
        raw_program=raw_program,
        registry=registry,
        scope=scope,
        scope_key=scope_key,
        correlation_id=correlation_id,
        tick_seq=tick_seq,
    )
    event_id = await write_agent_event(
        session_factory,
        event_type=adapted.partial.type,
        scope_key=scope_key,
        correlation_id=correlation_id,
        causation_id=adapted.partial.causation_id,
        payload=adapted.partial.payload,
        occurred_at=now,
    )
    return RegisteredResponse(
        event_id=event_id,
        accepted=adapted.accepted,
        program=adapted.program,
        program_sha256=adapted.program_sha256,
        prepared=adapted.prepared,
        error=adapted.error,
        left_asset=adapted.left_asset,
    )


__all__ = [
    "DECISION_EVENT_TYPE",
    "INVALID_ACTION_EVENT_TYPE",
    "AdaptedProgram",
    "RegisteredResponse",
    "adapt_program",
    "bounded_program_source",
    "program_sha256",
    "register_model_response",
]
