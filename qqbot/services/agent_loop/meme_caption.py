"""表情包收录/换描述时的看图写描述（meme 工具 save/recaption 的内部 LLM 调用）。

meme 工具不让 planner 在动作 JSON 里顺手写收藏描述：决策 tick 的主职是决策，
顺手写的一句话密度和稳定性都不够。这里单独调一次多模态 LLM：输入 = 图片 bytes
（+ planner 可选提供的群聊语境 context_note——纯看图写不出"这是谁的名场面/本群
怎么用"），输出 = 一段密度优先的中文描述，落进 agent_memes.description；之后
表情包收藏节渲染与 send_messages 选图都只看它。

**2026-08-02 起提示词复用 timeline 图片那张 `image_description.md`**（consumer
名仍是 `caption`，映射在 prompts/catalog.py），原专属的 `meme_caption.md` 删除：
那段"≤150 字覆盖画面/文字/情绪/场景"的写法实跑效果不如客观转录页，篇幅一卡死，
模型就写成一句概括，画面细节与图上文字反而丢了——而选图要的正是这些。取舍与
共用带来的代价见 catalog.py 模块 docstring。role 仍是独立的 `caption`（路由与
温度不合并）。

注入方式：caption_image 由 v2_main 传给 LoopSupervisor → ProgramExecutor，在
run() context 里以 ``caption_image`` 键到达 meme 工具 —— 工具不直接 import
本模块，契约测试塞假 captioner 即可全离线跑（与 session_factory 的注入/伪造
方式一致）。

失败语义：LLM 未配置 / 调用异常 / 空输出一律 **raise CaptionError**，由
meme 工具折成 ToolOutcome.failure("caption_failed", retryable=True)——收录的
核心产出就是描述，生成失败宁可整体失败让 LLM 下拍重试，不落无描述的残记录
（recaption 场景则保留旧描述不动）。
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.image_utils import normalize_image_for_llm
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    should_snapshot,
    write_snapshot,
)

logger = get_logger(__name__)

# 描述上限（字符）。收藏夹整体进 prompt（MAX_SAVED_MEMES 条 × 本上限 = 每拍
# 最坏体积），所以必须有上界；但 2026-08-02 换用 image_description.md 之后
# **提示词里不再有字数要求**（那页要的是"非常详细的描述 + 图上所有文字"），
# 旧的 300 会把详细转录拦腰截断，故放宽到 600：既留得下画面细节与图上文字，
# 100 条满仓也就 6 万字符量级。真嫌大就调这里或 meme_store.MAX_SAVED_MEMES，
# 两个旋钮独立。（ingest 那条链自己的上界是 1200，见 image_description。）
MAX_DESCRIPTION_CHARS = 600

# 看图写描述的 prompt 走 prompts/catalog.py 的 consumer 名 `caption`
# ——2026-08-02 起该名映射到 image_description.md（原 meme_caption.md 已删除，
# 理由见本模块 docstring）。required 段：文件缺失/为空时上抛，caption_image
# 折成 CaptionError（收藏失败、不落表），不静默用空指令看图。
def _load_caption_prompt() -> str:
    from qqbot.services.prompt_assembler import assemble

    return assemble("caption")

class CaptionError(RuntimeError):
    """caption 生成失败（LLM 未配置 / 调用异常 / 空输出）。"""


async def caption_image(
    image_bytes: bytes, mime: str, context_note: str | None = None
) -> str:
    """看图生成收藏描述。失败一律 raise CaptionError（见模块 docstring）。

    走出口网关 ``invoke(scene=caption)``（role=caption 路由）。
    """
    try:
        image_bytes, mime = normalize_image_for_llm(image_bytes, mime or "image/png")
    except Exception as exc:
        raise CaptionError(
            f"caption image conversion failed: {type(exc).__name__}: {exc}"
        ) from exc

    from langchain_core.messages import HumanMessage

    from qqbot.services.event_gateway.outbound import invoke

    try:
        prompt = _load_caption_prompt()
    except Exception as exc:
        raise CaptionError(
            f"caption prompt asset missing: {type(exc).__name__}: {exc}"
        ) from exc
    if context_note:
        prompt += f"\n收藏者附注（聊天语境，据实融进描述）：{context_note}"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime or 'image/png'};base64,{b64}"
                },
            },
        ]
    )
    # Prompt 快照（待办 #11）：辅助 LLM 调用同样留观测记录。图片只记
    # hash/mime/字节数（脱敏契约：base64 永不落盘）；scope_key=None——
    # 收藏夹是全 bot 共享的，caption 不属于任何单一 scope。
    snapshot: PromptSnapshot | None = None
    if should_snapshot(None):
        snapshot = PromptSnapshot(
            kind="meme_caption",
            model=None,
            user_text=prompt,
            images=[
                {
                    "hash": hashlib.sha256(image_bytes).hexdigest(),
                    "mime": mime or "image/png",
                    "bytes": len(image_bytes),
                }
            ],
        )
    started = time.monotonic()
    invoked = await invoke("caption", [message])
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
        if invoked.error == "llm_unavailable":
            raise CaptionError(
                "caption LLM not configured "
                "(config/model_providers.json 缺失，或 caption role 无候选)"
            )
        raise CaptionError(
            f"caption LLM call failed: {invoked.error}"
        )
    text = invoked.text.strip()
    if not text:
        raise CaptionError("caption LLM returned empty text")
    return text[:MAX_DESCRIPTION_CHARS]


def _extract_text(message: Any) -> str:
    """langchain BaseMessage.content 可能是 str 或 list[dict]，拍平成 str
    （与 llm_planner._extract_text 同语义的本地副本，避免反向 import）。"""
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
