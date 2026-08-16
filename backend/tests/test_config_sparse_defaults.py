from __future__ import annotations

import copy
import json

import config
import provider_registry
import pytest


def _isolate(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.setattr(provider_registry, "_default_registry_instance", None)
    return config_path


def test_canonical_defaults_match_reviewed_product_behavior(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)

    effective = config.load_config()

    assert not config_path.exists()
    assert effective["rag"] == {
        "top_k": 10,
        "similarity_threshold": 0.5,
        "hybrid": {
            "enabled": True,
            "mode": "reciprocal_rerank",
            "text_vector_weight": 0.7,
            "image_vector_weight": 0.4,
            "bm25_weight": 0.3,
            "candidate_top_k": 30,
        },
        "rerank": {
            "enabled": True,
            "provider": "dashscope",
            "model": "qwen3-vl-rerank",
            "top_n": 10,
            "candidate_top_k": 50,
        },
    }
    assert effective["compression"]["deepagents"]["summarization"]["trigger_tokens"] == 272000
    assert effective["analytics"]["database_qa"]["result_materialization_row_cap"] == 99999
    assert effective["analytics"]["database_qa"]["export_enabled"] is True
    assert effective["subagents"]["image_analyzer"]["enabled"] is True
    assert effective["harness"]["completion"]["rubric"]["model"] == "deepseek-v4-flash"
    assert effective["harness"]["completion"]["rubric"]["max_iterations"] == 3
    assert effective["harness"]["terminal"]["execution_mode"] == "spawn"
    assert effective["harness"]["terminal"]["docker"]["memory_limit_mb"] == 4096

    # Machine identity, absolute paths and plaintext secrets are never product defaults.
    assert effective["database"]["provider"] == "sqlite"
    assert effective["database"]["source"] == "local_file"
    assert effective["database"]["username"] == "puddingclaw"
    assert effective["database"]["password"] == ""
    assert effective["knowledge"]["root_dir"] == ""


def test_saving_defaults_keeps_home_config_schema_only(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)

    config.save_config(copy.deepcopy(config._DEFAULT_CONFIG))

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"schema_version": 1}


def test_single_leaf_override_and_restore_default_are_sparse(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)
    effective = config.load_config()
    effective["rag"]["top_k"] = 12

    config.save_config(effective)
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "rag": {"top_k": 12},
    }

    effective["rag"]["top_k"] = 10
    config.save_config(effective)
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"schema_version": 1}


def test_blank_rubric_model_inherits_product_default_and_is_not_rewritten(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "harness": {"completion": {"rubric": {"model": ""}}},
            }
        ),
        encoding="utf-8",
    )

    effective = config.load_config()
    assert effective["harness"]["completion"]["rubric"]["model"] == "deepseek-v4-flash"

    config.update_settings({"database": {"mode": "external", "username": "pet"}})
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "database": {"provider": "postgresql", "source": "external", "username": "pet"},
    }


def test_dynamic_default_maps_can_be_explicitly_cleared(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)
    effective = config.load_config()
    effective["subagents"] = {}
    effective["vanna"]["query"]["entity_top_k_by_type"] = {}

    config.save_config(effective)
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["subagents"] == {}
    assert persisted["vanna"]["query"]["entity_top_k_by_type"] == {}

    loaded = config.load_config()
    assert loaded["subagents"] == {}
    assert loaded["vanna"]["query"]["entity_top_k_by_type"] == {}


def test_float_rounding_does_not_create_phantom_override(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)
    effective = config.load_config()
    effective["rag"]["hybrid"]["bm25_weight"] = 1 - 0.7

    config.save_config(effective)

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"schema_version": 1}


def test_empty_password_write_keeps_existing_credential_reference(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)
    reference = "vault://users/local/credentials/database-config"
    config_path.write_text(
        json.dumps({"schema_version": 1, "database": {"password_ref": reference}}),
        encoding="utf-8",
    )

    config.update_settings({"database": {"password": ""}})

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["database"]["password_ref"] == reference
    assert "password" not in persisted["database"]


def test_environment_derived_knowledge_values_are_not_baked_into_home(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", str(tmp_path / "temporary-knowledge"))
    monkeypatch.setenv("PUDDINGCLAW_MILVUS_URI", "http://temporary-milvus:19530")

    config.update_settings(
        {
            "knowledge": {
                "root_dir": str(tmp_path / "temporary-knowledge"),
                "multimodal_index": {"milvus_uri": "http://temporary-milvus:19530"},
            }
        }
    )

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"schema_version": 1}


def test_direct_home_gbrain_overrides_are_ignored(tmp_path, monkeypatch):
    config_path = _isolate(tmp_path, monkeypatch)
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mcp": {
                    "enabled": ["gbrain"],
                    "auto_enable_gbrain": False,
                    "servers": {
                        "gbrain": {"transport": "stdio", "command": "other-gbrain"}
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    effective = config.load_config()
    assert effective["mcp"] == config._DEFAULT_CONFIG["mcp"]

    config.save_config(effective)
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"schema_version": 1}


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"schema_version": 2}, "schema_version"),
        ({"schema_version": 1, "unknown": True}, "Unknown settings"),
        ({"schema_version": 1, "thinking_mode": True}, "Retired settings"),
        (
            {"schema_version": 1, "compression": {"ratio": 0.5}},
            "compression.ratio",
        ),
        (
            {
                "schema_version": 1,
                "knowledge": {"multimodal_index": {"overwrite": False}},
            },
            "knowledge.multimodal_index.overwrite",
        ),
    ],
)
def test_removed_or_unknown_schema_fails_fast(tmp_path, monkeypatch, payload, message):
    config_path = _isolate(tmp_path, monkeypatch)
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        config.load_config()
