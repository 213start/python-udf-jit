from __future__ import annotations

import ast
import atexit
import builtins
import dis
import inspect
import os
import textwrap
import types
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Mapping

from python_udf_jit.compiler.identity import code_identity
from python_udf_jit.compiler.typed_frontend import (
    TypedCaptureError,
    TypedEntryGuard,
    capture_typed_loop,
)
from python_udf_jit.compiler.typed_ir import EXACT_UNICODE
from python_udf_jit.provider.scalar_python.typed_loop import (
    CinderXTypedLoopBackend,
    CompileStatus,
    RuntimeFeedback,
    TypedGuardMiss,
    TypedLoopBackend,
    TypedRegionCompileRequest,
    TypedRegionCompiler,
)


_PORTABLE_CONSTANT_TYPES = {type(None), bool, int, float, str, bytes}
_INPUT = object()


@dataclass(frozen=True)
class TypedLoopInvocation:
    handled: bool
    value: object | None = None
    reason_code: str = ""
    terminal: bool = False


@dataclass(frozen=True)
class TypedLoopRuntimeStats:
    calls: int
    compile_attempts: int
    compile_successes: int
    hits: int
    fallbacks: int
    guard_misses: int
    reason_code: str
    wrapper_depth: int
    semantic_hash: str
    execution_mode: str

    def to_document(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "compile_attempts": self.compile_attempts,
            "compile_successes": self.compile_successes,
            "execution_mode": self.execution_mode,
            "fallbacks": self.fallbacks,
            "guard_misses": self.guard_misses,
            "hits": self.hits,
            "reason_code": self.reason_code,
            "schema_version": 1,
            "semantic_hash": self.semantic_hash,
            "wrapper_depth": self.wrapper_depth,
        }


@dataclass(frozen=True)
class _LiveBinding:
    function: types.FunctionType
    kind: str
    name: str
    expected: object
    index: int | None = None
    identity: bool = False

    def _current(self) -> object:
        function = self.function
        if self.kind == "code":
            return function.__code__
        if self.kind == "closure":
            closure = function.__closure__ or ()
            if self.index is None or self.index >= len(closure):
                raise LookupError(self.name)
            return closure[self.index].cell_contents
        if self.kind == "global":
            return function.__globals__[self.name]
        if self.kind == "builtin":
            namespace = function.__globals__.get("__builtins__", builtins)
            if isinstance(namespace, dict):
                return namespace[self.name]
            return getattr(namespace, self.name)
        if self.kind == "positional_default":
            defaults = function.__defaults__ or ()
            names = function.__code__.co_varnames[: function.__code__.co_argcount]
            layout = names[-len(defaults) :] if defaults else ()
            return defaults[layout.index(self.name)]
        if self.kind == "keyword_default":
            return (function.__kwdefaults__ or {})[self.name]
        if self.kind == "attribute":
            return getattr(function, self.name)
        raise LookupError(self.kind)

    def matches(self) -> bool:
        try:
            current = self._current()
        except (AttributeError, KeyError, LookupError, ValueError):
            return False
        if self.identity:
            return current is self.expected
        if type(current) is not type(self.expected):
            return False
        if type(current) is float:
            return current.hex() == self.expected.hex()  # type: ignore[union-attr]
        return current == self.expected


@dataclass(frozen=True)
class _WrapperGuard:
    bindings: tuple[_LiveBinding, ...]

    def matches(self) -> bool:
        return all(binding.matches() for binding in self.bindings)


@dataclass(frozen=True)
class ResolvedTypedLoopCallable:
    function: types.FunctionType
    bound_arguments: Mapping[str, object]
    wrapper_guard: _WrapperGuard
    wrapper_depth: int


class _TypedLoopDiagnosticError(RuntimeError):
    """An explicitly requested typed-loop diagnostic could not run."""


def _function_node(function: types.FunctionType) -> ast.FunctionDef:
    try:
        lines, _ = inspect.getsourcelines(function)
    except (OSError, TypeError) as error:
        raise TypedCaptureError("source_unavailable") from error
    tree = ast.parse(textwrap.dedent("".join(lines)))
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef)
    ]
    if len(functions) != 1 or functions[0].name != function.__name__:
        raise TypedCaptureError("thin_wrapper_source_unsupported")
    return functions[0]


def _strip_docstring(statements: list[ast.stmt]) -> list[ast.stmt]:
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        return statements[1:]
    return statements


