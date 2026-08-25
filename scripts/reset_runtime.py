"""一键清空运行时数据：信息流 / 记忆 / 表情包（2026-08-02）。

为什么是脚本而不是工具（上游硬规矩）：
  ``agent_events`` 是 append-only 的唯一真相源，运行中的代码**永远不许**
  UPDATE/DELETE 它。清库只能是停机时的手工运维动作，因此这段能力放在
  scripts/ 下、由维护者手动执行，绝不注册成 Planner 工具或后台清理任务。

清哪些（默认全清）：
  agent_events           信息流本体。**记忆也在这里** —— 压缩摘要是
                         runtime.context_compacted 事件（memory_compactor.py），
                         系统没有独立的记忆表，清了事件流记忆随之消失。
  raw_events             原文绕库短表。满 100 行会自己清。
  group_memories         一群一行记忆正文。
  agent_delivery_claims  历史投递协调表（现役 Program Effect 不读取）。
  agent_memes            表情包收藏元数据。
  runtime_data/media/img 落盘图片文件（表情包钉住的就是这些文件，见
                         models/agent_meme.py 媒体生命周期一节）。按 sha256
                         内容寻址，收藏图与普通图片缓存混在同一棵树里、无法
                         分开删——全清场景下这是对的；只想清收藏元数据、
                         把文件留作后续复用就加 --keep-media。

默认**不清** agent_image_captions（VLM 图片描述缓存）：它按 file_hash 寻址、
随时可重算，留着能省下重复的 VLM 调用费，且清不清都不影响历史事件的可回放性
（投影读的是事件正文里那份 desc，不查这张表）。要一起清就加 --include-captions。

完全不碰的东西：runtime_data/prompt_snapshots/、runtime_data/api_lab/、
logs/ —— 纯调试日志，与运行时状态无关，要清自己 rm。

线上遗留的 ``agent_tasks`` 表也**不在默认清单**：任务已经坍缩为单栏便签，
业务代码不再读写这张表。若维护者确实要清理这张历史孤儿表，显式加
``--include-legacy-agent-tasks``；脚本只会 TRUNCATE，不会 DROP 表。

用法（在服务器项目根目录，**先停掉 bot 进程**）：
    python scripts/reset_runtime.py                     # 预览 + 交互确认
    python scripts/reset_runtime.py --dry-run           # 只演练，必定回滚、不删文件
    python scripts/reset_runtime.py --yes               # 跳过确认（非交互环境）
    python scripts/reset_runtime.py --keep-media        # 保留磁盘图片文件
    python scripts/reset_runtime.py --include-captions  # 连图片描述缓存一起清
    python scripts/reset_runtime.py --include-legacy-agent-tasks  # 清理遗留任务表

TRUNCATE 保留表结构与索引，重启后 init_db() 幂等、无需任何迁移动作。
DATABASE_URL 经 qqbot.core.database 加载（settings.py 统一读 .env，
本脚本不自己碰 dotenv）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# 从 scripts/ 直接执行时把仓库根目录挂上 sys.path，qqbot 才可导入
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import text  # noqa: E402

# 默认清空的表，顺序即打印顺序（无外键，TRUNCATE 不存在依赖顺序问题）
_TABLES: tuple[tuple[str, str], ...] = (
    ("agent_events", "信息流 + 记忆摘要"),
    ("raw_events", "原文绕库（可丢）"),
    ("group_memories", "群记忆正文"),
    ("agent_delivery_claims", "投递租约"),
    ("agent_memes", "表情包收藏"),
)
# 仅 --include-captions 时追加
_CAPTIONS_TABLE = ("agent_image_captions", "图片描述缓存")
# 线上历史遗留表：业务代码已不再读写，默认不随运行时重置清理。
_LEGACY_TASKS_TABLE = ("agent_tasks", "历史遗留任务表（已停用）")

# 心跳文件 mtime 早于这个秒数才算"进程已停"。napcat 心跳默认 30s 一次，
# 90s 给足两次丢包的余量；这只是提醒，不阻断执行。
_HEARTBEAT_STALE_SECONDS = 90.0

_EXISTING_TABLES_SQL = text(
    """
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = ANY(:names)
    """
)


def _resolve_runtime_path(relative: Path) -> Path:
    """把 qqbot 里 cwd 相对的 runtime_data 路径钉到仓库根目录。

    生产进程从项目根启动、这些常量按 cwd 解析；脚本可能在任意目录被调用，
    统一改用仓库根为基准，避免误删/漏删另一份 runtime_data。
    """
    return relative if relative.is_absolute() else (_REPO_ROOT / relative)


def _warn_if_bot_running() -> None:
    """心跳文件仍在被刷新 = bot 进程大概率还活着，清库会被立刻写回。"""
    from qqbot.services.event_ingest.heartbeat import HEARTBEAT_FILE

    path = _resolve_runtime_path(HEARTBEAT_FILE)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return  # 文件不存在/读不到：给不出结论，不打扰
    if age < _HEARTBEAT_STALE_SECONDS:
        print(
            f"\n⚠️  {path.name} {age:.0f} 秒前刚被刷新，bot 进程可能仍在运行。\n"
            "    AgentLoop 的折叠上下文与进程内计时器都在内存里，"
            "不停机清库会被新事件立刻盖回去。\n"
            "    建议先停掉 bot 再执行。"
        )


async def _existing_tables(conn, names: list[str]) -> list[str]:
    rows = (await conn.execute(_EXISTING_TABLES_SQL, {"names": names})).all()
    return [row.table_name for row in rows]


async def _print_counts(conn, tables: list[tuple[str, str]], title: str) -> int:
    """打印各表行数，返回总行数。"""
    print(f"\n{title}")
    total = 0
    for name, note in tables:
        n = (await conn.execute(text(f"SELECT COUNT(*) FROM {name}"))).scalar_one()
        total += n
        print(f"  {name:<22} {n:>8} 条   （{note}）")
    return total


def _scan_media(media_dir: Path) -> tuple[int, int]:
    """统计 media_dir 下的文件数与总字节数（不删）。"""
    files = 0
    total = 0
    if not media_dir.exists():
        return (0, 0)
    for path in media_dir.rglob("*"):
        if path.is_file():
            files += 1
            total += path.stat().st_size
    return (files, total)


def _purge_media(media_dir: Path) -> int:
    """删除 media_dir 下所有文件，回收空分桶目录，返回实际删除数。

    刻意保留 media_dir 本身：ingest 落盘时会自行 mkdir，但留着目录能让
    "已清空" 与 "从未配置" 两种状态在文件系统上仍可区分。
    """
    deleted = 0
    for path in media_dir.rglob("*"):
        if path.is_file():
            path.unlink()
            deleted += 1
    for bucket in media_dir.iterdir():
        if bucket.is_dir():
            try:
                bucket.rmdir()
            except OSError:
                # 非空（并发写入/意外的子目录）：留着，不做递归强删
                pass
    return deleted


def _confirm() -> bool:
    try:
        answer = input("\n确认清空？此操作不可撤销，输入 yes 继续 [yes/N] ")
    except EOFError:
        print("非交互环境无法确认，已取消。需要无人值守请加 --yes。")
        return False
    return answer.strip().lower() == "yes"


async def reset(
    *,
    dry_run: bool,
    assume_yes: bool,
    keep_media: bool,
    include_captions: bool,
    include_legacy_agent_tasks: bool = False,
) -> int:
    # 导入即建 engine（会读 .env 并打印数据库配置日志）
    from qqbot.core.database import engine

    try:
        return await _reset_with_engine(
            engine,
            dry_run=dry_run,
            assume_yes=assume_yes,
            keep_media=keep_media,
            include_captions=include_captions,
            include_legacy_agent_tasks=include_legacy_agent_tasks,
        )
    finally:
        await engine.dispose()


async def _reset_with_engine(
    engine,
    *,
    dry_run: bool,
    assume_yes: bool,
    keep_media: bool,
    include_captions: bool,
    include_legacy_agent_tasks: bool = False,
) -> int:
    from qqbot.services.event_ingest.media import MEDIA_IMG_DIR

    media_dir = _resolve_runtime_path(MEDIA_IMG_DIR)
    wanted = list(_TABLES)
    if include_captions:
        wanted.append(_CAPTIONS_TABLE)
    if include_legacy_agent_tasks:
        wanted.append(_LEGACY_TASKS_TABLE)

    _warn_if_bot_running()

    async with engine.connect() as conn:
        present = await _existing_tables(conn, [name for name, _ in wanted])
        tables = [(name, note) for name, note in wanted if name in present]
        missing = [name for name, _ in wanted if name not in present]
        if missing:
            # 首次启动前 / 手工 DROP 过：不是错误，跳过即可
            print(f"\n（跳过尚不存在的表：{', '.join(missing)}）")

        if not tables:
            print("\n目标表一张都不存在，数据库侧无事可做。")

        rows_before = 0
        if tables:
            rows_before = await _print_counts(conn, tables, "将要清空的表：")

        media_files, media_bytes = (0, 0) if keep_media else _scan_media(media_dir)
        if keep_media:
            print(f"\n磁盘图片文件：保留（--keep-media），目录 {media_dir}")
        else:
            print(
                f"\n将要删除的磁盘图片文件：{media_files} 个"
                f"（{media_bytes / 1024 / 1024:.1f} MB），目录 {media_dir}"
            )
            if not include_captions:
                print(
                    "  注：agent_image_captions 保留 —— 描述不钉住文件，"
                    "文件删掉描述依旧有效（只有 look_at_image 重看会失败）。"
                )

        # 上面几条 SELECT 会 autobegin 一个隐式事务，必须在这里显式结束：
        # ① 否则下面的 conn.begin() 直接抛 InvalidRequestError（事务已开）；
        # ② 等人敲确认可能是几分钟，不该让连接一直 idle in transaction ——
        #    既压着 vacuum，也占着这些表上的 ACCESS SHARE 锁，而 TRUNCATE 要
        #    的正是与之冲突的 ACCESS EXCLUSIVE。无事务时本调用是空操作。
        await conn.rollback()

        if rows_before == 0 and media_files == 0:
            print("\n运行时数据已经是空的，无事可做。")
            return 0

        if dry_run:
            print("\n--dry-run：以上为演练结果，数据库与文件均未改动。")
            return 0

        if not assume_yes and not _confirm():
            print("已取消，数据库与文件均未改动。")
            return 0

        # 先提交数据库、后删文件。反过来的话，一旦 TRUNCATE 失败，收藏元数据
        # 会指向已被删除的文件（meme 发送时 media_file_missing）；而当前顺序
        # 最坏只留下无人引用的孤儿文件——内容寻址缓存，无害。
        if tables:
            trans = await conn.begin()
            try:
                # TRUNCATE 要 ACCESS EXCLUSIVE 锁：bot 还在跑的话，这里会无限期
                # 排队等它的连接放手，同时把 bot 的后续查询一并堵在锁队列后面。
                # 设上超时，宁可快速失败并提示去停机，也不要挂死拖垮线上。
                await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
                joined = ", ".join(name for name, _ in tables)
                await conn.execute(text(f"TRUNCATE {joined}"))
                await _print_counts(conn, tables, "清空后（事务内校验）：")
                await trans.commit()
            except BaseException as exc:
                await trans.rollback()
                if "lock_timeout" in str(exc) or "lock timeout" in str(exc).lower():
                    print(
                        "\n❌ 拿不到表锁（10 秒超时），数据库与文件均未改动。\n"
                        "    多半是 bot 进程还连着库 —— 先停掉它再重跑本脚本。"
                    )
                    return 1
                raise
            print(f"\n✅ 已清空 {len(tables)} 张表，共 {rows_before} 条。")

    if not keep_media and media_files:
        deleted = _purge_media(media_dir)
        print(f"✅ 已删除磁盘图片文件 {deleted} 个。")

    print("\n完成。重启 bot 即可 —— init_db() 幂等，TRUNCATE 未动表结构，无需迁移。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一键清空运行时数据：信息流 / 记忆 / 表情包（停机时手工执行）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="完整演练并打印将要清空的内容，不改动数据库与文件",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过交互确认直接执行（供非交互环境使用）",
    )
    parser.add_argument(
        "--keep-media",
        action="store_true",
        help="保留 runtime_data/media/img 下的图片文件，只清数据库",
    )
    parser.add_argument(
        "--include-captions",
        action="store_true",
        help="连 agent_image_captions（VLM 描述缓存）一起清，默认保留",
    )
    parser.add_argument(
        "--include-legacy-agent-tasks",
        action="store_true",
        help="清理线上遗留的 agent_tasks 表（默认不清，不会 DROP 表）",
    )
    args = parser.parse_args()
    return asyncio.run(
        reset(
            dry_run=args.dry_run,
            assume_yes=args.yes,
            keep_media=args.keep_media,
            include_captions=args.include_captions,
            include_legacy_agent_tasks=args.include_legacy_agent_tasks,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
