from __future__ import annotations

import base64
import hashlib
import json
import operator
import os
import pickle
import subprocess
import sys
import unittest
from pathlib import Path

from python_udf_jit.integration.daft_ray.carrier import (
    CarrierContractError,
    ProductionCarrierState,
    ScalarCallView,
)
from python_udf_jit.integration.daft_ray.invocation_layout import (
    InvocationLayoutContract,
)
from python_udf_jit.integration.daft_ray.wrapper import FallbackOnlyWrapper


def _worker_environment(**overrides: str) -> dict[str, str]:
    roots = (
        str(Path(__file__).resolve().parent),
        str(Path(__file__).resolve().parents[2]),
        os.environ.get("PYTHONPATH", ""),
    )
    return dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(root for root in roots if root),
        **overrides,
    )


class DaftScalarCarrierContractTest(unittest.TestCase):
    def test_scalar_call_view_is_address_free_and_stable_across_worker_pickle(self):
        wrapper = FallbackOnlyWrapper(
            candidate_id="candidate-scalar-view",
            original_callable=operator.add,
            carrier=ProductionCarrierState.placeholder(
                "candidate-scalar-view",
                "a" * 64,
            ),
        )
        wrapper.finalize(
            '{"fields":[],"schema_version":1}',
            "projection",
            b"portable-artifact",
        )
        expected = wrapper.scalar_call_view().to_bytes()
        payload = base64.b64encode(pickle.dumps(wrapper)).decode("ascii")
        script = """
import base64, os, pickle
wrapper = pickle.loads(base64.b64decode(os.environ["UDFJIT_WRAPPER"]))
print(base64.b64encode(wrapper.scalar_call_view().to_bytes()).decode("ascii"))
"""

        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=_worker_environment(UDFJIT_WRAPPER=payload),
            timeout=15,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        actual = base64.b64decode(completed.stdout.strip())
        self.assertEqual(actual, expected)
        document = json.loads(actual)
        self.assertEqual(document["handle_kind"], "inline-artifact")
        self.assertEqual(
            document["policy_sha256"],
            wrapper.carrier.policy.sha256,
        )
        self.assertNotIn("payload", document)
        self.assertFalse(
            any(
                token in json.dumps(document).lower()
                for token in ("address", "pointer", "path")
            )
        )

    def test_scalar_call_view_rejects_unknown_version(self):
        view = ScalarCallView.from_carrier(
            candidate_id="candidate-version",
            usage_context="filter",
            logical_schema='{"fields":[],"schema_version":1}',
            carrier=ProductionCarrierState.placeholder(
                "candidate-version",
                "b" * 64,
            ),
        )
        document = json.loads(view.to_bytes())
        document["schema_version"] = 3

        with self.assertRaisesRegex(
            CarrierContractError,
            "unsupported scalar call view version",
        ):
            ScalarCallView.from_bytes(
                json.dumps(document, sort_keys=True).encode("ascii")
            )

        document["schema_version"] = True
        with self.assertRaisesRegex(
            CarrierContractError,
            "unsupported scalar call view version",
        ):
            ScalarCallView.from_bytes(
                json.dumps(document, sort_keys=True).encode("ascii")
            )

    def test_layout_call_view_uses_explicit_version_two_hash(self):
        logical_schema = '{"fields":[],"schema_version":1}'
        layout = InvocationLayoutContract.for_types(
            ("float64",),
            "float64",
            epoch="epoch-a",
        )
        view = ScalarCallView.from_carrier(
            candidate_id="candidate-layout",
            usage_context="projection",
            logical_schema=logical_schema,
            invocation_layout=layout,
            carrier=ProductionCarrierState.placeholder(
                "candidate-layout",
                "c" * 64,
            ),
        )

        restored = ScalarCallView.from_bytes(view.to_bytes())

        self.assertEqual(restored.schema_version, 2)
        self.assertEqual(
            restored.logical_schema_sha256,
            hashlib.sha256(logical_schema.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(restored.invocation_layout_sha256, layout.sha256)


if __name__ == "__main__":
    unittest.main()
