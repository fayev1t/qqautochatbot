"""出口网关：调模型走提供层，终态绕回入口网关当上游事件。

超时等错误处理在模型提供层做完，再把失败包送回来登记。
装配器只拼 prompt；本模块负责 create_llm + ainvoke + submit。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger

logger = get_logger(__name__)

_inbound: Any = None

SCENE_ROLES: dict[str, str] = {
    "planner": "planner",
    "caption": "caption",
    "image_description": "vision",
    "image_look": "vision",
    "memory": "memory",
    "web_digest": "web_digest",
}


@dataclass(frozen=True, slots=True)
class InvokeResult:
    ok: bool
    text: str
    raw: Any = None
    ingest: Any = None
    error: str | None = None


def set_inbound_gateway(gateway: Any) -> None:
    global _inbound
    _inbound = gateway


def get_inbound_gateway() -> Any:
    return _inbound


async def submit_model_outcome(payload: dict[str, Any]) -> Any:
    gateway = _inbound
    if gateway is None:
        return None
    try:
        return await gateway.submit("model", payload, source=payload)
    except Exception as exc:
        logger.warning("[event_gateway] model outcome submit failed: {}", exc)
        return None


async def submit_tool_outcome(payload: dict[str, Any]) -> Any:
    gateway = _inbound
    if gateway is None:
        return None
    try:
        return await gateway.submit("tool", payload, source=payload)
    except Exception as exc:
        logger.warning("[event_gateway] tool outcome submit failed: {}", exc)
        return None


def schedule_model_outcome(payload: dict[str, Any]) -> None:
    """兼容旧 sink：不阻塞 ainvoke 返回。新路径用 invoke() await 登记。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(submit_model_outcome(payload))


def _extract_text(raw: Any) -> str:
    content = getattr(raw, "content", raw)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return "" if content is None else str(content)


async def invoke(
    scene: str,
    messages: list[Any],
    *,
    extra: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> InvokeResult:
    """提供层调用 + 把终态送回入口网关。

    planner 且 ok 时适配器做预检，登记 decision_emitted / invalid_action。
    其它 scene 或失败包登记 runtime.model_responded（runtime_only）。
    """
    payload: dict[str, Any] = dict(extra or {})
    payload["scene"] = scene
    role = SCENE_ROLES.get(scene, "default")
    llm = await create_llm(role=role)
    if llm is None:
        payload["ok"] = False
        payload["error_kind"] = "llm_unavailable"
        ingest = await submit_model_outcome(payload)
        return InvokeResult(
            ok=False, text="", ingest=ingest, error="llm_unavailable"
        )
    try:
        if timeout is not None:
            raw = await asyncio.wait_for(llm.ainvoke(messages), timeout=timeout)
        else:
            raw = await llm.ainvoke(messages)
    except Exception as exc:
        payload["ok"] = False
        payload["error_kind"] = type(exc).__name__
        payload["error_message"] = str(exc)[:300]
        ingest = await submit_model_outcome(payload)
        logger.warning(
            "[event_gateway] invoke scene={} failed: {}: {}",
            scene,
            type(exc).__name__,
            exc,
        )
        return InvokeResult(
            ok=False,
            text="",
            ingest=ingest,
            error=f"{type(exc).__name__}",
        )
    text = _extract_text(raw)
    payload["ok"] = True
    payload["text"] = text
    if scene == "planner":
        payload["program"] = text
    ingest = await submit_model_outcome(payload)
    return InvokeResult(ok=True, text=text, raw=raw, ingest=ingest)
