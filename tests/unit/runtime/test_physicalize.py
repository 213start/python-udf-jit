from __future__ import annotations

import concurrent.futures
import dataclasses
import gc
import os
import unittest
import weakref

from python_udf_jit.runtime.descriptors import (
    scalar_input_spec,
    scalar_output_spec,
)
from python_udf_jit.runtime.guards import (
    DescriptorGuardError,
    DescriptorRejectCode,
    guard_descriptor,
)
from python_udf_jit.runtime.layout import (
    SUPPORTED_SCALAR_TYPES,
    ProcessIdentity,
)
from python_udf_jit.runtime.physicalize import ScalarPhysicalizer


VALUES = {
    "bool": True,
    "int32": -(1 << 31),
    "int64": (1 << 63) - 1,
    "float32": 1.25,
    "float64": -0.0,
}


class ScalarPhysicalizationTest(unittest.TestCase):
    def test_all_types_and_nulls_publish_atomically_with_value_free_metrics(
        self,
    ):
        physicalizer = ScalarPhysicalizer(epoch="epoch-a")
        for scalar_type in SUPPORTED_SCALAR_TYPES:
            for value in (VALUES[scalar_type], None):
                with self.subTest(
                    scalar_type=scalar_type,
                    value=value,
                ):
                    frame = physicalizer.open_call(
                        scalar_input_spec(
                            scalar_type,
                            nullable=True,
                        ),
                        scalar_output_spec(
                            scalar_type,
                            nullable=True,
                        ),
                        value,
                    )
                    with frame:
                        loaded = frame.load_input()
                        frame.stage_output(loaded)
                        published = frame.publish_output()
                    self.assertEqual(published, loaded)
                    self.assertEqual(
                        physicalizer.active_frame_count,
                        0,
                    )
                    metrics = dataclasses.asdict(frame.metrics)
                    self.assertEqual(
                        set(metrics),
                        {
                            "boxed_values",
                            "copied_bytes",
                            "copied_values",
                            "elapsed_ns",
                            "materialized_values",
                            "unboxed_values",
                        },
                    )
                    self.assertFalse(
                        any(
                            isinstance(item, (bool, float))
                            for item in metrics.values()
                        )
                    )
        physicalizer.close()

    def test_failed_output_is_aborted_and_never_exposes_partial_value(self):
        physicalizer = ScalarPhysicalizer(epoch="epoch-a")
        class Keepalive:
            pass

        keepalive = Keepalive()
        reference = weakref.ref(keepalive)
        frame = physicalizer.open_call(
            scalar_input_spec("int32", nullable=False),
            scalar_output_spec("int32", nullable=False),
            7,
            keepalive=keepalive,
        )
        del keepalive
        gc.collect()
        self.assertIsNotNone(reference())
        with self.assertRaises(OverflowError):
            with frame:
                frame.load_input()
                frame.stage_output(1 << 31)

        self.assertEqual(physicalizer.active_frame_count, 0)
        gc.collect()
        self.assertIsNone(reference())
        with self.assertRaises(RuntimeError):
            frame.publish_output()
        physicalizer.close()

    def test_concurrent_frames_are_isolated_and_old_process_is_rejected(self):
        physicalizer = ScalarPhysicalizer(epoch="epoch-a")

        def execute(value):
            with physicalizer.open_call(
                scalar_input_spec("int64", nullable=False),
                scalar_output_spec("int64", nullable=False),
                value,
            ) as frame:
                result = frame.load_input() + 1
                frame.stage_output(result)
                return frame.publish_output()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=8
        ) as executor:
            results = list(executor.map(execute, range(64)))
        self.assertEqual(results, list(range(1, 65)))
        self.assertEqual(physicalizer.active_frame_count, 0)

        with physicalizer.open_call(
            scalar_input_spec("int64", nullable=False),
            scalar_output_spec("int64", nullable=False),
            1,
        ) as frame:
            descriptor = frame.descriptor_set.input_descriptor
            with self.assertRaises(DescriptorGuardError) as raised:
                guard_descriptor(
                    descriptor,
                    expected_epoch=descriptor.epoch,
                    expected_access_id=descriptor.access_id,
                    expected_process=ProcessIdentity(
                        os.getpid(),
                        "restarted-process",
                    ),
                    expected_scalar_type=descriptor.scalar_type,
                    expected_nullable=descriptor.nullable,
                    expected_ownership=descriptor.ownership,
                    expected_access_mode=descriptor.access_mode,
                    expected_descriptor_generation=(
                        descriptor.descriptor_generation
                    ),
                )
            self.assertEqual(
                raised.exception.code,
                DescriptorRejectCode.PROCESS_MISMATCH,
            )
        physicalizer.close()


if __name__ == "__main__":
    unittest.main()
