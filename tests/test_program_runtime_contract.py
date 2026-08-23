"""Runtime, read-only ABI, quota, and failure contracts for Planner programs."""

# Tool stubs intentionally keep registry metadata and call recordings on the class.
# ruff: noqa: ARG002, RUF012, SIM117

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop.program_ast import preflight
from qqbot.services.agent_loop.program_events import EffectCallHandle
from qqbot.services.agent_loop.program_runtime import (
    ProgramExecutionError,
    ProgramExecutor,
)
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolGeneratedEvent,
    ToolOutcome,
    ToolRegistry,
)


class _MembersQuery(BaseTool):
    name = "members"
    program_kind = "query"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer"},
                        "nickname": {"type": "string"},
                        "card": {"type": ["string", "null"]},
                    },
                    "additionalProperties": False,
                },
            }
        },
        "additionalProperties": False,
    }
    calls: list[dict] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append(dict(arguments))
        return ToolOutcome.success(
            {
                "members": [
                    {"user_id": 1, "nickname": "Alice"},
                    {"user_id": 2, "nickname": "Bob", "card": "B"},
                ]
            }
        )


class _EchoQuery(BaseTool):
    name = "echo"
    program_kind = "effect"
    max_call_sites = 8
    arguments_schema = {
        "type": "object",
        "properties": {"value": {}},
        "required": ["value"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"value": {}},
        "additionalProperties": False,
    }
    calls: list[Any] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append(arguments["value"])
        return ToolOutcome.success({"value": arguments["value"]})


class _FailQuery(_EchoQuery):
    name = "fail_query"
    calls: list[Any] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append(arguments["value"])
        return ToolOutcome.failure("upstream_action_failed", "query failed")


class _SlowQuery(_EchoQuery):
    name = "slow_query"

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        await asyncio.sleep(0.05)
        return ToolOutcome.success({"value": arguments["value"]})


class _NotifyEffect(BaseTool):
    name = "notify"
    program_kind = "effect"
    max_call_sites = 2
    arguments_schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"accepted": {"type": "boolean"}},
        "additionalProperties": False,
    }
    calls: list[dict] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append({"arguments": dict(arguments), "context": context})
        return ToolOutcome.success({"accepted": True})


class _FailEffect(_NotifyEffect):
    name = "fail_effect"
    calls: list[dict] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append({"arguments": dict(arguments), "context": context})
        return ToolOutcome.failure(
            "upstream_action_failed", "effect failed", status="failed"
        )


