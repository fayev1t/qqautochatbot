"""In-process executor for statically validated Planner programs."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn

from qqbot.core.logging import get_logger
from qqbot.services.agent_loop.program_ast import (
    MAX_CONTAINER_ELEMENTS,
    MAX_STRING_LENGTH,
    SAFE_BUILTINS,
    PreflightResult,
    ProgramCallSite,
    ProgramErrorInfo,
    build_executable_tree,
)
from qqbot.services.agent_loop.program_events import (
    EffectCallHandle,
    begin_effect_call,
    finish_effect_call,
    uncertain_outcome,
)
from qqbot.services.agent_loop.tool_registry import (
    OUTCOME_ERROR_SCHEMA,
    OUTCOME_FIELDS,
    ProgramFunctionSpec,
    ToolOutcome,
    ToolRegistry,
    coerce_tool_outcome,
)

if TYPE_CHECKING:
    from qqbot.services.agent_loop.decision import DecisionContext

logger = get_logger(__name__)

MAX_PROGRAM_CALLS = 8
MAX_ITERATIONS = 1_000
MAX_STATEMENTS = 5_000

# 2026-08-15「失败即返回值」的例外：这两种不是"这次调用没成"，而是**程序正在
# 被拆掉**——墙钟到点或进程关停。后续语句既没有时间预算，副作用状态也存疑，
# 继续跑等于让一个已超时的程序接着发消息。它们保持中止语义，由启动收口写
# interrupted/uncertain。注意与 send_messages 的 status="uncertain" 区分：那是
# 一次投递结果不确定（工具级事实，是返回值），不是程序被拆。
FATAL_ERROR_KINDS = frozenset({"program_timeout", "interrupted"})
CALL_TIMEOUT_SECONDS = 20.0
PROGRAM_TIMEOUT_SECONDS = 40.0
MAX_RETURN_BYTES = 6_144
MAX_NUMBER_BITS = 16_384
# 结果侧(上游返回值)与程序侧(程序自造值)是两档上限:MAX_STRING_LENGTH /
# MAX_CONTAINER_ELEMENTS 约束模型在程序里能"造"多大的值;工具结果的体积由
# 各工具自行封顶(webfetch 正文硬上限 20000 字、websearch 单条 8000),wrap
# 阶段只做防线级检查——否则合法结果会在被读到任何字段之前废掉整段程序,
# 而查询费用已经花掉。
MAX_RESULT_STRING_CHARS = 20_000
MAX_RESULT_CONTAINER_ELEMENTS = 5_000
# 值的嵌套深度上限,wrap/unwrap 共用:防止深嵌套返回值在递归转换时以
# RecursionError 逃出执行器(那会让该拍没有 program terminal)。
MAX_VALUE_DEPTH = 16


@dataclass(frozen=True)
class ProgramCallTrace:
    name: str
    occurrence: int
    program_kind: str
    call_site: str
    arguments_hash: str
    status: str
    duration_ms: int
    result_bytes: int | None = None
    error_kind: str | None = None


@dataclass
class ProgramTrace:
    decision_id: str
    program_sha256: str
    scope_key: str
    duration_ms: int = 0
    statement_count: int = 0
    iteration_count: int = 0
    query_calls: list[str] = field(default_factory=list)
    effect_call_ids: list[str] = field(default_factory=list)
    calls: list[ProgramCallTrace] = field(default_factory=list)
    return_bytes: int = 0
    error_kind: str | None = None


@dataclass(frozen=True)
class ProgramExecutionResult:
    result: Any
    has_result: bool
    trace: ProgramTrace


class ProgramExecutionError(RuntimeError):
    def __init__(
        self,
        info: ProgramErrorInfo,
        *,
        trace: ProgramTrace | None = None,
        failed_call: ProgramCallSite | None = None,
    ) -> None:
        super().__init__(info.message)
        self.info = info
        self.trace = trace
        self.failed_call = failed_call

    def failed_call_payload(self) -> dict[str, Any] | None:
        site = self.failed_call
        if site is None:
            return None
        return {
            "name": site.name,
            "call_site": site.call_site,
            "occurrence": site.occurrence,
        }


class ProgramRecord:
    """Read-only object view exposing only result-schema fields."""

    __slots__ = ("_function", "_schema", "_values")

    def __init__(
        self,
        values: Mapping[str, Any],
        schema: dict,
        function: str,
    ) -> None:
        object.__setattr__(self, "_values", dict(values))
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_function", function)

    def __getattr__(self, field_name: str) -> Any:
        if field_name.startswith("_"):
            raise AttributeError(field_name)
        values = object.__getattribute__(self, "_values")
        if field_name in values:
            return values[field_name]
        function = object.__getattribute__(self, "_function")
        raise ProgramExecutionError(
            ProgramErrorInfo(
                "program_unknown_field",
                f"field {field_name!r} is not declared by {function}",
                details={"function": function, "field": field_name},
            )
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ProgramRecord is read-only")  # noqa: TRY003

    def __repr__(self) -> str:
        return "<program-record>"


class FailedProgramRecord(ProgramRecord):
    """失败调用的返回值（2026-08-15，失败即返回值）。

    ``ok`` / ``error`` 正常可读；**业务字段一读即中止程序**。

    这条守卫是 A 方案的必要配套。失败变成返回值以后，"不检查 ok"不再像从前
    那样安全地把整段程序停住，而是让 None 一路流进 f-string 和参数里——
    群里会看见「找到 None 条结果」。守卫把这种情况变回一次干净的中止，
    ``program_unchecked_failure`` 明确指出漏检了哪个函数的哪个字段。

    结果是三种写法各得其所：检查了 ``ok`` 再分支 → 照常跑；没检查但也没用
    返回值（只是继续调下一个）→ 照常跑；没检查却直接用数据 → 中止，与迁移前
    行为一致。
    """

    __slots__ = ()

    def __getattr__(self, field_name: str) -> Any:
        if field_name.startswith("_"):
            raise AttributeError(field_name)
        if field_name in OUTCOME_FIELDS:
            return ProgramRecord.__getattr__(self, field_name)
        values = object.__getattribute__(self, "_values")
        if field_name in values:
            function = object.__getattribute__(self, "_function")
            raise ProgramExecutionError(
                ProgramErrorInfo(
                    "program_unchecked_failure",
                    f"{function} failed; check .ok before reading "
                    f".{field_name}",
                    details={"function": function, "field": field_name},
                )
            )
        # 未声明的字段仍然是 program_unknown_field，与成功路径同一种错。
        return ProgramRecord.__getattr__(self, field_name)


class ProgramList(Sequence[Any]):
    """Read-only bounded sequence used for tool arrays and program lists."""

    __slots__ = ("_items",)

    def __init__(self, items: Iterable[Any]) -> None:
        object.__setattr__(self, "_items", tuple(items))

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_items"))

    def __iter__(self) -> Iterator[Any]:
        return iter(object.__getattribute__(self, "_items"))

    def __getitem__(self, index: int | slice) -> Any:
        value = object.__getattribute__(self, "_items")[index]
        return ProgramList(value) if isinstance(index, slice) else value

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ProgramList is read-only")  # noqa: TRY003

    def __repr__(self) -> str:
        return "<program-list>"


class ProgramExecutor:
    """Execute one prepared program. Hosted by ProgramRunner, not the decision tick."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        registry: ToolRegistry,
        session_factory: Any,
        scope_key: str,
        correlation_id: str,
        decision_id: str,
        context: DecisionContext | None = None,
        supervisor: Any | None = None,
        caption_image: Any | None = None,
        call_timeout_seconds: float = CALL_TIMEOUT_SECONDS,
        program_timeout_seconds: float = PROGRAM_TIMEOUT_SECONDS,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory
        self._scope_key = scope_key
        self._scope = scope_key.split(":", 1)[0]
        self._correlation_id = correlation_id
        self._decision_id = decision_id
        self._context = context
        self._supervisor = supervisor
        self._caption_image = caption_image
        self._call_timeout = call_timeout_seconds
        self._program_timeout = program_timeout_seconds

    async def execute(self, program: PreflightResult) -> ProgramExecutionResult:
        started = time.monotonic()
        state = _RuntimeState(
            executor=self,
            program=program,
            deadline=started + self._program_timeout,
            trace=ProgramTrace(
                decision_id=self._decision_id,
                program_sha256=program.program_sha256,
                scope_key=self._scope_key,
            ),
        )
        try:
            try:
                globals_dict = state.globals()
                code = compile(
                    build_executable_tree(program),
                    filename="<planner-program>",
                    mode="exec",
                )
                exec(
                    code,
                    globals_dict,
                    globals_dict,
                )
                main = globals_dict["__program_main__"]
                raw_result = await asyncio.wait_for(
                    main(), timeout=self._program_timeout
                )
                # unwrap/序列化必须留在本 try 内:它们抛出的任何非契约异常
                # (如深嵌套导致的 RecursionError)若逃出 execute(),该拍将
                # 没有 program terminal,decision_emitted 悬空且收口器在同
                # 进程内永不补写。
                result = unwrap_program_value(raw_result)
                encoded = _json_bytes(result)
            except asyncio.TimeoutError:
                state.fail(
                    "program_timeout",
                    "program wall-time limit exceeded",
                    scope="program",
                )
            except asyncio.CancelledError:
                state.fail(
                    "interrupted",
                    "program execution was cancelled",
                    status="uncertain",
                )
            except ProgramExecutionError:
                raise
            except Exception as exc:  # noqa: BLE001
                state.fail(
                    "program_forbidden_construct",
                    f"runtime operation failed: {type(exc).__name__}",
                    construct="runtime_operation",
                )

            state.trace.return_bytes = len(encoded)
            if len(encoded) > MAX_RETURN_BYTES:
                state.fail(
                    "program_output_too_large",
                    f"program return is {len(encoded)} bytes; maximum is "
                    f"{MAX_RETURN_BYTES}",
                    actual_bytes=len(encoded),
                    max_bytes=MAX_RETURN_BYTES,
                )
            return ProgramExecutionResult(
                result=result,
                has_result=program.has_return,
                trace=state.trace,
            )
        except ProgramExecutionError as exc:
            exc.trace = state.trace
            state.trace.error_kind = exc.info.error_kind
            raise
        finally:
            state.trace.duration_ms = _elapsed_ms(started)
            _log_trace(state.trace)

    # task_anchor 已于 2026-08-21 删除（渲染格式表 §一②/§八10）。它是敏感工具
    # 发起人反查的**回退路径**：调用挂在某个任务上而没显式传
    # triggered_by_event_id 时，运行时拿任务的起因事件顶上。便签没有 ID、
    # 也没有起因，这条路径没有等价物——发起人凭据现在只剩模型显式传
    # triggered_by_event_id= 一条路，不传则按 GUEST 保守拒绝
    # （tool_registry._resolve_triggering_tier）。不要发明新的隐式回退：
    # 猜出来的凭据比没有凭据更危险。


