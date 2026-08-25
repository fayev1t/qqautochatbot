"""agent_memes 分群收藏 → 全局收藏 的一次性迁移脚本（2026-07-06）。

背景（表情包工具黑盒设计.md §2）：表情包收藏夹改为当前账号内跨聊天 scope 共享后，
代码只读写 scope_key='global' 哨兵行；本脚本把存量的分群行合并进哨兵
scope——同一 file_hash 在多个群各有一条时，保留 created_at 最早的那条。
脚本幂等，跑多次结果一致。

用法（在服务器项目根目录）：
    python scripts/migrate_memes_to_global.py             # 预览 + 交互确认
    python scripts/migrate_memes_to_global.py --yes       # 不询问直接提交
    python scripts/migrate_memes_to_global.py --dry-run   # 只演练，必定回滚

整个迁移（INSERT 合并 + DELETE 旧行 + 结果校验）在**单个事务**里执行，
确认前/演练时一律 ROLLBACK，数据库不会留下半截状态。
DATABASE_URL 经 qqbot.core.database 加载（settings.py 统一读 .env，
本脚本不自己碰 dotenv）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 从 scripts/ 直接执行时把仓库根目录挂上 sys.path，qqbot 才可导入
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import text  # noqa: E402

GLOBAL_SCOPE = "global"

_MIGRATE_SQL = text(
    """
    INSERT INTO agent_memes
        (scope_key, file_hash, description, context_note, mime,
         source_event_id, created_at)
    SELECT DISTINCT ON (file_hash)
        :global_scope, file_hash, description, context_note, mime,
        source_event_id, created_at
    FROM agent_memes
    ORDER BY file_hash, created_at ASC
    ON CONFLICT DO NOTHING
    """
)

_DELETE_SQL = text("DELETE FROM agent_memes WHERE scope_key <> :global_scope")

_COUNT_BY_SCOPE_SQL = text(
    "SELECT scope_key, COUNT(*) AS n FROM agent_memes "
    "GROUP BY scope_key ORDER BY scope_key"
)


async def _print_scope_counts(conn, title: str) -> dict[str, int]:
    rows = (await conn.execute(_COUNT_BY_SCOPE_SQL)).all()
    counts = {row.scope_key: row.n for row in rows}
    print(f"\n{title}")
    if not counts:
        print("  （表为空）")
    for scope_key, n in counts.items():
        print(f"  {scope_key:<20} {n} 条")
    return counts


async def migrate(*, dry_run: bool, assume_yes: bool) -> int:
    # 导入即建 engine（会读 .env 并打印数据库配置日志）
    from qqbot.core.database import engine

    try:
        return await _migrate_with_engine(
            engine, dry_run=dry_run, assume_yes=assume_yes
        )
    finally:
        await engine.dispose()


async def _migrate_with_engine(engine, *, dry_run: bool, assume_yes: bool) -> int:
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            before = await _print_scope_counts(conn, "迁移前各 scope 行数：")
            non_global = sum(
                n for scope, n in before.items() if scope != GLOBAL_SCOPE
            )
            if non_global == 0:
                print("\n没有需要迁移的分群行，表已是全局形态，无事可做。")
                await trans.rollback()
                return 0

            inserted = (
                await conn.execute(
                    _MIGRATE_SQL, {"global_scope": GLOBAL_SCOPE}
                )
            ).rowcount
            deleted = (
                await conn.execute(
                    _DELETE_SQL, {"global_scope": GLOBAL_SCOPE}
                )
            ).rowcount
            print(f"\n合并进 '{GLOBAL_SCOPE}' 的新行：{inserted} 条")
            print(f"删除的旧分群行：{deleted} 条")
            print(
                f"（旧分群行 {non_global} 条 - 新增 {inserted} 条 = "
                f"{non_global - inserted} 条是同图多群/已收录的重复，被合并）"
            )

            after = await _print_scope_counts(conn, "迁移后各 scope 行数：")
            leftovers = {
                scope: n for scope, n in after.items() if scope != GLOBAL_SCOPE
            }
            if leftovers:
                # 事务内校验：迁移后不应残留任何非哨兵行
                print(f"\n❌ 校验失败，仍有非 global 行：{leftovers}，已回滚。")
                await trans.rollback()
                return 1

            if dry_run:
                print("\n--dry-run：演练结束，已回滚，数据库未改动。")
                await trans.rollback()
                return 0

            if not assume_yes:
                answer = input("\n确认提交以上迁移？[y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("已取消，回滚，数据库未改动。")
                    await trans.rollback()
                    return 0

            await trans.commit()
            print("\n✅ 迁移已提交。")
        except BaseException:
            await trans.rollback()
            raise
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="agent_memes 分群收藏合并为全局收藏（幂等，单事务）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="完整演练并打印结果，但必定回滚，不改动数据库",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认直接提交（供非交互环境使用）",
    )
    args = parser.parse_args()
    return asyncio.run(migrate(dry_run=args.dry_run, assume_yes=args.yes))


if __name__ == "__main__":
    sys.exit(main())
