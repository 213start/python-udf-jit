from __future__ import annotations

import json
import os
import secrets
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from python_udf_jit.runtime.guards import (
    DescriptorGuardError,
    DescriptorRejectCode,
    guard_arrow_batch_descriptor,
)
from python_udf_jit.runtime.layout import (
    ARROW_BATCH_ABI_VERSION,
    ArrowBatchDescriptor,
    ProcessIdentity,
)


COLUMNAR_WRAPPER_SERIALIZATION_VERSION = 1
_PROCESS_PID = 0
_PROCESS_GENERATION = ""
_NEXT_BORROW_ID = 0


@dataclass
class ColumnarRuntimeCounters:
    """Value-free, process-local counters; mutation relies on the CPython GIL."""

    batches: int = 0
    rows: int = 0
    arrow_borrows: int = 0
    batch_boundary_hits: int = 0
    materializations: int = 0
    vector_batches: int = 0
    vector_unavailable_batches: int = 0
    native_jit_rows: int = 0
    native_batch_batches: int = 0
    native_batch_rows: int = 0
    native_batch_unavailable_batches: int = 0
    python_scalar_fallback_rows: int = 0
    precommit_failures: int = 0
    published_batches: int = 0
    postcommit_replays: int = 0


_COUNTERS = ColumnarRuntimeCounters()


def columnar_boundary_proven(func: Any, original_callable: Any) -> bool:
    """Name-free proof that a one-string scalar call is safe to batch-lift.

    This is an admission proof for changing the framework execution boundary,
    not a claim that an actual vector executable exists.  The latter is counted
    only by ``vector_batches`` and remains zero for scalar-only exact-Unicode
    plans.
    """

    try:
        from python_udf_jit.compiler.invariant_calls import analyze_value_cache
        from python_udf_jit.compiler.typed_frontend import (
            TypedCaptureError,
            capture_typed_loop,
        )
        from python_udf_jit.compiler.typed_ir import EXACT_UNICODE
        from python_udf_jit.integration.daft_ray.schema import (
            canonicalize_logical_type,
        )
        from python_udf_jit.integration.daft_ray.typed_loop_worker import (
            resolve_typed_loop_callable,
        )

        if canonicalize_logical_type(func.return_dtype) != "string":
            return False
        resolved = resolve_typed_loop_callable(original_callable)
        try:
            capture_typed_loop(
                resolved.function,
                input_types=(EXACT_UNICODE,),
                bound_arguments=resolved.bound_arguments,
                allow_guarded_region=True,
            )
            return True
        except TypedCaptureError:
            # Value-cache analysis supplies a guarded exact-value semantics and
            # exception proof. It is generic structural analysis, never a
            # function-name allowlist.
            return analyze_value_cache(resolved.function) is not None
    except Exception:
        return False


def _process_identity() -> ProcessIdentity:
    global _PROCESS_PID, _PROCESS_GENERATION, _NEXT_BORROW_ID, _COUNTERS
    pid = os.getpid()
    if pid != _PROCESS_PID:
        _PROCESS_PID = pid
        _PROCESS_GENERATION = (
            os.environ.get("UDFJIT_PROCESS_GENERATION", "").strip()
            or secrets.token_hex(16)
        )
        _NEXT_BORROW_ID = 0
        _COUNTERS = ColumnarRuntimeCounters()
    return ProcessIdentity(pid, _PROCESS_GENERATION)


def _next_borrow_id() -> str:
    global _NEXT_BORROW_ID
    process = _process_identity()
    _NEXT_BORROW_ID += 1
    return f"{process.generation}:{_NEXT_BORROW_ID}"


def snapshot_columnar_counters() -> dict[str, int]:
    _process_identity()
    return asdict(_COUNTERS)


def reset_columnar_counters_for_testing() -> None:
    global _COUNTERS
    _process_identity()
    _COUNTERS = ColumnarRuntimeCounters()


