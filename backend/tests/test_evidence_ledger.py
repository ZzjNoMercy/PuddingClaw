from __future__ import annotations

from harness.evidence_ledger import (
    migrate_legacy_refs,
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
