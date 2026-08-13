from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.evaluation as evaluation_api
from evaluation.contracts import (
    EvalCase,
    EvalDataset,
    EvalError,
    EvalExpectations,
    EvalExperiment,
    EvalInput,
    ExperimentCandidate,
)
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


def test_experiment_results_expose_live_attempt_error_and_timing(tmp_path: Path, monkeypatch):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    experiment = repository.create_experiment(
        EvalExperiment(
            name="Live results",
            dataset_id="ds-live",
            dataset_version=1,
            dataset_version_id="version-live",
            dataset_content_hash="a" * 64,
            candidate=ExperimentCandidate(name="agent"),
            status="running",
        )
    )
    attempt_id = repository.create_attempt(experiment.experiment_id, "case-first", 0)
    error = EvalError(code="case_execution_failed", message="Docker setup failed", retryable=False)
    repository.finish_attempt(attempt_id, status="failed", error=error)
    monkeypatch.setattr(evaluation_api, "get_evaluation_repository", lambda: repository)
    app = FastAPI()
    app.include_router(evaluation_api.router, prefix="/api")

    response = TestClient(app).get(f"/api/evaluation/experiments/{experiment.experiment_id}/results")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["case_id"] == "case-first"
    assert item["attempt_status"] == "failed"
    assert item["error"]["message"] == "Docker setup failed"
    assert item["created_at"]
    assert item["updated_at"]


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


def test_experiment_delete_api_rejects_running_and_deletes_terminal(tmp_path: Path, monkeypatch):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    running = repository.create_experiment(
        EvalExperiment(
            name="Still running",
            dataset_id="ds-api-delete",
            dataset_version=1,
            dataset_version_id="version-api-delete",
            dataset_content_hash="b" * 64,
            candidate=ExperimentCandidate(name="agent"),
            status="running",
        )
    )
    failed = repository.create_experiment(
        running.model_copy(
            update={
                "experiment_id": "exp-api-delete-failed",
                "name": "Failed run",
                "status": "failed",
            }
        )
    )

    class WorkerManager:
        async def delete_artifacts(self, experiment_id: str) -> bool:
            assert experiment_id == failed.experiment_id
            return True

    monkeypatch.setattr(evaluation_api, "get_evaluation_repository", lambda: repository)
    monkeypatch.setattr(evaluation_api, "evaluation_worker_manager", WorkerManager())
    app = FastAPI()
    app.include_router(evaluation_api.router, prefix="/api")
    client = TestClient(app)

    active_response = client.delete(f"/api/evaluation/experiments/{running.experiment_id}")
    assert active_response.status_code == 409

    deleted_response = client.delete(f"/api/evaluation/experiments/{failed.experiment_id}")
    assert deleted_response.status_code == 204
    assert client.get(f"/api/evaluation/experiments/{failed.experiment_id}").status_code == 404


def test_completed_experiment_can_be_retried(tmp_path: Path, monkeypatch):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    draft = repository.create_dataset(
        EvalDataset(
            name="Retry completed",
            cases=[
                EvalCase(
                    name="Case",
                    input=EvalInput(message="hello"),
                    expectations=EvalExpectations(contains_all=["world"]),
                )
            ],
        )
    )
    bundle = repository.publish_dataset(draft.dataset_id, draft.revision)
    completed = repository.create_experiment(
        EvalExperiment(
            name="Completed run (retry) (retry)",
            dataset_id=draft.dataset_id,
            dataset_version=1,
            dataset_version_id=bundle.version_id,
            dataset_content_hash=bundle.checksum,
            candidate=ExperimentCandidate(name="agent"),
            status="completed",
        )
    )
    started: list[str] = []

    class WorkerManager:
        async def start(self, experiment_id: str):
            started.append(experiment_id)

    monkeypatch.setattr(evaluation_api, "get_evaluation_repository", lambda: repository)
    monkeypatch.setattr(evaluation_api, "evaluation_worker_manager", WorkerManager())
    app = FastAPI()
    app.include_router(evaluation_api.router, prefix="/api")

    response = TestClient(app).post(f"/api/evaluation/experiments/{completed.experiment_id}/retry")

    assert response.status_code == 202
    retried = response.json()
    assert retried["status"] == "queued"
    assert retried["experiment_id"] != completed.experiment_id
    assert retried["name"] == "Completed run"
    assert retried["candidate"]["candidate_id"] != completed.candidate.candidate_id
    assert retried["candidate"]["config"]["runtime_hash"]
    assert retried["summary"]["retry_of_experiment_id"] == completed.experiment_id
    assert retried["summary"]["retry_root_experiment_id"] == completed.experiment_id
    assert retried["summary"]["retry_generation"] == 1
    assert started == [retried["experiment_id"]]

    first_retry = repository.get_experiment(retried["experiment_id"])
    repository.update_experiment(
        first_retry.model_copy(update={"status": "failed"}),
        expected_status="queued",
    )
    second_response = TestClient(app).post(
        f"/api/evaluation/experiments/{retried['experiment_id']}/retry"
    )
    assert second_response.status_code == 202
    second_retry = second_response.json()
    assert second_retry["name"] == "Completed run"
    assert second_retry["summary"]["retry_root_experiment_id"] == completed.experiment_id
    assert second_retry["summary"]["retry_generation"] == 2
