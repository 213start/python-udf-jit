#!/usr/bin/env python3
"""Simple diagnostics-off A/B for generic CinderX typed HIR patterns."""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import statistics
import time
from collections.abc import Callable

import cinderx


if os.environ.get("UDFJIT_DIAGNOSTICS", "off") != "off" or os.environ.get(
    "PYTHONJITUDFDIAGNOSTICS", "0"
) not in {"", "0"}:
    raise RuntimeError("performance A/B requires diagnostics to be disabled")

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


def punctuation_normalize(text: str) -> str:
    table = str.maketrans(
        {
            "\u201c": '"',
            "\u201d": '"',
            "\u2018": "'",
            "\u2019": "'",
            "\u2013": "-",
            "\u2014": "-",
        }
    )
    return text.translate(table)


_SPACE_RUN = re.compile(r"\s+")


def whitespace_normalize(text: str) -> str:
    return _SPACE_RUN.sub(" ", text).strip()


def _workload(scale: int) -> tuple[str, ...]:
    fragments = (
        "  CinderX and Python 314 — 数据处理 12345!  ",
        "Fine web text; punctuation… ‘Ελληνικά’ العربية Ⅷ²\t",
        "spaces\tnewlines\nemoji🙂 and ASCII-letters-987 ",
        "\u2003中文段落与EnglishWords混合，数字１２３和٣٤٥。\u2029",
    )
    return tuple(fragment * scale for fragment in fragments)


def _compile(function: Callable[[str], object]):
    captured = capture_typed_loop(function, input_types=(EXACT_UNICODE,))
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
    return captured, decision.variant


def _checksum(value: object) -> int:
    if type(value) is bool:
        return int(value)
    if type(value) is str:
        return len(value) + (ord(value[0]) if value else 0)
    raise TypeError("unsupported benchmark result")


def _measure(function, values, *, iterations: int, rounds: int):
    samples: list[int] = []
    checksums: list[int] = []
    for _ in range(rounds):
        checksum = 0
        started = time.perf_counter_ns()
        for _iteration in range(iterations):
            for value in values:
                checksum += _checksum(function(value))
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
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--scale", type=int, default=256)
    arguments = parser.parse_args()
    if arguments.iterations <= 0 or arguments.rounds < 3 or arguments.scale <= 0:
        parser.error("iterations/scale must be positive and rounds >= 3")

    values = _workload(arguments.scale)
    cases = (
        ("alphanumeric", alnum_ratio),
        ("punctuation", punctuation_normalize),
        ("whitespace", whitespace_normalize),
    )
    compiled = []
    for name, function in cases:
        captured, variant = _compile(function)
        if not cinderx.jit.force_compile(function):
            raise RuntimeError(f"CinderX rejected baseline {name}")
        for value in values:
            if variant(value) != function(value):
                raise AssertionError(f"{name} changed result")
        compiled.append((name, function, captured, variant))

    for _ in range(20):
        for _name, function, _captured, variant in compiled:
            for value in values:
                function(value)
                variant(value)

    results: dict[str, object] = {}
    gc.collect()
    gc.disable()
    try:
        for name, function, captured, variant in compiled:
            baseline = _measure(
                function,
                values,
                iterations=arguments.iterations,
                rounds=arguments.rounds,
            )
            candidate = _measure(
                variant,
                values,
                iterations=arguments.iterations,
                rounds=arguments.rounds,
            )
            if baseline["checksum"] != candidate["checksum"]:
                raise AssertionError(f"{name} A/B checksum mismatch")
            results[name] = {
                "baseline": baseline,
                "candidate": candidate,
                "execution_mode": variant.execution_mode,
                "hir_opcode_counts": dict(variant.backend.hir_opcode_counts),
                "semantic_hash": captured.module.semantic_hash,
                "speedup": baseline["median_ns"] / candidate["median_ns"],
            }
    finally:
        gc.enable()

    document = {
        "diagnostics": {
            "PYTHONJITUDFDIAGNOSTICS": os.environ.get(
                "PYTHONJITUDFDIAGNOSTICS", "unset"
            ),
            "UDFJIT_DIAGNOSTICS": os.environ.get("UDFJIT_DIAGNOSTICS", "off"),
        },
        "input": {
            "calls_per_round": arguments.iterations * len(values),
            "characters_per_round": arguments.iterations
            * sum(len(value) for value in values),
            "rounds": arguments.rounds,
            "scale": arguments.scale,
        },
        "patterns": results,
        "schema_version": 1,
    }
    print(json.dumps(document, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
