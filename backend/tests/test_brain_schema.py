from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.brain_schema import router
from knowledge.brain_schema import BrainSchemaError, BrainSchemaService, SchemaPackManifest


@pytest.fixture()
def schema_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BrainSchemaService:
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.delenv("PUDDINGCLAW_GBRAIN_SCHEMA_DIR", raising=False)
    service = BrainSchemaService(Path(__file__).resolve().parent.parent)
    try:
        service.catalog()
    except BrainSchemaError:
        pytest.skip("installed or source-tree gbrain schema catalog is unavailable")
    return service


def test_catalog_renders_real_bundled_manifests(schema_env: BrainSchemaService) -> None:
    catalog = schema_env.catalog()
    names = {pack["name"] for pack in catalog["packs"]}
    assert names == {
        "gbrain-base",
        "gbrain-base-v2",
        "gbrain-creator",
        "gbrain-engineer",
        "gbrain-everything",
        "gbrain-investor",
        "gbrain-recommended",
    }
    recommended = catalog["packs"][0]
    assert recommended["name"] == "gbrain-base-v2"
    assert recommended["manifest"]["api_version"] == "gbrain-schema-pack-v1"
    assert hashlib.sha256(recommended["raw_yaml"].encode()).hexdigest() == recommended["manifest_sha256"]


def test_initialize_creates_schema_bundle_and_resolved_preview(schema_env: BrainSchemaService) -> None:
    bundle = schema_env.initialize()
    root = Path(bundle["brain_root"])
    assert (root / "raw" / "manifest.jsonl").exists()
    assert (root / "wiki" / "index.md").read_text() == "# Wiki Index\n"
    assert (root / "wiki" / "log.md").read_text() == "# Wiki Ingest Log\n"
    assert (root / "AGENTS.md").exists()
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "# LLM Wiki Agent 操作契约" in agents
    assert "## Ingest（摄取）" in agents
    assert "`raw/` 只读" in agents
    assert "`research_paper`：`papers/<slug>.md`" in agents
    assert "`concept`：`concepts/<slug>.md`、`concept/<slug>.md`" in agents
    assert "不是目标 Wiki 的分类结论" in agents
    assert "长期实体—稳定主题—关系" in agents
    assert "`source_refs`、文件路径、URL 和其他引用字段只表示来源线索" in agents
    assert "现有 `index.md` 只用于发现和解析已有页面 slug，不是事实证据" in agents
    assert "不得为了避免孤立页面或满足“互链”而添加关系" in agents
    assert "暂时没有可信关系的页面可以保持孤立" in agents
    assert "严格保留专有名词及主客体" in agents
    assert "[[wiki/" not in agents
    assert bundle["custom"]["manifest"]["extends"] == "gbrain-base-v2"
    resolved_types = {item["name"] for item in bundle["resolved"]["manifest"]["page_types"]}
    assert {
        "person",
        "company",
        "concept",
        "system",
        "debate",
        "research_paper",
        "software_framework",
        "ai_model",
        "programming_language",
        "engineering_practice",
    }.issubset(resolved_types)
    custom_types = {item["name"]: item for item in bundle["custom"]["manifest"]["page_types"]}
    assert custom_types["research_paper"]["path_prefixes"] == ["papers/"]
    assert custom_types["software_framework"]["path_prefixes"] == ["frameworks/"]
    assert custom_types["ai_model"]["aliases"] == ["model", "llm", "foundation-model"]
    assert "subtypes" not in custom_types["engineering_practice"]

    # Initialization is non-destructive.
    index_path = root / "wiki" / "index.md"
    index_path.write_text("# Existing Index\n", encoding="utf-8")
    schema_env.initialize()
    assert index_path.read_text(encoding="utf-8") == "# Existing Index\n"


