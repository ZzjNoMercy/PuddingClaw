from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.contracts import EvalCase, EvalDataset, EvalExpectations, EvalInput
from evaluation.langsmith_backend import LangSmithDatasetAdapter, _redact, langsmith_client_kwargs
from evaluation.repository import EvaluationRepository
from evaluation.settings import LangSmithSettings


class FakeClient:
    def __init__(self):
        self.datasets = []
        self.examples = []

    def list_datasets(self, **kwargs):
        metadata = kwargs.get("metadata")
        if metadata:
            return [item for item in self.datasets if all(item.metadata.get(key) == value for key, value in metadata.items())]
        return self.datasets[: kwargs.get("limit")]

    def create_dataset(self, name, **kwargs):
        item = SimpleNamespace(id=f"remote-{len(self.datasets) + 1}", name=name, metadata=kwargs.get("metadata") or {})
        self.datasets.append(item)
        return item

    def read_dataset(self, *, dataset_id):
        return next(item for item in self.datasets if item.id == dataset_id)

    def create_examples(self, **kwargs):
        self.examples.extend(kwargs["examples"])
        return {}


def test_langsmith_dataset_sync_is_idempotent_and_redacted(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    dataset = repository.create_dataset(
        EvalDataset(
            name="Safe",
            cases=[
                EvalCase(
                    name="redaction",
                    input=EvalInput(
                        message="Email alice@example.com; inspect /Users/alice/private/report.txt"
                    ),
                    expectations=EvalExpectations(contains_all=["ok"]),
                )
            ],
        )
    )
    bundle = repository.publish_dataset(dataset.dataset_id, dataset.revision)
    client = FakeClient()
    adapter = LangSmithDatasetAdapter(repository, LangSmithSettings(enabled=True, api_key="secret"), client=client)
    first = adapter.sync_dataset(bundle)
    second = adapter.sync_dataset(bundle)
    assert first["status"] == "synced"
    assert second["idempotent"] is True
    assert len(client.datasets) == 1
    assert len(client.examples) == 1
    assert "alice@example.com" not in str(client.examples)
    assert "/Users/alice/private/report.txt" not in str(client.examples)


def test_langsmith_sync_recreates_a_missing_remote_dataset(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    dataset = repository.create_dataset(
        EvalDataset(
            name="Recover",
            cases=[
                EvalCase(
                    name="case",
                    input=EvalInput(message="hello"),
                    expectations=EvalExpectations(contains_all=["hello"]),
                )
            ],
        )
    )
    bundle = repository.publish_dataset(dataset.dataset_id, dataset.revision)
    client = FakeClient()
    adapter = LangSmithDatasetAdapter(
        repository, LangSmithSettings(enabled=True, api_key="secret"), client=client
    )
    first = adapter.sync_dataset(bundle)
    client.datasets.clear()
    second = adapter.sync_dataset(bundle)

    assert first["status"] == "synced"
    assert second["idempotent"] is False
    assert len(client.datasets) == 1


def test_restricted_dataset_is_rejected_before_remote_call(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    dataset = repository.create_dataset(
        EvalDataset(
            name="Restricted",
            cases=[
                EvalCase(
                    name="secret",
                    input=EvalInput(message="secret"),
                    expectations=EvalExpectations(contains_all=["x"]),
                    data_classification="restricted",
                )
            ],
        )
    )
    bundle = repository.publish_dataset(dataset.dataset_id, dataset.revision)
    client = FakeClient()
    adapter = LangSmithDatasetAdapter(repository, LangSmithSettings(enabled=True, api_key="secret"), client=client)
    try:
        adapter.sync_dataset(bundle)
    except ValueError as exc:
        assert "Restricted" in str(exc)
    else:
        raise AssertionError("restricted Dataset should be blocked")
    assert not client.datasets


def test_sensitive_dataset_is_fail_closed_before_remote_call(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    dataset = repository.create_dataset(
        EvalDataset(
            name="Sensitive",
            cases=[
                EvalCase(
                    name="pii",
                    input=EvalInput(message="customer@example.com"),
                    expectations=EvalExpectations(contains_all=["ok"]),
                    data_classification="sensitive",
                )
            ],
        )
    )
    bundle = repository.publish_dataset(dataset.dataset_id, dataset.revision)
    client = FakeClient()
    adapter = LangSmithDatasetAdapter(
        repository, LangSmithSettings(enabled=True, api_key="secret"), client=client
    )

    with pytest.raises(ValueError, match="Sensitive"):
        adapter.sync_dataset(bundle)
    assert not client.datasets


def test_disabled_cases_are_not_projected_as_langsmith_examples(tmp_path: Path):
    repository = EvaluationRepository(tmp_path / "evaluation.db")
    dataset = repository.create_dataset(
        EvalDataset(
            name="Enabled view",
            cases=[
                EvalCase(
                    name="enabled",
                    input=EvalInput(message="run"),
                    expectations=EvalExpectations(contains_all=["ok"]),
                ),
                EvalCase(name="disabled", enabled=False, input=EvalInput(message="skip")),
            ],
        )
    )
    bundle = repository.publish_dataset(dataset.dataset_id, dataset.revision)
    client = FakeClient()

    result = LangSmithDatasetAdapter(
        repository, LangSmithSettings(enabled=True, api_key="secret"), client=client
    ).sync_dataset(bundle)

    assert result["case_count"] == 1
    assert len(client.examples) == 1
    assert client.examples[0]["inputs"]["puddingclaw_case_id"] == bundle.dataset.cases[0].case_id


def test_default_redaction_profile_covers_high_risk_credentials_and_pii():
    payload = {
        "text": (
            "postgresql://admin:supersecret@db.internal/app "
            "AKIAABCDEFGHIJKLMNOP alice@example.com /home/alice/private.txt "
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        )
    }

    rendered = str(_redact(payload, profile="default-v1"))

    for sensitive in [
        "supersecret",
        "AKIAABCDEFGHIJKLMNOP",
        "alice@example.com",
        "/home/alice/private.txt",
        "BEGIN PRIVATE KEY",
    ]:
        assert sensitive not in rendered
    with pytest.raises(ValueError, match="Unsupported redaction profile"):
        _redact(payload, profile="unknown-v1")


def test_langsmith_client_policy_has_explicit_timeout_and_finite_retries():
    kwargs = langsmith_client_kwargs(
        LangSmithSettings(request_timeout_seconds=7, max_retries=1)
    )

    assert kwargs["timeout_ms"] == 7_000
    assert kwargs["retry_config"].total == 1
