"""General Agent Evaluation API, separate from Skill review endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from evaluation.candidate import CandidateRequest, resolve_candidate
from evaluation.contracts import (
    EvalCase,
    EvalDataset,
    EvalExperiment,
    ExecutionPolicy,
    new_id,
    protocol_json_schemas,
    utc_now,
)
from evaluation.dataset_io import export_dataset, import_dataset
from evaluation.evaluators import evaluator_registry
from evaluation.langsmith_backend import LangSmithDatasetAdapter, _redact
from evaluation.repository import (
    ConflictError,
    EvaluationRepositoryError,
    NotFoundError,
    ValidationError,
    get_evaluation_repository,
)
from evaluation.runner import EvaluationRunner
from evaluation.settings import get_evaluation_settings_store
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
        versions = await run_in_threadpool(
            get_evaluation_repository().list_dataset_versions, dataset_id
        )
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
        content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{dataset_id}.{extension}"'}
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
    if not settings.enabled or not settings.api_key:
        raise HTTPException(status_code=409, detail="LangSmith evaluation backend is disabled or not configured")
    try:
        return await run_in_threadpool(LangSmithDatasetAdapter(get_evaluation_repository(), settings).test_connection)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(
                _redact(f"LangSmith connection failed: {type(exc).__name__}: {str(exc)[:500]}")
            ),
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
            raise ConflictError(
                "Phase 1 Experiments must use the evaluator profile frozen with the Dataset version"
            )
        candidate = await run_in_threadpool(resolve_candidate, BASE_DIR, body.candidate_request)
        experiment = EvalExperiment(
            name=body.name,
            dataset_id=body.dataset_id,
            dataset_version=body.dataset_version,
            dataset_version_id=bundle.version_id,
            dataset_content_hash=bundle.checksum,
            candidate=candidate,
            profile_id=body.profile_id,
            execution=body.execution,
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


@router.get("/experiments/{experiment_id}/results")
async def get_experiment_results(experiment_id: str) -> dict[str, Any]:
    repository = get_evaluation_repository()
    try:
        await run_in_threadpool(repository.get_experiment, experiment_id)
        items = await run_in_threadpool(repository.list_results, experiment_id)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    return {"items": items, "total": len(items)}


@router.post("/experiments/{experiment_id}/cancel")
async def cancel_experiment(experiment_id: str) -> dict[str, Any]:
    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        if experiment.status in {"completed", "failed", "cancelled"}:
            raise ConflictError("Experiment is already terminal")
        updated = experiment.model_copy(update={"status": "cancel_requested"})
        updated = await run_in_threadpool(
            repository.update_experiment, updated, expected_status=experiment.status
        )
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
            detail=str(
                _redact(f"LangSmith projection failed: {type(exc).__name__}: {str(exc)[:500]}")
            ),
        ) from exc
    return experiment.model_dump(mode="json")


@router.post("/experiments/{experiment_id}/retry", status_code=202)
async def retry_experiment(experiment_id: str) -> dict[str, Any]:
    repository = get_evaluation_repository()
    try:
        experiment = await run_in_threadpool(repository.get_experiment, experiment_id)
        if experiment.status not in {"failed", "cancelled"}:
            raise ConflictError("Only failed or cancelled Experiments can be retried")
        retried = experiment.model_copy(
            update={
                "experiment_id": new_id("exp"),
                "name": f"{experiment.name} (retry)",
                "status": "queued",
                "verdict": "pending",
                "error": None,
                "started_at": None,
                "finished_at": None,
                "remote_experiment_id": None,
                "remote_url": None,
                "summary": {},
                "created_at": utc_now(),
            }
        )
        retried = await run_in_threadpool(repository.create_experiment, retried)
    except EvaluationRepositoryError as exc:
        _raise_repository_error(exc)
    await evaluation_worker_manager.start(retried.experiment_id)
    return retried.model_dump(mode="json")