@dataclass
class _RuntimeState:
    executor: ProgramExecutor
    program: PreflightResult
    deadline: float
    trace: ProgramTrace
    query_call_count: int = 0

    def globals(self) -> dict[str, Any]:
        namespace: dict[str, Any] = {
            "__builtins__": {},
            "__program_step__": self.step,
            "__program_iter__": self.iterate,
            "__program_container__": self.container,
            "__program_string__": self.string,
            "__program_format__": self.format_value,
            "__program_binop__": self.binop,
        }
        namespace.update(self.safe_builtins())
        for spec in self.executor._registry.specs(self.executor._scope):
            namespace[spec.name] = self._program_function(spec)
        return namespace

    def safe_builtins(self) -> dict[str, Callable[..., Any]]:
        builtins: dict[str, Callable[..., Any]] = {
            "len": self.safe_len,
            "sorted": self.safe_sorted,
            "min": self.safe_min,
            "max": self.safe_max,
            "sum": self.safe_sum,
            "any": self.safe_any,
            "all": self.safe_all,
            "str": self.safe_str,
            "int": self.safe_int,
            "bool": self.safe_bool,
            "list": self.safe_list,
            "dict": self.safe_dict,
            "join": self.safe_join,
        }
        if set(builtins) != set(SAFE_BUILTINS):
            raise AssertionError("safe builtin registry drift")  # noqa: TRY003
        return builtins

    def _program_function(self, spec: ProgramFunctionSpec) -> Callable[..., Any]:
        async def invoke(
            *, __program_call_site__: str | None = None, **kwargs: Any
        ) -> Any:
            return await self.invoke_tool(
                spec,
                kwargs,
                call_site_id=__program_call_site__,
            )

        return invoke

    def step(self) -> None:
        self.trace.statement_count += 1
        if self.trace.statement_count > MAX_STATEMENTS:
            self.fail(
                "program_quota_exceeded",
                "program statement quota exceeded",
                quota="statements",
                actual=self.trace.statement_count,
                max=MAX_STATEMENTS,
            )
        self.check_deadline()

    def iterate(self, value: Any) -> Iterator[Any]:
        if not isinstance(value, (ProgramList, list, tuple)):
            self.fail(
                "program_forbidden_construct",
                "for/comprehension requires a bounded ProgramList",
                construct="unbounded_iterable",
            )
        for item in value:
            self.trace.iteration_count += 1
            if self.trace.iteration_count > MAX_ITERATIONS:
                self.fail(
                    "program_quota_exceeded",
                    "program iteration quota exceeded",
                    quota="iterations",
                    actual=self.trace.iteration_count,
                    max=MAX_ITERATIONS,
                )
            self.check_deadline()
            yield item

    def container(self, value: Any) -> Any:
        length = len(value)
        if length > MAX_CONTAINER_ELEMENTS:
            self.fail(
                "program_quota_exceeded",
                "container element quota exceeded",
                quota="container_elements",
                actual=length,
                max=MAX_CONTAINER_ELEMENTS,
            )
        if isinstance(value, list):
            return ProgramList(value)
        if isinstance(value, dict):
            return value
        self.fail(
            "program_forbidden_construct",
            "unsupported container value",
            construct="container_type",
        )
        return None

    def string(self, value: Any) -> str:
        text = value if isinstance(value, str) else str(value)
        if len(text) > MAX_STRING_LENGTH:
            self.fail(
                "program_quota_exceeded",
                "string length quota exceeded",
                quota="string_chars",
                actual=len(text),
                max=MAX_STRING_LENGTH,
            )
        return text

    def format_value(self, value: Any) -> str:
        if value is None:
            return "None"
        if not isinstance(value, (str, bool, int, float)):
            self.fail(
                "program_forbidden_construct",
                "f-string values must be JSON scalars",
                construct="fstring_value_type",
            )
        return self.string(str(value))

    def binop(  # noqa: C901, PLR0912
        self, operator: str, left: Any, right: Any
    ) -> Any:
        numeric = (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
        )
        try:
            if operator == "add":
                if numeric:
                    result = left + right
                elif (isinstance(left, str) and isinstance(right, str)) or (
                    isinstance(left, ProgramList) and isinstance(right, ProgramList)
                ):
                    result = (
                        left + right
                        if isinstance(left, str)
                        else ProgramList([*left, *right])
                    )
                else:
                    raise TypeError(  # noqa: TRY003, TRY301
                        "add requires numbers, strings, or ProgramLists"
                    )
            elif operator == "sub" and numeric:
                result = left - right
            elif operator == "mul" and numeric:
                result = left * right
            elif operator == "div" and numeric:
                result = left / right
            elif operator == "floordiv" and numeric:
                result = left // right
            elif operator == "mod" and numeric:
                result = left % right
            else:
                raise TypeError(  # noqa: TRY003, TRY301
                    f"unsupported operands for {operator}"
                )
        except (ArithmeticError, TypeError, ValueError) as exc:
            self.fail(
                "program_forbidden_construct",
                f"safe arithmetic failed: {exc}",
                construct=f"binop_{operator}",
            )
        if isinstance(result, str):
            return self.string(result)
        if isinstance(result, ProgramList):
            return self.container(list(result))
        if isinstance(result, float) and not math.isfinite(result):
            self.fail(
                "program_forbidden_construct",
                "non-finite numeric result is not JSON-compatible",
                construct="non_finite_number",
            )
        if isinstance(result, int) and result.bit_length() > MAX_NUMBER_BITS:
            self.fail(
                "program_quota_exceeded",
                "integer magnitude quota exceeded",
                quota="number_bits",
                actual=result.bit_length(),
                max=MAX_NUMBER_BITS,
            )
        return result

    async def invoke_tool(
        self,
        spec: ProgramFunctionSpec,
        kwargs: dict[str, Any],
        *,
        call_site_id: str | None,
    ) -> Any:
        self.check_deadline()
        site = next(
            (
                item
                for item in self.program.call_sites
                if item.call_site == call_site_id
            ),
            None,
        )
        if site is None:
            self.fail(
                "program_forbidden_construct",
                "program function call has no validated call-site id",
                construct="missing_call_site",
            )
        assert site is not None
        raw_kwargs = {key: unwrap_program_value(value) for key, value in kwargs.items()}
        schema = _select_runtime_schema(spec.arguments_schema, raw_kwargs)
        properties = _schema_properties(schema)
        declared = set(properties)
        # 保留通道判定与静态层同一口径(program_ast:reserved = 保留名 - declared):
        # schema 已声明的同名字段是业务参数,只进 arguments,不当挂靠锚。
        triggered = (
            raw_kwargs.get("triggered_by_event_id")
            if "triggered_by_event_id" not in declared
            else None
        )
        arguments = dict(raw_kwargs)
        if "triggered_by_event_id" not in declared:
            arguments.pop("triggered_by_event_id", None)

        if triggered is not None and not isinstance(triggered, str):
            return await self._invalid_reserved_effect_call(
                spec,
                site,
                arguments,
                "triggered_by_event_id must be a string or null",
            )
        # 2026-08-21：这里曾有一条回退——triggered 为空但挂了 task_id 时，去
        # created_task_anchors / executor.task_anchor 取任务的起因事件顶上。
        # 任务坍缩为单栏便签后 task_id 值域消失，回退随之删除。省略
        # triggered_by_event_id 的敏感调用现在会一路走到 GUEST 判定被拒，
        # 这是**有意的**：宁可明确失败，也不猜一个发起人出来。

        self.query_call_count += 1
        if self.query_call_count > MAX_PROGRAM_CALLS:
            self.fail(
                "program_quota_exceeded",
                "dynamic program call quota exceeded",
                failed_call=site,
                quota="program_calls",
                actual=self.query_call_count,
                max=MAX_PROGRAM_CALLS,
            )
        return await self._run_effect(
            spec,
            site,
            arguments,
            triggered_by_event_id=triggered,
        )

    async def _invalid_reserved_effect_call(
        self,
        spec: ProgramFunctionSpec,
        site: ProgramCallSite,
        arguments: dict[str, Any],
        message: str,
    ) -> Any:
        return await self._run_effect(
            spec,
            site,
            arguments,
            triggered_by_event_id=None,
            forced_outcome=ToolOutcome.failure("invalid_arguments", message),
        )

    async def _run_effect(  # noqa: C901, PLR0913
        self,
        spec: ProgramFunctionSpec,
        site: ProgramCallSite,
        arguments: dict[str, Any],
        *,
        triggered_by_event_id: str | None,
        forced_outcome: ToolOutcome | None = None,
    ) -> Any:
        context = self.executor._context
        handle = await begin_effect_call(
            self.executor._session_factory,
            scope_key=self.executor._scope_key,
            correlation_id=self.executor._correlation_id,
            decision_id=self.executor._decision_id,
            tool_name=spec.name,
            arguments=arguments,
            triggered_by_event_id=triggered_by_event_id,
            bot_role=context.bot_role if context is not None else None,
            call_site=site.call_site,
            occurrence=site.occurrence,
            occurred_at=context.now if context is not None else None,
        )
        self.trace.effect_call_ids.append(handle.tool_call_id)
        started = time.monotonic()
        cancelled = False
        try:
            if forced_outcome is not None:
                outcome = forced_outcome
            else:
                outcome = await self._call_tool(
                    spec,
                    arguments,
                    triggered_by_event_id=triggered_by_event_id,
                    tool_call_event_id=handle.called_event_id,
                )
        except asyncio.TimeoutError:
            timeout_scope = "program" if time.monotonic() >= self.deadline else "call"
            outcome = uncertain_outcome(
                error_kind="program_timeout",
                error_message=f"effect {spec.name} exceeded the call timeout",
                scope=timeout_scope,
            )
        except asyncio.CancelledError:
            cancelled = True
            timed_out = time.monotonic() >= self.deadline
            outcome = uncertain_outcome(
                error_kind="program_timeout" if timed_out else "interrupted",
                error_message=(
                    "program wall-time limit expired while effect was in flight"
                    if timed_out
                    else "program was cancelled while effect was in flight"
                ),
                scope="program" if timed_out else None,
            )

        try:
            terminal_cancelled = await _shield_finish_effect(
                self.executor._session_factory,
                scope_key=self.executor._scope_key,
                correlation_id=self.executor._correlation_id,
                handle=handle,
                outcome=outcome,
            )
        except Exception:  # noqa: BLE001
            terminal_cancelled = True
            if outcome.ok:
                outcome = uncertain_outcome(
                    error_kind="interrupted",
                    error_message=(
                        "effect terminal write failed after the call finished"
                    ),
                )
        cancelled = cancelled or terminal_cancelled
        if cancelled:
            error_kind = outcome.error_kind or "interrupted"
            self._record_call(
                spec,
                site,
                arguments,
                "uncertain",
                started,
                error_kind=error_kind,
            )
            # extra 里可能已经带了 status（uncertain_outcome 恒设 status=
            # "uncertain"），与这里显式传的重名会直接 TypeError，再被外层通用
            # 兜底吞成 program_forbidden_construct——真实原因（超时/取消）就此
            # 丢失。先摘掉再合。
            cancelled_extra = {
                key: value
                for key, value in (outcome.extra or {}).items()
                if key != "status"
            }
            self.fail(
                error_kind,
                outcome.error_message
                or "program execution stopped; effect state is uncertain",
                failed_call=site,
                status="uncertain",
                **cancelled_extra,
            )
        if not outcome.ok and outcome.error_kind in FATAL_ERROR_KINDS:
            # 程序自身正在被拆掉（墙钟到点、进程关停），不是"这次调用没成"。
            # 没有"继续往下跑"可言：后面的语句已经没有时间预算，副作用状态也
            # 存疑。这类必须保持中止，否则超时程序会继续发消息。
            self._record_call(
                spec,
                site,
                arguments,
                str((outcome.extra or {}).get("status") or "failed"),
                started,
                error_kind=outcome.error_kind,
            )
            self.fail(
                outcome.error_kind or "internal_tool_error",
                outcome.error_message or f"effect {spec.name} failed",
                failed_call=site,
                **(outcome.extra or {}),
            )
        if not outcome.ok:
            # 2026-08-15：其余失败不再中止程序，而是作为返回值交回去。工具终态
            # （agent.tool_failed）已经在上面写完，时间线上失败仍是既成事实；
            # 变的只是"这段程序还能不能继续"。
            self._record_call(
                spec,
                site,
                arguments,
                str((outcome.extra or {}).get("status") or "failed"),
                started,
                error_kind=outcome.error_kind,
            )
            return self._failure_value(spec, outcome)

        # 这里曾把 agent.task_created 的 task_id → triggered_by_event_id 记进
        # created_task_anchors，供同程序内后续调用省略 triggered_by_event_id。
        # 随任务坍缩为单栏便签一并删除（2026-08-21）。
        result_bytes = _result_size(outcome.result)
        try:
            wrapped = wrap_program_value(
                _with_success_envelope(outcome.result),
                spec.result_schema,
                function=spec.name,
            )
        except ProgramExecutionError as exc:
            if exc.failed_call is None:
                exc.failed_call = site
            self._record_call(
                spec,
                site,
                arguments,
                "failed",
                started,
                result_bytes=result_bytes,
                error_kind=exc.info.error_kind,
            )
            raise
        self._record_call(
            spec,
            site,
            arguments,
            "ok",
            started,
            result_bytes=result_bytes,
        )
        return wrapped

    async def _call_tool(
        self,
        spec: ProgramFunctionSpec,
        arguments: dict[str, Any],
        *,
        triggered_by_event_id: str | None,
        tool_call_event_id: str | None,
    ) -> ToolOutcome:
        tool = self.executor._registry.get(spec.name)
        if tool is None:
            return ToolOutcome.failure(
                "internal_tool_error",
                f"registered program function {spec.name} has no factory",
            )
        context = self.executor._context
        remaining = max(0.001, self.deadline - time.monotonic())
        timeout = min(self.executor._call_timeout, remaining)
        try:
            raw = await asyncio.wait_for(
                tool.run(
                    arguments,
                    scope_key=self.executor._scope_key,
                    correlation_id=self.executor._correlation_id,
                    session_factory=self.executor._session_factory,
                    triggered_by_event_id=triggered_by_event_id,
                    triggered_by_user_tier=None,
                    bot_role=context.bot_role if context is not None else None,
                    tool_call_event_id=tool_call_event_id,
                    wake_scope=getattr(self.executor._supervisor, "wake", None),
                    note_activity=getattr(
                        self.executor._supervisor, "note_activity", None
                    ),
                    caption_image=self.executor._caption_image,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[program] tool {} crashed", spec.name)
            return ToolOutcome.failure(
                "internal_tool_error", f"{type(exc).__name__}: {exc}"
            )
        return coerce_tool_outcome(raw)

    def _failure_value(
        self, spec: ProgramFunctionSpec, outcome: ToolOutcome
    ) -> FailedProgramRecord:
        """失败 outcome → 程序可读的返回值：``ok=False`` + ``error``，业务字段全 None。

        业务字段仍然逐个建出来（值为 None），这样 ``.results`` 这类访问命中的
        是 ``program_unchecked_failure``（漏检 ok），而不是
        ``program_unknown_field``（写错字段名）——两种错因对模型是不同的事。
        """
        extra = outcome.extra or {}
        status = extra.get("status")
        error = ProgramRecord(
            {
                "kind": outcome.error_kind or "internal_tool_error",
                "message": (
                    outcome.error_message or f"{spec.name} failed"
                )[:MAX_RESULT_STRING_CHARS],
                "status": str(status) if isinstance(status, str) else None,
            },
            OUTCOME_ERROR_SCHEMA,
            spec.name,
        )
        values: dict[str, Any] = dict.fromkeys(
            _schema_properties(spec.result_schema)
        )
        values["ok"] = False
        values["error"] = error
        return FailedProgramRecord(values, spec.result_schema, spec.name)

    def _record_call(  # noqa: PLR0913
        self,
        spec: ProgramFunctionSpec,
        site: ProgramCallSite,
        arguments: dict[str, Any],
        status: str,
        started: float,
        *,
        result_bytes: int | None = None,
        error_kind: str | None = None,
    ) -> None:
        self.trace.calls.append(
            ProgramCallTrace(
                name=spec.name,
                occurrence=site.occurrence,
                program_kind=spec.program_kind,
                call_site=site.call_site,
                arguments_hash=_arguments_hash(arguments),
                status=status,
                duration_ms=_elapsed_ms(started),
                result_bytes=result_bytes,
                error_kind=error_kind,
            )
        )

    def check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            self.fail(
                "program_timeout",
                "program wall-time limit exceeded",
                scope="program",
            )

    def fail(
        self,
        error_kind: str,
        message: str,
        *,
        failed_call: ProgramCallSite | None = None,
        **details: Any,
    ) -> NoReturn:
        raise ProgramExecutionError(
            ProgramErrorInfo(
                error_kind,
                str(message)[:500],
                details=details,
            ),
            trace=self.trace,
            failed_call=failed_call,
        )

    # Safe builtin implementations -------------------------------------------------

    def safe_len(self, value: Any) -> int:
        if not isinstance(value, (ProgramList, str, list, dict)):
            self.fail(
                "program_forbidden_construct",
                "len() only accepts strings and bounded containers",
                construct="builtin_len_type",
            )
        return len(value)

    def safe_sorted(self, value: Any, *, reverse: bool = False) -> ProgramList:
        items = list(self.iterate(value))
        try:
            result = sorted(items, reverse=bool(reverse))
        except (TypeError, ValueError) as exc:
            self.fail(
                "program_forbidden_construct",
                f"sorted() failed: {exc}",
                construct="builtin_sorted",
            )
        return self.container(result)

    def safe_min(self, *values: Any) -> Any:
        return self._safe_extreme("min", values)

    def safe_max(self, *values: Any) -> Any:
        return self._safe_extreme("max", values)

    def _safe_extreme(self, name: str, values: tuple[Any, ...]) -> Any:
        items = (
            list(self.iterate(values[0]))
            if len(values) == 1 and isinstance(values[0], (ProgramList, list, tuple))
            else list(values)
        )
        if not items:
            self.fail(
                "program_forbidden_construct",
                f"{name}() requires at least one value",
                construct=f"builtin_{name}",
            )
        try:
            return min(items) if name == "min" else max(items)
        except (TypeError, ValueError) as exc:
            self.fail(
                "program_forbidden_construct",
                f"{name}() failed: {exc}",
                construct=f"builtin_{name}",
            )

    def safe_sum(self, value: Any) -> int | float:
        total: int | float = 0
        for item in self.iterate(value):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                self.fail(
                    "program_forbidden_construct",
                    "sum() accepts only numbers",
                    construct="builtin_sum_type",
                )
            total = self.binop("add", total, item)
        return total

    def safe_any(self, value: Any) -> bool:
        return any(bool(item) for item in self.iterate(value))

    def safe_all(self, value: Any) -> bool:
        return all(bool(item) for item in self.iterate(value))

    def safe_str(self, value: Any = "") -> str:
        if value is None:
            return "None"
        if not isinstance(value, (str, bool, int, float)):
            self.fail(
                "program_forbidden_construct",
                "str() accepts only JSON scalar values",
                construct="builtin_str_type",
            )
        return self.string(str(value))

    def safe_int(self, value: Any = 0) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            self.fail(
                "program_forbidden_construct",
                "int() accepts only strings or numbers",
                construct="builtin_int_type",
            )
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            self.fail(
                "program_forbidden_construct",
                f"int() failed: {exc}",
                construct="builtin_int",
            )

    def safe_bool(self, value: Any = None) -> bool:
        if not isinstance(
            value,
            (type(None), bool, int, float, str, ProgramList, list, dict),
        ):
            self.fail(
                "program_forbidden_construct",
                "bool() received an unsupported value",
                construct="builtin_bool_type",
            )
        return bool(value)

    def safe_list(self, value: Any = None) -> ProgramList:
        if value is None:
            return ProgramList(())
        return self.container(list(self.iterate(value)))

    def safe_dict(self, value: Any = None, **fields: Any) -> dict[str, Any]:
        if value is None:
            result: dict[str, Any] = {}
        elif isinstance(value, dict):
            result = dict(value)
        else:
            self.fail(
                "program_forbidden_construct",
                "dict() accepts at most one dict value",
                construct="builtin_dict_type",
            )
        result.update(fields)
        return self.container(result)

    def safe_join(self, separator: Any, items: Any) -> str:
        if not isinstance(separator, str):
            self.fail(
                "program_forbidden_construct",
                "join() separator must be a string",
                construct="builtin_join_separator",
            )
        values = list(self.iterate(items))
        if any(not isinstance(item, str) for item in values):
            self.fail(
                "program_forbidden_construct",
                "join() items must all be strings",
                construct="builtin_join_items",
            )
        return self.string(separator.join(values))


async def _shield_finish_effect(
    session_factory: Any,
    *,
    scope_key: str,
    correlation_id: str,
    handle: EffectCallHandle,
    outcome: ToolOutcome,
) -> bool:
    """Finish transaction 2 despite task cancellation; return cancellation flag."""
    task = asyncio.create_task(
        finish_effect_call(
            session_factory,
            scope_key=scope_key,
            correlation_id=correlation_id,
            handle=handle,
            outcome=outcome,
        )
    )
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:  # noqa: PERF203
            cancelled = True
    task.result()
    return cancelled


def _with_success_envelope(result: Any) -> dict:
    """成功 outcome 的 result → 带 ``ok=True`` / ``error=None`` 的载荷。

    18 个工具的 result_schema 顶层都是 object，result 也都是 dict；非 Mapping
    只可能来自 stub，按空载荷处理，信封字段照样在。
    """
    payload = dict(result) if isinstance(result, Mapping) else {}
    payload["ok"] = True
    payload["error"] = None
    return payload


def wrap_program_value(  # noqa: C901, PLR0911, PLR0912
    value: Any, schema: dict, *, function: str, _depth: int = 0
) -> Any:
    """Recursively project a Tool result onto its declared read-only ABI.

    体积检查用结果侧上限(``MAX_RESULT_*``),不用程序侧上限:上游结果的
    大小由各工具自行封顶,这里只是防线——用 4000/1000 会把 webfetch /
    websearch 的合法结果在被读到之前整段拒掉。
    """
    if _depth > MAX_VALUE_DEPTH:
        _quota_value_error("value_depth", _depth, MAX_VALUE_DEPTH)
    schema = _select_value_schema(schema, value)
    types = _schema_types(schema)
    if value is None:
        return None
    if isinstance(value, bytes) or callable(value):
        _value_error(function, "tool returned a forbidden host value")
    if isinstance(value, Mapping):
        if types and "object" not in types:
            _value_error(function, "tool result does not match result_schema")
        properties = _schema_properties(schema)
        wrapped = {
            field_name: wrap_program_value(
                value.get(field_name),
                field_schema,
                function=function,
                _depth=_depth + 1,
            )
            for field_name, field_schema in properties.items()
        }
        return ProgramRecord(wrapped, schema, function)
    if isinstance(value, (list, tuple)):
        if types and "array" not in types:
            _value_error(function, "tool result does not match result_schema")
        if len(value) > MAX_RESULT_CONTAINER_ELEMENTS:
            _quota_value_error(
                "result_container_elements",
                len(value),
                MAX_RESULT_CONTAINER_ELEMENTS,
            )
        item_schema = schema.get("items") if isinstance(schema, dict) else {}
        if not isinstance(item_schema, dict):
            item_schema = {}
        return ProgramList(
            wrap_program_value(
                item,
                item_schema,
                function=function,
                _depth=_depth + 1,
            )
            for item in value
        )
    if isinstance(value, str):
        if types and "string" not in types:
            _value_error(function, "tool result does not match result_schema")
        if len(value) > MAX_RESULT_STRING_CHARS:
            _quota_value_error(
                "result_string_chars", len(value), MAX_RESULT_STRING_CHARS
            )
        return value
    if isinstance(value, bool):
        if types and "boolean" not in types:
            _value_error(function, "tool result does not match result_schema")
        return value
    if isinstance(value, int):
        if types and not ({"integer", "number"} & types):
            _value_error(function, "tool result does not match result_schema")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _value_error(function, "tool returned a non-finite number")
        if types and "number" not in types:
            _value_error(function, "tool result does not match result_schema")
        return value
    _value_error(
        function,
        f"tool returned unsupported host type {type(value).__name__}",
    )
    return None


def unwrap_program_value(value: Any, _depth: int = 0) -> Any:  # noqa: PLR0911
    if _depth > MAX_VALUE_DEPTH:
        _quota_value_error("value_depth", _depth, MAX_VALUE_DEPTH)
    inner = _depth + 1
    if isinstance(value, ProgramRecord):
        values = object.__getattribute__(value, "_values")
        return {key: unwrap_program_value(item, inner) for key, item in values.items()}
    if isinstance(value, ProgramList):
        return [unwrap_program_value(item, inner) for item in value]
    if isinstance(value, list):
        return [unwrap_program_value(item, inner) for item in value]
    if isinstance(value, tuple):
        return [unwrap_program_value(item, inner) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ELEMENTS:
            _quota_value_error("container_elements", len(value), MAX_CONTAINER_ELEMENTS)
        return {
            str(key): unwrap_program_value(item, inner)
            for key, item in value.items()
        }
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        _quota_value_error("string_chars", len(value), MAX_STRING_LENGTH)
    if isinstance(value, float) and not math.isfinite(value):
        _value_error("program", "return contains a non-finite number")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    _value_error("program", f"return contains unsupported type {type(value).__name__}")
    return None


def _value_error(function: str, message: str) -> NoReturn:
    raise ProgramExecutionError(
        ProgramErrorInfo(
            "program_forbidden_construct",
            message,
            details={"construct": "result_abi", "function": function},
        )
    )


def _quota_value_error(quota: str, actual: int, maximum: int) -> NoReturn:
    raise ProgramExecutionError(
        ProgramErrorInfo(
            "program_quota_exceeded",
            f"program quota {quota} exceeded: {actual} > {maximum}",
            details={"quota": quota, "actual": actual, "max": maximum},
        )
    )


def _select_runtime_schema(schema: dict, arguments: dict[str, Any]) -> dict:
    action = arguments.get("action")
    for branch in schema.get("oneOf") or []:
        action_schema = (branch.get("properties") or {}).get("action") or {}
        if action_schema.get("const") == action:
            return branch
    return schema


def _select_value_schema(schema: dict, value: Any) -> dict:
    branches = schema.get("oneOf") or schema.get("anyOf") or []
    for branch in branches:
        if _value_matches_types(value, _schema_types(branch)):
            return branch
    return schema


def _value_matches_types(value: Any, types: set[str]) -> bool:  # noqa: PLR0911
    if not types:
        return True
    if value is None:
        return "null" in types
    if isinstance(value, bool):
        return "boolean" in types
    if isinstance(value, int):
        return bool({"integer", "number"} & types)
    if isinstance(value, float):
        return "number" in types
    if isinstance(value, str):
        return "string" in types
    if isinstance(value, Mapping):
        return "object" in types
    if isinstance(value, (list, tuple)):
        return "array" in types
    return False


def _schema_types(schema: dict | None) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    raw = schema.get("type")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if "properties" in schema:
        return {"object"}
    types: set[str] = set()
    for keyword in ("oneOf", "anyOf"):
        for branch in schema.get(keyword) or []:
            types |= _schema_types(branch)
    return types


def _schema_properties(schema: dict | None) -> dict[str, dict]:
    if not isinstance(schema, dict):
        return {}
    properties = {
        str(key): value
        for key, value in (schema.get("properties") or {}).items()
        if isinstance(value, dict)
    }
    for keyword in ("oneOf", "anyOf"):
        for branch in schema.get(keyword) or []:
            properties.update(_schema_properties(branch))
    return properties


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProgramExecutionError(
            ProgramErrorInfo(
                "program_forbidden_construct",
                f"return is not JSON-compatible: {exc}",
                details={"construct": "non_json_return"},
            )
        ) from None


def _result_size(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except Exception:  # noqa: BLE001
        return 0


def _arguments_hash(arguments: dict[str, Any]) -> str:
    import hashlib

    body = json.dumps(
        arguments,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:16]


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _log_trace(trace: ProgramTrace) -> None:
    logger.info(
        "[program_trace] decision={} sha={} scope={} duration_ms={} "
        "statements={} iterations={} calls={} effect_calls={} "
        "return_bytes={} error_kind={} calls={}",
        trace.decision_id,
        trace.program_sha256[:12],
        trace.scope_key,
        trace.duration_ms,
        trace.statement_count,
        trace.iteration_count,
        len(trace.calls),
        len(trace.effect_call_ids),
        trace.return_bytes,
        trace.error_kind,
        [
            {
                "name": call.name,
                "occurrence": call.occurrence,
                "kind": call.program_kind,
                "args": call.arguments_hash,
                "status": call.status,
                "duration_ms": call.duration_ms,
                "result_bytes": call.result_bytes,
                "error_kind": call.error_kind,
            }
            for call in trace.calls
        ],
    )


__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "MAX_ITERATIONS",
    "MAX_PROGRAM_CALLS",
    "MAX_RESULT_CONTAINER_ELEMENTS",
    "MAX_RESULT_STRING_CHARS",
    "MAX_RETURN_BYTES",
    "MAX_STATEMENTS",
    "MAX_VALUE_DEPTH",
    "PROGRAM_TIMEOUT_SECONDS",
    "ProgramCallTrace",
    "ProgramExecutionError",
    "ProgramExecutionResult",
    "ProgramExecutor",
    "ProgramList",
    "ProgramRecord",
    "ProgramTrace",
    "unwrap_program_value",
    "wrap_program_value",
]
