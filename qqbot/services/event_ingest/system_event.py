"""SystemEvent value objects.

Contract: 开发文档/v2.0/20-横切契约/事件系统设计.md §2
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Origin = Literal["external", "agent", "runtime"]
Scope = Literal["system", "group", "private"]
Visibility = Literal["agent_visible", "runtime_only"]


@dataclass(frozen=True)
class PartialSystemEvent:
    """Pre-finalization ingest event without timestamps.

    ``event_id`` 缺省由注册层发放。``begin_effect_call`` 必须先把 id 注入
    工具 context，因而可预填——预填值仍须来自注册层发放口。
    外部入站的 ``correlation_id`` 在 finalize 时等于本枚 event_id；内部
    通道可把 tick 的 correlation / 直接前因带进来。
    """

    origin: Origin
    type: str
    scope: Scope
    group_id: int | None
    user_id: int | None
    visibility: Visibility
    payload: dict
    raw: dict | None
    idempotency_key: str | None
    correlation_id: str | None = None
    causation_id: str | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class SystemEvent:
    """Fully-formed event ready to be persisted into agent_events."""

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
    idempotency_key: str | None
    payload: dict
    raw: dict | None


def finalize(
    partial: PartialSystemEvent,
    *,
    occurred_at: datetime,
    event_id: str,
) -> SystemEvent:
    """把 PartialSystemEvent 定格为可落库的 SystemEvent。

    ``event_id`` 必须由注册层发放（或等于 partial 上预领的那枚），这里不 mint。
    外部入站自相关：``correlation_id`` 缺省等于这枚 ``event_id``。内部通道
    若在 partial 上带了 tick 的 correlation / causation，原样保留。
    见 事件系统设计.md §2、§6。

    业务内容指纹（``payload`` 里的 ``file_hash`` / ``program_sha256``，以及
    ``idempotency_key``）原样带过，本函数不改写。
    """
    eid = str(partial.event_id or event_id)
    if not eid:
        raise ValueError("finalize requires event_id issued by EventRegistrar")
    correlation = partial.correlation_id or eid
    return SystemEvent(
        event_id=eid,
        occurred_at=occurred_at,
        origin=partial.origin,
        type=partial.type,
        scope=partial.scope,
        group_id=partial.group_id,
        user_id=partial.user_id,
        visibility=partial.visibility,
        correlation_id=correlation,
        causation_id=partial.causation_id,
        idempotency_key=partial.idempotency_key,
        payload=partial.payload,
        raw=partial.raw,
    )
