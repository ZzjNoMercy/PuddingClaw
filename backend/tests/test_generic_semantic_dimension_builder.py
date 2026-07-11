"""E2E regression for HITL rule -> temporary inputs -> Crosswalk -> publication."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
from argparse import Namespace
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from graph.attachment_store import attachment_store
from graph.dimension_build_resume import DimensionBuildResumeRegistry
from knowledge.models import Base
from knowledge.semantic_dimension_jobs import (
    claim_next_semantic_dimension_build_job,
    create_semantic_dimension_build_job,
    mark_semantic_dimension_build_waiting_publish,
)
from knowledge.semantic_dimension_publisher import publish_semantic_dimension_build
from knowledge.semantic_dimension_rule_contract import SemanticDimensionRuleError, build_rule_from_decision, validate_build_rule


def _load_generic_adapter():
    path = Path(__file__).parents[1] / "skills" / "build-semantic-dimension" / "scripts" / "entity_crosswalk_v1.py"
    spec = importlib.util.spec_from_file_location("generic_entity_crosswalk_test_adapter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spreadsheet_bytes(frame: pd.DataFrame) -> io.BytesIO:
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def test_generic_dimension_builder_uses_hitl_selected_canonical_attachment_and_publishes(tmp_path: Path) -> None:
    async def run() -> None:
        attachment_store.initialize(tmp_path / "backend")
        session_id = "generic-builder-e2e"
        canonical_attachment = attachment_store.save(
            session_id=session_id,
            filename="规范车系.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            source="upload",
            stream=_spreadsheet_bytes(pd.DataFrame({"品牌": ["比亚迪", "丰田"], "车系": ["秦PLUS", "凯美瑞"]})),
        )
        source_attachment = attachment_store.save(
            session_id=session_id,
            filename="2023年12月上险量.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            source="upload",
            stream=_spreadsheet_bytes(pd.DataFrame({"品牌名称": ["比亚迪", "丰田", "未知"], "子车型": ["秦 PLUS", "凯美瑞", "不存在"]})),
        )
        request = {
            "dimension_id": "vehicle_series",
            "rule_template": {"adapter": "entity_crosswalk_v1", "reference_path": "references/active_crosswalk.json"},
            "candidates": [
                {
                    "id": "config",
                    "display_name": "规范车系.xlsx",
                    "input": {"kind": "attachment", "attachment_id": canonical_attachment["id"]},
                    "fields": ["品牌", "车系"],
                    "suggested_key_fields": ["品牌", "车系"],
                    "suggested_output_fields": ["canonical_brand", "canonical_series"],
                },
                {
                    "id": "insurance_december",
                    "display_name": "2023年12月上险量.xlsx",
                    "input": {"kind": "attachment", "attachment_id": source_attachment["id"]},
                    "fields": ["品牌名称", "子车型"],
                    "suggested_key_fields": ["品牌名称", "子车型"],
                    "suggested_output_fields": ["source_brand", "source_series"],
                },
            ],
        }
        decision = {
            "action": "confirm",
            "canonical_candidate_id": "config",
            "bindings": [
                {"candidate_id": "config", "key_fields": ["品牌", "车系"], "output_fields": ["canonical_brand", "canonical_series"]},
                {"candidate_id": "insurance_december", "key_fields": ["品牌名称", "子车型"], "output_fields": ["source_brand", "source_series"]},
            ],
        }
        rule = build_rule_from_decision(request, decision)
        assert rule["canonical_strategy"]["binding_id"] == "config"

        adapter = _load_generic_adapter()
        staging = tmp_path / "staging"
        rule_path = staging / "build-rule.json"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text(json.dumps(rule, ensure_ascii=False), encoding="utf-8")
        result = await adapter.run(
            Namespace(
                dimension_id="vehicle_series",
                session_id=session_id,
                rule_json=str(rule_path),
                output_dir=str(staging / "artifacts"),
                semantic_reference_path=str(staging / "references" / "active_crosswalk.json"),
            )
        )
        crosswalk = json.loads(Path(result["crosswalk"]).read_text(encoding="utf-8"))
        assert crosswalk["formatter"] == "entity-resolution-crosswalk"
        assert len(crosswalk["records"]) == 2
        assert crosswalk["records"][0]["entity"]["canonical_brand"] == "丰田"
        assert result["summary"]["canonical_with_source_binding"] == 2
        assert result["summary"]["source_diagnostics"] == 1
        stored_attachment = attachment_store.get(session_id, source_attachment["id"])
        assert stored_attachment is not None
        assert not Path(str(stored_attachment["path"])).is_relative_to(tmp_path / "backend" / "knowledge")

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        backend_dir = tmp_path / "backend-publish"
        dimension_dir = backend_dir / "semantic-assets" / "dimensions" / "vehicle_series"
        dimension_dir.mkdir(parents=True)
        (dimension_dir / "dimension.md").write_text(
            "---\nid: vehicle_series\nresolution:\n  reference_path: references/active_crosswalk.json\nbuild_skill:\n  adapter: entity_crosswalk_v1\nupdated_at: 2026-07-11 00:00:00\n---\n",
            encoding="utf-8",
        )
        async with session_factory() as session:
            job, _ = await create_semantic_dimension_build_job(
                session,
                dimension_id="vehicle_series",
                adapter="entity_crosswalk_v1",
                input_snapshot={"build_rule": rule},
                session_id=session_id,
            )
            claimed = await claim_next_semantic_dimension_build_job(session)
            assert claimed and claimed.id == job.id
            await mark_semantic_dimension_build_waiting_publish(
                session,
                claimed,
                staging_path=str(staging),
                published_reference_path="references/active_crosswalk.json",
                result_summary={"artifact_paths": {"crosswalk": result["crosswalk"]}},
            )
            published = await publish_semantic_dimension_build(session, base_dir=backend_dir, job_id=job.id)
            assert published["already_published"] is False
        active = dimension_dir / "references" / "active_crosswalk.json"
        assert active.is_file()
        assert json.loads(active.read_text(encoding="utf-8"))["build_rule"]["canonical_strategy"]["binding_id"] == "config"
        await engine.dispose()

    asyncio.run(run())


def test_append_source_locks_existing_crosswalk_and_cannot_change_canonical_entities(tmp_path: Path) -> None:
    async def run() -> None:
        backend_dir = tmp_path / "backend"
        attachment_store.initialize(backend_dir)
        session_id = "append-source-e2e"
        dimension_dir = backend_dir / "semantic-assets" / "dimensions" / "vehicle_series"
        references = dimension_dir / "references"
        references.mkdir(parents=True)
        active = {
            "formatter": "entity-resolution-crosswalk",
            "entity_type": "vehicle_series",
            "version": "v0.1.3",
            "canonical_key": {"fields": ["canonical_brand", "canonical_series"]},
            "records": [
                {"entity": {"entity_key": "比亚迪::秦plus", "canonical_brand": "比亚迪", "canonical_series": "秦PLUS"}, "bindings": [], "resolution": {"status": "canonical_only", "join_eligible": False}},
                {"entity": {"entity_key": "丰田::凯美瑞", "canonical_brand": "丰田", "canonical_series": "凯美瑞"}, "bindings": [], "resolution": {"status": "canonical_only", "join_eligible": False}},
            ],
        }
        (references / "active_crosswalk.json").write_text(json.dumps(active, ensure_ascii=False), encoding="utf-8")
        source_attachment = attachment_store.save(
            session_id=session_id,
            filename="2023年12月上险量.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            source="upload",
            stream=_spreadsheet_bytes(pd.DataFrame({"品牌": ["比亚迪", "丰田", "新品牌"], "1-子车型": ["秦 PLUS", "凯美瑞", "新车系"]})),
        )
        request = {
            "dimension_id": "vehicle_series",
            "operation": "append_source",
            "locked_canonical_candidate_id": "__active_canonical__",
            "rule_template": {"adapter": "entity_crosswalk_v1", "reference_path": "references/active_crosswalk.json"},
            "candidates": [
                {
                    "id": "__active_canonical__",
                    "display_name": "当前规范基准",
                    "input": {"kind": "active_crosswalk", "dimension_id": "vehicle_series"},
                    "fields": ["canonical_brand", "canonical_series"],
                    "suggested_key_fields": ["canonical_brand", "canonical_series"],
                    "suggested_output_fields": ["canonical_brand", "canonical_series"],
                },
                {
                    "id": "insurance_december",
                    "display_name": "2023年12月上险量.xlsx",
                    "input": {"kind": "attachment", "attachment_id": source_attachment["id"]},
                    "fields": ["品牌", "1-子车型"],
                    "suggested_key_fields": ["品牌", "1-子车型"],
                    "suggested_source_id": "insurance_sales",
                    "suggested_source_name": "乘用车上险量",
                },
            ],
            "registered_sources": [{"id": "insurance_sales", "name": "乘用车上险量", "identity_fields": ["品牌", "1-子车型"]}],
        }
        decision = {
            "action": "confirm",
            "canonical_candidate_id": "__active_canonical__",
            "bindings": [
                {"candidate_id": "__active_canonical__", "key_fields": ["canonical_brand", "canonical_series"], "output_fields": ["canonical_brand", "canonical_series"]},
                {"candidate_id": "insurance_december", "key_fields": ["品牌", "1-子车型"], "output_fields": ["source_brand", "source_series"], "source_id": "insurance_sales", "source_mode": "append"},
            ],
        }
        rule = build_rule_from_decision(request, decision)
        assert rule["canonical_strategy"]["type"] == "active_crosswalk"
        assert validate_build_rule(rule)["canonical_strategy"]["binding_id"] == "__active_canonical__"
        wrong = {**decision, "canonical_candidate_id": "insurance_december"}
        with pytest.raises(SemanticDimensionRuleError, match="retain the current canonical baseline"):
            build_rule_from_decision(request, wrong)

        adapter = _load_generic_adapter()
        adapter.BASE_DIR = backend_dir
        staging = tmp_path / "staging"
        rule_path = staging / "build-rule.json"
        rule_path.parent.mkdir(parents=True)
        rule_path.write_text(json.dumps(rule, ensure_ascii=False), encoding="utf-8")
        result = await adapter.run(Namespace(
            dimension_id="vehicle_series", session_id=session_id, rule_json=str(rule_path),
            output_dir=str(staging / "artifacts"), semantic_reference_path=str(staging / "references" / "active_crosswalk.json"),
        ))
        crosswalk = json.loads(Path(result["crosswalk"]).read_text(encoding="utf-8"))
        assert {record["entity"]["entity_key"] for record in crosswalk["records"]} == {"比亚迪::秦plus", "丰田::凯美瑞"}
        assert result["summary"]["canonical_entities"] == 2
        assert result["summary"]["canonical_with_source_binding"] == 2
        assert result["summary"]["source_diagnostics"] == 1
        assert crosswalk["source_diagnostics"][0]["bindings"][0]["key_fields"]["品牌"] == "新品牌"

    asyncio.run(run())


def test_dimension_build_resume_registry_returns_validated_rule() -> None:
    async def run() -> None:
        registry = DimensionBuildResumeRegistry()
        request = registry.create(
            session_id="session_test",
            query_id="query_test",
            tool_call_id="tool_test",
            payload={
                "dimension_id": "vehicle_series",
                "candidates": [
                    {"id": "a", "display_name": "A", "input": {"kind": "attachment", "attachment_id": "att_a"}, "fields": ["品牌"]},
                    {"id": "b", "display_name": "B", "input": {"kind": "attachment", "attachment_id": "att_b"}, "fields": ["品牌"]},
                ],
                "rule_template": {"adapter": "entity_crosswalk_v1", "reference_path": "references/active_crosswalk.json"},
            },
        )
        waiting = asyncio.create_task(registry.wait(request["id"]))
        decision = registry.resolve(request["id"], {
            "action": "confirm",
            "canonical_candidate_id": "b",
            "bindings": [
                {"candidate_id": "a", "key_fields": ["品牌"], "output_fields": ["source_brand"]},
                {"candidate_id": "b", "key_fields": ["品牌"], "output_fields": ["canonical_brand"]},
            ],
        })
        assert decision and decision["build_rule"]["canonical_strategy"]["binding_id"] == "b"
        assert (await waiting)["action"] == "confirm"

    asyncio.run(run())


def test_registered_source_must_be_appended() -> None:
    request = {
        "dimension_id": "vehicle_series",
        "registered_sources": [{"id": "insurance_sales", "name": "乘用车上险量"}],
        "rule_template": {"adapter": "entity_crosswalk_v1", "reference_path": "references/active_crosswalk.json"},
        "candidates": [
            {"id": "canonical", "display_name": "配置", "input": {"kind": "database_table", "table": "vehicle_params_wide"}, "fields": ["brand"]},
            {"id": "monthly", "display_name": "11月上险量", "input": {"kind": "attachment", "attachment_id": "att_monthly"}, "fields": ["品牌"], "suggested_source_id": "insurance_sales"},
        ],
    }
    decision = {
        "action": "confirm",
        "canonical_candidate_id": "canonical",
        "bindings": [
            {"candidate_id": "canonical", "key_fields": ["brand"], "output_fields": ["brand"]},
            {"candidate_id": "monthly", "key_fields": ["品牌"], "output_fields": ["品牌"], "source_id": "insurance_sales", "source_mode": "new"},
        ],
    }
    with pytest.raises(ValueError, match="must use append mode"):
        build_rule_from_decision(request, decision)


def test_worker_revalidation_preserves_confirmed_registered_append_source() -> None:
    request = {
        "dimension_id": "vehicle_series",
        "registered_sources": [{"id": "insurance_sales", "name": "乘用车上险量", "identity_fields": ["品牌", "1-子车型"]}],
        "rule_template": {"adapter": "entity_crosswalk_v1", "reference_path": "references/active_crosswalk.json"},
        "candidates": [
            {"id": "canonical", "display_name": "配置", "input": {"kind": "database_table", "table": "vehicle_params_wide"}, "fields": ["brand", "serial_name"]},
            {"id": "monthly", "display_name": "11月上险量", "input": {"kind": "attachment", "attachment_id": "att_monthly"}, "fields": ["品牌", "1-子车型"]},
        ],
    }
    rule = build_rule_from_decision(request, {
        "action": "confirm",
        "canonical_candidate_id": "canonical",
        "bindings": [
            {"candidate_id": "canonical", "key_fields": ["brand", "serial_name"], "output_fields": ["canonical_brand", "canonical_series"]},
            {"candidate_id": "monthly", "key_fields": ["品牌", "1-子车型"], "output_fields": ["source_brand", "source_series"], "source_id": "insurance_sales", "source_name": "乘用车上险量", "source_mode": "append"},
        ],
    })

    assert rule["registered_sources_snapshot"][0]["id"] == "insurance_sales"
    revalidated = validate_build_rule(rule)
    assert next(item for item in revalidated["bindings"] if item["role"] == "source")["source_mode"] == "append"


def test_resolve_binding_preserves_append_source_selection() -> None:
    """The HTTP DTO must not discard the HITL card's source-routing fields."""
    from api.dimension_build_rules import DimensionBindingDecision

    binding = DimensionBindingDecision.model_validate(
        {
            "candidate_id": "monthly",
            "key_fields": ["品牌", "1-子车型"],
            "output_fields": ["source_brand", "source_series"],
            "source_id": "insurance_sales",
            "source_name": "乘用车上险量",
            "source_mode": "append",
        }
    )

    assert binding.model_dump()["source_id"] == "insurance_sales"
    assert binding.model_dump()["source_name"] == "乘用车上险量"
    assert binding.model_dump()["source_mode"] == "append"


