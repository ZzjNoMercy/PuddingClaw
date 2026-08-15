"""General Agent Evaluation API, separate from Skill review endpoints."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from evaluation.candidate import CandidateRequest, bind_candidate_capability, resolve_candidate
from evaluation.contracts import (
    AgentRunEnvelope,
    EvalCase,
    EvalDataset,
    EvalExperiment,
    EvaluationDimension,
    EvaluationOutcome,
    EvaluationResult,
    EvidenceReference,
    ExecutionPolicy,
    new_id,
    protocol_json_schemas,
    utc_now,
)
from evaluation.dataset_io import export_dataset, import_dataset
from evaluation.evaluators import evaluator_registry
from evaluation.langsmith_backend import LangSmithDatasetAdapter, _redact
from evaluation.official_swebench import probe_official_swebench_runtime
from evaluation.repository import (
    ConflictError,
    EvaluationRepositoryError,
    NotFoundError,
    ValidationError,
    get_evaluation_repository,
)
from evaluation.runner import EvaluationRunner
from evaluation.settings import get_evaluation_settings_store
from evaluation.swebench_adapter import (
    DEFAULT_DATASET as DEFAULT_SWEBENCH_DATASET,
)
from evaluation.swebench_adapter import (
    fetch_swebench_rows,
    frozen_swebench_dataset_json,
    parse_official_swebench_results,
    parse_swebench_content,
    prediction_jsonl,
    swebench_dataset_from_rows,
    swebench_prediction_manifest,
    swebench_run_manifest,
)
from evaluation.validation import validate_dataset
from evaluation.worker_manager import evaluation_worker_manager

router = APIRouter(prefix="/evaluation", tags=["evaluation"])
BASE_DIR = Path(__file__).resolve().parent.parent


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDatasetRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    default_profile: str = "general_agent@1"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateDatasetRequest(StrictRequest):
    expected_revision: int = Field(ge=1)
    name: str | None = None
    description: str | None = None
    default_profile: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class CaseMutationRequest(StrictRequest):
    expected_revision: int = Field(ge=1)
    case: EvalCase


class RevisionRequest(StrictRequest):
    expected_revision: int = Field(ge=1)


class ImportRequest(StrictRequest):
    format: Literal["bundle", "jsonl", "csv"]
    content: str
    name: str | None = None


class SWEbenchImportRequest(StrictRequest):
    dataset_name: str = Field(default=DEFAULT_SWEBENCH_DATASET, min_length=1, max_length=200)
    split: str = Field(default="test", min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=500)
    name: str | None = Field(default=None, max_length=200)
    content: str | None = None


class SWEbenchResultImportRequest(StrictRequest):
    content: str = Field(min_length=1)
    manifest: dict[str, Any]


class LangSmithSettingsRequest(StrictRequest):
    enabled: bool | None = None
    endpoint: str | None = None
    project: str | None = None
    workspace_id: str | None = None
    redaction_profile: Literal["default-v1"] | None = None
    request_timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    trace_finalize_timeout_seconds: int | None = Field(default=None, ge=1, le=60)
    projection_timeout_seconds: int | None = Field(default=None, ge=5, le=600)
    api_key: str | None = None
    clear_api_key: bool = False


class CreateExperimentRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=200)
    dataset_id: str
    dataset_version: int = Field(ge=1)
    candidate_request: CandidateRequest
    profile_id: str = "general_agent@1"
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)


def _raise_repository_error(exc: EvaluationRepositoryError) -> None:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValidationError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail="Evaluation storage operation failed") from exc


def _dataset_response(dataset: EvalDataset) -> dict[str, Any]:
    return dataset.model_dump(mode="json")


@router.get("/schemas")
async def get_protocol_schemas() -> dict[str, Any]:
    return {"protocol_version": "1.0", "schemas": protocol_json_schemas()}


@router.get("/datasets")
async def list_datasets() -> dict[str, Any]:
    datasets = await run_in_threadpool(get_evaluation_repository().list_datasets)
    return {"items": [_dataset_response(dataset) for dataset in datasets], "total": len(datasets)}


@router.post("/datasets", status_code=201)
async def create_dataset(body: CreateDatasetRequest, response: Response) -> dict[str, Any]:
    try:
        dataset = await run_in_threadpool(
            get_evaluation_repository().create_dataset,
            EvalDataset(**body.model_dump()),
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    response.headers["ETag"] = f'W/"{dataset.revision}"'
    return _dataset_response(dataset)


@router.post("/datasets/import", status_code=201)
async def import_new_dataset(body: ImportRequest) -> dict[str, Any]:
    try:
        dataset = await run_in_threadpool(import_dataset, body.content, body.format, name=body.name)
        created = await run_in_threadpool(get_evaluation_repository().create_dataset, dataset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return _dataset_response(created)


@router.post("/datasets/import/swebench", status_code=201)
async def import_swebench_dataset(body: SWEbenchImportRequest) -> dict[str, Any]:
    try:
        rows = (
            await run_in_threadpool(
                parse_swebench_content,
                body.content,
            )
            if body.content is not None
            else await run_in_threadpool(
                fetch_swebench_rows,
                dataset_name=body.dataset_name,
                split=body.split,
                offset=body.offset,
                limit=body.limit,
            )
        )
        dataset = await run_in_threadpool(
            swebench_dataset_from_rows,
            rows,
            dataset_name=body.dataset_name,
            split=body.split,
            name=body.name,
        )
        created = await run_in_threadpool(get_evaluation_repository().create_dataset, dataset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"SWE-bench Dataset import failed: {type(exc).__name__}: {str(exc)[:500]}",
        ) from exc
    return _dataset_response(created)


@router.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: str, response: Response, version: int | None = Query(default=None, ge=1)
) -> dict[str, Any]:
    try:
        dataset = await run_in_threadpool(get_evaluation_repository().get_dataset, dataset_id, version)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    response.headers["ETag"] = f'W/"{dataset.revision}"'
    return _dataset_response(dataset)


@router.get("/datasets/{dataset_id}/versions")
async def list_dataset_versions(dataset_id: str) -> dict[str, Any]:
    try:
        versions = await run_in_threadpool(get_evaluation_repository().list_dataset_versions, dataset_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return {"items": [_dataset_response(item) for item in versions], "total": len(versions)}


@router.patch("/datasets/{dataset_id}")
async def update_dataset(dataset_id: str, body: UpdateDatasetRequest, response: Response) -> dict[str, Any]:
    updates = body.model_dump(exclude={"expected_revision"}, exclude_none=True)
    try:
        dataset = await run_in_threadpool(
            get_evaluation_repository().update_dataset, dataset_id, updates, body.expected_revision
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    response.headers["ETag"] = f'W/"{dataset.revision}"'
    return _dataset_response(dataset)


@router.post("/datasets/{dataset_id}/versions")
async def reopen_dataset_draft(dataset_id: str, body: RevisionRequest) -> dict[str, Any]:
    try:
        dataset = await run_in_threadpool(
            get_evaluation_repository().update_dataset,
            dataset_id,
            {"status": "draft"},
            body.expected_revision,
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return _dataset_response(dataset)


@router.post("/datasets/{dataset_id}/archive")
async def archive_dataset(dataset_id: str, body: RevisionRequest) -> dict[str, Any]:
    try:
        dataset = await run_in_threadpool(
            get_evaluation_repository().update_dataset,
            dataset_id,
            {"status": "archived"},
            body.expected_revision,
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return _dataset_response(dataset)


@router.post("/datasets/{dataset_id}/cases", status_code=201)
async def add_case(dataset_id: str, body: CaseMutationRequest) -> dict[str, Any]:
    try:
        await run_in_threadpool(
            get_evaluation_repository().add_case,
            dataset_id,
            body.case,
            body.expected_revision,
        )
        updated = await run_in_threadpool(get_evaluation_repository().get_dataset, dataset_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return _dataset_response(updated)


@router.patch("/datasets/{dataset_id}/cases/{case_id}")
async def update_case(dataset_id: str, case_id: str, body: CaseMutationRequest) -> dict[str, Any]:
    try:
        await run_in_threadpool(
            get_evaluation_repository().update_case,
            dataset_id,
            case_id,
            body.case,
            body.expected_revision,
        )
        updated = await run_in_threadpool(get_evaluation_repository().get_dataset, dataset_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return _dataset_response(updated)


@router.delete("/datasets/{dataset_id}/cases/{case_id}", status_code=204)
async def delete_case(dataset_id: str, case_id: str, expected_revision: int = Query(ge=1)) -> Response:
    try:
        await run_in_threadpool(
            get_evaluation_repository().delete_case,
            dataset_id,
            case_id,
            expected_revision,
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return Response(status_code=204)


@router.post("/datasets/{dataset_id}/validate")
async def validate_dataset_endpoint(dataset_id: str) -> dict[str, Any]:
    try:
        dataset = await run_in_threadpool(get_evaluation_repository().get_dataset, dataset_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return validate_dataset(dataset).model_dump(mode="json")


@router.post("/datasets/{dataset_id}/publish")
async def publish_dataset(dataset_id: str, body: RevisionRequest) -> dict[str, Any]:
    try:
        bundle = await run_in_threadpool(
            get_evaluation_repository().publish_dataset, dataset_id, body.expected_revision
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return bundle.model_dump(mode="json")


@router.get("/datasets/{dataset_id}/export", response_class=PlainTextResponse)
async def export_dataset_endpoint(
    dataset_id: str,
    format: Literal["bundle", "jsonl", "csv"] = "bundle",
    version: int | None = Query(default=None, ge=1),
) -> PlainTextResponse:
    try:
        bundle = await run_in_threadpool(get_evaluation_repository().export_bundle, dataset_id, version)
        content = await run_in_threadpool(export_dataset, bundle, format)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    media_type = (
        "application/json" if format == "bundle" else "application/x-ndjson" if format == "jsonl" else "text/csv"
    )
    extension = "json" if format == "bundle" else format
    return PlainTextResponse(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{dataset_id}.{extension}"'},
    )


@router.get("/datasets/{dataset_id}/export/swebench", response_class=PlainTextResponse)
async def export_frozen_swebench_dataset(
    dataset_id: str,
    version: int | None = Query(default=None, ge=1),
) -> PlainTextResponse:
    try:
        bundle = await run_in_threadpool(get_evaluation_repository().export_bundle, dataset_id, version)
        content = await run_in_threadpool(frozen_swebench_dataset_json, bundle.dataset)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PlainTextResponse(
        content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{dataset_id}-swebench-frozen.json"'},
    )


@router.post("/datasets/{dataset_id}/sync/langsmith")
async def sync_dataset_langsmith(dataset_id: str, version: int | None = Query(default=None, ge=1)) -> dict[str, Any]:
    repository = get_evaluation_repository()
    settings = get_evaluation_settings_store().load()
    if not settings.enabled or not settings.api_key:
        raise HTTPException(status_code=409, detail="LangSmith evaluation backend is disabled or not configured")
    try:
        bundle = await run_in_threadpool(repository.export_bundle, dataset_id, version)
        return await run_in_threadpool(LangSmithDatasetAdapter(repository, settings).sync_dataset, bundle)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(_redact(f"LangSmith sync failed: {type(exc).__name__}: {str(exc)[:500]}")),
        ) from exc


@router.get("/profiles")
async def list_profiles() -> dict[str, Any]:
    profiles = evaluator_registry.list_profiles()
    return {"items": [item.model_dump(mode="json") for item in profiles], "total": len(profiles)}


@router.get("/evaluators")
async def list_evaluators() -> dict[str, Any]:
    evaluators = evaluator_registry.list_specs()
    return {"items": [item.model_dump(mode="json") for item in evaluators], "total": len(evaluators)}


@router.get("/settings/langsmith")
async def get_langsmith_settings() -> dict[str, Any]:
    return get_evaluation_settings_store().public()


@router.put("/settings/langsmith")
async def update_langsmith_settings(body: LangSmithSettingsRequest) -> dict[str, Any]:
    updates = body.model_dump(exclude={"clear_api_key"}, exclude_none=True)
    try:
        return await run_in_threadpool(
            get_evaluation_settings_store().update,
            updates,
            clear_api_key=body.clear_api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/settings/langsmith/test")
async def test_langsmith_settings() -> dict[str, Any]:
    settings = get_evaluation_settings_store().load()
    # Connection testing and automatic projection are separate concerns. A
    # disabled backend may still be tested safely before the user enables it.
    if not settings.api_key:
        raise HTTPException(status_code=409, detail="LangSmith API Key is not configured")
    try:
        result = await run_in_threadpool(LangSmithDatasetAdapter(get_evaluation_repository(), settings).test_connection)
        return {**result, "projection_enabled": settings.enabled}
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(_redact(f"LangSmith connection failed: {type(exc).__name__}: {str(exc)[:500]}")),
        ) from exc


@router.get("/experiments")
async def list_experiments() -> dict[str, Any]:
    experiments = await run_in_threadpool(get_evaluation_repository().list_experiments)
    return {"items": [item.model_dump(mode="json") for item in experiments], "total": len(experiments)}


@router.post("/experiments", status_code=202)
async def create_experiment(body: CreateExperimentRequest) -> dict[str, Any]:
    repository = get_evaluation_repository()
    try:
        bundle = await run_in_threadpool(repository.export_bundle, body.dataset_id, body.dataset_version)
        if not bundle.version_id or not bundle.checksum:
            raise ConflictError("Experiment requires an immutable published Dataset version")
        if body.profile_id != bundle.dataset.default_profile:
            raise ConflictError("Phase 1 Experiments must use the evaluator profile frozen with the Dataset version")
        is_swebench = any(
            case.enabled and case.code is not None and case.code.repository.kind == "swebench"
            for case in bundle.dataset.cases
        )
        if is_swebench:
            if body.execution.repetitions != 1:
                raise ConflictError("SWE-bench Phase 1 requires repetitions=1 for unambiguous official scoring")
            verifier_status = await probe_official_swebench_runtime()
            if not verifier_status["available"]:
                raise ConflictError(
                    "SWE-bench Docker Verifier is unavailable: " + str(verifier_status.get("reason") or "unknown")
                )
        candidate = await run_in_threadpool(resolve_candidate, BASE_DIR, body.candidate_request)
        candidate = bind_candidate_capability(candidate, body.profile_id)
        experiment = EvalExperiment(
            name=body.name,
            dataset_id=body.dataset_id,
            dataset_version=body.dataset_version,
            dataset_version_id=bundle.version_id,
            dataset_content_hash=bundle.checksum,
            candidate=candidate,
            profile_id=body.profile_id,
            execution=body.execution,
            summary={
                "progress": {
                    "stage": "queued",
                    "message": "评测已入队，正在等待隔离 Worker",
                    "total": sum(1 for case in bundle.dataset.cases if case.enabled)
                    * body.execution.repetitions,
                    "completed": 0,
                    "failed": 0,
                    "updated_at": utc_now().isoformat(),
                }
            },
        )
        created = await run_in_threadpool(repository.create_experiment, experiment)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await evaluation_worker_manager.start(created.experiment_id)
    return created.model_dump(mode="json")


@router.get("/experiments/{experiment_id}")
async def get_experiment(experiment_id: str) -> dict[str, Any]:
    try:
        experiment = await run_in_threadpool(get_evaluation_repository().get_experiment, experiment_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return experiment.model_dump(mode="json")


@router.delete("/experiments/{experiment_id}", status_code=204)
async def delete_experiment(experiment_id: str) -> Response:
    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        if experiment.status not in {"completed", "failed", "cancelled"}:
            raise ConflictError("Only terminal Experiments can be deleted; cancel the Experiment first")
        if not await evaluation_worker_manager.delete_artifacts(experiment_id):
            raise ConflictError("Experiment Worker is still running; cancel the Experiment first")
        await run_in_threadpool(repository.delete_experiment, experiment_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return Response(status_code=204)


@router.get("/experiments/{experiment_id}/results")
async def get_experiment_results(experiment_id: str) -> dict[str, Any]:
    repository = get_evaluation_repository()
    try:
        await run_in_threadpool(repository.get_experiment, experiment_id)
        items = await run_in_threadpool(repository.list_results, experiment_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return {"items": items, "total": len(items)}


@router.get("/experiments/{experiment_id}/export/swebench", response_class=PlainTextResponse)
async def export_swebench_predictions(experiment_id: str) -> PlainTextResponse:
    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        dataset = (
            await run_in_threadpool(
                repository.export_bundle,
                experiment.dataset_id,
                experiment.dataset_version,
            )
        ).dataset
        envelopes = await run_in_threadpool(repository.load_run_envelopes, experiment_id)
        content = await run_in_threadpool(
            prediction_jsonl,
            dataset,
            envelopes,
            model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PlainTextResponse(
        content,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": (f'attachment; filename="{experiment_id}-swebench-predictions.jsonl"')},
    )


@router.get("/experiments/{experiment_id}/swebench/manifest")
async def get_swebench_run_manifest(experiment_id: str) -> dict[str, Any]:
    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        dataset = (
            await run_in_threadpool(repository.export_bundle, experiment.dataset_id, experiment.dataset_version)
        ).dataset
        envelopes = await run_in_threadpool(repository.load_run_envelopes, experiment_id)
        return await run_in_threadpool(
            swebench_run_manifest,
            dataset,
            envelopes,
            model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
            experiment_id=experiment.experiment_id,
            dataset_version_id=experiment.dataset_version_id,
            dataset_content_hash=experiment.dataset_content_hash,
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _recompute_official_summary(
    dataset: EvalDataset,
    result_rows: list[dict[str, Any]],
    previous: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    attempts: dict[str, dict[str, Any]] = {}
    cases = {case.case_id: case for case in dataset.cases}
    for row in result_rows:
        attempt = attempts.setdefault(
            row["attempt_id"],
            {"case_id": row["case_id"], "status": row["attempt_status"], "results": []},
        )
        if row["result"] is not None:
            attempt["results"].append(EvaluationResult.model_validate(row["result"]))
    summaries = []
    for attempt in attempts.values():
        case = cases[attempt["case_id"]]
        item = evaluator_registry.summarize(case, attempt["results"])
        if attempt["status"] != "completed":
            item["verdict"] = "fail"
        summaries.append({**item, "status": attempt["status"], "results": attempt["results"]})
    dimensions: dict[str, dict[str, Any]] = {}
    for attempt in summaries:
        for result in attempt["results"]:
            bucket = dimensions.setdefault(
                str(result.dimension),
                {
                    "sample_count": 0,
                    "applicable_count": 0,
                    "pass_count": 0,
                    "fail_count": 0,
                    "not_applicable_count": 0,
                    "not_evaluated_count": 0,
                    "error_count": 0,
                    "scores": [],
                    "evaluator_versions": set(),
                },
            )
            outcome = str(result.outcome)
            bucket["sample_count"] += 1
            bucket[f"{outcome}_count"] += 1
            if outcome in {"pass", "fail"}:
                bucket["applicable_count"] += 1
            if result.score is not None:
                bucket["scores"].append(result.score)
            bucket["evaluator_versions"].add(f"{result.evaluator_id}@{result.evaluator_version}")
    dimension_summary: dict[str, Any] = {}
    for dimension, bucket in dimensions.items():
        scores = bucket.pop("scores")
        versions = sorted(bucket.pop("evaluator_versions"))
        expected = bucket["sample_count"] - bucket["not_applicable_count"]
        dimension_summary[dimension] = {
            **bucket,
            "score": sum(scores) / len(scores) if scores else None,
            "coverage": bucket["applicable_count"] / expected if expected else None,
            "evaluator_versions": versions,
        }
    failed_attempts = sum(item["status"] != "completed" for item in summaries)
    summary = {
        **previous,
        "case_attempts": len(summaries),
        "completed_attempts": sum(item["status"] == "completed" for item in summaries),
        "failed_attempts": failed_attempts,
        "determinate": sum(item["verdict"] != "indeterminate" for item in summaries),
        "passed": sum(item["verdict"] == "pass" for item in summaries),
        "failed": sum(item["verdict"] == "fail" for item in summaries),
        "critical_failures": sum(bool(item["critical_failure"]) for item in summaries),
        "indeterminate": sum(item["verdict"] == "indeterminate" for item in summaries),
        "dimensions": dimension_summary,
    }
    summary["applicable_count"] = sum(item["applicable_count"] for item in dimension_summary.values())
    expected_metrics = sum(
        item["sample_count"] - item["not_applicable_count"] for item in dimension_summary.values()
    )
    summary["coverage"] = summary["applicable_count"] / expected_metrics if expected_metrics else None
    verdict = (
        "fail"
        if summary["failed"] or summary["critical_failures"] or failed_attempts
        else "indeterminate"
        if summary["indeterminate"]
        else "pass"
    )
    return summary, verdict


@router.post("/experiments/{experiment_id}/results/swebench")
async def import_official_swebench_results(
    experiment_id: str,
    body: SWEbenchResultImportRequest,
) -> dict[str, Any]:
    raise HTTPException(
        status_code=410,
        detail="Manual SWE-bench report import is retired; the Evaluation Worker now runs the official Docker Harness",
    )

    # Compatibility code below is intentionally unreachable for one release;
    # it can be deleted after old clients have migrated to managed verification.
    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        if experiment.status != "completed":
            raise ConflictError("Official SWE-bench results require a completed Experiment")
        dataset = (
            await run_in_threadpool(
                repository.export_bundle,
                experiment.dataset_id,
                experiment.dataset_version,
            )
        ).dataset
        envelopes = await run_in_threadpool(repository.load_run_envelopes, experiment_id)
        manifest = swebench_prediction_manifest(
            dataset,
            envelopes,
            model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
        )
        if manifest["missing_instance_ids"]:
            raise ConflictError(
                "Cannot score an incomplete SWE-bench prediction set: "
                + ", ".join(manifest["missing_instance_ids"][:20])
            )
        expected_manifest = swebench_run_manifest(
            dataset,
            envelopes,
            model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
            experiment_id=experiment.experiment_id,
            dataset_version_id=experiment.dataset_version_id,
            dataset_content_hash=experiment.dataset_content_hash,
        )
        if body.manifest != expected_manifest:
            raise ValueError("SWE-bench report manifest does not match this frozen Dataset and prediction set")
        official = parse_official_swebench_results(body.content)
        expected_ids = {item["instance_id"] for item in manifest["predictions"]}
        if set(official) != expected_ids:
            missing = sorted(expected_ids - set(official))
            unknown = sorted(set(official) - expected_ids)
            raise ValueError(f"Official report coverage mismatch; missing={missing[:20]}, unknown={unknown[:20]}")
        report_sha256 = hashlib.sha256(body.content.encode("utf-8")).hexdigest()
        manifest_sha256 = hashlib.sha256(
            json.dumps(expected_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        prior_import = experiment.summary.get("swebench_manual_report")
        if isinstance(prior_import, dict) and prior_import.get("report_sha256") != report_sha256:
            raise ConflictError("A different manual SWE-bench report is already attached to this Experiment")
        resolved_count = 0
        for prediction in manifest["predictions"]:
            instance_id = prediction["instance_id"]
            attempt_id = prediction["attempt_id"]
            envelope = next(item for item in envelopes[instance_id] if str(item.get("_attempt_id") or "") == attempt_id)
            checked_run = AgentRunEnvelope.model_validate(
                {key: value for key, value in envelope.items() if not key.startswith("_")}
            )
            official_result = official[instance_id]
            resolved = bool(official_result["resolved"])
            resolved_count += int(resolved)
            verification = dict(checked_run.metadata.get("code_verification") or {})
            verification.update(
                {
                    "status": "not_evaluated",
                    "passed": None,
                    "reason": "Manual SWE-bench report attached; provenance is not platform-verified",
                    "manual_unverified_result": official_result,
                }
            )
            checked_run = checked_run.model_copy(
                update={
                    "metadata": {
                        **checked_run.metadata,
                        "code_verification": verification,
                    }
                }
            )
            result = EvaluationResult(
                evaluator_id="code_verification.v1",
                evaluator_version="1",
                dimension=EvaluationDimension.TASK_COMPLETION,
                outcome=EvaluationOutcome.NOT_EVALUATED,
                score=None,
                passed=None,
                reason=verification["reason"],
                evidence=[
                    EvidenceReference(
                        kind="swebench_manual_unverified_result",
                        summary=f"instance_id={instance_id}; reported_resolved={resolved}; provenance=manual_unverified",
                    )
                ],
                metadata={
                    "provenance": "manual_unverified",
                    "reported_result": official_result,
                    "report_sha256": report_sha256,
                    "manifest_sha256": manifest_sha256,
                },
            )
            await run_in_threadpool(repository.update_attempt_run, attempt_id, checked_run)
            await run_in_threadpool(repository.save_result, experiment_id, attempt_id, result)
        total = len(manifest["predictions"])
        summary, verdict = _recompute_official_summary(
            dataset,
            await run_in_threadpool(repository.list_results, experiment_id),
            experiment.summary,
        )
        summary.update({
            "swebench_manual_report": {
                "status": "manual_unverified",
                "reported_resolved": resolved_count,
                "reported_unresolved": total - resolved_count,
                "total": total,
                "reported_resolve_rate": resolved_count / total,
                "source_snapshot_sha256": dataset.metadata.get("source_snapshot_sha256"),
                "manifest": expected_manifest,
                "manifest_sha256": manifest_sha256,
                "report_sha256": report_sha256,
            },
        })
        updated = experiment.model_copy(
            update={
                "verdict": verdict,
                "summary": summary,
            }
        )
        updated = await run_in_threadpool(repository.update_experiment, updated)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return updated.model_dump(mode="json")


@router.post("/experiments/{experiment_id}/cancel")
async def cancel_experiment(experiment_id: str) -> dict[str, Any]:
    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        if experiment.status in {"completed", "failed", "cancelled"}:
            raise ConflictError("Experiment is already terminal")
        updated = experiment.model_copy(
            update={
                "status": "cancel_requested",
                "summary": {
                    **experiment.summary,
                    "progress": {
                        **dict(experiment.summary.get("progress") or {}),
                        "stage": "cancel_requested",
                        "message": "正在停止 Worker 并清理隔离运行环境",
                        "updated_at": utc_now().isoformat(),
                    },
                },
            }
        )
        updated = await run_in_threadpool(repository.update_experiment, updated, expected_status=experiment.status)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    terminated = await evaluation_worker_manager.cancel(experiment_id)
    if terminated or experiment.status == "queued":
        updated = updated.model_copy(update={"status": "cancelled", "finished_at": utc_now()})
        await run_in_threadpool(
            repository.cancel_running_attempts,
            experiment_id,
            "Experiment process terminated by user",
        )
        try:
            updated = await run_in_threadpool(
                repository.update_experiment,
                updated,
                expected_status="cancel_requested",
            )
        except ConflictError:
            latest = await run_in_threadpool(repository.get_experiment, experiment_id)
            if latest.status != "cancelled":
                raise
            updated = latest
    return updated.model_dump(mode="json")


@router.post("/experiments/{experiment_id}/sync/langsmith")
async def retry_experiment_projection(experiment_id: str) -> dict[str, Any]:
    settings = get_evaluation_settings_store().load()
    if not settings.enabled or not settings.api_key:
        raise HTTPException(status_code=409, detail="LangSmith evaluation backend is disabled")
    try:
        runner = EvaluationRunner(get_evaluation_repository(), settings, BASE_DIR)
        experiment = await runner.retry_projection(experiment_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(_redact(f"LangSmith projection failed: {type(exc).__name__}: {str(exc)[:500]}")),
        ) from exc
    return experiment.model_dump(mode="json")


@router.post("/experiments/{experiment_id}/retry", status_code=202)
async def retry_experiment(experiment_id: str) -> dict[str, Any]:
    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        if experiment.status not in {"completed", "failed", "cancelled"}:
            raise ConflictError("Only terminal Experiments can be retried")
        dataset = await run_in_threadpool(
            repository.get_dataset,
            experiment.dataset_id,
            experiment.dataset_version,
        )
        total_attempts = (
            sum(1 for case in dataset.cases if case.enabled)
            * experiment.execution.repetitions
        )
        # A retry is a new execution against the current Agent implementation,
        # not a replay of a stale source snapshot. Freeze a fresh Candidate so
        # the audit record remains truthful after a deployment/code change.
        candidate_request = CandidateRequest(
            name=experiment.candidate.name,
            llm_model_id=experiment.candidate.llm_model_id,
            thinking_level=experiment.candidate.thinking_level,
            credential_name=experiment.candidate.credential_name,
            analytics_model_id=experiment.candidate.analytics_model_id,
        )
        candidate = await run_in_threadpool(resolve_candidate, BASE_DIR, candidate_request)
        candidate = bind_candidate_capability(candidate, experiment.profile_id)
        retry_root_id = str(experiment.summary.get("retry_root_experiment_id") or experiment.experiment_id)
        retry_generation = int(experiment.summary.get("retry_generation") or 0) + 1
        clean_name = re.sub(r"(?:\s*\(retry\))+\s*$", "", experiment.name).strip() or experiment.name
        is_swebench = any(
            case.enabled and case.code is not None and case.code.repository.kind == "swebench"
            for case in dataset.cases
        )
        execution = experiment.execution
        timeout_policy_migrated_from: int | None = None
        if is_swebench and execution.timeout_seconds == 300:
            # 300s was the old undifferentiated UI default. Existing SWE
            # Experiments retried after this release adopt the coding default;
            # newly created API runs can still explicitly choose another budget.
            timeout_policy_migrated_from = execution.timeout_seconds
            execution = execution.model_copy(update={"timeout_seconds": 900})
        retried = experiment.model_copy(
            update={
                "experiment_id": new_id("exp"),
                "name": clean_name,
                "candidate": candidate,
                "execution": execution,
                "status": "queued",
                "verdict": "pending",
                "error": None,
                "started_at": None,
                "finished_at": None,
                "remote_experiment_id": None,
                "remote_url": None,
                "summary": {
                    "retry_of_experiment_id": experiment.experiment_id,
                    "retry_root_experiment_id": retry_root_id,
                    "retry_generation": retry_generation,
                    **(
                        {
                            "timeout_policy_migrated_from_seconds": timeout_policy_migrated_from,
                            "timeout_policy_seconds": execution.timeout_seconds,
                        }
                        if timeout_policy_migrated_from is not None
                        else {}
                    ),
                    "progress": {
                        "stage": "queued",
                        "message": "重新评测已入队，正在等待隔离 Worker",
                        "total": total_attempts,
                        "completed": 0,
                        "failed": 0,
                        "updated_at": utc_now().isoformat(),
                    }
                },
                "created_at": utc_now(),
            }
        )
        retried = await run_in_threadpool(repository.create_experiment, retried)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    await evaluation_worker_manager.start(retried.experiment_id)
    return retried.model_dump(mode="json")


@router.post("/experiments/{experiment_id}/verify/swebench", status_code=202)
async def rerun_swebench_verifier(experiment_id: str) -> dict[str, Any]:
    """Re-run only the official verifier against persisted Agent patches."""

    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        if experiment.status not in {"completed", "failed", "cancelled"}:
            raise ConflictError("Only terminal Experiments can be re-verified")
        dataset = await run_in_threadpool(
            repository.get_dataset,
            experiment.dataset_id,
            experiment.dataset_version,
        )
        if dataset.current_version_id != experiment.dataset_version_id:
            raise ConflictError("Pinned Dataset version identity mismatch")
        swebench_cases = [
            case
            for case in dataset.cases
            if case.enabled
            and case.code is not None
            and case.code.repository.swebench is not None
        ]
        if not swebench_cases:
            raise ConflictError("Experiment has no enabled SWE-bench Cases")
        envelopes = await run_in_threadpool(repository.load_run_envelopes, experiment_id)
        manifest = swebench_prediction_manifest(
            dataset,
            envelopes,
            model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
        )
        if not manifest["predictions"]:
            raise ConflictError("Experiment has no persisted SWE-bench patch to verify")
        result_rows = await run_in_threadpool(repository.list_results, experiment_id)
        completed_attempt_ids = {
            str(row["attempt_id"])
            for row in result_rows
            if row.get("attempt_status") == "completed"
        }
        selected_attempt_ids = {
            str(item["attempt_id"]) for item in manifest["predictions"]
        }
        if not selected_attempt_ids or not selected_attempt_ids <= completed_attempt_ids:
            raise ConflictError("Persisted patches must belong to completed Attempts")
        verifier_status = await probe_official_swebench_runtime()
        if not verifier_status["available"]:
            raise ConflictError(
                "SWE-bench Docker Verifier is unavailable: "
                + str(verifier_status.get("reason") or "unknown")
            )
        patch_sha256 = {
            str(item["instance_id"]): hashlib.sha256(
                str(item["model_patch"]).encode("utf-8")
            ).hexdigest()
            for item in manifest["predictions"]
        }
        envelope_by_attempt_id = {
            str(envelope.get("_attempt_id") or ""): envelope
            for case_envelopes in envelopes.values()
            for envelope in case_envelopes
        }
        previous_instance_results: dict[str, dict[str, Any]] = {}
        for prediction in manifest["predictions"]:
            envelope = envelope_by_attempt_id.get(str(prediction["attempt_id"])) or {}
            verification = (envelope.get("metadata") or {}).get("code_verification") or {}
            previous_instance_results[str(prediction["instance_id"])] = {
                "status": verification.get("status"),
                "passed": verification.get("passed"),
                "reason": str(verification.get("reason") or "")[:500],
                "patch_sha256": patch_sha256[str(prediction["instance_id"])],
            }
        previous_replay = dict(experiment.summary.get("swebench_verifier_replay") or {})
        generation = int(previous_replay.get("generation") or 0) + 1
        requested_at = utc_now()
        projection_was_published = bool(
            experiment.remote_experiment_id
            or experiment.summary.get("experiment_projection") == "synced"
        )
        replay = {
            "generation": generation,
            "status": "queued",
            "requested_at": requested_at.isoformat(),
            "total": len(manifest["predictions"]),
            "dataset_total": len(swebench_cases),
            "missing_instance_ids": list(manifest["missing_instance_ids"]),
            "source_attempt_ids": sorted(selected_attempt_ids),
            "patch_sha256": patch_sha256,
            "previous_status": str(experiment.status),
            "previous_verdict": experiment.verdict,
            "previous_started_at": (
                experiment.started_at.isoformat() if experiment.started_at else None
            ),
            "previous_finished_at": (
                experiment.finished_at.isoformat() if experiment.finished_at else None
            ),
            "previous_report_sha256": (
                (experiment.summary.get("swebench_official_harness") or {}).get(
                    "report_sha256"
                )
            ),
            "previous_instance_results": previous_instance_results,
            "langsmith_projection_was_published": projection_was_published,
        }
        summary = {
            **experiment.summary,
            "execution_mode": "official_verifier_replay",
            "swebench_verifier_replay": replay,
            "swebench_official_harness": {
                "status": "queued",
                "total": len(manifest["predictions"]),
                "resolved": 0,
            },
            "progress": {
                "stage": "queued",
                "message": "已保存的 Agent patch 正在等待 Docker 重新判卷",
                "total": len(manifest["predictions"]),
                "completed": 0,
                "failed": 0,
                "updated_at": requested_at.isoformat(),
            },
        }
        if projection_was_published:
            summary["experiment_projection"] = "stale_after_verifier_replay"
            summary["langsmith_projection"] = "stale_after_verifier_replay"
        queued = experiment.model_copy(
            update={
                "status": "queued",
                "verdict": "pending",
                "error": None,
                "started_at": None,
                "finished_at": None,
                "summary": summary,
            }
        )
        queued = await run_in_threadpool(
            repository.update_experiment,
            queued,
            expected_status=experiment.status,
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await evaluation_worker_manager.start(experiment_id)
    return queued.model_dump(mode="json")


@router.post("/experiments/{experiment_id}/resume/swebench", status_code=202)
async def resume_missing_swebench_cases(experiment_id: str) -> dict[str, Any]:
    """Run only SWE-bench Cases that do not yet have a persisted valid patch."""

    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        if experiment.status not in {"completed", "failed", "cancelled"}:
            raise ConflictError("Only terminal Experiments can resume missing SWE-bench Cases")
        dataset = await run_in_threadpool(
            repository.get_dataset,
            experiment.dataset_id,
            experiment.dataset_version,
        )
        if dataset.current_version_id != experiment.dataset_version_id:
            raise ConflictError("Pinned Dataset version identity mismatch")
        swebench_cases = [
            case
            for case in dataset.cases
            if case.enabled
            and case.code is not None
            and case.code.repository.swebench is not None
        ]
        if not swebench_cases:
            raise ConflictError("Experiment has no enabled SWE-bench Cases")
        envelopes = await run_in_threadpool(repository.load_run_envelopes, experiment_id)
        manifest = swebench_prediction_manifest(
            dataset,
            envelopes,
            model_name_or_path=(experiment.candidate.llm_model_id or experiment.candidate.name),
        )
        missing_instance_ids = list(manifest["missing_instance_ids"])
        if not missing_instance_ids:
            raise ConflictError("Every enabled SWE-bench Case already has a persisted patch")

        # A resumed Case executes the Agent code that is deployed now. Freeze
        # that Candidate explicitly while retaining per-Attempt candidate IDs
        # and resume lineage for the already persisted patches.
        candidate_request = CandidateRequest(
            name=experiment.candidate.name,
            llm_model_id=experiment.candidate.llm_model_id,
            thinking_level=experiment.candidate.thinking_level,
            credential_name=experiment.candidate.credential_name,
            analytics_model_id=experiment.candidate.analytics_model_id,
        )
        candidate = await run_in_threadpool(resolve_candidate, BASE_DIR, candidate_request)
        candidate = bind_candidate_capability(candidate, experiment.profile_id)
        previous_resume = dict(experiment.summary.get("swebench_case_resume") or {})
        generation = int(previous_resume.get("generation") or 0) + 1
        requested_at = utc_now()
        projection_was_published = bool(
            experiment.remote_experiment_id
            or experiment.summary.get("experiment_projection") == "synced"
        )
        source_attempt_ids = sorted(
            str(item["attempt_id"]) for item in manifest["predictions"]
        )
        resume = {
            "generation": generation,
            "status": "queued",
            "requested_at": requested_at.isoformat(),
            "missing_instance_ids": missing_instance_ids,
            "source_attempt_ids": source_attempt_ids,
            "persisted_patch_count": len(manifest["predictions"]),
            "dataset_total": len(swebench_cases),
            "previous_candidate_id": experiment.candidate.candidate_id,
            "previous_candidate_fingerprint": experiment.candidate.fingerprint,
            "resume_candidate_id": candidate.candidate_id,
            "resume_candidate_fingerprint": candidate.fingerprint,
            "mixed_candidate_attempts": bool(source_attempt_ids),
            "previous_status": str(experiment.status),
            "previous_verdict": experiment.verdict,
            "langsmith_projection_was_published": projection_was_published,
        }
        summary = {
            **experiment.summary,
            "execution_mode": "swebench_missing_case_resume",
            "swebench_case_resume": resume,
            "swebench_missing_predictions": len(missing_instance_ids),
            "progress": {
                "stage": "queued",
                "message": f"正在等待补跑 {len(missing_instance_ids)} 个缺失 Patch 的 Case",
                "total": len(missing_instance_ids),
                "completed": 0,
                "failed": 0,
                "updated_at": requested_at.isoformat(),
            },
        }
        if projection_was_published:
            summary["experiment_projection"] = "stale_after_case_resume"
            summary["langsmith_projection"] = "stale_after_case_resume"
        queued = experiment.model_copy(
            update={
                "candidate": candidate,
                "status": "queued",
                "verdict": "pending",
                "error": None,
                "started_at": None,
                "finished_at": None,
                "summary": summary,
            }
        )
        queued = await run_in_threadpool(
            repository.update_experiment,
            queued,
            expected_status=experiment.status,
        )
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await evaluation_worker_manager.start(experiment_id)
    return queued.model_dump(mode="json")
