"""Replaceable boundaries for evaluation storage, execution and evidence."""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    AgentRunEnvelope,
    DatasetBundle,
    EvalCase,
    EvalDataset,
    EvalExperiment,
    EvaluationResult,
    TraceEvidence,
)


class DatasetBackend(Protocol):
    def list_datasets(self) -> list[EvalDataset]: ...
    def get_dataset(self, dataset_id: str, version: int | None = None) -> EvalDataset: ...
    def create_dataset(self, dataset: EvalDataset) -> EvalDataset: ...
    def update_dataset(
        self, dataset_id: str, updates: dict[str, object], expected_revision: int
    ) -> EvalDataset: ...
    def add_case(
        self, dataset_id: str, case: EvalCase, expected_revision: int | None = None
    ) -> EvalCase: ...
    def update_case(
        self,
        dataset_id: str,
        case_id: str,
        case: EvalCase,
        expected_revision: int | None = None,
    ) -> EvalCase: ...
    def delete_case(
        self, dataset_id: str, case_id: str, expected_revision: int | None = None
    ) -> None: ...
    def publish_dataset(self, dataset_id: str, expected_revision: int) -> DatasetBundle: ...
    def export_bundle(self, dataset_id: str, version: int | None = None) -> DatasetBundle: ...


class ExperimentBackend(Protocol):
    def create_experiment(self, experiment: EvalExperiment) -> EvalExperiment: ...
    def get_experiment(self, experiment_id: str) -> EvalExperiment: ...
    def update_experiment(
        self,
        experiment: EvalExperiment,
        *,
        expected_status: str | None = None,
    ) -> EvalExperiment: ...


class RemoteMappingStore(Protocol):
    def save_remote_mapping(self, **mapping: object) -> None: ...
    def get_remote_mapping(
        self, provider: str, local_type: str, local_id: str, version_id: str
    ) -> dict | None: ...


class EvidenceProvider(Protocol):
    def collect(self, run: AgentRunEnvelope) -> TraceEvidence: ...


class Evaluator(Protocol):
    spec: object

    def evaluate(
        self,
        case: EvalCase,
        run: AgentRunEnvelope,
        evidence: TraceEvidence,
    ) -> EvaluationResult: ...
