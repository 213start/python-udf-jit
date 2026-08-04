"""Discover row-invariant calls using behavior and exact-type facts.

The frontend never memoizes a business operation.  It proves that a Python
helper is read-only, that its argument is invariant across rows, and exports
only dependency metadata.  CinderX owns the guarded cache and its control
flow.
"""

from __future__ import annotations

import ast
import builtins
import functools
import inspect
import json
import os
import textwrap
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath

_IMMUTABLE_ARGUMENT_TYPES = {type(None), bool, int, float, str, bytes}
_EXACT_UNICODE = "exact_unicode"
_OPTIONAL_UNICODE = "optional_unicode"
_PATH = "immutable_path"


@dataclass(frozen=True)
class InvariantWatcher:
    kind: str
    owner: object
    key: object

    def descriptor(self) -> tuple[str, object, object]:
        return (self.kind, self.owner, self.key)


@dataclass(frozen=True)
class InvariantCallPlan:
    function: types.FunctionType
    argument: object
    argument_mode: str
    watchers: tuple[InvariantWatcher, ...]
    behavior_patterns: tuple[str, ...]
    result_type: str

    def backend_descriptor(self) -> dict[str, object]:
        return {
            "version": 1,
            "argument_modes": (self.argument_mode,),
            "watchers": tuple(
                watcher.descriptor() for watcher in self.watchers
            ),
        }


@dataclass(frozen=True)
class ValueEntryGuard:
    mode: str
    decoder: object
    key_path: tuple[str, ...]
    observer: object
    expected_path: tuple[str, ...]

    def descriptor(self) -> tuple[object, ...]:
        return (
            self.mode,
            self.decoder,
            self.key_path,
            self.observer,
            self.expected_path,
        )


@dataclass(frozen=True)
class ValueCachePlan:
    function: types.FunctionType
    argument_modes: tuple[str, ...]
    argument_values: tuple[object, ...]
    watchers: tuple[InvariantWatcher, ...]
    behavior_patterns: tuple[str, ...]
    input_type: str
    result_type: str
    capacity: int
    entry_guard: ValueEntryGuard | None = None

    def backend_descriptor(self) -> dict[str, object]:
        return {
            "version": 1,
            "argument_modes": self.argument_modes,
            "argument_values": self.argument_values,
            "capacity": self.capacity,
            "result_mode": self.result_type,
            "entry_guard": (
                None
                if self.entry_guard is None
                else self.entry_guard.descriptor()
            ),
            "watchers": tuple(
                watcher.descriptor() for watcher in self.watchers
            ),
        }


class _UnsupportedInvariantCall(ValueError):
    pass


def _function_node(function: types.FunctionType) -> ast.FunctionDef:
    try:
        lines, _ = inspect.getsourcelines(function)
    except (OSError, TypeError) as error:
        raise _UnsupportedInvariantCall("source_unavailable") from error
    tree = ast.parse(textwrap.dedent("".join(lines)))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(nodes) != 1 or nodes[0].name != function.__name__:
        raise _UnsupportedInvariantCall("source_shape_unsupported")
    return nodes[0]


def _builtins_dict(function: types.FunctionType) -> dict[str, object]:
    namespace = function.__globals__.get("__builtins__", builtins)
    if isinstance(namespace, dict):
        return namespace
    return vars(namespace)


class _WatcherCollector:
    def __init__(self) -> None:
        self._watchers: list[InvariantWatcher] = []
        self._seen: set[tuple[str, int, object]] = set()

    def add(self, kind: str, owner: object, key: object) -> None:
        identity = (kind, id(owner), key)
        if identity in self._seen:
            return
        self._seen.add(identity)
        self._watchers.append(InvariantWatcher(kind, owner, key))

    def dict_item(self, owner: object, key: object) -> None:
        if type(owner) is not dict or type(key) not in {str, bytes, int}:
            raise _UnsupportedInvariantCall("dict_dependency_unsupported")
        self.add("dict_item", owner, key)

    def function_code(self, function: object) -> None:
        if type(function) is types.FunctionType:
            self.add("function_code", function, "__code__")

    def type_attr(self, owner: type[object], key: str) -> None:
        if not isinstance(owner, type) or type(key) is not str:
            raise _UnsupportedInvariantCall("type_dependency_unsupported")
        self.add("type_attr", owner, key)
        value = inspect.getattr_static(owner, key, None)
        self.function_code(value)

    def extend(self, watchers: tuple[InvariantWatcher, ...]) -> None:
        for watcher in watchers:
            self.add(watcher.kind, watcher.owner, watcher.key)

    def finish(self) -> tuple[InvariantWatcher, ...]:
        return tuple(self._watchers)


