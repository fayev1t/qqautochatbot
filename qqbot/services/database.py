"""兼容旧导入路径的服务聚合模块。"""

from qqbot.services.group import GroupService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.user import UserService

__all__ = [
    "UserService",
    "GroupService",
    "GroupMemberService",
    "GroupMessageService",
]