def test_preview_and_save_custom_pack_with_optimistic_hash(schema_env: BrainSchemaService) -> None:
    bundle = schema_env.initialize()
    manifest = deepcopy(bundle["custom"]["manifest"])
    manifest["version"] = "0.2.0"
    manifest["page_types"].append(
        {
            "name": "decision",
            "primitive": "concept",
            "path_prefixes": [],
            "aliases": [],
            "extractable": False,
            "expert_routing": False,
        }
    )
    preview = schema_env.preview_custom(manifest)
    assert "decision" in {item["name"] for item in preview["resolved"]["manifest"]["page_types"]}
    saved = schema_env.save_custom(
        manifest,
        expected_sha256=bundle["custom"]["manifest_sha256"],
        expected_bundle_hash=bundle["bundle_hash"],
    )
    assert saved["brain_schema"]["document"]["bundle_version"] == manifest["version"]
    assert saved["custom"]["raw_yaml"] == schema_env.custom_pack_path.read_text(encoding="utf-8")
    assert hashlib.sha256(saved["custom"]["raw_yaml"].encode()).hexdigest() == saved["custom"]["manifest_sha256"]
    with pytest.raises(BrainSchemaError, match="changed since"):
        schema_env.save_custom(
            manifest,
            expected_sha256=bundle["custom"]["manifest_sha256"],
            expected_bundle_hash=bundle["bundle_hash"],
        )


def test_official_manifest_rejects_unknown_keys() -> None:
    with pytest.raises(Exception, match="extra_forbidden"):
        SchemaPackManifest.model_validate(
            {
                "api_version": "gbrain-schema-pack-v1",
                "name": "example",
                "version": "1.0.0",
                "not_official": True,
            }
        )


def test_brain_schema_api_e2e(schema_env: BrainSchemaService) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    catalog_response = client.get("/api/knowledge/brain/schema/catalog")
    assert catalog_response.status_code == 200
    assert catalog_response.json()["count"] == 7

    init_response = client.post("/api/knowledge/brain/initialize")
    assert init_response.status_code == 200
    bundle = init_response.json()

    agents_response = client.post("/api/knowledge/brain/agents/rebuild")
    assert agents_response.status_code == 200
    assert agents_response.json()["agents"]["sha256"] == bundle["agents"]["sha256"]

    manifest = deepcopy(bundle["custom"]["manifest"])
    manifest["version"] = "0.1.1"
    manifest["link_types"].append({"name": "decides", "inverse": "decided_by"})
    preview_response = client.post(
        "/api/knowledge/brain/schema/custom/preview",
        json={"manifest": manifest},
    )
    assert preview_response.status_code == 200
    assert preview_response.json()["validation_mode"] == "structural"
    assert "decides" in {item["name"] for item in preview_response.json()["resolved"]["manifest"]["link_types"]}

    save_response = client.put(
        "/api/knowledge/brain/schema/custom",
        json={
            "manifest": manifest,
            "expected_sha256": bundle["custom"]["manifest_sha256"],
            "expected_bundle_hash": bundle["bundle_hash"],
        },
    )
    assert save_response.status_code == 200
    assert save_response.json()["custom"]["manifest"]["link_types"][-1]["name"] == "decides"


