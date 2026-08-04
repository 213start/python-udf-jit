from __future__ import annotations

import ast
import hashlib
import json
import marshal
import types
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import RLock
from typing import Protocol

from python_udf_jit.compiler.typed_analysis import (
    TypedAnalysisBundle,
    _analyze_verified_typed_module,
    analyze_typed_module,
)
from python_udf_jit.compiler.typed_ir import (
    BOOL,
    EXACT_UNICODE,
    FLOAT64,
    INT64,
    UNICODE_BUILDER,
    UNICODE_SCALAR,
    Exactness,
    TypeKind,
    TypeSpec,
    TypedBlockArgument,
    TypedControlEdge,
    TypedOperation,
    TypedSemanticModule,
    decode_int_table,
)
from python_udf_jit.compiler.typed_verifier import verify_typed_module
from python_udf_jit.runtime.negative_cache import NegativeCache


TYPED_LOOP_ADAPTER_VERSION = "typed-loop-adapter-v2"


class TypedGuardMiss(RuntimeError):
    """A speculative exact-type fact did not hold at execution time."""


class TypedLoweringError(ValueError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code if not detail else f"{reason_code}:{detail}")


def _fail(reason_code: str, detail: str = "") -> None:
    raise TypedLoweringError(reason_code, detail)


def _canonical_hash(prefix: bytes, document: object) -> str:
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(prefix + payload).hexdigest()


@dataclass(frozen=True)
class TypedLoopSpecializationPlan:
    format_version: int
    module_hash: str
    analysis_hash: str
    pattern_kind: str
    iterator_strategy: str
    container_type: str
    element_type: str
    reduction_operation: str
    accumulator_type: str
    result_strategy: str
    state_operations: tuple[str, ...]
    predicate_operations: tuple[str, ...]
    required_guards: tuple[str, ...]
    backend_requirements: tuple[str, ...]
    plan_hash: str

    def semantic_document(self) -> dict[str, object]:
        return {
            "accumulator_type": self.accumulator_type,
            "analysis_hash": self.analysis_hash,
            "backend_requirements": list(self.backend_requirements),
            "container_type": self.container_type,
            "element_type": self.element_type,
            "format_version": self.format_version,
            "iterator_strategy": self.iterator_strategy,
            "module_hash": self.module_hash,
            "pattern_kind": self.pattern_kind,
            "predicate_operations": list(self.predicate_operations),
            "reduction_operation": self.reduction_operation,
            "result_strategy": self.result_strategy,
            "required_guards": list(self.required_guards),
            "state_operations": list(self.state_operations),
        }

    def recompute_hash(self) -> str:
        return _canonical_hash(
            b"python-udf-jit-typed-loop-plan-v2\0",
            self.semantic_document(),
        )

    def to_document(self) -> dict[str, object]:
        return {**self.semantic_document(), "plan_hash": self.plan_hash}


@dataclass(frozen=True)
class TypedLoopLowering:
    module_hash: str
    module: TypedSemanticModule
    analysis: TypedAnalysisBundle
    plan: TypedLoopSpecializationPlan
    function: types.FunctionType
    generated_source: str
    generated_ast_text: str
    generated_code_hash: str
    operation_lines: tuple[tuple[str, int], ...]

    @property
    def code_size(self) -> int:
        return len(marshal.dumps(self.function.__code__))


@dataclass(frozen=True)
class BackendCompilation:
    jit_compiled: bool
    execution_mode: str
    hir_opcode_counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.jit_compiled) is not bool
            or not self.execution_mode
            or tuple(sorted(self.hir_opcode_counts)) != self.hir_opcode_counts
            or any(
                not name or type(count) is not int or count < 0
                for name, count in self.hir_opcode_counts
            )
        ):
            raise ValueError("invalid typed backend compilation")


class TypedLoopBackend(Protocol):
    adapter_version: str

    def compile(self, lowering: TypedLoopLowering) -> BackendCompilation: ...


class RuntimeDependencyGuard(Protocol):
    @property
    def dependency_hashes(self) -> tuple[str, ...]: ...

    def matches(self) -> bool: ...


class TypedLoopDiagnosticSink(Protocol):
    def prepare_typed_compilation(
        self,
        function: types.FunctionType,
        generated_code_hash: str,
        operation_lines: tuple[tuple[str, int], ...],
    ) -> str: ...

    def record_typed_region_decision(
        self,
        request: "TypedRegionCompileRequest",
        decision: "TypedCompileDecision",
    ) -> None: ...


