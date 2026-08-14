from __future__ import annotations

import weakref
from dataclasses import dataclass
from threading import local
from typing import Any, Callable


@dataclass(frozen=True)
class NativeExpressionCandidate:
    """One exact fallback expression and its guarded native replacement."""

    native_expression: Any
    input_expression: Any
    wrapper_guard: Any
    semantic_guard: Any
    kind: str

    def matches(self) -> bool:
        try:
            return bool(
                self.wrapper_guard.matches()
                and self.semantic_guard.matches()
            )
        except Exception:
            return False

    def input_schema_matches(self, schema: Any) -> bool:
        try:
            from python_udf_jit.integration.daft_ray.invocation_layout import (
                resolve_expression_logical_type,
            )

            return resolve_expression_logical_type(
                self.input_expression,
                schema,
            ) == "string"
        except Exception:
            return False


@dataclass(frozen=True)
class LineageResolution:
    dataframe: Any
    guard_checks: int
    guard_misses: int
    rebuilt: bool


@dataclass(frozen=True)
class ValueResolution:
    value: Any
    guard_checks: int
    guard_misses: int
    rebuilt: bool


@dataclass(frozen=True)
class _DataFrameLineage:
    parent: Any
    operation: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    candidates: dict[int, NativeExpressionCandidate]


