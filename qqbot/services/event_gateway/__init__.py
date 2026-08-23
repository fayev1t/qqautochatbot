"""统一入口/出口网关。子模块按需导入，避免包 import 拉 SQLAlchemy。"""

from __future__ import annotations

from typing import Any

_LAZY: dict[str, str] = {
    "AdaptedEvent": "registry",
    "EventRegistrar": "registry",
    "issue_event_id": "registry",
    "InboundGateway": "inbound",
    "SilenceGateDecision": "silence_gate",
    "UpstreamEnvelope": "inbound",
    "apply_silence_gate": "silence_gate",
    "decide_silence_gate": "silence_gate",
    "decide_silence_gate_for_event": "silence_gate",
    "get_inbound_gateway": "outbound",
    "set_inbound_gateway": "outbound",
    "submit_model_outcome": "outbound",
    "submit_tool_outcome": "outbound",
    "invoke": "outbound",
    "InvokeResult": "outbound",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    sub = _LAZY.get(name)
    if sub is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    mod = import_module(f"{__name__}.{sub}")
    value = getattr(mod, name)
    globals()[name] = value
    return value
