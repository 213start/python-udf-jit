from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from python_udf_jit.governance.policy import PolicySnapshot


class RuntimeMode(StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    AUTO = "auto"


_MODE_RANK = {
    RuntimeMode.OFF: 0,
    RuntimeMode.OBSERVE: 1,
    RuntimeMode.AUTO: 2,
}


@dataclass(frozen=True)
class ModeDecision:
    mode: RuntimeMode
    reason: str
    capture_enabled: bool
    compile_enabled: bool
    optimized_execution: bool


def _decision(
    mode: RuntimeMode,
    reason: str,
    *,
    observe_compile: bool = False,
) -> ModeDecision:
    if mode is RuntimeMode.OFF:
        return ModeDecision(mode, reason, False, False, False)
    if mode is RuntimeMode.OBSERVE:
        return ModeDecision(mode, reason, True, observe_compile, False)
    return ModeDecision(mode, reason, True, True, True)


def resolve_mode(
    *,
    locally_disabled: bool,
    plugin_enabled: bool,
    requested_mode: str | RuntimeMode,
    compatible: bool,
    policy: PolicySnapshot,
    shadow_compile_requested: bool = False,
) -> ModeDecision:
    """Resolve the scalar runtime mode in the externally visible precedence.

    Precedence is intentionally explicit:
    local disable, plugin enable, requested mode, compatibility, then
    immutable policy.  A lower-priority input can only tighten the result.
    """

    if locally_disabled:
        return _decision(RuntimeMode.OFF, "locally_disabled")
    if not plugin_enabled:
        return _decision(RuntimeMode.OFF, "plugin_disabled")
    try:
        requested = RuntimeMode(requested_mode)
    except ValueError:
        return _decision(RuntimeMode.OFF, "invalid_mode")
    if requested is RuntimeMode.OFF:
        return _decision(RuntimeMode.OFF, "mode_off")
    if not compatible:
        return _decision(RuntimeMode.OFF, "incompatible")
    if not policy.provider_flags.get("scalar_python", False):
        return _decision(RuntimeMode.OFF, "policy_provider_disabled")

    ceiling = RuntimeMode(policy.mode_ceiling)
    if _MODE_RANK[ceiling] < _MODE_RANK[requested]:
        requested = ceiling
        reason = "policy_mode_ceiling"
    else:
        reason = f"mode_{requested.value}"

    if requested is RuntimeMode.AUTO and not policy.rollout_authorized:
        requested = RuntimeMode.OBSERVE
        reason = "rollout_not_authorized"
    observe_compile = (
        requested is RuntimeMode.OBSERVE
        and shadow_compile_requested
        and policy.observe_shadow_compile
    )
    return _decision(requested, reason, observe_compile=observe_compile)


def _enabled(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def resolve_environment_mode(
    *,
    policy: PolicySnapshot,
    compatible: bool,
    environment: Mapping[str, str] | None = None,
    shadow_compile_requested: bool = False,
) -> ModeDecision:
    values = os.environ if environment is None else environment
    return resolve_mode(
        locally_disabled=_enabled(
            values.get("UDFJIT_DISABLE"),
            default=False,
        ),
        plugin_enabled=_enabled(
            values.get("UDFJIT_PLUGIN_ENABLE"),
            default=False,
        ),
        requested_mode=values.get("UDFJIT_MODE", "off"),
        compatible=compatible,
        policy=policy,
        shadow_compile_requested=shadow_compile_requested,
    )
