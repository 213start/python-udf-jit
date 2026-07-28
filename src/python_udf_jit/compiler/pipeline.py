from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from python_udf_jit.compiler.abstract_interpreter import CapturedProgram
from python_udf_jit.compiler.analyses import AnalysisSummary
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


def _import_legacy_capture(captured: CaptureIR) -> SemanticCoreModule:
    if not captured.instructions:
        raise ValueError("legacy capture has no semantic operations")
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
                raise ValueError("legacy float constant is invalid")
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
                raise ValueError("legacy capture stack is invalid")
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
                f"legacy capture operation is unsupported: {instruction.op}"
            )
    if not return_operation_id:
        raise ValueError("legacy capture has no return")
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


def compile_semantic(
    capture_module: CaptureIR | CapturedProgram | SemanticCoreModule,
    policy: PassPolicy = PassPolicy(),
) -> SemanticCompileResult:
    try:
        policy.verify()
        if isinstance(capture_module, CaptureIR):
            module = _import_legacy_capture(capture_module)
            status = SemanticCompileStatus.COMPILED
            reason = "verified_semantic_ir"
        elif isinstance(capture_module, CapturedProgram):
            module = _import_python_fallback(capture_module)
            status = SemanticCompileStatus.PYTHON_FALLBACK
            reason = "whole_function_python_region"
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
