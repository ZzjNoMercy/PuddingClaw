"""Controlled publication of staged semantic-dimension build artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from knowledge.semantic_dimension_jobs import get_semantic_dimension_build_job, mark_semantic_dimension_build_published
from knowledge.semantic_dimension_crosswalk import ACTIVE_FILE, list_registered_sources, publish_generated_crosswalk


DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
_DIMENSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _active_crosswalk_summary(crosswalk: dict[str, Any], source_contracts: int) -> dict[str, int | str]:
    """Return the runtime facts an Agent should use in a publish receipt."""

    records = [record for record in crosswalk.get("records") or [] if isinstance(record, dict)]
    diagnostics = [record for record in crosswalk.get("source_diagnostics") or [] if isinstance(record, dict)]
    source_bindings = 0
    diagnostic_statuses: dict[str, int] = {}
    for record in records:
        source_bindings += sum(
            1 for binding in record.get("bindings") or []
            if isinstance(binding, dict) and binding.get("source_kind") not in {"database_table", "canonical_reference"}
        )
    for record in diagnostics:
        status = str((record.get("resolution") or {}).get("status") or "unknown")
        diagnostic_statuses[status] = diagnostic_statuses.get(status, 0) + 1
    return {
        "basis": "active_crosswalk",
        "canonical_entities": len(records),
        "source_contracts": source_contracts,
        "active_source_bindings": source_bindings,
        "pending_source_diagnostics": len(diagnostics),
        "pending_unmatched": diagnostic_statuses.get("unmatched", 0),
        "pending_candidates": diagnostic_statuses.get("candidate", 0),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_handle, tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    ) as output_handle:
        while chunk := input_handle.read(1024 * 1024):
            output_handle.write(chunk)
        temporary_path = Path(output_handle.name)
    os.replace(temporary_path, target)


def _atomic_write(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    ) as handle:
        handle.write(content)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def _update_dimension_frontmatter(path: Path, *, dimension_id: str, reference_path: str, adapter: str) -> str:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---") or f"id: {dimension_id}" not in content:
        raise ValueError("目标 dimension.md 无法确认对应当前 Job")
    beijing_now = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    replacements = (
        (r"(?m)^  reference_path:.*$", f"  reference_path: {reference_path}"),
        (r"(?m)^  adapter:.*$", f"  adapter: {adapter}"),
        (r"(?m)^updated_at:.*$", f"updated_at: {beijing_now}"),
    )
    for pattern, replacement in replacements:
        updated, count = re.subn(pattern, replacement, content, count=1)
        if count != 1:
            raise ValueError(f"目标 dimension.md 缺少发布字段：{replacement.split(':', 1)[0]}")
        content = updated
    return content


def _validate_registered_source_modes(base_dir: Path, dimension_id: str, payload: dict[str, Any]) -> None:
    """Do not let a stale staged Job replace an existing logical source."""

    registered_ids = {str(item.get("id") or "") for item in list_registered_sources(base_dir, dimension_id)}
    for binding in ((payload.get("build_rule") or {}).get("bindings") or []):
        if not isinstance(binding, dict) or binding.get("role") != "source":
            continue
        source_id = str(binding.get("source_id") or "")
        if source_id in registered_ids and str(binding.get("source_mode") or "") != "append":
            raise ValueError(f"Staged Job marks registered source '{source_id}' as new; rebuild it in append mode before publishing")


async def publish_semantic_dimension_build(
    session: AsyncSession,
    *,
    base_dir: Path,
    job_id: str,
) -> dict[str, Any]:
    """Atomically activate a staged Crosswalk and mark the corresponding Job published."""
    job = await get_semantic_dimension_build_job(session, job_id)
    if job is None:
        raise ValueError("Semantic dimension build job not found")
    if job.status == "published":
        active_path = base_dir / "semantic-assets" / "dimensions" / job.dimension_id / "references" / ACTIVE_FILE
        active = json.loads(active_path.read_text(encoding="utf-8")) if active_path.is_file() else {}
        return {
            "job": job,
            "already_published": True,
            "active_crosswalk": str(active_path) if active_path.is_file() else None,
            "published_summary": _active_crosswalk_summary(
                active,
                len(list_registered_sources(base_dir, job.dimension_id)),
            ),
        }
    if job.status != "waiting_for_publish_confirmation":
        raise ValueError("Only a validated job waiting for publish confirmation can be published")
    if not _DIMENSION_ID_RE.fullmatch(job.dimension_id):
        raise ValueError("Invalid semantic dimension id")

    artifact_paths = (job.result_summary or {}).get("artifact_paths") or {}
    staged_crosswalk = Path(str(artifact_paths.get("crosswalk") or "")).resolve()
    if not staged_crosswalk.is_file():
        raise ValueError("Staging Crosswalk artifact is missing")
    try:
        payload = json.loads(staged_crosswalk.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Staging Crosswalk is not valid JSON") from exc
    if payload.get("formatter") != "entity-resolution-crosswalk" or payload.get("entity_type") != job.dimension_id:
        raise ValueError("Staging Crosswalk does not match the semantic dimension")
    _validate_registered_source_modes(base_dir, job.dimension_id, payload)

    dimension_dir = (base_dir / "semantic-assets" / "dimensions" / job.dimension_id).resolve()
    dimension_md = dimension_dir / "dimension.md"
    relative_reference = Path("references") / ACTIVE_FILE
    active_crosswalk = (dimension_dir / relative_reference).resolve()
    if not dimension_md.is_file() or dimension_dir not in active_crosswalk.parents:
        raise ValueError("Active semantic dimension reference is outside its dimension directory")

    updated_dimension = _update_dimension_frontmatter(
        dimension_md,
        dimension_id=job.dimension_id,
        reference_path=str(relative_reference),
        adapter=job.adapter,
    )
    publication = publish_generated_crosswalk(base_dir, job.dimension_id, payload)
    if not active_crosswalk.is_file():
        raise RuntimeError("Active Crosswalk was not materialized after publication")
    _atomic_write(dimension_md, updated_dimension)

    await mark_semantic_dimension_build_published(
        session,
        job,
        active_reference_path=str(active_crosswalk),
    )
    return {
        "job": job,
        "already_published": False,
        "active_crosswalk": str(active_crosswalk),
        "version": publication["version"],
        "published_summary": _active_crosswalk_summary(
            publication["active"],
            len(publication.get("registry", {}).get("sources") or []),
        ),
    }
