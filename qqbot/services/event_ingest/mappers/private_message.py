"""Map OneBot V11 PrivateMessageEvent → external.message.private

scope=private。v2 第一版不实例化 PrivateAgentLoop（实例化策略 §10.1），
但事件依然入库以便审计与未来扩展。
"""

from __future__ import annotations

from typing import Any

from qqbot.services.event_ingest import idempotency
from qqbot.services.event_ingest.napcat_helpers import (
    dump_event,
    dump_message_segments,
    enrich_reply_segments,
)
from qqbot.services.event_ingest.system_event import PartialSystemEvent


class PrivateMessageMapper:
    post_type = "message"
    # 同 GroupMessageMapper 注释：MapperRegistry 用 sub_type=None 表 fallback，
    # 这里给非 None classifier 让 find() 当 exact 处理。判别仍由 can_map 看
    # message_type=private 完成。
    sub_type = "private_message"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "message"
            and getattr(event, "message_type", None) == "private"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        payload = {
            "onebot_message_id": str(getattr(event, "message_id", "")),
            "raw_message": getattr(event, "raw_message", "") or "",
            "sender": _dump_private_sender(event),
            "segments": dump_message_segments(event),
            "message_sub_type": getattr(event, "sub_type", "friend") or "friend",
        }
        # 被引消息富化：与 group_message 同源（napcat_helpers 注释详述）。
        enrich_reply_segments(event, payload["segments"])
        return PartialSystemEvent(
            origin="external",
            type="external.message.private",
            scope="private",
            group_id=None,
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_message(
                getattr(event, "self_id", 0),
                getattr(event, "message_id", ""),
            ),
        )


def _dump_private_sender(event: Any) -> dict:
    """私聊 sender → payload dict。

    OneBot 标准私聊 sender 有 user_id/nickname/sex/age 四个字段；核心 2 键
    （user_id/nickname）恒在，sex/age "有值才落键"（napcat 不上报；仅入库
    供未来用，投影层不渲染）。与 group_message._dump_sender 同一落库策略。
    """
    sender = getattr(event, "sender", None)
    if sender is None:
        return {"user_id": None, "nickname": None}
    out = {
        "user_id": getattr(sender, "user_id", None),
        "nickname": getattr(sender, "nickname", None),
    }
    for attr in ("sex", "age"):
        value = getattr(sender, attr, None)
        if value is not None and str(value).strip() != "":
            out[attr] = value
    return out
