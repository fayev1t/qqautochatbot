"""meme_store —— agent_memes 表（表情包收藏）的写入 / 读取。

定位见 models/agent_meme.py 顶部注释。

**全局共享（2026-07-06 起）**：收藏夹是全 bot 一份、所有聊天 scope 共用——
这是事件系统设计.md §11.3明确允许的例外：收藏以公共值
file_hash 为键、不携带 scope 上下文，与图片文件落盘缓存同类。实现方式是
scope_key 列固定写 MEME_SCOPE_GLOBAL 哨兵（列保留，表结构不变，主键
(scope_key, file_hash) 退化为全局一图一条；将来要恢复分域只改本模块）。
存量分群数据需一次性迁移合并到哨兵 scope，见 表情包工具黑盒设计.md §2。

对外接口（全部独立事务、每次新开 session）：

  get_meme(factory, hash)          单条精确查。发送（send_messages 的 meme
                                   气泡）的权限边界（只有收录过的才能发）与
                                   收录（meme.save）的去重前查都走它。
  insert_meme(factory, ...)        收录一条。ON CONFLICT DO NOTHING：
                                   并发重复保存时后到者静默不覆盖，
                                   返回 False（调用方按 already_saved
                                   处理），收录语义不做 UPDATE。
  load_saved_memes(factory)        取全局收藏夹（created_at 倒序、封顶
                                   MAX_SAVED_MEMES 条），Projector 渲染
                                   表情包收藏节用。
  delete_meme(factory, hash)       移除一条收藏（meme.delete，2026-07-12）。
                                   只删 agent_memes 元数据，**不动磁盘
                                   文件**——媒体文件是 EventIngest 的内容
                                   寻址缓存，归将来的媒体 GC 管（黑盒设计
                                   §7）。返回 False=本就不存在。
  update_meme_description(...)     换描述 + 语境留档（meme.recaption，
                                   2026-07-12）。只动 description /
                                   context_note 两列，created_at 保持收录
                                   时间不变。返回 False=该 hash 不存在
                                   （并发被删）。

本表是**可变操作状态表**（黑盒设计 §2），UPDATE/DELETE 不违反 agent_events
的 append-only 硬规矩——审计链在 agent.tool_called/tool_result 事件里。

失败语义：本模块**不吞异常** —— DB 失败原样 raise，由调用方决定降级（工具层
经 BaseTool.run 兜底成 internal_tool_error；投影层 try/except 降级为本 tick 不
渲染收藏夹）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import CHINA_TIMEZONE
from qqbot.models.agent_meme import AgentMeme
from qqbot.services.agent_loop.decision import MemeView

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

# 全局收藏的哨兵 scope_key：所有读写固定用它，事件系统设计 §11.3 例外的
# 显式标注就落在这里。列本身保留（见模块 docstring）。
MEME_SCOPE_GLOBAL = "global"

# 表情包收藏节渲染上限：收藏夹随时间增长，prompt 只带最近收录的 N 条。
# 超限的老表情包仍在表里（凭 hash 仍可作为 meme 气泡发送），只是不再进 prompt。
# 全局共享后所有群共用一个池，比分群时代放宽到 100。
MAX_SAVED_MEMES = 100


async def get_meme(
    session_factory: SessionFactory, file_hash: str
) -> MemeView | None:
    """按 file_hash 精确查一条全局收藏；不存在 → None。"""
    stmt = (
        select(AgentMeme)
        .where(AgentMeme.scope_key == MEME_SCOPE_GLOBAL)
        .where(AgentMeme.file_hash == file_hash)
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        row = result.scalars().first()
    return _row_to_meme_view(row) if row is not None else None


async def find_meme_by_prefix(
    session_factory: SessionFactory, hash_prefix: str
) -> tuple[MemeView | None, bool]:
    """按 hash 前缀（≥12 位十六进制，调用方已校验）唯一匹配一条全局收藏。

    Part 3 §2.2：信封展示 12 位前缀，工具回填按前缀查。返回
    ``(命中的收藏, 是否多义)``：
    - 唯一命中 → ``(view, False)``；
    - 无命中 → ``(None, False)``；
    - 多条命中 → ``(None, True)``（48 bit 前缀碰撞概率可忽略，但语义封死，
      调用方报 ``ambiguous_hash_prefix``）。
    完整 64 位输入等价精确查（LIKE 前缀即全等）。前缀已限定 hex 字符集，
    无 ``%``/``_`` 注入面。
    """
    if len(hash_prefix) == 64:
        return await get_meme(session_factory, hash_prefix), False
    stmt = (
        select(AgentMeme)
        .where(AgentMeme.scope_key == MEME_SCOPE_GLOBAL)
        .where(AgentMeme.file_hash.like(f"{hash_prefix}%"))
        .limit(2)
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
    if not rows:
        return None, False
    if len(rows) > 1:
        return None, True
    return _row_to_meme_view(rows[0]), False


async def insert_meme(
    session_factory: SessionFactory,
    *,
    file_hash: str,
    description: str,
    context_note: str | None,
    mime: str,
    source_event_id: str | None,
    created_at: datetime,
) -> bool:
    """收录一条表情包（进全局收藏夹）。返回 True=新插入，False=已存在
    （并发重复保存）。

    ON CONFLICT DO NOTHING 而非 upsert：收藏是一次性事实，重复保存不刷新
    描述/时间（想换描述属于将来的 recaption 特性，不混进收录语义）。
    """
    stmt = (
        pg_insert(AgentMeme)
        .values(
            scope_key=MEME_SCOPE_GLOBAL,
            file_hash=file_hash,
            description=description,
            context_note=context_note,
            mime=mime,
            source_event_id=source_event_id,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["scope_key", "file_hash"])
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def delete_meme(
    session_factory: SessionFactory, file_hash: str
) -> bool:
    """移除一条全局收藏。返回 True=删了一条，False=本就不存在（或并发已删）。

    只删 agent_memes 元数据，不碰 runtime_data/media 下的文件——文件是
    EventIngest 的内容寻址缓存，删除收藏后它只是失去"被收藏钉住"的身份
    （黑盒设计 §7），归将来的媒体 GC 处置。
    """
    stmt = (
        delete(AgentMeme)
        .where(AgentMeme.scope_key == MEME_SCOPE_GLOBAL)
        .where(AgentMeme.file_hash == file_hash)
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def update_meme_description(
    session_factory: SessionFactory,
    *,
    file_hash: str,
    description: str,
    context_note: str | None,
) -> bool:
    """换一条收藏的描述（recaption 落表）。返回 True=更新成功，False=该
    hash 不存在（并发被删，调用方按 unknown_meme 处理）。

    只动 description + context_note（新语境同步留档，供下次 recaption 沿用）；
    created_at / mime / source_event_id 保持收录时的值——created_at 语义是
    "收录时间"而非"最后修改时间"，收藏节排序不因换描述而跳动。
    """
    stmt = (
        update(AgentMeme)
        .where(AgentMeme.scope_key == MEME_SCOPE_GLOBAL)
        .where(AgentMeme.file_hash == file_hash)
        .values(description=description, context_note=context_note)
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        await session.commit()
    return bool(getattr(result, "rowcount", 0))


async def load_saved_memes(
    session_factory: SessionFactory,
    *,
    limit: int = MAX_SAVED_MEMES,
) -> list[MemeView]:
    """取全局收藏夹，created_at 倒序（最新收录在前）、封顶 limit 条。"""
    stmt = (
        select(AgentMeme)
        .where(AgentMeme.scope_key == MEME_SCOPE_GLOBAL)
        .order_by(AgentMeme.created_at.desc())
        .limit(limit)
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
    return [_row_to_meme_view(r) for r in rows]


def _row_to_meme_view(row: Any) -> MemeView:
    """AgentMeme ORM row → MemeView（时区 normalize 见 _norm_china）。"""
    return MemeView(
        file_hash=row.file_hash,
        description=row.description or "",
        saved_at=_norm_china(row.created_at),
        context_note=row.context_note,
    )


def _norm_china(dt: datetime | None) -> datetime:
    """asyncpg 把 TIMESTAMPTZ 读回 UTC tzinfo；统一 normalize 到北京时间，
    与 projection._snapshot_from_row 保持一致。"""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(CHINA_TIMEZONE)
    return dt  # type: ignore[return-value]
