"""Group message management service."""

from datetime import datetime
from sqlalchemy import (
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models import Group


class GroupMessageService:
    """Service for managing per-group message tables."""

    @staticmethod
    def get_messages_table_name(group_id: int) -> str:
        """Get the table name for a group's messages.

        Args:
            group_id: QQ group ID

        Returns:
            Table name like 'group_messages_610662657'
        """
        return f"group_messages_{group_id}"

    @staticmethod
    async def save_message(
        session: AsyncSession,
        group_id: int,
        user_id: int,
        message_content: str,
        message_type: str = "text",
    ) -> int:
        """Save a message to group message table.

        Args:
            session: Database session
            group_id: QQ group ID
            user_id: QQ user ID
            message_content: Message content
            message_type: Message type (text, img, vid, aud, others)

        Returns:
            Message ID (auto-generated)

        Raises:
            ValueError: If group not found
        """
        # Get the Group record to find table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.table_name

        # Use raw SQL for direct insertion and to get the returned ID
        # Note: We omit "timestamp" column to let database use DEFAULT CURRENT_TIMESTAMP
        sql = text(f"""
            INSERT INTO {table_name}
            (user_id, message_content, message_type, is_recalled)
            VALUES (:user_id, :message_content, :message_type, :is_recalled)
            RETURNING id
        """)

        result = await session.execute(
            sql,
            {
                "user_id": user_id,
                "message_content": message_content,
                "message_type": message_type,
                "is_recalled": False,
            },
        )

        message_id = result.scalar()
        return message_id

    @staticmethod
    async def recall_message(
        session: AsyncSession,
        group_id: int,
        message_id: int,
    ) -> None:
        """Mark a message as recalled.

        Args:
            session: Database session
            group_id: QQ group ID
            message_id: Message ID
        """
        # Get the Group record to find table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.table_name

        sql = text(f"""
            UPDATE {table_name}
            SET is_recalled = true
            WHERE id = :message_id
        """)

        await session.execute(
            sql,
            {"message_id": message_id},
        )

    @staticmethod
    async def get_message(
        session: AsyncSession,
        group_id: int,
        message_id: int,
    ) -> dict | None:
        """Get a specific message.

        Args:
            session: Database session
            group_id: QQ group ID
            message_id: Message ID

        Returns:
            Message data dict or None if not found
        """
        # Get the Group record to find table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.table_name

        # Use raw SQL instead of reflection to avoid async/sync engine conflicts
        sql = text(f"""
            SELECT * FROM {table_name} WHERE id = :message_id
        """)

        result = await session.execute(sql, {"message_id": message_id})
        row = result.first()

        if row:
            return dict(row._mapping)  # type: ignore

        return None

    @staticmethod
    async def get_group_messages(
        session: AsyncSession,
        group_id: int,
        limit: int = 100,
        offset: int = 0,
        include_recalled: bool = False,
    ) -> list[dict]:
        """Get messages from a group (paginated).

        Args:
            session: Database session
            group_id: QQ group ID
            limit: Maximum number of messages to return
            offset: Number of messages to skip
            include_recalled: Include recalled messages

        Returns:
            List of message dicts
        """
        # Get the Group record to find table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.table_name

        # Use raw SQL instead of reflection
        where_clause = ""
        if not include_recalled:
            where_clause = "WHERE is_recalled = false"

        sql = text(f"""
            SELECT * FROM {table_name}
            {where_clause}
            ORDER BY "timestamp" DESC
            LIMIT :limit OFFSET :offset
        """)

        result = await session.execute(sql, {"limit": limit, "offset": offset})
        rows = result.fetchall()

        return [dict(row._mapping) for row in rows]  # type: ignore

    @staticmethod
    async def get_recent_messages(
        session: AsyncSession,
        group_id: int,
        limit: int = 10,
    ) -> list[dict]:
        """Get recent messages in chronological order (for context).

        Returns messages from oldest to newest for conversation context.

        Args:
            session: Database session
            group_id: QQ group ID
            limit: Maximum number of recent messages to fetch

        Returns:
            List of message dicts in chronological order (oldest first)
        """
        # Get the Group record to find table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.table_name

        # Use raw SQL to get recent messages in chronological order
        sql = text(f"""
            SELECT * FROM {table_name}
            WHERE is_recalled = false
            ORDER BY "timestamp" DESC
            LIMIT :limit
        """)

        result = await session.execute(sql, {"limit": limit})
        rows = result.fetchall()

        # Reverse to get chronological order (oldest first)
        messages = [dict(row._mapping) for row in rows]  # type: ignore
        messages.reverse()

        return messages

    @staticmethod
    async def get_user_messages_in_group(
        session: AsyncSession,
        group_id: int,
        user_id: int,
        limit: int = 50,
    ) -> list[dict]:
        """Get recent messages from a specific user in a group.

        Args:
            session: Database session
            group_id: QQ group ID
            user_id: QQ user ID
            limit: Maximum number of messages

        Returns:
            List of message dicts
        """
        # Get the Group record to find table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.table_name

        # Use raw SQL instead of reflection
        sql = text(f"""
            SELECT * FROM {table_name}
            WHERE user_id = :user_id AND is_recalled = false
            ORDER BY "timestamp" DESC
            LIMIT :limit
        """)

        result = await session.execute(sql, {"user_id": user_id, "limit": limit})
        rows = result.fetchall()

        return [dict(row._mapping) for row in rows]  # type: ignore