class CinderXTypedLoopBackend:
    """Lazy Worker-local bridge; importing this type does not import CinderX."""

    adapter_version = TYPED_LOOP_ADAPTER_VERSION

    def compile(self, lowering: TypedLoopLowering) -> BackendCompilation:
        return self._compile(lowering, None)

    def compile_with_diagnostics(
        self,
        lowering: TypedLoopLowering,
        diagnostic_sink: TypedLoopDiagnosticSink,
    ) -> BackendCompilation:
        return self._compile(lowering, diagnostic_sink)

    @staticmethod
    def _prepare_diagnostics(
        diagnostic_sink: TypedLoopDiagnosticSink | None,
        function: types.FunctionType,
        generated_code_hash: str,
        operation_lines: tuple[tuple[str, int], ...],
    ) -> None:
        if diagnostic_sink is None:
            return
        try:
            diagnostic_sink.prepare_typed_compilation(
                function,
                generated_code_hash,
                operation_lines,
            )
        except Exception:
            # Diagnostics are observational and cannot reject compilation.
            pass

    def _compile(
        self,
        lowering: TypedLoopLowering,
        diagnostic_sink: TypedLoopDiagnosticSink | None,
    ) -> BackendCompilation:
        import cinderx

        initializer = getattr(cinderx, "init", None)
        initialized = getattr(cinderx, "is_initialized", None)
        if callable(initializer) and (
            not callable(initialized) or not initialized()
        ):
            initializer()
        import cinderx.jit

        self._prepare_diagnostics(
            diagnostic_sink,
            lowering.function,
            lowering.generated_code_hash,
            lowering.operation_lines,
        )
        typed_entry = getattr(cinderx.jit, "compile_typed_region", None)
        if not callable(typed_entry):
            return BackendCompilation(
                False,
                "cinderx_generic_typed_hir_unavailable",
            )
        result = typed_entry(
            lowering.function,
            lowering.module.to_document(),
            lowering.plan.to_document(),
        )
        if result is not True:
            return BackendCompilation(False, "cinderx_generic_typed_hir")
        counts = cinderx.jit.get_function_hir_opcode_counts(
            lowering.function
        ) or {}
        normalized_counts = {
            str(name): int(count) for name, count in counts.items()
        }
        return BackendCompilation(
            cinderx.jit.is_jit_compiled(lowering.function),
            "cinderx_generic_typed_hir",
            tuple(sorted(normalized_counts.items())),
        )


class CompileStatus(StrEnum):
    COMPILED = "compiled"
    DEFERRED = "deferred"
    UNSUPPORTED = "unsupported"
    FAILURE = "failure"
    NEGATIVE_CACHE = "negative_cache"


@dataclass(frozen=True)
class RuntimeFeedback:
    call_count: int
    deopt_count: int

    def __post_init__(self) -> None:
        if (
            type(self.call_count) is not int
            or self.call_count < 0
            or type(self.deopt_count) is not int
            or self.deopt_count < 0
        ):
            raise ValueError("invalid typed region runtime feedback")


@dataclass(frozen=True)
class TypedRegionCompileRequest:
    region: TypedSemanticModule
    runtime: RuntimeFeedback
    driver_analysis_hint: object | None = None
    runtime_guard: RuntimeDependencyGuard | None = None


@dataclass(frozen=True)
class CompiledTypedRegion:
    lowering: TypedLoopLowering
    backend: BackendCompilation
    runtime_guard: RuntimeDependencyGuard | None = None

    @property
    def semantic_hash(self) -> str:
        return self.lowering.module_hash

    @property
    def code_hash(self) -> str:
        return self.lowering.generated_code_hash

    @property
    def code_size(self) -> int:
        return self.lowering.code_size

    @property
    def jit_function(self) -> types.FunctionType:
        return self.lowering.function

    @property
    def execution_mode(self) -> str:
        return self.backend.execution_mode

    def __call__(self, *inputs: object) -> object:
        if self.runtime_guard is not None and not self.runtime_guard.matches():
            raise TypedGuardMiss("runtime_dependency_changed")
        return self.jit_function(*inputs)


@dataclass(frozen=True)
class TypedCompileDecision:
    status: CompileStatus
    reason_code: str
    variant: CompiledTypedRegion | None = None
    worker_analysis: TypedAnalysisBundle | None = None
    driver_analysis_hint_matched: bool | None = None


def _value_name(value_id: str) -> str:
    digest = hashlib.sha256(value_id.encode("utf-8")).hexdigest()[:12]
    return f"value_{digest}"


def _exact_python_type(type_spec: TypeSpec) -> type[object] | None:
    if type_spec.kind is TypeKind.SEQUENCE:
        return {"str": str, "list": list, "tuple": tuple, "range": range}.get(
            type_spec.name
        )
    if type_spec.kind is TypeKind.MAPPING:
        return {"dict": dict}.get(type_spec.name)
    return None


def _stamp(node: ast.AST, line: int) -> ast.AST:
    for value in ast.walk(node):
        attributes = getattr(value, "_attributes", ())
        if "lineno" in attributes:
            value.lineno = line
            value.col_offset = 0
            value.end_lineno = line
            value.end_col_offset = 1
    return node


def _stamp_shallow(node: ast.AST, line: int) -> ast.AST:
    attributes = getattr(node, "_attributes", ())
    if "lineno" in attributes:
        node.lineno = line
        node.col_offset = 0
        node.end_lineno = line
        node.end_col_offset = 1
    return node


_BINARY_AST: dict[str, type[ast.operator]] = {
    "binary.add": ast.Add,
    "binary.sub": ast.Sub,
    "binary.mul": ast.Mult,
    "binary.truediv": ast.Div,
}
_COMPARE_AST: dict[str, type[ast.cmpop]] = {
    "compare.eq": ast.Eq,
    "compare.ne": ast.NotEq,
    "compare.lt": ast.Lt,
    "compare.le": ast.LtE,
    "compare.gt": ast.Gt,
    "compare.ge": ast.GtE,
}
_UNICODE_PROPERTY_ID = {
    "alnum": 0,
    "alpha": 1,
    "decimal": 2,
    "digit": 3,
    "numeric": 4,
    "space": 5,
}


