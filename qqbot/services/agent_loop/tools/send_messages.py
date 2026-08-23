"""SendMessagesTool —— Planner 亲自发言的唯一出口（2026-07-31 删除 Replyer）。

2026-08-17 删除 `reply` / ReplyTask 后，"想好的话不会当拍就出去"不再靠提示词
纪律，而由提案-裁决流水线在结构上保证：写下 `send_messages(...)` 的那一拍只把
源码落成一条决策事件，谁都没被调用；要等下一拍重新读完时间线、写
``execute_program(program_hash=…)`` 指名它，气泡才真的发得出去。人打字要花的那段
时间，就是这两拍之间模型重新看世界的那一眼。

本工具自己**始终可调用**，不检查任何前置状态——它是一个普通 Program Effect：
执行器先写 ``agent.tool_called`` 意图，再调用本工具，最后把结构化 receipts 写进
``agent.tool_result | tool_failed``。它不新增 fence / finalizer。若 OneBot 已出手
但 terminal 尚未写成时进程退出，启动收口器会把半截调用标成 ``interrupted`` /
``uncertain``，**永不自动重放**。其 `<tool>send_messages` 行块（气泡 + 回执）就是
时间线上的唯一发言记录；不再派生第二条发言行，`<legacy_reply>` 只兼容历史链路。

结果语义（status 随 receipts 一起落 terminal payload）：

- ``sent``     全部气泡拿到 OneBot 成功响应与 message_id → ``tool_result``；
  这不是用户端最终可见性的证明；
- ``partial``  部分 sent、部分明确 failed → ``tool_failed``（携带完整逐条
  receipts，已 sent 的气泡是既成事实，不得重发）；
- ``failed``   全部明确未发出 → ``tool_failed``；
- ``uncertain`` 至少一条送达与否无法确认 → ``tool_failed``（可能已发出，
  禁止"保险再发一遍"）。

依赖注入：scope_key / session_factory 来自 ProgramExecutor 的 run() context；
目标群取自 scope_key，模型不传 target（跨群隔离，§4.1）。
"""

from __future__ import annotations

from typing import Any

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.outbound_messages import (
    MAX_OUTBOUND_MESSAGES,
    delivery_status,
    first_error_reason,
    invalid_args,
    preflight_memes,
    redact_runtime_value,
    send_all,
    validate_messages,
)
from qqbot.services.agent_loop.prompts import load_sibling_md
from qqbot.services.agent_loop.tool_registry import BaseTool, ToolOutcome
from qqbot.services.agent_loop.tools._onebot_common import get_bot

logger = get_logger(__name__)

_USAGE_PROMPT = load_sibling_md(__file__, "send_messages.md")

# ── 气泡 schema（2026-08-14 去协议化）：一项就是一条消息，键名即语义。此前
# chat 气泡是 {"kind":"chat","content":[{"type":"text","data":{"text":…}}]}——
# data 包装、type 判别、reply 必须 content[0]，三样都是 OneBot 11 的协议规则，
# 却要模型每次发言复述一遍。段数组现在由 outbound_messages.build_chat_content
# 在发送时构造，模型面只剩 text / reply / at / face / meme 五个键。
#
# schema 纯文档用途（tool_registry 模块头），真正的校验始终是
# outbound_messages.validate_messages——两边形状逐字对齐，不得出现 schema 放行
# 而校验拒绝的错位。带 kind 的旧形状仍被 validate_messages 无损接住，但那是迁移
# 兼容路径，故意不写进 schema：schema 是教模型怎么写，不是穷举运行时收什么。
_CHAT_BUBBLE_SCHEMA = {
    "properties": {
        "text": {
            "type": "string",
            "description": "这条消息的文字。只 @ 人或只发表情时可以省略。",
        },
        "reply": {
            "type": ["string", "integer"],
            "description": (
                "可选：被引用消息的 ID。普通发言默认不填；仅在群聊多话题"
                "并行、需要明确指着某一条具体历史消息说话以消除歧义时，"
                "才取时间线 #消息ID 记号里的号原样照抄。引用只作用于本条"
                "气泡。"
            ),
        },
        "at": {
            "type": ["string", "integer", "array"],
            "items": {"type": ["string", "integer"]},
            "description": (
                "可选：要 @ 的 QQ 号，单个或数组；\"all\" 表示 @全体成员。"
                "@ 出现在本条文字之前。"
            ),
        },
        "face": {
            "type": ["string", "integer", "array"],
            "items": {"type": ["string", "integer"]},
            "description": (
                "可选：QQ 系统表情 ID，单个或数组，出现在本条文字之后。"
                "发表情包用 meme 气泡，不是这个。"
            ),
        },
    },
    "additionalProperties": False,
}

