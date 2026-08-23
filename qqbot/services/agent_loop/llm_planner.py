"""LLMPlanner — 调真 LLM 输出一段受限 Python 程序。

复用 qqbot/core/llm.create_llm() 当 LLM 客户端工厂（纯基础设施层，
不含 v1 业务）。Prompt 与解析逻辑均按 v2 任务与决策契约从零写。

System prompt 不再硬编码 —— 完全交给 prompts/catalog.py 装配。默认库
（build_default_prompt_library）取根页 `planner.md`：2026-07-31 删除 Replyer 后
Planner 是唯一的对话消费者，原先切成 `persona` / `system` / `group_chat_rules`
三个文件槽的共享资产已并回该页，页里只剩 `{{envelope}}`（信封语法，纯格式规范，
与投影层成对维护故仍是独立文件）与动态的 `{{tools_usage}}`（内容来自
ToolRegistry，按 scope 过滤）。2026-08-01 起根页改用“角色决策规划器”框架：
Planner 内部建立第三人称人物模型，最终措辞仍由同一层直接通过工具呈现。需要迭代
人格、职责或参与规则时直接改 `planner.md`，改信封语法改
`envelope.md`，改工具说明改 `tools/<name>.md`，都不需要碰 planner。

每次 decide() 只调用模型一次，AgentLoop 每拍也只 decide 一次——2026-08-21 起
「一个响应绑定一次模型执行」（渲染格式表 §一⑦）：静态校验归注册层
（`program_registrar`），内容不合规当场签发 `agent.invalid_action`，**不**换
端点重生成。写错 Python 不是端点的错，AgentLoop 因此不再为此调
report_invalid_output()；该方法只留给"HTTP 200 但正文不可用"这类真正属于提供层
的失败。LLM 不可用或调用失败时返回带 planner_error 的空响应，AgentLoop 据此把
这一拍记成 planner_error 并**不写任何时间线事件**——传输失败不是模型的表达。

契约：任务与决策契约.md §2-§4
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from qqbot.core.llm import create_llm
from qqbot.core.logging import get_logger
from qqbot.core.time import CHINA_TIMEZONE
from qqbot.services.agent_loop.decision import (
    DecisionContext,
    DecisionOutput,
)
from qqbot.services.agent_loop.projection import (
    _hash12,
    _head_field,
    _ml_text,
    render_timeline_stream,
)
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    section_stats,
    should_snapshot,
    write_snapshot,
)
from qqbot.services.agent_loop.prompts.catalog import PromptLibrary
from qqbot.services.agent_loop.tool_registry import ToolRegistry

logger = get_logger(__name__)


# Planner 的正文与顺序全部写在根页 planner.md 里 —— 逻辑递进：身份与核心任务
# → 系统运行方式 → 人物模型 → 输入数据（{{envelope}} 槽）→ 决策要求 → 工具
# （{{tools_usage}} 槽）→ 输出协议。分工理由写在 catalog 的 docstring。


def build_default_prompt_library(
    *,
    tool_registry: ToolRegistry | None = None,
) -> PromptLibrary:
    """v2 默认 system prompt 装配 —— 委托 prompts/catalog.py。

    正文、顺序、分隔全写在根页 `planner.md` 里（`{{envelope}}` 位于输入数据段，
    `{{tools_usage}}` 位于工具段）；这里只是 Planner 侧的入口。页与槽都在 render
    时才读盘：改 .md 即生效、新增/下架工具立即反映。根页或文件槽读出来为空、
    槽名未登记都直接 raise（部署坏了不静默跑残缺 prompt）；tools_usage 未注入
    注册表时整槽跳过。
    """
    from qqbot.services.agent_loop.prompts.catalog import build_library

    return build_library("planner", tool_registry=tool_registry)


class LLMPlanner:
    """实现 Planner Protocol。线程安全的懒初始化 LLM 客户端。"""

    def __init__(
        self,
        llm_client: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        # 测试场景下可注入一个 stub client（提供 ainvoke(messages) 即可）；
        # 生产场景留 None，走出口网关 invoke（提供层 + 绕回入口登记）。
        self._llm = llm_client
        self._injected_llm = llm_client is not None
        self._tool_registry = tool_registry
        # prompt_library 优先：调用方明确传入就用它；否则按 tool_registry
        # 装配默认库（planner 根页 + envelope/tools_usage 两个槽）。
        if prompt_library is None:
            prompt_library = build_default_prompt_library(
                tool_registry=tool_registry
            )
        self._prompt_library = prompt_library
        self._init_lock = asyncio.Lock()

    async def decide(self, context: DecisionContext) -> DecisionOutput:
        try:
            messages, snapshot = _build_messages(context, self._prompt_library)
            _log_request(context, messages)
        except Exception as exc:
            logger.warning("[llm_planner] build messages failed: {}", exc)
            return DecisionOutput(
                program="",
                planner_error=f"prompt_build_error:{type(exc).__name__}",
            )

        started = time.monotonic()
        try:
            if self._injected_llm:
                llm = self._llm
                if llm is None:
                    return DecisionOutput(
                        program="", planner_error="llm_unavailable"
                    )
                if snapshot is not None:
                    snapshot.model = _llm_model_name(llm)
                raw = await llm.ainvoke(messages)
                text = _extract_text(raw)
                _log_response(context, text)
                if snapshot is not None:
                    snapshot.add_attempt(
                        latency_ms=_elapsed_ms(started),
                        response_text=text,
                        usage=extract_usage(raw),
                    )
                    snapshot.outcome = "received"
                return DecisionOutput(program=text, raw_response=text)

            from qqbot.services.event_gateway.outbound import invoke

            invoked = await invoke(
                "planner",
                messages,
                extra={
                    "scope_key": context.scope_key,
                    "tick_seq": context.tick_seq,
                    "correlation_id": context.correlation_id,
                },
            )
            text = invoked.text
            _log_response(context, text)
            if snapshot is not None:
                snapshot.model = _llm_model_name(
                    getattr(invoked.raw, "__class__", None)
                )
                snapshot.add_attempt(
                    latency_ms=_elapsed_ms(started),
                    response_text=text,
                    usage=extract_usage(invoked.raw),
                    error=invoked.error,
                )
                snapshot.outcome = (
                    "call_error" if not invoked.ok else "received"
                )
            if not invoked.ok:
                kind = invoked.error or "unknown"
                if kind == "llm_unavailable":
                    return DecisionOutput(
                        program="", planner_error="llm_unavailable"
                    )
                return DecisionOutput(
                    program="",
                    planner_error=f"llm_call_error:{kind}",
                )
            return _output_from_invoke(invoked)
        except Exception as exc:
            logger.warning("[llm_planner] LLM call failed: {}", exc)
            if snapshot is not None:
                snapshot.add_attempt(
                    latency_ms=_elapsed_ms(started),
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
                snapshot.outcome = "call_error"
            return DecisionOutput(
                program="",
                planner_error=f"llm_call_error:{type(exc).__name__}",
            )
        finally:
            if snapshot is not None:
                write_snapshot(snapshot)

    def report_invalid_output(self, reason: str) -> None:
        """Synchronously mark the last routed LLM body as unusable."""
        mark_failed = getattr(self._llm, "mark_last_call_failed", None)
        if not callable(mark_failed):
            return
        try:
            mark_failed(str(reason)[:300])
        except Exception:
            # 路由记账是 best-effort，绝不能反噬调用方的下一步。
            pass

    async def _ensure_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        async with self._init_lock:
            if self._llm is None:
                self._llm = await create_llm(role="planner")
            return self._llm


def _output_from_invoke(invoked: Any) -> DecisionOutput:
    ingest = getattr(invoked, "ingest", None)
    event = getattr(ingest, "event", None) if ingest is not None else None
    prepared = getattr(ingest, "prepared", None) if ingest is not None else None
    text = invoked.text
    if event is None:
        return DecisionOutput(program=text, raw_response=text)
    if getattr(event, "type", "") == "agent.invalid_action":
        return DecisionOutput(
            program=text,
            raw_response=text,
            event_id=event.event_id,
            accepted=False,
        )
    payload = getattr(event, "payload", None) or {}
    stored = payload.get("program")
    program = stored if isinstance(stored, str) else text
    return DecisionOutput(
        program=program,
        raw_response=text,
        event_id=event.event_id,
        accepted=True,
        prepared=prepared,
        left_asset="program_hash" in payload,
        program_sha256=payload.get("program_sha256"),
    )


def _build_messages(
    context: DecisionContext,
    prompt_library: PromptLibrary,
) -> tuple[list[Any], PromptSnapshot | None]:
    """构造 chat 输入 + 可选的 Prompt 快照（快照关闭 / scope 不在白名单时为
    None）。langchain_core.messages 在 langchain_openai 已是必依赖。

    System prompt 完全由提示词库输出 —— 默认装配是根页 `planner.md` 的正文，
    并在页内指定位置展开 `envelope.md` 与 Program API 参考。

    HumanMessage 的 text block 是**行文法信封**（2026-08-03 起替换 XML；不变量
    与安全模型见主线 Part 3 §2）：timeline 里每条
    item 的 render 字段本身就是 `<msg>` / `<tool>` / `<notice>` 等独立的行（或行块），
    外层只需 markdown 节头与少量骨架行；上下引用关系（回复标记、@、工具 ↔ 结果）
    留在行内文法里，不会被 JSON 字符串转义压平成扁平的字段表。

    图片（2026-07-28 起）：Planner 是**纯文本模型**，HumanMessage 只有
    文本，不再附带任何图像 block。群里的图在 EventIngest 落盘时就由专用 VLM
    转录成客观描述，随事件正文进 timeline，渲染成 `[img hash12 : 描述]` ——
    描述内联在它所属的那条消息里，图文时序天然对齐，旧多模态路径靠
    `↓ image hash=` label 给图块对位的做法（3 张图以上常错位）随之作废。
    需要就某张图追问具体问题时走 look_at_image 工具现场重看。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    scope = context.scope_key.split(":", 1)[0]
    # 片段直接拼起来与 render() 逐字节一致（render() 就是这么实现的）——多要
    # 一份分段统计给快照，不重复求值。片段之间没有额外分隔符：分隔线是根页
    # 正文里的字符。
    sections = prompt_library.render_sections(scope=scope)
    system_content = "".join(sec.text for sec in sections)
    input_text = _render_input_text(context)

    snapshot: PromptSnapshot | None = None
    if should_snapshot(context.scope_key):
        snapshot = PromptSnapshot(
            kind="planner",
            scope_key=context.scope_key,
            tick_seq=context.tick_seq,
            correlation_id=context.correlation_id,
            system_prompt=system_content,
            user_text=input_text,
            sections=section_stats(sections),
        )

    # content 是**纯字符串**而非单元素 block 数组：Planner 不再有图，多模态
    # 结构没有存在意义，字符串也是各网关兼容性最好的形态。
    return [
        SystemMessage(content=system_content),
        HumanMessage(content=input_text),
    ], snapshot


