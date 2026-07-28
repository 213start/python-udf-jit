from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path


def _continuation_source(value):
    return value


def _continuation_suffix(state):
    state.values["effects"].append(
        ("suffix", state.values["value"])
    )
    return state.values["value"] + 1


def _mapping_is_writable_and_executable(line: str) -> bool:
    fields = line.split(maxsplit=2)
    if len(fields) < 2:
        return False
    permissions = fields[1]
    return (
        len(permissions) >= 3
        and permissions[1] == "w"
        and permissions[2] == "x"
    )


class ProcMapsPermissionsTests(unittest.TestCase):
    def test_detects_every_writable_executable_permission_form(self) -> None:
        cases = {
            "1000-2000 rwxp 00000000 00:00 0": True,
            "1000-2000 rwxs 00000000 00:00 0": True,
            "1000-2000 -wxp 00000000 00:00 0": True,
            "1000-2000 -wxs 00000000 00:00 0": True,
            "1000-2000 r-xp 00000000 00:00 0": False,
            "1000-2000 rw-p 00000000 00:00 0": False,
            "1000-2000 ---p 00000000 00:00 0": False,
            "": False,
            "malformed": False,
            "1000-2000 wx": False,
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertIs(
                    _mapping_is_writable_and_executable(line),
                    expected,
                )


class _WorkerScalarSlotProbe:
    def run(self, cluster_epoch: str) -> dict[str, object]:
        from dataclasses import astuple
        import hashlib
        import sysconfig

        import cinderx.jit
        import ray
        from cinderjit import (
            _udf_build_continuation_payload,
            _udf_register_continuation_code,
        )

        from python_udf_jit.compiler.capture import FallbackIdentity
        from python_udf_jit.compiler.identity import capture_identities
        from python_udf_jit.compiler.region import (
            form_semantic_region_graph,
        )
        from python_udf_jit.protocol.artifact import build_artifact
        from python_udf_jit.provider.scalar_python.capability import (
            CapabilityRegistry,
        )
        from python_udf_jit.provider.scalar_python.executor import (
            CinderXScalarProviderFactory,
            ScalarExecutor,
        )
        from python_udf_jit.runtime.descriptors import (
            scalar_input_spec,
            scalar_output_spec,
        )
        from python_udf_jit.runtime.layout import (
            SCALAR_SLOT_ABI_VERSION,
            LocalScalarSlotBackend,
            normalize_scalar_value,
        )
        from python_udf_jit.runtime.continuation import (
            CONTINUATION_ABI_VERSION,
            CommitBoundary,
            ContinuationContract,
            InterpreterContinuation,
            LiveValueKind,
            LiveValueSpec,
            ResumeSourceMap,
        )
        from python_udf_jit.runtime.variant import (
            VariantKey,
            WorkerProcessKey,
        )
        from tests.unit.provider.scalar_python.test_scalar_matrix import (
            _LOGICAL_TYPES,
            _branch_module,
            _identity_module,
        )

        runtime_context = ray.get_runtime_context()
        node_id = runtime_context.get_node_id()
        actor_id = str(runtime_context.get_actor_id())
        worker_id = str(runtime_context.get_worker_id())
        pid = os.getpid()
        process_generation = hashlib.sha256(
            f"{cluster_epoch}:{node_id}:{actor_id}:{worker_id}:{pid}".encode(
                "ascii"
            )
        ).hexdigest()
        process_key = WorkerProcessKey(
            cluster_epoch,
            node_id,
            f"{actor_id}:{worker_id}",
            pid,
            process_generation,
        )
        factory = CinderXScalarProviderFactory()
        reports = []
        variants = []

        def digest(value: str) -> str:
            return hashlib.sha256(value.encode("ascii")).hexdigest()

        def compile_variant(
            scalar_type: str,
            *,
            nullable: bool,
            module,
            label: str,
        ):
            graph = form_semantic_region_graph(module)
            input_spec = scalar_input_spec(
                scalar_type,
                nullable=nullable,
            )
            output_spec = scalar_output_spec(
                scalar_type,
                nullable=nullable,
            )
            artifact = build_artifact(
                module,
                graph,
                FallbackIdentity(
                    "tests.ray_cinderx_scalar_matrix",
                    label,
                    module.function_id,
                ),
                input_access_specs=(input_spec,),
                output_access_spec=output_spec,
            )
            key = VariantKey(
                process=process_key,
                artifact_content_sha256=artifact.content_sha256,
                semantic_hash=module.semantic_hash,
                schema_fingerprint=digest(
                    f"{scalar_type}:{nullable}"
                ),
                callable_code_sha256=module.function_id,
                artifact_manifest_sha256=digest("formal-artifact-1.0"),
                experiment_manifest_sha256=digest(cluster_epoch),
                adapter_abi=1,
                runtime_abi=1,
                scalar_slot_abi=SCALAR_SLOT_ABI_VERSION,
                cpython_cinderx_soabi=str(
                    sysconfig.get_config_var("SOABI")
                ),
                cpu_features=("scalar",),
            )
            variant = factory.compile(artifact, key)
            variants.append(variant)
            return variant

        representatives = {
            "bool": True,
            "int32": -(1 << 31),
            "int64": 1 << 40,
            "float32": 1.1,
            "float64": -3.5,
        }
        hir_type_names = {
            "bool": "Bool",
            "int32": "I32",
            "int64": "I64",
            "float32": "F32",
            "float64": "F64",
        }
        try:
            for scalar_type, value in representatives.items():
                module = _identity_module(
                    _LOGICAL_TYPES[scalar_type],
                    nullable=True,
                )
                variant = compile_variant(
                    scalar_type,
                    nullable=True,
                    module=module,
                    label=f"identity_{scalar_type}",
                )
                actual = variant.execute(
                    value,
                    boundary=CommitBoundary(),
                )
                expected = normalize_scalar_value(
                    value,
                    scalar_type,
                    nullable=True,
                )
                if (
                    type(expected) is float
                    and actual.hex() != expected.hex()
                ) or (
                    type(expected) is not float
                    and (
                        actual != expected
                        or type(actual) is not type(expected)
                    )
                ):
                    raise AssertionError(
                        (scalar_type, actual, expected)
                    )
                if (
                    variant.execute(
                        None,
                        boundary=CommitBoundary(),
                    )
                    is not None
                ):
                    raise AssertionError(
                        f"{scalar_type} null value was not preserved"
                    )
                counts = cinderx.jit.get_function_hir_opcode_counts(
                    variant.compiled.jit_function
                )
                hir_type = hir_type_names[scalar_type]
                if (
                    counts.get(f"LoadUdfData{hir_type}", 0) != 1
                    or counts.get(f"StoreUdfData{hir_type}", 0) != 1
                    or counts.get("IsUdfDataNull", 0) != 1
                    or counts.get("StoreUdfDataNull", 0) != 1
                ):
                    raise AssertionError(
                        f"{scalar_type} HIR evidence drift: {counts}"
                    )
                input_descriptor = variant.registry.descriptor(
                    variant.input_handle
                )
                output_descriptor = variant.registry.descriptor(
                    variant.output_handle
                )
                if (
                    input_descriptor.scalar_type != scalar_type
                    or output_descriptor.scalar_type != scalar_type
                    or not input_descriptor.nullable
                    or not output_descriptor.nullable
                    or input_descriptor.epoch != cluster_epoch
                    or output_descriptor.epoch != cluster_epoch
                    or input_descriptor.process
                    != variant.registry.process_identity
                    or output_descriptor.process
                    != variant.registry.process_identity
                ):
                    raise AssertionError("typed descriptor identity drift")
                reports.append(
                    {
                        "scalar_type": scalar_type,
                        "semantic_hash": variant.compiled.semantic_hash,
                        "code_hash": variant.code_hash,
                        "intrinsic_counts": counts,
                    }
                )

            branch = compile_variant(
                "int64",
                nullable=False,
                module=_branch_module(),
                label="int64_branch",
            )
            branch_results = [
                branch.execute(-9, boundary=CommitBoundary()),
                branch.execute(7, boundary=CommitBoundary()),
            ]
            if branch_results != [9, 7]:
                raise AssertionError(
                    f"branch result drift: {branch_results}"
                )
            branch_counts = cinderx.jit.get_function_hir_opcode_counts(
                branch.compiled.jit_function
            )
            if (
                branch_counts.get("LoadUdfDataI64", 0) != 1
                or branch_counts.get("StoreUdfDataI64", 0) != 1
                or branch_counts.get("CondBranch", 0) < 1
            ):
                raise AssertionError(
                    f"branch HIR evidence drift: {branch_counts}"
                )
        finally:
            for variant in reversed(variants):
                variant.close()

        if len({report["code_hash"] for report in reports}) != 5:
            raise AssertionError("physical scalar types reused one code hash")

        identity = capture_identities(_continuation_source).source
        resume_id = "v1:" + "c" * 64
        source_map = ResumeSourceMap(
            schema_version=CONTINUATION_ABI_VERSION,
            bytecode_offset=18,
            line=identity.first_line,
            column=4,
            end_line=identity.first_line,
            end_column=18,
        )
        contract = ContinuationContract(
            abi_version=CONTINUATION_ABI_VERSION,
            resume_id=resume_id,
            source_identity=identity,
            source_code=_continuation_source.__code__,
            resume_code=_continuation_suffix.__code__,
            source_map=source_map,
            live_values=(
                LiveValueSpec(
                    "effects",
                    LiveValueKind.PYTHON_OBJECT,
                    borrowed=True,
                ),
                LiveValueSpec("value", LiveValueKind.INT64),
            ),
            proof_complete=True,
        )
        effects = []

        def compiled_continuation(_input, _output):
            effects.append(("compiled_prefix", 7))
            return _udf_build_continuation_payload(
                CONTINUATION_ABI_VERSION,
                "python_region",
                resume_id,
                identity.namespace_sha256,
                identity.code_sha256,
                identity.first_line,
                astuple(source_map),
                (effects, 7),
                ("python_object", "int64"),
                (False, False),
                (True, True),
                None,
                True,
            )

        if not _udf_register_continuation_code(compiled_continuation):
            raise AssertionError("continuation probe registration rejected")
        if not cinderx.jit.force_compile(compiled_continuation):
            raise AssertionError("continuation probe did not JIT compile")
        if not cinderx.jit.is_jit_compiled(compiled_continuation):
            raise AssertionError("continuation probe lacks JIT evidence")

        continuation_registry = CapabilityRegistry(
            epoch=f"{cluster_epoch}:continuation",
        )
        continuation_input = continuation_registry.register(
            LocalScalarSlotBackend()
        )
        continuation_output = continuation_registry.register(
            LocalScalarSlotBackend()
        )
        continuation_boundary = CommitBoundary()
        try:
            continuation_result = ScalarExecutor(
                continuation_registry
            ).execute_guarded(
                compiled_continuation,
                continuation_input,
                continuation_output,
                1.0,
                boundary=continuation_boundary,
                continuation=InterpreterContinuation(
                    contract,
                    _continuation_suffix,
                ),
            )
        finally:
            continuation_registry.release(continuation_output)
            continuation_registry.release(continuation_input)
        expected_effects = [
            ("compiled_prefix", 7),
            ("suffix", 7),
        ]
        if continuation_result != 8 or effects != expected_effects:
            raise AssertionError(
                "continuation did not resume the suffix exactly once"
            )
        with open("/proc/self/maps", encoding="ascii") as mappings:
            rwx_mapping_count = sum(
                1
                for line in mappings
                if _mapping_is_writable_and_executable(line)
            )
        if rwx_mapping_count:
            raise AssertionError(
                f"worker process contains {rwx_mapping_count} RWX mappings"
            )

        return {
            "node_id": node_id,
            "actor_id": actor_id,
            "worker_id": worker_id,
            "pid": pid,
            "process_generation": process_generation,
            "scalar_types": reports,
            "branch_results": branch_results,
            "branch_counts": branch_counts,
            "continuation_result": continuation_result,
            "continuation_effects": effects,
            "continuation_jit_compiled": True,
            "rwx_mapping_count": rwx_mapping_count,
        }


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 three-node Ray candidate cluster",
)
class RayCinderXScalarSlotSmokeTests(unittest.TestCase):
    def test_both_workers_execute_full_cinderx_scalar_matrix(self) -> None:
        import ray
        from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

        cluster_epoch = os.environ.get("UDFJIT_CLUSTER_EPOCH", "")
        self.assertTrue(cluster_epoch, "UDFJIT_CLUSTER_EPOCH is required")
        ray.init(address="auto")
        actors = []
        try:
            alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
            self.assertEqual(len(alive_nodes), 3)
            head_nodes = [
                node
                for node in alive_nodes
                if node.get("NodeName") == "ray-head-driver"
            ]
            self.assertEqual(len(head_nodes), 1)
            self.assertEqual(head_nodes[0].get("Resources", {}).get("CPU", 0), 0)

            worker_nodes = sorted(
                (
                    node
                    for node in alive_nodes
                    if node.get("NodeName") in {"ray-worker-1", "ray-worker-2"}
                ),
                key=lambda node: node["NodeName"],
            )
            self.assertEqual(
                [node["NodeName"] for node in worker_nodes],
                ["ray-worker-1", "ray-worker-2"],
            )

            remote_probe = ray.remote(num_cpus=1)(_WorkerScalarSlotProbe)
            result_refs = []
            for node in worker_nodes:
                node_id = node["NodeID"]
                actor = remote_probe.options(
                    scheduling_strategy=NodeAffinitySchedulingStrategy(
                        node_id=node_id,
                        soft=False,
                    )
                ).remote()
                actors.append(actor)
                result_refs.append(actor.run.remote(cluster_epoch))

            reports = ray.get(result_refs)
            self.assertEqual(
                {report["node_id"] for report in reports},
                {node["NodeID"] for node in worker_nodes},
            )
            for report in reports:
                self.assertGreater(report["pid"], 0)
                self.assertTrue(report["actor_id"])
                self.assertTrue(report["worker_id"])
                self.assertTrue(report["process_generation"])
                scalar_types = report["scalar_types"]
                self.assertEqual(
                    {
                        item["scalar_type"]
                        for item in scalar_types
                    },
                    {
                        "bool",
                        "int32",
                        "int64",
                        "float32",
                        "float64",
                    },
                )
                self.assertEqual(
                    len(
                        {
                            item["code_hash"]
                            for item in scalar_types
                        }
                    ),
                    5,
                )
                self.assertEqual(report["branch_results"], [9, 7])
                self.assertGreaterEqual(
                    report["branch_counts"].get("CondBranch", 0),
                    1,
                )
                self.assertEqual(report["continuation_result"], 8)
                self.assertEqual(
                    report["continuation_effects"],
                    [
                        ("compiled_prefix", 7),
                        ("suffix", 7),
                    ],
                )
                self.assertIs(
                    report["continuation_jit_compiled"],
                    True,
                )
                self.assertEqual(report["rwx_mapping_count"], 0)
            output_path = os.environ.get("UDFJIT_READINESS_REPORT_PATH", "")
            if output_path:
                manifest_path = Path(os.environ["UDFJIT_MANIFEST_PATH"])
                document = {
                    "schema_version": 1,
                    "phase": "readiness",
                    "run_id": os.environ["UDFJIT_RUN_ID"],
                    "cluster_epoch": cluster_epoch,
                    "manifest_sha256": hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest(),
                    "readiness": [
                        {
                            "node_id": report["node_id"],
                            "manifest_sha256": hashlib.sha256(
                                manifest_path.read_bytes()
                            ).hexdigest(),
                            "cinderx_compiled": True,
                            "process_generation": report["process_generation"],
                        }
                        for report in reports
                    ],
                }
                path = Path(output_path)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    json.dump(
                        document,
                        stream,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
        finally:
            for actor in actors:
                ray.kill(actor, no_restart=True)
            ray.shutdown()


if __name__ == "__main__":
    unittest.main()
