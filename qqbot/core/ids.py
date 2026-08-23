from __future__ import annotations

import os
import threading
import time

_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_MASK = (1 << 80) - 1
_TIME_MASK = (1 << 48) - 1

# 同进程内单调：同一毫秒（或时钟回拨）连续生成的 id 严格递增。
# agent_events.event_id 只经注册层 issue_event_id() 发放；本函数是 ULID 原语，
# 也给 correlation_id / tool_call_id / task_id / raw_id 等非事件身份用。
# 跨进程不协调。
_lock = threading.Lock()
_last_timestamp_ms = -1
_last_randomness = 0


def new_event_id() -> str:
    """生成一枚 ULID 风格的 id（26 字符 Crockford base32）。

    时间分量 48 bit（毫秒）+ 随机分量 80 bit。同一进程、同一毫秒内单调递增。
    ``agent_events.event_id`` 必须经 ``event_gateway.registry.issue_event_id``
    发放，不要在 Mapper / finalize 里直接调用。详见
    开发文档/v2.0/事件系统设计.md §2、§3。
    """
    global _last_timestamp_ms, _last_randomness
    timestamp_ms = int(time.time() * 1000) & _TIME_MASK
    with _lock:
        if timestamp_ms <= _last_timestamp_ms:
            timestamp_ms = _last_timestamp_ms
            randomness = _last_randomness + 1
            if randomness > _RANDOM_MASK:
                timestamp_ms = (_last_timestamp_ms + 1) & _TIME_MASK
                randomness = int.from_bytes(os.urandom(10), "big")
        else:
            randomness = int.from_bytes(os.urandom(10), "big")
        _last_timestamp_ms = timestamp_ms
        _last_randomness = randomness
        value = (timestamp_ms << 80) | randomness

    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD_BASE32[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
