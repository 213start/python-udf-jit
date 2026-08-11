from __future__ import annotations

import __future__
import ast
import builtins
import copy
import dis
import functools
import hashlib
import inspect
import json
import re
import sys
import textwrap
import types
from bisect import bisect_left
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from python_udf_jit.compiler.core_ir import (
    Determinism,
    EffectKind,
    SemanticLiteral,
)
from python_udf_jit.compiler.identity import code_identity, code_identity_from_code
from python_udf_jit.compiler.typed_analysis import (
    TypedAnalysisBundle,
    analyze_typed_module,
)
from python_udf_jit.compiler.typed_ir import (
    BOOL,
    EXACT_UNICODE,
    FLOAT64,
    INT64,
    UNICODE_BUILDER,
    UNICODE_SCALAR,
    TypeKind,
    TypeSpec,
    TypedBlock,
    TypedBlockArgument,
    TypedControlEdge,
    TypedOperation,
    TypedSemanticModule,
    build_typed_module,
    encode_int_table,
)


_PROPERTY_METHODS = {
    "isalnum": "alnum",
    "isalpha": "alpha",
    "isdecimal": "decimal",
    "isdigit": "digit",
    "isnumeric": "numeric",
    "isspace": "space",
}
_COMPARE = {
    ast.Eq: "compare.eq",
    ast.NotEq: "compare.ne",
    ast.Lt: "compare.lt",
    ast.LtE: "compare.le",
    ast.Gt: "compare.gt",
    ast.GtE: "compare.ge",
}
_BINARY = {
    ast.Add: "binary.add",
    ast.Sub: "binary.sub",
    ast.Mult: "binary.mul",
    ast.Div: "binary.truediv",
}
_PORTABLE_CONSTANT_TYPES = {type(None), bool, int, float, str, bytes}


class TypedCaptureError(ValueError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code if not detail else f"{reason_code}:{detail}")


def _fail(reason_code: str, detail: str = "") -> None:
    raise TypedCaptureError(reason_code, detail)


@dataclass(frozen=True)
class TypedCaptureResult:
    module: TypedSemanticModule
    analysis: TypedAnalysisBundle
    code_sha256: str
    source_sha256: str
    source_first_line: int
    normalized_pattern: str
    dependency_hashes: tuple[str, ...]
    runtime_guard: "TypedRuntimeGuard"
    entry_guard: "TypedEntryGuard | None" = None


@dataclass(frozen=True)
class TypedEntryGuard:
    """A proven outer side exit that protects one compiled loop region."""

    kind: str
    input_type: TypeSpec

    def matches(self, inputs: tuple[object, ...]) -> bool:
        if len(inputs) != 1:
            return False
        value = inputs[0]
        exact_type = {
            "str": str,
            "list": list,
            "tuple": tuple,
            "range": range,
        }.get(self.input_type.name)
        if exact_type is None or type(value) is not exact_type:
            return False
        if self.kind == "non_empty_sequence":
            return len(value) > 0  # type: ignore[arg-type]
        return False


@dataclass(frozen=True)
class _RuntimeDependency:
    kind: str
    name: str
    index: int | None
    expected: object
    identity: bool = False
    layout: tuple[str, ...] = ()

    def _expected_document(self) -> object:
        if self.kind == "code":
            return {
                "code": code_identity_from_code(self.expected).sha256,  # type: ignore[arg-type]
            }
        if self.kind == "global_regex":
            return {
                "flags": self.expected.flags,  # type: ignore[union-attr]
                "pattern": self.expected.pattern,  # type: ignore[union-attr]
            }
        if self.identity:
            return {"builtin": self.name}
        return SemanticLiteral.from_value(self.expected).to_document()  # type: ignore[arg-type]

    @property
    def sha256(self) -> str:
        document = {
            "expected": self._expected_document(),
            "index": self.index,
            "kind": self.kind,
            "layout": list(self.layout),
            "name": self.name,
        }
        payload = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(
            b"python-udf-jit-runtime-dependency-v1\0" + payload
        ).hexdigest()

    def current_value(self, function: types.FunctionType) -> object:
        if self.kind == "code":
            return function.__code__
        if self.kind == "positional_default":
            defaults = function.__defaults__ or ()
            positional_names = function.__code__.co_varnames[
                : function.__code__.co_argcount
            ]
            current_layout = (
                positional_names[-len(defaults) :] if defaults else ()
            )
            if current_layout != self.layout or self.name not in current_layout:
                raise LookupError(self.name)
            return defaults[current_layout.index(self.name)]
        if self.kind == "keyword_default":
            defaults = function.__kwdefaults__ or {}
            if set(defaults) != set(self.layout):
                raise LookupError(self.name)
            return defaults[self.name]
        if self.kind == "closure":
            closure = function.__closure__ or ()
            if self.index is None or self.index >= len(closure):
                raise LookupError(self.name)
            return closure[self.index].cell_contents
        if self.kind in {"global", "global_regex"}:
            return function.__globals__[self.name]
        if self.kind == "builtin":
            if self.name in function.__globals__:
                return function.__globals__[self.name]
            namespace = function.__globals__.get("__builtins__", builtins)
            if isinstance(namespace, dict):
                return namespace[self.name]
            return getattr(namespace, self.name)
        if self.kind == "bound_argument":
            return self.expected
        raise LookupError(self.kind)

    def matches(self, function: types.FunctionType) -> bool:
        try:
            current = self.current_value(function)
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
class TypedRuntimeGuard:
    function: types.FunctionType
    dependencies: tuple[_RuntimeDependency, ...]

    @functools.cached_property
    def dependency_hashes(self) -> tuple[str, ...]:
        return tuple(sorted(dependency.sha256 for dependency in self.dependencies))

    def matches(self) -> bool:
        return all(
            dependency.matches(self.function)
            for dependency in self.dependencies
        )


@dataclass(frozen=True)
class _PredicatePlan:
    kind: str
    operation: str
    constant: object | None = None


@dataclass(frozen=True)
class _LoopPlan:
    input_name: str
    item_name: str
    accumulator_name: str
    initial_value: int | float
    update: str
    predicate: _PredicatePlan | None
    return_expression: ast.expr
    reduction_call: ast.Call | None
    function_source_line: int
    initial_source_line: int
    loop_source_line: int
    predicate_source_line: int | None
    update_source_line: int
    return_source_line: int


@dataclass(frozen=True)
class _SequenceTransformPlan:
    input_name: str
    function_source_line: int
    setup_source_line: int
    loop_source_line: int
    transform_source_line: int
    return_source_line: int


@dataclass(frozen=True)
class _ImmutableLookupTransformPlan(_SequenceTransformPlan):
    lookup_keys: tuple[int, ...]
    lookup_values: tuple[int, ...]

    @property
    def kind(self) -> str:
        return "immutable_lookup_builder"


