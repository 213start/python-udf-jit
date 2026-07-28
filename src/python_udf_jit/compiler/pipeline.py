from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from python_udf_jit.compiler.abstract_interpreter import CapturedProgram
from python_udf_jit.compiler.analyses import AnalysisSummary
from python_udf_jit.compiler.call_models import CallKind, Effect
from python_udf_jit.compiler.capture import CaptureIR
from python_udf_jit.compiler.core_ir import (
    Determinism,
    EffectKind,
    LogicalType,
    Nullability,
    SemanticBlock,
    SemanticControlEdge,
    SemanticCoreModule,
    SemanticLiteral,
    SemanticOperation,
    SemanticPythonRegion,
    build_semantic_module,
)
from python_udf_jit.compiler.passes import (
    CanonicalizePass,
    PassManager,
    PassManagerError,
    SemanticSimplifyPass,
)
from python_udf_jit.compiler.region import (
    SemanticRegionGraph,
    form_semantic_region_graph,
)


SEMANTIC_PIPELINE_VERSION = 1


class SemanticCompileStatus(StrEnum):
    COMPILED = "compiled"
    PYTHON_FALLBACK = "python_fallback"
    REJECTED = "rejected"


class SemanticRejectCode(StrEnum):
    IMPORT_FAILED = "import_failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    ANALYSIS_CONFLICT = "analysis_conflict"
    VERIFY_FAILED = "verify_failed"


