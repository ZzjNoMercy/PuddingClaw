import asyncio
from pathlib import Path

import pytest

from evaluation.contracts import (
    EvalCase,
    EvalDataset,
    EvalExpectations,
    EvalExperiment,
    EvalInput,
    ExperimentCandidate,
)
from evaluation.repository import ConflictError, EvaluationRepository, NotFoundError, ValidationError
from evaluation.worker_manager import EvaluationWorkerManager


def _dataset() -> EvalDataset:
    return EvalDataset(
        name="Regression",
        cases=[
            EvalCase(
                name="Greeting",
                input=EvalInput(message="hello"),
                expectations=EvalExpectations(contains_all=["hello"]),
            )
        ],
    )


def test_published_snapshot_is_immutable_and_new_draft_creates_new_version(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    draft = repository.create_dataset(_dataset())
    first = repository.publish_dataset(draft.dataset_id, draft.revision)
    assert first.dataset.current_version == 1
    with pytest.raises(ConflictError):
        repository.update_case(first.dataset.dataset_id, first.dataset.cases[0].case_id, first.dataset.cases[0])

    reopened = repository.update_dataset(first.dataset.dataset_id, {"status": "draft"}, first.dataset.revision)
    edited = reopened.cases[0].model_copy(update={"description": "changed"})
    repository.update_case(reopened.dataset_id, edited.case_id, edited)
    head = repository.get_dataset(reopened.dataset_id)
    second = repository.publish_dataset(head.dataset_id, head.revision)

    assert second.dataset.current_version == 2
    assert second.version_id != first.version_id
    assert repository.get_dataset(draft.dataset_id, 1).cases[0].description == ""
    assert repository.get_dataset(draft.dataset_id, 2).cases[0].description == "changed"


def test_repository_uses_wal_and_optimistic_revision(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    dataset = repository.create_dataset(_dataset())
    with repository._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    repository.update_dataset(dataset.dataset_id, {"description": "one"}, dataset.revision)
    with pytest.raises(ConflictError):
        repository.update_dataset(dataset.dataset_id, {"description": "stale"}, dataset.revision)


def test_case_mutation_uses_compare_and_swap_across_repository_instances(tmp_path: Path):
    database = tmp_path / "evaluation.db"
    first = EvaluationRepository(database)
    second = EvaluationRepository(database)
    dataset = first.create_dataset(_dataset())
    revision = dataset.revision

    first.add_case(dataset.dataset_id, EvalCase(name="A", input=EvalInput(message="a")), revision)
    with pytest.raises(ConflictError):
        second.add_case(dataset.dataset_id, EvalCase(name="B", input=EvalInput(message="b")), revision)


def test_default_export_of_published_dataset_uses_immutable_snapshot(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    draft = repository.create_dataset(_dataset())
    published = repository.publish_dataset(draft.dataset_id, draft.revision)

    exported = repository.export_bundle(draft.dataset_id)

    assert exported.version_id == published.version_id
    assert exported.checksum == published.checksum
    assert exported.dataset.cases[0].resolved_evaluator_bindings


def test_dataset_with_only_disabled_cases_cannot_be_published(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    dataset = repository.create_dataset(
        EvalDataset(
            name="Empty execution",
            cases=[EvalCase(name="disabled", enabled=False, input=EvalInput(message="hello"))],
        )
    )

    with pytest.raises(ValidationError, match="no_enabled_cases"):
        repository.publish_dataset(dataset.dataset_id, dataset.revision)


def test_publish_rejects_plaintext_credentials_and_zero_effective_measurement(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    secret = repository.create_dataset(
        EvalDataset(
            name="Secret",
            cases=[
                EvalCase(
                    name="credential",
                    input=EvalInput(message="connect postgresql://admin:supersecret@db/app"),
                    expectations=EvalExpectations(contains_all=["ok"]),
                )
            ],
        )
    )
    with pytest.raises(ValidationError, match="plaintext_secret_detected"):
        repository.publish_dataset(secret.dataset_id, secret.revision)

    unmeasured = repository.create_dataset(
        EvalDataset(
            name="Unmeasured",
            cases=[EvalCase(name="case", input=EvalInput(message="hello"))],
        )
    )
    with pytest.raises(ValidationError, match="no_executable_evaluator"):
        repository.publish_dataset(unmeasured.dataset_id, unmeasured.revision)


def test_outbox_has_single_claimant_and_retryable_lease(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    first = repository.enqueue_outbox("langsmith", "experiment_projection", "exp-1", {"n": 1})
    duplicate = repository.enqueue_outbox("langsmith", "experiment_projection", "exp-1", {"n": 2})
    assert duplicate == first

    claimed = repository.claim_outbox("langsmith", "experiment_projection", "exp-1")
    assert claimed and claimed["outbox_id"] == first
    assert repository.claim_outbox("langsmith", "experiment_projection", "exp-1") is None

    repository.release_outbox(first, "network unavailable")
    assert repository.claim_outbox("langsmith", "experiment_projection", "exp-1") is not None
    repository.mark_outbox_delivered(first)
    with repository._connect() as connection:
        row = connection.execute(
            "SELECT status, attempts, last_error FROM eval_outbox WHERE outbox_id=?", (first,)
        ).fetchone()
    assert row["status"] == "delivered"
    assert row["attempts"] == 2
    assert row["last_error"] == "network unavailable"


def test_delete_experiment_requires_terminal_status_and_cascades_local_records(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    experiment = repository.create_experiment(
        EvalExperiment(
            name="Delete me",
            dataset_id="ds-delete",
            dataset_version=1,
            dataset_version_id="version-delete",
            dataset_content_hash="a" * 64,
            candidate=ExperimentCandidate(name="agent"),
        )
    )
    attempt_id = repository.create_attempt(experiment.experiment_id, "case-delete", 0)
    repository.enqueue_outbox("langsmith", "experiment_projection", experiment.experiment_id, {"x": 1})
    repository.save_remote_mapping(
        provider="langsmith",
        local_type="experiment",
        local_id=experiment.experiment_id,
        version_id="",
        remote_id="remote-delete",
        remote_name="remote",
        content_hash=None,
        status="synced",
    )

    with pytest.raises(ConflictError, match="terminal"):
        repository.delete_experiment(experiment.experiment_id)

    repository.update_experiment(experiment.model_copy(update={"status": "failed"}))
    repository.delete_experiment(experiment.experiment_id)

    with pytest.raises(NotFoundError):
        repository.get_experiment(experiment.experiment_id)
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM eval_case_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM eval_outbox WHERE aggregate_id=?", (experiment.experiment_id,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM eval_remote_mappings WHERE local_id=?", (experiment.experiment_id,)
        ).fetchone() is None


def test_application_shutdown_requeues_worker_and_clears_partial_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    experiment = repository.create_experiment(
        EvalExperiment(
            name="Reload recovery",
            dataset_id="ds-reload",
            dataset_version=1,
            dataset_version_id="version-reload",
            dataset_content_hash="b" * 64,
            candidate=ExperimentCandidate(name="agent"),
            status="running",
            started_at="2026-08-13T00:00:00Z",
        )
    )
    repository.create_attempt(experiment.experiment_id, "case-reload", 0)
    repository.enqueue_outbox(
        "langsmith",
        "experiment_projection",
        experiment.experiment_id,
        {"stale": True},
    )
    monkeypatch.setattr(
        "evaluation.repository.get_evaluation_repository",
        lambda: repository,
    )

    class Process:
        returncode = None

    process = Process()
    manager = EvaluationWorkerManager()
    manager._processes[experiment.experiment_id] = process  # noqa: SLF001

    async def terminate(_process):
        _process.returncode = -15

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager, "_terminate_worker_group", terminate)
    monkeypatch.setattr(manager, "_terminate_official_harness", noop)
    monkeypatch.setattr(manager, "_terminate_swebench_image_preparation", noop)

    asyncio.run(manager.stop())

    recovered = repository.get_experiment(experiment.experiment_id)
    assert recovered.status == "queued"
    assert recovered.started_at is None
    assert recovered.finished_at is None
    assert recovered.summary["application_restart_pending"] is True
    assert recovered.summary["progress"]["completed"] == 0
    assert repository.list_results(experiment.experiment_id) == []
    with repository._connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM eval_outbox WHERE aggregate_id=?",
            (experiment.experiment_id,),
        ).fetchone() is None
