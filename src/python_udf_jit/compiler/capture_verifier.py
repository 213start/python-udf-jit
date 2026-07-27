from __future__ import annotations

from enum import StrEnum

from python_udf_jit.compiler.abstract_interpreter import (
    ABSTRACT_CAPTURE_VERSION,
    AbstractCapture,
    CapturedProgram,
)
from python_udf_jit.compiler.call_models import CallKind, Effect
from python_udf_jit.compiler.capture_ir import verify_capture_frontend
from python_udf_jit.compiler.identity import verify_capture_identities


class CaptureVerificationRejectCode(StrEnum):
    INVALID_ANALYSIS = "invalid_capture_analysis"
    OPERATION_MISMATCH = "capture_operation_mismatch"
    REGION_OVERLAP = "python_region_overlap"
    REGION_COVERAGE = "python_region_coverage"
    LIVE_VALUE = "invalid_region_live_value"
    EXCEPTION_STATE = "invalid_region_exception_state"
    RESUME_ID = "invalid_resume_id"
    SEMANTIC_HASH = "capture_semantic_hash_mismatch"
    IDENTITY_MISMATCH = "capture_identity_mismatch"


class CaptureVerificationError(ValueError):
    def __init__(
        self,
        code: CaptureVerificationRejectCode,
        detail: str = "",
    ) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}:{detail}")


def _fail(
    code: CaptureVerificationRejectCode,
    detail: str = "",
) -> None:
    raise CaptureVerificationError(code, detail)


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _expected_effect(operation: str) -> tuple[Effect, bool]:
    if operation.startswith(("field.", "method.", "python.")):
        return Effect.SIDE_EFFECT, True
    if operation.startswith(
        (
            "aggregate.",
            "binary.",
            "compare.",
            "convert.",
            "exception.",
            "global.",
            "index.",
            "unary.",
        )
    ):
        return Effect.MAY_RAISE, True
    return Effect.PURE, False