def _flush_diagnostic_snapshot() -> None:
    directory = os.environ.get("UDFJIT_COLUMNAR_DIAGNOSTIC_DIR", "").strip()
    if not directory:
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    process = _process_identity()
    target = root / f"columnar-{process.pid}-{process.generation}.json"
    temporary = root / f".{target.name}.{secrets.token_hex(8)}.tmp"
    document = {
        "format_version": 1,
        "process": process.to_document(),
        "counters": snapshot_columnar_counters(),
    }
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _chunks(array: Any) -> tuple[Any, ...]:
    chunks = getattr(array, "chunks", None)
    if chunks is None:
        return (array,)
    normalized = tuple(chunks)
    return normalized or (array,)


def _physical_type(array: Any) -> str:
    physical_type = str(getattr(array, "type", "")).strip().lower()
    if not physical_type or len(physical_type) > 128:
        raise ValueError("arrow_physical_type_invalid")
    return physical_type


def _offset_width_bits(physical_type: str) -> int:
    if physical_type in {"large_string", "large_binary"}:
        return 64
    if physical_type in {"string", "binary"}:
        return 32
    return 0


def _validity_buffer_count(chunks: tuple[Any, ...]) -> int:
    count = 0
    for chunk in chunks:
        buffers = getattr(chunk, "buffers", None)
        if not callable(buffers):
            continue
        values = buffers()
        if values and values[0] is not None:
            count += 1
    return count


class ArrowBorrowScope:
    """Process-local keepalive and guard authority for one Arrow input."""

    def __init__(self, array: Any, *, epoch: str) -> None:
        self._array = array
        self._epoch = epoch
        self._process = _process_identity()
        self._borrow_id = _next_borrow_id()
        self._active = False
        self._guarded = False
        self.descriptor: ArrowBatchDescriptor | None = None

    def __getstate__(self) -> object:
        raise TypeError("Arrow borrow scope is process-local")

    def __enter__(self) -> "ArrowBorrowScope":
        if self._active:
            raise RuntimeError("arrow_borrow_already_active")
        chunks = _chunks(self._array)
        physical_type = _physical_type(self._array)
        length = len(self._array)
        self.descriptor = ArrowBatchDescriptor(
            abi_version=ARROW_BATCH_ABI_VERSION,
            physical_type=physical_type,
            length=length,
            offset=int(getattr(self._array, "offset", 0)),
            chunk_lengths=tuple(len(chunk) for chunk in chunks),
            chunk_offsets=tuple(int(getattr(chunk, "offset", 0)) for chunk in chunks),
            null_count=int(getattr(self._array, "null_count", 0)),
            validity_buffer_count=_validity_buffer_count(chunks),
            offset_width_bits=_offset_width_bits(physical_type),
            epoch=self._epoch,
            borrow_id=self._borrow_id,
            process=self._process,
        )
        self._active = True
        guard_arrow_batch_descriptor(
            self.descriptor,
            expected_epoch=self._epoch,
            expected_borrow_id=self._borrow_id,
            expected_process=self._process,
            expected_physical_type=physical_type,
            expected_length=length,
        )
        self._guarded = True
        return self

    def __exit__(self, *_exc: object) -> None:
        self._guarded = False
        self._active = False
        self._array = None

    def load_pylist(self) -> list[Any]:
        if not self._active or not self._guarded or self._array is None:
            raise DescriptorGuardError(DescriptorRejectCode.BORROW_EXPIRED)
        if _process_identity() != self._process:
            raise DescriptorGuardError(DescriptorRejectCode.PROCESS_MISMATCH)
        loader = getattr(self._array, "to_pylist", None)
        if not callable(loader):
            raise TypeError("arrow_pylist_loader_unavailable")
        return list(loader())


def _physical_types_for_logical(logical_type: str) -> frozenset[str]:
    return {
        "string": frozenset({"string", "large_string"}),
        "bool": frozenset({"bool"}),
        "int32": frozenset({"int32"}),
        "int64": frozenset({"int64"}),
        "float32": frozenset({"float"}),
        "float64": frozenset({"double"}),
    }.get(logical_type, frozenset())


