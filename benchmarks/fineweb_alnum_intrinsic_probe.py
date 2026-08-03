#!/usr/bin/env python3
"""A/B the current CinderX shape against an equivalent native scan prototype."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

import cinderx
import cinderx.jit

import _fineweb_alnum_probe
from ops.datajuicer_cpu_text_ops import (
    dj_alphanumeric_ok,
    dj_punctuation_normalize,
    dj_whitespace_normalization,
)


Predicate = Callable[..., bool]
Mapper = Callable[[str], str]
_DEFAULT_MAX_ROWS = 10_000
_DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024


def _load_texts(
    path: Path,
    *,
    max_rows: int,
    max_input_bytes: int,
) -> tuple[list[str], int]:
    texts: list[str] = []
    input_bytes = 0
    with path.open("rb") as source:
        while True:
            if len(texts) >= max_rows:
                if source.read(1):
                    raise ValueError("input_row_limit_exceeded")
                break
            remaining_bytes = max_input_bytes - input_bytes
            line = source.readline(remaining_bytes + 1)
            if not line:
                break
            input_bytes += len(line)
            if input_bytes > max_input_bytes:
                raise ValueError("input_byte_limit_exceeded")
            value = json.loads(line)["text"]
            if not isinstance(value, str):
                raise TypeError("input_text_not_string")
            texts.append(value)
    return texts, input_bytes


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _input_evidence(
    path: Path,
    *,
    rows: int,
    input_bytes: int,
    characters: int,
    max_rows: int,
    max_input_bytes: int,
    min_ratio: float,
) -> dict[str, object]:
    """Describe the input without publishing its machine-local path."""
    return {
        "artifact_id": f"sha256:{_sha256_file(path)}",
        "rows": rows,
        "bytes": input_bytes,
        "characters": characters,
        "max_rows": max_rows,
        "max_input_bytes": max_input_bytes,
        "min_ratio": min_ratio,
    }


def _native_alnum_candidate(text: str, *, min_ratio: float) -> bool:
    return _fineweb_alnum_probe.alnum_ratio_ok(text, min_ratio=min_ratio)


def _native_punctuation_candidate(text: str) -> str:
    return _fineweb_alnum_probe.punctuation_normalize(text)


def _native_whitespace_candidate(text: str) -> str:
    return _fineweb_alnum_probe.whitespace_normalize(text)


def _evaluate(
    predicate: Predicate,
    texts: list[str],
    *,
    min_ratio: float,
) -> tuple[int, str]:
    kept = 0
    digest = hashlib.sha256()
    for text in texts:
        accepted = predicate(text, min_ratio=min_ratio)
        kept += accepted
        digest.update(b"\x01" if accepted else b"\x00")
    return kept, digest.hexdigest()


def _time_predicate(
    predicate: Predicate,
    texts: list[str],
    *,
    min_ratio: float,
    repeats: int,
) -> list[float]:
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        kept = 0
        for text in texts:
            kept += predicate(text, min_ratio=min_ratio)
        elapsed = time.perf_counter_ns() - started
        if kept < 0:
            raise AssertionError("unreachable")
        samples.append(elapsed / 1_000_000_000)
    return samples


def _evaluate_mapper(mapper: Mapper, texts: list[str]) -> str:
    digest = hashlib.sha256()
    for text in texts:
        value = mapper(text)
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
    return digest.hexdigest()


def _time_mapper(
    mapper: Mapper,
    texts: list[str],
    *,
    repeats: int,
) -> list[float]:
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        output_characters = 0
        for text in texts:
            output_characters += len(mapper(text))
        elapsed = time.perf_counter_ns() - started
        if output_characters < 0:
            raise AssertionError("unreachable")
        samples.append(elapsed / 1_000_000_000)
    return samples


def _timing_result(
    *,
    share: float,
    baseline_shape: str,
    baseline_function: Callable[..., object],
    baseline_samples: list[float],
    candidate_shape: str,
    candidate_function: Callable[..., object],
    candidate_samples: list[float],
) -> dict[str, object]:
    baseline_median = statistics.median(baseline_samples)
    candidate_median = statistics.median(candidate_samples)
    speedup = baseline_median / candidate_median
    return {
        "historical_e2e_share": share,
        "baseline": {
            "shape": baseline_shape,
            "jit_compiled": cinderx.jit.is_jit_compiled(baseline_function),
            "samples_seconds": baseline_samples,
            "median_seconds": baseline_median,
        },
        "candidate": {
            "shape": candidate_shape,
            "jit_compiled": cinderx.jit.is_jit_compiled(candidate_function),
            "samples_seconds": candidate_samples,
            "median_seconds": candidate_median,
        },
        "performance": {
            "stage_speedup": speedup,
            "stage_time_reduction_percent": (1.0 - 1.0 / speedup) * 100.0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--min-ratio", type=float, default=0.2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-rows", type=int, default=_DEFAULT_MAX_ROWS)
    parser.add_argument(
        "--max-input-bytes",
        type=int,
        default=_DEFAULT_MAX_INPUT_BYTES,
    )
    args = parser.parse_args()

    if args.repeats <= 0:
        raise ValueError("repeats_must_be_positive")
    if args.max_rows <= 0:
        raise ValueError("max_rows_must_be_positive")
    if args.max_input_bytes <= 0:
        raise ValueError("max_input_bytes_must_be_positive")
    texts, input_bytes = _load_texts(
        args.input,
        max_rows=args.max_rows,
        max_input_bytes=args.max_input_bytes,
    )
    if not texts:
        raise ValueError("input_empty")

    if not cinderx.is_initialized():
        raise RuntimeError("cinderx_not_initialized")
    jit_functions = [
        dj_alphanumeric_ok,
        _native_alnum_candidate,
        dj_punctuation_normalize,
        _native_punctuation_candidate,
        dj_whitespace_normalization,
        _native_whitespace_candidate,
    ]
    for function in jit_functions:
        if not cinderx.jit.force_compile(function):
            raise RuntimeError(f"force_compile_failed:{function.__name__}")

    baseline_result = _evaluate(
        dj_alphanumeric_ok,
        texts,
        min_ratio=args.min_ratio,
    )
    candidate_result = _evaluate(
        _native_alnum_candidate,
        texts,
        min_ratio=args.min_ratio,
    )
    if baseline_result != candidate_result:
        raise AssertionError(
            f"result_mismatch:{baseline_result!r}:{candidate_result!r}"
        )

    edge_cases = [
        "",
        "abc123",
        "_+-",
        "中文１２３",
        "Cafe\N{COMBINING ACUTE ACCENT}",
        "Ⅷ²四",
        "🙂abc",
    ]
    baseline_edges = [
        dj_alphanumeric_ok(value, min_ratio=args.min_ratio)
        for value in edge_cases
    ]
    candidate_edges = [
        _native_alnum_candidate(value, min_ratio=args.min_ratio)
        for value in edge_cases
    ]
    if baseline_edges != candidate_edges:
        raise AssertionError("unicode_edge_result_mismatch")

    mapper_pairs = [
        (
            "punctuation",
            dj_punctuation_normalize,
            _native_punctuation_candidate,
        ),
        (
            "whitespace",
            dj_whitespace_normalization,
            _native_whitespace_candidate,
        ),
    ]
    mapper_hashes: dict[str, str] = {}
    for name, baseline_mapper, candidate_mapper in mapper_pairs:
        baseline_hash = _evaluate_mapper(baseline_mapper, texts)
        candidate_hash = _evaluate_mapper(candidate_mapper, texts)
        if baseline_hash != candidate_hash:
            raise AssertionError(f"{name}_full_input_result_mismatch")
        baseline_edge_values = [baseline_mapper(value) for value in edge_cases]
        candidate_edge_values = [candidate_mapper(value) for value in edge_cases]
        if baseline_edge_values != candidate_edge_values:
            raise AssertionError(f"{name}_unicode_edge_result_mismatch")
        mapper_hashes[name] = baseline_hash

    _time_predicate(
        dj_alphanumeric_ok,
        texts,
        min_ratio=args.min_ratio,
        repeats=1,
    )
    _time_predicate(
        _native_alnum_candidate,
        texts,
        min_ratio=args.min_ratio,
        repeats=1,
    )
    for _, baseline_mapper, candidate_mapper in mapper_pairs:
        _time_mapper(baseline_mapper, texts, repeats=1)
        _time_mapper(candidate_mapper, texts, repeats=1)

    gc.disable()
    try:
        baseline_samples = _time_predicate(
            dj_alphanumeric_ok,
            texts,
            min_ratio=args.min_ratio,
            repeats=args.repeats,
        )
        alnum_candidate_samples = _time_predicate(
            _native_alnum_candidate,
            texts,
            min_ratio=args.min_ratio,
            repeats=args.repeats,
        )
        punctuation_baseline_samples = _time_mapper(
            dj_punctuation_normalize,
            texts,
            repeats=args.repeats,
        )
        punctuation_candidate_samples = _time_mapper(
            _native_punctuation_candidate,
            texts,
            repeats=args.repeats,
        )
        whitespace_baseline_samples = _time_mapper(
            dj_whitespace_normalization,
            texts,
            repeats=args.repeats,
        )
        whitespace_candidate_samples = _time_mapper(
            _native_whitespace_candidate,
            texts,
            repeats=args.repeats,
        )
    finally:
        gc.enable()

    operations = {
        "alphanumeric": _timing_result(
            share=0.1219,
            baseline_shape="CinderX-compiled dj_alphanumeric_ok + genexpr",
            baseline_function=dj_alphanumeric_ok,
            baseline_samples=baseline_samples,
            candidate_shape="CinderX-compiled wrapper + native Unicode scan",
            candidate_function=_native_alnum_candidate,
            candidate_samples=alnum_candidate_samples,
        ),
        "punctuation": _timing_result(
            share=0.0984,
            baseline_shape="CinderX-compiled maketrans + str.translate",
            baseline_function=dj_punctuation_normalize,
            baseline_samples=punctuation_baseline_samples,
            candidate_shape="CinderX-compiled wrapper + specialized native scan",
            candidate_function=_native_punctuation_candidate,
            candidate_samples=punctuation_candidate_samples,
        ),
        "whitespace": _timing_result(
            share=0.0629,
            baseline_shape="CinderX-compiled regex sub + strip",
            baseline_function=dj_whitespace_normalization,
            baseline_samples=whitespace_baseline_samples,
            candidate_shape="CinderX-compiled wrapper + specialized native scan",
            candidate_function=_native_whitespace_candidate,
            candidate_samples=whitespace_candidate_samples,
        ),
    }
    projected_saved_fraction = sum(
        operation["historical_e2e_share"]
        * (1.0 - 1.0 / operation["performance"]["stage_speedup"])
        for operation in operations.values()
    )
    result = {
        "schema_version": "1.0",
        "classification": "backend_intrinsic_trend_probe",
        "input": _input_evidence(
            args.input,
            rows=len(texts),
            input_bytes=input_bytes,
            characters=sum(map(len, texts)),
            max_rows=args.max_rows,
            max_input_bytes=args.max_input_bytes,
            min_ratio=args.min_ratio,
        ),
        "environment": {
            "python": sys.version.split()[0],
            "machine": platform.machine(),
            "cinderx_initialized": cinderx.is_initialized(),
            "diagnostics": "off",
        },
        "correctness": {
            "kept_rows": baseline_result[0],
            "alphanumeric_result_sha256": baseline_result[1],
            "punctuation_result_sha256": mapper_hashes["punctuation"],
            "whitespace_result_sha256": mapper_hashes["whitespace"],
            "all_full_input_equal": True,
            "all_unicode_edges_equal": True,
        },
        "operations": operations,
        "projection_from_historical_stage_shares": {
            "covered_e2e_share": sum(
                operation["historical_e2e_share"]
                for operation in operations.values()
            ),
            "projected_e2e_time_reduction_fraction": projected_saved_fraction,
            "projected_e2e_speedup": 1.0 / (1.0 - projected_saved_fraction),
            "caveat": "Amdahl projection, not a full-pipeline timing result",
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
