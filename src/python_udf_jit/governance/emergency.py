from __future__ import annotations

import threading
from dataclasses import dataclass


class EmergencyTransitionError(ValueError):
    """An emergency update rolled back a generation or relaxed protection."""


@dataclass(frozen=True)
class EmergencySnapshot:
    generation: int
    disabled: bool
    revoke_credentials_through: int

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise EmergencyTransitionError("generation_invalid")
        if type(self.disabled) is not bool:
            raise EmergencyTransitionError("disabled_invalid")
        if (
            type(self.revoke_credentials_through) is not int
            or self.revoke_credentials_through < 0
        ):
            raise EmergencyTransitionError("revocation_generation_invalid")


@dataclass(frozen=True)
class EmergencyChannelLease:
    minimum_generation: int
    expires_at_ns: int

    def __post_init__(self) -> None:
        if (
            type(self.minimum_generation) is not int
            or self.minimum_generation < 0
        ):
            raise EmergencyTransitionError("minimum_generation_invalid")
        if type(self.expires_at_ns) is not int or self.expires_at_ns < 0:
            raise EmergencyTransitionError("channel_expiry_invalid")


@dataclass(frozen=True)
class SafePointDecision:
    snapshot: EmergencySnapshot
    optimized_execution_allowed: bool
    reason: str


class EmergencyControl:
    """Process-local emergency state observed only at explicit safe points."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot = EmergencySnapshot(0, False, 0)

    def apply(self, candidate: EmergencySnapshot) -> EmergencySnapshot:
        if not isinstance(candidate, EmergencySnapshot):
            raise EmergencyTransitionError("snapshot_invalid")
        with self._lock:
            current = self._snapshot
            if candidate == current:
                return current
            if candidate.generation <= current.generation:
                raise EmergencyTransitionError("generation_not_monotonic")
            if current.disabled and not candidate.disabled:
                raise EmergencyTransitionError("disable_cannot_be_relaxed")
            if (
                candidate.revoke_credentials_through
                < current.revoke_credentials_through
            ):
                raise EmergencyTransitionError("revocation_cannot_be_relaxed")
            self._snapshot = candidate
            return candidate

    def safe_point(self) -> EmergencySnapshot:
        with self._lock:
            return self._snapshot

    def safe_point_decision(
        self,
        lease: EmergencyChannelLease,
        *,
        now_ns: int,
    ) -> SafePointDecision:
        if not isinstance(lease, EmergencyChannelLease):
            raise EmergencyTransitionError("channel_lease_invalid")
        if type(now_ns) is not int or now_ns < 0:
            raise EmergencyTransitionError("safe_point_time_invalid")
        snapshot = self.safe_point()
        if now_ns > lease.expires_at_ns:
            return SafePointDecision(
                snapshot,
                False,
                "emergency_channel_expired",
            )
        if snapshot.generation < lease.minimum_generation:
            return SafePointDecision(
                snapshot,
                False,
                "emergency_generation_stale",
            )
        if snapshot.disabled:
            return SafePointDecision(snapshot, False, "emergency_disabled")
        return SafePointDecision(snapshot, True, "emergency_current")
