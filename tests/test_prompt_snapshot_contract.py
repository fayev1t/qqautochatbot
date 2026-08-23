"""Prompt 快照契约测试（待办 #11 可观测性基线）。

覆盖：
- 开关 / scope 白名单：关闭不落盘；私聊 scope 默认不落盘；无 scope 的辅助
  调用放行
- 快照 schema：元信息（scope/tick/correlation/model/outcome）、分段统计
  （name/chars/sha256）、attempts（latency/usage/响应原文/error）、
  整体 sha256 与正文一致
- 脱敏硬保证：内联 base64 data URL 抹掉、已配置密钥值抹掉、图片只落
  hash/mime/bytes 元信息
- 保留清理：文件数超 PROMPT_SNAPSHOT_KEEP 删最旧
- usage 归一化：usage_metadata / response_metadata.token_usage 两条来源
- LLMPlanner 集成：源码响应与调用异常各落一份快照且不改变决策行为
- meme_caption 集成：辅助调用落 kind=meme_caption 快照，base64 不落盘

所有用例显式设置 PROMPT_SNAPSHOT_* 环境变量（os.environ 优先于 .env 文件，
见 core/settings.get_env_value）——不依赖"默认值"，服务器 .env 里开没开
采集都不影响测试结果，也绝不写真实快照目录。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from qqbot.core.time import china_now
from qqbot.services.agent_loop.decision import DecisionContext
from qqbot.services.agent_loop.llm_planner import LLMPlanner
from qqbot.services.agent_loop.prompt_snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    PromptSnapshot,
    extract_usage,
    should_snapshot,
    write_snapshot,
)

_ENV_KEYS = (
    "PROMPT_SNAPSHOT_ENABLED",
    "PROMPT_SNAPSHOT_DIR",
    "PROMPT_SNAPSHOT_KEEP",
    "PROMPT_SNAPSHOT_SCOPES",
)


class _SnapshotEnvTestCase(unittest.TestCase):
    """临时目录 + 显式 env 的公共基座；tearDown 恢复原 env。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.snapshot_dir = Path(self._tmp.name)
        self._saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["PROMPT_SNAPSHOT_ENABLED"] = "true"
        os.environ["PROMPT_SNAPSHOT_DIR"] = str(self.snapshot_dir)
        os.environ["PROMPT_SNAPSHOT_KEEP"] = "50"
        os.environ["PROMPT_SNAPSHOT_SCOPES"] = "group,system"

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def files(self) -> list[Path]:
        return sorted(self.snapshot_dir.glob("*.json"))

    def read_single(self) -> dict[str, Any]:
        files = self.files()
        self.assertEqual(len(files), 1, f"expected 1 snapshot, got {files}")
        return json.loads(files[0].read_text(encoding="utf-8"))


def _snapshot(**overrides: Any) -> PromptSnapshot:
    base: dict[str, Any] = {
        "kind": "planner",
        "scope_key": "group:100",
        "tick_seq": 7,
        "correlation_id": "CID-1",
        "model": "gpt-test",
        "system_prompt": "SYSTEM BODY",
        "user_text": "<agent-input>USER BODY</agent-input>",
        "sections": [{"name": "identity", "chars": 11, "sha256": "x" * 64}],
        "images": [{"hash": "h" * 64, "mime": "image/png", "bytes": 123}],
        "outcome": "parsed",
    }
    base.update(overrides)
    return PromptSnapshot(**base)


class GatingTests(_SnapshotEnvTestCase):
    def test_disabled_writes_nothing(self) -> None:
        os.environ["PROMPT_SNAPSHOT_ENABLED"] = "false"
        self.assertFalse(should_snapshot("group:100"))
        self.assertIsNone(write_snapshot(_snapshot()))
        self.assertEqual(self.files(), [])

    def test_private_scope_blocked_by_default_whitelist(self) -> None:
        self.assertFalse(should_snapshot("private:42"))
        self.assertIsNone(write_snapshot(_snapshot(scope_key="private:42")))
        self.assertEqual(self.files(), [])

    def test_none_scope_allowed_for_aux_calls(self) -> None:
        # 辅助调用（meme_caption）无 scope：白名单不拦
        self.assertTrue(should_snapshot(None))

    def test_write_snapshot_rechecks_gate_itself(self) -> None:
        # 即便调用方忘了预判，write_snapshot 自己也复核（配置说不落就不落）
        os.environ["PROMPT_SNAPSHOT_SCOPES"] = "system"
        self.assertIsNone(write_snapshot(_snapshot(scope_key="group:100")))
        self.assertEqual(self.files(), [])