def _receiver_trampoline_bindings(
    function: types.FunctionType,
) -> tuple[types.FunctionType, tuple[_LiveBinding, ...]] | None:
    """Prove the Daft receiver shim is exactly ``wrapped(*args, **kwargs)``."""

    wrapped = vars(function).get("__wrapped__")
    if not isinstance(wrapped, types.FunctionType):
        return None
    code = function.__code__
    if (
        code.co_argcount != 1
        or code.co_kwonlyargcount != 0
        or not code.co_flags & inspect.CO_VARARGS
        or not code.co_flags & inspect.CO_VARKEYWORDS
        or function.__defaults__
        or function.__kwdefaults__
    ):
        return None
    closure = function.__closure__ or ()
    matches: list[tuple[int, str]] = []
    try:
        for index, (name, cell) in enumerate(
            zip(code.co_freevars, closure, strict=True)
        ):
            if cell.cell_contents is wrapped:
                matches.append((index, name))
    except (ValueError, TypeError):
        return None
    if len(matches) != 1:
        return None
    index, closure_name = matches[0]
    variadic_offset = code.co_argcount + code.co_kwonlyargcount
    if len(code.co_varnames) <= variadic_offset + 1:
        return None
    vararg_name = code.co_varnames[variadic_offset]
    keyword_name = code.co_varnames[variadic_offset + 1]
    instructions = [
        instruction
        for instruction in dis.get_instructions(function)
        if instruction.opname not in {"CACHE", "COPY_FREE_VARS", "NOP", "RESUME"}
    ]
    if len(instructions) != 8:
        return None
    if instructions[0].opname == "PUSH_NULL":
        callee = instructions[1]
    elif instructions[1].opname == "PUSH_NULL":
        callee = instructions[0]
    else:
        return None
    payload = instructions[2:]
    payload_names = tuple(
        (
            "LOAD_FAST"
            if instruction.opname == "LOAD_FAST_BORROW"
            else instruction.opname
        )
        for instruction in payload
    )
    if (
        callee.opname != "LOAD_DEREF"
        or callee.argval != closure_name
        or payload_names
        != (
            "LOAD_FAST",
            "BUILD_MAP",
            "LOAD_FAST",
            "DICT_MERGE",
            "CALL_FUNCTION_EX",
            "RETURN_VALUE",
        )
        or payload[0].argval != vararg_name
        or payload[1].arg != 0
        or payload[2].argval != keyword_name
        or payload[3].arg != 1
        or payload[4].arg not in {None, 1}
    ):
        return None
    return wrapped, (
        _LiveBinding(function, "code", "__code__", code, identity=True),
        _LiveBinding(
            function,
            "closure",
            closure_name,
            wrapped,
            index=index,
            identity=True,
        ),
        _LiveBinding(
            function,
            "attribute",
            "__wrapped__",
            wrapped,
            identity=True,
        ),
    )


def _default_bindings(
    function: types.FunctionType,
) -> dict[str, tuple[object, _LiveBinding]]:
    values: dict[str, tuple[object, _LiveBinding]] = {}
    defaults = function.__defaults__ or ()
    positional = function.__code__.co_varnames[: function.__code__.co_argcount]
    names = positional[-len(defaults) :] if defaults else ()
    for name, value in zip(names, defaults, strict=True):
        values[name] = (
            value,
            _LiveBinding(function, "positional_default", name, value),
        )
    for name, value in (function.__kwdefaults__ or {}).items():
        values[name] = (
            value,
            _LiveBinding(function, "keyword_default", name, value),
        )
    return values


def _live_name(
    function: types.FunctionType,
    name: str,
    incoming: Mapping[str, object],
) -> tuple[object, _LiveBinding | None]:
    if name in incoming:
        return incoming[name], None
    defaults = _default_bindings(function)
    if name in defaults:
        return defaults[name]
    freevars = function.__code__.co_freevars
    if name in freevars:
        index = freevars.index(name)
        closure = function.__closure__ or ()
        if index >= len(closure):
            raise TypedCaptureError("thin_wrapper_closure_missing", name)
        try:
            value = closure[index].cell_contents
        except ValueError as error:
            raise TypedCaptureError("thin_wrapper_closure_empty", name) from error
        return value, _LiveBinding(
            function,
            "closure",
            name,
            value,
            index=index,
            identity=isinstance(value, types.FunctionType),
        )
    if name in function.__globals__:
        value = function.__globals__[name]
        return value, _LiveBinding(
            function,
            "global",
            name,
            value,
            identity=isinstance(value, types.FunctionType),
        )
    namespace = function.__globals__.get("__builtins__", builtins)
    try:
        value = namespace[name] if isinstance(namespace, dict) else getattr(namespace, name)
    except (KeyError, AttributeError) as error:
        raise TypedCaptureError("thin_wrapper_name_unbound", name) from error
    return value, _LiveBinding(
        function,
        "builtin",
        name,
        value,
        identity=True,
    )


