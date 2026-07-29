from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessBudgetState:
    namespace_count: int
    variant_count: int
    code_bytes: int


class ProcessVariantGovernor:
    """Atomic process-wide admission for all namespace-local variant caches."""

    def __init__(
        self,
        *,
        max_namespaces: int,
        max_variants: int,
        max_code_bytes: int,
    ) -> None:
        if (
            type(max_namespaces) is not int
            or max_namespaces <= 0
            or type(max_variants) is not int
            or max_variants <= 0
            or type(max_code_bytes) is not int
            or max_code_bytes <= 0
        ):
            raise ValueError("invalid_process_variant_budget")
        self._max_namespaces = max_namespaces
        self._max_variants = max_variants
        self._max_code_bytes = max_code_bytes
        self._reservations: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def replace(
        self,
        owner: str,
        *,
        digest: str,
        code_bytes: int,
        removals: tuple[str, ...],
    ) -> bool:
        if (
            not isinstance(owner, str)
            or not owner
            or not isinstance(digest, str)
            or not digest
            or type(code_bytes) is not int
            or code_bytes <= 0
        ):
            raise ValueError("invalid_process_variant_reservation")
        with self._lock:
            current = self._reservations.get(owner, {})
            candidate = dict(current)
            for removal in removals:
                candidate.pop(removal, None)
            candidate[digest] = code_bytes
            namespace_count = len(self._reservations) + int(
                owner not in self._reservations
            )
            variant_count = sum(
                len(values)
                for namespace, values in self._reservations.items()
                if namespace != owner
            ) + len(candidate)
            total_code_bytes = sum(
                sum(values.values())
                for namespace, values in self._reservations.items()
                if namespace != owner
            ) + sum(candidate.values())
            if (
                namespace_count > self._max_namespaces
                or variant_count > self._max_variants
                or total_code_bytes > self._max_code_bytes
            ):
                return False
            self._reservations[owner] = candidate
            return True

    def release(self, owner: str, digests: tuple[str, ...] | None = None) -> None:
        with self._lock:
            if digests is None:
                self._reservations.pop(owner, None)
                return
            current = self._reservations.get(owner)
            if current is None:
                return
            for digest in digests:
                current.pop(digest, None)
            if not current:
                self._reservations.pop(owner, None)

    def state(self) -> ProcessBudgetState:
        with self._lock:
            return ProcessBudgetState(
                namespace_count=len(self._reservations),
                variant_count=sum(
                    len(values) for values in self._reservations.values()
                ),
                code_bytes=sum(
                    sum(values.values())
                    for values in self._reservations.values()
                ),
            )
