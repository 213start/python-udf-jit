from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import importlib
import json
import struct
from typing import Any

from python_udf_jit.runtime.ownership import AccessMode, OwnershipKind


SCALAR_SLOT_ABI_VERSION = 1
SCALAR_LAYOUT_KIND = "scalar_slot"
VECTOR_LAYOUT_KIND = "arrow_array"
BATCH_VIEW_LAYOUT = "batch_view"
BOOL_SCALAR_TYPE = "bool"
INT32_SCALAR_TYPE = "int32"
INT64_SCALAR_TYPE = "int64"
FLOAT32_SCALAR_TYPE = "float32"
FLOAT64_SCALAR_TYPE = "float64"
SUPPORTED_SCALAR_TYPES = (
    BOOL_SCALAR_TYPE,
    INT32_SCALAR_TYPE,
    INT64_SCALAR_TYPE,
    FLOAT32_SCALAR_TYPE,
    FLOAT64_SCALAR_TYPE,
)
_SCALAR_WIDTHS = {
    BOOL_SCALAR_TYPE: 1,
    INT32_SCALAR_TYPE: 4,
    INT64_SCALAR_TYPE: 8,
    FLOAT32_SCALAR_TYPE: 4,
    FLOAT64_SCALAR_TYPE: 8,
}
_UNINITIALIZED = object()


def _non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {field}")
    return value


def scalar_type_width(scalar_type: str) -> int:
    try:
        return _SCALAR_WIDTHS[scalar_type]
    except KeyError as error:
        raise ValueError("unsupported scalar type") from error


