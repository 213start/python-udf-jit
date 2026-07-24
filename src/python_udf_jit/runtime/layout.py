from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import importlib
from typing import Any


SCALAR_SLOT_ABI_VERSION = 1
FLOAT64_SCALAR_TYPE = "float64"


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True)
class ProcessIdentity:
    """Serializable identity of one worker process generation."""

    pid: int
    generation: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("invalid process pid")
        _non_empty_string(self.generation, "process generation")

    def to_document(self) -> dict[str, object]:
        return {"pid": self.pid, "generation": self.generation}

    @classmethod
    def from_document(cls, document: object) -> "ProcessIdentity":
        if not isinstance(document, dict) or set(document) != {"pid", "generation"}:
            raise ValueError("invalid process identity fields")
        return cls(document["pid"], document["generation"])  # type: ignore[arg-type]


@dataclass(frozen=True)
class ScalarSlotDescriptor:
    """Address-free description of a single physical float64 slot.

    This value may cross a serialization boundary. Native storage and authority
    deliberately live only in the process-local capability registry.
    """

    abi_version: int
    scalar_type: str
    epoch: str
    access_id: str
    process: ProcessIdentity

    def __post_init__(self) -> None:
        if type(self.abi_version) is not int or self.abi_version <= 0:
            raise ValueError("invalid scalar slot ABI version")
        _non_empty_string(self.scalar_type, "scalar type")
        _non_empty_string(self.epoch, "descriptor epoch")
        _non_empty_string(self.access_id, "descriptor access id")
        if not isinstance(self.process, ProcessIdentity):
            raise ValueError("invalid descriptor process identity")

    def to_document(self) -> dict[str, object]:
        return {
            "abi_version": self.abi_version,
            "scalar_type": self.scalar_type,
            "epoch": self.epoch,
            "access_id": self.access_id,
            "process": self.process.to_document(),
        }

    @classmethod
    def from_document(cls, document: object) -> "ScalarSlotDescriptor":
        expected = {"abi_version", "scalar_type", "epoch", "access_id", "process"}
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid scalar slot descriptor fields")
        return cls(
            document["abi_version"],  # type: ignore[arg-type]
            document["scalar_type"],  # type: ignore[arg-type]
            document["epoch"],  # type: ignore[arg-type]
            document["access_id"],  # type: ignore[arg-type]
            ProcessIdentity.from_document(document["process"]),
        )


class ScalarSlotBackend(ABC):
    """Process-local storage seam used by Python and native Capsule backends.

    A future CinderX Capsule implementation supplies these same operations; it
    must never place the Capsule or a native address in ``ScalarSlotDescriptor``.
    """

    @abstractmethod
    def write_f64(self, value: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def load_f64(self) -> float:
        raise NotImplementedError

    def borrow_keepalive(self) -> Any:
        return self

    def begin_borrow(self) -> Any:
        """Enter the backend's native borrow scope and return its keepalive."""

        return self.borrow_keepalive()

    def end_borrow(self) -> None:
        """Leave the backend's native borrow scope."""

    def execution_handle(self) -> object:
        """Return a borrow-scoped backend handle; never serialize this value."""

        return self

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class LocalScalarSlotBackend(ScalarSlotBackend):
    """Pure-Python reference backend; this does not claim native/JIT execution."""

    def __init__(self) -> None:
        self._value: float | None = None
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("scalar slot backend is closed")

    def write_f64(self, value: float) -> None:
        self._ensure_open()
        if type(value) is not float:
            raise TypeError("scalar slot accepts exactly one Python float")
        self._value = value

    def load_f64(self) -> float:
        self._ensure_open()
        if self._value is None:
            raise RuntimeError("scalar slot has not been initialized")
        return self._value

    def begin_borrow(self) -> Any:
        self._ensure_open()
        # A slot is rebound for every row.  Clearing here makes an omitted
        # write fail closed instead of reusing the previous row's value.
        self._value = None
        return self.borrow_keepalive()

    def close(self) -> None:
        self._closed = True
        self._value = None


class CinderXScalarSlotBackend(ScalarSlotBackend):
    """Lazy adapter for CinderX's process-local scalar-slot Capsule API.

    Importing this project does not require CinderX. The module and Capsule are
    acquired only when this backend is first borrowed or accessed.
    """

    _REQUIRED_HELPERS = (
        "_udf_create_scalar_slot",
        "_udf_set_scalar_slot",
        "_udf_begin_scalar_slot_borrow",
        "_udf_end_scalar_slot_borrow",
        "_udf_release_scalar_slot",
        "_udf_guard_data_handle",
        "_udf_data_load_f64",
    )

    def __init__(self, *, module_name: str = "cinderjit") -> None:
        if not isinstance(module_name, str) or not module_name:
            raise ValueError("CinderX module name must be a non-empty string")
        self._module_name = module_name
        self._module: Any = None
        self._capsule: object | None = None
        self._borrowed = False
        self._closed = False

    def _runtime(self) -> Any:
        if self._module is None:
            module = importlib.import_module(self._module_name)
            missing = [name for name in self._REQUIRED_HELPERS if not callable(getattr(module, name, None))]
            if missing:
                raise RuntimeError(f"CinderX scalar runtime helpers missing: {','.join(missing)}")
            self._module = module
        return self._module

    def _ensure_capsule(self) -> object:
        if self._closed:
            raise RuntimeError("CinderX scalar slot backend is closed")
        if self._capsule is None:
            self._capsule = self._runtime()._udf_create_scalar_slot()
        return self._capsule

    def write_f64(self, value: float) -> None:
        if type(value) is not float:
            raise TypeError("scalar slot accepts exactly one Python float")
        self._runtime()._udf_set_scalar_slot(self._ensure_capsule(), value)

    def load_f64(self) -> float:
        runtime = self._runtime()
        guarded = runtime._udf_guard_data_handle(self._ensure_capsule())
        value = runtime._udf_data_load_f64(guarded)
        if type(value) is not float:
            raise TypeError("CinderX scalar data load must return a Python float")
        return value

    def begin_borrow(self) -> object:
        if self._borrowed:
            raise RuntimeError("CinderX scalar slot is already borrowed")
        capsule = self._ensure_capsule()
        self._runtime()._udf_begin_scalar_slot_borrow(capsule)
        self._borrowed = True
        return capsule

    def end_borrow(self) -> None:
        if not self._borrowed or self._capsule is None:
            raise RuntimeError("CinderX scalar slot is not borrowed")
        try:
            self._runtime()._udf_end_scalar_slot_borrow(self._capsule)
        finally:
            self._borrowed = False

    def execution_handle(self) -> object:
        if not self._borrowed:
            raise RuntimeError("CinderX execution handle requires an active borrow")
        return self._ensure_capsule()

    def close(self) -> None:
        if self._borrowed:
            raise RuntimeError("cannot close a borrowed CinderX scalar slot")
        if self._closed:
            return
        if self._capsule is not None:
            self._runtime()._udf_release_scalar_slot(self._capsule)
            self._capsule = None
        self._closed = True
