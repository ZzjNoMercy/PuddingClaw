from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.evaluation as evaluation_api
from evaluation.contracts import EvalCase, EvalExpectations, EvalInput
from evaluation.repository import EvaluationRepository
from evaluation.settings import LangSmithSettings
from evaluation.swebench_adapter import swebench_dataset_from_rows


def test_dataset_api_lifecycle_and_revision_conflict(tmp_path: Path, monkeypatch):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    monkeypatch.setattr(evaluation_api, "get_evaluation_repository", lambda: repository)
    app = FastAPI()
    app.include_router(evaluation_api.router, prefix="/api")
    client = TestClient(app)

    created = client.post("/api/evaluation/datasets", json={"name": "API Dataset"})
    assert created.status_code == 201
    dataset = created.json()
    assert dataset["status"] == "draft"
    assert created.headers["etag"] == 'W/"1"'

    case = EvalCase(
        name="Case",
        input=EvalInput(message="hello"),
        expectations=EvalExpectations(contains_all=["world"]),
    )
    added = client.post(
        f"/api/evaluation/datasets/{dataset['dataset_id']}/cases",
        json={"expected_revision": dataset["revision"], "case": case.model_dump(mode="json")},
    )
    assert added.status_code == 201
    dataset = added.json()
    assert len(dataset["cases"]) == 1

    stale = client.patch(
        f"/api/evaluation/datasets/{dataset['dataset_id']}",
        json={"expected_revision": 1, "description": "stale"},
    )
    assert stale.status_code == 409

    validation = client.post(f"/api/evaluation/datasets/{dataset['dataset_id']}/validate")
    assert validation.status_code == 200
    assert validation.json()["valid"] is True
    published = client.post(
        f"/api/evaluation/datasets/{dataset['dataset_id']}/publish",
        json={"expected_revision": dataset["revision"]},
    )
    assert published.status_code == 200
    assert published.json()["dataset"]["current_version"] == 1


def test_langsmith_connection_can_be_tested_before_projection_is_enabled(tmp_path: Path, monkeypatch):
    repository = EvaluationRepository(tmp_path / "evaluation.db")

    class Store:
        def load(self):
            return LangSmithSettings(enabled=False, api_key="lsv2_test")

    class Adapter:
        def __init__(self, repository, settings):
            assert settings.enabled is False
            assert settings.api_key

        def test_connection(self):
            return {"ok": True, "dataset_access": True}

    monkeypatch.setattr(evaluation_api, "get_evaluation_repository", lambda: repository)
    monkeypatch.setattr(evaluation_api, "get_evaluation_settings_store", lambda: Store())
    monkeypatch.setattr(evaluation_api, "LangSmithDatasetAdapter", Adapter)
    app = FastAPI()
    app.include_router(evaluation_api.router, prefix="/api")

    response = TestClient(app).post("/api/evaluation/settings/langsmith/test")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "dataset_access": True,
        "projection_enabled": False,
    }


def test_swebench_import_api_uses_coding_profile_and_omits_gold(tmp_path: Path, monkeypatch):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    monkeypatch.setattr(evaluation_api, "get_evaluation_repository", lambda: repository)
    monkeypatch.setattr(
        evaluation_api,
        "fetch_swebench_rows",
        lambda **kwargs: [
            {
                "instance_id": "pytest-dev__pytest-1234",
                "repo": "pytest-dev/pytest",
                "base_commit": "b" * 40,
                "problem_statement": "Fix collection",
                "patch": "SECRET_GOLD_PATCH",
                "test_patch": "diff --git a/testing/test_x.py b/testing/test_x.py",
                "FAIL_TO_PASS": "[]",
                "PASS_TO_PASS": "[]",
            }
        ],
    )
    app = FastAPI()
    app.include_router(evaluation_api.router, prefix="/api")

    response = TestClient(app).post(
        "/api/evaluation/datasets/import/swebench",
        json={"limit": 1, "name": "SWE sample"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["default_profile"] == "coding_agent@1"
    assert payload["cases"][0]["code"]["repository"]["kind"] == "swebench"
    assert "SECRET_GOLD_PATCH" not in response.text


def test_manual_swebench_result_import_is_retired(tmp_path: Path, monkeypatch):
    from evaluation.contracts import (
        AgentRunEnvelope,
        EvalExperiment,
        ExperimentCandidate,
    )

    repository = EvaluationRepository(tmp_path / "evaluation.db")
    draft = repository.create_dataset(
        swebench_dataset_from_rows(
            [
                {
                    "instance_id": "pytest-dev__pytest-1234",
                    "repo": "pytest-dev/pytest",
                    "base_commit": "b" * 40,
                    "problem_statement": "Fix collection",
                    "test_patch": "diff --git a/testing/test_x.py b/testing/test_x.py",
                    "FAIL_TO_PASS": "[]",
                    "PASS_TO_PASS": "[]",
                }
            ],
            name="SWE score",
        )
    )
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    experiment = repository.create_experiment(
        EvalExperiment(
            name="SWE experiment",
            dataset_id=draft.dataset_id,
            dataset_version=1,
            dataset_version_id=str(bundle.version_id),
            dataset_content_hash=str(bundle.checksum),
            candidate=ExperimentCandidate(name="model"),
            profile_id="coding_agent@1",
            status="completed",
            verdict="indeterminate",
            summary={"failed_attempts": 0, "swebench_predictions_available": True},
        )
    )
    case = bundle.dataset.cases[0]
    attempt_id = repository.create_attempt(experiment.experiment_id, case.case_id, 0)
    repository.finish_attempt(
        attempt_id,
        status="completed",
        run=AgentRunEnvelope(
            case_id=case.case_id,
            experiment_id=experiment.experiment_id,
            candidate_id=experiment.candidate.candidate_id,
            session_id="swe-session",
            response="patched",
            metadata={
                "code_verification": {
                    "mode": "swebench",
                    "status": "not_evaluated",
                    "patch": "diff --git a/a.py b/a.py",
                }
            },
        ),
    )
    monkeypatch.setattr(evaluation_api, "get_evaluation_repository", lambda: repository)
    app = FastAPI()
    app.include_router(evaluation_api.router, prefix="/api")

    client = TestClient(app)
    response = client.post(
        f"/api/evaluation/experiments/{experiment.experiment_id}/results/swebench",
        json={
            "content": '{"pytest-dev__pytest-1234":{"resolved":true}}',
            "manifest": {},
        },
    )

    assert response.status_code == 410
    assert "Evaluation Worker" in response.json()["detail"]
