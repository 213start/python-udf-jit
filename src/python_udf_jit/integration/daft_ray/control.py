from __future__ import annotations

import functools
import hashlib
import importlib
import os
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from python_udf_jit.diagnostics import events
from python_udf_jit.diagnostics.events import DecisionEvent
from python_udf_jit.integration.daft_ray.compatibility import (
    DAFT_V0_7_2_TARGET,
    CompatibilityTarget,
    validate_daft_compatibility,
    validate_func_instance,
)
from python_udf_jit.integration.daft_ray.registry import CandidateRegistry


_HOOK_MARKER = "__python_udf_jit_u2_hook__"
_ORIGINAL_METHOD = "__python_udf_jit_original__"
_INSTALL_LOCK = threading.RLock()
_CALL_LOCK = threading.RLock()
_CALL_STATE = threading.local()
_DEFAULT_REGISTRY: CandidateRegistry | None = None


class HookStatus(StrEnum):
    INSTALLED = "installed"
    ALREADY_INSTALLED = "already_installed"
    DISABLED = "disabled"
    INCOMPATIBLE = "incompatible"
    ERROR = "error"


@dataclass(frozen=True)
class HookResult:
    status: HookStatus
    reason: str


def _emit_fail_open(reason_code: str) -> None:
    try:
        events.try_emit(
            DecisionEvent(
                stage="adapter",
                decision="fail_open",
                reason_code=reason_code,
            )
        )
    except Exception:
        pass


def _contains_expression(value: Any, expression_class: type[Any]) -> bool:
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, expression_class):
            return True
        identity = id(current)
        if identity in visited:
            continue
        visited.add(identity)
        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return False


def install_daft_control_hooks(
    *,
    daft_module: Any,
    func_class: type[Any],
    dataframe_class: type[Any],
    expression_class: type[Any],
    mode: str,
    registry: CandidateRegistry,
    target: CompatibilityTarget = DAFT_V0_7_2_TARGET,
) -> HookResult:
    if mode == "off":
        return HookResult(HookStatus.DISABLED, "mode_off")
    if mode not in {"observe", "auto"}:
        return HookResult(HookStatus.DISABLED, "invalid_mode")

    with _INSTALL_LOCK:
        func_method = func_class.__call__
        dataframe_methods = {
            name: getattr(dataframe_class, name)
            for name in ("where", "select", "with_columns")
        }
        func_installed = bool(getattr(func_method, _HOOK_MARKER, False))
        operation_installed = {
            name: bool(getattr(method, _HOOK_MARKER, False))
            for name, method in dataframe_methods.items()
        }
        if func_installed and all(operation_installed.values()):
            return HookResult(HookStatus.ALREADY_INSTALLED, "hooks_already_installed")
        if func_installed or any(operation_installed.values()):
            _emit_fail_open("partial_hook_state")
            return HookResult(HookStatus.ERROR, "partial_hook_state")

        compatibility = validate_daft_compatibility(
            daft_module, func_class, dataframe_class, target
        )
        if not compatibility.compatible:
            _emit_fail_open(compatibility.reason)
            return HookResult(HookStatus.INCOMPATIBLE, compatibility.reason)

        original_func_call = func_method

        @functools.wraps(original_func_call)
        def wrapped_func_call(self, *args, **kwargs):
            expression_values = (*args, *kwargs.values())
            if not any(
                _contains_expression(value, expression_class)
                for value in expression_values
            ):
                return original_func_call(self, *args, **kwargs)

            instance_report = validate_func_instance(self, target)
            if not instance_report.compatible:
                _emit_fail_open(instance_report.reason)
                return original_func_call(self, *args, **kwargs)

            with _CALL_LOCK:
                active = getattr(_CALL_STATE, "active_func_ids", set())
                if id(self) in active:
                    return original_func_call(self, *args, **kwargs)
                active = set(active)
                active.add(id(self))
                _CALL_STATE.active_func_ids = active
                original_callable = self._method
                try:
                    record = registry.register(self, original_callable)
                    self._method = record.wrapper
                except Exception:
                    active.remove(id(self))
                    _CALL_STATE.active_func_ids = active
                    _emit_fail_open("candidate_registration_failed")
                    return original_func_call(self, *args, **kwargs)

                try:
                    expression = original_func_call(self, *args, **kwargs)
                finally:
                    self._method = original_callable
                    active.remove(id(self))
                    _CALL_STATE.active_func_ids = active

            try:
                registry.bind_expression(expression, record)
            except Exception:
                _emit_fail_open("expression_binding_failed")
            return expression

        setattr(wrapped_func_call, _HOOK_MARKER, True)
        setattr(wrapped_func_call, _ORIGINAL_METHOD, original_func_call)
        func_class.__call__ = wrapped_func_call
        for operation_name, original_operation in dataframe_methods.items():
            def make_operation_wrapper(name, original):
                @functools.wraps(original)
                def wrapped(self, *args, **kwargs):
                    try:
                        registry.finalize_operation(
                            self,
                            name,
                            args,
                            kwargs,
                        )
                    except Exception:
                        _emit_fail_open("operation_finalization_failed")
                    return original(self, *args, **kwargs)

                setattr(wrapped, _HOOK_MARKER, True)
                setattr(wrapped, _ORIGINAL_METHOD, original)
                return wrapped

            setattr(
                dataframe_class,
                operation_name,
                make_operation_wrapper(operation_name, original_operation),
            )
        return HookResult(HookStatus.INSTALLED, "compatible_hooks_installed")


