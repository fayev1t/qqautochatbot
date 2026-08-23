"""提示词装配器：开口场景先在这里注册，装配器只拼文本。

调模型走出口网关；不写事件。根页仍在 prompts/catalog.py。
"""

from __future__ import annotations

from qqbot.services.agent_loop.prompts.catalog import (
    CONSUMERS,
    render_system_prompt,
)


def registered_scenes() -> tuple[str, ...]:
    return tuple(CONSUMERS)


def assemble(
    scene: str,
    *,
    scope: str | None = None,
    tool_registry: object | None = None,
) -> str:
    if scene not in CONSUMERS:
        raise KeyError(f"unregistered prompt scene: {scene!r}")
    return render_system_prompt(
        scene, scope=scope, tool_registry=tool_registry
    )
