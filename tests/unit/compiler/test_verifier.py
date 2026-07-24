from __future__ import annotations

import dataclasses
import unittest

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.core_ir import CoreNode, lower_capture
from python_udf_jit.compiler.region import form_verified_region
from python_udf_jit.compiler.verifier import VerificationError, VerificationRejectCode, verify_core_module


def affine(x):
    return x * 2.0 + 3.0


class VerifierTest(unittest.TestCase):
    def setUp(self):
        self.module = lower_capture(capture(CaptureRequest(affine)))

    def test_verified_region_has_one_entry_one_exit_and_semantic_hash(self):
        region = form_verified_region(self.module)

        self.assertEqual(region.entry_values, ("%0",))
        self.assertEqual(region.exit_values, (self.module.return_value,))
        self.assertTrue(region.pure)
        self.assertTrue(region.single_entry)
        self.assertTrue(region.single_exit)
        self.assertEqual(region.semantic_hash, self.module.semantic_hash)

    def test_rejects_forward_reference_duplicate_id_and_non_pure_op(self):
        nodes = list(self.module.nodes)
        nodes[2] = dataclasses.replace(nodes[2], operands=("%99", "%1"))
        forward = dataclasses.replace(self.module, nodes=tuple(nodes))

        with self.assertRaises(VerificationError) as raised:
            verify_core_module(forward)
        self.assertEqual(raised.exception.code, VerificationRejectCode.INVALID_OPERAND)

        duplicate_nodes = list(self.module.nodes)
        duplicate_nodes[1] = dataclasses.replace(duplicate_nodes[1], result_id="%0")
        duplicate = dataclasses.replace(self.module, nodes=tuple(duplicate_nodes))
        with self.assertRaises(VerificationError) as raised:
            verify_core_module(duplicate)
        self.assertEqual(raised.exception.code, VerificationRejectCode.DUPLICATE_VALUE)

        impure = dataclasses.replace(
            self.module,
            nodes=self.module.nodes
            + (CoreNode("%99", "call", (self.module.return_value,), None, "float64"),),
        )
        with self.assertRaises(VerificationError) as raised:
            verify_core_module(impure)
        self.assertEqual(raised.exception.code, VerificationRejectCode.UNSUPPORTED_OPERATION)

    def test_semantic_hash_covers_operator_and_constant(self):
        nodes = list(self.module.nodes)
        nodes[1] = dataclasses.replace(nodes[1], literal=2.5)
        changed_constant = dataclasses.replace(self.module, nodes=tuple(nodes))
        nodes = list(self.module.nodes)
        nodes[2] = dataclasses.replace(nodes[2], op="add.f64")
        changed_operator = dataclasses.replace(self.module, nodes=tuple(nodes))

        self.assertNotEqual(changed_constant.recompute_semantic_hash(), self.module.semantic_hash)
        self.assertNotEqual(changed_operator.recompute_semantic_hash(), self.module.semantic_hash)


if __name__ == "__main__":
    unittest.main()
