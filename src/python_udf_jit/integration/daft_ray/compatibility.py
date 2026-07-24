from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from typing import Any, NamedTuple


class CompatibilityTarget(NamedTuple):
    daft_version: str
    func_call_signature: tuple[tuple[str, str], ...]
    with_columns_signature: tuple[tuple[str, str], ...]
    func_call_fingerprint: str
    with_columns_fingerprint: str


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
        with_columns_signature=_signature_shape(dataframe_class.with_columns),
        func_call_fingerprint=callable_fingerprint(func_class.__call__),
        with_columns_fingerprint=callable_fingerprint(dataframe_class.with_columns),
    )


DAFT_V0_7_2_TARGET = CompatibilityTarget(
    daft_version="0.7.2",
    func_call_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("args", "VAR_POSITIONAL"),
        ("kwargs", "VAR_KEYWORD"),
    ),
    with_columns_signature=(
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("columns", "POSITIONAL_OR_KEYWORD"),
    ),
    func_call_fingerprint="a00f40a76de03be22da825d350a944f924db94f9e9e76282c67e64ba5e3c2f10",
    with_columns_fingerprint="ef8fda2e61c1a25f9f3d016bae088494556687e4b9af6c22b51755262a75ae6b",
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
        (
            "with_columns_signature",
            actual.with_columns_signature,
            target.with_columns_signature,
        ),
        ("func_fingerprint", actual.func_call_fingerprint, target.func_call_fingerprint),
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
