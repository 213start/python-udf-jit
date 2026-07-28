from __future__ import annotations

from dataclasses import astuple
import gc
import pickle
import weakref
import unittest

from python_udf_jit.compiler.identity import capture_identities
from python_udf_jit.runtime.continuation import (
    CONTINUATION_ABI_VERSION,
    CommitBoundary,
    CommitPhase,
    ContinuationContract,
    ContinuationError,
    ContinuationState,
    InterpreterContinuation,
    LiveValueKind,
    LiveValueSpec,
    MaterializedLiveValue,
    RecoveryScope,
    ResumeSourceMap,
    SideExit,
    SideExitOrigin,
    WholeFunctionInterpreter,
    select_interpreter_path,
    side_exit_from_cinderx_payload,
)


def _source_function(value):
    return value


def _resume_function(state):
    return state.values["value"]


_SOURCE_IDENTITY = capture_identities(_source_function).source


class _Marker:
    pass


def _contract(**changes) -> ContinuationContract:
    values = {
        "abi_version": CONTINUATION_ABI_VERSION,
        "resume_id": "v1:after-branch",
        "source_identity": _SOURCE_IDENTITY,
        "source_code": _source_function.__code__,
        "resume_code": _resume_function.__code__,
        "source_map": ResumeSourceMap(
            schema_version=1,
            bytecode_offset=8,
            line=20,
            column=4,
            end_line=20,
            end_column=12,
        ),
        "live_values": (
            LiveValueSpec("alias", LiveValueKind.PYTHON_OBJECT),
            LiveValueSpec(
                "nullable",
                LiveValueKind.PYTHON_OBJECT,
                nullable=True,
            ),
            LiveValueSpec(
                "value",
                LiveValueKind.PYTHON_OBJECT,
                branch_join=True,
                borrowed=True,
            ),
        ),
        "alias_groups": (("alias", "value"),),
        "preserves_active_exception": True,
        "proof_complete": True,
    }
    values.update(changes)
    return ContinuationContract(**values)


def _object(value):
    return MaterializedLiveValue.materialized(
        LiveValueKind.PYTHON_OBJECT,
        value,
    )


