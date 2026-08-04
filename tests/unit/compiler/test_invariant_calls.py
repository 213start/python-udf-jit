from __future__ import annotations

import json
import os
import unittest
from functools import lru_cache
from pathlib import Path

from python_udf_jit.compiler.invariant_calls import (
    analyze_invariant_calls,
    analyze_value_cache,
)


def _choose_location(explicit: str | None) -> str:
    if explicit:
        return explicit
    direct = os.environ.get("UDFJIT_TEST_DIRECT", "").strip()
    if direct:
        return direct
    base = os.environ.get("UDFJIT_TEST_BASE", "").strip()
    if base:
        return str(Path(base) / "frozen" / "dataset")
    return ""


def _format_record(row: str, *, location: str | None = None, **_extras) -> str:
    selected = _choose_location(location)
    return f"{selected}:{row}"


_SIDE_EFFECTS: list[str] = []
_UNMODELED_STATE = {"value": "first"}


def _mutating_helper(value: str | None) -> str:
    _SIDE_EFFECTS.append(str(value))
    return "changed"


def _uses_mutating_helper(
    row: str,
    *,
    location: str | None = None,
) -> str:
    return _mutating_helper(location) + row


def _dynamic_environment_key(key: str) -> str:
    return os.environ.get(key, "")


def _uses_dynamic_environment_key(
    row: str,
    *,
    key: str = "UDFJIT_TEST_DIRECT",
) -> str:
    return _dynamic_environment_key(key) + row


def _row_dependent_helper(value: str) -> str:
    return value.strip()


def _uses_row_dependent_helper(row: str) -> str:
    return _row_dependent_helper(row)


@lru_cache(maxsize=2)
def _load_frozen_records(location: str) -> dict[str, dict[str, object]]:
    return {"row-a": {"label": location}}


def _render_frozen_record(
    row: str,
    *,
    location: str | None = None,
    **_extras: object,
) -> str:
    selected = _choose_location(location)
    records = _load_frozen_records(selected)
    if not records:
        raise RuntimeError("records unavailable")
    record = records.get(row)
    if not record:
        return json.dumps({"row": str(row), "label": None}, ensure_ascii=False)
    return json.dumps(
        {"row": str(row), "label": record.get("label")},
        ensure_ascii=False,
    )


def _returns_mutable_value(
    row: str,
    *,
    location: str | None = None,
    **_extras: object,
) -> dict[str, str]:
    return {"row": row}


def _index_external_record(payload: str, **_extras: object) -> str:
    try:
        record = json.loads(payload)
        if not isinstance(record, dict):
            raise ValueError
    except Exception:
        record = {"path": str(payload), "valid": False}
    path = record.get("path")
    present = bool(path and os.path.exists(path))
    record["present"] = present
    if present:
        record["status"] = "available"
    else:
        record["status"] = "missing"
    return json.dumps(record, ensure_ascii=False)


def _index_external_record_with_side_effect(
    payload: str,
    **_extras: object,
) -> str:
    try:
        record = json.loads(payload)
        if not isinstance(record, dict):
            raise ValueError
    except Exception:
        record = {"path": str(payload), "valid": False}
    path = record.get("path")
    present = bool(path and os.path.exists(path))
    _SIDE_EFFECTS.append(payload)
    record["present"] = present
    return json.dumps(record, ensure_ascii=False)


def _index_external_record_with_unmodeled_state(
    payload: str,
    **_extras: object,
) -> str:
    try:
        record = json.loads(payload)
        if not isinstance(record, dict):
            raise ValueError
    except Exception:
        record = {"path": str(payload), "valid": False}
    path = record.get("path")
    present = bool(path and os.path.exists(path))
    record["present"] = present
    record["unmodeled"] = _UNMODELED_STATE["value"]
    return json.dumps(record, ensure_ascii=False)


class InvariantCallAnalysisTests(unittest.TestCase):
    def test_discovers_generic_control_state_read_and_sequence_patterns(self) -> None:
        plans = analyze_invariant_calls(_format_record)

        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertIs(plan.function, _choose_location)
        self.assertIsNone(plan.argument)
        self.assertEqual(plan.argument_mode, "identity")
        self.assertEqual(
            plan.behavior_patterns,
            (
                "branch",
                "immutable_sequence_construct",
                "process_state_read",
            ),
        )
        self.assertEqual(plan.result_type, "exact_unicode")
        watcher_kinds = {watcher.kind for watcher in plan.watchers}
        self.assertIn("dict_item", watcher_kinds)
        self.assertIn("function_code", watcher_kinds)
        self.assertIn("type_attr", watcher_kinds)
        descriptor = plan.backend_descriptor()
        self.assertEqual(descriptor["version"], 1)
        self.assertEqual(descriptor["argument_modes"], ("identity",))
        self.assertTrue(descriptor["watchers"])

    def test_rejects_mutating_helper(self) -> None:
        self.assertEqual(analyze_invariant_calls(_uses_mutating_helper), ())

    def test_rejects_dynamic_dependency_key(self) -> None:
        self.assertEqual(
            analyze_invariant_calls(_uses_dynamic_environment_key),
            (),
        )

    def test_rejects_row_dependent_argument(self) -> None:
        self.assertEqual(analyze_invariant_calls(_uses_row_dependent_helper), ())

    def test_discovers_generic_exact_value_reuse(self) -> None:
        plan = analyze_value_cache(_render_frozen_record)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertIs(plan.function, _render_frozen_record)
        self.assertEqual(
            plan.argument_modes,
            ("exact_unicode_value", "identity", "empty_dict"),
        )
        self.assertEqual(plan.argument_values, (None,))
        self.assertEqual(plan.input_type, "exact_unicode")
        self.assertEqual(plan.result_type, "exact_unicode")
        self.assertIn("branch", plan.behavior_patterns)
        self.assertIn("bounded_value_reuse", plan.behavior_patterns)
        self.assertIn("immutable_result_construct", plan.behavior_patterns)
        self.assertIn(
            "call_result_identity",
            {watcher.kind for watcher in plan.watchers},
        )
        descriptor = plan.backend_descriptor()
        self.assertEqual(descriptor["capacity"], 16_384)
        self.assertNotIn("row-a", repr(descriptor))

    def test_value_cache_rejects_mutable_results(self) -> None:
        self.assertIsNone(analyze_value_cache(_returns_mutable_value))

    def test_discovers_guarded_external_state_value_reuse(self) -> None:
        plan = analyze_value_cache(_index_external_record)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(
            plan.argument_modes,
            ("exact_unicode_value", "empty_dict"),
        )
        self.assertEqual(plan.argument_values, ())
        self.assertIn("external_state_guard", plan.behavior_patterns)
        self.assertIn("exception_region", plan.behavior_patterns)
        descriptor = plan.backend_descriptor()
        self.assertEqual(
            descriptor["entry_guard"][0],
            "json_result_call_value",
        )
        self.assertEqual(descriptor["entry_guard"][2], ("path",))
        self.assertIs(descriptor["entry_guard"][3], os.path.exists)
        self.assertEqual(descriptor["entry_guard"][4], ("present",))

    def test_guarded_value_cache_rejects_unmodeled_calls(self) -> None:
        self.assertIsNone(
            analyze_value_cache(_index_external_record_with_side_effect)
        )

    def test_guarded_value_cache_rejects_unmodeled_state_reads(self) -> None:
        self.assertIsNone(
            analyze_value_cache(_index_external_record_with_unmodeled_state)
        )


if __name__ == "__main__":
    unittest.main()
