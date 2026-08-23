"""EventIngest: v2 ingress layer.

Contracts:
- 开发文档/v2.0/20-横切契约/EventIngest契约.md
- 开发文档/v2.0/20-横切契约/事件系统设计.md

Every non-heartbeat NapCat input is reduced to exactly one committed terminal
event: either the mapped ``external.*`` fact or
``runtime.event_ingest_failed`` when required preprocessing cannot finish.

包级导入：轻量类型（SystemEvent / Mapper / failure）eager；``EventIngest`` /
``IngestResult`` 惰性。``ingest.py`` 依赖 ``event_gateway.registry``，而
registry 又要从本包取 ``system_event`` —— 若 ``__init__`` eager 拉 ingest，
启动会在 registry 半初始化时回头取 ``AdaptedEvent`` 环形失败。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qqbot.services.event_ingest.failure import (
    INGEST_FAILURE_EVENT_TYPE,
    IngestFailureDetail,
)
from qqbot.services.event_ingest.mapper import EventMapper, MapperRegistry
from qqbot.services.event_ingest.system_event import (
    PartialSystemEvent,
    SystemEvent,
    finalize,
)

if TYPE_CHECKING:
    from qqbot.services.event_ingest.ingest import EventIngest, IngestResult

_LAZY: dict[str, str] = {
    "EventIngest": "ingest",
    "IngestResult": "ingest",
}

__all__ = [
    "EventIngest",
    "IngestResult",
    "INGEST_FAILURE_EVENT_TYPE",
    "IngestFailureDetail",
    "EventMapper",
    "MapperRegistry",
    "PartialSystemEvent",
    "SystemEvent",
    "finalize",
]


def __getattr__(name: str) -> Any:
    sub = _LAZY.get(name)
    if sub is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    mod = import_module(f"{__name__}.{sub}")
    value = getattr(mod, name)
    globals()[name] = value
    return value