class _RecordEffect(BaseTool):
    """返回一个 event_id 的 effect，供后续调用当作保留参数的实参来源。"""

    name = "record"
    program_kind = "effect"
    arguments_schema = {
        "type": "object",
        "properties": {"description": {"type": "string"}},
        "required": ["description"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"event_id": {"type": "string"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success(
            {"event_id": "EV_NEW"},
            emitted_events=[
                ToolGeneratedEvent(
                    event_type="agent.reflection_written",
                    payload={"text": arguments["description"]},
                )
            ],
        )


class _LongTextQuery(BaseTool):
    name = "long_text"
    program_kind = "query"
    text_value: str = "x" * 8000
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"text": type(self).text_value})


class _TaskLikeEffect(BaseTool):
    """schema 自带一个与保留名同名字段的 effect。

    2026-08-21 前这个同名字段是 ``task_id``；任务坍缩后保留名只剩
    ``triggered_by_event_id``，于是用它来验同一条规则：schema 声明了就是业务
    参数，只进 arguments，不进保留挂靠通道。
    """

    name = "task_like"
    program_kind = "effect"
    arguments_schema = {
        "type": "object",
        "properties": {
            "triggered_by_event_id": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["triggered_by_event_id", "note"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"echoed": {"type": "string"}},
        "additionalProperties": False,
    }
    calls: list[dict] = []

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        type(self).calls.append({"arguments": dict(arguments), "context": context})
        return ToolOutcome.success({"echoed": arguments["triggered_by_event_id"]})


class _HostValueQuery(BaseTool):
    name = "host_value"
    program_kind = "query"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"value": {}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        class _SecretHost:
            def __repr__(self) -> str:
                return "SECRET_HOST_REPR"

        return ToolOutcome.success({"value": _SecretHost()})


def _registry(*tools: type[BaseTool]) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _effect_handle(name: str = "tool") -> EffectCallHandle:
    return EffectCallHandle(
        tool_call_id=f"TC_{name}",
        called_event_id=f"CALLED_{name}",
        decision_id="DECISION",
        tool_name=name,
        call_site=f"1:0:{name}:1",
        occurrence=1,
    )


async def _execute(
    source: str,
    registry: ToolRegistry,
    *,
    call_timeout: float = 0.2,
    program_timeout: float = 0.5,
    persist: bool = True,
):
    prepared = preflight(source, registry, "group")
    executor = ProgramExecutor(
        registry=registry,
        session_factory=object(),
        scope_key="group:1",
        correlation_id="CORR",
        decision_id="DECISION",
        call_timeout_seconds=call_timeout,
        program_timeout_seconds=program_timeout,
    )
    if not persist:
        return await executor.execute(prepared)

    async def _begin(*args: Any, **kwargs: Any) -> EffectCallHandle:
        return _effect_handle(str(kwargs.get("tool_name") or "tool"))

    begin = AsyncMock(side_effect=_begin)
    finish = AsyncMock(return_value="TERMINAL")
    with (
        patch(
            "qqbot.services.agent_loop.program_runtime.begin_effect_call",
            new=begin,
        ),
        patch(
            "qqbot.services.agent_loop.program_runtime.finish_effect_call",
            new=finish,
        ),
    ):
        return await executor.execute(prepared)


class ProgramReadOnlyAbiContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _MembersQuery.calls.clear()
        _EchoQuery.calls.clear()

    async def test_query_values_support_fields_comprehension_join_and_fstring(
        self,
    ) -> None:
        result = await _execute(
            "\n".join(
                [
                    "result = members()",
                    "names = [m.card or m.nickname for m in result.members]",
                    "spoken = f'members: {join(\"、\", names)}'",
                    'return {"names": names, "spoken": spoken}',
                ]
            ),
            _registry(_MembersQuery),
        )
        self.assertEqual(
            result.result,
            {"names": ["Alice", "B"], "spoken": "members: Alice、B"},
        )
        self.assertEqual(result.trace.query_calls, [])
        self.assertEqual(len(result.trace.calls), 1)
        self.assertEqual(result.trace.calls[0].status, "ok")

    async def test_declared_but_missing_field_reads_as_none(self) -> None:
        result = await _execute(
            "result = members()\nreturn result.members[0].card",
            _registry(_MembersQuery),
        )
        self.assertTrue(result.has_result)
        self.assertIsNone(result.result)

    async def test_comment_only_program_is_a_successful_empty_program(self) -> None:
        result = await _execute("# intentionally idle", ToolRegistry())
        self.assertFalse(result.has_result)
        self.assertIsNone(result.result)
        self.assertEqual(result.trace.query_calls, [])
        self.assertEqual(result.trace.effect_call_ids, [])

    async def test_upstream_long_text_uses_result_side_quota(self) -> None:
        """webfetch/websearch 级别的长正文（8000 字）必须能被程序读到并切片；
        体积检查走结果侧上限，不得用程序侧 4000 把合法结果整段拒掉。"""
        result = await _execute(
            "\n".join(
                [
                    "page = long_text()",
                    'return {"head": page.text[0:20], "size": len(page.text)}',
                ]
            ),
            _registry(_LongTextQuery),
        )
        self.assertEqual(result.result, {"head": "x" * 20, "size": 8000})

    async def test_upstream_text_over_result_side_quota_fails(self) -> None:
        with patch.object(_LongTextQuery, "text_value", "x" * 20_001):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(
                    "page = long_text()\nreturn len(page.text)",
                    _registry(_LongTextQuery),
                )
        self.assertEqual(caught.exception.info.error_kind, "program_quota_exceeded")
        self.assertEqual(
            caught.exception.info.details["quota"], "result_string_chars"
        )

    async def test_deep_nested_return_fails_with_value_depth(self) -> None:
        """深嵌套返回值必须以契约错误中止（value_depth 配额），不允许以
        RecursionError 等宿主异常逃出执行器——那会让该拍没有 program
        terminal。"""
        source = "\n".join(
            [
                "acc = 0",
                f"for item in [{', '.join('0' for _ in range(20))}]:",
                "    acc = [acc]",
                "return acc",
            ]
        )
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(source, ToolRegistry())
        self.assertEqual(caught.exception.info.error_kind, "program_quota_exceeded")
        self.assertEqual(caught.exception.info.details["quota"], "value_depth")

    async def test_tool_cannot_leak_host_object_or_its_repr(self) -> None:
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(
                "result = host_value()\nreturn result.value",
                _registry(_HostValueQuery),
            )
        self.assertEqual(
            caught.exception.info.error_kind, "program_forbidden_construct"
        )
        self.assertNotIn("SECRET_HOST_REPR", caught.exception.info.message)


class ProgramEffectContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _NotifyEffect.calls.clear()
        _FailEffect.calls.clear()
        _EchoQuery.calls.clear()
        _FailQuery.calls.clear()
        _TaskLikeEffect.calls.clear()

    async def test_effect_is_persisted_as_intent_then_terminal(self) -> None:
        handle = EffectCallHandle(
            tool_call_id="TC1",
            called_event_id="CALLED1",
            decision_id="DECISION",
            tool_name="notify",
            call_site="1:0:notify:1",
            occurrence=1,
        )
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(return_value=handle),
            ) as begin,
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERMINAL1"),
            ) as finish,
        ):
            result = await _execute(
                'outcome = notify(message="hello")\nreturn outcome.accepted',
                _registry(_NotifyEffect),
                persist=False,
            )

        self.assertTrue(result.result)
        self.assertEqual(result.trace.effect_call_ids, ["TC1"])
        self.assertEqual(len(_NotifyEffect.calls), 1)
        begin.assert_awaited_once()
        finish.assert_awaited_once()
        self.assertTrue(finish.await_args.kwargs["outcome"].ok)
        self.assertEqual(finish.await_args.kwargs["handle"].called_event_id, "CALLED1")

    async def test_failed_call_returns_a_value_and_the_program_continues(
        self,
    ) -> None:
        """2026-08-15：失败即返回值。此前任一失败会中止整段程序，程序里因此
        写不出任何失败分支——一段不能处理失败的程序等价于一次 JSON action。
        现在失败调用返回 `ok=False` + `error`，后续语句照常执行。

        工具终态不受影响：`agent.tool_failed` 仍然照写，时间线上失败是既成
        事实；变的只是"这段程序还能不能继续"。"""
        handles = [
            _effect_handle("fail_query"),
            _effect_handle("notify"),
        ]
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(side_effect=handles),
            ) as begin,
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERM"),
            ) as finish,
        ):
            result = await _execute(
                'r = fail_query(value="x")\n'
                'if not r.ok:\n'
                '    notify(message="查失败了")\n'
                "return r.error.kind",
                _registry(_FailQuery, _NotifyEffect),
                persist=False,
            )
        self.assertEqual(result.result, "upstream_action_failed")
        # 两个调用都真的发生了：失败没有吃掉后面那一条。
        self.assertEqual(begin.await_count, 2)
        self.assertEqual(
            [call.kwargs["tool_name"] for call in begin.await_args_list],
            ["fail_query", "notify"],
        )
        self.assertEqual(len(_NotifyEffect.calls), 1)
        # 失败调用的终态仍是 tool_failed。
        self.assertFalse(finish.await_args_list[0].kwargs["outcome"].ok)
        self.assertTrue(finish.await_args_list[1].kwargs["outcome"].ok)

    async def test_unchecked_failure_field_read_aborts(self) -> None:
        """失败变成返回值之后，"不检查 ok"不再安全地把程序停住，而是让 None
        一路流进 f-string 与参数——群里会看见「找到 None 条」。守卫把这种情况
        变回一次干净的中止，并指出漏检了哪个函数的哪个字段。

        `ok` / `error` 本身照常可读，否则就没法检查了。"""
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(side_effect=[_effect_handle("fail_query")]),
            ),
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERM"),
            ),
        ):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(
                    'r = fail_query(value="x")\nreturn r.value',
                    _registry(_FailQuery),
                    persist=False,
                )
        self.assertEqual(
            caught.exception.info.error_kind, "program_unchecked_failure"
        )
        self.assertEqual(caught.exception.info.details["function"], "fail_query")
        self.assertEqual(caught.exception.info.details["field"], "value")

    async def test_successful_call_carries_the_outcome_envelope(self) -> None:
        """成功路径同样带 ok / error——信封是无条件的，否则模型得先知道成功
        与否才能读 ok，等于没有。"""
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(side_effect=[_effect_handle("echo")]),
            ),
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERM"),
            ),
        ):
            result = await _execute(
                'r = echo(value="x")\nreturn [r.ok, r.error, r.value]',
                _registry(_EchoQuery),
                persist=False,
            )
        self.assertEqual(list(result.result), [True, None, "x"])

    async def test_effect_failure_writes_terminal_and_the_program_continues(
        self,
    ) -> None:
        handle = EffectCallHandle(
            tool_call_id="TC_FAIL",
            called_event_id="CALLED_FAIL",
            decision_id="DECISION",
            tool_name="fail_effect",
            call_site="1:0:fail_effect:1",
            occurrence=1,
        )
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(return_value=handle),
            ),
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERMINAL_FAIL"),
            ) as finish,
        ):
            result = await _execute(
                'e = fail_effect(message="x")\n'
                'later = echo(value="仍然执行")\n'
                "return [e.ok, e.error.status, later.value]",
                _registry(_FailEffect, _EchoQuery),
                persist=False,
            )
        self.assertEqual(list(result.result), [False, "failed", "仍然执行"])
        self.assertEqual(_EchoQuery.calls, ["仍然执行"])
        # 第一次 finish 写的仍是失败终态。
        self.assertFalse(finish.await_args_list[0].kwargs["outcome"].ok)

    async def test_declared_business_field_is_not_a_reserved_anchor(self) -> None:
        """schema 已声明的同名字段只进 arguments，不进保留挂靠通道——
        与静态层 reserved = 保留名 - declared 同一口径。否则工具自己的业务
        参数会被执行层截走当成系统锚，工具收到的 arguments 里反而没有它。"""
        handle = EffectCallHandle(
            tool_call_id="TC_TASKLIKE",
            called_event_id="CALLED_TASKLIKE",
            decision_id="DECISION",
            tool_name="task_like",
            call_site="1:0:task_like:1",
            occurrence=1,
        )
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(return_value=handle),
            ) as begin,
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(return_value="TERM_TASKLIKE"),
            ),
        ):
            result = await _execute(
                'done = task_like(triggered_by_event_id="E1", note="进展")\n'
                "return done.echoed",
                _registry(_TaskLikeEffect),
                persist=False,
            )

        self.assertEqual(result.result, "E1")
        begin_kwargs = begin.await_args.kwargs
        self.assertIsNone(begin_kwargs["triggered_by_event_id"])
        self.assertEqual(begin_kwargs["arguments"]["triggered_by_event_id"], "E1")
        self.assertEqual(
            _TaskLikeEffect.calls[0]["arguments"]["triggered_by_event_id"], "E1"
        )
        self.assertIsNone(
            _TaskLikeEffect.calls[0]["context"]["triggered_by_event_id"]
        )

    async def test_effect_result_variable_can_feed_a_reserved_argument(self) -> None:
        """前一个 effect 的返回值可以当后一个 effect 保留参数的实参。

        2026-08-21 前这条验的是 task_create 返回 task_id → 后续 effect
        task_id=；任务坍缩后 task_id 值域消失，同一条组合性质改由
        triggered_by_event_id 承担。
        """
        handles = [
            EffectCallHandle(
                tool_call_id="TC_RECORD",
                called_event_id="CALLED_RECORD",
                decision_id="DECISION",
                tool_name="record",
                call_site="1:0:record:1",
                occurrence=1,
            ),
            EffectCallHandle(
                tool_call_id="TC_NOTIFY",
                called_event_id="CALLED_NOTIFY",
                decision_id="DECISION",
                tool_name="notify",
                call_site="2:0:notify:1",
                occurrence=1,
            ),
        ]
        with (
            patch(
                "qqbot.services.agent_loop.program_runtime.begin_effect_call",
                new=AsyncMock(side_effect=handles),
            ) as begin,
            patch(
                "qqbot.services.agent_loop.program_runtime.finish_effect_call",
                new=AsyncMock(side_effect=["TERM_RECORD", "TERM_NOTIFY"]),
            ),
        ):
            result = await _execute(
                "\n".join(
                    [
                        'r = record(description="work")',
                        'notify(message="done", triggered_by_event_id=r.event_id)',
                        "return r.event_id",
                    ]
                ),
                _registry(_RecordEffect, _NotifyEffect),
                persist=False,
            )

        self.assertEqual(result.result, "EV_NEW")
        self.assertEqual(result.trace.effect_call_ids, ["TC_RECORD", "TC_NOTIFY"])
        second_call = begin.await_args_list[1].kwargs
        self.assertEqual(second_call["triggered_by_event_id"], "EV_NEW")
        self.assertEqual(
            _NotifyEffect.calls[0]["context"]["triggered_by_event_id"], "EV_NEW"
        )


class ProgramDynamicQuotaContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _EchoQuery.calls.clear()

    async def test_dynamic_program_call_limit(self) -> None:
        source = 'echo(value=1)\necho(value=2)\nreturn 1'
        with patch(
            "qqbot.services.agent_loop.program_runtime.MAX_PROGRAM_CALLS", 1
        ):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(source, _registry(_EchoQuery))
        self.assertEqual(caught.exception.info.error_kind, "program_quota_exceeded")
        self.assertEqual(caught.exception.info.details["quota"], "program_calls")
        self.assertEqual(len(_EchoQuery.calls), 1)

    async def test_iteration_limit(self) -> None:
        source = (
            "total = 0\nfor item in [1, 2, 3]:\n    total = total + item\nreturn total"
        )
        with patch("qqbot.services.agent_loop.program_runtime.MAX_ITERATIONS", 2):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(source, ToolRegistry())
        self.assertEqual(caught.exception.info.details["quota"], "iterations")

    async def test_statement_limit(self) -> None:
        source = "one = 1\ntwo = 2\nreturn one + two"
        with patch("qqbot.services.agent_loop.program_runtime.MAX_STATEMENTS", 2):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(source, ToolRegistry())
        self.assertEqual(caught.exception.info.details["quota"], "statements")

    async def test_string_growth_is_checked_during_arithmetic(self) -> None:
        source = (
            'text = "a"\nfor item in ["x", "x", "x"]:\n'
            '    text = text + "xx"\nreturn text'
        )
        with patch("qqbot.services.agent_loop.program_runtime.MAX_STRING_LENGTH", 5):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute(source, ToolRegistry())
        self.assertEqual(caught.exception.info.details["quota"], "string_chars")

    async def test_runtime_container_limit(self) -> None:
        with patch(
            "qqbot.services.agent_loop.program_runtime.MAX_CONTAINER_ELEMENTS", 2
        ):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute("return [1, 2, 3]", ToolRegistry())
        self.assertEqual(caught.exception.info.details["quota"], "container_elements")

    async def test_return_byte_limit_fails_instead_of_truncating(self) -> None:
        with patch("qqbot.services.agent_loop.program_runtime.MAX_RETURN_BYTES", 8):
            with self.assertRaises(ProgramExecutionError) as caught:
                await _execute('return {"text": "long"}', ToolRegistry())
        self.assertEqual(caught.exception.info.error_kind, "program_output_too_large")
        self.assertGreater(caught.exception.info.details["actual_bytes"], 8)

    async def test_call_timeout(self) -> None:
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(
                'return slow_query(value="x").value',
                _registry(_SlowQuery),
                call_timeout=0.01,
                program_timeout=0.2,
            )
        self.assertEqual(caught.exception.info.error_kind, "program_timeout")
        self.assertEqual(caught.exception.info.details["scope"], "call")

    async def test_program_timeout(self) -> None:
        with self.assertRaises(ProgramExecutionError) as caught:
            await _execute(
                'return slow_query(value="x").value',
                _registry(_SlowQuery),
                call_timeout=0.2,
                program_timeout=0.01,
            )
        self.assertEqual(caught.exception.info.error_kind, "program_timeout")
        self.assertEqual(caught.exception.info.details["scope"], "program")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
