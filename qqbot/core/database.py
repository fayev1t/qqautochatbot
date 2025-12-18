"""数据库配置和会话管理

提供异步数据库连接、会话工厂、分表操作等功能。
分表策略：
- 群成员表按 group_id 分表: group_members_{group_id}
- 群消息表按 group_id 分表: group_messages_{group_id}
"""

import logging
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class DatabaseConfig(BaseSettings):
    """数据库配置从环境变量读取"""

    db_host: str = "postgres16"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "qqbot"
    db_echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20

    class Config:
        env_file = ".env.dev"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"

    @property
    def database_url(self) -> str:
        """构建数据库URL"""
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


# 全局数据库配置
config = DatabaseConfig()

# 调试：打印实际加载的配置
logger.info(f"[database] 📋 Database Config Loaded:")
logger.info(f"[database]   DB_HOST: {config.db_host}")
logger.info(f"[database]   DB_PORT: {config.db_port}")
logger.info(f"[database]   DB_USER: {config.db_user}")
logger.info(f"[database]   DB_NAME: {config.db_name}")
logger.info(f"[database]   DATABASE_URL: {config.database_url}")

# 创建异步引擎
engine: AsyncEngine = create_async_engine(
    config.database_url,
    echo=config.db_echo,
    pool_size=config.pool_size,
    max_overflow=config.max_overflow,
    connect_args={
        "server_settings": {
            "application_name": "qqbot",
            "timezone": "Asia/Shanghai",  # 设置为北京时区
        }
    },
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

    创建所有静态表（users, groups, 模板表）
    """
    try:
        # 导入所有模型以注册到Base.metadata
        from qqbot.models import Base  # noqa: F401
        from qqbot.models.messages import User, Group, GroupMemberTemplate, GroupMessage  # noqa: F401

        async with engine.begin() as conn:
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
    """检查表是否存在

    Args:
        table_name: 表名

    Returns:
        bool: 表是否存在
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = :table_name
                )
            """),
            {"table_name": table_name},
        )
        return result.scalar() or False


async def create_group_tables(group_id: int) -> tuple[str, str]:
    """为指定群组创建分表

    创建以下两个表：
    - group_members_{group_id}: 群成员表
    - group_messages_{group_id}: 群消息表

    Args:
        group_id: 群组ID

    Returns:
        tuple[str, str]: (members_table_name, messages_table_name)

    Raises:
        Exception: 表创建失败
    """
    members_table = f"group_members_{group_id}"
    messages_table = f"group_messages_{group_id}"

    try:
        async with engine.begin() as conn:
            logger.debug(f"[create_group_tables] Creating tables for group {group_id}")

            # 创建群成员表（如果不存在）
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {members_table} (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL UNIQUE,
                    card VARCHAR(255),
                    join_time TIMESTAMP WITH TIME ZONE,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            logger.debug(f"[create_group_tables] Created/verified members table: {members_table}")

            # 创建群成员表索引（如果不存在）
            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{members_table}_user_id ON {members_table}(user_id)
            """))

            # 创建群消息表（如果不存在）
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {messages_table} (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    message_content TEXT,
                    message_type VARCHAR(20) DEFAULT 'text'
                        CHECK(message_type IN ('text', 'img', 'vid', 'aud', 'others')),
                    is_recalled BOOLEAN DEFAULT FALSE,
                    "timestamp" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            logger.debug(f"[create_group_tables] Created/verified messages table: {messages_table}")

            # 创建群消息表索引（如果不存在）
            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{messages_table}_user_id ON {messages_table}(user_id)
            """))

            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{messages_table}_is_recalled ON {messages_table}(is_recalled)
            """))

            await conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_{messages_table}_timestamp ON {messages_table}("timestamp")
            """))

        logger.info(
            f"[create_group_tables] ✅ Group tables ready: {members_table}, {messages_table}"
        )
        return members_table, messages_table

    except Exception as e:
        logger.error(
            f"[create_group_tables] ❌ Failed to create/verify group tables for {group_id}: {e}",
            exc_info=True,
        )
        raise
