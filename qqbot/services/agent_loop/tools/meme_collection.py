"""MemeCollectionTool —— 表情包收藏管理：action 分发 save / delete / recaption。

**工具名 2026-07-25 由 `meme` 改为 `meme_collection`**（文件名/类名同步）：旧名
是注册表里唯一的裸名词，读起来像"表情包能力"，而发送早在 2026-07-19 就移出了
本工具的参数面——模型（和读代码的人）容易再把它当发图入口。新名点明操作对象
是**收藏夹**而非"发表情包"这件事，description 首句同步改为"只管收藏、从不发送"。
旧名不再注册；append-only 事件表里历史 tool_called 行的 `meme` / `send_meme` /
`save_meme` 原样保留，投影 author index 仍认旧名（见 _build_author_index），
回溯不受影响。

2026-07-12 起由 save_meme / send_meme 两工具（2026-07-03）+ 当晚先行拆分的
delete_meme / recaption_meme 合并而来（应用户拍板"能力全集合在一个表情包
工具中"）：Program API 只暴露一个函数，模型按 `action` 选操作。

三个动作共享同一身份标识：`image_hash`（sha256，与时间线 `<图 hash12 …>`、
收藏节 `<meme>hash12 …` 同一值空间；信封展示 12 位前缀，LLM 原样照抄，
工具按前缀唯一匹配 —— 主线 Part 3 §2.2）。

  save       收录 timeline 里出现过的图片进当前账号的全局收藏夹。定位 EventIngest
             已落盘的文件（内容寻址复用，不复制）→ 已收录直接 already_saved
             → 经 context 注入的 caption_image 看图生成中文描述（planner 可
             用 context_note 补聊天语境）→ 落 agent_memes。**支持批量**
             （2026-07-12，待办 #5）：image_hash 传数组（≤MAX_SAVE_BATCH
             张，去重保序）逐张走单张流程、逐项回执；只要有一张
             saved/already_saved 即 success，全部失败折 batch_save_failed。
             结构类错误（数组里有非法 hash / 超上限 / 空数组）整调拒绝，
             不进入逐张处理——保持整调严格校验。
  delete     把一条收藏移出收藏夹。**只删元数据、不动磁盘文件**（文件是
             EventIngest 的内容寻址缓存，归将来媒体 GC 管，黑盒设计 §7）；
             回执被删条目的描述，确认话术能点名绑定对象。
  recaption  给已收藏的表情包重新生成描述。**描述仍由 caption 链看图生成，
             模型不直接写**（save 的铁律不破）：模型能换的只有 context_note，
             未提供则沿用收录时留档的旧语境（"留档备将来重生成"的兑现点）。
             caption 失败不落表、旧描述保留。

共享语义：收藏夹在当前账号内跨聊天 scope 共用（事件系统设计 §11.3
例外，见 meme_store 模块 docstring）——任何会话收录/删除，其余会话都可见。

失败语义（全程无 raise，error_kind 见黑盒设计 §8）：
  invalid_arguments    action 非法（bad_action）/ hash 非 64 位 hex
                       （bad_image_hash，批量时带 batch_index）/
                       context_note 非字符串（context_note_not_str）或给了
                       不消费它的动作（context_note_not_applicable）/
                       批量结构错（batch_not_supported：非 save 动作传数组；
                       empty_batch / too_many_images）。
  batch_save_failed    save 批量：无一张 saved/already_saved（逐项明细在
                       results；retryable=任一项 retryable）。
  image_not_found      save：hash 合法但盘上无此文件（抄错 / 未下载成功）。
  unknown_meme         delete/recaption：该 hash 不在收藏夹（未收录 /
                       已删除 / recaption 落表时被并发删除）。
  media_file_missing   recaption：收藏在、文件没了（违反 §7 钉住约束）。
  caption_failed       save/recaption：描述生成失败（retryable；recaption
                       旧描述保留）。
  internal_tool_error  session_factory / caption_image 未接线。
  另沿用 tool_unavailable_in_scope。

发送不属于本工具：Planner 在 send_messages 的 messages 里放
`{"kind":"meme","image_hash":…}` 气泡（数量不限），与文本一起按序发送。

依赖注入：session_factory / caption_image / tool_call_event_id 全部来自
ProgramExecutor 统一注入的 run() context，无构造依赖。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.core.time import china_now
from qqbot.services.agent_loop.meme_store import (
    delete_meme,
    find_meme_by_prefix,
    get_meme,
    insert_meme,
    update_meme_description,
)
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._meme_common import (
    coerce_image_hash,
    media_path_for_hash,
    resolve_media_hash,
    sniff_mime,
)

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "meme_collection.md")

# context_note 上限：它是 caption 的辅助输入，不是正文；过长说明模型在把
# 描述塞进 note（描述该由 caption 生成）。
MAX_CONTEXT_NOTE_CHARS = 300

_ACTIONS = ("save", "delete", "recaption")
# context_note 只是 caption 的输入，仅这两个动作消费；其余动作给了 →
# invalid_arguments（大概率是 action 选错了，给精确反馈好过静默忽略）。
_NOTE_ACTIONS = ("save", "recaption")

# save 批量上限：每张都要一次 caption LLM 调用（串行），上限同时约束成本
# 与单拍时长；超限让模型分批（invalid_arguments too_many_images）。
MAX_SAVE_BATCH = 10


def _ambiguous_prefix_failure(prefix: str) -> ToolOutcome:
    """收藏夹内 hash 前缀多义（Part 3 §2.2，几乎不可能但语义封死）。"""
    return ToolOutcome.failure(
        "invalid_arguments",
        f"hash prefix {prefix} matches more than one saved meme; copy more "
        "characters of the hash to disambiguate",
        field="image_hash",
        reason_code="ambiguous_hash_prefix",
        retryable=False,
        transient=False,
        user_fixable=True,
    )


class MemeCollectionTool(BaseTool):
    """实现 Tool 协议；这里只管理收藏，发送由 ``send_messages`` 完成。"""

    name = "meme_collection"
    program_kind = "effect"
    max_call_sites = 2
    description = (
        "管理当前账号内共享的表情包收藏夹，不执行发送。action=save 按 image_hash "
        "收录图片并生成检索描述，支持最多 10 个 hash 的数组；action=delete 删除"
        "收藏记录；action=recaption 重新生成收藏描述。save 和 recaption 可通过 "
        "context_note 提供图片本身不包含的上下文。表情包发送由 send_messages 的 "
        "meme 气泡完成。"
    )
    usage_prompt = _USAGE_PROMPT
    # 收藏管理挂在聊天 scope 上；system scope 不暴露。
    allowed_scopes = ("group", "private")
    arguments_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["save", "delete", "recaption"],
                "description": "收藏操作：save、delete 或 recaption。",
            },
            "image_hash": {
                "type": ["string", "array"],
                "items": {"type": "string", "pattern": "^[0-9a-fA-F]{12,64}$"},
                "description": (
                    "图片哈希：时间线 <图 …> 段或表情包收藏 <meme> 行中的 "
                    "12 位前缀原样照抄（也接受完整 64 位）。save 可接收单个"
                    "字符串或最多 10 个字符串的数组；delete 和 recaption 仅"
                    "接收单个字符串。"
                ),
            },
            "context_note": {
                "type": "string",
                "description": (
                    "仅用于 save 和 recaption 的可选上下文，内容会参与描述生成但"
                    "不会直接展示给用户。recaption 省略该字段时复用收录时保存的值。"
                ),
            },
        },
        "required": ["action", "image_hash"],
    }
    result_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "file_hash": {"type": ["string", "null"]},
            "already_saved": {"type": ["boolean", "null"]},
            "saved": {"type": ["boolean", "null"]},
            "deleted": {"type": ["boolean", "null"]},
            "recaptioned": {"type": ["boolean", "null"]},
            "description": {"type": ["string", "null"]},
            "previous_description": {"type": ["string", "null"]},
            "batch": {"type": ["boolean", "null"]},
            "saved_count": {"type": ["integer", "null"]},
            "already_saved_count": {"type": ["integer", "null"]},
            "failed_count": {"type": ["integer", "null"]},
            "results": {
                "type": ["array", "null"],
                "items": {
                    "type": "object",
                    "properties": {
                        "file_hash": {"type": ["string", "null"]},
                        "already_saved": {"type": ["boolean", "null"]},
                        "saved": {"type": ["boolean", "null"]},
                        "description": {"type": ["string", "null"]},
                        "error_kind": {"type": ["string", "null"]},
                        "error": {"type": ["string", "null"]},
                        "retryable": {"type": ["boolean", "null"]},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if fail := await self.enforce_access(context):
            return fail

        action = arguments.get("action")
        if action not in _ACTIONS:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"action must be one of save/delete/recaption, got {action!r}",
                field="action",
                reason_code="bad_action",
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        raw_hash = arguments.get("image_hash")
        # 批量形态（数组）只对 save 开放：delete/recaption 天然单对象
        # （一次发/删/改一张），传数组多半是想批量收录却选错了 action。
        batch_hashes: list[Any] | None = None
        if isinstance(raw_hash, list):
            if action != "save":
                return ToolOutcome.failure(
                    "invalid_arguments",
                    f"action={action!r} takes a single image_hash string; "
                    "only action=save accepts an array (batch save)",
                    field="image_hash",
                    reason_code="batch_not_supported",
                    retryable=False,
                    transient=False,
                    user_fixable=True,
                )
            batch_hashes = raw_hash
            file_hash = None
        else:
            file_hash, fail = coerce_image_hash(raw_hash)
            if fail or file_hash is None:
                return fail or ToolOutcome.failure(
                    "invalid_arguments", "image_hash is required"
                )

        note_raw = arguments.get("context_note")
        if note_raw is not None:
            if action not in _NOTE_ACTIONS:
                return ToolOutcome.failure(
                    "invalid_arguments",
                    f"context_note is not accepted by action={action!r}; "
                    "it only feeds description generation (save/recaption)",
                    field="context_note",
                    reason_code="context_note_not_applicable",
                    retryable=False,
                    transient=False,
                    user_fixable=True,
                )
            if not isinstance(note_raw, str):
                return ToolOutcome.failure(
                    "invalid_arguments",
                    "context_note must be a string",
                    field="context_note",
                    reason_code="context_note_not_str",
                    retryable=False,
                    transient=False,
                    user_fixable=True,
                )
        context_note = (note_raw or "").strip()[:MAX_CONTEXT_NOTE_CHARS] or None

        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        if not scope_key or not isinstance(scope_key, str) or session_factory is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "meme unavailable: missing scope_key/session_factory context",
            )

        if action == "save" and batch_hashes is not None:
            return await self._save_batch(
                batch_hashes, context_note, scope_key, session_factory, context
            )
        if file_hash is None:
            # 逻辑上不可达（批量只对 save 开放且已在上面分发），纯窄化守卫。
            return ToolOutcome.failure("invalid_arguments", "image_hash is required")
        if action == "save":
            return await self._save(
                file_hash, context_note, scope_key, session_factory, context
            )
        if action == "delete":
            return await self._delete(file_hash, session_factory)
        return await self._recaption(file_hash, context_note, session_factory, context)

    # ── action=save ──

    async def _save(
        self,
        file_hash: str,
        context_note: str | None,
        scope_key: str,
        session_factory: Any,
        context: dict,
    ) -> ToolOutcome:
        # 前缀 → 磁盘唯一完整 hash（Part 3 §2.2）；磁盘存在性 = "bot 真的见
        # 过这张图"。无命中 → hash 抄错 / 图当初没下载成功（<图> 无 hash 的
        # 那类），给 LLM 可自纠的精确失败。
        resolved, fail = resolve_media_hash(file_hash)
        if fail is not None:
            return fail
        if resolved is None:
            return ToolOutcome.failure(
                "image_not_found",
                f"no downloaded image matching hash prefix {file_hash}; "
                "copy the hash exactly from a <图 …> segment in the "
                "timeline (images without a hash were never downloaded and "
                "cannot be saved)",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        file_hash = resolved
        path = media_path_for_hash(file_hash)
        try:
            data = path.read_bytes()
        except OSError:
            return ToolOutcome.failure(
                "image_not_found",
                f"no downloaded image with hash {file_hash}; copy the hash "
                "exactly from a <图 …> segment in the timeline",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )

        # 去重前查（全局收藏夹）：已收录直接回执现有描述，不重复 caption、
        # 不覆盖——别的群先收录的同图也命中这里。
        existing = await get_meme(session_factory, file_hash)
        if existing is not None:
            return ToolOutcome.success(
                {
                    "action": "save",
                    "file_hash": file_hash,
                    "already_saved": True,
                    "description": existing.description,
                }
            )

        captioner = context.get("caption_image")
        if captioner is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "meme save unavailable: caption_image not injected",
            )
        mime = sniff_mime(data)
        try:
            description = await captioner(data, mime, context_note)
        except Exception as exc:  # noqa: BLE001 —— caption 失败折结构化 outcome
            logger.warning("[meme.save] caption failed for {}: {}", file_hash, exc)
            return ToolOutcome.failure(
                "caption_failed",
                f"description generation failed: {exc}",
                file_hash=file_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )
        description = str(description or "").strip()
        if not description:
            return ToolOutcome.failure(
                "caption_failed",
                "description generation returned empty text",
                file_hash=file_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )

        inserted = await insert_meme(
            session_factory,
            file_hash=file_hash,
            description=description,
            context_note=context_note,
            mime=mime,
            source_event_id=context.get("tool_call_event_id"),
            created_at=china_now(),
        )
        if not inserted:
            # 并发窗口：前查之后、落表之前别的保存先到。回执表内那份描述。
            racer = await get_meme(session_factory, file_hash)
            return ToolOutcome.success(
                {
                    "action": "save",
                    "file_hash": file_hash,
                    "already_saved": True,
                    "description": racer.description if racer else description,
                }
            )
        logger.info(
            "[meme.save] saved {} into {} ({} chars)",
            file_hash,
            scope_key,
            len(description),
        )
        return ToolOutcome.success(
            {
                "action": "save",
                "file_hash": file_hash,
                "saved": True,
                "description": description,
            }
        )

    # ── action=save 批量形态 ──

    async def _save_batch(
        self,
        raw_hashes: list,
        context_note: str | None,
        scope_key: str,
        session_factory: Any,
        context: dict,
    ) -> ToolOutcome:
        """批量收录：结构整调校验（空/超限/任一 hash 非法都整体拒绝、不进入
        逐张处理）→ 去重保序 → 逐张复用 `_save` 单张流程 → 逐项回执。
        content 级失败（image_not_found / caption_failed …）只影响该项；
        无一张 saved/already_saved → batch_save_failed。context_note 作用于
        整批（每张 caption 都收到同一份语境）。"""
        if not raw_hashes:
            return ToolOutcome.failure(
                "invalid_arguments",
                "image_hash array is empty; pass 1..%d hashes" % MAX_SAVE_BATCH,
                field="image_hash",
                reason_code="empty_batch",
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        if len(raw_hashes) > MAX_SAVE_BATCH:
            return ToolOutcome.failure(
                "invalid_arguments",
                f"image_hash array has {len(raw_hashes)} entries, max is "
                f"{MAX_SAVE_BATCH}; split into smaller batches",
                field="image_hash",
                reason_code="too_many_images",
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        hashes: list[str] = []
        for i, value in enumerate(raw_hashes):
            normalized, fail = coerce_image_hash(value)
            if fail or normalized is None:
                return ToolOutcome.failure(
                    "invalid_arguments",
                    f"image_hash[{i}] must be 12-64 hex chars (sha256 "
                    f"prefix), got {value!r}; copy each hash verbatim",
                    field="image_hash",
                    reason_code="bad_image_hash",
                    batch_index=i,
                    retryable=False,
                    transient=False,
                    user_fixable=True,
                )
            if normalized not in hashes:  # 同批重复 hash 静默去重（保序）
                hashes.append(normalized)

        results: list[dict] = []
        saved_count = 0
        already_count = 0
        failed_count = 0
        any_retryable = False
        for file_hash in hashes:
            outcome = await self._save(
                file_hash, context_note, scope_key, session_factory, context
            )
            if outcome.ok:
                item = {k: v for k, v in outcome.result.items() if k != "action"}
                if item.get("already_saved"):
                    already_count += 1
                else:
                    saved_count += 1
            else:
                item = {
                    "file_hash": file_hash,
                    "error_kind": outcome.error_kind,
                    "error": outcome.error_message,
                }
                if outcome.extra.get("retryable"):
                    item["retryable"] = True
                    any_retryable = True
                failed_count += 1
            results.append(item)

        if saved_count + already_count == 0:
            # 无一成功：整体折失败，逐项明细随 extra 带回（retryable = 任一
            # 项 retryable，如 caption_failed；纯 hash 抄错类则重试无意义）。
            return ToolOutcome.failure(
                "batch_save_failed",
                f"none of the {len(hashes)} images could be saved; "
                "see per-item results",
                results=results,
                retryable=any_retryable,
                transient=any_retryable,
                user_fixable=not any_retryable,
            )
        logger.info(
            "[meme.save] batch into {}: {} saved, {} already, {} failed",
            scope_key,
            saved_count,
            already_count,
            failed_count,
        )
        return ToolOutcome.success(
            {
                "action": "save",
                "batch": True,
                "results": results,
                "saved_count": saved_count,
                "already_saved_count": already_count,
                "failed_count": failed_count,
            }
        )

    # ── action=delete ──

    async def _delete(self, file_hash: str, session_factory: Any) -> ToolOutcome:
        # 前查（前缀唯一匹配）为了三件事：未收录给精确的 unknown_meme（而
        # 不是"删了 0 条"的含混成功）；前缀多义给 ambiguous_hash_prefix；
        # 命中时把描述带回结果，确认话术能点名删的是哪张。
        meme, ambiguous = await find_meme_by_prefix(session_factory, file_hash)
        if ambiguous:
            return _ambiguous_prefix_failure(file_hash)
        if meme is None:
            return ToolOutcome.failure(
                "unknown_meme",
                f"hash {file_hash} is not a saved meme; nothing to delete — "
                "copy the hash from a <meme> entry in 表情包收藏",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        file_hash = meme.file_hash

        # 并发窗口：前查之后别的删除先到 → rowcount=0。结果状态与本次意图
        # 一致（该 hash 已不在收藏夹），照常回执 deleted。
        await delete_meme(session_factory, file_hash)
        logger.info("[meme.delete] removed {} from collection", file_hash)
        return ToolOutcome.success(
            {
                "action": "delete",
                "file_hash": file_hash,
                "deleted": True,
                "description": meme.description,
            }
        )

    # ── action=recaption ──

    async def _recaption(
        self,
        file_hash: str,
        new_note: str | None,
        session_factory: Any,
        context: dict,
    ) -> ToolOutcome:
        # 只给收录过的换描述：收藏是本动作的操作边界——timeline 里见过
        # 但没收录的图没有描述可换。前缀唯一匹配，多义 → ambiguous。
        meme, ambiguous = await find_meme_by_prefix(session_factory, file_hash)
        if ambiguous:
            return _ambiguous_prefix_failure(file_hash)
        if meme is None:
            return ToolOutcome.failure(
                "unknown_meme",
                f"hash {file_hash} is not a saved meme; only saved memes "
                "have a description to regenerate — copy the hash from a "
                "<meme> entry in 表情包收藏",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        file_hash = meme.file_hash

        # 语境：新 note 优先；未提供沿用收录时留档的旧 note（"留档备将来
        # 重生成"的兑现点）。
        context_note = new_note if new_note is not None else meme.context_note

        path = media_path_for_hash(file_hash)
        try:
            data = path.read_bytes()
        except OSError as exc:
            # 收藏在、文件没了：违反 §7 钉住约束（media 目录被外部清理）。
            logger.warning(
                "[meme.recaption] media file missing for saved meme {}: {}",
                file_hash,
                exc,
            )
            return ToolOutcome.failure(
                "media_file_missing",
                f"meme {file_hash} is saved but its media file is gone from "
                "disk (media dir was cleaned externally); its description "
                "cannot be regenerated",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=False,
            )

        captioner = context.get("caption_image")
        if captioner is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                "meme recaption unavailable: caption_image not injected",
            )
        mime = sniff_mime(data)
        try:
            description = await captioner(data, mime, context_note)
        except Exception as exc:  # noqa: BLE001 —— caption 失败折结构化 outcome
            logger.warning("[meme.recaption] caption failed for {}: {}", file_hash, exc)
            return ToolOutcome.failure(
                "caption_failed",
                f"description regeneration failed: {exc}; the old "
                "description is kept unchanged",
                file_hash=file_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )
        description = str(description or "").strip()
        if not description:
            return ToolOutcome.failure(
                "caption_failed",
                "description regeneration returned empty text; the old "
                "description is kept unchanged",
                file_hash=file_hash,
                retryable=True,
                transient=True,
                user_fixable=False,
            )

        updated = await update_meme_description(
            session_factory,
            file_hash=file_hash,
            description=description,
            context_note=context_note,
        )
        if not updated:
            # 并发窗口：前查之后、落表之前该收藏被 delete 删掉了。
            return ToolOutcome.failure(
                "unknown_meme",
                f"meme {file_hash} was removed from the collection while "
                "regenerating its description",
                file_hash=file_hash,
                retryable=False,
                transient=False,
                user_fixable=True,
            )
        logger.info(
            "[meme.recaption] recaptioned {} ({} chars)",
            file_hash,
            len(description),
        )
        return ToolOutcome.success(
            {
                "action": "recaption",
                "file_hash": file_hash,
                "recaptioned": True,
                "description": description,
                "previous_description": meme.description,
            }
        )
