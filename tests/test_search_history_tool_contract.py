"""Contract tests for SearchHistoryTool.

Covers:
- arguments 参数解析（anchor_event_id / start_time / end_time / query / limit）
- scope_key 缺失 / 非法 → 返回 ToolOutcome.failure(invalid_arguments)（工具永不 raise）
- limit 兜底（默认 / 上限）
- 返回结构复用 Projector 渲染器，items 字段同构
- task_id 锚点路径已删除（2026-08-21，渲染格式表 §一②）：它读的是
  agent.task_created.payload.triggered_by_event_id，而任务坍缩为单栏便签后
  没有任务、没有 task_id、也没有起因事件。

不打真实 DB：直接 stub _query 方法，验证调用面。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.dialects import postgresql

from qqbot.services.agent_loop.projection import _EventSnapshot
from qqbot.services.agent_loop.tools.search_history import (
    SearchHistoryTool,
    _build_query_stmt,
)


def _ok(tool: SearchHistoryTool, args: dict, **ctx: Any) -> dict:
    """run() 现返回 ToolOutcome；happy-path 取 .result 复用既有断言。"""
    outcome = asyncio.run(tool.run(args, **ctx))
    assert outcome.ok, outcome
    return outcome.result


SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE_TIME = datetime(2026, 5, 26, 14, 30, 0, tzinfo=SHANGHAI)


class _StubRow:
    """模拟 AgentEvent ORM row 的最小子集，supply _snapshot_from_row 用到的字段。"""

    def __init__(self, snap: _EventSnapshot) -> None:
        self.event_id = snap.event_id
        self.occurred_at = snap.occurred_at
        self.origin = snap.origin
        self.type = snap.type
        self.scope = snap.scope
        self.group_id = snap.group_id
        self.user_id = snap.user_id
        self.visibility = snap.visibility
        self.correlation_id = snap.correlation_id
        self.causation_id = snap.causation_id
        self.payload = snap.payload


def _msg(text: str, *, seconds_offset: int = 0, event_id: str = "MSG") -> _StubRow:
    snap = _EventSnapshot(
        event_id=event_id,
        occurred_at=BASE_TIME + timedelta(seconds=seconds_offset),
        origin="external",
        type="external.message.group.normal",
        scope="group",
        group_id=999,
        user_id=222,
        visibility="agent_visible",
        correlation_id=None,
        causation_id=None,
        payload={
            "raw_message": text,
            "segments": [{"type": "text", "data": {"text": text}}],
            "sender": {"nickname": "alice", "user_id": 222},
        },
    )
    return _StubRow(snap)


class SearchHistoryToolContractTest(unittest.TestCase):
    def _make_tool(
        self,
        *,
        query_returns: list[_StubRow] | None = None,
    ) -> SearchHistoryTool:
        """构造工具，替换 _query 为 stub。"""
        # 无构造依赖；session_factory 现从 run() context 进，且这些用例都 stub
        # 掉了 _query，session_factory 不会被走到。
        tool = SearchHistoryTool()
        self.captured_query_kwargs: dict[str, Any] = {}

        async def _stub_query(**kwargs: Any) -> list[_StubRow]:
            self.captured_query_kwargs = kwargs
            return query_returns or []

        tool._query = _stub_query  # type: ignore[method-assign]
        return tool

    def test_scope_key_missing_returns_invalid_arguments(self) -> None:
        tool = self._make_tool()
        outcome = asyncio.run(tool.run({"limit": 5}))  # 没传 scope_key
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    def test_invalid_scope_key_returns_invalid_arguments(self) -> None:
        tool = self._make_tool()
        outcome = asyncio.run(tool.run({}, scope_key="bogus:1"))
        self.assertEqual(outcome.error_kind, "invalid_arguments")

    def test_happy_path_returns_rendered_items(self) -> None:
        rows = [
            _msg("hello world", seconds_offset=i, event_id=f"E{i:02d}")
            for i in range(3)
        ]
        tool = self._make_tool(query_returns=rows)
        result = _ok(tool, {"limit": 10}, scope_key="group:999")
        self.assertEqual(result["matched"], 3)
        self.assertEqual(len(result["items"]), 3)
        # 渲染走 Projector：必带 sender_name/sender_id（独立属性）+ text
        for item in result["items"]:
            self.assertEqual(item["kind"], "message")
            self.assertIn('sender_name="alice"', item["render"])
            self.assertIn('sender_qq="222"', item["render"])
            self.assertIn("hello world", item["render"])

    def test_limit_clamped_to_max(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({"limit": 9999}, scope_key="group:999"))
        # _query 收到的 limit 不超过工具的 _MAX_LIMIT (50)
        self.assertEqual(self.captured_query_kwargs["limit"], 50)

    def test_limit_defaults_when_invalid(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({"limit": "not-a-number"}, scope_key="group:999"))
        self.assertEqual(self.captured_query_kwargs["limit"], 20)  # _DEFAULT_LIMIT

    def test_limit_min_clamped_to_one(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({"limit": 0}, scope_key="group:999"))
        self.assertEqual(self.captured_query_kwargs["limit"], 1)

    def test_anchor_event_id_passed_through(self) -> None:
        tool = self._make_tool()
        asyncio.run(
            tool.run(
                {"anchor_event_id": "ANCHOR123"},
                scope_key="group:999",
            )
        )
        self.assertEqual(self.captured_query_kwargs["anchor_event_id"], "ANCHOR123")

    def test_task_id_is_not_an_argument_anymore(self) -> None:
        """2026-08-21：锚点只剩 anchor_event_id 一条路（§一②）。

        schema 里没有 task_id，工具也不再有 _resolve_task_anchor。传进来的
        task_id 是个陌生字段，不影响锚点——它既不会被解析成锚，也不会伪装成
        "解析失败"混进 warnings 里让模型以为自己写对了参数。
        """
        self.assertNotIn(
            "task_id", SearchHistoryTool.arguments_schema["properties"]
        )
        self.assertFalse(hasattr(SearchHistoryTool, "_resolve_task_anchor"))
        tool = self._make_tool()
        result = _ok(tool, {"task_id": "T1"}, scope_key="group:999")
        self.assertIsNone(self.captured_query_kwargs["anchor_event_id"])
        self.assertEqual(result["warnings"], [])

    def test_time_window_parsed_to_datetimes(self) -> None:
        tool = self._make_tool()
        asyncio.run(
            tool.run(
                {
                    "start_time": "2026-05-25T00:00:00+08:00",
                    "end_time": "2026-05-26T00:00:00+08:00",
                },
                scope_key="group:999",
            )
        )
        start = self.captured_query_kwargs["start_dt"]
        end = self.captured_query_kwargs["end_dt"]
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertLess(start, end)

    def test_unparseable_time_yields_warning(self) -> None:
        tool = self._make_tool()
        result = _ok(tool, {"start_time": "not-a-time"}, scope_key="group:999")
        self.assertIsNone(self.captured_query_kwargs["start_dt"])
        self.assertTrue(any("not-a-time" in w for w in result["warnings"]))

    def test_query_passed_through(self) -> None:
        tool = self._make_tool()
        asyncio.run(
            tool.run({"query": "5432 error"}, scope_key="group:999")
        )
        self.assertEqual(self.captured_query_kwargs["query"], "5432 error")

    def test_blank_arguments_yield_empty_strings_as_none(self) -> None:
        tool = self._make_tool()
        asyncio.run(
            tool.run(
                {"query": "   ", "anchor_event_id": ""},
                scope_key="group:999",
            )
        )
        self.assertIsNone(self.captured_query_kwargs["query"])
        self.assertIsNone(self.captured_query_kwargs["anchor_event_id"])

    def test_scope_filter_propagates_to_query(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({}, scope_key="group:42"))
        self.assertEqual(self.captured_query_kwargs["scope"], "group")
        self.assertEqual(self.captured_query_kwargs["group_id"], 42)
        self.assertIsNone(self.captured_query_kwargs["user_id"])

    def test_private_scope_propagates_user_id_not_group_id(self) -> None:
        tool = self._make_tool()
        asyncio.run(tool.run({}, scope_key="private:555"))
        self.assertEqual(self.captured_query_kwargs["scope"], "private")
        self.assertIsNone(self.captured_query_kwargs["group_id"])
        self.assertEqual(self.captured_query_kwargs["user_id"], 555)


class SearchHistoryQueryStatementTests(unittest.TestCase):
    """直接测 _build_query_stmt 拼出来的 SQL 形状（不建真实连接，只编译成串）。

    这层专门盯住两个曾经的真实 bug：query 过滤是否真的打在 search_text
    这个建了 GIN trgm 索引的列上（而不是重新表达一遍 payload JSONB 路径，
    导致索引对不上）；private scope 是否真的按 user_id 过滤（而不是像
    2026-07-23 前那样对所有用户的私聊事件不设防）。
    """

    def _compile(self, stmt: Any) -> str:
        return str(
            stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

    def _compile_where(self, stmt: Any) -> str:
        """只编译过滤谓词，避免把 SELECT 返回列误判成 WHERE 条件。"""
        self.assertIsNotNone(stmt.whereclause)
        return self._compile(stmt.whereclause)

    def test_query_filter_hits_search_text_via_trgm_operator(self) -> None:
        stmt = _build_query_stmt(
            scope="group",
            group_id=999,
            user_id=None,
            anchor_event_id=None,
            start_dt=None,
            end_dt=None,
            query="旅游",
            limit=20,
        )
        compiled = self._compile(stmt)
        self.assertIn("search_text", compiled)
        self.assertIn("<%", compiled)
        self.assertIn("word_similarity", compiled)
        self.assertNotIn("raw_message", compiled)

    def test_no_query_orders_by_recency(self) -> None:
        stmt = _build_query_stmt(
            scope="group",
            group_id=999,
            user_id=None,
            anchor_event_id=None,
            start_dt=None,
            end_dt=None,
            query=None,
            limit=20,
        )
        compiled = self._compile(stmt)
        self.assertNotIn("word_similarity", compiled)
        self.assertIn("occurred_at", compiled)
        self.assertIn("DESC", compiled)

    def test_private_scope_filters_on_user_id_column(self) -> None:
        stmt = _build_query_stmt(
            scope="private",
            group_id=None,
            user_id=555,
            anchor_event_id=None,
            start_dt=None,
            end_dt=None,
            query=None,
            limit=20,
        )
        where_sql = self._compile_where(stmt)
        self.assertIn("user_id", where_sql)
        self.assertIn("555", where_sql)

    def test_group_scope_does_not_filter_on_user_id(self) -> None:
        stmt = _build_query_stmt(
            scope="group",
            group_id=999,
            user_id=12345,
            anchor_event_id=None,
            start_dt=None,
            end_dt=None,
            query=None,
            limit=20,
        )
        where_sql = self._compile_where(stmt)
        # group scope 传了 user_id 也不该拼进 WHERE——group 场景下它恒为
        # None（parse_scope_key 保证），这里只是确认过滤条件按 scope 互斥。
        self.assertNotIn("user_id", where_sql)
        self.assertIn("group_id", where_sql)


if __name__ == "__main__":
    unittest.main()
