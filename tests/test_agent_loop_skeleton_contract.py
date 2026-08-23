"""Contract for the v2 AgentLoop skeleton (LoopSupervisor + AgentLoop + planner).

Pure unit-level; DB is faked by a recording session, no nonebot needed.

Verifies the skeleton produces the expected sequence of internal events on
one empty-program tick:
  runtime.tick_started → agent.decision_emitted → agent.program_completed
  → runtime.tick_ended
all sharing the same correlation_id.

Also verifies:
- LoopSupervisor lazy-instantiates GroupAgentLoop on wake.
- LoopSupervisor silently drops private:* wakes.
- LoopSupervisor.start() spawns the system loop up front.
- EventIngest publishes only newly committed SystemEvent values; duplicate
  inserts publish nothing, and plugin wiring owns scope-to-wake translation.
- scope_key parser handles all three AgentLoop scopes.
- 唤醒攒批窗口（2026-07-28 引入，2026-08-01 改固定窗口；2026-08-06 外部一律
  进窗）：wake() 由第一次开窗、到点才开拍，窗口内并入且不顺延；持续唤醒下每
  个窗口到点照常开拍。2026-08-17 起**自续拍也走同一条窗口**——落库的事件没有
  谁可以插队，那三秒正是人补完后半句的时间；自续与外部的差别只在是否清零
  `AGENT_CONTINUATION_MAX_TICKS` 的计数。

空程序拍的事件链在 2026-08-17 提案-裁决流水线下不变（它是唯一的停止符）；
提案拍 / 裁决拍的事件链由 test_program_decision_contract.py 覆盖。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from qqbot.services.agent_loop import (
    AgentLoop,
    DecisionOutput,
    LoopSupervisor,
)
from qqbot.services.agent_loop.event_writer import parse_scope_key
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
)


class _FakeIdlePlanner:
    """空程序 planner：只验证循环接线（events → tick → events），不碰 LLM。

    原为生产包里的 qqbot/services/agent_loop/planner.py::FakeIdlePlanner，
    2026-07-31 迁入测试——它从来没有生产消费者，LoopSupervisor 装的是
    LLMPlanner。按本目录惯例内联在用到它的测试文件里，不建共享 fixture 模块。
    """

    async def decide(self, context: Any) -> DecisionOutput:
        _ = context
        return DecisionOutput(program="# bootstrap skeleton: intentionally idle")


class _EmptyResult:
    """Empty result compatible with the recovery/backfill SELECT consumers."""

    def mappings(self) -> "_EmptyResult":
        return self

    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list:
        return []

    def first(self) -> None:
        return None


class _RecordingSession:
    """async session double that captures every executed insert statement.

    Reads used by task backfill and program crash recovery are
    ignored and return an empty result. Only mutating statements are appended
    to ``store``.
    """

    def __init__(self, store: list[Any]) -> None:
        self._store = store

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        from sqlalchemy.sql.elements import TextClause

        _ = params
        if isinstance(stmt, TextClause) or bool(getattr(stmt, "is_select", False)):
            return _EmptyResult()
        self._store.append(stmt)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> "_RecordingSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


def _factory_for(store: list[Any]):
    def factory() -> _RecordingSession:
        return _RecordingSession(store)

    return factory


def _values_of(stmt: Any) -> dict:
    """Pull the column→value map out of a SQLAlchemy insert statement."""
    # pg_insert(...).values(...) builds a dict; SQLAlchemy stores it on
    # stmt.parameters or .compile().params depending on construction. We
    # use the .compile() route to keep it dialect-agnostic.
    return {k: v for k, v in stmt.compile().params.items()}


def _bind(params: dict, prefix: str) -> Any:
    for key, value in params.items():
        if key == prefix or key.startswith(prefix + "_"):
            return value
    return None


def _mentioned_event_types(compiled: Any) -> set[str]:
    found: set[str] = set()
    text = str(compiled)
    for name in (
        "agent.tool_called",
        "agent.program_completed",
        "agent.program_failed",
        "agent.tool_result",
        "agent.tool_failed",
        "agent.decision_emitted",
    ):
        if name in text:
            found.add(name)
    for value in compiled.params.values():
        if isinstance(value, str) and value.startswith("agent."):
            found.add(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, str) and item.startswith("agent."):
                    found.add(item)
    return found


class _SelectResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> "_SelectResult":
        return self

    def scalars(self) -> "_SelectResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _DecisionLookupSession(_RecordingSession):
    """Recording session that can answer ``load_program_asset`` SELECTs.

    Other SELECTs (recovery, projection, task backfill) stay empty so idle-tick
    tests keep seeing a blank world.
    """

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        from sqlalchemy.sql.elements import TextClause

        _ = params
        if isinstance(stmt, TextClause) or bool(getattr(stmt, "is_select", False)):
            return self._select(stmt)
        self._store.append(stmt)
        return SimpleNamespace(rowcount=1)

    def _select(self, stmt: Any) -> _SelectResult:
        compiled = stmt.compile()
        params = compiled.params
        causation_id = _bind(params, "causation_id")
        event_id = _bind(params, "event_id")
        rows = []
        for item in self._store:
            vals = _values_of(item)
            rows.append(
                SimpleNamespace(
                    event_id=vals.get("event_id"),
                    type=vals.get("type"),
                    scope=vals.get("scope"),
                    group_id=vals.get("group_id"),
                    user_id=vals.get("user_id"),
                    correlation_id=vals.get("correlation_id"),
                    causation_id=vals.get("causation_id"),
                    payload=vals.get("payload") or {},
                )
            )
        if causation_id is not None:
            mentioned = _mentioned_event_types(compiled)
            hits = [
                row.event_id
                for row in rows
                if row.causation_id == causation_id and row.type in mentioned
            ]
            return _SelectResult(hits)
        if isinstance(event_id, str):
            scope = _bind(params, "scope")
            group_id = _bind(params, "group_id")
            user_id = _bind(params, "user_id")
            hits = []
            for row in rows:
                if row.event_id != event_id:
                    continue
                if scope is not None and row.scope != scope:
                    continue
                if group_id is not None and row.group_id != group_id:
                    continue
                if user_id is not None and row.user_id != user_id:
                    continue
                hits.append(row)
            return _SelectResult(hits)
        return _SelectResult([])


def _pipeline_factory_for(store: list[Any]):
    def factory() -> _DecisionLookupSession:
        return _DecisionLookupSession(store)

    return factory


class _ProposeThenCommitPlanner:
    """第一拍写业务代码，第二拍指名那条决策；之后空程序。"""

    def __init__(
        self,
        store: list[Any],
        program: str,
        *,
        delay_commit: float = 0.0,
    ) -> None:
        self._store = store
        self._program = program
        self._delay_commit = delay_commit

    async def decide(self, context: Any) -> DecisionOutput:
        _ = context
        decisions = [
            _values_of(stmt)
            for stmt in self._store
            if _values_of(stmt).get("type") == "agent.decision_emitted"
        ]
        if not decisions:
            return DecisionOutput(program=self._program)
        if len(decisions) == 1:
            if self._delay_commit:
                await asyncio.sleep(self._delay_commit)
            event_id = decisions[0].get("event_id")
            return DecisionOutput(
                program=f'execute_program(program_hash="{event_id}")'
            )
        return DecisionOutput(program="# nothing left to do")


class ScopeKeyParserTests(unittest.TestCase):
    def test_system(self) -> None:
        self.assertEqual(parse_scope_key("system"), ("system", None, None))

    def test_group(self) -> None:
        self.assertEqual(parse_scope_key("group:12345"), ("group", 12345, None))

    def test_private(self) -> None:
        self.assertEqual(parse_scope_key("private:99"), ("private", None, 99))

    def test_invalid(self) -> None:
        with self.assertRaises(ValueError):
            parse_scope_key("bogus")


class _SlowIdlePlanner:
    """模拟 LLM 往返：decide() 里睡一段可观测的时间再返回空程序。

    用来把"投影时刻"和"决策写入时刻"拉开到断言可分辨的距离。
    """

    DELAY = 0.15

    async def decide(self, context: Any) -> Any:
        _ = context
        await asyncio.sleep(self.DELAY)
        return DecisionOutput(program="# slow idle")


class _TimestampEffect(BaseTool):
    name = "timestamp_effect"
    program_kind = "effect"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    # 字段不能叫 ok：2026-08-15 起 ok/error 由结果信封统一注入。
    result_schema = {
        "type": "object",
        "properties": {"passed": {"type": "boolean"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"passed": True})


class AgentLoopSkeletonTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_wake_produces_idle_tick_event_chain(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        # 本用例验 tick 事件链，不是攒批时序；窗口置 0 立刻开拍。
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0
        ):
            loop.start()
            loop.wake()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if len(captured) >= 4:
                    break
            await loop.stop()

        # 空程序仍有独立 terminal；不再写 idle_decision。
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertEqual(
            types,
            [
                "runtime.tick_started",
                "agent.decision_emitted",
                "agent.program_completed",
                "runtime.tick_ended",
            ],
        )
        decision_payload = _values_of(captured[1]).get("payload")
        self.assertEqual(
            decision_payload["program"],
            "# bootstrap skeleton: intentionally idle",
        )
        self.assertIn("program_sha256", decision_payload)

        # 同一 tick 内 correlation_id 一致
        corrs = {_values_of(stmt).get("correlation_id") for stmt in captured}
        self.assertEqual(len(corrs), 1)

        # decision_emitted → program_completed 因果链
        decision_id = _values_of(captured[1]).get("event_id")
        terminal_caus = _values_of(captured[2]).get("causation_id")
        self.assertEqual(terminal_caus, decision_id)

        # tick_started → tick_ended 因果链
        tick_started_id = _values_of(captured[0]).get("event_id")
        tick_ended_caus = _values_of(captured[3]).get("causation_id")
        self.assertEqual(tick_ended_caus, tick_started_id)

    async def test_decision_timestamp_is_tick_start_not_write_time(self) -> None:
        """agent.decision_emitted.occurred_at = 本拍**投影时刻**，不是写入时刻
        （2026-07-24，待办清单#18）。

        投影读于 planner.decide() 之前、事件却写于 LLM 返回之后，而事件流按
        occurred_at 排序（Projector._fetch）。若取写入时刻，LLM 往返期间到达
        的消息会排到决策事件**之前**，事件流的因果顺序即与本拍真实看到的内容
        不符。

        2026-08-02 删除 `<message unseen="true">` 后本条护栏**不随之取消**：
        decision_emitted 虽不再投影、也不再充当水位线，但它与同拍
        ``tool_called`` 意图必须同刻，后者要进入时间线（事件系统设计.md
        §时间戳约束）。
        """
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_SlowIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0
        ):
            loop.start()
            loop.wake()
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(captured) >= 4:
                    break
            await loop.stop()

        by_type = {
            _values_of(stmt).get("type"): _values_of(stmt) for stmt in captured
        }
        started = by_type["runtime.tick_started"]["occurred_at"]
        decision = by_type["agent.decision_emitted"]["occurred_at"]
        ended = by_type["runtime.tick_ended"]["occurred_at"]

        # tick_started 取默认的写入时刻，写在 now=china_now() 之后；decision
        # 回填 now，因此必然 <= tick_started。旧行为（取写入时刻）会比它晚
        # 整整一个 planner 延迟，这条断言即失败。
        self.assertLessEqual(decision, started)
        # 反证 planner 确实慢过一拍：tick 收尾比决策时间戳晚至少一个 DELAY，
        # 说明上面的 <= 不是"planner 快到看不出差别"蒙对的。
        self.assertGreaterEqual(
            (ended - decision).total_seconds(), _SlowIdlePlanner.DELAY
        )
        # program terminal 陈述执行完成，取实际完成时刻而非投影锚点。
        terminal = by_type["agent.program_completed"]["occurred_at"]
        self.assertGreaterEqual(terminal, decision)

    async def test_effect_intent_timestamp_is_tick_start_not_write_time(self) -> None:
        """``tool_called.occurred_at`` 锚定**裁决拍**的投影时刻，不是写入时刻。

        提案拍只落库，不写 ``tool_called``。指名执行的那一拍才入队，Runner
        用那一拍的 ``context.now`` 当意图时间；correlation 仍归被引用的提案。
        """
        captured: list[Any] = []
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        delay = 0.15
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_ProposeThenCommitPlanner(
                captured, "timestamp_effect()", delay_commit=delay
            ),
            session_factory=_pipeline_factory_for(captured),
            tool_registry=registry,
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0
        ):
            loop.start()
            loop.wake()
            for _ in range(200):
                await asyncio.sleep(0.01)
                types = {
                    _values_of(stmt).get("type")
                    for stmt in captured
                    if getattr(stmt, "table", None) is not None
                }
                if (
                    "agent.tool_called" in types
                    and "agent.program_completed" in types
                ):
                    break
            await loop.stop()

        events = [
            _values_of(stmt)
            for stmt in captured
            if getattr(stmt, "table", None) is not None
        ]
        decisions = [
            item
            for item in events
            if item.get("type") == "agent.decision_emitted"
        ]
        self.assertGreaterEqual(len(decisions), 2)
        proposal, commit = decisions[0], decisions[1]
        called = next(
            item for item in events if item.get("type") == "agent.tool_called"
        )
        result = next(
            item for item in events if item.get("type") == "agent.tool_result"
        )
        commit_corr = commit["correlation_id"]
        commit_started = next(
            item
            for item in events
            if item.get("type") == "runtime.tick_started"
            and item.get("correlation_id") == commit_corr
        )
        commit_ended = next(
            item
            for item in events
            if item.get("type") == "runtime.tick_ended"
            and item.get("correlation_id") == commit_corr
        )
        self.assertEqual(called["occurred_at"], commit["occurred_at"])
        self.assertEqual(called["causation_id"], proposal["event_id"])
        self.assertEqual(called["correlation_id"], proposal["correlation_id"])
        self.assertLessEqual(called["occurred_at"], commit_started["occurred_at"])
        self.assertGreaterEqual(result["occurred_at"], called["occurred_at"])
        self.assertGreaterEqual(
            (commit_ended["occurred_at"] - commit["occurred_at"]).total_seconds(),
            delay,
        )

    async def test_loop_idle_when_not_waked(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:1",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        loop.start()
        await asyncio.sleep(0.05)
        await loop.stop()
        self.assertEqual(captured, [])

    async def test_bot_user_id_resolver_called_each_tick(self) -> None:
        """resolver 每 tick 被调一次 —— bot 重连后 self_id 可能变；每 tick
        重新 resolve 比启动期 snapshot 更稳。"""
        captured: list[Any] = []
        call_count = {"n": 0}

        def _resolver() -> str | None:
            call_count["n"] += 1
            return "3167291813"

        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
            bot_user_id_resolver=_resolver,
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0
        ):
            loop.start()
            loop.wake()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if call_count["n"] >= 1:
                    break
            await loop.stop()
        # 至少跑了一 tick → resolver 至少被调一次
        self.assertGreaterEqual(call_count["n"], 1)

    async def test_bot_user_id_resolver_exception_does_not_break_tick(self) -> None:
        """resolver 抛异常时整 tick 不应翻车 —— prompt 降级为没有 bot_user_id
        属性，业务继续。"""
        captured: list[Any] = []

        def _broken_resolver() -> str | None:
            raise RuntimeError("bot_registry unavailable")

        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
            bot_user_id_resolver=_broken_resolver,
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0
        ):
            loop.start()
            loop.wake()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if len(captured) >= 4:
                    break
            await loop.stop()
        # 正常空程序事件链应当落地，不被 resolver 异常掐断。
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertIn("runtime.tick_started", types)
        self.assertIn("runtime.tick_ended", types)


class WakeBatchWindowTests(unittest.IsolatedAsyncioTestCase):
    """唤醒攒批窗口（2026-07-28 引入，2026-08-01 由滑动改固定）。

    存在的理由不是省 tick —— asyncio.Event 早就能把"上一拍还在跑"期间的多次
    唤醒并成一次。堵的是 loop **空闲**时第一条消息立刻开拍这个洞：QQ 上一句话
    拆成三条发是常态，不等一等就会对着半截话表态。

    固定窗口：第一次唤醒开窗，窗口内的唤醒并入本窗、不顺延 deadline。开拍延迟
    因此有界，不再需要（也不再有）防饿死的封顶常量。

    窗口值用 patch 压到毫秒级跑，避免测试挂在真实的 3s 上。
    """

    @staticmethod
    async def _settle(captured: list[Any], count: int, budget: float) -> None:
        deadline = budget
        while deadline > 0 and len(captured) < count:
            await asyncio.sleep(0.01)
            deadline -= 0.01

    async def _tick_count(self, captured: list[Any]) -> int:
        return sum(
            1
            for stmt in captured
            if _values_of(stmt).get("type") == "runtime.tick_started"
        )

    async def test_plain_wake_waits_for_batch_window(self) -> None:
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0.2
        ):
            loop.start()
            loop.wake()
            # 窗口未到：还不该开拍
            await asyncio.sleep(0.05)
            self.assertEqual(await self._tick_count(captured), 0)
            # 安静下来之后开拍
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertEqual(await self._tick_count(captured), 1)

    async def test_burst_of_wakes_collapses_into_one_tick(self) -> None:
        """一句话拆成三条发 → 只开一拍，且这拍看得到全部三条。"""
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0.2
        ):
            loop.start()
            for _ in range(3):
                loop.wake()
                await asyncio.sleep(0.05)  # 窗口内陆续到达 → 并入本窗，不顺延
            self.assertEqual(await self._tick_count(captured), 0)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertEqual(await self._tick_count(captured), 1)

    async def test_external_wake_never_bypasses_window(self) -> None:
        """外部 wake 一律进攒批窗口（含 wait 到点 / 静默）。"""
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0.3
        ):
            loop.start()
            loop.wake()
            await asyncio.sleep(0.05)
            self.assertEqual(await self._tick_count(captured), 0)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertEqual(await self._tick_count(captured), 1)

    async def test_self_wake_uses_the_same_window(self) -> None:
        """自续拍没有 immediate 旁路（2026-08-17 维护者裁定）。

        提案拍写完源码后自己开下一拍，那一拍要等窗口到点才起——否则裁决拍的
        投影紧贴着提案拍拍出，这三秒里到达的消息（正是人补完的后半句）就照旧
        看不见，多出来的那一拍白花一次推理。
        """
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for([]),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0.3
        ):
            self.assertTrue(loop._wake_continuation())
            # 开的是窗口，不是立刻置位。
            self.assertFalse(loop._wake.is_set())
            self.assertIsNotNone(loop._wake_deadline)
            await asyncio.sleep(0.5)
            self.assertTrue(loop._wake.is_set())
        loop._cancel_wake_timer()

    async def test_continuous_wakes_tick_once_per_window(self) -> None:
        """持续刷屏不能把 tick 饿死：窗口不被顺延，到点就开拍，之后的唤醒开一
        个新窗口。这也是固定窗口不再需要封顶常量的原因。"""
        captured: list[Any] = []
        loop = AgentLoop(
            scope_key="group:12345",
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0.2
        ):
            loop.start()
            # 每 50ms 一次唤醒持续 0.6s：旧的滑动实现会一路顺延到封顶才开一拍，
            # 固定窗口下 0.2s 的窗口在这段时间里至少轮到两次。
            for _ in range(12):
                loop.wake()
                await asyncio.sleep(0.05)
            await self._settle(captured, 4, budget=1.0)
            await loop.stop()
        self.assertGreaterEqual(await self._tick_count(captured), 2)


class LoopSupervisorContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_spawns_system_loop(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        self.assertTrue(sup.started)
        self.assertEqual(sup.loop_count, 1)
        await sup.stop()

    async def test_wake_lazy_creates_group_loop(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        with patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0
        ):
            await sup.wake("group:12345")
            for _ in range(50):
                await asyncio.sleep(0.01)
                if captured:
                    break

        # 必须在 stop() 之前断言：stop 会 _loops.clear()，loop_count 归零。
        self.assertEqual(sup.loop_count, 2)  # system + group:12345
        # 至少有一个事件来自 group:12345
        group_ids = {_values_of(stmt).get("group_id") for stmt in captured}
        self.assertIn(12345, group_ids)

        await sup.stop()

    async def test_private_wake_is_silently_dropped(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        await sup.wake("private:222")
        await asyncio.sleep(0.02)

        # 同上：先断言再 stop。private wake 不应实例化 loop。
        self.assertEqual(sup.loop_count, 1)  # 只有 system
        # 没有 private 事件
        scopes = {_values_of(stmt).get("scope") for stmt in captured}
        self.assertNotIn("private", scopes)

        await sup.stop()

    async def test_wake_after_stop_is_noop(self) -> None:
        captured: list[Any] = []
        sup = LoopSupervisor(
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for(captured),
        )
        await sup.start()
        await sup.stop()
        # 断言对象是"stop 后 wake 不再产生任何新语句"，故取 stop 后的基线数
        # 比对，而不要求 captured 全程为空——start() 期间可能有别的启动查询。
        # （2026-08-21 前这里的启动查询是 task_store.backfill_recent，已随
        # agent_tasks 读模型一并删除。）
        baseline = len(captured)
        await sup.wake("group:1")
        await asyncio.sleep(0.02)
        self.assertEqual(len(captured), baseline)


class SupervisorSilenceArmingTests(unittest.IsolatedAsyncioTestCase):
    """静默武装挂在 note_activity，不挂在 wake（2026-08-06）。"""

    class _SpyWatcher:
        def __init__(self) -> None:
            self.armed: list[str] = []
            self.enabled = True

        def notify_activity(self, scope_key: str) -> None:
            self.armed.append(scope_key)

        async def stop(self) -> None:
            return None

    async def _supervisor(self) -> tuple[Any, "_SpyWatcher"]:
        sup = LoopSupervisor(
            planner=_FakeIdlePlanner(),
            session_factory=_factory_for([]),
        )
        await sup.start()
        watcher = self._SpyWatcher()
        sup._silence_watcher = watcher
        return sup, watcher

    async def test_wake_alone_does_not_arm_silence_timer(self) -> None:
        """纯 wake 只开拍，不算时间线动静。"""
        sup, watcher = await self._supervisor()
        await sup.wake("group:1")
        await sup.wake("group:2")
        await sup.stop()
        self.assertEqual(watcher.armed, [])

    async def test_note_activity_rearms_the_silence_timer(self) -> None:
        sup, watcher = await self._supervisor()
        sup.note_activity("group:1")
        sup.note_activity("group:2")
        await sup.stop()
        self.assertEqual(watcher.armed, ["group:1", "group:2"])


class MemoryCompactorWiringTests(unittest.IsolatedAsyncioTestCase):
    """记忆压缩器接线（记忆系统契约 §4.1/§4.2）：开关默认关 = 不构造；
    打开 = start 挂起等待触顶 + 投影装探针 + stop 收掉；未启用时 notify
    安全 no-op。"""

    async def test_disabled_by_default_no_compactor(self) -> None:
        import os

        # 显式关掉开关再断言（与下面 enabled 用例对称）。原先靠"环境里没配"
        # 隐式成立，而部署机的 .env 里 MEMORY_COMPACTION_ENABLED=true，
        # memory_compaction_enabled() 读的就是真实 env —— 这条在开了压缩的
        # 机器上必然失败。测试必须自己控制前置条件，不能继承部署配置。
        old = os.environ.get("MEMORY_COMPACTION_ENABLED")
        os.environ["MEMORY_COMPACTION_ENABLED"] = "false"
        try:
            captured: list[Any] = []
            sup = LoopSupervisor(
                planner=_FakeIdlePlanner(),
                session_factory=_factory_for(captured),
            )
            await sup.start()
            self.assertIsNone(sup._memory_compactor)
            sup.notify_compaction("group:1", 250)  # 未启用：安全 no-op
            await sup.stop()
        finally:
            if old is None:
                os.environ.pop("MEMORY_COMPACTION_ENABLED", None)
            else:
                os.environ["MEMORY_COMPACTION_ENABLED"] = old

    async def test_enabled_env_wires_compactor_and_probe(self) -> None:
        import os

        from qqbot.services.agent_loop.projection import Projector

        old = os.environ.get("MEMORY_COMPACTION_ENABLED")
        os.environ["MEMORY_COMPACTION_ENABLED"] = "true"
        try:
            captured: list[Any] = []
            projector = Projector(_factory_for(captured))
            sup = LoopSupervisor(
                planner=_FakeIdlePlanner(),
                session_factory=_factory_for(captured),
                projector=projector,
            )
            await sup.start()
            self.assertIsNotNone(sup._memory_compactor)
            self.assertIsNotNone(projector._uncovered_notifier)
            await sup.stop()
            self.assertIsNone(sup._memory_compactor)
        finally:
            if old is None:
                os.environ.pop("MEMORY_COMPACTION_ENABLED", None)
            else:
                os.environ["MEMORY_COMPACTION_ENABLED"] = old


class IngestSupervisorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingest_notifies_only_with_committed_internal_event(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        committed: list[Any] = []

        async def notify(event: Any) -> None:
            committed.append(event)

        class FakeSession:
            async def execute(self, stmt: Any) -> Any:
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(
            build_default_registry(),
            session_factory=FakeSession,
            committed_notifier=notify,
        )
        event = SimpleNamespace(
            post_type="message",
            message_type="group",
            sub_type="normal",
            time=1716700000,
            self_id=10000,
            message_id=12345,
            group_id=999,
            user_id=222,
            raw_message="hi",
            message=[],
            sender=None,
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "inserted")
        self.assertEqual(committed, [result.event])
        self.assertEqual(committed[0].scope, "group")
        self.assertEqual(committed[0].group_id, 999)

    async def test_private_event_is_still_published_as_committed(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        committed: list[Any] = []

        async def notify(event: Any) -> None:
            committed.append(event)

        class FakeSession:
            async def execute(self, stmt: Any) -> Any:
                return SimpleNamespace(rowcount=1)

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(
            build_default_registry(),
            session_factory=FakeSession,
            committed_notifier=notify,
        )
        event = SimpleNamespace(
            post_type="message",
            message_type="private",
            sub_type="friend",
            time=1716700000,
            self_id=10000,
            message_id=5,
            user_id=222,
            raw_message="hi",
            message=[],
            sender=None,
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "inserted")
        self.assertEqual(committed, [result.event])
        self.assertEqual(committed[0].scope, "private")

    async def test_ingest_does_not_notify_for_duplicate(self) -> None:
        from qqbot.services.event_ingest import EventIngest
        from qqbot.services.event_ingest.mappers import build_default_registry

        committed: list[Any] = []

        async def notify(event: Any) -> None:
            committed.append(event)

        class FakeSession:
            async def execute(self, stmt: Any) -> Any:
                return SimpleNamespace(rowcount=0)  # conflict

            async def commit(self) -> None:
                return None

            async def __aenter__(self) -> "FakeSession":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        ingest = EventIngest(
            build_default_registry(),
            session_factory=FakeSession,
            committed_notifier=notify,
        )
        event = SimpleNamespace(
            post_type="message", message_type="group", sub_type="normal",
            time=1716700000, self_id=10000, message_id=12345,
            group_id=999, user_id=222, raw_message="", message=[], sender=None,
        )
        result = await ingest.ingest(event)
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(committed, [])


class _ScriptedPlanner:
    """按脚本逐拍返回程序；脚本用尽后一律返回空程序。

    空程序收尾是自续拍的不动点，因此即使被测代码有 bug 也不会把测试跑成死循环。
    """

    def __init__(self, programs: list[str]) -> None:
        self._programs = list(programs)

    async def decide(self, context: Any) -> DecisionOutput:
        _ = context
        if self._programs:
            return DecisionOutput(program=self._programs.pop(0))
        return DecisionOutput(program="# nothing left to do")


class _AlwaysCallingPlanner:
    """每拍都调用一次 effect —— 只有上界才能让它停下来。"""

    async def decide(self, context: Any) -> DecisionOutput:
        _ = context
        return DecisionOutput(program="timestamp_effect()")


class _TimestampQuery(BaseTool):
    name = "timestamp_query"
    program_kind = "query"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"passed": {"type": "boolean"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.success({"passed": True})


class _FailingEffect(BaseTool):
    name = "failing_effect"
    program_kind = "effect"
    arguments_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {"passed": {"type": "boolean"}},
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        return ToolOutcome.failure("internal_tool_error", "boom")


class ContinuationMaxTicksResolverTests(unittest.TestCase):
    """``AGENT_CONTINUATION_MAX_TICKS`` 解析（任务与决策契约 §1.2）。"""

    def _resolve(self, raw: str | None) -> int | None:
        with patch(
            "qqbot.services.agent_loop.loop.get_env_value", return_value=raw
        ):
            from qqbot.services.agent_loop.loop import continuation_max_ticks

            return continuation_max_ticks()

    def test_unset_is_unlimited(self) -> None:
        self.assertIsNone(self._resolve(None))

    def test_blank_is_unlimited(self) -> None:
        self.assertIsNone(self._resolve("   "))

    def test_zero_disables(self) -> None:
        self.assertEqual(self._resolve("0"), 0)

    def test_positive_is_the_cap(self) -> None:
        self.assertEqual(self._resolve("5"), 5)

    def test_negative_clamps_to_disabled(self) -> None:
        self.assertEqual(self._resolve("-3"), 0)

    def test_garbage_falls_back_to_unlimited(self) -> None:
        self.assertIsNone(self._resolve("many"))


class ContinuationTickTests(unittest.IsolatedAsyncioTestCase):
    """自续拍：提案拍 / 裁决报错拍 / Runner terminal 都再开一拍；空程序是不动点。

    提案拍自己就会自唤醒，不代表程序跑过。要证明副作用发生，必须再看一拍
    ``execute_program`` 之后有没有 ``tool_called``。
    """

    @staticmethod
    def _tick_count(captured: list[Any]) -> int:
        return sum(
            1
            for stmt in captured
            if _values_of(stmt).get("type") == "runtime.tick_started"
        )

    async def _run(
        self,
        planner: Any,
        *,
        registry: ToolRegistry | None = None,
        expect_ticks: int,
        max_ticks: int | None = None,
        lookup: bool = False,
        store: list[Any] | None = None,
    ) -> list[Any]:
        """开一次外部 wake，跑到链条停稳，返回捕获到的语句。

        settle 预算给到期望拍数之后仍多等一截，好让"多续了一拍"这类回归表现为
        断言失败而不是恰好没观测到。
        """
        captured = store if store is not None else []
        factory = (
            _pipeline_factory_for(captured) if lookup else _factory_for(captured)
        )
        with patch(
            "qqbot.services.agent_loop.loop.continuation_max_ticks",
            return_value=max_ticks,
        ), patch(
            "qqbot.services.agent_loop.loop._WAKE_BATCH_WINDOW_SECONDS", 0
        ):
            loop = AgentLoop(
                scope_key="group:12345",
                planner=planner,
                session_factory=factory,
                tool_registry=registry,
            )
            loop.start()
            loop.wake()
            for _ in range(120):
                await asyncio.sleep(0.01)
                if self._tick_count(captured) > expect_ticks:
                    break
            await asyncio.sleep(0.05)
            await loop.stop()
        return captured

    async def test_empty_program_does_not_continue(self) -> None:
        """空程序是不动点：一次外部唤醒只换来一拍。"""
        captured = await self._run(_FakeIdlePlanner(), expect_ticks=1)
        self.assertEqual(self._tick_count(captured), 1)

    async def test_proposal_self_wakes_without_running(self) -> None:
        """提案拍只落库：自唤醒去审阅，当拍不写 tool_called。"""
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        captured = await self._run(
            _ScriptedPlanner(["timestamp_effect()"]),
            registry=registry,
            expect_ticks=2,
        )
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertEqual(self._tick_count(captured), 2)
        self.assertNotIn("agent.tool_called", types)
        ended = [
            _values_of(stmt)
            for stmt in captured
            if _values_of(stmt).get("type") == "runtime.tick_ended"
        ]
        self.assertEqual(ended[0]["payload"]["program_status"], "proposed")
        self.assertTrue(ended[0]["payload"]["left_proposal"])

    async def test_commit_runs_named_program_then_terminal_wakes(self) -> None:
        """后一拍 execute_program 才入队；terminal 再叫醒空程序收尾。"""
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        captured: list[Any] = []
        captured = await self._run(
            _ProposeThenCommitPlanner(captured, "timestamp_effect()"),
            registry=registry,
            expect_ticks=3,
            lookup=True,
            store=captured,
        )
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertEqual(self._tick_count(captured), 3)
        self.assertIn("agent.tool_called", types)
        self.assertIn("agent.program_completed", types)
        decisions = [
            _values_of(stmt)
            for stmt in captured
            if _values_of(stmt).get("type") == "agent.decision_emitted"
        ]
        called = next(
            _values_of(stmt)
            for stmt in captured
            if _values_of(stmt).get("type") == "agent.tool_called"
        )
        self.assertEqual(called["causation_id"], decisions[0]["event_id"])
        self.assertEqual(decisions[1]["payload"]["program"], "")

    async def test_query_only_commit_wakes_via_terminal(self) -> None:
        """现役一律 Effect：标成 query 的函数同样要先被指名才跑，同样写
        ``tool_called``，terminal 再叫醒。
        """
        registry = ToolRegistry()
        registry.register(_TimestampQuery)
        captured: list[Any] = []
        captured = await self._run(
            _ProposeThenCommitPlanner(captured, "timestamp_query()"),
            registry=registry,
            expect_ticks=3,
            lookup=True,
            store=captured,
        )
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertEqual(self._tick_count(captured), 3)
        self.assertIn("agent.tool_called", types)
        self.assertIn("agent.program_completed", types)
        called = next(
            _values_of(stmt)
            for stmt in captured
            if _values_of(stmt).get("type") == "agent.tool_called"
        )
        self.assertEqual(called["payload"]["tool_name"], "timestamp_query")

    async def test_failed_call_completes_and_wakes(self) -> None:
        """失败调用是返回值（2026-08-15），程序跑完仍写 terminal 并唤醒。"""
        registry = ToolRegistry()
        registry.register(_FailingEffect)
        captured: list[Any] = []
        captured = await self._run(
            _ProposeThenCommitPlanner(captured, "failing_effect()"),
            registry=registry,
            expect_ticks=3,
            lookup=True,
            store=captured,
        )
        types = [_values_of(stmt).get("type") for stmt in captured]
        self.assertEqual(self._tick_count(captured), 3)
        self.assertIn("agent.tool_called", types)
        self.assertIn("agent.tool_failed", types)
        self.assertIn("agent.program_completed", types)
        self.assertNotIn("agent.program_failed", types)

    async def test_max_ticks_caps_the_chain(self) -> None:
        """上界约束自续（提案拍自唤醒也算）：1 → 外部一拍 + 自续一拍后必须停。"""
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        captured = await self._run(
            _AlwaysCallingPlanner(),
            registry=registry,
            expect_ticks=2,
            max_ticks=1,
        )
        self.assertEqual(self._tick_count(captured), 2)

    async def test_zero_max_disables_continuation(self) -> None:
        """0 = 关闭自续拍，退回纯事件驱动。"""
        registry = ToolRegistry()
        registry.register(_TimestampEffect)
        captured = await self._run(
            _AlwaysCallingPlanner(),
            registry=registry,
            expect_ticks=1,
            max_ticks=0,
        )
        self.assertEqual(self._tick_count(captured), 1)

    async def test_external_wake_resets_continuation_depth(self) -> None:
        """外部唤醒 = 新一段活动，自转计数归零，上界重新起算。"""
        with patch(
            "qqbot.services.agent_loop.loop.continuation_max_ticks",
            return_value=2,
        ):
            loop = AgentLoop(
                scope_key="group:12345",
                planner=_FakeIdlePlanner(),
                session_factory=_factory_for([]),
            )
        loop._continuation_depth = 2
        self.assertFalse(loop._wake_continuation())
        loop.wake()
        self.assertEqual(loop._continuation_depth, 0)
        self.assertTrue(loop._wake_continuation())

    async def test_stopped_loop_does_not_continue(self) -> None:
        with patch(
            "qqbot.services.agent_loop.loop.continuation_max_ticks",
            return_value=None,
        ):
            loop = AgentLoop(
                scope_key="group:12345",
                planner=_FakeIdlePlanner(),
                session_factory=_factory_for([]),
            )
        loop._stopped = True
        self.assertFalse(loop._wake_continuation())


if __name__ == "__main__":
    unittest.main()
