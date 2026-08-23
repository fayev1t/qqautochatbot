"""Contract tests for ③ 模块解耦 —— 包级 __init__ 不在 import 期拉重基础设施。

这套测试**能在本地裸环境跑**(未装 sqlalchemy / langchain):如果 `qqbot.core`
或 `qqbot.services.agent_loop` 的 __init__ 仍 eager 导入依赖 sqlalchemy 的
database/projection/worker 等,下面的 `import` 行会直接 ModuleNotFoundError。
能导入成功,本身就证明重模块已惰性化。

冻结的契约:
- `import qqbot.core` / `import qqbot.services.agent_loop` 不连带拉重模块
- 二者均提供 PEP 562 `__getattr__`,公开名映射表 `_LAZY` 完整
- eager 名(纯数据类 / 轻量子模块)即时可用
- 未知名 → AttributeError;惰性名 → 路由到真实 import(而非 AttributeError)
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_QQBOT_ROOT = Path(__file__).resolve().parents[1]


def _runtime_top_level_import_modules(source: str) -> frozenset[str]:
    """模块顶层 import 的模块名；跳过 ``if TYPE_CHECKING:`` 块。"""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return frozenset(names)


class CoreLazyImportTests(unittest.TestCase):
    def test_import_core_pulls_no_heavy_infra(self) -> None:
        import qqbot.core as core  # 若 eager 拉 database(建 engine)→ 这里就炸

        self.assertTrue(hasattr(core, "__getattr__"))
        # 公开 API 名仍在 __all__
        for name in ("init_db", "close_db", "get_db_session", "create_llm"):
            self.assertIn(name, core.__all__)

    def test_unknown_attr_raises(self) -> None:
        import qqbot.core as core

        with self.assertRaises(AttributeError):
            _ = core.does_not_exist

    def test_lazy_name_routes_to_import_not_attribute_error(self) -> None:
        import qqbot.core as core

        # 访问惰性名应触发真实导入:服务器(装了 sqlalchemy)拿到函数;本地裸环境
        # 抛 ModuleNotFoundError —— 但**绝不**是 AttributeError(那意味着没接上)。
        try:
            obj = core.init_db
        except ModuleNotFoundError:
            return
        self.assertTrue(callable(obj))


class AgentLoopLazyImportTests(unittest.TestCase):
    def test_import_agent_loop_pulls_no_heavy_infra(self) -> None:
        import qqbot.services.agent_loop as al  # eager 拉 projection/worker 就会炸

        self.assertTrue(hasattr(al, "__getattr__"))

    def test_pure_dataclasses_are_eager(self) -> None:
        # 纯 stdlib 依赖,任何环境都能直接取
        from qqbot.services.agent_loop import DecisionContext, TimelineItem

        self.assertEqual(TimelineItem.__name__, "TimelineItem")
        self.assertEqual(DecisionContext.__name__, "DecisionContext")

    def test_bot_registry_submodule_eager(self) -> None:
        from qqbot.services.agent_loop import bot_registry

        self.assertTrue(hasattr(bot_registry, "register"))

    def test_heavy_classes_are_lazy_mapped(self) -> None:
        import qqbot.services.agent_loop as al

        for name, submod in {
            "LLMPlanner": "llm_planner",
            "Projector": "projection",
            "LoopSupervisor": "supervisor",
            "ToolRegistry": "tool_registry",
            "AgentLoop": "loop",
        }.items():
            self.assertEqual(al._LAZY.get(name), submod)
            self.assertIn(name, al.__all__)

    def test_unknown_attr_raises(self) -> None:
        import qqbot.services.agent_loop as al

        with self.assertRaises(AttributeError):
            _ = al.NoSuchSymbol

    def test_lazy_name_routes_to_import_not_attribute_error(self) -> None:
        import qqbot.services.agent_loop as al

        # 见 core 同名用例:惰性名必须路由到 import,本地裸环境抛
        # ModuleNotFoundError 也算"接上了",AttributeError 才是断链。
        try:
            obj = al.Projector
        except ModuleNotFoundError:
            return
        self.assertEqual(obj.__name__, "Projector")


class EventIngestRegistryImportCycleTests(unittest.TestCase):
    """冻结 registry ↔ ingest 导入方向。

    2026-08-20 启动失败：``event_writer`` → ``registry`` → 包
    ``event_ingest.__init__`` eager 拉 ``ingest.py`` → 半初始化的
    ``registry.AdaptedEvent``。
    """

    def test_event_ingest_package_lazy_maps_ingest_symbols(self) -> None:
        import qqbot.services.event_ingest as ei

        self.assertTrue(hasattr(ei, "__getattr__"))
        self.assertEqual(ei._LAZY.get("EventIngest"), "ingest")
        self.assertEqual(ei._LAZY.get("IngestResult"), "ingest")
        self.assertIn("EventIngest", ei.__all__)
        self.assertIn("IngestResult", ei.__all__)
        self.assertEqual(ei.SystemEvent.__name__, "SystemEvent")

    def test_event_ingest_init_does_not_eager_import_ingest_module(self) -> None:
        source = (
            _QQBOT_ROOT / "qqbot" / "services" / "event_ingest" / "__init__.py"
        ).read_text(encoding="utf-8")
        names = _runtime_top_level_import_modules(source)
        self.assertNotIn("qqbot.services.event_ingest.ingest", names)

    def test_registry_has_no_runtime_module_level_event_ingest_import(self) -> None:
        source = (
            _QQBOT_ROOT / "qqbot" / "services" / "event_gateway" / "registry.py"
        ).read_text(encoding="utf-8")
        names = _runtime_top_level_import_modules(source)
        for name in names:
            self.assertFalse(
                name == "qqbot.services.event_ingest"
                or name.startswith("qqbot.services.event_ingest."),
                msg=f"registry 模块顶层不得导入 {name}",
            )

    def test_registry_type_aliases_do_not_eval_type_checking_names(self) -> None:
        """``PersistFn = Callable[[SystemEvent, ...]]`` 会在 import 期 NameError。

        ``from __future__ import annotations`` 只管注解，不管类型别名赋值。
        """
        source = (
            _QQBOT_ROOT / "qqbot" / "services" / "event_gateway" / "registry.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        deferred = {"SystemEvent", "PartialSystemEvent"}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for child in ast.walk(node.value):
                if isinstance(child, ast.Name) and child.id in deferred:
                    self.fail(
                        "registry 类型别名赋值不得在运行时求值 "
                        f"{child.id}（TYPE_CHECKING 专有名须写成字符串）"
                    )

    def test_registry_imports_without_cycle(self) -> None:
        try:
            from qqbot.services.event_gateway.registry import (
                AdaptedEvent,
                EventRegistrar,
                issue_event_id,
            )
        except ModuleNotFoundError:
            return
        self.assertTrue(callable(issue_event_id))
        self.assertEqual(AdaptedEvent.__name__, "AdaptedEvent")
        self.assertEqual(EventRegistrar.__name__, "EventRegistrar")

    def test_lazy_ingest_name_routes_to_import_not_attribute_error(self) -> None:
        import qqbot.services.event_ingest as ei

        try:
            obj = ei.EventIngest
        except ModuleNotFoundError:
            return
        except AttributeError:
            self.fail(
                "EventIngest 必须经 __getattr__ 路由到 ingest，不能 AttributeError"
            )
        self.assertEqual(obj.__name__, "EventIngest")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