class SchemaTests(_SnapshotEnvTestCase):
    def test_written_payload_fields(self) -> None:
        snap = _snapshot()
        snap.add_attempt(
            latency_ms=812,
            response_text='{"actions":[]}',
            usage={"prompt_tokens": 10, "completion_tokens": 2},
        )
        path = write_snapshot(snap)
        self.assertIsNotNone(path)
        data = self.read_single()

        self.assertEqual(data["schema"], SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(data["kind"], "planner")
        self.assertEqual(data["scope_key"], "group:100")
        self.assertEqual(data["tick_seq"], 7)
        self.assertEqual(data["correlation_id"], "CID-1")
        self.assertEqual(data["model"], "gpt-test")
        self.assertEqual(data["outcome"], "parsed")
        self.assertNotIn("validation_retry", data)
        # 正文 + 与正文一致的体积/哈希
        self.assertEqual(data["system_prompt"], "SYSTEM BODY")
        self.assertEqual(data["system_prompt_chars"], len("SYSTEM BODY"))
        self.assertEqual(
            data["system_prompt_sha256"],
            hashlib.sha256(b"SYSTEM BODY").hexdigest(),
        )
        self.assertEqual(data["user_text_chars"], len(data["user_text"]))
        # 分段统计与图片元信息原样透传
        self.assertEqual(data["sections"][0]["name"], "identity")
        self.assertEqual(data["images"][0]["mime"], "image/png")
        # attempts
        self.assertEqual(len(data["attempts"]), 1)
        attempt = data["attempts"][0]
        self.assertEqual(attempt["latency_ms"], 812)
        self.assertEqual(attempt["response_text"], '{"actions":[]}')
        self.assertEqual(attempt["response_chars"], len('{"actions":[]}'))
        self.assertEqual(attempt["usage"]["prompt_tokens"], 10)
        self.assertIsNone(attempt["error"])

    def test_filename_carries_kind_scope_tick(self) -> None:
        write_snapshot(_snapshot())
        name = self.files()[0].name
        self.assertIn("planner", name)
        self.assertIn("group-100", name)  # scope 冒号已 sanitize
        self.assertIn("tick7", name)


class RedactionTests(_SnapshotEnvTestCase):
    def test_inline_base64_data_url_scrubbed(self) -> None:
        payload = "A" * 200
        text = f'<image src="data:image/png;base64,{payload}"/> tail'
        write_snapshot(_snapshot(user_text=text))
        data = self.read_single()
        self.assertNotIn(payload, json.dumps(data))
        self.assertIn("data:<base64-redacted>", data["user_text"])
        self.assertIn("tail", data["user_text"])

    def test_secret_env_value_scrubbed(self) -> None:
        saved = os.environ.get("LLM_API_KEY")
        os.environ["LLM_API_KEY"] = "sk-super-secret-key-12345"
        try:
            write_snapshot(
                _snapshot(
                    system_prompt="key is sk-super-secret-key-12345 here",
                    user_text="also sk-super-secret-key-12345",
                )
            )
            raw = self.files()[0].read_text(encoding="utf-8")
            self.assertNotIn("sk-super-secret-key-12345", raw)
            self.assertIn("[REDACTED:LLM_API_KEY]", raw)
        finally:
            if saved is None:
                os.environ.pop("LLM_API_KEY", None)
            else:
                os.environ["LLM_API_KEY"] = saved

    def test_model_providers_file_every_api_key_scrubbed(self) -> None:
        """多服务商注册表里的每把 api_key 都必须被抹掉。"""
        config = {
            "providers": [
                {
                    "name": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-provider-one-secret-111",
                    "models": ["deepseek-chat"],
                },
                {
                    "name": "relay",
                    "base_url": "https://relay.example.com/v1",
                    "api_key": "sk-provider-two-secret-222",
                    "models": ["gpt-4o"],
                },
            ]
        }
        # 放独立子目录：快照目录的 *.json 计数不能被配置文件污染
        config_dir = Path(self._tmp.name) / "cfg"
        config_dir.mkdir()
        config_path = config_dir / "model_providers.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
        saved = os.environ.get("MODEL_PROVIDERS_PATH")
        os.environ["MODEL_PROVIDERS_PATH"] = str(config_path)
        try:
            write_snapshot(
                _snapshot(
                    system_prompt="leak sk-provider-one-secret-111",
                    user_text="leak sk-provider-two-secret-222",
                )
            )
            raw = self.files()[0].read_text(encoding="utf-8")
            self.assertNotIn("sk-provider-one-secret-111", raw)
            self.assertNotIn("sk-provider-two-secret-222", raw)
            self.assertIn("[REDACTED:MODEL_PROVIDERS:deepseek]", raw)
            self.assertIn("[REDACTED:MODEL_PROVIDERS:relay]", raw)
        finally:
            if saved is None:
                os.environ.pop("MODEL_PROVIDERS_PATH", None)
            else:
                os.environ["MODEL_PROVIDERS_PATH"] = saved


class RetentionTests(_SnapshotEnvTestCase):
    def test_oldest_files_deleted_beyond_keep(self) -> None:
        os.environ["PROMPT_SNAPSHOT_KEEP"] = "3"
        for tick in range(1, 6):
            write_snapshot(_snapshot(tick_seq=tick))
        files = self.files()
        self.assertEqual(len(files), 3)
        names = "".join(f.name for f in files)
        # 最旧的 tick1/tick2 已清理，最新三份保留
        for expected in ("tick3", "tick4", "tick5"):
            self.assertIn(expected, names)
        self.assertNotIn("tick1", names)
        self.assertNotIn("tick2", names)


class ExtractUsageTests(unittest.TestCase):
    def test_from_usage_metadata(self) -> None:
        raw = SimpleNamespace(
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_token_details": {"cache_read": 64},
            }
        )
        self.assertEqual(
            extract_usage(raw),
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cache_read_tokens": 64,
            },
        )

    def test_from_response_metadata_token_usage(self) -> None:
        raw = SimpleNamespace(
            usage_metadata=None,
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 2,
                    "total_tokens": 9,
                    "prompt_tokens_details": {"cached_tokens": 4},
                }
            },
        )
        self.assertEqual(
            extract_usage(raw),
            {
                "prompt_tokens": 7,
                "completion_tokens": 2,
                "total_tokens": 9,
                "cache_read_tokens": 4,
            },
        )

    def test_stub_without_usage_returns_none(self) -> None:
        self.assertIsNone(extract_usage(SimpleNamespace(content="x")))


