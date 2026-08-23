"""Contract tests for the v2 EventIngest pipeline.

Static + fake-driven. Does NOT require nonebot, asyncpg, or a live DB.

Contract sources:
- 开发文档/v2.0/EventIngest契约.md
- 开发文档/v2.0/事件系统设计.md
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from qqbot.services.event_ingest import (
    EventIngest,
    MapperRegistry,
    finalize,
    idempotency,
)
from qqbot.services.event_ingest.mappers import (
    GroupMessageMapper,
    GroupRecallMapper,
    build_default_registry,
)
from qqbot.services.event_ingest.system_event import PartialSystemEvent


def _make_message_event(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        post_type="message",
        message_type="group",
        sub_type="normal",
        time=1716700000,
        self_id=10000,
        message_id=12345,
        group_id=999,
        user_id=222,
        raw_message="hello",
        message=[SimpleNamespace(type="text", data={"text": "hello"})],
        sender=SimpleNamespace(
            user_id=222, nickname="alice", card="A", role="member"
        ),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_recall_event(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        post_type="notice",
        notice_type="group_recall",
        time=1716700050,
        self_id=10000,
        message_id=12345,
        group_id=999,
        user_id=222,
        operator_id=222,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class GroupMessageMapperContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = GroupMessageMapper()

    def test_can_map_only_group_messages(self) -> None:
        self.assertTrue(self.mapper.can_map(_make_message_event()))
        self.assertFalse(
            self.mapper.can_map(_make_message_event(message_type="private"))
        )
        self.assertFalse(self.mapper.can_map(_make_message_event(post_type="notice")))

    def test_produces_external_message_group_normal(self) -> None:
        partial = self.mapper.map(_make_message_event())
        self.assertEqual(partial.origin, "external")
        self.assertEqual(partial.type, "external.message.group.normal")
        self.assertEqual(partial.scope, "group")
        self.assertEqual(partial.group_id, 999)
        self.assertEqual(partial.user_id, 222)
        self.assertEqual(partial.visibility, "agent_visible")

    def test_payload_includes_required_fields(self) -> None:
        partial = self.mapper.map(_make_message_event())
        for key in (
            "onebot_message_id",
            "raw_message",
            "sender",
            "segments",
            "message_sub_type",
        ):
            self.assertIn(key, partial.payload)
        self.assertNotIn("msg_hash", partial.payload)
        self.assertEqual(partial.payload["onebot_message_id"], "12345")
        self.assertEqual(partial.payload["sender"]["nickname"], "alice")
        self.assertEqual(
            partial.payload["segments"], [{"type": "text", "data": {"text": "hello"}}]
        )

    def test_segments_prefer_original_message_over_adapter_stripped(self) -> None:
        # nonebot v11 适配器分发前会原地改写 event.message（_check_reply 删
        # reply 段与紧随的 @bot 段、_check_at_me 剥首/尾 @bot 段），
        # original_message 才是 napcat 真实上报的完整段数组——契约
        # EventIngest契约.md §3.1。
        original = [
            SimpleNamespace(type="reply", data={"id": "8888"}),
            SimpleNamespace(type="at", data={"qq": "10000"}),
            SimpleNamespace(type="text", data={"text": " 滚"}),
        ]
        stripped = [SimpleNamespace(type="text", data={"text": "滚"})]
        partial = self.mapper.map(
            _make_message_event(message=stripped, original_message=original)
        )
        self.assertEqual(
            partial.payload["segments"],
            [
                {"type": "reply", "data": {"id": "8888"}},
                {"type": "at", "data": {"qq": "10000"}},
                {"type": "text", "data": {"text": " 滚"}},
            ],
        )

    def test_segments_fall_back_to_message_when_original_absent_or_empty(
        self,
    ) -> None:
        # 测试 fake / 非 v11 适配器没有 original_message；为空同样回退。
        expected = [{"type": "text", "data": {"text": "hello"}}]
        partial = self.mapper.map(_make_message_event())
        self.assertEqual(partial.payload["segments"], expected)
        partial = self.mapper.map(_make_message_event(original_message=[]))
        self.assertEqual(partial.payload["segments"], expected)

    def test_reply_segment_enriched_from_adapter_resolved_reply(self) -> None:
        # EventIngest契约.md §4：适配器已解析的 event.reply（分发前 get_msg
        # 的产物）固化进首个 reply 段顶层 quoted 键——被引消息滚出投影窗口后
        # from_*/excerpt 不再丢失。子键"有才落键"。
        original = [
            SimpleNamespace(type="reply", data={"id": "8888"}),
            SimpleNamespace(type="text", data={"text": "谢啦"}),
        ]
        reply = SimpleNamespace(
            message_id=8888,
            sender=SimpleNamespace(user_id=333, nickname="carol", card="C姐"),
            message=[SimpleNamespace(type="text", data={"text": "带伞~"})],
        )
        partial = self.mapper.map(
            _make_message_event(original_message=original, reply=reply)
        )
        seg = partial.payload["segments"][0]
        self.assertEqual(seg["type"], "reply")
        quoted = seg["quoted"]
        self.assertEqual(quoted["sender_qq"], "333")
        # card 优先于 nickname（与投影层作者名取值同序）。
        self.assertEqual(quoted["sender_name"], "C姐")
        self.assertIs(quoted["from_self"], False)
        self.assertEqual(
            quoted["segments"], [{"type": "text", "data": {"text": "带伞~"}}]
        )
        # 富化写在 segment 顶层，不污染 data。
        self.assertEqual(seg["data"], {"id": "8888"})

    def test_reply_segment_quoting_bot_marks_from_self(self) -> None:
        # 被引消息是 bot 自己发的（sender.user_id == self_id）→ from_self=True，
        # 投影层据此渲染 from_self="true"（"别人在回我"的服务端铁证）。
        original = [SimpleNamespace(type="reply", data={"id": "7777"})]
        reply = SimpleNamespace(
            message_id=7777,
            sender=SimpleNamespace(user_id=10000, nickname="小奏", card=None),
            message=[SimpleNamespace(type="text", data={"text": "带伞~"})],
        )
        partial = self.mapper.map(
            _make_message_event(original_message=original, reply=reply)
        )
        quoted = partial.payload["segments"][0]["quoted"]
        self.assertIs(quoted["from_self"], True)
        self.assertEqual(quoted["sender_name"], "小奏")

    def test_reply_segment_without_adapter_reply_stays_bare(self) -> None:
        # event.reply 缺失（get_msg 失败/被引已撤回/测试 fake）→ 不落 quoted
        # 键，投影退回窗口内索引兜底（行为与富化前逐字节一致）。
        original = [
            SimpleNamespace(type="reply", data={"id": "8888"}),
            SimpleNamespace(type="text", data={"text": "谢啦"}),
        ]
        partial = self.mapper.map(_make_message_event(original_message=original))
        self.assertNotIn("quoted", partial.payload["segments"][0])

    def test_adapter_reply_without_reply_segment_is_ignored(self) -> None:
        # 病态输入：event.reply 在但段里没有 reply 段（非 v11 适配器改写
        # 差异）→ 富化静默跳过，不 raise 不污染其他段。
        reply = SimpleNamespace(
            message_id=8888,
            sender=SimpleNamespace(user_id=333, nickname="carol"),
            message=[],
        )
        partial = self.mapper.map(_make_message_event(reply=reply))
        for seg in partial.payload["segments"]:
            self.assertNotIn("quoted", seg)

    def test_anonymous_subtype_routes_to_anonymous_type(self) -> None:
        partial = self.mapper.map(_make_message_event(sub_type="anonymous"))
        self.assertEqual(partial.type, "external.message.group.anonymous")

    def test_notice_subtype_routes_to_notice_type(self) -> None:
        partial = self.mapper.map(_make_message_event(sub_type="notice"))
        self.assertEqual(partial.type, "external.message.group.notice")

    def test_idempotency_key_format(self) -> None:
        partial = self.mapper.map(_make_message_event())
        self.assertEqual(partial.idempotency_key, "10000:msg:12345")

    def test_optional_metadata_stored_when_present(self) -> None:
        # "有才上报"的元数据（napcat 扩展 real_seq/group_name、OneBot 标准
        # anonymous、sender 的 title/level/sex/age/area）有值才落键。
        event = _make_message_event(
            real_seq="7788",
            group_name="测试群",
            anonymous=SimpleNamespace(
                id=80000001, name="匿名の马甲", flag="F_SECRET"
            ),
            sender=SimpleNamespace(
                user_id=222,
                nickname="alice",
                card="A",
                role="member",
                title="大佬",
                level="100",
            ),
        )
        payload = self.mapper.map(event).payload
        self.assertEqual(payload["real_seq"], "7788")
        self.assertEqual(payload["group_name"], "测试群")
        self.assertEqual(payload["anonymous"]["id"], 80000001)
        self.assertEqual(payload["anonymous"]["name"], "匿名の马甲")
        # flag 是 set_group_anonymous_ban 凭证：随事件入库（渲染层不透出）
        self.assertEqual(payload["anonymous"]["flag"], "F_SECRET")
        self.assertEqual(payload["sender"]["title"], "大佬")
        self.assertEqual(payload["sender"]["level"], "100")

    def test_optional_metadata_absent_when_missing(self) -> None:
        # napcat 默认形态（无匿名/无头衔/无扩展序号）：键整个不出现，
        # 而不是落一堆 None。
        payload = self.mapper.map(_make_message_event()).payload
        for key in ("anonymous", "real_seq", "message_seq", "group_name"):
            self.assertNotIn(key, payload)
        for key in ("title", "level", "sex", "age", "area"):
            self.assertNotIn(key, payload["sender"])
        # 核心 4 键恒在（既有 payload 形状不变）
        for key in ("user_id", "nickname", "card", "role"):
            self.assertIn(key, payload["sender"])
        self.assertNotIn("msg_hash", payload)


class GroupRecallMapperContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = GroupRecallMapper()

    def test_can_map_only_group_recall(self) -> None:
        self.assertTrue(self.mapper.can_map(_make_recall_event()))
        self.assertFalse(
            self.mapper.can_map(_make_recall_event(notice_type="group_admin"))
        )

    def test_produces_external_notice_group_recall(self) -> None:
        partial = self.mapper.map(_make_recall_event())
        self.assertEqual(partial.type, "external.notice.group_recall")
        self.assertEqual(partial.scope, "group")
        self.assertEqual(partial.visibility, "agent_visible")
        self.assertEqual(partial.payload["operator_id"], 222)
        self.assertEqual(partial.payload["onebot_message_id"], "12345")

    def test_idempotency_key_format(self) -> None:
        partial = self.mapper.map(_make_recall_event())
        self.assertEqual(
            partial.idempotency_key, "10000:recall:12345:1716700050"
        )


class MapperRegistryTests(unittest.TestCase):
    def test_exact_mapper_wins_over_fallback(self) -> None:
        class Fallback:
            post_type = "message"
            sub_type = None

            def can_map(self, event: Any) -> bool:
                return True

            def map(self, event: Any) -> PartialSystemEvent:
                raise NotImplementedError

        registry = MapperRegistry()
        registry.register(Fallback())
        registry.register(GroupMessageMapper())
        chosen = registry.find(_make_message_event())
        self.assertIsInstance(chosen, GroupMessageMapper)

    def test_returns_none_when_no_match(self) -> None:
        registry = build_default_registry()
        unknown = SimpleNamespace(post_type="meta_event", sub_type="heartbeat")
        self.assertIsNone(registry.find(unknown))


class FinalizeContractTests(unittest.TestCase):
    def test_correlation_id_is_self_event_id(self) -> None:
        partial = PartialSystemEvent(
            origin="external",
            type="external.message.group.normal",
            scope="group",
            group_id=1,
            user_id=2,
            visibility="agent_visible",
            payload={},
            raw=None,
            idempotency_key="k",
        )
        ev = finalize(
            partial,
            occurred_at=datetime(2026, 5, 26, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            event_id="0" * 25 + "B",
        )
        self.assertEqual(ev.event_id, "0" * 25 + "B")
        self.assertEqual(ev.correlation_id, ev.event_id)
        self.assertIsNone(ev.causation_id)
        self.assertEqual(ev.idempotency_key, "k")
        self.assertEqual(ev.scope, "group")

    def test_supplied_event_id_is_kept(self) -> None:
        partial = PartialSystemEvent(
            origin="external",
            type="external.message.group.normal",
            scope="group",
            group_id=1,
            user_id=2,
            visibility="agent_visible",
            payload={},
            raw=None,
            idempotency_key="k",
        )
        stamped = "0" * 25 + "A"
        ev = finalize(
            partial,
            occurred_at=datetime(2026, 5, 26, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            event_id=stamped,
        )
        self.assertEqual(ev.event_id, stamped)
        self.assertEqual(ev.correlation_id, stamped)

    def test_internal_correlation_is_kept(self) -> None:
        partial = PartialSystemEvent(
            origin="agent",
            type="agent.decision_emitted",
            scope="group",
            group_id=1,
            user_id=None,
            visibility="agent_visible",
            payload={"program": ""},
            raw=None,
            idempotency_key=None,
            correlation_id="TICK-CORR",
            causation_id=None,
        )
        ev = finalize(
            partial,
            occurred_at=datetime(2026, 5, 26, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            event_id="0" * 25 + "C",
        )
        self.assertEqual(ev.correlation_id, "TICK-CORR")
        self.assertEqual(ev.event_id, "0" * 25 + "C")

    def test_finalize_requires_event_id(self) -> None:
        partial = PartialSystemEvent(
            origin="external",
            type="external.message.group.normal",
            scope="group",
            group_id=1,
            user_id=2,
            visibility="agent_visible",
            payload={},
            raw=None,
            idempotency_key="k",
        )
        occurred = datetime(2026, 5, 26, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        with self.assertRaises(TypeError):
            finalize(partial, occurred_at=occurred)  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            finalize(partial, occurred_at=occurred, event_id="")


class IdempotencyHelpersTests(unittest.TestCase):
    def test_for_message(self) -> None:
        self.assertEqual(idempotency.for_message(1, 2), "1:msg:2")

    def test_for_notice_with_subtype(self) -> None:
        self.assertEqual(
            idempotency.for_notice(1, "group_admin", "set", 100, 9, 8),
            "1:notice:group_admin:set:100:9:8",
        )

    def test_for_notice_without_subtype(self) -> None:
        self.assertEqual(
            idempotency.for_notice(1, "group_recall", None, 100, 9, 9),
            "1:notice:group_recall:_:100:9:9",
        )

    def test_for_recall(self) -> None:
        self.assertEqual(idempotency.for_recall(1, 7, 100), "1:recall:7:100")

    def test_for_request(self) -> None:
        self.assertEqual(
            idempotency.for_request(1, "friend", "abc"), "1:request:friend:abc"
        )

    def test_for_unknown(self) -> None:
        self.assertEqual(
            idempotency.for_unknown(1, "notice", "profile_like", 100, 9),
            "1:unknown:notice:profile_like:100:9",
        )
        self.assertEqual(
            idempotency.for_unknown(1, None, None, 100, None),
            "1:unknown:_:_:100:_",
        )

    def test_for_ingest_failure_preserves_message_identity(self) -> None:
        event = _make_message_event()
        self.assertEqual(
            idempotency.for_ingest_failure(event),
            "10000:msg:12345",
        )


class IngestPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_notifier_runs_only_after_commit(self) -> None:
        order: list[str] = []

        class OrderedSession:
            async def execute(self, stmt: Any) -> Any:
                order.append("execute")
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                order.append("commit")

            async def __aenter__(self) -> "OrderedSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        async def notify(event: Any) -> None:
            _ = event
            order.append("notify")

        ingest = EventIngest(
            build_default_registry(),
            session_factory=OrderedSession,
            committed_notifier=notify,
        )

        result = await ingest.ingest(_make_message_event())

        self.assertEqual(result.status, "inserted")
        # raw 插入之后才登记；通知仍在终态 commit 之后。
        self.assertEqual(order[-1], "notify")
        self.assertLess(order.index("commit"), order.index("notify"))

    async def test_commit_failure_never_notifies(self) -> None:
        class FailingSession:
            async def execute(self, stmt: Any) -> Any:
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                raise RuntimeError("database unavailable")

            async def __aenter__(self) -> "FailingSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        notifier = _FakeCommittedNotifier()
        ingest = EventIngest(
            build_default_registry(),
            session_factory=FailingSession,
            committed_notifier=notifier,
        )

        result = await ingest.ingest(_make_message_event())

        self.assertEqual(result.status, "error")
        self.assertEqual(notifier.events, [])

    async def test_unknown_event_persists_terminal_failure(self) -> None:
        # 无 mapper 也是一种前置处理失败：只落一条安全的终态内部事件，
        # 原始报文进 raw 审计列，提交后才通知 AgentLoop 侧。
        recorder = _FakeSessionRecorder(rowcount=1)
        notifier = _FakeCommittedNotifier()
        ingest = EventIngest(
            build_default_registry(),
            session_factory=recorder.factory,
            committed_notifier=notifier,
        )
        result = await ingest.ingest(_UnknownEvent())

        self.assertEqual(result.status, "processing_failed")
        self.assertEqual(result.reason, "no_mapper")
        ev = result.event
        self.assertIsNotNone(ev)
        self.assertEqual(ev.type, "runtime.event_ingest_failed")
        self.assertEqual(ev.origin, "runtime")
        self.assertEqual(ev.scope, "system")
        self.assertIsNone(ev.group_id)
        self.assertEqual(ev.visibility, "agent_visible")
        self.assertEqual(ev.payload["source_post_type"], "notice")
        self.assertEqual(ev.payload["source_sub_type"], "profile_like")
        self.assertEqual(ev.payload["failures"][0]["error_code"], "no_mapper")
        self.assertNotIn("raw", ev.payload)
        self.assertEqual(ev.raw, _UnknownEvent.DUMP)
        self.assertEqual(
            ev.idempotency_key,
            "10000:notice:notify:profile_like:1716700000:222:_",
        )
        self.assertGreaterEqual(recorder.executes, 2)
        self.assertGreaterEqual(recorder.commits, 2)
        self.assertEqual(notifier.events, [ev])

    async def test_unknown_event_duplicate_on_repush(self) -> None:
        # napcat 重推同一未知报文 → 唯一键兜住，不重复入库、不再唤醒
        recorder = _FakeSessionRecorder(rowcount=0)
        notifier = _FakeCommittedNotifier()
        ingest = EventIngest(
            build_default_registry(),
            session_factory=recorder.factory,
            committed_notifier=notifier,
        )
        result = await ingest.ingest(_UnknownEvent())
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(notifier.events, [])

    async def test_mapper_exception_becomes_group_failure_event(self) -> None:
        class BrokenMapper:
            post_type = "message"
            sub_type = "broken"

            def can_map(self, event: Any) -> bool:
                return True

            def map(self, event: Any) -> PartialSystemEvent:
                raise ValueError("bad payload")

        registry = MapperRegistry()
        registry.register(BrokenMapper())
        recorder = _FakeSessionRecorder(rowcount=1)
        notifier = _FakeCommittedNotifier()
        ingest = EventIngest(
            registry,
            session_factory=recorder.factory,
            committed_notifier=notifier,
        )

        result = await ingest.ingest(_make_message_event())

        self.assertEqual(result.status, "processing_failed")
        self.assertEqual(result.reason, "mapper_failed")
        self.assertEqual(result.event.type, "runtime.event_ingest_failed")
        self.assertEqual(result.event.scope, "group")
        self.assertEqual(result.event.group_id, 999)
        self.assertEqual(result.event.payload["raw_message"], "hello")
        self.assertEqual(notifier.events, [result.event])

    async def test_ingest_group_message_inserts(self) -> None:
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        result = await ingest.ingest(_make_message_event())

        self.assertEqual(result.status, "inserted")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.type, "external.message.group.normal")
        self.assertEqual(result.event.scope, "group")
        self.assertEqual(result.event.idempotency_key, "10000:msg:12345")
        self.assertGreaterEqual(recorder.commits, 2)
        self.assertGreaterEqual(recorder.executes, 2)

    async def test_ingest_group_message_duplicate(self) -> None:
        recorder = _FakeSessionRecorder(rowcount=0)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        result = await ingest.ingest(_make_message_event())
        self.assertEqual(result.status, "duplicate")
        self.assertIsNotNone(result.event)

    async def test_ingest_group_recall_inserts(self) -> None:
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        result = await ingest.ingest(_make_recall_event())
        self.assertEqual(result.status, "inserted")
        self.assertEqual(result.event.type, "external.notice.group_recall")

    async def test_failure_terminal_uses_arrival_event_id(self) -> None:
        stamped = "0" * 25 + "F"
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        with patch(
            "qqbot.services.event_gateway.registry.issue_event_id",
            return_value=stamped,
        ):
            result = await ingest.ingest(_UnknownEvent())
        self.assertEqual(result.status, "processing_failed")
        self.assertEqual(result.event.event_id, stamped)
        self.assertEqual(result.event.correlation_id, stamped)

    async def test_same_window_sorts_by_gateway_time_not_vlm_finish(
        self,
    ) -> None:
        """同一聚水窗里先到的图即使 VLM 更慢，登记序仍按 occurred_at/seq。"""
        from qqbot.services.event_ingest.media import MediaProcessingResult

        image_id = "0" * 25 + "1"
        text_id = "0" * 25 + "2"
        ids = iter((image_id, text_id))

        async def fake_attach(
            payload: dict,
            describer: Any = None,
            batch_describer: Any = None,
        ) -> MediaProcessingResult:
            _ = describer, batch_describer
            segs = payload.get("segments") or []
            has_image = any(
                isinstance(seg, dict) and seg.get("type") == "image" for seg in segs
            )
            if has_image:
                await asyncio.sleep(0.05)
            return MediaProcessingResult()

        class RecordingSession:
            async def execute(self, stmt: Any) -> Any:
                _ = stmt
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "RecordingSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        import qqbot.services.event_ingest.ingest as ingest_mod

        original_attach = ingest_mod.attach_media_to_payload
        ingest_mod.attach_media_to_payload = fake_attach
        ingest = EventIngest(
            build_default_registry(),
            session_factory=RecordingSession,
            registration_window_seconds=0.1,
        )
        image_event = _make_message_event(
            message_id=1,
            raw_message="[image]",
            message=[
                SimpleNamespace(type="image", data={"url": "http://x/y.png"})
            ],
        )
        text_event = _make_message_event(
            message_id=2,
            raw_message="hello",
        )
        try:
            with patch(
                "qqbot.services.event_gateway.registry.issue_event_id",
                side_effect=lambda: next(ids),
            ):
                image_task = asyncio.create_task(ingest.ingest(image_event))
                await asyncio.sleep(0)
                text_task = asyncio.create_task(ingest.ingest(text_event))
                image_result, text_result = await asyncio.gather(
                    image_task, text_task
                )
        finally:
            ingest_mod.attach_media_to_payload = original_attach

        image_ev = image_result.event
        text_ev = text_result.event
        self.assertEqual(image_ev.event_id, image_id)
        self.assertEqual(text_ev.event_id, text_id)
        self.assertLessEqual(image_ev.occurred_at, text_ev.occurred_at)
        self.assertLess(
            (image_ev.occurred_at, image_ev.event_id),
            (text_ev.occurred_at, text_ev.event_id),
        )

    async def test_internal_channel_skips_pooling_window(self) -> None:
        """模型/工具等内部通道即时领号，不空等 1s 聚水窗。"""
        import time

        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(),
            session_factory=recorder.factory,
            registration_window_seconds=1.0,
        )
        started = time.monotonic()
        result = await ingest.ingest_channel(
            "model",
            {"ok": True, "scope": "system"},
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, "inserted")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.type, "runtime.model_responded")
        self.assertLess(elapsed, 0.5)

    async def test_planner_scene_registers_decision_or_invalid_action(self) -> None:
        from qqbot.services.agent_loop.tool_registry import (
            BaseTool,
            ToolOutcome,
            ToolRegistry,
        )

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

        registry = ToolRegistry()
        registry.register(_NotifyTool)
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(),
            session_factory=recorder.factory,
            tool_registry=registry,
        )
        ok = await ingest.ingest_channel(
            "model",
            {
                "ok": True,
                "scene": "planner",
                "program": 'notify(message="hi")',
                "scope_key": "group:1",
                "correlation_id": "CORR",
                "tick_seq": 3,
            },
        )
        self.assertEqual(ok.status, "inserted")
        self.assertEqual(ok.event.type, "agent.decision_emitted")
        self.assertEqual(ok.event.correlation_id, "CORR")
        self.assertIsNotNone(ok.prepared)
        bad = await ingest.ingest_channel(
            "model",
            {
                "ok": True,
                "scene": "planner",
                "program": "import os",
                "scope_key": "group:1",
                "correlation_id": "CORR",
                "tick_seq": 4,
            },
        )
        self.assertEqual(bad.status, "inserted")
        self.assertEqual(bad.event.type, "agent.invalid_action")
        self.assertEqual(bad.event.payload["raw_text"], "import os")

    async def test_tool_batch_skips_pool_and_keeps_preissued_id(self) -> None:
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        result = await ingest.ingest_channel(
            "tool",
            {
                "scope_key": "group:9",
                "correlation_id": "TICK",
                "events": [
                    {
                        "event_type": "agent.tool_called",
                        "event_id": "PREISSUEDTOOL01" + "0" * 10,
                        "causation_id": "DECISION",
                        "payload": {"tool_name": "wait"},
                    }
                ],
            },
        )
        self.assertEqual(result.status, "inserted")
        self.assertEqual(result.event.type, "agent.tool_called")
        self.assertEqual(result.event.event_id, "PREISSUEDTOOL01" + "0" * 10)
        self.assertEqual(result.event.correlation_id, "TICK")
        self.assertEqual(result.event.causation_id, "DECISION")

    async def test_registrar_preserves_idempotency_key(self) -> None:
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        result = await ingest.ingest(_make_message_event())
        self.assertEqual(result.status, "inserted")
        self.assertEqual(result.event.idempotency_key, "10000:msg:12345")
        self.assertNotIn("msg_hash", result.event.payload)

    async def test_file_hash_survives_registration(self) -> None:
        from qqbot.services.event_ingest.media import MediaProcessingResult

        file_hash = "a" * 64

        async def fake_attach(
            payload: dict,
            describer: Any = None,
            batch_describer: Any = None,
        ) -> MediaProcessingResult:
            _ = describer, batch_describer
            for seg in payload.get("segments") or []:
                if isinstance(seg, dict) and seg.get("type") == "image":
                    seg["file_hash"] = file_hash
            return MediaProcessingResult()

        import qqbot.services.event_ingest.ingest as ingest_mod

        original_attach = ingest_mod.attach_media_to_payload
        ingest_mod.attach_media_to_payload = fake_attach
        recorder = _FakeSessionRecorder(rowcount=1)
        ingest = EventIngest(
            build_default_registry(), session_factory=recorder.factory
        )
        try:
            result = await ingest.ingest(
                _make_message_event(
                    message=[
                        SimpleNamespace(
                            type="image", data={"url": "http://x/y.png"}
                        )
                    ]
                )
            )
        finally:
            ingest_mod.attach_media_to_payload = original_attach
        self.assertEqual(result.status, "inserted")
        self.assertEqual(
            result.event.payload["segments"][0]["file_hash"], file_hash
        )


class _FakeSession:
    def __init__(self, rowcount: int, recorder: "_FakeSessionRecorder") -> None:
        self._rowcount = rowcount
        self._recorder = recorder

    async def execute(self, stmt: Any) -> Any:
        self._recorder.executes += 1
        return SimpleNamespace(rowcount=self._rowcount)

    async def commit(self) -> None:
        self._recorder.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeSessionRecorder:
    def __init__(self, rowcount: int) -> None:
        self._rowcount = rowcount
        self.executes = 0
        self.commits = 0

    def factory(self) -> _FakeSession:
        return _FakeSession(self._rowcount, self)


class _FakeCommittedNotifier:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)


class _UnknownEvent:
    """没有 mapper 的 napcat 报文 fake（如 notify.profile_like）。

    带 dict() 以便 dump_event 走 pydantic v1 路径，验证原报文只进 raw 审计列。
    """

    post_type = "notice"
    notice_type = "notify"
    sub_type = "profile_like"
    user_id = 222
    self_id = 10000
    time = 1716700000

    DUMP = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "profile_like",
        "user_id": 222,
        "self_id": 10000,
        "time": 1716700000,
    }

    def dict(self) -> dict:
        return dict(self.DUMP)


if __name__ == "__main__":
    unittest.main()