@dataclass(frozen=True)
class _UnicodeFsmTransformPlan(_SequenceTransformPlan):
    unicode_property: str
    initial_state: int
    state_count: int
    transitions: tuple[int, ...]
    actions: tuple[int, ...]
    emissions: tuple[int, ...]

    @property
    def kind(self) -> str:
        return "unicode_fsm_builder"


@dataclass(frozen=True)
class _SignatureLayout:
    positional_default_names: tuple[str, ...]
    keyword_default_names: tuple[str, ...]


def _function_node(source: str, function: types.FunctionType) -> ast.FunctionDef:
    tree = ast.parse(source)
    candidates = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if len(candidates) != 1 or not isinstance(candidates[0], ast.FunctionDef):
        _fail("source_function_shape_unsupported")
    node = candidates[0]
    if node.name != function.__name__:
        _fail("source_function_identity_mismatch")
    variadic_names = {
        argument.arg
        for argument in (node.args.vararg, node.args.kwarg)
        if argument is not None
    }
    loaded_names = {
        candidate.id
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.Name) and isinstance(candidate.ctx, ast.Load)
    }
    used_variadics = sorted(variadic_names & loaded_names)
    if used_variadics:
        _fail("variadic_parameter_used", used_variadics[0])
    return node


def _future_flags(code: types.CodeType) -> int:
    return sum(
        getattr(__future__, name).compiler_flag
        for name in __future__.all_feature_names
        if code.co_flags & getattr(__future__, name).compiler_flag
    )


def _constant_key(
    value: object,
    code_key: Callable[[types.CodeType], tuple[object, ...]],
) -> object:
    if isinstance(value, types.CodeType):
        return code_key(value)
    if isinstance(value, tuple):
        return (
            "tuple",
            tuple(_constant_key(item, code_key) for item in value),
        )
    if isinstance(value, frozenset):
        return (
            "frozenset",
            tuple(
                sorted(
                    repr(_constant_key(item, code_key))
                    for item in value
                )
            ),
        )
    if type(value) is float:
        return ("float", value.hex())
    try:
        hash(value)
    except TypeError:
        return (type(value).__qualname__, repr(value))
    return (type(value).__qualname__, value)


def _code_constant_key(value: object) -> object:
    return _constant_key(value, _code_semantic_key)


def _code_semantic_key(code: types.CodeType) -> tuple[object, ...]:
    return (
        code.co_code,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_nlocals,
        code.co_stacksize,
        code.co_flags,
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        tuple(_code_constant_key(value) for value in code.co_consts),
        getattr(code, "co_exceptiontable", b""),
    )


def _normalized_code_constant_key(value: object) -> object:
    return _constant_key(value, _normalized_code_semantic_key)


def _normalized_instruction_key(
    instruction: dis.Instruction,
    instruction_index: Callable[[int], int],
) -> tuple[str, object]:
    if instruction.opcode in dis.hasjabs or instruction.opcode in dis.hasjrel:
        target = instruction.argval
        argument: object = (
            "jump_target",
            instruction_index(target)
            if type(target) is int
            else instruction.argrepr,
        )
    elif instruction.opname == "LOAD_CONST":
        argument = _normalized_code_constant_key(instruction.argval)
    elif instruction.arg is None:
        argument = None
    elif type(instruction.argval) in {type(None), bool, int, float, str, bytes}:
        argument = instruction.argval
    else:
        argument = instruction.argrepr
    return instruction.opname, argument


def _normalized_code_semantic_key(
    code: types.CodeType,
) -> tuple[object, ...]:
    """Compare source semantics across compatible CPython bytecode encodings.

    CPython micro releases may fold the internal ``PUSH_NULL`` call marker into
    ``LOAD_ATTR`` without changing source semantics.  Offset-bearing jumps and
    exception-table entries move with that encoding.  This key removes only
    those interpreter-internal positions while retaining opcodes, operands,
    constants, scope layout, and normalized exception regions.
    """

    instructions = tuple(
        instruction
        for instruction in dis.get_instructions(code, adaptive=False)
        if instruction.opname != "PUSH_NULL"
    )
    instruction_offsets = tuple(item.offset for item in instructions)

    def instruction_index(offset: int) -> int:
        return bisect_left(instruction_offsets, offset)

    exception_entries = tuple(
        (
            instruction_index(entry.start),
            instruction_index(entry.end),
            instruction_index(entry.target),
            entry.depth,
            entry.lasti,
        )
        for entry in dis.Bytecode(code).exception_entries
    )
    return (
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_nlocals,
        code.co_stacksize,
        code.co_flags,
        code.co_names,
        code.co_varnames,
        code.co_freevars,
        code.co_cellvars,
        tuple(
            _normalized_code_constant_key(value) for value in code.co_consts
        ),
        tuple(
            _normalized_instruction_key(item, instruction_index)
            for item in instructions
        ),
        exception_entries,
    )


def _nested_code_objects(code: types.CodeType) -> tuple[types.CodeType, ...]:
    nested: list[types.CodeType] = []
    for value in code.co_consts:
        if not isinstance(value, types.CodeType):
            continue
        nested.append(value)
        nested.extend(_nested_code_objects(value))
    return tuple(nested)


def _validate_source_identity(
    source: str,
    function: types.FunctionType,
) -> None:
    if function.__code__.co_freevars:
        bindings = "".join(
            f"    {name} = None\n"
            for name in function.__code__.co_freevars
        )
        compilable = (
            "def __udfjit_source_identity_outer():\n"
            + bindings
            + textwrap.indent(source, "    ")
        )
    else:
        compilable = source
    try:
        compiled = compile(
            compilable,
            "<udfjit-source-identity>",
            "exec",
            flags=_future_flags(function.__code__),
            dont_inherit=True,
            optimize=sys.flags.optimize,
        )
    except (SyntaxError, ValueError):
        _fail("source_code_identity_mismatch")
    candidates = tuple(
        code
        for code in _nested_code_objects(compiled)
        if code.co_name == function.__code__.co_name
    )
    live_key = _code_semantic_key(function.__code__)
    if any(_code_semantic_key(code) == live_key for code in candidates):
        return
    live_normalized = _normalized_code_semantic_key(function.__code__)
    if any(
        _normalized_code_semantic_key(code) == live_normalized
        for code in candidates
    ):
        return
    _fail("source_code_identity_mismatch")


