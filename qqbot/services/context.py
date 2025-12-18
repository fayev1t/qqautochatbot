"""Context extraction and formatting for conversation system."""

from datetime import datetime
from typing import Optional
import re

from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.services.group import GroupService
from qqbot.services.group_member import GroupMemberService
from qqbot.services.group_message import GroupMessageService
from qqbot.services.user import UserService


class ContextManager:
    """Extract and format group chat context for AI analysis."""

    @staticmethod
    async def parse_at_info(
        session: AsyncSession,
        group_id: int,
        message_content: str,
    ) -> str:
        """Parse CQ code @mentions and convert them to readable format.

        Converts [CQ:at,qq=123456] to @昵称(123456)

        Args:
            session: Database session
            group_id: QQ group ID
            message_content: Message content with CQ codes

        Returns:
            Message with @mentions converted to readable format
        """
        # Find all CQ at codes
        at_pattern = r"\[CQ:at,qq=(\d+)\]"
        matches = re.finditer(at_pattern, message_content)

        result = message_content
        for match in matches:
            at_qq = int(match.group(1))
            cq_code = match.group(0)

            # Get the user's display name
            try:
                user = await UserService.get_user(session, at_qq)
                user_nickname = user.get("nickname") if user else f"用户{at_qq}"
            except Exception:
                user_nickname = f"用户{at_qq}"

            try:
                member = await GroupMemberService.get_member(session, group_id, at_qq)
                group_card = member.get("card") if member else None
            except Exception:
                group_card = None

            display_name = group_card or user_nickname or f"用户{at_qq}"
            replacement = f"@{display_name}"

            result = result.replace(cq_code, replacement)

        return result

    @staticmethod
    async def get_recent_context(
        session: AsyncSession,
        group_id: int,
        limit: int = 30,
        bot_id: int | None = None,
    ) -> str:
        """Get recent message context before the current point.

        Fetches the most recent messages and formats them as:
        群员名(QQ) [时间] 说了: 内容
        或
        小奏 [时间] 说了: 内容

        Args:
            session: Database session
            group_id: QQ group ID
            limit: Number of recent messages to fetch (default: 30)
            bot_id: Bot's QQ ID for identifying bot messages (optional)

        Returns:
            Formatted context string with recent messages
        """
        # Get recent messages from database
        messages = await GroupMessageService.get_recent_messages(
            session=session,
            group_id=group_id,
            limit=limit,
        )

        if not messages:
            return "（暂无上下文消息）"

        context_lines: list[str] = []

        for msg in messages:
            user_id = msg.get("user_id")
            message_content = msg.get("message_content", "")
            timestamp = msg.get("timestamp")

            # Skip recalled messages
            is_recalled = msg.get("is_recalled", False)
            if is_recalled:
                continue

            # Check if this is a bot message
            if bot_id and user_id == bot_id:
                display_name = "小奏"
            else:
                # Get user info for regular users
                try:
                    user = await UserService.get_user(session, user_id)
                    user_nickname = user.get("nickname") if user else f"用户{user_id}"
                except Exception:
                    user_nickname = f"用户{user_id}"

                # Get group member card (group nickname)
                try:
                    member = await GroupMemberService.get_member(
                        session, group_id, user_id
                    )
                    group_card = member.get("card") if member else None
                except Exception:
                    group_card = None

                # Prioritize group card, fallback to QQ nickname
                display_name = group_card or user_nickname or f"用户{user_id}"

            # Format timestamp
            time_str = ""
            if isinstance(timestamp, datetime):
                time_str = timestamp.strftime("%H:%M")
            elif isinstance(timestamp, str):
                try:
                    dt = datetime.fromisoformat(timestamp)
                    time_str = dt.strftime("%H:%M")
                except (ValueError, TypeError):
                    time_str = timestamp[:5]  # Try to extract HH:MM

            # Parse @mentions in message content
            try:
                message_content = await ContextManager.parse_at_info(
                    session, group_id, message_content
                )
            except Exception:
                pass  # If parsing fails, keep original content

            line = f"{display_name}({user_id}) [{time_str}] 说了: {message_content}"
            context_lines.append(line)

        return "\n".join(context_lines)

    @staticmethod
    async def format_current_message(
        session: AsyncSession,
        group_id: int,
        user_id: int,
        message_content: str,
    ) -> str:
        """Format the current message being analyzed.

        Args:
            session: Database session
            group_id: QQ group ID
            user_id: QQ user ID of sender
            message_content: The message content

        Returns:
            Formatted message string
        """
        # Get user info
        try:
            user = await UserService.get_user(session, user_id)
            user_nickname = user.get("nickname") if user else f"用户{user_id}"
        except Exception:
            user_nickname = f"用户{user_id}"

        # Get group member card (group nickname)
        try:
            member = await GroupMemberService.get_member(
                session, group_id, user_id
            )
            group_card = member.get("card") if member else None
        except Exception:
            group_card = None

        # Prioritize group card, fallback to QQ nickname
        display_name = group_card or user_nickname or f"用户{user_id}"

        # Format timestamp (current time)
        time_str = datetime.now().strftime("%H:%M")

        return f"{display_name}({user_id}) [{time_str}] 说了: {message_content}"

    @staticmethod
    async def format_with_context(
        session: AsyncSession,
        group_id: int,
        user_id: int,
        message_content: str,
        context_limit: int = 30,
        bot_id: int | None = None,
    ) -> tuple[str, str]:
        """Get formatted context and current message together.

        Args:
            session: Database session
            group_id: QQ group ID
            user_id: QQ user ID of sender
            message_content: The message content
            context_limit: Number of recent messages for context
            bot_id: Bot's QQ ID for identifying bot messages (optional)

        Returns:
            Tuple of (formatted_context, formatted_current_message)
        """
        context = await ContextManager.get_recent_context(
            session=session,
            group_id=group_id,
            limit=context_limit,
            bot_id=bot_id,
        )

        current_msg = await ContextManager.format_current_message(
            session=session,
            group_id=group_id,
            user_id=user_id,
            message_content=message_content,
        )

        return context, current_msg
