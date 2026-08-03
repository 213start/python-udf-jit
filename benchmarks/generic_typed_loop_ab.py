#!/usr/bin/env python3
"""Deterministic A/B probe for the generic typed-loop CinderX backend."""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import time

import cinderx


cinderx.init()
import cinderx.jit  # noqa: E402

from python_udf_jit.compiler.typed_frontend import capture_typed_loop  # noqa: E402
from python_udf_jit.compiler.typed_ir import EXACT_UNICODE  # noqa: E402
from python_udf_jit.provider.scalar_python.typed_loop import (  # noqa: E402
    CinderXTypedLoopBackend,
    CompileStatus,
    RuntimeFeedback,
    TypedRegionCompileRequest,
    TypedRegionCompiler,
)


def alnum_ratio(text: str, threshold: float = 0.72) -> bool:
    return sum(1 for character in text if character.isalnum()) / len(text) >= threshold


def _workload(scale: int) -> tuple[str, ...]:
    fragments = (
        "CinderX and Python 314 — 数据处理 12345! ",
        "Fine web text; punctuation... Ελληνικά العربية Ⅷ² ",
        "spaces\tnewlines\nemoji🙂 and ASCII-letters-987 ",
        "中文段落与EnglishWords混合，数字１２３和٣٤٥。 ",
    )
    return tuple(fragment * scale for fragment in fragments)


def _measure(function, values, *, iterations: int, rounds: int):
    samples = []
    checksums = []
    for _ in range(rounds):
        checksum = 0
        started = time.perf_counter_ns()
        for _iteration in range(iterations):
            for value in values:
                checksum += int(function(value))
        samples.append(time.perf_counter_ns() - started)
        checksums.append(checksum)
    if len(set(checksums)) != 1:
        raise AssertionError("benchmark checksum changed across rounds")
    return {
        "checksum": checksums[0],
        "median_ns": int(statistics.median(samples)),
        "samples_ns": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--scale", type=int, default=256)
    args = parser.parse_args()
    if args.iterations <= 0 or args.rounds < 3 or args.scale <= 0:
        parser.error("iterations/scale must be positive and rounds >= 3")

    captured = capture_typed_loop(
        alnum_ratio,
        input_types=(EXACT_UNICODE,),
    )
    compiler = TypedRegionCompiler(
        CinderXTypedLoopBackend(),
        call_threshold=1,
        negative_ttl_ns=1_000_000_000,
    )
    decision = compiler.compile(
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
    variant = decision.variant
    if not cinderx.jit.force_compile(alnum_ratio):
        raise RuntimeError("CinderX rejected the original UDF")

    values = _workload(args.scale)
    for value in values:
        if variant(value) != alnum_ratio(value):
            raise AssertionError("typed variant changed the UDF result")
    for _ in range(50):
        for value in values:
            alnum_ratio(value)
            variant(value)

    gc.collect()
    gc.disable()
    try:
        original = _measure(
            alnum_ratio,
            values,
            iterations=args.iterations,
            rounds=args.rounds,
        )
        typed = _measure(
            variant,
            values,
            iterations=args.iterations,
            rounds=args.rounds,
        )
    finally:
        gc.enable()
    if original["checksum"] != typed["checksum"]:
        raise AssertionError("A/B checksum mismatch")

    document = {
        "backend": {
            "execution_mode": variant.execution_mode,
            "hir_opcode_counts": dict(variant.backend.hir_opcode_counts),
            "jit_compiled": variant.backend.jit_compiled,
        },
        "environment": {
            "cinderx_file": cinderx.__file__,
            "jit_enabled": cinderx.jit.is_enabled(),
            "original_jit_compiled": cinderx.jit.is_jit_compiled(alnum_ratio),
            "typed_jit_compiled": cinderx.jit.is_jit_compiled(
                variant.jit_function
            ),
        },
        "input": {
            "calls_per_round": args.iterations * len(values),
            "characters_per_round": args.iterations
            * sum(len(value) for value in values),
            "rounds": args.rounds,
            "scale": args.scale,
        },
        "original_udf": original,
        "schema_version": 1,
        "semantic_hash": captured.module.semantic_hash,
        "typed_variant": typed,
    }
    document["speedup"] = (
        original["median_ns"] / typed["median_ns"]
    )
    print(json.dumps(document, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
