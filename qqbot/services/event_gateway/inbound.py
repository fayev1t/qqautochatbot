"""统一上游入口网关。

盖本地 occurred_at（不用 NapCat 自带时间），把原文写入 raw 表，插入成功后
交给注册器。event_id 不在这里发。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from qqbot.core.time import china_now
from qqbot.services.raw_event_log import insert_raw_event

SessionFactory = Any


@dataclass
class UpstreamEnvelope:
    """网关盖章后、注册器处理前的上游对象。"""

    channel: str
    occurred_at: datetime
    seq: int
    payload: dict[str, Any]
    source: Any
    future: asyncio.Future[Any] = field(repr=False)
    notify: bool | None = None


class InboundGateway:
    """所有上游事件的指定入口。"""

    def __init__(self, session_factory: SessionFactory, registrar: Any) -> None:
        self._session_factory = session_factory
        self._registrar = registrar

    async def submit(
        self,
        channel: str,
        payload: dict[str, Any],
        *,
        source: Any = None,
        occurred_at: datetime | None = None,
        notify: bool | None = None,
    ) -> Any:
        stamped = occurred_at if occurred_at is not None else china_now()
        seq = self._registrar.allocate_seq()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        envelope = UpstreamEnvelope(
            channel=str(channel or "").strip() or "other",
            occurred_at=stamped,
            seq=seq,
            payload=payload if isinstance(payload, dict) else {},
            source=source,
            future=future,
            notify=notify,
        )
        inserted = await insert_raw_event(
            self._session_factory,
            channel=envelope.channel,
            payload=envelope.payload,
            received_at=occurred_at,
        )
        if not inserted:
            from qqbot.services.event_ingest.ingest import IngestResult

            result = IngestResult(status="error", reason="raw_insert_failed")
            if not future.done():
                future.set_result(result)
            return result
        return await self._registrar.enqueue(envelope)