def normalize_scalar_value(
    value: object,
    scalar_type: str,
    *,
    nullable: bool,
) -> object:
    """Validate and normalize one value to the formal ScalarSlot contract."""

    if scalar_type not in SUPPORTED_SCALAR_TYPES:
        raise ValueError("unsupported scalar type")
    if value is None:
        if not nullable:
            raise TypeError("non-nullable scalar slot rejects None")
        return None
    if scalar_type == BOOL_SCALAR_TYPE:
        if type(value) is not bool:
            raise TypeError("bool scalar slot accepts exactly bool")
        return value
    if scalar_type in {INT32_SCALAR_TYPE, INT64_SCALAR_TYPE}:
        if type(value) is not int:
            raise TypeError("integer scalar slot accepts exactly int")
        bits = 32 if scalar_type == INT32_SCALAR_TYPE else 64
        lower = -(1 << (bits - 1))
        upper = 1 << (bits - 1)
        if not lower <= value < upper:
            raise OverflowError(f"{scalar_type} scalar value out of range")
        return value
    if type(value) is not float:
        raise TypeError("floating scalar slot accepts exactly float")
    if scalar_type == FLOAT32_SCALAR_TYPE:
        try:
            return struct.unpack(">f", struct.pack(">f", value))[0]
        except OverflowError as error:
            raise OverflowError(
                "float32 scalar value out of range"
            ) from error
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
    """Address-free description of one formal scalar slot.

    This value may cross a serialization boundary. Native storage and authority
    deliberately live only in the process-local capability registry.
    """

    abi_version: int
    scalar_type: str
    epoch: str
    access_id: str
    process: ProcessIdentity
    layout_kind: str = SCALAR_LAYOUT_KIND
    nullable: bool = False
    ownership: str = OwnershipKind.OWNED_TEMPORARY.value
    access_mode: str = AccessMode.READ_WRITE.value
    capacity: int = 1
    descriptor_generation: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.abi_version) is not int
            or self.abi_version <= 0
            or type(self.capacity) is not int
            or self.capacity <= 0
            or type(self.descriptor_generation) is not int
            or self.descriptor_generation <= 0
            or type(self.nullable) is not bool
        ):
            raise ValueError("invalid scalar slot ABI version")
        _non_empty_string(self.scalar_type, "scalar type")
        _non_empty_string(self.epoch, "descriptor epoch")
        _non_empty_string(self.access_id, "descriptor access id")
        _non_empty_string(self.layout_kind, "layout kind")
        _non_empty_string(self.ownership, "ownership")
        _non_empty_string(self.access_mode, "access mode")
        if not isinstance(self.process, ProcessIdentity):
            raise ValueError("invalid descriptor process identity")
        if (
            self.layout_kind != SCALAR_LAYOUT_KIND
            or self.scalar_type not in SUPPORTED_SCALAR_TYPES
            or self.capacity != 1
            or self.ownership
            not in {value.value for value in OwnershipKind}
            or self.access_mode not in {value.value for value in AccessMode}
        ):
            raise ValueError("unsupported scalar slot descriptor")

    def to_document(self) -> dict[str, object]:
        return {
            "access_mode": self.access_mode,
            "abi_version": self.abi_version,
            "access_id": self.access_id,
            "capacity": self.capacity,
            "descriptor_generation": self.descriptor_generation,
            "epoch": self.epoch,
            "layout_kind": self.layout_kind,
            "nullable": self.nullable,
            "ownership": self.ownership,
            "process": self.process.to_document(),
            "scalar_type": self.scalar_type,
        }

    @classmethod
    def from_document(cls, document: object) -> "ScalarSlotDescriptor":
        expected = {
            "access_mode",
            "abi_version",
            "access_id",
            "capacity",
            "descriptor_generation",
            "epoch",
            "layout_kind",
            "nullable",
            "ownership",
            "process",
            "scalar_type",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ValueError("invalid scalar slot descriptor fields")
        return cls(
            document["abi_version"],  # type: ignore[arg-type]
            document["scalar_type"],  # type: ignore[arg-type]
            document["epoch"],  # type: ignore[arg-type]
            document["access_id"],  # type: ignore[arg-type]
            ProcessIdentity.from_document(document["process"]),
            document["layout_kind"],  # type: ignore[arg-type]
            document["nullable"],  # type: ignore[arg-type]
            document["ownership"],  # type: ignore[arg-type]
            document["access_mode"],  # type: ignore[arg-type]
            document["capacity"],  # type: ignore[arg-type]
            document["descriptor_generation"],  # type: ignore[arg-type]
        )

    @property
    def layout_fingerprint(self) -> str:
        payload = json.dumps(
            {
                "access_mode": self.access_mode,
                "abi_version": self.abi_version,
                "capacity": self.capacity,
                "layout_kind": self.layout_kind,
                "nullable": self.nullable,
                "ownership": self.ownership,
                "scalar_type": self.scalar_type,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()


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

    def write_scalar(
        self,
        value: object,
        *,
        scalar_type: str,
        nullable: bool,
    ) -> None:
        normalized = normalize_scalar_value(
            value,
            scalar_type,
            nullable=nullable,
        )
        if scalar_type != FLOAT64_SCALAR_TYPE or normalized is None:
            raise TypeError(
                "native scalar backend currently supports non-null float64"
            )
        self.write_f64(normalized)

    def load_scalar(
        self,
        *,
        scalar_type: str,
        nullable: bool,
    ) -> object:
        if scalar_type != FLOAT64_SCALAR_TYPE or nullable:
            raise TypeError(
                "native scalar backend currently supports non-null float64"
            )
        return self.load_f64()

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

    def __init__(
        self,
        *,
        scalar_type: str = FLOAT64_SCALAR_TYPE,
        nullable: bool = False,
    ) -> None:
        if scalar_type not in SUPPORTED_SCALAR_TYPES:
            raise ValueError("unsupported scalar type")
        if type(nullable) is not bool:
            raise TypeError("nullable must be bool")
        self._scalar_type = scalar_type
        self._nullable = nullable
        self._value: object = _UNINITIALIZED
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("scalar slot backend is closed")

    def write_f64(self, value: float) -> None:
        if (
            self._scalar_type != FLOAT64_SCALAR_TYPE
            or self._nullable
        ):
            raise TypeError(
                "write_f64 requires a non-null float64 local slot"
            )
        self.write_scalar(
            value,
            scalar_type=FLOAT64_SCALAR_TYPE,
            nullable=False,
        )

    def write_scalar(
        self,
        value: object,
        *,
        scalar_type: str,
        nullable: bool,
    ) -> None:
        self._ensure_open()
        if (
            scalar_type != self._scalar_type
            or nullable != self._nullable
        ):
            raise TypeError("scalar slot contract mismatch")
        self._value = normalize_scalar_value(
            value,
            scalar_type,
            nullable=nullable,
        )

    def load_f64(self) -> float:
        if (
            self._scalar_type != FLOAT64_SCALAR_TYPE
            or self._nullable
        ):
            raise TypeError(
                "load_f64 requires a non-null float64 local slot"
            )
        value = self.load_scalar(
            scalar_type=FLOAT64_SCALAR_TYPE,
            nullable=False,
        )
        if type(value) is not float:
            raise TypeError("float64 local slot returned a non-float")
        return value

    def load_scalar(
        self,
        *,
        scalar_type: str,
        nullable: bool,
    ) -> object:
        self._ensure_open()
        if (
            scalar_type != self._scalar_type
            or nullable != self._nullable
        ):
            raise TypeError("scalar slot contract mismatch")
        if self._value is _UNINITIALIZED:
            raise RuntimeError("scalar slot has not been initialized")
        return self._value

    def begin_borrow(self) -> Any:
        self._ensure_open()
        # A slot is rebound for every row.  Clearing here makes an omitted
        # write fail closed instead of reusing the previous row's value.
        self._value = _UNINITIALIZED
        return self.borrow_keepalive()

    def close(self) -> None:
        self._closed = True
        self._value = _UNINITIALIZED


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
