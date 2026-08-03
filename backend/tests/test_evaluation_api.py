from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.evaluation as evaluation_api
from evaluation.contracts import EvalCase, EvalExpectations, EvalInput
from evaluation.repository import EvaluationRepository


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
