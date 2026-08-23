"""WaitTool —— 模型给自己安排一次延迟唤醒（时间自主权）。

设计动机（2026-07-02，模型+prompt 优先哲学）：此前所有唤醒都是程序驱动的
（外部事件 / 批次收口），模型无法表达"我 10 分钟后再来看看"或"等这个人把话
说完再回"。本工具把"何时思考"的部分决定权交还模型。

职责收窄（2026-07-19，ReplyTask 换轨；2026-08-17 随 reply 删除重述）：
"等他把话说完再回"不由任何计时器负责——提案-裁决流水线让写下发言的那一拍
只落库、下一拍重新读完时间线才可能指名执行，人补完的后半句正落在这段间隔
里。wait 只保留自我提醒 / 延迟执行其它动作的用途，description 与 wait.md
不得再引导用它等分条消息。

用途明确化（2026-08-03，静默叫醒落地）：保留下来的"自我提醒"里现在有一个
具体主顾——她给自己的回想改期。系统的静默叫醒（silence_watcher.py）是保底，
本工具是她按处境自定的那一次。同批两处收紧：``note`` 由可选改必填（没有理由
的约定，到点只回显一个空提示），上界由 3600 提到 6000 秒（回想改期的自然
量级是一两小时）。

执行语义（**绝不在工具内 sleep**——程序函数按顺序串行执行，长眠会卡住本
scope 当前拍）：execute() 只登记一个 asyncio 定时器就立刻返回成功（带 wake_at）。
到点后回调先写 ``runtime.wait_elapsed``（agent_visible，携带模型当时留下的
note），**再**唤醒 scope——保证醒来那拍的投影必能看到这条 hint（与批次收口
"先写标记再唤醒"同序）。

Best-effort：定时器只活在进程内存里，**进程重启即丢**。可见证据链留给模型
自判：timeline 里有 wait 的 tool-call（result 带 wake_at）但迟迟没有对应的
``<system-hint kind="wait_elapsed">``，说明计时器已丢，可以再约一次。不建
持久化调度表——真到点了没醒，下一条外部消息也会把 loop 叫起来，模型看得到
自己的 wait 记录。

依赖注入：session_factory / wake_scope / tool_call_event_id / correlation_id
全部来自 ProgramExecutor 统一注入的 run() context，无构造依赖（黑盒不变）。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Awaitable, Callable

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.event_writer import announce
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "wait.md")

MIN_WAIT_SECONDS = 5
# 2026-08-03 由 3600 提到 6000：静默叫醒落地后，本工具同时承担"她自己给回想
# 改期"的用途（silence_watcher.py），而那个场景的自然上界是一两小时。
MAX_WAIT_SECONDS = 6000


class WaitTool(BaseTool):
    """实现 Tool 协议。required_permission 用 BaseTool 默认 GUEST——这是模型
    的自我调度动作，不涉及对任何用户/群的操作，无需触发者授权。"""

    name = "wait"
    program_kind = "effect"
    max_call_sites = 2
    description = (
        "在指定秒数后为当前 scope 安排一次唤醒。计时器触发时写入包含 note 的 "
        "<system>wait_elapsed 行并启动新 tick。计时器仅保存在进程内存中，进程"
        "重启后不会恢复；工具调用记录仍保留在时间线中。"
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "integer",
                "minimum": MIN_WAIT_SECONDS,
                "maximum": MAX_WAIT_SECONDS,
                "description": (
                    "唤醒前的等待秒数，取值范围为 "
                    f"{MIN_WAIT_SECONDS}–{MAX_WAIT_SECONDS}。"
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "必填备忘文本，说明约这一次唤醒是为了什么；计时器触发后"
                    "原样写入 wait_elapsed 行。去除首尾空白后最多 500 字。"
                ),
            },
        },
        "required": ["seconds", "note"],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "scheduled": {"type": "boolean"},
            "seconds": {"type": "integer"},
            "wake_at": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["scheduled", "seconds", "wake_at", "note"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> Any:
        seconds = _coerce_seconds(arguments.get("seconds"))
        if seconds is None:
            return ToolOutcome.failure(
                "invalid_arguments",
                "seconds must be an integer",
                reason_code="seconds_not_int",
            )
        if not (MIN_WAIT_SECONDS <= seconds <= MAX_WAIT_SECONDS):
            return ToolOutcome.failure(
                "invalid_arguments",
                (
                    f"seconds must be within [{MIN_WAIT_SECONDS}, "
                    f"{MAX_WAIT_SECONDS}], got {seconds}"
                ),
                reason_code="seconds_out_of_range",
            )
        # note 2026-08-03 由可选改必填：一次没有理由的自我约定，到点回显的
        # 只是一个空提示，那一拍除了"我醒了"什么都拿不到。
        note_raw = arguments.get("note")
        if not isinstance(note_raw, str):
            return ToolOutcome.failure(
                "invalid_arguments",
                "note must be a string",
                reason_code="note_not_str",
            )
        note = note_raw.strip()[:500]
        if not note:
            return ToolOutcome.failure(
                "invalid_arguments",
                "note must not be empty",
                reason_code="note_empty",
            )

        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        wake_scope = context.get("wake_scope")
        if not scope_key or session_factory is None or wake_scope is None:
            # supervisor / session 未注入（早期骨架、残缺测试装配）——工具
            # 黑盒地返回失败，不 raise、不静默假装约上了。
            return ToolOutcome.failure(
                "internal_tool_error",
                "wait unavailable: missing wake_scope/session context",
            )
        note_activity = context.get("note_activity")

        wake_at = china_now() + timedelta(seconds=seconds)
        loop = asyncio.get_running_loop()
        loop.call_later(
            seconds,
            lambda: asyncio.ensure_future(
                _fire_wait(
                    session_factory=session_factory,
                    wake_scope=wake_scope,
                    note_activity=note_activity,
                    scope_key=scope_key,
                    correlation_id=context.get("correlation_id"),
                    causation_id=context.get("tool_call_event_id"),
                    seconds=seconds,
                    note=note,
                    wake_at_iso=wake_at.isoformat(timespec="seconds"),
                )
            ),
        )
        result: dict[str, Any] = {
            "scheduled": True,
            "seconds": seconds,
            "wake_at": wake_at.isoformat(timespec="seconds"),
            "note": note,
        }
        return ToolOutcome.success(result)


async def _fire_wait(
    *,
    session_factory: Any,
    wake_scope: Callable[[str], Awaitable[None]],
    note_activity: Callable[[str], None] | None,
    scope_key: str,
    correlation_id: str | None,
    causation_id: str | None,
    seconds: int,
    note: str,
    wake_at_iso: str,
) -> None:
    """定时器到点回调：写 runtime.wait_elapsed，再唤醒 scope。

    2026-08-04 起走统一的 ``announce()``（event_writer.py），不再在这里手写
    persist-then-notify。``wake_on_write_failure=True`` 保留本工具原有语义：
    事件写失败仍要唤醒——宁可模型醒来少一条 hint，不可失约。

    causation 指向当初的 agent.tool_called，correlation 沿用发起 tick 的——
    与 tool_result 的因果语义一致。
    """
    payload: dict[str, Any] = {
        "seconds": seconds,
        "wake_at": wake_at_iso,
        "note": note,
    }
    await announce(
        session_factory,
        event_type="runtime.wait_elapsed",
        scope_key=scope_key,
        visibility="agent_visible",
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
        wake=wake_scope,
        note_activity=note_activity,
        wake_on_write_failure=True,
    )


def _coerce_seconds(raw: Any) -> int | None:
    """LLM 给的 seconds → int；bool / 非整数 / 非法字符串 → None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            return int(s)
    return None
