from __future__ import annotations

import unittest

from python_udf_jit.compiler.identity import capture_identities
from python_udf_jit.runtime.continuation import (
    CONTINUATION_ABI_VERSION,
    CommitBoundary,
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


def _materialized(kind, value):
    return MaterializedLiveValue.materialized(kind, value)


_SOURCE_IDENTITY = capture_identities(_whole_function).source
_JOIN_RESUME_ID = "v1:" + "a" * 64
_FIRST_RESUME_ID = "v1:" + "b" * 64
_SECOND_RESUME_ID = "v1:" + "c" * 64
_FAILING_RESUME_ID = "v1:" + "d" * 64


class ContinuationBoundaryIntegrationTests(unittest.TestCase):
    def test_counterexample_matrix_resumes_suffix_exactly_once(self) -> None:
        for branch, value in ((True, {"value": 1}), (False, None)):
            with self.subTest(branch=branch, value=value):
                effects = []
                effects_alias = effects
                active = RuntimeError("active")
                contract = ContinuationContract(
                    abi_version=CONTINUATION_ABI_VERSION,
                    resume_id=_JOIN_RESUME_ID,
                    source_identity=_SOURCE_IDENTITY,
                    source_code=_whole_function.__code__,
                    resume_code=_suffix.__code__,
                    source_map=_source_map(8, 20),
                    live_values=(
                        LiveValueSpec(
                            "effects",
                            LiveValueKind.PYTHON_OBJECT,
                            borrowed=True,
                        ),
                        LiveValueSpec(
                            "effects_alias",
                            LiveValueKind.PYTHON_OBJECT,
                        ),
                        LiveValueSpec(
                            "merged",
                            LiveValueKind.PYTHON_OBJECT,
                            nullable=True,
                            branch_join=True,
                        ),
                    ),
                    alias_groups=(("effects", "effects_alias"),),
                    preserves_active_exception=True,
                    proof_complete=True,
                )
                boundary = CommitBoundary()
                effects.append(("prefix", value))
                boundary.commit()
                state = ContinuationState.capture(
                    contract,
                    {
                        "effects": _materialized(
                            LiveValueKind.PYTHON_OBJECT,
                            effects,
                        ),
                        "effects_alias": _materialized(
                            LiveValueKind.PYTHON_OBJECT,
                            effects_alias,
                        ),
                        "merged": _materialized(
                            LiveValueKind.PYTHON_OBJECT,
                            value if branch else None,
                        ),
                    },
                    active_exception=active,
                    keepalives={"effects": effects},
                )

                result = InterpreterContinuation(contract, _suffix).resume(
                    SideExit(
                        abi_version=CONTINUATION_ABI_VERSION,
                        reason="branch_join",
                        resume_id=_JOIN_RESUME_ID,
                        source_identity=contract.source_identity,
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
                abi_version=CONTINUATION_ABI_VERSION,
                resume_id=_FIRST_RESUME_ID,
                source_identity=_SOURCE_IDENTITY,
                source_code=_whole_function.__code__,
                resume_code=_first_suffix.__code__,
                source_map=_source_map(10, 30),
                live_values=(
                    LiveValueSpec(
                        "effects",
                        LiveValueKind.PYTHON_OBJECT,
                    ),
                    LiveValueSpec("value", LiveValueKind.INT64),
                ),
                proof_complete=True,
            ),
            ContinuationContract(
                abi_version=CONTINUATION_ABI_VERSION,
                resume_id=_SECOND_RESUME_ID,
                source_identity=_SOURCE_IDENTITY,
                source_code=_whole_function.__code__,
                resume_code=_second_suffix.__code__,
                source_map=_source_map(20, 31),
                live_values=(
                    LiveValueSpec(
                        "effects",
                        LiveValueKind.PYTHON_OBJECT,
                    ),
                    LiveValueSpec("value", LiveValueKind.INT64),
                ),
                proof_complete=True,
            ),
        )
        exits = []
        for contract in contracts:
            boundary = CommitBoundary()
            boundary.commit()
            state = ContinuationState.capture(
                contract,
                {
                    "effects": _materialized(
                        LiveValueKind.PYTHON_OBJECT,
                        effects,
                    ),
                    "value": _materialized(LiveValueKind.INT64, 7),
                },
            )
            exits.append(
                SideExit(
                    abi_version=CONTINUATION_ABI_VERSION,
                    reason="graph_break",
                    resume_id=contract.resume_id,
                    source_identity=contract.source_identity,
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
            abi_version=CONTINUATION_ABI_VERSION,
            resume_id=_FAILING_RESUME_ID,
            source_identity=_SOURCE_IDENTITY,
            source_code=_whole_function.__code__,
            resume_code=_failing_suffix.__code__,
            source_map=_source_map(24, 40),
            live_values=(
                LiveValueSpec(
                    "effects",
                    LiveValueKind.PYTHON_OBJECT,
                ),
                LiveValueSpec("value", LiveValueKind.INT64),
            ),
            proof_complete=True,
        )
        uncommitted = CommitBoundary()
        state = ContinuationState.capture(
            contract,
            {
                "effects": _materialized(
                    LiveValueKind.PYTHON_OBJECT,
                    effects,
                ),
                "value": _materialized(LiveValueKind.INT64, 3),
            },
        )
        continuation = InterpreterContinuation(contract, _failing_suffix)

        with self.assertRaisesRegex(
            ContinuationError,
            "side_exit_requires_commit",
        ):
            SideExit(
                abi_version=CONTINUATION_ABI_VERSION,
                reason="pre_commit_failure",
                resume_id=contract.resume_id,
                source_identity=contract.source_identity,
                source_map=contract.source_map,
                state=state,
                boundary=uncommitted,
                origin=SideExitOrigin.INTERNAL_FAILURE,
            )
        self.assertEqual(effects, [])
        self.assertEqual(
            WholeFunctionInterpreter(_whole_function).execute(
                uncommitted,
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
            abi_version=CONTINUATION_ABI_VERSION,
            reason="post_commit_failure",
            resume_id=contract.resume_id,
            source_identity=contract.source_identity,
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
