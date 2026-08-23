"""TaskTool —— 小奏那张只有一栏的待办便签。

2026-08-21 整篇重写（渲染格式表 §一②「任务便签单一化」，维护者裁定甲案）。
此前它是一套有 ID、有 ``pending|running|done|failed`` 状态机、有父子层级、有
逐条进度笔记的任务系统，配一张 ``agent_tasks`` CQRS 读模型表。现在只剩一栏
自由文本：写一次就整段覆盖，写空串就清空。

**裁定理由**：两件事不需要两条结构化记录才能执行。一段自由文本完全写得下
"帮张三查天气；另外李四让我提醒他开会"——消费它的是大模型，不是机器程序。
多任务并行是数据模型层面的概念，模型侧并不需要它。

与 ``reflect`` 正好构成一组对调（§一②/③）：**反思要历史，便签只要现状**。
反思从 latest-wins 折叠搬到了时间线（逐版留痕），便签从时间线搬进了 latest-wins
折叠（只剩最新一版）。所以本工具写下的事件**不进时间线**，只折成信封顶部的
``<task>`` 单栏。

**append-only 不受影响。** 每次覆写照常追加一条 ``agent.task_note_written``，
库里留着历次便签的完整历史；被折掉的只是**渲染**。折叠语义在读侧，不可变性
在写侧，两者不冲突。

跨窗口持久：便签没有读模型表（事件系统设计 §7.3）。它靠 Projector 的
``_fetch_latest_task_note`` 一条不受取数窗约束的 LIMIT 1 查询兜底，与
``bot_role`` / ``<recall>`` 同一手法——写下之后长期不改也不会因为水群而蒸发。

依赖注入：无。本工具不读库、不调 napcat，只把文本交给执行层落库。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolGeneratedEvent,
    ToolOutcome,
)

_USAGE_PROMPT = load_sibling_md(__file__, "task.md")

# 正文上限。与 reflect 取同一个数不是巧合：两者共用同一套 latest-wins 折叠，
# 上限的作用也一样——写得下几件正在办的事、写不下一本流水账，于是每次重写
# 都必须做取舍。
MAX_TASK_CHARS = 600

TASK_NOTE_EVENT_TYPE = "agent.task_note_written"


class TaskTool(BaseTool):
    """实现 Tool 协议。

    ``required_permission`` 用 BaseTool 默认 GUEST——便签是她给自己记的，
    不对任何用户或群产生作用（与 ``reflect`` / ``wait`` 同级）。

    ``max_call_sites=1``：一拍最多重写一次。写两次的后一次会完整覆盖前一次，
    静态限死比留给运行时"最后一条胜"更早暴露问题。
    """

    name = "task"
    program_kind = "effect"
    max_call_sites = 1
    description = (
        "重写当前待办便签。整段覆盖，不是追加；传空串即清空。"
        "内容会折成信封顶部的 `<task>` 单栏，只显示最新一版。"
        f"上限 {MAX_TASK_CHARS} 字。本工具不发送任何消息，也不改变群内状态。"
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "新一版待办的完整正文，替换而非追加；空串表示清空。"
                    f"上限 {MAX_TASK_CHARS} 字，超出即调用失败。"
                ),
            },
        },
        "required": ["content"],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "cleared": {"type": "boolean"},
            "chars": {"type": "integer"},
        },
        "required": ["cleared", "chars"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> Any:
        if fail := await self.enforce_access(context):
            return fail

        raw = arguments.get("content")
        if not isinstance(raw, str):
            return ToolOutcome.failure(
                "invalid_arguments",
                "content must be a string",
                reason_code="content_not_str",
            )
        content = raw.strip()
        # 空串是**合法输入**，语义是"清空"——这与 reflect 相反，那边空文本是
        # 错误。便签有"办完了，现在没事"这个状态，自我认识没有。
        if len(content) > MAX_TASK_CHARS:
            return ToolOutcome.failure(
                "invalid_arguments",
                (
                    f"content must be at most {MAX_TASK_CHARS} characters, "
                    f"got {len(content)}"
                ),
                reason_code="content_too_long",
            )

        return ToolOutcome.success(
            {"cleared": not content, "chars": len(content)},
            emitted_events=(
                ToolGeneratedEvent(
                    event_type=TASK_NOTE_EVENT_TYPE,
                    payload={"content": content, "chars": len(content)},
                ),
            ),
        )
