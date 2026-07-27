"""Compile a native analytics model into a portable, filesystem-first project."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.models import AnalyticsModelError, get_analytics_model_registry
from analytics.nl2sql import guardrail_runtime
from analytics.nl2sql.guardrails import list_guardrail_rules
from analytics.semantic_assets import SemanticAssetError, get_semantic_asset_registry
from knowledge.models import KnowledgeTableAsset

from .portable_guardrails import PORTABLE_SQL_VALIDATOR
from .portable_materializer import PORTABLE_LOGICAL_MATERIALIZER
from .schemas import (
    AnalysisProjectExportArtifact,
    AnalysisProjectExportError,
    AnalysisProjectExportPlan,
    DataFileMode,
    ExportDataAssetPlan,
)

EXPORT_FORMAT = "analysis-project/v1"
MUTABLE_PROJECT_FILES = ("bindings.local.yaml",)
PORTABLE_GUARDRAIL_TYPES = {
    "forbid_sql_pattern",
    "require_sql_contains",
    "require_table_when_available",
    "require_group_by",
    "forbid_exists_distinct_pattern",
}


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", value.strip()).strip("-_").lower()
    return slug or "analysis-project"


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")


def _yaml_bytes(value: Any) -> bytes:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode("utf-8")


def _public_guardrail(rule: dict[str, Any]) -> dict[str, Any]:
    return {key: rule.get(key) for key in ("id", "name", "enabled", "type", "scope", "params", "action")}


def _guardrail_snapshot(rule: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    payload = _public_guardrail(rule)
    document_path = str(rule.get("document_path") or "")
    source = base_dir / document_path if document_path else None
    payload["document_sha256"] = _sha256_path(source) if source and source.is_file() else ""
    return payload


def _model_semantic_ids(model: dict[str, Any]) -> list[str]:
    semantic = (model.get("frontmatter") or {}).get("semantic_assets") or {}
    if not isinstance(semantic, dict):
        return []
    return list(
        dict.fromkeys(
            str(item).strip()
            for group in ("measures", "dimensions", "grains")
            for item in semantic.get(group) or []
            if str(item).strip()
        )
    )


def _source_filename(asset: KnowledgeTableAsset) -> str:
    path = Path(asset.storage_path)
    return path.name or asset.file_name or f"{asset.asset_id}.data"


def _database_binding_id(ref: str) -> str:
    digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:8]
    return f"{_safe_slug(ref)}-{digest}"


class AnalysisProjectExporter:
    """Resolve native IDs and emit a self-describing analysis project ZIP."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self.models = get_analytics_model_registry(self.base_dir)
        self.semantic_assets = get_semantic_asset_registry(self.base_dir)

    async def build_plan(
        self,
        session: AsyncSession,
        *,
        model_id: str,
        data_file_mode: DataFileMode,
    ) -> AnalysisProjectExportPlan:
        if data_file_mode not in {"copy", "reference"}:
            raise AnalysisProjectExportError("data_file_mode must be copy or reference")
        try:
            model = self.models.get_model(model_id)
        except AnalyticsModelError as exc:
            raise AnalysisProjectExportError(str(exc)) from exc

        package_name = _safe_slug(str(model.get("id") or model.get("name") or "analysis-project"))
        semantic_ids = _model_semantic_ids(model)
        relation_ids = [str(item).strip() for item in model.get("asset_relations") or [] if str(item).strip()]
        guardrail_ids = [str(item).strip() for item in model.get("guardrails") or [] if str(item).strip()]
        missing: list[str] = []
        warnings: list[str] = []

        for asset_id in [*semantic_ids, *relation_ids]:
            try:
                self.semantic_assets.get_asset(asset_id)
            except (SemanticAssetError, ValueError):
                missing.append(f"semantic_asset:{asset_id}")
        self._validate_dependency_closure(
            model=model,
            semantic_ids=semantic_ids,
            relation_ids=relation_ids,
            missing=missing,
            warnings=warnings,
        )

        guardrails = {str(item.get("id") or ""): item for item in list_guardrail_rules().get("guardrails") or []}
        for guardrail_id in guardrail_ids:
            rule = guardrails.get(guardrail_id)
            if not rule:
                missing.append(f"guardrail:{guardrail_id}")
            elif str(rule.get("type") or "") not in PORTABLE_GUARDRAIL_TYPES:
                action = rule.get("action") if isinstance(rule.get("action"), dict) else {}
                if str(action.get("type") or "block") == "warn":
                    warnings.append(f"Advisory Guardrail {guardrail_id} 将随包导出，但本地校验器会标为 not_evaluated。")
                else:
                    missing.append(f"guardrail_runtime:{guardrail_id}:{rule.get('type') or 'unknown'}")

        resolved_assets: dict[str, KnowledgeTableAsset] = {}

        async def resolve_table_asset(asset_id: str, ancestry: tuple[str, ...] = ()) -> KnowledgeTableAsset | None:
            if asset_id in ancestry:
                missing.append(f"table_asset_cycle:{' -> '.join((*ancestry, asset_id))}")
                return None
            if asset_id in resolved_assets:
                return resolved_assets[asset_id]
            asset = await session.get(KnowledgeTableAsset, asset_id)
            if asset is None or asset.reference_status == "removed":
                missing.append(f"table_asset:{asset_id}")
                return None
            resolved_assets[asset_id] = asset
            logical = (
                (asset.asset_metadata or {}).get("logical_dataset") if isinstance(asset.asset_metadata, dict) else None
            )
            source_ids = logical.get("source_asset_ids") if isinstance(logical, dict) else []
            for source_id in source_ids if isinstance(source_ids, list) else []:
                await resolve_table_asset(str(source_id), (*ancestry, asset_id))
            return asset

        data_plans: list[ExportDataAssetPlan] = []
        table_refs = ((model.get("frontmatter") or {}).get("data_assets") or {}).get("tables") or []
        for raw_ref in table_refs:
            ref = str(raw_ref).strip()
            if not ref:
                continue
            if not ref.startswith("table_asset:"):
                data_plans.append(ExportDataAssetPlan(ref=ref, kind="database_table"))
                continue
            asset_id = ref.removeprefix("table_asset:").strip()
            asset = await resolve_table_asset(asset_id)
            if asset is None:
                data_plans.append(ExportDataAssetPlan(ref=ref, kind="table_asset", status="missing", asset_id=asset_id))
                continue
            logical = (
                (asset.asset_metadata or {}).get("logical_dataset") if isinstance(asset.asset_metadata, dict) else None
            )
            source_ids = [str(item) for item in (logical or {}).get("source_asset_ids") or []]
            source_path = Path(asset.storage_path)
            profile_available = bool(asset.profile_path and Path(asset.profile_path).is_file())
            if not profile_available:
                warnings.append(f"数据资产 {asset_id} 没有独立 Profile，将导出目录字段摘要。")
            data_plans.append(
                ExportDataAssetPlan(
                    ref=ref,
                    kind="logical_dataset" if isinstance(logical, dict) else "table_asset",
                    asset_id=asset_id,
                    file_name=asset.file_name,
                    source_path=(
                        str(source_path.resolve())
                        if data_file_mode == "reference" and source_path.exists()
                        else str(source_path)
                        if data_file_mode == "reference"
                        else ""
                    ),
                    virtual_path=asset.virtual_path,
                    sheet_name=asset.sheet_name,
                    size_bytes=asset.size_bytes,
                    profile_available=profile_available,
                    source_asset_ids=source_ids,
                )
            )

        for asset_id, asset in sorted(resolved_assets.items()):
            source_path = Path(asset.storage_path)
            if not source_path.is_file():
                missing.append(f"data_file:{asset_id}")
            if not asset.profile_path or not Path(asset.profile_path).is_file():
                warnings.append(f"数据资产 {asset_id} 没有独立 Profile，将导出目录字段摘要。")

        unique_paths = {
            Path(asset.storage_path).resolve()
            for asset in resolved_assets.values()
            if Path(asset.storage_path).is_file()
        }
        copied_file_count = len(unique_paths) if data_file_mode == "copy" else 0
        copied_bytes = sum(path.stat().st_size for path in unique_paths) if data_file_mode == "copy" else 0
        plan_id = self._snapshot_id(
            model=model,
            semantic_ids=semantic_ids,
            relation_ids=relation_ids,
            guardrails=[_guardrail_snapshot(guardrails[item], self.base_dir) for item in guardrail_ids if item in guardrails],
            table_assets=resolved_assets,
            data_file_mode=data_file_mode,
        )
        return AnalysisProjectExportPlan(
            model_id=str(model.get("id") or model_id),
            model_name=str(model.get("name") or model_id),
            model_version=str(model.get("version") or "0.1.0"),
            package_name=package_name,
            plan_id=plan_id,
            data_file_mode=data_file_mode,
            semantic_asset_ids=semantic_ids,
            relation_ids=relation_ids,
            guardrail_ids=guardrail_ids,
            data_assets=data_plans,
            copied_file_count=copied_file_count,
            copied_bytes=copied_bytes,
            warnings=list(dict.fromkeys(warnings)),
            missing_dependencies=list(dict.fromkeys(missing)),
        )

    def _snapshot_id(
        self,
        *,
        model: dict[str, Any],
        semantic_ids: list[str],
        relation_ids: list[str],
        guardrails: list[dict[str, Any]],
        table_assets: dict[str, KnowledgeTableAsset],
        data_file_mode: DataFileMode,
    ) -> str:
        files: list[dict[str, Any]] = []
        for item in model.get("files") or []:
            path = self.base_dir / str(item.get("path") or "")
            if path.is_file():
                files.append({"path": str(item.get("relative_path") or path.name), "sha256": _sha256_path(path)})
        for asset_id in [*semantic_ids, *relation_ids]:
            try:
                asset = self.semantic_assets.get_asset(asset_id)
            except Exception:
                continue
            for item in asset.get("files") or []:
                path = self.base_dir / str(item.get("path") or "")
                if path.is_file():
                    files.append(
                        {
                            "path": f"{asset_id}:{item.get('relative_path') or path.name}",
                            "sha256": _sha256_path(path),
                        }
                    )
        data = []
        for asset_id, asset in sorted(table_assets.items()):
            path = Path(asset.storage_path)
            stat = path.stat() if path.is_file() else None
            data.append(
                {
                    "asset_id": asset_id,
                    "catalog_sha256": asset.content_sha256,
                    "size": stat.st_size if stat else None,
                    "mtime_ns": stat.st_mtime_ns if stat else None,
                    "profile": asset.profile_path,
                    "profile_sha256": (
                        _sha256_path(Path(asset.profile_path))
                        if asset.profile_path and Path(asset.profile_path).is_file()
                        else ""
                    ),
                }
            )
        payload = {
            "format": "analysis-project-export-snapshot/v1",
            "mode": data_file_mode,
            "files": sorted(files, key=lambda item: item["path"]),
            "guardrails": guardrails,
            "data": data,
        }
        return "export-plan-" + _sha256_bytes(_json_bytes(payload))[:24]

    def _validate_dependency_closure(
        self,
        *,
        model: dict[str, Any],
        semantic_ids: list[str],
        relation_ids: list[str],
        missing: list[str],
        warnings: list[str],
    ) -> None:
        """Fail closed when a model points at resources the package cannot include."""

        selected_semantic = set(semantic_ids)
        selected_data = {
            str(item).strip()
            for item in ((model.get("frontmatter") or {}).get("data_assets") or {}).get("tables") or []
            if str(item).strip()
        }
        for asset_id in [*semantic_ids, *relation_ids]:
            try:
                asset = self.semantic_assets.get_asset(asset_id)
            except Exception:
                continue
            frontmatter = asset.get("frontmatter") or {}
            asset_root = (self.base_dir / str(asset.get("path") or "")).parent
            declared_paths: list[str] = []

            def collect_paths(value: Any, key: str = "") -> None:
                if isinstance(value, dict):
                    for child_key, child in value.items():
                        collect_paths(child, str(child_key))
                elif isinstance(value, list):
                    for child in value:
                        collect_paths(child, key)
                elif key in {"reference_path", "generated_resources"} and str(value).strip():
                    declared_paths.append(str(value).strip())

            collect_paths(frontmatter)
            for declared_path in declared_paths:
                source = (
                    self.base_dir / declared_path.lstrip("/")
                    if declared_path.startswith("/semantic-assets/")
                    else asset_root / declared_path
                )
                try:
                    source.resolve(strict=True).relative_to(asset_root.resolve(strict=True))
                except (FileNotFoundError, ValueError):
                    missing.append(f"semantic_resource:{asset_id}:{declared_path}")
            build_skill = frontmatter.get("build_skill")
            if isinstance(build_skill, dict) and str(build_skill.get("name") or "").strip():
                warnings.append(
                    f"语义资产 {asset_id} 的 build_skill 仅作为 provenance 保留；运行分析不依赖平台构建工具。"
                )

            if str(asset.get("type") or "") != "relation":
                continue
            relation = frontmatter.get("relation") if isinstance(frontmatter.get("relation"), dict) else {}
            relation_type = str(frontmatter.get("relation_type") or "")
            if relation_type == "dimension_binding":
                asset_ref = str((relation.get("asset") or {}).get("ref") or "").strip()
                dimension_ref = str((relation.get("dimension") or {}).get("ref") or "").strip()
                if asset_ref not in selected_data:
                    missing.append(f"relation_endpoint:{asset_id}:{asset_ref}")
                if dimension_ref not in selected_semantic:
                    missing.append(f"relation_endpoint:{asset_id}:{dimension_ref}")
            elif relation_type == "direct_join":
                for side in ("left", "right"):
                    endpoint = str((relation.get(side) or {}).get("ref") or "").strip()
                    if endpoint not in selected_data:
                        missing.append(f"relation_endpoint:{asset_id}:{endpoint}")

        model_files = {
            str(item.get("relative_path") or "").strip()
            for item in model.get("files") or []
            if str(item.get("relative_path") or "").strip()
        }
        templates = model.get("templates") if isinstance(model.get("templates"), dict) else {}
        declared_templates: dict[str, str] = {}
        for template_id, definition in templates.items():
            if isinstance(definition, str):
                declared_templates[str(template_id)] = definition
            elif isinstance(definition, dict) and str(definition.get("path") or "").strip():
                declared_templates[str(template_id)] = str(definition["path"])
        for template_id, template_path in declared_templates.items():
            candidates = {template_path, f"templates/{template_path}"}
            if not candidates.intersection(model_files):
                missing.append(f"template:{template_id}:{template_path}")
        default_template = str(model.get("default_template") or "").strip()
        if default_template:
            default_path = declared_templates.get(default_template, default_template)
            candidates = {default_path, f"templates/{default_path}"}
            if not candidates.intersection(model_files):
                missing.append(f"default_template:{default_template}")

    async def export(
        self,
        session: AsyncSession,
        *,
        model_id: str,
        data_file_mode: DataFileMode,
        expected_plan_id: str | None = None,
    ) -> AnalysisProjectExportArtifact:
        plan = await self.build_plan(session, model_id=model_id, data_file_mode=data_file_mode)
        if expected_plan_id and plan.plan_id != expected_plan_id:
            raise AnalysisProjectExportError("导出计划已过期：模型或依赖在预览后发生变化，请刷新导出计划后重试。")
        if not plan.ready:
            raise AnalysisProjectExportError("分析项目依赖不完整：" + ", ".join(plan.missing_dependencies))
        model = self.models.get_model(model_id)
        table_assets = await self._resolve_all_table_assets(session, plan)
        entries: dict[str, bytes | Path] = {}

        self._add_model_files(entries, model)
        semantic_paths = self._add_semantic_files(entries, plan)
        rules = self._add_guardrails(entries, plan)
        copied_paths, profile_paths, bindings = self._add_data_assets(
            entries,
            plan=plan,
            table_assets=table_assets,
        )
        if any(binding.get("kind") == "logical_dataset_descriptor" for binding in bindings.values()):
            self._put_entry(
                entries,
                "data/runtime/materialize_logical.py",
                PORTABLE_LOGICAL_MATERIALIZER.encode("utf-8"),
            )
            self._put_entry(
                entries,
                "requirements-portable.txt",
                b"pandas>=2.0\nopenpyxl>=3.1\npyarrow>=14.0\n",
            )
        manifest = self._analysis_project_manifest(
            model=model,
            plan=plan,
            semantic_paths=semantic_paths,
            profile_paths=profile_paths,
            rules=rules,
            bindings=bindings,
            path_aliases=self._path_aliases(entries),
        )
        self._put_entry(entries, "analysis-project.yaml", _yaml_bytes(manifest))
        self._put_entry(entries, "bindings.local.yaml", _json_bytes({"bindings": bindings}))
        self._put_entry(
            entries,
            "bindings.example.yaml",
            _json_bytes({"bindings": self._example_bindings(bindings, plan.data_file_mode)}),
        )
        self._put_entry(entries, "AGENTS.md", self._agents_markdown(plan).encode("utf-8"))
        self._put_entry(entries, "README.md", self._readme_markdown(plan).encode("utf-8"))
        self._put_entry(entries, ".gitignore", b"bindings.local.yaml\n/data/*\n")
        self._put_entry(entries, "tests/validate_project.py", self._project_validator().encode("utf-8"))

        archive_root = plan.package_name
        fd, temp_name = tempfile.mkstemp(prefix="analysis-project-", suffix=".zip")
        os.close(fd)
        target = Path(temp_name)
        try:
            await asyncio.to_thread(
                self._write_archive,
                target,
                archive_root,
                entries,
                copied_paths,
                bindings,
            )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        version_slug = _safe_slug(plan.model_version)
        return AnalysisProjectExportArtifact(
            path=target,
            filename=f"{plan.package_name}-v{version_slug}.zip",
            plan=plan,
        )

    def _write_archive(
        self,
        target: Path,
        archive_root: str,
        entries: dict[str, bytes | Path],
        copied_paths: list[str],
        bindings: dict[str, dict[str, Any]],
    ) -> None:
        """Perform compression and file hashing off the async request loop."""

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            checksums: dict[str, str] = {}
            for relative_path, content in sorted(entries.items()):
                archive_path = f"{archive_root}/{relative_path}"
                if isinstance(content, bytes):
                    self._write_zip_bytes(archive, archive_path, content)
                    digest = _sha256_bytes(content)
                else:
                    digest = self._write_zip_path(archive, archive_path, content)
                if relative_path not in MUTABLE_PROJECT_FILES:
                    checksums[relative_path] = digest
            package_manifest = _json_bytes(
                {
                    "format": "analysis-project-package-manifest/v1",
                    "project": archive_root,
                    "files": checksums,
                    "mutable_files": list(MUTABLE_PROJECT_FILES),
                    "copied_data_paths": copied_paths,
                    "binding_contract": {
                        binding_id: {
                            key: value
                            for key, value in binding.items()
                            if key in {"kind", "sha256", "profile", "source_asset_ids", "materializer", "url_env"}
                        }
                        for binding_id, binding in bindings.items()
                    },
                }
            )
            self._write_zip_bytes(
                archive,
                f"{archive_root}/package-manifest.json",
                package_manifest,
            )

    async def _resolve_all_table_assets(
        self,
        session: AsyncSession,
        plan: AnalysisProjectExportPlan,
    ) -> dict[str, KnowledgeTableAsset]:
        resolved: dict[str, KnowledgeTableAsset] = {}

        async def load(asset_id: str) -> None:
            if not asset_id or asset_id in resolved:
                return
            asset = await session.get(KnowledgeTableAsset, asset_id)
            if asset is None:
                raise AnalysisProjectExportError(f"Table asset disappeared during export: {asset_id}")
            resolved[asset_id] = asset
            logical = (
                (asset.asset_metadata or {}).get("logical_dataset") if isinstance(asset.asset_metadata, dict) else None
            )
            for source_id in (logical or {}).get("source_asset_ids") or []:
                await load(str(source_id))

        for item in plan.data_assets:
            await load(item.asset_id)
        return resolved

    @staticmethod
    def _validate_destination(destination: str) -> str:
        if not destination or "\x00" in destination or "\\" in destination:
            raise AnalysisProjectExportError(f"Invalid archive destination: {destination!r}")
        path = PurePosixPath(destination)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise AnalysisProjectExportError(f"Invalid archive destination: {destination!r}")
        return path.as_posix()

    @classmethod
    def _put_entry(
        cls,
        entries: dict[str, bytes | Path],
        destination: str,
        content: bytes | Path,
        *,
        allowed_root: Path | None = None,
    ) -> None:
        safe_destination = cls._validate_destination(destination)
        if safe_destination in entries:
            raise AnalysisProjectExportError(f"Duplicate archive destination: {safe_destination}")
        if isinstance(content, bytes):
            entries[safe_destination] = content
            return
        source = content.expanduser().absolute()
        if not source.is_file():
            raise AnalysisProjectExportError(f"Export source file missing: {source}")
        source_resolved = source.resolve(strict=True)
        if allowed_root is not None:
            root_resolved = allowed_root.expanduser().resolve(strict=True)
            try:
                source_resolved.relative_to(root_resolved)
            except ValueError as exc:
                raise AnalysisProjectExportError(f"Export source escapes its declared root: {source}") from exc
            try:
                lexical_relative = source.relative_to(allowed_root.expanduser().absolute())
            except ValueError as exc:
                raise AnalysisProjectExportError(
                    f"Export source is not lexically under its declared root: {source}"
                ) from exc
            cursor = allowed_root.expanduser().absolute()
            for part in lexical_relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise AnalysisProjectExportError(f"Symlink export source is not allowed: {cursor}")
        elif source.is_symlink():
            raise AnalysisProjectExportError(f"Symlink export source is not allowed: {source}")
        entries[safe_destination] = source_resolved

    def _add_model_files(self, entries: dict[str, bytes | Path], model: dict[str, Any]) -> None:
        main_path = self.base_dir / str(model.get("path") or "")
        model_root = main_path.parent
        for item in model.get("files") or []:
            source = self.base_dir / str(item.get("path") or "")
            relative = str(item.get("relative_path") or source.name)
            if relative == "model.md":
                destination = "model/model.md"
            elif relative.startswith("templates/"):
                destination = relative
            elif relative.startswith("examples/"):
                destination = f"tests/{relative}"
            else:
                destination = f"model/{relative}"
            self._put_entry(entries, destination, source, allowed_root=model_root)

    def _add_semantic_files(
        self,
        entries: dict[str, bytes | Path],
        plan: AnalysisProjectExportPlan,
    ) -> dict[str, list[str]]:
        paths: dict[str, list[str]] = {"measures": [], "dimensions": [], "grains": [], "relations": []}
        for asset_id in [*plan.semantic_asset_ids, *plan.relation_ids]:
            asset = self.semantic_assets.get_asset(asset_id)
            asset_type = str(asset.get("type") or "")
            group = {"measure": "measures", "dimension": "dimensions", "grain": "grains", "relation": "relations"}[
                asset_type
            ]
            slug = Path(str(asset.get("path") or "")).parent.name
            asset_root = (self.base_dir / str(asset.get("path") or "")).parent
            main_destination = ""
            for item in asset.get("files") or []:
                source = self.base_dir / str(item.get("path") or "")
                relative = str(item.get("relative_path") or source.name)
                destination = f"semantic/{group}/{slug}/{relative}"
                self._put_entry(entries, destination, source, allowed_root=asset_root)
                if item.get("main"):
                    main_destination = f"./{destination}"
            if main_destination:
                paths[group].append(main_destination)
        return paths

    def _add_guardrails(
        self,
        entries: dict[str, bytes | Path],
        plan: AnalysisProjectExportPlan,
    ) -> list[dict[str, Any]]:
        available = {str(item.get("id") or ""): item for item in list_guardrail_rules().get("guardrails") or []}
        rules = [_public_guardrail(available[rule_id]) for rule_id in plan.guardrail_ids]
        for rule_id in plan.guardrail_ids:
            rule = available[rule_id]
            document_path = str(rule.get("document_path") or "")
            if document_path:
                source = (self.base_dir / document_path).resolve()
                if source.is_file():
                    self._put_entry(
                        entries,
                        f"guardrails/rules/{_safe_slug(rule_id)}/guardrail.md",
                        self.base_dir / document_path,
                        allowed_root=self.base_dir / "sql-guardrails" / "rules",
                    )
        rules_content = _json_bytes(rules)
        self._put_entry(entries, "guardrails/compiled/rules.json", rules_content)
        self._put_entry(
            entries,
            "guardrails/compiled/rules.lock.json",
            _json_bytes({"format": "portable-sql-guardrails/v1", "sha256": _sha256_bytes(rules_content)}),
        )
        self._put_entry(entries, "guardrails/runtime/validate_sql.py", PORTABLE_SQL_VALIDATOR.encode("utf-8"))
        self._put_entry(
            entries,
            "guardrails/runtime/guardrail_runtime.py",
            Path(guardrail_runtime.__file__).read_bytes(),
        )
        self._put_entry(
            entries,
            "guardrails/context.example.json",
            _json_bytes(
                {
                    "available_tables": [],
                    "semantic_asset_ids": plan.semantic_asset_ids,
                    "question": "",
                }
            ),
        )
        return rules

    def _add_data_assets(
        self,
        entries: dict[str, bytes | Path],
        *,
        plan: AnalysisProjectExportPlan,
        table_assets: dict[str, KnowledgeTableAsset],
    ) -> tuple[list[str], dict[str, str], dict[str, dict[str, Any]]]:
        copied_by_source: dict[Path, str] = {}
        profile_paths: dict[str, str] = {}
        bindings: dict[str, dict[str, Any]] = {}

        for asset_id, asset in sorted(table_assets.items()):
            source = Path(asset.storage_path).expanduser().absolute()
            source_resolved = source.resolve(strict=True)
            if plan.data_file_mode == "copy":
                project_path = copied_by_source.get(source_resolved)
                if project_path is None:
                    project_path = f"data/{asset_id}/{_source_filename(asset)}"
                    copied_by_source[source_resolved] = project_path
                    self._put_entry(entries, project_path, source)
                binding_path = f"./{project_path}"
                portable = True
            else:
                binding_path = str(source)
                portable = False
            profile_destination = f"profiles/{asset_id}.profile.json"
            profile_source = Path(asset.profile_path).expanduser().absolute() if asset.profile_path else None
            if profile_source and profile_source.is_file():
                self._put_entry(entries, profile_destination, profile_source)
            else:
                self._put_entry(
                    entries,
                    profile_destination,
                    _json_bytes(
                        {
                            "asset_id": asset_id,
                            "file_name": asset.file_name,
                            "source_type": asset.source_type,
                            "sheet_name": asset.sheet_name,
                            "shape": [asset.rows, asset.columns_count],
                            "columns": asset.columns or [],
                            "generated_from": "catalog_summary",
                        }
                    ),
                )
            profile_paths[asset_id] = f"./{profile_destination}"
            logical = (
                (asset.asset_metadata or {}).get("logical_dataset") if isinstance(asset.asset_metadata, dict) else None
            )
            bindings[asset_id] = {
                "kind": "logical_dataset_descriptor" if isinstance(logical, dict) else "spreadsheet",
                "path": binding_path,
                "sheet_name": asset.sheet_name,
                "profile": f"./{profile_destination}",
                "sha256": _sha256_path(source),
                "size_bytes": source.stat().st_size,
                "portable": portable,
                "source_asset_ids": [str(item) for item in (logical or {}).get("source_asset_ids") or []],
                "schema_mode": str((logical or {}).get("schema_mode") or "strict")
                if isinstance(logical, dict)
                else None,
                "canonical_columns": [str(item) for item in (logical or {}).get("canonical_columns") or []],
                "materializer": "./data/runtime/materialize_logical.py" if isinstance(logical, dict) else None,
                "provenance": {
                    "puddingclaw_asset_id": f"table_asset:{asset_id}",
                    "virtual_path": asset.virtual_path,
                },
            }

        for item in plan.data_assets:
            if item.kind != "database_table":
                continue
            source_id, separator, table_name = item.ref.rpartition(".")
            binding_id = _database_binding_id(item.ref)
            env_suffix = re.sub(r"[^0-9A-Za-z]+", "_", source_id or "DATABASE").strip("_").upper() or "DATABASE"
            bindings[binding_id] = {
                "kind": "database_table",
                "source_id": source_id if separator else "",
                "table": table_name if separator else item.ref,
                "url_env": f"ANALYSIS_DB_{env_suffix}_URL",
                "portable": False,
            }
        return sorted(f"./{item}" for item in copied_by_source.values()), profile_paths, bindings

    def _analysis_project_manifest(
        self,
        *,
        model: dict[str, Any],
        plan: AnalysisProjectExportPlan,
        semantic_paths: dict[str, list[str]],
        profile_paths: dict[str, str],
        rules: list[dict[str, Any]],
        bindings: dict[str, dict[str, Any]],
        path_aliases: dict[str, str],
    ) -> dict[str, Any]:
        data_sources: list[dict[str, Any]] = []
        declared_bindings: set[str] = set()
        for item in plan.data_assets:
            binding = _database_binding_id(item.ref) if item.kind == "database_table" else item.asset_id
            declared_bindings.add(binding)
            data_sources.append(
                {
                    "ref": item.ref,
                    "kind": item.kind,
                    "binding": binding,
                    "profile": profile_paths.get(item.asset_id),
                    "required": True,
                    "declared_by_model": True,
                }
            )
        for binding_id, binding in sorted(bindings.items()):
            if binding_id in declared_bindings:
                continue
            provenance = binding.get("provenance") if isinstance(binding.get("provenance"), dict) else {}
            data_sources.append(
                {
                    "ref": provenance.get("puddingclaw_asset_id") or binding_id,
                    "kind": binding.get("kind"),
                    "binding": binding_id,
                    "profile": binding.get("profile"),
                    "required": True,
                    "declared_by_model": False,
                    "required_by": [
                        parent_id
                        for parent_id, parent in bindings.items()
                        if binding_id in (parent.get("source_asset_ids") or [])
                    ],
                }
            )
        return {
            "format": EXPORT_FORMAT,
            "id": plan.model_id,
            "name": plan.model_name,
            "version": plan.model_version,
            "entry_model": "./model/model.md",
            "semantic_assets": semantic_paths,
            "data_sources": data_sources,
            "relations": semantic_paths.get("relations") or [],
            "guardrails": {
                "rules": "./guardrails/compiled/rules.json",
                "validator": "./guardrails/runtime/validate_sql.py",
                "count": len(rules),
            },
            "bindings": "./bindings.local.yaml",
            "path_aliases": path_aliases,
            "tests": {"root": "./tests", "project_validator": "./tests/validate_project.py"},
            "acceptance": (model.get("frontmatter") or {}).get("acceptance") or {},
            "export": {
                "data_file_mode": plan.data_file_mode,
                "source_platform": "PuddingClaw",
                "binding_count": len(bindings),
            },
        }

    def _path_aliases(self, entries: dict[str, bytes | Path]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for destination, source in entries.items():
            if not isinstance(source, Path):
                continue
            try:
                relative = source.resolve().relative_to(self.base_dir).as_posix()
            except ValueError:
                continue
            if relative.startswith(("semantic-assets/", "analytics-models/", "sql-guardrails/")):
                aliases[relative] = f"./{destination}"
                aliases[f"/{relative}"] = f"./{destination}"
        return dict(sorted(aliases.items()))

    @staticmethod
    def _example_bindings(
        bindings: dict[str, dict[str, Any]],
        mode: DataFileMode,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for binding_id, binding in bindings.items():
            item = json.loads(json.dumps(binding, ensure_ascii=False, default=str))
            if item.get("kind") in {"spreadsheet", "logical_dataset_descriptor"} and mode == "reference":
                item["path"] = f"/absolute/path/to/{Path(str(item.get('path') or 'data-file')).name}"
            result[binding_id] = item
        return result

    @staticmethod
    def _agents_markdown(plan: AnalysisProjectExportPlan) -> str:
        return f"""# {plan.model_name} analysis project

Read `analysis-project.yaml` first, then `model/model.md`. Load only the semantic assets relevant to the current question.

## Required workflow

1. Resolve physical data from `bindings.local.yaml`; never infer a missing path, sheet, table, or field.
2. Treat files under `semantic/` as the authority for measures, dimensions, grains, references, and relations.
3. Keep calculation grain, numerator, denominator, filters, and classifications explicit in every conclusion.
4. Before executing generated SQL, copy `guardrails/context.example.json` to a local context file, fill the current tables, semantic asset IDs and question, then run `python guardrails/runtime/validate_sql.py <sql-file> --context <context-file>`.
5. Run `python tests/validate_project.py` before relying on this project after moving it to another machine.
6. If a required binding, profile, semantic asset, or guardrail is missing, stop and report the missing dependency.

PuddingClaw asset IDs are provenance only. External execution must use the project-relative or absolute paths in the binding file.
"""

    @staticmethod
    def _readme_markdown(plan: AnalysisProjectExportPlan) -> str:
        data_note = (
            "数据文件已复制到 `data/`，项目可以随 ZIP 一起迁移。"
            if plan.data_file_mode == "copy"
            else "数据文件未复制；`bindings.local.yaml` 保存当前机器绝对路径，迁移后需要重新绑定。"
        )
        return f"""# {plan.model_name}

这是从 PuddingClaw 分析模型导出的文件系统型分析项目，可直接解压并作为 Codex、Claude Code 或其他本地 Agent 的项目目录打开。

{data_note}

## 开始使用

1. 检查 `bindings.local.yaml` 中的数据路径或数据库环境变量。
2. 运行 `python tests/validate_project.py` 检查文件完整性与 hash。
3. 向 Agent 提出分析问题；项目级执行规范位于 `AGENTS.md`。

数据库密码和连接 URL 不会写入导出包。数据库绑定使用 `url_env` 指定的环境变量。
"""

    @staticmethod
    def _project_validator() -> str:
        return """#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / "package-manifest.json").read_text(encoding="utf-8"))
failures = []
warnings = []
def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

for relative_path, expected in (manifest.get("files") or {}).items():
    path = ROOT / relative_path
    if not path.is_file():
        failures.append({"path": relative_path, "reason": "missing"})
        continue
    actual = sha256(path)
    if actual != expected:
        failures.append({"path": relative_path, "reason": "hash_mismatch", "expected": expected, "actual": actual})
for relative_path in manifest.get("mutable_files") or []:
    if not (ROOT / relative_path).is_file():
        failures.append({"path": relative_path, "reason": "missing_mutable_file"})

bindings_path = ROOT / "bindings.local.yaml"
try:
    bindings = json.loads(bindings_path.read_text(encoding="utf-8")).get("bindings") or {}
except Exception as exc:
    bindings = {}
    failures.append({"path": "bindings.local.yaml", "reason": "invalid_json", "detail": str(exc)})

contract = manifest.get("binding_contract") or {}
for binding_id, expected in contract.items():
    binding = bindings.get(binding_id)
    if not isinstance(binding, dict):
        failures.append({"binding": binding_id, "reason": "missing_binding"})
        continue
    if binding.get("kind") != expected.get("kind"):
        failures.append({"binding": binding_id, "reason": "kind_mismatch"})
        continue
    kind = binding.get("kind")
    if kind == "logical_dataset_descriptor" and [str(item) for item in binding.get("source_asset_ids") or []] != [str(item) for item in expected.get("source_asset_ids") or []]:
        failures.append({"binding": binding_id, "reason": "logical_sources_changed"})
    if kind in {"spreadsheet", "logical_dataset_descriptor"}:
        raw_path = str(binding.get("path") or "")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        if not path.is_file():
            failures.append({"binding": binding_id, "path": raw_path, "reason": "data_file_missing"})
        elif expected.get("sha256") and sha256(path) != expected["sha256"]:
            failures.append({"binding": binding_id, "path": raw_path, "reason": "data_hash_mismatch"})
        raw_profile = str(binding.get("profile") or expected.get("profile") or "")
        profile = Path(raw_profile)
        if raw_profile and not profile.is_absolute():
            profile = (ROOT / profile).resolve()
        if raw_profile and not profile.is_file():
            failures.append({"binding": binding_id, "path": raw_profile, "reason": "profile_missing"})
    elif kind == "database_table" and not binding.get("url_env"):
        failures.append({"binding": binding_id, "reason": "database_url_env_missing"})

def visit(binding_id, stack=()):
    if binding_id in stack:
        failures.append({"binding": binding_id, "reason": "logical_cycle", "stack": [*stack, binding_id]})
        return
    binding = bindings.get(binding_id) or {}
    if binding.get("kind") != "logical_dataset_descriptor":
        return
    sources = [str(item) for item in binding.get("source_asset_ids") or []]
    if not sources:
        failures.append({"binding": binding_id, "reason": "logical_sources_missing"})
    for source_id in sources:
        if source_id not in bindings:
            failures.append({"binding": binding_id, "source": source_id, "reason": "logical_source_binding_missing"})
        else:
            visit(source_id, (*stack, binding_id))
    materializer = Path(str(binding.get("materializer") or ""))
    if str(materializer) and not materializer.is_absolute():
        materializer = (ROOT / materializer).resolve()
    if not str(binding.get("materializer") or "") or not materializer.is_file():
        failures.append({"binding": binding_id, "reason": "materializer_missing"})

for binding_id in contract:
    visit(binding_id)

rules_path = ROOT / "guardrails/compiled/rules.json"
lock_path = ROOT / "guardrails/compiled/rules.lock.json"
if rules_path.is_file() and lock_path.is_file():
    try:
        expected = json.loads(lock_path.read_text(encoding="utf-8"))["sha256"]
        if sha256(rules_path) != expected:
            failures.append({"path": "guardrails/compiled/rules.json", "reason": "rules_lock_mismatch"})
    except Exception as exc:
        failures.append({"path": "guardrails/compiled/rules.lock.json", "reason": "invalid_rules_lock", "detail": str(exc)})

result = {
    "passed": not failures,
    "checked": len(manifest.get("files") or {}),
    "bindings_checked": len(contract),
    "failures": failures,
    "warnings": warnings,
}
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["passed"] else 1)
"""

    @staticmethod
    def _zip_info(path: str, *, executable: bool = False) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = ((0o755 if executable else 0o644) & 0xFFFF) << 16
        return info

    @classmethod
    def _write_zip_bytes(cls, archive: zipfile.ZipFile, path: str, content: bytes) -> None:
        executable = path.endswith(".py")
        archive.writestr(cls._zip_info(path, executable=executable), content)

    @classmethod
    def _write_zip_path(cls, archive: zipfile.ZipFile, path: str, source: Path) -> str:
        executable = path.endswith(".py")
        before = source.stat()
        digest = hashlib.sha256()
        with (
            source.open("rb") as source_handle,
            archive.open(cls._zip_info(path, executable=executable), "w") as target_handle,
        ):
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                digest.update(chunk)
                target_handle.write(chunk)
        after = source.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise AnalysisProjectExportError(f"Source file changed during export: {source}")
        return digest.hexdigest()