def _portable_dependency(
    kind: str,
    name: str,
    index: int | None,
    value: object,
    *,
    layout: tuple[str, ...] = (),
) -> _RuntimeDependency:
    if type(value) not in _PORTABLE_CONSTANT_TYPES:
        _fail(f"{kind}_constant_unsupported", name)
    try:
        SemanticLiteral.from_value(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        _fail(f"{kind}_constant_unsupported", name)
    return _RuntimeDependency(kind, name, index, value, layout=layout)


def _signature_layout(
    function: types.FunctionType,
    node: ast.FunctionDef,
    bound_arguments: Mapping[str, object],
) -> _SignatureLayout:
    positional_names = tuple(
        argument.arg for argument in (*node.args.posonlyargs, *node.args.args)
    )
    keyword_names = tuple(argument.arg for argument in node.args.kwonlyargs)
    code = function.__code__
    live_positional_names = code.co_varnames[: code.co_argcount]
    live_keyword_names = code.co_varnames[
        code.co_argcount : code.co_argcount + code.co_kwonlyargcount
    ]
    defaults = function.__defaults__ or ()
    keyword_defaults = function.__kwdefaults__ or {}
    if len(defaults) > len(live_positional_names):
        _fail("function_default_layout_mismatch", "positional")
    source_positional_defaults = (
        positional_names[-len(node.args.defaults) :]
        if node.args.defaults
        else ()
    )
    live_positional_defaults = (
        live_positional_names[-len(defaults) :] if defaults else ()
    )
    source_keyword_defaults = tuple(
        argument.arg
        for argument, value in zip(
            node.args.kwonlyargs,
            node.args.kw_defaults,
            strict=True,
        )
        if value is not None
    )
    live_keyword_defaults = tuple(
        name for name in live_keyword_names if name in keyword_defaults
    )
    if (
        positional_names != live_positional_names
        or keyword_names != live_keyword_names
        or source_positional_defaults != live_positional_defaults
        or source_keyword_defaults != live_keyword_defaults
        or set(keyword_defaults) != set(live_keyword_defaults)
    ):
        _fail("function_default_layout_mismatch")
    all_names = {*positional_names, *keyword_names}
    unknown_bound = sorted(set(bound_arguments) - all_names)
    if unknown_bound:
        _fail("bound_argument_unknown", unknown_bound[0])
    if positional_names and positional_names[0] in bound_arguments:
        _fail("bound_argument_input", positional_names[0])
    for name in positional_names[1:]:
        if name not in live_positional_defaults and name not in bound_arguments:
            _fail("additional_parameter_required", name)
    for name in keyword_names:
        if name not in live_keyword_defaults and name not in bound_arguments:
            _fail("additional_parameter_required", name)
    return _SignatureLayout(
        live_positional_defaults,
        live_keyword_defaults,
    )


def _constant_environment(
    function: types.FunctionType,
    layout: _SignatureLayout,
    bound_arguments: Mapping[str, object],
) -> tuple[dict[str, object], tuple[_RuntimeDependency, ...]]:
    defaults = function.__defaults__ or ()
    environment: dict[str, object] = {}
    dependencies: list[_RuntimeDependency] = []
    if defaults:
        for name, value in zip(
            layout.positional_default_names,
            defaults,
            strict=True,
        ):
            dependencies.append(
                _portable_dependency(
                    "positional_default",
                    name,
                    None,
                    value,
                    layout=layout.positional_default_names,
                )
            )
            environment[name] = value
    keyword_defaults = function.__kwdefaults__ or {}
    for name in layout.keyword_default_names:
        value = keyword_defaults[name]
        dependencies.append(
            _portable_dependency(
                "keyword_default",
                name,
                None,
                value,
                layout=layout.keyword_default_names,
            )
        )
        environment[name] = value
    for name in sorted(bound_arguments):
        value = bound_arguments[name]
        dependencies.append(
            _portable_dependency("bound_argument", name, None, value)
        )
        environment[name] = value
    for index, (name, cell) in enumerate(
        zip(
            function.__code__.co_freevars,
            function.__closure__ or (),
            strict=True,
        )
    ):
        try:
            value = cell.cell_contents
        except ValueError:
            _fail("closure_cell_empty", name)
        dependencies.append(
            _portable_dependency("closure", name, index, value)
        )
        environment[name] = value
    for name in function.__code__.co_names:
        if name not in function.__globals__:
            continue
        value = function.__globals__[name]
        if type(value) in _PORTABLE_CONSTANT_TYPES:
            dependencies.append(
                _portable_dependency("global", name, None, value)
            )
            environment[name] = value
    return environment, tuple(dependencies)


def _strip_non_empty_side_exit(
    statements: list[ast.stmt],
    *,
    input_name: str,
    input_type: TypeSpec,
    allow_guarded_region: bool,
) -> tuple[list[ast.stmt], TypedEntryGuard | None]:
    if not statements:
        return statements, None
    candidate = statements[0]
    if not (
        isinstance(candidate, ast.If)
        and not candidate.orelse
        and isinstance(candidate.test, ast.UnaryOp)
        and isinstance(candidate.test.op, ast.Not)
        and isinstance(candidate.test.operand, ast.Name)
        and candidate.test.operand.id == input_name
        and len(candidate.body) == 1
        and isinstance(candidate.body[0], ast.Return)
    ):
        return statements, None
    if not allow_guarded_region:
        return statements, None
    return statements[1:], TypedEntryGuard("non_empty_sequence", input_type)


class _ReplaceLoadedName(ast.NodeTransformer):
    def __init__(self, name: str, replacement: ast.expr) -> None:
        self._name = name
        self._replacement = replacement
        self.replacements = 0

    def visit_Name(self, node: ast.Name):  # noqa: N802 - ast API
        if isinstance(node.ctx, ast.Load) and node.id == self._name:
            self.replacements += 1
            return ast.copy_location(
                copy.deepcopy(self._replacement),
                node,
            )
        return node


def _inline_single_reduction_binding(
    statements: list[ast.stmt],
) -> list[ast.stmt]:
    """Normalize ``tmp = sum(...); return f(tmp)`` into one return expression."""

    if (
        len(statements) != 2
        or not isinstance(statements[0], ast.Assign)
        or len(statements[0].targets) != 1
        or not isinstance(statements[0].targets[0], ast.Name)
        or not isinstance(statements[1], ast.Return)
        or statements[1].value is None
    ):
        return statements
    assignment = statements[0]
    try:
        _contains_sum(assignment.value)
    except TypedCaptureError:
        return statements
    replacer = _ReplaceLoadedName(
        assignment.targets[0].id,
        assignment.value,
    )
    value = replacer.visit(ast.fix_missing_locations(statements[1].value))
    if replacer.replacements != 1 or not isinstance(value, ast.expr):
        return statements
    return [ast.copy_location(ast.Return(value), statements[1])]


def _builtin_dependencies(
    function: types.FunctionType,
    node: ast.FunctionDef,
) -> tuple[_RuntimeDependency, ...]:
    call_names = {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id in {"len", "sum", "str"}
    }
    if any(
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "maketrans"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "str"
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    ):
        call_names.add("str")
    dependencies: list[_RuntimeDependency] = []
    local_names = {
        *function.__code__.co_varnames,
        *function.__code__.co_cellvars,
        *function.__code__.co_freevars,
    }
    for name in sorted(call_names):
        if name in local_names:
            _fail("builtin_binding_not_exact", name)
        dependency = _RuntimeDependency(
            "builtin",
            name,
            None,
            getattr(builtins, name),
            True,
        )
        if not dependency.matches(function):
            _fail("builtin_binding_not_exact", name)
        dependencies.append(dependency)
    return tuple(dependencies)


def _strip_docstring(statements: list[ast.stmt]) -> list[ast.stmt]:
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        return statements[1:]
    return statements


def _predicate(
    expression: ast.expr,
    *,
    item_name: str,
    constants: dict[str, object],
) -> _PredicatePlan:
    if (
        isinstance(expression, ast.Call)
        and not expression.args
        and not expression.keywords
        and isinstance(expression.func, ast.Attribute)
        and isinstance(expression.func.value, ast.Name)
        and expression.func.value.id == item_name
        and expression.func.attr in _PROPERTY_METHODS
    ):
        return _PredicatePlan(
            "unicode_property",
            _PROPERTY_METHODS[expression.func.attr],
        )
    if (
        isinstance(expression, ast.Compare)
        and len(expression.ops) == 1
        and len(expression.comparators) == 1
        and isinstance(expression.left, ast.Name)
        and expression.left.id == item_name
        and type(expression.ops[0]) in _COMPARE
    ):
        value = _resolve_constant(expression.comparators[0], constants)
        return _PredicatePlan(
            "compare",
            _COMPARE[type(expression.ops[0])],
            value,
        )
    _fail("predicate_unsupported")


def _resolve_constant(expression: ast.expr, constants: dict[str, object]) -> object:
    if isinstance(expression, ast.Constant):
        value = expression.value
    elif isinstance(expression, ast.Name) and expression.id in constants:
        value = constants[expression.id]
    else:
        _fail("constant_expression_unsupported")
    if type(value) not in {bool, int, float}:
        _fail("numeric_constant_required")
    return value


def _contains_sum(expression: ast.expr) -> tuple[ast.Call, ast.GeneratorExp]:
    matches: list[tuple[ast.Call, ast.GeneratorExp]] = []
    for node in ast.walk(expression):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sum"
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.GeneratorExp)
        ):
            matches.append((node, node.args[0]))
    if len(matches) != 1:
        _fail("generator_reduction_shape_unsupported")
    return matches[0]


