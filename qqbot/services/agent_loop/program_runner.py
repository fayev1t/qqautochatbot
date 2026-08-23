"""Per-scope program dispatch: decision ticks enqueue, this runner executes.

拍间并行、拍内串行（2026-08-14）：决策拍只入队，本 Runner 为每段程序开一条
独立协程，同 scope 的多段程序**可以同时在跑**；单段程序内部仍由
ProgramExecutor 按源码顺序逐个调用，这一层不变。

为什么不是 FIFO 串行：慢调用全是只读的（websearch / webfetch /
look_at_image，秒~数十秒），副作用调用全是亚秒级的。串行队列会让"她想说的
那句话"排在一次网页检索后面，而重构的目标恰恰是长 I/O 不再堵住这个 scope。

代价明说：同 scope 不再有"至多一段程序在做副作用"的结构保证。旧的并行时代
（ToolWorker）靠 ``delivery_claims`` 的 claim-with-lease 兜重复投递，那套已
退役且不得复活。现在只保留两条：

- ``outbound_messages.send_all`` 的 per-scope 互斥，保证同一次 send_messages
  的气泡不被另一段程序劈开（只管连续性，不判重、不认领、不设 TTL）；
- "要不要再说一次"仍由下一拍模型对着 ``<action>`` 与 ``<tool>`` 终态判断。
  2026-08-21 起这条更吃重：``already_executed`` 守卫取消后，同一份代码资产
  指几次就跑几次，系统一侧再没有任何东西替模型拦住重复。

并发上限 ``AGENT_PROGRAM_MAX_CONCURRENCY`` 防的是 fan-out（每段程序都可能挂着
HTTP + LLM 调用），不是防重复发言；置 1 即退回串行。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from qqbot.core.logging import get_logger
from qqbot.core.settings import get_env_value
from qqbot.services.agent_loop.decision import DecisionContext
from qqbot.services.agent_loop.program_ast import PreflightResult

logger = get_logger(__name__)

_MAX_CONCURRENCY_ENV = "AGENT_PROGRAM_MAX_CONCURRENCY"
_DEFAULT_MAX_CONCURRENCY = 4

# 关停时给在飞程序的收尾余量。超时未结束的一律 cancel，缺失的 program
# terminal 由下次启动的收口器写成 interrupted/uncertain（永不重放）。
_STOP_GRACE_SECONDS = 5.0


def program_max_concurrency() -> int:
    """同 scope 最多几段程序并行。env ``AGENT_PROGRAM_MAX_CONCURRENCY``。

    未配置 / 空 / 非法 → 默认 4；``1`` = 退回串行。不提供"不限"档：每段程序
    都可能挂着 HTTP 与 LLM 调用，突发几拍就能开出十几路。
    """
    raw = get_env_value(_MAX_CONCURRENCY_ENV)
    if raw is None or not str(raw).strip():
        return _DEFAULT_MAX_CONCURRENCY
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning(
            "[runner] invalid {}={!r}, using {}",
            _MAX_CONCURRENCY_ENV,
            raw,
            _DEFAULT_MAX_CONCURRENCY,
        )
        return _DEFAULT_MAX_CONCURRENCY
    if value < 1:
        logger.warning(
            "[runner] {}={} must be >= 1, using {}",
            _MAX_CONCURRENCY_ENV,
            value,
            _DEFAULT_MAX_CONCURRENCY,
        )
        return _DEFAULT_MAX_CONCURRENCY
    return value


@dataclass(frozen=True)
class QueuedProgram:
    decision_id: str
    scope_key: str
    correlation_id: str
    prepared: PreflightResult
    context: DecisionContext
    enqueued_at: datetime
    # 下达 execute_program 的那条决策事件（2026-08-21）。terminal 靠
    # (dispatch_event_id, program_hash) 唯一确定一次运行——取消
    # already_executed 后同一份资产可以合法并发跑多次，只凭 hash 分不出
    # 是哪一次。空程序在自己那一拍收口、没有调度事件，故可为 None。
    dispatch_event_id: str | None = None


ExecuteQueued = Callable[[QueuedProgram], Awaitable[None]]
WakeFinished = Callable[[], bool]


class ProgramRunner:
    """并发执行本 scope 的已派发程序；单段程序内部仍是顺序执行。"""

    def __init__(
        self,
        *,
        scope_key: str,
        execute: ExecuteQueued,
        on_finished: WakeFinished,
        max_concurrency: int | None = None,
    ) -> None:
        self._scope_key = scope_key
        self._execute = execute
        self._on_finished = on_finished
        if max_concurrency is None:
            max_concurrency = program_max_concurrency()
        self._max_concurrency = max_concurrency
        # Semaphore 在 3.10+ 延迟绑定事件循环，构造期不在 loop 里也安全。
        self._sem = asyncio.Semaphore(self._max_concurrency)
        self._inflight: set[asyncio.Task[None]] = set()
        self._stopped = False

    @property
    def queue_depth(self) -> int:
        """在飞 + 等待信号量的程序数（观测值，不写事件 ABI）。"""
        return len(self._inflight)

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def start(self) -> None:
        """保留给 AgentLoop.start()；本 Runner 无常驻消费协程。"""
        self._stopped = False

    def enqueue(self, item: QueuedProgram) -> None:
        if self._stopped:
            raise RuntimeError("program runner is stopped")
        task = asyncio.create_task(
            self._run_one(item),
            name=f"program:{self._scope_key}:{item.decision_id}",
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        logger.info(
            "[runner {}] dispatched decision={} inflight={} max={}",
            self._scope_key,
            item.decision_id,
            len(self._inflight),
            self._max_concurrency,
        )

    async def stop(self) -> None:
        self._stopped = True
        pending = [task for task in self._inflight if not task.done()]
        if not pending:
            self._inflight.clear()
            return
        _, still_running = await asyncio.wait(
            pending, timeout=_STOP_GRACE_SECONDS
        )
        for task in still_running:
            task.cancel()
        if still_running:
            logger.warning(
                "[runner {}] cancelled {} in-flight program(s) at shutdown",
                self._scope_key,
                len(still_running),
            )
            await asyncio.gather(*still_running, return_exceptions=True)
        self._inflight.clear()

    async def _run_one(self, item: QueuedProgram) -> None:
        finished = False
        try:
            async with self._sem:
                if self._stopped:
                    return
                await self._execute(item)
                finished = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # _execute 自己已经把执行器契约内外的失败都写成 program terminal；
            # 走到这里说明连兜底写入都炸了，只能留日志，等启动收口。
            logger.exception(
                "[runner {}] decision={} failed: {}",
                self._scope_key,
                item.decision_id,
                exc,
            )
            finished = True
        finally:
            if finished and not self._stopped:
                self._on_finished()


__all__ = [
    "ProgramRunner",
    "QueuedProgram",
    "program_max_concurrency",
]
