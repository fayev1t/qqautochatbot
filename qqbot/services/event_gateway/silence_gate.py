"""静默门：登记完成之后才判定叫不叫醒。注册层不打 tag。

runtime_only → 不叫、不 note_activity
runtime.silence_elapsed → 叫、不 note_activity
agent.background_noted → 都不做（见下）
其它 agent_visible → 叫 + note_activity
private 不实例化 loop，也不叫
空程序停止符在 AgentLoop 里，不在这里。

``agent.background_noted`` 是 2026-08-21 从信封头部搬下来的每日群聊背景。它是
**背景，不是动静**：00:00 给每个群平白开一拍纯属噪音；note_activity 更糟——半夜
把所有群的静默计时器重新武装一遍，十分钟后一串 silence_elapsed 会真的把她叫
起来说话。判定放在这里而不是写入方的调用点：这一层才是"登记完成之后叫不叫醒"
的唯一出处，写入方换个入口不该改变结论。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SILENCE_ELAPSED_TYPE = "runtime.silence_elapsed"
BACKGROUND_NOTED_TYPE = "agent.background_noted"

WakeFn = Callable[[str], Awaitable[None]]
ActivityFn = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SilenceGateDecision:
    wake: bool
    note_activity: bool
    scope_key: str | None


def scope_key_of(event: Any) -> str | None:
    scope = getattr(event, "scope", None)
    if scope == "group" and getattr(event, "group_id", None) is not None:
        return f"group:{event.group_id}"
    if scope == "system":
        return "system"
    if scope == "private" and getattr(event, "user_id", None) is not None:
        return f"private:{event.user_id}"
    return None


def decide_silence_gate(
    *,
    event_type: str,
    visibility: str,
    scope_key: str | None,
) -> SilenceGateDecision:
    if not scope_key or scope_key.startswith("private:"):
        return SilenceGateDecision(False, False, scope_key)
    if visibility != "agent_visible":
        return SilenceGateDecision(False, False, scope_key)
    if event_type == SILENCE_ELAPSED_TYPE:
        return SilenceGateDecision(True, False, scope_key)
    if event_type == BACKGROUND_NOTED_TYPE:
        return SilenceGateDecision(False, False, scope_key)
    return SilenceGateDecision(True, True, scope_key)


def decide_silence_gate_for_event(event: Any) -> SilenceGateDecision:
    return decide_silence_gate(
        event_type=str(getattr(event, "type", "") or ""),
        visibility=str(getattr(event, "visibility", "") or ""),
        scope_key=scope_key_of(event),
    )


async def apply_silence_gate(
    event: Any,
    *,
    wake: WakeFn | None,
    note_activity: ActivityFn | None,
) -> SilenceGateDecision:
    decision = decide_silence_gate_for_event(event)
    if (
        decision.note_activity
        and note_activity is not None
        and decision.scope_key is not None
    ):
        try:
            note_activity(decision.scope_key)
        except Exception as exc:
            logger.warning(
                "[silence_gate] note_activity %s failed: %s",
                decision.scope_key,
                exc,
            )
    if decision.wake and wake is not None and decision.scope_key is not None:
        try:
            await wake(decision.scope_key)
        except Exception as exc:
            logger.warning(
                "[silence_gate] wake %s failed: %s",
                decision.scope_key,
                exc,
            )
    return decision
