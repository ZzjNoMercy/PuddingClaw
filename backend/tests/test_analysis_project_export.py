from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from analytics.project_export import AnalysisProjectExporter, AnalysisProjectExportError
from knowledge.models import KnowledgeDatabaseSource, KnowledgeTableAsset


class _FakeSession:
    def __init__(self, assets: dict[str, object]):
        self.assets = assets

    async def get(self, model_type, key):
        assert model_type in {KnowledgeTableAsset, KnowledgeDatabaseSource}
        return self.assets.get(key)


def _write_model_tree(base_dir: Path, *, data_ref: str = "table_asset:tbl_sales") -> None:
    model_dir = base_dir / "analytics-models" / "sales-analysis"
    model_dir.mkdir(parents=True)
    (model_dir / "templates").mkdir()
    (model_dir / "templates" / "report.md").write_text("# report\n", encoding="utf-8")
    (model_dir / "model.md").write_text(
        f"""---
formatter: analytics-model
id: sales-analysis
name: 销量分析
version: 1.2.0
data_assets:
  tables:
    - {data_ref}
semantic_assets:
  measures:
    - measure:sales
  dimensions:
    - dimension:brand
  grains: []
asset_relations:
  - relation:sales-brand
guardrails:
  - sales_requires_brand
templates: {{}}
default_template: report.md
---

# 销量分析
""",
        encoding="utf-8",
    )
    for group, slug, filename, name, asset_type in (
        ("measures", "sales", "measure.md", "销量", "measure"),
        ("dimensions", "brand", "dimension.md", "品牌", "dimension"),
        ("relations", "sales-brand", "relation.md", "销量品牌关联", "relation"),
    ):
        asset_dir = base_dir / "semantic-assets" / group / slug
        asset_dir.mkdir(parents=True)
        asset_id = f"{asset_type}:{slug}"
        (asset_dir / filename).write_text(
            f"---\nformatter: semantic-asset\nname: {name}\ntype: {asset_type}\nid: {asset_id}\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    references = base_dir / "semantic-assets" / "measures" / "sales" / "references"
    references.mkdir()
    (references / "sales-table.md").write_text("# 销量表字段口径\n", encoding="utf-8")


def _table_asset(base_dir: Path) -> KnowledgeTableAsset:
    data_path = base_dir / "knowledge" / "sales.csv"
    data_path.parent.mkdir(parents=True)
    data_path.write_text("brand,sales\nBYD,10\n", encoding="utf-8")
    profile_path = base_dir / "knowledge" / ".puddingclaw" / "table_profiles" / "tbl_sales.profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        json.dumps({"asset_id": "tbl_sales", "shape": [1, 2], "columns": ["brand", "sales"]}),
        encoding="utf-8",
    )
    return KnowledgeTableAsset(
        asset_id="tbl_sales",
        knowledge_base_id="default",
        source_type="csv",
        file_name="sales.csv",
        storage_path=str(data_path),
        virtual_path="/knowledge/sales.csv",
        sheet_name=None,
        size_bytes=data_path.stat().st_size,
        content_sha256=hashlib.sha256(data_path.read_bytes()).hexdigest(),
        profile_status="ready",
        profile_path=str(profile_path),
        rows=1,
        columns_count=2,
        columns=["brand", "sales"],
        reference_status="ready",
        asset_metadata={},
    )


def _guardrails():
    return {
        "guardrails": [
            {
                "id": "sales_requires_brand",
                "name": "销量必须包含品牌",
                "enabled": True,
                "type": "require_sql_contains",
                "scope": {},
                "params": {"contains": "brand"},
                "action": {"type": "block", "message": "SQL 必须包含 brand"},
            }
        ],
        "diagnostics": [],
    }