class _StubLLM:
    def __init__(
        self,
        response_content: str = "",
        raise_exc: Exception | None = None,
        usage_metadata: dict | None = None,
    ) -> None:
        self.response_content = response_content
        self.raise_exc = raise_exc
        self.usage_metadata = usage_metadata
        self.model_name = "stub-model"

    async def ainvoke(self, messages: Any) -> Any:
        if self.raise_exc:
            raise self.raise_exc
        return SimpleNamespace(
            content=self.response_content,
            usage_metadata=self.usage_metadata,
        )


def _ctx(scope_key: str = "group:100") -> DecisionContext:
    return DecisionContext(
        scope_key=scope_key,
        correlation_id="CID-9",
        tick_seq=3,
        now=china_now(),
    )


class PlannerSnapshotIntegrationTests(_SnapshotEnvTestCase):
    def test_successful_decide_writes_planner_snapshot(self) -> None:
        llm = _StubLLM(
            response_content="# idle",
            usage_metadata={
                "input_tokens": 11,
                "output_tokens": 3,
                "total_tokens": 14,
            },
        )
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))

        data = self.read_single()
        self.assertEqual(data["kind"], "planner")
        self.assertEqual(data["scope_key"], "group:100")
        self.assertEqual(data["tick_seq"], 3)
        self.assertEqual(data["correlation_id"], "CID-9")
        self.assertEqual(data["model"], "stub-model")
        self.assertEqual(data["outcome"], "received")
        # system prompt 分段统计（根页正文挂消费者名，槽挂槽名；2026-07-31
        # 并页后 group scope 只剩 planner / envelope 两种段名）
        section_names = [s["name"] for s in data["sections"]]
        self.assertIn("planner", section_names)
        self.assertIn("envelope", section_names)
        for sec in data["sections"]:
            self.assertGreater(sec["chars"], 0)
            self.assertEqual(len(sec["sha256"]), 64)
        # user 行文法信封原文在快照里
        self.assertIn("# 决策输入", data["user_text"])
        self.assertNotIn("## 工具目录", data["user_text"])
        # attempts：latency + usage + 响应原文
        self.assertEqual(len(data["attempts"]), 1)
        attempt = data["attempts"][0]
        self.assertIsInstance(attempt["latency_ms"], int)
        self.assertEqual(attempt["usage"]["total_tokens"], 14)
        self.assertIn("# idle", attempt["response_text"])

    def test_body_is_recorded_once_without_planner_side_parsing(self) -> None:
        llm = _StubLLM(response_content="NOT PYTHON AT ALL")
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(out.program, "NOT PYTHON AT ALL")

        data = self.read_single()
        self.assertEqual(data["outcome"], "received")
        self.assertEqual(len(data["attempts"]), 1)
        self.assertEqual(data["attempts"][0]["response_text"], "NOT PYTHON AT ALL")

    def test_llm_call_error_records_call_error_outcome(self) -> None:
        llm = _StubLLM(raise_exc=RuntimeError("boom"))
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(out.program, "")
        self.assertEqual(out.planner_error, "llm_call_error:RuntimeError")

        data = self.read_single()
        self.assertEqual(data["outcome"], "call_error")
        self.assertEqual(len(data["attempts"]), 1)
        self.assertIn("RuntimeError", data["attempts"][0]["error"])

    def test_disabled_snapshot_leaves_decide_untouched(self) -> None:
        os.environ["PROMPT_SNAPSHOT_ENABLED"] = "false"
        llm = _StubLLM(response_content="# idle")
        planner = LLMPlanner(llm_client=llm)
        out = asyncio.run(planner.decide(_ctx()))
        self.assertEqual(out.program, "# idle")
        self.assertEqual(self.files(), [])

    def test_private_scope_not_written(self) -> None:
        llm = _StubLLM(response_content="# idle")
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx(scope_key="private:55")))
        self.assertEqual(self.files(), [])

    def test_one_tick_writes_exactly_one_attempt(self) -> None:
        """一个响应绑定一次模型执行（2026-08-21 §一⑦）：快照与拍一一对应。

        同拍三次静态重试撤销后，同一 ``tick_seq`` 下不再出现多份 planner 快照，
        ``validation_retry`` 这个标志位也随之删除。
        """
        llm = _StubLLM(response_content="# idle")
        planner = LLMPlanner(llm_client=llm)
        asyncio.run(planner.decide(_ctx()))
        data = self.read_single()
        self.assertEqual(len(data["attempts"]), 1)
        self.assertNotIn("validation_retry", data)


