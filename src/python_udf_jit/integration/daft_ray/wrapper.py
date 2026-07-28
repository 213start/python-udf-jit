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


WRAPPER_SERIALIZATION_VERSION = 1


@dataclass
class FallbackOnlyWrapper:
    """Serializable Daft carrier with a lazy, process-local U5 Worker runtime."""

    candidate_id: str
    original_callable: Callable[..., Any]
    carrier: ProductionCarrierState
    logical_schema: str | None = None
    usage_context: str | None = None
    _worker_adapter: Any = field(default=None, init=False, repr=False, compare=False)
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
                        publisher = ray.put
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
            or not self.carrier.finalized
            or self.logical_schema is None
        ):
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
