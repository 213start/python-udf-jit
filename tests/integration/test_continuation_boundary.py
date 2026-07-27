from __future__ import annotations

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
)


def _whole_function(value, branch, effects):
    effects.append(("prefix", value))
    merged = value if branch else None
    effects.append(("suffix", merged))
    return merged


def _suffix(state):
    effects = state.values["effects"]
    merged = state.values["merged"]
    effects.append(("suffix", merged))
    if state.active_exception is not None:
        effects.append(("exception", type(state.active_exception).__name__))
    return merged


def _first_suffix(state):
    state.values["effects"].append(("middle", state.values["value"]))
    return state.values["value"]


def _second_suffix(state):
    state.values["effects"].append(("suffix", state.values["value"]))
    return state.values["value"]


def _failing_suffix(state):
    effects = state.values["effects"]
    effects.append(("suffix_enter", state.values["value"]))
    try:
        raise RuntimeError("suffix failed")
    finally:
        effects.append(("finally_cleanup", state.values["value"]))


def _source_map(offset: int, line: int) -> ResumeSourceMap:
    return ResumeSourceMap(
        schema_version=1,
        bytecode_offset=offset,
        line=line,
        column=4,
        end_line=line,
        end_column=12,
    )


class ContinuationBoundaryIntegrationTests(unittest.TestCase):
    def test_counterexample_matrix_resumes_suffix_exactly_once(self) -> None:
        for branch, value in ((True, {"value": 1}), (False, None)):
            with self.subTest(branch=branch, value=value):
                effects = []
                effects_alias = effects
                active = RuntimeError("active")
                contract = ContinuationContract(
                    contract_version=1,
                    resume_id="v1:join",
                    source_code=_whole_function.__code__,
                    resume_code=_suffix.__code__,
                    source_map=_source_map(8, 20),
                    live_names=("effects", "effects_alias", "merged"),
                    nullable_names=("merged",),
                    branch_join_names=("merged",),
                    borrowed_names=("effects",),
                    preserves_aliases=True,
                    preserves_active_exception=True,
                    commit_required=True,
                    proof_complete=True,
                )
                boundary = CommitBoundary()
                effects.append(("prefix", value))
                boundary.commit()
                state = ContinuationState.capture(
                    contract,
                    {
                        "effects": effects,
                        "effects_alias": effects_alias,
                        "merged": value if branch else None,
                    },
                    active_exception=active,
                    keepalives={"effects": effects},
                )

                result = InterpreterContinuation(contract, _suffix).resume(
                    SideExit(
                        reason="branch_join",
                        resume_id="v1:join",
                        source_map=contract.source_map,
                        state=state,
                        boundary=boundary,
                        origin=SideExitOrigin.REGION_SIDE_EXIT,
                        recovery_scope=RecoveryScope.REGION,
                    )
                )

                self.assertIs(state.values["effects"], state.values["effects_alias"])
                self.assertIs(result, value if branch else None)
                self.assertEqual(
                    effects,
                    [
                        ("prefix", value),
                        ("suffix", value if branch else None),
                        ("exception", "RuntimeError"),
                    ],
                )

    def test_two_consecutive_side_exits_execute_each_suffix_once(self) -> None:
        effects = [("prefix", 7)]
        contracts = (
            ContinuationContract(
                contract_version=1,
                resume_id="v1:first-break",
                source_code=_whole_function.__code__,
                resume_code=_first_suffix.__code__,
                source_map=_source_map(10, 30),
                live_names=("effects", "value"),
                preserves_aliases=True,
                proof_complete=True,
            ),
            ContinuationContract(
                contract_version=1,
                resume_id="v1:second-break",
                source_code=_whole_function.__code__,
                resume_code=_second_suffix.__code__,
                source_map=_source_map(20, 31),
                live_names=("effects", "value"),
                preserves_aliases=True,
                proof_complete=True,
            ),
        )
        exits = []
        for contract in contracts:
            boundary = CommitBoundary()
            boundary.commit()
            state = ContinuationState.capture(
                contract,
                {"effects": effects, "value": 7},
            )
            exits.append(
                SideExit(
                    reason="graph_break",
                    resume_id=contract.resume_id,
                    source_map=contract.source_map,
                    state=state,
                    boundary=boundary,
                    origin=SideExitOrigin.GRAPH_BREAK,
                )
            )

        self.assertEqual(
            InterpreterContinuation(contracts[0], _first_suffix).resume(
                exits[0]
            ),
            7,
        )
        self.assertEqual(
            InterpreterContinuation(contracts[1], _second_suffix).resume(
                exits[1]
            ),
            7,
        )
        self.assertEqual(
            effects,
            [("prefix", 7), ("middle", 7), ("suffix", 7)],
        )
        for contract, resume, side_exit in (
            (contracts[0], _first_suffix, exits[0]),
            (contracts[1], _second_suffix, exits[1]),
        ):
            with self.assertRaisesRegex(
                ContinuationError,
                "suffix_already_claimed",
            ):
                InterpreterContinuation(contract, resume).resume(side_exit)

    def test_commit_boundary_distinguishes_safe_fallback_from_failed_suffix(
        self,
    ) -> None:
        effects = []
        contract = ContinuationContract(
            contract_version=1,
            resume_id="v1:failing-suffix",
            source_code=_whole_function.__code__,
            resume_code=_failing_suffix.__code__,
            source_map=_source_map(24, 40),
            live_names=("effects", "value"),
            preserves_aliases=True,
            proof_complete=True,
        )
        uncommitted = CommitBoundary()
        state = ContinuationState.capture(
            contract,
            {"effects": effects, "value": 3},
        )
        uncommitted_exit = SideExit(
            reason="pre_commit_failure",
            resume_id=contract.resume_id,
            source_map=contract.source_map,
            state=state,
            boundary=uncommitted,
            origin=SideExitOrigin.INTERNAL_FAILURE,
        )
        continuation = InterpreterContinuation(contract, _failing_suffix)

        with self.assertRaisesRegex(
            ContinuationError,
            "commit_required_before_suffix",
        ):
            continuation.resume(uncommitted_exit)
        self.assertEqual(effects, [])
        self.assertEqual(
            WholeFunctionInterpreter(_whole_function).execute(
                3,
                True,
                effects,
            ),
            3,
        )
        self.assertEqual(effects, [("prefix", 3), ("suffix", 3)])

        effects.clear()
        committed = CommitBoundary()
        committed.commit()
        committed_exit = SideExit(
            reason="post_commit_failure",
            resume_id=contract.resume_id,
            source_map=contract.source_map,
            state=state,
            boundary=committed,
            origin=SideExitOrigin.INTERNAL_FAILURE,
        )
        with self.assertRaisesRegex(RuntimeError, "suffix failed"):
            continuation.resume(committed_exit)
        self.assertEqual(
            effects,
            [("suffix_enter", 3), ("finally_cleanup", 3)],
        )
        with self.assertRaisesRegex(
            ContinuationError,
            "suffix_already_claimed",
        ):
            continuation.resume(committed_exit)
        self.assertEqual(
            effects,
            [("suffix_enter", 3), ("finally_cleanup", 3)],
        )


if __name__ == "__main__":
    unittest.main()
