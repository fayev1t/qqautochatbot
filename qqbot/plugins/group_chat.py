"""Group chat AI conversation plugin with message aggregation.

This plugin enables the bot to participate naturally in group conversations
using a message aggregation system and two-tier AI:
1. Message Aggregator: Collects messages into response blocks
2. Block Judger (first-tier): Analyzes block and decides reply strategy
3. Conversation Service (second-tier): Generates appropriate responses

Execution order (lower priority runs first):
- Priority 10: event_handlers.py (saves message to database)
- Priority 50: group_chat.py (this plugin - aggregates and responds)

The aggregation mechanism:
- Messages are collected into a "response block" per group
- After receiving a message, ask the wait-time judge how long to wait
- If new messages arrive, reset the timer and add to block
- When the wait expires, analyze the entire block and respond
"""

import asyncio
import logging

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from qqbot.core.database import AsyncSessionLocal
from qqbot.services.context import ContextManager
from qqbot.services.conversation import ConversationService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.user import UserService
from qqbot.services.message_aggregator import ResponseBlock, message_aggregator
from qqbot.services.block_judge import block_judger, JudgeResult

logger = logging.getLogger(__name__)

# Handler with priority 50 (after message saving at priority 10)
group_chat_handler = on_message(priority=50, block=False)

# Store bot instance for callback use
_bot_instance: Bot | None = None


async def _process_response_block(group_id: int, block: ResponseBlock) -> None:
    """Process a response block and generate replies.

    This is the callback function called by the aggregator when
    a block is ready to be processed.

    Args:
        group_id: QQ group ID
        block: The response block containing aggregated messages
    """
    global _bot_instance

    if not _bot_instance:
        logger.error("[group_chat] No bot instance available")
        return

    if block.get_message_count() == 0:
        logger.debug(f"[group_chat] Empty block for group {group_id}, skipping")
        return

    bot = _bot_instance

    logger.info(
        f"[group_chat] ══════ 开始处理对话块 ══════ 群={group_id}, "
        f"消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}",
        extra={
            "group_id": group_id,
            "message_count": block.get_message_count(),
            "unique_users": len(block.get_unique_users()),
        },
    )
    print(f"[group_chat] ══════ 开始处理对话块 ══════ 群={group_id}, 消息数={block.get_message_count()}, 用户数={len(block.get_unique_users())}")

    try:
        async with AsyncSessionLocal() as session:
            # 1. Get user names for the block
            user_names: dict[int, str] = {}
            for user_id in block.get_unique_users():
                try:
                    # Try group card first
                    member = await GroupMemberService.get_member(
                        session, group_id, user_id
                    )
                    if member and member.get("card"):
                        user_names[user_id] = member["card"]
                    else:
                        # Fallback to user nickname
                        user = await UserService.get_user(session, user_id)
                        if user and user.get("nickname"):
                            user_names[user_id] = user["nickname"]
                        else:
                            user_names[user_id] = f"用户{user_id}"
                except Exception:
                    user_names[user_id] = f"用户{user_id}"

            # 2. Get historical context from database
            context = await ContextManager.get_recent_context(
                session=session,
                group_id=group_id,
                limit=30,
                bot_id=block.messages[0].event.self_id if block.messages else None,
            )

            # 3. Judge the block (first-tier AI)
            msg = f"[group_chat] 🧠 第一层AI判断块内容..."
            logger.info(msg, extra={"group_id": group_id})
            print(msg)

            judge_result = await block_judger.judge_block(
                block=block,
                context=context,
                group_id=group_id,
                user_names=user_names,
            )

            # Early exit if no reply needed
            if not judge_result.should_reply or judge_result.reply_count == 0:
                msg = f"[group_chat] ❌ 不需要回复 | 群={group_id}, 原因={judge_result.explanation}"
                logger.info(msg, extra={"group_id": group_id})
                print(msg)
                return

            # 4. Generate and send responses for each reply plan
            conversation = ConversationService()

            msg = f"[group_chat] ✅ 准备生成回复 | 群={group_id}, 需要{len(judge_result.replies)}条回复"
            logger.info(msg, extra={"group_id": group_id, "reply_count": len(judge_result.replies)})
            print(msg)

            for i, reply_plan in enumerate(judge_result.replies):
                msg = f"[group_chat] 🔷 正在生成第 {i + 1}/{len(judge_result.replies)} 条回复 | 类型={reply_plan.reply_type}, 态度={reply_plan.emotion}, @用户={reply_plan.target_user_id}"
                logger.info(msg, extra={
                    "group_id": group_id,
                    "reply_index": i + 1,
                    "reply_type": reply_plan.reply_type,
                    "emotion": reply_plan.emotion,
                    "target_user_id": reply_plan.target_user_id,
                })
                print(msg)

                # Convert ReplyPlan to JudgeResult for ConversationService
                # This maintains compatibility with existing conversation service
                legacy_judge_result = JudgeResult(
                    should_reply=True,
                    reply_type=reply_plan.reply_type,
                    target_user_id=reply_plan.target_user_id,
                    emotion=reply_plan.emotion,
                    instruction=reply_plan.instruction,
                    should_mention=reply_plan.should_mention,
                )

                # Build context with block content
                block_context = f"{context}\n\n【当前对话块摘要】\n{judge_result.block_summary}\n\n【相关消息】\n{reply_plan.related_messages}"

                # Generate response (second-tier AI)
                response = await conversation.generate_response(
                    session=session,
                    context=block_context,
                    judge_result=legacy_judge_result,
                    group_id=group_id,
                )

                # Send response
                await bot.send_group_msg(group_id=group_id, message=response)

                msg = f"[group_chat] 📤 第 {i + 1} 条回复已发送 | 群={group_id}, 长度={len(response)}, 内容={response[:50]}"
                logger.info(msg, extra={
                    "group_id": group_id,
                    "reply_index": i + 1,
                    "response_length": len(response),
                    "reply_type": reply_plan.reply_type,
                })
                print(msg)

                # Save bot's response to database
                try:
                    # Get bot's self_id from first message in block
                    bot_self_id = (
                        block.messages[0].event.self_id if block.messages else None
                    )
                    if bot_self_id:
                        await GroupMessageService.save_message(
                            session=session,
                            group_id=group_id,
                            user_id=bot_self_id,
                            message_content=response,
                            message_type="text",
                        )
                        await session.commit()
                except Exception as e:
                    logger.warning(
                        f"[group_chat] Failed to save bot response: {e}",
                        extra={"group_id": group_id},
                    )

                # If there are more replies, wait a bit before sending next
                if i < len(judge_result.replies) - 1:
                    msg = f"[group_chat] ⏳ 等待1秒后发送下一条回复 | 群={group_id}"
                    logger.debug(msg, extra={"group_id": group_id})
                    print(msg)
                    await asyncio.sleep(1.0)  # 1 second between multiple replies

            msg = f"[group_chat] 🎉 对话块处理完成 | 群={group_id}, 共发送{len(judge_result.replies)}条回复"
            logger.info(msg, extra={
                "group_id": group_id,
                "total_replies": len(judge_result.replies),
            })
            print(msg)

    except Exception as e:
        logger.error(
            f"[group_chat] Failed to process block for group {group_id}: {e}",
            exc_info=True,
        )


