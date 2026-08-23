"""AgentLoop contracts for program-shaped decisions.

三组：

- **一个响应绑定一次模型执行**（2026-08-21 渲染格式表 §一⑦）：每拍只问一次
  模型；内容不合规当场签发 ``agent.invalid_action``（回灌被拒源码、自唤醒、
  计入续拍深度），不重试、不换端点、不冷却；传输失败归模型提供层，
  **不进时间线**。
- 决策事件本身：合规响应成为一份代码资产。
- 2026-08-17 的提案-裁决流水线——写下的程序当拍只落库，要等后来某一拍
  ``execute_program(program_hash=…)`` 指名才交给 Runner 执行。
"""

# Async mocks accept the production call shape while recording only ordering.
# ruff: noqa: ARG001

from __future__ import annotations

import hashlib
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from qqbot.services.agent_loop.decision import DecisionContext, DecisionOutput
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.program_events import ProgramAsset
from qqbot.services.agent_loop.tool_registry import (
    BaseTool,
    ToolOutcome,
    ToolRegistry,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET_ID = "01K2X9F3MQ8B4NVYRTC7HDZ6EW"
TARGET_HASH = "8f3c4e5a6b7c"


class _NotifyTool(BaseTool):
    """派发路径的替身；本文件的用例都在 `_tick` 层打桩，它不会真的被执行。"""

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


class _SequencePlanner:
    def __init__(self, *programs: str) -> None:
        self._programs = list(programs)
        self.contexts: list[DecisionContext] = []
        self.reports: list[str] = []

    async def decide(self, context: DecisionContext) -> DecisionOutput:
        self.contexts.append(context)
        return DecisionOutput(program=self._programs.pop(0))

    def report_invalid_output(self, reason: str) -> None:
        self.reports.append(reason)


def _context() -> DecisionContext:
    return DecisionContext(
        scope_key="group:1",
        correlation_id="CORR",
        tick_seq=1,
        now=NOW,
    )


class ResponseBindsOneExecutionContractTests(unittest.IsolatedAsyncioTestCase):
    """一个响应绑定一次模型执行（2026-08-21 渲染格式表 §一⑦）。

    2026-08-11 之前这里钉的是"同拍三次静态重试 + 换端点、不回灌校验拒绝"。
    整条链路已撤销：校验归注册层，内容不合规当场签发 ``agent.invalid_action``，
    **不重试、不换端点、不冷却**——写错 Python 不是端点的错。
    """

    async def test_planner_is_asked_exactly_once_per_tick(self) -> None:
        planner = _SequencePlanner("import os", "# never reached")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="INVALID_ID"),
            ),
        ):
            await loop._tick()
        self.assertEqual(len(planner.contexts), 1)

    async def test_content_error_does_not_cool_the_endpoint(self) -> None:
        """内容有误归注册层，不得回报给模型路由层冷却端点。"""
        planner = _SequencePlanner("import os")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="INVALID_ID"),
            ),
        ):
            await loop._tick()
        self.assertEqual(planner.reports, [])

    async def test_invalid_response_writes_invalid_action_and_nothing_else(
        self,
    ) -> None:
        """不合规响应只留一条 agent.invalid_action：没有决策根，也没有终态。

        ``invalid_program_giveup`` 这个 kind 随同拍重试一起消失——不存在"攒够
        三次才放弃"，reason 直接就是真实静态错误 kind。
        """
        planner = _SequencePlanner("import os")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="INVALID_ID"),
            ) as write_event,
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(return_value="FAILED"),
            ) as write_failed,
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(return_value="COMPLETED"),
            ) as write_completed,
        ):
            await loop._tick()

        write_event.assert_awaited_once()
        kwargs = write_event.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "agent.invalid_action")
        payload = kwargs["payload"]
        self.assertEqual(payload["error_kind"], "program_forbidden_construct")
        self.assertNotEqual(payload["error_kind"], "invalid_program_giveup")
        # 回灌被拒源码：模型看得见自己写错的那一段才谈得上自纠正。
        self.assertEqual(payload["raw_text"], "import os")
        self.assertIn("program_forbidden_construct", payload["reason"])
        self.assertEqual(payload["tick_seq"], loop._tick_seq)
        write_failed.assert_not_awaited()
        write_completed.assert_not_awaited()

    async def test_invalid_action_self_wakes_and_counts_toward_continuation(
        self,
    ) -> None:
        """自唤醒且计入深度——撤掉同拍重试后这是唯一的闸。"""
        planner = _SequencePlanner("import os")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        loop._continuation_max = 5
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="INVALID_ID"),
            ),
            patch.object(loop, "_arm_wake") as arm_wake,
        ):
            await loop._tick()
        arm_wake.assert_called_once()
        self.assertEqual(loop._continuation_depth, 1)

    async def test_invalid_tick_ends_as_invalid_action_not_invalid(self) -> None:
        planner = _SequencePlanner("import os")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        runtime_mock = AsyncMock(return_value="RUNTIME")
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=runtime_mock,
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="INVALID_ID"),
            ),
        ):
            await loop._tick()
        ended = [
            call
            for call in runtime_mock.await_args_list
            if call.kwargs.get("event_type") == "runtime.tick_ended"
        ]
        self.assertEqual(len(ended), 1)
        self.assertEqual(
            ended[0].kwargs["payload"]["program_status"], "invalid_action"
        )

    async def test_transport_failure_leaves_no_timeline_event(self) -> None:
        """响应没到达归模型提供层：不进时间线，尤其不能伪造成一次空程序。"""

        class _FailingPlanner:
            def __init__(self) -> None:
                self.calls = 0

            async def decide(self, context: DecisionContext) -> DecisionOutput:
                self.calls += 1
                return DecisionOutput(
                    program="", planner_error="llm_call_error:TimeoutError"
                )

        planner = _FailingPlanner()
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        runtime_mock = AsyncMock(return_value="RUNTIME")
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=runtime_mock,
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="SHOULD_NOT_BE_WRITTEN"),
            ) as write_event,
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(return_value="COMPLETED"),
            ) as write_completed,
            patch.object(loop, "_arm_wake") as arm_wake,
        ):
            await loop._tick()

        self.assertEqual(planner.calls, 1)
        write_event.assert_not_awaited()
        write_completed.assert_not_awaited()
        arm_wake.assert_not_called()
        ended = [
            call
            for call in runtime_mock.await_args_list
            if call.kwargs.get("event_type") == "runtime.tick_ended"
        ]
        self.assertEqual(
            ended[0].kwargs["payload"]["program_status"], "planner_error"
        )


class ProgramDecisionEventContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_program_writes_decision_root_with_stored_source(
        self,
    ) -> None:
        """合规响应成为一份代码资产：决策根落库，源码与指纹都取剥离后的正文。"""
        source = 'notify(message="hi")'
        planner = _SequencePlanner(source)
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=_registry(),
        )
        loop._recovery_done = True
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="DECISION_ID"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(return_value="PROGRAM_FAILED"),
            ) as write_failed,
        ):
            await loop._tick()

        self.assertEqual(len(planner.contexts), 1)
        write_decision.assert_awaited_once()
        kwargs = write_decision.await_args.kwargs
        self.assertEqual(kwargs["event_type"], "agent.decision_emitted")
        payload = kwargs["payload"]
        self.assertEqual(payload["program"], source)
        self.assertEqual(
            payload["program_sha256"],
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(payload["program_hash"], payload["program_sha256"][:12])
        write_failed.assert_not_awaited()

    async def test_runtime_failure_does_not_trigger_static_retry(self) -> None:
        """静态重试只由 preflight 失败触发。

        2026-08-14 派发拍之后这条更强了：执行整个发生在 Runner 里、`_tick`
        之外，运行时失败**结构上**不可能回到 decide。2026-08-17 起更强一层：
        提案拍连派发都不做，只落库。
        """
        planner = _SequencePlanner("return 1")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="DECISION"),
            ),
            patch.object(loop._runner, "enqueue") as enqueue,
        ):
            await loop._tick()
        self.assertEqual(len(planner.contexts), 1)
        self.assertEqual(planner.reports, [])
        enqueue.assert_not_called()

    async def test_empty_program_has_decision_and_program_terminal_but_no_idle_event(
        self,
    ) -> None:
        planner = _SequencePlanner("# intentionally idle")
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=ToolRegistry(),
        )
        loop._recovery_done = True
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="DECISION"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(return_value="PROGRAM_COMPLETED"),
            ) as write_completed,
        ):
            await loop._tick()

        write_decision.assert_awaited_once()
        write_completed.assert_awaited_once()
        completed = write_completed.await_args.kwargs
        self.assertEqual(completed["decision_id"], "DECISION")
        self.assertEqual(completed["query_calls"], [])
        self.assertEqual(completed["effect_call_ids"], [])
        self.assertFalse(completed["has_result"])


