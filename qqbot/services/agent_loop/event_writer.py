"""Internal event writers for agent.* and runtime.* events.

External events go through EventIngest (with idempotency_key + ON CONFLICT).
Internal events (agent.* / runtime.*) come from the loop and its runtime
workers; no external dedup is needed because they immediately request a
unique event_id from the registrar (no 1s pooling window).

``announce()`` is the single "写一条事实，然后叫醒这个 scope" boundary
(2026-08-04)。此前 wait 工具、SilenceWatcher、ReplyExecutor（已于 2026-08-17
删除）各写一遍
persist-then-notify，连"写失败还叫不叫醒"这种真语义都埋在各自的 try 里；
现在只有这一个函数，差异全部收敛成参数。``RuntimeEventPublisher``
退化成一个绑好配置的薄封装，供要长期持有 publisher 的
生产者使用。

公开唤醒一律进 AgentLoop 的固定攒批窗口；静默计时器武装不再挂在 wake 上，
而由 ``note_activity`` 在成功写入非 ``runtime.silence_elapsed`` 的
agent_visible 事实后触发（见 announce）。本模块看到的 wake 永远是朴素的
``(scope_key) -> Awaitable[None]``。

Contract: 开发文档/v2.0/事件系统设计.md §2, §4.2-4.3
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.event_gateway.registry import issue_event_id
from qqbot.services.event_gateway.silence_gate import decide_silence_gate
from qqbot.services.event_ingest.persistence import persist_event
from qqbot.services.event_ingest.system_event import SystemEvent

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]
EventAvailableNotifier = Callable[[str], Awaitable[None]]
ActivityNotifier = Callable[[str], None]
RuntimeEventWriter = Callable[..., Awaitable[str]]


@dataclass(frozen=True)
class AgentEventWrite:
    """一条待写入的 agent.* 事件。

    ``event_id`` 可由调用方预先向注册层领号后指定（inline 调度需要先把
    tool_called id 注入工具 context）；省略时 batch writer 按列表顺序向
    注册层领号，确保同时间戳事件仍按 ULID 保持因果顺序。
    """

    event_type: str
    causation_id: str | None
    payload: dict
    occurred_at: datetime | None = None
    event_id: str | None = None


def parse_scope_key(scope_key: str) -> tuple[str, int | None, int | None]:
    """scope_key → (scope, group_id, user_id).

    Accepts:
    - "system"            → ("system", None, None)
    - "group:<int>"       → ("group", group_id, None)
    - "private:<int>"     → ("private", None, user_id)
    """
    if scope_key == "system":
        return "system", None, None
    if scope_key.startswith("group:"):
        return "group", int(scope_key.split(":", 1)[1]), None
    if scope_key.startswith("private:"):
        return "private", None, int(scope_key.split(":", 1)[1])
    raise ValueError(f"invalid scope_key: {scope_key!r}")


async def write_internal_event(
    session_factory: SessionFactory,
    *,
    origin: str,
    event_type: str,
    scope_key: str,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
    occurred_at: datetime | None = None,
) -> str:
    """Append a single internal (agent.* / runtime.*) event.

    Returns the generated event_id. Callers use it for downstream causation
    links (e.g. tool_called.causation_id = decision_emitted.event_id).
    """
    scope, group_id, user_id = parse_scope_key(scope_key)
    sys_event = _build_system_event(
        event_id=issue_event_id(),
        occurred_at=occurred_at or china_now(),
        origin=origin,
        event_type=event_type,
        scope=scope,
        group_id=group_id,
        user_id=user_id,
        visibility=visibility,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
    )
    async with session_factory() as session:
        await persist_event(session, sys_event)
    return sys_event.event_id


async def write_agent_events(
    session_factory: SessionFactory,
    *,
    scope_key: str,
    correlation_id: str,
    events: list[AgentEventWrite],
) -> list[str]:
    """原子追加一组 agent.* 事件，并按输入顺序返回 event_id。

    有入口网关时走 ``channel=tool``（跳过 1s 窗、同一事务落库）。未接线时
    退回本函数直写。组内所有 INSERT 同一 session、最后只 commit 一次。
    """
    if not events:
        return []
    from qqbot.services.event_gateway.outbound import get_inbound_gateway

    gateway = get_inbound_gateway()
    if gateway is not None:
        ids = await _submit_agent_batch(
            gateway,
            scope_key=scope_key,
            correlation_id=correlation_id,
            events=events,
        )
        if ids is not None:
            return ids
    scope, group_id, user_id = parse_scope_key(scope_key)
    default_occurred_at = china_now()
    system_events: list[SystemEvent] = []
    for event in events:
        if not event.event_type.startswith("agent."):
            raise ValueError(
                "write_agent_events only accepts agent.* events; "
                f"got {event.event_type!r}"
            )
        system_events.append(
            _build_system_event(
                event_id=event.event_id or issue_event_id(),
                occurred_at=event.occurred_at or default_occurred_at,
                origin="agent",
                event_type=event.event_type,
                scope=scope,
                group_id=group_id,
                user_id=user_id,
                visibility="agent_visible",
                correlation_id=correlation_id,
                causation_id=event.causation_id,
                payload=event.payload,
            )
        )

    async with session_factory() as session:
        for system_event in system_events:
            await persist_event(session, system_event, commit=False)
        await session.commit()

    return [event.event_id for event in system_events]


async def _submit_agent_batch(
    gateway: Any,
    *,
    scope_key: str,
    correlation_id: str,
    events: list[AgentEventWrite],
) -> list[str] | None:
    payload = {
        "scope_key": scope_key,
        "correlation_id": correlation_id,
        "events": [
            {
                "event_type": item.event_type,
                "causation_id": item.causation_id,
                "payload": item.payload,
                "event_id": item.event_id,
            }
            for item in events
        ],
    }
    occurred = next(
        (item.occurred_at for item in events if item.occurred_at is not None),
        None,
    )
    try:
        result = await gateway.submit(
            "tool", payload, occurred_at=occurred
        )
    except Exception as exc:
        logger.warning(
            "[event_writer] gateway tool submit failed, falling back: {}", exc
        )
        return None
    if result is None or getattr(result, "status", None) == "error":
        logger.warning(
            "[event_writer] gateway tool submit status={}, falling back",
            getattr(result, "status", None),
        )
        return None
    batch = getattr(result, "events", ()) or ()
    if batch:
        return [item.event_id for item in batch]
    event = getattr(result, "event", None)
    if event is not None:
        return [event.event_id]
    return None


def _build_system_event(  # noqa: PLR0913
    *,
    event_id: str,
    occurred_at: datetime,
    origin: str,
    event_type: str,
    scope: str,
    group_id: int | None,
    user_id: int | None,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
) -> SystemEvent:
    return SystemEvent(
        event_id=event_id,
        occurred_at=occurred_at,
        origin=origin,
        type=event_type,
        scope=scope,
        group_id=group_id,
        user_id=user_id,
        visibility=visibility,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=None,
        payload=payload,
        raw=None,
    )


# _project_task_event 已于 2026-08-21 删除（渲染格式表 §一②）。它是 agent_tasks
# 读模型的双写钩子；便签坍缩为单栏后没有读模型表，也就没有要投影的派生视图。
# 便签的跨窗口持久改由 Projector._fetch_latest_task_note 一条 LIMIT 1 查询承担
# ——比 CQRS 双写省一张可变表、也省掉"事件写成功但投影失败"的漂移面。


async def write_runtime_event(
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
    occurred_at: datetime | None = None,
) -> str:
    """``occurred_at`` 缺省=写入时刻；只有"事件真正发生的时刻早于写入"的
    场景才显式回填（如 runtime.context_compacted 回填为覆盖边界 +1ms，
    见 记忆系统契约 §2.2）。"""
    return await write_internal_event(
        session_factory,
        origin="runtime",
        event_type=event_type,
        scope_key=scope_key,
        visibility=visibility,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
        occurred_at=occurred_at,
    )


async def announce(  # noqa: PLR0913
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    visibility: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
    wake: EventAvailableNotifier | None = None,
    note_activity: ActivityNotifier | None = None,
    wake_on_write_failure: bool = False,
    occurred_at: datetime | None = None,
    write_event: RuntimeEventWriter | None = None,
) -> str | None:
    """写一条 runtime 事实，落库后叫醒该 scope。**唯一**的 persist-then-notify 入口。

    顺序不可颠倒（事件系统设计.md §2）：wake 不能领先于事实，否则被叫醒那一拍
    的投影读不到自己被叫醒的理由。

    ``wake`` 是朴素的 ``(scope_key) -> Awaitable[None]``，一律进攒批窗口。传
    None = 只写不叫。

    ``note_activity`` / ``wake`` 的规则与入口登记后的静默门同一套
    （``decide_silence_gate``）：runtime_only 不叫；silence_elapsed 只叫不
    note；其它可见事实叫并且重排计时器。写失败不 note。

    ``wake_on_write_failure``：

    - ``False``（reply 完成）：写失败就不叫。
    - ``True``（``wait`` / 静默到点）：写失败仍然叫醒。

    通知本身永远 best-effort。返回 event_id；写失败且仍叫醒时返回 None。
    """
    writer = write_event or write_runtime_event
    event_id: str | None = None
    from qqbot.services.event_gateway.outbound import get_inbound_gateway

    gateway = get_inbound_gateway()
    if gateway is not None and write_event is None:
        try:
            scope, group_id, user_id = parse_scope_key(scope_key)
            result = await gateway.submit(
                "other",
                {
                    "event_type": event_type,
                    "scope": scope,
                    "group_id": group_id,
                    "user_id": user_id,
                    "visibility": visibility,
                    "correlation_id": correlation_id,
                    "causation_id": causation_id,
                    "payload": payload,
                },
                occurred_at=occurred_at,
            )
            status = getattr(result, "status", None)
            event = getattr(result, "event", None)
            if status != "error" and event is not None:
                event_id = event.event_id
            elif status == "error":
                raise RuntimeError(getattr(result, "reason", None) or "submit")
        except Exception as exc:
            if not wake_on_write_failure:
                raise
            logger.warning(
                "[announce] gateway write {} for {} failed (still waking): {}",
                event_type,
                scope_key,
                exc,
            )
            event_id = None
        if event_id is not None:
            # 登记后的静默门已由 persist 侧 notifier 跑过，这里不再二次 wake。
            return event_id
        if not wake_on_write_failure:
            return None
        # 写失败仍叫醒：落到下面的 gate/wake。
    else:
        try:
            event_id = await writer(
                session_factory,
                event_type=event_type,
                scope_key=scope_key,
                visibility=visibility,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        except Exception as exc:
            if not wake_on_write_failure:
                raise
            logger.warning(
                "[announce] write {} for {} failed (still waking): {}",
                event_type,
                scope_key,
                exc,
            )

    gate = decide_silence_gate(
        event_type=event_type,
        visibility=visibility,
        scope_key=scope_key,
    )
    if event_id is not None and gate.note_activity and note_activity is not None:
        try:
            note_activity(scope_key)
        except Exception as exc:  # noqa: BLE001 — 武装失败不拖垮主路径
            logger.warning(
                "[announce] note_activity {} after {} failed: {}",
                scope_key,
                event_type,
                exc,
            )

    if wake is None:
        return event_id
    # 写失败且 wake_on_write_failure：兑现「到点我会来」，不看静默门。
    # 写成功则跟登记后的静默门同一套规则。
    if event_id is not None and not gate.wake:
        return event_id
    try:
        await wake(scope_key)
    except Exception as exc:  # noqa: BLE001 — event 已落库，通知必须 best-effort
        logger.warning(
            "[announce] wake {} after {} failed: {}", scope_key, event_type, exc
        )
    return event_id


class RuntimeEventPublisher:
    """``announce()`` 的绑定配置封装，供需要长期持有发布口的生产者使用。

    长期存活的组件在构造时拿到它、之后只管 ``publish(...)``，不必每次重复
    传 wake / note_activity 与失败策略。语义完全等同于直接调 ``announce()``。
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        notify_event_available: EventAvailableNotifier | None = None,
        note_activity: ActivityNotifier | None = None,
        write_event: RuntimeEventWriter | None = None,
        wake_on_write_failure: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._notify_event_available = notify_event_available
        self._note_activity = note_activity
        self._write_event = write_event
        self._wake_on_write_failure = wake_on_write_failure

    async def publish(  # noqa: PLR0913
        self,
        *,
        event_type: str,
        scope_key: str,
        visibility: str,
        correlation_id: str,
        causation_id: str | None,
        payload: dict,
        occurred_at: datetime | None = None,
    ) -> str | None:
        return await announce(
            self._session_factory,
            event_type=event_type,
            scope_key=scope_key,
            visibility=visibility,
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=payload,
            wake=self._notify_event_available,
            note_activity=self._note_activity,
            wake_on_write_failure=self._wake_on_write_failure,
            occurred_at=occurred_at,
            write_event=self._write_event,
        )


async def write_agent_event(
    session_factory: SessionFactory,
    *,
    event_type: str,
    scope_key: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict,
    occurred_at: datetime | None = None,
) -> str:
    """``occurred_at`` 缺省=写入时刻。只有"事件真正发生的时刻早于写入"的
    场景才显式传（如 agent.decision_emitted 回填为本拍投影时刻，见
    loop._tick）——事件流按 occurred_at 排序，用错基准会让时间线错位。"""
    event_ids = await write_agent_events(
        session_factory,
        scope_key=scope_key,
        correlation_id=correlation_id,
        events=[
            AgentEventWrite(
                event_type=event_type,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        ],
    )
    return event_ids[0]
