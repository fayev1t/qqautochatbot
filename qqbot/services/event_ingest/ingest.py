"""EventIngest: NapCat 适配 + 接到统一入口网关 / 注册器。

契约：开发文档/v2.0/20-横切契约/提案-重新设计agent_loop前后的模块以及流水线.md
以及 EventIngest契约.md。

heartbeat 仍旁路。其余上游走入口网关盖 occurred_at → raw 插入 → 注册器：
  外部（NapCat）1s 聚水、并发适配、按 (occurred_at, seq) 排序后发 event_id；
  内部（模型/工具/其它）即时领号落库，不进聚水窗。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.event_gateway.inbound import InboundGateway, UpstreamEnvelope
from qqbot.services.event_gateway.registry import AdaptedEvent, EventRegistrar
from qqbot.services.event_ingest.failure import (
    IngestFailureDetail,
    build_ingest_failure_event,
)
from qqbot.services.event_ingest.heartbeat import write_heartbeat
from qqbot.services.event_ingest.mapper import MapperRegistry
from qqbot.services.event_ingest.media import (
    BatchImageDescriber,
    ImageDescriber,
    attach_media_to_payload,
)
from qqbot.services.event_ingest.napcat_helpers import dump_event
from qqbot.services.event_ingest.persistence import persist_event
from qqbot.services.event_ingest.system_event import (
    PartialSystemEvent,
    SystemEvent,
)

logger = get_logger(__name__)

IngestStatus = Literal[
    "inserted",
    "duplicate",
    "processing_failed",
    "error",
    "heartbeat",
]
SessionFactory = Callable[[], AsyncSession]
CommittedNotifier = Callable[[SystemEvent], Awaitable[None]]

# 这些事实的唤醒由 loop / Runner 自己排（自续拍计数），不能再走静默门
# 的外部 wake（那会把 continuation 清零）。
_LOOP_OWNED_WAKE_TYPES = frozenset(
    {
        "agent.decision_emitted",
        "agent.invalid_action",
        "agent.tool_called",
        "agent.tool_result",
        "agent.tool_failed",
        "agent.program_completed",
        "agent.program_failed",
        "agent.reflection_written",
        "agent.task_note_written",
    }
)


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    event: SystemEvent | None = None
    reason: str | None = None
    events: tuple[SystemEvent, ...] = ()
    prepared: Any = None


class EventIngest:
    """NapCat 适配器挂在统一网关上。决策拍不在这里。"""

    def __init__(
        self,
        registry: MapperRegistry,
        session_factory: SessionFactory,
        committed_notifier: CommittedNotifier | None = None,
        image_describer: ImageDescriber | None = None,
        batch_image_describer: BatchImageDescriber | None = None,
        registration_window_seconds: float | None = None,
        tool_registry: Any = None,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory
        self._committed_notifier = committed_notifier
        self._image_describer = image_describer
        self._batch_image_describer = batch_image_describer
        self._tool_registry = tool_registry
        self._registrar = EventRegistrar(
            adapter=self._adapt,
            persist=self._persist_terminal,
            window_seconds=registration_window_seconds,
        )
        self._gateway = InboundGateway(
            session_factory=session_factory,
            registrar=self._registrar,
        )

    @property
    def gateway(self) -> InboundGateway:
        return self._gateway

    async def ingest(self, event: Any) -> IngestResult:
        if (
            getattr(event, "post_type", None) == "meta_event"
            and getattr(event, "meta_event_type", None) == "heartbeat"
        ):
            await write_heartbeat(event)
            return IngestResult(status="heartbeat")

        payload = dump_event(event)
        if not payload:
            payload = {"_repr": repr(event)}
        return await self._gateway.submit(
            "external",
            payload,
            source=event,
        )

    async def ingest_channel(
        self,
        channel: str,
        payload: dict[str, Any],
        *,
        source: Any = None,
    ) -> IngestResult:
        return await self._gateway.submit(channel, payload, source=source)

    async def _adapt(self, envelope: UpstreamEnvelope) -> AdaptedEvent:
        channel = envelope.channel
        if channel == "external":
            return await self._adapt_napcat(envelope.source)
        if channel == "model":
            return self._adapt_model(envelope.payload)
        if channel == "tool":
            return self._adapt_tool(envelope.payload)
        return self._adapt_other(envelope)

    async def _adapt_napcat(self, event: Any) -> AdaptedEvent:
        try:
            mapper = self._registry.find(event)
        except Exception as exc:
            logger.warning("[event_ingest] mapper lookup failed: {}", exc)
            return self._failure(
                event,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="mapper_lookup_failed",
                        reason="事件映射器查找失败",
                    ),
                ),
            )
        if mapper is None:
            logger.warning(
                "[event_ingest] no mapper matched: post_type={} sub_type={}",
                getattr(event, "post_type", "?"),
                getattr(event, "sub_type", "?"),
            )
            return self._failure(
                event,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="no_mapper",
                        reason="未识别的 NapCat 事件类型",
                    ),
                ),
            )

        try:
            partial: PartialSystemEvent = mapper.map(event)
        except Exception as exc:
            logger.warning(
                "[event_ingest] mapper failed: mapper={} err={}",
                type(mapper).__name__,
                exc,
            )
            return self._failure(
                event,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="mapper_failed",
                        reason="NapCat 事件格式化失败",
                    ),
                ),
            )

        try:
            media_result = await attach_media_to_payload(
                partial.payload,
                self._image_describer,
                batch_describer=self._batch_image_describer,
            )
        except Exception as exc:
            logger.warning("[event_ingest] media preprocessing failed: {}", exc)
            return self._failure(
                event,
                (
                    IngestFailureDetail(
                        stage="media_processing",
                        error_code="media_processing_failed",
                        reason="媒体前置处理失败",
                    ),
                ),
                partial=partial,
            )
        if media_result.failures:
            return self._failure(event, media_result.failures, partial=partial)

        return AdaptedEvent(partial=partial, status="inserted")

    def _adapt_model(self, payload: dict[str, Any]) -> AdaptedEvent:
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("scene") == "planner" and payload.get("ok"):
            return self._adapt_planner_program(payload)
        if "ok" not in payload:
            return AdaptedEvent(
                partial=PartialSystemEvent(
                    origin="runtime",
                    type="runtime.model_responded",
                    scope="system",
                    group_id=None,
                    user_id=None,
                    visibility="runtime_only",
                    payload={"ok": False, "error_kind": "invalid_shape"},
                    raw=payload,
                    idempotency_key=None,
                    correlation_id=_optional_str(payload.get("correlation_id")),
                ),
                status="processing_failed",
                reason="invalid_shape",
            )
        scope = str(payload.get("scope") or "system")
        if scope not in ("system", "group", "private"):
            scope = "system"
        return AdaptedEvent(
            partial=PartialSystemEvent(
                origin="runtime",
                type="runtime.model_responded",
                scope=scope,  # type: ignore[arg-type]
                group_id=_optional_int(payload.get("group_id")),
                user_id=_optional_int(payload.get("user_id")),
                visibility="runtime_only",
                payload=dict(payload),
                raw=dict(payload),
                idempotency_key=None,
                correlation_id=_optional_str(payload.get("correlation_id")),
            ),
            status="inserted" if payload.get("ok") else "processing_failed",
            reason=(
                None
                if payload.get("ok")
                else str(payload.get("error_kind") or "model_failed")
            ),
        )

    def _adapt_planner_program(self, payload: dict[str, Any]) -> AdaptedEvent:
        registry = self._tool_registry
        if registry is None:
            return AdaptedEvent(
                partial=PartialSystemEvent(
                    origin="runtime",
                    type="runtime.model_responded",
                    scope="system",
                    group_id=None,
                    user_id=None,
                    visibility="runtime_only",
                    payload={"ok": False, "error_kind": "planner_registry_missing"},
                    raw=dict(payload),
                    idempotency_key=None,
                ),
                status="processing_failed",
                reason="planner_registry_missing",
            )
        from qqbot.services.agent_loop.program_registrar import adapt_program

        scope_key = str(payload.get("scope_key") or "system")
        scope = scope_key.split(":", 1)[0]
        tick_seq = payload.get("tick_seq")
        try:
            tick_seq_i = int(tick_seq) if tick_seq is not None else 0
        except (TypeError, ValueError):
            tick_seq_i = 0
        adapted = adapt_program(
            raw_program=payload.get("program") or payload.get("text") or "",
            registry=registry,
            scope=scope,
            scope_key=scope_key,
            correlation_id=str(payload.get("correlation_id") or ""),
            tick_seq=tick_seq_i,
        )
        return AdaptedEvent(
            partial=adapted.partial,
            status="inserted",
            prepared=adapted.prepared,
        )

    def _adapt_tool(self, payload: dict[str, Any]) -> AdaptedEvent:
        if not isinstance(payload, dict):
            payload = {}
        batch = payload.get("events")
        if isinstance(batch, list) and batch:
            partials = [
                _internal_partial(item, fallback=payload) for item in batch
            ]
            return AdaptedEvent(
                partial=partials[0],
                status="inserted",
                siblings=tuple(partials[1:]),
            )
        scope = str(payload.get("scope") or "system")
        if scope not in ("system", "group", "private"):
            scope = "system"
        return AdaptedEvent(
            partial=PartialSystemEvent(
                origin="runtime",
                type="runtime.tool_responded",
                scope=scope,  # type: ignore[arg-type]
                group_id=_optional_int(payload.get("group_id")),
                user_id=_optional_int(payload.get("user_id")),
                visibility="runtime_only",
                payload=dict(payload),
                raw=dict(payload),
                idempotency_key=None,
                correlation_id=_optional_str(payload.get("correlation_id")),
            ),
            status="inserted",
        )

    def _adapt_other(self, envelope: UpstreamEnvelope) -> AdaptedEvent:
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        event_type = str(payload.get("event_type") or "")
        if event_type == "runtime.silence_elapsed":
            scope = payload.get("scope") or "group"
            if scope not in ("system", "group", "private"):
                scope = "group"
            visibility = payload.get("visibility") or "agent_visible"
            if visibility not in ("agent_visible", "runtime_only"):
                visibility = "agent_visible"
            seconds = payload.get("seconds")
            return AdaptedEvent(
                partial=PartialSystemEvent(
                    origin="runtime",
                    type="runtime.silence_elapsed",
                    scope=scope,  # type: ignore[arg-type]
                    group_id=_optional_int(payload.get("group_id")),
                    user_id=_optional_int(payload.get("user_id")),
                    visibility=visibility,  # type: ignore[arg-type]
                    payload={"seconds": seconds},
                    raw=dict(payload),
                    idempotency_key=None,
                    correlation_id=_optional_str(payload.get("correlation_id")),
                ),
                status="inserted",
            )
        if event_type:
            return AdaptedEvent(
                partial=_internal_partial(payload, fallback=payload),
                status="inserted",
            )
        return AdaptedEvent(
            partial=PartialSystemEvent(
                origin="runtime",
                type="runtime.other_event",
                scope="system",
                group_id=None,
                user_id=None,
                visibility="runtime_only",
                payload=dict(payload),
                raw=dict(payload),
                idempotency_key=None,
            ),
            status="inserted",
        )

    def _failure(
        self,
        event: Any,
        failures: tuple[IngestFailureDetail, ...],
        *,
        partial: PartialSystemEvent | None = None,
    ) -> AdaptedEvent:
        return AdaptedEvent(
            partial=build_ingest_failure_event(event, failures, partial=partial),
            status="processing_failed",
            reason=failures[0].error_code,
        )

    async def _persist_terminal(
        self,
        events: SystemEvent | list[SystemEvent],
        inserted_status: str,
        reason: str | None,
    ) -> IngestResult:
        batch = events if isinstance(events, list) else [events]
        if not batch:
            return IngestResult(status="error", reason="empty_batch")
        primary = batch[0]
        try:
            async with self._session_factory() as session:
                inserted_any = False
                for sys_event in batch:
                    inserted = await persist_event(
                        session, sys_event, commit=False
                    )
                    inserted_any = inserted_any or inserted
                await session.commit()
        except Exception as exc:
            logger.error(
                "[event_ingest] persist failed: type={} err={}",
                primary.type,
                exc,
            )
            return IngestResult(
                status="error",
                event=primary,
                events=tuple(batch),
                reason=str(exc),
            )

        if not inserted_any:
            logger.info(
                "[event_ingest] duplicate skipped: type={} key={}",
                primary.type,
                primary.idempotency_key,
            )
            return IngestResult(
                status="duplicate", event=primary, events=tuple(batch)
            )

        should_notify = not any(
            item.type in _LOOP_OWNED_WAKE_TYPES for item in batch
        )
        if should_notify:
            for sys_event in batch:
                await self._notify_committed(sys_event)
        status: IngestStatus = (
            "processing_failed"
            if inserted_status == "processing_failed"
            else "inserted"
        )
        return IngestResult(
            status=status,
            event=primary,
            events=tuple(batch),
            reason=reason,
        )

    async def _notify_committed(self, event: SystemEvent) -> None:
        if self._committed_notifier is None:
            return
        try:
            await self._committed_notifier(event)
        except Exception as exc:
            logger.warning("[event_ingest] committed notifier failed: {}", exc)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _internal_partial(
    item: Any, *, fallback: dict[str, Any]
) -> PartialSystemEvent:
    """把内部通道的一条草稿收成 Partial。item 可以是事件 dict。"""
    if not isinstance(item, dict):
        item = {}
    event_type = str(
        item.get("event_type") or item.get("type") or fallback.get("event_type") or ""
    )
    if not event_type:
        event_type = "runtime.other_event"
    origin = "agent" if event_type.startswith("agent.") else "runtime"
    scope_key = str(item.get("scope_key") or fallback.get("scope_key") or "")
    scope = str(item.get("scope") or fallback.get("scope") or "")
    group_id = _optional_int(item.get("group_id") or fallback.get("group_id"))
    user_id = _optional_int(item.get("user_id") or fallback.get("user_id"))
    if scope_key:
        parsed_scope, parsed_gid, parsed_uid = _split_scope_key(scope_key)
        scope = scope or parsed_scope
        if group_id is None:
            group_id = parsed_gid
        if user_id is None:
            user_id = parsed_uid
    if scope not in ("system", "group", "private"):
        scope = "system"
    visibility = str(
        item.get("visibility") or fallback.get("visibility") or "agent_visible"
    )
    if visibility not in ("agent_visible", "runtime_only"):
        visibility = "agent_visible"
    inner = item.get("payload")
    if not isinstance(inner, dict):
        inner = {
            key: value
            for key, value in item.items()
            if key
            not in {
                "event_type",
                "type",
                "scope",
                "scope_key",
                "group_id",
                "user_id",
                "visibility",
                "correlation_id",
                "causation_id",
                "event_id",
                "payload",
                "origin",
            }
        }
    return PartialSystemEvent(
        origin=origin,  # type: ignore[arg-type]
        type=event_type,
        scope=scope,  # type: ignore[arg-type]
        group_id=group_id,
        user_id=user_id,
        visibility=visibility,  # type: ignore[arg-type]
        payload=inner if isinstance(inner, dict) else {},
        raw=None,
        idempotency_key=None,
        correlation_id=_optional_str(
            item.get("correlation_id") or fallback.get("correlation_id")
        ),
        causation_id=_optional_str(item.get("causation_id")),
        event_id=_optional_str(item.get("event_id")),
    )


def _split_scope_key(scope_key: str) -> tuple[str, int | None, int | None]:
    if scope_key == "system":
        return "system", None, None
    if scope_key.startswith("group:"):
        try:
            return "group", int(scope_key.split(":", 1)[1]), None
        except ValueError:
            return "group", None, None
    if scope_key.startswith("private:"):
        try:
            return "private", None, int(scope_key.split(":", 1)[1])
        except ValueError:
            return "private", None, None
    return "system", None, None