def _typed_sequence_length(value: str) -> int:
    return len(value)


def _typed_unicode_read(value: str, index: int) -> int:
    return ord(value[index])


def _typed_unicode_classify(value: int, property_id: int) -> bool:
    methods = (
        "isalnum",
        "isalpha",
        "isdecimal",
        "isdigit",
        "isnumeric",
        "isspace",
    )
    return bool(getattr(chr(value), methods[property_id])())


def _typed_table_get(table: tuple[int, ...], index: int) -> int:
    return table[index]


def _typed_table_lookup(
    keys: tuple[int, ...],
    values: tuple[int, ...],
    key: int,
    default: int,
) -> int:
    table_index = bisect_left(keys, key)
    if table_index == len(keys) or keys[table_index] != key:
        return default
    return values[table_index]


def _typed_builder_create(_capacity: int) -> list[int]:
    return []


def _typed_builder_append(builder: list[int], value: int) -> list[int]:
    builder.append(value)
    return builder


def _typed_builder_finish(builder: list[int]) -> str:
    return "".join(chr(value) for value in builder)


class _CanonicalLoopLowerer:
    def __init__(
        self,
        module: TypedSemanticModule,
        analysis: TypedAnalysisBundle,
    ) -> None:
        self.module = module
        self.analysis = analysis
        self.blocks = {block.block_id: block for block in module.blocks}
        self.operations = {
            operation.operation_id: operation for operation in module.operations
        }
        self.lines: dict[str, int] = {}
        self.next_line = 1000

    def _record(self, operation: TypedOperation, node: ast.stmt) -> ast.stmt:
        line = self.next_line
        self.next_line += 1
        self.lines[operation.operation_id] = line
        return _stamp(node, line)  # type: ignore[return-value]

    def _name(self, value_id: str, context: ast.expr_context) -> ast.Name:
        return ast.Name(id=_value_name(value_id), ctx=context)

    def _expression(self, operation: TypedOperation) -> ast.expr:
        operands = [self._name(value, ast.Load()) for value in operation.operands]
        if operation.op == "argument":
            index = int(operation.attribute("index") or "-1")
            return ast.Name(id=f"arg{index}", ctx=ast.Load())
        if operation.op == "constant":
            if operation.literal is None:
                _fail("constant_literal_missing", operation.operation_id)
            return ast.Constant(value=operation.literal.value)
        if operation.op in _BINARY_AST:
            return ast.BinOp(
                left=operands[0],
                op=_BINARY_AST[operation.op](),
                right=operands[1],
            )
        if operation.op in _COMPARE_AST:
            return ast.Compare(
                left=operands[0],
                ops=[_COMPARE_AST[operation.op]()],
                comparators=[operands[1]],
            )
        if operation.op == "cast":
            target = operation.attribute("target") or ""
            name = {"bool": "bool", "int64": "int", "float64": "float"}.get(
                target
            )
            if name is None:
                _fail("cast_target_unsupported", target)
            return ast.Call(
                func=ast.Name(id=name, ctx=ast.Load()),
                args=[operands[0]],
                keywords=[],
            )
        if operation.op == "select":
            return ast.IfExp(test=operands[0], body=operands[1], orelse=operands[2])
        if operation.op == "sequence.length":
            return ast.Call(
                func=ast.Name(id="_typed_sequence_length", ctx=ast.Load()),
                args=[operands[0]],
                keywords=[],
            )
        if operation.op == "sequence.get":
            if operation.result_type != UNICODE_SCALAR:
                return ast.Subscript(
                    value=operands[0],
                    slice=operands[1],
                    ctx=ast.Load(),
                )
            return ast.Call(
                func=ast.Name(id="_typed_unicode_read", ctx=ast.Load()),
                args=operands,
                keywords=[],
            )
        if operation.op == "mapping.lookup":
            return ast.Subscript(value=operands[0], slice=operands[1], ctx=ast.Load())
        if operation.op == "immutable.lookup":
            keys = decode_int_table(
                operation.attribute("keys") or "",
                max_items=256,
            )
            replacements = decode_int_table(
                operation.attribute("values") or "",
                max_items=256,
            )
            return ast.Call(
                func=ast.Name(id="_typed_table_lookup", ctx=ast.Load()),
                args=[
                    ast.Constant(keys),
                    ast.Constant(replacements),
                    operands[0],
                    operands[0],
                ],
                keywords=[],
            )
        if operation.op == "unicode.property":
            property_id = _UNICODE_PROPERTY_ID.get(
                operation.attribute("property") or ""
            )
            if property_id is None:
                _fail("unicode_property_unsupported", operation.operation_id)
            return ast.Call(
                func=ast.Name(id="_typed_unicode_classify", ctx=ast.Load()),
                args=[operands[0], ast.Constant(property_id)],
                keywords=[],
            )
        if operation.op == "fsm.transition":
            class_count = int(operation.attribute("class_count") or "0")
            transitions = decode_int_table(
                operation.attribute("transitions") or "",
                max_items=128,
                maximum=int(operation.attribute("state_count") or "0") - 1,
            )
            return ast.Call(
                func=ast.Name(id="_typed_table_get", ctx=ast.Load()),
                args=[
                    ast.Constant(transitions),
                    ast.BinOp(
                        ast.BinOp(
                            operands[0],
                            ast.Mult(),
                            ast.Constant(class_count),
                        ),
                        ast.Add(),
                        ast.Call(ast.Name("int", ast.Load()), [operands[1]], []),
                    ),
                ],
                keywords=[],
            )
        if operation.op == "sequence.builder.create":
            return ast.Call(
                func=ast.Name(id="_typed_builder_create", ctx=ast.Load()),
                args=operands,
                keywords=[],
            )
        if operation.op == "sequence.builder.append":
            return ast.Call(
                func=ast.Name(id="_typed_builder_append", ctx=ast.Load()),
                args=operands,
                keywords=[],
            )
        if operation.op == "sequence.builder.apply":
            _fail("builder_apply_requires_control_lowering")
        if operation.op == "sequence.builder.finish":
            return ast.Call(
                func=ast.Name(id="_typed_builder_finish", ctx=ast.Load()),
                args=[operands[0]],
                keywords=[],
            )
        _fail("operation_lowering_unsupported", operation.op)

    def _guard(self, value_id: str, type_spec: TypeSpec) -> list[ast.stmt]:
        value = self._name(value_id, ast.Load())
        invalid: ast.expr | None = None
        if type_spec == BOOL:
            invalid = ast.Compare(
                left=ast.Call(ast.Name("type", ast.Load()), [value], []),
                ops=[ast.IsNot()],
                comparators=[ast.Name("bool", ast.Load())],
            )
        elif type_spec == INT64:
            wrong_type = ast.Compare(
                left=ast.Call(ast.Name("type", ast.Load()), [value], []),
                ops=[ast.IsNot()],
                comparators=[ast.Name("int", ast.Load())],
            )
            too_small = ast.Compare(
                left=value,
                ops=[ast.Lt()],
                comparators=[ast.Constant(-(1 << 63))],
            )
            too_large = ast.Compare(
                left=value,
                ops=[ast.GtE()],
                comparators=[ast.Constant(1 << 63)],
            )
            invalid = ast.BoolOp(ast.Or(), [wrong_type, too_small, too_large])
        elif type_spec == FLOAT64:
            invalid = ast.Compare(
                left=ast.Call(ast.Name("type", ast.Load()), [value], []),
                ops=[ast.IsNot()],
                comparators=[ast.Name("float", ast.Load())],
            )
        elif type_spec == UNICODE_SCALAR:
            wrong_type = ast.Compare(
                left=ast.Call(ast.Name("type", ast.Load()), [value], []),
                ops=[ast.IsNot()],
                comparators=[ast.Name("int", ast.Load())],
            )
            out_of_range = ast.BoolOp(
                ast.Or(),
                [
                    ast.Compare(value, [ast.Lt()], [ast.Constant(0)]),
                    ast.Compare(
                        value,
                        [ast.Gt()],
                        [ast.Constant(0x10FFFF)],
                    ),
                ],
            )
            invalid = ast.BoolOp(ast.Or(), [wrong_type, out_of_range])
        elif type_spec.requires_guard:
            expected = _exact_python_type(type_spec)
            if expected is None:
                _fail("exact_type_guard_unsupported", type_spec.name)
            invalid = ast.Compare(
                left=ast.Call(ast.Name("type", ast.Load()), [value], []),
                ops=[ast.IsNot()],
                comparators=[ast.Name(expected.__name__, ast.Load())],
            )
        if invalid is None:
            return []
        return [
            ast.If(
                test=invalid,
                body=[
                    ast.Raise(
                        exc=ast.Call(
                            ast.Name("_TypedGuardMiss", ast.Load()),
                            [ast.Constant("typed_region_guard_miss")],
                            [],
                        ),
                        cause=None,
                    )
                ],
                orelse=[],
            )
        ]

    def _operation_statements(self, operation: TypedOperation) -> list[ast.stmt]:
        if operation.op in {"branch", "jump", "return"}:
            _fail("terminator_lowered_as_value", operation.operation_id)
        if operation.result_id is None or operation.result_type is None:
            _fail("operation_result_missing", operation.operation_id)
        if operation.op == "sequence.builder.apply":
            return self._builder_apply_statements(operation)
        assignment = ast.Assign(
            targets=[self._name(operation.result_id, ast.Store())],
            value=self._expression(operation),
        )
        result = [self._record(operation, assignment)]
        if operation.op not in {"argument", "constant"}:
            result.extend(self._guard(operation.result_id, operation.result_type))
        return result

    def _builder_apply_statements(
        self,
        operation: TypedOperation,
    ) -> list[ast.stmt]:
        if len(operation.operands) != 4 or operation.result_id is None:
            _fail("builder_apply_shape_invalid", operation.operation_id)
        actions = decode_int_table(
            operation.attribute("actions") or "",
            max_items=128,
            maximum=4,
        )
        emissions = decode_int_table(
            operation.attribute("emissions") or "",
            max_items=128,
        )
        builder, value, state, classification = (
            self._name(operand, ast.Load()) for operand in operation.operands
        )
        suffix = operation.operation_id.replace("-", "_")
        class_count = int(operation.attribute("class_count") or "0")
        index_name = ast.Name(f"_typed_table_index_{suffix}", ast.Store())
        index_load = ast.Name(f"_typed_table_index_{suffix}", ast.Load())
        action_name = ast.Name(f"_typed_action_{suffix}", ast.Store())
        action_load = ast.Name(f"_typed_action_{suffix}", ast.Load())
        emission_name = ast.Name(f"_typed_emission_{suffix}", ast.Store())
        emission_load = ast.Name(f"_typed_emission_{suffix}", ast.Load())

        def append(argument: ast.expr) -> ast.Expr:
            return ast.Expr(
                ast.Call(
                    ast.Name("_typed_builder_append", ast.Load()),
                    [builder, argument],
                    [],
                )
            )

        statements: list[ast.stmt] = [
            ast.Assign(
                [index_name],
                ast.BinOp(
                    ast.BinOp(
                        state,
                        ast.Mult(),
                        ast.Constant(class_count),
                    ),
                    ast.Add(),
                    ast.Call(
                        ast.Name("int", ast.Load()),
                        [classification],
                        [],
                    ),
                ),
            ),
            ast.Assign(
                [action_name],
                ast.Call(
                    ast.Name("_typed_table_get", ast.Load()),
                    [ast.Constant(actions), index_load],
                    [],
                ),
            ),
            ast.Assign(
                [emission_name],
                ast.Call(
                    ast.Name("_typed_table_get", ast.Load()),
                    [ast.Constant(emissions), index_load],
                    [],
                ),
            ),
            ast.If(
                ast.Compare(action_load, [ast.In()], [ast.Constant((2, 3))]),
                [append(emission_load)],
                [],
            ),
            ast.If(
                ast.Compare(
                    action_load,
                    [ast.In()],
                    [ast.Constant((1, 3, 4))],
                ),
                [append(value)],
                [],
            ),
            ast.If(
                ast.Compare(action_load, [ast.Eq()], [ast.Constant(4)]),
                [append(emission_load)],
                [],
            ),
            ast.Assign(
                [self._name(operation.result_id, ast.Store())],
                builder,
            ),
        ]
        line = self.next_line
        self.next_line += 1
        self.lines[operation.operation_id] = line
        return [_stamp(statement, line) for statement in statements]  # type: ignore[misc]

    def _edge(self, source: str, kind: str) -> TypedControlEdge:
        matches = tuple(
            edge
            for edge in self.module.control_edges
            if edge.source_block == source and edge.kind == kind
        )
        if len(matches) != 1:
            _fail("canonical_edge_missing", f"{source}:{kind}")
        return matches[0]

    def build(self) -> tuple[types.FunctionType, str, str, tuple[tuple[str, int], ...]]:
        loops = self.analysis.patterns.loops
        if len(loops) != 1:
            _fail("single_typed_loop_required")
        loop = loops[0]
        header = self.blocks[loop.header]
        if len(loop.latches) != 1:
            _fail("single_loop_latch_required")
        latch_id = loop.latches[0]
        true_edge = self._edge(header.block_id, "branch_true")
        false_edge = self._edge(header.block_id, "branch_false")
        if true_edge.target_block not in loop.blocks:
            _fail("canonical_loop_body_missing")
        body_block = self.blocks[true_edge.target_block]
        exit_block = self.blocks[false_edge.target_block]
        entry_edge = tuple(
            edge
            for edge in self.module.control_edges
            if edge.target_block == header.block_id
            and edge.source_block not in loop.blocks
            and edge.kind == "jump"
        )
        if len(entry_edge) != 1:
            _fail("canonical_preheader_missing")
        backedge = self._edge(latch_id, "jump")
        if backedge.target_block != header.block_id:
            _fail("canonical_backedge_invalid")

        function_body: list[ast.stmt] = []
        for index, type_spec in enumerate(self.module.input_types):
            argument_id = next(
                operation.result_id
                for operation in self.module.operations
                if operation.op == "argument"
                and operation.attribute("index") == str(index)
            )
            if argument_id is None:
                _fail("argument_value_missing", str(index))
            expected = _exact_python_type(type_spec)
            if type_spec.exactness is Exactness.EXACT and expected is not None:
                invalid = ast.Compare(
                    left=ast.Call(
                        ast.Name("type", ast.Load()),
                        [ast.Name(f"arg{index}", ast.Load())],
                        [],
                    ),
                    ops=[ast.IsNot()],
                    comparators=[
                        ast.Name(f"_input_type_{index}", ast.Load())
                    ],
                )
                function_body.append(
                    _stamp(
                        ast.If(
                            invalid,
                            [
                                ast.Raise(
                                    ast.Call(
                                        ast.Name("_TypedGuardMiss", ast.Load()),
                                        [ast.Constant("exact_input_type_guard")],
                                        [],
                                    ),
                                    None,
                                )
                            ],
                            [],
                        ),
                        900 + index,
                    )  # type: ignore[arg-type]
                )

        entry_block = self.blocks[self.module.entry_block]
        entry_terminator: TypedOperation | None = None
        for operation_id in entry_block.operation_ids:
            operation = self.operations[operation_id]
            if operation.op == "jump":
                entry_terminator = operation
            else:
                function_body.extend(self._operation_statements(operation))
        if entry_terminator is None:
            _fail("canonical_entry_terminator_missing")

        initial_assignment = ast.Assign(
            targets=[
                ast.Tuple(
                    [self._name(argument.value_id, ast.Store()) for argument in header.arguments],
                    ast.Store(),
                )
            ],
            value=ast.Tuple(
                [self._name(value, ast.Load()) for value in entry_edge[0].arguments],
                ast.Load(),
            ),
        )
        function_body.append(self._record(entry_terminator, initial_assignment))

        loop_body: list[ast.stmt] = []
        header_terminator: TypedOperation | None = None
        for operation_id in header.operation_ids:
            operation = self.operations[operation_id]
            if operation.op == "branch":
                header_terminator = operation
            else:
                loop_body.extend(self._operation_statements(operation))
        if header_terminator is None or len(header_terminator.operands) != 1:
            _fail("canonical_header_terminator_missing")
        break_statement = ast.If(
            test=ast.UnaryOp(
                ast.Not(),
                self._name(header_terminator.operands[0], ast.Load()),
            ),
            body=[ast.Break()],
            orelse=[],
        )
        loop_body.append(self._record(header_terminator, break_statement))

        body_terminator: TypedOperation | None = None
        for operation_id in body_block.operation_ids:
            operation = self.operations[operation_id]
            if operation.op == "jump":
                body_terminator = operation
            else:
                loop_body.extend(self._operation_statements(operation))
        if body_terminator is None:
            _fail("canonical_body_terminator_missing")
        update = ast.Assign(
            targets=[
                ast.Tuple(
                    [self._name(argument.value_id, ast.Store()) for argument in header.arguments],
                    ast.Store(),
                )
            ],
            value=ast.Tuple(
                [self._name(value, ast.Load()) for value in backedge.arguments],
                ast.Load(),
            ),
        )
        loop_body.append(self._record(body_terminator, update))
        function_body.append(
            _stamp_shallow(
                ast.While(ast.Constant(True), loop_body, []),
                950,
            )  # type: ignore[arg-type]
        )

        if len(exit_block.arguments) != len(false_edge.arguments):
            _fail("canonical_exit_arguments_invalid")
        if exit_block.arguments:
            exit_assignment = ast.Assign(
                targets=[
                    ast.Tuple(
                        [
                            self._name(argument.value_id, ast.Store())
                            for argument in exit_block.arguments
                        ],
                        ast.Store(),
                    )
                ],
                value=ast.Tuple(
                    [self._name(value, ast.Load()) for value in false_edge.arguments],
                    ast.Load(),
                ),
            )
            function_body.append(_stamp(exit_assignment, 960))  # type: ignore[arg-type]

        for operation_id in exit_block.operation_ids:
            operation = self.operations[operation_id]
            if operation.op == "return":
                if len(operation.operands) != 1:
                    _fail("canonical_return_invalid")
                statement = ast.Return(self._name(operation.operands[0], ast.Load()))
                function_body.append(self._record(operation, statement))
            else:
                function_body.extend(self._operation_statements(operation))

        missing = set(self.operations) - set(self.lines)
        if missing:
            _fail("unmapped_semantic_operations", ",".join(sorted(missing)))
        function_node = ast.FunctionDef(
            name="_typed_region",
            args=ast.arguments(
                posonlyargs=[],
                args=[ast.arg(arg=f"arg{index}") for index in range(len(self.module.input_types))],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=function_body,
            decorator_list=[],
        )
        module_node = ast.fix_missing_locations(
            ast.Module(body=[function_node], type_ignores=[])
        )
        filename = f"<udfjit-typed-{self.module.semantic_hash[:16]}>"
        code = compile(module_node, filename, "exec")
        globals_namespace: dict[str, object] = {
            "_TypedGuardMiss": TypedGuardMiss,
            "_typed_builder_append": _typed_builder_append,
            "_typed_builder_create": _typed_builder_create,
            "_typed_builder_finish": _typed_builder_finish,
            "_typed_sequence_length": _typed_sequence_length,
            "_typed_table_get": _typed_table_get,
            "_typed_table_lookup": _typed_table_lookup,
            "_typed_unicode_classify": _typed_unicode_classify,
            "_typed_unicode_read": _typed_unicode_read,
            "__builtins__": __builtins__,
        }
        for index, type_spec in enumerate(self.module.input_types):
            expected = _exact_python_type(type_spec)
            if expected is not None:
                globals_namespace[f"_input_type_{index}"] = expected
        namespace: dict[str, object] = {}
        exec(code, globals_namespace, namespace)
        function = namespace["_typed_region"]
        if not isinstance(function, types.FunctionType):
            raise RuntimeError("typed lowering did not create a function")
        function.__module__ = "python_udf_jit.generated"
        function.__qualname__ = "_typed_region"
        function.__dict__["__udf_jit_typed_region__"] = self.module.semantic_hash
        source = ast.unparse(module_node)
        return (
            function,
            source,
            ast.dump(module_node, annotate_fields=True, include_attributes=False, indent=2),
            tuple(sorted(self.lines.items())),
        )



def _specialization_plan(
    module: TypedSemanticModule,
    analysis: TypedAnalysisBundle,
) -> TypedLoopSpecializationPlan:
    if len(module.input_types) != 1:
        _fail("single_sequence_input_required")
    input_type = module.input_types[0]
    if input_type.kind is not TypeKind.SEQUENCE or len(input_type.parameters) != 1:
        _fail("typed_sequence_required")
    if input_type.exactness is not Exactness.EXACT:
        _fail("exact_sequence_required")
    if len(analysis.patterns.loops) != 1 or len(analysis.patterns.reductions) > 1:
        _fail("single_typed_loop_required")
    loop = analysis.patterns.loops[0]
    blocks = {block.block_id: block for block in module.blocks}
    if analysis.patterns.reductions:
        reduction = analysis.patterns.reductions[0]
        accumulator_type = next(
            argument.type
            for argument in blocks[loop.header].arguments
            if argument.value_id == reduction.accumulator
        )
        reduction_operation = reduction.operation
        result_strategy = "scalar_reduction"
    elif analysis.patterns.builder_operations:
        accumulator_type = next(
            argument.type
            for argument in blocks[loop.header].arguments
            if argument.type.kind is TypeKind.BUILDER
        )
        reduction_operation = "none"
        result_strategy = "sequence_builder"
    else:
        _fail("typed_loop_result_strategy_unsupported")
    iterator_strategy = {
        "str": "unicode_storage",
        "list": "exact_list_elements",
        "tuple": "exact_tuple_elements",
        "range": "exact_range_values",
    }.get(input_type.name)
    if iterator_strategy is None:
        _fail("sequence_specialization_unsupported", input_type.name)
    predicates = tuple(
        sorted(
            operation.op
            + (
                f":{operation.attribute('property')}"
                if operation.op == "unicode.property"
                else ""
            )
            for operation in module.operations
            if operation.op.startswith("compare.")
            or operation.op
            in {"fsm.transition", "immutable.lookup", "select", "unicode.property"}
        )
    )
    state_operations = tuple(
        sorted(
            operation.op
            for operation in module.operations
            if operation.op == "fsm.transition"
            or operation.op.startswith("sequence.builder")
        )
    )
    guards = tuple(
        sorted(
            {
                f"exact:{input_type.name}",
                *(
                    (f"element:{input_type.parameters[0].name}",)
                    if input_type.name != "str"
                    else ()
                ),
            }
        )
    )
    requirements = {
        "cfg_phi",
        "exact_type_guard",
        "typed_sequence_iteration",
    }
    if analysis.patterns.reductions:
        requirements.add("unboxed_accumulator")
    if analysis.patterns.builder_operations:
        requirements.add("sequence_builder")
    if analysis.patterns.immutable_lookup_operations:
        requirements.add("immutable_lookup")
    if analysis.patterns.fsm_operations:
        requirements.add("table_fsm")
    if any(operation.op == "unicode.property" for operation in module.operations):
        requirements.add("unicode_property_primitive")
    provisional = TypedLoopSpecializationPlan(
        2,
        module.semantic_hash,
        analysis.analysis_hash,
        loop.kind,
        iterator_strategy,
        input_type.name,
        input_type.parameters[0].name,
        reduction_operation,
        accumulator_type.name,
        result_strategy,
        state_operations,
        predicates,
        guards,
        tuple(sorted(requirements)),
        "",
    )
    return replace(
        provisional,
        plan_hash=provisional.recompute_hash(),
    )


def _lower_verified_typed_loop(
    module: TypedSemanticModule,
    analysis: TypedAnalysisBundle,
) -> TypedLoopLowering:
    plan = _specialization_plan(module, analysis)
    function, source, ast_text, operation_lines = _CanonicalLoopLowerer(
        module,
        analysis,
    ).build()
    return TypedLoopLowering(
        module.semantic_hash,
        module,
        analysis,
        plan,
        function,
        source,
        ast_text,
        hashlib.sha256(marshal.dumps(function.__code__)).hexdigest(),
        operation_lines,
    )


def lower_typed_loop(module: TypedSemanticModule) -> TypedLoopLowering:
    analysis = analyze_typed_module(module)
    return _lower_verified_typed_loop(module, analysis)


@dataclass(frozen=True)
class _CachedTypedRegion:
    lowering: TypedLoopLowering
    backend: BackendCompilation


class _PositiveVariantCache:
    def __init__(self, max_variants: int) -> None:
        self._max_variants = max_variants
        self._entries: OrderedDict[str, _CachedTypedRegion] = OrderedDict()
        self._lock = RLock()

    def get(self, key: str) -> _CachedTypedRegion | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key: str, value: _CachedTypedRegion) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_variants:
                self._entries.popitem(last=False)


