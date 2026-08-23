"""ReflectTool —— 小奏写下并持续重写自己的那一段自我认识。

设计动机（2026-08-03）：她以拍为单位存在，两拍之间不存在一个仍在惦记着什么
的后台意识（`planner.md` §系统运行方式）。在本工具之前，"由她自己写下、且会
一直回到眼前"的东西只有任务一种——而任务是**未竟之事**，有收束条件；"我最近
是不是话太多了""这个群好像没人接我的话"这类对自己的认识套不进
create/note/complete/fail 的生命周期，写进程序注释则当拍即焚。本工具补上第二
种连续装置，两者分工：任务承载**待办**，反思承载**自我认识**。

与已删除的 `<my-thought>` 思考回显的区别（开发日志 2026-08-01）：那次删掉的是
**逐拍自由 reasoning 原样回到下一拍**。真实快照里它变成了模型写给自己的高显著
度提示词——等待前一拍写下的「按傲娇嘴硬的性格准备怼回去」在等待结束后被近乎
原样继承为「傲娇嘴硬带点小脾气」，最终生成模板化台词。那次结论同时否掉了
"把字段改写成客观事实就行"这条退路。本工具据此做了三处结构性区分：

  - **低频**：由静默触发或她自己经 `wait` 改期，不是每拍都写；
  - **全量替换**：latest-wins，每次都要读着上一版重写，逼整合而不是堆积；
  - **有硬上限**：``MAX_REFLECTION_CHARS`` 封顶，写不下一整套教条。

中间隔着一次整合，立场就不会逐字继承——它得先被压成一句结论，再被下一次反思
拿当期事实重新审视。频率是那个病理的开关，这三条把它压在最弱形态。

存储：``agent.reflection_written`` 领域事件，经 ``ToolGeneratedEvent`` 随工具
terminal 同事务落库（程序执行黑盒设计 §Effect 两段事务）。

2026-08-21 起它是**时间线事实事件**：渲染成 ``<reflection>`` 行逐版留痕，不再折叠成
`## 反思` 一节、也不再被后来的版本覆写。全量覆写会让历史各版彻底消失，模型
因此看不到自己认识的演变。

腾出的 latest-wins 折叠器已交给 task 便签（渲染格式表 §一②）：
分工是"反思要历史、便签只要现状"。

依赖注入：无。本工具不读库、不调 napcat——它只把文本交给执行层落库，
scope/correlation/causation 全由 ProgramExecutor 统一接。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolGeneratedEvent,
    ToolOutcome,
)

_USAGE_PROMPT = load_sibling_md(__file__, "reflect.md")

# 正文上限。定这个数不是为了省 token，是为了让"全量替换"真的构成约束：
# 写得下一段结论、写不下一套逐条教条，于是每次重写都必须做取舍。
MAX_REFLECTION_CHARS = 600

REFLECTION_EVENT_TYPE = "agent.reflection_written"


class ReflectTool(BaseTool):
    """实现 Tool 协议。

    ``required_permission`` 用 BaseTool 默认 GUEST——这是她对自己的整理，
    不对任何用户或群产生作用，不需要触发者授权（与 ``wait`` 同级）。

    ``max_call_sites=1``：一拍最多重写一次自我认识。写两次的后一次会完整覆盖
    前一次，静态限死比留给运行时"最后一条胜"更早暴露问题。
    """

    name = "reflect"
    program_kind = "effect"
    max_call_sites = 1
    description = (
        "写下当前这一版自我认识。文本会作为一条 `<reflection>` 落在时间线上，"
        "和别的事一样按时刻留在流里；此前各版都还在，不会被这一版抹掉。"
        f"上限 {MAX_REFLECTION_CHARS} 字。"
        "本工具不发送任何消息，也不改变群内状态。"
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "新一版自我认识的完整正文，替换而非追加；上限 "
                    f"{MAX_REFLECTION_CHARS} 字，超出即调用失败。"
                ),
            },
        },
        "required": ["text"],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "written": {"type": "boolean"},
            "chars": {"type": "integer"},
        },
        "required": ["written", "chars"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> Any:
        if fail := await self.enforce_access(context):
            return fail

        raw = arguments.get("text")
        if not isinstance(raw, str):
            return ToolOutcome.failure(
                "invalid_arguments",
                "text must be a string",
                reason_code="text_not_str",
            )
        text = raw.strip()
        if not text:
            return ToolOutcome.failure(
                "invalid_arguments",
                "text must not be empty",
                reason_code="text_empty",
            )
        # 超长**不**静默截断：截断会让她以为整段都留下了，而实际尾部结论已经
        # 丢掉——下一拍读到一段半截话还当成自己的完整想法。失败让她重写。
        if len(text) > MAX_REFLECTION_CHARS:
            return ToolOutcome.failure(
                "invalid_arguments",
                (
                    f"text must be at most {MAX_REFLECTION_CHARS} characters, "
                    f"got {len(text)}"
                ),
                reason_code="text_too_long",
            )

        return ToolOutcome.success(
            {"written": True, "chars": len(text)},
            emitted_events=(
                ToolGeneratedEvent(
                    event_type=REFLECTION_EVENT_TYPE,
                    payload={"text": text, "chars": len(text)},
                ),
            ),
        )
