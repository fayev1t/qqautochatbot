"""Message judgment service (first-tier AI for decision-making)."""

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.llm import LLMConfig, create_llm
from qqbot.services.prompt import PromptManager
from qqbot.services.silence_mode import is_silent, set_silent

logger = logging.getLogger(__name__)


class JudgeResult:
    """Result of message judgment analysis."""

    def __init__(
        self,
        should_reply: bool,
        reply_type: str,
        target_user_id: int | None = None,
        emotion: str = "happy",
        explanation: str = "",
        instruction: str = "",
        should_mention: bool = False,
        user_complaining_too_much: bool = False,
        user_asking_to_speak: bool = False,
    ) -> None:
        """Initialize judgment result.

        Args:
            should_reply: Whether bot should reply to this message
            reply_type: Type of reply - "person", "topic", "knowledge", or "none"
            target_user_id: If replying to a person, their QQ ID
            emotion: Emotion for the response - happy/serious/sarcastic/confused/gentle
            explanation: Explanation of the judgment decision
            instruction: Instructions for the response generation layer
            should_mention: Whether to @ mention the target user (only in special cases)
            user_complaining_too_much: User is complaining bot talks too much
            user_asking_to_speak: User is asking bot to speak more
        """
        self.should_reply = should_reply
        self.reply_type = reply_type
        self.target_user_id = target_user_id
        self.emotion = emotion
        self.explanation = explanation
        self.instruction = instruction
        self.should_mention = should_mention
        self.user_complaining_too_much = user_complaining_too_much
        self.user_asking_to_speak = user_asking_to_speak

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JudgeResult":
        """Create JudgeResult from dictionary.

        Args:
            data: Dictionary with judgment result fields

        Returns:
            JudgeResult instance
        """
        return cls(
            should_reply=data.get("should_reply", False),
            reply_type=data.get("reply_type", "none"),
            target_user_id=data.get("target_user_id"),
            emotion=data.get("emotion", "happy"),
            explanation=data.get("explanation", ""),
            instruction=data.get("instruction", ""),
            should_mention=data.get("should_mention", False),
            user_complaining_too_much=data.get("user_complaining_too_much", False),
            user_asking_to_speak=data.get("user_asking_to_speak", False),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary.

        Returns:
            Dictionary representation of judgment result
        """
        return {
            "should_reply": self.should_reply,
            "reply_type": self.reply_type,
            "target_user_id": self.target_user_id,
            "emotion": self.emotion,
            "explanation": self.explanation,
            "instruction": self.instruction,
            "should_mention": self.should_mention,
            "user_complaining_too_much": self.user_complaining_too_much,
            "user_asking_to_speak": self.user_asking_to_speak,
        }


class MessageJudger:
    """First-tier AI for judging whether and how to reply to messages."""

    def __init__(self) -> None:
        """Initialize the message judger."""
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

            # Use lower temperature (0.6) for rational judgment
            self._llm = ChatOpenAI(
                model_name=self.config.llm_model,
                api_key=self.config.llm_api_key,
                base_url="https://api.deepseek.com/v1",
                temperature=0.6,
            )
        return self._llm

    async def judge_message(
        self,
        context: str,
        current_msg: str,
        group_id: int | None = None,
    ) -> JudgeResult:
        """Judge whether and how to reply to a message.

        Args:
            context: Formatted recent message context
            current_msg: Formatted current message
            group_id: QQ group ID for silence mode management

        Returns:
            JudgeResult with decision and guidance for response layer
        """
        try:
            llm = await self._get_llm()

            # Check silence mode status
            silence_mode = is_silent(group_id) if group_id else False

            # Build the prompt for LLM
            user_prompt = f"""【上下文信息】
{context}

【当前消息】
{current_msg}

请分析上述消息并判断：
1. 是否需要回复？
2. 如果回复，是针对某个人、对话题还是知识问题？
3. 应该用什么情绪去回复？
4. 用户是否在抱怨AI说话太多/太频繁？
5. 用户是否在催促AI说话/询问为什么不说话？
6. 给下一步生成AI的具体指导

输出JSON格式，必须包含字段：should_reply, reply_type, target_user_id, emotion, explanation, instruction, user_complaining_too_much, user_asking_to_speak"""

            # Modify judge prompt based on silence mode
            judge_prompt = self.prompt_manager.judge_prompt
            if silence_mode:
                judge_prompt += "\n\n【特殊状态：沉默模式激活】当前群处于沉默模式，你的说话意愿应该大幅降低。只有在以下情况才应该回复：用户明确@你、用户提出直接问题、或有重要信息需要传达。请相应降低 should_reply 的可能性。"

            # Add instruction for detecting user feedback
            judge_prompt += "\n\n【用户反馈检测】请仔细检测：\\n- 如果用户抱怨你说话太多、太频繁、太烦人，设置 user_complaining_too_much 为 true\\n- 如果用户催促你说话、问为什么不说话、觉得你太沉默，设置 user_asking_to_speak 为 true"

            logger.info(
                "[judge] Judging message",
                extra={
                    "silence_mode": silence_mode,
                },
            )

            # Call LLM with system prompt
            messages = [
                SystemMessage(content=judge_prompt),
                HumanMessage(content=user_prompt),
            ]

            response = await llm.ainvoke(messages)
            response_text = response.content.strip()

            print(f"[judge] API Response: {response_text}")

            # Parse JSON response
            try:
                # Try to extract JSON from response (in case there's extra text)
                json_start = response_text.find("{")
                json_end = response_text.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    json_str = response_text[json_start:json_end]
                    result_data = json.loads(json_str)
                else:
                    raise ValueError("No JSON found in response")
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"[MessageJudger] 【第一层AI】JSON解析失败: {response_text}\n{e}")
                # Return conservative result (don't reply) on parse error
                return JudgeResult(
                    should_reply=False,
                    reply_type="none",
                    explanation="Failed to parse judgment response",
                    instruction="",
                )

            judge_result = JudgeResult.from_dict(result_data)

            # Log judgment result
            print(f"[judge] Result: should_reply={judge_result.should_reply}, reply_type={judge_result.reply_type}, emotion={judge_result.emotion}")

            # Handle silence mode transitions
            if group_id:
                if judge_result.user_complaining_too_much:
                    set_silent(group_id, True)
                    print(f"[silence-mode] Entered: user complained about excessive talking")
                elif judge_result.user_asking_to_speak:
                    set_silent(group_id, False)
                    print(f"[silence-mode] Exited: user asked to speak more")

            return judge_result

        except Exception as e:
            logger.error(f"[MessageJudger] 【第一层AI】调用出错: {e}", exc_info=True)
            # Return conservative result on error
            return JudgeResult(
                should_reply=False,
                reply_type="none",
                explanation="AI judgment failed",
                instruction="",
            )
