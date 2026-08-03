"""Independent SQLite ledger for evaluation datasets and experiments."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from .contracts import (
    DatasetBundle,
    DatasetStatus,
    EvalCase,
    EvalDataset,
    EvalExperiment,
    EvaluationResult,
    ExperimentStatus,
    ResolvedEvaluatorBinding,
    new_id,
    utc_now,
)
from .validation import validate_dataset


class EvaluationRepositoryError(RuntimeError):
    pass


class NotFoundError(EvaluationRepositoryError):
    pass


class ConflictError(EvaluationRepositoryError):
    pass


class ValidationError(EvaluationRepositoryError):
    pass


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EvaluationRepository:
    """Small synchronous repository; each operation owns a short transaction."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        default = Path(__file__).resolve().parent.parent / "data" / "evaluation.db"
        self.db_path = Path(db_path or os.getenv("PUDDINGCLAW_EVALUATION_DB") or default)
        self._lock = threading.RLock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS eval_datasets (
                    dataset_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eval_cases (
                    dataset_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, case_id),
                    FOREIGN KEY (dataset_id) REFERENCES eval_datasets(dataset_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS eval_dataset_versions (
                    version_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(dataset_id, version_number),
                    UNIQUE(dataset_id, content_hash),
                    FOREIGN KEY (dataset_id) REFERENCES eval_datasets(dataset_id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS eval_dataset_version_cases (
                    version_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    case_revision_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    case_hash TEXT NOT NULL,
                    PRIMARY KEY(version_id, case_id),
                    FOREIGN KEY(version_id) REFERENCES eval_dataset_versions(version_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS eval_remote_mappings (
                    provider TEXT NOT NULL,
                    local_type TEXT NOT NULL,
                    local_id TEXT NOT NULL,
                    version_id TEXT NOT NULL DEFAULT '',
                    remote_id TEXT,
                    remote_name TEXT,
                    content_hash TEXT,
                    status TEXT NOT NULL,
                    last_synced_at TEXT,
                    last_error TEXT,
                    PRIMARY KEY(provider, local_type, local_id, version_id)
                );
                CREATE TABLE IF NOT EXISTS eval_experiments (
                    experiment_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eval_case_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    repetition INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    run_envelope_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(experiment_id, case_id, repetition),
                    FOREIGN KEY(experiment_id) REFERENCES eval_experiments(experiment_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS eval_results (
                    result_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    evaluator_id TEXT NOT NULL,
                    evaluator_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(attempt_id, evaluator_id, evaluator_version),
                    FOREIGN KEY(experiment_id) REFERENCES eval_experiments(experiment_id) ON DELETE CASCADE,
                    FOREIGN KEY(attempt_id) REFERENCES eval_case_attempts(attempt_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS eval_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS eval_outbox_one_active
                ON eval_outbox(provider, kind, aggregate_id)
                WHERE status IN ('pending', 'processing');
                """
            )
        if os.name != "nt" and self.db_path.exists():
            os.chmod(self.db_path, 0o600)

    @staticmethod
    def _dataset_payload(dataset: EvalDataset) -> dict[str, Any]:
        return dataset.model_dump(mode="json", exclude={"cases"})

    def _load_cases(self, connection: sqlite3.Connection, dataset_id: str) -> list[EvalCase]:
        rows = connection.execute(
            "SELECT payload_json FROM eval_cases WHERE dataset_id=? ORDER BY created_at, case_id",
            (dataset_id,),
        ).fetchall()
        return [EvalCase.model_validate_json(row["payload_json"]) for row in rows]

    def create_dataset(self, dataset: EvalDataset) -> EvalDataset:
        dataset = dataset.model_copy(
            update={
                "status": DatasetStatus.DRAFT,
                "current_version": 0,
                "current_version_id": None,
                "revision": 1,
                "updated_at": utc_now(),
            }
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO eval_datasets VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        dataset.dataset_id,
                        _json(self._dataset_payload(dataset)),
                        dataset.status,
                        dataset.revision,
                        dataset.created_at.isoformat(),
                        dataset.updated_at.isoformat(),
                    ),
                )
                for case in dataset.cases:
                    self._insert_case(connection, dataset.dataset_id, case)
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"Dataset already exists: {dataset.dataset_id}") from exc
        return self.get_dataset(dataset.dataset_id)

    def list_datasets(self) -> list[EvalDataset]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM eval_datasets ORDER BY updated_at DESC").fetchall()
            result = []
            for row in rows:
                dataset = EvalDataset.model_validate_json(row["payload_json"])
                result.append(dataset.model_copy(update={"cases": self._load_cases(connection, dataset.dataset_id)}))
            return result

    def get_dataset(self, dataset_id: str, version: int | None = None) -> EvalDataset:
        with self._connect() as connection:
            if version is not None:
                row = connection.execute(
                    "SELECT snapshot_json FROM eval_dataset_versions WHERE dataset_id=? AND version_number=?",
                    (dataset_id, version),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Dataset version not found: {dataset_id}@{version}")
                return EvalDataset.model_validate_json(row["snapshot_json"])
            row = connection.execute(
                "SELECT payload_json FROM eval_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Dataset not found: {dataset_id}")
            dataset = EvalDataset.model_validate_json(row["payload_json"])
            return dataset.model_copy(update={"cases": self._load_cases(connection, dataset_id)})

    def list_dataset_versions(self, dataset_id: str) -> list[EvalDataset]:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM eval_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if exists is None:
                raise NotFoundError(f"Dataset not found: {dataset_id}")
            rows = connection.execute(
                "SELECT snapshot_json FROM eval_dataset_versions WHERE dataset_id=? ORDER BY version_number DESC",
                (dataset_id,),
            ).fetchall()
        return [EvalDataset.model_validate_json(row["snapshot_json"]) for row in rows]

    def update_dataset(self, dataset_id: str, updates: dict[str, Any], expected_revision: int) -> EvalDataset:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.get_dataset(dataset_id)
            if current.revision != expected_revision:
                raise ConflictError(f"Dataset revision changed: expected {expected_revision}, got {current.revision}")
            if current.status == DatasetStatus.ARCHIVED:
                raise ConflictError("Archived Dataset cannot be edited")
            if current.status == DatasetStatus.PUBLISHED and updates.get("status") not in {
                DatasetStatus.DRAFT,
                DatasetStatus.ARCHIVED,
            }:
                raise ConflictError("Published Dataset must be reopened as draft before editing")
            allowed = {"name", "description", "default_profile", "tags", "metadata", "status"}
            unknown = set(updates) - allowed
            if unknown:
                raise ValidationError(f"Unsupported Dataset fields: {sorted(unknown)}")
            if updates.get("status") not in {None, DatasetStatus.DRAFT, DatasetStatus.ARCHIVED}:
                raise ValidationError("Use publish operation to publish a Dataset")
            updated = current.model_copy(update={**updates, "revision": current.revision + 1, "updated_at": utc_now()})
            payload = self._dataset_payload(updated)
            cursor = connection.execute(
                "UPDATE eval_datasets SET payload_json=?, status=?, revision=?, updated_at=? "
                "WHERE dataset_id=? AND revision=?",
                (
                    _json(payload),
                    updated.status,
                    updated.revision,
                    updated.updated_at.isoformat(),
                    dataset_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Dataset was updated concurrently")
        return self.get_dataset(dataset_id)

    @staticmethod
    def _insert_case(connection: sqlite3.Connection, dataset_id: str, case: EvalCase) -> None:
        connection.execute(
            "INSERT INTO eval_cases VALUES (?, ?, ?, ?, ?, ?)",
            (
                dataset_id,
                case.case_id,
                case.revision_id,
                _json(case),
                case.created_at.isoformat(),
                case.updated_at.isoformat(),
            ),
        )

    def _assert_draft(
        self,
        connection: sqlite3.Connection,
        dataset_id: str,
        expected_revision: int | None = None,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT status, revision FROM eval_datasets WHERE dataset_id=?", (dataset_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Dataset not found: {dataset_id}")
        if row["status"] != DatasetStatus.DRAFT:
            raise ConflictError("Cases may only be edited in a draft Dataset")
        if expected_revision is not None and row["revision"] != expected_revision:
            raise ConflictError(
                f"Dataset revision changed: expected {expected_revision}, got {row['revision']}"
            )
        return row

    def add_case(
        self, dataset_id: str, case: EvalCase, expected_revision: int | None = None
    ) -> EvalCase:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._assert_draft(connection, dataset_id, expected_revision)
            try:
                self._insert_case(connection, dataset_id, case)
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"Case already exists: {case.case_id}") from exc
            self._bump_revision(connection, dataset_id, row["revision"])
        return case

    def update_case(
        self,
        dataset_id: str,
        case_id: str,
        case: EvalCase,
        expected_revision: int | None = None,
    ) -> EvalCase:
        if case.case_id != case_id:
            raise ValidationError("case_id cannot be changed")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._assert_draft(connection, dataset_id, expected_revision)
            existing = connection.execute(
                "SELECT payload_json FROM eval_cases WHERE dataset_id=? AND case_id=?", (dataset_id, case_id)
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"Case not found: {case_id}")
            existing_case = EvalCase.model_validate_json(existing["payload_json"])
            case = case.model_copy(
                update={"revision_id": new_id("rev"), "created_at": existing_case.created_at, "updated_at": utc_now()}
            )
            connection.execute(
                "UPDATE eval_cases SET revision_id=?, payload_json=?, updated_at=? WHERE dataset_id=? AND case_id=?",
                (case.revision_id, _json(case), case.updated_at.isoformat(), dataset_id, case_id),
            )
            self._bump_revision(connection, dataset_id, row["revision"])
        return case

    def delete_case(
        self, dataset_id: str, case_id: str, expected_revision: int | None = None
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._assert_draft(connection, dataset_id, expected_revision)
            cursor = connection.execute(
                "DELETE FROM eval_cases WHERE dataset_id=? AND case_id=?", (dataset_id, case_id)
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Case not found: {case_id}")
            self._bump_revision(connection, dataset_id, row["revision"])

    def _bump_revision(self, connection: sqlite3.Connection, dataset_id: str, revision: int) -> None:
        now = utc_now()
        raw = connection.execute("SELECT payload_json FROM eval_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()[
            "payload_json"
        ]
        dataset = EvalDataset.model_validate_json(raw).model_copy(update={"revision": revision + 1, "updated_at": now})
        cursor = connection.execute(
            "UPDATE eval_datasets SET payload_json=?, revision=?, updated_at=? "
            "WHERE dataset_id=? AND revision=?",
            (
                _json(self._dataset_payload(dataset)),
                dataset.revision,
                now.isoformat(),
                dataset_id,
                revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError("Dataset was updated concurrently")

    def publish_dataset(self, dataset_id: str, expected_revision: int) -> DatasetBundle:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.get_dataset(dataset_id)
            if (
                current.status == DatasetStatus.PUBLISHED
                and current.revision == expected_revision + 1
                and current.current_version > 0
            ):
                return self.export_bundle(dataset_id, current.current_version)
            if current.revision != expected_revision:
                raise ConflictError(f"Dataset revision changed: expected {expected_revision}, got {current.revision}")
            if current.status != DatasetStatus.DRAFT:
                raise ConflictError("Only a draft Dataset can be published")
            validation = validate_dataset(current)
            if not validation.valid:
                raise ValidationError(_json(validation))
            from .evaluators import evaluator_code_hash, evaluator_registry

            profiles = {profile.profile_id: profile for profile in evaluator_registry.list_profiles()}
            profile = profiles.get(current.default_profile)
            if profile is None:
                raise ValidationError(f"Unknown default profile: {current.default_profile}")
            specs = {spec.evaluator_id: spec for spec in evaluator_registry.list_specs()}
            frozen_cases = []
            for case in current.cases:
                selected_profile_ids = [
                    evaluator_id
                    for evaluator_id in profile.evaluator_ids
                    if not case.dimensions or specs[evaluator_id].dimension in case.dimensions
                ]
                source_bindings = case.evaluator_bindings or [
                    {"evaluator_id": evaluator_id, "version": specs[evaluator_id].version}
                    for evaluator_id in selected_profile_ids
                ]
                resolved = []
                for item in source_bindings:
                    binding = (
                        item
                        if hasattr(item, "evaluator_id")
                        else ResolvedEvaluatorBinding.model_validate({**item, "code_hash": "pending"})
                    )
                    spec = specs.get(binding.evaluator_id)
                    if spec is None or str(spec.version) != str(binding.version):
                        raise ValidationError(f"Unknown evaluator binding: {binding.evaluator_id}@{binding.version}")
                    if case.dimensions and spec.dimension not in case.dimensions:
                        raise ValidationError(
                            f"Evaluator {binding.evaluator_id} is outside Case dimensions"
                        )
                    registered = evaluator_registry.get_registered(binding.evaluator_id)
                    assert registered is not None
                    code_hash = evaluator_code_hash(spec, registered[1])
                    resolved.append(
                        ResolvedEvaluatorBinding(
                            evaluator_id=binding.evaluator_id,
                            version=binding.version,
                            required=binding.required,
                            config=binding.config,
                            code_hash=code_hash,
                        )
                    )
                frozen_cases.append(case.model_copy(update={"resolved_evaluator_bindings": resolved}))
            current = current.model_copy(update={"cases": frozen_cases})
            content_hash = hashlib.sha256(_json(current).encode("utf-8")).hexdigest()
            duplicate = connection.execute(
                "SELECT version_id, version_number, snapshot_json FROM eval_dataset_versions "
                "WHERE dataset_id=? AND content_hash=?",
                (dataset_id, content_hash),
            ).fetchone()
            if duplicate:
                snapshot = EvalDataset.model_validate_json(duplicate["snapshot_json"])
                return DatasetBundle(dataset=snapshot, version_id=duplicate["version_id"], checksum=content_hash)
            version = current.current_version + 1
            version_id = new_id("dsv")
            published = current.model_copy(
                update={
                    "status": DatasetStatus.PUBLISHED,
                    "current_version": version,
                    "current_version_id": version_id,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            content_hash = hashlib.sha256(_json(published).encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO eval_dataset_versions VALUES (?, ?, ?, ?, ?, ?)",
                (version_id, dataset_id, version, content_hash, _json(published), utc_now().isoformat()),
            )
            for position, case in enumerate(published.cases):
                connection.execute(
                    "INSERT INTO eval_dataset_version_cases VALUES (?, ?, ?, ?, ?)",
                    (
                        version_id,
                        case.case_id,
                        case.revision_id,
                        position,
                        hashlib.sha256(_json(case).encode("utf-8")).hexdigest(),
                    ),
                )
            connection.execute(
                "UPDATE eval_datasets SET payload_json=?, status=?, revision=?, updated_at=? WHERE dataset_id=?",
                (
                    _json(self._dataset_payload(published)),
                    DatasetStatus.PUBLISHED,
                    published.revision,
                    published.updated_at.isoformat(),
                    dataset_id,
                ),
            )
        return DatasetBundle(dataset=published, version_id=version_id, checksum=content_hash)

    def export_bundle(self, dataset_id: str, version: int | None = None) -> DatasetBundle:
        dataset = self.get_dataset(dataset_id, version)
        if version is None and dataset.status == DatasetStatus.PUBLISHED and dataset.current_version > 0:
            # The immutable version row is authoritative after publication;
            # the mutable head keeps working-copy Case rows for reopening.
            dataset = self.get_dataset(dataset_id, dataset.current_version)
        checksum = hashlib.sha256(_json(dataset).encode("utf-8")).hexdigest()
        return DatasetBundle(dataset=dataset, version_id=dataset.current_version_id, checksum=checksum)

    def save_remote_mapping(
        self,
        *,
        provider: str,
        local_type: str,
        local_id: str,
        version_id: str,
        remote_id: str | None,
        remote_name: str | None,
        content_hash: str | None,
        status: str,
        error: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO eval_remote_mappings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, local_type, local_id, version_id) DO UPDATE SET "
                "remote_id=excluded.remote_id, remote_name=excluded.remote_name, content_hash=excluded.content_hash, "
                "status=excluded.status, last_synced_at=excluded.last_synced_at, last_error=excluded.last_error",
                (provider, local_type, local_id, version_id, remote_id, remote_name, content_hash, status, now, error),
            )

    def get_remote_mapping(self, provider: str, local_type: str, local_id: str, version_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM eval_remote_mappings WHERE provider=? AND local_type=? AND local_id=? AND version_id=?",
                (provider, local_type, local_id, version_id),
            ).fetchone()
            return dict(row) if row else None

    def create_experiment(self, experiment: EvalExperiment) -> EvalExperiment:
        experiment = experiment.model_copy(update={"candidate": experiment.candidate.with_fingerprint()})
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO eval_experiments VALUES (?, ?, ?, ?, ?)",
                    (experiment.experiment_id, _json(experiment), experiment.status, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"Experiment already exists: {experiment.experiment_id}") from exc
        return experiment

    def list_experiments(self) -> list[EvalExperiment]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM eval_experiments ORDER BY created_at DESC").fetchall()
        return [EvalExperiment.model_validate_json(row["payload_json"]) for row in rows]

    def get_experiment(self, experiment_id: str) -> EvalExperiment:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM eval_experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Experiment not found: {experiment_id}")
        return EvalExperiment.model_validate_json(row["payload_json"])

    def update_experiment(
        self,
        experiment: EvalExperiment,
        *,
        expected_status: ExperimentStatus | str | None = None,
    ) -> EvalExperiment:
        with self._lock, self._connect() as connection:
            if expected_status is None:
                cursor = connection.execute(
                    "UPDATE eval_experiments SET payload_json=?, status=?, updated_at=? WHERE experiment_id=?",
                    (_json(experiment), experiment.status, utc_now().isoformat(), experiment.experiment_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE eval_experiments SET payload_json=?, status=?, updated_at=? "
                    "WHERE experiment_id=? AND status=?",
                    (
                        _json(experiment),
                        experiment.status,
                        utc_now().isoformat(),
                        experiment.experiment_id,
                        str(expected_status),
                    ),
                )
            if cursor.rowcount != 1:
                if expected_status is not None:
                    raise ConflictError(
                        f"Experiment {experiment.experiment_id} status changed; expected {expected_status}"
                    )
                raise NotFoundError(f"Experiment not found: {experiment.experiment_id}")
        return experiment

    def create_attempt(self, experiment_id: str, case_id: str, repetition: int) -> str:
        attempt_id = new_id("attempt")
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO eval_case_attempts VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (attempt_id, experiment_id, case_id, repetition, ExperimentStatus.QUEUED, now, now),
            )
        return attempt_id

    def finish_attempt(self, attempt_id: str, *, status: str, run: Any = None, error: Any = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE eval_case_attempts SET status=?, run_envelope_json=?, error_json=?, updated_at=? WHERE attempt_id=?",
                (
                    status,
                    _json(run) if run is not None else None,
                    _json(error) if error is not None else None,
                    utc_now().isoformat(),
                    attempt_id,
                ),
            )

    def update_attempt_status(self, attempt_id: str, status: str) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE eval_case_attempts SET status=?, updated_at=? WHERE attempt_id=?",
                (status, utc_now().isoformat(), attempt_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Attempt not found: {attempt_id}")

    def cancel_running_attempts(self, experiment_id: str, reason: str) -> int:
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE eval_case_attempts SET status='cancelled', error_json=?, updated_at=? "
                "WHERE experiment_id=? AND status IN ('queued', 'running')",
                (_json({"code": "cancelled", "message": reason}), now, experiment_id),
            )
        return cursor.rowcount

    def list_results(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT a.case_id, a.repetition, a.attempt_id, a.status AS attempt_status, r.payload_json "
                "FROM eval_case_attempts a LEFT JOIN eval_results r ON r.attempt_id=a.attempt_id "
                "WHERE a.experiment_id=? ORDER BY a.created_at, r.evaluator_id",
                (experiment_id,),
            ).fetchall()
        return [
            {
                "case_id": row["case_id"],
                "repetition": row["repetition"],
                "attempt_id": row["attempt_id"],
                "attempt_status": row["attempt_status"],
                "result": json.loads(row["payload_json"]) if row["payload_json"] else None,
            }
            for row in rows
        ]

    def load_projection_outputs(self, experiment_id: str) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as connection:
            attempts = connection.execute(
                "SELECT attempt_id, case_id, run_envelope_json FROM eval_case_attempts "
                "WHERE experiment_id=? ORDER BY repetition",
                (experiment_id,),
            ).fetchall()
            outputs: dict[str, list[dict[str, Any]]] = {}
            for attempt in attempts:
                results = connection.execute(
                    "SELECT payload_json FROM eval_results WHERE attempt_id=? ORDER BY evaluator_id",
                    (attempt["attempt_id"],),
                ).fetchall()
                run = json.loads(attempt["run_envelope_json"]) if attempt["run_envelope_json"] else {}
                outputs.setdefault(attempt["case_id"], []).append(
                    {
                        "case_id": attempt["case_id"],
                        "attempt_id": attempt["attempt_id"],
                        "response": str(run.get("response") or "")[:8_000],
                        "results": [json.loads(item["payload_json"]) for item in results],
                    }
                )
        return outputs

    def claim_outbox(self, provider: str, kind: str, aggregate_id: str) -> dict[str, Any] | None:
        now = utc_now()
        stale_before = (now - timedelta(minutes=5)).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE eval_outbox SET status='pending', updated_at=? "
                "WHERE provider=? AND kind=? AND aggregate_id=? "
                "AND status='processing' AND updated_at<?",
                (now.isoformat(), provider, kind, aggregate_id, stale_before),
            )
            row = connection.execute(
                "SELECT * FROM eval_outbox WHERE provider=? AND kind=? AND aggregate_id=? "
                "AND status='pending' ORDER BY created_at LIMIT 1",
                (provider, kind, aggregate_id),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                "UPDATE eval_outbox SET status='processing', attempts=attempts+1, updated_at=? "
                "WHERE outbox_id=? AND status='pending'",
                (now.isoformat(), row["outbox_id"]),
            )
            if cursor.rowcount != 1:
                return None
            return dict(row)

    def release_outbox(self, outbox_id: str, error: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE eval_outbox SET status='pending', last_error=?, updated_at=? "
                "WHERE outbox_id=? AND status='processing'",
                (error[:1000], utc_now().isoformat(), outbox_id),
            )

    def mark_outbox_delivered(self, outbox_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE eval_outbox SET status='delivered', updated_at=? "
                "WHERE outbox_id=? AND status='processing'",
                (utc_now().isoformat(), outbox_id),
            )

    def complete_experiment_projection(
        self, experiment: EvalExperiment, outbox_id: str
    ) -> EvalExperiment:
        """Atomically persist the remote identity and acknowledge its outbox claim."""

        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE eval_experiments SET payload_json=?, status=?, updated_at=? "
                "WHERE experiment_id=? AND status='completed'",
                (_json(experiment), experiment.status, now, experiment.experiment_id),
            )
            if updated.rowcount != 1:
                raise ConflictError(
                    f"Experiment {experiment.experiment_id} is no longer completed"
                )
            acknowledged = connection.execute(
                "UPDATE eval_outbox SET status='delivered', updated_at=? "
                "WHERE outbox_id=? AND status='processing'",
                (now, outbox_id),
            )
            if acknowledged.rowcount != 1:
                raise ConflictError(f"Outbox claim is no longer owned: {outbox_id}")
        return experiment

    def save_result(self, experiment_id: str, attempt_id: str, result: EvaluationResult) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO eval_results VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(attempt_id, evaluator_id, evaluator_version) DO UPDATE SET payload_json=excluded.payload_json",
                (
                    new_id("result"),
                    experiment_id,
                    attempt_id,
                    result.evaluator_id,
                    result.evaluator_version,
                    _json(result),
                    utc_now().isoformat(),
                ),
            )

    def enqueue_outbox(self, provider: str, kind: str, aggregate_id: str, payload: Any) -> str:
        outbox_id = new_id("outbox")
        now = utc_now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT outbox_id FROM eval_outbox WHERE provider=? AND kind=? AND aggregate_id=? "
                "AND status IN ('pending', 'processing') LIMIT 1",
                (provider, kind, aggregate_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE eval_outbox SET payload_json=?, updated_at=? WHERE outbox_id=?",
                    (_json(payload), now, existing["outbox_id"]),
                )
                return str(existing["outbox_id"])
            connection.execute(
                "INSERT INTO eval_outbox VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)",
                (outbox_id, provider, kind, aggregate_id, _json(payload), now, now),
            )
        return outbox_id


_repository: EvaluationRepository | None = None


def get_evaluation_repository() -> EvaluationRepository:
    global _repository
    if _repository is None:
        _repository = EvaluationRepository()
    return _repository
