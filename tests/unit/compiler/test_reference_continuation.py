from __future__ import annotations

import unittest

from python_udf_jit.compiler.reference import (
    reference_execute_semantic,
    reference_resume_live_names,
    reference_resume_semantic,
)
from tests.semantic_cases import (
    python_continuation_cycle_module,
    python_continuation_module,
)


class ReferenceContinuationTest(unittest.TestCase):
    def test_verified_resume_executes_only_the_unfinished_suffix(self):
        module = python_continuation_module()
        region = module.python_regions[0]
        resume_id = f"v1:{region.resume_id}"
        effects = []

        def execute_python(_region, values):
            effects.append(("region", values[0]))
            return values[0] + 1

        self.assertEqual(
            reference_execute_semantic(
                module,
                (4,),
                python_region_executor=execute_python,
            ),
            10,
        )
        self.assertEqual(effects, [("region", 4)])

        effects.clear()
        self.assertEqual(
            reference_resume_live_names(module, resume_id),
            ("%1",),
        )
        self.assertEqual(
            reference_resume_semantic(
                module,
                resume_id,
                {"%1": 5},
                python_region_executor=execute_python,
            ),
            10,
        )
        self.assertEqual(effects, [])

    def test_resume_id_and_live_value_shape_are_fail_closed(self):
        module = python_continuation_module()
        resume_id = f"v1:{module.python_regions[0].resume_id}"

        for candidate, reason in (
            ("v2:" + module.python_regions[0].resume_id, "ABI mismatch"),
            ("v1:" + "A" * 64, "ABI mismatch"),
            ("v1:" + "0" * 64, "resume id mismatch"),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(ValueError, reason):
                    reference_resume_semantic(
                        module,
                        candidate,
                        {"%1": 5},
                    )

        for values in ({}, {"%1": 5, "%unexpected": 1}):
            with self.subTest(values=values):
                with self.assertRaisesRegex(
                    ValueError,
                    "live values mismatch",
                ):
                    reference_resume_semantic(
                        module,
                        resume_id,
                        values,
                    )

    def test_resume_rejects_control_flow_that_can_reenter_prefix(self):
        module = python_continuation_cycle_module()
        resume_id = f"v1:{module.python_regions[0].resume_id}"

        with self.assertRaisesRegex(
            ValueError,
            "cyclic resume unsupported",
        ):
            reference_resume_live_names(module, resume_id)


if __name__ == "__main__":
    unittest.main()
