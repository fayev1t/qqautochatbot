"""LLM configuration and initialization."""

from typing import TYPE_CHECKING, Optional

from pydantic_settings import BaseSettings

if TYPE_CHECKING:
    from langchain_core.language_model.llm import LLM


class LLMConfig(BaseSettings):
    """LLM configuration from environment."""

    llm_provider: str = "deepseek"  # deepseek, openai, claude, etc.
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.7

    class Config:
        env_file = ".env.dev"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


async def create_llm() -> Optional["LLM"]:
    """Create and return LLM instance based on config.

    Returns:
        LLM instance or None if configuration is incomplete
    """
    config = LLMConfig()

    if not config.llm_api_key:
        print("Warning: LLM_API_KEY not configured")
        return None

    if config.llm_provider == "deepseek":
        try:
            from langchain_openai import ChatOpenAI

            # DeepSeek compatible with OpenAI API
            llm = ChatOpenAI(
                model_name=config.llm_model,
                api_key=config.llm_api_key,
                base_url="https://api.deepseek.com/v1",
                temperature=config.llm_temperature,
            )
            return llm
        except ImportError as e:
            print(f"Error: Failed to import ChatOpenAI: {e}")
            return None

    elif config.llm_provider == "openai":
        try:
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model_name=config.llm_model,
                api_key=config.llm_api_key,
                temperature=config.llm_temperature,
            )
            return llm
        except ImportError as e:
            print(f"Error: Failed to import ChatOpenAI: {e}")
            return None

    else:
        print(f"Unknown LLM provider: {config.llm_provider}")
        return None
