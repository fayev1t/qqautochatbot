"""Contracts for the single Program API registry."""

# The small registry double exposes immutable-by-convention class metadata.
# ruff: noqa: ARG002, RUF012

from __future__ import annotations

import unittest
from typing import Any

from qqbot.core.permissions import PermissionTier
from qqbot.services.agent_loop.tool_registry import (
    OUTCOME_ERROR_SCHEMA,
    OUTCOME_FIELDS,
    BaseTool,
    ToolOutcome,
    ToolRegistry,
    get_tool_program_kind,
)
from qqbot.services.agent_loop.tools import build_default_registry


class _StubTool(BaseTool):
    name = "stub"
    description = "stub description"
    usage_prompt = "stub usage"
    arguments_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"echo": {"type": "string"}},
        "required": ["echo"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"echo": arguments["value"]})


class ToolRegistryContractTest(unittest.TestCase):
    def test_register_builds_program_spec_and_fresh_instances(self) -> None:
        registry = ToolRegistry()
        registry.register(_StubTool)

        spec = registry.spec("stub")
        assert spec is not None
        self.assertEqual(spec.program_kind, "effect")
        self.assertEqual(spec.max_call_sites, 2)
        self.assertEqual(spec.required_permission, PermissionTier.GUEST)
        self.assertIn("triggered_by_event_id=None", spec.signature)
        # 2026-08-21：task_id 随任务坍缩为单栏便签退出保留参数面（§一②）。
        self.assertNotIn("task_id", spec.signature)
        self.assertIsNot(registry.get("stub"), registry.get("stub"))

    def test_legacy_query_kind_is_normalized_to_effect(self) -> None:
        class _Query(_StubTool):
            name = "query_stub"
            program_kind = "query"

        registry = ToolRegistry()
        registry.register(_Query)
        spec = registry.spec("query_stub")
        assert spec is not None
        self.assertEqual(spec.program_kind, "effect")
        self.assertIn("triggered_by_event_id=None", spec.signature)
        self.assertNotIn("task_id", spec.signature)

    def test_scope_filtering_uses_same_specs_as_runtime(self) -> None:
        class _GroupOnly(_StubTool):
            name = "group_only"
            allowed_scopes = ("group",)

        registry = ToolRegistry()
        registry.register(_StubTool)
        registry.register(_GroupOnly)
        self.assertEqual(registry.names("system"), ["stub"])
        self.assertEqual(registry.names("group"), ["group_only", "stub"])

    def test_usage_docs_are_complete_program_api_reference(self) -> None:
        registry = ToolRegistry()
        registry.register(_StubTool)
        rendered = registry.usage_docs("group")
        self.assertIn("## 程序函数：stub", rendered)
        self.assertIn("stub(*, value", rendered)
        self.assertIn("参数 schema", rendered)
        self.assertIn("返回 schema", rendered)
        self.assertIn("stub usage", rendered)
        self.assertIn("只有 return 的 JSON", rendered)
        self.assertFalse(hasattr(registry, "catalog"))

    def test_registration_rejects_invalid_metadata(self) -> None:
        registry = ToolRegistry()

        class _BadKind(_StubTool):
            name = "bad_kind"
            program_kind = "background"

        with self.assertRaises(ValueError):
            registry.register(_BadKind)

        class _BadResult(_StubTool):
            name = "bad_result"
            result_schema = None

        with self.assertRaises(ValueError):
            registry.register(_BadResult)

        class _BadName(_StubTool):
            name = "bad-name"

        with self.assertRaises(ValueError):
            registry.register(_BadName)

    def test_default_program_api_contains_exactly_active_eighteen_tools(self) -> None:
        registry = build_default_registry()
        self.assertEqual(
            set(registry.names()),
            {
                "get_group_info",
                "get_member_info",
                "get_member_list",
                "get_pending_join_requests",
                "get_recent_thoughts",
                "kick",
                "leave_group",
                "look_at_image",
                "meme_collection",
                "poke",
                "reflect",
                "respond_to_group_join_request",
                "search_history",
                "send_messages",
                "task",
                "wait",
                "webfetch",
                "websearch",
            },
        )
        self.assertEqual(len(registry), 18)

    def test_all_active_tools_declare_machine_readable_result_abi(self) -> None:
        registry = build_default_registry()
        for spec in registry.specs():
            with self.subTest(tool=spec.name):
                self.assertEqual(spec.program_kind, "effect")
                self.assertIsInstance(spec.result_schema, dict)
                self.assertTrue(spec.result_schema)
                self.assertGreaterEqual(spec.max_call_sites, 1)

    def test_active_result_schema_keys_are_locked(self) -> None:
        expected_top_level = {
            "get_group_info": {
                "group_create_time",
                "group_id",
                "group_name",
                "group_remark",
                "max_member_count",
                "member_count",
            },
            "get_member_info": {
                "banned_until",
                "card",
                "join_time",
                "last_sent_time",
                "level",
                "nickname",
                "role",
                "title",
                "user_id",
            },
            "get_member_list": {"count", "matched", "members"},
            "get_pending_join_requests": {
                "group_id",
                "handled_recent_count",
                "may_be_incomplete",
                "pending_count",
                "requests",
            },
            "kick": {"applied", "group_id", "reject_add_request", "user_id"},
            "leave_group": {"group_id", "is_dismiss", "left"},
            "look_at_image": {"answer", "image_hash", "question"},
            "poke": {"group_id", "user_id"},
            "meme_collection": {
                "action",
                "already_saved",
                "already_saved_count",
                "batch",
                "deleted",
                "description",
                "failed_count",
                "file_hash",
                "previous_description",
                "recaptioned",
                "results",
                "saved",
                "saved_count",
            },
            "respond_to_group_join_request": {
                "applied",
                "approve",
                "group_id",
                "request_event_id",
                "user_id",
            },
            "search_history": {
                "anchor_event_id",
                "items",
                "matched",
                "warnings",
            },
            "send_messages": {"message_ids", "sent_messages", "status"},
            "task": {"cleared", "chars"},
            "wait": {"note", "scheduled", "seconds", "wake_at"},
            "reflect": {"chars", "written"},
            "get_recent_thoughts": {"returned", "ticks"},
            "webfetch": {
                "content_type",
                "final_url",
                "status_code",
                "text",
                "title",
                "truncated",
                "url",
            },
            "websearch": {"engine", "query", "results", "warnings"},
        }
        expected_array_items = {
            ("get_member_list", "members"): {
                "banned_until",
                "card",
                "join_time",
                "last_sent_time",
                "nickname",
                "role",
                "user_id",
            },
            ("get_pending_join_requests", "requests"): {
                "comment",
                "nickname",
                "user_id",
            },
            ("meme_collection", "results"): {
                "already_saved",
                "description",
                "error",
                "error_kind",
                "file_hash",
                "retryable",
                "saved",
            },
            ("search_history", "items"): {
                "event_id",
                "kind",
                "occurred_at",
                "render",
            },
            ("send_messages", "sent_messages"): {
                # 2026-08-14 去协议化：回执逐气泡回显领域键，不再回显 OneBot
                # 段数组（原 "content"）。
                "at",
                "face",
                "image_hash",
                "index",
                "kind",
                "message_id",
                "receipt",
                "reply",
                "self_id",
                "status",
                "text",
            },
            ("websearch", "results"): {
                "fetch_error",
                "fetched_text",
                "snippet",
                "title",
                "url",
            },
        }

        registry = build_default_registry()
        self.assertEqual(set(registry.names()), set(expected_top_level))
        for name, expected_keys in expected_top_level.items():
            with self.subTest(tool=name):
                spec = registry.spec(name)
                assert spec is not None
                properties = spec.result_schema.get("properties") or {}
                # ok/error 由结果信封统一注入（2026-08-15），不属于任何工具
                # 自己声明的业务字段——这里只锁业务字段。
                self.assertEqual(
                    set(properties) - set(OUTCOME_FIELDS), expected_keys
                )
        for (name, field), expected_keys in expected_array_items.items():
            with self.subTest(tool=name, field=field):
                spec = registry.spec(name)
                assert spec is not None
                field_schema = spec.result_schema["properties"][field]
                item_properties = field_schema["items"].get("properties") or {}
                self.assertEqual(set(item_properties), expected_keys)

    def test_every_result_schema_carries_the_outcome_envelope(self) -> None:
        """2026-08-15 失败即返回值：每个函数的返回都带 ok / error。

        注入点必须只有 `with_outcome_envelope` 一处——静态字段校验
        （program_ast）、给模型的返回 schema（usage_docs）、运行时的
        `wrap_program_value` 读的是同一份 spec.result_schema，任何一处自带
        一套就会出现"模型看得到但校验不认"的字段。

        同时钉住：工具自己不得声明 ok/error（ToolOutcome 已经承载成功/失败，
        重复声明必然漂移），以及原 required 被移到 x-payload-required——失败
        返回里那些字段是 null，schema 不能继续声称它们必然存在。
        """
        registry = build_default_registry()
        for spec in registry.specs():
            with self.subTest(tool=spec.name):
                properties = spec.result_schema["properties"]
                self.assertEqual(properties["ok"]["type"], "boolean")
                self.assertEqual(properties["error"], OUTCOME_ERROR_SCHEMA)
                self.assertEqual(
                    list(spec.result_schema["required"]), list(OUTCOME_FIELDS)
                )
                payload_required = spec.result_schema.get("x-payload-required")
                self.assertIsInstance(payload_required, list)
                for field in payload_required:
                    self.assertIn(field, properties)
                    self.assertNotIn(field, OUTCOME_FIELDS)

    def test_tool_declaring_envelope_fields_is_rejected(self) -> None:
        class _Clashing:
            name = "clashing"
            description = "x"
            arguments_schema: dict = {"type": "object", "properties": {}}
            result_schema: dict = {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
            }

            async def run(self, arguments: dict, **context: object) -> object:
                return {}

        with self.assertRaises(ValueError) as caught:
            ToolRegistry().register(_Clashing())
        self.assertIn("outcome envelope", str(caught.exception))

    def test_all_active_tools_are_effects(self) -> None:
        registry = build_default_registry()
        kinds = {spec.program_kind for spec in registry.specs()}
        self.assertEqual(kinds, {"effect"})
        self.assertEqual(get_tool_program_kind(registry.get("websearch")), "effect")
        self.assertEqual(get_tool_program_kind(registry.get("send_messages")), "effect")
        self.assertEqual(get_tool_program_kind(registry.get("poke")), "effect")
        self.assertEqual(get_tool_program_kind(registry.get("reflect")), "effect")


if __name__ == "__main__":
    unittest.main()