# Register the callback with the aggregator
message_aggregator.set_reply_callback(_process_response_block)


@group_chat_handler.handle()
async def handle_group_chat(bot: Bot, event: GroupMessageEvent) -> None:
    """Handle group message by adding to aggregation block.

    Instead of processing each message immediately, we add it to an
    aggregation block. The block will be processed after a short delay,
    allowing multiple messages to be analyzed together.

    Args:
        bot: NoneBot bot instance
        event: Group message event
    """
    global _bot_instance
    _bot_instance = bot  # Store for callback use

    # Skip if not a group message
    if not hasattr(event, "group_id"):
        return

    group_id = event.group_id
    user_id = event.user_id

    # Skip bot's own messages to avoid self-loops
    if user_id == event.self_id:
        logger.debug(f"Skipping bot's own message in group {group_id}")
        return

    # Extract message content from event
    message_content = ""
    is_bot_mentioned = False

    # Use raw_message first (contains original complete text)
    if hasattr(event, "raw_message") and event.raw_message:
        message_content = event.raw_message
        if "小奏" in event.raw_message:
            is_bot_mentioned = True
    else:
        # Fallback to segment parsing
        for segment in event.message:
            seg_type = segment.type
            seg_data = segment.data

            if seg_type == "text":
                message_content += seg_data.get("text", "")
            elif seg_type == "at":
                at_qq = seg_data.get("qq", "")
                at_name = seg_data.get("name", "")
                is_at_bot = str(at_qq) == str(event.self_id)

                if is_at_bot:
                    is_bot_mentioned = True
                    bot_name = at_name or "小奏"
                    message_content += bot_name
                else:
                    message_content += f"@{at_name or at_qq}"
            elif seg_type == "image":
                message_content += "【图片】"
            elif seg_type == "record":
                message_content += "【语音】"
            elif seg_type == "video":
                message_content += "【视频】"
            elif seg_type == "file":
                message_content += "【文件】"
            elif seg_type in ["face", "emoji", "shake", "poke"]:
                pass  # Skip these

    # Only skip if message is truly empty AND bot is not mentioned
    if not message_content.strip() and not is_bot_mentioned:
        logger.debug(f"Skipping empty message in group {group_id}")
        return

    # Add message to aggregation block (等待时间由聚合器判断)
    try:
        logger.debug(
            f"[group_chat] 💬 处理消息 | 群={group_id}, 用户={user_id}, "
            f"@机器人={is_bot_mentioned}, 内容={message_content[:40]}...",
            extra={
                "group_id": group_id,
                "user_id": user_id,
                "is_bot_mentioned": is_bot_mentioned,
                "message_preview": message_content[:40],
            },
        )

        await message_aggregator.add_message(
            group_id=group_id,
            user_id=user_id,
            message_content=message_content,
            event=event,
            is_bot_mentioned=is_bot_mentioned,
        )

    except Exception as e:
        logger.error(
            f"[group_chat] Failed to add message to aggregator: {e}",
            extra={"group_id": group_id, "user_id": user_id},
            exc_info=True,
        )