_MEME_BUBBLE_SCHEMA = {
    "properties": {
        "meme": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{12,64}$",
            "description": (
                "表情包收藏 <meme> 行中的哈希，12 位前缀原样照抄（也接受完整 64 位）。"
                "该气泡只发这张图，不能同时带文字。"
            ),
        },
    },
    "required": ["meme"],
    "additionalProperties": False,
}


class SendMessagesTool(BaseTool):
    name = "send_messages"
    program_kind = "effect"
    max_call_sites = 2
    # 私聊没有 AgentLoop（Supervisor 丢弃 private:*），system scope 没有聊天
    # 目标——不照抄旧 send_message.py 的 ("group", "private")。
    allowed_scopes = ("group",)
    description = (
        "向当前群发送一条或多条有序气泡。messages 中每项就是一条消息："
        '文字气泡 {"text": …}（可选 reply / at / face）或表情包气泡 '
        '{"meme": …}。返回值中的逐气泡回执是送达状态记录；status=uncertain '
        "表示至少一条气泡可能已经送达。"
    )
    usage_prompt = _USAGE_PROMPT
    arguments_schema = {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OUTBOUND_MESSAGES,
                "description": (
                    "按发送顺序排列的气泡数组，一条或多条均可。每项是一条文字"
                    "消息或一个表情包，两者平级、可任意穿插。"
                ),
                "items": {"oneOf": [_CHAT_BUBBLE_SCHEMA, _MEME_BUBBLE_SCHEMA]},
            },
        },
        "required": ["messages"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["sent"]},
            "message_ids": {
                "type": "array",
                "items": {"type": ["integer", "string"]},
            },
            "sent_messages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "kind": {"type": "string"},
                        "text": {"type": ["string", "null"]},
                        "reply": {"type": ["string", "integer", "null"]},
                        "at": {},
                        "face": {},
                        "image_hash": {"type": ["string", "null"]},
                        "status": {"type": "string"},
                        "message_id": {"type": ["integer", "string", "null"]},
                        "self_id": {"type": ["string", "null"]},
                        "receipt": {"type": "object", "properties": {}},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "required": ["status", "message_ids", "sent_messages"],
        "additionalProperties": False,
    }

    async def execute(self, arguments: dict, **context: Any) -> ToolOutcome:
        if not isinstance(arguments, dict):
            return invalid_args(
                "arguments_not_object",
                "send_messages arguments must be a JSON object",
                field="arguments",
            )
        if fail := await self.enforce_access(context):
            return fail
        scope_key = context.get("scope_key")
        session_factory = context.get("session_factory")
        if not isinstance(scope_key, str) or not scope_key:
            return ToolOutcome.failure(
                "internal_tool_error", "send_messages requires scope_key"
            )

        extras = sorted(set(arguments) - {"messages"})
        if extras:
            return invalid_args(
                "unexpected_argument",
                f"send_messages received unknown argument(s): {', '.join(extras)}",
            )

        # ── 静态校验：形状、段白名单、气泡条数上限（无副作用；meme 不限量）。
        prepared, fail = validate_messages(arguments.get("messages"))
        if fail is not None:
            return fail

        # ── 动态 preflight：meme 是否仍在收藏、媒体是否可读（仍无副作用）。
        if session_factory is None and any(item["kind"] == "meme" for item in prepared):
            return ToolOutcome.failure(
                "internal_tool_error",
                "send_messages requires session_factory to send a meme",
            )
        loaded, error = await preflight_memes(session_factory, prepared)
        if error is not None:
            reason_code, message = error
            return invalid_args(reason_code, message)

        bot, fail = get_bot()
        if fail:
            return fail

        # ── OneBotGateway 逐条发送 → 逐条 receipts → status 折叠（§4.3）。
        receipts = await send_all(bot, scope_key, loaded)
        status = delivery_status(receipts)
        public = redact_runtime_value(receipts)
        message_ids = [
            item["message_id"]
            for item in public
            if item.get("status") == "sent" and item.get("message_id") is not None
        ]
        if status == "sent":
            return ToolOutcome.success(
                {
                    "status": "sent",
                    "message_ids": message_ids,
                    "sent_messages": public,
                }
            )
        reason = first_error_reason(receipts) or (
            "delivery result is unknown for at least one bubble"
            if status == "uncertain"
            else "no bubble was delivered"
        )
        logger.warning("[send_messages] {} delivery {}: {}", scope_key, status, reason)
        return ToolOutcome.failure(
            "upstream_action_failed",
            reason,
            status=status,
            message_ids=message_ids,
            sent_messages=public,
            retryable=False,
            transient=False,
            # partial/uncertain 不是"改参数重试"能修的；failed 可另组新调用。
            user_fixable=status == "failed",
        )
