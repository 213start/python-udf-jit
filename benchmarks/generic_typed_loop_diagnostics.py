#!/usr/bin/env python3
"""Capture the full generic typed-loop diagnostic chain with CinderX."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import cinderx


if os.environ.get("PYTHONJITUDFDIAGNOSTICS") != "1":
    raise RuntimeError(
        "full CinderX diagnostics require "
        "PYTHONJITUDFDIAGNOSTICS=1 before JIT initialization"
    )

cinderx.init()

from python_udf_jit.compiler.typed_frontend import capture_typed_loop  # noqa: E402
from python_udf_jit.compiler.typed_ir import EXACT_UNICODE  # noqa: E402
from python_udf_jit.diagnostics.bundle import (  # noqa: E402
    read_bundle,
    read_json_artifact,
)
from python_udf_jit.diagnostics.config import (  # noqa: E402
    DiagnosticRuntimeContext,
    resolve_diagnostic_policy,
)
from python_udf_jit.diagnostics.worker_runtime import (  # noqa: E402
    WorkerDiagnosticRuntime,
)
from python_udf_jit.provider.scalar_python.typed_loop import (  # noqa: E402
    CinderXTypedLoopBackend,
    CompileStatus,
    RuntimeFeedback,
    TypedRegionCompileRequest,
    TypedRegionCompiler,
)


def alnum_ratio(text: str, threshold: float = 0.72) -> bool:
    return sum(1 for character in text if character.isalnum()) / len(text) >= threshold


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    policy = resolve_diagnostic_policy(
        {
            "UDFJIT_DIAGNOSTICS": "full",
            "UDFJIT_DIAGNOSTIC_DIR": str(output),
            "UDFJIT_DIAGNOSTIC_FILTER": "candidate:generic-typed-loop",
            "UDFJIT_DIAGNOSTIC_SOURCE": "ranges",
            "UDFJIT_DIAGNOSTIC_PERF": "off",
            "UDFJIT_DIAGNOSTIC_SAMPLE_RATE": "1",
            "UDFJIT_DIAGNOSTIC_MAX_BYTES": str(32 * 1024 * 1024),
        },
        DiagnosticRuntimeContext(
            dedicated_worker=True,
            workspace_root=output.parent / "workspace-sentinel",
            home_root=output.parent / "home-sentinel",
        ),
    )
    runtime = WorkerDiagnosticRuntime(
        policy,
        run_id="generic-typed-loop-diagnostic",
        runtime_mode="auto",
        process_key=f"worker-{os.getpid()}",
        process_id=os.getpid(),
        user_function=alnum_ratio,
    )
    captured = capture_typed_loop(
        alnum_ratio,
        input_types=(EXACT_UNICODE,),
    )
    decision = TypedRegionCompiler(
        CinderXTypedLoopBackend(),
        call_threshold=1,
        negative_ttl_ns=1_000_000_000,
        diagnostic_sink=runtime,
    ).compile(
        TypedRegionCompileRequest(
            captured.module,
            RuntimeFeedback(call_count=1, deopt_count=0),
            captured.analysis.to_documents(),
            captured.runtime_guard,
        )
    )
    if decision.status is not CompileStatus.COMPILED or decision.variant is None:
        raise RuntimeError(
            f"typed compilation failed: {decision.status}:{decision.reason_code}"
        )
    sample = "CinderX 数据 123!"
    if decision.variant(sample) != alnum_ratio(sample):
        raise AssertionError("diagnostic variant changed the UDF result")
    bundle_reference = runtime.finalize()
    if bundle_reference is None:
        raise RuntimeError("diagnostic bundle was not published")
    bundle = read_bundle(bundle_reference.path)
    chain = read_json_artifact(bundle, "typed/chain-status.json")
    provenance = read_json_artifact(
        bundle,
        "typed/operation-provenance.json",
    )
    required_chain = (
        "source_ranges",
        "original_bytecode",
        "typed_semantic",
        "generic_lowering",
        "generated_bytecode",
        "cinderx_hir",
        "cinderx_lir",
        "machine",
    )
    unavailable = {
        stage: chain.get(stage)
        for stage in required_chain
        if chain.get(stage) != "available"
    }
    if unavailable:
        raise RuntimeError(f"diagnostic chain incomplete: {unavailable}")
    linked_operations = sum(
        bool(entry["machine_range_ids"])
        for entry in provenance["entries"]
    )
    if linked_operations == 0:
        raise RuntimeError("diagnostic chain has no operation-to-machine links")
    document = {
        "artifact_count": len(bundle.artifacts),
        "bundle_path": str(bundle_reference.path),
        "bundle_status": bundle.status.value,
        "chain": chain,
        "execution_mode": decision.variant.execution_mode,
        "linked_operations": linked_operations,
        "operation_count": len(provenance["entries"]),
        "semantic_hash": captured.module.semantic_hash,
    }
    print(json.dumps(document, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
