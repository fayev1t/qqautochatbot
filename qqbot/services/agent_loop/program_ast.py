"""Static validation and instrumentation for Planner programs.

The model writes a deliberately tiny Python subset.  This module is pure: it
does not execute tools or touch the database.  It strips an optional outer
fence, wraps the source in an async function shell (so module-level ``return``
is legal), validates the AST and static quotas, then builds an instrumented
tree for :mod:`program_runtime`.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import io
import re
import textwrap
import tokenize
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from qqbot.services.agent_loop.tool_registry import (
        ProgramFunctionSpec,
        ToolRegistry,
    )

MAX_SOURCE_CHARS = 8_000
MAX_AST_NODES = 400
MAX_SYNTAX_DEPTH = 8
MAX_EFFECT_CALL_SITES = 8
MAX_CONTAINER_ELEMENTS = 1_000
MAX_STRING_LENGTH = 4_000

SAFE_BUILTINS = frozenset(
    {
        "len",
        "sorted",
        "min",
        "max",
        "sum",
        "any",
        "all",
        "str",
        "int",
        "bool",
        "list",
        "dict",
        "join",
    }
)

_SHELL_NAME = "__program_main__"
_SHELL_INDENT = "    "
_MIN_FENCED_LINE_COUNT = 2
_FENCE_OPEN = re.compile(r"^```(?:python)?[ \t]*$", re.IGNORECASE)
# Effect 调用的系统保留具名参数：不在工具 arguments_schema 里，由执行层截下来
# 当挂靠锚，不进 arguments。
# 2026-08-21 起只剩 ``triggered_by_event_id`` —— ``task_id`` 随任务坍缩为单栏
# 便签一并消失（渲染格式表 §一②）。这不只是少一个参数：它同时抽掉了敏感工具
# 发起人权限反查的**回退路径**（原先"调用挂在任务上就沿用任务起因"），因此
# ``triggered_by_event_id=`` 从"通常可省"变成敏感工具**必须显式传**。
# 见 planner.md 与各敏感工具 md。
_RESERVED_EFFECT_ARGUMENTS = frozenset({"triggered_by_event_id"})
_SCHEMA_ORIGIN_KEY = "x-program-function"

# 裁决层的调度元指令（2026-08-17 提案-裁决流水线 §1.0；2026-08-21 改为资产
# 寻址）。模型每拍的输出解耦为两层：**裁决层**一行 ``execute_program``，告诉
# 调度器"把代码资产 H 提交给 Runner 执行"；**动作层**才是这一拍新写的业务代码。
# 两层完全正交，一次输出里可以两者都有——那正是流水线形态：确认上一段的同时
# 写下一段。
#
# **两个值域分工**（2026-08-21）：``event_id`` 命名的是时间线上的**历史事实
# 事件（Event）**；``program_hash`` = ``sha256(源码)[:12]``，命名的是**不可变的
# 代码逻辑资产（Program Object）**。``execute_program`` 表达的是"调度执行某段
# 具体的代码资产"，而不是"重新执行当年的某个事件"，因此它消费的是 hash 不是
# 事件 ID。哈希**不掺 occurred_at 或任何时间戳**——掺了它就不再是内容指纹而是
# 一个 ID，与它要表达的资产语义自相矛盾。
#
# 同源码必然同 hash，这是内容寻址的应有之义，不是缺陷：同一份资产可以反复调度，
# 调度几次跑几次，系统不拦（``already_executed`` 守卫已于 2026-08-21 取消）。
#
# 它不是 Program API 工具：registry 里没有它，不占调用点，ProgramExecutor 永远
# 见不到它。**落库解耦（防套娃）**：preflight 在这里就把指令行从源码里剥掉，
# ``PreflightResult.source`` 与 ``program_sha256`` 都只覆盖剩下的纯业务代码，
# 因此 ``decision_emitted.payload.program`` 里绝不会再嵌一条裁决指令；指令本身
# 化为 ``commit_program_hash``，由 AgentLoop 在派发处消费。
#
# 形状必须在静态期锁死：模块顶层的独立语句、唯一具名参数、字面量 hash。写在
# 表达式里（赋值右侧、当参数、放进 if 条件）一律拒绝——那会让"这一拍要不要执行
# 某段代码"变成运行期才知道的事。
_COMMIT_FUNCTION_NAME = "execute_program"
_COMMIT_MAX_CALL_SITES = 1
# 12 位小写十六进制：sha256 摘要的展示前缀，与图片 file_hash、meme hash 同构。
_PROGRAM_HASH_PATTERN = re.compile(r"[0-9a-f]{12}")
PROGRAM_HASH_CHARS = 12
# 事件 ID = 26 位 Crockford base32 ULID（core/ids.new_event_id）。信封里带
# ``ev:`` 前缀展示，实参取裸值。


@dataclass(frozen=True)
class ProgramErrorInfo:
    error_kind: str
    message: str
    line: int | None = None
    column: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_kind": self.error_kind,
            "error_message": self.message,
        }
        if self.line is not None:
            payload["line"] = self.line
        if self.column is not None:
            payload["column"] = self.column
        payload.update(self.details)
        return payload


class ProgramPreflightError(ValueError):
    def __init__(self, info: ProgramErrorInfo) -> None:
        super().__init__(info.message)
        self.info = info


@dataclass(frozen=True)
class ProgramCallSite:
    call_site: str
    name: str
    program_kind: str
    line: int
    column: int
    occurrence: int


@dataclass(frozen=True)
class PreflightResult:
    source: str
    program_sha256: str
    tree: ast.Module = field(repr=False, compare=False)
    call_sites: tuple[ProgramCallSite, ...] = ()
    has_return: bool = False
    # 裁决层：非 None 表示本拍还带了一条调度元指令，值是被指名的**代码资产
    # hash**（不是事件 ID）。它与动作层（source / call_sites / has_return）互不
    # 相干，可以同时存在；``source`` 里**已经剥掉**了这条指令。
    commit_program_hash: str | None = None
    # 指令在**原始**响应里占的物理行范围，只在 preflight 内部用于剥离。
    commit_lines: tuple[int, int] | None = field(default=None, compare=False)

    @property
    def program_hash(self) -> str:
        """本段源码作为**代码资产**的身份：``program_sha256`` 的 12 位前缀。

        与 ``event_id`` 分属两个值域，不可互推：``event_id`` 命名时间线上的
        历史事实事件，本值命名不可变的代码逻辑资产。同源码必然同 hash——这是
        内容寻址的应有之义，``execute_program`` 据此反复调度同一份资产。
        """
        return self.program_sha256[:PROGRAM_HASH_CHARS]


def strip_outer_fence(raw: str) -> str:
    """Accept bare source or exactly one outer ```/```python fence."""
    if not isinstance(raw, str):
        raise ProgramPreflightError(
            ProgramErrorInfo(
                "program_syntax_error",
                "program response must be text",
            )
        )
    text = raw.strip()
    if not text:
        return ""
    if not text.startswith("```"):
        if "```" in text:
            raise ProgramPreflightError(
                ProgramErrorInfo(
                    "program_syntax_error",
                    "markdown fence must wrap the whole response",
                )
            )
        return text
    lines = text.splitlines()
    if len(lines) < _MIN_FENCED_LINE_COUNT or _FENCE_OPEN.fullmatch(lines[0]) is None:
        raise ProgramPreflightError(
            ProgramErrorInfo(
                "program_syntax_error",
                "invalid opening code fence; use ``` or ```python",
                line=1,
                column=0,
            )
        )
    if lines[-1].strip() != "```":
        raise ProgramPreflightError(
            ProgramErrorInfo(
                "program_syntax_error",
                "code fence is not closed at the end of the response",
            )
        )
    body = "\n".join(lines[1:-1])
    if "```" in body:
        raise ProgramPreflightError(
            ProgramErrorInfo(
                "program_syntax_error",
                "nested or additional markdown fences are not allowed",
            )
        )
    return body.strip()


def preflight(
    source: str,
    registry: ToolRegistry,
    scope: str,
) -> PreflightResult:
    """Validate source without executing it and return its stable metadata.

    带裁决指令时走两遍（提案-裁决流水线 §1.1「落库解耦（防套娃）」）：第一遍认出
    指令并拿到它的物理行范围，把那几行从源码里剥掉；第二遍在**纯业务代码**上重新
    走完整校验，于是返回的 ``source`` / ``program_sha256`` / ``call_sites`` 行号
    全部对齐真正落库的那段文本。落库的 ``payload.program`` 因此绝不会再嵌一条
    裁决指令。
    """
    cleaned = strip_outer_fence(source)
    result = _preflight_source(cleaned, registry, scope)
    if result.commit_program_hash is None or result.commit_lines is None:
        return result
    body = _strip_lines(cleaned, result.commit_lines)
    stripped = _preflight_source(body, registry, scope)
    return replace(
        stripped,
        commit_program_hash=result.commit_program_hash,
        commit_lines=result.commit_lines,
    )


def _strip_lines(source: str, span: tuple[int, int]) -> str:
    """删掉 1-based 闭区间 ``span`` 覆盖的物理行。"""
    start, end = span
    kept = [
        line
        for index, line in enumerate(source.splitlines(), start=1)
        if not start <= index <= end
    ]
    return "\n".join(kept).strip()


def _preflight_source(
    cleaned: str,
    registry: ToolRegistry,
    scope: str,
) -> PreflightResult:
    if len(cleaned) > MAX_SOURCE_CHARS:
        _raise_quota(
            None,
            "source_chars",
            len(cleaned),
            MAX_SOURCE_CHARS,
        )
    _reject_multiline_strings(cleaned)
    tree = _parse_with_shell(cleaned)
    function = _program_function(tree)
    node_count = sum(1 for statement in function.body for _ in ast.walk(statement))
    if node_count > MAX_AST_NODES:
        _raise_quota(None, "ast_nodes", node_count, MAX_AST_NODES)
    depth = max((_syntax_depth(stmt) for stmt in function.body), default=0)
    if depth > MAX_SYNTAX_DEPTH:
        _raise_quota(None, "syntax_depth", depth, MAX_SYNTAX_DEPTH)

    normalized_scope = scope.split(":", 1)[0]
    validator = _Validator(registry, normalized_scope)
    validator.validate(function)
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    return PreflightResult(
        source=cleaned,
        program_sha256=digest,
        tree=tree,
        call_sites=tuple(validator.call_sites),
        has_return=validator.return_count == 1,
        commit_program_hash=validator.commit_program_hash,
        commit_lines=validator.commit_lines,
    )


def build_executable_tree(result: PreflightResult) -> ast.Module:
    """Return a copied AST with awaits and quota hooks inserted.

    树里不会有裁决指令——``preflight`` 已在落库前把那一层剥掉，这里拿到的始终
    是纯动作层代码。
    """
    tree = copy.deepcopy(result.tree)
    tree = _Instrumenter(result.call_sites).visit(tree)
    ast.fix_missing_locations(tree)
    return tree


def _reject_multiline_strings(source: str) -> None:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type == tokenize.STRING and token.start[0] != token.end[0]:
                raise ProgramPreflightError(
                    ProgramErrorInfo(
                        "program_forbidden_construct",
                        "string literals may not cross physical lines; use \\n",
                        line=token.start[0],
                        column=token.start[1],
                        details={"construct": "multiline_string"},
                    )
                )
    except (tokenize.TokenError, IndentationError) as exc:
        line = None
        if len(exc.args) > 1 and isinstance(exc.args[1], tuple):
            line = int(exc.args[1][0])
        raise ProgramPreflightError(
            ProgramErrorInfo(
                "program_syntax_error",
                str(exc)[:500],
                line=line,
            )
        ) from None


def _parse_with_shell(source: str) -> ast.Module:
    body = textwrap.indent(source, _SHELL_INDENT) if source else ""
    if body:
        body += "\n"
    # The synthetic pass makes empty/comment-only source a valid function body.
    # Remove it after parsing so a real return remains the final model statement.
    wrapped = f"async def {_SHELL_NAME}():\n{body}    pass\n"
    try:
        tree = ast.parse(wrapped, mode="exec")
    except SyntaxError as exc:
        line = max(1, (exc.lineno or 2) - 1)
        column = max(0, (exc.offset or 1) - 1 - len(_SHELL_INDENT))
        raise ProgramPreflightError(
            ProgramErrorInfo(
                "program_syntax_error",
                (exc.msg or "invalid Python syntax")[:500],
                line=line,
                column=column,
            )
        ) from None
    function = _program_function(tree)
    synthetic_pass = function.body.pop()
    if not isinstance(synthetic_pass, ast.Pass):
        raise AssertionError(  # noqa: TRY003, TRY004
            "program shell sentinel invariant broken"
        )
    if not function.body:
        function.body.append(synthetic_pass)
    return tree


def _program_function(tree: ast.Module) -> ast.AsyncFunctionDef:
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.AsyncFunctionDef):
        raise AssertionError("program shell invariant broken")  # noqa: TRY003
    return tree.body[0]


_SYNTAX_DEPTH_NODES = (
    ast.Attribute,
    ast.BinOp,
    ast.BoolOp,
    ast.Call,
    ast.Compare,
    ast.Dict,
    ast.DictComp,
    ast.For,
    ast.FormattedValue,
    ast.If,
    ast.JoinedStr,
    ast.List,
    ast.ListComp,
    ast.Subscript,
    ast.UnaryOp,
)


def _syntax_depth(node: ast.AST) -> int:
    """Count model-visible nesting, not CPython's AST bookkeeping nodes.

    ``ast.walk`` exposes implementation details such as ``keyword``, ``Load``
    and operator marker nodes. Counting those made an ordinary nested
    ``send_messages(messages=[{...}])`` payload exceed the advertised depth
    limit even though the source itself was only six levels deep. AST node
    count already bounds total syntax size; this quota is specifically about
    constructs a reader experiences as nested.
    """

    own_depth = 1 if isinstance(node, _SYNTAX_DEPTH_NODES) else 0
    children = list(ast.iter_child_nodes(node))
    if not children:
        return own_depth
    return own_depth + max(_syntax_depth(child) for child in children)


class _Validator:
    def __init__(self, registry: ToolRegistry, scope: str) -> None:
        self._registry = registry
        self._scope = scope
        self._visible = {spec.name: spec for spec in registry.specs(scope)}
        self.call_sites: list[ProgramCallSite] = []
        self.return_count = 0
        self.commit_program_hash: str | None = None
        self.commit_lines: tuple[int, int] | None = None
        self._call_occurrences: dict[str, int] = {}
        self._effect_counts: dict[str, int] = {}
        self._commit_count = 0

    def validate(self, function: ast.AsyncFunctionDef) -> None:
        if function.decorator_list or function.args.args or function.args.kwonlyargs:
            self._forbidden(function, "function_definition")
        # Empty source is represented by our own pass node.
        if len(function.body) == 1 and isinstance(function.body[0], ast.Pass):
            return
        self._block(function.body, {}, top_level=True)
        effect_total = sum(self._effect_counts.values())
        if effect_total > MAX_EFFECT_CALL_SITES:
            _raise_quota(
                None,
                "effect_call_sites",
                effect_total,
                MAX_EFFECT_CALL_SITES,
            )
        if self.return_count > 1:
            self._error(
                None,
                "program_forbidden_construct",
                "a program may contain at most one return statement",
                construct="multiple_return",
            )

    def _block(  # noqa: C901, PLR0912
        self,
        statements: list[ast.stmt],
        env: dict[str, dict | None],
        *,
        in_loop: bool = False,
        top_level: bool = False,
    ) -> dict[str, dict | None]:
        for index, statement in enumerate(statements):
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(
                    statement.targets[0], ast.Name
                ):
                    self._forbidden(statement, "assignment_target")
                target = statement.targets[0]
                self._validate_assignment_name(target, target.id)
                env[target.id] = self._expr(
                    statement.value,
                    env,
                    in_loop=in_loop,
                    in_comp=False,
                )
                continue
            if isinstance(statement, ast.Expr):
                if not isinstance(statement.value, ast.Call):
                    self._forbidden(statement, "bare_expression")
                if _is_commit_call(statement.value):
                    self._commit(statement.value, top_level=top_level)
                    continue
                self._expr(
                    statement.value,
                    env,
                    in_loop=in_loop,
                    in_comp=False,
                )
                continue
            if isinstance(statement, ast.If):
                self._expr(
                    statement.test,
                    env,
                    in_loop=in_loop,
                    in_comp=False,
                )
                before = dict(env)
                body_env = self._block(
                    statement.body,
                    dict(env),
                    in_loop=in_loop,
                )
                else_env = self._block(
                    statement.orelse,
                    dict(env),
                    in_loop=in_loop,
                )
                for name in set(body_env) & set(else_env):
                    if name in before or (
                        name not in before and name in body_env and name in else_env
                    ):
                        env[name] = _merge_schema(
                            body_env.get(name), else_env.get(name)
                        )
                continue
            if isinstance(statement, ast.For):
                if statement.orelse:
                    self._forbidden(statement, "for_else")
                if not isinstance(statement.target, ast.Name):
                    self._forbidden(statement, "for_target")
                self._validate_assignment_name(statement.target, statement.target.id)
                iterable_schema = self._expr(
                    statement.iter,
                    env,
                    in_loop=in_loop,
                    in_comp=False,
                )
                if not _schema_allows_type(iterable_schema, "array"):
                    self._forbidden(statement.iter, "unbounded_for_iterable")
                loop_env = dict(env)
                loop_env[statement.target.id] = _array_item_schema(iterable_schema)
                self._block(statement.body, loop_env, in_loop=True)
                continue
            if isinstance(statement, ast.Return):
                if in_loop:
                    self._forbidden(statement, "return_in_loop")
                if index != len(statements) - 1:
                    self._forbidden(statement, "return_not_last")
                self.return_count += 1
                if statement.value is not None:
                    self._expr(
                        statement.value,
                        env,
                        in_loop=in_loop,
                        in_comp=False,
                    )
                continue
            self._forbidden(statement, type(statement).__name__)
        return env

    def _expr(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        node: ast.expr,
        env: dict[str, dict | None],
        *,
        in_loop: bool,
        in_comp: bool,
    ) -> dict | None:
        if isinstance(node, ast.Constant):
            if node.value is Ellipsis or isinstance(node.value, (bytes, complex)):
                self._forbidden(node, "non_json_literal")
            if isinstance(node.value, str) and len(node.value) > MAX_STRING_LENGTH:
                _raise_quota(
                    node,
                    "string_chars",
                    len(node.value),
                    MAX_STRING_LENGTH,
                )
            return _literal_schema(node.value)
        if isinstance(node, ast.Name):
            if node.id not in env:
                self._unknown_name(node, node.id)
            return env[node.id]
        if isinstance(node, ast.List):
            if len(node.elts) > MAX_CONTAINER_ELEMENTS:
                _raise_quota(
                    node,
                    "container_elements",
                    len(node.elts),
                    MAX_CONTAINER_ELEMENTS,
                )
            item_schema: dict | None = None
            for element in node.elts:
                item_schema = _merge_schema(
                    item_schema,
                    self._expr(
                        element,
                        env,
                        in_loop=in_loop,
                        in_comp=in_comp,
                    ),
                )
            return {"type": "array", "items": item_schema or {}}
        if isinstance(node, ast.Dict):
            if len(node.keys) > MAX_CONTAINER_ELEMENTS:
                _raise_quota(
                    node,
                    "container_elements",
                    len(node.keys),
                    MAX_CONTAINER_ELEMENTS,
                )
            properties: dict[str, dict] = {}
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    self._forbidden(node, "dict_unpack")
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    self._forbidden(key, "non_string_dict_key")
                properties[key.value] = (
                    self._expr(
                        value,
                        env,
                        in_loop=in_loop,
                        in_comp=in_comp,
                    )
                    or {}
                )
            return {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            }
        if isinstance(node, ast.Call):
            return self._call(
                node,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
        if isinstance(node, ast.Attribute):
            self._validate_public_name(node, node.attr)
            base_schema = self._expr(
                node.value,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
            field_schema, known_object = _schema_property(base_schema, node.attr)
            if known_object and field_schema is None:
                self._error(
                    node,
                    "program_unknown_field",
                    f"field {node.attr!r} is not declared by the result schema",
                    function=(
                        _schema_origin(base_schema) or _root_call_name(node.value)
                    ),
                    field=node.attr,
                )
            if not known_object:
                self._forbidden(node, "attribute_non_record")
            return field_schema
        if isinstance(node, ast.Subscript):
            value_schema = self._expr(
                node.value,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
            if not (
                _schema_allows_type(value_schema, "array")
                or _schema_allows_type(value_schema, "string")
            ):
                self._forbidden(node, "subscript_non_sequence")
            if isinstance(node.slice, ast.Slice):
                for part in (node.slice.lower, node.slice.upper, node.slice.step):
                    if part is not None:
                        self._expr(
                            part,
                            env,
                            in_loop=in_loop,
                            in_comp=in_comp,
                        )
                return value_schema
            self._expr(
                node.slice,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
            if _schema_allows_type(value_schema, "string"):
                return {"type": "string"}
            return _array_item_schema(value_schema)
        if isinstance(node, ast.BinOp):
            if not isinstance(
                node.op,
                (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod),
            ):
                self._forbidden(node, type(node.op).__name__)
            left = self._expr(
                node.left,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
            right = self._expr(
                node.right,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
            if isinstance(node.op, ast.Mult) and any(
                _schema_allows_type(schema, kind)
                for schema in (left, right)
                for kind in ("string", "array")
            ):
                self._forbidden(node, "sequence_multiplication")
            return _binop_schema(node.op, left, right)
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.Not, ast.UAdd, ast.USub)):
                self._forbidden(node, type(node.op).__name__)
            operand = self._expr(
                node.operand,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
            return {"type": "boolean"} if isinstance(node.op, ast.Not) else operand
        if isinstance(node, ast.BoolOp):
            if not isinstance(node.op, (ast.And, ast.Or)):
                self._forbidden(node, type(node.op).__name__)
            result: dict | None = None
            for value in node.values:
                result = _merge_schema(
                    result,
                    self._expr(
                        value,
                        env,
                        in_loop=in_loop,
                        in_comp=in_comp,
                    ),
                )
            return result
        if isinstance(node, ast.Compare):
            self._expr(
                node.left,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
            for operator, comparator in zip(
                node.ops,
                node.comparators,
                strict=True,
            ):
                if not isinstance(
                    operator,
                    (
                        ast.Eq,
                        ast.NotEq,
                        ast.Lt,
                        ast.LtE,
                        ast.Gt,
                        ast.GtE,
                        ast.In,
                        ast.NotIn,
                        ast.Is,
                        ast.IsNot,
                    ),
                ):
                    self._forbidden(node, type(operator).__name__)
                self._expr(
                    comparator,
                    env,
                    in_loop=in_loop,
                    in_comp=in_comp,
                )
            return {"type": "boolean"}
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    if value.conversion != -1 or value.format_spec is not None:
                        self._forbidden(value, "fstring_conversion_or_format")
                    value_schema = self._expr(
                        value.value,
                        env,
                        in_loop=in_loop,
                        in_comp=in_comp,
                    )
                    if value_schema is not None and not any(
                        _schema_allows_type(value_schema, scalar)
                        for scalar in (
                            "null",
                            "boolean",
                            "integer",
                            "number",
                            "string",
                        )
                    ):
                        self._forbidden(value, "fstring_non_scalar")
                elif not isinstance(value, ast.Constant):
                    self._forbidden(value, "fstring_component")
            return {"type": "string"}
        if isinstance(node, (ast.ListComp, ast.DictComp)):
            if len(node.generators) != 1:
                self._forbidden(node, "nested_comprehension")
            generator = node.generators[0]
            if generator.is_async or not isinstance(generator.target, ast.Name):
                self._forbidden(node, "comprehension_generator")
            self._validate_assignment_name(generator.target, generator.target.id)
            iterable_schema = self._expr(
                generator.iter,
                env,
                in_loop=in_loop,
                in_comp=True,
            )
            if not _schema_allows_type(iterable_schema, "array"):
                self._forbidden(generator.iter, "unbounded_comprehension")
            comp_env = dict(env)
            comp_env[generator.target.id] = _array_item_schema(iterable_schema)
            for condition in generator.ifs:
                self._expr(
                    condition,
                    comp_env,
                    in_loop=True,
                    in_comp=True,
                )
            if isinstance(node, ast.ListComp):
                element_schema = self._expr(
                    node.elt,
                    comp_env,
                    in_loop=True,
                    in_comp=True,
                )
                return {"type": "array", "items": element_schema or {}}
            key_schema = self._expr(
                node.key,
                comp_env,
                in_loop=True,
                in_comp=True,
            )
            if not _schema_allows_type(key_schema, "string"):
                self._forbidden(node.key, "non_string_dict_key")
            value_schema = self._expr(
                node.value,
                comp_env,
                in_loop=True,
                in_comp=True,
            )
            return {
                "type": "object",
                "additionalProperties": value_schema or {},
            }
        self._forbidden(node, type(node).__name__)
        return None

    def _call(  # noqa: C901, PLR0912
        self,
        node: ast.Call,
        env: dict[str, dict | None],
        *,
        in_loop: bool,
        in_comp: bool,
    ) -> dict | None:
        if not isinstance(node.func, ast.Name):
            self._forbidden(node, "method_or_indirect_call")
        name = node.func.id
        if name == _COMMIT_FUNCTION_NAME:
            # 顶层独立语句的形态已被 _block 截走；能落到表达式求值这里，说明它
            # 出现在赋值右侧 / 实参 / 分支条件里。它没有返回值，也不该被当成值。
            self._error(
                node,
                "program_forbidden_construct",
                f"{_COMMIT_FUNCTION_NAME}() is a statement, not a value",
                construct="commit_not_a_statement",
            )
        if name in self._visible:
            spec = self._visible[name]
            if node.args:
                self._forbidden(node, "program_function_positional_args")
            if any(keyword.arg is None for keyword in node.keywords):
                self._forbidden(node, "starred_arguments")
            if in_loop or in_comp:
                self._forbidden(node, "effect_in_loop_or_comprehension")
            seen: set[str] = set()
            value_schemas: dict[str, dict | None] = {}
            for keyword in node.keywords:
                assert keyword.arg is not None
                if keyword.arg in seen:
                    self._forbidden(keyword, "duplicate_keyword")
                seen.add(keyword.arg)
                value_schemas[keyword.arg] = self._expr(
                    keyword.value,
                    env,
                    in_loop=in_loop,
                    in_comp=in_comp,
                )
            self._validate_program_arguments(node, spec, value_schemas)
            occurrence = self._call_occurrences.get(name, 0) + 1
            self._call_occurrences[name] = occurrence
            line, column = _source_position(node)
            self.call_sites.append(
                ProgramCallSite(
                    call_site=f"{line}:{column}:{name}:{occurrence}",
                    name=name,
                    program_kind=spec.program_kind,
                    line=line,
                    column=column,
                    occurrence=occurrence,
                )
            )
            count = self._effect_counts.get(name, 0) + 1
            self._effect_counts[name] = count
            if count > spec.max_call_sites:
                _raise_quota(
                    node,
                    f"effect_call_sites:{name}",
                    count,
                    spec.max_call_sites,
                )
            return _schema_with_origin(spec.result_schema, spec.name)
        if self._registry.spec(name) is not None:
            self._unknown_name(node, name)
        if name not in SAFE_BUILTINS:
            self._unknown_name(node, name)
        if any(isinstance(argument, ast.Starred) for argument in node.args) or any(
            keyword.arg is None for keyword in node.keywords
        ):
            self._forbidden(node, "starred_arguments")
        for argument in node.args:
            self._expr(
                argument,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
        for keyword in node.keywords:
            self._expr(
                keyword.value,
                env,
                in_loop=in_loop,
                in_comp=in_comp,
            )
        return _builtin_result_schema(name)

    def _validate_program_arguments(
        self,
        node: ast.Call,
        spec: ProgramFunctionSpec,
        value_schemas: dict[str, dict | None],
    ) -> None:
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        schema = _select_call_schema(spec.arguments_schema, keywords)
        properties = _schema_properties(schema)
        declared = set(properties)
        reserved = _RESERVED_EFFECT_ARGUMENTS - declared
        allowed = declared | reserved
        unknown = sorted(set(keywords) - allowed)
        if unknown:
            self._error(
                node,
                "program_forbidden_construct",
                f"unknown keyword(s) for {spec.name}: {unknown}",
                construct="unknown_keyword",
                function=spec.name,
            )
        for field_name in sorted(set(keywords) & reserved):
            value_schema = value_schemas.get(field_name)
            if value_schema is not None and not (
                _schema_allows_type(value_schema, "string")
                or _schema_allows_type(value_schema, "null")
            ):
                self._error(
                    keywords[field_name],
                    "program_forbidden_construct",
                    f"{field_name} must be a string or null",
                    construct="reserved_argument_type",
                    function=spec.name,
                    field=field_name,
                )
        missing = sorted(set(schema.get("required") or []) - set(keywords))
        if missing:
            self._error(
                node,
                "program_forbidden_construct",
                f"missing required keyword(s) for {spec.name}: {missing}",
                construct="missing_keyword",
                function=spec.name,
            )
        for field_name, value in keywords.items():
            field_schema = properties.get(field_name)
            if (
                field_schema is not None
                and isinstance(value, ast.Constant)
                and not _literal_matches_schema(value.value, field_schema)
            ):
                self._error(
                    value,
                    "program_forbidden_construct",
                    f"literal value for {spec.name}.{field_name} violates schema",
                    construct="literal_schema_mismatch",
                    function=spec.name,
                    field=field_name,
                )

    def _commit(self, node: ast.Call, *, top_level: bool) -> None:
        """``execute_program(program_hash="…")`` 的静态形状。

        只在模块顶层、作为独立语句成立。参数唯一且必须是 12 位 hex 字面量
        ``program_hash``——运行期不去解析变量，"这一拍要执行哪段代码"必须在
        派发之前就是确定的。
        """
        if not top_level:
            self._forbidden(node, "commit_not_top_level")
        if node.args:
            self._forbidden(node, "program_function_positional_args")
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        if len(node.keywords) != 1 or set(keywords) != {"program_hash"}:
            self._error(
                node,
                "program_forbidden_construct",
                f"{_COMMIT_FUNCTION_NAME} takes exactly one keyword argument: "
                'program_hash="<12位哈希>"',
                construct="commit_signature",
                function=_COMMIT_FUNCTION_NAME,
            )
        value = keywords["program_hash"]
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            self._error(
                value,
                "program_forbidden_construct",
                f"{_COMMIT_FUNCTION_NAME} program_hash must be a string literal",
                construct="commit_program_hash_not_literal",
                function=_COMMIT_FUNCTION_NAME,
            )
        text = value.value
        if _PROGRAM_HASH_PATTERN.fullmatch(text) is None:
            self._error(
                value,
                "program_forbidden_construct",
                f"{_COMMIT_FUNCTION_NAME} program_hash must be the 12-hex code "
                "asset hash copied from a program row in the timeline",
                construct="commit_program_hash_malformed",
                function=_COMMIT_FUNCTION_NAME,
                program_hash=text[:64],
            )
        self._commit_count += 1
        if self._commit_count > _COMMIT_MAX_CALL_SITES:
            _raise_quota(
                node,
                f"effect_call_sites:{_COMMIT_FUNCTION_NAME}",
                self._commit_count,
                _COMMIT_MAX_CALL_SITES,
            )
        self.commit_program_hash = text
        # 物理行范围（含首尾），供 preflight 从源码里剥掉这一层。函数壳占一行，
        # 因此 AST 行号比模型源码大 1。
        self.commit_lines = (
            node.lineno - 1,
            (node.end_lineno or node.lineno) - 1,
        )

    def _validate_assignment_name(self, node: ast.AST, name: str) -> None:
        self._validate_public_name(node, name)
        if name in SAFE_BUILTINS or name.startswith("__program_"):
            self._forbidden(node, "reserved_assignment_name")
        if name == _COMMIT_FUNCTION_NAME:
            self._forbidden(node, "reserved_assignment_name")
        if self._registry.spec(name) is not None:
            self._forbidden(node, "reserved_assignment_name")

    def _validate_public_name(self, node: ast.AST, name: str) -> None:
        if not name.isidentifier() or name.startswith("_"):
            self._forbidden(node, "private_or_invalid_name")

    def _unknown_name(self, node: ast.AST, name: str) -> NoReturn:
        self._error(
            node,
            "program_unknown_name",
            f"name {name!r} is not available in scope {self._scope!r}",
            name=name,
        )

    def _forbidden(self, node: ast.AST, construct: str) -> NoReturn:
        self._error(
            node,
            "program_forbidden_construct",
            f"forbidden construct: {construct}",
            construct=construct,
        )

    def _error(
        self,
        node: ast.AST | None,
        error_kind: str,
        message: str,
        **details: Any,
    ) -> NoReturn:
        line, column = _source_position(node)
        raise ProgramPreflightError(
            ProgramErrorInfo(
                error_kind,
                message[:500],
                line=line if node is not None else None,
                column=column if node is not None else None,
                details=details,
            )
        )


class _Instrumenter(ast.NodeTransformer):
    def __init__(self, call_sites: tuple[ProgramCallSite, ...]) -> None:
        self._sites = {
            (site.line, site.column, site.name): site.call_site for site in call_sites
        }

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.body = self._instrument_block(node.body)
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        node.test = self.visit(node.test)
        node.body = self._instrument_block(node.body)
        node.orelse = self._instrument_block(node.orelse)
        return node

    def visit_For(self, node: ast.For) -> ast.AST:
        node.target = self.visit(node.target)
        node.iter = ast.copy_location(
            ast.Call(
                func=ast.Name(id="__program_iter__", ctx=ast.Load()),
                args=[self.visit(node.iter)],
                keywords=[],
            ),
            node.iter,
        )
        node.body = self._instrument_block(node.body)
        node.orelse = []
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        original_name = node.func.id if isinstance(node.func, ast.Name) else None
        line, column = _source_position(node)
        node = self.generic_visit(node)
        if original_name is not None:
            call_site = self._sites.get((line, column, original_name))
            if call_site is not None:
                node.keywords.append(
                    ast.keyword(
                        arg="__program_call_site__",
                        value=ast.Constant(call_site),
                    )
                )
                return ast.copy_location(ast.Await(value=node), node)
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        operator = {
            ast.Add: "add",
            ast.Sub: "sub",
            ast.Mult: "mul",
            ast.Div: "div",
            ast.FloorDiv: "floordiv",
            ast.Mod: "mod",
        }[type(node.op)]
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__program_binop__", ctx=ast.Load()),
                args=[
                    ast.Constant(operator),
                    self.visit(node.left),
                    self.visit(node.right),
                ],
                keywords=[],
            ),
            node,
        )

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        value = self.generic_visit(node)
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__program_string__", ctx=ast.Load()),
                args=[value],
                keywords=[],
            ),
            node,
        )

    def visit_FormattedValue(self, node: ast.FormattedValue) -> ast.AST:
        value = ast.Call(
            func=ast.Name(id="__program_format__", ctx=ast.Load()),
            args=[self.visit(node.value)],
            keywords=[],
        )
        return ast.copy_location(
            ast.FormattedValue(
                value=value,
                conversion=-1,
                format_spec=None,
            ),
            node,
        )

    def visit_List(self, node: ast.List) -> ast.AST:
        value = self.generic_visit(node)
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__program_container__", ctx=ast.Load()),
                args=[value],
                keywords=[],
            ),
            node,
        )

    def visit_Dict(self, node: ast.Dict) -> ast.AST:
        value = self.generic_visit(node)
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__program_container__", ctx=ast.Load()),
                args=[value],
                keywords=[],
            ),
            node,
        )

    def visit_ListComp(self, node: ast.ListComp) -> ast.AST:
        node = self.generic_visit(node)
        node.generators[0].iter = ast.Call(
            func=ast.Name(id="__program_iter__", ctx=ast.Load()),
            args=[node.generators[0].iter],
            keywords=[],
        )
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__program_container__", ctx=ast.Load()),
                args=[node],
                keywords=[],
            ),
            node,
        )

    def visit_DictComp(self, node: ast.DictComp) -> ast.AST:
        node = self.generic_visit(node)
        node.generators[0].iter = ast.Call(
            func=ast.Name(id="__program_iter__", ctx=ast.Load()),
            args=[node.generators[0].iter],
            keywords=[],
        )
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__program_container__", ctx=ast.Load()),
                args=[node],
                keywords=[],
            ),
            node,
        )

    def _instrument_block(self, statements: list[ast.stmt]) -> list[ast.stmt]:
        output: list[ast.stmt] = []
        for statement in statements:
            transformed = self.visit(statement)
            if transformed is None:
                continue
            step = ast.copy_location(
                ast.Expr(
                    value=ast.Call(
                        func=ast.Name(id="__program_step__", ctx=ast.Load()),
                        args=[],
                        keywords=[],
                    )
                ),
                statement,
            )
            output.append(step)
            if isinstance(transformed, list):
                output.extend(transformed)
            else:
                output.append(transformed)
        return output


def _is_commit_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == _COMMIT_FUNCTION_NAME
    )


def _source_position(node: ast.AST | None) -> tuple[int, int]:
    if node is None:
        return 1, 0
    line = max(1, int(getattr(node, "lineno", 2)) - 1)
    column = max(
        0,
        int(getattr(node, "col_offset", len(_SHELL_INDENT))) - len(_SHELL_INDENT),
    )
    return line, column


def _raise_quota(
    node: ast.AST | None,
    quota: str,
    actual: int,
    maximum: int,
) -> None:
    line, column = _source_position(node)
    raise ProgramPreflightError(
        ProgramErrorInfo(
            "program_quota_exceeded",
            f"program quota {quota} exceeded: {actual} > {maximum}",
            line=line if node is not None else None,
            column=column if node is not None else None,
            details={"quota": quota, "actual": actual, "max": maximum},
        )
    )


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


def _schema_allows_type(schema: dict | None, expected: str) -> bool:
    types = _schema_types(schema)
    return not types or expected in types


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


def _schema_property(schema: dict | None, field_name: str) -> tuple[dict | None, bool]:
    if schema is None:
        return None, False
    properties = _schema_properties(schema)
    if properties:
        return (
            _schema_with_origin(
                properties.get(field_name),
                _schema_origin(schema),
            ),
            True,
        )
    if _schema_allows_type(schema, "object"):
        return None, True
    return None, False


def _array_item_schema(schema: dict | None) -> dict | None:
    if not isinstance(schema, dict):
        return None
    if "array" in _schema_types(schema):
        items = schema.get("items")
        return _schema_with_origin(
            items if isinstance(items, dict) else None,
            _schema_origin(schema),
        )
    for keyword in ("oneOf", "anyOf"):
        for branch in schema.get(keyword) or []:
            item = _array_item_schema(branch)
            if item is not None:
                return item
    return None


def _merge_schema(left: dict | None, right: dict | None) -> dict | None:
    if left is None:
        return right
    if right is None:
        return left
    if left == right:
        return left
    branches: list[dict] = []
    for schema in (left, right):
        if "oneOf" in schema:
            for branch in schema["oneOf"]:
                if branch not in branches:
                    branches.append(branch)
        elif schema not in branches:
            branches.append(schema)
    merged = {"oneOf": branches}
    left_origin = _schema_origin(left)
    if left_origin and left_origin == _schema_origin(right):
        merged[_SCHEMA_ORIGIN_KEY] = left_origin
    return merged


def _schema_with_origin(schema: dict | None, origin: str | None) -> dict | None:
    if schema is None or not origin:
        return schema
    if schema.get(_SCHEMA_ORIGIN_KEY) == origin:
        return schema
    return {**schema, _SCHEMA_ORIGIN_KEY: origin}


def _schema_origin(schema: dict | None) -> str | None:
    if not isinstance(schema, dict):
        return None
    value = schema.get(_SCHEMA_ORIGIN_KEY)
    return value if isinstance(value, str) and value else None


def _root_call_name(node: ast.expr) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _select_call_schema(schema: dict, keywords: dict[str | None, ast.expr]) -> dict:
    action = keywords.get("action")
    if isinstance(action, ast.Constant):
        for branch in schema.get("oneOf") or []:
            properties = branch.get("properties") or {}
            action_schema = properties.get("action") or {}
            if action_schema.get("const") == action.value:
                return branch
    if schema.get("oneOf") and not schema.get("properties"):
        return {
            **schema,
            "properties": _schema_properties(schema),
            "required": list(
                set.intersection(
                    *[
                        set(branch.get("required") or [])
                        for branch in schema.get("oneOf") or []
                    ]
                )
                if schema.get("oneOf")
                else []
            ),
        }
    return schema


def _literal_matches_schema(  # noqa: C901, PLR0911
    value: Any, schema: dict
) -> bool:
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    allowed = _schema_types(schema)
    if allowed:
        actual = _json_type(value)
        if actual not in allowed and not (actual == "integer" and "number" in allowed):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            return False
    return True


def _json_type(value: Any) -> str:  # noqa: PLR0911
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _literal_schema(value: Any) -> dict:
    return {"type": _json_type(value)}


def _binop_schema(
    operator: ast.operator,
    left: dict | None,
    right: dict | None,
) -> dict | None:
    if isinstance(operator, ast.Add):
        for kind in ("string", "array", "integer", "number"):
            if _schema_allows_type(left, kind) and _schema_allows_type(right, kind):
                return {"type": kind}
    if isinstance(operator, (ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod)):
        if _schema_allows_type(left, "integer") and _schema_allows_type(
            right, "integer"
        ):
            return {"type": "integer"}
        return {"type": "number"}
    if isinstance(operator, ast.Div):
        return {"type": "number"}
    return None


def _builtin_result_schema(name: str) -> dict | None:
    if name in {"len", "int", "sum"}:
        return {"type": "integer"}
    if name in {"any", "all", "bool"}:
        return {"type": "boolean"}
    if name in {"str", "join"}:
        return {"type": "string"}
    if name in {"list", "sorted"}:
        return {"type": "array", "items": {}}
    if name == "dict":
        return {"type": "object", "properties": {}}
    return None


__all__ = [
    "MAX_AST_NODES",
    "MAX_CONTAINER_ELEMENTS",
    "MAX_EFFECT_CALL_SITES",
    "MAX_SOURCE_CHARS",
    "MAX_STRING_LENGTH",
    "MAX_SYNTAX_DEPTH",
    "SAFE_BUILTINS",
    "PreflightResult",
    "ProgramCallSite",
    "ProgramErrorInfo",
    "ProgramPreflightError",
    "build_executable_tree",
    "preflight",
    "strip_outer_fence",
]