def _parse_generator(
    input_name: str,
    expression: ast.expr,
    constants: dict[str, object],
    *,
    function_source_line: int,
) -> _LoopPlan:
    call, generator = _contains_sum(expression)
    if len(generator.generators) != 1:
        _fail("generator_nesting_unsupported")
    comprehension = generator.generators[0]
    if (
        comprehension.is_async
        or not isinstance(comprehension.target, ast.Name)
        or not isinstance(comprehension.iter, ast.Name)
        or comprehension.iter.id != input_name
        or len(comprehension.ifs) > 1
    ):
        _fail("generator_iteration_shape_unsupported")
    item_name = comprehension.target.id
    predicate = (
        None
        if not comprehension.ifs
        else _predicate(
            comprehension.ifs[0],
            item_name=item_name,
            constants=constants,
        )
    )
    if isinstance(generator.elt, ast.Constant) and generator.elt.value == 1:
        update = "count"
    elif isinstance(generator.elt, ast.Name) and generator.elt.id == item_name:
        update = "sum"
    else:
        _fail("generator_reduction_value_unsupported")
    predicate_line = (
        None
        if not comprehension.ifs
        else comprehension.ifs[0].lineno
    )
    return _LoopPlan(
        input_name=input_name,
        item_name=item_name,
        accumulator_name="__accumulator",
        initial_value=0,
        update=update,
        predicate=predicate,
        return_expression=expression,
        reduction_call=call,
        function_source_line=function_source_line,
        initial_source_line=call.lineno,
        loop_source_line=getattr(comprehension, "lineno", call.lineno),
        predicate_source_line=predicate_line,
        update_source_line=generator.elt.lineno,
        return_source_line=expression.lineno,
    )


def _augmented_update(
    statement: ast.stmt,
    *,
    accumulator_name: str,
    item_name: str,
) -> str:
    if (
        not isinstance(statement, ast.AugAssign)
        or not isinstance(statement.target, ast.Name)
        or statement.target.id != accumulator_name
        or not isinstance(statement.op, ast.Add)
    ):
        _fail("loop_body_effect_unsupported")
    if isinstance(statement.value, ast.Constant) and statement.value.value == 1:
        return "count"
    if isinstance(statement.value, ast.Name) and statement.value.id == item_name:
        return "sum"
    _fail("loop_update_unsupported")


def _parse_explicit(
    input_name: str,
    statements: list[ast.stmt],
    constants: dict[str, object],
    *,
    function_source_line: int,
) -> _LoopPlan:
    if (
        len(statements) != 3
        or not isinstance(statements[0], ast.Assign)
        or len(statements[0].targets) != 1
        or not isinstance(statements[0].targets[0], ast.Name)
        or not isinstance(statements[0].value, ast.Constant)
        or type(statements[0].value.value) not in {int, float}
        or not isinstance(statements[1], ast.For)
        or statements[1].orelse
        or not isinstance(statements[2], ast.Return)
        or statements[2].value is None
    ):
        _fail("explicit_loop_shape_unsupported")
    accumulator_name = statements[0].targets[0].id
    loop = statements[1]
    if (
        not isinstance(loop.target, ast.Name)
        or not isinstance(loop.iter, ast.Name)
        or loop.iter.id != input_name
    ):
        _fail("explicit_iteration_shape_unsupported")
    item_name = loop.target.id
    predicate: _PredicatePlan | None = None
    predicate_source_line: int | None = None
    if len(loop.body) != 1:
        _fail("loop_body_effect_unsupported")
    update_statement = loop.body[0]
    if isinstance(update_statement, ast.If):
        if update_statement.orelse or len(update_statement.body) != 1:
            _fail("loop_body_effect_unsupported")
        predicate = _predicate(
            update_statement.test,
            item_name=item_name,
            constants=constants,
        )
        predicate_source_line = update_statement.test.lineno
        update_statement = update_statement.body[0]
    update = _augmented_update(
        update_statement,
        accumulator_name=accumulator_name,
        item_name=item_name,
    )
    return _LoopPlan(
        input_name=input_name,
        item_name=item_name,
        accumulator_name=accumulator_name,
        initial_value=statements[0].value.value,
        update=update,
        predicate=predicate,
        return_expression=statements[2].value,
        reduction_call=None,
        function_source_line=function_source_line,
        initial_source_line=statements[0].lineno,
        loop_source_line=loop.lineno,
        predicate_source_line=predicate_source_line,
        update_source_line=update_statement.lineno,
        return_source_line=statements[2].lineno,
    )


