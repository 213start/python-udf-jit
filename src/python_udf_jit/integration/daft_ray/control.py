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
from python_udf_jit.diagnostics.config import (
    DiagnosticRuntimeContext,
    OFF_DIAGNOSTIC_POLICY,
    resolve_diagnostic_policy,
)
from python_udf_jit.diagnostics.events import DecisionEvent
from python_udf_jit.integration.daft_ray.compatibility import (
    DAFT_V0_7_2_TARGET,
    CompatibilityTarget,
    target_for_objects,
    validate_daft_compatibility,
    validate_func_instance,
)
from python_udf_jit.integration.daft_ray.batch_kernel import build_batch_kernel
from python_udf_jit.integration.daft_ray.objectref_bridge import (
    install_daft_objectref_bridge,
)
from python_udf_jit.integration.daft_ray.registry import CandidateRegistry
from python_udf_jit.integration.daft_ray.wrapper import BatchExecutionWrapper


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


def _batch_positive_int(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 1 <= value <= maximum else default


def _forced_batch_size() -> int | None:
    if os.environ.get("UDFJIT_BATCH_MODE", "off").strip() != "force":
        return None
    return _batch_positive_int("UDFJIT_BATCH_MAX_ROWS", 128, 1_048_576)


def _forced_batch_eligible(func: Any, *, has_explicit_kernel: bool) -> bool:
    """仅显式批 Kernel 才被强制批化；无 Kernel 的函数一律回退原始行式执行。

    此前按 return_dtype（string/list）宽放门禁：无 Kernel 的普通 mapper 也被
    物理化为 batch UDF 并走 scalar envelope（批输入 + 批内逐行重放原函数），
    对无批计算收益的算子引入纯包装开销（实测 10k/50k 全部劣化）。
    改为严格门禁后，无 Kernel 的函数保持原 `@daft.func` 行式路径，不再 envelope。
    """
    return has_explicit_kernel


def _native_batch_inner(func: Any) -> Callable[..., Any] | None:
    """取 Daft 原生 batch UDF（`@daft.udf` / `@daft.func.batch`）的批内函数。

    这类对象私有字段为空、无 `_method` 替换缝，批处理逻辑在 `inner`/
    `wrapped_inner` 中；UDF JIT 对批内函数做透明形态识别，命中 Kernel 后
    用 kernel 包装替换 `inner`，保持 Daft 批输入热路径的同时替换批内计算。
    """
    try:
        namespace = object.__getattribute__(func, "__dict__")
    except (AttributeError, TypeError):
        return None
    if type(namespace) is not dict:
        return None
    inner = namespace.get("inner") or namespace.get("wrapped_inner")
    return inner if callable(inner) else None


def _kernel_native_batch_inner(
    batch_kernel: Any,
    batch_size: int,
) -> Callable[..., Any]:
    """把原生 batch UDF 的批内函数替换为透明识别 Kernel 的批执行包装。

    返回函数签名与 Daft `@daft.udf` 的 `inner` 一致：接收 Series（或 list），
    整批调用 kernel.invoke，不再批内逐元素重放原业务函数。
    """

    def kernel_inner(series: Any) -> list[Any]:
        values = (
            series.to_pylist()
            if hasattr(series, "to_pylist")
            else list(series)
        )
        if not values:
            return []
        return batch_kernel.invoke(values)

    kernel_inner.__name__ = "kernel_batch_inner"
    kernel_inner.__qualname__ = "kernel_batch_inner"
    return kernel_inner


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

            forced_batch_size = _forced_batch_size()
            native_inner = _native_batch_inner(self)
            original_callable = (
                native_inner if native_inner is not None else self._method
            )
            batch_kernel = None
            if (
                forced_batch_size is not None
                and os.environ.get("UDFJIT_BATCH_KERNEL_MODE", "auto").strip() != "off"
            ):
                batch_kernel = build_batch_kernel(original_callable)
            if forced_batch_size is not None and not _forced_batch_eligible(
                self,
                has_explicit_kernel=batch_kernel is not None,
            ):
                return original_func_call(self, *args, **kwargs)
            original_is_batch = bool(getattr(self, "is_batch", False))
            if forced_batch_size is not None and original_is_batch and batch_kernel is None:
                return original_func_call(self, *args, **kwargs)
            if (
                native_inner is not None
                and batch_kernel is not None
                and forced_batch_size is not None
            ):
                # 原生 batch UDF：保持 Daft 批输入热路径，仅把批内逐元素计算
                # 替换为透明识别的批 Kernel（命中 inner 闭包捕获的业务函数）。
                namespace = object.__getattribute__(self, "__dict__")
                namespace["inner"] = _kernel_native_batch_inner(
                    batch_kernel, forced_batch_size
                )
                return original_func_call(self, *args, **kwargs)

            with _CALL_LOCK:
                active = getattr(_CALL_STATE, "active_func_ids", set())
                if id(self) in active:
                    return original_func_call(self, *args, **kwargs)
                active = set(active)
                active.add(id(self))
                _CALL_STATE.active_func_ids = active
                original_is_batch_value = getattr(self, "is_batch", None)
                original_batch_size = getattr(self, "batch_size", None)
                try:
                    record = registry.register(self, original_callable)
                    if forced_batch_size is None:
                        self._method = record.wrapper
                    else:
                        if record.batch_wrapper is None:
                            record.batch_wrapper = BatchExecutionWrapper(
                                candidate_id=record.candidate_id,
                                scalar_wrapper=record.wrapper,
                                batch_kernel=batch_kernel,
                            )
                        self._method = record.batch_wrapper
                        self.is_batch = True
                        self.batch_size = forced_batch_size
                except Exception:
                    if original_is_batch_value is not None:
                        self.is_batch = original_is_batch_value
                    if hasattr(self, "batch_size"):
                        self.batch_size = original_batch_size
                    active.remove(id(self))
                    _CALL_STATE.active_func_ids = active
                    _emit_fail_open("candidate_registration_failed")
                    return original_func_call(self, *args, **kwargs)

                try:
                    expression = original_func_call(self, *args, **kwargs)
                finally:
                    self._method = original_callable
                    if original_is_batch_value is not None:
                        self.is_batch = original_is_batch_value
                    if hasattr(self, "batch_size"):
                        self.batch_size = original_batch_size
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


def install_legacy_udf_hooks(
    legacy_udf_class: type[Any],
    *,
    mode: str,
) -> HookResult:
    """劫持 Daft `@daft.udf`（daft.udf.legacy.UDF）的 `__call__`。

    legacy UDF 是 VOLC_BATCH_MAPPER_UDF=1 时业务仓使用的原生批 UDF 形态：
    私有字段为空、无 `_method` 替换缝，批处理逻辑在 `inner`/`wrapped_inner` 中。
    现有 Func hook（`install_daft_control_hooks`）只劫持 `@daft.func`（udf_v2.Func），
    对 legacy UDF 完全不感知，导致 D 组（batch开/UDF开）的批内计算从未进入识别。

    本 hook：在 `legacy.UDF.__call__` 构造 Expression 前，用 `_native_batch_inner`
    取批内函数做透明形态识别（build_batch_kernel），命中 Kernel 后把
    `inner` 与 `wrapped_inner.inner` 替换为 kernel 包装，保持 Daft 批输入热路径，
    仅替换批内逐元素计算为真正批 Kernel。
    """

    if mode == "off":
        return HookResult(HookStatus.DISABLED, "mode_off")
    if mode not in {"observe", "auto"}:
        return HookResult(HookStatus.DISABLED, "invalid_mode")

    with _INSTALL_LOCK:
        original_func_call = legacy_udf_class.__call__
        if getattr(original_func_call, _HOOK_MARKER, False):
            return HookResult(HookStatus.ALREADY_INSTALLED, "hooks_already_installed")

        @functools.wraps(original_func_call)
        def wrapped_func_call(self, *args: Any, **kwargs: Any) -> Any:
            forced_batch_size = _forced_batch_size()
            if forced_batch_size is None:
                return original_func_call(self, *args, **kwargs)
            if (
                os.environ.get("UDFJIT_BATCH_KERNEL_MODE", "auto").strip() == "off"
            ):
                return original_func_call(self, *args, **kwargs)
            native_inner = _native_batch_inner(self)
            if native_inner is None:
                return original_func_call(self, *args, **kwargs)
            try:
                batch_kernel = build_batch_kernel(native_inner)
            except Exception:
                _emit_fail_open("legacy_udf_kernel_build_failed")
                return original_func_call(self, *args, **kwargs)
            if batch_kernel is None:
                return original_func_call(self, *args, **kwargs)
            try:
                namespace = object.__getattribute__(self, "__dict__")
                kernel_inner = _kernel_native_batch_inner(
                    batch_kernel, forced_batch_size
                )
                # 只替换真正的批函数 `inner`。`wrapped_inner.inner` 是工厂
                # （UninitializedUdf.initialize 调用它返回批函数），必须保持工厂
                # 语义：实测 `wrapped_inner.inner() is namespace["inner"]`，
                # 因此替换 inner 后工厂调用自然返回 kernel 包装。
                namespace["inner"] = kernel_inner
            except Exception:
                _emit_fail_open("legacy_udf_inner_replace_failed")
            return original_func_call(self, *args, **kwargs)

        setattr(wrapped_func_call, _HOOK_MARKER, True)
        setattr(wrapped_func_call, _ORIGINAL_METHOD, original_func_call)
        legacy_udf_class.__call__ = wrapped_func_call
        return HookResult(HookStatus.INSTALLED, "legacy_udf_hooks_installed")


def uninstall_legacy_udf_hooks(legacy_udf_class: type[Any]) -> None:
    """卸载 legacy UDF hook（与 uninstall_daft_control_hooks 对称）。"""

    with _INSTALL_LOCK:
        func_method = legacy_udf_class.__call__
        if getattr(func_method, _HOOK_MARKER, False):
            legacy_udf_class.__call__ = getattr(func_method, _ORIGINAL_METHOD)


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
    if os.environ.get("UDFJIT_DIAGNOSTICS", "off") == "off":
        diagnostic_policy = OFF_DIAGNOSTIC_POLICY
    else:
        diagnostic_policy = resolve_diagnostic_policy(
            os.environ,
            DiagnosticRuntimeContext(
                dedicated_worker=(
                    os.environ.get("PYTHONJITUDFDIAGNOSTICS") == "1"
                ),
                workspace_root=Path.cwd(),
            ),
        )
    try:
        batch_observe = mode == "observe" and _forced_batch_size() is not None
        if not batch_observe:
            flotilla_module = importlib.import_module(
                "daft.runners.flotilla"
            )
            bridge = install_daft_objectref_bridge(
                flotilla_module
            )
            if not bridge.installed:
                _emit_fail_open(bridge.reason)
                return HookResult(
                    HookStatus.ERROR,
                    bridge.reason,
                )
        udf_module = importlib.import_module("daft.udf.udf_v2")
        dataframe_module = importlib.import_module("daft.dataframe.dataframe")
        expressions_module = importlib.import_module("daft.expressions.expressions")
        target = DAFT_V0_7_2_TARGET
        if batch_observe and os.environ.get("UDFJIT_BATCH_RUNTIME_TARGET", "0") == "1":
            runtime_target = target_for_objects(
                daft_module,
                udf_module.Func,
                dataframe_module.DataFrame,
            )
            target = runtime_target._replace(
                func_private_fields=DAFT_V0_7_2_TARGET.func_private_fields,
                func_option_fields=DAFT_V0_7_2_TARGET.func_option_fields,
            )
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = CandidateRegistry(
                manifest_sha256,
                job_namespace=os.environ.get(
                    "UDFJIT_JOB_NAMESPACE",
                    "default-ray-job",
                ),
                diagnostic_policy=diagnostic_policy,
                diagnostic_run_id=os.environ.get(
                    "UDFJIT_RUN_ID",
                    "driver-diagnostic",
                ),
                diagnostic_runtime_mode=mode,
                diagnostic_process_key=f"driver-{os.getpid()}",
            )
        return install_daft_control_hooks(
            daft_module=daft_module,
            func_class=udf_module.Func,
            dataframe_class=dataframe_module.DataFrame,
            expression_class=expressions_module.Expression,
            mode=mode,
            registry=_DEFAULT_REGISTRY,
            target=target,
        )
    except Exception as error:
        _emit_fail_open(f"hook_install_failed:{type(error).__name__}")
        return HookResult(HookStatus.ERROR, "hook_install_failed")


def install_default_daft_hooks_with_legacy(daft_module: Any) -> HookResult:
    """安装 Func hook + legacy `@daft.udf` hook。

    legacy UDF（daft.udf.legacy.UDF）是 VOLC_BATCH_MAPPER_UDF=1 时业务仓的原生批
    UDF 形态；Func hook 对它不感知，需额外劫持其 `__call__` 才能对批内函数做透明
    识别。legacy hook 失败只 fail-open，不影响主 Func hook 安装。
    """

    result = install_default_daft_hooks(daft_module)
    if result.status not in (HookStatus.INSTALLED, HookStatus.ALREADY_INSTALLED):
        return result
    try:
        legacy_udf_module = importlib.import_module("daft.udf.legacy")
        legacy_result = install_legacy_udf_hooks(
            legacy_udf_module.UDF,
            mode=os.environ.get("UDFJIT_MODE", "off"),
        )
        if legacy_result.status == HookStatus.INSTALLED:
            return HookResult(
                HookStatus.INSTALLED,
                f"{result.reason};{legacy_result.reason}",
            )
        return result
    except Exception as error:
        _emit_fail_open(f"legacy_udf_hook_install_failed:{type(error).__name__}")
        return result
