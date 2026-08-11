from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Callable

from python_udf_jit.diagnostics import events
from python_udf_jit.diagnostics.events import DecisionEvent
from python_udf_jit.integration.daft_ray.carrier import (
    DEFAULT_INLINE_ARTIFACT_THRESHOLD,
    ProductionCarrierState,
    ScalarCallView,
)
from python_udf_jit.integration.daft_ray.invocation_layout import (
    EXACT_UNICODE_LAYOUT_KIND,
    PYTHON_OBJECT_LAYOUT_KIND,
    InvocationLayoutContract,
)


WRAPPER_SERIALIZATION_VERSION = 2


@dataclass
class FallbackOnlyWrapper:
    """Serializable Daft carrier with a lazy, process-local U5 Worker runtime."""

    candidate_id: str
    original_callable: Callable[..., Any]
    carrier: ProductionCarrierState
    logical_schema: str | None = None
    usage_context: str | None = None
    invocation_layout: InvocationLayoutContract | None = None
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
        *,
        invocation_layout: InvocationLayoutContract | None = None,
    ) -> bool:
        """Attach operation context once, before Daft serializes the finalized plan."""

        if self.logical_schema is not None or self.usage_context is not None:
            return False
        self.logical_schema = logical_schema
        self.usage_context = usage_context
        if invocation_layout is not None and not isinstance(
            invocation_layout,
            InvocationLayoutContract,
        ):
            raise TypeError("invocation layout contract required")
        self.invocation_layout = invocation_layout
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
            invocation_layout=self.invocation_layout,
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
        layout = self.invocation_layout
        if layout is not None and layout.layout_kind == EXACT_UNICODE_LAYOUT_KIND:
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
                # The cluster epoch is fixed for a Worker process. Validate it
                # after deserialization/fork, before binding process-local state,
                # and keep the per-row warm path free of environment lookups.
                observed_epoch = os.environ.get("UDFJIT_CLUSTER_EPOCH", "")
                if observed_epoch != layout.epoch:
                    return self._fallback(
                        args,
                        kwargs,
                        "layout_epoch_mismatch",
                    )
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
        if layout is not None and layout.layout_kind == PYTHON_OBJECT_LAYOUT_KIND:
            return self._fallback(args, kwargs, "candidate_layout_unsupported")
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
