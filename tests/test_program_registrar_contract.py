"""注册层：一个响应绑定一次模型执行（2026-08-21 渲染格式表 §一⑦）。

预检、合规校验与 ``program_hash`` 计算已从 AgentLoop 的 tick 体搬进
``program_registrar``。本文件冻结注册层自己的契约；"loop 怎么用它"由
``test_program_decision_contract`` 冻结。

钉四件事：

1. **二选一，无第三态。** 一次响应要么成为一份代码资产
   （``agent.decision_emitted``），要么成为一条非法行动
   （``agent.invalid_action``），恰好写一条事件。
2. **不攒。** 不合规就是不合规，``error_kind`` 永远是真实的静态错误 kind；
   ``invalid_program_giveup``（"攒够三次才放弃"）已不存在。
3. **回灌被拒源码。** ``raw_text`` 是模型写下的原文，一字不改地进事件。
4. **资产索引只在动作层留下代码时才写。** 空程序与纯裁决拍不写
   ``program_hash``，因此 ``execute_program`` 指名不到——旧的
   ``decision_not_a_proposal`` 由此并入 ``program_not_found``。
"""

from __future__ import annotations

import hashlib
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.program_registrar import (
    RegisteredResponse,
    adapt_program,
    register_model_response,
)
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
)

NOW = datetime(2026, 8, 21, 10, 38, tzinfo=ZoneInfo("Asia/Shanghai"))


class _NotifyTool(BaseTool):
    name = "notify"
    program_kind = "effect"
    allowed_scopes = ("group",)
    arguments_schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"sent": {"type": "boolean"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"sent": True})


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_NotifyTool)
    return registry


async def _register(source: str) -> tuple[RegisteredResponse, Any]:
    with patch(
        "qqbot.services.agent_loop.program_registrar.write_agent_event",
        new=AsyncMock(return_value="EVENT_ID"),
    ) as write_event:
        result = await register_model_response(
            object(),
            scope_key="group:1",
            scope="group",
            correlation_id="CORR",
            raw_program=source,
            registry=_registry(),
            tick_seq=7,
            now=NOW,
        )
    return result, write_event


class AdaptProgramIsPureTests(unittest.TestCase):
    def test_adapt_program_does_not_write(self) -> None:
        adapted = adapt_program(
            raw_program='notify(message="hi")',
            registry=_registry(),
            scope="group",
            scope_key="group:1",
            correlation_id="CORR",
            tick_seq=1,
        )
        self.assertTrue(adapted.accepted)
        self.assertEqual(adapted.partial.type, "agent.decision_emitted")
        self.assertEqual(adapted.partial.correlation_id, "CORR")


class ResponseBecomesAssetOrInvalidActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_program_registers_as_asset(self) -> None:
        source = 'notify(message="hi")'
        result, write_event = await _register(source)

        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.prepared)
        self.assertIsNone(result.error)
        self.assertTrue(result.left_asset)
        write_event.assert_awaited_once()
        kwargs = write_event.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "agent.decision_emitted")
        self.assertEqual(kwargs["occurred_at"], NOW)
        payload = kwargs["payload"]
        self.assertEqual(payload["program"], source)
        self.assertEqual(
            payload["program_sha256"],
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(payload["program_hash"], payload["program_sha256"][:12])
        self.assertEqual(payload["tick_seq"], 7)

    async def test_invalid_program_registers_as_invalid_action(self) -> None:
        result, write_event = await _register("import os")

        self.assertFalse(result.accepted)
        self.assertIsNone(result.prepared)
        self.assertIsNotNone(result.error)
        self.assertFalse(result.left_asset)
        write_event.assert_awaited_once()
        kwargs = write_event.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "agent.invalid_action")
        payload = kwargs["payload"]
        self.assertEqual(payload["error_kind"], "program_forbidden_construct")
        self.assertEqual(payload["raw_text"], "import os")
        self.assertEqual(payload["tick_seq"], 7)

    async def test_exactly_one_event_per_response(self) -> None:
        """二选一，无第三态：合规与不合规都恰好写一条事件。"""
        for source in ('notify(message="hi")', "import os", "# idle"):
            with self.subTest(source=source):
                _, write_event = await _register(source)
                self.assertEqual(write_event.await_count, 1)


class NoAccumulationTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_kind_is_the_real_static_kind(self) -> None:
        """不存在"攒够三次才放弃"，reason 直接就是真实静态错误 kind。"""
        cases = {
            "import os": "program_forbidden_construct",
            "send_nothing()": "program_unknown_name",
            "def f():\n": "program_syntax_error",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                result, write_event = await _register(source)
                self.assertFalse(result.accepted)
                payload = write_event.await_args.kwargs["payload"]
                self.assertEqual(payload["error_kind"], expected)
                self.assertNotEqual(payload["error_kind"], "invalid_program_giveup")

    async def test_reason_carries_kind_and_position(self) -> None:
        _, write_event = await _register("import os")
        payload = write_event.await_args.kwargs["payload"]
        self.assertTrue(payload["reason"].startswith("program_forbidden_construct"))
        self.assertIn(":", payload["reason"])


class RawTextFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_source_is_fed_back_verbatim(self) -> None:
        """回灌被拒源码：模型看得见自己写错的那一段才谈得上自纠正。

        这推翻了 2026-08-11「不回灌被拒源码」的旧决议。
        """
        source = 'notify(message="a"\nnotify(message="b")'
        result, write_event = await _register(source)
        self.assertFalse(result.accepted)
        self.assertEqual(
            write_event.await_args.kwargs["payload"]["raw_text"], source
        )


class AssetIndexOnlyWhenActionLayerLeftCodeTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_empty_program_is_addressable_by_nobody(self) -> None:
        result, write_event = await _register("# 什么都不做")
        self.assertTrue(result.accepted)
        self.assertFalse(result.left_asset)
        payload = write_event.await_args.kwargs["payload"]
        self.assertNotIn("program_hash", payload)

    async def test_pure_commit_tick_stores_empty_body_without_hash(self) -> None:
        """落库解耦：裁决指令被剥掉，纯裁决拍的动作层为空、不成为资产。"""
        result, write_event = await _register(
            'execute_program(program_hash="8f3c4e5a6b7c")'
        )
        self.assertTrue(result.accepted)
        self.assertFalse(result.left_asset)
        assert result.prepared is not None
        self.assertEqual(result.prepared.commit_program_hash, "8f3c4e5a6b7c")
        payload = write_event.await_args.kwargs["payload"]
        self.assertEqual(payload["program"].strip(), "")
        self.assertNotIn("program_hash", payload)

    async def test_mixed_tick_stores_only_the_action_layer(self) -> None:
        """形态④：裁决 + 新代码。资产 hash 只描述剥离后的业务代码。"""
        result, write_event = await _register(
            'execute_program(program_hash="8f3c4e5a6b7c")\n'
            'notify(message="下一步")'
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.left_asset)
        payload = write_event.await_args.kwargs["payload"]
        self.assertNotIn("execute_program", payload["program"])
        self.assertEqual(
            payload["program_hash"],
            hashlib.sha256(payload["program"].encode("utf-8")).hexdigest()[:12],
        )


if __name__ == "__main__":
    unittest.main()