class NativeExpressionLineageRegistry:
    """Driver-only alternate-plan lineage for guarded expression lowering.

    The native DataFrame remains the common path.  Raw operation arguments keep
    the original Daft UDF expression alive as the semantic fallback.  At an
    execution boundary, dependency guards choose the existing native plan or
    rebuild only the affected lineage before Daft submits work.
    """

    def __init__(self, expression_class: type[Any], dataframe_class: type[Any]):
        self._expression_class = expression_class
        self._dataframe_class = dataframe_class
        self._candidates: dict[
            int,
            tuple[weakref.ReferenceType[Any] | None, NativeExpressionCandidate],
        ] = {}
        self._lineages: dict[
            int,
            tuple[weakref.ReferenceType[Any] | None, _DataFrameLineage],
        ] = {}
        self._state = local()

    def bypass_enabled(self) -> bool:
        return bool(getattr(self._state, "rebuilding", False))

    def _call_operation(
        self,
        operation: Callable[..., Any],
        parent: Any,
        args: Any,
        kwargs: Any,
    ) -> Any:
        previous = self.bypass_enabled()
        self._state.rebuilding = True
        try:
            return operation(parent, *args, **kwargs)
        finally:
            self._state.rebuilding = previous

    @staticmethod
    def _weakref_or_none(value: Any, callback) -> weakref.ReferenceType[Any] | None:
        try:
            return weakref.ref(value, callback)
        except TypeError:
            return None

    def bind_candidate(
        self,
        fallback_expression: Any,
        candidate: NativeExpressionCandidate,
    ) -> None:
        identity = id(fallback_expression)
        reference = self._weakref_or_none(
            fallback_expression,
            lambda _ref, key=identity: self._candidates.pop(key, None),
        )
        self._candidates[identity] = (reference, candidate)

    def _candidate(self, value: Any) -> NativeExpressionCandidate | None:
        entry = self._candidates.get(id(value))
        if entry is None:
            return None
        reference, candidate = entry
        if reference is not None and reference() is not value:
            self._candidates.pop(id(value), None)
            return None
        return candidate

    def candidates_for(
        self,
        roots: tuple[Any, ...],
        *,
        dataframe: Any | None = None,
        max_nodes: int = 4096,
        max_depth: int = 32,
    ) -> dict[int, NativeExpressionCandidate]:
        pending = [(root, 0) for root in roots]
        found: dict[int, NativeExpressionCandidate] = {}
        visited: set[int] = set()
        while pending:
            value, depth = pending.pop()
            if depth > max_depth:
                raise ValueError("native_expression_depth_limit")
            identity = id(value)
            if identity in visited:
                continue
            visited.add(identity)
            if len(visited) > max_nodes:
                raise ValueError("native_expression_node_limit")
            candidate = self._candidate(value)
            if candidate is not None:
                found[identity] = candidate
                continue
            if isinstance(value, dict):
                pending.extend((item, depth + 1) for item in value.keys())
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, (list, tuple)):
                pending.extend((item, depth + 1) for item in value)
        if not found or dataframe is None:
            return found
        try:
            schema = dataframe.schema()
        except Exception:
            return {}
        return {
            identity: candidate
            for identity, candidate in found.items()
            if candidate.input_schema_matches(schema)
        }

    def native_arguments(
        self,
        value: Any,
        candidates: dict[int, NativeExpressionCandidate],
    ) -> Any:
        candidate = candidates.get(id(value))
        if candidate is not None:
            return candidate.native_expression
        if isinstance(value, dict):
            return {
                self.native_arguments(key, candidates): self.native_arguments(
                    item,
                    candidates,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.native_arguments(item, candidates) for item in value]
        if isinstance(value, tuple):
            return tuple(self.native_arguments(item, candidates) for item in value)
        return value

    def bind_operation(
        self,
        result: Any,
        *,
        parent: Any,
        operation: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        candidates: dict[int, NativeExpressionCandidate] | None = None,
    ) -> bool:
        if not isinstance(result, self._dataframe_class):
            return False
        if self._lineage(parent) is None and not candidates:
            return False
        identity = id(result)
        lineage = _DataFrameLineage(
            parent=parent,
            operation=operation,
            args=tuple(args),
            kwargs=dict(kwargs),
            candidates=dict(candidates or {}),
        )
        reference = self._weakref_or_none(
            result,
            lambda _ref, key=identity: self._lineages.pop(key, None),
        )
        self._lineages[identity] = (reference, lineage)
        return True

    def _lineage(self, dataframe: Any) -> _DataFrameLineage | None:
        entry = self._lineages.get(id(dataframe))
        if entry is None:
            return None
        reference, lineage = entry
        if reference is not None and reference() is not dataframe:
            self._lineages.pop(id(dataframe), None)
            return None
        return lineage

    def has_lineage(self, dataframe: Any) -> bool:
        return self._lineage(dataframe) is not None

    def has_lineage_in(self, value: Any) -> bool:
        if isinstance(value, self._dataframe_class):
            return self.has_lineage(value)
        if isinstance(value, dict):
            return any(
                self.has_lineage_in(item)
                for pair in value.items()
                for item in pair
            )
        if isinstance(value, (list, tuple)):
            return any(self.has_lineage_in(item) for item in value)
        return False

    def resolve_value(
        self,
        value: Any,
        *,
        force_fallback: bool,
    ) -> ValueResolution:
        if isinstance(value, self._dataframe_class):
            resolution = self.resolve(
                value,
                force_fallback=force_fallback,
            )
            return ValueResolution(
                resolution.dataframe,
                resolution.guard_checks,
                resolution.guard_misses,
                resolution.rebuilt,
            )
        if isinstance(value, dict):
            checks = misses = 0
            rebuilt = False
            resolved: dict[Any, Any] = {}
            for key, item in value.items():
                key_resolution = self.resolve_value(
                    key,
                    force_fallback=force_fallback,
                )
                item_resolution = self.resolve_value(
                    item,
                    force_fallback=force_fallback,
                )
                resolved[key_resolution.value] = item_resolution.value
                checks += key_resolution.guard_checks + item_resolution.guard_checks
                misses += key_resolution.guard_misses + item_resolution.guard_misses
                rebuilt = bool(
                    rebuilt
                    or key_resolution.rebuilt
                    or item_resolution.rebuilt
                )
            return ValueResolution(resolved, checks, misses, rebuilt)
        if isinstance(value, (list, tuple)):
            checks = misses = 0
            rebuilt = False
            items: list[Any] = []
            for item in value:
                resolution = self.resolve_value(
                    item,
                    force_fallback=force_fallback,
                )
                items.append(resolution.value)
                checks += resolution.guard_checks
                misses += resolution.guard_misses
                rebuilt = rebuilt or resolution.rebuilt
            return ValueResolution(
                type(value)(items),
                checks,
                misses,
                rebuilt,
            )
        return ValueResolution(value, 0, 0, False)

    @staticmethod
    def _resolved_arguments(
        value: Any,
        candidates: dict[int, NativeExpressionCandidate],
        matches: dict[int, bool],
    ) -> Any:
        candidate = candidates.get(id(value))
        if candidate is not None and matches[id(value)]:
            return candidate.native_expression
        if isinstance(value, dict):
            return {
                NativeExpressionLineageRegistry._resolved_arguments(
                    key,
                    candidates,
                    matches,
                ): NativeExpressionLineageRegistry._resolved_arguments(
                    item,
                    candidates,
                    matches,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                NativeExpressionLineageRegistry._resolved_arguments(
                    item,
                    candidates,
                    matches,
                )
                for item in value
            ]
        if isinstance(value, tuple):
            return tuple(
                NativeExpressionLineageRegistry._resolved_arguments(
                    item,
                    candidates,
                    matches,
                )
                for item in value
            )
        return value

    def resolve(
        self,
        dataframe: Any,
        *,
        force_fallback: bool = False,
    ) -> LineageResolution:
        lineage = self._lineage(dataframe)
        if lineage is None:
            return LineageResolution(dataframe, 0, 0, False)
        parent = self.resolve(
            lineage.parent,
            force_fallback=force_fallback,
        )
        matches: dict[int, bool] = {}
        guard_misses = parent.guard_misses
        for identity, candidate in lineage.candidates.items():
            matched = False if force_fallback else candidate.matches()
            matches[identity] = matched
            if not matched:
                guard_misses += 1
        guard_checks = parent.guard_checks + len(lineage.candidates)
        local_valid = all(matches.values())
        if not force_fallback and not parent.rebuilt and local_valid:
            return LineageResolution(
                dataframe,
                guard_checks,
                guard_misses,
                False,
            )
        args = self._resolved_arguments(
            lineage.args,
            lineage.candidates,
            matches,
        )
        kwargs = self._resolved_arguments(
            lineage.kwargs,
            lineage.candidates,
            matches,
        )
        rebuilt = self._call_operation(
            lineage.operation,
            parent.dataframe,
            args,
            kwargs,
        )
        if not isinstance(rebuilt, self._dataframe_class):
            raise TypeError("native_expression_lineage_not_dataframe")
        return LineageResolution(
            rebuilt,
            guard_checks,
            guard_misses,
            True,
        )
