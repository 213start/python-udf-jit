from __future__ import annotations

from dataclasses import astuple
import importlib.util
import unittest

from python_udf_jit.compiler.identity import capture_identities
from python_udf_jit.provider.scalar_python.capability import CapabilityRegistry
from python_udf_jit.provider.scalar_python.executor import ScalarExecutor
from python_udf_jit.runtime.continuation import (
    CONTINUATION_ABI_VERSION,
    CommitBoundary,
    ContinuationContract,
    InterpreterContinuation,
    LiveValueKind,
    LiveValueSpec,
    ResumeSourceMap,
)
from python_udf_jit.runtime.layout import LocalScalarSlotBackend


_REGION_GUARD_TARGET = len


def _source_udf(value):
    return value


def _resume_suffix(state):
    state.values["effects"].append(("suffix", state.values["value"]))
    return state.values["value"] + 1


@unittest.skipUnless(
    importlib.util.find_spec("cinderx") is not None,
    "CinderX candidate runtime is required",
)
class CinderXUdfDeoptIntegrationTest(unittest.TestCase):
    def test_native_payload_crosses_exactly_once_into_python_suffix(self):
        global _REGION_GUARD_TARGET

        import cinderx.jit
        from cinderjit import (
            _udf_build_continuation_payload,
            _udf_register_continuation_code,
        )

        identity = capture_identities(_source_udf).source
        resume_id = "v1:" + "b" * 64
        source_map = ResumeSourceMap(
            schema_version=CONTINUATION_ABI_VERSION,
            bytecode_offset=18,
            line=identity.first_line,
            column=4,
            end_line=identity.first_line,
            end_column=18,
        )
        contract = ContinuationContract(
            abi_version=CONTINUATION_ABI_VERSION,
            resume_id=resume_id,
            source_identity=identity,
            source_code=_source_udf.__code__,
            resume_code=_resume_suffix.__code__,
            source_map=source_map,
            live_values=(
                LiveValueSpec(
                    "effects",
                    LiveValueKind.PYTHON_OBJECT,
                    borrowed=True,
                ),
                LiveValueSpec("value", LiveValueKind.INT64),
            ),
            proof_complete=True,
        )
        effects = []
        active_exception = None
        native_source_map = astuple(source_map)

        def compiled(_input, _output):
            effects.append(("compiled_prefix", 7))
            _REGION_GUARD_TARGET(())
            return _udf_build_continuation_payload(
                CONTINUATION_ABI_VERSION,
                "cinderx_deopt",
                resume_id,
                identity.namespace_sha256,
                identity.code_sha256,
                identity.first_line,
                native_source_map,
                (effects, 7),
                ("python_object", "int64"),
                (False, False),
                (True, True),
                active_exception,
                True,
            )

        self.assertTrue(_udf_register_continuation_code(compiled))
        self.assertTrue(cinderx.jit.force_compile(compiled))
        self.assertTrue(cinderx.jit.is_jit_compiled(compiled))
        cinderx.jit.clear_runtime_stats()

        registry = CapabilityRegistry(epoch="cinderx-continuation")
        input_handle = registry.register(LocalScalarSlotBackend())
        output_handle = registry.register(LocalScalarSlotBackend())
        boundary = CommitBoundary()
        original_guard_target = _REGION_GUARD_TARGET
        _REGION_GUARD_TARGET = lambda _: 0
        try:
            result = ScalarExecutor(registry).execute_guarded(
                compiled,
                input_handle,
                output_handle,
                1.0,
                boundary=boundary,
                continuation=InterpreterContinuation(
                    contract,
                    _resume_suffix,
                ),
            )
        finally:
            _REGION_GUARD_TARGET = original_guard_target
            registry.release(output_handle)
            registry.release(input_handle)

        runtime_stats = cinderx.jit.get_and_clear_runtime_stats()
        guard_deopts = [
            event
            for event in runtime_stats["deopt"]
            if event["normal"]["func_qualname"].endswith(".compiled")
            and event["normal"]["reason"] == "GuardFailure"
        ]
        self.assertEqual(result, 8)
        self.assertGreaterEqual(
            sum(event["int"]["count"] for event in guard_deopts),
            1,
            runtime_stats,
        )
        self.assertEqual(effects.count(("compiled_prefix", 7)), 1)
        self.assertEqual(effects.count(("suffix", 7)), 1)
        self.assertEqual(
            effects,
            [("compiled_prefix", 7), ("suffix", 7)],
        )


if __name__ == "__main__":
    unittest.main()
