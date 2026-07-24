from __future__ import annotations

import asyncio
import hashlib
import os
import unittest


class _NestedObjectRefProbe:
    async def resolve(self, refs: list[object]) -> dict[str, object]:
        import ray

        values = await asyncio.gather(*refs)
        payload = values[0]
        return {
            "digest": hashlib.sha256(payload).hexdigest(),
            "node_id": ray.get_runtime_context().get_node_id(),
            "size_bytes": len(payload),
        }


@unittest.skipUnless(
    os.environ.get("UDFJIT_LIVE_RAY") == "1",
    "requires the blue-98 three-node Ray candidate cluster",
)
class RayObjectStoreDataPlaneTests(unittest.TestCase):
    def test_head_owned_object_ref_reaches_both_workers(self) -> None:
        import ray
        from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

        ray.init(address="auto")
        actors = []
        try:
            alive_nodes = [node for node in ray.nodes() if node.get("Alive")]
            head = [
                node
                for node in alive_nodes
                if node.get("NodeName") == "ray-head-driver"
            ]
            self.assertEqual(len(head), 1)
            self.assertEqual(head[0].get("Resources", {}).get("CPU", 0), 0)
            workers = sorted(
                (
                    node
                    for node in alive_nodes
                    if node.get("NodeName") in {"ray-worker-1", "ray-worker-2"}
                ),
                key=lambda node: node["NodeName"],
            )
            self.assertEqual(
                [node["NodeName"] for node in workers],
                ["ray-worker-1", "ray-worker-2"],
            )

            payload = b"udfjit-object-store-data-plane:" + b"x" * (2 * 1024 * 1024)
            expected_digest = hashlib.sha256(payload).hexdigest()
            payload_ref = ray.put(payload)
            remote_probe = ray.remote(num_cpus=0)(_NestedObjectRefProbe)
            result_refs = []
            for worker in workers:
                actor = remote_probe.options(
                    scheduling_strategy=NodeAffinitySchedulingStrategy(
                        node_id=worker["NodeID"],
                        soft=False,
                    )
                ).remote()
                actors.append(actor)
                result_refs.append(actor.resolve.remote([payload_ref]))

            reports = ray.get(result_refs, timeout=30)
            self.assertEqual(
                {report["node_id"] for report in reports},
                {worker["NodeID"] for worker in workers},
            )
            for report in reports:
                self.assertEqual(report["digest"], expected_digest)
                self.assertEqual(report["size_bytes"], len(payload))
        finally:
            for actor in actors:
                ray.kill(actor, no_restart=True)
            ray.shutdown()


if __name__ == "__main__":
    unittest.main()
