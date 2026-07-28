from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.protocol.artifact import (
    artifact_from_documents,
    build_artifact,
)
from python_udf_jit.protocol.codec import (
    ArtifactCodecError,
    ArtifactRejectCode,
    decode_artifact,
    encode_artifact,
)
from python_udf_jit.runtime.descriptors import (
    LayoutRejectCode,
    admit_access_spec,
    scalar_input_spec,
    scalar_output_spec,
)
from python_udf_jit.runtime.layout import SUPPORTED_SCALAR_TYPES
from python_udf_jit.runtime.physicalize import ScalarPhysicalizer


def affine(value):
    return value * 2.0 + 3.0


class RFC005UnitTests(unittest.TestCase):
    def test_rfc005_unit_contract(self):
        captured = capture(CaptureRequest(affine))
        compiled = compile_semantic(captured)
        artifact = build_artifact(
            compiled.core_module,
            compiled.region_graph,
            captured.fallback_identity,
        )
        restored = decode_artifact(encode_artifact(artifact))
        self.assertEqual(
            restored.input_access_specs,
            (scalar_input_spec("float64", nullable=False),),
        )
        self.assertEqual(
            restored.output_access_spec,
            scalar_output_spec("float64", nullable=False),
        )
        unknown_layout_field = artifact.section_documents()
        unknown_layout_field["physical_layout"] = {
            **unknown_layout_field["physical_layout"],
            "unreleased_extension": {},
        }
        with self.assertRaises(ValueError):
            artifact_from_documents(unknown_layout_field)

        mismatched_layout = artifact.section_documents()
        mismatched_layout["physical_layout"] = {
            "inputs": [
                scalar_input_spec(
                    "int64",
                    nullable=False,
                ).to_document()
            ],
            "output": (
                scalar_output_spec(
                    "float64",
                    nullable=False,
                ).to_document()
            ),
        }
        with self.assertRaises(ValueError):
            artifact_from_documents(mismatched_layout)

        vector_layout = artifact.section_documents()
        vector_input = dataclasses.replace(
            artifact.input_access_specs[0],
            layout_kind="arrow_array",
        )
        vector_layout["physical_layout"] = {
            "inputs": [vector_input.to_document()],
            "output": artifact.output_access_spec.to_document(),
        }
        with self.assertRaises(ArtifactCodecError) as raised:
            artifact_from_documents(vector_layout)
        self.assertEqual(
            raised.exception.code,
            ArtifactRejectCode.LAYOUT_UNSUPPORTED,
        )
        self.assertEqual(
            raised.exception.detail,
            LayoutRejectCode.ARROW_LAYOUT_NOT_IMPLEMENTED.value,
        )

        values = {
            "bool": True,
            "int32": -(1 << 31),
            "int64": (1 << 63) - 1,
            "float32": 1.25,
            "float64": -0.0,
        }
        physicalizer = ScalarPhysicalizer(epoch="rfc005")
        for scalar_type in SUPPORTED_SCALAR_TYPES:
            with physicalizer.open_call(
                scalar_input_spec(scalar_type, nullable=False),
                scalar_output_spec(scalar_type, nullable=False),
                values[scalar_type],
            ) as frame:
                value = frame.load_input()
                frame.stage_output(value)
                self.assertEqual(frame.publish_output(), value)
        physicalizer.close()

        vector = dataclasses.replace(
            scalar_input_spec("float64", nullable=False),
            layout_kind="arrow_array",
        )
        decision = admit_access_spec(vector)
        self.assertFalse(decision.accepted)
        self.assertEqual(
            decision.reason,
            LayoutRejectCode.ARROW_LAYOUT_NOT_IMPLEMENTED.value,
        )


if __name__ == "__main__":
    unittest.main()
