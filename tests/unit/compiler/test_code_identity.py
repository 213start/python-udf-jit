from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest

from python_udf_jit.compiler.abstract_interpreter import analyze_function
from python_udf_jit.compiler.capture_cache import (
    CaptureCache,
    CaptureCacheKey,
)
from python_udf_jit.compiler.identity import (
    IdentityError,
    IdentityRejectCode,
    capture_identities,
)


GLOBAL_SCALE = 2.0
HELPER_SCALE = 4.0


def global_scale(value):
    return value * GLOBAL_SCALE


def helper(value):
    return value * HELPER_SCALE


def calls_helper(value):
    return helper(value)


def make_offset(offset):
    def apply(value):
        return value + offset

    return apply


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class CodeIdentityTest(unittest.TestCase):
    def test_code_and_dependencies_are_separate(self):
        first = make_offset(1.0)
        second = make_offset(2.0)
        first_ids = capture_identities(first)
        second_ids = capture_identities(second)

        self.assertEqual(first_ids.code, second_ids.code)
        self.assertNotEqual(
            first_ids.dependency.sha256,
            second_ids.dependency.sha256,
        )
        self.assertEqual(
            first_ids.source.code_sha256,
            first_ids.code.sha256,
        )
        other_namespace = capture_identities(
            first,
            namespace_salt="another-job",
        )
        self.assertEqual(first_ids.code, other_namespace.code)
        self.assertEqual(first_ids.dependency, other_namespace.dependency)
        self.assertNotEqual(
            first_ids.source.namespace_sha256,
            other_namespace.source.namespace_sha256,
        )

    def test_global_and_default_changes_only_dependency_fingerprint(self):
        global GLOBAL_SCALE
        original = GLOBAL_SCALE
        try:
            first = capture_identities(global_scale)
            GLOBAL_SCALE = 3.0
            second = capture_identities(global_scale)
        finally:
            GLOBAL_SCALE = original

        self.assertEqual(first.code, second.code)
        self.assertNotEqual(first.dependency, second.dependency)

        def defaulted(value, scale=2.0):
            return value * scale

        before = capture_identities(defaulted)
        defaulted.__defaults__ = (3.0,)
        after = capture_identities(defaulted)
        self.assertEqual(before.code, after.code)
        self.assertNotEqual(before.dependency, after.dependency)

        global HELPER_SCALE
        helper_original = HELPER_SCALE
        try:
            helper_before = capture_identities(calls_helper)
            HELPER_SCALE = 5.0
            helper_after = capture_identities(calls_helper)
        finally:
            HELPER_SCALE = helper_original
        self.assertEqual(helper_before.code, helper_after.code)
        self.assertNotEqual(
            helper_before.dependency,
            helper_after.dependency,
        )

    def test_opaque_dependency_rejects_without_repr_or_address(self):
        repr_calls = 0

        class Opaque:
            def __repr__(self):
                nonlocal repr_calls
                repr_calls += 1
                raise AssertionError("repr must not run")

        value = Opaque()

        def closure(_item):
            return value

        with self.assertRaises(IdentityError) as raised:
            capture_identities(closure)
        self.assertEqual(
            raised.exception.code,
            IdentityRejectCode.UNSUPPORTED_DEPENDENCY,
        )
        self.assertEqual(repr_calls, 0)

    def test_identity_is_cross_process_stable(self):
        expected = capture_identities(global_scale)
        command = (
            "import importlib,json,os;"
            "module=importlib.import_module(os.environ['UDFJIT_TEST_MODULE']);"
            "from python_udf_jit.compiler.identity "
            "import capture_identities;"
            "print(json.dumps(capture_identities(module.global_scale).to_document(),"
            "sort_keys=True,separators=(',',':')))"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            ("src", "tests/unit")
        )
        environment["UDFJIT_TEST_MODULE"] = global_scale.__module__
        completed = subprocess.run(
            [sys.executable, "-c", command],
            check=True,
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            expected.to_document(),
        )

    def test_capture_cache_is_job_scoped_ttl_and_lru_bounded(self):
        now = 100.0

        def clock():
            return now

        program = analyze_function(global_scale)

        def key(job, suffix):
            return CaptureCacheKey(
                job,
                program.identities.code.sha256,
                program.identities.dependency.sha256,
                program.identities.source.namespace_sha256,
                _digest(f"schema-{suffix}"),
                _digest("adapter"),
                _digest("policy"),
            )

        cache = CaptureCache(capacity=2, ttl_seconds=10, clock=clock)
        first = key("job-a", "first")
        second = key("job-a", "second")
        third = key("job-b", "third")
        cache.put(first, program)
        cache.put(second, program)
        self.assertIs(cache.get(first), program)
        cache.put(third, program)
        self.assertIsNone(cache.get(second))
        self.assertIs(cache.get(first), program)
        self.assertEqual(cache.clear_job("job-a"), 1)
        self.assertIsNone(cache.get(first))
        self.assertIs(cache.get(third), program)
        now = 111.0
        self.assertEqual(cache.purge_expired(), 1)
        self.assertEqual(len(cache), 0)


if __name__ == "__main__":
    unittest.main()
