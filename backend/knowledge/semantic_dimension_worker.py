"""Background worker for staged semantic-dimension builds."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import get_sessionmaker
from knowledge.models import SemanticDimensionBuildJob
from knowledge.queue_repository import (
    LeaseLostError,
    bind_lease_owner,
    heartbeat_loop,
    new_worker_id,
    reset_lease_owner,
)
from knowledge.semantic_dimension_jobs import (
    claim_next_semantic_dimension_build_job,
    mark_semantic_dimension_build_failed,
    mark_semantic_dimension_build_waiting_baseline_change,
    mark_semantic_dimension_build_waiting_publish,
    update_semantic_dimension_build_progress,
)
from knowledge.semantic_dimension_rule_contract import validate_build_rule
from knowledge.semantic_dimension_crosswalk import load_crosswalk_state, materialize_crosswalk

logger = logging.getLogger(__name__)


def _published_preview_summary(crosswalk: dict[str, Any]) -> dict[str, int]:
    """Summarize the Crosswalk exactly as it would look after publication."""

    records = [record for record in crosswalk.get("records") or [] if isinstance(record, dict)]
    diagnostics = [record for record in crosswalk.get("source_diagnostics") or [] if isinstance(record, dict)]
    source_keys: set[tuple[str, str]] = set()
    source_matched = 0
    for record in records:
        bindings = [
            binding for binding in record.get("bindings") or []
            if isinstance(binding, dict) and binding.get("source_kind") not in {"database_table", "canonical_reference"}
        ]
        source_matched += len(bindings)
        for binding in bindings:
            source_keys.add((str(binding.get("source_ref") or ""), json.dumps(binding.get("key_fields") or {}, ensure_ascii=False, sort_keys=True)))
    diagnostic_statuses: dict[str, int] = {}
    for record in diagnostics:
        status = str((record.get("resolution") or {}).get("status") or "unknown")
        diagnostic_statuses[status] = diagnostic_statuses.get(status, 0) + 1
        for binding in record.get("bindings") or []:
            if isinstance(binding, dict):
                source_keys.add((str(binding.get("source_ref") or ""), json.dumps(binding.get("key_fields") or {}, ensure_ascii=False, sort_keys=True)))
    return {
        "canonical_entities": len(records),
        "canonical_with_source_binding": sum(1 for record in records if len(record.get("bindings") or []) > 1),
        "canonical_only": sum(1 for record in records if len(record.get("bindings") or []) <= 1),
        "source_distinct_keys": len(source_keys),
        "source_matched": source_matched,
        "source_diagnostics": len(diagnostics),
        "source_unmatched": diagnostic_statuses.get("unmatched", 0),
        "source_candidates": diagnostic_statuses.get("candidate", 0),
    }


class SemanticDimensionBuildWorkerManager:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._base_dir: Path | None = None
        self._worker_id: str | None = None

    def start(self, base_dir: Path) -> None:
        if os.getenv("PUDDINGCLAW_DISABLE_SEMANTIC_DIMENSION_WORKER", "").strip().lower() in {"1", "true", "yes", "on"}:
            logger.info("[semantic-dimension-worker] disabled by environment")
            return
        if self._task is not None and not self._task.done():
            return
        self._base_dir = base_dir
        self._worker_id = new_worker_id("semantic-dimension-build")
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="semantic-dimension-build-worker")
        logger.info("[semantic-dimension-worker] started")

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("[semantic-dimension-worker] stopped")

    async def _run_loop(self) -> None:
        assert self._base_dir is not None
        sessionmaker = get_sessionmaker()
        idle_sleep = float(os.getenv("PUDDINGCLAW_SEMANTIC_DIMENSION_WORKER_POLL_SECONDS", "2") or "2")
        while True:
            try:
                did_work = await self._run_once(sessionmaker, self._base_dir)
                if not did_work:
                    await asyncio.sleep(idle_sleep)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[semantic-dimension-worker] loop error")
                await asyncio.sleep(idle_sleep)

    async def _run_once(self, sessionmaker: async_sessionmaker[AsyncSession], base_dir: Path) -> bool:
        async with sessionmaker() as session:
            job = await claim_next_semantic_dimension_build_job(session, worker_id=self._worker_id)
            if job is None:
                return False
            job_id = job.id
            worker_id = job.lease_owner or self._worker_id or ""

        stop_hb = asyncio.Event()
        lost = asyncio.Event()
        hb_task = asyncio.create_task(
            heartbeat_loop(
                sessionmaker,
                SemanticDimensionBuildJob,
                job_id,
                worker_id,
                stop_event=stop_hb,
                lost_event=lost,
            ),
            name=f"semantic-dimension-build-heartbeat-{job_id}",
        )
        token = bind_lease_owner(worker_id)
        completed = False
        try:
            try:
                await self._process_job(sessionmaker, base_dir, job_id)
                completed = True
            except LeaseLostError:
                logger.warning("[semantic-dimension-worker] 租约已丢失，跳过终态写入 job_id=%s", job_id)
            except Exception as exc:
                logger.exception("[semantic-dimension-worker] failed job_id=%s", job_id)
                if not lost.is_set():
                    async with sessionmaker() as session:
                        job = await session.get(SemanticDimensionBuildJob, job_id)
                        if job is not None and job.status == "running":
                            try:
                                await mark_semantic_dimension_build_failed(session, job, exc)
                                completed = True
                            except LeaseLostError:
                                logger.warning("[semantic-dimension-worker] 租约已丢失，跳过失败状态写入 job_id=%s", job_id)
        finally:
            reset_lease_owner(token)
            stop_hb.set()
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
        if lost.is_set() and not completed:
            logger.warning("[semantic-dimension-worker] 租约已丢失且未写入终态，任务将由回收方重新执行 job_id=%s", job_id)
        return True

    async def _process_job(self, sessionmaker: async_sessionmaker[AsyncSession], base_dir: Path, job_id: str) -> None:
        async with sessionmaker() as session:
            job = await session.get(SemanticDimensionBuildJob, job_id)
            if job is None or job.status != "running":
                return
            if job.adapter not in {"vehicle_series_full", "entity_crosswalk_v1"}:
                raise ValueError(f"Unsupported semantic dimension adapter: {job.adapter}")
            await update_semantic_dimension_build_progress(
                session,
                job,
                step="resolve_entities",
                progress=20,
                message=(
                    "正在加载用户确认的维度输入并执行实体归一匹配。"
                    if job.adapter == "entity_crosswalk_v1"
                    else "正在加载来源键并执行车系归一匹配。"
                ),
            )
            snapshot = dict(job.input_snapshot or {})

        runtime_root = base_dir
        definitions_root = runtime_root / "definitions"
        package_root = Path(__file__).resolve().parents[1]
        task_dir = runtime_root / "data" / "semantic-dimension-build-jobs" / job_id
        artifact_dir = task_dir / "artifacts"
        build_rule = snapshot.get("build_rule") if isinstance(snapshot.get("build_rule"), dict) else None
        if job.adapter == "entity_crosswalk_v1":
            if build_rule is None:
                raise ValueError("Generic entity Crosswalk build requires a confirmed build_rule")
            build_rule = validate_build_rule(build_rule)
            if build_rule["dimension_id"] != job.dimension_id:
                raise ValueError("Confirmed build_rule does not match this semantic dimension")
            relative_reference = str((build_rule.get("artifact") or {}).get("reference_path") or "")
            if not relative_reference.startswith("references/"):
                raise ValueError("Generic build reference_path must stay in references/")
        else:
            relative_reference = "references/active_crosswalk.json"
        reference_path = task_dir / relative_reference
        log_dir = runtime_root / "logs" / "semantic-dimension-build-jobs"
        task_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"
        if job.adapter == "entity_crosswalk_v1":
            script_path = package_root / "skills" / "build-semantic-dimension" / "scripts" / "entity_crosswalk_v1.py"
            rule_path = task_dir / "build-rule.json"
            rule_path.write_text(json.dumps(build_rule, ensure_ascii=False, indent=2), encoding="utf-8")
            command = [
                sys.executable,
                str(script_path),
                "--dimension-id",
                job.dimension_id,
                "--session-id",
                job.session_id,
                "--rule-json",
                str(rule_path),
                "--output-dir",
                str(artifact_dir),
                "--semantic-reference-path",
                str(reference_path),
                "--prior-reference-path",
                str(definitions_root / "semantic-assets" / "dimensions" / job.dimension_id / relative_reference),
            ]
        else:
            script_path = package_root / "skills" / "build-semantic-dimension" / "scripts" / "vehicle_series_full.py"
            command = [
                sys.executable,
                str(script_path),
                "--output-dir",
                str(artifact_dir),
                "--semantic-reference-path",
                str(reference_path),
                "--prior-reference-path",
                str(definitions_root / "semantic-assets" / "dimensions" / "vehicle_series" / "references" / "byd_chery_demo.json"),
            ]
            sales_file_name = str(snapshot.get("sales_file_name") or "").strip()
            source_id = str(snapshot.get("source_id") or "").strip()
            if sales_file_name:
                command.extend(["--sales-file-name", sales_file_name])
            if source_id:
                command.extend(["--source-id", source_id])

        logger.info("[semantic-dimension-worker] running adapter=%s job_id=%s", job.adapter, job_id)
        with log_path.open("ab") as log_file:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(package_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=log_file,
            )
            stdout, _ = await process.communicate()
            if stdout:
                log_file.write(stdout)
        if process.returncode != 0:
            raise RuntimeError(f"车系构建子进程失败（exit={process.returncode}），详见日志：{log_path}")

        try:
            output: dict[str, Any] = json.loads(stdout.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"构建已结束但无法读取结构化摘要，详见日志：{log_path}") from exc
        if not reference_path.is_file():
            raise RuntimeError("构建未产出 staging Crosswalk，拒绝进入发布确认阶段。")

        # The adapter deliberately writes its raw matching result so unmatched
        # inputs remain auditable. Jobs, however, should report what the user
        # will actually get after the already-approved manual rules are applied.
        staged_crosswalk = json.loads(reference_path.read_text(encoding="utf-8"))
        crosswalk_state = load_crosswalk_state(definitions_root, job.dimension_id)
        published_preview = materialize_crosswalk(staged_crosswalk, crosswalk_state["overrides"])
        preview_path = artifact_dir / "published-preview-crosswalk.json"
        preview_path.write_text(json.dumps(published_preview, ensure_ascii=False, indent=2), encoding="utf-8")
        raw_summary = output.get("summary") or {}
        preview_summary = _published_preview_summary(published_preview)
        manually_resolved = max(
            0,
            int(raw_summary.get("source_diagnostics") or 0) - int(preview_summary.get("source_diagnostics") or 0),
        )

        summary = {
            "summary": preview_summary,
            "raw_summary": raw_summary,
            "summary_basis": "published_preview",
            "manual_rule_projection": {
                "resolved_source_keys": manually_resolved,
                "message": "主摘要已叠加当前人工规则；原始构建结果保留在 raw_summary 与 staging Crosswalk。",
            },
            "delta": output.get("delta") or {},
            "baseline_delta": output.get("baseline_delta") or {},
            "artifact_paths": {
                "resolution_json": str(output.get("json") or ""),
                "crosswalk_csv": str(output.get("csv") or ""),
                "source_diagnostics_csv": str(output.get("diagnostic_csv") or ""),
                "crosswalk": str(reference_path),
                "published_preview_crosswalk": str(preview_path),
                "log": str(log_path),
            },
            "publish_instructions": "请回到原对话，明确要求发布此 Job；Agent 会先核对 staging 摘要，再更新活跃 dimension.md。",
        }
        async with sessionmaker() as session:
            job = await session.get(SemanticDimensionBuildJob, job_id)
            if job is None or job.status != "running":
                return
            if (summary.get("baseline_delta") or {}).get("removed"):
                await mark_semantic_dimension_build_waiting_baseline_change(
                    session, job, staging_path=str(task_dir), published_reference_path=relative_reference, result_summary=summary,
                )
            else:
                await mark_semantic_dimension_build_waiting_publish(
                    session, job, staging_path=str(task_dir), published_reference_path=relative_reference, result_summary=summary,
                )


semantic_dimension_build_worker_manager = SemanticDimensionBuildWorkerManager()
