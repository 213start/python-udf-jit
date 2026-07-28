from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentCapabilities:
    wheel_major: int
    wrapper_format: int
    carrier_format: int
    artifact_format_major: int
    artifact_format_minor: int
    runtime_abi: int
    adapter_abi: int

    def __post_init__(self) -> None:
        positive_fields = (
            "wheel_major",
            "wrapper_format",
            "carrier_format",
            "artifact_format_major",
            "runtime_abi",
            "adapter_abi",
        )
        for field in positive_fields:
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if (
            type(self.artifact_format_minor) is not int
            or self.artifact_format_minor < 0
        ):
            raise ValueError(
                "artifact_format_minor must be a non-negative integer"
            )

    @classmethod
    def current(cls) -> "ComponentCapabilities":
        return cls(
            wheel_major=1,
            wrapper_format=1,
            carrier_format=1,
            artifact_format_major=1,
            artifact_format_minor=0,
            runtime_abi=1,
            adapter_abi=1,
        )


@dataclass(frozen=True)
class AdmissionDecision:
    accepted: bool
    reason: str


def admit_driver_worker(
    driver: ComponentCapabilities,
    worker: ComponentCapabilities,
) -> AdmissionDecision:
    """Require exact first-formal-version agreement before qualification."""

    checks = (
        ("wheel_major", "wheel_major_mismatch"),
        ("wrapper_format", "wrapper_format_mismatch"),
        ("carrier_format", "carrier_format_mismatch"),
        (
            "artifact_format_major",
            "artifact_format_major_mismatch",
        ),
        (
            "artifact_format_minor",
            "artifact_format_minor_mismatch",
        ),
        ("runtime_abi", "runtime_abi_mismatch"),
        ("adapter_abi", "adapter_abi_mismatch"),
    )
    for field, reason in checks:
        if getattr(driver, field) != getattr(worker, field):
            return AdmissionDecision(False, reason)
    return AdmissionDecision(True, "compatible")
