from __future__ import annotations

import hashlib
import json
import unittest

from python_udf_jit.integration.daft_ray.schema import (
    SchemaContractError,
    canonicalize_schema,
)


class _DaftType:
    __module__ = "daft.datatype"

    def __init__(self, name: str):
        self._name = name

    def __str__(self) -> str:
        return self._name


class _Field:
    __module__ = "daft.schema"

    def __init__(self, dtype: _DaftType):
        self.dtype = dtype


class _Schema:
    def __init__(self):
        self._fields = {
            "customer_email": _Field(_DaftType("Utf8")),
            "score": _Field(_DaftType("Float64")),
        }

    def column_names(self):
        return list(self._fields)

    def __getitem__(self, name):
        return self._fields[name]


class SchemaContractTest(unittest.TestCase):
    def test_real_daft_shape_is_canonical_and_value_free(self):
        document = json.loads(canonicalize_schema(_Schema()))

        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            {field["logical_type"] for field in document["fields"]},
            {"utf8", "float64"},
        )
        self.assertNotIn("customer_email", json.dumps(document))
        self.assertIn(
            hashlib.sha256(b"customer_email").hexdigest(),
            {field["name_sha256"] for field in document["fields"]},
        )

    def test_mapping_order_does_not_change_canonical_bytes(self):
        first = canonicalize_schema({"left": "int64", "right": "float64"})
        second = canonicalize_schema({"right": "float64", "left": "int64"})

        self.assertEqual(first, second)

    def test_schema_limits_and_unknown_types_fail_closed(self):
        with self.assertRaisesRegex(SchemaContractError, "schema_type_unsupported"):
            canonicalize_schema({"value": object()})
        with self.assertRaisesRegex(SchemaContractError, "schema_field_limit"):
            canonicalize_schema(
                {f"field-{index}": "int64" for index in range(4097)}
            )


if __name__ == "__main__":
    unittest.main()
