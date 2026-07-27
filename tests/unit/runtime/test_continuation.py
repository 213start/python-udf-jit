from __future__ import annotations

import gc
import pickle
import weakref
import unittest

from python_udf_jit.runtime.continuation import (
    CommitBoundary,
    ContinuationContract,
    ContinuationError,
    ContinuationState,
    InterpreterContinuation,
    RecoveryScope,
    ResumeSourceMap,
    SideExit,
    SideExitOrigin,
    WholeFunctionInterpreter,
    select_interpreter_path,
)


def _source_function(value):
    return value


def _resume_function(state):
    return state.values["value"]


class _Marker:
    pass


def _contract(**changes) -> ContinuationContract:
    values = {
        "contract_version": 1,
        "resume_id": "v1:after-branch",
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
        "live_names": ("alias", "nullable", "value"),
        "nullable_names": ("nullable",),
        "branch_join_names": ("value",),
        "borrowed_names": ("value",),
        "preserves_aliases": True,
        "preserves_active_exception": True,
        "commit_required": True,
        "proof_complete": True,
    }
    values.update(changes)
    return ContinuationContract(**values)


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
            {"alias": marker, "nullable": None, "value": marker},
            active_exception=failure,
            keepalives={"value": marker},
        )
        continuation = InterpreterContinuation(contract, resume)
        result = continuation.resume(
            SideExit(
                reason="guard_miss",
                resume_id="v1:after-branch",
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
            {"alias": marker, "nullable": None, "value": marker},
            active_exception=RuntimeError("active"),
            keepalives={"value": marker},
        )
        side_exit = SideExit(
            reason="graph_break",
            resume_id=contract.resume_id,
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
        state = ContinuationState.capture(
            contract,
            {"alias": object(), "nullable": None, "value": object()},
            active_exception=RuntimeError("active"),
            keepalives={"value": object()},
        )

        with self.assertRaisesRegex(ContinuationError, "deopt_scope"):
            SideExit(
                reason="deopt",
                resume_id=contract.resume_id,
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
        self.assertEqual(selected.execute(4), 5)
        self.assertEqual(calls, [("whole", 4)])


if __name__ == "__main__":
    unittest.main()
