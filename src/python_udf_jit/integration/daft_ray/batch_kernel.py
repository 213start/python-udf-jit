from __future__ import annotations

from dataclasses import dataclass
import inspect
import re
from types import FunctionType
from typing import Any, Callable, Protocol, runtime_checkable


_BATCH_KERNEL_ATTRIBUTE = "__python_udf_jit_batch_kernel__"
_VALIDATED_REGEX_SUBSTITUTIONS = frozenset(
    {
        (r"https?://\S+|www\.\S+", True, ""),
        (r"[\w.+-]+@[\w.-]+\.\w+", False, ""),
        (
            r"(?i)(copyright\s*\(?c\)?|©|\(c\)|all rights reserved)"
            r"[^\n.]*\.?",
            True,
            "",
        ),
    }
)


@runtime_checkable
class BatchKernel(Protocol):
    kind: str
    fallback_on_error: bool

    def invoke(self, values: list[Any]) -> list[Any]: ...


@dataclass(frozen=True)
class CallableBatchKernel:
    """Explicit, serializable batch implementation supplied by an integration."""

    kind: str
    callable: Callable[[list[Any]], list[Any]]
    fallback_on_error: bool = False

    def invoke(self, values: list[Any]) -> list[Any]:
        output = self.callable(values)
        return output if type(output) is list else list(output)


@dataclass(frozen=True)
class RegexSubBatchKernel:
    """Arrow regex substitution proven equivalent for a specific descriptor."""

    pattern: str
    replacement: str
    ignore_case: bool
    kind: str = "arrow_regex_sub"
    fallback_on_error: bool = True

    def invoke(self, values: list[Any]) -> list[Any]:
        import pyarrow as pa
        import pyarrow.compute as pc

        pattern = self.pattern
        if self.ignore_case and not pattern.startswith("(?i)"):
            pattern = "(?i)" + pattern
        output = pc.replace_substring_regex(
            pa.array(values),
            pattern,
            self.replacement,
        )
        return output.to_pylist()


def register_batch_kernel(
    function: Callable[..., Any],
    batch_callable: Callable[[list[Any]], list[Any]],
    *,
    kind: str,
) -> Callable[..., Any]:
    """Attach an explicit batch contract without wrapping the scalar callable."""

    if not callable(function) or not callable(batch_callable):
        raise TypeError("batch kernel registration requires callables")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("batch kernel kind must not be empty")
    setattr(
        function,
        _BATCH_KERNEL_ATTRIBUTE,
        CallableBatchKernel(kind.strip(), batch_callable),
    )
    return function


def _callable_graph(function: Callable[..., Any]) -> tuple[Callable[..., Any], ...]:
    pending = [function]
    found: list[Callable[..., Any]] = []
    visited: set[int] = set()
    while pending and len(visited) < 64:
        current = pending.pop()
        if not callable(current) or id(current) in visited:
            continue
        visited.add(id(current))
        found.append(current)
        if inspect.isfunction(current):
            pending.extend(
                cell.cell_contents
                for cell in (current.__closure__ or ())
                if callable(cell.cell_contents)
            )
            pending.extend(
                value for value in (current.__defaults__ or ()) if callable(value)
            )
            pending.extend(
                value
                for value in (current.__kwdefaults__ or {}).values()
                if callable(value)
            )
    return tuple(found)


def _explicit_batch_kernel(
    function: Callable[..., Any],
) -> BatchKernel | None:
    matches = []
    for candidate in _callable_graph(function):
        kernel = getattr(candidate, _BATCH_KERNEL_ATTRIBUTE, None)
        if isinstance(kernel, BatchKernel):
            matches.append(kernel)
    if not matches:
        return None
    kinds = {kernel.kind for kernel in matches}
    if len(kinds) != 1:
        return None
    return matches[0]


def _transparent_leaf(function: Callable[..., Any]) -> FunctionType | None:
    current = function
    visited: set[int] = set()
    for _ in range(8):
        if not inspect.isfunction(current) or id(current) in visited:
            return None
        visited.add(id(current))
        closure_functions = [
            cell.cell_contents
            for cell in (current.__closure__ or ())
            if inspect.isfunction(cell.cell_contents)
        ]
        if not closure_functions:
            return current
        if len(closure_functions) != 1:
            return None
        current = closure_functions[0]
    return None


def _regex_sub_descriptor(
    function: FunctionType,
) -> tuple[str, bool, str] | None:
    code = function.__code__
    if code.co_argcount != 1 or code.co_kwonlyargcount != 0:
        return None
    if len(code.co_names) != 2 or code.co_names[1] != "sub":
        return None
    pattern = function.__globals__.get(code.co_names[0])
    if not isinstance(pattern, re.Pattern):
        return None
    replacements = [
        value
        for value in code.co_consts
        if type(value) is str and value != function.__doc__
    ]
    if len(replacements) != 1:
        return None
    return (
        pattern.pattern,
        bool(pattern.flags & re.IGNORECASE),
        replacements[0],
    )


def build_batch_kernel(
    original_callable: Callable[..., Any],
) -> BatchKernel | None:
    explicit = _explicit_batch_kernel(original_callable)
    if explicit is not None:
        return explicit
    leaf = _transparent_leaf(original_callable)
    if leaf is None:
        return None
    descriptor = _regex_sub_descriptor(leaf)
    if descriptor not in _VALIDATED_REGEX_SUBSTITUTIONS:
        return None
    pattern, ignore_case, replacement = descriptor
    return RegexSubBatchKernel(pattern, replacement, ignore_case)