@pytest.mark.asyncio
async def test_export_copy_mode_builds_self_contained_project(tmp_path: Path, monkeypatch) -> None:
    _write_model_tree(tmp_path)
    model_dir = tmp_path / "analytics-models" / "sales-analysis"
    (model_dir / ".DS_Store").write_bytes(b"finder metadata")
    (model_dir / "templates" / "._report.md").write_bytes(b"appledouble metadata")
    asset = _table_asset(tmp_path)
    monkeypatch.setattr("analytics.project_export.service.list_guardrail_rules", _guardrails)
    exporter = AnalysisProjectExporter(tmp_path)
    session = _FakeSession({asset.asset_id: asset})

    plan = await exporter.build_plan(session, model_id="sales-analysis", data_file_mode="copy")
    assert plan.ready
    assert plan.plan_id.startswith("export-plan-")
    assert plan.data_assets[0].source_path == ""
    assert plan.copied_file_count == 1
    assert plan.copied_bytes == asset.size_bytes

    artifact = await exporter.export(session, model_id="sales-analysis", data_file_mode="copy")
    try:
        extract_root = tmp_path / "extracted"
        with zipfile.ZipFile(artifact.path) as archive:
            names = set(archive.namelist())
            root = "sales-analysis/"
            assert root + "analysis-project.yaml" in names
            assert root + "AGENTS.md" in names
            assert root + "semantic/measures/sales/measure.md" in names
            assert root + "semantic/measures/sales/references/sales-table.md" in names
            assert root + "guardrails/runtime/validate_sql.py" in names
            assert root + "guardrails/runtime/guardrail_runtime.py" in names
            assert root + "data/tbl_sales/sales.csv" in names
            assert not any(name.endswith(".DS_Store") or "/._" in name for name in names)
            generated_at = datetime(*archive.getinfo(root + "analysis-project.yaml").date_time)
            source_modified_at = datetime(*archive.getinfo(root + "data/tbl_sales/sales.csv").date_time)
            assert generated_at.year == datetime.now().year
            assert source_modified_at.year == datetime.fromtimestamp(Path(asset.storage_path).stat().st_mtime).year
            yaml_mode = archive.getinfo(root + "bindings.example.yaml").external_attr >> 16
            validator_mode = archive.getinfo(root + "tests/validate_project.py").external_attr >> 16
            assert stat.S_ISREG(yaml_mode)
            assert stat.S_IMODE(yaml_mode) == 0o644
            assert stat.S_ISREG(validator_mode)
            assert stat.S_IMODE(validator_mode) == 0o755
            archive.extractall(extract_root)

        project_root = extract_root / "sales-analysis"
        manifest = yaml.safe_load((project_root / "analysis-project.yaml").read_text(encoding="utf-8"))
        assert manifest["format"] == "analysis-project/v1"
        assert not any(".DS_Store" in path or "/._" in path for path in manifest["path_aliases"])
        assert manifest["semantic_assets"]["measures"] == ["./semantic/measures/sales/measure.md"]
        bindings = yaml.safe_load((project_root / "bindings.local.yaml").read_text(encoding="utf-8"))["bindings"]
        assert bindings["tbl_sales"]["path"] == "./data/tbl_sales/sales.csv"
        assert bindings["tbl_sales"]["portable"] is True

        validation = subprocess.run(
            [sys.executable, "tests/validate_project.py"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert validation.returncode == 0, validation.stdout + validation.stderr

        package_manifest_path = project_root / "package-manifest.json"
        package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
        package_manifest["files"]["../outside.txt"] = "0" * 64
        package_manifest_path.write_text(json.dumps(package_manifest), encoding="utf-8")
        unsafe_manifest_validation = subprocess.run(
            [sys.executable, "tests/validate_project.py"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert unsafe_manifest_validation.returncode == 1
        assert "unsafe_project_resource_path" in unsafe_manifest_validation.stdout
        package_manifest["files"].pop("../outside.txt")
        package_manifest_path.write_text(json.dumps(package_manifest), encoding="utf-8")

        # Mutable bindings may be edited, but required bindings cannot disappear.
        (project_root / "bindings.local.yaml").write_text("bindings: {}\n", encoding="utf-8")
        rebound_validation = subprocess.run(
            [sys.executable, "tests/validate_project.py"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert rebound_validation.returncode == 1
        assert "missing_binding" in rebound_validation.stdout

        sql_path = project_root / "query.sql"
        sql_path.write_text("SELECT SUM(sales) FROM sales", encoding="utf-8")
        guardrail = subprocess.run(
            [sys.executable, "guardrails/runtime/validate_sql.py", str(sql_path)],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert guardrail.returncode == 1
        assert "sales_requires_brand" in guardrail.stdout
    finally:
        artifact.path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_export_reference_mode_keeps_absolute_path_without_copying_data(tmp_path: Path, monkeypatch) -> None:
    _write_model_tree(tmp_path)
    asset = _table_asset(tmp_path)
    monkeypatch.setattr("analytics.project_export.service.list_guardrail_rules", _guardrails)
    exporter = AnalysisProjectExporter(tmp_path)
    session = _FakeSession({asset.asset_id: asset})

    artifact = await exporter.export(session, model_id="sales-analysis", data_file_mode="reference")
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            names = set(archive.namelist())
            assert not any("/data/" in name for name in names)
            bindings = yaml.safe_load(archive.read("sales-analysis/bindings.local.yaml"))["bindings"]
            assert bindings["tbl_sales"]["path"] == str(Path(asset.storage_path).resolve())
            assert bindings["tbl_sales"]["portable"] is False
        plan = await exporter.build_plan(session, model_id="sales-analysis", data_file_mode="reference")
        assert plan.data_assets[0].source_path == str(Path(asset.storage_path).resolve())
    finally:
        artifact.path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_database_export_includes_connection_contract_without_credentials(tmp_path: Path, monkeypatch) -> None:
    _write_model_tree(tmp_path, data_ref="dbs_sales.public.sales")
    monkeypatch.setattr("analytics.project_export.service.list_guardrail_rules", _guardrails)
    source = KnowledgeDatabaseSource(
        id="dbs_sales",
        knowledge_base_id="kb_default",
        source_type="postgresql",
        name="Sales Warehouse",
        description="",
        host="db.internal.example",
        port=5432,
        database="analytics",
        username="secret-user",
        password="secret-password",
        selected_tables=["public.sales", "private.audit_log"],
        source_metadata={"schema": "public"},
    )
    exporter = AnalysisProjectExporter(tmp_path)
    session = _FakeSession({source.id: source})

    plan = await exporter.build_plan(session, model_id="sales-analysis", data_file_mode="reference")
    assert plan.ready
    assert plan.data_assets[0].host == "db.internal.example"
    assert plan.data_assets[0].database == "analytics"
    assert plan.data_assets[0].schema_name == "public"

    artifact = await exporter.export(session, model_id="sales-analysis", data_file_mode="reference")
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            root = "sales-analysis/"
            binding_payload = json.loads(archive.read(root + "bindings.local.yaml"))
            binding = next(iter(binding_payload["bindings"].values()))
            assert binding["connection"] == {
                "type": "postgresql",
                "host": "db.internal.example",
                "port": 5432,
                "database": "analytics",
                "schema": "public",
            }
            assert binding["table"] == "public.sales"
            assert binding["credentials"]["mode"] == "agent_configured"
            env_example = archive.read(root + ".env.example").decode("utf-8")
            assert "ANALYSIS_DB_DBS_SALES_USERNAME=" in env_example
            assert "ANALYSIS_DB_DBS_SALES_PASSWORD=" in env_example
            assert "ANALYSIS_DB_DBS_SALES_URL=" in env_example
            assert "public.sales" in env_example
            exported_text = "\n".join(
                archive.read(name).decode("utf-8", errors="ignore")
                for name in archive.namelist()
                if not name.endswith("/")
            )
            assert "secret-user" not in exported_text
            assert "secret-password" not in exported_text
            assert "private.audit_log" not in exported_text
    finally:
        artifact.path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_export_fails_closed_when_required_asset_is_missing(tmp_path: Path, monkeypatch) -> None:
    _write_model_tree(tmp_path)
    monkeypatch.setattr("analytics.project_export.service.list_guardrail_rules", _guardrails)
    exporter = AnalysisProjectExporter(tmp_path)
    session = _FakeSession({})

    plan = await exporter.build_plan(session, model_id="sales-analysis", data_file_mode="copy")
    assert not plan.ready
    assert "table_asset:tbl_sales" in plan.missing_dependencies
    with pytest.raises(AnalysisProjectExportError, match="依赖不完整"):
        await exporter.export(session, model_id="sales-analysis", data_file_mode="copy")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource_prefix",
    ["", "templates/"],
)
async def test_export_plan_refreshes_model_registry_after_external_template_rename(
    tmp_path: Path,
    monkeypatch,
    resource_prefix: str,
) -> None:
    _write_model_tree(tmp_path)
    asset = _table_asset(tmp_path)
    monkeypatch.setattr("analytics.project_export.service.list_guardrail_rules", _guardrails)
    exporter = AnalysisProjectExporter(tmp_path)
    session = _FakeSession({asset.asset_id: asset})

    initial_plan = await exporter.build_plan(session, model_id="sales-analysis", data_file_mode="copy")
    assert initial_plan.ready

    model_dir = tmp_path / "analytics-models" / "sales-analysis"
    model_path = model_dir / "model.md"
    updated_model = model_path.read_text(encoding="utf-8").replace(
        "templates: {}\ndefault_template: report.md",
        "templates:\n  monthly_report:\n"
        f"    path: {resource_prefix}monthly_report.html\n"
        f"    guide: {resource_prefix}TEMPLATE.md\n"
        "    assets:\n"
        f"      - {resource_prefix}renderer.js\n"
        "default_template: monthly_report",
    )
    model_path.write_text(updated_model, encoding="utf-8")
    (model_dir / "templates" / "report.md").rename(model_dir / "templates" / "monthly_report.html")
    (model_dir / "templates" / "TEMPLATE.md").write_text("# Guide\n", encoding="utf-8")
    (model_dir / "templates" / "renderer.js").write_text("window.render = () => {};\n", encoding="utf-8")

    refreshed_plan = await exporter.build_plan(session, model_id="sales-analysis", data_file_mode="copy")

    assert refreshed_plan.ready
    assert refreshed_plan.missing_dependencies == []
    assert refreshed_plan.plan_id != initial_plan.plan_id

    artifact = await exporter.export(session, model_id="sales-analysis", data_file_mode="copy")
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            manifest = yaml.safe_load(archive.read("sales-analysis/analysis-project.yaml"))
            assert manifest["templates"]["monthly_report"]["path"] == "./templates/monthly_report.html"
            assert manifest["templates"]["monthly_report"]["guide"] == "./templates/TEMPLATE.md"
            assert manifest["templates"]["monthly_report"]["assets"] == ["./templates/renderer.js"]
            package_manifest = json.loads(archive.read("sales-analysis/package-manifest.json"))
            assert "./templates/monthly_report.html" in package_manifest["project_resource_paths"]
            assert "./templates/TEMPLATE.md" in package_manifest["project_resource_paths"]
            assert "./templates/renderer.js" in package_manifest["project_resource_paths"]
            assert "sales-analysis/templates/renderer.js" in archive.namelist()
            assert "templates/templates" not in archive.read(
                "sales-analysis/analysis-project.yaml"
            ).decode("utf-8")
    finally:
        artifact.path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_logical_dataset_recursively_exports_physical_sources(tmp_path: Path, monkeypatch) -> None:
    _write_model_tree(tmp_path, data_ref="table_asset:tbl_logical")
    source = _table_asset(tmp_path)
    definition_path = tmp_path / "data" / "analytics-concat-datasets" / "tbl_logical" / "dataset.json"
    definition_path.parent.mkdir(parents=True)
    definition_path.write_text(
        json.dumps({"formatter": "logical-data-asset", "source_asset_ids": ["tbl_sales"]}),
        encoding="utf-8",
    )
    logical = KnowledgeTableAsset(
        asset_id="tbl_logical",
        knowledge_base_id="default",
        source_type="logical_concat",
        file_name="销量逻辑表",
        storage_path=str(definition_path),
        virtual_path="/knowledge/.puddingclaw/derived/concat/tbl_logical/dataset.json",
        sheet_name=None,
        size_bytes=definition_path.stat().st_size,
        content_sha256=hashlib.sha256(definition_path.read_bytes()).hexdigest(),
        profile_status="missing",
        profile_path="",
        rows=1,
        columns_count=2,
        columns=["brand", "sales"],
        reference_status="ready",
        asset_metadata={"logical_dataset": {"source_asset_ids": ["tbl_sales"]}},
    )
    monkeypatch.setattr("analytics.project_export.service.list_guardrail_rules", _guardrails)
    exporter = AnalysisProjectExporter(tmp_path)
    session = _FakeSession({source.asset_id: source, logical.asset_id: logical})

    plan = await exporter.build_plan(session, model_id="sales-analysis", data_file_mode="copy")
    assert plan.ready
    assert plan.copied_file_count == 2
    assert plan.data_assets[0].source_asset_ids == ["tbl_sales"]

    artifact = await exporter.export(session, model_id="sales-analysis", data_file_mode="copy")
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            names = set(archive.namelist())
            assert "sales-analysis/data/tbl_logical/dataset.json" in names
            assert "sales-analysis/data/tbl_sales/sales.csv" in names
            bindings = yaml.safe_load(archive.read("sales-analysis/bindings.local.yaml"))["bindings"]
            assert bindings["tbl_logical"]["source_asset_ids"] == ["tbl_sales"]
            assert bindings["tbl_sales"]["path"] == "./data/tbl_sales/sales.csv"
            archive.extractall(tmp_path / "logical-extracted")
        project_root = tmp_path / "logical-extracted" / "sales-analysis"
        materialized = subprocess.run(
            [sys.executable, "data/runtime/materialize_logical.py", "tbl_logical", "--output", "materialized.csv"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert materialized.returncode == 0, materialized.stdout + materialized.stderr
        assert (project_root / "materialized.csv").read_text(encoding="utf-8").startswith("brand,sales")
    finally:
        artifact.path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_export_rejects_stale_plan_and_symlink_sources(tmp_path: Path, monkeypatch) -> None:
    _write_model_tree(tmp_path)
    asset = _table_asset(tmp_path)
    monkeypatch.setattr("analytics.project_export.service.list_guardrail_rules", _guardrails)
    exporter = AnalysisProjectExporter(tmp_path)
    session = _FakeSession({asset.asset_id: asset})

    plan = await exporter.build_plan(session, model_id="sales-analysis", data_file_mode="copy")
    model_path = tmp_path / "analytics-models" / "sales-analysis" / "model.md"
    model_path.write_text(model_path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    with pytest.raises(AnalysisProjectExportError, match="导出计划已过期"):
        await exporter.export(
            session,
            model_id="sales-analysis",
            data_file_mode="copy",
            expected_plan_id=plan.plan_id,
        )

    real_path = Path(asset.storage_path)
    symlink_path = real_path.with_name("sales-link.csv")
    symlink_path.symlink_to(real_path)
    asset.storage_path = str(symlink_path)
    with pytest.raises(AnalysisProjectExportError, match="Symlink export source"):
        await exporter.export(session, model_id="sales-analysis", data_file_mode="copy")


def test_export_entry_contract_rejects_unsafe_or_duplicate_destinations(tmp_path: Path) -> None:
    entries: dict[str, bytes | Path] = {}
    AnalysisProjectExporter._put_entry(entries, "model/model.md", b"ok")
    with pytest.raises(AnalysisProjectExportError, match="Duplicate archive destination"):
        AnalysisProjectExporter._put_entry(entries, "model/model.md", b"duplicate")
    with pytest.raises(AnalysisProjectExportError, match="Invalid archive destination"):
        AnalysisProjectExporter._put_entry(entries, "../escape", b"escape")
