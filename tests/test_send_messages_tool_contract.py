"""SendMessagesTool 合同（2026-07-31 删除 Replyer：Planner 亲自发言的出口）。

钉住四组边界（v2.0/30-工具设计/发言链路设计.md §2–§4）：
- 普通 Program Effect：不查任何前置状态——没有任何前置事件时
  调用照常执行；不写任何领域/runtime 事件（发送事实只活在 terminal 里）；
- 静态校验与 meme preflight 失败无副作用（不碰 OneBot）；
- 结果语义：sent → success；partial / failed / uncertain → failure，
  status 与完整逐条 receipts 经 extra 平铺进 tool_failed payload，供投影渲染
  该调用自己的 `<tool>send_messages` 行块（**不派生**第二份发言行）；
- allowed_scopes 只有 group；回执脱敏（base64 不进事件流）。
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.outbound_messages import (
    _ALLOWED_SEGMENT_TYPES,
    MAX_OUTBOUND_MESSAGES,
    build_chat_content,
    validate_messages,
)
from qqbot.services.agent_loop.tools.send_messages import SendMessagesTool

HASH_A = "ab" * 32
_TEXT = {"type": "text", "data": {"text": "hi"}}  # 迁移前的段形状
_CHAT = {"text": "hi"}


class _FakeActionFailed(Exception):
    def __init__(self, retcode: int, wording: str) -> None:
        super().__init__(f"ActionFailed: retcode={retcode}")
        self.info = {
            "status": "failed",
            "retcode": retcode,
            "message": "",
            "wording": wording,
            "stream": "normal-action",
        }


class NetworkError(Exception):
    pass


class _StubBot:
    def __init__(
        self,
        message_ids: list[int | None] | None = None,
        raises: list[Exception | None] | None = None,
    ) -> None:
        self.self_id = "10001"
        self._message_ids = message_ids or [111, 222, 333, 444]
        self._raises = raises or []
        self.calls: list[dict] = []

    async def send_group_msg(self, **kwargs: Any) -> dict:
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._raises) and self._raises[index] is not None:
            raise self._raises[index]  # type: ignore[misc]
        mid = self._message_ids[index] if index < len(self._message_ids) else 999
        return {"message_id": mid} if mid is not None else {}


def _context(**overrides: Any) -> dict:
    ctx: dict[str, Any] = {
        "scope_key": "group:100",
        "session_factory": object(),
        "correlation_id": "CID",
        "tool_call_event_id": "E_TOOL_CALL",
    }
    ctx.update(overrides)
    return ctx


class SendMessagesToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot_registry.clear()

    def tearDown(self) -> None:
        bot_registry.clear()

    async def _run(self, arguments: dict, *, context: dict | None = None):
        return await SendMessagesTool().run(
            arguments, **(context or _context())
        )

    # ── 主路径：普通工具，不读任何前置状态 ──

    async def test_send_without_any_completed_event_still_executes(self) -> None:
        """§0.5 软约束：运行时不因缺少完成事件拒绝调用；工具也不查询
        前置状态——这里没有打任何前置补丁，调用照常成功。"""
        bot = _StubBot()
        bot_registry.register(bot)
        outcome = await self._run(
            {
                "messages": [
                    _CHAT,
                    _CHAT,
                ]
            }
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["status"], "sent")
        self.assertEqual(outcome.result["message_ids"], [111, 222])
        self.assertEqual(len(bot.calls), 2)
        self.assertEqual(bot.calls[0]["group_id"], 100)
        # receipts 随 result 落 terminal，供投影渲染 <tool>send_messages 行块。
        receipts = outcome.result["sent_messages"]
        self.assertEqual(
            [item["status"] for item in receipts], ["sent", "sent"]
        )
        self.assertEqual(receipts[0]["self_id"], "10001")
        self.assertEqual(receipts[0]["receipt"]["action"], "send_group_msg")
        self.assertEqual(receipts[0]["receipt"]["status"], "ok")
        self.assertEqual(receipts[0]["receipt"]["retcode"], 0)

    async def test_meme_bubble_is_loaded_and_sent_as_image(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        fake_meme = SimpleNamespace(file_hash=HASH_A)
        with (
            patch(
                "qqbot.services.agent_loop.meme_store.get_meme",
                new=AsyncMock(return_value=fake_meme),
            ),
            patch(
                "qqbot.services.agent_loop.tools._meme_common."
                "media_path_for_hash",
                return_value=SimpleNamespace(read_bytes=lambda: b"imgbytes"),
            ),
        ):
            outcome = await self._run(
                {"messages": [{"meme": HASH_A}]}
            )
        self.assertTrue(outcome.ok)
        sent = bot.calls[0]["message"]
        self.assertEqual(sent[0]["type"], "image")
        self.assertTrue(sent[0]["data"]["file"].startswith("base64://"))
        # 落进 terminal 的回执必须脱敏，不携带 base64 正文。
        self.assertNotIn("base64://", str(outcome.result))
        self.assertEqual(
            outcome.result["sent_messages"][0]["image_hash"], HASH_A
        )

    async def test_two_distinct_commands_both_execute(self) -> None:
        """两条不同的发送命令都会执行——运行时不合并、不去重；约束模型的
        只有提示词。"""
        bot = _StubBot()
        bot_registry.register(bot)
        for event_id in ("E_CALL_A", "E_CALL_B"):
            outcome = await self._run(
                {"messages": [_CHAT]},
                context=_context(tool_call_event_id=event_id),
            )
            self.assertTrue(outcome.ok)
        self.assertEqual(len(bot.calls), 2)

    # ── 无副作用的失败：静态校验与 preflight ──

    async def test_static_invalid_never_touches_onebot(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        cases = [
            {"messages": []},
            {"messages": [{"kind": "verbatim"}]},
            {"messages": [_CHAT], "tone": "x"},
            {"messages": [{"meme": "short"}]},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                outcome = await self._run(arguments)
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(bot.calls, [])

    async def test_meme_gone_fails_before_sending(self) -> None:
        bot = _StubBot()
        bot_registry.register(bot)
        with patch(
            "qqbot.services.agent_loop.meme_store.get_meme",
            new=AsyncMock(return_value=None),
        ):
            outcome = await self._run(
                {
                    "messages": [
                        _CHAT,
                        {"meme": HASH_A},
                    ]
                }
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")
        self.assertEqual(outcome.extra["reason_code"], "meme_not_saved")
        self.assertEqual(bot.calls, [])

    async def test_no_bot_available(self) -> None:
        outcome = await self._run(
            {"messages": [_CHAT]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "no_bot_available")

    # ── 结果语义：status 与终态配对，receipts 平铺进 payload ──

    async def test_partial_is_failure_with_full_receipts(self) -> None:
        """§4.3：部分成功 → failure("upstream_action_failed") + status=
        "partial" + 完整逐条 receipts——投影据此把已 sent 气泡渲染为既成
        事实。"""
        bot = _StubBot(raises=[None, _FakeActionFailed(1404, "群不存在")])
        bot_registry.register(bot)
        outcome = await self._run(
            {
                "messages": [
                    _CHAT,
                    _CHAT,
                ]
            }
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["status"], "partial")
        self.assertEqual(outcome.extra["message_ids"], [111])
        receipts = outcome.extra["sent_messages"]
        self.assertEqual(
            [item["status"] for item in receipts], ["sent", "failed"]
        )

    async def test_all_failed_is_failure_with_status_failed(self) -> None:
        bot = _StubBot(raises=[_FakeActionFailed(1404, "群不存在")])
        bot_registry.register(bot)
        outcome = await self._run(
            {"messages": [_CHAT]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "upstream_action_failed")
        self.assertEqual(outcome.extra["status"], "failed")
        self.assertIn("群不存在", outcome.error_message)

    async def test_transport_exception_is_uncertain(self) -> None:
        """网关识别到 OneBot 传输中断：该气泡可能已发出，整体收敛
        uncertain——终态失败，提示词禁止"保险再发一遍"。"""
        bot = _StubBot(raises=[NetworkError("socket closed")])
        bot_registry.register(bot)
        outcome = await self._run(
            {"messages": [_CHAT]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra["status"], "uncertain")
        self.assertEqual(
            outcome.extra["sent_messages"][0]["status"], "uncertain"
        )
        self.assertEqual(
            outcome.extra["sent_messages"][0]["error"]["kind"],
            "network_error",
        )

    async def test_missing_message_id_is_uncertain_not_success(self) -> None:
        bot = _StubBot(message_ids=[None])
        bot_registry.register(bot)
        outcome = await self._run(
            {"messages": [_CHAT]}
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.extra["status"], "uncertain")

    # ── scope 边界 ──

    async def test_group_only_scope(self) -> None:
        """私聊没有 AgentLoop、system 没有聊天目标：allowed_scopes 只有
        group，不照抄旧 send_message 的 ("group", "private")。"""
        self.assertEqual(SendMessagesTool.allowed_scopes, ("group",))
        bot_registry.register(_StubBot())
        for scope_key in ("system", "private:555"):
            with self.subTest(scope_key=scope_key):
                outcome = await self._run(
                    {"messages": [_CHAT]},
                    context=_context(scope_key=scope_key),
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(
                    outcome.error_kind, "tool_unavailable_in_scope"
                )


class SendMessagesMetadataTests(unittest.TestCase):
    def test_name_and_schema(self) -> None:
        self.assertEqual(SendMessagesTool.name, "send_messages")
        schema = SendMessagesTool.arguments_schema
        self.assertEqual(schema["required"], ["messages"])
        self.assertFalse(schema["additionalProperties"])
        # 只有 messages 一个业务参数：没有 target / token / 完成事件 ID。
        self.assertEqual(sorted(schema["properties"]), ["messages"])

    def test_usage_doc_records_uncertain_and_partial_semantics(self) -> None:
        """发言链路设计 §5：uncertain 表示可能已送达，新调用可能造成重复；partial 按
        逐条 receipt 表示既成发送事实；调用行自己的回执就是发言记录（不派生
        <my-reply>）。完成事件不是发送参数，文档不使用 token 式措辞。"""
        doc = SendMessagesTool.usage_prompt
        self.assertIn("可能已经送达", doc)
        self.assertIn("再次调用会产生新的独立发送命令", doc)
        self.assertIn("逐气泡回执", doc)
        self.assertRegex(doc, r"不会\s*另外生成")
        for forbidden in ("授权", "兑换", "消费", "领取"):
            self.assertNotIn(forbidden, doc)

    def test_usage_doc_does_not_advertise_the_missing_gate(self) -> None:
        """2026-08-01：运行时确实不检查完成事件（§0.5 有意的软约束，不得补
        授权门闩），但**用法文档不再主动向模型交底这一点**。

        "正常在完成事件之后发言"是提示词纪律；一份工具用法文档没有义务告诉
        模型某条纪律缺少强制力——那句话唯一的作用就是邀请它绕过流程。实现
        事实仍写在 send_messages.py 的 docstring 里，供维护者查阅。
        """
        doc = SendMessagesTool.usage_prompt
        for leak in ("运行时允许独立调用", "不会检查", "不校验"):
            self.assertNotIn(leak, doc)

    def test_usage_doc_allows_loose_meme_association(self) -> None:
        """表情包是与文字平级的随手表达，不以精准语义匹配为发送门槛。"""
        doc = SendMessagesTool.usage_prompt
        self.assertIn("不要求与当前话题精确对应", doc)
        self.assertIn("弱关联", doc)
        self.assertIn("轻微无厘头", doc)
        self.assertIn("无须增加文字解释", doc)

    def test_description_no_longer_names_a_two_step_flow(self) -> None:
        """2026-08-17 删除 reply/ReplyTask：description 不再描述发言前置流程。

        "想好的话不会当拍就出去"现在由提案-裁决流水线在结构上保证（写下调用
        的那一拍只落库，要等下一拍 execute_program 指名），不是这个工具的
        参数面，也不该由它的介绍来交代。
        """
        desc = SendMessagesTool.description
        self.assertNotIn("reply", desc)
        self.assertNotIn("reply-task-completed", desc)
        self.assertIn("逐气泡回执", desc)
        self.assertIn("可能已经送达", desc)
        self.assertNotIn("保存分析", desc)

    def test_bubble_cap_lives_in_schema_and_usage_doc_only(self) -> None:
        """条数上限只有一处真相（outbound_messages），schema 直接引用它；
        具体数字只在工具介绍（usage doc）里写明，description 与其它提示词
        层一律只说"一条或多条"（2026-07-31 放宽到 10、meme 不再限量；
        2026-08-02 由"多条"改口，光秃秃的"多条"读起来像下限是两条）。"""
        schema = SendMessagesTool.arguments_schema
        self.assertEqual(
            schema["properties"]["messages"]["maxItems"], MAX_OUTBOUND_MESSAGES
        )
        self.assertIn(
            f"1–{MAX_OUTBOUND_MESSAGES} 个有序气泡",
            SendMessagesTool.usage_prompt,
        )
        # 单条也是完整的一次发送：description / schema 都不得只说"多条"。
        self.assertIn("一条或多条", SendMessagesTool.description)
        self.assertIn(
            "一条或多条",
            schema["properties"]["messages"]["description"],
        )
        self.assertIn("一条或多条", SendMessagesTool.usage_prompt)
        self.assertNotIn("1-4", SendMessagesTool.description)
        self.assertNotIn("at most one", SendMessagesTool.description)

    def test_bubble_schema_presents_text_and_meme_as_peer_branches(self) -> None:
        """2026-08-01 起两种气泡是 oneOf 下的平级兄弟（此前 `messages.items`
        是 `{"type": "object"}`，表情包在结构上等于不存在，而提示词层却把它当
        惯用表达）。2026-08-14 去协议化后判别键从 `kind` 换成"有没有 meme"，
        平级这条不变：各自带完整 additionalProperties，没有主次没有嵌套。
        """
        items = SendMessagesTool.arguments_schema["properties"]["messages"][
            "items"
        ]
        branches = items["oneOf"]
        self.assertEqual(len(branches), 2)
        chat, meme = branches
        self.assertEqual(
            sorted(chat["properties"]), ["at", "face", "reply", "text"]
        )
        self.assertEqual(sorted(meme["properties"]), ["meme"])
        self.assertEqual(meme["required"], ["meme"])
        for branch in branches:
            self.assertFalse(branch["additionalProperties"])
        # chat 支没有 required：只 @ 人或只发系统表情时 text 可以省略。
        self.assertNotIn("required", chat)

    def test_model_facing_surface_has_no_onebot_segment_vocabulary(self) -> None:
        """2026-08-14 去协议化的实质断言。

        `send_messages` 是全部 18 个程序函数里唯一把上游协议摊给模型的那个：
        气泡曾是 `{"kind":"chat","content":[{"type":"text","data":{...}}]}`，
        `data` 包装、`type` 判别、reply 必须 content[0] 三样都是 OneBot 11 的
        规则，与"说一句话"无关，却要模型每次发言复述一遍。段数组现在只在
        `outbound_messages.build_chat_content` 里构造。

        这条钉的是**暴露面**，不是实现：schema / description / 用法文档三处
        合起来不得再出现段形状词汇。运行时仍无损接住旧形状（见
        `LegacySegmentShapeTests`），但那是迁移兼容，不重新教给模型。
        """
        surface = "\n".join(
            [
                json.dumps(SendMessagesTool.arguments_schema, ensure_ascii=False),
                SendMessagesTool.description,
                SendMessagesTool.usage_prompt,
            ]
        )
        for token in ('"data"', '"content"', "消息段", "段数组", "OneBot V11"):
            with self.subTest(token=token):
                self.assertNotIn(token, surface)

    def test_bubble_schema_shape_matches_validate_messages(self) -> None:
        """schema 是纯文档（tool_registry 模块头），真正的校验是
        validate_messages——两边形状必须逐字对齐，否则模型照 schema 写出的
        气泡会被校验拒绝，而它看不到 schema 之外的真相。

        每支造一个最小气泡送进真校验（必须通过），再多塞一个键（必须被拒）
        ——证明 additionalProperties:False 确有 extras 检查兜底，不是一句
        装饰性的文档。
        """
        for bubble, kind in ((_CHAT, "chat"), ({"meme": HASH_A}, "meme")):
            with self.subTest(bubble=bubble):
                normalized, fail = validate_messages([bubble])
                self.assertIsNone(fail)
                self.assertEqual(normalized[0]["kind"], kind)
                _, rejected = validate_messages([{**bubble, "extra": 1}])
                self.assertEqual(
                    getattr(rejected, "error_kind", None), "invalid_arguments"
                )

    def test_schema_optional_keys_all_reach_the_wire(self) -> None:
        """chat 支声明的四个键必须条条落到 OneBot 段上，且顺序固定
        reply → at → text → face（模型不再能控制段序，所以这个顺序是契约）。"""
        content = build_chat_content(
            validate_messages(
                [{"text": "hi", "reply": 88, "at": [10001, "all"], "face": 178}]
            )[0][0]
        )
        self.assertEqual(
            [(seg["type"], seg["data"]) for seg in content],
            [
                ("reply", {"id": "88"}),
                ("at", {"qq": "10001"}),
                ("at", {"qq": "all"}),
                ("text", {"text": "hi"}),
                ("face", {"id": "178"}),
            ],
        )
        self.assertEqual(
            {seg["type"] for seg in content} | {"text"}, set(_ALLOWED_SEGMENT_TYPES)
        )


class LegacySegmentShapeTests(unittest.TestCase):
    """旧 OneBot 形状的迁移兼容（2026-08-14）。

    模型带着旧习惯写出 `kind`/`content` 时无损转成新形状——一次形状迁移不该
    表现为线上发不出话。与 `normalize_segment` 同性质：接住漂移，不写进 usage
    文档，也不是可依赖的第二套输入契约。
    """

    def test_legacy_bubbles_are_converted_losslessly(self) -> None:
        normalized, fail = validate_messages(
            [
                {
                    "kind": "chat",
                    "content": [
                        {"type": "reply", "data": {"id": "M1"}},
                        {"type": "at", "data": {"qq": "10001"}},
                        {"type": "text", "data": {"text": "hi"}},
                        {"type": "face", "data": {"id": "178"}},
                    ],
                },
                {"kind": "meme", "image_hash": HASH_A},
            ]
        )
        self.assertIsNone(fail)
        self.assertEqual(
            normalized[0],
            {
                "kind": "chat",
                "text": "hi",
                "reply": "M1",
                "at": ["10001"],
                "face": ["178"],
            },
        )
        self.assertEqual(
            normalized[1], {"kind": "meme", "image_hash": HASH_A}
        )

    def test_legacy_segment_rules_still_reject_bad_shapes(self) -> None:
        for messages, reason in (
            (
                [{"kind": "chat", "content": [{"type": "image", "data": {}}]}],
                "unsupported_segment_type",
            ),
            ([{"kind": "verbatim", "content": []}], "bad_message_kind"),
        ):
            with self.subTest(reason=reason):
                _, fail = validate_messages(messages)
                assert fail is not None
                self.assertEqual(fail.extra["reason_code"], reason)


if __name__ == "__main__":
    unittest.main()
