"""SWE-bench Dataset import, frozen fixtures, and official result interchange."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from .contracts import (
    CodeEvaluationSpec,
    CodeRepositorySpec,
    CodeVerificationSpec,
    EvalCase,
    EvalDataset,
    EvalInput,
    SWEbenchReference,
)

DEFAULT_DATASET = "princeton-nlp/SWE-bench_Verified"
MAX_REMOTE_IMPORT = 500
REMOTE_PAGE_SIZE = 100
MAX_IMPORT_BYTES = 20 * 1024 * 1024


def _json_list(value: Any, *, field: str, instance_id: str) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"SWE-bench {instance_id} has malformed {field}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"SWE-bench {instance_id} {field} must be a JSON list")
    return [str(item) for item in parsed]


def parse_swebench_content(content: str) -> list[dict[str, Any]]:
    if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("SWE-bench import exceeds 20 MiB")
    stripped = content.strip()
    if not stripped:
        raise ValueError("SWE-bench import content is empty")
    if stripped.startswith("["):
        value = json.loads(stripped)
        if not isinstance(value, list):
            raise ValueError("SWE-bench JSON must be an array")
        rows = value
    else:
        rows = [json.loads(line) for line in stripped.splitlines() if line.strip()]
    normalized = []
    for item in rows:
        row = item.get("row") if isinstance(item, dict) and isinstance(item.get("row"), dict) else item
        if not isinstance(row, dict):
            raise ValueError("Every SWE-bench row must be an object")
        normalized.append(row)
    return normalized


def fetch_swebench_rows(
    *,
    dataset_name: str = DEFAULT_DATASET,
    split: str = "test",
    offset: int = 0,
    limit: int = 10,
    timeout_seconds: int = 30,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= MAX_REMOTE_IMPORT:
        raise ValueError(f"SWE-bench remote import limit must be 1-{MAX_REMOTE_IMPORT}")
    collected: list[dict[str, Any]] = []
    total_bytes = 0
    with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
        while len(collected) < limit:
            response = client.get(
                "https://datasets-server.huggingface.co/rows",
                params={
                    "dataset": dataset_name,
                    "config": "default",
                    "split": split,
                    "offset": offset + len(collected),
                    "length": min(REMOTE_PAGE_SIZE, limit - len(collected)),
                },
            )
            response.raise_for_status()
            total_bytes += len(response.content)
            if total_bytes > MAX_IMPORT_BYTES:
                raise ValueError("SWE-bench Dataset Server response exceeds 20 MiB")
            payload = response.json()
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Hugging Face Dataset Server returned no rows")
            page = parse_swebench_content(json.dumps(rows, ensure_ascii=False))
            collected.extend(page)
            total_rows = int(payload.get("num_rows_total") or 0)
            if not page or (total_rows and offset + len(collected) >= total_rows):
                break
    return collected


def swebench_dataset_from_rows(
    rows: list[dict[str, Any]],
    *,
    dataset_name: str = DEFAULT_DATASET,
    split: str = "test",
    name: str | None = None,
) -> EvalDataset:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        required = ["instance_id", "repo", "base_commit", "problem_statement"]
        missing = [key for key in required if not str(row.get(key) or "").strip()]
        if missing:
            raise ValueError(f"SWE-bench row {index} is missing fields: {', '.join(missing)}")
        instance_id = str(row["instance_id"])
        if instance_id in seen:
            raise ValueError(f"Duplicate SWE-bench instance_id: {instance_id}")
        seen.add(instance_id)
        row_without_gold = {key: value for key, value in row.items() if key != "patch"}
        row_hash = hashlib.sha256(
            json.dumps(
                row_without_gold,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        reference = SWEbenchReference(
            dataset_name=dataset_name,
            split=split,
            instance_id=instance_id,
            repo=str(row["repo"]),
            base_commit=str(row["base_commit"]),
            version=str(row.get("version") or "") or None,
            environment_setup_commit=str(row.get("environment_setup_commit") or "") or None,
            test_patch=str(row.get("test_patch") or ""),
            fail_to_pass=_json_list(row.get("FAIL_TO_PASS"), field="FAIL_TO_PASS", instance_id=instance_id),
            pass_to_pass=_json_list(row.get("PASS_TO_PASS"), field="PASS_TO_PASS", instance_id=instance_id),
        )
        cases.append(
            EvalCase(
                case_id=instance_id,
                name=instance_id,
                description=f"SWE-bench issue from {reference.repo} at {reference.base_commit[:12]}",
                input=EvalInput(message=str(row["problem_statement"])),
                dimensions=["task_completion", "tool_use", "trajectory", "safety", "robustness"],
                code=CodeEvaluationSpec(
                    repository=CodeRepositorySpec(kind="swebench", swebench=reference),
                    verification=CodeVerificationSpec(mode="swebench", require_patch=True),
                ),
                criticality="critical",
                data_classification="public",
                tags=["coding", "swebench", reference.repo],
                metadata={
                    "source": "swebench",
                    "instance_id": instance_id,
                    "hints_text": str(row.get("hints_text") or ""),
                    "gold_patch_omitted": True,
                    "source_row_sha256": row_hash,
                },
            )
        )
    if not cases:
        raise ValueError("SWE-bench import produced no cases")
    snapshot_hash = hashlib.sha256(
        "\n".join(str(case.metadata["source_row_sha256"]) for case in cases).encode("utf-8")
    ).hexdigest()
    return EvalDataset(
        name=name or f"SWE-bench {split}",
        description="SWE-bench coding tasks; gold patches are omitted and scoring uses the official Harness.",
        default_profile="coding_agent@1",
        tags=["coding", "swebench"],
        metadata={
            "source": "swebench",
            "dataset_name": dataset_name,
            "split": split,
            "gold_patch_policy": "omitted",
            "source_snapshot_sha256": snapshot_hash,
        },
        cases=cases,
    )


def _selected_prediction_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for attempt in reversed(attempts):
        verification = (attempt.get("metadata") or {}).get("code_verification") or {}
        if (
            attempt.get("outcome") == "completed"
            and verification.get("mode") == "swebench"
            and verification.get("status") in {"not_evaluated", "passed", "failed", "error"}
            and str(verification.get("patch") or "").strip()
        ):
            return attempt
    return None


def swebench_prediction_manifest(
    dataset: EvalDataset,
    run_envelopes: dict[str, list[dict[str, Any]]],
    *,
    model_name_or_path: str,
) -> dict[str, Any]:
    predictions: list[dict[str, str]] = []
    missing: list[str] = []
    for case in dataset.cases:
        if not case.enabled or case.code is None or case.code.repository.swebench is None:
            continue
        attempt = _selected_prediction_attempt(run_envelopes.get(case.case_id) or [])
        if attempt is None:
            missing.append(case.code.repository.swebench.instance_id)
            continue
        verification = (attempt.get("metadata") or {}).get("code_verification") or {}
        predictions.append(
            {
                "instance_id": case.code.repository.swebench.instance_id,
                "model_patch": str(verification["patch"]),
                "model_name_or_path": model_name_or_path,
                "attempt_id": str(attempt.get("_attempt_id") or ""),
            }
        )
    return {"predictions": predictions, "missing_instance_ids": missing}


def prediction_jsonl(
    dataset: EvalDataset,
    run_envelopes: dict[str, list[dict[str, Any]]],
    *,
    model_name_or_path: str,
) -> str:
    manifest = swebench_prediction_manifest(dataset, run_envelopes, model_name_or_path=model_name_or_path)
    predictions = manifest["predictions"]
    if not predictions:
        raise ValueError("Experiment has no SWE-bench code patches to export")
    if manifest["missing_instance_ids"]:
        raise ValueError(
            "SWE-bench predictions are incomplete; missing: " + ", ".join(manifest["missing_instance_ids"][:20])
        )
    return (
        "\n".join(
            json.dumps(
                {key: row[key] for key in ("instance_id", "model_patch", "model_name_or_path")},
                ensure_ascii=False,
            )
            for row in predictions
        )
        + "\n"
    )


def swebench_run_manifest(
    dataset: EvalDataset,
    run_envelopes: dict[str, list[dict[str, Any]]],
    *,
    model_name_or_path: str,
    experiment_id: str,
    dataset_version_id: str,
    dataset_content_hash: str,
) -> dict[str, Any]:
    content = prediction_jsonl(dataset, run_envelopes, model_name_or_path=model_name_or_path)
    prediction_manifest = swebench_prediction_manifest(
        dataset,
        run_envelopes,
        model_name_or_path=model_name_or_path,
    )
    patch_hashes = {
        row["instance_id"]: hashlib.sha256(row["model_patch"].encode("utf-8")).hexdigest()
        for row in prediction_manifest["predictions"]
    }
    return {
        "schema_version": "1",
        "experiment_id": experiment_id,
        "dataset_version_id": dataset_version_id,
        "dataset_content_hash": dataset_content_hash,
        "source_snapshot_sha256": dataset.metadata.get("source_snapshot_sha256"),
        "predictions_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "patch_sha256": patch_hashes,
    }


def frozen_swebench_dataset_json(dataset: EvalDataset) -> str:
    rows: list[dict[str, Any]] = []
    for case in dataset.cases:
        reference = case.code.repository.swebench if case.code is not None else None
        if reference is None:
            continue
        rows.append(
            {
                "instance_id": reference.instance_id,
                "repo": reference.repo,
                "base_commit": reference.base_commit,
                "problem_statement": case.input.message or "",
                "hints_text": str(case.metadata.get("hints_text") or ""),
                "version": reference.version or "",
                "environment_setup_commit": reference.environment_setup_commit or "",
                "test_patch": reference.test_patch,
                "patch": "",
                "FAIL_TO_PASS": json.dumps(reference.fail_to_pass),
                "PASS_TO_PASS": json.dumps(reference.pass_to_pass),
            }
        )
    if not rows:
        raise ValueError("Dataset has no SWE-bench cases")
    return json.dumps(rows, ensure_ascii=False, indent=2) + "\n"


def parse_official_swebench_results(content: str) -> dict[str, dict[str, Any]]:
    if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("SWE-bench result import exceeds 20 MiB")
    stripped = content.strip()
    if not stripped:
        raise ValueError("SWE-bench result import is empty")
    payload = json.loads(stripped)
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and payload.get("instance_id"):
        records = [payload]
    elif isinstance(payload, dict) and payload and all(isinstance(value, dict) for value in payload.values()):
        records = [dict(value, instance_id=key) for key, value in payload.items()]
    elif isinstance(payload, dict):
        records = []
        for key, resolved in (("resolved_ids", True), ("unresolved_ids", False)):
            values = payload.get(key) or []
            if not isinstance(values, list):
                raise ValueError(f"Official SWE-bench report {key} must be a list")
            records.extend({"instance_id": item, "resolved": resolved} for item in values)
    else:
        raise ValueError("Unsupported official SWE-bench report shape")
    results: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not str(record.get("instance_id") or ""):
            raise ValueError("Every official SWE-bench result requires instance_id")
        instance_id = str(record["instance_id"])
        resolved = record.get("resolved")
        if not isinstance(resolved, bool):
            raise ValueError(f"Official SWE-bench result {instance_id} requires boolean resolved")
        results[instance_id] = {**record, "instance_id": instance_id, "resolved": resolved}
    if not results:
        raise ValueError("Official SWE-bench report contains no per-instance results")
    return results