def test_generated_pack_passes_gbrain_cli_validate(schema_env: BrainSchemaService, tmp_path: Path) -> None:
    binary = shutil.which("gbrain")
    if not binary:
        pytest.skip("gbrain CLI is unavailable")
    bundle = schema_env.initialize()
    isolated_root = tmp_path / "gbrain-runtime"
    install_dir = isolated_root / ".gbrain" / "schema-packs" / "puddingclaw-wiki"
    install_dir.mkdir(parents=True)
    shutil.copy2(schema_env.custom_pack_path, install_dir / "pack.yaml")
    env = os.environ.copy()
    env["GBRAIN_HOME"] = str(isolated_root)
    result = subprocess.run(
        [binary, "schema", "validate", "puddingclaw-wiki"],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert bundle["custom"]["manifest"]["name"] in result.stdout


def test_preview_rejects_gbrain_semantic_alias_cycle(schema_env: BrainSchemaService) -> None:
    bundle = schema_env.initialize()
    manifest = deepcopy(bundle["custom"]["manifest"])
    manifest["page_types"] = [
        {"name": "a", "primitive": "concept", "aliases": ["b"]},
        {"name": "b", "primitive": "concept", "aliases": ["c"]},
        {"name": "c", "primitive": "concept", "aliases": ["a"]},
    ]
    with pytest.raises(BrainSchemaError, match="gbrain official Schema"):
        schema_env.preview_custom(manifest)


def test_schema_content_change_requires_semver_bump(schema_env: BrainSchemaService) -> None:
    bundle = schema_env.initialize()
    manifest = deepcopy(bundle["custom"]["manifest"])
    manifest["link_types"].append({"name": "decides", "inverse": "decided_by"})
    with pytest.raises(BrainSchemaError, match="version must be bumped"):
        schema_env.save_custom(
            manifest,
            expected_sha256=bundle["custom"]["manifest_sha256"],
            expected_bundle_hash=bundle["bundle_hash"],
        )


def test_agents_contract_is_part_of_bundle_integrity(schema_env: BrainSchemaService) -> None:
    bundle = schema_env.initialize()
    assert bundle["agents"]["sha256"]
    agents_path = Path(bundle["agents"]["path"])
    agents_path.write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(BrainSchemaError, match="AGENTS.md does not match"):
        schema_env.bundle()
    repaired = schema_env.initialize()
    assert repaired["agents"]["sha256"] == hashlib.sha256(agents_path.read_bytes()).hexdigest()


def test_all_official_advanced_fields_round_trip_through_gbrain(schema_env: BrainSchemaService) -> None:
    bundle = schema_env.initialize()
    manifest = deepcopy(bundle["custom"]["manifest"])
    manifest["version"] = "0.2.0"
    manifest["borrow_from"] = [{"pack": "gbrain-engineer", "types": ["learning"]}]
    manifest["page_types"].append(
        {
            "name": "decision",
            "primitive": "concept",
            "path_prefixes": ["decisions/"],
            "aliases": ["concept"],
            "extractable": {
                "prompt_template": "Extract a decision and its rationale.",
                "fixture_corpus": "fixtures/extract/decision.jsonl",
                "eval_dimensions": ["rationale", "owner"],
                "benchmark_min_recall": 0.8,
            },
            "expert_routing": False,
            "subtypes": [
                {
                    "name": "architecture",
                    "when": {"frontmatter_field": "domain", "frontmatter_value": "architecture"},
                }
            ],
        }
    )
    manifest["link_types"].append({"name": "decides", "inverse": "decided_by"})
    manifest["frontmatter_links"] = [
        {"page_type": "decision", "fields": ["decision_makers"], "link_type": "decides"}
    ]
    manifest["enrichable_types"] = [{"type": "decision", "rubric": "Add current outcome evidence."}]
    manifest["filing_rules"] = [
        {
            "kind": "decision",
            "directory": "decisions/",
            "examples": ["decisions/adopt-postgresql"],
            "description": "Architecture and product decisions.",
        }
    ]
    manifest["phases"] = ["extract_atoms"]
    manifest["calibration_domains"] = [
        {"name": "decision_quality", "aggregator": "weighted_brier", "page_types": ["decision"]}
    ]
    manifest["migration_from"] = {"pack": "gbrain-base", "version": "1.x"}
    manifest["mapping_rules"] = [
        {
            "kind": "retype",
            "from_type": "legacy-decision",
            "to_type": "decision",
            "subtype": "architecture",
            "subtype_field": "subtype",
        },
        {
            "kind": "page_to_link",
            "from_type": "legacy-decision-link",
            "link_type": "decides",
            "source_slug_from": "slug",
            "target_slug_from": {"frontmatter_field": "target"},
            "preserve_notes": True,
        },
        {
            "kind": "page_to_alias",
            "from_type": "legacy-decision-alias",
            "canonical_from": "body_first_link",
            "alias_slug_from": "slug",
            "notes_from": "body_excerpt",
        },
    ]

    preview = schema_env.preview_custom(manifest)
    assert preview["custom"]["manifest"] == manifest
    assert preview["custom"]["manifest"]["page_types"][-1]["extractable"]["benchmark_min_recall"] == 0.8
    assert preview["custom"]["manifest"]["mapping_rules"][-1]["kind"] == "page_to_alias"
