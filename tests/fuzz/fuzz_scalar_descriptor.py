from __future__ import annotations

import random

from python_udf_jit.runtime.descriptors import (
    admit_access_spec,
    scalar_input_spec,
)
from python_udf_jit.runtime.layout import (
    SUPPORTED_SCALAR_TYPES,
    normalize_scalar_value,
)


def fuzz_one(seed: int) -> None:
    randomizer = random.Random(seed)
    scalar_type = randomizer.choice(SUPPORTED_SCALAR_TYPES)
    nullable = bool(randomizer.getrandbits(1))
    spec = scalar_input_spec(scalar_type, nullable=nullable)
    assert admit_access_spec(spec).accepted
    if scalar_type == "bool":
        value = bool(randomizer.getrandbits(1))
    elif scalar_type == "int32":
        value = randomizer.randint(-(1 << 31), (1 << 31) - 1)
    elif scalar_type == "int64":
        value = randomizer.randint(-(1 << 63), (1 << 63) - 1)
    else:
        value = randomizer.uniform(-1e20, 1e20)
    normalize_scalar_value(value, scalar_type, nullable=nullable)


if __name__ == "__main__":
    for iteration in range(10_000):
        fuzz_one(iteration)