class _HelperAnalyzer:
    def __init__(
        self,
        function: types.FunctionType,
        argument: object,
    ) -> None:
        self.function = function
        self.argument = argument
        self.node = _function_node(function)
        self.watchers = _WatcherCollector()
        self.patterns: set[str] = set()

    def _resolve_name(self, name: str) -> object:
        if name in self.function.__globals__:
            self.watchers.dict_item(self.function.__globals__, name)
            return self.function.__globals__[name]
        namespace = _builtins_dict(self.function)
        if name in namespace:
            self.watchers.dict_item(namespace, name)
            return namespace[name]
        raise _UnsupportedInvariantCall("name_unbound")

    def _resolve_static(self, expression: ast.expr) -> object:
        if isinstance(expression, ast.Name):
            return self._resolve_name(expression.id)
        if isinstance(expression, ast.Attribute):
            owner = self._resolve_static(expression.value)
            if type(owner) is not types.ModuleType:
                raise _UnsupportedInvariantCall("attribute_owner_unsupported")
            namespace = vars(owner)
            self.watchers.dict_item(namespace, expression.attr)
            try:
                return namespace[expression.attr]
            except KeyError as error:
                raise _UnsupportedInvariantCall("attribute_missing") from error
        raise _UnsupportedInvariantCall("static_value_unsupported")

    def _watch_python_callable(self, function: object) -> None:
        self.watchers.function_code(function)
        if type(function) is types.FunctionType and function.__closure__:
            # Mutable closure cells would need a distinct backend guard.  Fail
            # closed unless all captured values are exact immutable objects.
            for cell in function.__closure__:
                try:
                    value = cell.cell_contents
                except ValueError as error:
                    raise _UnsupportedInvariantCall(
                        "callable_closure_empty"
                    ) from error
                if type(value) not in _IMMUTABLE_ARGUMENT_TYPES:
                    raise _UnsupportedInvariantCall(
                        "callable_closure_mutable"
                    )

    def _environment_get(self, call: ast.Call, receiver: object) -> str:
        if receiver is not os.environ or type(receiver).__name__ != "_Environ":
            raise _UnsupportedInvariantCall("mapping_state_unsupported")
        if (
            len(call.args) not in {1, 2}
            or call.keywords
            or not isinstance(call.args[0], ast.Constant)
            or type(call.args[0].value) is not str
        ):
            raise _UnsupportedInvariantCall("dynamic_dependency_key")
        if len(call.args) == 2 and (
            not isinstance(call.args[1], ast.Constant)
            or type(call.args[1].value) is not str
        ):
            raise _UnsupportedInvariantCall("dependency_default_unsupported")

        receiver_type = type(receiver)
        self.watchers.type_attr(receiver_type, "get")
        self.watchers.type_attr(receiver_type, "__getitem__")
        state = vars(receiver)
        for name in ("_data", "encodekey", "decodevalue"):
            self.watchers.dict_item(state, name)
            if name != "_data":
                self._watch_python_callable(state[name])
        data = state["_data"]
        encode_key = state["encodekey"]
        if type(data) is not dict or type(encode_key) is not types.FunctionType:
            raise _UnsupportedInvariantCall("environment_layout_unsupported")
        encoded_key = encode_key(call.args[0].value)
        if type(encoded_key) not in {str, bytes}:
            raise _UnsupportedInvariantCall("environment_key_type_unsupported")
        self.watchers.dict_item(data, encoded_key)
        self.patterns.add("process_state_read")
        return _EXACT_UNICODE

    def _path_constructor(self, constructor: type[object]) -> str:
        if not issubclass(constructor, PurePath):
            raise _UnsupportedInvariantCall("constructor_unsupported")
        self.watchers.type_attr(constructor, "__new__")
        concrete_type = type(Path())
        self.watchers.type_attr(concrete_type, "__truediv__")
        self.watchers.type_attr(concrete_type, "__str__")
        self.patterns.add("immutable_sequence_construct")
        return _PATH

    def _expression_type(
        self,
        expression: ast.expr,
        locals_: Mapping[str, str],
    ) -> str:
        if isinstance(expression, ast.Constant):
            if type(expression.value) is str:
                return _EXACT_UNICODE
            if expression.value is None:
                return _OPTIONAL_UNICODE
            raise _UnsupportedInvariantCall("constant_type_unsupported")
        if isinstance(expression, ast.Name):
            try:
                return locals_[expression.id]
            except KeyError as error:
                raise _UnsupportedInvariantCall("local_value_unbound") from error
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Div):
            left = self._expression_type(expression.left, locals_)
            right = self._expression_type(expression.right, locals_)
            if left != _PATH or right != _EXACT_UNICODE:
                raise _UnsupportedInvariantCall("path_join_types_unsupported")
            self.patterns.add("immutable_sequence_construct")
            return _PATH
        if not isinstance(expression, ast.Call):
            raise _UnsupportedInvariantCall("expression_unsupported")

        if isinstance(expression.func, ast.Attribute):
            if expression.func.attr == "get":
                try:
                    receiver = self._resolve_static(expression.func.value)
                except _UnsupportedInvariantCall:
                    receiver = None
                if receiver is not None:
                    return self._environment_get(expression, receiver)
            if expression.func.attr == "strip":
                if expression.args or expression.keywords:
                    raise _UnsupportedInvariantCall("strip_shape_unsupported")
                receiver_type = self._expression_type(
                    expression.func.value,
                    locals_,
                )
                if receiver_type != _EXACT_UNICODE:
                    raise _UnsupportedInvariantCall("strip_type_unsupported")
                return _EXACT_UNICODE
            raise _UnsupportedInvariantCall("method_call_unsupported")

        callee = self._resolve_static(expression.func)
        if callee is str:
            if len(expression.args) != 1 or expression.keywords:
                raise _UnsupportedInvariantCall("str_shape_unsupported")
            argument_type = self._expression_type(expression.args[0], locals_)
            if argument_type not in {_EXACT_UNICODE, _PATH}:
                raise _UnsupportedInvariantCall("str_argument_unsupported")
            if argument_type == _PATH:
                self.patterns.add("immutable_sequence_construct")
            return _EXACT_UNICODE
        if type(callee) is type and issubclass(callee, PurePath):
            if len(expression.args) != 1 or expression.keywords:
                raise _UnsupportedInvariantCall("path_shape_unsupported")
            if self._expression_type(expression.args[0], locals_) != _EXACT_UNICODE:
                raise _UnsupportedInvariantCall("path_argument_unsupported")
            return self._path_constructor(callee)
        raise _UnsupportedInvariantCall("call_target_unsupported")

    def _analyze_statements(
        self,
        statements: list[ast.stmt],
        locals_: dict[str, str],
    ) -> tuple[set[str], bool]:
        returns: set[str] = set()
        falls_through = True
        for statement in statements:
            if not falls_through:
                raise _UnsupportedInvariantCall("unreachable_statement")
            if isinstance(statement, ast.Assign):
                if (
                    len(statement.targets) != 1
                    or not isinstance(statement.targets[0], ast.Name)
                ):
                    raise _UnsupportedInvariantCall("assignment_unsupported")
                locals_[statement.targets[0].id] = self._expression_type(
                    statement.value,
                    locals_,
                )
                continue
            if isinstance(statement, ast.If):
                test_type = self._expression_type(statement.test, locals_)
                if test_type not in {_EXACT_UNICODE, _OPTIONAL_UNICODE}:
                    raise _UnsupportedInvariantCall("branch_type_unsupported")
                self.patterns.add("branch")
                body_locals = dict(locals_)
                if (
                    test_type == _OPTIONAL_UNICODE
                    and isinstance(statement.test, ast.Name)
                ):
                    body_locals[statement.test.id] = _EXACT_UNICODE
                body_returns, body_falls = self._analyze_statements(
                    list(statement.body),
                    body_locals,
                )
                else_returns, else_falls = self._analyze_statements(
                    list(statement.orelse),
                    dict(locals_),
                )
                returns.update(body_returns)
                returns.update(else_returns)
                falls_through = body_falls or else_falls
                continue
            if isinstance(statement, ast.Return):
                if statement.value is None:
                    raise _UnsupportedInvariantCall("return_value_required")
                returns.add(self._expression_type(statement.value, locals_))
                falls_through = False
                continue
            raise _UnsupportedInvariantCall("statement_has_effect")
        return returns, falls_through

    def analyze(self) -> InvariantCallPlan:
        signature = inspect.signature(self.function)
        parameters = tuple(signature.parameters.values())
        if (
            len(parameters) != 1
            or parameters[0].kind
            not in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            or type(self.argument) not in _IMMUTABLE_ARGUMENT_TYPES
            or self.function.__closure__
        ):
            raise _UnsupportedInvariantCall("helper_signature_unsupported")

        argument_type = (
            _OPTIONAL_UNICODE
            if self.argument is None
            else _EXACT_UNICODE
            if type(self.argument) is str
            else "unsupported"
        )
        if argument_type == "unsupported":
            raise _UnsupportedInvariantCall("helper_argument_type_unsupported")
        self.watchers.function_code(self.function)
        returns, falls_through = self._analyze_statements(
            list(self.node.body),
            {parameters[0].name: argument_type},
        )
        if falls_through or returns != {_EXACT_UNICODE}:
            raise _UnsupportedInvariantCall("helper_result_type_unsupported")
        return InvariantCallPlan(
            self.function,
            self.argument,
            "identity",
            self.watchers.finish(),
            tuple(sorted(self.patterns)),
            _EXACT_UNICODE,
        )


