from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


_MODE_RANK = {"off": 0, "observe": 1, "auto": 2}
_ADVANCED_FLAGS = (
    "vector",
    "arrow",
    "rfc_009",
    "rfc_010",
    "rfc_011",
    "rfc_012",
)


class PolicyError(ValueError):
    """A policy snapshot is malformed or attempts to expand authority."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{field}_invalid")
    return value


def _freeze_budgets(values: Mapping[str, int]) -> Mapping[str, int]:
    if not isinstance(values, Mapping) or not values:
        raise PolicyError("budgets_invalid")
    frozen: dict[str, int] = {}
    for name, value in values.items():
        if (
            not isinstance(name, str)
            or not name
            or type(value) is not int
            or value < 0
        ):
            raise PolicyError("budget_invalid")
        frozen[name] = value
    return MappingProxyType(dict(sorted(frozen.items())))


def _freeze_flags(values: Mapping[str, bool]) -> Mapping[str, bool]:
    if not isinstance(values, Mapping):
        raise PolicyError("provider_flags_invalid")
    flags = {
        "scalar_python": True,
        **{name: False for name in _ADVANCED_FLAGS},
    }
    for name, enabled in values.items():
        if not isinstance(name, str) or not name or type(enabled) is not bool:
            raise PolicyError("provider_flag_invalid")
        flags[name] = enabled
    if any(flags.get(name) is True for name in _ADVANCED_FLAGS):
        raise PolicyError("advanced_provider_enabled")
    return MappingProxyType(dict(sorted(flags.items())))


@dataclass(frozen=True)
class PolicySnapshot:
    """Immutable policy switched only at an explicit runtime safe point.

    This is the scalar production baseline.  Vector/Arrow providers and
    RFC-009 through RFC-012 are deliberately closed until a later contract
    opts into them.
    """

    version: str
    mode_ceiling: str
    budgets: Mapping[str, int]
    provider_flags: Mapping[str, bool]
    observe_shadow_compile: bool = False
    rollout_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.version, "policy_version")
        if self.mode_ceiling not in _MODE_RANK:
            raise PolicyError("mode_ceiling_invalid")
        if type(self.observe_shadow_compile) is not bool:
            raise PolicyError("observe_shadow_compile_invalid")
        if type(self.rollout_authorized) is not bool:
            raise PolicyError("rollout_authorized_invalid")
        object.__setattr__(self, "budgets", _freeze_budgets(self.budgets))
        object.__setattr__(
            self,
            "provider_flags",
            _freeze_flags(self.provider_flags),
        )

    @classmethod
    def mainline(
        cls,
        *,
        version: str,
        budgets: Mapping[str, int],
        mode_ceiling: str = "auto",
        observe_shadow_compile: bool = False,
        rollout_authorized: bool = False,
    ) -> PolicySnapshot:
        return cls(
            version=version,
            mode_ceiling=mode_ceiling,
            budgets=budgets,
            provider_flags={
                "scalar_python": True,
                **{name: False for name in _ADVANCED_FLAGS},
            },
            observe_shadow_compile=observe_shadow_compile,
            rollout_authorized=rollout_authorized,
        )

    def tighten(
        self,
        *,
        version: str,
        mode_ceiling: str | None = None,
        budgets: Mapping[str, int] | None = None,
        disable_providers: tuple[str, ...] = (),
        observe_shadow_compile: bool | None = None,
        rollout_authorized: bool | None = None,
    ) -> PolicySnapshot:
        """Create a newer snapshot without expanding any capability or budget."""

        next_mode = self.mode_ceiling if mode_ceiling is None else mode_ceiling
        next_budgets = dict(self.budgets if budgets is None else budgets)
        next_shadow = (
            self.observe_shadow_compile
            if observe_shadow_compile is None
            else observe_shadow_compile
        )
        next_rollout = (
            self.rollout_authorized
            if rollout_authorized is None
            else rollout_authorized
        )
        flags = dict(self.provider_flags)
        for provider in disable_providers:
            if provider not in flags:
                raise PolicyError("provider_unknown")
            flags[provider] = False
        candidate = PolicySnapshot(
            version=version,
            mode_ceiling=next_mode,
            budgets=next_budgets,
            provider_flags=flags,
            observe_shadow_compile=next_shadow,
            rollout_authorized=next_rollout,
        )
        tightened = (
            _MODE_RANK[candidate.mode_ceiling] <= _MODE_RANK[self.mode_ceiling]
            and set(candidate.budgets) == set(self.budgets)
            and all(
                candidate.budgets[name] <= self.budgets[name]
                for name in self.budgets
            )
            and all(
                not candidate.provider_flags.get(name, False)
                or self.provider_flags.get(name, False)
                for name in set(candidate.provider_flags) | set(self.provider_flags)
            )
            and (
                not candidate.observe_shadow_compile
                or self.observe_shadow_compile
            )
            and (not candidate.rollout_authorized or self.rollout_authorized)
        )
        if not tightened:
            raise PolicyError("policy_not_tightened")
        return candidate

    @property
    def document(self) -> dict[str, object]:
        return {
            "budgets": dict(self.budgets),
            "mode_ceiling": self.mode_ceiling,
            "observe_shadow_compile": self.observe_shadow_compile,
            "provider_flags": dict(self.provider_flags),
            "rollout_authorized": self.rollout_authorized,
            "version": self.version,
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()
