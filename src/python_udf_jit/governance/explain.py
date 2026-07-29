from __future__ import annotations

import hashlib
from collections import Counter
from typing import Iterable

from python_udf_jit.governance.telemetry import GovernanceEvent


_STAGES = (
    "adapter",
    "capture",
    "ir",
    "artifact",
    "layout",
    "variant",
    "execute",
)


def source_identity(module: str, function: str, code_sha256: str) -> str:
    payload = f"{module}\0{function}\0{code_sha256}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_explain_report(
    events: Iterable[GovernanceEvent],
    *,
    run_id: str,
    job_id: str,
    tenant_id: str,
    policy_sha256: str,
) -> dict[str, object]:
    selected = [
        event
        for event in events
        if event.run_id == run_id
        and event.job_id == job_id
        and event.tenant_id == tenant_id
        and event.policy_sha256 == policy_sha256
    ]
    counts = Counter(
        (event.stage, event.decision, event.reason_code)
        for event in selected
    )
    return {
        "schema_version": 1,
        "run_id": run_id,
        "job_id": job_id,
        "tenant_id": tenant_id,
        "policy_sha256": policy_sha256,
        "stages": [
            {
                "stage": stage,
                "events": [
                    {
                        "count": sum(
                            event.count
                            for event in selected
                            if (
                                event.stage,
                                event.decision,
                                event.reason_code,
                            )
                            == key
                        ),
                        "decision": key[1],
                        "reason_code": key[2],
                    }
                    for key in sorted(counts)
                    if key[0] == stage
                ],
            }
            for stage in _STAGES
        ],
        "dropped_business_values": True,
    }
