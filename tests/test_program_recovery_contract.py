"""Crash-closure contracts for half-written effect calls.

2026-08-17 提案-裁决流水线：``agent.decision_emitted`` 不再参与启动收口。
决策事件和时间线上任何一条事件同级——写进去就是一段文本，没有状态、没有
生命周期。"这条执行过没有"是对事件流的一次查询，不是被维护的标志位，因此
收口器无从、也不该给它补终态。
"""

# Lightweight async protocol doubles deliberately accept generic call shapes.
# ruff: noqa: ANN001, ANN002, ANN003, ARG001, ARG002, PYI034

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qqbot.services.agent_loop.decision import DecisionContext, DecisionOutput
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.program_events import (
    ProgramAsset,
    RecoveryReport,
    _load_recovery_rows,
    load_program_asset,
    recover_interrupted_programs,
)
from qqbot.services.agent_loop.tool_registry import ToolRegistry


def _row(
    event_id: str,
    event_type: str,
    *,
    causation_id: str | None = None,
    payload: dict | None = None,
    correlation_id: str = "CORR",
):
    return SimpleNamespace(
        event_id=event_id,
        type=event_type,
        causation_id=causation_id,
        payload=payload or {},
        correlation_id=correlation_id,
    )


class ProgramRecoveryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_call_closes_but_decision_is_left_alone(self) -> None:
        rows = [
            _row(
                "D1",
                "agent.decision_emitted",
                payload={"program": 'notify(message="x")', "program_sha256": "SHA"},
            ),
            _row(
                "C1",
                "agent.tool_called",
                causation_id="D1",
                # 存量行仍带 task_id（2026-08-21 前的写入）——收口必须照常
                # 工作，但不把这个已死字段抄进新写的终态里。
                payload={
                    "tool_call_id": "TC1",
                    "tool_name": "notify",
                    "task_id": "T1",
                },
            ),
        ]
        session_factory = object()
        with (
            patch(
                "qqbot.services.agent_loop.program_events._load_recovery_rows",
                new=AsyncMock(return_value=rows),
            ) as load_rows,
            patch(
                "qqbot.services.agent_loop.program_events.write_agent_event",
                new=AsyncMock(return_value="TOOL_TERMINAL"),
            ) as write_tool,
            patch(
                "qqbot.services.agent_loop.program_events.write_program_failed",
                new=AsyncMock(return_value="PROGRAM_TERMINAL"),
            ) as write_program,
        ):
            report = await recover_interrupted_programs(
                session_factory, scope_key="group:7"
            )

        self.assertEqual(report, RecoveryReport(tool_calls_closed=1))
        load_rows.assert_awaited_once_with(session_factory, scope_key="group:7")
        write_tool.assert_awaited_once()
        tool_kwargs = write_tool.await_args.kwargs
        self.assertEqual(tool_kwargs["causation_id"], "C1")
        self.assertEqual(tool_kwargs["payload"]["error_kind"], "interrupted")
        self.assertEqual(tool_kwargs["payload"]["status"], "uncertain")
        self.assertIn("not replayed", tool_kwargs["payload"]["error_message"])
        self.assertNotIn("task_id", tool_kwargs["payload"])
        # 决策事件不收口：D1 没有 program terminal，收口器也不给它补一个。
        write_program.assert_not_awaited()

    async def test_recovery_never_queries_decision_events(self) -> None:
        """收口只认工具调用——决策事件根本不进这次查询。"""
        from qqbot.services.agent_loop import program_events

        self.assertNotIn("agent.decision_emitted", program_events._RECOVERY_TYPES)
        self.assertEqual(
            set(program_events._RECOVERY_TYPES),
            {"agent.tool_called", "agent.tool_result", "agent.tool_failed"},
        )
        self.assertNotIn(
            "programs_closed", RecoveryReport.__dataclass_fields__
        )

    async def test_existing_terminals_are_not_closed_again(self) -> None:
        rows = [
            _row(
                "D1",
                "agent.decision_emitted",
                payload={"program": "# done", "program_sha256": "SHA"},
            ),
            _row(
                "C1",
                "agent.tool_called",
                causation_id="D1",
                payload={"tool_call_id": "TC1", "tool_name": "notify"},
            ),
            _row("T1", "agent.tool_result", causation_id="C1"),
            _row("P1", "agent.program_completed", causation_id="D1"),
        ]
        with (
            patch(
                "qqbot.services.agent_loop.program_events._load_recovery_rows",
                new=AsyncMock(return_value=rows),
            ),
            patch(
                "qqbot.services.agent_loop.program_events.write_agent_event",
                new=AsyncMock(),
            ) as write_tool,
            patch(
                "qqbot.services.agent_loop.program_events.write_program_failed",
                new=AsyncMock(),
            ) as write_program,
        ):
            report = await recover_interrupted_programs(object(), scope_key="group:7")
        self.assertEqual(report, RecoveryReport())
        write_tool.assert_not_awaited()
        write_program.assert_not_awaited()

    async def test_legacy_batched_pending_call_is_closed_by_same_recovery(self) -> None:
        rows = [
            _row(
                "C_OLD",
                "agent.tool_called",
                payload={
                    "tool_call_id": "TC_OLD",
                    "tool_name": "send_messages",
                    "tool_batch_id": "OLD_BATCH",
                    "tool_batch_size": 2,
                },
            )
        ]
        with (
            patch(
                "qqbot.services.agent_loop.program_events._load_recovery_rows",
                new=AsyncMock(return_value=rows),
            ),
            patch(
                "qqbot.services.agent_loop.program_events.write_agent_event",
                new=AsyncMock(return_value="CLOSED"),
            ) as write_tool,
            patch(
                "qqbot.services.agent_loop.program_events.write_program_failed",
                new=AsyncMock(),
            ),
        ):
            report = await recover_interrupted_programs(object(), scope_key="group:7")
        self.assertEqual(report.tool_calls_closed, 1)
        self.assertEqual(
            write_tool.await_args.kwargs["payload"]["tool_call_id"], "TC_OLD"
        )

    async def test_database_query_is_filtered_to_requested_scope(self) -> None:
        captured: list[object] = []

        class _Result:
            def scalars(self) -> "_Result":
                return self

            def all(self) -> list:
                return []

        class _Session:
            async def execute(self, statement):
                captured.append(statement)
                return _Result()

            async def __aenter__(self) -> "_Session":
                return self

            async def __aexit__(self, *args) -> None:
                return None

        await _load_recovery_rows(_Session, "group:77")
        sql = str(captured[0])
        self.assertIn("agent_events.scope", sql)
        self.assertIn("agent_events.group_id", sql)
        self.assertNotIn("agent_events.user_id =", sql)


