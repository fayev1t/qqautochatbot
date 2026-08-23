"""Static-contract tests for restricted Planner programs."""

# Test doubles intentionally expose immutable-by-convention class metadata.
# ruff: noqa: ARG002, RUF012

from __future__ import annotations

import ast
import hashlib
import unittest
from typing import Any
from unittest.mock import patch

from qqbot.services.agent_loop.program_ast import (
    MAX_AST_NODES,
    MAX_CONTAINER_ELEMENTS,
    MAX_SOURCE_CHARS,
    MAX_STRING_LENGTH,
    ProgramPreflightError,
    build_executable_tree,
    preflight,
    strip_outer_fence,
)
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
)


class _MembersQuery(BaseTool):
    name = "members"
    program_kind = "effect"
    max_call_sites = 4
    allowed_scopes = ("group",)
    arguments_schema = {
        "type": "object",
        "properties": {"role": {"type": "string", "default": None}},
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

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"members": []})


class _NotifyEffect(BaseTool):
    name = "notify"
    program_kind = "effect"
    max_call_sites = 2
    allowed_scopes = ("group",)
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

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"accepted": True})


class _SendMessagesEffect(BaseTool):
    name = "send_messages"
    program_kind = "effect"
    max_call_sites = 2
    allowed_scopes = ("group",)
    arguments_schema = {
        "type": "object",
        "properties": {"messages": {"type": "array", "items": {}}},
        "required": ["messages"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"status": "sent"})


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_MembersQuery)
    registry.register(_NotifyEffect)
    registry.register(_SendMessagesEffect)
    return registry


class ProgramFenceContractTests(unittest.TestCase):
    def test_accepts_bare_python_and_both_outer_fences(self) -> None:
        expected = '# decide nothing\nreturn {"ok": True}'
        self.assertEqual(strip_outer_fence(expected), expected)
        self.assertEqual(strip_outer_fence(f"```python\n{expected}\n```"), expected)
        self.assertEqual(strip_outer_fence(f"```\n{expected}\n```"), expected)

    def test_rejects_prose_outside_or_extra_fences(self) -> None:
        for source in (
            "Here is the program:\n```python\nreturn None\n```",
            "```python\nreturn None\n```\nextra",
            "```python\n```text\nx\n```\n```",
        ):
            with self.subTest(source=source):
                with self.assertRaises(ProgramPreflightError) as caught:
                    strip_outer_fence(source)
                self.assertEqual(
                    caught.exception.info.error_kind, "program_syntax_error"
                )


class ProgramWhitelistContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()

    def _error(self, source: str, scope: str = "group"):
        with self.assertRaises(ProgramPreflightError) as caught:
            preflight(source, self.registry, scope)
        return caught.exception.info

    def test_common_query_transform_return_program_passes(self) -> None:
        prepared = preflight(
            "\n".join(
                [
                    'result = members(role="admin")',
                    "names = [m.card or m.nickname for m in result.members]",
                    'return {"names": names, "spoken": join("、", names)}',
                ]
            ),
            self.registry,
            "group",
        )
        self.assertTrue(prepared.has_return)
        self.assertEqual([site.name for site in prepared.call_sites], ["members"])

    def test_normal_send_messages_payload_fits_depth_limit(self) -> None:
        source = """send_messages(
    messages=[{"kind": "chat", "content": [
        {"type": "text", "data": {"text": "hello"}}
    ]}],
    triggered_by_event_id="01JZQ8",
)"""
        prepared = preflight(source, self.registry, "group")
        self.assertEqual(prepared.call_sites[0].name, "send_messages")

    def test_forbidden_constructs_are_rejected(self) -> None:
        cases = {
            "import": "import os",
            "while": "while True:\n    pass",
            "def": "def helper():\n    return 1",
            "lambda": "fn = lambda x: x",
            "try": "try:\n    x = 1\nexcept Exception:\n    x = 2",
            "raise": 'raise RuntimeError("x")',
            "with": "with resource:\n    x = 1",
            "del": "x = 1\ndel x",
            "assert": "assert True",
            "await": 'await notify(message="x")',
            "method": 'text = "x"\ntext.upper()',
            "dunder": "result = members()\nreturn result.__class__",
            "subscript_write": "items = [1]\nitems[0] = 2",
            "star_unpack": "items = [1]\ncopy = [*items]",
            "set": "items = {1, 2}",
            "set_comp": "items = {x for x in [1]}",
            "nested_comp": "items = [x for row in [[1]] for x in row]",
            "pow": "value = 2 ** 3",
            "sequence_multiply": 'value = "x" * 2',
        }
        for label, source in cases.items():
            with self.subTest(label=label):
                info = self._error(source)
                self.assertEqual(info.error_kind, "program_forbidden_construct")

    def test_unavailable_host_names_have_stable_error_kind(self) -> None:
        for name in ("eval", "exec", "getattr", "type", "range", "open"):
            with self.subTest(name=name):
                info = self._error(f"return {name}(1)")
                self.assertEqual(info.error_kind, "program_unknown_name")
                self.assertEqual(info.details["name"], name)

    def test_multiline_string_is_rejected_at_model_source_line(self) -> None:
        info = self._error('x = 1\ntext = """a\nb"""\nreturn text')
        self.assertEqual(info.error_kind, "program_forbidden_construct")
        self.assertEqual(info.details["construct"], "multiline_string")
        self.assertEqual(info.line, 2)

    def test_effects_cannot_hide_in_loops_or_comprehensions(self) -> None:
        for source in (
            'for item in ["a"]:\n    notify(message=item)',
            'calls = [notify(message=item) for item in ["a"]]',
        ):
            with self.subTest(source=source):
                info = self._error(source)
                self.assertEqual(
                    info.details["construct"],
                    "effect_in_loop_or_comprehension",
                )

    def test_return_cannot_hide_in_loop(self) -> None:
        info = self._error("for item in [1]:\n    return item")
        self.assertEqual(info.details["construct"], "return_in_loop")

    def test_effect_call_site_quota_is_static(self) -> None:
        info = self._error(
            'notify(message="a")\nnotify(message="b")\nnotify(message="c")'
        )
        self.assertEqual(info.error_kind, "program_quota_exceeded")
        self.assertEqual(info.details["quota"], "effect_call_sites:notify")
        self.assertEqual(info.details["actual"], 3)
        self.assertEqual(info.details["max"], 2)

    def test_program_functions_require_named_arguments(self) -> None:
        info = self._error('notify("hello")')
        self.assertEqual(info.details["construct"], "program_function_positional_args")

    def test_former_query_accepts_system_reserved_arguments(self) -> None:
        prepared = preflight(
            'members(triggered_by_event_id="E1")',
            self.registry,
            "group",
        )
        self.assertEqual(prepared.call_sites[0].name, "members")

    def test_task_id_is_no_longer_a_reserved_argument(self) -> None:
        """2026-08-21：任务坍缩为单栏便签，task_id 值域消失（§一②）。

        它现在只是个普通未知具名参数，静态层当场拒掉——而不是被执行层默默
        当成挂靠锚吞掉。同时抽掉的还有敏感工具权限反查的回退路径，见
        planner.md：triggered_by_event_id 必须显式传。
        """
        info = self._error('notify(message="x", task_id="T1")')
        self.assertEqual(info.details["construct"], "unknown_keyword")

    def test_effect_reserved_arguments_must_be_string_or_null(self) -> None:
        for source in (
            'notify(message="x", triggered_by_event_id=True)',
            'notify(message="x", triggered_by_event_id=123)',
        ):
            with self.subTest(source=source):
                info = self._error(source)
                self.assertEqual(info.details["construct"], "reserved_argument_type")

    def test_fstring_only_accepts_json_scalars(self) -> None:
        info = self._error('result = members()\ntext = f"{result.members}"')
        self.assertEqual(info.details["construct"], "fstring_non_scalar")

    def test_scope_hidden_function_is_unknown_during_preflight(self) -> None:
        info = self._error("result = members()", scope="system")
        self.assertEqual(info.error_kind, "program_unknown_name")
        self.assertEqual(info.details["name"], "members")

    def test_schema_field_reads_are_checked_statically(self) -> None:
        preflight(
            "result = members()\nreturn result.members[0].nickname",
            self.registry,
            "group",
        )
        info = self._error("result = members()\nreturn result.secret")
        self.assertEqual(info.error_kind, "program_unknown_field")
        self.assertEqual(info.details["function"], "members")
        self.assertEqual(info.details["field"], "secret")

    def test_call_sites_are_numbered_in_source_order(self) -> None:
        prepared = preflight(
            "first = members()\nsecond = members()\nreturn len(first.members)",
            self.registry,
            "group",
        )
        self.assertEqual([site.occurrence for site in prepared.call_sites], [1, 2])
        self.assertTrue(
            all(
                site.call_site.endswith(f":members:{site.occurrence}")
                for site in prepared.call_sites
            )
        )


class ProgramStaticQuotaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()

    def _quota(self, source: str):
        with self.assertRaises(ProgramPreflightError) as caught:
            preflight(source, self.registry, "group")
        self.assertEqual(caught.exception.info.error_kind, "program_quota_exceeded")
        return caught.exception.info.details

    def test_source_character_limit(self) -> None:
        details = self._quota("#" + ("x" * MAX_SOURCE_CHARS))
        self.assertEqual(details["quota"], "source_chars")

    def test_ast_node_limit(self) -> None:
        source = "items = [" + ",".join("0" for _ in range(MAX_AST_NODES)) + "]"
        details = self._quota(source)
        self.assertEqual(details["quota"], "ast_nodes")

    def test_container_element_limit_is_independently_enforced(self) -> None:
        source = (
            "items = [" + ",".join("0" for _ in range(MAX_CONTAINER_ELEMENTS + 1)) + "]"
        )
        with patch(
            "qqbot.services.agent_loop.program_ast.MAX_AST_NODES",
            MAX_CONTAINER_ELEMENTS * 2,
        ):
            details = self._quota(source)
        self.assertEqual(details["quota"], "container_elements")

    def test_string_character_limit(self) -> None:
        details = self._quota('text = "' + ("x" * (MAX_STRING_LENGTH + 1)) + '"')
        self.assertEqual(details["quota"], "string_chars")

    def test_model_visible_nesting_limit(self) -> None:
        source = "value = 0\n"
        indent = ""
        for _ in range(9):
            source += f"{indent}if True:\n"
            indent += "    "
        source += f"{indent}value = 1\nreturn value"
        details = self._quota(source)
        self.assertEqual(details["quota"], "syntax_depth")