def test_append_merge_preserves_prior_diagnostics_and_rejects_baseline_shrink() -> None:
    adapter = _load_generic_adapter()
    build_rule = {"merge": {"mode": "append_source_bindings"}}
    crosswalk = {
        "build_rule": build_rule,
        "records": [{"entity": {"entity_key": "brand::a"}, "bindings": [{"source_kind": "database_table"}, {"source_ref": "attachment:new", "key_fields": {"品牌": "A"}}]}],
        "source_diagnostics": [],
    }
    prior = {
        "records": [{"entity": {"entity_key": "brand::a"}, "bindings": [{"source_kind": "database_table"}, {"source_ref": "attachment:old", "key_fields": {"品牌": "A"}}]}],
        "source_diagnostics": [{"bindings": [{"source_ref": "attachment:old", "key_fields": {"品牌": "旧未匹配"}}], "resolution": {"status": "unmatched"}}],
    }
    merged = adapter.merge_prior_source_bindings(crosswalk, prior)
    assert len(merged["records"][0]["bindings"]) == 3
    assert merged["source_diagnostics"][0]["bindings"][0]["key_fields"]["品牌"] == "旧未匹配"

    with pytest.raises(RuntimeError, match="规范实体基准发生缩减"):
        adapter.assert_incremental_canonical_baseline(crosswalk, {"records": [{"entity": {"entity_key": "brand::a"}}, {"entity": {"entity_key": "brand::missing"}}]})


