"""Process-local scalar runtime contracts."""

from python_udf_jit.runtime.layout import (
    FLOAT64_SCALAR_TYPE,
    SCALAR_SLOT_ABI_VERSION,
    CinderXScalarSlotBackend,
    LocalScalarSlotBackend,
    ProcessIdentity,
    ScalarSlotBackend,
    ScalarSlotDescriptor,
)

__all__ = [
    "FLOAT64_SCALAR_TYPE",
    "SCALAR_SLOT_ABI_VERSION",
    "CinderXScalarSlotBackend",
    "LocalScalarSlotBackend",
    "ProcessIdentity",
    "ScalarSlotBackend",
    "ScalarSlotDescriptor",
]
