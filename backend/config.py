"""Global configuration management — JSON-based persistence."""

import copy
import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from runtime_identity.paths import PuddingClawPaths

logger = logging.getLogger(__name__)

# Production always reads the user-owned sparse override below
# PUDDINGCLAW_HOME. Tests may replace this with an explicit temporary path.
CONFIG_FILE: Path | None = None
DEEPAGENTS_SUMMARY_INPUT_CONTEXT_RATIO = 0.8
_LEGACY_TERMINAL_EXECUTION_KEYS = frozenset(
    {"sandbox_mode", "docker_enabled", "on_unavailable"}
)
_REPLACE_DICT_PATHS = frozenset(
    {
        ("vanna", "query", "entity_top_k_by_type"),
        ("subagents",),
        ("mcp", "servers"),
    }
)


class UnsupportedTerminalExecutionConfig(ValueError):
    """Raised when a config still uses the removed execution-mode contract."""

# Ch5 迁移提示标记：legacy summarization 阈值检测只提示一次（防日志噪声）
_LEGACY_WARN_SHOWN: bool = False

_DEFAULT_CONFIG: dict[str, Any] = {
    "rag": {
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
    },
    "database": {
        # Shared Core database for runtime state and extension metadata.
        # SQLite local file is the zero-config default; PostgreSQL stays a
        # server-side option. Knowledge additionally requires pgvector when
        # PostgreSQL is selected.
        #
        # Settings page is the normal desktop source of truth. Only the
        # CLI Runtime uses the PUDDINGCLAW_DATABASE_PROVIDER / MODE / URL /
        # SOURCE deployment contract to override it.
        "provider": "sqlite",  # sqlite | postgresql
        "source": "local_file",  # local_file | bundled | external
        "host": "127.0.0.1",
        "port": 5432,
        "database": "puddingclaw",
        "username": "puddingclaw",
        # Secrets are never code defaults or config.json values. Bundled mode
        # receives its credential through the deployment environment.
        "password": "",
        # Advanced escape hatch for deployments that need a full SQLAlchemy URL.
        # The frontend intentionally does not ask normal desktop users to write this.
        "url": "",
    },
    "knowledge": {
        # User-owned physical directory for local knowledge artifacts. Empty
        # resolves to the canonical knowledge directory under PuddingClaw Home.
        "root_dir": "",
        "llm_wiki": {
            # Hybrid retrieval is enabled by default. Dedicated compiler or
            # GBrain model ids are persisted only when the user overrides the
            # corresponding Provider Registry binding.
            "retrieval": {"hybrid_enabled": True},
        },
        "mineru": {
            "base_url": "http://localhost:8002",
            # Empty means PUDDINGCLAW_HOME/tmp/mineru-runtime/output. An explicit
            # relative path is resolved from PUDDINGCLAW_HOME, never the package.
            # PuddingClaw copies final assets into the user knowledge directory,
            # so successful imports clean runtime output by default.
            "runtime_output_dir": "",
            "keep_runtime_output": False,
            # Large PDFs can take minutes in MinerU pipeline mode. Keep connect
            # timeout short, but allow a long read timeout for parsing.
            "connect_timeout_seconds": 10,
            "read_timeout_seconds": 1800,
        },
        "multimodal_index": {
            "enabled": True,
            "vector_store": "milvus",
            "milvus_uri": "http://localhost:19530",
            "text_collection": "puddingclaw_knowledge_text",
            "image_collection": "puddingclaw_knowledge_image",
            "bm25_enabled": True,
            # Model identity and credentials live in Provider Registry; batch
            # size is part of the knowledge indexing runtime.
            "embedding_batch_size": 10,
        },
        "search": {
            "enabled": True,
            "directories": [
                {"id": "assets", "path": "assets", "enabled": True, "recursive": True, "content_types": ["image"], "referenced_images_only": True},
                {"id": "imported", "path": "imported", "enabled": True, "recursive": True, "content_types": ["markdown", "pdf", "document"]},
                {"id": "originals", "path": "originals", "enabled": True, "recursive": True, "content_types": ["pdf", "document", "image"]},
                {"id": "llm-wiki", "path": "llm-wiki/wiki", "enabled": True, "recursive": True, "content_types": ["markdown"]},
                {"id": "source-code-updates", "path": "source-code-updates", "enabled": True, "recursive": True, "content_types": ["markdown"]},
            ],
            "sources": {"read_later": {"enabled": True}},
            "exclude": ["**/.DS_Store", "**/.git/**", "**/.puddingclaw/**", "llm-wiki/raw/**", "llm-wiki/wiki/index.md", "llm-wiki/wiki/log.md"],
        },
    },
    "vanna": {
        # Global NL2SQL runtime for the analytics workbench. Training data is
        # stored in Milvus collections; database credentials stay in the
        # database-source catalog instead of being duplicated here.
        "enabled": True,
        "default_database_source_id": "project_postgres",
        "default_dialect": "PostgreSQL",
        "llm": {
            # Resolve the dedicated Provider Registry binding by default.
            "reuse": "vanna_llm",
            "model": "",
            "base_url": "",
            "api_key": "",
            "temperature": 0.2,
            "max_tokens": 14000,
        },
        "embedding": {
            # Vanna training data is text-only, so it resolves the dedicated
            # text-embedding binding instead of the multimodal binding.
            "reuse": "vanna_embedding",
            "provider": "qwen",
            "model": "text-embedding-v4",
            "base_url": "",
            "api_key": "",
            "batch_size": 10,
        },
        "milvus": {
            # Keep Vanna NL2SQL collections separate from document RAG
            # collections. These are global to the app workspace.
            "uri": "",
            "sql_collection": "puddingclaw_vanna_sql",
            "ddl_collection": "puddingclaw_vanna_ddl",
            "doc_collection": "puddingclaw_vanna_doc",
            "entity_collection": "puddingclaw_vanna_entity",
            "metric_type": "COSINE",
        },
        "training": {
            "train_models": True,
            "train_measures": True,
            "train_sql_examples": True,
            "train_entities": True,
            "train_temporary_files": False,
        },
        "query": {
            # Entity recall is performed once by the NL2SQL service for trace
            # visibility, then the exact same entity list is passed into Vanna
            # SQL generation. Keep this default conservative; individual
            # business entity types can override it below.
            "entity_top_k_default": 10,
            "entity_top_k_by_type": {
                "品牌": 5,
                "款型": 5,
                "车系": 5,
                "配置分类": 10,
                "配置名称": 20,
            },
        },
    },
    "analytics": {
        "database_qa": {
            "full_rows_token_budget": 10000,
            "preview_rows_token_budget": 3000,
            "profile_token_budget": 3000,
            "full_rows_hard_row_cap": 200,
            "full_rows_hard_column_cap": 20,
            "max_cell_chars_for_llm": 500,
            "result_materialization_row_cap": 99999,
            "query_timeout_ms": 30000,
            "sql_generation_timeout_ms": 210000,
            "result_store_enabled": True,
            "result_store_ttl_hours": 168,
            "default_page_size": 100,
            "max_page_size": 500,
            "export_enabled": True,
            "profile_enabled": True,
        },
    },
    "compression": {
        # Must be less than MAX_HISTORY_MESSAGES (50) so compression fires before truncation
        "trigger_count": 20,
        "max_history_messages": 100,
        # Agent/DeepAgents uses its own built-in history offload + summarization
        # lifecycle. Keep this separate from the legacy Chat middleware stack.
        "deepagents": {
            "summarization": {
                "enabled": True,
                # Optional registered Provider model id used by both automatic
                # DeepAgents summarization and manual /compact. An empty value
                # preserves the legacy behavior of following the Agent model.
                # Empty follows the current Agent Provider binding. Model
                # identity has one source of truth in Provider Registry.
                "model_id": "",
                "trigger_tokens": 272000,
                "keep_tokens": 64000,
            },
            "tool_context": {
                "enabled": True,
                "immediate_compaction_enabled": False,
                "single_tool_trigger_tokens": 8000,
                "background_min_result_tokens": 1000,
                "retain_tool_context_tokens": 32000,
                "batch_size": 6,
                "max_concurrency": 4,
                "job_timeout_seconds": 120,
                "max_candidates_per_job": 48,
            },
        },
        "middleware": {
            "enabled": True,
            # 工具结果摘要：保留最近 10 条完整 tool output，且只摘要 >=500 字符的历史 tool output
            "tool_clear":    {"keep_recent": 10, "min_summary_length": 500},
            # 叙述性摘要：总 token 超过 200K 时触发，保留最近 10 条消息
            "summarization": {"enabled": True, "trigger_tokens": 200000, "keep_messages": 10, "use_chinese_prompt": True},
            # DEPRECATED: MessageTrim 已由 cache.tail_trim 接管（cache-friendly），
            # 此块仅保留供 MessageTrimMiddleware 类外部/测试引用，生产路径不再装配
            "trim":          {"max_tokens": 12000, "keep_last": 10},
            # 全局 reset：总 token 超过 500K 时触发，保留最近 8 条消息，摘要输入预算 120K
            "compaction":    {"enabled": True, "trigger_tokens": 500000, "keep_recent": 8, "compact_budget_tokens": 120000},
        },
    },
    # DeepSeek V4 1M 上下文：cache-friendly 中段裁剪阈值 200K
    "cache": {
        "enabled": True,
        "cache_boundary": {"enabled": True},
        "tail_trim": {"enabled": True, "max_tokens": 200000, "head_keep": 2, "keep_recent": 30},
        "middle_trim": {
            "enabled": True,
            "max_tokens": 200000,
            "head_keep": 2,
            "keep_recent": 30,
            "summary_budget_chars": 60000,
        },
    },
    "subagents": {
        "image_analyzer": {
            "enabled": True,
            # The image analyzer resolves the dedicated Provider binding; this
            # field remains only as the generic subagent display fallback.
            "model": "qwen:qwen3.7",
            "description": "Analyze image inputs and answer questions about them.",
            "route_trigger": "image_input",
            "tools": {"mode": "inherit"},
            "skills": {"mode": "inherit", "paths": []},
            "system_prompt": (
                "You are an image analysis specialist. When given an image, describe its contents "
                "in detail and answer any questions about it. Return your findings as concise, "
                "structured text."
            ),
        },
    },
    "harness": {
        # Cache-safe request layout is the default.  Stable tool schemas remain
        # opt-in because a bounded schema superset can materially increase the
        # input size for tool-heavy installations.
        "prompt_cache": {
            "trace_part_diagnostics": True,
            "ordered_system_sections": True,
            "tail_routing_message": True,
            "deterministic_session_projection": True,
            "stable_tool_schema": False,
        },
        "model_call_limit": {
            "enabled": True,
            "run_limit": 50,
            "thread_limit": None,
            "exit_behavior": "end",
        },
        "model_resilience": {
            # Transport retries are safe only before the first provider chunk
            # crosses into the Agent graph. Provider SDK retries are disabled;
            # this is the single PuddingClaw-owned retry boundary.
            "transport_retry": {
                "enabled": True,
                "max_attempts": 2,
                "initial_delay_seconds": 0.25,
                "max_delay_seconds": 2.0,
            },
            # A normal provider EOF is not necessarily a valid Agent result.
            # Give a terminal response without deliverable content one bounded
            # semantic continuation before failing the Run explicitly.
            "terminal_response": {
                "enabled": True,
                "max_recovery_attempts": 1,
            },
        },
        "completion": {
            "rubric": {
                "enabled": False,
                # Task classification, permission review and completion grading
                # share this non-thinking model unless a deployment overrides it.
                "model": "deepseek-v4-flash",
                "max_iterations": 3,
                "max_stagnant_repairs": 2,
                "custom_rules_enabled": False,
                "custom_rules": [],
            },
        },
        "goals": {
            "enabled": True,
            "activation": "explicit_user_only",
            "default_enabled": False,
            "auto_promote_from_run": False,
            "max_rounds": 8,
        },
        "terminal": {
            "execution_mode": "spawn",
            "external_directory_writable_enabled": False,
            "default_timeout_seconds": 120,
            "docker": {
                "connection": "",
                "context": "",
                "probe_timeout_seconds": 5,
                "image": "puddingclaw/sandbox:python3.12-node22-chromium-v5",
                "cpu_limit": "2",
                "memory_limit_mb": 4096,
                "pids_limit": 256,
                "network_enabled": False,
                "dependency_setup_enabled": False,
                "dependency_setup_opt_in_version": 1,
                "lifecycle": "project",
                "idle_stop_minutes": 30,
            },
        },
    },
    "tool_intent_router": {
        "enabled": True,
        "history_window": 2,
    },
    "write_middleware": {
        "enabled": True,
        "task_state": {
            "enabled": True,
            "todo_path": "workspace/TODO.md",
            "triggers": ["帮我", "待办", "记得", "提醒", "任务", "需要做"],
        },
    },
    # The initialized gbrain home belonging to the active knowledge base is the
    # capability boundary. When it is ready, Agent runtimes automatically
    # attach only the hard-allowlisted gbrain query tools; an incomplete
    # runtime fails closed.
    "mcp": {
        "enabled": [],
        # Every MCP server is described in this one config section. Secret
        # values use environment references and are resolved at runtime.
        "servers": {
            "zhihuiya_patents": {
                "name": "智慧芽专利检索",
                "transport": "streamable-http",
                "url": "https://connect.zhihuiya.com/1458a4/mcp",
                "headers": {"Authorization": "${ZHIHUIYA_MCP_API_KEY}"},
                "timeout": 60,
            },
        },
    },
}


