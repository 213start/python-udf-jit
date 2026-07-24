from __future__ import annotations


def calibrate_measurement(value: float) -> float:
    """Representative row-wise float64 calibration used by the piercing."""

    return value * 1.5 + 1.25