class TypedRegionCompiler:
    def __init__(
        self,
        backend: TypedLoopBackend,
        *,
        call_threshold: int,
        negative_ttl_ns: int,
        max_deopts: int = 3,
        max_variants: int = 128,
        diagnostic_sink: TypedLoopDiagnosticSink | None = None,
    ) -> None:
        if (
            not getattr(backend, "adapter_version", "")
            or type(call_threshold) is not int
            or call_threshold <= 0
            or type(max_deopts) is not int
            or max_deopts < 0
            or type(max_variants) is not int
            or max_variants <= 0
        ):
            raise ValueError("invalid typed region compiler configuration")
        self._backend = backend
        self._call_threshold = call_threshold
        self._max_deopts = max_deopts
        self._negative = NegativeCache(ttl_ns=negative_ttl_ns)
        self._positive = _PositiveVariantCache(max_variants)
        self._diagnostic_sink = diagnostic_sink

    def _finish(
        self,
        request: TypedRegionCompileRequest,
        decision: TypedCompileDecision,
    ) -> TypedCompileDecision:
        if self._diagnostic_sink is not None:
            try:
                self._diagnostic_sink.record_typed_region_decision(
                    request,
                    decision,
                )
            except Exception:
                # Diagnostics are observational and must never change whether
                # a verified region compiles or falls back.
                pass
        return decision

    def _cache_key(self, module: TypedSemanticModule) -> str:
        return _canonical_hash(
            b"python-udf-jit-typed-region-compile-v1\0",
            {
                "adapter_version": self._backend.adapter_version,
                "module_hash": module.semantic_hash,
            },
        )

    @staticmethod
    def _hint_match(
        request: TypedRegionCompileRequest,
        analysis: TypedAnalysisBundle,
    ) -> bool | None:
        return (
            None
            if request.driver_analysis_hint is None
            else request.driver_analysis_hint == analysis.to_documents()
        )

    def _runtime_guard_reason(
        self,
        request: TypedRegionCompileRequest,
    ) -> str | None:
        expected = request.region.runtime_dependency_hashes
        guard = request.runtime_guard
        if guard is None:
            return "runtime_dependency_guard_missing" if expected else None
        try:
            observed = guard.dependency_hashes
        except Exception:
            return "runtime_dependency_guard_invalid"
        if observed != expected:
            return "runtime_dependency_guard_mismatch"
        try:
            return None if guard.matches() else "runtime_dependency_guard_miss"
        except Exception:
            return "runtime_dependency_guard_invalid"

    def compile(self, request: TypedRegionCompileRequest) -> TypedCompileDecision:
        # The portable region crosses a trust boundary.  Verify it before even
        # applying runtime ROI gates, but defer the more expensive analyses
        # until the region is actually eligible for compilation.
        verify_typed_module(request.region)
        guard_reason = self._runtime_guard_reason(request)
        if guard_reason is not None:
            return self._finish(
                request,
                TypedCompileDecision(
                    CompileStatus.UNSUPPORTED,
                    guard_reason,
                ),
            )
        if request.runtime.call_count < self._call_threshold:
            return self._finish(
                request,
                TypedCompileDecision(
                    CompileStatus.DEFERRED,
                    "runtime_call_threshold",
                ),
            )
        if request.runtime.deopt_count > self._max_deopts:
            return self._finish(
                request,
                TypedCompileDecision(
                    CompileStatus.DEFERRED,
                    "runtime_deopt_backoff",
                ),
            )
        cache_key = self._cache_key(request.region)
        negative = self._negative.get(cache_key)
        if negative is not None:
            return self._finish(
                request,
                TypedCompileDecision(
                    CompileStatus.NEGATIVE_CACHE,
                    negative.reason_code,
                ),
            )
        cached = self._positive.get(cache_key)
        if cached is not None:
            worker_analysis = cached.lowering.analysis
            return self._finish(
                request,
                TypedCompileDecision(
                    CompileStatus.COMPILED,
                    "compiled_cache_hit",
                    CompiledTypedRegion(
                        cached.lowering,
                        cached.backend,
                        request.runtime_guard,
                    ),
                    worker_analysis,
                    self._hint_match(request, worker_analysis),
                ),
            )
        worker_analysis = _analyze_verified_typed_module(request.region)
        hint_matched = self._hint_match(request, worker_analysis)
        try:
            lowering = _lower_verified_typed_loop(
                request.region,
                worker_analysis,
            )
        except TypedLoweringError as error:
            return self._finish(
                request,
                TypedCompileDecision(
                    CompileStatus.UNSUPPORTED,
                    error.reason_code,
                    worker_analysis=worker_analysis,
                    driver_analysis_hint_matched=hint_matched,
                ),
            )
        try:
            if self._diagnostic_sink is None:
                backend = self._backend.compile(lowering)
            else:
                diagnostic_compile = getattr(
                    self._backend,
                    "compile_with_diagnostics",
                    None,
                )
                backend = (
                    diagnostic_compile(lowering, self._diagnostic_sink)
                    if callable(diagnostic_compile)
                    else self._backend.compile(lowering)
                )
        except Exception:
            self._negative.record(cache_key, "backend_compile_failed")
            return self._finish(
                request,
                TypedCompileDecision(
                    CompileStatus.FAILURE,
                    "backend_compile_failed",
                    worker_analysis=worker_analysis,
                    driver_analysis_hint_matched=hint_matched,
                ),
            )
        if not backend.jit_compiled:
            self._negative.record(cache_key, "backend_compile_rejected")
            return self._finish(
                request,
                TypedCompileDecision(
                    CompileStatus.FAILURE,
                    "backend_compile_rejected",
                    worker_analysis=worker_analysis,
                    driver_analysis_hint_matched=hint_matched,
                ),
            )
        self._negative.clear(cache_key)
        self._positive.put(cache_key, _CachedTypedRegion(lowering, backend))
        return self._finish(
            request,
            TypedCompileDecision(
                CompileStatus.COMPILED,
                "compiled",
                CompiledTypedRegion(
                    lowering,
                    backend,
                    request.runtime_guard,
                ),
                worker_analysis,
                hint_matched,
            ),
        )
