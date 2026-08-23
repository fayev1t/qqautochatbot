"""每日群聊背景 —— ``agent.background_noted``。

设计动机（2026-08-21，渲染格式表 §一①）：群名、群号、本账号在这个群的昵称与
群角色，原先是信封头部每拍重算的折叠快照。头部的毛病不是不准，而是它**不在
时间流上**——模型读到的永远是"现在这个群叫什么"，读不到"从什么时候起它叫这个"。
改成事实事件之后，这几项和别的事实同权：有发生时刻、有先后、由 ``<t>`` 头承载。

代价是滞后。群名改了、角色升了，下一条 ``<background>`` 要等到明天 00:00，而
群名变更目前没有对应的通知行去纠偏。这条已在契约里挂账（事件系统设计 §4.9
已知缺口①），本轮不补。**滞后的只是给模型看的提示，不是权限判定**：工具在
调用时另行实时复查真实角色（``tool_registry._effective_bot_role``），快照过期
不会误判。

触发（维护者裁定 2026-08-21：走调度器，不做每拍懒加载）：

  - APScheduler 每日 00:00 跑一次，给 bot 所在的每个群各写一条；
  - ``lifecycle.connect`` 再补一次，兜住"00:00 那会儿进程是关着的"——否则那一
    整天一条 ``<background>`` 都没有，比原来的常驻头部还差。

两条入口共用同一个幂等判据：**该群今天已经有一条就跳过**。所以重连风暴、跨零点
重启、手动重跑都不会写重。

**不叫醒、也不重排静默计时器。** 它是背景，不是动静：00:00 给每个群平白开一拍
纯属噪音；``note_activity`` 更糟——半夜把所有群的静默计时器重新武装一遍，十分钟
后一串 ``silence_elapsed`` 会真的把她叫起来说话。这条规则落在静默门里
（``event_gateway/silence_gate.py``），不靠本模块的调用点自觉。

群集合取"bot 当前所在的全部群"，不筛"活跃群"：没有 loop 的群本来就没人读这条，
写进去无害；而一个刚进的新群，它的第一拍反倒因此就有背景可读。

Contracts:
- 开发文档/v2.0/20-横切契约/事件系统设计.md §4.9
- 开发文档/v2.0/事件流渲染格式表.md §一① / §五1
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.ids import new_event_id
from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.models.agent_event import AgentEvent
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.event_writer import write_internal_event

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

BACKGROUND_EVENT_TYPE = "agent.background_noted"

# APScheduler job id：固定值 + replace_existing，重复注册覆盖而不叠加。
JOB_ID = "daily_background"

_WEEKDAY_NAMES = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)

_VALID_ROLES = frozenset({"owner", "admin", "member"})


def background_date(now: datetime | None = None) -> tuple[str, str]:
    """当天 → ``("2026-08-21", "星期五")``。

    两栏刻意分开存：``date`` 保持纯 ISO 日期，它是幂等判据的键；星期只是给人
    读的附注。拼成一个字符串会让"今天写过没有"这个判断依赖本地化文案。
    """
    moment = now or china_now()
    return moment.strftime("%Y-%m-%d"), _WEEKDAY_NAMES[moment.weekday()]


async def run_daily_background(
    session_factory: SessionFactory,
    *,
    bot: Any | None = None,
    now: datetime | None = None,
) -> int:
    """给（指定 bot 或全部已连接 bot 的）每个群补上今天的背景行。

    返回真正写入的条数。单群失败不阻塞其他群；整个 bot 取不到群列表时返回 0。
    """
    date, weekday = background_date(now)
    bots = [bot] if bot is not None else _connected_bots()
    if not bots:
        logger.info("[daily_background] no bot connected; nothing to write")
        return 0
    written = 0
    for one in bots:
        written += await _run_for_bot(
            session_factory, one, date=date, weekday=weekday
        )
    return written


def register_daily_background_job(
    session_factory: SessionFactory,
    *,
    scheduler: Any | None = None,
) -> None:
    """挂上每日 00:00 的注入任务。调度器时区在 ``init_scheduler`` 里已设为北京。

    ``misfire_grace_time`` 给一小时：进程在零点前后卡住时，晚一点写仍然有意义
    （这一天的背景总比没有强）。``coalesce`` 让积压的多次触发只补一次。
    """
    from qqbot.core.scheduler import get_scheduler

    sched = scheduler if scheduler is not None else get_scheduler()
    sched.add_job(
        run_daily_background,
        trigger="cron",
        hour=0,
        minute=0,
        second=0,
        id=JOB_ID,
        replace_existing=True,
        kwargs={"session_factory": session_factory},
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info("[daily_background] daily 00:00 job registered")


def schedule_catch_up_from_meta(
    bot: Any,
    event: Any | None,
    session_factory: SessionFactory,
) -> None:
    """已落库的 ``lifecycle.connect`` → 补写今天的背景；其它元事件忽略。

    同步函数，内部 fire-and-forget（形态同 ``bot_role_sweep.schedule_sweep``）：
    napcat 主循环不该为一次补写等待。异常一律吞掉——补写失败最多是今天少一条
    背景行，不值得把消息处理路径拖下水。
    """
    try:
        if event is None or getattr(event, "type", None) != "external.meta.lifecycle":
            return
        payload = getattr(event, "payload", None) or {}
        if str(payload.get("sub_type") or "").strip().lower() != "connect":
            return
        asyncio.create_task(
            _catch_up(bot, session_factory),
            name=f"daily_background:{getattr(bot, 'self_id', '?')}",
        )
    except RuntimeError:
        logger.warning("[daily_background] no running loop; catch-up skipped")
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        logger.warning("[daily_background] catch-up scheduling swallowed: {}", exc)


# ─── internals ───


async def _catch_up(bot: Any, session_factory: SessionFactory) -> None:
    try:
        await run_daily_background(session_factory, bot=bot)
    except Exception as exc:  # noqa: BLE001 —— fire-and-forget，日志是唯一兜底
        logger.warning("[daily_background] catch-up failed: {}", exc)


def _connected_bots() -> list[Any]:
    bots: list[Any] = []
    for self_id in bot_registry.all_self_ids():
        one = bot_registry.get(self_id)
        if one is not None:
            bots.append(one)
    return bots


async def _run_for_bot(
    session_factory: SessionFactory,
    bot: Any,
    *,
    date: str,
    weekday: str,
) -> int:
    self_id = str(getattr(bot, "self_id", "") or "")
    if not self_id:
        logger.warning("[daily_background] bot.self_id is empty, skipping")
        return 0

    try:
        groups = await bot.call_api("get_group_list")
    except Exception as exc:  # noqa: BLE001 —— 拿不到群列表就整轮跳过
        logger.warning("[daily_background] get_group_list failed: {}", exc)
        return 0
    if not isinstance(groups, list):
        logger.warning(
            "[daily_background] get_group_list returned non-list: {}", type(groups)
        )
        return 0

    written = 0
    for entry in groups:
        group_id = _group_id_of(entry)
        if group_id is None:
            continue
        try:
            if await _already_noted(session_factory, group_id, date):
                continue
        except Exception as exc:  # noqa: BLE001 —— 判据查不了就别写，宁可少一条
            logger.warning(
                "[daily_background] {} idempotency check failed: {}", group_id, exc
            )
            continue
        # 成员信息拿不到不是跳过的理由：群名与群号本身就够写一条了，昵称与
        # 角色缺失按通则一读作"未知"，比整条不写更接近事实。
        info = await _member_info(bot, group_id, self_id)
        try:
            await _write_background(
                session_factory,
                group_id=group_id,
                group_name=_group_name_of(entry),
                self_group_nick_name=_self_card_of(info),
                group_role=_role_of(info),
                self_id=self_id,
                date=date,
                weekday=weekday,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 —— 单群失败不阻塞其他群
            logger.exception("[daily_background] write {} failed: {}", group_id, exc)

    logger.info(
        "[daily_background] self_id={} {} groups → wrote {} for {}",
        self_id,
        len(groups),
        written,
        date,
    )
    return written


async def _already_noted(
    session_factory: SessionFactory, group_id: int, date: str
) -> bool:
    """本群今天是否已经有一条。

    只取最近一条比对 ``payload.date``，不把条件下推成 JSONB 表达式：这是每天
    一次的低频查询，走现成的 ``(scope, group_id, occurred_at)`` 索引就够，不值得
    为它新开一个表达式索引。取最近一条即可判定，因为本模块只写"今天"。
    """
    stmt = (
        select(AgentEvent.payload)
        .where(AgentEvent.scope == "group")
        .where(AgentEvent.group_id == group_id)
        .where(AgentEvent.type == BACKGROUND_EVENT_TYPE)
        .order_by(AgentEvent.occurred_at.desc())
        .limit(1)
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        payload = result.scalars().first()
    if not isinstance(payload, dict):
        return False
    return payload.get("date") == date


async def _member_info(bot: Any, group_id: int, self_id: str) -> dict | None:
    try:
        info = await bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=int(self_id),
            no_cache=True,
        )
    except Exception as exc:  # noqa: BLE001 —— 降级为"昵称与角色未知"
        logger.warning(
            "[daily_background] get_group_member_info({}) failed: {}", group_id, exc
        )
        return None
    return info if isinstance(info, dict) else None


async def _write_background(  # noqa: PLR0913 —— 全是要落进 payload 的平铺字段
    session_factory: SessionFactory,
    *,
    group_id: int,
    group_name: str | None,
    self_group_nick_name: str | None,
    group_role: str | None,
    self_id: str,
    date: str,
    weekday: str,
) -> str:
    payload: dict[str, Any] = {
        "group_id": group_id,
        "group_name": group_name,
        "self_group_nick_name": self_group_nick_name,
        "group_role": group_role,
        "date": date,
        "weekday": weekday,
        "self_id": self_id,
    }
    # correlation_id 用新号：每日背景自成一条因果链起点，不挂在任何一拍上
    # （同 bot_role_sweep._write_role_observed）。
    return await write_internal_event(
        session_factory,
        origin="agent",
        event_type=BACKGROUND_EVENT_TYPE,
        scope_key=f"group:{group_id}",
        visibility="agent_visible",
        correlation_id=new_event_id(),
        causation_id=None,
        payload=payload,
    )


def _group_id_of(entry: Any) -> int | None:
    if not isinstance(entry, dict):
        return None
    gid = entry.get("group_id")
    if gid is None:
        return None
    try:
        return int(gid)
    except (TypeError, ValueError):
        return None


def _group_name_of(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("group_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _self_card_of(info: Any) -> str | None:
    """群名片；为空时退回账号昵称——那正是群里别人看到的名字。"""
    if not isinstance(info, dict):
        return None
    for key in ("card", "nickname"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _role_of(info: Any) -> str | None:
    if not isinstance(info, dict):
        return None
    role = info.get("role")
    if not isinstance(role, str):
        return None
    role = role.strip().lower()
    return role if role in _VALID_ROLES else None


__all__ = [
    "BACKGROUND_EVENT_TYPE",
    "JOB_ID",
    "background_date",
    "register_daily_background_job",
    "run_daily_background",
    "schedule_catch_up_from_meta",
]
