from __future__ import annotations

from harness.evidence_ledger import (
    migrate_legacy_refs,
    repair_legacy_validation_wrapper_records,
    register_activation_evidence,
    resolve_evidence_ref,
)


def _run() -> dict:
    return {
        "run_id": "run-a",
        "query_id": "query-a",
        "goal_id": "goal-a",
        "goal_revision": 2,
        "verification_activations": [],
    }


def _activation() -> dict:
    return {
        "activation_id": "activation-a",
        "status": "succeeded",
        "pack": "analytics",
        "tool_call_id": "call-db",
        "tool_name": "database_sql_execute",
        "evidence_refs": [
            {
                "kind": "tool_result",
                "tool_call_id": "call-db",
                "output_digest": "sha256:output",
            },
            {
                "kind": "analytics_result",
                "tool_call_id": "call-db",
                "result_id": "result-1",
                "query_trace_id": "trace-1",
                "generation_id": "generation-1",
                "output_digest": "sha256:output",
            },
        ],
    }


def _data() -> tuple[dict, dict, dict]:
    run = _run()
    activation = _activation()
    data = {"harness": {"runs": {run["run_id"]: run}}}
    activation["stable_evidence_refs"] = register_activation_evidence(
        data,
        run=run,
        activation=activation,
    )
    run["verification_activations"].append(activation)
    return data, run, activation


def test_analytics_ref_resolves_to_authoritative_cross_run_lineage():
    data, run, activation = _data()
    analytics_ref = next(
        ref
        for ref in activation["stable_evidence_refs"]
        if ref["type"] == "analytics_result"
    )

    resolved = resolve_evidence_ref(
        data,
        analytics_ref,
        goal_id="goal-a",
        goal_revision=2,
    )

    assert resolved is not None
    assert resolved.source_run_id == run["run_id"]
    assert resolved.source_query_id == run["query_id"]
    assert resolved.origin_tool_call_id == "call-db"
    assert resolved.query_trace_id == "trace-1"
    assert resolved.generation_id == "generation-1"
    assert resolved.result_id == "result-1"


def test_forged_or_wrong_revision_ref_is_rejected():
    data, _, activation = _data()
    analytics_ref = next(
        ref
        for ref in activation["stable_evidence_refs"]
        if ref["type"] == "analytics_result"
    )

    assert resolve_evidence_ref(
        data,
        {"type": "analytics_result", "id": "does-not-exist"},
    ) is None
    assert resolve_evidence_ref(
        data,
        analytics_ref,
        goal_id="goal-a",
        goal_revision=3,
    ) is None


def test_duplicate_identity_is_deduplicated_without_copying_payload():
    data, run, activation = _data()

    again = register_activation_evidence(
        data,
        run=run,
        activation=activation,
    )

    assert again == activation["stable_evidence_refs"]
    assert len(data["evidence_ledger"]) == 2
    assert all(set(ref) == {"type", "id"} for ref in again)


def test_legacy_complete_evidence_migrates_and_incomplete_is_audited():
    data, run, activation = _data()
    activation["stable_evidence_refs"] = []
    data["evidence_ledger"] = {}

    migrated = migrate_legacy_refs(
        data,
        [
            {
                "kind": "analytics_result",
                "origin_run_id": run["run_id"],
                "tool_call_id": "call-db",
                "result_id": "result-legacy",
                "query_trace_id": "trace-legacy",
                "output_digest": "sha256:legacy",
            },
            {
                "kind": "analytics_result",
                "result_id": "missing-origin",
            },
        ],
        goal_id="goal-a",
        goal_revision=2,
    )

    assert migrated == [
        {"type": "analytics_result", "id": "result-legacy"}
    ]
    resolved = resolve_evidence_ref(
        data,
        migrated[0],
        goal_id="goal-a",
        goal_revision=2,
    )
    assert resolved is not None
    assert resolved.query_trace_id == "trace-legacy"
    assert data["evidence_migration_audit"][-1]["reason"] == (
        "legacy_origin_lineage_incomplete"
    )


def test_mutation_wrapper_and_nested_validation_keep_distinct_identities():
    run = _run()
    validation = {
        "kind": "validation_receipt",
        "validation_receipt_id": "validation-1",
        "tool_call_id": "call-write",
        "artifact_refs": [
            {
                "artifact_id": "artifact-1",
                "path": "/external/report.js",
                "content_sha256": "sha256:bytes",
            }
        ],
        "output_digest": "sha256:validation",
        "material": True,
    }
    wrapper = {
        "kind": "external_mutation_completed",
        "receipt_id": "mutation-1",
        "validation_receipt_id": "validation-1",
        "tool_call_id": "call-write",
        "output_digest": "sha256:mutation",
        "material": True,
    }
    activation = {
        "activation_id": "activation-write",
        "status": "succeeded",
        "pack": "code",
        "tool_call_id": "call-write",
        "tool_name": "patch_file",
        "evidence_refs": [wrapper, validation],
    }
    data = {"harness": {"runs": {run["run_id"]: run}}}
    activation["stable_evidence_refs"] = register_activation_evidence(
        data,
        run=run,
        activation=activation,
    )
    run["verification_activations"].append(activation)

    assert {tuple(sorted(item.items())) for item in activation["stable_evidence_refs"]} == {
        tuple(sorted({"type": "external_mutation", "id": "mutation-1"}.items())),
        tuple(sorted({"type": "validation_receipt", "id": "validation-1"}.items())),
    }
    validation_record = resolve_evidence_ref(
        data,
        {"type": "validation_receipt", "id": "validation-1"},
    )
    assert validation_record is not None
    assert validation_record.inheritable is True
    assert validation_record.payload["artifact_refs"][0]["artifact_id"] == "artifact-1"


