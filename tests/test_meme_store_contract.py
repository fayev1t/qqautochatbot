"""Contract tests for meme_store（agent_memes 表情包收藏读写）。

Covers（表情包工具黑盒设计.md §存储；2026-07-06 起当前账号内跨聊天 scope 共享）：
- 当前账号内共享：所有读写固定用哨兵 scope_key = MEME_SCOPE_GLOBAL（事件系统设计
  §11.3 例外），insert 落 'global'、get/load 按 'global' 过滤。
- insert_meme：INSERT ... ON CONFLICT (scope_key, file_hash) DO NOTHING；
  rowcount=1 → True（新插入），rowcount=0 → False（已存在，调用方折
  already_saved，**不覆盖**）；values 完整落所有列。
- get_meme：按 hash 单条精确查 → MemeView（含 context_note 留档回读，供
  `meme_collection(action="recaption")` 沿用旧语境）；未命中 → None。
- load_saved_memes：created_at 倒序 + LIMIT（语句面断言）；UTC 时间
  normalize 到北京时间。
- delete_meme（2026-07-12）：DELETE 按哨兵 scope + hash 过滤；rowcount → bool。
- update_meme_description（2026-07-12）：UPDATE 只动 description /
  context_note 两列（created_at 等收录事实不动）；rowcount → bool（0 =
  并发被删，调用方折 unknown_meme）。

不打真实 DB：recording / stub session 捕获语句，postgresql dialect 编译，
与其它读侧契约测试同式。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete, Insert, Update

from qqbot.services.agent_loop.meme_store import (
    MAX_SAVED_MEMES,
    MEME_SCOPE_GLOBAL,
    delete_meme,
    get_meme,
    insert_meme,
    load_saved_memes,
    update_meme_description,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 7, 3, 12, 0, 0, tzinfo=SHANGHAI)
HASH_A = "ab" * 32
HASH_B = "cd" * 32


# ─── fakes ───


class _Result:
    def __init__(self, rows: list[Any] | None = None, rowcount: int = 1) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self) -> "_Result":
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _StubSession:
    """execute 捕获语句；select 回固定行、DML（insert/update/delete）回固定
    rowcount。"""

    def __init__(self, owner: "_StubDB") -> None:
        self._owner = owner

    async def execute(self, stmt: Any) -> _Result:
        self._owner.statements.append(stmt)
        if isinstance(stmt, (Insert, Update, Delete)):
            return _Result(rowcount=self._owner.dml_rowcount)
        return _Result(rows=self._owner.select_rows)

    async def commit(self) -> None:
        self._owner.commits += 1

    async def __aenter__(self) -> "_StubSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _StubDB:
    def __init__(
        self,
        select_rows: list[Any] | None = None,
        dml_rowcount: int = 1,
    ) -> None:
        self.select_rows = list(select_rows or [])
        self.dml_rowcount = dml_rowcount
        self.statements: list[Any] = []
        self.commits = 0

    def factory(self) -> _StubSession:
        return _StubSession(self)


def _meme_row(
    *,
    file_hash: str = HASH_A,
    description: str = "黑猫瞪眼，嘲讽用",
    created_at: datetime | None = None,
    context_note: str | None = "张三的名场面",
) -> SimpleNamespace:
    """伪 AgentMeme ORM row（_row_to_meme_view 只读这些属性）。"""
    return SimpleNamespace(
        file_hash=file_hash,
        description=description,
        created_at=created_at or BASE,
        context_note=context_note,
    )


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


class InsertMemeTests(unittest.IsolatedAsyncioTestCase):
    async def _insert(self, db: _StubDB) -> bool:
        return await insert_meme(
            db.factory,
            file_hash=HASH_A,
            description="黑猫瞪眼，嘲讽用",
            context_note="张三的名场面",
            mime="image/png",
            source_event_id="TC_EVENT",
            created_at=BASE,
        )

    async def test_insert_writes_all_columns_with_conflict_target(self) -> None:
        db = _StubDB()
        inserted = await self._insert(db)
        self.assertTrue(inserted)
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.statements), 1)
        stmt = db.statements[0]
        self.assertIsInstance(stmt, Insert)
        params = stmt.compile(dialect=postgresql.dialect()).params
        # 当前账号内共享：scope_key 固定落哨兵，不接受调用方传入
        self.assertEqual(params["scope_key"], MEME_SCOPE_GLOBAL)
        self.assertEqual(params["file_hash"], HASH_A)
        self.assertEqual(params["description"], "黑猫瞪眼，嘲讽用")
        self.assertEqual(params["context_note"], "张三的名场面")
        self.assertEqual(params["mime"], "image/png")
        self.assertEqual(params["source_event_id"], "TC_EVENT")
        self.assertEqual(params["created_at"], BASE)
        # 冲突语义：ON CONFLICT (主键) DO NOTHING —— 重复收藏不覆盖
        sql = _sql(stmt)
        self.assertIn("ON CONFLICT (scope_key, file_hash) DO NOTHING", sql)

    async def test_insert_conflict_returns_false(self) -> None:
        db = _StubDB(dml_rowcount=0)
        inserted = await self._insert(db)
        self.assertFalse(inserted)


class GetMemeTests(unittest.IsolatedAsyncioTestCase):
    async def test_hit_maps_row_to_view(self) -> None:
        db = _StubDB(select_rows=[_meme_row()])
        view = await get_meme(db.factory, HASH_A)
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual(view.file_hash, HASH_A)
        self.assertEqual(view.description, "黑猫瞪眼，嘲讽用")
        self.assertEqual(view.saved_at, BASE)
        # context_note 留档回读：recaption 未带新语境时沿用它
        self.assertEqual(view.context_note, "张三的名场面")

    async def test_miss_returns_none(self) -> None:
        db = _StubDB(select_rows=[])
        view = await get_meme(db.factory, HASH_A)
        self.assertIsNone(view)

    async def test_statement_filters_global_sentinel(self) -> None:
        # 当前账号内共享的实现锚点：查询按哨兵 scope_key 过滤（未迁移的旧分群行
        # 不可见），而不是不带 scope 条件裸查 hash。
        db = _StubDB(select_rows=[])
        await get_meme(db.factory, HASH_A)
        params = db.statements[0].compile(dialect=postgresql.dialect()).params
        self.assertIn(MEME_SCOPE_GLOBAL, params.values())


class DeleteMemeTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_filters_global_sentinel_and_hash(self) -> None:
        db = _StubDB()
        deleted = await delete_meme(db.factory, HASH_A)
        self.assertTrue(deleted)
        self.assertEqual(db.commits, 1)
        self.assertEqual(len(db.statements), 1)
        stmt = db.statements[0]
        self.assertIsInstance(stmt, Delete)
        params = stmt.compile(dialect=postgresql.dialect()).params
        # 与 get_meme 同：按哨兵 scope + hash 过滤，不裸删 hash
        self.assertIn(MEME_SCOPE_GLOBAL, params.values())
        self.assertIn(HASH_A, params.values())

    async def test_missing_row_returns_false(self) -> None:
        db = _StubDB(dml_rowcount=0)
        deleted = await delete_meme(db.factory, HASH_A)
        self.assertFalse(deleted)


class UpdateMemeDescriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_touches_only_description_and_note(self) -> None:
        db = _StubDB()
        updated = await update_meme_description(
            db.factory,
            file_hash=HASH_A,
            description="白猫叹气，摆烂用",
            context_note="李四的名场面",
        )
        self.assertTrue(updated)
        self.assertEqual(db.commits, 1)
        stmt = db.statements[0]
        self.assertIsInstance(stmt, Update)
        compiled = stmt.compile(dialect=postgresql.dialect())
        self.assertEqual(compiled.params["description"], "白猫叹气，摆烂用")
        self.assertEqual(compiled.params["context_note"], "李四的名场面")
        # 收录事实不动：created_at / mime / source_event_id 不在 SET 列里
        sql = _sql(stmt)
        set_clause = sql.split("WHERE")[0]
        self.assertNotIn("created_at", set_clause)
        self.assertNotIn("mime", set_clause)
        self.assertNotIn("source_event_id", set_clause)
        # 与 get_meme 同：按哨兵 scope + hash 过滤
        self.assertIn(MEME_SCOPE_GLOBAL, compiled.params.values())
        self.assertIn(HASH_A, compiled.params.values())

    async def test_missing_row_returns_false(self) -> None:
        # rowcount=0 = 并发被删，调用方折 unknown_meme
        db = _StubDB(dml_rowcount=0)
        updated = await update_meme_description(
            db.factory,
            file_hash=HASH_A,
            description="白猫叹气，摆烂用",
            context_note=None,
        )
        self.assertFalse(updated)


class LoadSavedMemesTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_rows_and_normalizes_timezone(self) -> None:
        utc_row = _meme_row(
            file_hash=HASH_B,
            created_at=datetime(2026, 7, 3, 4, 0, 0, tzinfo=timezone.utc),
        )
        db = _StubDB(select_rows=[_meme_row(), utc_row])
        views = await load_saved_memes(db.factory)
        self.assertEqual([v.file_hash for v in views], [HASH_A, HASH_B])
        # UTC 04:00 → 北京 12:00（asyncpg 读回 UTC，读侧统一 normalize）
        self.assertEqual(views[1].saved_at.tzinfo, SHANGHAI)
        self.assertEqual(views[1].saved_at.hour, 12)

    async def test_statement_orders_desc_and_limits(self) -> None:
        db = _StubDB(select_rows=[])
        await load_saved_memes(db.factory)
        sql = _sql(db.statements[0])
        self.assertIn("ORDER BY agent_memes.created_at DESC", sql)
        self.assertIn("LIMIT", sql)
        # 与 get_meme 同：全局哨兵过滤
        params = db.statements[0].compile(dialect=postgresql.dialect()).params
        self.assertIn(MEME_SCOPE_GLOBAL, params.values())

    async def test_default_limit_is_capped(self) -> None:
        # 表情包收藏是每 tick 注入的 prompt 区，默认上限必须存在且有限。
        self.assertGreater(MAX_SAVED_MEMES, 0)
        self.assertLessEqual(MAX_SAVED_MEMES, 200)


if __name__ == "__main__":
    unittest.main()
