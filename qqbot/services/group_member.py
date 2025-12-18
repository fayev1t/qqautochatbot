"""Group member management service."""

from datetime import datetime
from sqlalchemy import (
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models import Group


class GroupMemberService:
    """Service for managing per-group members tables."""

    @staticmethod
    def get_members_table_name(group_id: int) -> str:
        """Get the table name for a group's members.

        Args:
            group_id: QQ group ID

        Returns:
            Table name like 'group_members_610662657'
        """
        return f"group_members_{group_id}"

    @staticmethod
    async def add_or_update_member(
        session: AsyncSession,
        group_id: int,
        user_id: int,
        card: str | None = None,
        join_time: datetime | None = None,
    ) -> None:
        """Add or update a group member (idempotent using ON CONFLICT).

        Args:
            session: Database session
            group_id: QQ group ID
            user_id: QQ user ID
            card: Group nickname
            join_time: Member join time
        """
        # Get the Group record to find members table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.members_table_name

        # Use raw SQL with ON CONFLICT for idempotent insert
        # This avoids checking if member exists, just try to insert and handle conflict
        sql = text(f"""
            INSERT INTO {table_name}
            (user_id, card, join_time, is_active, created_at, updated_at)
            VALUES (:user_id, :card, :join_time, :is_active, :created_at, :updated_at)
            ON CONFLICT(user_id) DO UPDATE SET
                card = COALESCE(:card, {table_name}.card),
                is_active = :is_active,
                updated_at = :updated_at
        """)

        await session.execute(
            sql,
            {
                "user_id": user_id,
                "card": card,
                "join_time": join_time or datetime.utcnow(),
                "is_active": True,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            },
        )

    @staticmethod
    async def add_member_from_join_event(
        session: AsyncSession,
        group_id: int,
        user_id: int,
    ) -> None:
        """Add member when join event received (minimal fields, idempotent).

        This is optimized for GroupIncreaseNoticeEvent handling.
        Uses ON CONFLICT to ensure idempotency in case of event duplication.

        Args:
            session: Database session
            group_id: QQ group ID
            user_id: QQ user ID being added
        """
        # Get the Group record to find members table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.members_table_name

        # Use ON CONFLICT to handle event duplication (napcat might send twice)
        sql = text(f"""
            INSERT INTO {table_name}
            (user_id, join_time, is_active, created_at, updated_at)
            VALUES (:user_id, :join_time, :is_active, :created_at, :updated_at)
            ON CONFLICT(user_id) DO UPDATE SET
                is_active = true,
                updated_at = :updated_at
        """)

        await session.execute(
            sql,
            {
                "user_id": user_id,
                "join_time": datetime.utcnow(),
                "is_active": True,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            },
        )

    @staticmethod
    async def batch_update_cards(
        session: AsyncSession,
        group_id: int,
        card_updates: dict[int, str],
    ) -> None:
        """Batch update group member cards (for background sync task).

        Args:
            session: Database session
            group_id: QQ group ID
            card_updates: Dict of {user_id: new_card}
        """
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.members_table_name

        for user_id, card in card_updates.items():
            if card:  # Only update if card is not empty
                # Use UPSERT (INSERT ... ON CONFLICT DO UPDATE) to create or update
                sql = text(f"""
                    INSERT INTO {table_name}
                    (user_id, card, is_active, created_at, updated_at)
                    VALUES (:user_id, :card, :is_active, :created_at, :updated_at)
                    ON CONFLICT(user_id) DO UPDATE SET
                        card = :card,
                        updated_at = :updated_at
                """)
                await session.execute(
                    sql,
                    {
                        "user_id": user_id,
                        "card": card,
                        "is_active": True,
                        "created_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    },
                )

    @staticmethod
    async def get_member(
        session: AsyncSession,
        group_id: int,
        user_id: int,
    ) -> dict | None:
        """Get a specific group member.

        Args:
            session: Database session
            group_id: QQ group ID
            user_id: QQ user ID

        Returns:
            Member data dict or None if not found
        """
        # Get the Group record to find members table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.members_table_name

        # Use raw SQL instead of reflection
        sql = text(f"""
            SELECT * FROM {table_name} WHERE user_id = :user_id
        """)

        result = await session.execute(sql, {"user_id": user_id})
        row = result.first()

        if row:
            return dict(row._mapping)  # type: ignore

        return None

    @staticmethod
    async def get_group_members(
        session: AsyncSession,
        group_id: int,
        active_only: bool = True,
    ) -> list[dict]:
        """Get all members of a group.

        Args:
            session: Database session
            group_id: QQ group ID
            active_only: Only return active members

        Returns:
            List of member dicts
        """
        # Get the Group record to find members table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.members_table_name

        # Use raw SQL instead of reflection
        where_clause = ""
        if active_only:
            where_clause = "WHERE is_active = true"

        sql = text(f"""
            SELECT * FROM {table_name}
            {where_clause}
            ORDER BY user_id
        """)

        result = await session.execute(sql)
        rows = result.fetchall()

        return [dict(row._mapping) for row in rows]  # type: ignore

    @staticmethod
    async def mark_member_inactive(
        session: AsyncSession,
        group_id: int,
        user_id: int,
    ) -> None:
        """Mark a member as inactive (left the group).

        Args:
            session: Database session
            group_id: QQ group ID
            user_id: QQ user ID
        """
        # Get the Group record to find members table name
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        table_name = group.members_table_name

        # Use raw SQL for consistency with add operations
        sql = text(f"""
            UPDATE {table_name}
            SET is_active = false, updated_at = :updated_at
            WHERE user_id = :user_id
        """)

        await session.execute(
            sql,
            {
                "user_id": user_id,
                "updated_at": datetime.utcnow(),
            },
        )