def _render_input_text(context: DecisionContext) -> str:
    """拼装喂给 LLM 的行文法信封（2026-08-03 替换 XML）。

    节序与安全模型见主线 Part 3 §2、§4。

    结构（顺序按**变化频率升序**排列——前缀缓存契约，2026-07-12）：

      # 决策输入 <scope>
      本账号(QQ)                        (可缺)

      <task>                              (便签为空时整块不出)
      便签正文

      <memes>                             (有收藏才出)
      <meme><hash12> (<日期>): 描述

      ## 时间线
      <t>…
      {item.render …}                    (同秒的行共享一个时刻头)

      <now>YYYY-MM-DD HH:MM:SS
      （生产路径不再附 <校验拒绝>；字段仅遗留兼容）

    缓存契约（2026-07-12）：OpenAI 系 API 的自动前缀缓存要求前缀**逐字节
    一致**。now 每拍必变，曾是信封的头字段，把可缓存前缀掐断在 system
    prompt 末尾——时间线（追加为主，窗口起点锚定见 projection）每拍全价
    重计费。现按变化频率排序：头两行只留 scope/
    bot_qq（稳定/极少变）；表情包收藏只在 save/delete/recaption
    时变，排在时间线之前，收藏稳定时整段进入可缓存前缀；每拍必变的 now
    沉为尾部 ``<now>``。待办便签 2026-08-21 前是"未收束任务"节、活跃期逐拍
    变（在途调用集合随工具收口增删），因此排在时间线之后；坍缩成单栏
    latest-wins 之后它只在模型主动重写时才变，与收藏同级，于是上提到收藏
    之前一起进可缓存前缀。2026-08-01 曾把收藏挪到时间线之后换显著性
    （选图发生在读完局面之后）；2026-08-20 撤回——发图已稳定，收藏段重新
    进入可缓存前缀。``## 反思`` 常驻节已于 2026-08-21 撤销：反思改为时间线
    事实事件，随时间线一起追加，不再单独占一节。

    工具结果只在时间线的 ``<tool>`` 行呈现一次（2026-07-02 删除了
    pending-tool-results 区——同一调用双重渲染、且无消费切割地每拍重复
    出现，是模型复读的直接诱饵）。2026-08-01 起 decision_emitted.reasoning
    只保留在运行日志与快照，不再投影进 timeline；跨拍事实与义务分别由客观
    事件行和未收束任务表达。

    安全模型（Part 3 §2.1）：一切动态值经 `_esc_text` / `_head_field` /
    `_ml_text` 净化后才进信封——节头、行头、``<now>`` 等列 0 结构只可能
    由本函数与投影渲染器落笔。
    """
    parts: list[str] = []
    parts.append(f"# 决策输入 {_head_field(context.scope_key)}")
    # bot_qq 可选（值来自 context.bot_user_id）：未注入（启动初期 bot_registry
    # 还空、或测试场景）时不渲染；此时模型仍可靠回复标记上服务端标注的
    # ``*``（from_self 由投影层解析，不依赖本行）识别"这条是回复我的"。
    #
    # 群角色已于 2026-08-21 从头部移走（渲染格式表 §一①、§八1）：它是**群信息**，
    # 现在随群名／群昵称一起作为 <background> 事实事件进时间线，有发生时刻、
    # 有先后。留在这里的只剩 scope 与本账号 QQ——前者是信封自己的结构头，后者是
    # 账号身份，system scope 也成立，都不属于"某个群的情况"。
    # DecisionContext.bot_role 本身保留：它仍是工具层与快照的读取面，只是不再
    # 渲进信封。真正判 bot 权限时工具照旧**实时**复查当前角色（见
    # tool_registry._effective_bot_role），所以 <background> 那 24 小时滞后
    # （事件系统设计 §4.9 已知缺口①）不会造成误判。
    account_bits: list[str] = []
    if context.bot_user_id:
        account_bits.append(f"本账号({_head_field(context.bot_user_id)})")
    if account_bits:
        parts.append(" ".join(account_bits))

    # ─── 待办便签（2026-08-21 单栏化，渲染格式表 §三2）───
    # 单一内容栏，latest-wins，正文逐字保留。空/无便签时整块不出现——旧的
    # `## 未收束任务` 空节头写作"明确当前无任务"，但单栏形态下"没有这一节"
    # 已经就是这个意思，留个空壳只是每拍多两行。
    # 正文**整体缩进两空格**（含第一行），与 <reflection> / <recall> 同型。
    # 格式表 §三2 的样例把正文画在列 0，这里有意不照抄：列 0 是渲染器的，
    # 正文写着 `## 时间线` 就能凭空长出一个节头。这是她写给自己的字，不是
    # 外人的输入，但便签里完全可能抄进别人的原话。缩进两空格既守住通则三
    # （缩进行从属于上方最近的列 0 行），也不改变可读性。
    task_note = getattr(context, "task_note", None)
    if isinstance(task_note, str) and task_note.strip():
        parts.append("")
        parts.append("<task>")
        parts.append(f"  {_ml_text(task_note.strip())}")

    # ─── 表情包收藏（有才渲染）。少变，排在时间线之前吃前缀缓存。空收藏
    # 整节省略。2026-08-01 曾挪到时间线后换显著性，2026-08-20 撤回。───
    saved_memes = getattr(context, "saved_memes", None) or []
    if saved_memes:
        parts.append("")
        # 2026-08-21：Markdown 节头 `## 表情包收藏` 换成列 0 区块标记
        # `<memes>`（渲染格式表 §三1）——信封里除 `## 时间线` 外不再混用两套
        # 结构语法。逐条仍是 `<meme>` 行，形态不变。
        parts.append("<memes>")
        now_year = context.now.year
        for meme in saved_memes:
            saved = meme.saved_at
            date_disp = (
                saved.strftime("%m-%d")
                if saved.year == now_year
                else saved.strftime("%Y-%m-%d")
            )
            parts.append(
                f"<meme>{_hash12(meme.file_hash)} ({date_disp}): "
                f"{_ml_text(meme.description)}"
            )

    parts.append("")
    parts.append("## 时间线")
    # 时间流渲染：行按同秒分组共享 <t> 时刻头，行内无时间字段
    # （render_timeline_stream，时间流契约 2026-07-26；纯追加见其 docstring）。
    parts.extend(render_timeline_stream(context.timeline))

    # ─── 反思不再单独成节（2026-08-21）───
    # agent.reflection_written 改为时间线事实事件，由 render_timeline_stream
    # 渲成 <reflection> 行逐版留痕；原 `## 反思` 常驻节与 latest-wins 折叠一并撤销。
    # 那套折叠器交给 task 便签：反思要历史，便签只要现状。

    # ─── `## 未收束任务` 节已于 2026-08-21 删除（渲染格式表 §一②）───
    # 任务坍缩为单栏便签后它没有条目、没有状态、没有在途调用集合可渲染，
    # 于是也不再需要一个排在时间线之后的独立节。便签本体上提到了收藏之前。

    # <pending-reply> 段已于 2026-07-24 删除（待办#19）——待发稿的调度事实与
    # 授权内容都在 timeline 的 <tool>reply 行上（参数/结果），独立状态区属于
    # 重复渲染。顺带一个缓存收益：它曾是全信封变化最频繁的业务段（每次落稿
    # revision/flush_at 都变、创建/flush 时整段出现消失），撤掉后从未收束
    # 任务到 <now> 之间不再有抖动源。

    # ─── 每拍必变的时钟字段，沉底（缓存契约见本函数 docstring）───
    # @tick 已于 2026-07-30 从信封删除（DecisionContext.tick_seq 本身保留——
    # 事件 payload / 日志配对 / 快照文件名都还靠它）。删的理由不是"省字节"：
    # 它与 now 同处时钟行，而 now 每拍必变且删不掉，所以 tick 从来没有
    # 额外掐断过任何一段可缓存前缀，缓存收益恒为零。真正的问题是它对模型
    # **无锚点**：全信封只有这一处出现拍号，timeline 的事件行都不带拍号，
    # 模型既减不出步数也定位不了任何行；而进程重启后 _tick_seq 归零、
    # timeline 却从库里重新折叠出满窗历史，tick="1" 配一整段往事是**误导**而
    # 不只是噪音。若将来要给模型"这是本轮连续推演第几步"的信号，应新增
    # 带锚点的 burst_id/burst_step，不要把这个无锚点的绝对计数加回来。
    #
    # 时区契约：所有暴露给 LLM 的时间都是北京时间（与数据库写入侧 china_now()
    # 一致），行文法下时区全局固定、不再逐处渲染 +08:00 尾巴。caller 传错
    # 时区时 astimezone() 兜底，naive datetime 假设它就是北京时间。
    now = context.now
    if now.tzinfo is None:
        now = now.replace(tzinfo=CHINA_TIMEZONE)
    else:
        now = now.astimezone(CHINA_TIMEZONE)
    parts.append("")
    parts.append(f"<now>{now.strftime('%Y-%m-%d %H:%M:%S')}")

    return "\n".join(parts)


