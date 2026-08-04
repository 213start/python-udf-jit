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
    encode_int_table,
)
from python_udf_jit.runtime.negative_cache import NegativeCache
from python_udf_jit.compiler.typed_verifier import verify_typed_module


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
class PhysicalTypedLoopLowering:
    module_hash: str
    plan_hash: str
    physical_operation: str
    property_id: int | None
    function: types.FunctionType
    generated_source: str
    generated_ast_text: str
    generated_code_hash: str
    operation_lines: tuple[tuple[str, int], ...]
    physical_attributes: tuple[tuple[str, str], ...] = ()

    @property
    def code_size(self) -> int:
        return len(marshal.dumps(self.function.__code__))

    def to_document(self) -> dict[str, object]:
        return {
            "generated_code_hash": self.generated_code_hash,
            "module_hash": self.module_hash,
            "operation_lines": [list(value) for value in self.operation_lines],
            "physical_operation": self.physical_operation,
            "plan_hash": self.plan_hash,
            "property_id": self.property_id,
            "physical_attributes": [list(value) for value in self.physical_attributes],
        }


@dataclass(frozen=True)
class BackendCompilation:
    jit_compiled: bool
    execution_mode: str
    hir_opcode_counts: tuple[tuple[str, int], ...] = ()
    physical_lowering: PhysicalTypedLoopLowering | None = None

    def __post_init__(self) -> None:
        if (
            type(self.jit_compiled) is not bool
            or not self.execution_mode
            or tuple(sorted(self.hir_opcode_counts)) != self.hir_opcode_counts
            or any(
                not name or type(count) is not int or count < 0
                for name, count in self.hir_opcode_counts
            )
            or (
                self.physical_lowering is not None
                and self.physical_lowering.module_hash == ""
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
    ) -> None:
        if diagnostic_sink is None:
            return
        try:
            diagnostic_sink.prepare_typed_compilation(
                function,
                generated_code_hash,
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

        physical_attempts = (
            (
                getattr(cinderx.jit, "_udf_unicode_map_sequence", None),
                lower_unicode_map_physical,
                "UnicodeMapSequence",
                "cinderx_unicode_map_hir",
            ),
            (
                getattr(cinderx.jit, "_udf_unicode_fsm_sequence", None),
                lower_unicode_fsm_physical,
                "UnicodeFsmSequence",
                "cinderx_unicode_fsm_hir",
            ),
            (
                getattr(cinderx.jit, "_udf_unicode_count_property", None),
                lower_unicode_count_physical,
                "UnicodeCountProperty",
                "cinderx_unicode_property_hir",
            ),
        )
        for helper, physical_lowerer, required_opcode, execution_mode in physical_attempts:
            if not callable(helper):
                continue
            try:
                physical = physical_lowerer(lowering, helper)
            except TypedLoweringError:
                continue
            self._prepare_diagnostics(
                diagnostic_sink,
                physical.function,
                physical.generated_code_hash,
            )
            if cinderx.jit.force_compile(physical.function):
                raw_counts = cinderx.jit.get_function_hir_opcode_counts(
                    physical.function
                )
                counts = {
                    str(name): int(count)
                    for name, count in (raw_counts or {}).items()
                }
                if (
                    cinderx.jit.is_jit_compiled(physical.function)
                    and counts.get(required_opcode, 0) == 1
                ):
                    return BackendCompilation(
                        True,
                        execution_mode,
                        tuple(sorted(counts.items())),
                        physical,
                    )

        if lowering.analysis.patterns.builder_operations:
            return BackendCompilation(
                False,
                "cinderx_sequence_transform_intrinsic",
            )

        self._prepare_diagnostics(
            diagnostic_sink,
            lowering.function,
            lowering.generated_code_hash,
        )
        typed_entry = getattr(cinderx.jit, "compile_typed_region", None)
        if typed_entry is not None:
            result = typed_entry(
                lowering.function,
                lowering.plan.to_document(),
            )
            if result is not True:
                return BackendCompilation(False, "cinderx_typed_hir_adapter")
            mode = "cinderx_typed_hir_adapter"
        else:
            if not cinderx.jit.force_compile(lowering.function):
                return BackendCompilation(False, "cinderx_standard_bytecode_bridge")
            mode = "cinderx_standard_bytecode_bridge"
        counts = cinderx.jit.get_function_hir_opcode_counts(lowering.function) or {}
        return BackendCompilation(
            cinderx.jit.is_jit_compiled(lowering.function),
            mode,
            tuple(sorted((str(name), int(count)) for name, count in counts.items())),
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
        physical = self.backend.physical_lowering
        return (
            physical.generated_code_hash
            if physical is not None
            else self.lowering.generated_code_hash
        )

    @property
    def code_size(self) -> int:
        physical = self.backend.physical_lowering
        return physical.code_size if physical is not None else self.lowering.code_size

    @property
    def jit_function(self) -> types.FunctionType:
        physical = self.backend.physical_lowering
        return physical.function if physical is not None else self.lowering.function

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
_UNICODE_METHOD = {
    "alnum": "isalnum",
    "alpha": "isalpha",
    "decimal": "isdecimal",
    "digit": "isdigit",
    "numeric": "isnumeric",
    "space": "isspace",
}
_UNICODE_PROPERTY_ID = {
    "alnum": 0,
    "alpha": 1,
    "decimal": 2,
    "digit": 3,
    "numeric": 4,
    "space": 5,
}


def _immutable_unicode_lookup(
    value: str,
    keys: tuple[int, ...],
    replacements: tuple[int, ...],
) -> str:
    codepoint = ord(value)
    table_index = bisect_left(keys, codepoint)
    if table_index == len(keys) or keys[table_index] != codepoint:
        return value
    return chr(replacements[table_index])


def _sequence_builder_append(builder: list[str], value: str) -> list[str]:
    result = list(builder)
    result.append(value)
    return result


def _fsm_transition(
    state: int,
    classification: bool,
    transitions: tuple[int, ...],
) -> int:
    return transitions[state * 2 + int(classification)]


def _sequence_builder_apply(
    builder: list[str],
    value: str,
    state: int,
    classification: bool,
    actions: tuple[int, ...],
    emissions: tuple[int, ...],
) -> list[str]:
    table_index = state * 2 + int(classification)
    action = actions[table_index]
    result = list(builder)
    if action in {2, 3}:
        result.append(chr(emissions[table_index]))
    if action in {1, 3, 4}:
        result.append(value)
    if action == 4:
        result.append(chr(emissions[table_index]))
    return result


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
                func=ast.Name(id="len", ctx=ast.Load()),
                args=[operands[0]],
                keywords=[],
            )
        if operation.op == "sequence.get":
            return ast.Subscript(value=operands[0], slice=operands[1], ctx=ast.Load())
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
                func=ast.Name(id="_immutable_unicode_lookup", ctx=ast.Load()),
                args=[operands[0], ast.Constant(keys), ast.Constant(replacements)],
                keywords=[],
            )
        if operation.op == "unicode.property":
            method = _UNICODE_METHOD.get(operation.attribute("property") or "")
            if method is None:
                _fail("unicode_property_unsupported", operation.operation_id)
            return ast.Call(
                func=ast.Attribute(value=operands[0], attr=method, ctx=ast.Load()),
                args=[],
                keywords=[],
            )
        if operation.op == "fsm.transition":
            transitions = decode_int_table(
                operation.attribute("transitions") or "",
                max_items=128,
                maximum=int(operation.attribute("state_count") or "0") - 1,
            )
            return ast.Call(
                func=ast.Name(id="_fsm_transition", ctx=ast.Load()),
                args=[operands[0], operands[1], ast.Constant(transitions)],
                keywords=[],
            )
        if operation.op == "sequence.builder.create":
            return ast.List(elts=[], ctx=ast.Load())
        if operation.op == "sequence.builder.append":
            return ast.Call(
                func=ast.Name(id="_sequence_builder_append", ctx=ast.Load()),
                args=operands,
                keywords=[],
            )
        if operation.op == "sequence.builder.apply":
            actions = decode_int_table(
                operation.attribute("actions") or "",
                max_items=128,
                maximum=4,
            )
            emissions = decode_int_table(
                operation.attribute("emissions") or "",
                max_items=128,
            )
            return ast.Call(
                func=ast.Name(id="_sequence_builder_apply", ctx=ast.Load()),
                args=[
                    *operands,
                    ast.Constant(actions),
                    ast.Constant(emissions),
                ],
                keywords=[],
            )
        if operation.op == "sequence.builder.finish":
            return ast.Call(
                func=ast.Attribute(
                    value=ast.Constant(""),
                    attr="join",
                    ctx=ast.Load(),
                ),
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
                comparators=[ast.Name("str", ast.Load())],
            )
            wrong_length = ast.Compare(
                left=ast.Call(ast.Name("len", ast.Load()), [value], []),
                ops=[ast.NotEq()],
                comparators=[ast.Constant(1)],
            )
            invalid = ast.BoolOp(ast.Or(), [wrong_type, wrong_length])
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
        assignment = ast.Assign(
            targets=[self._name(operation.result_id, ast.Store())],
            value=self._expression(operation),
        )
        result = [self._record(operation, assignment)]
        if operation.op not in {"argument", "constant"}:
            result.extend(self._guard(operation.result_id, operation.result_type))
        return result

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
            "_fsm_transition": _fsm_transition,
            "_immutable_unicode_lookup": _immutable_unicode_lookup,
            "_sequence_builder_append": _sequence_builder_append,
            "_sequence_builder_apply": _sequence_builder_apply,
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


@dataclass(frozen=True)
class _UnicodeCountKernel:
    property_name: str
    property_id: int
    input_value: str
    result_value: str
    collapsed_operation_ids: tuple[str, ...]


def _match_unicode_count_kernel(
    lowering: TypedLoopLowering,
) -> _UnicodeCountKernel:
    module = lowering.module
    analysis = lowering.analysis
    if (
        len(module.input_types) != 1
        or module.input_types[0].name != "str"
        or module.input_types[0].exactness is not Exactness.EXACT
        or len(analysis.patterns.loops) != 1
        or len(analysis.patterns.reductions) != 1
    ):
        _fail("unicode_count_kernel_shape_mismatch")
    loop = analysis.patterns.loops[0]
    reduction = analysis.patterns.reductions[0]
    if loop.kind != "iterator_loop" or reduction.operation != "binary.add":
        _fail("unicode_count_pattern_mismatch")

    blocks = {block.block_id: block for block in module.blocks}
    operations = {
        operation.operation_id: operation for operation in module.operations
    }
    definitions = {
        operation.result_id: operation
        for operation in module.operations
        if operation.result_id is not None
    }
    input_operations = tuple(
        operation
        for operation in module.operations
        if operation.op == "argument" and operation.attribute("index") == "0"
    )
    if len(input_operations) != 1 or input_operations[0].result_id is None:
        _fail("unicode_count_input_missing")
    input_value = input_operations[0].result_id

    loop_operations = tuple(
        operations[operation_id]
        for block_id in loop.blocks
        for operation_id in blocks[block_id].operation_ids
    )
    loop_shape = sorted(operation.op for operation in loop_operations)
    if loop_shape != sorted(
        (
            "binary.add",
            "binary.add",
            "branch",
            "cast",
            "compare.lt",
            "jump",
            "sequence.get",
            "unicode.property",
        )
    ):
        _fail("unicode_count_loop_operations_mismatch")
    property_operations = tuple(
        operation
        for operation in loop_operations
        if operation.op == "unicode.property"
    )
    if len(property_operations) != 1:
        _fail("unicode_count_property_missing")
    property_operation = property_operations[0]
    property_name = property_operation.attribute("property") or ""
    property_id = _UNICODE_PROPERTY_ID.get(property_name)
    if property_id is None or property_operation.result_id is None:
        _fail("unicode_count_property_unsupported", property_name)

    if len(property_operation.operands) != 1:
        _fail("unicode_count_property_operand_invalid")
    item = definitions.get(property_operation.operands[0])
    if (
        item is None
        or item.op != "sequence.get"
        or len(item.operands) != 2
        or item.operands[0] != input_value
    ):
        _fail("unicode_count_iteration_dataflow_invalid")
    casts = tuple(
        operation
        for operation in loop_operations
        if operation.op == "cast"
        and operation.operands == (property_operation.result_id,)
        and operation.attribute("target") == "int64"
    )
    if len(casts) != 1 or casts[0].result_id is None:
        _fail("unicode_count_cast_invalid")

    reduction_update = definitions.get(reduction.update_value)
    if (
        reduction_update is None
        or reduction_update.op != "binary.add"
        or reduction.accumulator not in reduction_update.operands
        or casts[0].result_id not in reduction_update.operands
    ):
        _fail("unicode_count_reduction_dataflow_invalid")

    header = blocks[loop.header]
    accumulator_indexes = tuple(
        index
        for index, argument in enumerate(header.arguments)
        if argument.value_id == reduction.accumulator and argument.type == INT64
    )
    entry_edges = tuple(
        edge
        for edge in module.control_edges
        if edge.target_block == loop.header
        and edge.source_block not in loop.blocks
        and edge.kind == "jump"
    )
    if len(accumulator_indexes) != 1 or len(entry_edges) != 1:
        _fail("unicode_count_accumulator_entry_invalid")
    accumulator_index = accumulator_indexes[0]
    induction_indexes = tuple(
        index
        for index, argument in enumerate(header.arguments)
        if index != accumulator_index and argument.type == INT64
    )
    if len(header.arguments) != 2 or len(induction_indexes) != 1:
        _fail("unicode_count_induction_variable_invalid")
    induction_index = induction_indexes[0]
    induction_value = header.arguments[induction_index].value_id
    if item.operands[1] != induction_value:
        _fail("unicode_count_iteration_index_invalid")

    header_operations = tuple(
        operations[operation_id]
        for operation_id in header.operation_ids
    )
    branches = tuple(
        operation
        for operation in header_operations
        if operation.op == "branch" and len(operation.operands) == 1
    )
    if len(branches) != 1:
        _fail("unicode_count_loop_branch_invalid")
    comparison = definitions.get(branches[0].operands[0])
    if (
        comparison is None
        or comparison.op != "compare.lt"
        or len(comparison.operands) != 2
        or comparison.operands[0] != induction_value
    ):
        _fail("unicode_count_loop_bound_invalid")
    length = definitions.get(comparison.operands[1])
    if (
        length is None
        or length.op != "sequence.length"
        or length.operands != (input_value,)
    ):
        _fail("unicode_count_loop_length_invalid")

    backedges = tuple(
        edge
        for edge in module.control_edges
        if edge.target_block == loop.header
        and edge.source_block in loop.latches
        and edge.kind == "jump"
    )
    if len(loop.latches) != 1 or len(backedges) != 1:
        _fail("unicode_count_backedge_invalid")
    backedge = backedges[0]
    if backedge.arguments[accumulator_index] != reduction.update_value:
        _fail("unicode_count_reduction_backedge_invalid")
    index_update = definitions.get(backedge.arguments[induction_index])
    if (
        index_update is None
        or index_update.op != "binary.add"
        or len(index_update.operands) != 2
        or induction_value not in index_update.operands
    ):
        _fail("unicode_count_induction_update_invalid")
    increment_values = tuple(
        value for value in index_update.operands if value != induction_value
    )
    if len(increment_values) != 1:
        _fail("unicode_count_induction_increment_invalid")
    increment = definitions.get(increment_values[0])
    if (
        increment is None
        or increment.op != "constant"
        or increment.literal is None
        or type(increment.literal.value) is not int
        or increment.literal.value != 1
    ):
        _fail("unicode_count_induction_increment_invalid")

    initial = definitions.get(entry_edges[0].arguments[accumulator_index])
    if (
        initial is None
        or initial.op != "constant"
        or initial.literal is None
        or type(initial.literal.value) is not int
        or initial.literal.value != 0
    ):
        _fail("unicode_count_initial_value_invalid")
    initial_index = definitions.get(
        entry_edges[0].arguments[induction_index]
    )
    if (
        initial_index is None
        or initial_index.op != "constant"
        or initial_index.literal is None
        or type(initial_index.literal.value) is not int
        or initial_index.literal.value != 0
    ):
        _fail("unicode_count_initial_index_invalid")

    exit_edges = tuple(
        edge
        for edge in module.control_edges
        if edge.source_block == loop.header and edge.kind == "branch_false"
    )
    if len(exit_edges) != 1:
        _fail("unicode_count_exit_missing")
    exit_edge = exit_edges[0]
    exit_block = blocks[exit_edge.target_block]
    exit_indexes = tuple(
        index
        for index, value_id in enumerate(exit_edge.arguments)
        if value_id == reduction.accumulator
    )
    if (
        len(exit_indexes) != 1
        or exit_indexes[0] >= len(exit_block.arguments)
        or len(exit_block.arguments) != 1
    ):
        _fail("unicode_count_result_mapping_invalid")
    result_value = exit_block.arguments[exit_indexes[0]].value_id

    covered_blocks = {
        module.entry_block,
        exit_block.block_id,
        *loop.blocks,
    }
    if covered_blocks != set(blocks):
        _fail("unicode_count_extra_control_flow")
    collapsed = tuple(
        sorted(
            {
                *(
                    operation.operation_id
                    for operation in loop_operations
                ),
                *(
                    operation.operation_id
                    for operation in module.operations
                    if operation.block_id == module.entry_block
                    and operation.op == "jump"
                ),
            }
        )
    )
    return _UnicodeCountKernel(
        property_name,
        property_id,
        input_value,
        result_value,
        collapsed,
    )


def lower_unicode_count_physical(
    lowering: TypedLoopLowering,
    helper: object,
) -> PhysicalTypedLoopLowering:
    if not callable(helper):
        _fail("unicode_count_helper_unavailable")
    kernel = _match_unicode_count_kernel(lowering)
    module = lowering.module
    blocks = {block.block_id: block for block in module.blocks}
    operations = {
        operation.operation_id: operation for operation in module.operations
    }
    lowerer = _CanonicalLoopLowerer(module, lowering.analysis)

    function_body: list[ast.stmt] = [
        _stamp(
            ast.If(
                ast.Compare(
                    ast.Call(
                        ast.Name("type", ast.Load()),
                        [ast.Name("arg0", ast.Load())],
                        [],
                    ),
                    [ast.IsNot()],
                    [ast.Name("_input_type_0", ast.Load())],
                ),
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
            900,
        )  # type: ignore[list-item]
    ]
    entry_block = blocks[module.entry_block]
    for operation_id in entry_block.operation_ids:
        operation = operations[operation_id]
        if operation.op != "jump":
            function_body.extend(lowerer._operation_statements(operation))

    helper_line = lowerer.next_line
    lowerer.next_line += 1
    helper_assignment = ast.Assign(
        [lowerer._name(kernel.result_value, ast.Store())],
        ast.Call(
            ast.Name("_unicode_count_property", ast.Load()),
            [
                lowerer._name(kernel.input_value, ast.Load()),
                ast.Constant(kernel.property_id),
            ],
            [],
        ),
    )
    function_body.append(
        _stamp(helper_assignment, helper_line)  # type: ignore[arg-type]
    )
    for operation_id in kernel.collapsed_operation_ids:
        lowerer.lines[operation_id] = helper_line

    loop = lowering.analysis.patterns.loops[0]
    exit_edge = next(
        edge
        for edge in module.control_edges
        if edge.source_block == loop.header and edge.kind == "branch_false"
    )
    exit_block = blocks[exit_edge.target_block]
    for operation_id in exit_block.operation_ids:
        operation = operations[operation_id]
        if operation.op == "return":
            if len(operation.operands) != 1:
                _fail("unicode_count_return_invalid")
            function_body.append(
                lowerer._record(
                    operation,
                    ast.Return(lowerer._name(operation.operands[0], ast.Load())),
                )
            )
        else:
            function_body.extend(lowerer._operation_statements(operation))

    missing = set(operations) - set(lowerer.lines)
    if missing:
        _fail("unmapped_physical_operations", ",".join(sorted(missing)))
    function_node = ast.FunctionDef(
        name="_typed_region",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="arg0")],
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
    filename = f"<udfjit-physical-{module.semantic_hash[:16]}>"
    code = compile(module_node, filename, "exec")
    globals_namespace: dict[str, object] = {
        "_TypedGuardMiss": TypedGuardMiss,
        "_input_type_0": str,
        "_unicode_count_property": helper,
        "__builtins__": __builtins__,
    }
    namespace: dict[str, object] = {}
    exec(code, globals_namespace, namespace)
    function = namespace["_typed_region"]
    if not isinstance(function, types.FunctionType):
        raise RuntimeError("physical lowering did not create a function")
    function.__module__ = "python_udf_jit.generated"
    function.__qualname__ = "_typed_region"
    function.__dict__["__udf_jit_typed_region__"] = module.semantic_hash
    function.__dict__["__udf_jit_physical_operation__"] = (
        "unicode.count_property"
    )
    source = ast.unparse(module_node)
    return PhysicalTypedLoopLowering(
        module.semantic_hash,
        lowering.plan.plan_hash,
        "unicode.count_property",
        kernel.property_id,
        function,
        source,
        ast.dump(
            module_node,
            annotate_fields=True,
            include_attributes=False,
            indent=2,
        ),
        hashlib.sha256(marshal.dumps(function.__code__)).hexdigest(),
        tuple(sorted(lowerer.lines.items())),
    )


@dataclass(frozen=True)
class _SequenceTransformScaffold:
    input_value: str
    item_value: str
    builder_value: str
    state_value: str | None
    result_value: str
    operations: dict[str, TypedOperation]
    definitions: dict[str, TypedOperation]
    collapsed_operation_ids: tuple[str, ...]


@dataclass(frozen=True)
class _UnicodeMapKernel:
    scaffold: _SequenceTransformScaffold
    keys: tuple[int, ...]
    values: tuple[int, ...]


@dataclass(frozen=True)
class _UnicodeFsmKernel:
    scaffold: _SequenceTransformScaffold
    property_name: str
    property_id: int
    initial_state: int
    state_count: int
    transitions: tuple[int, ...]
    actions: tuple[int, ...]
    emissions: tuple[int, ...]


def _single_operation(
    operations: tuple[TypedOperation, ...],
    op: str,
    *,
    reason: str,
) -> TypedOperation:
    matches = tuple(operation for operation in operations if operation.op == op)
    if len(matches) != 1:
        _fail(reason)
    return matches[0]


def _constant_int(
    definitions: dict[str, TypedOperation],
    value_id: str,
    *,
    reason: str,
) -> int:
    operation = definitions.get(value_id)
    if (
        operation is None
        or operation.op != "constant"
        or operation.literal is None
        or type(operation.literal.value) is not int
    ):
        _fail(reason)
    return operation.literal.value


def _match_sequence_transform_scaffold(
    lowering: TypedLoopLowering,
    *,
    with_state: bool,
) -> _SequenceTransformScaffold:
    module = lowering.module
    analysis = lowering.analysis
    expected_arguments = 3 if with_state else 2
    if (
        module.input_types != (EXACT_UNICODE,)
        or module.output_type != EXACT_UNICODE
        or len(analysis.patterns.loops) != 1
        or analysis.patterns.reductions
    ):
        _fail("sequence_transform_shape_mismatch")
    loop = analysis.patterns.loops[0]
    blocks = {block.block_id: block for block in module.blocks}
    if (
        set(blocks) != {"entry", "header", "body", "exit"}
        or module.entry_block != "entry"
        or loop.header != "header"
        or loop.latches != ("body",)
        or set(loop.blocks) != {"header", "body"}
        or loop.kind != "iterator_loop"
        or len(blocks["header"].arguments) != expected_arguments
        or blocks["header"].arguments[0].value_id != "%index"
        or blocks["header"].arguments[0].type != INT64
        or blocks["header"].arguments[1].value_id != "%builder"
        or blocks["header"].arguments[1].type != UNICODE_BUILDER
        or blocks["body"].arguments
        or blocks["exit"].arguments
        != (TypedBlockArgument("%result_builder", UNICODE_BUILDER),)
    ):
        _fail("sequence_transform_cfg_mismatch")
    state_value = None
    if with_state:
        state_argument = blocks["header"].arguments[2]
        if state_argument.value_id != "%state" or state_argument.type != INT64:
            _fail("sequence_transform_state_mismatch")
        state_value = state_argument.value_id

    operations = {operation.operation_id: operation for operation in module.operations}
    definitions = {
        operation.result_id: operation
        for operation in module.operations
        if operation.result_id is not None
    }
    by_block = {
        block_id: tuple(operations[value] for value in block.operation_ids)
        for block_id, block in blocks.items()
    }
    entry_operation_names = tuple(
        operation.op for operation in by_block["entry"]
    )
    constant_count = entry_operation_names.count("constant")
    if (
        sorted(
            name for name in entry_operation_names if name != "constant"
        )
        != sorted(
            (
                "argument",
                "jump",
                "sequence.builder.create",
                "sequence.length",
            )
        )
        or constant_count not in ({2, 3} if with_state else {2})
    ):
        _fail("sequence_transform_entry_operations_mismatch")
    input_operation = _single_operation(
        by_block["entry"],
        "argument",
        reason="sequence_transform_input_missing",
    )
    length = _single_operation(
        by_block["entry"],
        "sequence.length",
        reason="sequence_transform_length_missing",
    )
    builder = _single_operation(
        by_block["entry"],
        "sequence.builder.create",
        reason="sequence_transform_builder_missing",
    )
    entry_jump = _single_operation(
        by_block["entry"],
        "jump",
        reason="sequence_transform_entry_jump_missing",
    )
    if (
        input_operation.result_id is None
        or input_operation.attribute("index") != "0"
        or length.operands != (input_operation.result_id,)
        or builder.result_id is None
        or entry_jump.attribute("target_block") != "header"
    ):
        _fail("sequence_transform_entry_invalid")

    comparison = _single_operation(
        by_block["header"],
        "compare.lt",
        reason="sequence_transform_condition_missing",
    )
    branch = _single_operation(
        by_block["header"],
        "branch",
        reason="sequence_transform_branch_missing",
    )
    if (
        len(by_block["header"]) != 2
        or length.result_id is None
        or comparison.operands != ("%index", length.result_id)
        or comparison.result_id is None
        or branch.operands != (comparison.result_id,)
        or branch.attribute("true_block") != "body"
        or branch.attribute("false_block") != "exit"
    ):
        _fail("sequence_transform_condition_invalid")

    item = _single_operation(
        by_block["body"],
        "sequence.get",
        reason="sequence_transform_item_missing",
    )
    index_update = tuple(
        operation
        for operation in by_block["body"]
        if operation.op == "binary.add" and "%index" in operation.operands
    )
    body_jump = _single_operation(
        by_block["body"],
        "jump",
        reason="sequence_transform_backedge_missing",
    )
    if (
        item.result_id is None
        or item.operands != (input_operation.result_id, "%index")
        or len(index_update) != 1
        or index_update[0].result_id is None
        or len(index_update[0].operands) != 2
        or body_jump.attribute("target_block") != "header"
    ):
        _fail("sequence_transform_iteration_invalid")
    increment_ids = tuple(
        value for value in index_update[0].operands if value != "%index"
    )
    if (
        len(increment_ids) != 1
        or _constant_int(
            definitions,
            increment_ids[0],
            reason="sequence_transform_increment_invalid",
        )
        != 1
    ):
        _fail("sequence_transform_increment_invalid")

    entry_edge = next(
        (
            edge
            for edge in module.control_edges
            if edge.source_block == "entry"
            and edge.target_block == "header"
            and edge.kind == "jump"
        ),
        None,
    )
    backedge = next(
        (
            edge
            for edge in module.control_edges
            if edge.source_block == "body"
            and edge.target_block == "header"
            and edge.kind == "jump"
        ),
        None,
    )
    true_edge = next(
        (
            edge
            for edge in module.control_edges
            if edge.source_block == "header" and edge.kind == "branch_true"
        ),
        None,
    )
    false_edge = next(
        (
            edge
            for edge in module.control_edges
            if edge.source_block == "header" and edge.kind == "branch_false"
        ),
        None,
    )
    if (
        len(module.control_edges) != 4
        or entry_edge is None
        or backedge is None
        or true_edge is None
        or true_edge.target_block != "body"
        or true_edge.arguments
        or false_edge is None
        or false_edge.target_block != "exit"
        or false_edge.arguments != ("%builder",)
        or len(entry_edge.arguments) != expected_arguments
        or len(backedge.arguments) != expected_arguments
        or _constant_int(
            definitions,
            entry_edge.arguments[0],
            reason="sequence_transform_initial_index_invalid",
        )
        != 0
        or entry_edge.arguments[1] != builder.result_id
        or backedge.arguments[0] != index_update[0].result_id
    ):
        _fail("sequence_transform_edges_invalid")

    finish = _single_operation(
        by_block["exit"],
        "sequence.builder.finish",
        reason="sequence_transform_finish_missing",
    )
    return_operation = _single_operation(
        by_block["exit"],
        "return",
        reason="sequence_transform_return_missing",
    )
    if (
        len(by_block["exit"]) != 2
        or finish.operands != ("%result_builder",)
        or finish.result_id is None
        or return_operation.operands != (finish.result_id,)
        or return_operation.operation_id != module.return_operation_id
    ):
        _fail("sequence_transform_exit_invalid")
    collapsed = tuple(
        sorted(
            operation.operation_id
            for operation in module.operations
            if operation is not input_operation and operation is not return_operation
        )
    )
    return _SequenceTransformScaffold(
        input_operation.result_id,
        item.result_id,
        "%builder",
        state_value,
        finish.result_id,
        operations,
        definitions,
        collapsed,
    )


def _match_unicode_map_kernel(lowering: TypedLoopLowering) -> _UnicodeMapKernel:
    scaffold = _match_sequence_transform_scaffold(lowering, with_state=False)
    body = tuple(
        operation
        for operation in lowering.module.operations
        if operation.block_id == "body"
    )
    if sorted(operation.op for operation in body) != sorted(
        (
            "binary.add",
            "immutable.lookup",
            "jump",
            "sequence.builder.append",
            "sequence.get",
        )
    ):
        _fail("unicode_map_operations_mismatch")
    lookup = _single_operation(
        body,
        "immutable.lookup",
        reason="unicode_map_lookup_missing",
    )
    append = _single_operation(
        body,
        "sequence.builder.append",
        reason="unicode_map_append_missing",
    )
    keys = decode_int_table(lookup.attribute("keys") or "", max_items=256)
    values = decode_int_table(lookup.attribute("values") or "", max_items=256)
    backedge = next(
        edge
        for edge in lowering.module.control_edges
        if edge.source_block == "body" and edge.kind == "jump"
    )
    if (
        lookup.operands != (scaffold.item_value, scaffold.item_value)
        or lookup.result_id is None
        or append.operands != (scaffold.builder_value, lookup.result_id)
        or append.result_id is None
        or backedge.arguments[1] != append.result_id
        or len(keys) != len(values)
        or tuple(sorted(keys)) != keys
        or len(set(keys)) != len(keys)
    ):
        _fail("unicode_map_dataflow_invalid")
    return _UnicodeMapKernel(scaffold, keys, values)


def _match_unicode_fsm_kernel(lowering: TypedLoopLowering) -> _UnicodeFsmKernel:
    scaffold = _match_sequence_transform_scaffold(lowering, with_state=True)
    body = tuple(
        operation
        for operation in lowering.module.operations
        if operation.block_id == "body"
    )
    if sorted(operation.op for operation in body) != sorted(
        (
            "binary.add",
            "fsm.transition",
            "jump",
            "sequence.builder.apply",
            "sequence.get",
            "unicode.property",
        )
    ):
        _fail("unicode_fsm_operations_mismatch")
    classifier = _single_operation(
        body,
        "unicode.property",
        reason="unicode_fsm_classifier_missing",
    )
    transition = _single_operation(
        body,
        "fsm.transition",
        reason="unicode_fsm_transition_missing",
    )
    apply = _single_operation(
        body,
        "sequence.builder.apply",
        reason="unicode_fsm_builder_action_missing",
    )
    property_name = classifier.attribute("property") or ""
    property_id = _UNICODE_PROPERTY_ID.get(property_name)
    if property_id is None:
        _fail("unicode_fsm_property_unsupported", property_name)
    state_count = int(transition.attribute("state_count") or "0")
    transitions = decode_int_table(
        transition.attribute("transitions") or "",
        max_items=128,
        maximum=state_count - 1,
    )
    actions = decode_int_table(
        apply.attribute("actions") or "",
        max_items=128,
        maximum=4,
    )
    emissions = decode_int_table(
        apply.attribute("emissions") or "",
        max_items=128,
    )
    entry_edge = next(
        edge
        for edge in lowering.module.control_edges
        if edge.source_block == "entry" and edge.kind == "jump"
    )
    backedge = next(
        edge
        for edge in lowering.module.control_edges
        if edge.source_block == "body" and edge.kind == "jump"
    )
    initial_state = _constant_int(
        scaffold.definitions,
        entry_edge.arguments[2],
        reason="unicode_fsm_initial_state_invalid",
    )
    if (
        classifier.operands != (scaffold.item_value,)
        or classifier.result_id is None
        or transition.operands != (scaffold.state_value, classifier.result_id)
        or transition.result_id is None
        or apply.operands
        != (
            scaffold.builder_value,
            scaffold.item_value,
            scaffold.state_value,
            classifier.result_id,
        )
        or apply.result_id is None
        or apply.attribute("state_count") != str(state_count)
        or apply.attribute("class_count") != "2"
        or transition.attribute("class_count") != "2"
        or backedge.arguments[1:] != (apply.result_id, transition.result_id)
        or not 0 <= initial_state < state_count
        or len(transitions) != state_count * 2
        or len(actions) != len(transitions)
        or len(emissions) != len(transitions)
    ):
        _fail("unicode_fsm_dataflow_invalid")
    return _UnicodeFsmKernel(
        scaffold,
        property_name,
        property_id,
        initial_state,
        state_count,
        transitions,
        actions,
        emissions,
    )


def _codepoint_string(values: tuple[int, ...]) -> str:
    return "".join(chr(value) for value in values)


def _lower_unicode_sequence_physical(
    lowering: TypedLoopLowering,
    helper: object,
    *,
    scaffold: _SequenceTransformScaffold,
    physical_operation: str,
    helper_global: str,
    helper_arguments: tuple[object, ...],
    property_id: int | None,
    physical_attributes: tuple[tuple[str, str], ...],
) -> PhysicalTypedLoopLowering:
    if not callable(helper):
        _fail("unicode_sequence_helper_unavailable")
    module = lowering.module
    lowerer = _CanonicalLoopLowerer(module, lowering.analysis)
    input_operation = scaffold.definitions[scaffold.input_value]
    function_body: list[ast.stmt] = [
        _stamp(
            ast.If(
                ast.Compare(
                    ast.Call(
                        ast.Name("type", ast.Load()),
                        [ast.Name("arg0", ast.Load())],
                        [],
                    ),
                    [ast.IsNot()],
                    [ast.Name("_input_type_0", ast.Load())],
                ),
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
            900,
        )  # type: ignore[list-item]
    ]
    function_body.extend(lowerer._operation_statements(input_operation))
    helper_line = lowerer.next_line
    lowerer.next_line += 1
    function_body.append(
        _stamp(
            ast.Assign(
                [lowerer._name(scaffold.result_value, ast.Store())],
                ast.Call(
                    ast.Name(helper_global, ast.Load()),
                    [
                        lowerer._name(scaffold.input_value, ast.Load()),
                        *(ast.Constant(value) for value in helper_arguments),
                    ],
                    [],
                ),
            ),
            helper_line,
        )  # type: ignore[arg-type]
    )
    for operation_id in scaffold.collapsed_operation_ids:
        lowerer.lines[operation_id] = helper_line
    return_operation = next(
        operation for operation in module.operations if operation.op == "return"
    )
    function_body.append(
        lowerer._record(
            return_operation,
            ast.Return(lowerer._name(scaffold.result_value, ast.Load())),
        )
    )
    missing = set(scaffold.operations) - set(lowerer.lines)
    if missing:
        _fail("unmapped_physical_operations", ",".join(sorted(missing)))
    function_node = ast.FunctionDef(
        name="_typed_region",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="arg0")],
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
    code = compile(
        module_node,
        f"<udfjit-physical-{module.semantic_hash[:16]}>",
        "exec",
    )
    globals_namespace: dict[str, object] = {
        "_TypedGuardMiss": TypedGuardMiss,
        "_input_type_0": str,
        helper_global: helper,
        "__builtins__": __builtins__,
    }
    namespace: dict[str, object] = {}
    exec(code, globals_namespace, namespace)
    function = namespace["_typed_region"]
    if not isinstance(function, types.FunctionType):
        raise RuntimeError("physical lowering did not create a function")
    function.__module__ = "python_udf_jit.generated"
    function.__qualname__ = "_typed_region"
    function.__dict__["__udf_jit_typed_region__"] = module.semantic_hash
    function.__dict__["__udf_jit_physical_operation__"] = physical_operation
    return PhysicalTypedLoopLowering(
        module.semantic_hash,
        lowering.plan.plan_hash,
        physical_operation,
        property_id,
        function,
        ast.unparse(module_node),
        ast.dump(
            module_node,
            annotate_fields=True,
            include_attributes=False,
            indent=2,
        ),
        hashlib.sha256(marshal.dumps(function.__code__)).hexdigest(),
        tuple(sorted(lowerer.lines.items())),
        physical_attributes,
    )


def lower_unicode_map_physical(
    lowering: TypedLoopLowering,
    helper: object,
) -> PhysicalTypedLoopLowering:
    kernel = _match_unicode_map_kernel(lowering)
    return _lower_unicode_sequence_physical(
        lowering,
        helper,
        scaffold=kernel.scaffold,
        physical_operation="unicode.map_sequence",
        helper_global="_unicode_map_sequence",
        helper_arguments=(
            _codepoint_string(kernel.keys),
            _codepoint_string(kernel.values),
        ),
        property_id=None,
        physical_attributes=(
            ("keys", encode_int_table(kernel.keys)),
            ("values", encode_int_table(kernel.values)),
        ),
    )


def lower_unicode_fsm_physical(
    lowering: TypedLoopLowering,
    helper: object,
) -> PhysicalTypedLoopLowering:
    kernel = _match_unicode_fsm_kernel(lowering)
    return _lower_unicode_sequence_physical(
        lowering,
        helper,
        scaffold=kernel.scaffold,
        physical_operation="unicode.fsm_sequence",
        helper_global="_unicode_fsm_sequence",
        helper_arguments=(
            kernel.property_id,
            kernel.initial_state,
            _codepoint_string(
                kernel.transitions + kernel.actions + kernel.emissions
            ),
        ),
        property_id=kernel.property_id,
        physical_attributes=(
            ("actions", encode_int_table(kernel.actions)),
            ("emissions", encode_int_table(kernel.emissions)),
            ("initial_state", str(kernel.initial_state)),
            ("property", kernel.property_name),
            ("state_count", str(kernel.state_count)),
            (
                "transitions",
                encode_int_table(kernel.transitions),
            ),
        ),
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
