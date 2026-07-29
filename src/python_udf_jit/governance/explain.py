from __future__ import annotations

import hashlib
from collections import Counter
from typing import Iterable

from python_udf_jit.governance.telemetry import (
    GOVERNANCE_STAGES,
    GovernanceEvent,
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
    scope = GovernanceEvent(
        run_id=run_id,
        job_id=job_id,
        tenant_id=tenant_id,
        policy_sha256=policy_sha256,
        stage="adapter",
        decision="discover",
        reason_code="candidate_discovered",
        source_identity="0" * 64,
        count=0,
    )
    selected = [
        event
        for event in events
        if event.run_id == scope.run_id
        and event.job_id == scope.job_id
        and event.tenant_id == scope.tenant_id
        and event.policy_sha256 == scope.policy_sha256
    ]
    counts: Counter[
        tuple[str, str, str, str, str | None, str | None]
    ] = Counter()
    for event in selected:
        counts[
            (
                event.stage,
                event.decision,
                event.reason_code,
                event.source_identity,
                event.artifact_sha256,
                event.variant_sha256,
            )
        ] += event.count
    return {
        "schema_version": 1,
        "run_id": scope.run_id,
        "job_id": scope.job_id,
        "tenant_id": scope.tenant_id,
        "policy_sha256": scope.policy_sha256,
        "stages": [
            {
                "stage": stage,
                "events": [
                    {
                        "artifact_sha256": key[4],
                        "count": count,
                        "decision": key[1],
                        "reason_code": key[2],
                        "source_identity": key[3],
                        "variant_sha256": key[5],
                    }
                    for key, count in sorted(
                        counts.items(),
                        key=lambda item: tuple(
                            "" if value is None else value
                            for value in item[0]
                        ),
                    )
                    if key[0] == stage
                ],
            }
            for stage in GOVERNANCE_STAGES
        ],
        "dropped_business_values": True,
    }


def decode_explain_report(document: object) -> dict[str, object]:
    """Strictly decode and canonically rebuild a value-free Explain report."""

    root_fields = {
        "schema_version",
        "run_id",
        "job_id",
        "tenant_id",
        "policy_sha256",
        "stages",
        "dropped_business_values",
    }
    if (
        not isinstance(document, dict)
        or set(document) != root_fields
        or document["schema_version"] != 1
        or document["dropped_business_values"] is not True
        or not isinstance(document["stages"], list)
    ):
        raise ValueError("explain_report_invalid")

    events: list[GovernanceEvent] = []
    for expected_stage, stage_document in zip(
        GOVERNANCE_STAGES,
        document["stages"],
        strict=True,
    ):
        if (
            not isinstance(stage_document, dict)
            or set(stage_document) != {"stage", "events"}
            or stage_document["stage"] != expected_stage
            or not isinstance(stage_document["events"], list)
        ):
            raise ValueError("explain_report_invalid")
        for event_document in stage_document["events"]:
            if (
                not isinstance(event_document, dict)
                or set(event_document)
                != {
                    "artifact_sha256",
                    "count",
                    "decision",
                    "reason_code",
                    "source_identity",
                    "variant_sha256",
                }
            ):
                raise ValueError("explain_report_invalid")
            try:
                event = GovernanceEvent(
                    run_id=document["run_id"],
                    job_id=document["job_id"],
                    tenant_id=document["tenant_id"],
                    policy_sha256=document["policy_sha256"],
                    stage=expected_stage,
                    decision=event_document["decision"],
                    reason_code=event_document["reason_code"],
                    source_identity=event_document["source_identity"],
                    artifact_sha256=event_document["artifact_sha256"],
                    variant_sha256=event_document["variant_sha256"],
                    count=event_document["count"],
                )
            except (TypeError, ValueError) as error:
                raise ValueError("explain_report_invalid") from error
            events.append(event)

    if len(document["stages"]) != len(GOVERNANCE_STAGES):
        raise ValueError("explain_report_invalid")
    try:
        canonical = build_explain_report(
            events,
            run_id=document["run_id"],
            job_id=document["job_id"],
            tenant_id=document["tenant_id"],
            policy_sha256=document["policy_sha256"],
        )
    except (TypeError, ValueError) as error:
        raise ValueError("explain_report_invalid") from error
    if document != canonical:
        raise ValueError("explain_report_invalid")
    return canonical
