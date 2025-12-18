"""Group chat AI conversation plugin.

This plugin enables the bot to participate naturally in group conversations
using a two-tier AI system:
1. Message Judger (first-tier): Decides whether/how to reply
2. Conversation Service (second-tier): Generates appropriate response

Execution order (lower priority runs first):
- Priority 10: event_handlers.py (saves message to database)
- Priority 50: group_chat.py (this plugin - responds to message)
"""

import logging

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from qqbot.core.database import AsyncSessionLocal
from qqbot.services.context import ContextManager
from qqbot.services.message_judge import MessageJudger
from qqbot.services.conversation import ConversationService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.silence_mode import is_silent

logger = logging.getLogger(__name__)

# Handler with priority 50 (after message saving at priority 100)
group_chat_handler = on_message(priority=50, block=False)


@group_chat_handler.handle()
async def handle_group_chat(bot: Bot, event: GroupMessageEvent) -> None:
    """Handle group message and generate AI response if appropriate.

    Args:
        bot: NoneBot bot instance
        event: Group message event
    """
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

    # Use raw_message first (contains original complete text before NoneBot processing)
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

    try:
        async with AsyncSessionLocal() as session:
            # 1. Extract context from recent messages
            logger.debug(
                f"[group_chat] Extracting context for group {group_id}",
                extra={"group_id": group_id, "user_id": user_id},
            )

            context_mgr = ContextManager()
            context, current_msg = await context_mgr.format_with_context(
                session=session,
                group_id=group_id,
                user_id=user_id,
                message_content=message_content,
                context_limit=30,
                bot_id=event.self_id,  # Pass bot's own ID for context identification
            )

            # 2. Judge whether and how to reply (first-tier AI)
            logger.debug(
                f"[group_chat] Judging message for group {group_id}",
                extra={"group_id": group_id, "user_id": user_id},
            )

            judger = MessageJudger()
            judge_result = await judger.judge_message(
                context=context,
                current_msg=current_msg,
                group_id=group_id,  # Pass group_id for silence mode management
            )

            # Print judge result details
            logger.info(
                f"[group_chat] Judge: should_reply={judge_result.should_reply}, "
                f"reply_type={judge_result.reply_type}, emotion={judge_result.emotion}",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "should_reply": judge_result.should_reply,
                    "reply_type": judge_result.reply_type,
                },
            )


            # Early exit if no reply needed (unless user is complaining, need apology)
            if not judge_result.should_reply and not judge_result.user_complaining_too_much:
                logger.debug(
                    f"[group_chat] Not replying: {judge_result.explanation}",
                    extra={"group_id": group_id},
                )
                return

            # 3. Generate response (second-tier AI)
            logger.debug(
                f"[group_chat] Generating response for group {group_id}",
                extra={"group_id": group_id},
            )

            conversation = ConversationService()
            response = await conversation.generate_response(
                session=session,
                context=context,
                judge_result=judge_result,
                group_id=group_id,
            )

            # 4. Send response
            await group_chat_handler.send(response)

            logger.info(
                f"[group_chat] Response sent to group {group_id}",
                extra={
                    "group_id": group_id,
                    "user_id": user_id,
                    "response_length": len(response),
                },
            )

            # 5. Save bot's response to database for context in future messages
            try:
                await GroupMessageService.save_message(
                    session=session,
                    group_id=group_id,
                    user_id=event.self_id,  # Bot's own ID
                    message_content=response,
                    message_type="text",
                )
                await session.commit()
                logger.debug(
                    f"[group_chat] Bot response saved to database",
                    extra={"group_id": group_id},
                )
            except Exception as e:
                logger.warning(
                    f"[group_chat] Failed to save bot response to database: {e}",
                    extra={"group_id": group_id},
                )

    except Exception as e:
        logger.error(
            f"[group_chat] Failed to process group message: {e}",
            extra={"group_id": group_id, "user_id": user_id},
            exc_info=True,
        )
        # Don't send error message, just log