def verify_abstract_capture(
    analysis: AbstractCapture,
    program: CapturedProgram,
) -> None:
    decoded = program.frontend.decoded_bytecode
    graph = program.frontend.control_flow_graph
    if (
        analysis.format_version != ABSTRACT_CAPTURE_VERSION
        or analysis.code_sha256 != program.identities.code.sha256
        or not _valid_digest(analysis.code_sha256)
    ):
        _fail(CaptureVerificationRejectCode.IDENTITY_MISMATCH)
    if len(analysis.operations) != len(decoded.instructions):
        _fail(CaptureVerificationRejectCode.OPERATION_MISMATCH, "count")
    state_by_offset = {
        state.bytecode_offset: state
        for state in graph.instruction_states
    }
    string_values: set[str] = set()
    for abstract, instruction in zip(
        analysis.operations,
        decoded.instructions,
        strict=True,
    ):
        if (
            abstract.bytecode_offset != instruction.offset
            or abstract.operation != instruction.operation
            or abstract.execution not in {"capture", "python_region"}
            or type(abstract.may_raise) is not bool
            or not isinstance(abstract.effect, Effect)
        ):
            _fail(
                CaptureVerificationRejectCode.OPERATION_MISMATCH,
                str(instruction.offset),
            )
        state = state_by_offset[instruction.offset]
        if instruction.constant_kind == "str":
            string_values.update(
                set(state.stack_after) - set(state.stack_before)
            )
        uses_string = bool(set(state.stack_before) & string_values)
        must_be_python = (
            instruction.capability == "python_region"
            or instruction.operation.startswith(
                (
                    "exception.",
                    "field.",
                    "global.",
                    "method.",
                    "python.",
                )
            )
            or uses_string
        )
        if must_be_python and abstract.execution != "python_region":
            _fail(
                CaptureVerificationRejectCode.OPERATION_MISMATCH,
                f"python_region:{instruction.offset}",
            )
        if uses_string:
            string_values.update(
                set(state.stack_after) - set(state.stack_before)
            )
        if abstract.call_kind is None:
            if abstract.name_sha256 is not None:
                _fail(
                    CaptureVerificationRejectCode.INVALID_ANALYSIS,
                    "call_name_without_kind",
                )
            expected_effect, expected_may_raise = _expected_effect(
                abstract.operation
            )
            if (
                abstract.effect is not expected_effect
                or abstract.may_raise != expected_may_raise
            ):
                _fail(
                    CaptureVerificationRejectCode.INVALID_ANALYSIS,
                    "operation_effect",
                )
        else:
            try:
                call_kind = CallKind(abstract.call_kind)
            except ValueError as error:
                raise CaptureVerificationError(
                    CaptureVerificationRejectCode.INVALID_ANALYSIS,
                    "call_kind",
                ) from error
            if (
                abstract.execution != "python_region"
                or not _valid_digest(abstract.name_sha256)
                or not abstract.may_raise
                or (
                    call_kind is CallKind.OPAQUE
                    and abstract.effect is not Effect.SIDE_EFFECT
                )
                or (
                    call_kind is not CallKind.OPAQUE
                    and abstract.effect is not Effect.MAY_RAISE
                )
            ):
                _fail(
                    CaptureVerificationRejectCode.INVALID_ANALYSIS,
                    "call_model",
                )

    instruction_offsets = {
        instruction.offset for instruction in decoded.instructions
    }
    all_values: set[str] = set()
    for state in graph.instruction_states:
        all_values.update(state.stack_before)
        all_values.update(state.stack_after)
        all_values.update(
            value
            for value in (*state.locals_before, *state.locals_after)
            if value is not None
        )
    covered: set[int] = set()
    previous_end = -1
    decoded_indexes = {
        instruction.offset: index
        for index, instruction in enumerate(decoded.instructions)
    }
    block_by_offset = {
        offset: block.block_id
        for block in graph.blocks
        for offset in block.instruction_offsets
    }
    operation_by_offset = {
        operation.bytecode_offset: operation
        for operation in analysis.operations
    }
    for region in analysis.python_regions:
        region_indexes = [
            decoded_indexes[offset]
            for offset in region.instruction_offsets
            if offset in decoded_indexes
        ]
        if (
            not region.instruction_offsets
            or region.start_offset != region.instruction_offsets[0]
            or region.end_offset != region.resume_offset
            or region.start_offset < previous_end
            or tuple(region.instruction_offsets)
            != tuple(sorted(set(region.instruction_offsets)))
            or not set(region.instruction_offsets) <= instruction_offsets
            or not isinstance(region.effect, Effect)
            or type(region.may_raise) is not bool
            or not region_indexes
            or region_indexes
            != list(
                range(
                    region_indexes[0],
                    region_indexes[-1] + 1,
                )
            )
            or len(
                {
                    block_by_offset[offset]
                    for offset in region.instruction_offsets
                }
            )
            != 1
        ):
            _fail(CaptureVerificationRejectCode.REGION_OVERLAP)
        if covered & set(region.instruction_offsets):
            _fail(CaptureVerificationRejectCode.REGION_OVERLAP)
        covered.update(region.instruction_offsets)
        previous_end = region.end_offset
        if (
            tuple(region.live_in) != tuple(dict.fromkeys(region.live_in))
            or tuple(region.live_out)
            != tuple(dict.fromkeys(region.live_out))
            or not set((*region.live_in, *region.live_out)) <= all_values
        ):
            _fail(CaptureVerificationRejectCode.LIVE_VALUE)
        expected_handlers = {
            handler.target_offset
            for handler in decoded.exception_handlers
            if any(
                handler.start_offset <= offset < handler.end_offset
                for offset in region.instruction_offsets
            )
        }
        expected_lasti = any(
            handler.preserve_lasti
            for handler in decoded.exception_handlers
            if any(
                handler.start_offset <= offset < handler.end_offset
                for offset in region.instruction_offsets
            )
        )
        if (
            set(region.exception_state.handler_offsets)
            != expected_handlers
            or region.exception_state.preserves_lasti != expected_lasti
        ):
            _fail(CaptureVerificationRejectCode.EXCEPTION_STATE)
        region_operations = [
            operation_by_offset[offset]
            for offset in region.instruction_offsets
        ]
        expected_effect = (
            Effect.SIDE_EFFECT
            if any(
                operation.effect is Effect.SIDE_EFFECT
                for operation in region_operations
            )
            else Effect.MAY_RAISE
            if any(
                operation.effect is Effect.MAY_RAISE
                for operation in region_operations
            )
            else Effect.PURE
        )
        last_index = region_indexes[-1]
        expected_resume = (
            decoded.instructions[last_index + 1].offset
            if last_index + 1 < len(decoded.instructions)
            else decoded.code_size
        )
        if (
            region.effect is not expected_effect
            or region.may_raise
            != any(
                operation.may_raise
                for operation in region_operations
            )
            or region.resume_offset != expected_resume
            or region.end_offset != expected_resume
        ):
            _fail(CaptureVerificationRejectCode.INVALID_ANALYSIS, "region_summary")
        if (
            not _valid_digest(region.resume_id)
            or region.recompute_resume_id(analysis.code_sha256)
            != region.resume_id
        ):
            _fail(CaptureVerificationRejectCode.RESUME_ID)

    required = {
        operation.bytecode_offset
        for operation in analysis.operations
        if operation.execution == "python_region"
    }
    if not required <= covered:
        _fail(CaptureVerificationRejectCode.REGION_COVERAGE)
    if (
        not _valid_digest(analysis.semantic_hash)
        or analysis.recompute_semantic_hash() != analysis.semantic_hash
    ):
        _fail(CaptureVerificationRejectCode.SEMANTIC_HASH)


def verify_captured_program(program: CapturedProgram) -> None:
    verify_capture_frontend(program.frontend)
    verify_capture_identities(program.identities)
    verify_abstract_capture(program.analysis, program)