class _LookupResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalars(self) -> "_LookupResult":
        return self

    def first(self) -> object:
        return self._value

    def all(self) -> list:
        return [] if self._value is None else [self._value]


class _LookupSession:
    """Serves ``load_program_asset``'s single SELECT from a programmed row.

    2026-08-21：资产查找只剩一条语句。此前的第二条（"存不存在以那条决策为因的
    执行痕迹"）随 ``already_executed`` 守卫一并撤销——同源码即同资产，一份资产
    调度几次跑几次，系统不拦。
    """

    def __init__(self, *, decision: object | None = None) -> None:
        self.decision = decision
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _LookupResult:
        self.statements.append(statement)
        return _LookupResult(self.decision)

    async def __aenter__(self) -> "_LookupSession":
        return self

    async def __aexit__(self, *args) -> None:
        return None


def _decision_row(
    event_id: str = "01K2X9F3MQ8B4NVYRTC7HDZ6EW",
    *,
    event_type: str = "agent.decision_emitted",
    program: object = 'notify(message="hi")',
    correlation_id: str = "OLD_CORR",
) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id,
        type=event_type,
        correlation_id=correlation_id,
        payload={
            "program": program,
            "program_sha256": "SHA",
            "program_hash": "8f3c4e5a6b7c",
        },
    )


