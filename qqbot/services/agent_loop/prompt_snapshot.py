"""Prompt 快照 — LLM 请求全链路可观测性基线（待办 #11）。

每次真实 LLM 请求（Planner 决策 / meme caption 等辅助调用）落一份完整的
JSON 快照到本地目录，回答"这一拍模型实际看到了什么、每段占多少、花了多少
token、模型回了什么"。快照是**纯观测产物**：

  - 不写 agent_events（不是事件，不进 timeline，不影响任何决策路径）；
  - 写失败静默降级（log warning），绝不让观测层拖垮决策 tick；
  - 落盘文件数有硬上限（PROMPT_SNAPSHOT_KEEP），超限删最旧。

脱敏契约（本模块的硬保证，contract test 钉死）：
  1. **图片 base64 永不落盘**——调用方只传 (hash, mime, bytes 数) 元信息；
     文本字段再兜底扫一遍 data URL 正则替换（防未来某处把 base64 拼进文本）。
  2. **密钥永不落盘**——LLM_API_KEY / TAVILY_API_KEY / ONEBOT_ACCESS_TOKEN /
     DATABASE_URL 的实际值若出现在任何存储文本中，替换为 [REDACTED:<KEY>]；
     config/model_providers.json（多服务商注册表）里的**每一把** api_key 同样替换，
     标注为 [REDACTED:MODEL_PROVIDERS:<服务商名>]。
  3. **scope 白名单**——默认只落 group / system 两类 scope 的快照；私聊
     scope（将来若实例化 private loop）默认不落盘，显式配置才开。

env 配置（qqbot/core/settings.get_env_value 单一入口）：
  PROMPT_SNAPSHOT_ENABLED  默认 false（部署侧 .env 置 true 开启采集）
  PROMPT_SNAPSHOT_DIR      默认 ./runtime_data/prompt_snapshots
  PROMPT_SNAPSHOT_KEEP     默认 200（目录内快照文件总数上限）
  PROMPT_SNAPSHOT_SCOPES   默认 "group,system"（逗号分隔的 scope 前缀白名单）

文件名 `<北京时间戳>_<kind>[_<scope>_tickN].json`，字典序即时间序——
保留清理直接按文件名排序删最旧，不依赖 mtime。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qqbot.core.llm_routing import collect_api_keys
from qqbot.core.logging import get_logger
from qqbot.core.settings import get_env_value, get_model_providers_path
from qqbot.core.time import china_now

logger = get_logger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1

DEFAULT_SNAPSHOT_DIR = "./runtime_data/prompt_snapshots"
DEFAULT_SNAPSHOT_KEEP = 200
DEFAULT_SNAPSHOT_SCOPES = "group,system"

# 值出现在快照文本里就必须被抹掉的 env 键（脱敏契约 #2）。
# LLM_API_KEY 于 2026-07-28 不再被路由读取（密钥只在 model_providers.json 里，
# 那份由 collect_api_keys 单独收集），但**保留在本列表**：老部署的 .env 里多半
# 还留着这一行，脱敏的职责是"环境里存在的密钥都别泄漏"，而不是"我们读的才管"。
_SECRET_ENV_KEYS = (
    "LLM_API_KEY",
    "TAVILY_API_KEY",
    "ONEBOT_ACCESS_TOKEN",
    "DATABASE_URL",
)
# 短于此长度的 env 值不参与替换——防 "true"/"dev" 这类平凡值把正文打成筛子。
_MIN_SECRET_LEN = 8

# data URL 形态的内联 base64（脱敏契约 #1 的文本兜底）。payload 至少 64 字符
# 才算——短串可能是正文里合法讨论的样例，不值得误伤。
_DATA_URL_RE = re.compile(
    r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]{64,}"
)

_TRUTHY = {"1", "true", "yes", "on"}


def snapshot_enabled() -> bool:
    raw = get_env_value("PROMPT_SNAPSHOT_ENABLED")
    return (raw or "").strip().lower() in _TRUTHY


def snapshot_scope_allowed(scope_key: str | None) -> bool:
    """scope 白名单判定。scope_key=None（无 scope 的辅助调用）恒放行。"""
    if scope_key is None:
        return True
    raw = get_env_value("PROMPT_SNAPSHOT_SCOPES") or DEFAULT_SNAPSHOT_SCOPES
    allowed = {p.strip().lower() for p in raw.split(",") if p.strip()}
    prefix = scope_key.split(":", 1)[0].strip().lower()
    return prefix in allowed


def should_snapshot(scope_key: str | None) -> bool:
    """调用方在构建快照对象前的总闸：开关 + scope 白名单。"""
    return snapshot_enabled() and snapshot_scope_allowed(scope_key)


@dataclass
class SnapshotAttempt:
    """单次 LLM 往返。

    Planner 侧一拍恒为一条：2026-08-21 起「一个响应绑定一次模型执行」，同拍
    静态重试链路已撤销，快照与拍一一对应（渲染格式表 §一⑦）。辅助 LLM 若自带
    解析重试仍可产生多条。
    """

    latency_ms: int | None = None
    response_text: str | None = None
    usage: dict[str, int] | None = None
    error: str | None = None


@dataclass
class PromptSnapshot:
    """一次决策 / 辅助 LLM 调用的完整请求-响应记录。

    sections 元素形如 {"name","chars","sha256"}（只存统计不存正文——正文已在
    system_prompt 整体保存，逐段再存一遍徒然翻倍）；images 元素形如
    {"hash","mime","bytes"}（永不含像素数据）。
    """

    kind: str  # "planner" / "meme_caption" / ...
    scope_key: str | None = None
    tick_seq: int | None = None
    correlation_id: str | None = None
    model: str | None = None
    system_prompt: str = ""
    user_text: str = ""
    sections: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[SnapshotAttempt] = field(default_factory=list)
    outcome: str | None = None

    def add_attempt(
        self,
        *,
        latency_ms: int | None = None,
        response_text: str | None = None,
        usage: dict[str, int] | None = None,
        error: str | None = None,
    ) -> None:
        self.attempts.append(
            SnapshotAttempt(
                latency_ms=latency_ms,
                response_text=response_text,
                usage=usage,
                error=error,
            )
        )


def section_stats(sections: list[Any]) -> list[dict[str, Any]]:
    """RenderedSection 列表 → 快照 sections 统计（name/chars/sha256）。"""
    return [
        {
            "name": sec.name,
            "chars": len(sec.text),
            "sha256": _sha256(sec.text),
        }
        for sec in sections
    ]


def write_snapshot(snapshot: PromptSnapshot) -> Path | None:
    """快照落盘 + 保留清理。任何异常吞掉打 warning——观测不影响决策。

    调用方通常已用 should_snapshot() 预判过；这里再复核一遍（enabled 与
    scope 白名单），保证"配置说不落就绝不落"不依赖调用方自觉。
    """
    try:
        if not should_snapshot(snapshot.scope_key):
            return None
        directory = _snapshot_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _snapshot_filename(snapshot)
        payload = _to_payload(snapshot)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        _enforce_retention(directory)
    except Exception as exc:
        logger.warning("[prompt_snapshot] write failed: {}", exc)
        return None
    return path


def extract_usage(raw: Any) -> dict[str, int] | None:
    """从 langchain AIMessage 提取 token 用量，归一为
    {prompt_tokens, completion_tokens, total_tokens, cache_read_tokens}。

    两个来源按优先级尝试（不同 provider / langchain 版本落点不同）：
      1. message.usage_metadata（langchain 标准化字段：input_tokens/
         output_tokens/total_tokens/input_token_details.cache_read）
      2. message.response_metadata["token_usage"]（OpenAI 原始形态：
         prompt_tokens/completion_tokens/total_tokens/
         prompt_tokens_details.cached_tokens）
    都取不到（stub / 网关不上报）返回 None——快照记 null，不编数字。
    """
    try:
        um = getattr(raw, "usage_metadata", None)
        if isinstance(um, dict) and um:
            details = um.get("input_token_details") or {}
            return _usage_dict(
                um.get("input_tokens"),
                um.get("output_tokens"),
                um.get("total_tokens"),
                details.get("cache_read") if isinstance(details, dict) else None,
            )
        rm = getattr(raw, "response_metadata", None)
        if isinstance(rm, dict):
            tu = rm.get("token_usage")
            if isinstance(tu, dict) and tu:
                details = tu.get("prompt_tokens_details") or {}
                return _usage_dict(
                    tu.get("prompt_tokens"),
                    tu.get("completion_tokens"),
                    tu.get("total_tokens"),
                    details.get("cached_tokens")
                    if isinstance(details, dict)
                    else None,
                )
    except Exception as exc:
        logger.warning("[prompt_snapshot] usage extract failed: {}", exc)
    return None


# ─────────────────────────── 内部实现 ───────────────────────────


def _usage_dict(
    prompt: Any, completion: Any, total: Any, cache_read: Any
) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in (
        ("prompt_tokens", prompt),
        ("completion_tokens", completion),
        ("total_tokens", total),
        ("cache_read_tokens", cache_read),
    ):
        if isinstance(value, int):
            out[key] = value
    return out


def _snapshot_dir() -> Path:
    raw = get_env_value("PROMPT_SNAPSHOT_DIR") or DEFAULT_SNAPSHOT_DIR
    return Path(raw)


def _keep_limit() -> int:
    raw = get_env_value("PROMPT_SNAPSHOT_KEEP")
    try:
        value = int(str(raw).strip()) if raw else DEFAULT_SNAPSHOT_KEEP
    except ValueError:
        value = DEFAULT_SNAPSHOT_KEEP
    return max(1, value)


def _snapshot_filename(snapshot: PromptSnapshot) -> str:
    ts = china_now().strftime("%Y%m%dT%H%M%S.%f")
    parts = [ts, snapshot.kind]
    if snapshot.scope_key:
        parts.append(re.sub(r"[^\w.-]", "-", snapshot.scope_key))
    if snapshot.tick_seq is not None:
        parts.append(f"tick{snapshot.tick_seq}")
    return "_".join(parts) + ".json"


def _to_payload(snapshot: PromptSnapshot) -> dict[str, Any]:
    system_prompt = _scrub_text(snapshot.system_prompt)
    user_text = _scrub_text(snapshot.user_text)
    return {
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "kind": snapshot.kind,
        "occurred_at": china_now().isoformat(timespec="seconds"),
        "scope_key": snapshot.scope_key,
        "tick_seq": snapshot.tick_seq,
        "correlation_id": snapshot.correlation_id,
        "model": snapshot.model,
        "system_prompt_chars": len(system_prompt),
        "system_prompt_sha256": _sha256(system_prompt),
        "sections": snapshot.sections,
        "user_text_chars": len(user_text),
        "images": snapshot.images,
        "outcome": snapshot.outcome,
        "attempts": [
            {
                "latency_ms": a.latency_ms,
                "response_chars": (
                    len(a.response_text) if a.response_text is not None else None
                ),
                "response_text": (
                    _scrub_text(a.response_text)
                    if a.response_text is not None
                    else None
                ),
                "usage": a.usage,
                "error": a.error,
            }
            for a in snapshot.attempts
        ],
        # 大文本放尾部，人肉翻 JSON 时元信息先入眼。
        "system_prompt": system_prompt,
        "user_text": user_text,
    }


def _scrub_text(text: str) -> str:
    """脱敏：内联 base64 data URL + 已配置密钥值（含多服务商每把 key）。"""
    scrubbed = _DATA_URL_RE.sub("data:<base64-redacted>", text)
    for key in _SECRET_ENV_KEYS:
        try:
            value = get_env_value(key)
        except Exception:
            continue
        if value and len(value) >= _MIN_SECRET_LEN and value in scrubbed:
            scrubbed = scrubbed.replace(value, f"[REDACTED:{key}]")
    # config/model_providers.json（多服务商注册表）里的 api_key 逐把替换；读文件与
    # collect_api_keys 均永不 raise，配置烂掉也不能反噬脱敏环节。
    try:
        config_path = get_model_providers_path()
        raw_config = (
            config_path.read_text(encoding="utf-8")
            if config_path.exists()
            else None
        )
    except Exception:
        raw_config = None
    if raw_config:
        for provider_name, secret in collect_api_keys(raw_config):
            if len(secret) >= _MIN_SECRET_LEN and secret in scrubbed:
                scrubbed = scrubbed.replace(
                    secret, f"[REDACTED:MODEL_PROVIDERS:{provider_name}]"
                )
    return scrubbed


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enforce_retention(directory: Path) -> None:
    files = sorted(p for p in directory.glob("*.json") if p.is_file())
    # excess ≤ 0 必须显式短路：负数直接进切片会变成 files[:-n]（删最旧！）
    excess = len(files) - _keep_limit()
    if excess <= 0:
        return
    for stale in files[:excess]:
        try:
            stale.unlink()
        except OSError as exc:
            logger.warning(
                "[prompt_snapshot] retention unlink failed: {} path={}",
                exc,
                stale,
            )
