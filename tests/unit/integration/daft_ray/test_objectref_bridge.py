from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import unittest
from types import SimpleNamespace
from unittest import mock

from python_udf_jit.compiler.capture import CaptureRequest, capture
from python_udf_jit.compiler.pipeline import compile_semantic
from python_udf_jit.integration.daft_ray.carrier import (
    ObjectRefArtifactHandle,
)
from python_udf_jit.integration.daft_ray.objectref_bridge import (
    clear_driver_artifact_references,
    install_daft_objectref_bridge,
    register_driver_artifact_reference,
    target_for_objectref_bridge,
)
from python_udf_jit.protocol.artifact import build_artifact
from python_udf_jit.protocol.codec import encode_artifact
from python_udf_jit.protocol.loader import (
    ArtifactLoader,
    LoaderNamespace,
)


def affine(value):
    return value * 2.0 + 3.0


def _encoded_artifact() -> bytes:
    captured = capture(CaptureRequest(affine))
    compiled = compile_semantic(captured)
    return encode_artifact(
        build_artifact(
            compiled.core_module,
            compiled.region_graph,
            captured.fallback_identity,
        )
    )


class _ResolvedReference:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __await__(self):
        async def resolve():
            return self.payload

        return resolve().__await__()


class _FakeActorImplementation:
    async def run_plan(
        self,
        plan,
        exec_cfg,
        psets,
        context,
    ):
        yield plan(exec_cfg, psets, context)


class _FakeActorClass:
    __ray_metadata__ = SimpleNamespace(
        modified_class=_FakeActorImplementation
    )


class _FakeRunPlanRemote:
    def __init__(self):
        self.name = None
        self.arguments = None

    def options(self, *, name):
        self.name = name
        return self

    def remote(self, *arguments):
        self.arguments = arguments
        return ("result-handle", arguments)


class _FakeActorHandle:
    def __init__(self):
        self.run_plan = _FakeRunPlanRemote()


class _FakeRaySwordfishActorHandle:
    def __init__(self):
        self.actor_handle = _FakeActorHandle()

    def submit_task(self, task):
        return ("original-submit", task)


class _FakeRaySwordfishTaskHandle:
    def __init__(self, result_handle, actor_handle):
        self.result_handle = result_handle
        self.actor_handle = actor_handle


class _FakeTask:
    def __init__(self, plan, context=None):
        self._plan = plan
        self._context = (
            {"existing": "value"}
            if context is None
            else context
        )

    def psets(self):
        return {
            "input": [
                SimpleNamespace(object_ref="partition-ref")
            ]
        }

    def context(self):
        return self._context

    def name(self):
        return "bridge-test"

    def plan(self):
        return self._plan

    def config(self):
        return {"config": "value"}


def _flotilla_module():
    return SimpleNamespace(
        RaySwordfishActor=_FakeActorClass,
        RaySwordfishActorHandle=_FakeRaySwordfishActorHandle,
        RaySwordfishTaskHandle=_FakeRaySwordfishTaskHandle,
    )


class ObjectRefBridgeTest(unittest.TestCase):
    def setUp(self):
        self.original_run_plan = _FakeActorImplementation.run_plan
        self.original_submit_task = (
            _FakeRaySwordfishActorHandle.submit_task
        )
        clear_driver_artifact_references()

    def tearDown(self):
        _FakeActorImplementation.run_plan = self.original_run_plan
        _FakeRaySwordfishActorHandle.submit_task = (
            self.original_submit_task
        )
        clear_driver_artifact_references()

    def test_bridge_awaits_on_actor_loop_and_prefetches_for_sync_udf(self):
        payload = _encoded_artifact()
        reference = _ResolvedReference(payload)
        register_driver_artifact_reference(payload, reference)
        module = _flotilla_module()
        target = target_for_objectref_bridge(
            self.original_run_plan,
            self.original_submit_task,
        )

        installed = install_daft_objectref_bridge(
            module,
            target=target,
        )
        repeated = install_daft_objectref_bridge(
            module,
            target=target,
        )

        self.assertTrue(installed.installed)
        self.assertEqual(
            installed.reason,
            "objectref_bridge_installed",
        )
        self.assertTrue(repeated.installed)
        self.assertEqual(
            repeated.reason,
            "objectref_bridge_already_installed",
        )

        content_sha256 = hashlib.sha256(payload).hexdigest()
        handle = ObjectRefArtifactHandle(
            "object-ref",
            content_sha256,
            len(payload),
            reference,
        )
        resolver_calls = 0

        def plan(exec_cfg, psets, context):
            nonlocal resolver_calls

            def unexpected_resolver(_reference):
                nonlocal resolver_calls
                resolver_calls += 1
                raise AssertionError(
                    "sync UDF must use Actor-prefetched bytes"
                )

            loaded = ArtifactLoader(
                resolver=unexpected_resolver
            ).load(
                handle,
                LoaderNamespace(
                    "bridge-job",
                    "default",
                    "process-1",
                ),
            )
            return (
                loaded.content_sha256,
                exec_cfg,
                psets,
                context,
            )

        actor_handle = _FakeRaySwordfishActorHandle()
        submitted = actor_handle.submit_task(_FakeTask(plan))
        self.assertIsInstance(
            submitted,
            _FakeRaySwordfishTaskHandle,
        )
        arguments = actor_handle.actor_handle.run_plan.arguments
        self.assertIsNotNone(arguments)

        async def collect():
            return [
                item
                async for item in _FakeActorImplementation().run_plan(
                    *arguments
                )
            ]

        with mock.patch.dict(
            "os.environ",
            {
                "UDFJIT_RUN_ID": "bridge-job",
                "UDFJIT_TENANT_NAMESPACE": "default",
            },
            clear=False,
        ):
            results = asyncio.run(collect())

        self.assertEqual(resolver_calls, 0)
        self.assertEqual(results[0][0], content_sha256)
        self.assertEqual(results[0][1], {"config": "value"})
        self.assertEqual(
            results[0][2],
            {"input": ["partition-ref"]},
        )
        self.assertEqual(
            results[0][3],
            {"existing": "value"},
        )
        collision_task = _FakeTask(
            plan,
            {
                "__python_udf_jit_artifacts__": "user-value",
            },
        )
        self.assertEqual(
            actor_handle.submit_task(collision_task),
            ("original-submit", collision_task),
        )

    def test_bridge_rejects_contract_mismatch_and_partial_state(self):
        module = _flotilla_module()
        target = target_for_objectref_bridge(
            self.original_run_plan,
            self.original_submit_task,
        )
        mismatch = dataclasses.replace(
            target,
            run_plan_fingerprint="0" * 64,
        )

        rejected = install_daft_objectref_bridge(
            module,
            target=mismatch,
        )

        self.assertFalse(rejected.installed)
        self.assertEqual(
            rejected.reason,
            "objectref_bridge_contract_mismatch",
        )
        self.assertIs(
            _FakeActorImplementation.run_plan,
            self.original_run_plan,
        )
        self.assertIs(
            _FakeRaySwordfishActorHandle.submit_task,
            self.original_submit_task,
        )

        setattr(
            self.original_run_plan,
            "__python_udf_jit_objectref_bridge__",
            True,
        )
        try:
            partial = install_daft_objectref_bridge(
                module,
                target=target,
            )
        finally:
            delattr(
                self.original_run_plan,
                "__python_udf_jit_objectref_bridge__",
            )
        self.assertFalse(partial.installed)
        self.assertEqual(
            partial.reason,
            "objectref_bridge_partial_state",
        )


if __name__ == "__main__":
    unittest.main()
