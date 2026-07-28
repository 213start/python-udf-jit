from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from python_udf_jit.compiler.core_ir import (
    Determinism,
    EffectKind,
    SemanticCoreModule,
)
from python_udf_jit.compiler.verifier import verify_semantic_module


class AnalysisKind(StrEnum):
    TYPE = "type"
    NULL = "null"
    EFFECT = "effect"
    EXCEPTION_ORDER = "exception_order"
    LIVENESS = "liveness"
    ALIAS = "alias"
    DETERMINISM = "determinism"


class StaleAnalysisError(ValueError):
    def __init__(self, kind: AnalysisKind) -> None:
        self.kind = kind
        super().__init__(f"stale_preserved_analysis:{kind.value}")


@dataclass(frozen=True)
class AnalysisRecord:
    kind: AnalysisKind
    module_hash: str
    entries: tuple[tuple[str, tuple[str, ...]], ...]

    def to_document(self) -> dict[str, object]:
        return {
            "entries": [
                [key, list(values)] for key, values in self.entries
            ],
            "kind": self.kind.value,
            "module_hash": self.module_hash,
        }

    @classmethod
    def from_document(cls, document: object) -> "AnalysisRecord":
        expected = {"entries", "kind", "module_hash"}
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or not isinstance(document["entries"], list)
            or len(document["entries"]) > 65_536
            or not isinstance(document["kind"], str)
            or not isinstance(document["module_hash"], str)
        ):
            raise ValueError("invalid analysis record fields")
        entries: list[tuple[str, tuple[str, ...]]] = []
        for entry in document["entries"]:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not isinstance(entry[1], list)
                or len(entry[1]) > 65_536
                or any(
                    not isinstance(value, str)
                    for value in entry[1]
                )
            ):
                raise ValueError("invalid analysis record entry")
            entries.append((entry[0], tuple(entry[1])))
        return cls(
            AnalysisKind(document["kind"]),
            document["module_hash"],
            tuple(entries),
        )


