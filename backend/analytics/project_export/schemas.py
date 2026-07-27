"""Contracts for compiling one analytics model into a portable project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DataFileMode = Literal["copy", "reference"]


class ExportDataAssetPlan(BaseModel):
    ref: str
    kind: Literal["database_table", "table_asset", "logical_dataset"]
    status: Literal["ready", "missing"] = "ready"
    asset_id: str = ""
    file_name: str = ""
    source_path: str = ""
    virtual_path: str = ""
    sheet_name: str | None = None
    size_bytes: int = 0
    profile_available: bool = False
    source_asset_ids: list[str] = Field(default_factory=list)


class AnalysisProjectExportPlan(BaseModel):
    format: str = "analysis-project-export-plan/v1"
    model_id: str
    model_name: str
    model_version: str
    package_name: str
    plan_id: str
    data_file_mode: DataFileMode
    semantic_asset_ids: list[str] = Field(default_factory=list)
    relation_ids: list[str] = Field(default_factory=list)
    guardrail_ids: list[str] = Field(default_factory=list)
    data_assets: list[ExportDataAssetPlan] = Field(default_factory=list)
    copied_file_count: int = 0
    copied_bytes: int = 0
    warnings: list[str] = Field(default_factory=list)
    missing_dependencies: list[str] = Field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.missing_dependencies


@dataclass(frozen=True)
class AnalysisProjectExportArtifact:
    path: Path
    filename: str
    plan: AnalysisProjectExportPlan


class AnalysisProjectExportError(ValueError):
    """Raised when an analytics model cannot be exported faithfully."""