class ProposalCommitPipelineContractTests(unittest.IsolatedAsyncioTestCase):
    """提案-裁决流水线（2026-08-17）。

    钉两件事。其一是唯一的不变量：**任何有副作用的程序都不可能在模型只看过一次
    世界的情况下跑起来**——写下它的那一拍只落库，让它生效的必须是后来重新读完
    时间线的另一拍。其二是两个层级互不相干：裁决作用在别的事件上，提案是本拍新
    写的代码，一次输出里可以两者都有。
    """

    def _loop(self, *programs: str) -> tuple[AgentLoop, _SequencePlanner]:
        planner = _SequencePlanner(*programs)
        loop = AgentLoop(
            scope_key="group:1",
            planner=planner,
            session_factory=object(),
            tool_registry=_registry(),
        )
        loop._recovery_done = True
        return loop, planner

    async def test_proposal_tick_persists_source_without_dispatching(self) -> None:
        loop, _ = self._loop('notify(message="hi")')
        statuses: list[str] = []

        async def _runtime(*args: Any, **kwargs: Any) -> str:
            if kwargs["event_type"] == "runtime.tick_ended":
                statuses.append(kwargs["payload"]["program_status"])
            return "RUNTIME"

        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(side_effect=_runtime),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="DECISION"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(),
            ) as write_completed,
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(),
            ) as write_failed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        write_decision.assert_awaited_once()
        self.assertEqual(
            write_decision.await_args.kwargs["payload"]["program"],
            'notify(message="hi")',
        )
        # 一个函数都没跑：没入队，也没有任何终态。
        enqueue.assert_not_called()
        write_completed.assert_not_awaited()
        write_failed.assert_not_awaited()
        # 提案拍自己开下一拍去复核。
        wake.assert_called_once()
        self.assertEqual(statuses, ["proposed"])

    async def test_commit_tick_dispatches_the_referenced_program(self) -> None:
        loop, _ = self._loop(f'execute_program(program_hash="{TARGET_HASH}")')
        statuses: list[str] = []

        async def _runtime(*args: Any, **kwargs: Any) -> str:
            if kwargs["event_type"] == "runtime.tick_ended":
                statuses.append(kwargs["payload"]["program_status"])
            return "RUNTIME"

        referenced = ProgramAsset(
            event_id=TARGET_ID,
            correlation_id="OLD_CORR",
            program='notify(message="hi")',
            program_sha256="SHA",
            program_hash=TARGET_HASH,
        )
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(side_effect=_runtime),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="COMMIT_DECISION"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.load_program_asset",
                new=AsyncMock(return_value=(referenced, None)),
            ) as load,
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(),
            ) as write_failed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        load.assert_awaited_once()
        self.assertEqual(load.await_args.kwargs["program_hash"], TARGET_HASH)
        # 落库解耦：纯裁决剥完是空串。指令再嵌进 payload.program 就是套娃。
        stored = write_decision.await_args.kwargs["payload"]["program"]
        self.assertEqual(stored, "")
        self.assertNotIn("execute_program", stored)
        enqueue.assert_called_once()
        queued = enqueue.call_args.args[0]
        # 跑的是资产里那段源码，terminal 挂回资产落库那条事件；
        # 同时带上本拍的调度事件 ID——(调度事件, hash) 才唯一确定一次运行。
        self.assertEqual(queued.decision_id, TARGET_ID)
        self.assertEqual(queued.dispatch_event_id, "COMMIT_DECISION")
        self.assertEqual(queued.prepared.source, 'notify(message="hi")')
        # 程序的事件归属它的出处那一拍，不归属按下执行键的这一拍。
        self.assertEqual(queued.correlation_id, "OLD_CORR")
        write_failed.assert_not_awaited()
        # 唤醒交给被执行程序的 terminal 接力，本拍不自唤醒。
        wake.assert_not_called()
        self.assertEqual(statuses, ["dispatched"])

    async def test_commit_failure_lands_on_this_tick_and_wakes(self) -> None:
        """裁决报错按提案 §1.1 写 ``agent.program_failed``，挂在本拍决策上。"""
        loop, _ = self._loop(f'execute_program(program_hash="{TARGET_HASH}")')
        statuses: list[str] = []

        async def _runtime(*args: Any, **kwargs: Any) -> str:
            if kwargs["event_type"] == "runtime.tick_ended":
                statuses.append(kwargs["payload"]["program_status"])
            return "RUNTIME"

        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(side_effect=_runtime),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="COMMIT_DECISION"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.load_program_asset",
                new=AsyncMock(return_value=(None, "program_not_found")),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(return_value="FAILED"),
            ) as write_failed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        enqueue.assert_not_called()
        write_failed.assert_awaited_once()
        failed = write_failed.await_args.kwargs
        self.assertEqual(failed["decision_id"], "COMMIT_DECISION")
        self.assertEqual(failed["error_kind"], "program_not_found")
        self.assertEqual(failed["target_event_id"], TARGET_ID)
        # 报错拍没有后台任务接力，必须自己开下一拍让模型看见报错。
        wake.assert_called_once()
        self.assertEqual(statuses, ["commit_rejected"])

    async def test_pipeline_tick_dispatches_and_persists_stripped_body(self) -> None:
        """④ 流水线混合：派发历史事件的同时把**剥离指令后**的新代码落库。

        落库解耦（§1.1 防套娃）：`payload.program` 里绝不能再嵌一条
        `execute_program`，否则那段代码日后被指名时会再调度一次。
        """
        loop, _ = self._loop(
            f'execute_program(program_hash="{TARGET_HASH}")\nnotify(message="next")'
        )
        runtime_events: list[dict] = []

        async def _runtime(*args: Any, **kwargs: Any) -> str:
            runtime_events.append(kwargs)
            return "RUNTIME"

        referenced = ProgramAsset(
            event_id=TARGET_ID,
            correlation_id="OLD_CORR",
            program='notify(message="earlier")',
            program_sha256="SHA",
            program_hash=TARGET_HASH,
        )
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(side_effect=_runtime),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="MIXED_DECISION"),
            ) as write_decision,
            patch(
                "qqbot.services.agent_loop.loop.load_program_asset",
                new=AsyncMock(return_value=(referenced, None)),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_program_completed",
                new=AsyncMock(),
            ) as write_completed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        # 上层：派发被引用的那条，跑的是它自己的源码。
        enqueue.assert_called_once()
        queued = enqueue.call_args.args[0]
        self.assertEqual(queued.decision_id, TARGET_ID)
        self.assertEqual(queued.prepared.source, 'notify(message="earlier")')
        # 动作层：只有剥掉指令的纯业务代码落库，等以后被指名；此刻没有终态。
        stored = write_decision.await_args.kwargs["payload"]["program"]
        self.assertEqual(stored, 'notify(message="next")')
        self.assertNotIn("execute_program", stored)
        write_completed.assert_not_awaited()
        # 恰好唤醒一次：交给被执行程序的 terminal，本拍不自唤醒。
        wake.assert_not_called()
        ended = [
            call
            for call in runtime_events
            if call["event_type"] == "runtime.tick_ended"
        ]
        self.assertEqual(ended[0]["payload"]["program_status"], "dispatched")
        self.assertTrue(ended[0]["payload"]["left_proposal"])

    async def test_naming_a_program_that_is_not_in_the_asset_store(self) -> None:
        """空程序与纯裁决拍不进资产库，指名它们即 ``program_not_found``。

        2026-08-21：旧的 ``decision_not_a_proposal`` 并入这一类。判据从"那条
        决策的动作层有没有代码"变成"资产库里有没有这个 hash"——写入侧只在本拍
        确实留下动作层代码时才写 ``payload.program_hash``，因此空体根本进不去。
        """
        loop, _ = self._loop(f'execute_program(program_hash="{TARGET_HASH}")')
        with (
            patch(
                "qqbot.services.agent_loop.loop.write_runtime_event",
                new=AsyncMock(return_value="RUNTIME"),
            ),
            patch(
                "qqbot.services.agent_loop.program_registrar.write_agent_event",
                new=AsyncMock(return_value="COMMIT_DECISION"),
            ),
            patch(
                "qqbot.services.agent_loop.loop.load_program_asset",
                new=AsyncMock(return_value=(None, "program_not_found")),
            ),
            patch(
                "qqbot.services.agent_loop.loop.write_program_failed",
                new=AsyncMock(return_value="FAILED"),
            ) as write_failed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True),
        ):
            await loop._tick()

        enqueue.assert_not_called()
        kwargs = write_failed.await_args.kwargs
        self.assertEqual(kwargs["error_kind"], "program_not_found")
        # 拒绝回执指向的是**代码资产**，不是历史事件。
        self.assertEqual(kwargs["target_program_hash"], TARGET_HASH)

    async def test_same_asset_can_be_dispatched_more_than_once(self) -> None:
        """2026-08-21：``already_executed`` 守卫取消，同一份资产指几次跑几次。

        同源码必然同 hash 是内容寻址的应有之义。系统不拦重复调度，重复副作用
        由模型读 ``<program_result> status:ok|失败`` 自行判断（任务与决策契约 §1.-1）。
        """
        asset = ProgramAsset(
            event_id=TARGET_ID,
            correlation_id="OLD_CORR",
            program='notify(message="hi")',
            program_sha256="SHA",
            program_hash=TARGET_HASH,
        )
        enqueued: list[Any] = []
        for dispatch_id in ("COMMIT_A", "COMMIT_B"):
            loop, _ = self._loop(f'execute_program(program_hash="{TARGET_HASH}")')
            with (
                patch(
                    "qqbot.services.agent_loop.loop.write_runtime_event",
                    new=AsyncMock(return_value="RUNTIME"),
                ),
                patch(
                    "qqbot.services.agent_loop.program_registrar.write_agent_event",
                    new=AsyncMock(return_value=dispatch_id),
                ),
                patch(
                    "qqbot.services.agent_loop.loop.load_program_asset",
                    new=AsyncMock(return_value=(asset, None)),
                ),
                patch(
                    "qqbot.services.agent_loop.loop.write_program_failed",
                    new=AsyncMock(),
                ) as write_failed,
                patch.object(loop._runner, "enqueue") as enqueue,
                patch.object(loop, "_wake_continuation", return_value=True),
            ):
                await loop._tick()
            enqueue.assert_called_once()
            write_failed.assert_not_awaited()
            enqueued.append(enqueue.call_args.args[0])

        # 两次都真的入队了，没有任何守卫拦第二次。
        self.assertEqual(len(enqueued), 2)
        # 两次跑的是同一份资产，靠调度事件 ID 区分是哪一次运行。
        sources = {item.prepared.source for item in enqueued}
        self.assertEqual(sources, {'notify(message="hi")'})
        self.assertEqual(
            [item.dispatch_event_id for item in enqueued], ["COMMIT_A", "COMMIT_B"]
        )

    async def test_empty_program_closes_in_tick_and_never_wakes(self) -> None:
        """空程序是唯一的停止符：当拍收口、不唤醒，这段连续运行就结束。"""
        loop, _ = self._loop("# nothing to do")
        with (
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
                new=AsyncMock(return_value="COMPLETED"),
            ) as write_completed,
            patch.object(loop._runner, "enqueue") as enqueue,
            patch.object(loop, "_wake_continuation", return_value=True) as wake,
        ):
            await loop._tick()

        enqueue.assert_not_called()
        write_completed.assert_awaited_once()
        wake.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
