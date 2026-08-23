"""Tool 协议与唯一的 Program API registry。

一个 Tool 描述了 agent 可调用的一项能力：
  - name                 工具名（agent.tool_called.payload.tool_name 匹配它）
  - description          面向 LLM 的客观中文能力简介
  - program_kind         现役一律 effect；漏标默认 effect
  - arguments_schema     JSON Schema (dict)，字段说明使用客观中文，纯文档用途
  - result_schema        程序可读取的只读结果 ABI
  - required_permission  (可选) 触发用户最低 tier；默认 GUEST
  - required_bot_role    (可选) 要求 bot 自己在群里的最低角色 "admin"/"owner"；默认
                         None（不限）。旧 require_bot_admin=True 等价 "admin"
  - allowed_scopes       (可选) 限定可见/可调的 scope；默认 None（不限）
  - execute(arguments, **context)  子类实现工具逻辑，**返回** ToolOutcome（成功或
                         失败），永不 raise；BaseTool.run() 把它归一成统一输出

权限/判定语义（**全部在工具内**，详见 BaseTool 与 core/permissions.py）：

- required_permission / required_bot_role / allowed_scopes 是**纯元数据**，不影响
  Program API 可见性（scope 隔离除外）—— LLM 能看见自己 scope 内的全部函数，
  根据接口说明与权限元数据生成调用。
- execute() 第一行 ``if fail := await self.enforce_access(context): return fail``——
  enforce_access = enforce_scope（越 scope → tool_unavailable_in_scope）+
  enforce_permission（发起人 tier，**实时**查其当前群角色）+ enforce_bot_admin（bot
  自身角色，同样**实时**查 napcat、查不到才回退注入的快照）。AgentLoop **不再做任何
  scope/tier/role 判定或闸门**。
- ToolRegistry.usage_docs() 把签名、双向 schema 与权限元数据渲染成 Program API 参考。

执行约定（任务与决策契约 §5.1, §6, §7.2）—— **全程无 raise 控制流**：
  - 子类实现 execute()：成功 ``return ToolOutcome.success(result)``；可预期失败把
    helper 返回的 ``ToolOutcome.failure(error_kind, msg, **extra)`` 直接 return 上来
    （enforce_access / coerce_int / require_group_scope / get_bot / call_action 都
    **返回**失败，不 raise）。
  - BaseTool.run() 是统一出口（调用方只见它、永不 raise）：归一 execute 的返回；只
    兜底**预料外**的第三方异常（httpx / sqlalchemy / napcat 适配器 raise 的）→
    internal_tool_error。
  - 执行层只搬运 ToolOutcome → 领域事件 + agent.tool_result / agent.tool_failed。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from qqbot.core.logging import get_logger
from qqbot.core.permissions import (
    PermissionTier,
    resolve_user_tier_from_event,
    tier_from_group_role,
)

logger = get_logger(__name__)

# _effective_bot_role 的缓存哨兵：区分"没缓存"与"缓存值就是 None"，
# 让 bot 角色一次 execute() 内只实时查一次。
_UNSET = object()

ProgramKind = Literal["query", "effect"]
ToolFactory = Callable[[], "Tool"]


@dataclass(frozen=True)
class ProgramFunctionSpec:
    name: str
    program_kind: ProgramKind
    signature: str
    arguments_schema: dict
    result_schema: dict
    allowed_scopes: tuple[str, ...] | None
    max_call_sites: int
    required_permission: PermissionTier
    required_bot_role: str | None
    description: str
    usage_prompt: str
    tool_factory: ToolFactory = field(repr=False, compare=False)


@dataclass(frozen=True)
class ToolGeneratedEvent:
    """工具返回、由调度层随 terminal 一并落库的 agent 领域事件意图。

    工具只描述 ``event_type`` / ``payload``，不自行决定 correlation、causation
    或事务边界；执行层统一把 causation 指向本次 ``agent.tool_called``。当前
    ``task`` inline 工具用它生成 ``agent.task_*``，以后其它工具也可复用。
    """

    event_type: str
    payload: dict
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.event_type.startswith("agent."):
            raise ValueError("ToolGeneratedEvent.event_type must start with 'agent.'")
        if not isinstance(self.payload, dict):
            raise TypeError("ToolGeneratedEvent.payload must be a dict")


@dataclass(frozen=True)
class ToolOutcome:
    """工具调用的结构化输出 —— 取代旧的"成功 return dict / 失败 raise 字符串"。

    工具直接产出 outcome（成功 → ``result``；失败 → ``error_kind`` /
    ``error_message`` / ``extra``）。执行层只把它机械搬运成
    ``agent.tool_result`` / ``agent.tool_failed``，**不再 introspect 异常类型、不猜
    error_kind**（契约 §6/§7.2）；Projection 据这两类事件渲染 ``<tool>`` 行
    （两态：``完成`` + 结果行 / ``失败`` + 原因行）。

    ``error_kind`` 收敛成固定语义集（见 planner.md §输入信封格式规范 的 <error>、
    契约 §7.2）：
      ``tool_unavailable_in_scope`` / ``invalid_arguments`` /
      ``permission_denied_user_tier`` / ``permission_denied_bot_role`` /
      ``no_bot_available`` / ``upstream_action_failed`` / ``internal_tool_error``。
    ``extra`` 是结构化附加字段（required_tier / actual_bot_role / retcode /
    wording / action ...），随 tool_failed.payload 落表供审计与渲染。
    """

    ok: bool
    result: Any = None
    error_kind: str | None = None
    error_message: str | None = None
    extra: dict = field(default_factory=dict)
    emitted_events: tuple[ToolGeneratedEvent, ...] = field(default_factory=tuple)

    @classmethod
    def success(
        cls,
        result: Any = None,
        *,
        emitted_events: Iterable[ToolGeneratedEvent] = (),
        **fields: Any,
    ) -> "ToolOutcome":
        """成功 outcome。``result`` 传 dict 或用 kwargs 拼字段，二者可合并。"""
        if fields:
            merged = dict(result) if isinstance(result, dict) else {}
            merged.update(fields)
            return cls(
                ok=True,
                result=merged,
                emitted_events=tuple(emitted_events),
            )
        return cls(
            ok=True,
            result={} if result is None else result,
            emitted_events=tuple(emitted_events),
        )

    @classmethod
    def failure(
        cls, error_kind: str, error_message: str, **extra: Any
    ) -> "ToolOutcome":
        """失败 outcome。``error_kind`` 用固定语义集，``error_message`` 回给 LLM。"""
        return cls(
            ok=False,
            error_kind=error_kind,
            error_message=str(error_message)[:1000],
            extra=dict(extra),
        )


def coerce_tool_outcome(raw: Any) -> ToolOutcome:
    """把轻量工具/stub 的返回值归一成 ``ToolOutcome``。"""
    if isinstance(raw, ToolOutcome):
        return raw
    if isinstance(raw, dict):
        return ToolOutcome.success(raw)
    if raw is None:
        return ToolOutcome.success({})
    return ToolOutcome.success({"value": raw})


# 全链路**无 raise 控制流**：工具（含 enforce_* / coerce_int / require_group_scope /
# get_bot / call_action 等 helper）一律**返回** ToolOutcome / (value, ToolOutcome|None)
# 表达失败，不再 raise 任何结构化异常。故不存在 ToolError / ToolPermissionError ——
# 失败即 ``ToolOutcome.failure(error_kind, msg, **extra)``。真正预料外的第三方异常
# （httpx / sqlalchemy / napcat 适配器）由 ``BaseTool.run`` 的兜底 except 收敛成
# ``internal_tool_error``，也不越出工具边界。


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    arguments_schema: dict

    # `usage_prompt` 不是必填——老工具或单测里的 stub 可以省略。
    # ToolRegistry.usage_docs() 用 getattr 兜底，缺失等同于空串。
    # 命名约定：把客观中文接口文档（能力、参数、权限与作用域、返回和失败）放在
    # sibling .md，由 prompts.load_sibling_md(__file__, "...") 加载注入；不在
    # 工具文档中加入人格、情境偏好或调用倾向。

    # `required_permission` / `required_bot_role` 都不是必填——老工具或单测
    # stub 可以省略。Program API 参考与工具内 enforce_* 用 getattr 兜底，缺失等同于
    # GUEST / None。

    # `allowed_scopes` 同样可选：None（默认）= 不限 scope，任何 AgentLoop 都
    # 可见可调；非空序列 = 仅列出的 scope（"system"/"group"/"private"）可见，
    # registry.specs(scope) 在别的 scope 里隐藏它、工具内 enforce_scope 拒绝硬调（返回
    # tool_unavailable_in_scope）。这是契约 §2.2「scope 限定工具（如群管理类
    # 仅 GroupAgentLoop 可见）」的落地点。

    async def run(self, arguments: dict, **context: Any) -> Any:
        """运行工具，**返回** ToolOutcome（成功或失败）。

        BaseTool 子类改实现 `execute()`、继承 `BaseTool.run()`（统一出口、永不
        raise）。`arguments` 是 LLM 给的、匹配 `arguments_schema` 的 dict；
        `context` 是系统注入的同一套 kwargs（scope_key / correlation_id /
        session_factory / triggered_by_event_id / bot_role ...），每个工具收到
        的完全相同，故无需任何 __init__ 接线。详见 BaseTool。
        """
        ...


class BaseTool:
    """工具基类：实现 run()（统一出口、永不 raise）+ enforce_access（scope/tier/
    bot 角色判定），把"可选属性"默认值固化在一处，realize "系统只认输入输出"。

    继承它后，工具只需实现 `execute()`（**返回** ToolOutcome）并覆盖与默认不同的
    字段（几乎总是 name / description / arguments_schema / usage_prompt）；权限
    相关默认 GUEST / 不限 bot 角色。需要敏感权限的工具显式覆盖 `required_permission`
    / `required_bot_role`（如踢人工具 = ADMIN + "admin"）。

    系统级依赖（session_factory 写/查 agent_events、触发身份 triggered_by_event_id
    / bot_role 等）一律由程序执行器在 run() context 里统一注入，
    不走各工具的 __init__ —— build_default_registry 无参构造所有工具，系统也不必
    按名字特判。

    注：`get_tool_required_permission` / `get_tool_require_bot_admin` /
    `get_tool_required_bot_role` 仍保留为防御层 —— 测试 stub 或第三方工具不继承
    BaseTool 时也能拿到默认值。
    """

    usage_prompt: str = ""
    program_kind: ProgramKind = "effect"
    result_schema: dict = {"type": "object", "properties": {}}
    max_call_sites: int = 2
    required_permission: PermissionTier = PermissionTier.GUEST
    require_bot_admin: bool = False
    # None = 不限 scope（默认，所有 AgentLoop 可见可调）；非空 tuple 限定
    # 仅这些 scope 可见（如 ban / respond_to_group_join_request = ("group",)，
    # 契约 §2.2）。
    allowed_scopes: tuple[str, ...] | None = None
    # bot 自身在群里的最低角色要求：None=不限；"admin"=须管理员或群主；
    # "owner"=须群主。由 enforce_bot_admin 在工具内判——bot 角色经
    # _effective_bot_role **实时**查 napcat（查不到才回退 context.bot_role 快照）。
    required_bot_role: str | None = None

    async def run(self, arguments: dict, **context: Any) -> "ToolOutcome":
        """工具统一出口：**无论成功还是失败都返回 ToolOutcome，永不 raise**。

        「输入参数 → 工具直接给统一结果」的落地点。子类实现 ``execute()``，全链路
        **无 raise 控制流**：成功 ``return ToolOutcome.success(...)``；失败把 helper
        返回的失败 outcome 直接 return 上来（``enforce_access`` / ``coerce_int`` /
        ``require_group_scope`` / ``get_bot`` / ``call_action`` 都**返回**失败、不
        raise）。本方法只做归一 + 兜底，异常都不越出工具边界：
          - ``ToolOutcome`` → 原样；``dict`` / 其它标量 → success（兼容轻量返回）；
          - ``execute`` 里若冒出**预料外**第三方异常（httpx / sqlalchemy / napcat
            适配器 raise 的）→ 收敛成 ``internal_tool_error``（并记 exception 日志）。
        调用方（ProgramExecutor / 测试）拿到的永远是一个 ToolOutcome，
        不需要 try/except，也不需要认得任何异常类型。
        """
        try:
            result = await self.execute(arguments, **context)
        except Exception as exc:  # noqa: BLE001 —— 仅兜底预料外的第三方异常
            logger.exception(
                "[tool {}] unexpected error: {}",
                getattr(self, "name", "?"),
                exc,
            )
            return ToolOutcome.failure(
                "internal_tool_error", f"{type(exc).__name__}: {exc}"
            )
        return coerce_tool_outcome(result)

    async def execute(self, arguments: dict, **context: Any) -> Any:
        """子类实现：跑工具逻辑，**返回** ToolOutcome（成功或失败），不 raise。
        典型骨架（scope/tier/bot 角色/参数校验逐个 return failure）::

            if fail := await self.enforce_access(context):
                return fail
            group_id, fail = require_group_scope(context, self.name)
            if fail:
                return fail
            ...
            return ToolOutcome.success(...)
        """
        raise NotImplementedError

    async def enforce_access(self, context: dict) -> "ToolOutcome | None":
        """工具访问总闸：scope + 发起人 tier + bot 自身角色。任一不过**返回**对应
        失败 ``ToolOutcome``；全过返回 None。工具 execute() 第一行::

            if fail := await self.enforce_access(context):
                return fail

        ``enforce_permission``（发起人 tier）与 ``enforce_bot_admin``（bot 自身角色）
        都可能现场向 napcat **实时**查群角色，故都是 async；``enforce_scope`` 是纯比较。
        ``or`` 短路：前者失败即返回。
        """
        return (
            self.enforce_scope(context)
            or await self.enforce_permission(context)
            or await self.enforce_bot_admin(context)
        )

    async def enforce_permission(self, context: dict) -> "ToolOutcome | None":
        """发起人 tier 判定（**解析也在工具内**）：tier 不足**返回**
        ``permission_denied_user_tier`` 失败；否则 None。

        tier 来源两条：① context 预置 ``triggered_by_user_tier``（测试/预解析）；
        ② 否则据 ``triggered_by_event_id`` 拿到发起人 user_id，再**实时**向 napcat
        查其在当前群的**当前**角色（``get_group_member_info``, no_cache）→ tier
        （生产路径，loop 不再代解析）。GUEST 工具在解析前就放行（省一次 IO）。
        """
        required = get_tool_required_permission(self)
        if required <= PermissionTier.GUEST:
            return None
        user_tier = await self._resolve_triggering_tier(context)
        if user_tier < required:
            return ToolOutcome.failure(
                "permission_denied_user_tier",
                f"{getattr(self, 'name', '?')} requires {required.name}; "
                f"triggering user tier is {user_tier.name}",
                required_tier=required.name,
                actual_tier=user_tier.name,
            )
        return None

    async def _resolve_triggering_tier(self, context: dict) -> PermissionTier:
        """解析触发用户 tier（**实时**，非事件快照）：
          ① context 预置 ``triggered_by_user_tier``（测试/预解析）→ 直接用，免 IO；
          ② 读触发事件拿 user_id + SUPERUSERS 判定——命中 SUPERUSERS 即
             SYSTEM_ADMIN（cross-cutting，越过群角色，不查群）；
          ③ 否则**实时**向 napcat 查该 user 在当前群的**当前**角色
             （``get_group_member_info``, no_cache=True）→ ``tier_from_group_role``。

        无 ``session_factory`` / 无 user / 无群 / 查不到 / 无 bot → GUEST（保守拒绝）。
        注：``resolve_user_tier_from_event`` 返回的 snap_tier 只在 SUPERUSERS 命中时
        为 SYSTEM_ADMIN（群角色最高只到 OWNER），据此识别 SU；其快照的群角色被丢弃、
        改用 ③ 的实时值。
        """
        raw = context.get("triggered_by_user_tier")
        if isinstance(raw, str) and raw:
            try:
                return PermissionTier[raw]
            except KeyError:
                return PermissionTier.GUEST
        session_factory = context.get("session_factory")
        if session_factory is None:
            return PermissionTier.GUEST
        snap_tier, user_id = await resolve_user_tier_from_event(
            context.get("triggered_by_event_id"),
            session_factory=session_factory,
            superusers=context.get("superusers"),
        )
        if snap_tier == PermissionTier.SYSTEM_ADMIN:
            return snap_tier  # SUPERUSERS：越过群角色，无需实时查
        if not user_id:
            return PermissionTier.GUEST
        group_id = _group_id_from_scope_key(context.get("scope_key"))
        if group_id is None:
            return PermissionTier.GUEST
        role = await self._fetch_live_member_role(group_id, user_id)
        return tier_from_group_role(role)

    async def _fetch_live_member_role(self, group_id: int, user_id: str) -> str | None:
        """实时查该 user 在群里的**当前**角色（owner/admin/member）。

        经 bot_registry 取 Bot，再通过 OneBotGateway 调
        ``get_group_member_info(no_cache=True)`` 强制取最新值（不吃缓存）。无 bot /
        napcat 报错（如该用户已退群）/ 无 role 字段 → None（上层据此保守判
        GUEST）。延迟 import 避免潜在循环。
        """
        from qqbot.services.agent_loop import bot_registry
        from qqbot.services.agent_loop.program_api.onebot_gateway import (
            OneBotGateway,
            RawOneBotResponse,
        )

        bot = bot_registry.get_any()
        if bot is None:
            return None
        try:
            response = await OneBotGateway(lambda: bot).query(
                "get_group_member_info",
                group_id=int(group_id), user_id=int(user_id), no_cache=True
            )
        except Exception:  # noqa: BLE001 —— 查不到/napcat 错都保守当无角色
            return None
        if not isinstance(response, RawOneBotResponse) or not response.ok:
            return None
        info = response.data
        return info.get("role") if isinstance(info, dict) else None

    async def enforce_bot_admin(self, context: dict) -> "ToolOutcome | None":
        """bot 自身群角色判定：角色不够**返回** ``permission_denied_bot_role``
        失败；否则 None。``required_bot_role``=None 放行；"admin" 要求 admin/owner；
        "owner" 要求 owner。

        bot 角色经 ``_effective_bot_role`` **实时**向 napcat 查其当前群角色（与发起人
        tier 同源，no_cache），查不到才回退执行层透传的 ``context.bot_role``
        快照——这样 bot 刚被升/降权也能立刻反映，不再受投影层 sweep 时延影响。未知
        （实时+快照都拿不到）保守拒绝。
        """
        need = get_tool_required_bot_role(self)
        if need is None:
            return None
        bot_role = await self._effective_bot_role(context)
        ok = bot_role == "owner" or (need == "admin" and bot_role == "admin")
        if not ok:
            return ToolOutcome.failure(
                "permission_denied_bot_role",
                f"{getattr(self, 'name', '?')} requires the bot itself to be "
                f"group {need}; current bot_role={bot_role or 'unknown'}",
                required_bot_role=need,
                actual_bot_role=bot_role or None,
            )
        return None

    async def _effective_bot_role(self, context: dict) -> str | None:
        """bot 自身在当前群的**当前**角色（owner/admin/member）——工具判 bot 权限时
        统一走它。**实时**向 napcat 查（与发起人 tier 解析同源），查不到才回退
        ``context.bot_role`` 快照；结果缓存进 context，一次 ``execute()`` 内
        ``enforce_bot_admin`` 与细粒度层级判定（kick/ban/recall...）复用同一个值、
        不重复打 napcat。None = 实时与快照都拿不到（上层保守按无角色处理）。
        """
        cached = context.get("_effective_bot_role", _UNSET)
        if cached is not _UNSET:
            return cached
        resolved = await self._resolve_live_bot_role(context)
        context["_effective_bot_role"] = resolved
        return resolved

    async def _resolve_live_bot_role(self, context: dict) -> str | None:
        """实时解析 bot 自身群角色：取本 bot 的 self_id + scope 的 group_id，调
        ``_fetch_live_member_role``（``get_group_member_info`` no_cache）。查不到 / 无
        bot / 非群 scope → 回退注入的 ``context.bot_role`` 快照（好过凭空拒绝）。
        """
        from qqbot.services.agent_loop import bot_registry

        snap = context.get("bot_role")
        snap = snap.strip().lower() if isinstance(snap, str) and snap.strip() else None
        bot = bot_registry.get_any()
        self_id = getattr(bot, "self_id", None) if bot is not None else None
        group_id = _group_id_from_scope_key(context.get("scope_key"))
        if self_id is None or group_id is None:
            return snap
        role = await self._fetch_live_member_role(group_id, str(self_id))
        if isinstance(role, str) and role.strip():
            return role.strip().lower()
        return snap

    def enforce_scope(self, context: dict) -> "ToolOutcome | None":
        """scope 闸门：``allowed_scopes`` 限定的工具在别的 scope 被（硬）调用时
        **返回** ``tool_unavailable_in_scope`` 失败；否则 None。

        AgentLoop 不做 scope 判定（契约 §2.2 下放工具）——只按 Program API scope 隐藏
        LLM 看不到的工具；真要硬调由这里在工具内拦下。``allowed_scopes=None`` 不限。
        """
        allowed = get_tool_allowed_scopes(self)
        if allowed is None:
            return None
        scope_key = context.get("scope_key")
        current = (
            scope_key.split(":", 1)[0]
            if isinstance(scope_key, str) and scope_key
            else None
        )
        if current not in allowed:
            return ToolOutcome.failure(
                "tool_unavailable_in_scope",
                f"{getattr(self, 'name', '?')} is only available in scope(s) "
                f"{list(allowed)}; current scope={current!r}",
                allowed_scopes=list(allowed),
                actual_scope=current,
            )
        return None


def _group_id_from_scope_key(scope_key: Any) -> int | None:
    """从 ``group:<id>`` 形态的 scope_key 取 group_id；非群 scope / 非法 → None。
    只在实时解析发起人群角色时用（enforce_permission），避免额外 import event_writer。
    """
    if not isinstance(scope_key, str) or not scope_key.startswith("group:"):
        return None
    try:
        return int(scope_key.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def get_tool_program_kind(tool: Any) -> ProgramKind:
    """读取程序函数分类；现役一律 effect，漏标也按 effect。"""
    raw = getattr(tool, "program_kind", "effect")
    if raw in ("query", "effect"):
        return "effect"
    raise ValueError(f"tool.program_kind must be 'query' or 'effect'; got {raw!r}")


# ── 结果信封（2026-08-15，失败即返回值）─────────────────────────────────
#
# 此前任一调用失败会中止整段程序，程序里没有失败分支可写；模型于是只敢一拍
# 做一件事——一段不能处理失败的程序等价于一次 JSON action，程序形态白给了。
# 现在每个函数的返回值都多两个**必然存在**的字段：
#
#   ok     bool          调用是否成功
#   error  object|null   失败时为 {kind, message, status}，成功时为 null
#
# 失败时 result_schema 原有字段全部为 None，程序继续往下跑。注入点只有这里，
# 保证三处看到的是同一份 schema：program_ast 的静态字段校验、usage_docs 给
# 模型的返回 schema、program_runtime 的 wrap_program_value。工具自己**不**声明
# 这两个字段——ToolOutcome 已经承载了成功/失败，重复声明只会两处漂移。
OUTCOME_ERROR_SCHEMA: dict = {
    "type": ["object", "null"],
    "properties": {
        "kind": {
            "type": "string",
            "description": (
                "稳定错误语义：invalid_arguments / permission_denied_user_tier / "
                "permission_denied_bot_role / tool_unavailable_in_scope / "
                "no_bot_available / upstream_action_failed / internal_tool_error。"
            ),
        },
        "message": {"type": "string", "description": "失败原因的可读说明。"},
        "status": {
            "type": ["string", "null"],
            "description": (
                "部分工具的补充状态，如 send_messages 的 "
                "partial / failed / uncertain；其余为 null。"
            ),
        },
    },
    "required": ["kind", "message", "status"],
    "additionalProperties": False,
}

OUTCOME_FIELDS: tuple[str, ...] = ("ok", "error")


def with_outcome_envelope(schema: dict) -> dict:
    """给一份 result_schema 加上 ``ok`` / ``error``（就地复制，不改原对象）。

    顶层是 ``oneOf`` 时逐支注入——现役 18 个工具都是普通 object，这条只是不
    留下一个会静默漏掉字段的形状分支。
    """
    if not isinstance(schema, dict):
        return schema
    if schema.get("oneOf") and not schema.get("properties"):
        return {
            **schema,
            "oneOf": [with_outcome_envelope(branch) for branch in schema["oneOf"]],
        }
    properties = dict(schema.get("properties") or {})
    properties["ok"] = {
        "type": "boolean",
        "description": "调用是否成功。为 false 时下列业务字段全部为 null。",
    }
    properties["error"] = OUTCOME_ERROR_SCHEMA
    required = list(schema.get("required") or [])
    required = [name for name in required if name not in OUTCOME_FIELDS]
    return {
        **schema,
        "properties": properties,
        # ok/error 必然存在；原有 required 字段在失败时是 null，故降为非必填，
        # 否则 schema 会声称一个失败返回里仍有它们。
        "required": [*OUTCOME_FIELDS],
        "x-payload-required": required,
    }


def get_tool_result_schema(tool: Any) -> dict:
    schema = getattr(tool, "result_schema", None)
    if not isinstance(schema, dict):
        raise ValueError(
            f"tool {getattr(tool, 'name', '?')!r} must declare result_schema"
        )
    for field_name in OUTCOME_FIELDS:
        if field_name in (schema.get("properties") or {}):
            raise ValueError(
                f"tool {getattr(tool, 'name', '?')!r} must not declare "
                f"result field {field_name!r}; it is supplied by the outcome "
                "envelope (see with_outcome_envelope)"
            )
    return with_outcome_envelope(copy.deepcopy(schema))


def get_tool_max_call_sites(tool: Any) -> int:
    raw = getattr(tool, "max_call_sites", 2)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(f"tool.max_call_sites must be a positive integer; got {raw!r}")
    return raw


def get_tool_required_permission(tool: Any) -> PermissionTier:
    """统一读 tool.required_permission 的兜底入口。

    缺失 → GUEST；字符串值（"ADMIN" / "admin"）按 enum name 解析；其它非法
    值 fall back 到 GUEST。集中实现避免每个 caller 都写重复 isinstance。
    """
    raw = getattr(tool, "required_permission", None)
    if raw is None:
        return PermissionTier.GUEST
    if isinstance(raw, PermissionTier):
        return raw
    if isinstance(raw, str):
        try:
            return PermissionTier[raw.strip().upper()]
        except KeyError:
            return PermissionTier.GUEST
    if isinstance(raw, int):
        try:
            return PermissionTier(raw)
        except ValueError:
            return PermissionTier.GUEST
    return PermissionTier.GUEST


def get_tool_require_bot_admin(tool: Any) -> bool:
    """统一读 tool.require_bot_admin 的兜底入口。缺失 → False。"""
    return bool(getattr(tool, "require_bot_admin", False))


def get_tool_required_bot_role(tool: Any) -> str | None:
    """统一读 tool 要求的 bot 最低群角色。优先显式 `required_bot_role`
    （"admin"/"owner"）；回退到旧的 `require_bot_admin=True` → "admin"；
    都没有 → None（不限）。"""
    raw = getattr(tool, "required_bot_role", None)
    if isinstance(raw, str) and raw.strip().lower() in ("admin", "owner"):
        return raw.strip().lower()
    if get_tool_require_bot_admin(tool):
        return "admin"
    return None


def get_tool_allowed_scopes(tool: Any) -> tuple[str, ...] | None:
    """统一读 tool.allowed_scopes 的兜底入口。

    返回 None = 不限 scope（缺失、显式 None、或解析失败都按"不限"处理，
    保守地保证老工具/stub 全 scope 可见）。返回非空 tuple = 仅这些 scope
    可见可调（"system"/"group"/"private"）。字符串单值自动包成单元素 tuple。
    """
    raw = getattr(tool, "allowed_scopes", None)
    if raw is None:
        return None
    if isinstance(raw, str):
        return (raw,)
    try:
        scopes = tuple(str(s) for s in raw)
    except TypeError:
        return None
    return scopes or None


class ToolRegistry:
    """The single name → Program function registry.

    A new Tool instance is created for every invocation. Production code
    registers no-argument classes/factories; instance registration remains a
    compatibility convenience for test doubles and uses ``copy.copy``.
    """

    def __init__(self) -> None:
        self._specs: dict[str, ProgramFunctionSpec] = {}

    def register(self, candidate: Tool | ToolFactory | type[Tool]) -> None:
        factory, probe = _coerce_tool_factory(candidate)
        name = getattr(probe, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("tool.name must be a non-empty string")
        if not name.isidentifier() or name.startswith("_"):
            raise ValueError(f"tool.name must be a public Python identifier: {name!r}")
        if name in self._specs:
            raise ValueError(f"tool already registered: {name}")
        arguments_schema = getattr(probe, "arguments_schema", None)
        if not isinstance(arguments_schema, dict):
            raise ValueError(f"tool {name!r} must declare arguments_schema")
        program_kind = get_tool_program_kind(probe)
        spec = ProgramFunctionSpec(
            name=name,
            program_kind=program_kind,
            signature=_program_signature(
                name,
                arguments_schema,
                program_kind=program_kind,
            ),
            arguments_schema=copy.deepcopy(arguments_schema),
            result_schema=get_tool_result_schema(probe),
            allowed_scopes=get_tool_allowed_scopes(probe),
            max_call_sites=get_tool_max_call_sites(probe),
            required_permission=get_tool_required_permission(probe),
            required_bot_role=get_tool_required_bot_role(probe),
            description=str(getattr(probe, "description", "") or ""),
            usage_prompt=str(getattr(probe, "usage_prompt", "") or "").strip(),
            tool_factory=factory,
        )
        self._specs[name] = spec

    def get(self, name: str) -> Tool | None:
        spec = self._specs.get(name)
        return spec.tool_factory() if spec is not None else None

    def spec(self, name: str) -> ProgramFunctionSpec | None:
        return self._specs.get(name)

    def specs(self, scope: str | None = None) -> list[ProgramFunctionSpec]:
        return [
            self._specs[name]
            for name in sorted(self._specs)
            if _spec_visible_in_scope(self._specs[name], scope)
        ]

    def names(self, scope: str | None = None) -> list[str]:
        return [spec.name for spec in self.specs(scope)]

    def usage_docs(self, scope: str | None = None) -> str:
        sections = [
            "程序函数只能用具名参数调用。每次调用都会留下一条终态工具记录；"
            "程序结束后，除这些调用记录外，只有 return 的 JSON 会作为 "
            "<program_result> 的结果行留到下一拍。\n\n"
            "下面每个函数的返回 schema 里，`ok` 与 `error` 是所有函数共有的："
            "`ok` 为 true 时业务字段有效、`error` 为 null；`ok` 为 false 时"
            "业务字段全是 null，`error` 给出 `kind` / `message` / `status`。"
            "调用失败不会中止程序——**但要用业务字段之前必须先判 `ok`**，"
            "没判就读会当场中止整段程序。"
        ]
        for spec in self.specs(scope):
            meta = ["effect"]
            if spec.required_permission > PermissionTier.GUEST:
                meta.append(f"用户权限>={spec.required_permission.name}")
            if spec.required_bot_role:
                meta.append(f"本账号群角色>={spec.required_bot_role}")
            if spec.allowed_scopes:
                meta.append("scope=" + ",".join(spec.allowed_scopes))
            meta.append(f"静态调用点<={spec.max_call_sites}")
            # 敏感工具（required_permission > GUEST）的这句话是硬要求，不是
            # 提示：2026-08-21 任务坍缩后没有了"从任务起因回填发起人"的回退，
            # 不传就是 GUEST，就会被拒。
            if spec.required_permission > PermissionTier.GUEST:
                reserved = (
                    "额外保留具名参数：triggered_by_event_id=None"
                    "（本工具需要发起人权限，**必须显式传**触发它的那条事件的"
                    " ev: 值；省略即按 GUEST 判定，多半会被拒）。"
                )
            else:
                reserved = "额外保留具名参数：triggered_by_event_id=None。"
            block = [
                f"## 程序函数：{spec.name}",
                "",
                f"```python\n{spec.signature}\n```",
                "",
                f"类型与权限：{'；'.join(meta)}。{reserved}",
                "",
                spec.description,
                "",
                "参数 schema：",
                "```json",
                _schema_json(spec.arguments_schema),
                "```",
                "",
                "返回 schema：",
                "```json",
                _schema_json(spec.result_schema),
                "```",
            ]
            if spec.usage_prompt:
                block.extend(["", spec.usage_prompt])
            sections.append("\n".join(block).strip())
        return "\n\n".join(sections)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs


def _coerce_tool_factory(
    candidate: Tool | ToolFactory | type[Tool],
) -> tuple[ToolFactory, Tool]:
    if isinstance(candidate, type):
        return candidate, candidate()
    if callable(candidate) and not isinstance(getattr(candidate, "name", None), str):
        probe = candidate()
        if probe is None:
            raise ValueError("tool factory returned None")
        return candidate, probe
    probe = candidate

    def clone() -> Tool:
        return copy.copy(probe)

    return clone, probe  # type: ignore[return-value]


def _spec_visible_in_scope(spec: ProgramFunctionSpec, scope: str | None) -> bool:
    return scope is None or spec.allowed_scopes is None or scope in spec.allowed_scopes


def _schema_json(schema: dict) -> str:
    return json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _program_signature(
    name: str,
    schema: dict,
    *,
    program_kind: ProgramKind,
) -> str:
    properties = _merged_schema_properties(schema)
    required = _required_schema_fields(schema)
    parameters: list[str] = []
    for field_name, field_schema in properties.items():
        if field_name in required and "default" not in field_schema:
            parameters.append(field_name)
        else:
            parameters.append(
                f"{field_name}={_python_literal(field_schema.get('default'))}"
            )
    if "triggered_by_event_id" not in properties:
        parameters.append("triggered_by_event_id=None")
    if not parameters:
        return f"{name}()"
    return f"{name}(*, {', '.join(parameters)})"


def _merged_schema_properties(schema: dict) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for key, value in (schema.get("properties") or {}).items():
        if isinstance(key, str) and isinstance(value, dict):
            merged[key] = value
    for branch in schema.get("oneOf") or []:
        if not isinstance(branch, dict):
            continue
        for key, value in (branch.get("properties") or {}).items():
            if isinstance(key, str) and isinstance(value, dict):
                merged.setdefault(key, value)
    return merged


def _required_schema_fields(schema: dict) -> set[str]:
    required = {str(item) for item in schema.get("required") or []}
    branches = [
        branch for branch in schema.get("oneOf") or [] if isinstance(branch, dict)
    ]
    if branches:
        branch_required = [
            {str(item) for item in branch.get("required") or []} for branch in branches
        ]
        if branch_required:
            required |= set.intersection(*branch_required)
    return required


def _python_literal(value: Any) -> str:
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    return repr(value)


__all__ = [
    "BaseTool",
    "ProgramFunctionSpec",
    "ProgramKind",
    "Tool",
    "ToolGeneratedEvent",
    "OUTCOME_ERROR_SCHEMA",
    "OUTCOME_FIELDS",
    "ToolOutcome",
    "ToolRegistry",
    "coerce_tool_outcome",
    "get_tool_allowed_scopes",
    "get_tool_max_call_sites",
    "get_tool_program_kind",
    "get_tool_required_bot_role",
    "get_tool_required_permission",
    "get_tool_result_schema",
    "with_outcome_envelope",
]