def _translation_table(call: ast.Call) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "maketrans"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "str"
        and len(call.args) == 1
        and not call.keywords
        and isinstance(call.args[0], ast.Dict)
    ):
        _fail("translation_table_unsupported")
    entries: dict[int, int] = {}
    for key_node, value_node in zip(
        call.args[0].keys,
        call.args[0].values,
        strict=True,
    ):
        if (
            key_node is None
            or not isinstance(key_node, ast.Constant)
            or type(key_node.value) is not str
            or len(key_node.value) != 1
        ):
            _fail("translation_key_unsupported")
        if (
            not isinstance(value_node, ast.Constant)
            or type(value_node.value) is not str
            or len(value_node.value) != 1
        ):
            _fail("translation_value_unsupported")
        entries[ord(key_node.value)] = ord(value_node.value)
    ordered = tuple(sorted(entries.items()))
    return (
        tuple(key for key, _ in ordered),
        tuple(value for _, value in ordered),
    )


def _translation_plan(
    input_name: str,
    statements: list[ast.stmt],
    *,
    function_source_line: int,
) -> _SequenceTransformPlan | None:
    table_call: ast.Call | None = None
    table_name: str | None = None
    setup_line: int | None = None
    return_statement: ast.Return | None = None
    if (
        len(statements) == 2
        and isinstance(statements[0], ast.Assign)
        and len(statements[0].targets) == 1
        and isinstance(statements[0].targets[0], ast.Name)
        and isinstance(statements[0].value, ast.Call)
        and isinstance(statements[1], ast.Return)
        and statements[1].value is not None
    ):
        table_name = statements[0].targets[0].id
        table_call = statements[0].value
        setup_line = statements[0].lineno
        return_statement = statements[1]
    elif (
        len(statements) == 1
        and isinstance(statements[0], ast.Return)
        and statements[0].value is not None
    ):
        return_statement = statements[0]
    else:
        return None

    expression = return_statement.value
    if not (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr == "translate"
        and isinstance(expression.func.value, ast.Name)
        and expression.func.value.id == input_name
        and len(expression.args) == 1
        and not expression.keywords
    ):
        return None
    if table_call is not None and table_name == input_name:
        _fail("translation_input_binding_rebound")
    argument = expression.args[0]
    if table_call is None:
        if not isinstance(argument, ast.Call):
            _fail("translation_table_unsupported")
        table_call = argument
        setup_line = argument.lineno
    elif not (
        isinstance(argument, ast.Name)
        and argument.id == table_name
    ):
        _fail("translation_table_binding_mismatch")
    keys, values = _translation_table(table_call)
    assert setup_line is not None
    return _ImmutableLookupTransformPlan(
        input_name=input_name,
        function_source_line=function_source_line,
        setup_source_line=setup_line,
        loop_source_line=expression.lineno,
        transform_source_line=expression.lineno,
        return_source_line=return_statement.lineno,
        lookup_keys=keys,
        lookup_values=values,
    )


def _regex_fsm_plan(
    function: types.FunctionType,
    input_name: str,
    statements: list[ast.stmt],
    *,
    function_source_line: int,
) -> tuple[_SequenceTransformPlan, _RuntimeDependency] | None:
    if not (
        len(statements) == 1
        and isinstance(statements[0], ast.Return)
        and statements[0].value is not None
    ):
        return None
    return_statement = statements[0]
    strip_call = return_statement.value
    if not (
        isinstance(strip_call, ast.Call)
        and isinstance(strip_call.func, ast.Attribute)
        and strip_call.func.attr == "strip"
        and not strip_call.args
        and not strip_call.keywords
    ):
        return None
    sub_call = strip_call.func.value
    if not (
        isinstance(sub_call, ast.Call)
        and isinstance(sub_call.func, ast.Attribute)
        and sub_call.func.attr == "sub"
        and isinstance(sub_call.func.value, ast.Name)
        and len(sub_call.args) == 2
        and not sub_call.keywords
        and isinstance(sub_call.args[1], ast.Name)
        and sub_call.args[1].id == input_name
    ):
        return None
    regex_name = sub_call.func.value.id
    regex = function.__globals__.get(regex_name)
    if type(regex) is not re.Pattern:
        return None
    if regex.pattern != r"\s+" or regex.flags != re.UNICODE:
        _fail("regex_language_unsupported")
    replacement = sub_call.args[0]
    if (
        not isinstance(replacement, ast.Constant)
        or type(replacement.value) is not str
        or len(replacement.value) != 1
        or not replacement.value.isspace()
    ):
        _fail("regex_replacement_unsupported")

    # Boolean class 0 is non-space and class 1 is space.  The table describes
    # leading, emitted, and pending-space states without naming an operator.
    transitions = (1, 0, 1, 2, 1, 2)
    actions = (1, 0, 1, 0, 3, 0)
    emissions = (0, 0, 0, 0, ord(replacement.value), 0)
    return (
        _UnicodeFsmTransformPlan(
            input_name=input_name,
            function_source_line=function_source_line,
            setup_source_line=sub_call.lineno,
            loop_source_line=sub_call.lineno,
            transform_source_line=sub_call.lineno,
            return_source_line=return_statement.lineno,
            unicode_property="space",
            initial_state=0,
            state_count=3,
            transitions=transitions,
            actions=actions,
            emissions=emissions,
        ),
        _RuntimeDependency(
            "global_regex",
            regex_name,
            None,
            regex,
            identity=True,
        ),
    )


def _parse_sequence_transform(
    function: types.FunctionType,
    input_name: str,
    statements: list[ast.stmt],
    *,
    function_source_line: int,
) -> tuple[_SequenceTransformPlan, tuple[_RuntimeDependency, ...]] | None:
    translation = _translation_plan(
        input_name,
        statements,
        function_source_line=function_source_line,
    )
    if translation is not None:
        return translation, ()
    regex = _regex_fsm_plan(
        function,
        input_name,
        statements,
        function_source_line=function_source_line,
    )
    if regex is not None:
        plan, dependency = regex
        return plan, (dependency,)
    return None


def _literal_type(value: object) -> TypeSpec:
    if type(value) is bool:
        return BOOL
    if type(value) is int:
        return INT64
    if type(value) is float:
        return FLOAT64
    _fail("literal_type_unsupported")


