"""Services for QQ Bot."""

from qqbot.services.user import UserService
from qqbot.services.group import GroupService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.message_aggregator import MessageAggregator, message_aggregator
from qqbot.services.block_judge import BlockJudger, block_judger

__all__ = [
    "UserService",
    "GroupService",
    "GroupMemberService",
    "GroupMessageService",
    "MessageAggregator",
    "message_aggregator",
    "BlockJudger",
    "block_judger",
]
