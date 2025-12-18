"""User and group relationship models.

数据库设计规范：
- users: 用户表 (静态)
- groups: 群组表 (静态) - 记录群的基本信息和分表名
- group_members_{group_id}: 群成员表 (动态，按群ID分表)
- group_messages_{group_id}: 群消息表 (动态，按群ID分表)
"""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, Text, func
from qqbot.models.base import Base


class User(Base):
    """用户表 (users)

    存储QQ用户信息，user_id全局唯一
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, unique=True, nullable=False, index=True)
    nickname = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<User(user_id={self.user_id}, nickname={self.nickname})>"


class Group(Base):
    """群组表 (groups)

    存储群的基本信息和动态分表名称
    """

    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(BigInteger, unique=True, nullable=False, index=True)
    group_name = Column(String(255), nullable=True)
    table_name = Column(String(255), nullable=False)  # 消息表名: group_messages_{group_id}
    members_table_name = Column(String(255), nullable=False)  # 群成员表名: group_members_{group_id}
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Group(group_id={self.group_id}, group_name={self.group_name})>"


class GroupMemberTemplate(Base):
    """群成员表模板 (group_members_template)

    实际表名: group_members_{group_id}
    每个群有一个对应的成员表，存储该群的成员信息

    约束说明：
    - user_id: 在同一群中唯一（一个用户在一个群中只能有一条成员记录）
    - is_active: 标记用户是否还在群中（离群时设为False而不是删除记录）
    """

    __tablename__ = "group_members_template"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, unique=True, index=True)
    card = Column(String(255), nullable=True)  # 群昵称
    join_time = Column(DateTime, nullable=True)  # 入群时间
    is_active = Column(Boolean, default=True)  # 是否在群中
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<GroupMember(user_id={self.user_id}, card={self.card}, is_active={self.is_active})>"


class GroupMessage(Base):
    """群消息表模板 (group_messages_template)

    实际表名: group_messages_{group_id}
    每个群有一个对应的消息表，存储该群的所有消息
    """

    __tablename__ = "group_messages_template"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    message_content = Column(Text, nullable=True)
    message_type = Column(String(20), default="text")  # text, img, vid, aud, others
    is_recalled = Column(Boolean, default=False, index=True)  # 是否被撤回
    timestamp = Column(DateTime, nullable=False, default=func.now(), index=True)  # 消息时间

    def __repr__(self) -> str:
        return f"<GroupMessage(id={self.id}, user_id={self.user_id}, message_type={self.message_type})>"
