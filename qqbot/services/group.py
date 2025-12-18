"""Group management service."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models import Group
from qqbot.core.database import create_group_tables, table_exists


class GroupService:
    """Service for managing groups."""

    @staticmethod
    async def get_or_create_group(
        session: AsyncSession,
        group_id: int,
        group_name: str | None = None,
    ) -> Group:
        """Get an existing group or create a new one.

        Args:
            session: Database session
            group_id: QQ group ID
            group_name: Group name (optional)

        Returns:
            Group object
        """
        # Try to get existing group
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if group:
            # Verify tables exist for existing group
            # (in case table creation failed previously)
            import logging
            logger = logging.getLogger(__name__)
            members_exists = await table_exists(group.members_table_name)
            messages_exists = await table_exists(group.table_name)

            if not members_exists or not messages_exists:
                logger.warning(
                    f"[GroupService] Tables missing for existing group {group_id}, recreating..."
                )
                try:
                    await create_group_tables(group_id)
                    logger.info(f"[GroupService] ✅ Tables recreated for group {group_id}")
                except Exception as e:
                    logger.error(
                        f"[GroupService] ❌ Failed to recreate tables for {group_id}: {e}",
                        exc_info=True,
                    )
                    raise

            # Update name if provided and different
            if group_name and group.group_name != group_name:
                group.group_name = group_name
                await session.commit()
                await session.refresh(group)
            return group

        # Create new group
        table_name = f"group_messages_{group_id}"
        members_table_name = f"group_members_{group_id}"

        group = Group(
            group_id=group_id,
            group_name=group_name,
            table_name=table_name,
            members_table_name=members_table_name,
        )

        # Create the per-group tables FIRST (before committing Group record)
        # Tables must exist before any data can be inserted
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"[GroupService] Creating tables for group {group_id}...")
        try:
            await create_group_tables(group_id)
            logger.info(f"[GroupService] ✅ Tables created for group {group_id}")
        except Exception as e:
            logger.error(f"[GroupService] ❌ Failed to create tables for group {group_id}: {e}", exc_info=True)
            raise

        # Only commit Group record after tables are successfully created
        session.add(group)
        await session.commit()
        await session.refresh(group)
        logger.info(f"[GroupService] ✅ Group record saved for {group_id}")

        return group

    @staticmethod
    async def get_group(
        session: AsyncSession,
        group_id: int,
    ) -> Group | None:
        """Get a group by ID.

        Args:
            session: Database session
            group_id: QQ group ID

        Returns:
            Group object or None if not found
        """
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_all_groups(
        session: AsyncSession,
    ) -> list[Group]:
        """Get all groups from database.

        Args:
            session: Database session

        Returns:
            List of Group objects
        """
        result = await session.execute(select(Group))
        return result.scalars().all()

    @staticmethod
    async def update_group_name(
        session: AsyncSession,
        group_id: int,
        group_name: str,
    ) -> Group:
        """Update group name.

        Args:
            session: Database session
            group_id: QQ group ID
            group_name: New group name

        Returns:
            Updated Group object
        """
        result = await session.execute(
            select(Group).where(Group.group_id == group_id)
        )
        group = result.scalar_one_or_none()

        if not group:
            raise ValueError(f"Group {group_id} not found")

        group.group_name = group_name
        await session.commit()
        await session.refresh(group)

        return group
