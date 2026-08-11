from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace

from python_udf_jit.integration.daft_ray.invocation_layout import (
    EXACT_UNICODE_LAYOUT_KIND,
    SCALAR_SLOT_LAYOUT_KIND,
    InvocationLayoutContract,
    InvocationLayoutError,
    build_invocation_layout,
)
from python_udf_jit.integration.daft_ray.schema import (
    SchemaContractError,
    canonicalize_logical_type,
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


class _PyField:
    def __init__(self, dtype: _DaftType):
        self._dtype = dtype

    def dtype(self):
        return self._dtype


class _PyDataType:
    """Match the Rust extension type's misleading ``dtype`` member."""

    __module__ = "daft.daft"

    def dtype(self):
        raise AssertionError("a data type is already terminal")

    def __str__(self) -> str:
        return "String"


class _PyExpr:
    def __init__(self, input_name: str):
        self._input_name = input_name

    def to_field(self, schema):
        return _PyField(schema[self._input_name].dtype)


class _Expression:
    def __init__(self, input_name: str):
        self._expr = _PyExpr(input_name)


class SchemaContractTest(unittest.TestCase):
    def test_rust_extension_data_type_is_terminal(self):
        self.assertEqual(canonicalize_logical_type(_PyDataType()), "string")

    def test_real_daft_shape_is_canonical_and_value_free(self):
        document = json.loads(canonicalize_schema(_Schema()))

        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            {field["logical_type"] for field in document["fields"]},
            {"string", "float64"},
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

    def test_candidate_layout_uses_only_its_input_and_output_types(self):
        schema = _Schema()
        schema._schema = schema

        text = build_invocation_layout(
            SimpleNamespace(return_dtype=_DaftType("String")),
            (_Expression("customer_email"),),
            {},
            schema,
            epoch="layout-epoch",
        )
        numeric = build_invocation_layout(
            SimpleNamespace(return_dtype=_DaftType("Float64")),
            (_Expression("score"),),
            {},
            schema,
            epoch="layout-epoch",
        )

        self.assertEqual(text.input_types, ("string",))
        self.assertEqual(text.output_type, "string")
        self.assertEqual(text.layout_kind, EXACT_UNICODE_LAYOUT_KIND)
        self.assertEqual(numeric.input_types, ("float64",))
        self.assertEqual(numeric.output_type, "float64")
        self.assertEqual(numeric.layout_kind, SCALAR_SLOT_LAYOUT_KIND)
        self.assertNotEqual(text.sha256, numeric.sha256)

    def test_unrelated_dataframe_columns_do_not_change_candidate_layout(self):
        schema = _Schema()
        schema._schema = schema
        function = SimpleNamespace(return_dtype=_DaftType("String"))
        expression = _Expression("customer_email")

        with_unrelated_float = build_invocation_layout(
            function,
            (expression,),
            {},
            schema,
            epoch="layout-epoch",
        )
        string_only = _Schema()
        string_only._fields.pop("score")
        string_only._schema = string_only
        without_unrelated_float = build_invocation_layout(
            function,
            (expression,),
            {},
            string_only,
            epoch="layout-epoch",
        )

        self.assertEqual(with_unrelated_float, without_unrelated_float)

    def test_non_scalar_layouts_reject_unknown_abi_versions(self):
        layouts = (
            InvocationLayoutContract.for_types(
                ("string",),
                "string",
                epoch="layout-epoch",
            ),
            InvocationLayoutContract.for_types(
                ("float64", "float64"),
                "float64",
                epoch="layout-epoch",
            ),
        )

        for layout in layouts:
            document = layout.to_document()
            document["layout_abi_version"] = 2
            with self.subTest(layout_kind=layout.layout_kind):
                with self.assertRaises(InvocationLayoutError):
                    InvocationLayoutContract.from_document(document)


if __name__ == "__main__":
    unittest.main()
