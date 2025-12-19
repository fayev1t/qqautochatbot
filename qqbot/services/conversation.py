"""Conversation service for generating AI responses (second-tier AI)."""

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.llm import LLMConfig, create_llm
from qqbot.services.group_member import GroupMemberService
from qqbot.services.prompt import PromptManager
from qqbot.services.user import UserService
from qqbot.services.block_judge import JudgeResult

logger = logging.getLogger(__name__)


class ConversationService:
    """Service for generating AI responses based on judgment and context."""

    def __init__(self) -> None:
        """Initialize the conversation service."""
        self.config = LLMConfig()
        self.prompt_manager = PromptManager()
        self._llm = None

    async def _get_llm(self) -> Any:
        """Get or create LLM instance.

        Returns:
            LLM instance for making API calls
        """
        if self._llm is None:
            from langchain_openai import ChatOpenAI

            # Use higher temperature (0.9) for creative response generation
            self._llm = ChatOpenAI(
                model_name=self.config.llm_model,
                api_key=self.config.llm_api_key,
                base_url="https://api.deepseek.com/v1",
                temperature=0.9,
            )
        return self._llm

    async def generate_response(
        self,
        session: AsyncSession,
        context: str,
        judge_result: JudgeResult,
        group_id: int | None = None,
    ) -> str:
        """Generate a response based on judgment and context.

        Args:
            session: Database session
            context: Formatted message context
            judge_result: Result from first-tier judgment
            group_id: Group ID for looking up member info (optional)

        Returns:
            Generated response text (with @ mention if applicable)
        """
        try:
            llm = await self._get_llm()

            # Build response prompt with guidance from judge layer
            response_prompt = f"""【对话背景】
{context}

【当前指导】
{judge_result.instruction}

【回复要求】
- 情绪: {judge_result.emotion}
- 回复类型: {judge_result.reply_type}"""

            response_prompt += "\n\n请根据以上信息生成一条自然的群聊回复。注意保持小奏的人格特征。"

            print(f"[conversation] Generating response: {judge_result.reply_type}, {judge_result.emotion}")

            # Get messages for LLM call
            messages = [
                SystemMessage(content=self.prompt_manager.response_prompt),
                HumanMessage(content=response_prompt),
            ]

            # Call LLM to generate response
            response = await llm.ainvoke(messages)
            generated_text = response.content.strip()

            # Add @mention prefix if replying to a specific person AND should_mention=true
            if (
                judge_result.reply_type == "person"
                and judge_result.target_user_id
                and judge_result.should_mention
                and group_id
            ):
                try:
                    # Get target user info for @ mention
                    target_user = await UserService.get_user(
                        session, judge_result.target_user_id
                    )
                    target_member = await GroupMemberService.get_member(
                        session, group_id, judge_result.target_user_id
                    )

                    target_card = (
                        target_member.get("card")
                        if target_member
                        else target_user.get("nickname") if target_user else None
                    )
                    target_name = (
                        target_card or
                        (target_user.get("nickname") if target_user else None) or
                        f"用户{judge_result.target_user_id}"
                    )

                    # Format as CQ code mention
                    prefix = f"[CQ:at,qq={judge_result.target_user_id}] "
                    generated_text = prefix + generated_text

                    logger.debug(
                        "[ConversationService] 【第二层AI】添加@提及",
                        extra={
                            "layer": "response",
                            "target_user_id": judge_result.target_user_id,
                            "target_name": target_name,
                        },
                    )

                except Exception as e:
                    logger.debug(f"[conversation] Failed to add mention: {e}")
                    # Continue without mention if lookup fails

            print(f"[conversation] Response generated ({len(generated_text)} chars): {generated_text[:50]}...")

            return generated_text

        except Exception as e:
            logger.error(f"[ConversationService] 【第二层AI】调用出错: {e}", exc_info=True)
            # Return fallback response on error
            return "喵~让我想想..."