def _deep_merge(
    base: dict,
    override: dict,
    *,
    _path: tuple[str, ...] = (),
) -> dict:
    """Deep merge override into base, preserving nested defaults."""
    # Callers mutate the returned settings object. A shallow copy would retain
    # references into _DEFAULT_CONFIG whenever an override omits a nested key,
    # making one settings save silently change process-wide defaults.
    result = copy.deepcopy(base)
    for key, value in override.items():
        path = (*_path, str(key))
        if path in _REPLACE_DICT_PATHS and isinstance(value, dict):
            result[key] = copy.deepcopy(value)
            continue
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value, _path=path)
        else:
            result[key] = value
    return result


def _deepagents_summary_input_tokens(config: dict[str, Any]) -> int:
    """Derive the summary-call input ceiling from the configured model window."""

    from provider_registry import get_provider_registry

    resolved = get_provider_registry().resolve_binding("agent")
    raw_context_window = resolved.get("context_window", 1000000)
    try:
        context_window = max(1, int(raw_context_window))
    except (TypeError, ValueError):
        context_window = 1000000
    return max(1, int(context_window * DEEPAGENTS_SUMMARY_INPUT_CONTEXT_RATIO))


def _validate_config_document(value: Any) -> dict[str, Any]:
    """Validate the current sparse override schema without compatibility rewrites."""

    if not isinstance(value, dict):
        raise ValueError("PuddingClaw settings must be a JSON object")
    data = copy.deepcopy(value)
    schema_version = data.pop("schema_version", 1)
    if schema_version != 1:
        raise ValueError(f"Unsupported config schema_version: {schema_version!r}")

    retired = {
        "llm",
        "embedding",
        "subagent",
        "ai_gateway",
        "gateway_llm",
        "fallback_llm",
        "fallback_embedding",
        "multimodal_embedding",
        "rag_mode",
        "memory_backend",
        "thinking_mode",
        "mem0",
        "smart_extractor",
    }.intersection(data)
    if retired:
        raise ValueError("Retired settings are not supported: " + ", ".join(sorted(retired)))
    unknown = set(data).difference(_DEFAULT_CONFIG)
    if unknown:
        raise ValueError("Unknown settings: " + ", ".join(sorted(unknown)))

    compression = data.get("compression")
    if isinstance(compression, dict) and "ratio" in compression:
        raise ValueError("Retired setting is not supported: compression.ratio")
    knowledge = data.get("knowledge")
    multimodal_index = knowledge.get("multimodal_index") if isinstance(knowledge, dict) else None
    if isinstance(multimodal_index, dict) and "overwrite" in multimodal_index:
        raise ValueError("Retired setting is not supported: knowledge.multimodal_index.overwrite")
    database = data.get("database")
    if isinstance(database, dict) and database.get("password"):
        raise ValueError("database.password must be stored through Credential Vault")
    analytics = data.get("analytics")
    database_qa = analytics.get("database_qa") if isinstance(analytics, dict) else None
    if isinstance(database_qa, dict):
        removed_analytics = {
            "database_agent_sql_path_enabled",
            "database_agent_sql_path_rollout_percentage",
            "database_agent_sql_shadow_compare_enabled",
        }.intersection(database_qa)
        if removed_analytics:
            raise ValueError(
                "Retired analytics settings are not supported: "
                + ", ".join(sorted(removed_analytics))
            )
    harness = data.get("harness")
    terminal = harness.get("terminal") if isinstance(harness, dict) else None
    docker = terminal.get("docker") if isinstance(terminal, dict) else None
    removed_images = {
        "python:3.12-slim",
        "puddingclaw/sandbox:python3.12-node22-v1",
        "puddingclaw/sandbox:python3.12-node22-v2",
        "puddingclaw/sandbox:python3.12-node22-curl-v3",
    }
    if isinstance(docker, dict) and docker.get("image") in removed_images:
        raise ValueError(f"Unsupported sandbox image: {docker['image']}")
    mcp = data.get("mcp")
    if isinstance(mcp, dict):
        # gbrain is mandatory and readiness-driven. Ignore all user attempts
        # to toggle or redefine it, including direct edits to Home JSON.
        mcp.pop("auto_enable_gbrain", None)
        enabled = mcp.get("enabled")
        if isinstance(enabled, list):
            mcp["enabled"] = [name for name in enabled if name != "gbrain"]
        servers = mcp.get("servers")
        if isinstance(servers, dict):
            only_builtin_gbrain = bool(servers) and all(name == "gbrain" for name in servers)
            servers.pop("gbrain", None)
            if only_builtin_gbrain:
                mcp.pop("servers", None)
    _strip_empty_inherited_overrides(data)
    return data


def _strip_empty_inherited_overrides(config: dict[str, Any]) -> bool:
    """Remove blank overrides whose product default is a meaningful value."""

    harness = config.get("harness")
    completion = harness.get("completion") if isinstance(harness, dict) else None
    rubric = completion.get("rubric") if isinstance(completion, dict) else None
    if not isinstance(rubric, dict):
        return False
    model = rubric.get("model")
    if not isinstance(model, str) or model.strip():
        return False
    rubric.pop("model", None)
    if not rubric:
        completion.pop("rubric", None)
    if not completion:
        harness.pop("completion", None)
    if not harness:
        config.pop("harness", None)
    return True


def _config_path() -> Path:
    """Return the user override file, while honoring explicit test overrides."""

    if CONFIG_FILE is not None:
        return Path(CONFIG_FILE)
    return PuddingClawPaths.from_environment().root / "config.json"


def load_config() -> dict[str, Any]:
    """Load bundled defaults plus user overrides from PUDDINGCLAW_HOME."""
    config_path = _config_path()
    if not config_path.exists():
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    try:
        data = _validate_config_document(
            json.loads(config_path.read_text(encoding="utf-8"))
        )
        terminal = data.get("harness", {}).get("terminal") if isinstance(data, dict) else None
        if isinstance(terminal, dict):
            legacy_keys = sorted(_LEGACY_TERMINAL_EXECUTION_KEYS.intersection(terminal))
            if legacy_keys:
                raise UnsupportedTerminalExecutionConfig(
                    "harness.terminal uses removed execution fields: "
                    + ", ".join(legacy_keys)
                    + "; choose execution_mode=spawn or kernel"
                )
        merged = _deep_merge(_DEFAULT_CONFIG, data)
        return merged
    except UnsupportedTerminalExecutionConfig:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("[config] invalid settings file %s: %s", config_path, exc)
        raise ValueError(f"Invalid PuddingClaw settings file: {config_path}") from exc
    except Exception:
        # A Vault, permission, or validation failure must not be disguised as
        # a clean default configuration; doing so can overwrite user intent
        # and make credentials appear to have vanished.
        logger.exception("[config] failed to load settings from %s", config_path)
        raise


