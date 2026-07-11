"""Agent tools for controlled semantic-dimension build jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from db import get_sessionmaker
from knowledge.semantic_dimension_jobs import (
    create_semantic_dimension_build_job as _create_semantic_dimension_build_job,
    get_semantic_dimension_build_job,
    list_semantic_dimension_build_events,
    semantic_dimension_event_to_dict,
    semantic_dimension_job_to_dict,
)
from knowledge.semantic_dimension_publisher import publish_semantic_dimension_build
from analytics.semantic_assets import SemanticAssetError, get_semantic_asset_registry


class EnqueueSemanticDimensionBuildInput(BaseModel):
    dimension_id: str = Field(description="Dimension id to rebuild, for example vehicle_series.")
    adapter: str = Field(default="entity_crosswalk_v1", description="Approved builder adapter name.")
    requested_scope: dict[str, Any] = Field(default_factory=dict, description="Requested build scope, such as all brands.")
    input_snapshot: dict[str, Any] = Field(default_factory=dict, description="Confirmed build_rule or adapter-specific input snapshot.")
    session_id: str = Field(default="", description="Current chat session id for returning to the original conversation.")
    query_id: str = Field(default="", description="Optional originating tool/query id.")
    dimension_name: str = Field(default="", description="Display name used only when a new dimension package must be created.")
    dimension_description: str = Field(default="", description="Business description used only when a new dimension package must be created.")


def _ensure_dimension_package(*, base_dir: Path, dimension_id: str, input_snapshot: dict[str, Any], name: str, description: str) -> None:
    """Create the portable package before queuing a new dimension, never during publish."""

    dimension_md = base_dir / "semantic-assets" / "dimensions" / dimension_id / "dimension.md"
    if dimension_md.is_file():
        return
    build_rule = input_snapshot.get("build_rule") if isinstance(input_snapshot.get("build_rule"), dict) else {}
    bindings = build_rule.get("bindings") if isinstance(build_rule.get("bindings"), list) else []
    canonical = next((item for item in bindings if isinstance(item, dict) and item.get("role") == "canonical"), {})
    try:
        get_semantic_asset_registry(base_dir).create_asset(
            name=name.strip() or dimension_id,
            asset_type="dimension",
            description=description.strip() or f"由语义维度构建技能创建的 {dimension_id} 规范实体维度。",
            slug=dimension_id,
            dimension_definition={
                "mode": "entity_lookup",
                "canonical": {"key": "entity_key", "fields": list(canonical.get("output_fields") or [])},
                "reference_path": "references/active_crosswalk.json",
            },
        )
    except SemanticAssetError as exc:
        raise RuntimeError(f"无法创建语义维度包：{exc}") from exc


class GetSemanticDimensionBuildJobInput(BaseModel):
    job_id: str = Field(description="Semantic dimension build job id returned by enqueue_semantic_dimension_build.")
    include_events: bool = Field(default=True, description="Include a concise build event timeline.")


class PublishSemanticDimensionBuildInput(BaseModel):
    job_id: str = Field(description="Validated semantic-dimension build job id the user explicitly approved for publication.")


class EnqueueSemanticDimensionBuildTool(BaseTool):
    name: str = "enqueue_semantic_dimension_build"
    description: str = (
        "Queue an approved, potentially long-running semantic-dimension build. "
        "Use for an explicitly requested refresh/build of a reusable cross-source dimension. "
        "For a new dimension it creates the portable semantic package before queueing. It stages and validates results but never publishes them; after enqueueing, tell the user the job id and end the current analysis turn."
    )
    args_schema: Type[BaseModel] = EnqueueSemanticDimensionBuildInput
    risk_level: str = "moderate"
    session_id: str = ""
    query_id: str = ""

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for semantic dimension builds")

    async def _arun(self, **kwargs: Any) -> str:
        tool_call_id = str(kwargs.pop("tool_call_id", "") or "")
        kwargs["session_id"] = str(kwargs.get("session_id") or self.session_id or "")
        kwargs["query_id"] = str(kwargs.get("query_id") or self.query_id or tool_call_id or "")
        if not kwargs["session_id"]:
            return "❌ 无法创建语义维度构建任务：缺少当前会话标识。"
        base_dir = Path(__file__).resolve().parents[1]
        _ensure_dimension_package(
            base_dir=base_dir,
            dimension_id=str(kwargs.get("dimension_id") or ""),
            input_snapshot=dict(kwargs.get("input_snapshot") or {}),
            name=str(kwargs.pop("dimension_name", "") or ""),
            description=str(kwargs.pop("dimension_description", "") or ""),
        )
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            job, queued = await _create_semantic_dimension_build_job(session, **kwargs)
        payload = semantic_dimension_job_to_dict(job)
        payload["queued"] = queued
        payload["next_action"] = "等待后台任务完成；收到通知后，在原对话明确要求发布该 job。"
        return "🧩 语义维度构建任务已受理\n" + json.dumps(payload, ensure_ascii=False, indent=2)


class GetSemanticDimensionBuildJobTool(BaseTool):
    name: str = "get_semantic_dimension_build_job"
    description: str = (
        "Read one queued semantic-dimension build job and its validation summary. "
        "Use when the user asks for build progress, completion, errors, or asks to publish a completed job."
    )
    args_schema: Type[BaseModel] = GetSemanticDimensionBuildJobInput
    risk_level: str = "safe"

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for semantic dimension jobs")

    async def _arun(self, job_id: str, include_events: bool = True) -> str:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            job = await get_semantic_dimension_build_job(session, job_id)
            if job is None:
                return f"🧩 未找到语义维度构建任务：{job_id}"
            events = await list_semantic_dimension_build_events(session, job_id, limit=50) if include_events else []
        payload = semantic_dimension_job_to_dict(job)
        if include_events:
            payload["events"] = [semantic_dimension_event_to_dict(item) for item in events]
        return "🧩 语义维度构建任务\n" + json.dumps(payload, ensure_ascii=False, indent=2)


class PublishSemanticDimensionBuildTool(BaseTool):
    name: str = "publish_semantic_dimension_build"
    description: str = (
        "Publish one validated semantic-dimension build only after the user explicitly confirms. "
        "It atomically copies the staged Crosswalk into the active semantic asset, updates dimension.md, "
        "then marks the Job published with an event and notification. Do not use write_file for publication."
    )
    args_schema: Type[BaseModel] = PublishSemanticDimensionBuildInput
    risk_level: str = "moderate"

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        raise RuntimeError("Use async execution for semantic dimension publication")

    async def _arun(self, job_id: str) -> str:
        sessionmaker = get_sessionmaker()
        base_dir = Path(__file__).resolve().parents[1]
        async with sessionmaker() as session:
            result = await publish_semantic_dimension_build(session, base_dir=base_dir, job_id=job_id)
        payload = semantic_dimension_job_to_dict(result["job"])
        payload["already_published"] = result["already_published"]
        payload["active_crosswalk"] = result.get("active_crosswalk")
        payload["published_summary"] = result.get("published_summary") or {}
        return "🧩 语义维度已发布\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def create_semantic_dimension_build_tools() -> list[BaseTool]:
    return [EnqueueSemanticDimensionBuildTool(), GetSemanticDimensionBuildJobTool(), PublishSemanticDimensionBuildTool()]
