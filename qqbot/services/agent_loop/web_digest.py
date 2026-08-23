"""网页正文的 LLM 提炼（2026-08-03，程序形态重构的配套收口）。

背景：程序形态下查询结果不落事件，但「查到的东西要给以后用就得 return」——
webfetch / websearch 抓回的正文动辄 8000–20000 字，若原样进入程序值，模型
唯一的留痕方式就是把大段原文 return 进事件流（`<program_result>` 结果行），窗口内每拍
重复渲染。维护者裁定（2026-08-03）：抓取正文在**工具内部**先过一道 LLM 提炼，
程序拿到的只是信息密度高的短转述——原文从一开始就不进入程序 ABI，自然也
不可能被 return 进事件流。

与 image_description 的分工同构：那条链把「图」压成文字给纯文本 Planner，
这条链把「页」压成短文。区别在缓存与降级——网页内容随时间变化且带 focus
（同页不同关注点答案不同），**不缓存**；提炼失败不让整次查询翻车，降级为
原文截断到同一长度上限（有界，不会冲垮下一拍的信封）。

路由：role="web_digest"。未在 config/model_providers.json 配置该 role 时按
路由契约回退 default 规则——零配置可用，想给它换便宜/快的端点时单独加一行
role 即可（见 LLM 路由契约 §2）。

失败语义：`digest_page_text` 任何失败（prompt 资产缺失 / 无可用端点 / 调用
异常 / 超时 / 空输出）一律返回 None 并记 warning，不 raise；调用方用
`digest_or_truncate` 拿到「提炼或截断」二选一的有界文本。取消（CancelledError）
原样上抛——程序超时/关停的取消语义归执行器管，这里不吞。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    should_snapshot,
    write_snapshot,
)

logger = get_logger(__name__)

# 提炼产物上限。它是程序里被 return 进事件流的主要候选，必须显著小于
# `<program_result>` 结果行的 6144 字节截断阈值；1500 字足够装下一页的关键事实。
# 降级（截断原文）共用同一上限——无论走哪条路，离开工具的文本都有界。
MAX_DIGEST_CHARS = 1500

# focus 上限：它是一个关注点，不是正文。过长说明模型在把 timeline 抄进来
# （与 look_at_image.MAX_QUESTION_CHARS 同一取舍，关注点比问题更短）。
MAX_FOCUS_CHARS = 200

# 单次提炼调用的墙钟上限。查询工具整体受 ProgramExecutor 的单调用 20s 约束，
# 提炼必须给抓取阶段留出余量；超时按失败处理（降级截断），不中止查询。
_DIGEST_TIMEOUT_SEC = 12.0

# 供应商并发闸（与 image_description._semaphore 同理，各守各的 role）：
# websearch fetch_top_n 最多 5 条正文并发提炼，全局再压一道。
_MAX_CONCURRENT_CALLS = 5
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CALLS)


async def digest_or_truncate(
    text: str,
    *,
    url: str,
    title: str = "",
    focus: str | None = None,
) -> str:
    """提炼网页正文；提炼不可用时降级为原文截断。两条路都不超过
    `MAX_DIGEST_CHARS`——这是「抓取正文不进事件流」承诺的唯一出口。"""
    if not text.strip():
        return text
    digest = await digest_page_text(text, url=url, title=title, focus=focus)
    if digest is not None:
        return digest
    return text[:MAX_DIGEST_CHARS]


async def digest_page_text(
    text: str,
    *,
    url: str,
    title: str = "",
    focus: str | None = None,
) -> str | None:
    """一次提炼调用。任何失败返回 None（不 raise），由调用方决定降级。"""
    if not text.strip():
        return None
    try:
        prompt = _load_prompt("web_digest")
    except Exception as exc:
        logger.warning("[web_digest] prompt asset missing: {}", exc)
        return None

    user_text = _compose_user_text(text, url=url, title=title, focus=focus)

    from langchain_core.messages import HumanMessage, SystemMessage

    from qqbot.services.event_gateway.outbound import invoke

    messages = [SystemMessage(content=prompt), HumanMessage(content=user_text)]
    # 快照脱敏口径：正文来自公网抓取、不含群聊内容，user_text 可整体入档
    # （与 planner 快照保存完整信封同级）。scope_key=None：提炼不属于单一 scope。
    snapshot: PromptSnapshot | None = None
    if should_snapshot(None):
        snapshot = PromptSnapshot(
            kind="web_digest",
            model=None,
            system_prompt=prompt,
            user_text=user_text,
        )

    started = time.monotonic()
    try:
        async with _semaphore:
            invoked = await invoke(
                "web_digest", messages, timeout=_DIGEST_TIMEOUT_SEC
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "[web_digest] call failed: {}: {} url={}",
            type(exc).__name__,
            exc,
            url,
        )
        if snapshot is not None:
            snapshot.add_attempt(
                latency_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            snapshot.outcome = "call_error"
            write_snapshot(snapshot)
        return None

    if snapshot is not None:
        snapshot.add_attempt(
            latency_ms=int((time.monotonic() - started) * 1000),
            response_text=invoked.text,
            usage=extract_usage(invoked.raw),
            error=invoked.error,
        )
        snapshot.outcome = (
            "call_error"
            if not invoked.ok
            else ("ok" if invoked.text.strip() else "empty_response")
        )
        write_snapshot(snapshot)
    if not invoked.ok:
        logger.warning(
            "[web_digest] call failed: {} url={}", invoked.error, url
        )
        return None
    digest = invoked.text.strip()
    if not digest:
        logger.warning("[web_digest] LLM returned empty text url={}", url)
        return None
    return digest[:MAX_DIGEST_CHARS]


def _compose_user_text(
    text: str, *, url: str, title: str, focus: str | None
) -> str:
    """提炼请求的用户正文：元信息 → 可选关注点 → 抓取正文。"""
    lines = [f"URL：{url}"]
    if title.strip():
        lines.append(f"标题：{title.strip()}")
    if focus and focus.strip():
        lines.append(f"关注点：{focus.strip()[:MAX_FOCUS_CHARS]}")
    lines.append("")
    lines.append("正文：")
    lines.append(text)
    return "\n".join(lines)


def _load_prompt(consumer: str) -> str:
    """required 页：缺失/为空时 render 直接 raise，由调用方折成 None 降级
    （拿空指令去提炼会产出无信息量的转述，不如降级截断原文）。"""
    from qqbot.services.prompt_assembler import assemble

    return assemble(consumer)


def _extract_text(raw: Any) -> str:
    """AIMessage.content → str（与 image_description._extract_text 同构：
    部分网关把回复拆成 block 数组）。"""
    content = getattr(raw, "content", raw)
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


__all__ = [
    "MAX_DIGEST_CHARS",
    "MAX_FOCUS_CHARS",
    "digest_or_truncate",
    "digest_page_text",
]
