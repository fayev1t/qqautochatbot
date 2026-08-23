"""Contract tests for web_digest（2026-08-03，webfetch/websearch 抓取正文的内部提炼）.

Covers:
- happy path：role=web_digest 一次调用、system prompt 来自 web_digest.md、
  user 正文含 URL/标题/关注点/原文、产物截断到 MAX_DIGEST_CHARS
- focus 在 user 正文里按 MAX_FOCUS_CHARS 钳制
- create_llm 无候选 / 调用异常 / 超时 / 空输出 → digest_page_text 返回 None
- digest_or_truncate：提炼可用时用提炼；不可用时降级为原文截断到同一上限
  ——两条路的产物都有界（「抓取正文不进事件流」的唯一出口）
- 空正文短路：不调 LLM
- block 数组形态的回复内容拼接（部分网关拆 content）

LLM 与快照全部 mock；prompt 页从磁盘真实加载（纯文件 IO，锁"页存在且非空"）。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

import qqbot.services.agent_loop.web_digest as web_digest
from qqbot.services.agent_loop.web_digest import (
    MAX_DIGEST_CHARS,
    MAX_FOCUS_CHARS,
    digest_or_truncate,
    digest_page_text,
)


class _FakeLLM:
    def __init__(self, reply: Any = "提炼结果", delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.calls: list[Any] = []

    async def ainvoke(self, messages: Any) -> Any:
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=self.reply)


def _patched(llm: _FakeLLM | None):
    """create_llm → 固定 fake；快照关掉（环境无关、零盘写）。"""
    return (
        mock.patch(
            "qqbot.services.event_gateway.outbound.create_llm",
            mock.AsyncMock(return_value=llm),
        ),
        mock.patch.object(web_digest, "should_snapshot", lambda scope: False),
    )


def _digest(text: str, llm: _FakeLLM | None, **kwargs: Any) -> str | None:
    patch_llm, patch_snap = _patched(llm)
    with patch_llm, patch_snap:
        return asyncio.run(
            digest_page_text(text, url="https://site/page", **kwargs)
        )


class WebDigestHappyPathTests(unittest.TestCase):
    def test_digest_truncated_and_messages_composed(self) -> None:
        llm = _FakeLLM(reply="要" * (MAX_DIGEST_CHARS + 500))
        result = _digest(
            "正文" * 100, llm, title="页面标题", focus="保修期多久"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result), MAX_DIGEST_CHARS)
        self.assertEqual(len(llm.calls), 1)
        system, human = llm.calls[0]
        # system prompt 从 web_digest.md 真实装配——页缺失/为空时这里会炸
        self.assertIn("提炼", getattr(system, "content", ""))
        user_text = getattr(human, "content", "")
        self.assertIn("https://site/page", user_text)
        self.assertIn("页面标题", user_text)
        self.assertIn("保修期多久", user_text)
        self.assertIn("正文", user_text)

    def test_focus_clamped_in_user_text(self) -> None:
        llm = _FakeLLM()
        _digest("正文", llm, focus="F" * (MAX_FOCUS_CHARS + 100))
        user_text = getattr(llm.calls[0][1], "content", "")
        self.assertIn("F" * MAX_FOCUS_CHARS, user_text)
        self.assertNotIn("F" * (MAX_FOCUS_CHARS + 1), user_text)

    def test_block_array_reply_content_is_joined(self) -> None:
        llm = _FakeLLM(reply=[{"text": "前半"}, "后半"])
        self.assertEqual(_digest("正文", llm), "前半后半")


class WebDigestFailureTests(unittest.TestCase):
    def test_no_llm_returns_none(self) -> None:
        self.assertIsNone(_digest("正文", None))

    def test_call_error_returns_none(self) -> None:
        self.assertIsNone(_digest("正文", _FakeLLM(reply=RuntimeError("拒"))))

    def test_empty_reply_returns_none(self) -> None:
        self.assertIsNone(_digest("正文", _FakeLLM(reply="   ")))

    def test_timeout_returns_none(self) -> None:
        with mock.patch.object(web_digest, "_DIGEST_TIMEOUT_SEC", 0.01):
            self.assertIsNone(_digest("正文", _FakeLLM(delay=0.05)))

    def test_empty_text_short_circuits_without_llm(self) -> None:
        create = mock.AsyncMock()
        with mock.patch(
            "qqbot.services.event_gateway.outbound.create_llm", create
        ):
            self.assertIsNone(
                asyncio.run(digest_page_text("   ", url="https://x/"))
            )
        create.assert_not_awaited()


class DigestOrTruncateTests(unittest.TestCase):
    def test_uses_digest_when_available(self) -> None:
        patch_llm, patch_snap = _patched(_FakeLLM(reply="短转述"))
        with patch_llm, patch_snap:
            out = asyncio.run(
                digest_or_truncate("原" * 8000, url="https://x/")
            )
        self.assertEqual(out, "短转述")

    def test_degrades_to_bounded_truncation(self) -> None:
        """提炼不可用 ≠ 原文放行：降级产物与提炼共用同一长度上限。"""
        patch_llm, patch_snap = _patched(None)
        with patch_llm, patch_snap:
            out = asyncio.run(
                digest_or_truncate("原" * 8000, url="https://x/")
            )
        self.assertEqual(len(out), MAX_DIGEST_CHARS)
        self.assertEqual(out, "原" * MAX_DIGEST_CHARS)

    def test_empty_text_passthrough(self) -> None:
        create = mock.AsyncMock()
        with mock.patch(
            "qqbot.services.event_gateway.outbound.create_llm", create
        ):
            self.assertEqual(
                asyncio.run(digest_or_truncate("", url="https://x/")), ""
            )
        create.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