def _invariant_parameter_values(
    function: types.FunctionType,
    bound_arguments: Mapping[str, object],
) -> dict[str, object]:
    signature = inspect.signature(function)
    positional = tuple(
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    )
    if not positional:
        return {}
    row_name = positional[0].name
    values: dict[str, object] = {}
    for name, parameter in signature.parameters.items():
        if name == row_name:
            continue
        if name in bound_arguments:
            value = bound_arguments[name]
        elif parameter.default is not inspect.Parameter.empty:
            value = parameter.default
        else:
            continue
        if type(value) in _IMMUTABLE_ARGUMENT_TYPES:
            values[name] = value
    return values


def _resolve_outer_name(
    function: types.FunctionType,
    name: str,
) -> object | None:
    if name in function.__globals__:
        return function.__globals__[name]
    freevars = function.__code__.co_freevars
    closure = function.__closure__ or ()
    if name in freevars:
        index = freevars.index(name)
        if index < len(closure):
            try:
                return closure[index].cell_contents
            except ValueError:
                return None
    return None


def analyze_invariant_calls(
    function: types.FunctionType,
    *,
    bound_arguments: Mapping[str, object] | None = None,
) -> tuple[InvariantCallPlan, ...]:
    """Return strictly proven one-argument invariant helper calls.

    Unsupported behavior is not an error for admission: it simply produces no
    plan so the ordinary Python/CinderX path remains authoritative.
    """

    if type(function) is not types.FunctionType:
        return ()
    bound = {} if bound_arguments is None else dict(bound_arguments)
    try:
        node = _function_node(function)
        invariant_values = _invariant_parameter_values(function, bound)
    except (TypeError, ValueError, _UnsupportedInvariantCall):
        return ()

    plans: list[InvariantCallPlan] = []
    seen: set[int] = set()
    for call in (value for value in ast.walk(node) if isinstance(value, ast.Call)):
        if (
            not isinstance(call.func, ast.Name)
            or len(call.args) != 1
            or call.keywords
        ):
            continue
        callee = _resolve_outer_name(function, call.func.id)
        if type(callee) is not types.FunctionType or id(callee) in seen:
            continue
        argument_expression = call.args[0]
        if isinstance(argument_expression, ast.Constant):
            argument = argument_expression.value
        elif (
            isinstance(argument_expression, ast.Name)
            and argument_expression.id in invariant_values
        ):
            argument = invariant_values[argument_expression.id]
        else:
            continue
        try:
            plan = _HelperAnalyzer(callee, argument).analyze()
        except (KeyError, TypeError, ValueError, _UnsupportedInvariantCall):
            continue
        seen.add(id(callee))
        plans.append(plan)
    return tuple(plans)