def save_config(config: dict[str, Any]) -> None:
    """Persist user overrides atomically below PUDDINGCLAW_HOME."""
    target = _config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    persisted = copy.deepcopy(config)
    _strip_empty_inherited_overrides(persisted)
    mcp = persisted.get("mcp")
    if isinstance(mcp, dict):
        mcp.pop("auto_enable_gbrain", None)
        if isinstance(mcp.get("enabled"), list):
            mcp["enabled"] = [name for name in mcp["enabled"] if name != "gbrain"]
        if isinstance(mcp.get("servers"), dict):
            only_builtin_gbrain = bool(mcp["servers"]) and all(
                name == "gbrain" for name in mcp["servers"]
            )
            mcp["servers"].pop("gbrain", None)
            if only_builtin_gbrain:
                mcp.pop("servers", None)
    _strip_provider_credentials(persisted)
    _strip_database_credentials(persisted)
    database = persisted.get("database")
    if isinstance(database, dict) and database.get("password_ref") and database.get("password") == "":
        database.pop("password", None)
    payload = {"schema_version": 1, **_config_overrides(persisted, _DEFAULT_CONFIG)}
    fd, raw_path = tempfile.mkstemp(prefix=".config.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw_path, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    finally:
        if os.path.exists(raw_path):
            os.unlink(raw_path)


def _config_overrides(
    value: Any,
    defaults: Any,
    *,
    _path: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return only user-owned differences from package defaults."""

    if not isinstance(value, dict) or not isinstance(defaults, dict):
        return value
    result: dict[str, Any] = {}
    for key, current in value.items():
        if key == "schema_version":
            continue
        if key not in defaults:
            result[key] = current
            continue
        default = defaults[key]
        path = (*_path, str(key))
        if path in _REPLACE_DICT_PATHS:
            if current != default:
                result[key] = copy.deepcopy(current)
            continue
        if isinstance(current, dict) and isinstance(default, dict):
            nested = _config_overrides(current, default, _path=path)
            if nested:
                result[key] = nested
        elif isinstance(current, float) and isinstance(default, (int, float)):
            if not math.isclose(current, float(default), rel_tol=1e-12, abs_tol=1e-12):
                result[key] = current
        elif current != default:
            result[key] = current
    return result


def _strip_provider_credentials(config: dict[str, Any]) -> bool:
    """Keep provider secrets out of the general settings document."""
    changed = False
    paths = [
        ("rag", "rerank"),
        ("vanna", "llm"),
        ("vanna", "embedding"),
    ]
    for path in paths:
        current: Any = config
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, dict) and current.get("api_key"):
            current["api_key"] = ""
            changed = True
    return changed


def _strip_database_credentials(config: dict[str, Any]) -> bool:
    database = config.get("database")
    if not isinstance(database, dict) or not database.get("password"):
        return False
    from provider_registry import LocalCredentialStore

    reference = LocalCredentialStore().put("database-config", str(database.get("password")))
    database["password_ref"] = reference
    database["password"] = ""
    return True


def get_middleware_config() -> dict:
    """Get compression middleware configuration.

    Context Engineering 迁移检测：若磁盘 config.json 里 summarization.trigger_tokens < 200000
    （或 compaction.trigger_tokens < 500000），说明用户沿用了旧版低阈值，
    不符合 DeepSeek V4 1M 上下文窗口的分级兜底策略 —— 首次调用时 warn 一次。
    """
    global _LEGACY_WARN_SHOWN
    config = load_config()
    mw = config.get("compression", {}).get("middleware", {
        "enabled": True,
        "tool_clear":    {"keep_recent": 10, "min_summary_length": 500},
        "summarization": {"enabled": True, "trigger_tokens": 200000, "keep_messages": 10, "use_chinese_prompt": True},
        "trim":          {"max_tokens": 12000, "keep_last": 10},
        "compaction":    {"enabled": True, "trigger_tokens": 500000, "keep_recent": 8, "compact_budget_tokens": 120000},
    })

    if not _LEGACY_WARN_SHOWN:
        sum_trigger = mw.get("summarization", {}).get("trigger_tokens", 200000)
        comp_trigger = mw.get("compaction", {}).get("trigger_tokens", 500000)
        if sum_trigger < 200000 or comp_trigger < 500000:
            logger.warning(
                "[config] 检测到 legacy 压缩阈值 (summarization=%d, compaction=%d)，"
                "低于 Context Engineering 推荐 (200000, 500000)。建议在 config.json 的 compression.middleware 下抬高阈值。",
                sum_trigger, comp_trigger,
            )
        _LEGACY_WARN_SHOWN = True

    return mw


def get_cache_config() -> dict:
    """Get cache middleware configuration (Context Engineering: CacheBoundary + TailTrim)."""
    config = load_config()
    return config.get("cache", {
        "enabled": True,
        "cache_boundary": {"enabled": True},
        "tail_trim": {"enabled": True, "max_tokens": 200000, "head_keep": 2, "keep_recent": 30},
        "middle_trim": {
            "enabled": True,
            "max_tokens": 200000,
            "head_keep": 2,
            "keep_recent": 30,
            "summary_budget_chars": 60000,
        },
    })


def get_compress_trigger_count() -> int:
    """Get the message count threshold for auto-compression."""
    config = load_config()
    return int(config.get("compression", {}).get("trigger_count", 20))


def get_rag_config() -> dict[str, Any]:
    """Read document-knowledge retrieval settings used by Agent tools."""

    config = load_config()
    rag = config.get("rag", {})

    def _positive_int(value: Any, fallback: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return fallback

    def _float(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    return {
        "top_k": _positive_int(rag.get("top_k"), 10),
        "similarity_threshold": _float(rag.get("similarity_threshold"), 0.5),
    }


def get_rag_hybrid_config() -> dict[str, Any]:
    """Read LlamaIndex semantic hybrid retrieval settings from config.json."""

    config = load_config()
    rag = config.get("rag", {})
    hybrid = rag.get("hybrid", {})

    def _positive_int(value: Any, fallback: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return fallback

    def _weight(value: Any, fallback: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return fallback
        return min(1.0, max(0.0, parsed))

    return {
        "enabled": bool(hybrid.get("enabled", True)),
        "mode": hybrid.get("mode", "reciprocal_rerank"),
        "text_vector_weight": _weight(hybrid.get("text_vector_weight"), 0.7),
        "image_vector_weight": _weight(hybrid.get("image_vector_weight"), 0.4),
        "bm25_weight": _weight(hybrid.get("bm25_weight"), 0.3),
        "candidate_top_k": _positive_int(hybrid.get("candidate_top_k"), 30),
    }


def get_tool_intent_router_config() -> dict:
    """Get tool-intent router middleware configuration."""
    config = load_config()
    return config.get("tool_intent_router", {
        "enabled": True,
        "history_window": 2,
    })


def get_write_middleware_config() -> dict:
    """Get write-middleware (after_model side-effect) configuration."""
    config = load_config()
    return config.get("write_middleware", {
        "enabled": True,
        "task_state": {
            "enabled": True,
            "todo_path": "workspace/TODO.md",
            "triggers": ["帮我", "待办", "记得", "提醒", "任务", "需要做"],
        },
    })


def get_fallback_llm_config(
    *,
    thinking_enabled_override: bool | None = None,
    binding: str = "agent",
    model_id_override: str | None = None,
    thinking_level: str | None = None,
    credential_name: str | None = None,
) -> dict[str, Any]:
    """Resolve one LLM workload binding from the local Provider Registry.

    ``agent`` is the main assistant model. Other workloads, such as the
    built-in image analyzer, can select a different Provider model while
    sharing the same direct-connection implementation.
    """
    config = load_config()
    from provider_registry import get_provider_registry

    registry = get_provider_registry()
    resolved = (
        registry.resolve_model(
            model_id_override,
            credential_name=credential_name,
        )
        if model_id_override
        else registry.resolve_binding(
            binding,
            credential_name=credential_name,
        )
    )
    from llm.thinking_mapping import map_thinking_request

    # Conversation models follow their Provider Profile by default. Internal
    # callers such as summaries, titles and rubric checks can still explicitly
    # force reasoning off without reintroducing a global thinking_mode switch.
    mapped_thinking = (
        {
            "thinking_enabled": False,
            "thinking_level": None,
            "reasoning_effort": None,
            "extra_body": None,
        }
        if thinking_enabled_override is False
        else map_thinking_request(
            resolved.get("thinking_profile", {}),
            thinking_level,
        )
    )
    return {
        "provider": resolved.get("provider_id", "deepseek"),
        "model": resolved.get("name") or "deepseek-chat",
        "api_key": resolved.get("api_key", ""),
        "base_url": resolved.get("base_url", "https://api.deepseek.com"),
        "protocol": resolved.get("protocol", "deepseek"),
        "model_id": resolved.get("id", ""),
        "credential_name": resolved.get("credential_name", "default"),
        "temperature": float(resolved.get("temperature", 0.7)),
        "max_tokens": int(resolved.get("max_tokens", 4096)),
        "context_window": int(resolved.get("context_window", 1000000)),
        **mapped_thinking,
    }


def get_llm_wiki_compiler_agent_config(
    *,
    model_id_override: str | None = None,
) -> dict[str, Any]:
    """Resolve the dedicated LLM Wiki compiler model from Model Services.

    The knowledge setting stores only a registry model id. Credentials and
    endpoint details remain owned by Provider Registry and are never copied
    into the knowledge configuration or background job metadata.
    """

    config = load_config()
    compiler = (
        config.get("knowledge", {})
        .get("llm_wiki", {})
        .get("compiler_agent", {})
    )
    configured_model_id = str(
        model_id_override
        if model_id_override is not None
        else compiler.get("model_id") or ""
    ).strip()

    from provider_registry import get_provider_registry

    registry = get_provider_registry()
    resolved = (
        registry.resolve_model(configured_model_id)
        if configured_model_id
        else registry.resolve_binding("agent")
    )
    return {
        "model_id": str(resolved.get("id") or ""),
        "configured_model_id": configured_model_id,
        "model": str(resolved.get("name") or ""),
        "provider": str(resolved.get("provider_id") or ""),
        "uses_agent_default": not configured_model_id,
    }


def get_llm_wiki_retrieval_config() -> dict[str, Any]:
    """Return the LLM Wiki query mode and its shared vector infrastructure."""

    config = load_config()
    settings = (
        config.get("knowledge", {})
        .get("llm_wiki", {})
        .get("retrieval", {})
    )
    if not isinstance(settings, dict):
        settings = {}
    return {
        "hybrid_enabled": bool(settings.get("hybrid_enabled", True)),
        "lexical_weight": 0.45,
        "semantic_weight": 0.55,
        "candidate_multiplier": 6,
        "rrf_k": 10,
        "lexical_strength_weight": 0.03,
        "dual_channel_bonus": 0.008,
        "exact_title_bonus": 0.04,
        "intent_type_bonus": 0.012,
    }


def get_llm_wiki_gbrain_config() -> dict[str, Any]:
    """Resolve gbrain's embedding and Think models from Model Services."""

    config = load_config()
    settings = (
        config.get("knowledge", {})
        .get("llm_wiki", {})
        .get("gbrain", {})
    )
    if not isinstance(settings, dict):
        settings = {}
    embedding_model_id = str(settings.get("embedding_model_id") or "").strip()
    think_model_id = str(settings.get("think_model_id") or "").strip()

    from provider_registry import get_provider_registry

    registry = get_provider_registry()
    embedding = (
        registry.resolve_model(
            embedding_model_id,
            expected_capability="text_embedding",
        )
        if embedding_model_id
        else registry.resolve_binding("text_embedding")
    )
    think = (
        registry.resolve_model(think_model_id)
        if think_model_id
        else registry.resolve_binding("agent")
    )
    dimension = int(embedding.get("dimension") or 0)
    if dimension <= 0:
        raise ValueError("GBrain Embedding 模型必须配置有效维度")
    return {
        "embedding": {
            **embedding,
            "configured_model_id": embedding_model_id,
            "uses_default_binding": not embedding_model_id,
            "dimension": dimension,
        },
        "think": {
            **think,
            "configured_model_id": think_model_id,
            "uses_default_binding": not think_model_id,
        },
    }


def get_fallback_embedding_config() -> dict[str, Any]:
    """Resolve the text-embedding workload from Provider Registry."""
    config = load_config()
    from llm.embedding_limits import clamp_embedding_batch_size
    from provider_registry import get_provider_registry

    resolved = get_provider_registry().resolve_binding("text_embedding")
    model = str(resolved.get("name") or "text-embedding-v4")
    return {
        "provider": resolved.get("provider_id", "dashscope"),
        "model": model,
        "api_key": resolved.get("api_key", ""),
        "api_base": resolved.get("base_url", "https://api.openai.com/v1"),
        "protocol": resolved.get("protocol", "openai_compatible"),
        "model_id": resolved.get("id", ""),
        "dimension": int(resolved.get("dimension", 1024)),
        "batch_size": clamp_embedding_batch_size(model, int(resolved.get("batch_size", 10))),
    }


def get_multimodal_embedding_config() -> dict[str, Any]:
    """Resolve the Model Services binding plus knowledge-index concurrency."""

    config = load_config()
    from provider_registry import get_provider_registry

    resolved = get_provider_registry().resolve_binding("multimodal_embedding")
    # Model identity, endpoint, dimension and credential belong to Model
    # Services. Request concurrency is an indexing runtime concern and remains
    # configurable from Knowledge settings for compatibility with existing
    # installations.
    runtime = config.get("knowledge", {}).get("multimodal_index", {})
    return {
        "provider": resolved.get("provider_id", "dashscope"),
        "model": resolved.get("name", "qwen3-vl-embedding"),
        "dimension": int(resolved.get("dimension", 1024)),
        "batch_size": max(
            1,
            int(runtime.get("embedding_batch_size", resolved.get("batch_size", resolved.get("concurrency", 10)))),
        ),
        "api_key": resolved.get("api_key", ""),
        "base_url": resolved.get("base_url", "https://dashscope.aliyuncs.com"),
        "route_path": resolved.get("route_path", "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"),
        "protocol": resolved.get("protocol", "dashscope_multimodal_embedding"),
        "model_id": resolved.get("id", ""),
    }


def _env_bool(name: str, default: bool) -> bool:
    import os

    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_knowledge_multimodal_index_config() -> dict[str, Any]:
    """Read LlamaIndex multimodal index publishing config.

    Stored in config.json for normal use, with env variables kept as deployment
    overrides for Docker/CI.
    """

    import os

    config = load_config()
    index = config.get("knowledge", {}).get("multimodal_index", {})
    return {
        "enabled": _env_bool("PUDDINGCLAW_ENABLE_MULTIMODAL_INDEX", bool(index.get("enabled", False))),
        "vector_store": os.getenv("PUDDINGCLAW_MULTIMODAL_VECTOR_STORE") or index.get("vector_store", "milvus"),
        "milvus_uri": (
            os.getenv("MILVUS_URI")
            or os.getenv("PUDDINGCLAW_MILVUS_URI")
            or index.get("milvus_uri", "http://localhost:19530")
        ),
        "text_collection": (
            os.getenv("PUDDINGCLAW_MILVUS_TEXT_COLLECTION")
            or index.get("text_collection", "puddingclaw_knowledge_text")
        ),
        "image_collection": (
            os.getenv("PUDDINGCLAW_MILVUS_IMAGE_COLLECTION")
            or index.get("image_collection", "puddingclaw_knowledge_image")
        ),
        "bm25_enabled": bool(index.get("bm25_enabled", True)),
        "overwrite": False,
    }


def get_rag_rerank_config() -> dict[str, Any]:
    config = load_config()
    rerank = config.get("rag", {}).get("rerank", {})

    def _positive_int(value: Any, fallback: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return fallback

    return {
        "enabled": bool(rerank.get("enabled", True)),
        "provider": rerank.get("provider", "dashscope"),
        "model": rerank.get("model", "qwen3-vl-rerank"),
        "top_n": _positive_int(rerank.get("top_n"), 5),
        "candidate_top_k": _positive_int(rerank.get("candidate_top_k"), 50),
    }


def get_knowledge_root_config() -> dict[str, Any]:
    """Read user-configurable local knowledge root directory."""

    import os

    config = load_config()
    knowledge = config.get("knowledge", {})
    env_root = os.getenv("PUDDINGCLAW_KNOWLEDGE_DIR", "").strip()
    configured_root = str(knowledge.get("root_dir", "") or "").strip()
    return {
        "root_dir": env_root or configured_root,
        "configured_by": "PUDDINGCLAW_KNOWLEDGE_DIR" if env_root else "config.json" if configured_root else "default",
        "environment_override": bool(env_root),
    }


def get_knowledge_mineru_config() -> dict[str, Any]:
    """Read MinerU runtime behavior from config.json."""

    config = load_config()
    mineru = config.get("knowledge", {}).get("mineru", {})
    output_dir = str(mineru.get("runtime_output_dir") or "").strip()
    env_base_url = os.getenv("PUDDINGCLAW_MINERU_URL", "").strip()
    return {
        "base_url": env_base_url or str(mineru.get("base_url") or "http://localhost:8002").strip(),
        "runtime_output_dir": output_dir,
        "keep_runtime_output": bool(mineru.get("keep_runtime_output", False)),
        "connect_timeout_seconds": int(mineru.get("connect_timeout_seconds") or 10),
        "read_timeout_seconds": int(mineru.get("read_timeout_seconds") or 1800),
    }


def get_vanna_config() -> dict[str, Any]:
    """Read global Vanna NL2SQL runtime settings from config.json."""

    config = load_config()
    vanna = config.get("vanna", {})
    embedding = vanna.get("embedding", {})
    llm = vanna.get("llm", {})
    milvus = vanna.get("milvus", {})
    training = vanna.get("training", {})
    query = vanna.get("query", {})
    knowledge_index = config.get("knowledge", {}).get("multimodal_index", {})
    agent_llm = get_fallback_llm_config()
    text_embedding = get_fallback_embedding_config()

    def _positive_int(value: Any, default: int) -> int:
        try:
            return max(1, int(value))
        except Exception:
            return default

    def _positive_int_map(value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, int] = {}
        for key, item in value.items():
            name = str(key).strip()
            if not name:
                continue
            try:
                normalized[name] = max(1, int(item))
            except Exception:
                continue
        return normalized

    # Legacy reuse labels are imported once, while runtime resolution always
    # follows the dedicated Provider Registry binding.
    llm_reuse = str(llm.get("reuse") or "vanna_llm")
    default_llm_model = agent_llm.get("model", "")
    default_llm_base = agent_llm.get("base_url", "")
    default_llm_key = agent_llm.get("api_key", "")

    embedding_reuse = str(embedding.get("reuse") or "vanna_embedding")
    default_embedding_model = text_embedding.get("model", "")
    default_embedding_base = text_embedding.get("api_base", "")
    default_embedding_key = text_embedding.get("api_key", "")

    return {
        "enabled": bool(vanna.get("enabled", True)),
        "default_database_source_id": str(vanna.get("default_database_source_id") or "project_postgres"),
        "default_dialect": str(vanna.get("default_dialect") or "PostgreSQL"),
        "llm": {
            "reuse": llm_reuse,
            "model": str(llm.get("model") or default_llm_model),
            "base_url": str(llm.get("base_url") or default_llm_base),
            "api_key": str(llm.get("api_key") or default_llm_key),
            "temperature": float(llm.get("temperature", 0.2)),
            "max_tokens": int(llm.get("max_tokens", 14000)),
        },
        "embedding": {
            "reuse": embedding_reuse,
            "provider": str(embedding.get("provider") or text_embedding.get("provider") or "qwen"),
            "model": str(embedding.get("model") or default_embedding_model),
            "base_url": str(embedding.get("base_url") or default_embedding_base),
            "api_key": str(embedding.get("api_key") or default_embedding_key),
            "batch_size": max(1, int(embedding.get("batch_size") or text_embedding.get("batch_size") or 10)),
        },
        "milvus": {
            "uri": str(milvus.get("uri") or knowledge_index.get("milvus_uri") or "http://localhost:19530"),
            "sql_collection": str(milvus.get("sql_collection") or "puddingclaw_vanna_sql"),
            "ddl_collection": str(milvus.get("ddl_collection") or "puddingclaw_vanna_ddl"),
            "doc_collection": str(milvus.get("doc_collection") or "puddingclaw_vanna_doc"),
            "entity_collection": str(milvus.get("entity_collection") or "puddingclaw_vanna_entity"),
            "metric_type": str(milvus.get("metric_type") or "COSINE").upper(),
        },
        "training": {
            "train_models": bool(training.get("train_models", True)),
            "train_measures": bool(training.get("train_measures", True)),
            "train_sql_examples": bool(training.get("train_sql_examples", True)),
            "train_entities": bool(training.get("train_entities", True)),
            "train_temporary_files": bool(training.get("train_temporary_files", False)),
        },
        "query": {
            "entity_top_k_default": _positive_int(query.get("entity_top_k_default"), 10),
            "entity_top_k_by_type": _positive_int_map(query.get("entity_top_k_by_type")),
        },
    }


def get_database_qa_config() -> dict[str, Any]:
    """Read Smart Database Q&A result handling settings."""

    config = load_config()
    database_qa = config.get("analytics", {}).get("database_qa", {})

    def _positive_int(value: Any, fallback: int, *, minimum: int = 1, maximum: int | None = None) -> int:
        try:
            parsed = max(minimum, int(value))
        except Exception:
            parsed = fallback
        if maximum is not None:
            return min(maximum, parsed)
        return parsed

    return {
        "full_rows_token_budget": _positive_int(database_qa.get("full_rows_token_budget"), 10000, maximum=100000),
        "preview_rows_token_budget": _positive_int(database_qa.get("preview_rows_token_budget"), 3000, maximum=50000),
        "profile_token_budget": _positive_int(database_qa.get("profile_token_budget"), 3000, maximum=50000),
        "full_rows_hard_row_cap": _positive_int(database_qa.get("full_rows_hard_row_cap"), 200, maximum=10000),
        "full_rows_hard_column_cap": _positive_int(database_qa.get("full_rows_hard_column_cap"), 20, maximum=200),
        "max_cell_chars_for_llm": _positive_int(database_qa.get("max_cell_chars_for_llm"), 500, maximum=10000),
        "result_materialization_row_cap": _positive_int(
            database_qa.get("result_materialization_row_cap"),
            99999,
            maximum=100000,
        ),
        "query_timeout_ms": _positive_int(database_qa.get("query_timeout_ms"), 30000, minimum=1000, maximum=300000),
        "sql_generation_timeout_ms": _positive_int(
            database_qa.get("sql_generation_timeout_ms"),
            210000,
            minimum=30000,
            maximum=600000,
        ),
        "result_store_enabled": bool(database_qa.get("result_store_enabled", True)),
        "result_store_ttl_hours": _positive_int(database_qa.get("result_store_ttl_hours"), 168, maximum=24 * 365),
        "default_page_size": _positive_int(database_qa.get("default_page_size"), 100, maximum=5000),
        "max_page_size": _positive_int(database_qa.get("max_page_size"), 500, maximum=10000),
        "export_enabled": bool(database_qa.get("export_enabled", True)),
        "profile_enabled": bool(database_qa.get("profile_enabled", True)),
    }


def _raw_database_overrides() -> dict[str, Any]:
    """Read the user-written database section without merged defaults.

    Merged config always contains provider/source defaults, so only the raw
    override file can tell an explicit new-schema provider apart from a
    legacy ``mode``-only document.
    """

    config_path = _config_path()
    try:
        if not config_path.exists():
            return {}
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    database = data.get("database")
    return database if isinstance(database, dict) else {}


def get_database_config() -> dict[str, Any]:
    """Read catalog database connection config.

    Settings page / config.json is the normal desktop source of truth. CLI
    Runtime can override provider, mode, source and URL through the
    PUDDINGCLAW_DATABASE_* deployment contract. Generic DATABASE_URL /
    POSTGRES_URL are ignored here: they are too easy to inherit from Docker
    shells and make the UI look wrong.

    The canonical schema is ``provider`` (sqlite | postgresql) plus ``source``
    (local_file | bundled | external). Legacy ``mode`` (sqlite | bundled |
    external) documents are still read, and the returned dict keeps a derived
    ``mode`` for backward-compatible callers.
    """

    import os
    from urllib.parse import quote, unquote, urlparse

    env_url = (os.getenv("PUDDINGCLAW_DATABASE_URL") or "").strip()
    env_mode = (os.getenv("PUDDINGCLAW_DATABASE_MODE") or "").strip().lower()
    env_source = (os.getenv("PUDDINGCLAW_DATABASE_SOURCE") or "").strip().lower()
    env_provider = (os.getenv("PUDDINGCLAW_DATABASE_PROVIDER") or "").strip().lower()
    database = load_config().get("database", {})
    raw_database = _raw_database_overrides()
    configured_url = str(database.get("url", "") or "").strip()
    host = str(database.get("host", "127.0.0.1") or "127.0.0.1").strip()
    port = int(database.get("port") or 5432)
    db_name = str(database.get("database", "puddingclaw") or "puddingclaw").strip()
    username = str(database.get("username", "puddingclaw") or "puddingclaw").strip()

    # Resolve provider/source. An explicit new-schema provider wins; a legacy
    # mode-only document is mapped (sqlite -> sqlite/local_file, bundled ->
    # postgresql/bundled, external -> postgresql/external).
    provider = str(raw_database.get("provider", "") or "").strip().lower()
    source = str(raw_database.get("source", "") or "").strip().lower()
    legacy_mode = str(raw_database.get("mode", "") or "").strip().lower()
    if provider not in {"sqlite", "postgresql"}:
        if legacy_mode == "sqlite":
            provider = "sqlite"
            source = source or "local_file"
        elif legacy_mode in {"bundled", "external"}:
            provider = "postgresql"
            source = source or legacy_mode
        else:
            provider = str(database.get("provider", "sqlite") or "sqlite").strip().lower()
            if provider not in {"sqlite", "postgresql"}:
                provider = "sqlite"
            source = source or str(database.get("source", "") or "").strip().lower()
    if not source:
        source = "local_file" if provider == "sqlite" else "external"

    from provider_registry import LocalCredentialStore

    raw_password = str(database.get("password") or "")
    password_ref = str(database.get("password_ref") or "")
    if raw_password and not password_ref:
        password_ref = LocalCredentialStore().put("database-config", raw_password)
    password = LocalCredentialStore().get(password_ref) if password_ref else ""
    if not password:
        password = os.getenv("PUDDINGCLAW_DATABASE_PASSWORD", "")
    if env_mode == "sqlite" or env_provider == "sqlite":
        provider = "sqlite"
        source = env_source or "local_file"
    elif env_url:
        parsed = urlparse(env_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        host = parsed.hostname or host
        port = int(parsed.port or port)
        db_name = unquote(parsed.path.lstrip("/")) or db_name
        username = unquote(parsed.username or username)
        provider = "postgresql"
        source = "external" if env_source == "external" else (env_source or "bundled")
    elif env_mode in {"postgresql", "bundled", "external"} or env_provider == "postgresql":
        provider = "postgresql"
        if env_mode in {"bundled", "external"}:
            source = env_mode
        elif env_source:
            source = env_source
    assembled_url = ""
    if provider == "postgresql":
        assembled_url = (
            "postgresql+asyncpg://"
            f"{quote(username)}:{quote(password)}@{host}:{port}/{quote(db_name)}"
        )
    effective_config_url = "" if provider == "sqlite" else configured_url or assembled_url
    environment_override = bool(env_url or env_mode or env_provider)
    # Backward-compatible legacy mode derived from provider/source.
    if provider == "sqlite":
        mode = "sqlite"
    elif source == "external":
        mode = "external"
    else:
        mode = "bundled"
    return {
        "provider": provider,
        "source": source,
        "mode": mode,
        "catalog_path": (
            str(PuddingClawPaths.from_environment().databases() / "catalog.sqlite3")
            if provider == "sqlite"
            else ""
        ),
        "host": host,
        "port": port,
        "database": db_name,
        "username": username,
        "password": password,
        "password_ref": password_ref,
        "url": "" if provider == "sqlite" else env_url or effective_config_url,
        "configured_url": configured_url,
        "configured_by": (
            "environment"
            if environment_override
            else "config.json"
            if raw_database
            else "default"
        ),
        "environment_override": environment_override,
    }


_SUBAGENT_RESERVED_KEYS = {"enabled", "items"}


def _subagent_items_for_display(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return display-friendly items from the canonical keyed config."""
    if not raw:
        return []

    if "items" in raw:
        raise ValueError("subagents.items is not supported; use a keyed subagents object")

    items: list[dict[str, Any]] = []
    for key, value in raw.items():
        if key in _SUBAGENT_RESERVED_KEYS or not isinstance(value, dict):
            continue
        item = dict(value)
        item["name"] = str(item.get("name") or key)
        item.setdefault("enabled", False)
        item.setdefault("description", "Analyze image inputs and answer questions about them.")
        item.setdefault("model", "")
        item.setdefault("system_prompt", "")
        item.setdefault("route_trigger", "image_input")
        item.setdefault("tools", {"mode": "inherit"})
        item.setdefault("skills", {"mode": "inherit", "paths": []})
        items.append(item)
    return items


def _subagent_config_from_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist subagents as a keyed object: {"image_analyzer": {...}}."""
    result: dict[str, Any] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"subagent_{index + 1}").strip() or f"subagent_{index + 1}"
        stored = dict(item)
        stored.pop("name", None)
        result[name] = stored
    return result


def _normalize_subagent_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize subagent config for frontend display while keeping storage keyed."""
    return {"items": _subagent_items_for_display(raw)}


def get_settings_for_display() -> dict[str, Any]:
    """Get settings with masked API keys for frontend display."""
    from provider_registry import get_provider_registry

    config = load_config()
    effective_knowledge_index = get_knowledge_multimodal_index_config()
    effective_knowledge_root = get_knowledge_root_config()
    effective_knowledge_mineru = get_knowledge_mineru_config()
    effective_database = get_database_config()
    effective_vanna = get_vanna_config()
    effective_database_qa = get_database_qa_config()
    raw_vanna = config.get("vanna", {})
    provider_registry = get_provider_registry().display()
    display_database = dict(effective_database)
    display_database["password"] = ""
    display_database["url"] = _redact_database_url(str(display_database.get("url") or ""))
    raw_parser_settings = config.get("knowledge", {}).get("parsers", {})
    raw_parser_items = raw_parser_settings.get("items", {}) if isinstance(raw_parser_settings, dict) else {}
    display_parser_items = {
        str(parser_id): {
            key: value
            for key, value in item.items()
            if key not in {"credential_ref", "api_key", "token"}
        }
        for parser_id, item in raw_parser_items.items()
        if isinstance(item, dict)
    }
    result = {
        "provider_registry": provider_registry,
        "rag": config.get("rag", {}),
        "vanna": {
            "enabled": bool(raw_vanna.get("enabled", True)),
            "default_database_source_id": str(raw_vanna.get("default_database_source_id") or "project_postgres"),
            "default_dialect": str(raw_vanna.get("default_dialect") or "PostgreSQL"),
            "query": effective_vanna.get("query", {}),
        },
        "analytics": {
            **config.get("analytics", {}),
            "database_qa": effective_database_qa,
        },
        "knowledge": {
            **config.get("knowledge", {}),
            **effective_knowledge_root,
            "mineru": {
                **config.get("knowledge", {}).get("mineru", {}),
                **effective_knowledge_mineru,
            },
            "multimodal_index": {
                **config.get("knowledge", {}).get("multimodal_index", {}),
                **effective_knowledge_index,
            },
            "search": config.get("knowledge", {}).get("search", {}),
            "parsers": {"items": display_parser_items},
        },
        "database": {
            **config.get("database", {}),
            **display_database,
        },
        "compression": config.get("compression", {}),
        "harness": config.get("harness", {}),
        "subagents": _normalize_subagent_config(config.get("subagents", {})),
    }
    result["database"].pop("password", None)
    return result


def _redact_database_url(value: str) -> str:
    if "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    authority, suffix = rest.split("@", 1)
    if ":" in authority:
        user = authority.split(":", 1)[0]
        return f"{scheme}://{user}:***@{suffix}"
    return value


def update_settings(updates: dict[str, Any]) -> dict[str, Any] | None:
    """Update settings from frontend, handling partial updates and API key logic.

    Returns ``None`` normally, or a dict of extra response fields (for example
    a migration warning when the database provider switches to SQLite) that
    the API layer merges into its response payload.
    """
    retired = {
        "ai_gateway",
        "gateway_llm",
        "fallback_llm",
        "fallback_embedding",
        "multimodal_embedding",
        "rag_mode",
        "memory_backend",
        "thinking_mode",
        "mem0",
        "smart_extractor",
        "subagent",
    }.intersection(updates)
    if retired:
        raise ValueError(
            "Retired settings are not accepted: " + ", ".join(sorted(retired))
        )
    config = load_config()

    if "analytics" in updates:
        analytics_update = updates["analytics"]
        if isinstance(analytics_update, dict):
            config.setdefault("analytics", {})
            database_qa_update = analytics_update.get("database_qa")
            if isinstance(database_qa_update, dict):
                config["analytics"].setdefault("database_qa", {})
                for key in (
                    "full_rows_token_budget",
                    "preview_rows_token_budget",
                    "profile_token_budget",
                    "full_rows_hard_row_cap",
                    "full_rows_hard_column_cap",
                    "max_cell_chars_for_llm",
                    "result_materialization_row_cap",
                    "query_timeout_ms",
                    "sql_generation_timeout_ms",
                    "result_store_ttl_hours",
                    "default_page_size",
                    "max_page_size",
                ):
                    if key in database_qa_update:
                        config["analytics"]["database_qa"][key] = database_qa_update[key]
                for key in (
                    "result_store_enabled",
                    "export_enabled",
                    "profile_enabled",
                ):
                    if key in database_qa_update:
                        config["analytics"]["database_qa"][key] = bool(database_qa_update[key])

    if "rag" in updates:
        rag_update = updates["rag"]
        if "rag" not in config:
            config["rag"] = {}
        for key in ("top_k", "similarity_threshold"):
            if key in rag_update:
                config["rag"][key] = rag_update[key]
        if isinstance(rag_update.get("hybrid"), dict):
            existing_hybrid = config["rag"].get("hybrid", {})
            if not isinstance(existing_hybrid, dict):
                existing_hybrid = {}
            for key in (
                "enabled",
                "mode",
                "text_vector_weight",
                "image_vector_weight",
                "bm25_weight",
                "candidate_top_k",
            ):
                if key in rag_update["hybrid"]:
                    existing_hybrid[key] = rag_update["hybrid"][key]
            config["rag"]["hybrid"] = existing_hybrid
        if isinstance(rag_update.get("rerank"), dict):
            existing_rerank = config["rag"].get("rerank", {})
            if not isinstance(existing_rerank, dict):
                existing_rerank = {}
            for key in ("enabled", "provider", "model", "top_n", "candidate_top_k"):
                if key in rag_update["rerank"]:
                    existing_rerank[key] = rag_update["rerank"][key]
            config["rag"]["rerank"] = existing_rerank
    if "vanna" in updates:
        vanna_update = updates["vanna"]
        if "vanna" not in config:
            config["vanna"] = {}
        if isinstance(vanna_update, dict):
            for key in ("enabled", "default_database_source_id", "default_dialect"):
                if key in vanna_update:
                    config["vanna"][key] = vanna_update[key]
            if isinstance(vanna_update.get("query"), dict):
                existing_query = config["vanna"].get("query", {})
                if not isinstance(existing_query, dict):
                    existing_query = {}
                query_update = vanna_update["query"]
                if "entity_top_k_default" in query_update:
                    try:
                        existing_query["entity_top_k_default"] = max(1, int(query_update.get("entity_top_k_default") or 10))
                    except (TypeError, ValueError):
                        existing_query["entity_top_k_default"] = 10
                if "entity_top_k_by_type" in query_update:
                    by_type: dict[str, int] = {}
                    if isinstance(query_update.get("entity_top_k_by_type"), dict):
                        for raw_key, raw_value in query_update["entity_top_k_by_type"].items():
                            entity_type = str(raw_key).strip()
                            if not entity_type:
                                continue
                            try:
                                by_type[entity_type] = max(1, int(raw_value))
                            except (TypeError, ValueError):
                                continue
                    existing_query["entity_top_k_by_type"] = by_type
                config["vanna"]["query"] = existing_query

    response_extra: dict[str, Any] | None = None
    if "database" in updates:
        database_update = updates["database"]
        if "database" not in config:
            config["database"] = {}
        if isinstance(database_update, dict):
            previous_provider = get_database_config().get("provider")
            requested_provider = ""
            requested_source = ""
            # Legacy GUI contract: mode sqlite | bundled | external. New
            # contract: provider sqlite | postgresql + source. Both are
            # accepted; provider/source win when both are present, and only
            # the new fields are persisted.
            legacy_mode = str(database_update.get("mode") or "").strip().lower()
            if legacy_mode == "sqlite":
                requested_provider, requested_source = "sqlite", "local_file"
            elif legacy_mode == "external":
                requested_provider, requested_source = "postgresql", "external"
            elif legacy_mode:
                requested_provider, requested_source = "postgresql", "bundled"
            if "provider" in database_update:
                provider_value = str(database_update.get("provider") or "").strip().lower()
                if provider_value not in {"sqlite", "postgresql"}:
                    raise ValueError("database.provider 必须是 sqlite 或 postgresql")
                requested_provider = provider_value
            if "source" in database_update:
                requested_source = str(database_update.get("source") or "").strip().lower()
            if requested_provider or requested_source or legacy_mode:
                if not requested_provider:
                    requested_provider = "sqlite" if requested_source == "local_file" else "postgresql"
                if not requested_source:
                    requested_source = "local_file" if requested_provider == "sqlite" else "external"
                config["database"].pop("mode", None)
                config["database"]["provider"] = requested_provider
                config["database"]["source"] = requested_source
                if previous_provider == "postgresql" and requested_provider == "sqlite":
                    # No hard refusal here (the CLI `database configure` owns
                    # the hard gate); the settings response must surface that
                    # the new SQLite catalog starts empty.
                    response_extra = {
                        "requires_migration": True,
                        "migration_warning": (
                            "已从 PostgreSQL 切换为 SQLite：新的 Catalog 为空，"
                            "原有数据不会自动迁移，请使用迁移流程完成数据搬迁。"
                        ),
                    }
            for key in ("host", "database", "username", "password", "url"):
                if key in database_update:
                    value = str(database_update.get(key) or "").strip()
                    if key == "password":
                        # The settings API never returns a stored password.
                        # Therefore an empty write means "unchanged", not
                        # "erase the credential". A future explicit clear
                        # action must use a separate, intentional contract.
                        if not value:
                            continue
                        from provider_registry import LocalCredentialStore

                        config["database"]["password_ref"] = LocalCredentialStore().put("database-config", value)
                        config["database"]["password"] = ""
                    else:
                        config["database"][key] = value
            if "port" in database_update:
                try:
                    config["database"]["port"] = int(database_update.get("port") or 5432)
                except (TypeError, ValueError):
                    config["database"]["port"] = 5432

    if "knowledge" in updates:
        knowledge_update = updates["knowledge"]
        if "knowledge" not in config:
            config["knowledge"] = {}
        if (
            isinstance(knowledge_update, dict)
            and "root_dir" in knowledge_update
            and not os.getenv("PUDDINGCLAW_KNOWLEDGE_DIR", "").strip()
        ):
            config["knowledge"]["root_dir"] = str(knowledge_update.get("root_dir") or "").strip()

        if isinstance(knowledge_update, dict) and "search" in knowledge_update:
            search_update = knowledge_update.get("search")
            if not isinstance(search_update, dict):
                raise ValueError("knowledge.search 必须是对象")
            existing_search = config["knowledge"].get("search", {})
            if not isinstance(existing_search, dict):
                existing_search = {}
            for key in ("enabled", "directories", "sources", "exclude"):
                if key in search_update:
                    existing_search[key] = search_update[key]
            config["knowledge"]["search"] = existing_search
        if isinstance(knowledge_update, dict) and "llm_wiki" in knowledge_update:
            llm_wiki_update = knowledge_update.get("llm_wiki")
            existing_llm_wiki = config["knowledge"].get("llm_wiki", {})
            if not isinstance(existing_llm_wiki, dict):
                existing_llm_wiki = {}
            if isinstance(llm_wiki_update, dict) and "compiler_agent" in llm_wiki_update:
                compiler_update = llm_wiki_update.get("compiler_agent")
                existing_compiler = existing_llm_wiki.get("compiler_agent", {})
                if not isinstance(existing_compiler, dict):
                    existing_compiler = {}
                if isinstance(compiler_update, dict) and "model_id" in compiler_update:
                    model_id = str(compiler_update.get("model_id") or "").strip()
                    if model_id:
                        from provider_registry import get_provider_registry

                        resolved = get_provider_registry().resolve_model(model_id)
                        if str(resolved.get("capability") or "") != "llm":
                            raise ValueError("LLM Wiki 编译 Agent 只能选择 LLM 模型")
                    if model_id:
                        existing_compiler["model_id"] = model_id
                    else:
                        existing_compiler.pop("model_id", None)
                if existing_compiler:
                    existing_llm_wiki["compiler_agent"] = existing_compiler
                else:
                    existing_llm_wiki.pop("compiler_agent", None)
            if isinstance(llm_wiki_update, dict) and "retrieval" in llm_wiki_update:
                retrieval_update = llm_wiki_update.get("retrieval")
                existing_retrieval = existing_llm_wiki.get("retrieval", {})
                if not isinstance(existing_retrieval, dict):
                    existing_retrieval = {}
                if isinstance(retrieval_update, dict) and "hybrid_enabled" in retrieval_update:
                    existing_retrieval["hybrid_enabled"] = bool(retrieval_update.get("hybrid_enabled"))
                existing_llm_wiki["retrieval"] = existing_retrieval
            if isinstance(llm_wiki_update, dict) and "gbrain" in llm_wiki_update:
                gbrain_update = llm_wiki_update.get("gbrain")
                existing_gbrain = existing_llm_wiki.get("gbrain", {})
                if not isinstance(existing_gbrain, dict):
                    existing_gbrain = {}
                if isinstance(gbrain_update, dict):
                    capability_by_key = {
                        "embedding_model_id": "text_embedding",
                        "think_model_id": "llm",
                    }
                    for key, capability in capability_by_key.items():
                        if key not in gbrain_update:
                            continue
                        model_id = str(gbrain_update.get(key) or "").strip()
                        if model_id:
                            from provider_registry import get_provider_registry

                            get_provider_registry().resolve_model(
                                model_id,
                                expected_capability=capability,
                            )
                        if model_id:
                            existing_gbrain[key] = model_id
                        else:
                            existing_gbrain.pop(key, None)
                if existing_gbrain:
                    existing_llm_wiki["gbrain"] = existing_gbrain
                else:
                    existing_llm_wiki.pop("gbrain", None)
            config["knowledge"]["llm_wiki"] = existing_llm_wiki
        if isinstance(knowledge_update, dict) and "mineru" in knowledge_update:
            mineru_update = knowledge_update["mineru"]
            existing_mineru = config["knowledge"].get("mineru", {})
            if not isinstance(existing_mineru, dict):
                existing_mineru = {}
            if isinstance(mineru_update, dict):
                if "runtime_output_dir" in mineru_update:
                    existing_mineru["runtime_output_dir"] = str(mineru_update.get("runtime_output_dir") or "").strip()
                if "base_url" in mineru_update:
                    existing_mineru["base_url"] = str(mineru_update.get("base_url") or "").strip()
                if "keep_runtime_output" in mineru_update:
                    existing_mineru["keep_runtime_output"] = bool(mineru_update.get("keep_runtime_output", False))
                if "connect_timeout_seconds" in mineru_update:
                    existing_mineru["connect_timeout_seconds"] = int(mineru_update.get("connect_timeout_seconds") or 10)
                if "read_timeout_seconds" in mineru_update:
                    existing_mineru["read_timeout_seconds"] = int(mineru_update.get("read_timeout_seconds") or 1800)
            config["knowledge"]["mineru"] = existing_mineru
        if isinstance(knowledge_update, dict) and "multimodal_index" in knowledge_update:
            mm_index_update = knowledge_update["multimodal_index"]
            existing = config["knowledge"].get("multimodal_index", {})
            if not isinstance(existing, dict):
                existing = {}
            for key in (
                "enabled",
                "vector_store",
                "milvus_uri",
                "text_collection",
                "image_collection",
                "bm25_enabled",
                "embedding_batch_size",
            ):
                if key in mm_index_update:
                    env_by_key = {
                        "enabled": "PUDDINGCLAW_ENABLE_MULTIMODAL_INDEX",
                        "vector_store": "PUDDINGCLAW_MULTIMODAL_VECTOR_STORE",
                        "milvus_uri": "PUDDINGCLAW_MILVUS_URI",
                        "text_collection": "PUDDINGCLAW_MILVUS_TEXT_COLLECTION",
                        "image_collection": "PUDDINGCLAW_MILVUS_IMAGE_COLLECTION",
                    }
                    env_name = env_by_key.get(key)
                    if env_name and os.getenv(env_name, "").strip():
                        continue
                    existing[key] = mm_index_update[key]
            config["knowledge"]["multimodal_index"] = existing

    if "compression" in updates:
        comp_update = updates["compression"]
        if "compression" not in config:
            config["compression"] = {}
        if "trigger_count" in comp_update:
            config["compression"]["trigger_count"] = comp_update["trigger_count"]
        if "deepagents" in comp_update:
            deepagents_update = comp_update["deepagents"]
            if not isinstance(deepagents_update, dict):
                raise ValueError("compression.deepagents must be an object")
            summary_update = deepagents_update.get("summarization")
            if isinstance(summary_update, dict):
                summary_update = dict(summary_update)
                summary_update.pop("summary_input_tokens", None)
                if "model_id" in summary_update:
                    model_id = summary_update.get("model_id")
                    if model_id is not None and not isinstance(model_id, str):
                        raise ValueError("摘要 / Compact 模型 ID 必须是字符串")
                    summary_update["model_id"] = str(model_id or "").strip()[:512]
                if "trigger_tokens" in summary_update:
                    trigger = int(summary_update["trigger_tokens"])
                    if not 10000 <= trigger <= 1000000:
                        raise ValueError("全局摘要阈值必须在 10,000 到 1,000,000 tokens 之间")
                    summary_update["trigger_tokens"] = trigger
                if "keep_tokens" in summary_update:
                    keep_tokens = int(summary_update["keep_tokens"])
                    effective_trigger = int(
                        summary_update.get(
                            "trigger_tokens",
                            config["compression"].get("deepagents", {})
                            .get("summarization", {})
                            .get("trigger_tokens", 272000),
                        )
                    )
                    if not 1000 <= keep_tokens < effective_trigger:
                        raise ValueError("摘要保留预算必须在 1,000 tokens 到摘要阈值之间")
                    summary_update["keep_tokens"] = keep_tokens
                deepagents_update = dict(deepagents_update)
                deepagents_update["summarization"] = summary_update
            tool_context_update = deepagents_update.get("tool_context")
            if isinstance(tool_context_update, dict):
                current_tool_context = (
                    config["compression"].get("deepagents", {}).get("tool_context", {})
                )
                merged_tool_context = _deep_merge(current_tool_context, tool_context_update)
                immediate_enabled = bool(
                    merged_tool_context.get("immediate_compaction_enabled", False)
                )
                single = int(merged_tool_context.get("single_tool_trigger_tokens", 8000))
                background = int(merged_tool_context.get("background_min_result_tokens", 1000))
                retain_tokens = int(merged_tool_context.get("retain_tool_context_tokens", 32000))
                if not 1000 <= single <= 20000:
                    raise ValueError("执行中单条工具阈值必须在 1,000 到 20,000 tokens 之间")
                if not 100 <= background <= 100000:
                    raise ValueError("静默压缩单条下限必须在 100 到 100,000 tokens 之间")
                if not 1000 <= retain_tokens <= 500000:
                    raise ValueError("工具上下文保留预算必须在 1,000 到 500,000 tokens 之间")
                tool_context_update = dict(tool_context_update)
                tool_context_update["immediate_compaction_enabled"] = immediate_enabled
                deepagents_update = dict(deepagents_update)
                deepagents_update["tool_context"] = tool_context_update
            existing_deepagents = config["compression"].get("deepagents", {})
            config["compression"]["deepagents"] = _deep_merge(existing_deepagents, deepagents_update)
            config["compression"]["deepagents"].setdefault("summarization", {}).pop(
                "summary_input_tokens", None
            )
        if "middleware" in comp_update:
            existing_mw = config["compression"].get("middleware", {})
            config["compression"]["middleware"] = _deep_merge(existing_mw, comp_update["middleware"])

    if "write_middleware" in updates:
        existing = config.get("write_middleware", {})
        config["write_middleware"] = _deep_merge(existing, updates["write_middleware"])

    if "harness" in updates:
        existing = config.get("harness", {})
        config["harness"] = _deep_merge(
            existing,
            _normalize_harness_update(updates["harness"]),
        )

    sub_update = updates.get("subagents")
    if sub_update is not None:
        if isinstance(sub_update, dict):
            config["subagents"] = _subagent_config_from_items(_subagent_items_for_display(sub_update))

    _strip_provider_credentials(config)
    save_config(config)
    return response_extra


def _normalize_harness_update(value: Any) -> dict[str, Any]:
    """Validate user-editable Harness settings and freeze managed invariants."""

    if not isinstance(value, dict):
        raise ValueError("harness settings must be an object")
    result = copy.deepcopy(value)

    prompt_cache = result.get("prompt_cache")
    if prompt_cache is not None:
        if not isinstance(prompt_cache, dict):
            raise ValueError("harness.prompt_cache must be an object")
        for key in (
            "trace_part_diagnostics",
            "ordered_system_sections",
            "tail_routing_message",
            "deterministic_session_projection",
            "stable_tool_schema",
        ):
            if key in prompt_cache and not isinstance(prompt_cache[key], bool):
                raise ValueError(f"harness.prompt_cache.{key} must be a boolean")

    model_resilience = result.get("model_resilience")
    if model_resilience is not None:
        if not isinstance(model_resilience, dict):
            raise ValueError("harness.model_resilience must be an object")
        transport_retry = model_resilience.get("transport_retry")
        if transport_retry is not None:
            if not isinstance(transport_retry, dict):
                raise ValueError("harness.model_resilience.transport_retry must be an object")
            if "enabled" in transport_retry and not isinstance(transport_retry["enabled"], bool):
                raise ValueError("harness.model_resilience.transport_retry.enabled must be a boolean")
            max_attempts = transport_retry.get("max_attempts", 2)
            if (
                not isinstance(max_attempts, int)
                or isinstance(max_attempts, bool)
                or not 1 <= max_attempts <= 5
            ):
                raise ValueError(
                    "harness.model_resilience.transport_retry.max_attempts must be in [1, 5]"
                )
            for key, default, maximum in (
                ("initial_delay_seconds", 0.25, 10.0),
                ("max_delay_seconds", 2.0, 60.0),
            ):
                item = transport_retry.get(key, default)
                if (
                    not isinstance(item, (int, float))
                    or isinstance(item, bool)
                    or not 0 <= float(item) <= maximum
                ):
                    raise ValueError(
                        f"harness.model_resilience.transport_retry.{key} must be in [0, {maximum:g}]"
                    )
                transport_retry[key] = float(item)
            if transport_retry["max_delay_seconds"] < transport_retry["initial_delay_seconds"]:
                raise ValueError(
                    "harness.model_resilience.transport_retry.max_delay_seconds must be "
                    ">= initial_delay_seconds"
                )
        terminal_response = model_resilience.get("terminal_response")
        if terminal_response is not None:
            if not isinstance(terminal_response, dict):
                raise ValueError("harness.model_resilience.terminal_response must be an object")
            if "enabled" in terminal_response and not isinstance(terminal_response["enabled"], bool):
                raise ValueError("harness.model_resilience.terminal_response.enabled must be a boolean")
            max_recovery_attempts = terminal_response.get("max_recovery_attempts", 1)
            if (
                not isinstance(max_recovery_attempts, int)
                or isinstance(max_recovery_attempts, bool)
                or not 0 <= max_recovery_attempts <= 3
            ):
                raise ValueError(
                    "harness.model_resilience.terminal_response.max_recovery_attempts must be in [0, 3]"
                )

    goals = result.get("goals")
    if goals is not None:
        if not isinstance(goals, dict):
            raise ValueError("harness.goals must be an object")
        goals["activation"] = "explicit_user_only"
        goals["default_enabled"] = False
        goals["auto_promote_from_run"] = False
        max_rounds = goals.get("max_rounds", 8)
        if (
            not isinstance(max_rounds, int)
            or isinstance(max_rounds, bool)
            or not 1 <= max_rounds <= 100
        ):
            raise ValueError("harness.goals.max_rounds must be in [1, 100]")

    completion = result.get("completion")
    if completion is not None:
        if not isinstance(completion, dict):
            raise ValueError("harness.completion must be an object")
        rubric = completion.get("rubric")
        if rubric is not None:
            if not isinstance(rubric, dict):
                raise ValueError("harness.completion.rubric must be an object")
            max_iterations = rubric.get("max_iterations", 2)
            if (
                not isinstance(max_iterations, int)
                or isinstance(max_iterations, bool)
                or not 1 <= max_iterations <= 20
            ):
                raise ValueError(
                    "harness.completion.rubric.max_iterations must be in [1, 20]"
                )
            max_stagnant_repairs = rubric.get("max_stagnant_repairs", 2)
            if (
                not isinstance(max_stagnant_repairs, int)
                or isinstance(max_stagnant_repairs, bool)
                or not 1 <= max_stagnant_repairs <= 20
            ):
                raise ValueError(
                    "harness.completion.rubric.max_stagnant_repairs must be in [1, 20]"
                )
            model = rubric.get("model", "")
            if not isinstance(model, str) or len(model.strip()) > 200:
                raise ValueError(
                    "harness.completion.rubric.model must be a string of at most 200 characters"
                )
            rubric["model"] = model.strip()
            rules = rubric.get("custom_rules", [])
            if not isinstance(rules, list) or len(rules) > 50:
                raise ValueError(
                    "harness.completion.rubric.custom_rules must contain at most 50 rules"
                )
            # Natural-language settings cannot create executable deterministic
            # logic. Deterministic verifiers are code-registered managed rules.
            allowed_verifiers = {"analytics", "llm_grader"}
            normalized_rules: list[dict[str, Any]] = []
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(f"custom_rules[{index}] must be an object")
                statement = str(rule.get("statement") or "").strip()
                if not statement or len(statement) > 1000:
                    raise ValueError(
                        f"custom_rules[{index}].statement must contain 1-1000 characters"
                    )
                verifier = str(rule.get("verifier") or "llm_grader")
                if verifier not in allowed_verifiers:
                    raise ValueError(
                        f"custom_rules[{index}].verifier is not registered"
                    )
                normalized_rules.append(
                    {
                        "id": str(rule.get("id") or f"custom_{index + 1}")[:100],
                        "enabled": bool(rule.get("enabled", True)),
                        "statement": statement,
                        "required": bool(rule.get("required", True)),
                        "verifier": verifier,
                    }
                )
            rubric["custom_rules"] = normalized_rules

    terminal = result.get("terminal")
    if terminal is not None:
        if not isinstance(terminal, dict):
            raise ValueError("harness.terminal must be an object")
        legacy_keys = sorted(_LEGACY_TERMINAL_EXECUTION_KEYS.intersection(terminal))
        if legacy_keys:
            raise UnsupportedTerminalExecutionConfig(
                "harness.terminal uses removed execution fields: "
                + ", ".join(legacy_keys)
                + "; choose execution_mode=spawn or kernel"
            )
        if terminal.get("execution_mode", "spawn") not in {"spawn", "kernel"}:
            raise ValueError(
                "harness.terminal.execution_mode must be spawn or kernel"
            )
        timeout = terminal.get("default_timeout_seconds", 120)
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 3600
        ):
            raise ValueError(
                "harness.terminal.default_timeout_seconds must be in [1, 3600]"
            )
        if "external_directory_writable_enabled" in terminal:
            terminal["external_directory_writable_enabled"] = bool(
                terminal["external_directory_writable_enabled"]
            )
        docker = terminal.get("docker")
        if docker is not None:
            if not isinstance(docker, dict):
                raise ValueError("harness.terminal.docker must be an object")
            docker["lifecycle"] = "project"
            for key, minimum, maximum in (
                ("probe_timeout_seconds", 1, 30),
                ("memory_limit_mb", 128, 131072),
                ("pids_limit", 16, 4096),
                ("idle_stop_minutes", 1, 10080),
            ):
                item = docker.get(key)
                if item is not None and (
                    not isinstance(item, int)
                    or isinstance(item, bool)
                    or not minimum <= item <= maximum
                ):
                    raise ValueError(
                        f"harness.terminal.docker.{key} must be in "
                        f"[{minimum}, {maximum}]"
                    )
            image = str(docker.get("image") or "").strip()
            if not image:
                raise ValueError("harness.terminal.docker.image cannot be empty")
            docker["image"] = image
            if "dependency_setup_enabled" in docker:
                docker["dependency_setup_enabled"] = bool(
                    docker["dependency_setup_enabled"]
                )
                docker["dependency_setup_opt_in_version"] = 1

    return result


def get_max_history_messages() -> int:
    """获取最大历史消息条数。"""
    return load_config().get("compression", {}).get("max_history_messages", 100)


def get_context_window() -> int:
    """获取当前模型的上下文窗口大小。"""
    return int(get_fallback_llm_config().get("context_window", 1000000))


def get_compaction_trigger_tokens() -> int:
    """获取 CompactionMiddleware 触发阈值（前端进度条分母）。"""
    return load_config().get("compression", {}).get("middleware", {}).get("compaction", {}).get("trigger_tokens", 500000)


def get_deepagents_summarization_config() -> dict[str, Any]:
    """Return Agent-mode summarization settings, isolated from legacy Chat."""

    config = load_config()
    result = dict(
        config
        .get("compression", {})
        .get("deepagents", {})
        .get(
            "summarization",
            {
                "enabled": True,
                "model_id": "",
                "trigger_tokens": 272000,
                "keep_tokens": 64000,
            },
        )
    )
    result["summary_input_tokens"] = _deepagents_summary_input_tokens(config)
    return result


def get_deepagents_tool_context_config() -> dict[str, Any]:
    """Return DeepAgents-only Tool Context settings."""

    return dict(
        load_config()
        .get("compression", {})
        .get("deepagents", {})
        .get(
            "tool_context",
            {
                "enabled": True,
                "immediate_compaction_enabled": False,
                "single_tool_trigger_tokens": 8000,
                "background_min_result_tokens": 1000,
                "retain_tool_context_tokens": 32000,
                "batch_size": 6,
                "max_concurrency": 4,
                "job_timeout_seconds": 120,
                "max_candidates_per_job": 48,
            },
        )
    )
