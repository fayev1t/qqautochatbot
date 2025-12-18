"""Event handlers for database operations.

This plugin listens to QQ Bot events and performs database operations:
- GroupMessageEvent: Save messages to database
- GroupIncreaseNoticeEvent: Add member to database
- GroupDecreaseNoticeEvent: Mark member as inactive
- GroupRecallNoticeEvent: Mark message as recalled
- Group name & member nickname sync: Every 30 minutes (background task in sync_nicknames.py)
"""

import logging
from nonebot import on_notice, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupRecallNoticeEvent,
)

from qqbot.core.database import AsyncSessionLocal, table_exists
from qqbot.services.user import UserService
from qqbot.services.group import GroupService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.group_message import GroupMessageService

logger = logging.getLogger(__name__)

# ============================================================================
# 1. GroupMessageEvent - 保存群消息到数据库
# ============================================================================

message_handler = on_message(priority=10, block=False)


@message_handler.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    """Handle group message event - save to database.

    Priority: P0 - 高频操作，同步执行
    """
    # Only handle group messages, not private messages
    if not hasattr(event, "group_id"):
        return

    group_id = event.group_id
    user_id = event.user_id

    # Determine message type and content based on message segments
    message_type = "text"
    message_content = ""

    # Try to get the full message from raw_message first
    # NoneBot removes the bot's name from event.message when it detects the bot is being called
    # So we need to use raw_message to get the original content
    if hasattr(event, "raw_message") and event.raw_message:
        original_message = event.raw_message
        message_content = original_message
    else:
        # Fallback to segment parsing if raw_message is not available
        for i, segment in enumerate(event.message):
            seg_type = segment.type
            seg_data = segment.data

            if seg_type == "text":
                text = segment.data.get("text", "")
                message_content += text
            elif seg_type == "at":
                # Preserve @mention information
                at_qq = segment.data.get("qq", "")
                at_name = segment.data.get("name", "")

                # Check if this is a mention of the bot itself
                is_at_bot = str(at_qq) == str(event.self_id)

                if is_at_bot:
                    # When bot is mentioned, just use the name without "@" prefix
                    bot_name = at_name or "小奏"
                    message_content += bot_name
                elif at_name:
                    # Real @ mention of other users - keep the @ prefix
                    message_content += f"@{at_name}"
                else:
                    message_content += f"@{at_qq}"
            elif seg_type == "image":
                message_type = "img"
                message_content = "【图片】"
                break
            elif seg_type == "record":  # Voice message
                message_type = "aud"
                message_content = "【语音】"
                break
            elif seg_type == "video":
                message_type = "vid"
                message_content = "【视频】"
                break
            elif seg_type == "file":
                message_type = "others"
                message_content = "【文件】"
                break
            elif seg_type in ["face", "emoji", "shake", "poke"]:
                # Add these but don't break
                message_content += f"[CQ:{seg_type}]"

    # If no content determined, use full message string representation
    if not message_content:
        message_content = str(event.message)
        if not message_content.strip():
            message_content = "【空消息】"

    # Log the parsed message for debugging
    logger.debug(
        f"Parsed message content from {len(event.message)} segments",
        extra={
            "group_id": group_id,
            "user_id": user_id,
            "segments_count": len(event.message),
            "parsed_content": message_content[:100],  # First 100 chars
        },
    )

    async with AsyncSessionLocal() as session:
        try:
            # 1. 确保用户存在
            await UserService.get_or_create_user(
                session,
                user_id=user_id,
            )

            # 2. 确保群存在
            await GroupService.get_or_create_group(
                session,
                group_id=group_id,
            )

            # 3. 保存消息
            message_id = await GroupMessageService.save_message(
                session,
                group_id=group_id,
                user_id=user_id,
                message_content=message_content,
                message_type=message_type,
            )

            # Commit all operations together to ensure message is saved before group_chat handler runs
            await session.commit()

            logger.info(
                "Message saved successfully",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "message_id": message_id,
                    "content_length": len(message_content),
                },
            )

        except ValueError as e:
            logger.warning(f"Message event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            logger.error(
                f"Failed to save message: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 2. GroupIncreaseNoticeEvent - 成员进群
# ============================================================================

increase_handler = on_notice(priority=5, block=False)


@increase_handler.handle()
async def handle_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    """Handle group member join event.

    Priority: P0 - 必需操作，同步执行
    Idempotency: ✅ ON CONFLICT处理重复事件
    """
    group_id = event.group_id
    user_id = event.user_id

    async with AsyncSessionLocal() as session:
        try:
            # 1. 确保用户存在
            await UserService.get_or_create_user(
                session,
                user_id=user_id,
            )
            await session.commit()

            # 2. 确保群存在
            await GroupService.get_or_create_group(
                session,
                group_id=group_id,
            )
            await session.commit()

            # 3. 添加成员（幂等操作）
            await GroupMemberService.add_member_from_join_event(
                session,
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()

            # Note: 昵称更新现在由后台任务定期处理，避免频繁调用 QQ API

            logger.info(
                "Member added to group",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "operator_id": event.operator_id,
                },
            )

        except ValueError as e:
            logger.warning(f"Join event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            logger.error(
                f"Failed to add member: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 3. GroupDecreaseNoticeEvent - 成员离群
# ============================================================================

decrease_handler = on_notice(priority=5, block=False)


@decrease_handler.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    """Handle group member leave event.

    Priority: P0 - 必需操作，同步执行
    Idempotency: ✅ UPDATE无唯一性约束，多次执行安全
    """
    group_id = event.group_id
    user_id = event.user_id

    async with AsyncSessionLocal() as session:
        try:
            # 标记成员为离线（软删除）
            await GroupMemberService.mark_member_inactive(
                session,
                group_id=group_id,
                user_id=user_id,
            )
            await session.commit()

            logger.info(
                "Member left group",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "operator_id": event.operator_id,
                },
            )

        except ValueError as e:
            logger.warning(f"Leave event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            logger.error(
                f"Failed to mark member inactive: {e}",
                extra={"group_id": group_id, "user_id": user_id},
            )


# ============================================================================
# 4. GroupRecallNoticeEvent - 消息撤回
# ============================================================================

recall_handler = on_notice(priority=10, block=False)


@recall_handler.handle()
async def handle_group_recall(bot: Bot, event: GroupRecallNoticeEvent) -> None:
    """Handle group message recall event.

    Priority: P1 - 异步优先级，非关键操作
    Idempotency: ✅ UPDATE幂等
    """
    group_id = event.group_id
    message_id = event.message_id

    async with AsyncSessionLocal() as session:
        try:
            # 标记消息已撤回
            await GroupMessageService.recall_message(
                session,
                group_id=group_id,
                message_id=message_id,
            )
            await session.commit()

            logger.info(
                "Message marked as recalled",
                extra={
                    "group_id": group_id,
                    "message_id": message_id,
                    "operator_id": event.operator_id,
                },
            )

        except ValueError as e:
            logger.warning(f"Recall event error: {e}", extra={"group_id": group_id})
        except Exception as e:
            logger.error(
                f"Failed to recall message: {e}",
                extra={"group_id": group_id, "message_id": message_id},
            )