def test_append_treats_legacy_entity_key_migration_as_same_canonical_entity() -> None:
    adapter = _load_generic_adapter()
    build_rule = {"merge": {"mode": "append_source_bindings"}}
    prior = {
        "records": [{
            "entity": {"entity_key": "丰田::rav4荣放双擎e", "canonical_brand": "丰田", "canonical_series": "RAV4荣放 双擎E+"},
            "bindings": [
                {"source_kind": "database_table", "source_ref": "database:config", "key_fields": {"brand": "丰田"}},
                {"source_kind": "table_asset", "source_ref": "table_asset:old", "key_fields": {"品牌": "丰田", "1-子车型": "RAV4荣放 双擎E+"}},
            ],
        }],
        "source_diagnostics": [],
    }
    current = {
        "build_rule": build_rule,
        "records": [{
            "entity": {"entity_key": "丰田::rav4荣放双擎e+", "brand": "丰田", "serial_name": "RAV4荣放 双擎E+"},
            "bindings": [{"source_kind": "database_table", "source_ref": "database:config", "key_fields": {"brand": "丰田"}}],
        }],
        "source_diagnostics": [],
    }

    adapter.assert_incremental_canonical_baseline(current, prior)
    merged = adapter.merge_prior_source_bindings(current, prior)
    assert any(binding.get("source_ref") == "table_asset:old" for binding in merged["records"][0]["bindings"])


def test_canonical_key_preserves_meaningful_plus_suffix() -> None:
    adapter = _load_generic_adapter()
    rule = {
        "dimension_id": "vehicle_series",
        "bindings": [
            {"id": "canonical", "role": "canonical", "key_fields": ["brand", "series"], "output_fields": ["canonical_brand", "canonical_series"]},
            {"id": "source", "role": "source", "key_fields": ["brand", "series"], "output_fields": ["brand", "series"]},
        ],
    }
    crosswalk, _ = adapter.build_crosswalk(rule, [
        ({"source_kind": "database_table", "source_ref": "database:test", "source_name": "base"}, pd.DataFrame({"brand": ["小鹏", "小鹏"], "series": ["小鹏P7", "小鹏P7+"]})),
        ({"source_kind": "attachment", "source_ref": "attachment:test", "source_name": "source"}, pd.DataFrame({"brand": [], "series": []})),
    ])
    assert {record["entity"]["entity_key"] for record in crosswalk["records"]} == {"小鹏::小鹏p7", "小鹏::小鹏p7+"}