def test_legacy_mutation_wrapper_collision_is_repaired_from_activation():
    run = _run()
    activation = {
        "activation_id": "activation-write",
        "status": "succeeded",
        "pack": "code",
        "tool_call_id": "call-write",
        "tool_name": "patch_file",
        "evidence_refs": [
            {
                "kind": "external_mutation_completed",
                "receipt_id": "mutation-legacy",
                "validation_receipt_id": "validation-legacy",
                "tool_call_id": "call-write",
                "output_digest": "sha256:mutation",
                "material": True,
            },
            {
                "kind": "validation_receipt",
                "validation_receipt_id": "validation-legacy",
                "tool_call_id": "call-write",
                "artifact_refs": [
                    {
                        "artifact_id": "artifact-legacy",
                        "path": "/external/report.js",
                        "content_sha256": "sha256:bytes",
                    }
                ],
                "output_digest": "sha256:validation",
                "material": True,
            },
        ],
        "stable_evidence_refs": [
            {"type": "validation_receipt", "id": "validation-legacy"}
        ],
    }
    run["verification_activations"].append(activation)
    data = {
        "harness": {"runs": {run["run_id"]: run}},
        "evidence_ledger": {
            "validation_receipt:validation-legacy": {
                "id": "validation-legacy",
                "kind": "validation_receipt",
                "source_run_id": run["run_id"],
                "source_query_id": run["query_id"],
                "origin_tool_call_id": "call-write",
                "origin_tool_name": "patch_file",
                "verification_pack": "code",
                "goal_id": run["goal_id"],
                "goal_revision": run["goal_revision"],
                "output_digest": "sha256:mutation",
                "validation_receipt_id": "validation-legacy",
                "receipt_id": "mutation-legacy",
                "status": "active",
                "inheritable": False,
                "non_inheritable_reason": "validation_artifact_binding_missing",
                "payload": activation["evidence_refs"][0],
                "created_at": 1.0,
            }
        },
    }
    activation["evidence_refs"].extend(
        [
            {
                "kind": "external_mutation_completed",
                "receipt_id": "mutation-legacy-2",
                "validation_receipt_id": "validation-legacy-2",
                "tool_call_id": "call-write",
                "output_digest": "sha256:mutation-2",
                "material": True,
            },
            {
                "kind": "validation_receipt",
                "validation_receipt_id": "validation-legacy-2",
                "tool_call_id": "call-write",
                "artifact_refs": [
                    {
                        "artifact_id": "artifact-legacy-2",
                        "path": "/external/report-2.js",
                        "content_sha256": "sha256:bytes-2",
                    }
                ],
                "output_digest": "sha256:validation-2",
                "material": True,
            },
        ]
    )
    activation["stable_evidence_refs"].append(
        {"type": "validation_receipt", "id": "validation-legacy-2"}
    )
    second_collision = {
        **data["evidence_ledger"]["validation_receipt:validation-legacy"],
        "id": "validation-legacy-2",
        "output_digest": "sha256:mutation-2",
        "validation_receipt_id": "validation-legacy-2",
        "receipt_id": "mutation-legacy-2",
        "payload": activation["evidence_refs"][-2],
    }
    data["evidence_ledger"][
        "validation_receipt:validation-legacy-2"
    ] = second_collision

    assert repair_legacy_validation_wrapper_records(data) is True
    repaired = resolve_evidence_ref(
        data,
        {"type": "validation_receipt", "id": "validation-legacy"},
    )
    assert repaired is not None
    assert repaired.inheritable is True
    assert repaired.payload["kind"] == "validation_receipt"
    assert "external_mutation:mutation-legacy" in data["evidence_ledger"]
    assert "external_mutation:mutation-legacy-2" in data["evidence_ledger"]
    repaired_second = resolve_evidence_ref(
        data,
        {"type": "validation_receipt", "id": "validation-legacy-2"},
    )
    assert repaired_second is not None
    assert repaired_second.payload["artifact_refs"][0]["artifact_id"] == (
        "artifact-legacy-2"
    )