class InterpreterContinuationTests(unittest.TestCase):
    def test_cross_code_object_state_preserves_null_alias_and_exception(self) -> None:
        marker = _Marker()
        failure = LookupError("active")
        seen = {}

        def resume(captured):
            seen["same_alias"] = (
                captured.values["alias"] is captured.values["value"]
            )
            seen["exception"] = captured.active_exception
            return captured.values["nullable"]

        contract = _contract(resume_code=resume.__code__)
        boundary = CommitBoundary()
        boundary.commit()
        state = ContinuationState.capture(
            contract,
            {
                "alias": _object(marker),
                "nullable": _object(None),
                "value": _object(marker),
            },
            active_exception=failure,
            keepalives={"value": marker},
        )
        continuation = InterpreterContinuation(contract, resume)
        result = continuation.resume(
            SideExit(
                abi_version=CONTINUATION_ABI_VERSION,
                reason="guard_miss",
                resume_id="v1:after-branch",
                source_identity=contract.source_identity,
                source_map=contract.source_map,
                state=state,
                boundary=boundary,
                origin=SideExitOrigin.CINDERX_DEOPT,
                recovery_scope=RecoveryScope.REGION,
            )
        )

        self.assertIsNone(result)
        self.assertTrue(seen["same_alias"])
        self.assertIs(seen["exception"], failure)
        self.assertIsNot(contract.source_code, contract.resume_code)
        with self.assertRaises(TypeError):
            pickle.dumps(continuation)

    def test_keepalive_dominates_resume_and_suffix_is_claimed_exactly_once(self) -> None:
        marker = _Marker()
        reference = weakref.ref(marker)
        contract = _contract()
        boundary = CommitBoundary()
        boundary.commit()
        state = ContinuationState.capture(
            contract,
            {
                "alias": _object(marker),
                "nullable": _object(None),
                "value": _object(marker),
            },
            active_exception=RuntimeError("active"),
            keepalives={"value": marker},
        )
        side_exit = SideExit(
            abi_version=CONTINUATION_ABI_VERSION,
            reason="graph_break",
            resume_id=contract.resume_id,
            source_identity=contract.source_identity,
            source_map=contract.source_map,
            state=state,
            boundary=boundary,
            origin=SideExitOrigin.GRAPH_BREAK,
            recovery_scope=RecoveryScope.REGION,
        )
        marker = None
        gc.collect()
        self.assertIsNotNone(reference())

        continuation = InterpreterContinuation(contract, _resume_function)
        self.assertIs(continuation.resume(side_exit), reference())
        with self.assertRaisesRegex(ContinuationError, "suffix_already_claimed"):
            continuation.resume(side_exit)

    def test_cinderx_deopt_can_only_recover_the_region(self) -> None:
        contract = _contract()
        boundary = CommitBoundary()
        boundary.commit()
        marker = object()
        state = ContinuationState.capture(
            contract,
            {
                "alias": _object(marker),
                "nullable": _object(None),
                "value": _object(marker),
            },
            active_exception=RuntimeError("active"),
            keepalives={"value": marker},
        )

        with self.assertRaisesRegex(ContinuationError, "side_exit_scope"):
            SideExit(
                abi_version=CONTINUATION_ABI_VERSION,
                reason="deopt",
                resume_id=contract.resume_id,
                source_identity=contract.source_identity,
                source_map=contract.source_map,
                state=state,
                boundary=boundary,
                origin=SideExitOrigin.CINDERX_DEOPT,
                recovery_scope=RecoveryScope.WHOLE_FUNCTION,
            )

    def test_incomplete_proof_selects_whole_function_before_region_execution(self) -> None:
        calls = []

        def original(value):
            calls.append(("whole", value))
            return value + 1

        selected = select_interpreter_path(
            _contract(proof_complete=False),
            resume=_resume_function,
            original=original,
        )

        self.assertIsInstance(selected, WholeFunctionInterpreter)
        boundary = CommitBoundary()
        self.assertEqual(selected.execute(boundary, 4), 5)
        self.assertEqual(calls, [("whole", 4)])
        self.assertIs(boundary.phase, CommitPhase.WHOLE_FUNCTION_CLAIMED)
        with self.assertRaisesRegex(
            ContinuationError,
            "whole_function_already_claimed",
        ):
            selected.execute(boundary, 4)
        self.assertEqual(calls, [("whole", 4)])

    def test_live_value_shape_kind_and_materialization_are_fail_closed(
        self,
    ) -> None:
        contract = _contract(
            live_values=(
                LiveValueSpec("flag", LiveValueKind.BOOL),
                LiveValueSpec("count", LiveValueKind.INT32),
                LiveValueSpec(
                    "ratio",
                    LiveValueKind.FLOAT32,
                    nullable=True,
                ),
            ),
            alias_groups=(),
            preserves_active_exception=False,
        )
        valid = {
            "flag": MaterializedLiveValue.materialized(
                LiveValueKind.BOOL,
                True,
            ),
            "count": MaterializedLiveValue.materialized(
                LiveValueKind.INT32,
                (1 << 31) - 1,
            ),
            "ratio": MaterializedLiveValue.materialized(
                LiveValueKind.FLOAT32,
                None,
            ),
        }
        state = ContinuationState.capture(contract, valid)
        self.assertEqual(state.values["count"], (1 << 31) - 1)

        cases = (
            (
                {name: value for name, value in valid.items() if name != "flag"},
                "live_value_names_mismatch",
            ),
            (
                {
                    **valid,
                    "flag": MaterializedLiveValue.materialized(
                        LiveValueKind.INT32,
                        1,
                    ),
                },
                "live_value_kind_mismatch:flag",
            ),
            (
                {
                    **valid,
                    "count": MaterializedLiveValue.materialized(
                        LiveValueKind.INT32,
                        1 << 31,
                    ),
                },
                "live_value_type_mismatch:count",
            ),
            (
                {
                    **valid,
                    "ratio": MaterializedLiveValue.failed(
                        LiveValueKind.FLOAT32
                    ),
                },
                "live_value_materialization_failed:ratio",
            ),
        )
        for values, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ContinuationError, reason):
                    ContinuationState.capture(contract, values)

    def test_stale_continuation_abi_is_rejected_before_resume(self) -> None:
        with self.assertRaisesRegex(
            ContinuationError,
            "continuation_abi_mismatch",
        ):
            _contract(abi_version=CONTINUATION_ABI_VERSION + 1)

    def test_cinderx_payload_is_bound_to_contract_and_commit_boundary(
        self,
    ) -> None:
        marker = _Marker()
        contract = _contract(resume_id="v1:" + "a" * 64)
        boundary = CommitBoundary()
        boundary.commit()
        payload = (
            CONTINUATION_ABI_VERSION,
            "cinderx_deopt",
            contract.resume_id,
            contract.source_identity.namespace_sha256,
            contract.source_identity.code_sha256,
            contract.source_identity.first_line,
            astuple(contract.source_map),
            (
                ("python_object", False, True, marker),
                ("python_object", True, True, None),
                ("python_object", False, True, marker),
            ),
            RuntimeError("active"),
            True,
        )

        side_exit = side_exit_from_cinderx_payload(
            payload,
            contract=contract,
            boundary=boundary,
        )
        self.assertIs(
            InterpreterContinuation(contract, _resume_function).resume(
                side_exit
            ),
            marker,
        )
        self.assertIs(
            side_exit.state.values["alias"],
            side_exit.state.values["value"],
        )

        failed_materialization = list(payload)
        failed_entries = list(failed_materialization[7])
        failed_entries[0] = ("python_object", False, False, None)
        failed_materialization[7] = tuple(failed_entries)
        failed_boundary = CommitBoundary()
        failed_boundary.commit()
        with self.assertRaisesRegex(
            ContinuationError,
            "live_value_materialization_failed",
        ):
            side_exit_from_cinderx_payload(
                tuple(failed_materialization),
                contract=contract,
                boundary=failed_boundary,
            )

        tampered = list(payload)
        tampered[4] = "0" * 64
        with self.assertRaisesRegex(
            ContinuationError,
            "source_identity_mismatch",
        ):
            side_exit_from_cinderx_payload(
                tuple(tampered),
                contract=contract,
                boundary=CommitBoundary(),
            )


if __name__ == "__main__":
    unittest.main()
