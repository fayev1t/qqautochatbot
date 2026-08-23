"""LoopSupervisor — process-wide registry and lifecycle manager for AgentLoops.

Contract:
- 事件系统设计.md §11
- EventIngest契约.md §8

Behaviour:
- One SystemAgentLoop is created up front (on start()).
- GroupAgentLoops are lazy: instantiated on first wake("group:<id>").
- PrivateAgentLoop is NOT instantiated (实例化策略 §10.1); wake() silently
  drops scope_key="private:*".
- wake() before start() is a no-op (events keep accumulating in
  agent_events; the loop will see them once it tickets).
- stop() cancels every running loop with a 5s grace timeout.

Programs are enqueued only when a later tick commits them by event id
(``execute_program``); a per-scope ProgramRunner executes them concurrently.
There is no ToolWorker, no ReplyExecutor, no pending-tool notification, and no
tool-batch completion wake.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.decision import Planner
from qqbot.services.agent_loop.loop import AgentLoop
from qqbot.services.agent_loop.projection import Projector
from qqbot.services.agent_loop.silence_watcher import SilenceWatcher
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]


class LoopSupervisor:
    def __init__(
        self,
        planner: Planner,
        session_factory: SessionFactory,
        projector: Projector | None = None,
        tool_registry: ToolRegistry | None = None,
        caption_image: Any | None = None,
    ) -> None:
        self._planner = planner
        self._session_factory = session_factory
        self._projector = projector
        self._tool_registry = tool_registry
        # 看图写描述回调（生产 = meme_caption.caption_image，由 v2_main 注入），
        # 原样转发给每个 AgentLoop 的 ProgramExecutor。
        self._caption_image = caption_image
        self._loops: dict[str, AgentLoop] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._stopped = False
        # 滚动记忆压缩器（记忆系统契约 §4）：MEMORY_COMPACTION_ENABLED
        # 打开时 start() 拉起；类型留 Any——模块惰性导入，避免默认关闭时
        # 平白拉进 LLM 依赖链。
        self._memory_compactor: Any | None = None
        # 静默叫醒（2026-08-03）：群里彻底安静满阈值时落一条事实事件并开一拍，
        # 给"回想"一个发生的时机。见 silence_watcher.py。
        self._silence_watcher: SilenceWatcher | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def loop_count(self) -> int:
        return len(self._loops)

    async def start(self) -> None:
        if self._started or self._stopped:
            return
        # 任务读模型回填已于 2026-08-21 删除（渲染格式表 §一②）：agent_tasks
        # 表随任务坍缩为单栏便签一并取消，没有读模型也就没有要回填的漂移。
        # 便签的跨窗口持久由 Projector._fetch_latest_task_note 每拍一条 LIMIT 1
        # 查询承担，不需要启动期预热。
        # 计时器由可见事实落库武装；silence_elapsed 本身不算动静。
        self._silence_watcher = SilenceWatcher(
            self._session_factory, self.wake
        )
        if self._silence_watcher.enabled:
            logger.info("[supervisor] silence watcher online")
        # MemoryCompactor（记忆系统契约 §4）：滚动折叠式场景记忆。开关
        # 默认关；启用时只挂起 worker 并给投影装推式探针。worker 启动不
        # 扫描、不 merge；只有 tick 投影报告真正触顶才会唤醒。best-effort：
        # 记忆压缩失败不能挡启动。
        try:
            from qqbot.services.agent_loop.memory_compactor import (
                MemoryCompactor,
                memory_compaction_enabled,
            )

            if memory_compaction_enabled():
                self._memory_compactor = MemoryCompactor(self._session_factory)
                self._memory_compactor.start()
                if self._projector is not None:
                    self._projector.set_uncovered_notifier(
                        self.notify_compaction
                    )
                logger.info("[supervisor] memory compactor online")
        except Exception as exc:
            logger.warning(
                "[supervisor] memory compactor start failed (continuing): {}",
                exc,
            )
        # SystemAgentLoop wakes up to handle scope=system events
        # (request.*, lifecycle, bot_offline, ...).
        await self._ensure("system")
        self._started = True
        logger.info("[supervisor] started, system loop online")

    async def stop(self) -> None:
        self._stopped = True
        loops = list(self._loops.values())
        self._loops.clear()
        await asyncio.gather(
            *(loop.stop() for loop in loops), return_exceptions=True
        )
        if self._silence_watcher is not None:
            try:
                await self._silence_watcher.stop()
            except Exception as exc:
                logger.warning("[supervisor] silence watcher stop failed: {}", exc)
            finally:
                self._silence_watcher = None
        if self._memory_compactor is not None:
            try:
                await self._memory_compactor.stop()
            except Exception as exc:
                logger.warning(
                    "[supervisor] memory compactor stop failed: {}", exc
                )
            finally:
                self._memory_compactor = None
        logger.info("[supervisor] stopped, {} loops drained", len(loops))

    def note_activity(self, scope_key: str) -> None:
        """可见时间线有新动静 → 重排该 scope 的静默计时器。

        挂在事实落库之后（announce / EventIngest 提交），不挂在 wake 上：唤醒
        只表示"该看一眼"，活动语义属于"时间线多了什么"。
        """
        if self._stopped or self._silence_watcher is None:
            return
        self._silence_watcher.notify_activity(scope_key)

    async def wake(self, scope_key: str) -> None:
        """唤醒某个 scope 的 loop。一律走固定攒批窗口（默认 3s）。

        第一条开窗，窗口内后续唤醒并入本窗、不顺延 deadline。静默计时器不在
        这里武装——见 ``note_activity``。
        """
        if self._stopped:
            return
        if scope_key.startswith("private:"):
            # 实例化策略 §10.1: private 不实例化 loop
            return
        try:
            loop = await self._ensure(scope_key)
        except ValueError:
            logger.warning("[supervisor] invalid scope_key: {}", scope_key)
            return
        loop.wake()

    def notify_compaction(self, scope_key: str, uncovered_events: int) -> None:
        """转发投影计数；压缩器只接受达到阈值的 scope。

        未启用记忆压缩时 no-op。
        """
        if self._memory_compactor is not None:
            self._memory_compactor.notify(scope_key, uncovered_events)

    async def _ensure(self, scope_key: str) -> AgentLoop:
        async with self._lock:
            existing = self._loops.get(scope_key)
            if existing is not None:
                return existing
            loop = AgentLoop(
                scope_key=scope_key,
                planner=self._planner,
                session_factory=self._session_factory,
                projector=self._projector,
                supervisor=self,
                bot_user_id_resolver=_default_bot_user_id_resolver,
                tool_registry=self._tool_registry,
                caption_image=self._caption_image,
            )
            loop.start()
            self._loops[scope_key] = loop
            logger.info("[supervisor] loop spawned: {}", scope_key)
            return loop


def _default_bot_user_id_resolver() -> str | None:
    """单 bot 部署的默认 resolver：从 bot_registry 取第一个已注册 self_id。

    多账号场景（同一进程同时注册多个 Bot 实例）下应当按 scope_key 选合适的
    bot——比如这个群里 bot A 是成员、bot B 不是——但目前 v2 还没有 scope →
    bot 的路由表，先用单 bot 假设兜底，等真有多账号需求时再细化。

    返回 None 时（启动初期，nonebot 还没把 Bot 注册进来）AgentLoop 把
    bot_user_id 保持为 None，prompt 渲染层不输出该属性；此时 LLM 仍可靠别人
    <reply ... from_self="true"/> 的服务端标注识别"这条是回复我的"——这是降级而非错误。
    """
    ids = bot_registry.all_self_ids()
    return ids[0] if ids else None
