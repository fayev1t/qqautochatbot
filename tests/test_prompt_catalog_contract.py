"""prompts/catalog.py 提示词库契约。

装配机制 2026-07-30 统一为**根页 + `{{槽}}`**：`CONSUMERS` 把消费者映射到根页
`.md`，页正文里的 `{{name}}` 决定要哪几段、什么顺序、怎么分隔。`ASSEMBLY` 与
`SECTION_SEP` 已删除，所以本文件不再钉"段清单"，改为钉每张根页实际用到的槽序列。

2026-07-31 共享资产并回根页：`persona.md` / `system.md` / `group_chat_rules.md`
三份文件已并进 `planner.md`（删除 Replyer 后它们只剩 Planner 一个消费者），页里
只剩 `{{envelope}}`（纯格式规范，与投影层成对维护，故留成独立文件）与动态的
`{{tools_usage}}`。所以本文件的"两份副本会漂移"这条防线换了钉法：不再比对
"卡片文件 vs 渲染结果"，而是从 `planner.md` 的人物模型段现取锚点，钉它**没有第二
份**（别的根页、`envelope.md`、`tools/*.md` 里都不许出现）。

2026-08-02 收藏描述换页：`caption` 消费者改读 `image_description.md`（原
`meme_caption.md` 删除），于是 CONSUMERS 里第一次出现**两个消费者共用一张根页**。
钉法照旧走第 1 条（登记表逐字节对账）+ 第 4 条（渲染产物与文件对账），额外钉两个
消费者渲染结果相同——共用是有意的，不是漏改。

钉住四件事：
1. 根页登记与各页的槽序列——它们是五个 LLM 调用点的 prompt 组成的唯一权威，
   改动必须是有意的（2026-07-31 删除 Replyer 后 Planner 是唯一对话消费者）；
2. 分工的**语义**边界：Planner 先声明角色模拟职责，角色卡正文只在后续人物模型段
   出现一份；纯记录/观察层的根页无槽。2026-07-30 删掉
   kind/_FORBIDDEN_KINDS/_validate_assembly 之后，这类断言是唯一还抓得到内容
   漂移的手段——结构校验的粒度是"哪个文件进哪个消费者"，而真实发生过的事故是
   "人格正文被抄进另一个文件"，那种它一声不响；
3. 空文件 fail loudly（部署坏了不静默跑残缺 prompt），动态段求值为空则跳过；
4. 装配产物与 prompts/*.md 文件逐字节对账。

文件读取走真实 prompts/ 目录（与部署同源）；库内核语义用内联 fake source，
不依赖文件系统。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from qqbot.services.agent_loop.prompts.catalog import (
    _FILES,
    _PROMPTS_DIR,
    CONSUMERS,
    DYNAMIC_SLOTS,
    SLOT_PATTERN,
    PromptLibrary,
    PromptSectionMissing,
    build_library,
    render_system_prompt,
)


class AssemblyPinningTests(unittest.TestCase):
    """装配现在完全写在根页的 `{{槽}}` 里（2026-07-30 起 ASSEMBLY 已删除）。"""

    def test_consumers_and_root_pages_are_pinned(self) -> None:
        self.assertEqual(
            CONSUMERS,
            {
                "planner": "planner.md",
                # 2026-08-02：收藏描述改读图片客观转录那张页（原
                # meme_caption.md 已删除）。同页两个消费者是有意的，改回去
                # = 加一张 .md + 改 CONSUMERS 一行。
                "caption": "image_description.md",
                "image_description": "image_description.md",
                "image_look": "image_look.md",
                "memory": "memory_compaction.md",
                # 2026-08-03：webfetch / websearch 抓取正文的内部提炼。
                "web_digest": "web_digest.md",
            },
        )

    def test_assembler_is_the_scene_registry(self) -> None:
        from qqbot.services.prompt_assembler import assemble, registered_scenes

        self.assertEqual(registered_scenes(), tuple(CONSUMERS))
        self.assertEqual(
            assemble("web_digest"), render_system_prompt("web_digest")
        )
        with self.assertRaises(KeyError):
            assemble("not_a_scene")

    def test_page_slot_lists_are_pinned(self) -> None:
        """每张根页实际用到哪几个槽、什么顺序——改动必须是有意的。

        2026-07-31 起 planner 只剩两个槽：`envelope`（信封语法，唯一的文件槽）
        与 `tools_usage`（动态，来自 ToolRegistry）。人格 / 机器事实 / 参与判断
        三段已并回页里，不再是文件。"""
        self.assertEqual(
            build_library("planner").slot_names(), ["envelope", "tools_usage"]
        )
        self.assertEqual(_FILES, {"envelope": "envelope.md"})

    def test_every_file_slot_has_a_real_file(self) -> None:
        for name, filename in {**_FILES, **CONSUMERS}.items():
            with self.subTest(asset=name):
                self.assertTrue((_PROMPTS_DIR / filename).is_file(), filename)

    def test_root_pages_are_not_slots(self) -> None:
        """硬规则：根页之间永不互相开槽。根页不登记为槽，页里写别的消费者名
        会按未知槽名炸掉。"""
        for consumer in CONSUMERS:
            with self.subTest(consumer=consumer):
                self.assertNotIn(consumer, _FILES)
        slots = build_library("planner").slot_names()
        for other in CONSUMERS:
            with self.subTest(other=other):
                self.assertNotIn(other, slots)

    def test_pages_only_reference_known_slots(self) -> None:
        """页里写的每个槽都必须能解析：文件槽在 _FILES，动态槽在 DYNAMIC_SLOTS。
        写错一个名字就是让 `{{typo}}` 字面量出现在模型眼前。"""
        known = set(_FILES) | set(DYNAMIC_SLOTS)
        for consumer, filename in CONSUMERS.items():
            text = (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
            for match in SLOT_PATTERN.finditer(text):
                with self.subTest(consumer=consumer, slot=match.group(1)):
                    self.assertIn(match.group(1), known)

    def test_unknown_consumer_raises(self) -> None:
        """未登记的消费者必须炸，不能静默给出空 system prompt。"""
        with self.assertRaises(KeyError):
            build_library("no_such_consumer")

    def test_legacy_assets_are_absent(self) -> None:
        """历史资产不得复活——两份都在时改一处忘另一处就是两个真相源。
        replyer.md 随 2026-07-31 删除 Replyer 一并删除；同日 persona /
        system / group_chat_rules 三份并回 planner.md，文件再出现就意味着同一段
        正文有两处出处（页里那份仍在注入，文件那份没人读，改错地方毫无反馈）。
        envelope.md **不在此列**：它仍是唯一的文件槽。"""
        for stale in (
            "xml_format.md",
            "protocol.md",
            "identity.md",
            "disposition.md",
            "replyer.md",
            "persona.md",
            "system.md",
            "group_chat_rules.md",
        ):
            self.assertFalse((_PROMPTS_DIR / stale).exists(), stale)
        for stale in (
            "xml_format",
            "protocol",
            "identity",
            "replyer",
            "replyer_composer",
            "persona",
            "system",
            "group_chat_rules",
        ):
            self.assertNotIn(stale, _FILES)


class LayerBoundaryTests(unittest.TestCase):
    """分工的语义边界 —— kind 结构校验删除之后的唯一防线。"""

    #: 人物模型段的边界：`# 人物模型` 起、到下一个一级标题为止。
    PERSONA_HEADING = "# 人物模型"

    @classmethod
    def _persona_block(cls) -> str:
        """planner.md 里的角色卡正文（2026-07-31 起卡片不再是独立文件）。"""
        page = (_PROMPTS_DIR / "planner.md").read_text(encoding="utf-8")
        lines = page.splitlines()
        start = lines.index(cls.PERSONA_HEADING) + 1
        end = next(
            (
                i
                for i in range(start, len(lines))
                if lines[i].startswith("# ")
            ),
            len(lines),
        )
        return "\n".join(lines[start:end])

    @classmethod
    def _persona_anchors(cls) -> list[str]:
        """卡片里的角色长句——写死原句的话卡片一改断言就假通过，所以现取。"""
        return [
            line.strip()
            for line in cls._persona_block().splitlines()
            if len(line.strip()) > 24
            and line.strip().startswith(("你要持续模拟", "小奏", "她", "QQ 号"))
        ]

    def test_persona_card_reaches_the_planner(self) -> None:
        """角色卡进唯一的对话消费者 Planner（2026-07-31 删除 Replyer 后分析
        与措辞同归一层）。钉的是**卡片还在、且渲染没被槽残渣污染**：段被删空、
        标题被改名都要在这里当场红。"""
        anchors = self._persona_anchors()
        self.assertTrue(anchors, "planner.md 人物模型段没有角色锚点，断言会假通过")
        rendered = build_library("planner").render(scope="group")
        for line in anchors:
            self.assertIn(line, rendered)
        self.assertIsNone(SLOT_PATTERN.search(rendered))

    def test_planner_frames_the_card_as_character_model(self) -> None:
        """Planner 先钉角色模拟职责，再读取第三人称人物模型；最终措辞仍由
        Planner 同层完成，不恢复 Replyer。"""
        rendered = build_library("planner").render(scope="group")
        first = self._persona_anchors()[0]
        self.assertIn("角色决策规划器，不是通用问答助手", rendered)
        self.assertNotIn("\n你是小奏，", rendered)
        self.assertLess(
            rendered.index("# 身份与核心任务"), rendered.index(first)
        )
        self.assertIn("只能是小奏本人此刻会说的话", rendered)

    def test_persona_body_has_no_second_copy(self) -> None:
        """人格正文全系统只此一份。真实发生过的事故是"卡片被抄进另一个文件"
        （旧 planner.md 的第三人称投影、voice.md、replyer.md、
        tools/send_message.md 的 Voice 节），两份都在时改一处忘另一处就当场
        自相矛盾。2026-07-31 把卡片并进 planner.md 之后，这条防线只剩这里：
        锚点现取，扫遍别的根页、文件槽与全部工具用法文档。"""
        anchors = self._persona_anchors()
        self.assertTrue(anchors, "planner.md 人物模型段没有角色锚点，断言会假通过")
        others = [
            _PROMPTS_DIR / filename
            for consumer, filename in CONSUMERS.items()
            if consumer != "planner"
        ]
        others += [_PROMPTS_DIR / filename for filename in _FILES.values()]
        others += sorted((_PROMPTS_DIR.parent / "tools").glob("*.md"))
        for path in others:
            text = path.read_text(encoding="utf-8")
            for line in anchors:
                with self.subTest(file=path.name):
                    self.assertNotIn(line, text)

    def test_real_assembly_fails_loudly_when_the_page_is_empty(self) -> None:
        """根页为空必须 raise：残缺人格的 prompt 照跑，产出的是一个没有性格的
        账号，而从日志上看一切正常——这正是最难发现的坏法。走真实装配路径
        （build_library），不只是内核。"""
        from unittest.mock import patch

        from qqbot.services.agent_loop.prompts import catalog

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in {**_FILES, **CONSUMERS}.values():
                (root / filename).write_text(
                    (_PROMPTS_DIR / filename).read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            (root / "planner.md").write_text("   \n", encoding="utf-8")
            with patch.object(catalog, "_PROMPTS_DIR", root):
                with self.assertRaisesRegex(PromptSectionMissing, "planner"):
                    build_library("planner").render(scope="group")

    def test_real_assembly_fails_loudly_when_the_page_is_missing(self) -> None:
        """根页文件不在 = 部署损坏，读盘异常原样上抛（llm_planner 兜底降级
        idle），绝不静默渲染一个没有人格的 prompt。"""
        from unittest.mock import patch

        from qqbot.services.agent_loop.prompts import catalog

        with patch.dict(
            catalog.CONSUMERS,
            {"planner": "__no_such_planner_page__.md"},
        ):
            with self.assertRaises(FileNotFoundError):
                build_library("planner").render(scope="group")

    def test_record_layers_read_only_their_own_page(self) -> None:
        """纯记录/观察层的输出会被永久写进事件正文并被下游反复读取，掺进人格
        或群规就是污染所有下游语境（记忆系统契约 §5.1）。它们的根页里不许有
        任何槽。"""
        for consumer in (
            "caption",
            "image_description",
            "image_look",
            "memory",
        ):
            with self.subTest(consumer=consumer):
                self.assertEqual(build_library(consumer).slot_names(), [])


class LibraryKernelTests(unittest.TestCase):
    """根页 + 槽的内核语义（纯内联 source，不碰文件系统）。"""

    def test_slots_expand_in_page_order(self) -> None:
        lib = PromptLibrary("A\n\n{{x}}\n\nB\n\n{{y}}", {"x": "XX", "y": "YY"})
        self.assertEqual(lib.slot_names(), ["x", "y"])
        self.assertEqual(lib.render(), "A\n\nXX\n\nB\n\nYY")

    def test_page_without_slots_is_verbatim(self) -> None:
        lib = PromptLibrary("just a page\n", {"x": "XX"})
        self.assertEqual(lib.slot_names(), [])
        self.assertEqual(lib.render(), "just a page")

    def test_separators_come_from_the_page_not_the_library(self) -> None:
        """分隔符是页正文里的字符——库不再替页决定段之间长什么样。"""
        lib = PromptLibrary("{{x}}\n---\n{{y}}", {"x": "XX", "y": "YY"})
        self.assertEqual(lib.render(), "XX\n---\nYY")

    def test_get_by_name(self) -> None:
        lib = PromptLibrary("{{a}}{{b}}", {"a": "AAA", "b": lambda: "BBB"})
        self.assertEqual(lib.get("a"), "AAA")
        self.assertEqual(lib.get("b"), "BBB")
        with self.assertRaises(KeyError):
            lib.get("nope")

    def test_add_overwrites_and_remove_has(self) -> None:
        lib = PromptLibrary("{{a}}", {"a": "AAA"})
        self.assertTrue(lib.has("a"))
        lib.add("a", "NEW")
        self.assertEqual(lib.render(), "NEW")
        lib.remove("a")
        self.assertFalse(lib.has("a"))

    def test_callable_source_is_lazy(self) -> None:
        """render 时才求值 —— 改 .md 立即生效靠的就是这一点。"""
        calls: list[int] = []

        def source() -> str:
            calls.append(1)
            return "X"

        lib = PromptLibrary("{{a}}", {"a": source})
        self.assertEqual(calls, [])
        lib.render()
        self.assertEqual(calls, [1])

    def test_scope_is_passed_only_to_sources_that_accept_it(self) -> None:
        """钉的是 scope 只路由给收位置参的 source。槽用换行分隔——同一行内
        槽左右的空格按设计会被替换吃掉（见模块 docstring），这里不顺带把
        空格行为钉成契约。"""
        lib = PromptLibrary(
            "{{scoped}}\n{{plain}}",
            {"scoped": lambda scope: f"S={scope}", "plain": lambda: "P"},
        )
        self.assertEqual(lib.render(scope="group"), "S=group\nP")

    def test_empty_dynamic_slot_takes_its_separator_with_it(self) -> None:
        """未注入工具注册表时 tools_usage 求值为空：整槽跳过，且不能在页尾
        留下一条孤零零的分隔线。"""
        lib = PromptLibrary("AAA\n\n---\n\n{{tools_usage}}\n", {"tools_usage": "  "})
        self.assertEqual(lib.render(), "AAA")

    def test_empty_file_slot_fails_loudly(self) -> None:
        """文件槽为空 = 部署坏了，绝不静默跑残缺 prompt。"""
        lib = PromptLibrary("{{envelope}}", {"envelope": lambda: ""})
        with self.assertRaisesRegex(PromptSectionMissing, "envelope"):
            lib.render()

    def test_empty_page_fails_loudly(self) -> None:
        lib = PromptLibrary(lambda: "   ", {}, name="planner")
        with self.assertRaisesRegex(PromptSectionMissing, "planner"):
            lib.render()

    def test_unknown_slot_fails_loudly(self) -> None:
        """槽名写错/资产改名时静默留下一个 `{{typo}}` 字面量会直接出现在模型
        眼前，比缺一整段更难发现。"""
        lib = PromptLibrary("A {{nope}} B", {"a": "AAA"}, name="planner")
        with self.assertRaisesRegex(PromptSectionMissing, "nope"):
            lib.render()

    def test_source_exception_propagates(self) -> None:
        def boom() -> str:
            raise ValueError("deployment broken")

        lib = PromptLibrary("{{a}}", {"a": boom})
        with self.assertRaisesRegex(ValueError, "deployment broken"):
            lib.render()

    def test_render_sections_joins_back_to_render(self) -> None:
        """快照的分段统计与真正送进模型的字节必须逐字节一致（无额外分隔符）。"""
        lib = PromptLibrary("A\n{{x}}\nB", {"x": "XX"}, name="page")
        sections = lib.render_sections()
        self.assertEqual([s.name for s in sections], ["page", "x", "page"])
        self.assertEqual("".join(s.text for s in sections), lib.render())


class FileAssemblyTests(unittest.TestCase):
    """装配产物 ↔ prompts/*.md 逐字节对账。"""

    @staticmethod
    def _md(name: str) -> str:
        return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()

    def _expand(self, consumer: str) -> str:
        """独立复算一遍装配：根页原文里每个槽换成对应资产（已 strip），求值为空
        的动态槽连它前面那条分隔线一起去掉（槽在页中时同样成立），首尾再
        strip。与 catalog 的实现互为
        对照——两边同时写错才可能假通过。"""
        page = (_PROMPTS_DIR / CONSUMERS[consumer]).read_text(encoding="utf-8")
        page = re.sub(
            r"\n[ \t]*-{3,}[ \t]*\n[ \t]*\{\{tools_usage\}\}[ \t]*", "", page
        )
        return SLOT_PATTERN.sub(
            lambda m: self._md(_FILES[m.group(1)])
            if m.group(1) in _FILES
            else "",
            page,
        ).strip()

    def test_planner_render_matches_md_files(self) -> None:
        """渲染结果 = planner.md 正文 + 就地展开的 envelope.md（无注册表时页中的
        tools_usage 连同它前面那条分隔线一起消失）。envelope 归输入数据段，
        tools_usage 归工具段。"""
        rendered = build_library("planner").render(scope="group")
        self.assertEqual(rendered, self._expand("planner"))
        page = self._md("planner.md")
        self.assertIn("\n---\n{{tools_usage}}\n", page)
        self.assertLess(
            page.index("# 决策要求"), page.index("{{tools_usage}}")
        )
        self.assertLess(page.index("{{tools_usage}}"), page.index("# 输出协议"))
        self.assertIn(self._md("envelope.md"), rendered)

    def test_planner_page_carries_every_merged_topic(self) -> None:
        """并页的主题必须逐个还在，且顺序即模型的阅读顺序：
        身份任务 → 系统运行 → 人物模型 → 输入信封 → 决策要求 → 工具 → 输出。
        掉一段就是掉一整块语境，而渲染不会报错。"""
        rendered = build_library("planner").render(scope="group")
        anchors = [
            "# 身份与核心任务",
            "# 系统运行方式",  # 原 system.md（机器事实+行动纪律+提案-裁决流水线）
            "# 人物模型",  # 原 persona.md
            "# 输入数据",
            "输入信封格式规范",  # envelope.md 槽（仍是独立文件）
            "# 决策要求",
            "# 工具",
            "# 输出协议",
        ]
        positions = []
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, rendered)
            positions.append(rendered.index(anchor))
        self.assertEqual(positions, sorted(positions))

    def test_envelope_slot_is_the_file_itself(self) -> None:
        """信封语法的唯一出处是 envelope.md——它是 2026-07-31 并页后仅存的文件
        槽（纯格式规范，与投影层渲染成对维护）。2026-08-03 起信封为行文法：
        锚点取当前行型标记；XML 时代的元素名（<agent-input> /
        <reply-task-completed> / <my-reply> / <replyer-input>）不得复现。"""
        planner_env = build_library("planner").get("envelope")
        self.assertEqual(planner_env, self._md("envelope.md"))
        for tag in (
            "<t>", "<msg>", "<tool>", "<action>", "<program_result>",
            "<reflection>", "<invalid_action>", "<notice>", "<system>",
            "<recall>", "<now>", "<memes>", "[img ", "[@ ", "[face ",
        ):
            self.assertIn(tag, planner_env)
        self.assertNotIn("<校验拒绝>", planner_env)
        for stale in (
            "<agent-input>",
            "<reply-task-completed>",
            "<my-reply>",
            "<replyer-input>",
            "<my-thought",
        ):
            self.assertNotIn(stale, planner_env)

    def test_planner_entry_delegates_to_catalog(self) -> None:
        from qqbot.services.agent_loop.llm_planner import (
            build_default_prompt_library,
        )

        self.assertEqual(
            build_default_prompt_library().render(scope="group"),
            build_library("planner").render(scope="group"),
        )

    def test_tools_usage_rendered_with_registry(self) -> None:
        from qqbot.services.agent_loop.tools import build_default_registry

        sections = build_library(
            "planner", tool_registry=build_default_registry()
        ).render_sections(scope="group")
        names = [sec.name for sec in sections]
        self.assertIn("tools_usage", names)
        idx = names.index("tools_usage")
        self.assertTrue(sections[idx].text)
        # 槽在页中：其后还有输出协议正文。
        self.assertIn("# 输出协议", sections[-1].text)

    def test_tools_usage_skipped_without_registry(self) -> None:
        names = [
            sec.name for sec in build_library("planner").render_sections()
        ]
        self.assertNotIn("tools_usage", names)

    def test_legacy_voice_asset_is_absent(self) -> None:
        """角色卡的居所只能有一处（planner.md 人物模型段）：voice.md / replyer.md /
        persona.md 都不得复活——两份都在时 prompt 里会前后各读一遍人格，改一处
        就当场自相矛盾。（正文层面的"没有第二份副本"钉在
        LayerBoundaryTests.test_persona_body_has_no_second_copy。）"""
        self.assertFalse((_PROMPTS_DIR / "voice.md").exists())
        self.assertNotIn("voice", _FILES)
        for filename in CONSUMERS.values():
            text = (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
            for match in SLOT_PATTERN.finditer(text):
                self.assertNotEqual(match.group(1), "voice")

    def test_caption_render_matches_file(self) -> None:
        """2026-08-02：收藏描述与 timeline 图片转录读同一张页，逐字节相同。

        旧钉法（比对 meme_caption.md + `150 字` 限长锚点）随该文件一并删除：
        现在这条链的产出上界由 meme_caption.MAX_DESCRIPTION_CHARS 兜底，
        提示词里不再有字数要求。"""
        rendered = render_system_prompt("caption")
        self.assertEqual(rendered, self._md("image_description.md"))
        self.assertEqual(rendered, render_system_prompt("image_description"))
        self.assertFalse((_PROMPTS_DIR / "meme_caption.md").exists())

    def test_image_description_render_matches_file(self) -> None:
        rendered = render_system_prompt("image_description")
        self.assertEqual(rendered, self._md("image_description.md"))

    def test_image_look_render_matches_file(self) -> None:
        rendered = render_system_prompt("image_look")
        self.assertEqual(rendered, self._md("image_look.md"))

    def test_memory_render_matches_file(self) -> None:
        rendered = render_system_prompt("memory")
        self.assertEqual(rendered, self._md("memory_compaction.md"))
        self.assertIn("<recall-cues>", rendered)
        self.assertNotIn("只输出一个 JSON 对象", rendered)


if __name__ == "__main__":
    unittest.main()
