"""Long-running per-scope loop for program-shaped Planner decisions.

Each tick projects the scope, asks the Planner for exactly one restricted
Python program, and hands that single response to the registrar
(``program_registrar``), which validates it and registers it as either a code
asset or an invalid action. **One response binds one model execution**: there
is no same-tick retry, no endpoint failover on content errors, and no
validation feedback rewrite (2026-08-21 渲染格式表 §一⑦).

模型每拍的输出在结构上解耦为**两层**（2026-08-17 提案-裁决流水线 §1.0）：

- **裁决层**（调度元指令）：``execute_program(program_hash=…)``，告诉调度器把某份
  历史决策事件提交给 Runner 执行。至多一条，可以没有。
- **动作层**（业务程序代码）：这一拍新写的 Python 代码，当拍一个函数都不跑，
  只作为新事件落库。

两层完全正交，四种组合都合法::

    ① 两层皆空     写 program_completed，**不唤醒** —— 唯一停止符   (completed)
    ② 纯提案       动作层落库，不派发；本拍自己再开一拍去审阅       (proposed)
    ③ 纯裁决       派发被引用的那条决策，等它的 terminal 接力       (dispatched)
    ④ 流水线混合   派发 + 新代码落库；唤醒同样交给 terminal 接力    (dispatched)

落库解耦（§1.1 防套娃）：preflight 把裁决指令从源码里剥掉，
``decision_emitted.payload.program`` 只存纯业务代码。

由此没有任何一段有副作用的程序会在模型只看过一次世界的情况下跑起来：写下它的那
一拍和让它生效的那一拍之间，必然隔着一次重新读时间线。④ 让这次多出来的推理被
摊掉——稳态下每拍既确认上一段又写下一段。

A per-scope ProgramRunner runs committed programs concurrently (one coroutine
each, calls inside one program stay sequential) and wakes the loop when a
program terminal is written.

**Every** wake goes through the same fixed batching window — external ingest,
proposal self-wake and Runner completion alike. The first wake opens one bounded
window; later wakes join it without extending the deadline, so split QQ messages
can land before the next decision starts. That window is exactly what makes the
extra tick worth having: the half sentence a human is still typing arrives in it.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_env_value
from qqbot.core.time import china_now
from qqbot.services.agent_loop.decision import (
    DecisionContext,
    DecisionOutput,
    Planner,
)
from qqbot.services.agent_loop.event_writer import write_runtime_event
from qqbot.services.agent_loop.program_ast import (
    ProgramPreflightError,
    preflight,
)
from qqbot.services.agent_loop.program_events import (
    load_program_asset,
    recover_interrupted_programs,
    write_program_completed,
    write_program_failed,
)
from qqbot.services.agent_loop.program_registrar import register_model_response
from qqbot.services.agent_loop.program_runner import ProgramRunner, QueuedProgram
from qqbot.services.agent_loop.program_runtime import (
    ProgramExecutionError,
    ProgramExecutor,
    ProgramTrace,
)
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)

# 唤醒攒批窗口（2026-07-28 引入，2026-08-01 由滑动改固定，见模块 docstring）。
# 第一次唤醒开窗，窗口内的后续唤醒并入本窗、不顺延 deadline，到点开一拍——
# 开拍延迟因此天然有界（≤ 窗口本身），不再需要防饿死的封顶常量。
_WAKE_BATCH_WINDOW_SECONDS = 3.0

# 自续拍（2026-08-04）：一段程序只要真的调用过函数，本拍收尾后立刻再开一拍。
#
# 动机——程序是**盲写**的：写下它的那一刻结果还不存在。文法虽有 if/for，那只
# 够对结果做机械分支；「读懂 search_history 拿回来的二十条再决定说什么」这类
# 需要判断力的事，当拍无论如何写不出来。而在此之前，那下一拍除非群里恰好又有
# 人说话否则永远不来，于是所有查完要接着办的链路都断在原地。
#
# 终止条件是不动点：某一拍的程序一个函数都没调用（空程序、或只有赋值与注释），
# 链条自然结束——恰好就是「没什么可做」的既有输出形态，不需要新概念。
#
# 抑制规则一条都没有（2026-08-04 明确决定）：`wait` 这类自带定时器的调用同样
# 续拍，她可能反复改期或反复记任务自转，守住这条的只有提示词纪律。
# 2026-08-17 起自续拍的口径扩大：提案拍与裁决报错拍也走它（那两种拍没有后台任务
# 替它们唤醒），因此一次「提案→裁决→执行→再决策」正常就要吃掉 3 层深度——
# 配这个 env 时按此估算，别照旧按「一次查询一层」算。
# 2026-08-21 再扩一次：注册层拦下的非法行动同样自续（§一⑦ 回灌被拒源码让模型
# 重写）。挡住「错→回灌→再错」的是**提示词纪律**——planner.md 要求她连着两条
# <invalid_action> 就交空程序停下（维护者裁定：约束大模型用提示词，不新造机制；
# 与 2026-08-04 对 wait 自转的处理同源）。本 env 仍是部署侧兜底，`.env.example`
# 给了 12 当安全网。
_CONTINUATION_MAX_ENV = "AGENT_CONTINUATION_MAX_TICKS"

SessionFactory = Callable[[], AsyncSession]


def continuation_max_ticks() -> int | None:
    """连续自续拍上界。env ``AGENT_CONTINUATION_MAX_TICKS``。

    未配置 / 空 → ``None``，即**不限制**；``0`` → 关掉自续拍，退回纯事件驱动；
    ``N > 0`` → 一段活动内最多连续自续 N 拍，之后必须等一次真正的外部唤醒。
    计数被任何外部唤醒清零，因此约束的是「一次自转能有多长」，不是「一小时能
    跑多少拍」。

    留这个旋钮是因为默认无界：真在生产里自转起来时，部署侧不改代码就能收。
    它是**兜底**不是主闸——同拍静态重试撤销后，一次语法错就是一整拍 + 一次自
    唤醒 + 一层深度，而挡住「错→回灌→再错」的是 planner.md 里那条纪律（连着
    两条 ``<invalid_action>`` 就交空程序停下）。系统一侧不替大模型数错误次数：
    ``<invalid_action>`` 是时间线事实事件，她自己看得见有几条。
    """
    raw = get_env_value(_CONTINUATION_MAX_ENV)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning(
            "[loop] invalid {}={!r}, treating as unlimited",
            _CONTINUATION_MAX_ENV,
            raw,
        )
        return None
    return max(value, 0)


class AgentLoop:
    def __init__(
        self,
        scope_key: str,
        planner: Planner,
        session_factory: SessionFactory,
        projector: Projector | None = None,
        supervisor: Any | None = None,
        bot_user_id_resolver: Callable[[], str | None] | None = None,
        tool_registry: ToolRegistry | None = None,
        caption_image: Any | None = None,
    ) -> None:
        self._scope_key = scope_key
        self._planner = planner
        self._session_factory = session_factory
        self._projector = projector
        # supervisor 鸭子类型注入，规避 supervisor → loop 的循环 import；
        # 程序内 wait 等工具仍用它的 wake / note_activity 回调。
        self._supervisor = supervisor
        # bot_user_id 每 tick 重新 resolve —— bot 重连后 self_id 不变但实例
        # 会换；启动初期可能返回 None，prompt 渲染层接受 None 优雅降级。
        # None resolver 表示不注入（旧测试 / 早期骨架兼容）。
        self._bot_user_id_resolver = bot_user_id_resolver
        # Registry 是唯一 Program API。权限/scope/role 判定仍全部下放工具内
        # BaseTool.enforce_access；空 registry 只允许空程序与安全 builtin。
        self._tool_registry = (
            tool_registry if tool_registry is not None else ToolRegistry()
        )
        # 可选的图片描述依赖会随 ProgramExecutor context 注入工具。
        self._caption_image = caption_image
        self._wake = asyncio.Event()
        self._stopped = False
        self._tick_seq = 0
        self._task: asyncio.Task[None] | None = None
        # 攒批窗口状态：deadline 是当前窗口的到点时刻，开窗时算一次、窗口内
        # 不再变动（None = 当前没开窗，下一次 wake() 负责开）；timer 是正在睡
        # 到那个时刻的协程，同一时刻至多一个。
        self._wake_deadline: float | None = None
        self._wake_timer: asyncio.Task[None] | None = None
        # 每个 loop 实例的第一拍、投影之前收口一次历史半截程序；成功后不重跑。
        self._recovery_done = False
        # Runner 完成 wake 计入自续拍深度；外部 wake 清零。上界仍是
        # AGENT_CONTINUATION_MAX_TICKS，防止「跑完→决策→再入队」自转。
        self._continuation_depth = 0
        self._continuation_max = continuation_max_ticks()
        self._runner = ProgramRunner(
            scope_key=scope_key,
            execute=self._run_queued_program,
            on_finished=self._wake_continuation,
        )

    @property
    def scope_key(self) -> str:
        return self._scope_key

    def start(self) -> None:
        if self._task is not None:
            return
        self._runner.start()
        self._task = asyncio.create_task(
            self._run(), name=f"agent_loop:{self._scope_key}"
        )

    async def stop(self) -> None:
        self._stopped = True
        self._cancel_wake_timer()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None
        await self._runner.stop()

    def wake(self) -> None:
        """请求开拍。外部入口，走固定攒批窗口（见模块 docstring）。

        EventIngest / wait 到点 / 静默叫醒都经此入口。顺带把自续拍计数清零：
        外面又有事发生，上一段自转到此为止。自续拍走 ``_wake_continuation``，
        不经过这里——差别只在计数，不在窗口。
        """
        if self._stopped:
            return
        self._continuation_depth = 0
        self._arm_wake()

    def _wake_continuation(self) -> bool:
        """本 loop 自己刚落了事实、需要再开一拍。返回是否真的排上。

        三个来源：决策拍自己写完提案 / 裁决报错、注册层拦下非法行动（2026-08-21
        新增，回灌被拒源码让模型重写），以及 Runner 写出 program terminal。
        **与外部唤醒同一条窗口**（2026-08-17 维护者裁定）：所有落库
        的事件都走 3 秒攒批窗，没有旁路——决策事件和别的事件一样，凭什么它
        引发的那次唤醒可以插队。这三秒不是等待成本，正是人补完后半句的时间，
        跳过它就等于让下一拍照旧看不见新消息。

        与 ``wake()`` 的唯一区别是计数：自转不清零，受
        ``AGENT_CONTINUATION_MAX_TICKS`` 约束。
        """
        if self._stopped:
            return False
        if self._continuation_max is not None:
            if self._continuation_depth >= self._continuation_max:
                if self._continuation_max > 0:
                    logger.info(
                        "[loop {}] continuation capped at {} tick(s)",
                        self._scope_key,
                        self._continuation_max,
                    )
                return False
        self._continuation_depth += 1
        self._arm_wake()
        return True

    def _arm_wake(self) -> None:
        """唤醒排程本体。不碰自续拍计数——由两个入口各自负责。

        没有 immediate 旁路：唤醒只有这一条路径。``_WAKE_BATCH_WINDOW_SECONDS
        <= 0`` 是测试用的关窗档。
        """
        if _WAKE_BATCH_WINDOW_SECONDS <= 0:
            self._cancel_wake_timer()
            self._wake_deadline = None
            self._wake.set()
            return

        if self._wake_deadline is not None:
            # 窗口已开：本次唤醒并入这一拍，**不**顺延 deadline。
            return
        self._wake_deadline = time.monotonic() + _WAKE_BATCH_WINDOW_SECONDS
        if self._wake_timer is None or self._wake_timer.done():
            self._wake_timer = asyncio.create_task(
                self._wake_after_window(),
                name=f"agent_loop_wake:{self._scope_key}",
            )

    async def _wake_after_window(self) -> None:
        """睡到窗口到点再置位。deadline 在开窗那一刻就定死、窗口内不会被后续
        唤醒推后，所以只睡一次即可（旧的滑动实现要在这里重读 deadline 续睡）。
        """
        deadline = self._wake_deadline
        if deadline is None:
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._wake_deadline = None
        self._wake.set()

    def _cancel_wake_timer(self) -> None:
        timer, self._wake_timer = self._wake_timer, None
        if timer is not None and not timer.done():
            timer.cancel()

    async def _run(self) -> None:
        logger.info("[loop {}] started", self._scope_key)
        try:
            while not self._stopped:
                await self._wake.wait()
                self._wake.clear()
                if self._stopped:
                    break
                try:
                    await self._tick()
                except Exception as exc:
                    logger.exception(
                        "[loop {}] tick failed: {}", self._scope_key, exc
                    )
        finally:
            logger.info("[loop {}] stopped", self._scope_key)

    async def _tick(self) -> None:
        self._tick_seq += 1
        correlation_id = new_event_id()
        now = china_now()
        tick_started_id = await write_runtime_event(
            self._session_factory,
            event_type="runtime.tick_started",
            scope_key=self._scope_key,
            visibility="runtime_only",
            correlation_id=correlation_id,
            causation_id=None,
            payload={"tick_seq": self._tick_seq},
        )
        if not self._recovery_done:
            report = await recover_interrupted_programs(
                self._session_factory,
                scope_key=self._scope_key,
            )
            self._recovery_done = True
            if report.tool_calls_closed:
                logger.warning(
                    "[loop {}] recovered interrupted tool call(s): {}",
                    self._scope_key,
                    report.tool_calls_closed,
                )

        context = await self._build_context(correlation_id, now)
        decision = await self._decide_program(context)
        if decision is None or decision.planner_error:
            # 响应根本没到达（网络失败/端点超时/上游拒绝/返回空）。这归模型
            # 提供层：由它自己重试、换端点、最终放弃，**不进时间线**。本拍
            # 无输出，仅此而已——尤其不能把它落成一次空程序，那会把一次网络
            # 故障伪造成"她这拍选择了沉默"（渲染格式表 §一⑦ 失败分层）。
            if decision is not None:
                logger.warning(
                    "[loop {}] planner returned no usable response: {}",
                    self._scope_key,
                    decision.planner_error,
                )
            await self._write_tick_ended(
                correlation_id,
                tick_started_id,
                program_status="planner_error",
            )
            return

        # 生产路径：outbound.invoke 已经把响应送进入口网关，适配器做过预检。
        # Stub planner / 未接线时 decision.event_id 为空，走降级直写。
        if decision.event_id:
            from qqbot.services.agent_loop.program_registrar import (
                RegisteredResponse,
            )

            registered = RegisteredResponse(
                event_id=decision.event_id,
                accepted=bool(decision.accepted),
                program=decision.program,
                program_sha256=decision.program_sha256 or "",
                prepared=decision.prepared,
                left_asset=decision.left_asset,
            )
        else:
            registered = await register_model_response(
                self._session_factory,
                scope_key=self._scope_key,
                scope=self._scope_key.split(":", 1)[0],
                correlation_id=correlation_id,
                raw_program=decision.program,
                registry=self._tool_registry,
                tick_seq=self._tick_seq,
                now=now,
            )
        if not registered.accepted:
            # 被拒源码已随 agent.invalid_action 回灌进时间线；本拍没有 decision
            # root，也不写任何 program terminal。自唤醒让模型看着自己写错的那段
            # 重写一遍——它**计入** AGENT_CONTINUATION_MAX_TICKS，撤掉同拍重试后
            # 那个上界是这套自纠正循环唯一的闸。
            self._wake_continuation()
            await self._write_tick_ended(
                correlation_id,
                tick_started_id,
                program_status="invalid_action",
            )
            return

        prepared = registered.prepared
        assert prepared is not None  # accepted 蕴含 prepared 非空
        decision_id = registered.event_id
        program_sha256 = registered.program_sha256
        left_proposal = registered.left_asset

        # 两层各自独立结算（§1.0）：裁决层调度的是**别的**事件，动作层是本拍新写
        # 的代码，同一次输出里可以两者都有，也可以只有一个、一个都没有。
        commit_outcome: str | None = None
        if prepared.commit_program_hash is not None:
            commit_outcome = await self._commit_program(
                commit_decision_id=decision_id,
                commit_program_sha256=program_sha256,
                target_program_hash=prepared.commit_program_hash,
                correlation_id=correlation_id,
                context=context,
                now=now,
            )

        if commit_outcome is None and not left_proposal:
            # 两层都空 = 停止符：当拍收口，**不**唤醒，这段连续运行到此为止。
            await _shield_write(
                write_program_completed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    decision_id=decision_id,
                    program_sha256=program_sha256,
                    duration_ms=0,
                    query_calls=[],
                    effect_call_ids=[],
                    result=None,
                    has_result=prepared.has_return,
                )
            )
            await self._write_tick_ended(
                correlation_id,
                tick_started_id,
                program_status="completed",
                left_proposal=False,
            )
            return

        # 每个非空拍恰好唤醒一次：派发成功了就等被执行程序的 terminal 接力，
        # 否则本拍自己再开一拍（提案要有人来复核，裁决报错要让模型看见）。
        if commit_outcome != "dispatched":
            self._wake_continuation()
        await self._write_tick_ended(
            correlation_id,
            tick_started_id,
            program_status=commit_outcome or "proposed",
            left_proposal=left_proposal,
        )

    async def _commit_program(  # noqa: PLR0913
        self,
        *,
        commit_decision_id: str,
        commit_program_sha256: str,
        target_program_hash: str,
        correlation_id: str,
        context: DecisionContext,
        now: datetime,
    ) -> str:
        """裁决层：把被指名的那份**代码资产**交给 Runner 真正执行。

        2026-08-21 起裁决按 ``program_hash`` 寻址：``event_id`` 命名时间线上的
        历史事实事件，``program_hash`` 命名不可变的代码逻辑资产，两个值域分工
        明确。``execute_program`` 表达的是"调度执行某段具体的代码资产"，而不是
        "重新执行当年的某个事件"。

        资产里存的本来就是纯业务代码（preflight 落库前已剥掉裁决层），因此这里
        重新 preflight 一遍拿到的必然 ``commit_program_hash is None``，不存在
        套娃。

        被执行程序沿用资产落库那一拍的 correlation_id：那些 ``tool_called`` /
        terminal 是这段程序的事件，归属它的出处，不归属按下执行键的这一拍。

        **没有 already_executed 守卫**：同源码即同资产，一份资产调度几次跑几次，
        系统不拦，重复副作用由模型读 ``<program_result>`` 自行判断（任务与决策
        契约 §1.-1）。派发前只剩 ``program_not_found`` 一条运行期检查。

        成功时本拍**不写**任何 program terminal——终态属于被执行的那段程序，
        并带上 ``(dispatch_event_id, program_hash)`` 双键：取消守卫后同一份资产
        可以合法并发跑多次，只凭 hash 分不出是哪一次运行。
        唤醒由调用方统一处理。
        """
        asset, error_kind = await load_program_asset(
            self._session_factory,
            scope_key=self._scope_key,
            program_hash=target_program_hash,
        )
        if asset is None:
            await self._reject_commit(
                decision_id=commit_decision_id,
                program_sha256=commit_program_sha256,
                correlation_id=correlation_id,
                error_kind=error_kind or "program_not_found",
                error_message=_COMMIT_REJECTION_MESSAGES.get(
                    error_kind or "",
                    f"cannot execute program {target_program_hash}",
                ),
                target_program_hash=target_program_hash,
            )
            return "commit_rejected"

        scope = self._scope_key.split(":", 1)[0]
        try:
            target = preflight(asset.program, self._tool_registry, scope)
        except ProgramPreflightError as exc:
            # 存量源码现在过不了预检——工具下线、scope 权限变了都会这样。
            # 报的是被指名程序自己的错，不是裁决语法错。
            await self._reject_commit(
                decision_id=commit_decision_id,
                program_sha256=commit_program_sha256,
                correlation_id=correlation_id,
                error_kind=exc.info.error_kind,
                error_message=(
                    f"program {target_program_hash} no longer passes "
                    f"preflight: {exc.info.message}"
                ),
                target_program_hash=target_program_hash,
            )
            return "commit_rejected"

        try:
            self._runner.enqueue(
                QueuedProgram(
                    decision_id=asset.event_id,
                    scope_key=self._scope_key,
                    correlation_id=asset.correlation_id or correlation_id,
                    prepared=target,
                    context=context,
                    enqueued_at=now,
                    dispatch_event_id=commit_decision_id,
                )
            )
        except Exception as exc:
            logger.exception("[loop {}] enqueue failed: {}", self._scope_key, exc)
            # 这一条终态写在**资产落库**的那条决策上，而不是本拍：这次派发真的
            # 没成，带上双键让模型看得出是哪一次调度失败的。资产本身仍在库里，
            # 可以再次指名——取消 already_executed 后终态不阻止重新调度。
            await _shield_write(
                write_program_failed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=asset.correlation_id or correlation_id,
                    decision_id=asset.event_id,
                    program_sha256=target.program_sha256,
                    program_hash=asset.program_hash,
                    dispatch_event_id=commit_decision_id,
                    duration_ms=0,
                    query_calls=[],
                    effect_call_ids=[],
                    error_kind="dispatch_failed",
                    error_message=f"program enqueue failed: {type(exc).__name__}",
                )
            )
            return "failed"
        return "dispatched"

    async def _reject_commit(
        self,
        *,
        decision_id: str,
        program_sha256: str,
        correlation_id: str,
        error_kind: str,
        error_message: str,
        target_program_hash: str,
    ) -> None:
        """裁决被拒：本拍写 ``agent.program_failed``（提案 §1.1）。

        附 ``target_program_hash=`` 而不是 ``target_event_id=``（2026-08-21）：
        裁决指向的是代码资产，不是历史事件。

        这条终态扣在**本拍**的决策事件上；④ 混合拍里本拍同时承载动作层新代码。
        2026-08-17 曾登记过一条已知副作用——``already_executed`` 会因此把那段
        新代码一起判死。守卫取消后该因果链断裂：终态扣在哪个事件上都不再影响
        任何资产能否被调度（待办 #21 随之关闭）。
        """
        await _shield_write(
            write_program_failed(
                self._session_factory,
                scope_key=self._scope_key,
                correlation_id=correlation_id,
                decision_id=decision_id,
                program_sha256=program_sha256,
                duration_ms=0,
                query_calls=[],
                effect_call_ids=[],
                error_kind=error_kind,
                error_message=error_message,
                target_program_hash=target_program_hash,
            )
        )

    async def _build_context(
        self,
        correlation_id: str,
        now: datetime,
    ) -> DecisionContext:
        bot_user_id: str | None = None
        if self._bot_user_id_resolver is not None:
            try:
                resolved = self._bot_user_id_resolver()
                if resolved is not None:
                    bot_user_id = str(resolved)
            except Exception as exc:
                logger.warning(
                    "[loop {}] bot_user_id_resolver failed: {}",
                    self._scope_key,
                    exc,
                )
        if self._projector is not None:
            try:
                return await self._projector.build_context(
                    scope_key=self._scope_key,
                    correlation_id=correlation_id,
                    tick_seq=self._tick_seq,
                    now=now,
                    bot_user_id=bot_user_id,
                )
            except Exception as exc:
                logger.exception(
                    "[loop {}] projection failed; using empty context: {}",
                    self._scope_key,
                    exc,
                )
        return DecisionContext(
            scope_key=self._scope_key,
            correlation_id=correlation_id,
            tick_seq=self._tick_seq,
            now=now,
            bot_user_id=bot_user_id,
        )

    async def _decide_program(
        self,
        context: DecisionContext,
    ) -> DecisionOutput | None:
        """问一次模型，返回它这一次的响应。**只问一次。**

        2026-08-21（渲染格式表 §一⑦）删掉了同拍三次静态重试链路：校验不再在
        这里发生，preflight 归注册层（``program_registrar``），而"一个响应绑定
        一次模型执行"意味着内容有误时不换端点重生成——那不是端点的错，冷却它
        既无益也不诚实（旧的 ``report_invalid_output`` 调用点随之撤销）。

        返回 ``None`` 只代表 Planner 自身抛了异常；传输类失败由 Planner 填
        ``planner_error`` 表达。两者都归模型提供层，都不进时间线。
        """
        try:
            return await self._planner.decide(context)
        except Exception as exc:
            logger.exception("[loop {}] planner failed: {}", self._scope_key, exc)
            return None

    async def _run_queued_program(self, item: QueuedProgram) -> None:
        """Runner 回调：顺序执行一段已 preflight 的程序并写 program terminal。"""
        executor = ProgramExecutor(
            registry=self._tool_registry,
            session_factory=self._session_factory,
            scope_key=self._scope_key,
            correlation_id=item.correlation_id,
            decision_id=item.decision_id,
            context=item.context,
            supervisor=self._supervisor,
            caption_image=self._caption_image,
        )
        prepared = item.prepared
        try:
            result = await executor.execute(prepared)
        except ProgramExecutionError as exc:
            trace = exc.trace or ProgramTrace(
                decision_id=item.decision_id,
                program_sha256=prepared.program_sha256,
                scope_key=self._scope_key,
            )
            details = dict(exc.info.details)
            if exc.info.line is not None:
                details["line"] = exc.info.line
            if exc.info.column is not None:
                details["column"] = exc.info.column
            await _shield_write(
                write_program_failed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=item.correlation_id,
                    decision_id=item.decision_id,
                    program_sha256=prepared.program_sha256,
                    program_hash=prepared.program_hash,
                    dispatch_event_id=item.dispatch_event_id,
                    duration_ms=trace.duration_ms,
                    query_calls=list(trace.query_calls),
                    effect_call_ids=list(trace.effect_call_ids),
                    error_kind=exc.info.error_kind,
                    error_message=exc.info.message,
                    failed_call=exc.failed_call_payload(),
                    **details,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[loop {}] program host failure escaped executor", self._scope_key
            )
            await _shield_write(
                write_program_failed(
                    self._session_factory,
                    scope_key=self._scope_key,
                    correlation_id=item.correlation_id,
                    decision_id=item.decision_id,
                    program_sha256=prepared.program_sha256,
                    program_hash=prepared.program_hash,
                    dispatch_event_id=item.dispatch_event_id,
                    duration_ms=0,
                    query_calls=[],
                    effect_call_ids=[],
                    error_kind="internal_tool_error",
                    error_message=f"program host failure: {type(exc).__name__}",
                )
            )
            return

        await _shield_write(
            write_program_completed(
                self._session_factory,
                scope_key=self._scope_key,
                correlation_id=item.correlation_id,
                decision_id=item.decision_id,
                program_sha256=prepared.program_sha256,
                program_hash=prepared.program_hash,
                dispatch_event_id=item.dispatch_event_id,
                duration_ms=result.trace.duration_ms,
                query_calls=list(result.trace.query_calls),
                effect_call_ids=list(result.trace.effect_call_ids),
                result=result.result,
                has_result=result.has_result,
            )
        )

    async def _write_tick_ended(
        self,
        correlation_id: str,
        tick_started_id: str,
        program_status: str,
        left_proposal: bool = False,
    ) -> None:
        """``program_status`` 记裁决层的结果，``left_proposal`` 记动作层有没有
        留下新代码——两层独立，一拍可以同时有。"""
        await write_runtime_event(
            self._session_factory,
            event_type="runtime.tick_ended",
            scope_key=self._scope_key,
            visibility="runtime_only",
            correlation_id=correlation_id,
            causation_id=tick_started_id,
            payload={
                "tick_seq": self._tick_seq,
                "program_status": program_status,
                "left_proposal": left_proposal,
            },
        )


# 裁决失败的说明文本。它会作为 ``<program_result> status:failed`` 的 ``reason:``
# 行进入信封，是模型唯一能看到的纠正依据——写成让人一眼知道下一步该干什么的话。
_COMMIT_REJECTION_MESSAGES = {
    "program_not_found": (
        "no such program in this scope; copy the 12-hex hash from a program row "
        "in the current timeline"
    ),
}


async def _shield_write(awaitable: Any) -> None:
    """Let a terminal transaction finish even if loop shutdown cancels the tick."""
    task = asyncio.create_task(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()
