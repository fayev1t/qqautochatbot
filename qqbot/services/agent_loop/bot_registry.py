"""V2 自己的 nonebot Bot 实例缓存。

每个连接上来的 Bot 实例由 v2 ingest plugin handler 在事件触发时调
register() 写进这里；Program Effect / Query 工具（send_messages / kick / ...）
通过 self_id 找回 Bot 实例来调 napcat API。

不复用 v1 (group_chat.py 里的 _bot_instances)：v2 完全自包含。

缓存按进程维护；``threading.Lock`` 保护同一进程内可能出现的多线程访问。
当前部署只绑定一个 QQ 账号，但进程拓扑由部署侧决定，跨进程协调不由本模块提供。
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_bots: dict[str, Any] = {}


def register(bot: Any) -> None:
    """每次 nonebot handler 触发都可以调一遍，同 self_id 反复写无副作用。"""
    self_id = str(getattr(bot, "self_id", "") or "")
    if not self_id:
        return
    with _lock:
        _bots[self_id] = bot


def get(self_id: str | int | None) -> Any | None:
    if self_id is None:
        return None
    with _lock:
        return _bots.get(str(self_id))


def get_any() -> Any | None:
    """取当前进程登记的账号实例（当前单账号边界下的便利接口）。"""
    with _lock:
        if not _bots:
            return None
        return next(iter(_bots.values()))


def all_self_ids() -> list[str]:
    with _lock:
        return list(_bots.keys())


def clear() -> None:
    """仅供测试调用。"""
    with _lock:
        _bots.clear()
