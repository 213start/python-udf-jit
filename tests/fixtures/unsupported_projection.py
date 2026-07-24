from __future__ import annotations


def unsupported_calibration(value: float) -> float:
    """A real opaque call: Driver capture must reject it without pre-execution."""

    return round(value * 1.5 + 1.25, 6)
