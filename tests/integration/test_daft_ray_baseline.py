from __future__ import annotations

import math
import os
import socket
import unittest

from tests.fixtures.scalar_partitioned_projection import (
    build_partition_rows,
    expected_calibrated_values,
)
from tests.fixtures.scalar_projection import calibrate_measurement


def _live_calibrate_measurement(value: float) -> float:
    """Fail inside the UDF if Ray schedules its data plane on the Driver."""

    hostname = socket.gethostname()
    if hostname not in {"ray-worker-1", "ray-worker-2"}:
        raise RuntimeError(f"data plane escaped to {hostname}")
    if type(value) is not float:
        raise TypeError(f"expected a Python float scalar, got {type(value)!r}")
    return value * 2.0 + 3.0


class DaftRayBaselineContractTests(unittest.TestCase):
    def test_scalar_fixture_is_non_empty_float_to_float(self) -> None:
        result = calibrate_measurement(2.0)

        self.assertIsInstance(result, float)
        self.assertEqual(4.25, result)

    def test_partition_fixture_has_multiple_files_worth_of_rows(self) -> None:
        partitions = build_partition_rows()

        self.assertGreaterEqual(len(partitions), 2)
        self.assertTrue(all(partition for partition in partitions))
        flat = [row["measurement"] for partition in partitions for row in partition]
        expected = [calibrate_measurement(value) for value in flat]
        actual = expected_calibrated_values(partitions)
        self.assertEqual(expected[:-1], actual[:-1])
        self.assertTrue(math.isnan(actual[-1]))
        self.assertTrue(any(math.isnan(value) for value in flat))


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 three-node Ray candidate cluster",
)
class DaftRayLiveBaselineTests(unittest.TestCase):
    def test_partitioned_float_projection_runs_only_on_worker_nodes(self) -> None:
        import daft

        daft.set_runner_ray(address="auto", noop_if_initialized=True)
        projection = daft.func(_live_calibrate_measurement)
        values = [0.0, 1.25, -2.5, 9.0]
        result = (
            daft.from_pydict({"x": values})
            .repartition(2)
            .with_column("y", projection(daft.col("x")))
            .select("x", "y")
            .to_pydict()
        )

        actual = sorted(zip(result["x"], result["y"], strict=True))
        expected = sorted((value, value * 2.0 + 3.0) for value in values)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
