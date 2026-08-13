from __future__ import annotations

from dataclasses import dataclass
import inspect
import re
from types import FunctionType
from typing import Any, Callable


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


@dataclass(frozen=True)
class RegexSubBatchKernel:
    """Arrow regex substitution proven equivalent for a specific descriptor."""

    pattern: str
    replacement: str
    ignore_case: bool
    kind: str = "arrow_regex_sub"

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


BatchKernel = RegexSubBatchKernel


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
    leaf = _transparent_leaf(original_callable)
    if leaf is None:
        return None
    descriptor = _regex_sub_descriptor(leaf)
    if descriptor not in _VALIDATED_REGEX_SUBSTITUTIONS:
        return None
    pattern, ignore_case, replacement = descriptor
    return RegexSubBatchKernel(pattern, replacement, ignore_case)