@dataclass(frozen=True)
class AnalysisSummary:
    module_hash: str
    records: tuple[AnalysisRecord, ...]
    summary_hash: str

    def semantic_document(self) -> dict[str, object]:
        return {
            "module_hash": self.module_hash,
            "records": [record.to_document() for record in self.records],
        }

    def recompute_summary_hash(self) -> str:
        encoded = json.dumps(
            self.semantic_document(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(
            b"python-udf-jit-analysis-summary-v1\0" + encoded
        ).hexdigest()

    def to_document(self) -> dict[str, object]:
        return {
            **self.semantic_document(),
            "summary_hash": self.summary_hash,
        }

    @classmethod
    def from_document(
        cls,
        document: object,
        *,
        module_hash: str,
    ) -> "AnalysisSummary":
        expected = {"module_hash", "records", "summary_hash"}
        if (
            not isinstance(document, dict)
            or set(document) != expected
            or document["module_hash"] != module_hash
            or not isinstance(document["records"], list)
            or len(document["records"]) != len(AnalysisKind)
            or not isinstance(document["summary_hash"], str)
        ):
            raise ValueError("invalid analysis summary fields")
        result = cls(
            document["module_hash"],
            tuple(
                AnalysisRecord.from_document(record)
                for record in document["records"]
            ),
            document["summary_hash"],
        )
        if (
            tuple(
                record.kind
                for record in result.records
            )
            != tuple(
                sorted(AnalysisKind, key=lambda value: value.value)
            )
            or any(
                record.module_hash != module_hash
                for record in result.records
            )
            or result.recompute_summary_hash() != result.summary_hash
        ):
            raise ValueError("analysis summary verification failed")
        return result


def _type_analysis(module: SemanticCoreModule) -> AnalysisRecord:
    return AnalysisRecord(
        AnalysisKind.TYPE,
        module.semantic_hash,
        tuple(
            (
                operation.result_id,
                (operation.result_type.value,),
            )
            for operation in module.operations
            if operation.result_id is not None
        ),
    )


def _null_analysis(module: SemanticCoreModule) -> AnalysisRecord:
    return AnalysisRecord(
        AnalysisKind.NULL,
        module.semantic_hash,
        tuple(
            (
                operation.result_id,
                (operation.nullability.value,),
            )
            for operation in module.operations
            if operation.result_id is not None
        ),
    )


def _effect_analysis(module: SemanticCoreModule) -> AnalysisRecord:
    return AnalysisRecord(
        AnalysisKind.EFFECT,
        module.semantic_hash,
        tuple(
            (
                operation.operation_id,
                (
                    operation.effect.value,
                    (
                        "barrier"
                        if operation.effect is not EffectKind.PURE
                        or operation.op == "python.region"
                        else "transparent"
                    ),
                ),
            )
            for operation in module.operations
        ),
    )


def _exception_analysis(module: SemanticCoreModule) -> AnalysisRecord:
    return AnalysisRecord(
        AnalysisKind.EXCEPTION_ORDER,
        module.semantic_hash,
        tuple(
            (
                operation.operation_id,
                (
                    str(operation.exception_order),
                    operation.block_id,
                ),
            )
            for operation in module.operations
            if operation.may_raise
        ),
    )


def _liveness_analysis(module: SemanticCoreModule) -> AnalysisRecord:
    live: set[str] = set()
    entries: list[tuple[str, tuple[str, ...]]] = []
    for operation in reversed(module.operations):
        if operation.result_id is not None:
            live.discard(operation.result_id)
        live.update(operation.operands)
        entries.append((operation.operation_id, tuple(sorted(live))))
    entries.reverse()
    return AnalysisRecord(
        AnalysisKind.LIVENESS,
        module.semantic_hash,
        tuple(entries),
    )


def _alias_analysis(module: SemanticCoreModule) -> AnalysisRecord:
    def classification(op: str) -> str:
        if op == "argument":
            return "borrowed"
        if op in {"tuple.make", "list.make"}:
            return "fresh_identity"
        if op == "constant":
            return "immutable"
        if op in {"field.load", "modeled.call", "python.region"}:
            return "unknown"
        return "value"

    return AnalysisRecord(
        AnalysisKind.ALIAS,
        module.semantic_hash,
        tuple(
            (
                operation.result_id,
                (classification(operation.op),),
            )
            for operation in module.operations
            if operation.result_id is not None
        ),
    )


def _determinism_analysis(module: SemanticCoreModule) -> AnalysisRecord:
    return AnalysisRecord(
        AnalysisKind.DETERMINISM,
        module.semantic_hash,
        tuple(
            (
                operation.operation_id,
                (
                    operation.determinism.value,
                    (
                        "stable"
                        if operation.determinism
                        is Determinism.DETERMINISTIC
                        else "barrier"
                    ),
                ),
            )
            for operation in module.operations
        ),
    )


_COMPUTE = {
    AnalysisKind.TYPE: _type_analysis,
    AnalysisKind.NULL: _null_analysis,
    AnalysisKind.EFFECT: _effect_analysis,
    AnalysisKind.EXCEPTION_ORDER: _exception_analysis,
    AnalysisKind.LIVENESS: _liveness_analysis,
    AnalysisKind.ALIAS: _alias_analysis,
    AnalysisKind.DETERMINISM: _determinism_analysis,
}


class AnalysisManager:
    def __init__(self, module: SemanticCoreModule) -> None:
        verify_semantic_module(module)
        self._module = module
        self._cache: dict[AnalysisKind, AnalysisRecord] = {}
        self._compute_counts = {kind: 0 for kind in AnalysisKind}

    @property
    def module(self) -> SemanticCoreModule:
        return self._module

    def require(self, kind: AnalysisKind) -> AnalysisRecord:
        cached = self._cache.get(kind)
        if (
            cached is not None
            and cached.module_hash == self._module.semantic_hash
        ):
            return cached
        result = _COMPUTE[kind](self._module)
        self._cache[kind] = result
        self._compute_counts[kind] += 1
        return result

    def require_all(
        self,
        kinds: Iterable[AnalysisKind],
    ) -> tuple[AnalysisRecord, ...]:
        return tuple(self.require(kind) for kind in kinds)

    def compute_count(self, kind: AnalysisKind) -> int:
        return self._compute_counts[kind]

    def update_module(
        self,
        module: SemanticCoreModule,
        *,
        preserved: frozenset[AnalysisKind],
    ) -> None:
        verify_semantic_module(module)
        if module.semantic_hash == self._module.semantic_hash:
            self._module = module
            return
        for kind in preserved:
            old = self.require(kind)
            new = _COMPUTE[kind](module)
            if old.entries != new.entries:
                raise StaleAnalysisError(kind)
        self._module = module
        self._cache = {
            kind: AnalysisRecord(kind, module.semantic_hash, record.entries)
            for kind, record in self._cache.items()
            if kind in preserved
        }

    def invalidate(
        self,
        preserved: frozenset[AnalysisKind] = frozenset(),
    ) -> None:
        self._cache = {
            kind: record
            for kind, record in self._cache.items()
            if kind in preserved
        }

    def summary(self) -> AnalysisSummary:
        records = tuple(
            self.require(kind)
            for kind in sorted(AnalysisKind, key=lambda value: value.value)
        )
        provisional = AnalysisSummary(
            self._module.semantic_hash,
            records,
            "",
        )
        return AnalysisSummary(
            provisional.module_hash,
            provisional.records,
            provisional.recompute_summary_hash(),
        )
