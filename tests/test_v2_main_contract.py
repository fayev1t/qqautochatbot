"""Contract for the v2 main plugin (qqbot.plugins.v2_main).

Static-only. Verifies the plugin is wired up to:
- import EventIngest + mapper registry
- register message / notice / request / metaevent handlers at priority=10 block=True
- register bot to bot_registry inside every handler
- delegate heartbeat to EventIngest internal bypass
- translate only committed SystemEvent values into AgentLoop wakes
- swallow ingest exceptions so napcat doesn't retry-spin
- launch LoopSupervisor on startup, stop on shutdown
- register the daily 00:00 <background> job, plus its lifecycle.connect catch-up
- be discoverable by both __main__ PLUGIN_MODULES and pyproject plugin_dirs
- v1 plugins MUST NOT appear in PLUGIN_MODULES (v1 fully discarded)
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V2MainPluginContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin_text = (ROOT / "qqbot" / "plugins" / "v2_main.py").read_text(
            encoding="utf-8"
        )
        self.main_text = (ROOT / "qqbot" / "__main__.py").read_text(encoding="utf-8")
        self.pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.ingest_text = (
            ROOT / "qqbot" / "services" / "event_ingest" / "ingest.py"
        ).read_text(encoding="utf-8")

    def test_plugin_opens_gateway_and_one_second_window(self) -> None:
        self.assertIn("registration_window_seconds=1.0", self.plugin_text)
        self.assertIn("batch_image_describer=_describe_images", self.plugin_text)
        self.assertIn("set_inbound_gateway", self.plugin_text)
        self.assertIn("tool_registry=sup._tool_registry", self.plugin_text)

    def test_plugin_imports_event_ingest(self) -> None:
        self.assertIn(
            "from qqbot.services.event_ingest.mappers import build_default_registry",
            self.plugin_text,
        )

    def test_plugin_imports_agent_loop_and_tools(self) -> None:
        self.assertIn("LLMPlanner", self.plugin_text)
        self.assertIn("LoopSupervisor", self.plugin_text)
        self.assertIn("bot_registry", self.plugin_text)
        self.assertIn(
            "from qqbot.services.agent_loop.tools import",
            self.plugin_text,
        )
        self.assertIn("build_default_registry as build_tool_registry", self.plugin_text)

    def test_plugin_uses_async_session_local(self) -> None:
        self.assertIn(
            "from qqbot.core.database import AsyncSessionLocal", self.plugin_text
        )
        self.assertIn("session_factory=AsyncSessionLocal", self.plugin_text)

    def test_plugin_registers_all_four_handler_types_at_priority_10_block_true(
        self,
    ) -> None:
        # v2 是唯一消费者：block=True 保证事件不会被任何其他 matcher 二次处理。
        self.assertIn("on_message(priority=10, block=True)", self.plugin_text)
        self.assertIn("on_notice(priority=10, block=True)", self.plugin_text)
        self.assertIn("on_request(priority=10, block=True)", self.plugin_text)
        self.assertIn("on_metaevent(priority=10, block=True)", self.plugin_text)

    def test_handlers_register_bot_to_registry(self) -> None:
        # Program Effect 工具依赖 bot_registry 反查 Bot 实例。
        self.assertIn("bot_registry.register(bot)", self.plugin_text)
        self.assertIn("_remember_bot(bot)", self.plugin_text)

    def test_ingest_handles_heartbeat_via_bypass(self) -> None:
        # heartbeat 不入 agent_events，走文件旁路（EventIngest契约 §7）
        self.assertIn("write_heartbeat", self.ingest_text)
        self.assertIn('"heartbeat"', self.ingest_text)
        self.assertIn("meta_event_type", self.ingest_text)

    def test_agent_loop_wake_is_driven_by_committed_system_event(self) -> None:
        self.assertIn(
            "committed_notifier=_notify_committed_event",
            self.plugin_text,
        )
        self.assertIn(
            "async def _notify_committed_event(event: SystemEvent)",
            self.plugin_text,
        )
        self.assertIn("apply_silence_gate", self.plugin_text)
        self.assertIn("wake=sup.wake", self.plugin_text)
        self.assertIn("note_activity=sup.note_activity", self.plugin_text)
        self.assertNotIn("supervisor=_get_supervisor()", self.plugin_text)

    def test_raw_notice_and_meta_events_do_not_reach_role_reflection(self) -> None:
        self.assertIn("_newly_committed_event(result)", self.plugin_text)
        self.assertNotIn(
            "reflect_bot_role_from_notice(bot, event",
            self.plugin_text,
        )
        self.assertNotIn(
            "reflect_bot_role_from_meta(bot, event",
            self.plugin_text,
        )

    def test_plugin_swallows_handler_exceptions(self) -> None:
        self.assertIn("except Exception", self.plugin_text)
        self.assertIn("swallowed", self.plugin_text)

    def test_plugin_has_no_persona_plumbing(self) -> None:
        # 钉的是 **plugin 层**没有人格管线：角色卡的装配全部由
        # prompts/catalog.py 负责（planner.md 人物模型段），插件不碰——
        # plugin_text 里出现 persona 字样依然是接线倒退。角色卡历经
        # tools/send_message.md Voice 节、prompts/voice.md、prompts/replyer.md
        # （三者均已删除）、prompts/persona.md（2026-07-31 并回根页），现居
        # prompts/planner.md。
        self.assertNotIn("persona", self.plugin_text)
        # 职责与角色卡同页（planner.md），必须存在且非空
        prompts_dir = ROOT / "qqbot" / "services" / "agent_loop" / "prompts"
        identity_text = (prompts_dir / "planner.md").read_text(encoding="utf-8")
        self.assertIn("# 身份与核心任务", identity_text)
        self.assertIn("# 系统运行方式", identity_text)
        self.assertIn("# 人物模型", identity_text)
        self.assertIn("# 决策要求", identity_text)
        self.assertIn("小奏", identity_text)
        self.assertIn("创造者", identity_text)
        # 旧居所不得复活（防两处副本漂移；send_message.md / replyer.md 随
        # 2026-07-31 删除 Replyer 一并删除，persona.md 同日并回 planner.md）
        self.assertFalse((prompts_dir / "voice.md").exists())
        self.assertFalse((prompts_dir / "replyer.md").exists())
        self.assertFalse((prompts_dir / "persona.md").exists())
        self.assertFalse(
            (
                ROOT / "qqbot" / "services" / "agent_loop" / "tools" / "send_message.md"
            ).exists()
        )

    def test_request_handler_wires_auto_approval(self) -> None:
        # 2026-07-03 拆分：request handler 在 ingest 返回后调自动审批（好友申请 /
        # 邀请入群不走 LLM，见EventIngest契约.md §2）。_ingest_event 须把
        # IngestResult 传出来供其判断 inserted / 事件类型。
        self.assertIn(
            "from qqbot.services.request_auto_approval import maybe_auto_approve",
            self.plugin_text,
        )
        self.assertIn(
            "await maybe_auto_approve(bot, result, AsyncSessionLocal)",
            self.plugin_text,
        )
        self.assertIn("result = await _ingest_event(event)", self.plugin_text)

    def test_plugin_starts_and_stops_supervisor(self) -> None:
        self.assertIn("@_driver.on_startup", self.plugin_text)
        self.assertIn("@_driver.on_shutdown", self.plugin_text)
        self.assertIn("supervisor", self.plugin_text)
        self.assertIn(".start()", self.plugin_text)
        self.assertIn(".stop()", self.plugin_text)

    def test_daily_background_is_wired_on_both_entrances(self) -> None:
        """每日背景两条入口（2026-08-21，渲染格式表 §一①）。

        调度器那条给出"每天一条"；lifecycle.connect 那条兜住"00:00 时进程是
        关着的"——只有前者的话，白天重启一次就整天没有 ``<background>``，比它
        取代的那个常驻头部还差。两条共用同一个幂等判据，所以叠加不会写重。

        注册点在本 plugin 而不是 ``startup.py``：那边只管 DB 与调度器本身，
        不认识 napcat。
        """
        self.assertIn(
            "from qqbot.services.agent_loop.daily_background import",
            self.plugin_text,
        )
        self.assertIn(
            "register_daily_background_job(AsyncSessionLocal)", self.plugin_text
        )
        self.assertIn(
            "schedule_catch_up_from_meta(bot, committed, AsyncSessionLocal)",
            self.plugin_text,
        )
        startup_text = (
            ROOT / "qqbot" / "plugins" / "startup.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("daily_background", startup_text)

    def test_no_legacy_toggle_env_vars(self) -> None:
        # v1 已删，过渡 env 开关也跟着删掉
        self.assertNotIn("QQBOT_V2_INGEST_ENABLED", self.plugin_text)
        self.assertNotIn("QQBOT_V2_LOOP_ENABLED", self.plugin_text)

    def test_plugin_listed_in_main_module_list(self) -> None:
        self.assertIn('"qqbot.plugins.v2_main"', self.main_text)

    def test_main_does_not_load_v1_plugins(self) -> None:
        # v1 三个 plugin 必须从 PLUGIN_MODULES 移除
        self.assertNotIn("event_handlers", self.main_text)
        self.assertNotIn("group_chat", self.main_text)
        self.assertNotIn("friend_private", self.main_text)
        self.assertNotIn("sync_nicknames", self.main_text)

    def test_pyproject_plugin_dirs_covers_qqbot_plugins(self) -> None:
        self.assertIn('plugin_dirs = ["qqbot/plugins"]', self.pyproject_text)


if __name__ == "__main__":
    unittest.main()
