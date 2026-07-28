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
        import cinderx.jit
        from cinderjit import _udf_build_continuation_payload

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
        effects = [("compiled_prefix", 7)]
        active_exception = None
        native_source_map = astuple(source_map)

        def compiled(_input, _output):
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

        self.assertTrue(cinderx.jit.force_compile(compiled))
        self.assertTrue(cinderx.jit.is_jit_compiled(compiled))

        registry = CapabilityRegistry(epoch="cinderx-continuation")
        input_handle = registry.register(LocalScalarSlotBackend())
        output_handle = registry.register(LocalScalarSlotBackend())
        boundary = CommitBoundary()
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
            registry.release(output_handle)
            registry.release(input_handle)

        self.assertEqual(result, 8)
        self.assertEqual(
            effects,
            [("compiled_prefix", 7), ("suffix", 7)],
        )


if __name__ == "__main__":
    unittest.main()
