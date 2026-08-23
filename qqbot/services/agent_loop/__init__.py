"""AgentLoop: v2 program-shaped decision/tick layer.

Contracts:
- 开发文档/v2.0/20-横切契约/任务与决策契约.md
- 开发文档/v2.0/20-横切契约/事件系统设计.md §11
- 开发文档/v2.0/20-横切契约/EventIngest契约.md §8

包级导入策略(③ 模块解耦,见开发日志 2026-06-23):
  纯数据类(decision)与轻量 bot_registry 保持 eager —— 它们只依赖 stdlib;
  但 **重模块惰性化**:LLMPlanner / Projector / AgentLoop /
  LoopSupervisor / ToolRegistry 等会拉 sqlalchemy(DB)或 langchain(LLM),
  改用 PEP 562 module `__getattr__` 按需导入。于是 `import qqbot.services.
  agent_loop`(或导入其任一**纯**子模块,如 decision)
  不再连带把整套运行时拉进来;`from qqbot.services.agent_loop import LLMPlanner`
  语义不变 —— 首次访问该名字时才 import llm_planner。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# 轻量、纯 stdlib 依赖 —— eager 导入无成本,且被测试/类型大量直接引用。
from qqbot.services.agent_loop import bot_registry
from qqbot.services.agent_loop.decision import (
    DecisionContext,
    DecisionOutput,
    ImageRef,
    MemeView,
    Planner,
    TimelineItem,
    ToolResultView,
)

# 重模块(拉 sqlalchemy / langchain)→ 惰性:公开名 → 所属子模块。
_LAZY: dict[str, str] = {
    "LLMPlanner": "llm_planner",
    "build_default_prompt_library": "llm_planner",
    "AgentLoop": "loop",
    "Projector": "projection",
    "PromptLibrary": "prompts.catalog",
    "LoopSupervisor": "supervisor",
    "Tool": "tool_registry",
    "ToolRegistry": "tool_registry",
}


def __getattr__(name: str):
    submodule = _LAZY.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"qqbot.services.agent_loop.{submodule}")
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # 静态分析仍看得到惰性符号
    from qqbot.services.agent_loop.llm_planner import (
        LLMPlanner,
        build_default_prompt_library,
    )
    from qqbot.services.agent_loop.loop import AgentLoop
    from qqbot.services.agent_loop.projection import Projector
    from qqbot.services.agent_loop.prompts.catalog import PromptLibrary
    from qqbot.services.agent_loop.supervisor import LoopSupervisor
    from qqbot.services.agent_loop.tool_registry import Tool, ToolRegistry

__all__ = [
    "AgentLoop",
    "DecisionContext",
    "DecisionOutput",
    "ImageRef",
    "LLMPlanner",
    "LoopSupervisor",
    "MemeView",
    "Planner",
    "PromptLibrary",
    "build_default_prompt_library",
    "Projector",
    "TimelineItem",
    "Tool",
    "ToolRegistry",
    "ToolResultView",
    "bot_registry",
]