class _ValueCacheAnalyzer:
    """Prove a narrow row-value reuse shape without recognizing UDF names."""

    def __init__(self, function: types.FunctionType) -> None:
        self.function = function
        self.node = _function_node(function)
        self.watchers = _WatcherCollector()
        self.patterns: set[str] = {"bounded_value_reuse"}
        self.invariant_plans = analyze_invariant_calls(function)
        self.invariant_by_function = {
            id(plan.function): plan for plan in self.invariant_plans
        }
        self.invariant_locals: dict[str, object] = {}
        self.mapping_locals: set[str] = set()

    def _resolve_static(self, expression: ast.expr) -> object:
        if isinstance(expression, ast.Name):
            if expression.id in self.function.__globals__:
                self.watchers.dict_item(
                    self.function.__globals__, expression.id
                )
                return self.function.__globals__[expression.id]
            namespace = _builtins_dict(self.function)
            if expression.id in namespace:
                self.watchers.dict_item(namespace, expression.id)
                return namespace[expression.id]
            raise _UnsupportedInvariantCall("value_name_unbound")
        if isinstance(expression, ast.Attribute):
            owner = self._resolve_static(expression.value)
            if type(owner) is not types.ModuleType:
                raise _UnsupportedInvariantCall(
                    "value_attribute_owner_unsupported"
                )
            namespace = vars(owner)
            self.watchers.dict_item(namespace, expression.attr)
            try:
                return namespace[expression.attr]
            except KeyError as error:
                raise _UnsupportedInvariantCall(
                    "value_attribute_missing"
                ) from error
        raise _UnsupportedInvariantCall("value_static_unsupported")

    def _is_typed_lru_mapping(self, value: object) -> bool:
        wrapper_type = getattr(functools, "_lru_cache_wrapper", None)
        if wrapper_type is None or not isinstance(value, wrapper_type):
            return False
        wrapped = getattr(value, "__wrapped__", None)
        if type(wrapped) is not types.FunctionType:
            return False
        try:
            result_type = typing.get_type_hints(wrapped).get("return")
        except (NameError, TypeError):
            return False
        return result_type is dict or typing.get_origin(result_type) is dict

    def _record_top_level_dependencies(self) -> None:
        for statement in self.node.body:
            if (
                not isinstance(statement, ast.Assign)
                or len(statement.targets) != 1
                or not isinstance(statement.targets[0], ast.Name)
                or not isinstance(statement.value, ast.Call)
                or not isinstance(statement.value.func, ast.Name)
            ):
                continue
            target = statement.targets[0].id
            callee = self._resolve_static(statement.value.func)
            invariant = self.invariant_by_function.get(id(callee))
            if invariant is not None:
                if len(statement.value.args) != 1 or statement.value.keywords:
                    raise _UnsupportedInvariantCall(
                        "value_invariant_call_shape"
                    )
                try:
                    resolved = invariant.function(invariant.argument)
                except Exception as error:
                    raise _UnsupportedInvariantCall(
                        "value_invariant_evaluation_failed"
                    ) from error
                if type(resolved) is not str:
                    raise _UnsupportedInvariantCall(
                        "value_invariant_result_type"
                    )
                self.watchers.extend(invariant.watchers)
                self.invariant_locals[target] = resolved
                continue
            if not self._is_typed_lru_mapping(callee):
                continue
            if (
                len(statement.value.args) != 1
                or statement.value.keywords
                or not isinstance(statement.value.args[0], ast.Name)
                or statement.value.args[0].id not in self.invariant_locals
            ):
                raise _UnsupportedInvariantCall(
                    "value_cached_mapping_call_shape"
                )
            argument = self.invariant_locals[statement.value.args[0].id]
            self.watchers.add("call_result_identity", callee, argument)
            self.mapping_locals.add(target)
            self.patterns.add("process_state_read")

    def _json_call_supported(self, call: ast.Call) -> bool:
        if not call.args or any(keyword.arg is None for keyword in call.keywords):
            return False
        for keyword in call.keywords:
            if keyword.arg in {"cls", "default"}:
                return False
            if not isinstance(keyword.value, ast.Constant):
                return False
        return True

    def _call_supported(self, call: ast.Call) -> bool:
        if isinstance(call.func, ast.Name):
            callee = self._resolve_static(call.func)
            if id(callee) in self.invariant_by_function:
                return len(call.args) == 1 and not call.keywords
            if self._is_typed_lru_mapping(callee):
                return (
                    len(call.args) == 1
                    and not call.keywords
                    and isinstance(call.args[0], ast.Name)
                    and call.args[0].id in self.invariant_locals
                )
            if (
                isinstance(callee, type)
                and issubclass(callee, BaseException)
                and not call.keywords
            ):
                return True
            return callee in {str, bool} and len(call.args) == 1 and not call.keywords
        if not isinstance(call.func, ast.Attribute):
            return False
        if (
            call.func.attr == "get"
            and isinstance(call.func.value, ast.Name)
            and not call.keywords
            and len(call.args) in {1, 2}
        ):
            self.mapping_locals.add(call.func.value.id)
            return True
        try:
            callee = self._resolve_static(call.func)
        except _UnsupportedInvariantCall:
            return False
        if callee is json.dumps:
            self.patterns.add("immutable_result_construct")
            return self._json_call_supported(call)
        if callee is os.path.basename:
            return len(call.args) == 1 and not call.keywords
        return False

    def _validate_ast(self) -> None:
        forbidden = (
            ast.AnnAssign,
            ast.AugAssign,
            ast.Delete,
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.With,
            ast.AsyncWith,
            ast.Try,
            ast.Global,
            ast.Nonlocal,
            ast.Yield,
            ast.YieldFrom,
            ast.Await,
            ast.Lambda,
        )
        for node in ast.walk(self.node):
            if isinstance(node, forbidden):
                raise _UnsupportedInvariantCall("value_effect_unsupported")
            if isinstance(node, ast.Assign) and any(
                not isinstance(target, ast.Name) for target in node.targets
            ):
                raise _UnsupportedInvariantCall("value_mutation_unsupported")
            if isinstance(node, ast.If):
                self.patterns.add("branch")
            if isinstance(node, ast.Call) and not self._call_supported(node):
                raise _UnsupportedInvariantCall("value_call_unsupported")
            if isinstance(node, ast.Return):
                if not (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and self._resolve_static(node.value.func) is json.dumps
                    and self._json_call_supported(node.value)
                ):
                    raise _UnsupportedInvariantCall(
                        "value_result_construct_unsupported"
                    )

    def analyze(self, *, capacity: int) -> ValueCachePlan:
        if self.function.__closure__:
            raise _UnsupportedInvariantCall("value_closure_unsupported")
        signature = inspect.signature(self.function)
        parameters = tuple(signature.parameters.values())
        positional = tuple(
            parameter
            for parameter in parameters
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        )
        keyword_only = tuple(
            parameter
            for parameter in parameters
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        )
        var_keyword = tuple(
            parameter
            for parameter in parameters
            if parameter.kind is inspect.Parameter.VAR_KEYWORD
        )
        if (
            len(positional) != 1
            or positional[0].default is not inspect.Parameter.empty
            or len(keyword_only) != 1
            or keyword_only[0].default is inspect.Parameter.empty
            or type(keyword_only[0].default) not in _IMMUTABLE_ARGUMENT_TYPES
            or len(var_keyword) != 1
        ):
            raise _UnsupportedInvariantCall("value_signature_unsupported")
        try:
            hints = typing.get_type_hints(self.function)
        except (NameError, TypeError) as error:
            raise _UnsupportedInvariantCall("value_type_hints_unavailable") from error
        if (
            hints.get(positional[0].name) is not str
            or hints.get("return") is not str
        ):
            raise _UnsupportedInvariantCall("value_types_unsupported")
        if not 1 <= capacity <= 65_536:
            raise _UnsupportedInvariantCall("value_capacity_unsupported")

        self.watchers.function_code(self.function)
        self._record_top_level_dependencies()
        if not self.mapping_locals or not self.invariant_locals:
            raise _UnsupportedInvariantCall("value_dependency_graph_missing")
        self._validate_ast()
        watchers = self.watchers.finish()
        if not watchers or len(watchers) > 64:
            raise _UnsupportedInvariantCall("value_watcher_count_unsupported")
        return ValueCachePlan(
            self.function,
            ("exact_unicode_value", "identity", "empty_dict"),
            (keyword_only[0].default,),
            watchers,
            tuple(sorted(self.patterns)),
            _EXACT_UNICODE,
            _EXACT_UNICODE,
            capacity,
        )


