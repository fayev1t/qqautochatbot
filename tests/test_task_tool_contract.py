"""Contract tests for the effect ``task`` Program API function.

2026-08-21 整篇重写（渲染格式表 §一②「任务便签单一化」，维护者裁定甲案）。
冻结的四条：

1. **只有一栏**：``content`` 一个参数，没有 action 分支、没有 task_id、
   没有状态机；
2. **整段覆盖**：写一次替换上一版，事件载荷就是这一版全文；
3. **空串是合法输入**，语义是清空——这与 ``reflect`` 相反，那边空文本是错误；
4. **超长失败而不截断**：半截便签会让她以为整段都记下了。

任务坍缩连带的拆除（agent_tasks 读模型、agent.task_* 事件族、task_id 值域、
`<task_closed>` 行型、triggered_by_event_id 回退路径）分别由 projection /
program_ast / program_runtime / tool_registry 的契约测试盯着。
"""

from __future__ import annotations

import asyncio
import unittest

from qqbot.services.agent_loop.tool_registry import ToolOutcome
from qqbot.services.agent_loop.tools import build_default_registry
from qqbot.services.agent_loop.tools.task import (
    MAX_TASK_CHARS,
    TASK_NOTE_EVENT_TYPE,
    TaskTool,
)


def _run(arguments: dict, **context: object) -> ToolOutcome:
    return asyncio.run(TaskTool().run(arguments, **context))


class TaskToolContractTests(unittest.TestCase):
    def test_registered_as_effect_program_function(self) -> None:
        registry = build_default_registry()
        tool = registry.get("task")
        self.assertIsNotNone(tool)
        self.assertEqual(registry.spec("task").program_kind, "effect")
        self.assertIn("task", registry.names())

    def test_schema_is_a_single_content_field(self) -> None:
        schema = TaskTool.arguments_schema
        self.assertNotIn("oneOf", schema)
        self.assertEqual(set(schema["properties"]), {"content"})
        self.assertEqual(schema["required"], ["content"])

    def test_no_task_id_anywhere_on_the_surface(self) -> None:
        """task_id 值域整体消失（§八10），不许从任何一条缝里漏回来。"""
        registry = build_default_registry()
        spec = registry.spec("task")
        self.assertNotIn("task_id", spec.signature)
        self.assertNotIn("task_id", TaskTool.arguments_schema["properties"])
        self.assertNotIn("task_id", TaskTool.result_schema["properties"])

    def test_one_call_site_per_tick(self) -> None:
        """写两次的后一次会完整覆盖前一次，静态限死比运行时"最后一条胜"更早
        暴露问题（同 reflect）。"""
        self.assertEqual(TaskTool.max_call_sites, 1)

    def test_write_emits_one_note_event_with_the_full_text(self) -> None:
        text = "帮张三查明天上海的天气预报；另外李四让我下午三点提醒他开会"
        outcome = _run({"content": text})
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result, {"cleared": False, "chars": len(text)})
        self.assertEqual(len(outcome.emitted_events), 1)
        event = outcome.emitted_events[0]
        self.assertEqual(event.event_type, TASK_NOTE_EVENT_TYPE)
        self.assertEqual(event.payload, {"content": text, "chars": len(text)})

    def test_empty_content_clears_and_is_not_an_error(self) -> None:
        """空串是"办完了/没事了"这个状态的表达，不是坏输入。

        它必须真的落一条事件——清空是一次**覆写**，得压掉更早那版有内容的
        便签，不能什么都不写就返回成功。
        """
        outcome = _run({"content": ""})
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result, {"cleared": True, "chars": 0})
        self.assertEqual(len(outcome.emitted_events), 1)
        self.assertEqual(outcome.emitted_events[0].payload["content"], "")

    def test_whitespace_only_content_also_clears(self) -> None:
        outcome = _run({"content": "   \n  "})
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["cleared"])
        self.assertEqual(outcome.emitted_events[0].payload["content"], "")

    def test_content_is_stripped_but_inner_text_is_verbatim(self) -> None:
        outcome = _run({"content": "  查天气\n然后提醒开会  "})
        self.assertEqual(
            outcome.emitted_events[0].payload["content"], "查天气\n然后提醒开会"
        )

    def test_non_string_content_is_invalid_arguments(self) -> None:
        outcome = _run({"content": 42})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "content_not_str")
        self.assertEqual(outcome.emitted_events, ())

    def test_missing_content_is_invalid_arguments(self) -> None:
        outcome = _run({})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    def test_too_long_fails_instead_of_truncating(self) -> None:
        """截断会让她以为整段都记下了，实际尾部已经丢掉——下一拍读到半截
        便签还当成完整待办。失败让她重写。"""
        outcome = _run({"content": "字" * (MAX_TASK_CHARS + 1)})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "content_too_long")
        self.assertEqual(outcome.emitted_events, ())

    def test_exactly_at_the_limit_is_accepted(self) -> None:
        outcome = _run({"content": "字" * MAX_TASK_CHARS})
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["chars"], MAX_TASK_CHARS)

    def test_no_permission_required(self) -> None:
        """便签是她给自己记的，不对任何用户或群产生作用（同 reflect / wait）。

        顺带这条也说明它不需要 triggered_by_event_id——2026-08-21 之后那个参数
        是敏感工具的硬要求，但 GUEST 工具不受影响。
        """
        from qqbot.services.agent_loop.tool_registry import (
            PermissionTier,
            get_tool_required_permission,
        )

        self.assertEqual(
            get_tool_required_permission(TaskTool()), PermissionTier.GUEST
        )
        outcome = _run({"content": "记一笔"})
        self.assertTrue(outcome.ok)


if __name__ == "__main__":
    unittest.main()
