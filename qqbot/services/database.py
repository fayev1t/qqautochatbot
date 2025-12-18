"""数据库服务 - 提供用户、群组、消息的操作接口"""

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.models.messages import User, Group, GroupMessage, GroupMemberTemplate


class UserService:
    """用户服务"""

    @staticmethod
    async def get_or_create_user(
        session: AsyncSession, user_id: int, nickname: str | None = None
    ) -> User:
        """获取或创建用户

        Args:
            session: 数据库会话
            user_id: QQ号
            nickname: QQ昵称

        Returns:
            User: 用户对象
        """
        stmt = select(User).where(User.user_id == user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if user is None:
            user = User(user_id=user_id, nickname=nickname)
            session.add(user)
            await session.commit()
        elif nickname and user.nickname != nickname:
            # 更新昵称
            user.nickname = nickname
            await session.commit()

        return user

    @staticmethod
    async def get_user(session: AsyncSession, user_id: int) -> User | None:
        """获取用户信息

        Args:
            session: 数据库会话
            user_id: QQ号

        Returns:
            User | None: 用户对象或None
        """
        stmt = select(User).where(User.user_id == user_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def update_user_nickname(
        session: AsyncSession, user_id: int, nickname: str
    ) -> User:
        """更新用户昵称

        Args:
            session: 数据库会话
            user_id: QQ号
            nickname: 新昵称

        Returns:
            User: 更新后的用户对象
        """
        stmt = update(User).where(User.user_id == user_id).values(nickname=nickname)
        await session.execute(stmt)
        await session.commit()
        return await UserService.get_user(session, user_id)


class GroupService:
    """群组服务"""

    @staticmethod
    async def get_or_create_group(
        session: AsyncSession,
        group_id: int,
        group_name: str | None = None,
        table_name: str | None = None,
        members_table_name: str | None = None,
    ) -> Group:
        """获取或创建群组

        Args:
            session: 数据库会话
            group_id: 群ID
            group_name: 群名称
            table_name: 消息表名
            members_table_name: 群成员表名

        Returns:
            Group: 群组对象
        """
        stmt = select(Group).where(Group.group_id == group_id)
        result = await session.execute(stmt)
        group = result.scalar_one_or_none()

        if group is None:
            if table_name is None:
                table_name = f"group_messages_{group_id}"
            if members_table_name is None:
                members_table_name = f"group_members_{group_id}"

            group = Group(
                group_id=group_id,
                group_name=group_name,
                table_name=table_name,
                members_table_name=members_table_name,
            )
            session.add(group)
            await session.commit()
        elif group_name and group.group_name != group_name:
            # 更新群名称
            group.group_name = group_name
            await session.commit()

        return group

    @staticmethod
    async def get_group(session: AsyncSession, group_id: int) -> Group | None:
        """获取群组信息

        Args:
            session: 数据库会话
            group_id: 群ID

        Returns:
            Group | None: 群组对象或None
        """
        stmt = select(Group).where(Group.group_id == group_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def update_group_name(
        session: AsyncSession, group_id: int, group_name: str
    ) -> Group:
        """更新群名称

        Args:
            session: 数据库会话
            group_id: 群ID
            group_name: 新群名称

        Returns:
            Group: 更新后的群组对象
        """
        stmt = (
            update(Group)
            .where(Group.group_id == group_id)
            .values(group_name=group_name)
        )
        await session.execute(stmt)
        await session.commit()
        return await GroupService.get_group(session, group_id)


class GroupMemberService:
    """群成员服务"""

    @staticmethod
    async def add_member(
        session: AsyncSession,
        group_id: int,
        user_id: int,
        card: str | None = None,
        join_time=None,
    ) -> None:
        """添加群成员

        Args:
            session: 数据库会话
            group_id: 群ID
            user_id: QQ号
            card: 群昵称
            join_time: 入群时间

        使用示例：
            await GroupMemberService.add_member(session, group_id, user_id, card, join_time)
        """
        # 实际实现需要动态表操作，这里提供接口示例
        # 真实实现需要在执行SQL时处理动态表名
        raise NotImplementedError("请使用原始SQL执行insert操作或使用session.execute()")

    @staticmethod
    async def update_member_active(
        session: AsyncSession, group_id: int, user_id: int, is_active: bool
    ) -> None:
        """更新成员活跃状态

        Args:
            session: 数据库会话
            group_id: 群ID
            user_id: QQ号
            is_active: 是否在群中
        """
        # 实际实现需要动态表操作
        raise NotImplementedError("请使用原始SQL执行update操作或使用session.execute()")


class GroupMessageService:
    """群消息服务"""

    @staticmethod
    async def add_message(
        session: AsyncSession,
        group_id: int,
        user_id: int,
        message_content: str,
        message_type: str = "text",
    ) -> None:
        """添加群消息

        Args:
            session: 数据库会话
            group_id: 群ID
            user_id: QQ号
            message_content: 消息内容
            message_type: 消息类型 (text, img, vid, aud, others)

        使用示例：
            await GroupMessageService.add_message(
                session, group_id, user_id, "Hello", "text"
            )
        """
        # 实际实现需要动态表操作
        raise NotImplementedError("请使用原始SQL执行insert操作或使用session.execute()")

    @staticmethod
    async def recall_message(
        session: AsyncSession, group_id: int, message_id: int
    ) -> None:
        """撤回消息

        Args:
            session: 数据库会话
            group_id: 群ID
            message_id: 消息ID
        """
        # 实际实现需要动态表操作
        raise NotImplementedError("请使用原始SQL执行update操作或使用session.execute()")

    @staticmethod
    async def get_messages(
        session: AsyncSession, group_id: int, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """获取群消息

        Args:
            session: 数据库会话
            group_id: 群ID
            limit: 限制数量
            offset: 偏移量

        Returns:
            list[dict]: 消息列表
        """
        # 实际实现需要动态表操作
        raise NotImplementedError("请使用原始SQL查询或使用session.execute()")
