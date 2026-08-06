"""AI Gateway 与 Provider 设置边界测试。"""

import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage

import config
import higress_config_reader
import provider_registry
from app import app
from graph.attachment_store import attachment_store
from graph.deepagents_manager import (
    AttachmentImageContentMiddleware,
    DeepAgentsAgentManager,
    _build_subagent_item,
)


@pytest.fixture(autouse=True)
def _isolated_provider_registry(tmp_path, monkeypatch):
    """Legacy config fixtures must not share a migrated user profile."""
    monkeypatch.setenv("PUDDINGDATA_USER_DATA_DIR", str(tmp_path / "user-data"))
    monkeypatch.setattr(provider_registry, "_default_registry_instance", None)


def _stored_image_attachment(tmp_path, session_id: str = "session-attachments"):
    attachment_store.initialize(tmp_path)
    return attachment_store.save(
        session_id=session_id,
        filename="diagram.png",
        mime_type="image/png",
        source="upload",
        stream=BytesIO(b"\x89PNG\r\n\x1a\n"),
    )


def test_attachment_lookup_is_strictly_session_scoped(tmp_path):
    attachment = _stored_image_attachment(tmp_path, session_id="session-owner")

    assert attachment_store.get("session-owner", attachment["id"]) is not None
    assert attachment_store.get("session-other", attachment["id"]) is None


