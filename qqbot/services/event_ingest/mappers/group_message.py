"""Map OneBot V11 GroupMessageEvent → external.message.group.*

Contract: 开发文档/v2.0/20-横切契约/EventIngest契约.md §4

Self-message 回流策略（明确不回流）：bot 自己发出的群消息**不会**进入
external.message.group.*，有两道闸——
① napcat 上报自身消息用的是 post_type="message_sent"（且默认关闭
   reportSelfMessage），nonebot 的 on_message 只匹配 type="message"，
   v2_main 的四个 matcher 都不会接到它；
② 本 mapper 的 can_map 也只认 post_type=="message"。
因此 bot 的发言在事件流里只有一种形态：send_messages 工具的
agent.tool_called/tool_result 对（projection 渲染成 `<tool>send_messages`
行块，那一行块本身就是时间线上的发言记录；author 索引据其
逐条回执里的 message_id+self_id 折出 reply 段的 from_self="true"。已删除的
旧链路 send_message / runtime.reply_flushed 只在历史兼容渲染路径里还认，
不再产生新事件）。
若未来想让"bot 账号在别的设备上手动发的消息"对模型可见，
需要显式开 napcat reportSelfMessage 并新增 message_sent 的 mapper——那是一个
有意的契约变更，不要顺手放行。
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

_SUBTYPE_TO_TYPE = {
    "normal": "external.message.group.normal",
    "anonymous": "external.message.group.anonymous",
    "notice": "external.message.group.notice",
}


class GroupMessageMapper:
    post_type = "message"
    # MapperRegistry 把 sub_type=None 当 fallback 处理；即使 OneBot V11 在
    # message 上的判别器实际是 message_type 而不是 sub_type，这里也要给一个
    # 非 None 的 classifier，让本 mapper 在 find() 里走"exact"分支，盖过
    # 真正的 catch-all fallback。取值仅供 registry 分流，不影响 payload。
    sub_type = "group_message"

    def can_map(self, event: Any) -> bool:
        return (
            getattr(event, "post_type", None) == "message"
            and getattr(event, "message_type", None) == "group"
        )

    def map(self, event: Any) -> PartialSystemEvent:
        msg_sub_type = getattr(event, "sub_type", "normal") or "normal"
        type_name = _SUBTYPE_TO_TYPE.get(msg_sub_type, "external.message.group.normal")

        payload = {
            "onebot_message_id": str(getattr(event, "message_id", "")),
            "raw_message": getattr(event, "raw_message", "") or "",
            "sender": _dump_sender(event),
            "segments": dump_message_segments(event),
            "message_sub_type": msg_sub_type,
        }
        # 被引消息富化：适配器已解析的 event.reply 固化进 reply 段顶层
        # quoted 键——被引消息滚出投影窗口后 from_*/excerpt 不再丢失。
        enrich_reply_segments(event, payload["segments"])
        # 可选补充字段——"有才落键"，不给一库 None：
        # - anonymous：OneBot 标准匿名消息对象（napcat 无匿名支持、恒缺失；
        #   标准实现才会给）。含 flag（set_group_anonymous_ban 凭证），只入库
        #   不渲染——投影层用 name 当 sender_name 兜底并标 anonymous="true"。
        # - real_seq：napcat 扩展的群内真实消息序号（get_group_msg_history
        #   锚点，供未来历史工具用，不渲染）。
        # - message_seq：go-cqhttp 系实现的消息序号（napcat 里恒等于
        #   message_id、无增量信息；非 napcat 实现有真值）。
        # - group_name：napcat 事件级附带的群名（供未来渲染到 agent-input，
        #   让 LLM 知道自己在哪个群——群号被提示词禁止复述，群名可以说）。
        anonymous = _dump_anonymous(getattr(event, "anonymous", None))
        if anonymous:
            payload["anonymous"] = anonymous
        for attr in ("real_seq", "message_seq", "group_name"):
            value = getattr(event, attr, None)
            if value is not None and str(value).strip() != "":
                payload[attr] = value

        return PartialSystemEvent(
            origin="external",
            type=type_name,
            scope="group",
            group_id=getattr(event, "group_id", None),
            user_id=getattr(event, "user_id", None),
            visibility="agent_visible",
            payload=payload,
            raw=dump_event(event),
            idempotency_key=idempotency.for_message(
                getattr(event, "self_id", 0),
                getattr(event, "message_id", ""),
            ),
        )


def _dump_sender(event: Any) -> dict:
    """群消息 sender → payload dict。

    OneBot 标准 sender 有 9 个字段；napcat 实际只给 user_id/nickname/card/
    role 四个（title/level/sex/age/area 在其接收转换器里不赋值——NapCatQQ
    helper/data.ts 实测），但其他 OneBot 实现可能给全。核心 4 键**恒在**
    （保持既有 payload 形状，下游渲染直接 .get），其余 5 键"有值才落键"。
    投影层只渲染 title（sender_title=，社交语境线索）；level/sex/age/area
    仅入库供未来工具/分析用，不进 prompt。
    """
    sender = getattr(event, "sender", None)
    if sender is None:
        return {"user_id": getattr(event, "user_id", None)}
    out = {
        "user_id": getattr(sender, "user_id", None),
        "nickname": getattr(sender, "nickname", None),
        "card": getattr(sender, "card", None),
        "role": getattr(sender, "role", None),
    }
    for attr in ("title", "level", "sex", "age", "area"):
        value = getattr(sender, attr, None)
        if value is not None and str(value).strip() != "":
            out[attr] = value
    return out


def _dump_anonymous(anonymous: Any) -> dict | None:
    """OneBot 匿名消息 anonymous 对象 → dict（id/name/flag），全空 → None。

    兼容 pydantic 对象与 dict 两种形态（nonebot 的 Anonymous 模型 /
    测试 fake）。flag 是后续匿名禁言 API 的凭证，随事件入库、不渲染给 LLM。
    """
    if anonymous is None:
        return None

    def _pick(key: str) -> Any:
        if isinstance(anonymous, dict):
            return anonymous.get(key)
        return getattr(anonymous, key, None)

    out = {"id": _pick("id"), "name": _pick("name"), "flag": _pick("flag")}
    if all(v is None for v in out.values()):
        return None
    return out
