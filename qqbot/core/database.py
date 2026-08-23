from typing import Any, AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from pydantic_settings import BaseSettings

from qqbot.core.logging import get_logger
from qqbot.core.settings import get_settings_env_files

logger = get_logger(__name__)


class DatabaseConfig(BaseSettings):
    database_url: str | None = None
    db_echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20

    class Config:
        env_file = get_settings_env_files()
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self._normalize_database_url(self.database_url)

        raise ValueError("Missing DATABASE_URL. Please configure PostgreSQL DATABASE_URL.")

    @property
    def dialect_name(self) -> str:
        """当前数据库方言名称。"""
        return make_url(self.resolved_database_url).get_backend_name()

    @staticmethod
    def _normalize_database_url(database_url: str) -> str:
        normalized = database_url.strip()

        if normalized.startswith("postgres://"):
            return normalized.replace("postgres://", "postgresql+asyncpg://", 1)

        if normalized.startswith("postgresql://"):
            return normalized.replace("postgresql://", "postgresql+asyncpg://", 1)

        if normalized.startswith("postgresql+asyncpg://"):
            return normalized

        if normalized.startswith(("sqlite://", "sqlite+aiosqlite://")):
            raise ValueError("SQLite is no longer supported. Please configure PostgreSQL.")

        raise ValueError(
            "Unsupported database URL. Please configure a PostgreSQL DATABASE_URL."
        )


# 全局数据库配置
config = DatabaseConfig()
database_url = make_url(config.resolved_database_url)

# 调试：打印实际加载的配置
logger.info("[database] 📋 Database Config Loaded:")
logger.info(f"[database]   DIALECT: {config.dialect_name}")
logger.info(f"[database]   DB_HOST: {database_url.host}")
logger.info(f"[database]   DB_PORT: {database_url.port}")
logger.info(f"[database]   DB_USER: {database_url.username}")
logger.info(f"[database]   DB_NAME: {database_url.database}")


def _build_engine_kwargs() -> dict[str, Any]:
    return {
        "echo": config.db_echo,
        "pool_size": config.pool_size,
        "max_overflow": config.max_overflow,
        "connect_args": {
            "server_settings": {
                "application_name": "qqbot",
                "timezone": "Asia/Shanghai",
            }
        },
    }


# 创建异步引擎
engine: AsyncEngine = create_async_engine(
    config.resolved_database_url,
    **_build_engine_kwargs(),
)

# 会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话

    用于依赖注入（NoneBot2 Depends）

    Yields:
        AsyncSession: 数据库会话
    """
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """初始化数据库表

    表：
    - agent_events（append-only event stream，唯一真相源）
    - raw_events（原文绕库，满 100 清表；见 models/raw_event.py）
    - group_memories（一群一行记忆正文，UPDATE；见 models/group_memory.py）
    - agent_delivery_claims（历史 worker 的投递租约表；Program Effect 不再使用，
      为数据库向后兼容保留。见 models/agent_delivery_claim.py）

    pg_trgm 扩展先建好，AgentEvent.search_text 上的 GIN trgm 索引才能创建
    （供 search_history 关键字检索使用）。

    幂等 ALTER：`Base.metadata.create_all` 不会给已存在的表加新列。线上若
    在加入 search_text 列**之前**就建过 agent_events，重启后 SELECT 会
    `UndefinedColumnError`——这里用 `ALTER TABLE IF EXISTS ADD COLUMN
    IF NOT EXISTS` 补上。第一次部署（表还没建）时这条 ALTER 是 no-op，
    随后 create_all 直接建带 search_text 的全新表。

    **agent_tasks 已于 2026-08-21 移出**（渲染格式表 §一②：任务坍缩为单栏
    便签，读模型表随之取消）。这里只是不再声明该模型，**不发 DROP TABLE**：
    线上已建的表会原样留着、不再被读写。要不要清掉是运维决定，不该由一次
    业务改动顺手做掉——里面是历史任务数据。
    """
    try:
        # 触发模型模块加载以注册到 Base.metadata
        # （agent_events + raw_events + group_memories + agent_delivery_claims）
        from qqbot.models import Base  # noqa: F401
        from qqbot.models import agent_delivery_claim  # noqa: F401
        from qqbot.models import agent_event  # noqa: F401
        from qqbot.models import group_memory  # noqa: F401
        from qqbot.models import raw_event  # noqa: F401

        async with engine.begin() as conn:
            # 必须在 create_all 之前，否则 GIN trgm 索引创建会失败
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            # 已存在表的迁移补丁——column 必须先于 index 创建，否则 create_all
            # 里的 agent_events_search_trgm_idx 会引用不存在的列。
            await conn.execute(
                text(
                    "ALTER TABLE IF EXISTS agent_events "
                    "ADD COLUMN IF NOT EXISTS search_text TEXT "
                    "GENERATED ALWAYS AS (payload->>'raw_message') STORED"
                )
            )
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise


async def close_db() -> None:
    """关闭数据库连接"""
    try:
        await engine.dispose()
        logger.info("Database connection closed")
    except Exception as e:
        logger.error(f"Failed to close database connection: {e}")


async def table_exists(table_name: str) -> bool:
    """检查表是否存在"""
    async with engine.begin() as conn:
        return await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).has_table(table_name)
        )
