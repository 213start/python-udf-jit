from __future__ import annotations

from collections.abc import Sequence

from tests.fixtures.scalar_projection import calibrate_measurement


def build_partition_rows() -> tuple[tuple[dict[str, float], ...], ...]:
    """Return deterministic rows intended to be written as separate Parquet files."""

    return (
        ({"measurement": 0.0}, {"measurement": -0.0}, {"measurement": 2.0}),
        ({"measurement": -3.5}, {"measurement": float("inf")}, {"measurement": float("nan")}),
    )


def expected_calibrated_values(
    partitions: Sequence[Sequence[dict[str, float]]],
) -> list[float]:
    return [
        calibrate_measurement(row["measurement"])
        for partition in partitions
        for row in partition
    ]
