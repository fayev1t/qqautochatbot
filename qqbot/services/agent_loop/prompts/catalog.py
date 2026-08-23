"""提示词库 — 每个消费者一张根页 `.md`，动态内容由页内 `{{槽}}` 拼进来。

系统里六个 LLM 调用点（Planner / meme caption / image description /
look_at_image / 记忆压缩 / 网页正文提炼）的 system prompt 都从这里装配。
**只有一种装配机制**：
`CONSUMERS` 把消费者映射到它的根页 `.md`，根页正文里写 `{{name}}` 就把对应资产
拼在那个位置。改提示词 = 改 `prompts/` 下的 `.md`（render 时才读盘，改完即生效，
无需重启）；给新消费者配 prompt = 加一张根页 + 在 `CONSUMERS` 里登记。

**2026-07-30 统一为槽**：此前是两套机制并存——`ASSEMBLY` 列出段名按序拼接，
外加一个 `PERSONA_SLOT` 专供角色卡就地替换。同一件事两种做法，且顺序（在
`ASSEMBLY` 里）与框定（在 `.md` 里）分居两处，读页的人看不出隔壁那句话最后会落
在哪一段旁边。现在位置、顺序、分隔符全部由根页自己写死，看得见即所得。
`ASSEMBLY` 与 `SECTION_SEP` 一并删除——段间那行 `---` 现在是各页正文里的字符。

**2026-07-31 删除 Replyer**（v2.0/30-工具设计/发言链路设计.md §7）：`replyer.md`
根页随之删除，Planner 独自承担分析与最终措辞。

**2026-07-31 共享资产并回根页**：`persona.md` / `system.md` /
`group_chat_rules.md` 三份文件并进 `planner.md`，三个文件槽一并删除。删除
Replyer 之后它们只剩 Planner 一个消费者，"共享资产"已无人共享，拆分只剩代价：
模型实际读到的那一整段文字要跨几个文件才拼得出来，而顺序与分隔又只写在根页里。
现在 Planner 的人格、系统事实、行动纪律与输出契约按模型的阅读顺序写在同一张
页上。2026-08-01 进一步改为角色模拟框架：身份与核心任务 → 系统运行方式 →
人物模型 → 输入数据（`{{envelope}}`）→ 决策要求 → 工具
（`{{tools_usage}}`）→ 输出协议。Planner 不再被直接声明成小奏，而是先建立小奏
的第三人称心理模型，再由同一层通过 `send_messages` 直接呈现她的措辞；这不是恢复
已删除的 Replyer，也没有新增一次模型调用。

**2026-08-02 收藏描述改用 image_description.md**：`caption`（meme_collection
save/recaption 的看图调用）此前有自己的 `meme_caption.md`——一段"≤150 字、覆盖
画面/文字/情绪/适用场景"的检索描述指令。实跑下来它的产出明显不如 timeline 图片
那条链：篇幅卡得死、模型倾向写成一句概括，画面细节与图上文字反而丢了，而选图恰恰
要靠这些。维护者据此拍板两个消费者读同一张页，`meme_caption.md` 随之删除（正文
留在 git 历史里）。**role 仍是两个**（`caption` / `vision`，见 LLM路由契约）：
共用的是提示词，不是路由与温度——收藏是低频调用，将来想给它换端点或换回专属
提示词，改这里一行即可，不牵动 ingest 那条高频链。共用带来的代价照实记在这里：
改 `image_description.md` 会同时改掉收藏描述的写法，两处一起验。

**`envelope.md` 不并页**：它和上面三份不是一类东西——纯格式规范（行文法：
逐节、逐行型、逐字段的值域与缺省语义），维护它的场合是改投影层渲染时逐条对照，而不是调
人格或纪律。留成独立文件，改渲染的人打开的是一份字典，读 planner.md 的人看到的
是一页连贯的行为约定，两种读法不互相淹没。

**本文件同时是提示词资产的地图——维护注记写在这里，不写进 `.md`（那些文件逐
字节注入 prompt，放不了给人看的注释）。**

资产分工：

  根页（`CONSUMERS`）—— 一个调用点一页，正文只归这一页所有：
  - `planner.md`  Planner 的行为约定，一页读完：角色模拟职责→系统运行事实与
                  行动纪律（念头≠动作 / 跨拍靠任务 / 一批工具不要重拨）及
                  提案-裁决流水线（写下的程序当拍不执行 → 下一拍
                  `execute_program` 指名才跑；2026-08-17 取代 reply 两步
                  发言）→唯一人物模型→输入信封→决策要求→工具规范→程序
                  输出协议。
  - 另外五个小消费者页内无槽，其中 `caption` 与 `image_description` **共用同一
    张 `image_description.md`**（2026-08-02，见下）；`web_digest.md` 是
    webfetch / websearch 内部提炼抓取正文的指令页（2026-08-03）。

  槽 —— 页内 `{{name}}` 就地展开：
  - `envelope`    唯一的文件槽（`envelope.md`）：输入信封的行文法语法，每个
                  行型/字段的唯一出处。纯格式规范，与投影层渲染成对维护，
                  故不并进根页。
  - `tools_usage` 唯一的动态槽：render 时遍历 ToolRegistry，按 scope 过滤，
                  正文来自 `tools/<name>.md`。求值为空时**连同它独占的那一行
                  一起消失**，不留空洞。
  再切新的文件槽之前先问一句：这段正文真的会被多个消费者共用，或者真的属于
  另一种维护场合吗？只有一个消费者、只在调 prompt 时改的正文一律写进它自己的
  根页——那正是 2026-07-31 那次合并修掉的东西。

  **硬规则：根页之间永不互相开槽。** 加新 worker = 一张根页 + 它需要哪几个槽。

装配为什么这么分（历史故障，改动前先读）：
  - **角色卡只有一份真相源（`planner.md` 的 `# 人物模型` 段）。** 历史上 `planner.md`
    里存过一份与卡片性格段逐字节相同、只差人称的第三人称投影，是不折不扣的第二
    份副本；也曾切成独立的 `voice.md` / `replyer.md` 各存一份。**不要再往任何
    别的文件抄人格正文**——`tools/*.md`、别的根页都不行。
  - **纯记录/观察层（caption / image_description / image_look / memory /
    web_digest）只读自己那一页。** 它们的输出会被永久写进事件正文并被下游
    反复读取，掺进人格或群规就等于污染所有下游语境，且无法回收。
  以上几条 2026-07-30 之前由 `kind` + `_FORBIDDEN_KINDS` + `_validate_assembly`
  做 build 期结构校验。已删除：它的粒度是"哪个文件进哪个消费者"，而真实发生过
  的事故是"人格正文被抄进另一个文件"，那种它一声不响。守这几条现在靠上面这段
  说明 + 契约测试里的语义断言（`test_prompt_catalog_contract.LayerBoundaryTests`：
  锚点在运行时从 `planner.md` 的人格段现取，锚点没了会先失败而不是假通过）——
  那才是唯一抓得到内容漂移的手段。

不变量：
  - 根页或文件槽读出来是空的 = 部署坏了，直接 raise `PromptSectionMissing`，
    绝不静默拿残缺 system prompt 继续跑（Planner 的 prompt 装配失败 → 该拍降级
    idle，见 llm_planner 的兜底）。根页缺失则读盘异常原样上抛，同理。
  - 未知槽名（写错、资产改名）同样 raise：静默留下一个 `{{typo}}` 字面量会直接
    出现在模型眼前，比缺一整段更难发现。
  - 动态槽（只有 `tools_usage`）求值为空时跳过：未注入工具注册表的场景
    （早期骨架 / 部分测试）本就不该有工具用法段。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Union

_PROMPTS_DIR = Path(__file__).parent

# source 可以是纯字符串、无参 ``() -> str``、或接受一个 scope 位置参的
# ``(scope) -> str``（如 ToolRegistry.usage_docs）。按 arity 决定是否传 scope。
PromptSource = Union[str, Callable[..., str]]

# 槽语法：``{{name}}``，名字限小写字母与下划线——正文里大量出现的 JSON 片段
# （`{"type":"text","data":{...}}`）因此不会被误当成槽。约定各页把槽单独写一行，
# 但**替换只吃 `{{…}}` 本身与同行左右的空格**，不碰换行：页里写了几个空行，
# 展开后就还是几个空行，分隔完全由页正文说了算。
SLOT_PATTERN = re.compile(r"\{\{([a-z_]+)\}\}")
_SLOT = re.compile(r"[ \t]*\{\{([a-z_]+)\}\}[ \t]*")
# 动态槽求值为空时，连它前面那条分隔线一起收掉（见 render_sections）。
_TRAILING_RULE = re.compile(r"\n[ \t]*-{3,}[ \t]*\n\s*\Z")


class PromptSectionMissing(RuntimeError):
    """根页、文件槽缺失或为空，或槽名未登记：部署损坏，fail loudly。"""


@dataclass(frozen=True)
class Section:
    """渲染产物的一段：名字 + 正文。

    `render_sections` 把根页按槽切开，literal 片段挂消费者名、槽片段挂槽名，
    **顺序拼起来逐字节等于 `render()`**（片段之间没有额外分隔符——分隔符是各页
    正文里的字符）。快照用它统计每部分体积。
    """

    name: str
    text: str


# ── 槽名 → 文件名。任何根页都可以 {{name}} 引用 ──
# 2026-07-31 起只剩 envelope 一个文件槽：persona / system / group_chat_rules
# 三份已并回 planner.md（见模块 docstring）。信封语法留在自己的文件里——它是
# 一份体量与性质都不同的纯格式规范（20KB 的元素/属性字典，投影层改渲染时对照
# 着改），混进人格与纪律那页会把两种读法压在一起。
_FILES: dict[str, str] = {
    "envelope": "envelope.md",
}

# ── 消费者 → 根页文件名。根页不是槽，不能被别的页引用 ──
CONSUMERS: dict[str, str] = {
    "planner": "planner.md",
    # 收藏描述 2026-08-02 起复用图片客观转录那张页（原 meme_caption.md 已删除，
    # 理由见模块 docstring）。两个消费者一张页是有意的，不是漏改：想让收藏描述
    # 重新分家 = 加一张 .md + 改这一行。
    "caption": "image_description.md",
    "image_description": "image_description.md",
    "image_look": "image_look.md",
    "memory": "memory_compaction.md",
    # webfetch / websearch 抓取正文的内部提炼（2026-08-03）：原文不进程序 ABI，
    # 程序拿到的只是这张页指挥产出的短转述。
    "web_digest": "web_digest.md",
}

# ── 动态槽名：不读盘，由注入的 source 求值；求值为空则整行跳过 ──
DYNAMIC_SLOTS = ("tools_usage",)


class PromptLibrary:
    """一张根页 + 它可用的槽，render 时展开。

    槽的来源可以是字符串字面量（测试注入）或 callable（读盘 / 遍历工具注册表，
    render 时才求值，因此改 `.md` 立即生效）。
    """

    def __init__(
        self,
        page: PromptSource,
        slots: dict[str, PromptSource] | None = None,
        *,
        name: str = "page",
    ) -> None:
        self._page = page
        self._slots: dict[str, PromptSource] = dict(slots or {})
        self._name = name

    def add(self, name: str, source: PromptSource) -> None:
        """登记/替换一个槽（方便测试替换某一段）。"""
        if not name:
            raise ValueError("slot name required")
        self._slots[name] = source

    def remove(self, name: str) -> None:
        self._slots.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._slots

    def slot_names(self) -> list[str]:
        """本页正文里实际出现的槽名，按出现顺序。"""
        return [m.group(1) for m in SLOT_PATTERN.finditer(self._page_text())]

    def section_names(self) -> list[str]:
        """渲染产物各部分的名字，按顺序（literal 片段用消费者名）。"""
        return [sec.name for sec in self.render_sections()]

    def get(self, name: str, *, scope: str | None = None) -> str:
        """按名字取一个槽的正文。槽未登记时 KeyError。"""
        if name not in self._slots:
            raise KeyError(name)
        return str(_resolve(self._slots[name], scope) or "").strip()

    def render_sections(self, *, scope: str | None = None) -> list[Section]:
        """把根页按槽切成有名字的片段，顺序即拼接顺序。

        literal 片段挂消费者名，槽片段挂槽名；**顺序拼起来逐字节等于
        `render()`**（首尾已 trim，片段间没有额外分隔符）。文件槽为空即 raise，
        动态槽为空则整行跳过——两条规则见模块 docstring。
        """
        page = self._page_text()
        out: list[Section] = []
        cursor = 0
        for match in _SLOT.finditer(page):
            name = match.group(1)
            text = self._slot_text(name, scope)
            literal = page[cursor : match.start()]
            cursor = match.end()
            if text is None:
                # 动态槽求值为空：整行连同**紧挨它前面那条分隔线**一起消失，
                # 否则页尾会留下一条孤零零的 `---`。
                literal = _TRAILING_RULE.sub("", literal)
                if literal:
                    out.append(Section(name=self._name, text=literal))
                continue
            if literal:
                out.append(Section(name=self._name, text=literal))
            out.append(Section(name=name, text=text))
        tail = page[cursor:]
        if tail:
            out.append(Section(name=self._name, text=tail))
        return _trim_edges(out)

    def render(self, *, scope: str | None = None) -> str:
        """展开全部槽，拼出最终 system prompt。"""
        return "".join(sec.text for sec in self.render_sections(scope=scope))

    # ── 内部 ──

    def _page_text(self) -> str:
        text = str(_resolve(self._page, None) or "")
        if not text.strip():
            raise PromptSectionMissing(f"prompt page {self._name!r} is empty")
        return text

    def _slot_text(self, name: str, scope: str | None) -> str | None:
        """槽正文；动态槽求值为空返回 None（调用方据此整行跳过）。"""
        if name not in self._slots:
            raise PromptSectionMissing(
                f"unknown slot {{{{{name}}}}} in prompt page {self._name!r}; "
                f"known slots: {', '.join(sorted(self._slots)) or '(none)'}"
            )
        text = str(_resolve(self._slots[name], scope) or "").strip()
        if text:
            return text
        if name in DYNAMIC_SLOTS:
            return None
        raise PromptSectionMissing(
            f"prompt slot {name!r} ({_FILES.get(name, 'dynamic')}) is empty"
        )


def _trim_edges(sections: list[Section]) -> list[Section]:
    """掐掉首尾片段的边缘空白，让 `"".join(片段)` 直接等于最终 prompt——
    快照的分段统计与真正送进模型的字节因此不会差一个换行。"""
    while sections:
        first = sections[0]
        trimmed = first.text.lstrip()
        if trimmed:
            sections[0] = Section(name=first.name, text=trimmed)
            break
        sections.pop(0)
    while sections:
        last = sections[-1]
        trimmed = last.text.rstrip()
        if trimmed:
            sections[-1] = Section(name=last.name, text=trimmed)
            break
        sections.pop()
    return sections


def _file_source(filename: str) -> Callable[[], str]:
    """文件槽的懒加载 source：render 时读盘（热更新）。读盘异常原样上抛。"""

    def load() -> str:
        return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")

    return load


def build_library(
    consumer: str,
    *,
    tool_registry: Any | None = None,
) -> PromptLibrary:
    """取某个消费者的根页 + 全部可用槽。未登记的消费者 KeyError。

    槽是**全部登记、按需使用**：页里没写 `{{group_chat_rules}}` 它就不出现，
    不需要在别处再声明一次要哪几段。`tools_usage` 只在传入 tool_registry 时
    登记；页里写了它而没传注册表，按动态槽规则整行跳过。
    """
    slots: dict[str, PromptSource] = {
        name: _file_source(filename) for name, filename in _FILES.items()
    }
    if tool_registry is not None:
        slots["tools_usage"] = tool_registry.usage_docs
    else:
        slots["tools_usage"] = ""
    return PromptLibrary(
        _file_source(CONSUMERS[consumer]), slots, name=consumer
    )


def render_system_prompt(
    consumer: str,
    *,
    scope: str | None = None,
    tool_registry: Any | None = None,
) -> str:
    """一步到位：取页 + 展开槽。Replyer / caption 这类单消费者调用点用。"""
    return build_library(consumer, tool_registry=tool_registry).render(
        scope=scope
    )


def _resolve(source: PromptSource, scope: str | None) -> str:
    """求值一个 source。字符串原样返回；callable 按 arity 调用：接受位置参的
    传 scope（如 ToolRegistry.usage_docs 按 scope 过滤工具），无参的直接调用。"""
    if not callable(source):
        return source
    if _accepts_positional_arg(source):
        return source(scope)
    return source()


def _accepts_positional_arg(fn: Callable[..., str]) -> bool:
    """fn 是否接受至少一个位置参数（用来接收 scope）。无法内省（内置 / C 实现）
    时保守当作"不接受"，按无参调用——绝不因内省失败而误传参把老 source 打挂。"""
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    for p in sig.parameters.values():
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            return True
    return False
