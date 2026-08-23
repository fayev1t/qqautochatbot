"""统一事件注册器。

外部入站：激活后收集 1s，窗口内并发走适配器，再按 occurred_at（同刻用 seq）
排序，然后发 event_id 并落事件流。

内部事件（模型/工具/其它入口、以及 writer 领号）：即时向本层领号落库，
不进 1s 聚水窗。

不注入 register_at。不改写业务内容指纹（file_hash / program_sha256 /
idempotency_key），只做结构校验。

模块顶层不导入 ``event_ingest.ingest`` / ``system_event``（``finalize`` 在
登记时再取）。否则 ``event_writer → registry → event_ingest → ingest →
registry`` 会在启动时环形失败。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger

if TYPE_CHECKING:
    from qqbot.services.event_ingest.system_event import (
        PartialSystemEvent,
        SystemEvent,
    )

logger = get_logger(__name__)

# 生产由 EventIngest(..., registration_window_seconds=1.0) 打开。
# 契约测试默认 0，避免每条 ingest 空等一秒。
_REGISTRATION_WINDOW_SECONDS = 0.0
_POOLED_CHANNELS = frozenset({"external"})

AdaptFn = Callable[[Any], Awaitable["AdaptedEvent"]]
# SystemEvent 只在 TYPE_CHECKING 里导入；这是赋值不是注解，必须写成前向引用。
PersistFn = Callable[["SystemEvent", str, str | None], Awaitable[Any]]


def issue_event_id() -> str:
    """事件身份唯一发放口。

    外部聚水排序后、内部即时领号，都走这里。Mapper / finalize / 各模块
    不得私自 mint ``agent_events.event_id``。
    """
    return new_event_id()


def _validate_content_fingerprints(partial: PartialSystemEvent) -> None:
    """注册层不改写业务指纹，只检查形状。缺失合法；类型不对才记警告。"""
    key = partial.idempotency_key
    if key is not None and not isinstance(key, str):
        logger.warning(
            "[event_registry] idempotency_key is not str: {!r}", type(key).__name__
        )
    payload = partial.payload
    if not isinstance(payload, dict):
        return
    sha = payload.get("program_sha256")
    if sha is not None and not isinstance(sha, str):
        logger.warning("[event_registry] program_sha256 is not str")
    for item in payload.get("segments") or []:
        if not isinstance(item, dict):
            continue
        file_hash = item.get("file_hash")
        if file_hash is not None and not isinstance(file_hash, str):
            logger.warning("[event_registry] file_hash is not str")


@dataclass(frozen=True, slots=True)
class AdaptedEvent:
    """适配器产物：尚未发身份的 partial。

    ``siblings`` 与 ``partial`` 同一事务落库（工具回执 + 同事务领域事件）。
    ``prepared`` 仅 planner 预检成功时带上，给 loop 派发用，不进库。
    """

    partial: PartialSystemEvent
    status: Literal["inserted", "processing_failed"]
    reason: str | None = None
    prepared: Any = None
    siblings: tuple[Any, ...] = ()


class EventRegistrar:
    """全局一只窗口，不分群。外部聚水；内部即时领号。"""

    def __init__(
        self,
        *,
        adapter: AdaptFn,
        persist: PersistFn,
        window_seconds: float | None = None,
    ) -> None:
        self._adapter = adapter
        self._persist = persist
        self._window = (
            _REGISTRATION_WINDOW_SECONDS
            if window_seconds is None
            else float(window_seconds)
        )
        self._buffer: list[Any] = []
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._seq = 0

    def allocate_seq(self) -> int:
        self._seq += 1
        return self._seq

    def issue_event_id(self) -> str:
        return issue_event_id()

    async def enqueue(self, envelope: Any) -> Any:
        channel = str(getattr(envelope, "channel", "") or "")
        if channel not in _POOLED_CHANNELS:
            return await self.register_now(envelope)
        async with self._lock:
            self._buffer.append(envelope)
            if self._task is None:
                self._task = asyncio.create_task(self._run_window())
        return await envelope.future

    async def register_now(self, envelope: Any) -> Any:
        """内部事件：跳过聚水窗，即时适配、发 event_id、落库。"""
        item = await self._adapt_one(envelope)
        await self._register_one(envelope, item)
        return await envelope.future

    async def _run_window(self) -> None:
        delay = self._window
        if delay > 0:
            await asyncio.sleep(delay)
        async with self._lock:
            batch = self._buffer
            self._buffer = []
            self._task = None
        if not batch:
            return
        try:
            adapted = await asyncio.gather(
                *[self._adapt_one(item) for item in batch],
            )
            ordered = sorted(
                zip(batch, adapted, strict=True),
                key=lambda pair: (pair[0].occurred_at, pair[0].seq),
            )
            for envelope, item in ordered:
                await self._register_one(envelope, item)
        except Exception as exc:
            logger.warning("[event_registry] window crashed err={}", exc)
            from qqbot.services.event_ingest.ingest import IngestResult

            for envelope in batch:
                if not envelope.future.done():
                    envelope.future.set_result(
                        IngestResult(status="error", reason=str(exc))
                    )

    async def _adapt_one(self, envelope: Any) -> AdaptedEvent:
        try:
            return await self._adapter(envelope)
        except Exception as exc:
            logger.warning(
                "[event_registry] adapter crashed channel={} err={}",
                getattr(envelope, "channel", "?"),
                exc,
            )
            from qqbot.services.event_ingest.failure import (
                IngestFailureDetail,
                build_ingest_failure_event,
            )

            source = envelope.source if envelope.source is not None else envelope
            partial = build_ingest_failure_event(
                source,
                (
                    IngestFailureDetail(
                        stage="event_mapping",
                        error_code="adapter_failed",
                        reason="事件适配失败",
                    ),
                ),
            )
            return AdaptedEvent(
                partial=partial,
                status="processing_failed",
                reason="adapter_failed",
            )

    async def _register_one(self, envelope: Any, item: AdaptedEvent) -> None:
        from dataclasses import replace

        from qqbot.services.event_ingest.ingest import IngestResult
        from qqbot.services.event_ingest.system_event import finalize

        try:
            partials = (item.partial, *tuple(item.siblings or ()))
            events = []
            for partial in partials:
                _validate_content_fingerprints(partial)
                event_id = partial.event_id or issue_event_id()
                events.append(
                    finalize(
                        partial,
                        occurred_at=envelope.occurred_at,
                        event_id=event_id,
                    )
                )
            result = await self._persist(
                events,
                item.status,
                item.reason,
            )
            if item.prepared is not None:
                result = replace(result, prepared=item.prepared)
        except Exception as exc:
            logger.warning("[event_registry] persist crashed err={}", exc)
            result = IngestResult(status="error", reason=str(exc))
        if not envelope.future.done():
            envelope.future.set_result(result)
