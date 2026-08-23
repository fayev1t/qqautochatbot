"""Decision context and Planner protocol for program-shaped decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class DecisionOutput:
    program: str
    raw_response: str | None = None
    planner_error: str | None = None
    event_id: str | None = None
    accepted: bool | None = None
    prepared: Any = None
    left_asset: bool = False
    program_sha256: str | None = None


# ─── Projection-fed view dataclasses (任务与决策契约 §2.1、§8) ───


@dataclass(frozen=True)
class ImageRef:
    """已下载落盘的图片素材引用。

    projection 把 message 里 downloaded=true 的 image segment 收集到
    TimelineItem.images 上。downloaded=false 的图不进 ImageRef（只在 render
    文本里留占位）。

    2026-07-28 起**没有任何 prompt 装配路径消费它**：Planner/Replyer 已是纯
    文本模型，图片语义经 ingest 期写入的 desc= 属性随 render 文本抵达（见
    services/agent_loop/image_description.py）。保留本结构是因为它仍是"这条
    消息带了哪些已落盘图片"的结构化记录，读盘取像素的活现在只有
    look_at_image 工具做，且它按 hash 自己定位文件、不走这里。
    """

    file_hash: str
    local_path: str
    mime: str


@dataclass(frozen=True)
class MemeView:
    """一条表情包收藏（agent_memes 读出的视图）。

    Projector 经 meme_store.load_saved_memes 挂到 DecisionContext.saved_memes，
    llm_planner 渲染成信封 `## 表情包收藏` 一节里的一行
    ``<meme>hash12 (MM-DD): 描述``。description 由收录（meme.save）时的
    caption LLM 调用生成，是 Planner 经 send_messages 发图时选图的唯一依据；
    hash 与时间线 `[img hash12 …]` 同一值空间（展示 12 位前缀，库存完整 64 位）。

    context_note 是收录时留档的聊天语境（表情包工具黑盒设计.md §2"留档备将来
    重生成"）：meme.recaption 不带新语境时沿用它重跑 caption。**不进 prompt**
    ——`## 表情包收藏` 节只渲染 description。
    """

    file_hash: str
    description: str
    saved_at: datetime
    context_note: str | None = None


# PendingReplyView 已于 2026-07-24 删除（待办#19），承载它的 reply / ReplyTask
# 体系整套已于 2026-08-17 删除（提案-裁决流水线取而代之）。TimelineItem 仍保留
# "reply_task_completed" 这个 kind：库里存量的 runtime.reply_task_completed 还要
# 兼容渲染一个版本周期，只是不会再有新的写入方。


@dataclass(frozen=True)
class TimelineItem:
    """One renderable row in the LLM context (任务与决策契约 §2.1)."""

    event_id: str
    occurred_at: datetime
    kind: Literal[
        "message",
        "notice",
        "tool_call",
        "system_hint",
        "request",
        "my_reply",
        "reply_task_completed",
        "program",
        "reflection",
        "invalid_action",
        "background",
    ]
    render: str
    related_event_ids: list[str] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)


# ProgressNote / TaskView 已于 2026-08-21 删除（渲染格式表 §一②，甲案）。
# 任务坍缩为单栏便签：没有 ID、没有状态机、没有父子层级、没有逐条进度笔记，
# 因而也没有需要 dataclass 承载的结构——只剩 DecisionContext.task_note 一个
# 字符串。连带删除的还有 agent_tasks 读模型表与 agent.task_* 事件族。
# 不要为了"以后也许要多任务"把它们加回来：多任务并行是数据模型层的概念，
# 消费便签的是大模型，一段自由文本就写得下两件事。


@dataclass(frozen=True)
class ToolResultView:
    """A folded view of an agent.tool_called and its eventual result/failure
    (任务与决策契约 §7.2、§11).

    成功/失败只靠内容区分：``error_kind is None`` 为成功（``result`` 有效），
    非 None 为失败（error_* 有效）。所属 decision 尚无 program terminal 且
    调用本身无 terminal 时，``error_kind`` 为 ``pending``，渲染成中性
    「已调用」。只有收口后的半截才是 ``interrupted`` / ``uncertain``。
    """

    tool_call_id: str
    tool_name: str
    arguments: dict
    result: Any | None
    error_kind: str | None
    error_message: str | None
    # 失败时 ToolOutcome.extra 平铺进 agent.tool_failed.payload 顶层的结构化附加
    # 字段（required_tier / actual_tier / required_bot_role / actual_bot_role /
    # retcode / action / allowed_scopes ...）。渲染时随 <error> 属性透给 LLM，让
    # 它能精确解释"差在哪一级权限 / napcat 具体报了什么"，而非只看一段 message。
    # None = 无附加字段或非失败态。
    error_extra: dict | None = None


@dataclass(frozen=True)
class DecisionContext:
    scope_key: str
    correlation_id: str
    tick_seq: int
    now: datetime

    timeline: list[TimelineItem] = field(default_factory=list)
    # ─── 待办便签（2026-08-21 起单栏 latest-wins）───
    # 由 agent.task_note_written 折出的最新一版正文，渲染成信封顶部的 <task>
    # 单栏。None / 空串 = 当前无未竟之事，整节不渲染。
    # 窗口外持久由 Projector._fetch_latest_task_note 一条不受取数窗约束的
    # LIMIT 1 查询兜底（同 bot_role / <recall>），不再有 agent_tasks 读模型表。
    task_note: str | None = None
    # ─── 表情包收藏夹（meme_collection 管收藏；send_messages 发送）───
    # 全局共享的 agent_memes（2026-07-06 起全 bot 一份，created_at 倒序、
    # 封顶 meme_store.MAX_SAVED_MEMES 条），由 Projector.
    # _augment_with_saved_memes 注入；渲染成 `## 表情包收藏` 节，meme 工具凭
    # 其中的 hash 精确删除/换描述，并供发言时选图。空 = 不渲染。
    saved_memes: list[MemeView] = field(default_factory=list)
    # 2026-07-02 起不再有独立的 pending_tool_results 字段：工具结果只在
    # timeline 的终态 <tool> 行呈现一次（单一事实源）。
    # 旧的"待消费工具更新区"实现从未做过消费切割——窗口内所有结果每拍
    # 重复以"待你处理"的名义出现，是复读的直接诱饵；且同一调用在 timeline
    # 与 pending 区双重渲染，两处语义必然漂移。ToolResultView 仍保留——它是
    # timeline 渲染 tool-call 行时的折叠视图（fold_tool_results）。

    # ─── 程序源码进入时间线（2026-08-14）───
    # DecisionOutput.program 随 agent.decision_emitted 落库并渲染为 <action>。
    # 它表示当拍产出了什么，不表示已经落地；落地看 <tool> 与 <program_result>。

    # ─── 自我认识（2026-08-03 立；2026-08-21 时间线化）───
    # agent.reflection_written 不再折叠成 `## 反思` 一节，而是作为**时间线事实
    # 事件**渲染成 <reflection> 行、逐版留痕。全量覆写会让历史各版彻底消失，模型
    # 因此看不到自己认识的演变——这是把它搬上时间线的理由。
    #
    # 与便签恰好对调（任务与决策契约 §8）：反思要历史，便签只要现状。腾出来的
    # latest-wins 折叠器交给 task 便签。
    #
    # 与 2026-08-01 删除的 `<my-thought>` 的边界不变：那次删的是**程序注释**
    # 逐拍原样回灌（快照实证：变成写给自己的高显著度提示词、产出模板化台词），
    # 现在仍然如此，注释只能经 get_recent_thoughts 主动读回。这里铺开的是
    # reflect 显式写下、有意留给将来的自我认识，性质不同。

    # 当前 tick 上 bot 自己的 QQ user_id（由 bot_registry 提供,AgentLoop
    # 在 tick() 时 resolve 后注入）。None 表示 bot 还没连接 napcat / 注册
    # 第一条事件 —— prompt 渲染时不输出该属性，模型回退到"靠引用反推"。
    bot_user_id: str | None = None

    # 当前 tick 在该 group scope 下小奏自己的群角色（owner / admin / member）。
    # 由 Projector.fold_bot_role() 从 runtime.bot_role_observed 事件折出最新值，
    # AgentLoop 在 dispatch 时原样注入 tool_called.payload.bot_role。
    # 2026-08-21 起它**不再进信封**：群角色随群名/群昵称下沉为 <background>
    # 事实事件（渲染格式表 §一①）。本字段保留，只剩一个用途——作为工具内
    # enforce_bot_admin 的**回退快照**：真正判权限时工具先**实时**向 napcat 查
    # bot 当前角色（_effective_bot_role），查不到才回退到它。它同时也是 Prompt
    # 快照里记录"这一拍系统认为自己是什么角色"的那一栏。
    # None = 未观测到（启动初期 sweep 未跑完 / 该群从未写过 baseline）——工具侧
    # 若实时查也拿不到，则保守拒绝带 required_bot_role 的调用。
    bot_role: Literal["owner", "admin", "member"] | None = None


class Planner(Protocol):
    """Stateless decision function.

    Implementations:
    - LLMPlanner — 现役唯一实现；每次调用模型一次并返回响应源码。
    - report_invalid_output — 把"HTTP 200 但正文不可用"这类**属于提供层**的失败
      同步回报路由层以冷却端点。2026-08-21 起 AgentLoop **不**为静态预检失败调
      它（渲染格式表 §一⑦ 失败分层）：写错 Python 不是端点的错，也没有同拍下
      一次 decide 可换——一个响应绑定一次模型执行。
    """

    async def decide(self, context: DecisionContext) -> DecisionOutput: ...

    def report_invalid_output(self, reason: str) -> None: ...