class MemeCaptionSnapshotIntegrationTests(_SnapshotEnvTestCase):
    def test_caption_call_writes_snapshot_without_base64(self) -> None:
        from qqbot.services.agent_loop.meme_caption import caption_image

        llm = _StubLLM(response_content="一张测试用表情包描述")

        async def fake_create_llm(
            temperature: float | None = None, **_kwargs: Any
        ) -> Any:
            return llm

        with mock.patch(
            "qqbot.services.event_gateway.outbound.create_llm",
            fake_create_llm,
        ):
            text = asyncio.run(
                caption_image(b"\x89PNG-fake-bytes", "image/png", "群里语境")
            )
        self.assertEqual(text, "一张测试用表情包描述")

        data = self.read_single()
        self.assertEqual(data["kind"], "meme_caption")
        self.assertIsNone(data["scope_key"])
        self.assertEqual(data["outcome"], "ok")
        self.assertIn("收藏者附注", data["user_text"])
        # 图片只落元信息：hash 是原始 bytes 的 sha256，无任何 base64
        image = data["images"][0]
        self.assertEqual(
            image["hash"], hashlib.sha256(b"\x89PNG-fake-bytes").hexdigest()
        )
        self.assertEqual(image["bytes"], len(b"\x89PNG-fake-bytes"))
        raw = self.files()[0].read_text(encoding="utf-8")
        self.assertNotIn("base64,", raw)

    def test_caption_failure_still_snapshots_then_raises(self) -> None:
        from qqbot.services.agent_loop.meme_caption import (
            CaptionError,
            caption_image,
        )

        llm = _StubLLM(raise_exc=RuntimeError("gateway down"))

        async def fake_create_llm(
            temperature: float | None = None, **_kwargs: Any
        ) -> Any:
            return llm

        with mock.patch(
            "qqbot.services.event_gateway.outbound.create_llm",
            fake_create_llm,
        ):
            with self.assertRaises(CaptionError):
                asyncio.run(caption_image(b"\x89PNG-fake-bytes", "image/png"))

        data = self.read_single()
        self.assertEqual(data["outcome"], "call_error")
        self.assertIn("RuntimeError", data["attempts"][0]["error"])


if __name__ == "__main__":
    unittest.main()
