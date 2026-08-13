from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from typing import Any, NamedTuple


class CompatibilityTarget(NamedTuple):
    daft_version: str
    func_call_signature: tuple[tuple[str, str], ...]
    where_signature: tuple[tuple[str, str], ...]
    select_signature: tuple[tuple[str, str], ...]
    with_columns_signature: tuple[tuple[str, str], ...]
    func_call_fingerprint: str
    where_fingerprint: str
    select_fingerprint: str
    with_columns_fingerprint: str
    func_private_fields: tuple[str, ...]
    func_option_fields: tuple[str, ...]


class CompatibilityReport(NamedTuple):
    compatible: bool
    reason: str


def _remove_docstrings(node: ast.AST) -> None:
    """Exclude non-executable documentation from the semantic fingerprint.

    CPython micro releases may change ``repr()`` details for Unicode strings.
    ``ast.dump()`` uses that representation, so including a rich docstring can
    make byte-identical source hash differently across supported 3.14.x builds.
    """

    documentable_nodes = (
        ast.Module,
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )
    for candidate in ast.walk(node):
        if not isinstance(candidate, documentable_nodes) or not candidate.body:
            continue
        first = candidate.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            del candidate.body[0]


def callable_fingerprint(callable_object: Any) -> str:
    source = textwrap.dedent(inspect.getsource(callable_object))
    module = ast.parse(source)
    functions = [
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not functions:
        raise ValueError("callable source does not contain a function")
    function = functions[0]
    _remove_docstrings(function)
    normalized = ast.dump(function, include_attributes=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _signature_shape(callable_object: Any) -> tuple[tuple[str, str], ...]:
    return tuple(
        (parameter.name, parameter.kind.name)
        for parameter in inspect.signature(callable_object).parameters.values()
    )


def target_for_objects(
    daft_module: Any, func_class: type[Any], dataframe_class: type[Any]
) -> CompatibilityTarget:
    return CompatibilityTarget(
        daft_version=str(daft_module.__version__),
        func_call_signature=_signature_shape(func_class.__call__),
        where_signature=_signature_shape(dataframe_class.where),
        select_signature=_signature_shape(dataframe_class.select),
        with_columns_signature=_signature_shape(dataframe_class.with_columns),
        func_call_fingerprint=callable_fingerprint(func_class.__call__),
        where_fingerprint=callable_fingerprint(dataframe_class.where),
        select_fingerprint=callable_fingerprint(dataframe_class.select),
        with_columns_fingerprint=callable_fingerprint(dataframe_class.with_columns),
        func_private_fields=("_method",),
        func_option_fields=(),
    )


DAFT_V0_7_2_TARGET = CompatibilityTarget(
    daft_version="0.7.2",
    func_call_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("args", "VAR_POSITIONAL"),
        ("kwargs", "VAR_KEYWORD"),
    ),
    where_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("predicate", "POSITIONAL_OR_KEYWORD"),
    ),
    select_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("columns", "VAR_POSITIONAL"),
        ("projections", "VAR_KEYWORD"),
    ),
    with_columns_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("columns", "POSITIONAL_OR_KEYWORD"),
    ),
    func_call_fingerprint="a00f40a76de03be22da825d350a944f924db94f9e9e76282c67e64ba5e3c2f10",
    where_fingerprint="c5a026d3a4107af36cbdf27114996bf1ec47f0f320eaecee8905994f53e74255",
    select_fingerprint="c536214f29d8f081655de1e287108fba75d4139fd13528484e5e77f925db269e",
    with_columns_fingerprint="ef8fda2e61c1a25f9f3d016bae088494556687e4b9af6c22b51755262a75ae6b",
    func_private_fields=("_cls", "_method"),
    func_option_fields=(
        "batch_size",
        "gpus",
        "is_async",
        "is_batch",
        "is_generator",
        "max_concurrency",
        "max_retries",
        "on_error",
        "return_dtype",
        "unnest",
        "use_process",
    ),
)


def validate_daft_compatibility(
    daft_module: Any,
    func_class: type[Any],
    dataframe_class: type[Any],
    target: CompatibilityTarget = DAFT_V0_7_2_TARGET,
) -> CompatibilityReport:
    try:
        actual = target_for_objects(daft_module, func_class, dataframe_class)
    except Exception as error:
        return CompatibilityReport(False, f"fingerprint_unavailable:{type(error).__name__}")
    checks = (
        ("version", actual.daft_version, target.daft_version),
        ("func_signature", actual.func_call_signature, target.func_call_signature),
        ("where_signature", actual.where_signature, target.where_signature),
        ("select_signature", actual.select_signature, target.select_signature),
        (
            "with_columns_signature",
            actual.with_columns_signature,
            target.with_columns_signature,
        ),
        ("func_fingerprint", actual.func_call_fingerprint, target.func_call_fingerprint),
        ("where_fingerprint", actual.where_fingerprint, target.where_fingerprint),
        ("select_fingerprint", actual.select_fingerprint, target.select_fingerprint),
        (
            "with_columns_fingerprint",
            actual.with_columns_fingerprint,
            target.with_columns_fingerprint,
        ),
    )
    for name, actual_value, expected_value in checks:
        if actual_value != expected_value:
            return CompatibilityReport(False, f"{name}_mismatch")
    return CompatibilityReport(True, "compatible")


def validate_func_instance(
    func: Any,
    target: CompatibilityTarget = DAFT_V0_7_2_TARGET,
) -> CompatibilityReport:
    """Validate the private replacement seam and option-bearing state."""

    try:
        namespace = object.__getattribute__(func, "__dict__")
    except (AttributeError, TypeError) as error:
        return CompatibilityReport(
            False,
            f"func_state_unavailable:{type(error).__name__}",
        )
    if type(namespace) is not dict:
        return CompatibilityReport(False, "func_state_nonstandard")
    private_fields = tuple(
        sorted(
            name
            for name in namespace
            if name.startswith("_") and not name.startswith("__")
        )
    )
    if private_fields == tuple(sorted(target.func_private_fields)):
        missing_options = tuple(
            field for field in target.func_option_fields if field not in namespace
        )
        if missing_options:
            return CompatibilityReport(False, "func_option_fields_mismatch")
        if not callable(namespace.get("_method")):
            return CompatibilityReport(False, "func_method_invalid")
        return CompatibilityReport(True, "compatible")
    # 原生 batch UDF 形态（`@daft.udf` / `@daft.func.batch`）：私有字段为空、
    # 无 `_method` 替换缝，但带 `inner`/`wrapped_inner` 批处理函数与 batch_size。
    # 这类函数已经是 Daft 原生批 UDF（is_batch 路径），UDF JIT 应放行以便对
    # 批内函数（inner）做透明形态识别，而不是在识别前回退为原始执行。
    if private_fields == ():
        inner = namespace.get("inner") or namespace.get("wrapped_inner")
        if callable(inner) and "batch_size" in namespace:
            return CompatibilityReport(True, "compatible_native_batch_udf")
    return CompatibilityReport(False, "func_private_fields_mismatch")