def _elapsed_ms(started_monotonic: float) -> int:
    return int((time.monotonic() - started_monotonic) * 1000)


def _llm_model_name(llm: Any) -> str | None:
    """best-effort 取模型名（ChatOpenAI 是 model_name；stub / 其它实现取不到
    就 None——快照记 null，不猜）。"""
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _log_request(context: DecisionContext, messages: list[Any]) -> None:
    """喂给 LLM 的完整内容打到 INFO 日志，方便核对事件流是否合理。

    布局优先可读：用换行 + 分隔条把 system / user / response 三段切开，
    避免一行 5KB+ 撑爆 terminal。system prompt 只打首尾几行 + 长度，
    因为它基本静态（planner.md 正文 + envelope + tools usage）；
    user 段（行文法信封）是 tick 之间真正变化的东西，原样全打。
    """
    # messages = [SystemMessage, HumanMessage]
    sys_msg, human_msg = messages[0], messages[1]
    system_text = getattr(sys_msg, "content", "") or ""
    human_content = getattr(human_msg, "content", "") or ""

    # 2026-07-28 起 content 恒为 str（Planner 无图）；list 分支留着兜住早期
    # 快照/测试里手工构造的多模态消息，不为它多记一个恒零的 image_blocks 计数。
    if isinstance(human_content, list):
        user_text = "\n".join(
            b.get("text", "")
            for b in human_content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        user_text = str(human_content)

    sep = "=" * 80
    logger.info(
        "\n{sep}\n[llm_planner] → LLM  scope={scope} tick={tick} "
        "system_prompt_chars={syslen} user_input_chars={ulen}\n"
        "{sep}\n"
        "── system_prompt (head 400 / tail 200) ──\n{sys_head}\n…\n{sys_tail}\n"
        "── user_input ──\n{user}\n{sep}",
        sep=sep,
        scope=context.scope_key,
        tick=context.tick_seq,
        syslen=len(system_text),
        ulen=len(user_text),
        sys_head=system_text[:400],
        sys_tail=system_text[-200:] if len(system_text) > 600 else "",
        user=user_text,
    )


def _log_response(context: DecisionContext, text: str) -> None:
    sep = "=" * 80
    logger.info(
        "\n{sep}\n[llm_planner] ← LLM  scope={scope} tick={tick} "
        "response_chars={rlen}\n{sep}\n{body}\n{sep}",
        sep=sep,
        scope=context.scope_key,
        tick=context.tick_seq,
        rlen=len(text),
        body=text,
    )


def _extract_text(message: Any) -> str:
    """langchain BaseMessage.content 在多模态/分片场景下可能是 list[dict]，
    统一拍平成 str。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, dict) and "text" in chunk:
                parts.append(str(chunk["text"]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "".join(parts)
    return str(content)

