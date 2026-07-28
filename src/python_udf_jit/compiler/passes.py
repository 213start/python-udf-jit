from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol

from python_udf_jit.compiler.analyses import (
    AnalysisKind,
    AnalysisManager,
)
from python_udf_jit.compiler.core_ir import SemanticCoreModule
from python_udf_jit.compiler.verifier import verify_semantic_module


class PassRejectCode(StrEnum):
    BUDGET_EXCEEDED = "budget_exceeded"
    PASS_ORDER_INVALID = "pass_order_invalid"
    VERIFY_FAILED = "verify_failed"


class PassManagerError(ValueError):
    def __init__(self, code: PassRejectCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code.value if not detail else f"{code.value}:{detail}")


class SemanticPass(Protocol):
    name: str
    required_analyses: frozenset[AnalysisKind]
    preserved_analyses: frozenset[AnalysisKind]

    def run(
        self,
        module: SemanticCoreModule,
        analyses: AnalysisManager,
    ) -> SemanticCoreModule: ...


@dataclass(frozen=True)
class CanonicalizePass:
    name: str = "canonicalize"
    required_analyses: frozenset[AnalysisKind] = frozenset()
    preserved_analyses: frozenset[AnalysisKind] = frozenset(AnalysisKind)

    def run(
        self,
        module: SemanticCoreModule,
        analyses: AnalysisManager,
    ) -> SemanticCoreModule:
        del analyses
        return module


@dataclass(frozen=True)
class SemanticSimplifyPass:
    name: str = "semantic_simplify"
    required_analyses: frozenset[AnalysisKind] = frozenset(
        {
            AnalysisKind.TYPE,
            AnalysisKind.NULL,
            AnalysisKind.EFFECT,
            AnalysisKind.EXCEPTION_ORDER,
        }
    )
    preserved_analyses: frozenset[AnalysisKind] = frozenset(AnalysisKind)

    def run(
        self,
        module: SemanticCoreModule,
        analyses: AnalysisManager,
    ) -> SemanticCoreModule:
        analyses.require_all(self.required_analyses)
        return module


class PassManager:
    def __init__(
        self,
        module: SemanticCoreModule,
        *,
        max_nodes: int,
        max_iterations: int,
        max_time_ms: int,
        verify_each_stage: bool = True,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            max_nodes <= 0
            or max_iterations <= 0
            or max_time_ms <= 0
        ):
            raise ValueError("pass budgets must be positive")
        verify_semantic_module(module, max_nodes=max_nodes)
        self.analyses = AnalysisManager(module)
        self.max_nodes = max_nodes
        self.max_iterations = max_iterations
        self.max_time_ns = max_time_ms * 1_000_000
        self.verify_each_stage = verify_each_stage
        self.clock_ns = clock_ns
        self.executed_passes: list[str] = []

    def run(
        self,
        passes: tuple[SemanticPass, ...],
    ) -> SemanticCoreModule:
        if len(passes) > self.max_iterations:
            raise PassManagerError(
                PassRejectCode.BUDGET_EXCEEDED,
                "iteration_budget",
            )
        started = self.clock_ns()
        for semantic_pass in passes:
            if self.clock_ns() - started > self.max_time_ns:
                raise PassManagerError(
                    PassRejectCode.BUDGET_EXCEEDED,
                    "time_budget",
                )
            self.analyses.require_all(semantic_pass.required_analyses)
            result = semantic_pass.run(
                self.analyses.module,
                self.analyses,
            )
            if (
                len(result.operations) > self.max_nodes
                or len(result.operations) < 1
            ):
                raise PassManagerError(
                    PassRejectCode.BUDGET_EXCEEDED,
                    "node_budget",
                )
            if self.verify_each_stage:
                try:
                    verify_semantic_module(
                        result,
                        max_nodes=self.max_nodes,
                    )
                except ValueError as error:
                    raise PassManagerError(
                        PassRejectCode.VERIFY_FAILED,
                        semantic_pass.name,
                    ) from error
            self.analyses.update_module(
                result,
                preserved=semantic_pass.preserved_analyses,
            )
            self.executed_passes.append(semantic_pass.name)
            if self.clock_ns() - started > self.max_time_ns:
                raise PassManagerError(
                    PassRejectCode.BUDGET_EXCEEDED,
                    "time_budget",
                )
        return self.analyses.module
