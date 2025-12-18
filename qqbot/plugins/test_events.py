"""Test script to verify if napcat pushes group member join/leave events.

This plugin tests whether the OneBot adapter receives the following events:
- GroupIncreaseNoticeEvent: When a member joins the group
- GroupDecreaseNoticeEvent: When a member leaves the group
- GroupRecallNoticeEvent: When a message is recalled

Usage:
1. Invite a user to the test group (should trigger GroupIncreaseNoticeEvent)
2. Remove a user from the test group (should trigger GroupDecreaseNoticeEvent)
3. Recall a message in the test group (should trigger GroupRecallNoticeEvent)
4. Send a normal message (sanity check)

Check console/logs for event information.
"""

import logging
from nonebot import on_notice, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupRecallNoticeEvent,
    MessageEvent,
)

logger = logging.getLogger(__name__)

# Handler for group member join (increase)
group_increase_handler = on_notice(priority=1, block=False)


@group_increase_handler.handle()
async def handle_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    """Handle group member join event."""
    if isinstance(event, GroupIncreaseNoticeEvent):
        logger.info(
            "✅ GROUP INCREASE EVENT RECEIVED",
            extra={
                "event_type": "GroupIncreaseNoticeEvent",
                "group_id": event.group_id,
                "user_id": event.user_id,
                "operator_id": event.operator_id,
                "timestamp": event.time,
            },
        )
        message = (
            f"✅ 检测到成员加入事件！\n"
            f"群号: {event.group_id}\n"
            f"新成员QQ: {event.user_id}\n"
            f"操作者QQ: {event.operator_id}"
        )
        await group_increase_handler.send(message)


# Handler for group member leave (decrease)
group_decrease_handler = on_notice(priority=1, block=False)


@group_decrease_handler.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    """Handle group member leave event."""
    if isinstance(event, GroupDecreaseNoticeEvent):
        logger.info(
            "✅ GROUP DECREASE EVENT RECEIVED",
            extra={
                "event_type": "GroupDecreaseNoticeEvent",
                "group_id": event.group_id,
                "user_id": event.user_id,
                "operator_id": event.operator_id,
                "timestamp": event.time,
            },
        )
        message = (
            f"✅ 检测到成员离开事件！\n"
            f"群号: {event.group_id}\n"
            f"离开成员QQ: {event.user_id}\n"
            f"操作者QQ: {event.operator_id}"
        )
        await group_decrease_handler.send(message)


# Handler for message recall
group_recall_handler = on_notice(priority=1, block=False)


@group_recall_handler.handle()
async def handle_group_recall(bot: Bot, event: GroupRecallNoticeEvent) -> None:
    """Handle group message recall event."""
    if isinstance(event, GroupRecallNoticeEvent):
        logger.info(
            "✅ GROUP RECALL EVENT RECEIVED",
            extra={
                "event_type": "GroupRecallNoticeEvent",
                "group_id": event.group_id,
                "message_id": event.message_id,
                "user_id": event.user_id,
                "operator_id": event.operator_id,
                "timestamp": event.time,
            },
        )
        message = (
            f"✅ 检测到消息撤回事件！\n"
            f"群号: {event.group_id}\n"
            f"消息ID: {event.message_id}\n"
            f"消息发送者QQ: {event.user_id}\n"
            f"撤回操作者QQ: {event.operator_id}"
        )
        await group_recall_handler.send(message)


# Handler for baseline message (sanity check)
message_handler = on_message(priority=100, block=False)


@message_handler.handle()
async def handle_message(event: MessageEvent) -> None:
    """Handle regular message (sanity check)."""
    if str(event.message).strip():  # Only log non-empty messages
        logger.info(
            "✅ MESSAGE RECEIVED (sanity check)",
            extra={
                "event_type": "MessageEvent",
                "group_id": getattr(event, "group_id", None),
                "user_id": event.user_id,
                "message": str(event.message)[:100],  # First 100 chars
            },
        )
