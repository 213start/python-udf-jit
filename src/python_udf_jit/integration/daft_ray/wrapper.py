from __future__ import annotations

from dataclasses import dataclass, field
import atexit
import json
import os
from typing import Any, Callable

from python_udf_jit.diagnostics import events
from python_udf_jit.diagnostics.events import DecisionEvent
from python_udf_jit.integration.daft_ray.carrier import (
    DEFAULT_INLINE_ARTIFACT_THRESHOLD,
    ProductionCarrierState,
    ScalarCallView,
)
from python_udf_jit.integration.daft_ray.batch_kernel import BatchKernel


WRAPPER_SERIALIZATION_VERSION = 1
BATCH_WRAPPER_SERIALIZATION_VERSION = 2


def _series_values(value: Any) -> list[Any] | None:
    converter = getattr(value, "to_pylist", None)
    if not callable(converter):
        return None
    converted = converter()
    return converted if type(converted) is list else list(converted)


@dataclass
class BatchExecutionWrapper:
    """Serializable Daft batch carrier for a scalar JIT candidate.

    Daft invokes this object once per physical Series batch.  The wrapper keeps
    batching outside the scalar callable, so the existing scalar Worker adapter
    can be reused while the plan-level row-wise call boundary is removed.
    """

    candidate_id: str
    scalar_wrapper: "FallbackOnlyWrapper"
    batch_kernel: BatchKernel | None = None
    _trace_calls: int = field(default=0, init=False, repr=False, compare=False)
    _trace_rows: int = field(default=0, init=False, repr=False, compare=False)
    _trace_min: int | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _trace_max: int | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _trace_registered: bool = field(
        default=False, init=False, repr=False, compare=False
    )
    _serialization_version: int = field(
        default=BATCH_WRAPPER_SERIALIZATION_VERSION,
        init=False,
        repr=False,
        compare=False,
    )

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state["_trace_calls"] = 0
        state["_trace_rows"] = 0
        state["_trace_min"] = None
        state["_trace_max"] = None
        state["_trace_registered"] = False
        state["_serialization_version"] = BATCH_WRAPPER_SERIALIZATION_VERSION
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        version = (
            state.get("_serialization_version")
            if type(state) is dict
            else None
        )
        if version != BATCH_WRAPPER_SERIALIZATION_VERSION:
            raise ValueError("batch_wrapper_serialization_version_unsupported")
        self.__dict__.update(state)
        self._trace_registered = False

    def _record_trace(self, row_count: int) -> None:
        if os.environ.get("UDFJIT_BATCH_TRACE", "0") != "1":
            return
        self._trace_calls += 1
        self._trace_rows += row_count
        self._trace_min = (
            row_count if self._trace_min is None else min(self._trace_min, row_count)
        )
        self._trace_max = (
            row_count if self._trace_max is None else max(self._trace_max, row_count)
        )
        trace_every_call = os.environ.get("UDFJIT_BATCH_TRACE_ALL", "0") == "1"
        if trace_every_call or not self._trace_registered:
            print(
                ("UDFJIT_BATCH_CALL " if trace_every_call else "UDFJIT_BATCH_SAMPLE ")
                + json.dumps(
                    {
                        "candidate_id": self.candidate_id,
                        "kernel": (
                            self.batch_kernel.kind
                            if self.batch_kernel is not None
                            else "scalar_envelope"
                        ),
                        "pid": os.getpid(),
                        "rows": row_count,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        if not self._trace_registered:
            atexit.register(self._emit_trace_summary)
            self._trace_registered = True

    def _emit_trace_summary(self) -> None:
        if self._trace_calls <= 0:
            return
        print(
            "UDFJIT_BATCH_SUMMARY "
            + json.dumps(
                {
                    "calls": self._trace_calls,
                    "candidate_id": self.candidate_id,
                    "kernel": (
                        self.batch_kernel.kind
                        if self.batch_kernel is not None
                        else "scalar_envelope"
                    ),
                    "pid": os.getpid(),
                    "rows_max": self._trace_max,
                    "rows_min": self._trace_min,
                    "rows_total": self._trace_rows,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        self._trace_calls = 0

    def _invoke_row(
        self,
        receiver: Any,
        positional: tuple[Any, ...],
        keywords: dict[str, Any],
    ) -> Any:
        if any(value is None for value in (*positional, *keywords.values())):
            return None
        return self.scalar_wrapper(receiver, *positional, **keywords)

    def __call__(self, receiver: Any, *args: Any, **kwargs: Any) -> list[Any]:
        positional_columns = [_series_values(value) for value in args]
        keyword_columns = {
            name: _series_values(value) for name, value in kwargs.items()
        }
        lengths = {
            len(values)
            for values in (*positional_columns, *keyword_columns.values())
            if values is not None
        }
        if not lengths:
            raise ValueError("batch_series_input_missing")
        if len(lengths) != 1:
            raise ValueError("batch_input_length_mismatch")
        row_count = next(iter(lengths))
        self._record_trace(row_count)

        if (
            self.batch_kernel is not None
            and len(args) == 1
            and not kwargs
            and positional_columns[0] is not None
        ):
            try:
                output = self.batch_kernel.invoke(positional_columns[0])
                if len(output) == row_count:
                    return output
            except Exception:
                # The validated kernel is an optional pre-semantics fast path.
                # Any runtime/backend rejection falls back to the exact scalar
                # envelope before a user-visible result is committed.
                pass

        rows = []
        for index in range(row_count):
            positional = tuple(
                column[index] if column is not None else args[position]
                for position, column in enumerate(positional_columns)
            )
            keywords = {
                name: column[index] if column is not None else kwargs[name]
                for name, column in keyword_columns.items()
            }
            rows.append((receiver, positional, keywords))

        return [self._invoke_row(*row) for row in rows]


@dataclass
class FallbackOnlyWrapper:
    """Serializable Daft carrier with a lazy, process-local U5 Worker runtime."""

    candidate_id: str
    original_callable: Callable[..., Any]
    carrier: ProductionCarrierState
    logical_schema: str | None = None
    usage_context: str | None = None
    _worker_adapter: Any = field(default=None, init=False, repr=False, compare=False)
    _typed_loop_adapter: Any = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _typed_loop_terminal_bypass: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )
    _serialization_version: int = field(
        default=WRAPPER_SERIALIZATION_VERSION,
        init=False,
        repr=False,
        compare=False,
    )

    def finalize(
        self,
        logical_schema: str,
        usage_context: str,
        artifact: bytes | None = None,
    ) -> bool:
        """Attach operation context once, before Daft serializes the finalized plan."""

        if self.logical_schema is not None or self.usage_context is not None:
            return False
        self.logical_schema = logical_schema
        self.usage_context = usage_context
        if artifact is not None:
            publisher = None
            threshold = DEFAULT_INLINE_ARTIFACT_THRESHOLD
            if len(artifact) > threshold:
                try:
                    import ray

                    if ray.is_initialized():
                        from python_udf_jit.integration.daft_ray.objectref_bridge import (
                            register_driver_artifact_reference,
                        )

                        def publish(payload: bytes) -> object:
                            return register_driver_artifact_reference(
                                payload,
                                ray.put(payload),
                            )

                        publisher = publish
                except Exception:
                    publisher = None
                if publisher is None:
                    # Correctness is more important than the size optimization.
                    # A later initialized Driver may republish another artifact;
                    # this immutable carrier remains a valid inline fallback.
                    threshold = len(artifact)
            self.carrier = self.carrier.finalize(
                artifact,
                inline_threshold=threshold,
                publisher=publisher,
            )
        return True

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state["_worker_adapter"] = None
        state["_typed_loop_adapter"] = None
        state["_typed_loop_terminal_bypass"] = False
        state["_serialization_version"] = WRAPPER_SERIALIZATION_VERSION
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        version = (
            state.get("_serialization_version")
            if type(state) is dict
            else None
        )
        if (
            type(state) is not dict
            or type(version) is not int
            or version != WRAPPER_SERIALIZATION_VERSION
        ):
            raise ValueError("wrapper_serialization_version_unsupported")
        self.__dict__.update(state)
        self._worker_adapter = None
        self._typed_loop_adapter = None
        self._typed_loop_terminal_bypass = False

    def scalar_call_view(self) -> ScalarCallView:
        if self.logical_schema is None or self.usage_context is None:
            raise ValueError("wrapper_operation_not_finalized")
        return ScalarCallView.from_carrier(
            candidate_id=self.candidate_id,
            usage_context=self.usage_context,
            logical_schema=self.logical_schema,
            carrier=self.carrier,
        )

    def _fallback(self, args: tuple[Any, ...], kwargs: dict[str, Any], reason: str) -> Any:
        try:
            events.try_emit(
                DecisionEvent(
                    stage="execute",
                    decision="fallback",
                    reason_code=reason,
                    candidate_id=self.candidate_id,
                )
            )
        except Exception:
            pass
        return self.original_callable(*args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if (
            os.environ.get("UDFJIT_MODE", "off") != "auto"
            or self.logical_schema is None
        ):
            return self._fallback(args, kwargs, "u2_fallback_only")
        schema = self.logical_schema.lower()
        if "string" in schema and "float64" not in schema:
            if self._typed_loop_terminal_bypass:
                return self.original_callable(*args, **kwargs)
            typed_adapter = self._typed_loop_adapter
            if (
                typed_adapter is not None
                and getattr(typed_adapter, "owner_pid", None) != os.getpid()
            ):
                typed_adapter = None
                self._typed_loop_adapter = None
            if typed_adapter is None:
                try:
                    from python_udf_jit.integration.daft_ray.typed_loop_worker import (
                        build_worker_typed_loop_adapter,
                    )

                    typed_adapter = build_worker_typed_loop_adapter(self)
                    self._typed_loop_adapter = typed_adapter
                except Exception as error:
                    return self._fallback(
                        args,
                        kwargs,
                        f"typed_loop_adapter_init_failed:{type(error).__name__}",
                    )
            outcome = typed_adapter.invoke(args, kwargs)
            if outcome.handled:
                return outcome.value
            if outcome.terminal:
                self._typed_loop_terminal_bypass = True
            return self._fallback(args, kwargs, outcome.reason_code)
        if not self.carrier.finalized:
            return self._fallback(args, kwargs, "u2_fallback_only")
        adapter = self._worker_adapter
        if adapter is not None and getattr(adapter, "owner_pid", None) != os.getpid():
            adapter = None
            self._worker_adapter = None
        if adapter is None:
            try:
                from python_udf_jit.integration.daft_ray.worker import (
                    build_default_worker_adapter,
                )

                adapter = build_default_worker_adapter(self)
                self._worker_adapter = adapter
            except Exception as error:
                return self._fallback(
                    args,
                    kwargs,
                    f"worker_adapter_init_failed:{type(error).__name__}",
                )
        # WorkerScalarAdapter owns the commit boundary. Do not catch an exception
        # here: after machine-code entry the original callable must not be replayed.
        return adapter.invoke(args, kwargs)