def _arrow_output_type(pa: Any, logical_type: str) -> Any:
    factory = {
        "string": "large_string",
        "bool": "bool_",
        "int32": "int32",
        "int64": "int64",
        "float32": "float32",
        "float64": "float64",
    }.get(logical_type)
    if factory is None:
        raise TypeError("columnar_output_type_unsupported")
    return getattr(pa, factory)()


@dataclass
class ColumnarBatchWrapper:
    """Serializable Daft batch method preserving scalar API semantics."""

    scalar_wrapper: Any
    _native_batch_executor: Any = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _native_batch_unavailable_process: tuple[int, str] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _serialization_version: int = field(
        default=COLUMNAR_WRAPPER_SERIALIZATION_VERSION,
        init=False,
        repr=False,
        compare=False,
    )

    def __getstate__(self) -> dict[str, Any]:
        return {
            "scalar_wrapper": self.scalar_wrapper,
            "_serialization_version": COLUMNAR_WRAPPER_SERIALIZATION_VERSION,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        if (
            type(state) is not dict
            or state.get("_serialization_version")
            != COLUMNAR_WRAPPER_SERIALIZATION_VERSION
            or set(state) != {"scalar_wrapper", "_serialization_version"}
        ):
            raise ValueError("columnar_wrapper_serialization_version_unsupported")
        self.scalar_wrapper = state["scalar_wrapper"]
        self._serialization_version = COLUMNAR_WRAPPER_SERIALIZATION_VERSION
        self._native_batch_executor = None
        self._native_batch_unavailable_process = None

    def _resolve_native_batch_executor(self) -> tuple[Any | None, ProcessIdentity]:
        process = _process_identity()
        executor = self._native_batch_executor
        if executor is not None and not executor.matches_process(process):
            executor = None
            self._native_batch_executor = None
            self._native_batch_unavailable_process = None
        if executor is None:
            process_key = (process.pid, process.generation)
            if self._native_batch_unavailable_process == process_key:
                return None, process
            try:
                from python_udf_jit.integration.daft_ray.native_batch import (
                    build_native_batch_executor,
                )

                executor = build_native_batch_executor(
                    self.scalar_wrapper,
                    process=process,
                )
            except Exception:
                executor = None
            if executor is None:
                self._native_batch_unavailable_process = process_key
                return None, process
            self._native_batch_executor = executor
            self._native_batch_unavailable_process = None
        if not executor.guards_match(process):
            # A live binding/code/dependency drift is a pre-semantics whole-batch
            # fallback. Do not silently compile a new target in the same process.
            return None, process
        return executor, process

    def _invoke_scalar_rows(
        self,
        daft_instance: Any,
        columns: tuple[list[Any], ...],
    ) -> list[Any]:
        outputs: list[Any] = []
        for lane in zip(*columns, strict=True):
            adapter_before = getattr(self.scalar_wrapper, "_typed_loop_adapter", None)
            # A snapshot would acquire the typed adapter's lock twice per row.
            # The GIL-protected integer is sufficient for value-free attribution
            # and adds no diagnostics-only lock to timing runs.
            hits_before = getattr(adapter_before, "_hits", 0)
            try:
                value = self.scalar_wrapper(daft_instance, *lane)
            except Exception:
                _COUNTERS.python_scalar_fallback_rows += 1
                raise
            adapter_after = getattr(self.scalar_wrapper, "_typed_loop_adapter", None)
            hits_after = getattr(adapter_after, "_hits", 0)
            if adapter_after is not None and hits_after > hits_before:
                # This is a native scalar hit inside one batch boundary.  It is
                # intentionally not reported as a vector batch.
                _COUNTERS.native_jit_rows += 1
            else:
                _COUNTERS.python_scalar_fallback_rows += 1
            outputs.append(value)
        return outputs

    def __call__(self, daft_instance: Any, *series: Any) -> Any:
        _process_identity()
        _COUNTERS.batches += 1
        _COUNTERS.batch_boundary_hits += 1
        # Exact-Unicode currently has no semantics-preserving whole-column
        # executable. Keep the transparent boundary, but attribute every batch
        # to the scalar providers instead of inflating the vector hit count.
        _COUNTERS.vector_unavailable_batches += 1
        semantic_entry = False
        counted_rows = False
        native_accounted = False
        try:
            layout = getattr(self.scalar_wrapper, "invocation_layout", None)
            epoch = getattr(layout, "epoch", "")
            if not epoch or not series:
                raise ValueError("columnar_invocation_layout_unavailable")
            arrows = tuple(value.to_arrow() for value in series)
            row_count = len(arrows[0])
            if any(len(value) != row_count for value in arrows[1:]):
                raise ValueError("columnar_input_length_mismatch")
            if len(getattr(layout, "input_types", ())) != len(arrows):
                raise ValueError("columnar_input_arity_mismatch")
            with ExitStack() as stack:
                scopes = tuple(
                    stack.enter_context(ArrowBorrowScope(value, epoch=epoch))
                    for value in arrows
                )
                for scope, logical_type in zip(
                    scopes, layout.input_types, strict=True
                ):
                    descriptor = scope.descriptor
                    assert descriptor is not None
                    if descriptor.physical_type not in _physical_types_for_logical(
                        logical_type
                    ):
                        raise TypeError("columnar_input_physical_type_mismatch")
                native_executor, native_process = (
                    self._resolve_native_batch_executor()
                )
                if native_executor is None:
                    _COUNTERS.native_batch_unavailable_batches += 1
                    native_accounted = True
                _COUNTERS.arrow_borrows += len(scopes)
                columns = tuple(scope.load_pylist() for scope in scopes)
                _COUNTERS.materializations += len(columns)
                _COUNTERS.rows += row_count
                counted_rows = True
                if (
                    native_executor is not None
                    and native_executor.guards_match(native_process)
                ):
                    semantic_entry = True
                    _COUNTERS.native_batch_batches += 1
                    _COUNTERS.native_batch_rows += row_count
                    native_accounted = True
                    outputs = native_executor.invoke(columns)
                else:
                    if not native_accounted:
                        _COUNTERS.native_batch_unavailable_batches += 1
                        native_accounted = True
                    semantic_entry = True
                    outputs = self._invoke_scalar_rows(daft_instance, columns)
            import pyarrow as pa

            result = pa.array(
                outputs,
                type=_arrow_output_type(pa, layout.output_type),
            )
            if len(result) != row_count:
                raise ValueError("columnar_output_length_mismatch")
            _COUNTERS.published_batches += 1
            return result
        except (DescriptorGuardError, TypeError, ValueError):
            if semantic_entry:
                # No replay is legal after the first scalar semantic call.
                raise
            if not native_accounted:
                _COUNTERS.native_batch_unavailable_batches += 1
                native_accounted = True
            _COUNTERS.precommit_failures += 1
            # A framework-compatible Arrow value is still required.  Recover
            # only before semantic entry and evaluate the original callable in
            # row order; publication remains whole-batch.
            arrows = tuple(value.to_arrow() for value in series)
            row_count = len(arrows[0]) if arrows else 0
            if any(len(value) != row_count for value in arrows[1:]):
                raise ValueError("columnar_fallback_input_length_mismatch")
            columns = tuple(list(value.to_pylist()) for value in arrows)
            if columns:
                if not counted_rows:
                    _COUNTERS.rows += row_count
                _COUNTERS.materializations += len(columns)
            semantic_entry = True
            outputs = self._invoke_scalar_rows(daft_instance, columns)
            import pyarrow as pa

            layout = getattr(self.scalar_wrapper, "invocation_layout", None)
            output_type = getattr(layout, "output_type", "")
            result = pa.array(
                outputs,
                type=(
                    _arrow_output_type(pa, output_type)
                    if output_type
                    else None
                ),
            )
            _COUNTERS.published_batches += 1
            return result
        finally:
            _flush_diagnostic_snapshot()