def test_harness_settings_freeze_explicit_goal_and_validate_rules(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    config.update_settings(
        {
            "harness": {
                "goals": {
                    "enabled": True,
                    "activation": "automatic",
                    "default_enabled": True,
                    "auto_promote_from_run": True,
                    "max_rounds": 5,
                },
                "completion": {
                    "rubric": {
                        "enabled": True,
                        "model": "  grader-model  ",
                        "max_iterations": 3,
                        "max_stagnant_repairs": 4,
                        "custom_rules": [
                            {
                                "id": "quantified",
                                "statement": "原因必须给出影响量级",
                                "required": True,
                                "verifier": "analytics",
                            }
                        ],
                    },
                },
            },
        }
    )

    saved = json.loads(config_path.read_text(encoding="utf-8"))["harness"]
    assert saved["goals"]["activation"] == "explicit_user_only"
    assert saved["goals"]["default_enabled"] is False
    assert saved["goals"]["auto_promote_from_run"] is False
    assert saved["completion"]["rubric"]["custom_rules"][0]["statement"] == "原因必须给出影响量级"
    assert saved["completion"]["rubric"]["model"] == "grader-model"
    assert saved["completion"]["rubric"]["max_stagnant_repairs"] == 4

    with pytest.raises(ValueError, match="max_stagnant_repairs"):
        config.update_settings(
            {
                "harness": {
                    "completion": {
                        "rubric": {
                            "max_stagnant_repairs": 0,
                        },
                    },
                },
            }
        )

    with pytest.raises(ValueError, match="verifier is not registered"):
        config.update_settings(
            {
                "harness": {
                    "completion": {
                        "rubric": {
                            "custom_rules": [
                                {
                                    "statement": "run shell",
                                    "verifier": "shell",
                                }
                            ],
                        },
                    },
                },
            }
        )

    with pytest.raises(ValueError, match="verifier is not registered"):
        config.update_settings(
            {
                "harness": {
                    "completion": {
                        "rubric": {
                            "custom_rules": [
                                {
                                    "statement": "自然语言不能冒充代码验证器",
                                    "verifier": "deterministic",
                                }
                            ],
                        },
                    },
                },
            }
        )


def test_legacy_python_only_sandbox_image_migrates_to_managed_runtime(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "harness": {
                    "terminal": {
                        "docker": {
                            "image": "python:3.12-slim",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    loaded = config.load_config()

    docker = loaded["harness"]["terminal"]["docker"]
    assert docker["image"] == "puddingclaw/sandbox:python3.12-node22-chromium-v4"
    assert docker["dependency_setup_enabled"] is False
    assert docker["dependency_setup_opt_in_version"] == 1
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["harness"]["terminal"]["docker"]["image"] == "puddingclaw/sandbox:python3.12-node22-chromium-v4"


def test_legacy_implicit_project_dependency_setup_is_reset_to_clean_default(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "harness": {
                    "terminal": {
                        "docker": {
                            "image": "puddingclaw/sandbox:python3.12-node22-v1",
                            "dependency_setup_enabled": True,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    loaded = config.load_config()

    docker = loaded["harness"]["terminal"]["docker"]
    assert docker["image"] == "puddingclaw/sandbox:python3.12-node22-chromium-v4"
    assert docker["dependency_setup_enabled"] is False
    assert docker["dependency_setup_opt_in_version"] == 1


@pytest.mark.parametrize(
    "legacy_image",
    [
        "puddingclaw/sandbox:python3.12-node22-v2",
        "puddingclaw/sandbox:python3.12-node22-curl-v3",
    ],
)
def test_managed_sandbox_migrates_to_browser_runtime(
    tmp_path,
    monkeypatch,
    legacy_image,
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "harness": {
                    "terminal": {
                        "docker": {
                            "image": legacy_image,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    loaded = config.load_config()

    assert (
        loaded["harness"]["terminal"]["docker"]["image"]
        == "puddingclaw/sandbox:python3.12-node22-chromium-v4"
    )


def test_docker_probe_endpoint_reports_daemon_status(monkeypatch):
    from harness.workspace_backends import ProjectSandboxManager

    monkeypatch.setattr(
        ProjectSandboxManager,
        "probe",
        lambda self: (True, "27.0.1"),
    )
    response = TestClient(app).post(
        "/api/settings/harness/docker/probe",
        json={"connection": "", "context": "desktop-linux"},
    )

    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["detail"] == "27.0.1"


def test_gateway_has_no_key_and_provider_key_is_masked(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "ai_gateway": {
                    "base_url": "http://gateway:8080/v1",
                    "health_path": "/ready",
                    "fallback_to_direct": True,
                },
                "fallback_llm": {"api_key": "provider-secret-1234"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    displayed = config.get_settings_for_display()
    assert "api_key" not in displayed["ai_gateway"]
    assert "api_key_masked" not in displayed["ai_gateway"]
    assert displayed["fallback_llm"]["api_key_masked"].endswith("1234")

    config.update_settings({"ai_gateway": {"base_url": "http://new-gateway:8080/v1"}})
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["ai_gateway"]["base_url"] == "http://new-gateway:8080/v1"
    assert "enabled" not in saved["ai_gateway"]


def test_provider_connection_check_is_separate_from_model_discovery(monkeypatch):
    called: dict[str, object] = {}

    def fake_test_endpoint(self, provider_id, endpoint_id, *, base_url="", api_key=""):
        called.update(
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            base_url=base_url,
            api_key=api_key,
        )
        return {"reachable": True, "status_code": 204}

    monkeypatch.setattr(provider_registry.ProviderRegistry, "test_endpoint", fake_test_endpoint)

    response = TestClient(app).post(
        "/api/providers/deepseek/endpoints/deepseek-openai/test-connection",
        json={"base_url": "https://example.test/v1", "api_key": "unsaved-test-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["reachable"] is True
    assert body["status_code"] == 204
    assert called == {
        "provider_id": "deepseek",
        "endpoint_id": "deepseek-openai",
        "base_url": "https://example.test/v1",
        "api_key": "unsaved-test-key",
    }


def test_multimodal_embedding_settings_are_separate_from_openai_embedding(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "fallback_embedding": {
                    "model": "text-embedding-v4",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "api_key": "text-secret-1234",
                },
                "multimodal_embedding": {
                    "provider": "dashscope",
                    "model": "qwen2.5-vl-embedding",
                    "dimension": 1024,
                    "base_url": "http://localhost:8080",
                    "route_path": "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
                    "api_key": "mm-secret-5678",
                    "prefer_gateway": True,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    mm = config.get_multimodal_embedding_config()
    assert mm["model"] == "qwen2.5-vl-embedding"
    assert mm["dimension"] == 1024
    # Legacy Higress passthrough is retained as an archived config value, but
    # the active multimodal binding resolves to DashScope's native endpoint.
    assert mm["base_url"] == "https://dashscope.aliyuncs.com"
    assert mm["route_path"].endswith("/multimodal-embedding")

    displayed = config.get_settings_for_display()
    # Bailian endpoints share the OpenAI-compatible credential selected during
    # the lossless Provider migration.
    assert displayed["multimodal_embedding"]["api_key_masked"].endswith("1234")
    assert displayed["multimodal_embedding"]["openai_compatible"] is False
    assert "api_key" not in displayed["multimodal_embedding"]

    config.update_settings(
        {
            "multimodal_embedding": {
                "base_url": "http://higress:8080",
                "model": "qwen3-vl-embedding",
                "dimension": 2560,
                "batch_size": 6,
            }
        }
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["multimodal_embedding"]["base_url"] == "http://higress:8080"
    assert saved["multimodal_embedding"]["model"] == "qwen3-vl-embedding"
    assert saved["multimodal_embedding"]["dimension"] == 2560
    # Provider Registry owns the selected model and its dimensions, while the
    # knowledge-index runtime keeps a separately tunable request concurrency.
    assert config.get_multimodal_embedding_config()["batch_size"] == 6


def test_multimodal_embedding_can_reuse_higress_qwen_token(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "multimodal_embedding": {
                    "provider": "dashscope",
                    "model": "qwen2.5-vl-embedding",
                    "dimension": 1024,
                    "api_key": "",
                }
            }
        ),
        encoding="utf-8",
    )
    higress_dir = tmp_path / "higress"
    plugin_dir = higress_dir / "wasmplugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "ai-proxy.internal.yaml").write_text(
        """
apiVersion: extensions.higress.io/v1alpha1
kind: WasmPlugin
metadata:
  name: ai-proxy.internal
spec:
  defaultConfig:
    providers:
      - id: base-model
        type: deepseek
        apiTokens: deepseek-secret
      - id: multi-model
        type: qwen
        protocol: openai
        qwenEnableCompatible: true
        apiTokens:
          - dashscope-secret-9999
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.setattr(higress_config_reader, "DEFAULT_HIGRESS_DATA_DIR", higress_dir)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)

    mm = config.get_multimodal_embedding_config()
    assert mm["api_key"] == "dashscope-secret-9999"

    displayed = config.get_settings_for_display()
    assert displayed["multimodal_embedding"]["api_key_masked"].endswith("9999")
    assert "api_key" not in displayed["multimodal_embedding"]


def test_knowledge_multimodal_index_settings_live_in_config_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "knowledge": {
                    "multimodal_index": {
                        "enabled": True,
                        "vector_store": "milvus",
                        "milvus_uri": "http://milvus.local:19530",
                        "text_collection": "kb_text",
                        "image_collection": "kb_image",
                        # Legacy value should be ignored. Collection reset is now an
                        # explicit user action, not an ingestion-time flag.
                        "overwrite": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    index = config.get_knowledge_multimodal_index_config()
    assert index["enabled"] is True
    assert index["vector_store"] == "milvus"
    assert index["milvus_uri"] == "http://milvus.local:19530"
    assert index["text_collection"] == "kb_text"
    assert index["image_collection"] == "kb_image"
    assert index["overwrite"] is False

    displayed = config.get_settings_for_display()
    assert displayed["knowledge"]["multimodal_index"]["text_collection"] == "kb_text"

    config.update_settings(
        {
            "knowledge": {
                "multimodal_index": {
                    "enabled": False,
                    "text_collection": "pudding_text",
                    "image_collection": "pudding_image",
                }
            }
        }
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["knowledge"]["multimodal_index"]["enabled"] is False
    assert saved["knowledge"]["multimodal_index"]["text_collection"] == "pudding_text"
    assert saved["knowledge"]["multimodal_index"]["image_collection"] == "pudding_image"
    assert saved["knowledge"]["multimodal_index"]["milvus_uri"] == "http://milvus.local:19530"
    assert saved["knowledge"]["multimodal_index"]["overwrite"] is False


def test_knowledge_root_dir_settings_live_in_config_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    root_dir = tmp_path / "user-kb"
    config_path.write_text(
        json.dumps(
            {
                "knowledge": {
                    "root_dir": str(root_dir),
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.delenv("PUDDINGCLAW_KNOWLEDGE_DIR", raising=False)

    displayed = config.get_settings_for_display()
    assert displayed["knowledge"]["root_dir"] == str(root_dir)
    assert displayed["knowledge"]["configured_by"] == "config.json"
    assert displayed["knowledge"]["environment_override"] is False

    config.update_settings({"knowledge": {"root_dir": str(tmp_path / "next-kb")}})
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["knowledge"]["root_dir"] == str(tmp_path / "next-kb")


def test_llm_wiki_compiler_model_setting_uses_provider_registry(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    class FakeRegistry:
        def ensure_migrated(self, legacy_config):
            return None

        def resolve_model(self, model_id, *, legacy_config, expected_capability="llm"):
            models = {
                "provider:endpoint:wiki-model": ("wiki-model", "llm", 0),
                "provider:endpoint:wiki-embedding": ("wiki-embedding", "text_embedding", 1024),
                "provider:endpoint:wiki-think": ("wiki-think", "llm", 0),
            }
            assert model_id in models
            name, capability, dimension = models[model_id]
            assert capability == expected_capability
            return {
                "id": model_id,
                "name": name,
                "provider_id": "provider",
                "capability": capability,
                "dimension": dimension,
                "base_url": "https://example.test/v1",
                "api_key": "secret",
                "protocol": "openai_compatible",
            }

        def resolve_binding(self, binding, *, legacy_config):
            assert binding == "agent"
            return {
                "id": "provider:endpoint:agent-model",
                "name": "agent-model",
                "provider_id": "provider",
                "capability": "llm",
            }

    fake_registry = FakeRegistry()
    monkeypatch.setattr(provider_registry, "get_provider_registry", lambda: fake_registry)

    config.update_settings(
        {
            "knowledge": {
                "llm_wiki": {
                    "compiler_agent": {"model_id": "provider:endpoint:wiki-model"},
                    "gbrain": {
                        "embedding_model_id": "provider:endpoint:wiki-embedding",
                        "think_model_id": "provider:endpoint:wiki-think",
                    },
                }
            }
        }
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["knowledge"]["llm_wiki"]["compiler_agent"]["model_id"] == "provider:endpoint:wiki-model"
    assert saved["knowledge"]["llm_wiki"]["gbrain"] == {
        "embedding_model_id": "provider:endpoint:wiki-embedding",
        "think_model_id": "provider:endpoint:wiki-think",
    }
    assert config.get_llm_wiki_compiler_agent_config() == {
        "model_id": "provider:endpoint:wiki-model",
        "configured_model_id": "provider:endpoint:wiki-model",
        "model": "wiki-model",
        "provider": "provider",
        "uses_agent_default": False,
    }
    gbrain = config.get_llm_wiki_gbrain_config()
    assert gbrain["embedding"]["name"] == "wiki-embedding"
    assert gbrain["embedding"]["dimension"] == 1024
    assert gbrain["think"]["name"] == "wiki-think"


def test_database_settings_live_in_config_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "database": {
                    "mode": "external",
                    "url": "postgresql+asyncpg://alice:secret@127.0.0.1:15432/pudding",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.delenv("PUDDINGCLAW_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)

    database = config.get_database_config()
    assert database["mode"] == "external"
    assert database["url"] == "postgresql+asyncpg://alice:secret@127.0.0.1:15432/pudding"
    assert database["configured_by"] == "config.json"
    assert database["environment_override"] is False

    displayed = config.get_settings_for_display()
    assert displayed["database"]["mode"] == "external"
    assert displayed["database"]["url"].endswith(":15432/pudding")

    config.update_settings(
        {
            "database": {
                "mode": "bundled",
                "url": "postgresql+asyncpg://puddingclaw:puddingclaw@127.0.0.1:5432/puddingclaw",
            }
        }
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["database"]["mode"] == "bundled"
    assert saved["database"]["url"].startswith("postgresql+asyncpg://puddingclaw:")


def test_database_settings_can_build_url_from_local_fields(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "database": {
                    "mode": "bundled",
                    "host": "127.0.0.1",
                    "port": 5433,
                    "database": "puddingclaw",
                    "username": "puddingclaw",
                    "password": "puddingclaw",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.delenv("PUDDINGCLAW_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)

    database = config.get_database_config()
    assert database["mode"] == "bundled"
    assert database["port"] == 5433
    assert database["url"] == "postgresql+asyncpg://puddingclaw:puddingclaw@127.0.0.1:5433/puddingclaw"

    config.update_settings(
        {
            "database": {
                "mode": "external",
                "port": 15432,
                "database": "mydb",
                "username": "me",
                "password": "secret",
            }
        }
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["database"]["mode"] == "external"
    assert saved["database"]["port"] == 15432
    assert saved["database"]["database"] == "mydb"
    assert saved["database"]["username"] == "me"


def test_database_generic_env_does_not_override_config_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "database": {
                    "mode": "external",
                    "url": "postgresql+asyncpg://alice:secret@127.0.0.1:15432/pudding",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://env:secret@127.0.0.1:5432/envdb")

    database = config.get_database_config()
    assert database["url"] == "postgresql+asyncpg://alice:secret@127.0.0.1:15432/pudding"
    assert database["configured_url"].endswith(":15432/pudding")
    assert database["configured_by"] == "config.json"
    assert database["environment_override"] is False


def test_database_puddingclaw_env_can_override_config_json(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "database": {
                    "mode": "external",
                    "url": "postgresql+asyncpg://alice:secret@127.0.0.1:15432/pudding",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    monkeypatch.setenv("PUDDINGCLAW_DATABASE_URL", "postgresql+asyncpg://env:secret@127.0.0.1:5432/envdb")

    database = config.get_database_config()
    assert database["url"] == "postgresql+asyncpg://env:secret@127.0.0.1:5432/envdb"
    assert database["configured_url"].endswith(":15432/pudding")
    assert database["configured_by"] == "environment"
    assert database["environment_override"] is True


def test_thinking_mode_switches_to_thinking_model(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "thinking_mode": False,
                "gateway_llm": {
                    "model": "deepseek-v4-flash",
                    "thinking": {
                        "model": "deepseek-v4-pro",
                        "reasoning_effort": "high",
                        "extra_body": {"thinking": {"type": "enabled"}},
                    },
                },
                "fallback_llm": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "thinking": {
                        "model": "deepseek-v4-pro",
                        "reasoning_effort": "high",
                        "extra_body": {"thinking": {"type": "enabled"}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    # Off by default
    gateway = config.get_gateway_llm_config()
    fallback = config.get_fallback_llm_config()
    assert gateway["model"] == "deepseek-v4-flash"
    assert gateway["reasoning_effort"] is None
    assert fallback["model"] == "deepseek-v4-flash"
    assert fallback["reasoning_effort"] is None

    # Enable thinking mode
    config.update_settings({"thinking_mode": True})
    gateway = config.get_gateway_llm_config()
    fallback = config.get_fallback_llm_config()
    assert gateway["model"] == "deepseek-v4-pro"
    assert gateway["reasoning_effort"] == "high"
    assert gateway["extra_body"] == {"thinking": {"type": "enabled"}}
    assert fallback["model"] == "deepseek-v4-pro"
    assert fallback["reasoning_effort"] == "high"
    assert fallback["extra_body"] == {"thinking": {"type": "enabled"}}

    # Displayed settings include the flag
    displayed = config.get_settings_for_display()
    assert displayed["thinking_mode"] is True
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["thinking_mode"] is True

    rubric_fallback = config.get_fallback_llm_config(
        thinking_enabled_override=False,
    )
    rubric_gateway = config.get_gateway_llm_config(
        thinking_enabled_override=False,
    )
    assert rubric_fallback["reasoning_effort"] is None
    assert rubric_fallback["extra_body"] is None
    assert rubric_gateway["reasoning_effort"] is None
    assert rubric_gateway["extra_body"] is None


def test_default_agent_routing_and_rubric_models_use_pro(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    loaded = config.load_config()

    assert loaded["gateway_llm"]["model"] == "deepseek-v4-pro"
    assert loaded["fallback_llm"]["model"] == "deepseek-v4-pro"
    assert loaded["harness"]["completion"]["rubric"]["model"] == "deepseek-v4-pro"


def test_settings_api_accepts_thinking_mode(tmp_path, monkeypatch):
    """PUT /api/settings must persist the thinking_mode toggle from the dialog."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "thinking_mode": True,
                "gateway_llm": {"model": "deepseek-v4-flash"},
                "fallback_llm": {"model": "deepseek-v4-flash"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    client = TestClient(app)
    response = client.put("/api/settings", json={"thinking_mode": False})
    assert response.status_code == 200, response.text

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["thinking_mode"] is False

    gateway = config.get_gateway_llm_config()
    fallback = config.get_fallback_llm_config()
    assert gateway["model"] == "deepseek-v4-flash"
    assert gateway["reasoning_effort"] is None
    assert fallback["model"] == "deepseek-v4-flash"
    assert fallback["reasoning_effort"] is None


def test_subagent_defaults_are_displayed_when_config_is_empty(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    displayed = config.get_settings_for_display()
    image_analyzer = displayed["subagents"]["items"][0]

    assert image_analyzer["enabled"] is False
    assert image_analyzer["name"] == "image_analyzer"
    assert image_analyzer["model"] == "qwen:qwen3.7"
    assert image_analyzer["route_trigger"] == "image_input"
    assert image_analyzer["tools"]["mode"] == "inherit"
    assert image_analyzer["skills"]["mode"] == "inherit"
    assert "image analysis specialist" in image_analyzer["system_prompt"]
    model_call_limit = displayed["harness"]["model_call_limit"]
    assert model_call_limit["enabled"] is True
    assert model_call_limit["run_limit"] == 50
    assert model_call_limit["thread_limit"] is None
    assert model_call_limit["exit_behavior"] == "end"


def test_settings_api_persists_subagent_spec(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    payload = {
        "subagents": {
            "vision_router": {
                "enabled": True,
                "model": "qwen:qwen3.7",
                "description": "Analyze uploaded images for the main agent.",
                "route_trigger": "image_input",
                "tools": {"mode": "inherit"},
                "skills": {"mode": "custom", "paths": ["/skills/"]},
                "system_prompt": "Focus on visual evidence and return structured findings.",
            },
        }
    }

    client = TestClient(app)
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["subagents"] == payload["subagents"]
    assert "subagent" not in saved

    displayed = config.get_settings_for_display()
    vision_router = next(item for item in displayed["subagents"]["items"] if item["name"] == "vision_router")
    assert vision_router["enabled"] is True
    assert vision_router["model"] == "qwen:qwen3.7"


def test_settings_api_persists_harness_model_call_limit(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    payload = {
        "harness": {
            "model_call_limit": {
                "enabled": True,
                "run_limit": 12,
                "thread_limit": 100,
                "exit_behavior": "error",
            }
        }
    }

    client = TestClient(app)
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["harness"]["model_call_limit"] == payload["harness"]["model_call_limit"]

    displayed = config.get_settings_for_display()
    assert displayed["harness"]["model_call_limit"]["run_limit"] == 12
    assert displayed["harness"]["model_call_limit"]["thread_limit"] == 100


def test_settings_api_persists_harness_prompt_cache_controls(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    prompt_cache = {
        "trace_part_diagnostics": True,
        "ordered_system_sections": True,
        "tail_routing_message": True,
        "deterministic_session_projection": True,
        "stable_tool_schema": False,
    }
    client = TestClient(app)
    response = client.put("/api/settings", json={"harness": {"prompt_cache": prompt_cache}})
    assert response.status_code == 200, response.text

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["harness"]["prompt_cache"] == prompt_cache
    assert config.get_settings_for_display()["harness"]["prompt_cache"] == prompt_cache


def test_settings_api_rejects_non_boolean_prompt_cache_control(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    client = TestClient(app)
    response = client.put(
        "/api/settings",
        json={"harness": {"prompt_cache": {"stable_tool_schema": "yes"}}},
    )

    assert response.status_code == 400
    assert "stable_tool_schema must be a boolean" in response.json()["detail"]


def test_settings_api_persists_deepagents_context_engineering_without_touching_chat(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    chat_before = config.load_config()["compression"]["middleware"]
    payload = {
        "compression": {
            "deepagents": {
                "summarization": {
                    "model_id": "deepseek:deepseek-openai:deepseek-v4-flash:llm",
                    "trigger_tokens": 260000,
                },
                "tool_context": {
                    "enabled": False,
                    "immediate_compaction_enabled": True,
                    "single_tool_trigger_tokens": 9000,
                    "background_min_result_tokens": 1100,
                    "keep_recent_tool_results": 15,
                },
            }
        }
    }

    client = TestClient(app)
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text
    displayed = client.get("/api/settings").json()
    deepagents = displayed["compression"]["deepagents"]
    assert deepagents["summarization"]["model_id"] == (
        "deepseek:deepseek-openai:deepseek-v4-flash:llm"
    )
    assert deepagents["summarization"]["trigger_tokens"] == 260000
    assert "summary_input_tokens" not in deepagents["summarization"]
    assert deepagents["tool_context"] == {
        **deepagents["tool_context"],
        "enabled": False,
        "immediate_compaction_enabled": True,
        "single_tool_trigger_tokens": 9000,
        "background_min_result_tokens": 1100,
        "keep_recent_tool_results": 15,
    }
    assert displayed["compression"]["middleware"] == chat_before


def test_settings_api_rejects_immediate_tool_threshold_above_offload_boundary(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    client = TestClient(app)
    response = client.put(
        "/api/settings",
        json={
            "compression": {
                "deepagents": {
                    "tool_context": {
                        "single_tool_trigger_tokens": 20001,
                    }
                }
            }
        },
    )
    assert response.status_code == 400
    assert "20,000" in response.json()["detail"]


def test_settings_api_migrates_legacy_subagent_items_to_keyed_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    payload = {
        "subagent": {
            "items": [
                {
                    "enabled": True,
                    "name": "image_analyzer",
                    "model": "qwen3.7-plus",
                    "description": "Analyze image inputs and answer questions about them.",
                    "route_trigger": "image_input",
                    "tools": {"mode": "inherit"},
                    "skills": {"mode": "inherit", "paths": []},
                    "system_prompt": "Analyze images.",
                }
            ]
        }
    }

    client = TestClient(app)
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "subagent" not in saved
    assert "items" not in saved["subagents"]
    assert saved["subagents"]["image_analyzer"]["model"] == "qwen3.7-plus"
    assert "name" not in saved["subagents"]["image_analyzer"]


def test_subagent_route_hint_is_exposed_through_native_description():
    spec = _build_subagent_item(
        {
            "enabled": True,
            "name": "image_analyzer",
            "model": "qwen:qwen3.7",
            "description": "Analyze image inputs and answer questions about them.",
            "route_trigger": "image_input",
            "tools": {"mode": "inherit"},
            "skills": {"mode": "inherit", "paths": []},
            "system_prompt": "Analyze images.",
        },
        default_tools=[],
        default_skills=[],
    )

    assert "Use this subagent when the main request matches this routing hint: `image_input`." in spec["description"]
    assert "native task tool" in spec["description"]
    assert "runnable" not in spec
    assert spec["model"]._client.binding == "image_analyzer"
    assert any(isinstance(item, AttachmentImageContentMiddleware) for item in spec["middleware"])


def test_runtime_inventory_includes_deepagents_default_subagent(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)

    mounted = DeepAgentsAgentManager._subagent_inventory()

    assert mounted[0]["name"] == "general-purpose"
    assert mounted[0]["source"] == "deepagents.default"
    assert mounted[1]["name"] == "image_analyzer"
    assert mounted[1]["source"] == "config"


def test_agent_user_content_supports_uploaded_image_attachment(tmp_path):
    attachment = _stored_image_attachment(tmp_path)
    content = DeepAgentsAgentManager._build_user_content(
        "请看图",
        [attachment],
        session_id="session-attachments",
        allow_multimodal=True,
    )

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请看图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_agent_user_content_recognizes_local_image_path(tmp_path):
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    content = DeepAgentsAgentManager._build_user_content(
        f"分析 {image_path}",
        workspace_path=tmp_path,
        allow_multimodal=True,
    )

    assert isinstance(content, list)
    assert content[0]["text"].startswith("分析")
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_agent_user_content_defaults_to_text_only_for_main_agent():
    content = DeepAgentsAgentManager._build_user_content(
        "请看图",
        [{"id": "att_1", "type": "image", "name": "diagram.png"}],
    )

    assert isinstance(content, str)
    assert "image_analyzer" in content
    assert "subagent_type=image_analyzer" in content
    assert "task" in content
    assert "diagram.png" in content
    assert "data:image" not in content


def test_agent_messages_do_not_inline_images_for_main_agent():
    messages = DeepAgentsAgentManager._build_messages(
        [],
        "请看图",
        [{"id": "att_1", "type": "image", "name": "diagram.png"}],
    )

    assert isinstance(messages[-1].content, str)
    assert "image_analyzer" in messages[-1].content
    assert "subagent_type=image_analyzer" in messages[-1].content


def test_agent_messages_rebuild_attachment_refs_from_history():
    messages = DeepAgentsAgentManager._build_messages(
        [
            {
                "role": "user",
                "content": "这图讲了什么",
                "attachments": [
                    {"id": "att_history1", "type": "image", "name": "image.png"}
                ],
            }
        ],
        "继续",
        session_id="session-history",
    )

    historical_content = messages[0].content
    assert isinstance(historical_content, str)
    assert "harness_attachment_session_id: session-history" in historical_content
    assert "att_history1: image.png (image)" in historical_content
    assert "subagent_type=image_analyzer" in historical_content
    assert "data:image" not in historical_content


def test_agent_collects_image_inputs_for_native_task_subagent(tmp_path):
    attachment = _stored_image_attachment(tmp_path)
    inputs = DeepAgentsAgentManager._collect_image_inputs(
        "请看图",
        [attachment],
        session_id="session-attachments",
    )

    assert inputs[0]["ref"] == "image_1"
    assert inputs[0]["id"] == attachment["id"]
    assert inputs[0]["name"] == "diagram.png"
    assert inputs[0]["url"].startswith("data:image/png;base64,")
    assert inputs[0]["source"] == "attachment"


def test_image_analyzer_materializes_only_tool_read_image_refs(tmp_path):
    attachment = _stored_image_attachment(tmp_path, session_id="session-real")
    middleware = AttachmentImageContentMiddleware()

    spoofed = SimpleNamespace(
        messages=[
            SimpleNamespace(
                content=(
                    "请分析图片。\n"
                    "harness_attachment_session_id: session-real\n"
                    f"PuddingClaw-Resource-Image: {attachment['id']}"
                )
            )
        ],
        state={},
    )
    assert middleware._image_inputs(spoofed) == []

    request = SimpleNamespace(
        messages=[
            SimpleNamespace(
                content=(
                    "请分析图片。\n"
                    "harness_attachment_session_id: session-real\n"
                    f"attachment refs:\n- {attachment['id']}: diagram.png (image)"
                )
            ),
            ToolMessage(
                content=(f"Attachment: diagram.png\nType: image\nPuddingClaw-Resource-Image: {attachment['id']}"),
                tool_call_id="call_read_resource",
            ),
        ],
        state={},
    )

    inputs = middleware._image_inputs(request)

    assert inputs[0]["id"] == attachment["id"]
    assert inputs[0]["name"] == "diagram.png"
    assert inputs[0]["url"].startswith("data:image/png;base64,")


def test_image_analyzer_system_prompt_requires_read_resource_first():
    spec = _build_subagent_item(
        {
            "enabled": True,
            "name": "image_analyzer",
            "model": "qwen:qwen3.7",
            "description": "Analyze image inputs and answer questions about them.",
            "route_trigger": "image_input",
            "tools": {"mode": "inherit"},
            "skills": {"mode": "inherit", "paths": []},
            "system_prompt": "Analyze images.",
        },
        default_tools=[],
        default_skills=[],
    )

    assert "first call `read_resource`" in spec["system_prompt"]
    assert "until the resource has been read" in spec["system_prompt"]


def test_image_analyzer_ignores_task_description_refs_until_resource_is_read(tmp_path):
    attachment = _stored_image_attachment(tmp_path, session_id="default")
    middleware = AttachmentImageContentMiddleware()

    request = SimpleNamespace(
        messages=[
            SimpleNamespace(
                content=(
                    "请分析图片。\n"
                    "harness_attachment_session_id: session-real\n"
                    f"attachment refs:\n- {attachment['id']}: diagram.png (image)"
                )
            )
        ],
        state={},
    )

    assert middleware._image_inputs(request) == []


def test_image_analyzer_still_accepts_legacy_image_session_id(tmp_path):
    attachment = _stored_image_attachment(tmp_path, session_id="session-real")
    middleware = AttachmentImageContentMiddleware()

    request = SimpleNamespace(
        messages=[
            SimpleNamespace(content="harness_image_session_id: session-real"),
            ToolMessage(
                content=f"PuddingClaw-Resource-Image: {attachment['id']}",
                tool_call_id="call_read_resource",
            ),
        ],
        state={},
    )

    inputs = middleware._image_inputs(request)

    assert inputs[0]["id"] == attachment["id"]


def test_agent_user_content_does_not_inline_external_image_without_permission(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    image_path = tmp_path / "outside.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    content = DeepAgentsAgentManager._build_user_content(
        f"分析 {image_path}",
        session_id="session-without-external-image-permission",
        workspace_path=workspace_path,
    )

    assert isinstance(content, str)
    assert "workspace 外的本地图片路径" in content
    assert str(image_path) in content


def test_agent_user_content_routes_pasted_absolute_file_path_to_host_file_broker(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    external_file = tmp_path / "Documents" / "notes.md"
    external_file.parent.mkdir()
    external_file.write_text("# Notes", encoding="utf-8")

    content = DeepAgentsAgentManager._build_user_content(
        f"{external_file} 这篇md说了什么",
        session_id="session-without-external-path-permission",
        workspace_path=workspace_path,
    )

    assert isinstance(content, str)
    assert "[本地文件路径]" in content
    assert "非 workspace 本地路径" in content
    assert "直接对原始绝对路径使用 read_file" in content
    assert "HostFileBroker 原子落到正式路径" in content
    assert "文件授权不授予 execute" in content
    assert str(external_file) in content


def test_agent_user_content_preserves_spaced_external_html_target(tmp_path):
    from harness.artifact_paths import extract_declared_artifact_targets

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    external_file = tmp_path / "Design Reports" / "产品配置分析模型模板 v2.html"
    external_file.parent.mkdir()
    external_file.write_text("<html></html>", encoding="utf-8")

    content = DeepAgentsAgentManager._build_user_content(
        f"{external_file} 刷新这个报告到 2026 年",
        session_id="session-spaced-html-path",
        workspace_path=workspace_path,
    )

    assert isinstance(content, str)
    assert str(external_file) in content
    assert (
        "直接对原始绝对路径使用 "
        "read_file/write_file/materialize_source_ref/patch_file"
    ) in content
    assert "HostFileBroker 原子落到正式路径" in content
    assert "不要创建 /workspace 或 /scratch 影子副本" in content
    assert extract_declared_artifact_targets(f"{external_file} 刷新这个报告到 2026 年") == [str(external_file)]
    source_file = tmp_path / "input data.csv"
    assert extract_declared_artifact_targets(f"读取 {source_file} 并更新分析报告") == []


def test_artifact_target_parser_handles_compact_chinese_and_negation():
    from harness.artifact_paths import extract_declared_artifact_targets

    target = "/Users/pet/报告目录/产品配置分析模型模板 v2.html"
    assert extract_declared_artifact_targets(f"请修改{target}并刷新到2026年") == [target]
    assert extract_declared_artifact_targets(f"不要修改 {target}，只读取并总结") == []
    for target in (
        "/data/jobs/report.py",
        "/srv/queries/latest.sql",
        "/opt/designs/chart.svg",
        "/tmp/export.zip",
    ):
        assert extract_declared_artifact_targets(f"请修改{target}并交付") == [target]
        assert extract_declared_artifact_targets(f"请勿修改 {target}，只做审查") == []
    assert extract_declared_artifact_targets("请分析 https://example.com/reports/latest.html，不要写入本地") == []


def test_artifact_target_resolver_derives_v2_html_and_script_companion(tmp_path):
    from harness.artifact_paths import resolve_declared_artifact_targets

    source = tmp_path / "产品配置分析_2026.html"
    source.write_text(
        (
            '<html><script src="echarts-6.1.0.min.js"></script>'
            '<script src="product-config-charts-2026.js?v=1"></script></html>'
        ),
        encoding="utf-8",
    )

    targets = resolve_declared_artifact_targets(
        f"参考{source}，开一个新的V2版本（包含html和js）"
    )

    assert targets == [
        str(tmp_path / "产品配置分析_2026_v2.html"),
        str(tmp_path / "product-config-charts-2026-v2.js"),
    ]


def test_artifact_target_resolver_preserves_already_versioned_script(tmp_path):
    from harness.artifact_paths import resolve_declared_artifact_targets

    source = tmp_path / "产品配置分析_2026.html"
    source.write_text(
        '<script src="product-config-charts-2026-v2.js"></script>',
        encoding="utf-8",
    )

    targets = resolve_declared_artifact_targets(
        f"参考{source}，开一个新的V2版本（包含html和js）"
    )

    assert targets[-1] == str(tmp_path / "product-config-charts-2026-v2.js")


def test_artifact_target_resolver_couples_requested_year_to_template_v3(
    tmp_path,
):
    from harness.artifact_paths import resolve_declared_artifact_targets

    source = tmp_path / "产品配置分析模型模板.html"
    source.write_text(
        '<script src="product-config-charts-2024.js"></script>',
        encoding="utf-8",
    )

    targets = resolve_declared_artifact_targets(
        f"参考{source}，开一个新的2026 V3版本（包含html和js），"
        "时间范围框定2021-2026年"
    )

    assert targets == [
        str(tmp_path / "产品配置分析_2026_v3.html"),
        str(tmp_path / "product-config-charts-2026-v3.js"),
    ]


def test_explicit_output_does_not_promote_reference_template_to_target(tmp_path):
    from harness.artifact_paths import resolve_declared_artifact_targets

    source = tmp_path / "source.html"
    target = tmp_path / "report-v3.html"
    source.write_text("<html></html>", encoding="utf-8")

    targets = resolve_declared_artifact_targets(
        f"参考 {source}，输出到 {target}，生成 V3 html 和 js"
    )

    assert targets == [str(target)]


def test_agent_user_content_keeps_virtual_workspace_path_as_managed_input(tmp_path):
    content = DeepAgentsAgentManager._build_user_content(
        "确认 /workspace/e2e-goal-report.md 的内容",
        session_id="session-virtual-path",
        workspace_path=tmp_path,
    )

    assert content == "确认 /workspace/e2e-goal-report.md 的内容"


def test_web_markdown_url_is_not_extracted_as_local_windows_path(tmp_path):
    from harness.artifact_paths import extract_local_resource_paths

    url = "https://open.feishu.cn/document/no_class/mcp-archive/feishu-cli-installation-guide.md"

    assert extract_local_resource_paths(f"帮我安装飞书 CLI：{url}") == []
    assert DeepAgentsAgentManager._build_user_content(
        f"帮我安装飞书 CLI：{url}",
        session_id="session-web-md",
        workspace_path=tmp_path,
    ) == f"帮我安装飞书 CLI：{url}"