class _ModuleBuilder:
    def __init__(
        self,
        *,
        function_id: str,
        input_type: TypeSpec,
        plan: _LoopPlan | _SequenceTransformPlan,
        constants: dict[str, object],
        source_first_line: int,
        runtime_dependency_hashes: tuple[str, ...],
    ) -> None:
        self.function_id = function_id
        self.input_type = input_type
        self.element_type = input_type.parameters[0]
        self.plan = plan
        self.constants = constants
        self.source_first_line = source_first_line
        self.runtime_dependency_hashes = runtime_dependency_hashes
        self.operations: dict[str, list[TypedOperation]] = {
            "entry": [],
            "header": [],
            "body": [],
            "exit": [],
        }
        self._next_operation = 0
        self._next_value = 0
        self._constants: dict[tuple[str, str], str] = {}
        self._exception_order = 0

    def source_line(self, relative_line: int | None) -> int:
        return self.source_first_line + (
            self.plan.function_source_line
            if relative_line is None
            else relative_line
        ) - 1

    def operation(
        self,
        block_id: str,
        op: str,
        operands: tuple[str, ...] = (),
        *,
        result_type: TypeSpec | None = None,
        attributes: tuple[tuple[str, str], ...] = (),
        literal: SemanticLiteral | None = None,
        may_raise: bool = False,
        source_offset: int | None = None,
    ) -> str | None:
        operation_id = f"op{self._next_operation}"
        self._next_operation += 1
        result_id = None
        if result_type is not None:
            result_id = f"%v{self._next_value}"
            self._next_value += 1
        exception_order = None
        if may_raise:
            exception_order = self._exception_order
            self._exception_order += 1
        self.operations[block_id].append(
            TypedOperation(
                operation_id,
                block_id,
                op,
                operands,
                result_id,
                result_type,
                EffectKind.PURE,
                may_raise,
                exception_order,
                Determinism.DETERMINISTIC,
                tuple(sorted(attributes)),
                literal,
                self.source_line(source_offset),
            )
        )
        return result_id

    def constant(
        self,
        value: object,
        *,
        source_offset: int | None = None,
    ) -> str:
        literal = SemanticLiteral.from_value(value)  # type: ignore[arg-type]
        key = (literal.kind.value, literal.encoded_value)
        existing = self._constants.get(key)
        if existing is not None:
            return existing
        result = self.operation(
            "entry",
            "constant",
            result_type=_literal_type(value),
            literal=literal,
            source_offset=source_offset,
        )
        assert result is not None
        self._constants[key] = result
        return result

    def _post_expression(
        self,
        expression: ast.expr,
        *,
        accumulator: str,
        input_value: str,
        length: str,
    ) -> tuple[str, TypeSpec]:
        if isinstance(expression, ast.Name):
            if expression.id in {self.plan.accumulator_name, "__accumulator"}:
                return accumulator, _literal_type(self.plan.initial_value)
            if expression.id == self.plan.input_name:
                return input_value, self.input_type
            if expression.id in self.constants:
                value = self.constants[expression.id]
                return self.constant(
                    value,
                    source_offset=expression.lineno,
                ), _literal_type(value)
            _fail("return_name_unsupported", expression.id)
        if isinstance(expression, ast.Constant):
            value = _resolve_constant(expression, self.constants)
            return self.constant(
                value,
                source_offset=expression.lineno,
            ), _literal_type(value)
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Name)
            and expression.func.id == "len"
            and len(expression.args) == 1
            and not expression.keywords
            and isinstance(expression.args[0], ast.Name)
            and expression.args[0].id == self.plan.input_name
        ):
            return length, INT64
        if (
            expression is self.plan.reduction_call
        ):
            return accumulator, _literal_type(self.plan.initial_value)
        if isinstance(expression, ast.BinOp) and type(expression.op) in _BINARY:
            left, left_type = self._post_expression(
                expression.left,
                accumulator=accumulator,
                input_value=input_value,
                length=length,
            )
            right, right_type = self._post_expression(
                expression.right,
                accumulator=accumulator,
                input_value=input_value,
                length=length,
            )
            operation = _BINARY[type(expression.op)]
            if left_type != right_type:
                _fail("binary_type_mismatch")
            result_type = FLOAT64 if operation == "binary.truediv" else left_type
            result = self.operation(
                "exit",
                operation,
                (left, right),
                result_type=result_type,
                may_raise=operation == "binary.truediv",
                source_offset=expression.lineno,
            )
            assert result is not None
            return result, result_type
        if (
            isinstance(expression, ast.Compare)
            and len(expression.ops) == 1
            and len(expression.comparators) == 1
            and type(expression.ops[0]) in _COMPARE
        ):
            left, left_type = self._post_expression(
                expression.left,
                accumulator=accumulator,
                input_value=input_value,
                length=length,
            )
            right, right_type = self._post_expression(
                expression.comparators[0],
                accumulator=accumulator,
                input_value=input_value,
                length=length,
            )
            if left_type != right_type:
                _fail("compare_type_mismatch")
            result = self.operation(
                "exit",
                _COMPARE[type(expression.ops[0])],
                (left, right),
                result_type=BOOL,
                source_offset=expression.lineno,
            )
            assert result is not None
            return result, BOOL
        _fail("return_expression_unsupported", type(expression).__name__)

    def build(self) -> TypedSemanticModule:
        if not isinstance(self.plan, _LoopPlan):
            _fail("reduction_plan_required")
        if self.input_type.kind is not TypeKind.SEQUENCE:
            _fail("input_type_not_sequence")
        accumulator_type = _literal_type(self.plan.initial_value)
        input_value = self.operation(
            "entry",
            "argument",
            result_type=self.input_type,
            attributes=(("index", "0"),),
            source_offset=self.plan.function_source_line,
        )
        assert input_value is not None
        initial = self.constant(
            self.plan.initial_value,
            source_offset=self.plan.initial_source_line,
        )
        index_zero = self.constant(
            0,
            source_offset=self.plan.loop_source_line,
        )
        accumulator_zero = self.constant(
            0 if accumulator_type == INT64 else 0.0,
            source_offset=(
                self.plan.predicate_source_line
                or self.plan.update_source_line
            ),
        )
        one = self.constant(1, source_offset=self.plan.loop_source_line)
        length = self.operation(
            "entry",
            "sequence.length",
            (input_value,),
            result_type=INT64,
            source_offset=self.plan.loop_source_line,
        )
        assert length is not None

        # Pre-materialize constants referenced by the post expression so every
        # value is defined before the entry terminator.
        for node in ast.walk(self.plan.return_expression):
            if isinstance(node, ast.Constant) and type(node.value) in {bool, int, float}:
                self.constant(node.value, source_offset=node.lineno)
            elif isinstance(node, ast.Name) and node.id in self.constants:
                self.constant(
                    self.constants[node.id],
                    source_offset=node.lineno,
                )
        if self.plan.predicate is not None and self.plan.predicate.constant is not None:
            self.constant(
                self.plan.predicate.constant,
                source_offset=self.plan.predicate_source_line,
            )
        self.operation(
            "entry",
            "jump",
            attributes=(("target_block", "header"),),
            source_offset=self.plan.loop_source_line,
        )

        continue_value = self.operation(
            "header",
            "compare.lt",
            ("%index", length),
            result_type=BOOL,
            source_offset=self.plan.loop_source_line,
        )
        assert continue_value is not None
        self.operation(
            "header",
            "branch",
            (continue_value,),
            attributes=(
                ("false_block", "exit"),
                ("true_block", "body"),
            ),
            source_offset=self.plan.loop_source_line,
        )

        item = self.operation(
            "body",
            "sequence.get",
            (input_value, "%index"),
            result_type=self.element_type,
            may_raise=True,
            source_offset=self.plan.loop_source_line,
        )
        assert item is not None
        update_value = item
        if self.plan.predicate is not None:
            predicate = self.plan.predicate
            if predicate.kind == "unicode_property":
                match = self.operation(
                    "body",
                    "unicode.property",
                    (item,),
                    result_type=BOOL,
                    attributes=(("property", predicate.operation),),
                    source_offset=self.plan.predicate_source_line,
                )
            else:
                literal = SemanticLiteral.from_value(predicate.constant)  # type: ignore[arg-type]
                constant = self._constants[
                    (literal.kind.value, literal.encoded_value)
                ]
                match = self.operation(
                    "body",
                    predicate.operation,
                    (item, constant),
                    result_type=BOOL,
                    source_offset=self.plan.predicate_source_line,
                )
            assert match is not None
            if self.plan.update == "count":
                update_value = self.operation(
                    "body",
                    "cast",
                    (match,),
                    result_type=INT64,
                    attributes=(("target", "int64"),),
                    source_offset=self.plan.update_source_line,
                )
            else:
                update_value = self.operation(
                    "body",
                    "select",
                    (match, item, accumulator_zero),
                    result_type=accumulator_type,
                    source_offset=self.plan.update_source_line,
                )
            assert update_value is not None
        elif self.plan.update == "count":
            update_value = one
        if accumulator_type != self.element_type and self.plan.update == "sum":
            _fail("reduction_element_type_mismatch")
        next_accumulator = self.operation(
            "body",
            "binary.add",
            ("%accumulator", update_value),
            result_type=accumulator_type,
            source_offset=self.plan.update_source_line,
        )
        next_index = self.operation(
            "body",
            "binary.add",
            ("%index", one),
            result_type=INT64,
            source_offset=self.plan.loop_source_line,
        )
        assert next_accumulator is not None and next_index is not None
        self.operation(
            "body",
            "jump",
            attributes=(("target_block", "header"),),
            source_offset=self.plan.loop_source_line,
        )

        result, output_type = self._post_expression(
            self.plan.return_expression,
            accumulator="%result_accumulator",
            input_value=input_value,
            length=length,
        )
        return_operation = self.operation(
            "exit",
            "return",
            (result,),
            source_offset=self.plan.return_source_line,
        )
        assert return_operation is None
        flat_operations = tuple(
            operation
            for block_id in ("entry", "header", "body", "exit")
            for operation in self.operations[block_id]
        )
        blocks = tuple(
            TypedBlock(
                block_id,
                arguments,
                tuple(operation.operation_id for operation in self.operations[block_id]),
            )
            for block_id, arguments in (
                ("entry", ()),
                (
                    "header",
                    (
                        TypedBlockArgument("%index", INT64),
                        TypedBlockArgument("%accumulator", accumulator_type),
                    ),
                ),
                ("body", ()),
                (
                    "exit",
                    (TypedBlockArgument("%result_accumulator", accumulator_type),),
                ),
            )
        )
        return build_typed_module(
            function_id=self.function_id,
            entry_block="entry",
            input_types=(self.input_type,),
            output_type=output_type,
            blocks=blocks,
            control_edges=(
                TypedControlEdge(
                    "entry",
                    "header",
                    "jump",
                    (index_zero, initial),
                ),
                TypedControlEdge("header", "body", "branch_true"),
                TypedControlEdge(
                    "header",
                    "exit",
                    "branch_false",
                    ("%accumulator",),
                ),
                TypedControlEdge(
                    "body",
                    "header",
                    "jump",
                    (next_index, next_accumulator),
                ),
            ),
            operations=flat_operations,
            return_operation_id=self.operations["exit"][-1].operation_id,
            runtime_dependency_hashes=self.runtime_dependency_hashes,
        )

    def build_sequence_transform(self) -> TypedSemanticModule:
        if not isinstance(self.plan, _SequenceTransformPlan):
            _fail("sequence_transform_plan_required")
        plan = self.plan
        if self.input_type != EXACT_UNICODE:
            _fail("exact_unicode_transform_required")
        input_value = self.operation(
            "entry",
            "argument",
            result_type=self.input_type,
            attributes=(("index", "0"),),
            source_offset=plan.function_source_line,
        )
        assert input_value is not None
        zero = self.constant(0, source_offset=plan.loop_source_line)
        one = self.constant(1, source_offset=plan.loop_source_line)
        length = self.operation(
            "entry",
            "sequence.length",
            (input_value,),
            result_type=INT64,
            source_offset=plan.loop_source_line,
        )
        builder = self.operation(
            "entry",
            "sequence.builder.create",
            (length,),
            result_type=UNICODE_BUILDER,
            may_raise=True,
            source_offset=plan.setup_source_line,
        )
        assert length is not None and builder is not None
        initial_state: str | None = None
        if isinstance(plan, _UnicodeFsmTransformPlan):
            initial_state = self.constant(
                plan.initial_state,
                source_offset=plan.setup_source_line,
            )
        self.operation(
            "entry",
            "jump",
            attributes=(("target_block", "header"),),
            source_offset=plan.loop_source_line,
        )

        continue_value = self.operation(
            "header",
            "compare.lt",
            ("%index", length),
            result_type=BOOL,
            source_offset=plan.loop_source_line,
        )
        assert continue_value is not None
        self.operation(
            "header",
            "branch",
            (continue_value,),
            attributes=(("false_block", "exit"), ("true_block", "body")),
            source_offset=plan.loop_source_line,
        )

        item = self.operation(
            "body",
            "sequence.get",
            (input_value, "%index"),
            result_type=UNICODE_SCALAR,
            may_raise=True,
            source_offset=plan.loop_source_line,
        )
        assert item is not None
        if isinstance(plan, _ImmutableLookupTransformPlan):
            mapped = self.operation(
                "body",
                "immutable.lookup",
                (item, item),
                result_type=UNICODE_SCALAR,
                attributes=(
                    ("keys", encode_int_table(plan.lookup_keys)),
                    ("values", encode_int_table(plan.lookup_values)),
                ),
                source_offset=plan.transform_source_line,
            )
            assert mapped is not None
            next_builder = self.operation(
                "body",
                "sequence.builder.append",
                ("%builder", mapped),
                result_type=UNICODE_BUILDER,
                may_raise=True,
                source_offset=plan.transform_source_line,
            )
            next_state = None
        elif isinstance(plan, _UnicodeFsmTransformPlan):
            classification = self.operation(
                "body",
                "unicode.property",
                (item,),
                result_type=BOOL,
                attributes=(("property", plan.unicode_property),),
                source_offset=plan.transform_source_line,
            )
            assert classification is not None
            transition_attributes = (
                ("class_count", "2"),
                ("state_count", str(plan.state_count)),
                ("transitions", encode_int_table(plan.transitions)),
            )
            next_state = self.operation(
                "body",
                "fsm.transition",
                ("%state", classification),
                result_type=INT64,
                attributes=transition_attributes,
                source_offset=plan.transform_source_line,
            )
            next_builder = self.operation(
                "body",
                "sequence.builder.apply",
                ("%builder", item, "%state", classification),
                result_type=UNICODE_BUILDER,
                attributes=(
                    ("actions", encode_int_table(plan.actions)),
                    ("class_count", "2"),
                    ("emissions", encode_int_table(plan.emissions)),
                    ("state_count", str(plan.state_count)),
                ),
                may_raise=True,
                source_offset=plan.transform_source_line,
            )
        else:
            _fail("sequence_transform_kind_unsupported", plan.kind)
        next_index = self.operation(
            "body",
            "binary.add",
            ("%index", one),
            result_type=INT64,
            source_offset=plan.loop_source_line,
        )
        assert next_builder is not None and next_index is not None
        self.operation(
            "body",
            "jump",
            attributes=(("target_block", "header"),),
            source_offset=plan.loop_source_line,
        )

        result = self.operation(
            "exit",
            "sequence.builder.finish",
            ("%result_builder",),
            result_type=EXACT_UNICODE,
            may_raise=True,
            source_offset=plan.return_source_line,
        )
        assert result is not None
        self.operation(
            "exit",
            "return",
            (result,),
            source_offset=plan.return_source_line,
        )
        header_arguments = [
            TypedBlockArgument("%index", INT64),
            TypedBlockArgument("%builder", UNICODE_BUILDER),
        ]
        entry_arguments = [zero, builder]
        backedge_arguments = [next_index, next_builder]
        if initial_state is not None:
            assert next_state is not None
            header_arguments.append(TypedBlockArgument("%state", INT64))
            entry_arguments.append(initial_state)
            backedge_arguments.append(next_state)
        flat_operations = tuple(
            operation
            for block_id in ("entry", "header", "body", "exit")
            for operation in self.operations[block_id]
        )
        blocks = tuple(
            TypedBlock(
                block_id,
                arguments,
                tuple(
                    operation.operation_id
                    for operation in self.operations[block_id]
                ),
            )
            for block_id, arguments in (
                ("entry", ()),
                ("header", tuple(header_arguments)),
                ("body", ()),
                (
                    "exit",
                    (TypedBlockArgument("%result_builder", UNICODE_BUILDER),),
                ),
            )
        )
        return build_typed_module(
            function_id=self.function_id,
            entry_block="entry",
            input_types=(self.input_type,),
            output_type=EXACT_UNICODE,
            blocks=blocks,
            control_edges=(
                TypedControlEdge(
                    "entry",
                    "header",
                    "jump",
                    tuple(entry_arguments),
                ),
                TypedControlEdge("header", "body", "branch_true"),
                TypedControlEdge(
                    "header",
                    "exit",
                    "branch_false",
                    ("%builder",),
                ),
                TypedControlEdge(
                    "body",
                    "header",
                    "jump",
                    tuple(backedge_arguments),
                ),
            ),
            operations=flat_operations,
            return_operation_id=self.operations["exit"][-1].operation_id,
            runtime_dependency_hashes=self.runtime_dependency_hashes,
        )