class _GuardedJsonValueCacheAnalyzer:
    """Prove exact JSON-result reuse guarded by a dynamic state read.

    The proof is expressed only in behavior and data-shape terms: an exact
    Unicode input is decoded into a local mapping, an idempotent external
    observer result is stored in that mapping, and the exact Unicode JSON
    result carries both the observer key and its snapshot.  CinderX validates
    that snapshot on every cache hit.
    """

    def __init__(self, function: types.FunctionType) -> None:
        self.function = function
        self.node = _function_node(function)
        self.watchers = _WatcherCollector()
        self.patterns = {
            "bounded_value_reuse",
            "branch",
            "exception_region",
            "external_state_guard",
            "immutable_result_construct",
            "local_mapping_mutation",
        }

    def _resolve_static(self, expression: ast.expr) -> object:
        if isinstance(expression, ast.Name):
            if expression.id in self.function.__globals__:
                self.watchers.dict_item(
                    self.function.__globals__, expression.id
                )
                return self.function.__globals__[expression.id]
            namespace = _builtins_dict(self.function)
            if expression.id in namespace:
                self.watchers.dict_item(namespace, expression.id)
                return namespace[expression.id]
            raise _UnsupportedInvariantCall("guarded_name_unbound")
        if isinstance(expression, ast.Attribute):
            owner = self._resolve_static(expression.value)
            if type(owner) is not types.ModuleType:
                raise _UnsupportedInvariantCall(
                    "guarded_attribute_owner_unsupported"
                )
            namespace = vars(owner)
            self.watchers.dict_item(namespace, expression.attr)
            try:
                return namespace[expression.attr]
            except KeyError as error:
                raise _UnsupportedInvariantCall(
                    "guarded_attribute_missing"
                ) from error
        raise _UnsupportedInvariantCall("guarded_static_unsupported")

    def _peek_name(self, name: str) -> object:
        if name in self.function.__globals__:
            return self.function.__globals__[name]
        namespace = _builtins_dict(self.function)
        if name in namespace:
            return namespace[name]
        raise _UnsupportedInvariantCall("guarded_name_unbound")

    @staticmethod
    def _constant_mapping_key(expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Constant) and type(expression.value) is str:
            return expression.value
        return None

    def _json_load_target(self, row_name: str) -> tuple[str, object]:
        tries = [
            statement
            for statement in self.node.body
            if isinstance(statement, ast.Try)
        ]
        if len(tries) != 1 or not tries[0].handlers or tries[0].finalbody:
            raise _UnsupportedInvariantCall("guarded_try_shape_unsupported")
        candidate: tuple[str, object] | None = None
        for statement in tries[0].body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
            ):
                try:
                    callee = self._resolve_static(statement.value.func)
                except _UnsupportedInvariantCall:
                    continue
                if (
                    callee is json.loads
                    and len(statement.value.args) == 1
                    and not statement.value.keywords
                    and isinstance(statement.value.args[0], ast.Name)
                    and statement.value.args[0].id == row_name
                ):
                    candidate = (statement.targets[0].id, callee)
        if candidate is None:
            raise _UnsupportedInvariantCall("guarded_json_load_missing")
        mapping_name, decoder = candidate
        fallback_assigns = [
            statement
            for handler in tries[0].handlers
            for statement in handler.body
            if isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == mapping_name
            and isinstance(statement.value, ast.Dict)
        ]
        if not fallback_assigns:
            raise _UnsupportedInvariantCall("guarded_fallback_mapping_missing")
        return mapping_name, decoder

    def _guard_shape(
        self,
        mapping_name: str,
    ) -> tuple[tuple[str, ...], object, tuple[str, ...]]:
        path_locals: dict[str, str] = {}
        snapshot_locals: dict[str, tuple[str, object]] = {}
        snapshot_fields: dict[str, str] = {}
        body_nodes = (
            node
            for statement in self.node.body
            for node in ast.walk(statement)
        )
        for node in body_nodes:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == mapping_name
                and node.value.func.attr == "get"
                and len(node.value.args) == 1
                and not node.value.keywords
            ):
                key = self._constant_mapping_key(node.value.args[0])
                if key is not None:
                    path_locals[node.targets[0].id] = key
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and self._resolve_static(node.value.func) is bool
                and len(node.value.args) == 1
                and not node.value.keywords
                and isinstance(node.value.args[0], ast.BoolOp)
                and isinstance(node.value.args[0].op, ast.And)
                and len(node.value.args[0].values) == 2
                and isinstance(node.value.args[0].values[0], ast.Name)
                and isinstance(node.value.args[0].values[1], ast.Call)
            ):
                key_local = node.value.args[0].values[0].id
                observer_call = node.value.args[0].values[1]
                if (
                    key_local in path_locals
                    and len(observer_call.args) == 1
                    and not observer_call.keywords
                    and isinstance(observer_call.args[0], ast.Name)
                    and observer_call.args[0].id == key_local
                ):
                    observer = self._resolve_static(observer_call.func)
                    if observer is os.path.exists:
                        snapshot_locals[node.targets[0].id] = (
                            path_locals[key_local],
                            observer,
                        )
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].value, ast.Name)
                and node.targets[0].value.id == mapping_name
                and isinstance(node.value, ast.Name)
            ):
                field = self._constant_mapping_key(node.targets[0].slice)
                if field is not None:
                    snapshot_fields[node.value.id] = field
        matches = [
            (key, observer, snapshot_fields[local])
            for local, (key, observer) in snapshot_locals.items()
            if local in snapshot_fields
        ]
        if len(matches) != 1:
            raise _UnsupportedInvariantCall("guarded_snapshot_shape_unsupported")
        key, observer, expected = matches[0]
        return (key,), observer, (expected,)

    def _validate_effects(self, mapping_name: str, row_name: str) -> None:
        forbidden = (
            ast.AnnAssign,
            ast.AugAssign,
            ast.Delete,
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.With,
            ast.AsyncWith,
            ast.Global,
            ast.Nonlocal,
            ast.Yield,
            ast.YieldFrom,
            ast.Await,
            ast.Lambda,
        )
        local_names = {
            parameter.arg
            for parameter in (
                *self.node.args.posonlyargs,
                *self.node.args.args,
                *self.node.args.kwonlyargs,
            )
        }
        if self.node.args.vararg is not None:
            local_names.add(self.node.args.vararg.arg)
        if self.node.args.kwarg is not None:
            local_names.add(self.node.args.kwarg.arg)
        local_names.update(
            node.id
            for node in ast.walk(self.node)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        )
        local_names.update(
            handler.name
            for handler in ast.walk(self.node)
            if isinstance(handler, ast.ExceptHandler)
            and handler.name is not None
        )

        returns = 0
        for node in (
            node
            for statement in self.node.body
            for node in ast.walk(statement)
        ):
            if isinstance(node, forbidden):
                raise _UnsupportedInvariantCall("guarded_effect_unsupported")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        continue
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == mapping_name
                        and self._constant_mapping_key(target.slice) is not None
                    ):
                        continue
                    raise _UnsupportedInvariantCall(
                        "guarded_mutation_unsupported"
                    )
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id not in local_names
            ):
                static_value = self._peek_name(node.id)
                if not (
                    type(static_value) is types.ModuleType
                    or isinstance(static_value, type)
                    or callable(static_value)
                ):
                    raise _UnsupportedInvariantCall(
                        "guarded_state_read_unsupported"
                    )
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == mapping_name
                    and node.func.attr == "get"
                    and not node.keywords
                    and len(node.args) in {1, 2}
                    and self._constant_mapping_key(node.args[0]) is not None
                ):
                    continue
                callee = self._resolve_static(node.func)
                if callee is json.loads:
                    allowed = (
                        len(node.args) == 1
                        and not node.keywords
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == row_name
                    )
                elif callee is json.dumps:
                    allowed = (
                        len(node.args) == 1
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == mapping_name
                        and all(
                            keyword.arg not in {None, "cls", "default"}
                            and isinstance(keyword.value, ast.Constant)
                            for keyword in node.keywords
                        )
                    )
                elif callee is isinstance:
                    allowed = (
                        len(node.args) == 2
                        and not node.keywords
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == mapping_name
                        and isinstance(node.args[1], ast.Name)
                        and self._resolve_static(node.args[1]) is dict
                    )
                elif callee in {str, bool}:
                    allowed = len(node.args) == 1 and not node.keywords
                elif callee is os.path.exists:
                    allowed = (
                        len(node.args) == 1
                        and not node.keywords
                        and isinstance(node.args[0], ast.Name)
                    )
                else:
                    allowed = False
                if not allowed:
                    raise _UnsupportedInvariantCall(
                        "guarded_call_unsupported"
                    )
            if isinstance(node, ast.Return):
                returns += 1
                if not (
                    isinstance(node.value, ast.Call)
                    and self._resolve_static(node.value.func) is json.dumps
                    and len(node.value.args) == 1
                    and isinstance(node.value.args[0], ast.Name)
                    and node.value.args[0].id == mapping_name
                    and all(
                        keyword.arg not in {None, "cls", "default"}
                        and isinstance(keyword.value, ast.Constant)
                        for keyword in node.value.keywords
                    )
                ):
                    raise _UnsupportedInvariantCall(
                        "guarded_result_construct_unsupported"
                    )
        if returns != 1 or mapping_name == row_name:
            raise _UnsupportedInvariantCall("guarded_return_shape_unsupported")

    def analyze(self, *, capacity: int) -> ValueCachePlan:
        if self.function.__closure__ or not 1 <= capacity <= 65_536:
            raise _UnsupportedInvariantCall("guarded_function_unsupported")
        signature = inspect.signature(self.function)
        parameters = tuple(signature.parameters.values())
        positional = tuple(
            parameter
            for parameter in parameters
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        )
        var_keyword = tuple(
            parameter
            for parameter in parameters
            if parameter.kind is inspect.Parameter.VAR_KEYWORD
        )
        if (
            len(positional) != 1
            or positional[0].default is not inspect.Parameter.empty
            or len(parameters) != 2
            or len(var_keyword) != 1
        ):
            raise _UnsupportedInvariantCall("guarded_signature_unsupported")
        try:
            hints = typing.get_type_hints(self.function)
        except (NameError, TypeError) as error:
            raise _UnsupportedInvariantCall(
                "guarded_type_hints_unavailable"
            ) from error
        if (
            hints.get(positional[0].name) is not str
            or hints.get("return") is not str
        ):
            raise _UnsupportedInvariantCall("guarded_types_unsupported")

        mapping_name, decoder = self._json_load_target(positional[0].name)
        key_path, observer, expected_path = self._guard_shape(mapping_name)
        self._validate_effects(mapping_name, positional[0].name)
        self.watchers.function_code(self.function)
        self.watchers.function_code(decoder)
        self.watchers.function_code(observer)
        self.watchers.type_attr(dict, "get")
        self.watchers.type_attr(dict, "__setitem__")
        watchers = self.watchers.finish()
        if not watchers or len(watchers) > 64:
            raise _UnsupportedInvariantCall("guarded_watcher_count_unsupported")
        return ValueCachePlan(
            self.function,
            ("exact_unicode_value", "empty_dict"),
            (),
            watchers,
            tuple(sorted(self.patterns)),
            _EXACT_UNICODE,
            _EXACT_UNICODE,
            capacity,
            ValueEntryGuard(
                "json_result_call_value",
                decoder,
                key_path,
                observer,
                expected_path,
            ),
        )


def analyze_value_cache(
    function: types.FunctionType,
    *,
    capacity: int = 16_384,
) -> ValueCachePlan | None:
    """Return a generic exact-value reuse proof or fail closed with ``None``."""

    if type(function) is not types.FunctionType:
        return None
    for analyzer in (_ValueCacheAnalyzer, _GuardedJsonValueCacheAnalyzer):
        try:
            return analyzer(function).analyze(capacity=capacity)
        except (KeyError, TypeError, ValueError, _UnsupportedInvariantCall):
            continue
    return None