def _constant_expression(
    function: types.FunctionType,
    expression: ast.expr,
    *,
    input_name: str,
    incoming: Mapping[str, object],
) -> tuple[object, _LiveBinding | None]:
    if isinstance(expression, ast.Name) and expression.id == input_name:
        return _INPUT, None
    if isinstance(expression, ast.Constant):
        value = expression.value
        if type(value) not in _PORTABLE_CONSTANT_TYPES:
            raise TypedCaptureError("thin_wrapper_constant_unsupported")
        return value, None
    if (
        isinstance(expression, ast.UnaryOp)
        and isinstance(expression.op, (ast.USub, ast.UAdd))
        and isinstance(expression.operand, ast.Constant)
        and type(expression.operand.value) in {int, float}
    ):
        value = expression.operand.value
        return (-value if isinstance(expression.op, ast.USub) else +value), None
    if isinstance(expression, ast.Name):
        value, binding = _live_name(function, expression.id, incoming)
        if type(value) not in _PORTABLE_CONSTANT_TYPES:
            raise TypedCaptureError(
                "thin_wrapper_constant_unsupported",
                expression.id,
            )
        return value, binding
    raise TypedCaptureError("thin_wrapper_argument_unsupported")


@dataclass(frozen=True)
class _UnwrappedCall:
    function: types.FunctionType
    bound_arguments: dict[str, object]
    bindings: tuple[_LiveBinding, ...]


def _unwrap_thin_call(
    function: types.FunctionType,
    incoming: Mapping[str, object],
) -> _UnwrappedCall | None:
    node = _function_node(function)
    statements = _strip_docstring(list(node.body))
    if (
        len(statements) != 1
        or not isinstance(statements[0], ast.Return)
        or not isinstance(statements[0].value, ast.Call)
    ):
        return None
    positional = [*node.args.posonlyargs, *node.args.args]
    if not positional:
        return None
    input_name = positional[0].arg
    call = statements[0].value
    if not isinstance(call.func, ast.Name):
        return None
    callee, callee_binding = _live_name(function, call.func.id, incoming)
    if not isinstance(callee, types.FunctionType):
        return None

    checks: list[_LiveBinding] = [
        _LiveBinding(function, "code", "__code__", function.__code__, identity=True)
    ]
    if callee_binding is not None:
        checks.append(callee_binding)
    positional_values: list[object] = []
    for expression in call.args:
        value, binding = _constant_expression(
            function,
            expression,
            input_name=input_name,
            incoming=incoming,
        )
        positional_values.append(value)
        if binding is not None:
            checks.append(binding)
    keyword_values: dict[str, object] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            return None
        value, binding = _constant_expression(
            function,
            keyword.value,
            input_name=input_name,
            incoming=incoming,
        )
        keyword_values[keyword.arg] = value
        if binding is not None:
            checks.append(binding)
    try:
        bound = inspect.signature(callee).bind(
            *positional_values,
            **keyword_values,
        )
    except TypeError:
        return None
    input_parameters = [
        name for name, value in bound.arguments.items() if value is _INPUT
    ]
    if len(input_parameters) != 1:
        return None
    parameters = tuple(inspect.signature(callee).parameters.values())
    if not parameters or input_parameters[0] != parameters[0].name:
        return None
    bound_arguments: dict[str, object] = {}
    for name, value in bound.arguments.items():
        if value is _INPUT:
            continue
        if type(value) not in _PORTABLE_CONSTANT_TYPES:
            return None
        bound_arguments[name] = value
    return _UnwrappedCall(callee, bound_arguments, tuple(checks))


def resolve_typed_loop_callable(
    function: Callable[..., object],
    *,
    max_wrapper_depth: int = 8,
) -> ResolvedTypedLoopCallable:
    if not isinstance(function, types.FunctionType):
        raise TypedCaptureError("function_required")
    current = function
    incoming: dict[str, object] = {}
    bindings: list[_LiveBinding] = []
    seen: set[int] = set()
    depth = 0
    while depth < max_wrapper_depth:
        if id(current) in seen:
            raise TypedCaptureError("thin_wrapper_cycle")
        seen.add(id(current))
        receiver_trampoline = _receiver_trampoline_bindings(current)
        if receiver_trampoline is not None:
            wrapped, receiver_bindings = receiver_trampoline
            bindings.extend(receiver_bindings)
            current = wrapped
            depth += 1
            continue
        if "__wrapped__" in vars(current):
            return ResolvedTypedLoopCallable(
                current,
                dict(incoming),
                _WrapperGuard(tuple(bindings)),
                depth,
            )
        unwrapped = _unwrap_thin_call(current, incoming)
        if unwrapped is None:
            return ResolvedTypedLoopCallable(
                current,
                dict(incoming),
                _WrapperGuard(tuple(bindings)),
                depth,
            )
        bindings.extend(unwrapped.bindings)
        current = unwrapped.function
        incoming = unwrapped.bound_arguments
        depth += 1
    raise TypedCaptureError("thin_wrapper_depth_limit")