class LoadProgramAssetContractTests(unittest.IsolatedAsyncioTestCase):
    """资产库是对事件流的查询：按 ``payload.program_hash`` 找源码。

    2026-08-21 撤销 ``already_executed``：同源码必然同 hash，一份资产可以反复
    调度，调度几次跑几次。此前"存不存在以那条决策为因的 tool_called 或 program
    terminal"那条查询连同它挡住的两件事一并取消（任务与决策契约 §1.-1）。
    """

    HASH = "8f3c4e5a6b7c"
    EVENT_ID = "01K2X9F3MQ8B4NVYRTC7HDZ6EW"

    async def test_asset_is_returned_by_hash(self) -> None:
        session = _LookupSession(decision=_decision_row(self.EVENT_ID))
        asset, error = await load_program_asset(
            lambda: session, scope_key="group:7", program_hash=self.HASH
        )
        self.assertIsNone(error)
        self.assertEqual(
            asset,
            ProgramAsset(
                event_id=self.EVENT_ID,
                correlation_id="OLD_CORR",
                program='notify(message="hi")',
                program_sha256="SHA",
                program_hash=self.HASH,
            ),
        )

    async def test_lookup_issues_exactly_one_statement(self) -> None:
        """防回潮：第二条 SELECT 回来就等于 ``already_executed`` 守卫复活。"""
        session = _LookupSession(decision=_decision_row(self.EVENT_ID))
        await load_program_asset(
            lambda: session, scope_key="group:7", program_hash=self.HASH
        )
        self.assertEqual(len(session.statements), 1)

    async def test_missing_hash_is_program_not_found(self) -> None:
        session = _LookupSession(decision=None)
        asset, error = await load_program_asset(
            lambda: session, scope_key="group:7", program_hash=self.HASH
        )
        self.assertIsNone(asset)
        self.assertEqual(error, "program_not_found")

    async def test_empty_body_is_program_not_found(self) -> None:
        """空程序与纯裁决拍不进资产库——旧 ``decision_not_a_proposal`` 并入此类。"""
        for program in ("", "   ", None):
            with self.subTest(program=program):
                session = _LookupSession(
                    decision=_decision_row(self.EVENT_ID, program=program)
                )
                asset, error = await load_program_asset(
                    lambda: session, scope_key="group:7", program_hash=self.HASH
                )
                self.assertIsNone(asset)
                self.assertEqual(error, "program_not_found")

    async def test_query_filters_by_hash_type_and_scope(self) -> None:
        session = _LookupSession(decision=None)
        await load_program_asset(
            lambda: session, scope_key="group:77", program_hash=self.HASH
        )
        sql = str(session.statements[0])
        self.assertIn("agent_events.scope", sql)
        self.assertIn("agent_events.group_id", sql)
        self.assertNotIn("agent_events.user_id =", sql)
        # 只认带源码的决策事件，且按 payload 里的 hash 键寻址。
        self.assertIn("agent_events.type", sql)
        self.assertIn("program_hash", sql)


class RecoveryOrderingContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_tick_recovers_before_projection_and_only_once(self) -> None:
        order: list[str] = []

        class _Planner:
            async def decide(self, context: DecisionContext) -> DecisionOutput:
                return DecisionOutput(program="# idle")

        loop = AgentLoop(
            scope_key="group:1",
            planner=_Planner(),
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )

        async def _recover(*args, **kwargs) -> RecoveryReport:
            order.append("recover")
            return RecoveryReport()

        async def _build_context(*args, **kwargs) -> DecisionContext:
            order.append("project")
            return DecisionContext(
                scope_key="group:1",
                correlation_id=args[0],
                tick_seq=loop._tick_seq,
                now=args[1],
            )

        with (
            patch(
                "qqbot.services.agent_loop.loop.recover_interrupted_programs",
                new=_recover,
            ),
            patch.object(loop, "_build_context", new=_build_context),
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="DECISION"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(return_value="PROGRAM"),
            ),
        ):
            await loop._tick()
            await loop._tick()

        self.assertEqual(order, ["recover", "project", "project"])
        self.assertTrue(loop._recovery_done)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
