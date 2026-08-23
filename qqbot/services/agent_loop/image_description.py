"""timeline 图片的客观描述（2026-07-28，替代逐拍多模态上传）。

背景：2026-07-28 之前 Planner 与 Replyer 都是 VLM，每一拍把 timeline 窗口里
**所有**图片重新 base64 上传一遍（llm_planner._build_image_blocks，Replyer 复用
同一函数）。一张图在窗口里待几十条事件，就被重复上传几十次 × 2 个调用点，还把
系统里最高频的两个调用点硬绑成必须走 VLM。现在改成：每张图在 ingest
落盘后由 role=vision 看**一次**，写一段客观描述进事件正文（模型由
config 的 groups/roles 选定，不再用 capabilities 标签硬过滤）。现役
Planner 保持纯文本；Replyer 后续已经退役。

为什么描述里不带聊天语境（关键设计取舍）：
  1. ingest 时刻**语境往往还不存在** —— 群里先甩图、隔几条消息再补一句话是常态，
     此刻能抓到的"上下文"要么是空的，要么是别人的话，喂进去就是喂错，而事件
     正文 append-only，错了改不回来。
  2. 语境本来就在 timeline 里，Planner 读整个窗口时自己会合成，不需要 VLM 预先
     理解一遍。
  3. 带语境会让描述**有偏**：VLM 知道"在问报错"就只描述报错那块，画面里别的东西
     不再转录，Planner 之后想问别的已经没有了。
  4. 语境无关才能按 file_hash 全局缓存 —— 重复表情包只付一次调用，这是整个方案
     的成本来源。
  语境相关的理解交给 look_at_image 工具（Planner 现场带着具体问题重看）。

与 meme_caption 的分工（两条 VLM 链并存，刻意不合并）：
  本模块 = 客观转录，给 Planner 判断"发生了什么"，不可变、按 hash 缓存；
  meme_caption = 主观用途标注（"什么场合甩这张"），给 Planner 选图，带
  context_note、可 recaption 重写。同一张图这两份描述内容/可变性/存储位置都不同，
  合并会两头不讨好。

失败语义：本模块仍不向调用方抛可预期的供应商/配置失败，而是记 warning 并返回
None；但 None 不再表示"可静默跳过描述"。EventIngest 把它解释为前置处理失败，
最终写一条 ``runtime.event_ingest_failed``，不会同时写半成品消息事件。

注入方式：`describe_image` 由 v2_main 传给 EventIngest → media.attach_media_to_payload，
event_ingest 侧不静态 import 本模块（保持 ingest 不反向依赖 agent_loop）；生产依赖
统一在 plugin 装配，契约测试塞假 describer 即可全离线跑。
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.models.agent_image_caption import AgentImageCaption
from qqbot.services.agent_loop.image_utils import normalize_image_for_llm
from qqbot.services.agent_loop.prompt_snapshot import (
    PromptSnapshot,
    extract_usage,
    should_snapshot,
    write_snapshot,
)

logger = get_logger(__name__)

SessionFactory = Callable[[], AsyncSession]

# 描述硬截兜底。转录型描述本来就该长（截图要逐字抄），比收藏描述的 300 宽得多，
# 但仍要有上界——一张塞满文字的长图能让模型吐出几千字，那会把 timeline 冲垮。
MAX_DESCRIPTION_CHARS = 1200

# 带问重看的答案上限。答案只回答被问到的那件事，比通篇转录短得多；它会作为
# tool_result 进事件流并在窗口期内每拍重复渲染，放太宽等于把省下的 token 吐回去。
MAX_ANSWER_CHARS = 600

# 供应商单模型的并发上限。一条消息里的 9 图相册、以及几乎同时到达的多条图片
# 消息，都会并发打到同一个 role="vision" 路由上；round_robin 只是把请求摊到多个
# 端点、并不限制在飞数量，所以这里再压一道全局闸。超出的调用排队等待——ingest
# 慢一点可以接受，被网关 429 打回不行。
_MAX_CONCURRENT_CALLS = 5
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CALLS)

# 在途调用表：file_hash → 正在跑的那次描述的 Future。
# agent_image_captions 那层缓存只在**调用结束落表之后**才拦得住重复，拦不住
# 同一张新图被并发首次描述：两条几乎同时到达的消息带同一张新表情包、或同一条
# 消息里重复贴同一张图，都会双双查空缓存、双双调 VLM，再把两段措辞不同的描述
# 分别写进各自的事件正文 —— 事件 append-only，改不回来（同一条消息内会渲染成
# 同 hash 两个 desc，模型多半当成两张图）。这张表让后来者直接等前一个的结果：
# 一次调用、一份描述。
# 无需锁：注册是"读 dict → 建 Future → 写 dict"，三步之间没有 await，单线程
# 事件循环下不可能被切走。
_inflight: dict[str, "asyncio.Future[str | None]"] = {}


async def describe_images(
    items: list[tuple[bytes, str, str]],
    *,
    session_factory: SessionFactory,
) -> list[str | None]:
    """最多 5 张图一次模型请求。空位与失败返回 None。"""
    if not items:
        return []
    if len(items) == 1:
        image_bytes, mime, file_hash = items[0]
        return [
            await describe_image(
                image_bytes, mime, file_hash, session_factory=session_factory
            )
        ]
    results: list[str | None] = []
    pending: list[tuple[int, bytes, str, str]] = []
    for index, (image_bytes, mime, file_hash) in enumerate(items):
        cached = await _load_cached(session_factory, file_hash)
        if cached is not None:
            results.append(cached)
            continue
        results.append(None)
        pending.append((index, image_bytes, mime, file_hash))
    if not pending:
        return results
    prepared: list[tuple[int, bytes, str, str]] = []
    for index, image_bytes, mime, file_hash in pending:
        try:
            payload, payload_mime = normalize_image_for_llm(
                image_bytes, mime or "image/png"
            )
        except Exception as exc:
            logger.warning(
                "[image_description] image conversion failed: {} hash={}",
                exc,
                file_hash,
            )
            continue
        prepared.append((index, payload, payload_mime, file_hash))
    if not prepared:
        return results
    try:
        prompt = _load_prompt("image_description")
    except Exception as exc:
        logger.warning("[image_description] prompt asset missing: {}", exc)
        return results
    async with _semaphore:
        texts = await _invoke_vision_batch(prompt, prepared)
    for (index, payload, payload_mime, file_hash), text in zip(
        prepared, texts, strict=True
    ):
        if not text:
            continue
        results[index] = text
        await _store(
            session_factory,
            file_hash=file_hash,
            description=text,
            mime=payload_mime,
            byte_size=len(payload),
            model=None,
        )
    return results


async def _invoke_vision_batch(
    prompt: str,
    prepared: list[tuple[int, bytes, str, str]],
) -> list[str | None]:
    from langchain_core.messages import HumanMessage

    from qqbot.services.event_gateway.outbound import invoke

    n = len(prepared)
    user_text = (
        prompt
        + f"\n\n下面共 {n} 张图，按顺序各写一段客观描述。"
        + "只输出 JSON 字符串数组，长度必须等于图数，不要其它文字。"
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for _, payload, payload_mime, _hash in prepared:
        b64 = base64.b64encode(payload).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{payload_mime};base64,{b64}"},
            }
        )
    invoked = await invoke(
        "image_description",
        [HumanMessage(content=content)],
        extra={"batch_size": n},
    )
    if not invoked.ok:
        logger.warning(
            "[image_description] batch VLM call failed: {}",
            invoked.error,
        )
        return [None] * n
    parsed = _parse_description_list(invoked.text.strip(), n)
    return [item[:MAX_DESCRIPTION_CHARS] if item else None for item in parsed]


def _parse_description_list(text: str, expected: int) -> list[str | None]:
    import json

    blob = text.strip()
    start = blob.find("[")
    end = blob.rfind("]")
    if start >= 0 and end > start:
        blob = blob[start : end + 1]
    try:
        data = json.loads(blob)
    except Exception:
        parts = [p.strip() for p in text.split("\n---\n") if p.strip()]
        data = parts
    if not isinstance(data, list):
        return [None] * expected
    out: list[str | None] = []
    for item in data[:expected]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        else:
            out.append(None)
    while len(out) < expected:
        out.append(None)
    return out


async def describe_image(
    image_bytes: bytes,
    mime: str,
    file_hash: str,
    *,
    session_factory: SessionFactory,
) -> str | None:
    """看图写客观描述。命中缓存直接返回；失败返回 None（不 raise）。

    调用方是 media.attach_media_to_payload 里的每图协程，本函数自己负责查表、
    在途去重、限并发、落表，调用方只管把返回值写进 segment。

    同 hash 的并发调用只会真跑一次（见 `_inflight`）：注册必须在**任何 await
    之前**完成，否则第二个协程会在第一个还卡在查库的 await 上时溜过去。
    """
    existing = _inflight.get(file_hash)
    if existing is not None:
        # 同一张图已经在描述中：等它，别再查库也别再调 VLM。
        return await existing
    future: "asyncio.Future[str | None]" = (
        asyncio.get_running_loop().create_future()
    )
    _inflight[file_hash] = future
    result: str | None = None
    try:
        result = await _describe_uncached(
            image_bytes, mime, file_hash, session_factory=session_factory
        )
        return result
    finally:
        # 无论正常返回、异常还是被取消，都必须摘掉登记并给等待者一个结果，
        # 否则同 hash 的后来者会永远挂在这个 Future 上。异常/取消时 result
        # 仍是 None —— 等待者据此生成同样的入站失败终态。
        _inflight.pop(file_hash, None)
        if not future.done():
            future.set_result(result)


async def _describe_uncached(
    image_bytes: bytes,
    mime: str,
    file_hash: str,
    *,
    session_factory: SessionFactory,
) -> str | None:
    """describe_image 去掉在途去重之后的本体：查缓存 → 转换 → 限并发调用 →
    落表。自身不 raise（每步各自吞异常降级成 None）。"""
    cached = await _load_cached(session_factory, file_hash)
    if cached is not None:
        return cached

    try:
        payload, payload_mime = normalize_image_for_llm(
            image_bytes, mime or "image/png"
        )
    except Exception as exc:
        logger.warning(
            "[image_description] image conversion failed: {} hash={}",
            exc,
            file_hash,
        )
        return None

    try:
        prompt = _load_prompt("image_description")
    except Exception as exc:
        logger.warning("[image_description] prompt asset missing: {}", exc)
        return None

    async with _semaphore:
        description, model_spec = await _invoke_vision(
            prompt,
            payload,
            payload_mime,
            file_hash,
            kind="image_description",
            max_chars=MAX_DESCRIPTION_CHARS,
        )
    if description is None:
        return None

    await _store(
        session_factory,
        file_hash=file_hash,
        description=description,
        mime=payload_mime,
        byte_size=len(payload),
        model=model_spec,
    )
    return description


async def _load_cached(
    session_factory: SessionFactory, file_hash: str
) -> str | None:
    """查写时缓存。DB 出问题不该让整条 ingest 翻车——降级成"没缓存"，大不了
    多花一次 VLM 调用。"""
    stmt = select(AgentImageCaption.description).where(
        AgentImageCaption.file_hash == file_hash
    )
    try:
        async with session_factory() as session:
            result = await session.execute(stmt)
            return result.scalars().first()
    except Exception as exc:
        logger.warning(
            "[image_description] cache lookup failed: {} hash={}", exc, file_hash
        )
        return None


async def _store(
    session_factory: SessionFactory,
    *,
    file_hash: str,
    description: str,
    mime: str,
    byte_size: int,
    model: str | None,
) -> None:
    """落写时缓存。ON CONFLICT DO NOTHING：并发描述同一张图时先到先得，
    描述本身就是不可变事实，后到的没必要覆盖。落表失败只记日志——描述已经
    拿到手，这一次仍然算成功，只是下次同一张图要再付一次调用。"""
    stmt = (
        pg_insert(AgentImageCaption)
        .values(
            file_hash=file_hash,
            description=description,
            mime=mime,
            byte_size=byte_size,
            model=model,
            created_at=china_now(),
        )
        .on_conflict_do_nothing(index_elements=["file_hash"])
    )
    try:
        async with session_factory() as session:
            await session.execute(stmt)
            await session.commit()
    except Exception as exc:
        logger.warning(
            "[image_description] cache store failed: {} hash={}", exc, file_hash
        )


def _load_prompt(consumer: str) -> str:
    """required 段：文件缺失/为空时 render 直接 raise，由调用方折成失败
    （不静默拿空指令看图——那会写出一段没有信息量的描述并永久缓存）。"""
    from qqbot.services.prompt_assembler import assemble

    return assemble(consumer)


async def _invoke_vision(
    prompt: str,
    image_bytes: bytes,
    mime: str,
    file_hash: str,
    *,
    kind: str,
    max_chars: int,
) -> tuple[str | None, str | None]:
    """一次看图调用（描述与带问重看共用）。返回 (文本, 端点 spec)；任何失败
    → (None, None)。调用方负责持 `_semaphore`——所有 vision 调用共享同一道
    并发闸，不因为走了不同入口就绕开供应商的并发上限。"""
    # 温度在 vision 这条 role 指向的**端点**上配（2026-08-14 起采样参数只在
    # providers[].models[] 上声明）。建议低温 0.2：同一张图的客观描述应当稳定，
    # 不需要发散；与 caption 同理。见 LLM 路由契约 §2。
    from langchain_core.messages import HumanMessage

    from qqbot.services.event_gateway.outbound import invoke

    b64 = base64.b64encode(image_bytes).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            },
        ]
    )

    scene = "image_look" if kind == "image_look" else "image_description"
    # Prompt 快照：脱敏契约要求 base64 永不落盘，图片只记 hash/mime/字节数。
    # scope_key=None —— 描述按 hash 全局共享，不属于任何单一 scope。
    snapshot: PromptSnapshot | None = None
    if should_snapshot(None):
        snapshot = PromptSnapshot(
            kind=kind,
            model=None,
            user_text=prompt,
            images=[
                {"hash": file_hash, "mime": mime, "bytes": len(image_bytes)}
            ],
        )

    started = time.monotonic()
    invoked = await invoke(scene, [message], extra={"file_hash": file_hash})
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
            "[image_description] VLM call failed ({}) hash={} err={}",
            kind,
            file_hash,
            invoked.error,
        )
        return None, None
    text = invoked.text.strip()
    if not text:
        logger.warning(
            "[image_description] VLM returned empty text ({}) hash={}",
            kind,
            file_hash,
        )
        return None, None
    model_spec = getattr(invoked.raw, "model_name", None) or getattr(
        invoked.raw, "model", None
    )
    return text[:max_chars], model_spec


class ImageLookError(RuntimeError):
    """look_at_image 的带问重看失败（未配置 / 转换失败 / 调用失败 / 空输出）。

    与 describe_image 的"吞成 None"相反：重看是模型**主动发起**的工具调用，
    失败必须让它知道（折成 ToolOutcome.failure），不能静默返回空答案。
    """


async def answer_about_image(
    image_bytes: bytes, mime: str, file_hash: str, question: str
) -> str:
    """带着具体问题重看一张图（look_at_image 工具的内部调用）。

    与 describe_image 的分工：那条是 ingest 期的无语境客观转录、结果永久缓存；
    这条是 Planner 现场带着 timeline 语境提问，**不缓存**（同一张图不同问题
    答案不同，而相同问题的答案已经作为 tool_result 留在事件流里了）。
    共用同一把 `_semaphore` 与 role="vision" 路由。
    """
    try:
        payload, payload_mime = normalize_image_for_llm(
            image_bytes, mime or "image/png"
        )
    except Exception as exc:
        raise ImageLookError(
            f"image conversion failed: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        prompt = _load_prompt("image_look")
    except Exception as exc:
        raise ImageLookError(
            f"prompt asset missing: {type(exc).__name__}: {exc}"
        ) from exc
    prompt = f"{prompt}\n\n提问：{question}"

    async with _semaphore:
        answer, _ = await _invoke_vision(
            prompt,
            payload,
            payload_mime,
            file_hash,
            kind="image_look",
            max_chars=MAX_ANSWER_CHARS,
        )
    if answer is None:
        raise ImageLookError(
            "vision LLM unavailable or returned nothing "
            "(未配置带 vision 能力的候选 / 调用失败 / 空输出，详见日志)"
        )
    return answer


def _extract_text(raw: Any) -> str:
    """AIMessage.content → str（与 meme_caption._extract_text 同构：部分网关把
    多模态回复拆成 block 数组）。"""
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