def _positive_int_environment(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 1 <= value <= maximum else default


class WorkerTypedLoopAdapter:
    """Per-process lazy compiler for a scalar callable captured by Daft."""

    def __init__(
        self,
        original_callable: Callable[..., object],
        *,
        candidate_id: str,
        call_threshold: int = 8,
        backend: TypedLoopBackend | None = None,
    ) -> None:
        if call_threshold <= 0:
            raise ValueError("typed_loop_call_threshold_invalid")
        self.owner_pid = os.getpid()
        self._original_callable = original_callable
        self._candidate_id = candidate_id
        self._call_threshold = call_threshold
        self._backend = backend or CinderXTypedLoopBackend()
        self._has_receiver_trampoline = (
            isinstance(original_callable, types.FunctionType)
            and _receiver_trampoline_bindings(original_callable) is not None
        )
        self._lock = RLock()
        self._calls = 0
        self._compile_attempts = 0
        self._compile_successes = 0
        self._hits = 0
        self._fallbacks = 0
        self._guard_misses = 0
        self._reason_code = "runtime_call_threshold"
        self._wrapper_depth = 0
        self._semantic_hash = ""
        self._execution_mode = ""
        self._terminal = False
        self._variant = None
        self._entry_guard: TypedEntryGuard | None = None
        self._wrapper_guard = _WrapperGuard(())
        self._diagnostic_runtime = None
        self._diagnostic_finalized = False
        self._diagnostic_hit_threshold = _positive_int_environment(
            "UDFJIT_TYPED_LOOP_DIAGNOSTIC_HITS",
            64,
            1_000_000,
        )

    def _logical_inputs(
        self,
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
    ) -> tuple[object, ...] | None:
        if kwargs:
            return None
        if self._has_receiver_trampoline:
            return args[1:] if len(args) == 2 else None
        return args if len(args) == 1 else None

    def snapshot(self) -> TypedLoopRuntimeStats:
        with self._lock:
            return TypedLoopRuntimeStats(
                self._calls,
                self._compile_attempts,
                self._compile_successes,
                self._hits,
                self._fallbacks,
                self._guard_misses,
                self._reason_code,
                self._wrapper_depth,
                self._semantic_hash,
                self._execution_mode,
            )

    def _diagnostic_sink(self, function: types.FunctionType):
        if os.environ.get("UDFJIT_DIAGNOSTICS", "off") != "full":
            return None
        try:
            from pathlib import Path

            from python_udf_jit.diagnostics.config import (
                DiagnosticRuntimeContext,
                resolve_diagnostic_policy,
            )
            from python_udf_jit.diagnostics.worker_runtime import (
                WorkerDiagnosticRuntime,
            )

            policy = resolve_diagnostic_policy(
                os.environ,
                DiagnosticRuntimeContext(
                    dedicated_worker=(
                        os.environ.get("PYTHONJITUDFDIAGNOSTICS") == "1"
                    ),
                    workspace_root=Path.cwd(),
                    home_root=Path.home(),
                ),
            )
            selector_kind, _, selector_value = policy.selector.partition(":")
            selected = (
                selector_kind == "candidate"
                and self._candidate_id == selector_value
            ) or (
                selector_kind == "udf"
                and code_identity(function).sha256.startswith(selector_value)
            )
            if not selected:
                return None
            runtime = WorkerDiagnosticRuntime(
                policy,
                run_id=os.environ.get("UDFJIT_RUN_ID", "typed-loop-worker"),
                runtime_mode="auto",
                process_key=(
                    f"typed-loop-{self._candidate_id[:12]}-{os.getpid()}"
                ),
                process_id=os.getpid(),
                user_function=function,
            )
            self._diagnostic_runtime = runtime
            atexit.register(self._finalize_diagnostics)
            return runtime
        except Exception as error:
            raise _TypedLoopDiagnosticError(
                "typed_loop_diagnostics_initialize_failed:"
                f"{type(error).__name__}:{error}"
            ) from error

    def _compile_locked(self) -> None:
        self._compile_attempts += 1
        try:
            resolved = resolve_typed_loop_callable(self._original_callable)
            captured = capture_typed_loop(
                resolved.function,
                input_types=(EXACT_UNICODE,),
                bound_arguments=resolved.bound_arguments,
                allow_guarded_region=True,
            )
            diagnostic_sink = self._diagnostic_sink(resolved.function)
            decision = TypedRegionCompiler(
                self._backend,
                call_threshold=self._call_threshold,
                negative_ttl_ns=1_000_000_000,
                diagnostic_sink=diagnostic_sink,
            ).compile(
                TypedRegionCompileRequest(
                    captured.module,
                    RuntimeFeedback(self._calls, self._guard_misses),
                    captured.analysis.to_documents(),
                    captured.runtime_guard,
                )
            )
        except _TypedLoopDiagnosticError:
            raise
        except TypedCaptureError as error:
            self._reason_code = error.reason_code
            self._terminal = True
            return
        except Exception as error:
            self._reason_code = f"typed_loop_compile_failed:{type(error).__name__}"
            self._terminal = True
            return
        self._wrapper_depth = resolved.wrapper_depth
        self._semantic_hash = captured.module.semantic_hash
        if decision.status is not CompileStatus.COMPILED or decision.variant is None:
            self._reason_code = decision.reason_code
            self._terminal = decision.status is not CompileStatus.DEFERRED
            return
        self._variant = decision.variant
        self._entry_guard = captured.entry_guard
        self._wrapper_guard = resolved.wrapper_guard
        self._compile_successes += 1
        self._execution_mode = decision.variant.execution_mode
        self._reason_code = decision.reason_code

    def _fallback(
        self,
        reason_code: str,
        *,
        terminal: bool = False,
    ) -> TypedLoopInvocation:
        with self._lock:
            self._fallbacks += 1
            self._reason_code = reason_code
        return TypedLoopInvocation(
            False,
            reason_code=reason_code,
            terminal=terminal,
        )

    def invoke(
        self,
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
    ) -> TypedLoopInvocation:
        if os.getpid() != self.owner_pid:
            return self._fallback("typed_loop_process_mismatch")
        logical_inputs = self._logical_inputs(args, kwargs)
        if logical_inputs is None:
            return self._fallback("typed_loop_input_shape")
        with self._lock:
            self._calls += 1
            if self._variant is None and not self._terminal:
                if self._calls >= self._call_threshold:
                    self._compile_locked()
                else:
                    self._fallbacks += 1
                    self._reason_code = "runtime_call_threshold"
                    return TypedLoopInvocation(
                        False,
                        reason_code="runtime_call_threshold",
                    )
            variant = self._variant
            entry_guard = self._entry_guard
            wrapper_guard = self._wrapper_guard
            terminal_reason = self._reason_code
        if variant is None:
            return self._fallback(
                terminal_reason,
                terminal=self._terminal,
            )
        if not wrapper_guard.matches():
            with self._lock:
                self._guard_misses += 1
            return self._fallback("thin_wrapper_guard_miss")
        if entry_guard is not None and not entry_guard.matches(logical_inputs):
            with self._lock:
                self._guard_misses += 1
            return self._fallback("typed_entry_guard_miss")
        try:
            value = variant(*logical_inputs)
        except TypedGuardMiss:
            with self._lock:
                self._guard_misses += 1
            return self._fallback("runtime_dependency_guard_miss")
        with self._lock:
            self._hits += 1
            self._reason_code = "typed_loop_hit"
            should_finalize = self._hits >= self._diagnostic_hit_threshold
        if should_finalize:
            self._finalize_diagnostics()
        return TypedLoopInvocation(True, value, "typed_loop_hit")

    def _finalize_diagnostics(self) -> None:
        with self._lock:
            if self._diagnostic_finalized or self._diagnostic_runtime is None:
                return
            self._diagnostic_finalized = True
            runtime = self._diagnostic_runtime
            document = self.snapshot().to_document()
        try:
            runtime.record_typed_runtime_summary(document)
            runtime.finalize()
        except Exception as error:
            raise _TypedLoopDiagnosticError(
                "typed_loop_diagnostics_finalize_failed:"
                f"{type(error).__name__}:{error}"
            ) from error


def build_worker_typed_loop_adapter(wrapper: Any) -> WorkerTypedLoopAdapter:
    return WorkerTypedLoopAdapter(
        wrapper.original_callable,
        candidate_id=wrapper.candidate_id,
        call_threshold=_positive_int_environment(
            "UDFJIT_TYPED_LOOP_CALL_THRESHOLD",
            8,
            1_000_000,
        ),
    )