@dataclass(frozen=True)
class PassPolicy:
    format_version: int = SEMANTIC_PIPELINE_VERSION
    max_nodes: int = 4096
    max_iterations: int = 8
    max_time_ms: int = 1_000
    verify_each_stage: bool = True

    def to_document(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "max_iterations": self.max_iterations,
            "max_nodes": self.max_nodes,
            "max_time_ms": self.max_time_ms,
            "verify_each_stage": self.verify_each_stage,
        }

    def policy_hash(self) -> str:
        encoded = json.dumps(
            self.to_document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(
            b"python-udf-jit-semantic-policy-v1\0" + encoded
        ).hexdigest()

    def verify(self) -> None:
        if (
            self.format_version != SEMANTIC_PIPELINE_VERSION
            or self.max_nodes <= 0
            or self.max_iterations < 2
            or self.max_time_ms <= 0
            or type(self.verify_each_stage) is not bool
        ):
            raise ValueError("invalid semantic pass policy")


@dataclass(frozen=True)
class SemanticCompileResult:
    status: SemanticCompileStatus
    reason_code: str
    core_module: SemanticCoreModule | None
    region_graph: SemanticRegionGraph | None
    analysis_summary: AnalysisSummary | None
    policy_hash: str
    executed_passes: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.status is not SemanticCompileStatus.REJECTED


def _attributes(**values: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(values.items()))


def _lower_scalar_capture(captured: CaptureIR) -> SemanticCoreModule:
    if not captured.instructions:
        raise ValueError("scalar capture has no semantic operations")
    operations: list[SemanticOperation] = []
    operation_ids: list[str] = []
    stack: list[str] = []
    next_value = 0
    return_operation_id = ""
    argument_value: str | None = None
    operation_mapping = {
        "add.f64": "binary.add",
        "sub.f64": "binary.sub",
        "mul.f64": "binary.mul",
    }

    def append(operation: SemanticOperation) -> None:
        operations.append(operation)
        operation_ids.append(operation.operation_id)

    for instruction in captured.instructions:
        operation_id = f"op{len(operations)}"
        if instruction.op == "arg.load":
            if argument_value is not None:
                stack.append(argument_value)
                continue
            result_id = f"%{next_value}"
            next_value += 1
            append(
                SemanticOperation(
                    operation_id,
                    "b0",
                    "argument",
                    (),
                    result_id,
                    LogicalType.FLOAT64,
                    Nullability.NON_NULL,
                    EffectKind.PURE,
                    False,
                    None,
                    Determinism.DETERMINISTIC,
                    _attributes(index="0"),
                )
            )
            stack.append(result_id)
            argument_value = result_id
        elif instruction.op == "const.f64":
            if type(instruction.literal) is not float:
                raise ValueError("scalar float constant is invalid")
            result_id = f"%{next_value}"
            next_value += 1
            append(
                SemanticOperation(
                    operation_id,
                    "b0",
                    "constant",
                    (),
                    result_id,
                    LogicalType.FLOAT64,
                    Nullability.NON_NULL,
                    EffectKind.PURE,
                    False,
                    None,
                    Determinism.DETERMINISTIC,
                    literal=SemanticLiteral.from_value(
                        instruction.literal
                    ),
                )
            )
            stack.append(result_id)
        elif instruction.op in operation_mapping:
            right = stack.pop()
            left = stack.pop()
            result_id = f"%{next_value}"
            next_value += 1
            append(
                SemanticOperation(
                    operation_id,
                    "b0",
                    operation_mapping[instruction.op],
                    (left, right),
                    result_id,
                    LogicalType.FLOAT64,
                    Nullability.NON_NULL,
                    EffectKind.PURE,
                    False,
                    None,
                    Determinism.DETERMINISTIC,
                )
            )
            stack.append(result_id)
        elif instruction.op == "return":
            if len(stack) != 1:
                raise ValueError("scalar capture stack is invalid")
            return_operation_id = operation_id
            append(
                SemanticOperation(
                    operation_id,
                    "b0",
                    "return",
                    (stack.pop(),),
                    None,
                    LogicalType.FLOAT64,
                    Nullability.NON_NULL,
                    EffectKind.PURE,
                    False,
                    None,
                    Determinism.DETERMINISTIC,
                )
            )
        else:
            raise ValueError(
                f"scalar capture operation is unsupported: {instruction.op}"
            )
    if not return_operation_id:
        raise ValueError("scalar capture has no return")
    return build_semantic_module(
        function_id=captured.fallback_identity.code_sha256,
        entry_block="b0",
        input_types=(LogicalType.FLOAT64,),
        input_nullability=(Nullability.NON_NULL,),
        output_type=LogicalType.FLOAT64,
        output_nullability=Nullability.NON_NULL,
        blocks=(SemanticBlock("b0", tuple(operation_ids)),),
        control_edges=(),
        operations=tuple(operations),
        return_operation_id=return_operation_id,
    )


def _import_python_fallback(
    captured: CapturedProgram,
) -> SemanticCoreModule:
    argument_count = captured.frontend.decoded_bytecode.argument_count
    if argument_count <= 0:
        raise ValueError("captured program has no positional input")
    operations: list[SemanticOperation] = []
    operation_ids: list[str] = []
    arguments: list[str] = []
    for index in range(argument_count):
        operation_id = f"op{len(operations)}"
        result_id = f"%{index}"
        operations.append(
            SemanticOperation(
                operation_id,
                "b0",
                "argument",
                (),
                result_id,
                LogicalType.UNKNOWN,
                Nullability.NULLABLE,
                EffectKind.PURE,
                False,
                None,
                Determinism.DETERMINISTIC,
                _attributes(index=str(index)),
            )
        )
        operation_ids.append(operation_id)
        arguments.append(result_id)

    source_regions = captured.analysis.python_regions
    source_start = (
        min(region.start_offset for region in source_regions)
        if source_regions
        else 0
    )
    source_end = (
        max(region.end_offset for region in source_regions)
        if source_regions
        else captured.frontend.decoded_bytecode.code_size
    )
    resume_id = (
        source_regions[0].resume_id
        if source_regions
        else captured.identities.code.sha256
    )
    python_operation_id = f"op{len(operations)}"
    python_result = f"%{len(arguments)}"
    python_region_id = "python:whole-function"
    operations.append(
        SemanticOperation(
            python_operation_id,
            "b0",
            "python.region",
            tuple(arguments),
            python_result,
            LogicalType.UNKNOWN,
            Nullability.NULLABLE,
            EffectKind.PYTHON,
            True,
            0,
            Determinism.UNKNOWN,
            source_offset=source_start,
            python_region_id=python_region_id,
        )
    )
    operation_ids.append(python_operation_id)
    return_operation_id = f"op{len(operations)}"
    operations.append(
        SemanticOperation(
            return_operation_id,
            "b0",
            "return",
            (python_result,),
            None,
            LogicalType.UNKNOWN,
            Nullability.NULLABLE,
            EffectKind.PURE,
            False,
            None,
            Determinism.DETERMINISTIC,
            source_offset=source_end,
        )
    )
    operation_ids.append(return_operation_id)
    return build_semantic_module(
        function_id=captured.identities.code.sha256,
        entry_block="b0",
        input_types=tuple(
            LogicalType.UNKNOWN for _ in range(argument_count)
        ),
        input_nullability=tuple(
            Nullability.NULLABLE for _ in range(argument_count)
        ),
        output_type=LogicalType.UNKNOWN,
        output_nullability=Nullability.NULLABLE,
        blocks=(SemanticBlock("b0", tuple(operation_ids)),),
        control_edges=(),
        operations=tuple(operations),
        python_regions=(
            SemanticPythonRegion(
                python_region_id,
                python_operation_id,
                tuple(arguments),
                (python_result,),
                resume_id,
                EffectKind.PYTHON,
                True,
                (),
                source_start,
                source_end,
            ),
        ),
        return_operation_id=return_operation_id,
    )


def _import_scalar_graph_break(
    captured: CapturedProgram,
) -> SemanticCoreModule:
    """Import one proven float64 global-call barrier without widening scope."""

    frontend = captured.frontend
    decoded = frontend.decoded_bytecode
    if (
        decoded.argument_count != 1
        or len(frontend.control_flow_graph.blocks) != 1
        or decoded.exception_handlers
        or any(
            instruction.jump_target is not None
            for instruction in decoded.instructions
        )
        or len(captured.analysis.python_regions) != 1
    ):
        raise ValueError("unsupported scalar graph-break control flow")
    source_region = captured.analysis.python_regions[0]
    if (
        source_region.effect is not Effect.SIDE_EFFECT
        or source_region.exception_state.handler_offsets
        or source_region.exception_state.preserves_lasti
        or len(source_region.live_in) != 1
        or len(source_region.live_out) != 1
    ):
        raise ValueError("unsupported scalar graph-break region")
    instructions = decoded.instructions
    instruction_by_offset = {
        instruction.offset: instruction for instruction in instructions
    }
    region_instructions = tuple(
        instruction_by_offset[offset]
        for offset in source_region.instruction_offsets
    )
    allowed_region_opcodes = {
        "LOAD_GLOBAL",
        "LOAD_FAST",
        "LOAD_FAST_BORROW",
        "LOAD_CONST",
        "CALL",
    }
    call_operations = tuple(
        operation
        for operation in captured.analysis.operations
        if operation.bytecode_offset in source_region.instruction_offsets
        and operation.call_kind is not None
    )
    if (
        not region_instructions
        or region_instructions[0].opcode_name != "LOAD_GLOBAL"
        or region_instructions[-1].opcode_name != "CALL"
        or any(
            instruction.opcode_name not in allowed_region_opcodes
            for instruction in region_instructions
        )
        or sum(
            instruction.opcode_name == "LOAD_GLOBAL"
            for instruction in region_instructions
        )
        != 1
        or sum(
            instruction.opcode_name == "CALL"
            for instruction in region_instructions
        )
        != 1
        or len(call_operations) != 1
        or call_operations[0].call_kind != CallKind.OPAQUE.value
    ):
        raise ValueError("unsupported scalar graph-break call shape")

    states = {
        state.bytecode_offset: state
        for state in frontend.control_flow_graph.instruction_states
    }
    first_state = states[source_region.start_offset]
    if first_state.stack_before:
        raise ValueError("scalar graph-break requires an empty entry stack")
    live_fast_indexes = {
        index
        for index, name in enumerate(first_state.locals_before)
        if name == source_region.live_in[0]
    }
    if not live_fast_indexes or any(
        instruction.argument not in live_fast_indexes
        for instruction in region_instructions
        if instruction.opcode_name in {"LOAD_FAST", "LOAD_FAST_BORROW"}
    ):
        raise ValueError("scalar graph-break live-in mismatch")
    for instruction in region_instructions:
        if instruction.opcode_name != "LOAD_CONST":
            continue
        if (
            instruction.argument is None
            or not 0
            <= instruction.argument
            < len(captured.scalar_constants)
            or captured.scalar_constants[instruction.argument] is None
        ):
            raise ValueError("scalar graph-break region constant is not float64")
    following_index = next(
        (
            index
            for index, instruction in enumerate(instructions)
            if instruction.offset == source_region.end_offset
        ),
        None,
    )
    if (
        following_index is None
        or instructions[following_index].operation != "stack.pop"
    ):
        raise ValueError("scalar graph-break call result is not discarded")

    operations: list[SemanticOperation] = []
    operation_ids: list[str] = []
    stack: list[str] = []
    locals_: list[str | None] = [None] * decoded.local_count
    argument_value: str | None = None
    next_value = 0
    exception_order = 0

    def next_result() -> str:
        nonlocal next_value
        result = f"%{next_value}"
        next_value += 1
        return result

    def append(operation: SemanticOperation) -> None:
        operations.append(operation)
        operation_ids.append(operation.operation_id)

    def resolve_abstract_value(name: str) -> str:
        for index, abstract_name in enumerate(first_state.locals_before):
            if abstract_name == name and locals_[index] is not None:
                return locals_[index]  # type: ignore[return-value]
        for index, abstract_name in enumerate(first_state.stack_before):
            if abstract_name == name:
                return stack[index]
        raise ValueError("scalar graph-break live-in is unavailable")

    region_offsets = set(source_region.instruction_offsets)
    cursor = 0
    return_operation_id = ""
    while cursor < len(instructions):
        instruction = instructions[cursor]
        if instruction.offset == source_region.start_offset:
            if not region_offsets:
                raise ValueError("scalar graph-break region is empty")
            live_in = tuple(
                resolve_abstract_value(name)
                for name in source_region.live_in
            )
            if len(live_in) != 1:
                raise ValueError("scalar graph-break live-in shape")
            result_id = next_result()
            operation_id = f"op{len(operations)}"
            region_id = "python:opaque-global-call"
            append(
                SemanticOperation(
                    operation_id,
                    "b0",
                    "python.region",
                    live_in,
                    result_id,
                    LogicalType.UNKNOWN,
                    Nullability.NULLABLE,
                    EffectKind.PYTHON,
                    True,
                    exception_order,
                    Determinism.UNKNOWN,
                    source_offset=source_region.start_offset,
                    python_region_id=region_id,
                )
            )
            exception_order += 1
            stack.append(result_id)
            while (
                cursor < len(instructions)
                and instructions[cursor].offset in region_offsets
            ):
                cursor += 1
            continue
        if instruction.offset in region_offsets:
            raise ValueError("scalar graph-break region is noncontiguous")
        operation_id = f"op{len(operations)}"
        opname = instruction.opcode_name
        if opname == "RESUME":
            cursor += 1
            continue
        if instruction.operation == "local.load":
            if instruction.argument is None:
                raise ValueError("scalar local load has no index")
            if instruction.argument == 0 and locals_[0] is None:
                argument_value = next_result()
                locals_[0] = argument_value
                append(
                    SemanticOperation(
                        operation_id,
                        "b0",
                        "argument",
                        (),
                        argument_value,
                        LogicalType.FLOAT64,
                        Nullability.NON_NULL,
                        EffectKind.PURE,
                        False,
                        None,
                        Determinism.DETERMINISTIC,
                        _attributes(index="0"),
                        source_offset=instruction.offset,
                    )
                )
            value = locals_[instruction.argument]
            if value is None:
                raise ValueError("scalar local is undefined")
            stack.append(value)
        elif instruction.operation == "local.store":
            if instruction.argument is None or not stack:
                raise ValueError("scalar local store is invalid")
            locals_[instruction.argument] = stack.pop()
        elif instruction.operation == "constant.load":
            if instruction.argument is None:
                raise ValueError("scalar constant has no index")
            if not 0 <= instruction.argument < len(
                captured.scalar_constants
            ):
                raise ValueError("scalar constant index is invalid")
            encoded_value = captured.scalar_constants[
                instruction.argument
            ]
            if encoded_value is None:
                raise ValueError("scalar graph-break constant is not float64")
            value = float.fromhex(encoded_value)
            result_id = next_result()
            append(
                SemanticOperation(
                    operation_id,
                    "b0",
                    "constant",
                    (),
                    result_id,
                    LogicalType.FLOAT64,
                    Nullability.NON_NULL,
                    EffectKind.PURE,
                    False,
                    None,
                    Determinism.DETERMINISTIC,
                    literal=SemanticLiteral.from_value(value),
                    source_offset=instruction.offset,
                )
            )
            stack.append(result_id)
        elif instruction.operation in {
            "binary.add",
            "binary.subtract",
            "binary.multiply",
        }:
            if len(stack) < 2:
                raise ValueError("scalar graph-break binary stack underflow")
            right = stack.pop()
            left = stack.pop()
            result_id = next_result()
            append(
                SemanticOperation(
                    operation_id,
                    "b0",
                    {
                        "binary.add": "binary.add",
                        "binary.subtract": "binary.sub",
                        "binary.multiply": "binary.mul",
                    }[instruction.operation],
                    (left, right),
                    result_id,
                    LogicalType.FLOAT64,
                    Nullability.NON_NULL,
                    EffectKind.PURE,
                    False,
                    None,
                    Determinism.DETERMINISTIC,
                    source_offset=instruction.offset,
                )
            )
            stack.append(result_id)
        elif instruction.operation == "stack.pop":
            if not stack:
                raise ValueError("scalar graph-break pop underflow")
            stack.pop()
        elif instruction.operation == "return.value":
            if len(stack) != 1:
                raise ValueError("scalar graph-break return stack")
            return_operation_id = operation_id
            append(
                SemanticOperation(
                    operation_id,
                    "b0",
                    "return",
                    (stack.pop(),),
                    None,
                    LogicalType.FLOAT64,
                    Nullability.NON_NULL,
                    EffectKind.PURE,
                    False,
                    None,
                    Determinism.DETERMINISTIC,
                    source_offset=instruction.offset,
                )
            )
        else:
            raise ValueError(
                f"unsupported scalar graph-break opcode:{opname}"
            )
        cursor += 1

    python_operation = next(
        operation
        for operation in operations
        if operation.op == "python.region"
    )
    if (
        argument_value is None
        or not return_operation_id
        or stack
        or exception_order != 1
        or any(
            python_operation.result_id in operation.operands
            for operation in operations
            if operation.operation_id
            != python_operation.operation_id
        )
    ):
        raise ValueError("scalar graph-break semantic stack is incomplete")
    semantic_region = SemanticPythonRegion(
        python_operation.python_region_id or "",
        python_operation.operation_id,
        python_operation.operands,
        (python_operation.result_id or "",),
        source_region.resume_id,
        EffectKind.PYTHON,
        True,
        (),
        source_region.start_offset,
        source_region.end_offset,
    )
    return build_semantic_module(
        function_id=captured.identities.code.sha256,
        entry_block="b0",
        input_types=(LogicalType.FLOAT64,),
        input_nullability=(Nullability.NON_NULL,),
        output_type=LogicalType.FLOAT64,
        output_nullability=Nullability.NON_NULL,
        blocks=(SemanticBlock("b0", tuple(operation_ids)),),
        control_edges=(),
        operations=tuple(operations),
        python_regions=(semantic_region,),
        return_operation_id=return_operation_id,
    )


def compile_semantic(
    capture_module: CaptureIR | CapturedProgram | SemanticCoreModule,
    policy: PassPolicy = PassPolicy(),
) -> SemanticCompileResult:
    try:
        policy.verify()
        if isinstance(capture_module, CaptureIR):
            module = _lower_scalar_capture(capture_module)
            status = SemanticCompileStatus.COMPILED
            reason = "verified_semantic_ir"
        elif isinstance(capture_module, CapturedProgram):
            try:
                module = _import_scalar_graph_break(capture_module)
            except ValueError:
                module = _import_python_fallback(capture_module)
                status = SemanticCompileStatus.PYTHON_FALLBACK
                reason = "whole_function_python_region"
            else:
                status = SemanticCompileStatus.COMPILED
                reason = "verified_scalar_graph_break"
        elif isinstance(capture_module, SemanticCoreModule):
            module = capture_module
            status = SemanticCompileStatus.COMPILED
            reason = "verified_semantic_ir"
        else:
            raise TypeError("unsupported semantic pipeline input")
    except (TypeError, ValueError):
        return SemanticCompileResult(
            SemanticCompileStatus.REJECTED,
            SemanticRejectCode.IMPORT_FAILED.value,
            None,
            None,
            None,
            policy.policy_hash(),
            (),
        )

    manager: PassManager | None = None
    try:
        if len(module.operations) > policy.max_nodes:
            return SemanticCompileResult(
                SemanticCompileStatus.REJECTED,
                SemanticRejectCode.BUDGET_EXCEEDED.value,
                None,
                None,
                None,
                policy.policy_hash(),
                (),
            )
        manager = PassManager(
            module,
            max_nodes=policy.max_nodes,
            max_iterations=policy.max_iterations,
            max_time_ms=policy.max_time_ms,
            verify_each_stage=policy.verify_each_stage,
        )
        result_module = manager.run(
            (CanonicalizePass(), SemanticSimplifyPass())
        )
        graph = form_semantic_region_graph(result_module)
        summary = manager.analyses.summary()
        return SemanticCompileResult(
            status,
            reason,
            result_module,
            graph,
            summary,
            policy.policy_hash(),
            tuple(manager.executed_passes),
        )
    except PassManagerError as error:
        reason_code = (
            SemanticRejectCode.BUDGET_EXCEEDED
            if error.code.value == "budget_exceeded"
            else SemanticRejectCode.VERIFY_FAILED
        )
    except ValueError as error:
        reason_code = (
            SemanticRejectCode.ANALYSIS_CONFLICT
            if "stale_preserved_analysis" in str(error)
            else SemanticRejectCode.VERIFY_FAILED
        )
    return SemanticCompileResult(
        SemanticCompileStatus.REJECTED,
        reason_code.value,
        None,
        None,
        None,
        policy.policy_hash(),
        () if manager is None else tuple(manager.executed_passes),
    )


def compile_semantic_sequence(
    capture_modules: tuple[
        CaptureIR | CapturedProgram | SemanticCoreModule,
        ...,
    ],
    policy: PassPolicy = PassPolicy(),
) -> tuple[SemanticCompileResult, ...]:
    return tuple(
        compile_semantic(capture_module, policy)
        for capture_module in capture_modules
    )