class ExecuteDecisionContractTests(unittest.TestCase):
    """裁决层调度元指令的静态形状（2026-08-17 提案-裁决流水线 §1.0/§1.1）。

    它不是 Program API 工具，不占调用点；与动作层新代码**可以写在同一次输出里**
    （④ 流水线混合）。形状必须在静态期就确定：模块顶层的独立语句、唯一具名参数、
    写死的事件 ID。
    """

    PROGRAM_HASH = "8f3c4e5a6b7c"

    def setUp(self) -> None:
        self.registry = _registry()

    def _error(self, source: str, scope: str = "group"):
        with self.assertRaises(ProgramPreflightError) as caught:
            preflight(source, self.registry, scope)
        return caught.exception.info

    def test_bare_commit_statement_yields_commit_program_hash(self) -> None:
        prepared = preflight(
            f'execute_program(program_hash="{self.PROGRAM_HASH}")',
            self.registry,
            "group",
        )
        self.assertEqual(prepared.commit_program_hash, self.PROGRAM_HASH)
        # 它自己不是调用点：裁决拍没有任何工具被调用。
        self.assertEqual(prepared.call_sites, ())
        self.assertFalse(prepared.has_return)

    def test_ordinary_program_has_no_commit_program_hash(self) -> None:
        prepared = preflight(
            'notify(message="hi")', self.registry, "group"
        )
        self.assertIsNone(prepared.commit_program_hash)
        self.assertEqual(len(prepared.call_sites), 1)

    def test_a_commit_and_new_code_coexist_in_one_response(self) -> None:
        """④ 流水线混合：两层可以同时出现——它们作用在不同的东西上。

        2026-08-17 维护者裁定：此前的 `program_mixed_commit_and_action` 是错的，
        它把调度指令当成了程序的一部分才推出"同一 AST 两种生效时机"。稳态下模型
        每拍都该两层都写：确认上一段、同时写下一段，多出来的那次推理才被摊掉。
        """
        prepared = preflight(
            f'execute_program(program_hash="{self.PROGRAM_HASH}")\n'
            'notify(message="hi")',
            self.registry,
            "group",
        )
        self.assertEqual(prepared.commit_program_hash, self.PROGRAM_HASH)
        self.assertEqual([site.name for site in prepared.call_sites], ["notify"])

        with_return = preflight(
            f'execute_program(program_hash="{self.PROGRAM_HASH}")\nreturn 1',
            self.registry,
            "group",
        )
        self.assertEqual(with_return.commit_program_hash, self.PROGRAM_HASH)
        self.assertTrue(with_return.has_return)

    def test_the_directive_is_stripped_before_storage(self) -> None:
        """落库解耦（§1.1 防套娃）：`source` / sha / 可执行树都不含调度指令。

        存进 `payload.program` 的必须是纯业务代码——否则那条决策日后被指名时会
        连带再调度一次，形成套娃。
        """
        prepared = preflight(
            f'execute_program(program_hash="{self.PROGRAM_HASH}")\n'
            'notify(message="hi")',
            self.registry,
            "group",
        )
        self.assertEqual(prepared.source, 'notify(message="hi")')
        self.assertEqual(
            prepared.program_sha256,
            hashlib.sha256(prepared.source.encode("utf-8")).hexdigest(),
        )
        self.assertNotIn(
            "execute_program", ast.unparse(build_executable_tree(prepared))
        )
        # 落库的源码再走一遍 preflight（裁决时就是这么做的）必须干净。
        again = preflight(prepared.source, self.registry, "group")
        self.assertIsNone(again.commit_program_hash)
        self.assertEqual(
            [site.name for site in again.call_sites],
            [site.name for site in prepared.call_sites],
        )

    def test_a_pure_directive_leaves_an_empty_body(self) -> None:
        """③ 纯裁决：剥完只剩空串，动作层为空，日后不可被指名。"""
        prepared = preflight(
            f'execute_program(program_hash="{self.PROGRAM_HASH}")',
            self.registry,
            "group",
        )
        self.assertEqual(prepared.source, "")
        self.assertEqual(prepared.call_sites, ())
        self.assertFalse(prepared.has_return)

    def test_a_multiline_directive_is_stripped_whole(self) -> None:
        prepared = preflight(
            f'execute_program(\n    program_hash="{self.PROGRAM_HASH}",\n)\n'
            'notify(message="hi")',
            self.registry,
            "group",
        )
        self.assertEqual(prepared.commit_program_hash, self.PROGRAM_HASH)
        self.assertEqual(prepared.source, 'notify(message="hi")')

    def test_program_hash_must_be_a_literal_12_hex(self) -> None:
        cases = {
            'execute_program(program_hash="not-a-hash")': (
                "commit_program_hash_malformed"
            ),
            # 大写十六进制不是合法 hash（展示前缀恒为小写）。
            'execute_program(program_hash="8F3C4E5A6B7C")': (
                "commit_program_hash_malformed"
            ),
            # 长度必须恰好 12：短一位、长一位都不行。
            'execute_program(program_hash="8f3c4e5a6b7")': (
                "commit_program_hash_malformed"
            ),
            'execute_program(program_hash="8f3c4e5a6b7cd")': (
                "commit_program_hash_malformed"
            ),
            # 26 位事件 ID 属于另一个值域，不能拿来当代码资产 hash。
            'execute_program(program_hash="01K2X9F3MQ8B4NVYRTC7HDZ6EW")': (
                "commit_program_hash_malformed"
            ),
            f'e = "{self.PROGRAM_HASH}"\nexecute_program(program_hash=e)': (
                "commit_program_hash_not_literal"
            ),
        }
        for source, construct in cases.items():
            with self.subTest(source=source):
                info = self._error(source)
                self.assertEqual(info.error_kind, "program_forbidden_construct")
                self.assertEqual(info.details["construct"], construct)

    def test_commit_must_be_a_top_level_statement(self) -> None:
        nested = self._error(
            f'if 1 == 1:\n    execute_program(program_hash="{self.PROGRAM_HASH}")'
        )
        self.assertEqual(nested.details["construct"], "commit_not_top_level")
        assigned = self._error(
            f'x = execute_program(program_hash="{self.PROGRAM_HASH}")'
        )
        self.assertEqual(assigned.details["construct"], "commit_not_a_statement")

    def test_signature_is_exactly_one_named_event_id(self) -> None:
        positional = self._error(f'execute_program("{self.PROGRAM_HASH}")')
        self.assertEqual(
            positional.details["construct"], "program_function_positional_args"
        )
        extra = self._error(
            f'execute_program(program_hash="{self.PROGRAM_HASH}", extra=1)'
        )
        self.assertEqual(extra.details["construct"], "commit_signature")

    def test_one_commit_per_program(self) -> None:
        info = self._error(
            f'execute_program(program_hash="{self.PROGRAM_HASH}")\n'
            f'execute_program(program_hash="{self.PROGRAM_HASH}")'
        )
        self.assertEqual(info.error_kind, "program_quota_exceeded")
        self.assertEqual(info.details["max"], 1)

    def test_the_name_cannot_be_shadowed(self) -> None:
        info = self._error("execute_program = 1")
        self.assertEqual(info.details["construct"], "reserved_assignment_name")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