def capture_typed_loop(
    function: types.FunctionType,
    *,
    input_types: tuple[TypeSpec, ...],
    bound_arguments: Mapping[str, object] | None = None,
    allow_guarded_region: bool = False,
) -> TypedCaptureResult:
    if type(function) is not types.FunctionType:
        _fail("function_required")
    if len(input_types) != 1 or input_types[0].kind is not TypeKind.SEQUENCE:
        _fail("single_sequence_input_required")
    if bound_arguments is None:
        bound_arguments = {}
    elif not isinstance(bound_arguments, Mapping):
        _fail("bound_arguments_invalid")
    try:
        source_lines, source_first_line = inspect.getsourcelines(function)
    except (OSError, TypeError):
        _fail("source_unavailable")
    source = textwrap.dedent("".join(source_lines))
    node = _function_node(source, function)
    _validate_source_identity(source, function)
    positional = [*node.args.posonlyargs, *node.args.args]
    if not positional:
        _fail("function_input_missing")
    input_name = positional[0].arg
    layout = _signature_layout(function, node, bound_arguments)
    identity = code_identity(function)
    constants, constant_dependencies = _constant_environment(
        function,
        layout,
        bound_arguments,
    )
    statements = _strip_docstring(list(node.body))
    statements, entry_guard = _strip_non_empty_side_exit(
        statements,
        input_name=input_name,
        input_type=input_types[0],
        allow_guarded_region=allow_guarded_region,
    )
    statements = _inline_single_reduction_binding(statements)
    transform = _parse_sequence_transform(
        function,
        input_name,
        statements,
        function_source_line=node.lineno,
    )
    transform_dependencies: tuple[_RuntimeDependency, ...] = ()
    normalized_pattern = "iterator_reduction"
    if transform is not None:
        plan, transform_dependencies = transform
        normalized_pattern = f"iterator_{plan.kind}"
    elif (
        len(statements) == 1
        and isinstance(statements[0], ast.Return)
        and statements[0].value is not None
    ):
        plan = _parse_generator(
            input_name,
            statements[0].value,
            constants,
            function_source_line=node.lineno,
        )
    else:
        plan = _parse_explicit(
            input_name,
            statements,
            constants,
            function_source_line=node.lineno,
        )
    dependencies = (
        _RuntimeDependency(
            "code",
            "__code__",
            None,
            function.__code__,
            True,
        ),
        *constant_dependencies,
        *_builtin_dependencies(function, node),
        *transform_dependencies,
    )
    runtime_guard = TypedRuntimeGuard(function, dependencies)
    builder = _ModuleBuilder(
        function_id=identity.sha256,
        input_type=input_types[0],
        plan=plan,
        constants=constants,
        source_first_line=source_first_line,
        runtime_dependency_hashes=runtime_guard.dependency_hashes,
    )
    module = (
        builder.build_sequence_transform()
        if isinstance(plan, _SequenceTransformPlan)
        else builder.build()
    )
    return TypedCaptureResult(
        module,
        analyze_typed_module(module),
        identity.sha256,
        hashlib.sha256(source.encode("utf-8")).hexdigest(),
        source_first_line,
        normalized_pattern,
        runtime_guard.dependency_hashes,
        runtime_guard,
        entry_guard,
    )