def uninstall_daft_control_hooks(
    func_class: type[Any], dataframe_class: type[Any]
) -> None:
    """Test/diagnostic rollback; production normally leaves hooks for process life."""

    with _INSTALL_LOCK:
        func_method = func_class.__call__
        if getattr(func_method, _HOOK_MARKER, False):
            func_class.__call__ = getattr(func_method, _ORIGINAL_METHOD)
        for operation_name in ("where", "select", "with_columns"):
            dataframe_method = getattr(dataframe_class, operation_name)
            if getattr(dataframe_method, _HOOK_MARKER, False):
                setattr(
                    dataframe_class,
                    operation_name,
                    getattr(dataframe_method, _ORIGINAL_METHOD),
                )


def _manifest_sha256_from_environment() -> str | None:
    explicit = os.environ.get("UDFJIT_MANIFEST_SHA256", "")
    if explicit:
        return explicit
    path = os.environ.get("UDFJIT_MANIFEST_PATH", "")
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def install_default_daft_hooks(daft_module: Any) -> HookResult:
    """Resolve Daft-private objects only after the top-level Daft import completed."""

    global _DEFAULT_REGISTRY
    mode = os.environ.get("UDFJIT_MODE", "off")
    if mode == "off":
        return HookResult(HookStatus.DISABLED, "mode_off")
    manifest_sha256 = _manifest_sha256_from_environment()
    if manifest_sha256 is None:
        _emit_fail_open("manifest_missing")
        return HookResult(HookStatus.ERROR, "manifest_missing")
    try:
        udf_module = importlib.import_module("daft.udf.udf_v2")
        dataframe_module = importlib.import_module("daft.dataframe.dataframe")
        expressions_module = importlib.import_module("daft.expressions.expressions")
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = CandidateRegistry(
                manifest_sha256,
                job_namespace=os.environ.get(
                    "UDFJIT_JOB_NAMESPACE",
                    "default-ray-job",
                ),
            )
        return install_daft_control_hooks(
            daft_module=daft_module,
            func_class=udf_module.Func,
            dataframe_class=dataframe_module.DataFrame,
            expression_class=expressions_module.Expression,
            mode=mode,
            registry=_DEFAULT_REGISTRY,
        )
    except Exception as error:
        _emit_fail_open(f"hook_install_failed:{type(error).__name__}")
        return HookResult(HookStatus.ERROR, "hook_install_failed")
