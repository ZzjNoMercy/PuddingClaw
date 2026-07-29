"""Global configuration management — JSON-based persistence."""

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
DEEPAGENTS_SUMMARY_INPUT_CONTEXT_RATIO = 0.8

# Ch5 迁移提示标记：legacy summarization 阈值检测只提示一次（防日志噪声）
_LEGACY_WARN_SHOWN: bool = False

_DEFAULT_CONFIG: dict[str, Any] = {
    "rag_mode": False,
    "memory_backend": "markdown",  # "markdown" = MEMORY.md 原生方案, "mem0" = mem0 框架
    "thinking_mode": False,  # 开启后 gateway_llm / fallback_llm 使用 thinking 模型与参数
    "ai_gateway": {
        # 覆盖地址：为空时由 backend 自动探测 Docker full profile 中的 Higress
        "base_url": "",
        "health_path": "/health",
        "fallback_to_direct": True,
    },
    "gateway_llm": {
        # Higress 可用时实际使用的模型；与 fallback_llm 分离，避免和 fallback 直连配置混淆
        "model": "deepseek-v4-pro",
        # 思考模式开关开启时使用的模型与参数（DeepSeek 通过 extra_body 启用 thinking）
        "thinking": {
            "model": "deepseek-v4-pro",
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        },
    },
    "fallback_llm": {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "temperature": 0.7,
        "max_tokens": 4096,
        "context_window": 1000000,
        # 直连 fallback 的思考模式配置
        "thinking": {
            "model": "deepseek-v4-pro",
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        },
    },
    "fallback_embedding": {
        "provider": "qwen",
        "model": "text-embedding-v4",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "dimension": 1024,
        "batch_size": 20,
    },
    "multimodal_embedding": {
        "provider": "dashscope",
        "model": "qwen2.5-vl-embedding",
        "dimension": 1024,
        # DashScope multimodal embedding does not accept multiple same-type
        # inputs in one request. Keep the legacy key name for config
        # compatibility, but use it as provider request concurrency.
        "batch_size": 10,
        # qwen-vl embedding uses DashScope native API, not OpenAI-compatible /v1/embeddings.
        # Leave base_url empty for direct DashScope SDK mode. If a Higress native passthrough
        # route is configured, set base_url to the gateway root and keep route_path below.
        "base_url": "",
        "route_path": "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
        "api_key": "",
        "prefer_gateway": False,
    },
    "rag": {
        "top_k": 3,
        "similarity_threshold": 0.7,
        "hybrid": {
            "enabled": True,
            "mode": "reciprocal_rerank",
            "text_vector_weight": 0.45,
            "image_vector_weight": 0.35,
            "bm25_weight": 0.2,
            "candidate_top_k": 10,
        },
        "rerank": {
            "enabled": True,
            "provider": "dashscope",
            "model": "qwen3-vl-rerank",
            "top_n": 3,
            "candidate_top_k": 50,
            "base_url": "",
            "api_key": "",
        },
    },
    "database": {
        # Catalog database for knowledge documents, ingestion jobs and future
        # business facts. start-local-infra.sh detects local PostgreSQL and
        # writes either bundled or external into this section.
        #
        # Settings page is the normal desktop source of truth. Only the
        # PUDDINGCLAW_DATABASE_URL deployment escape hatch can override it.
        "mode": "bundled",  # bundled | external
        "host": "127.0.0.1",
        "port": 5432,
        "database": "puddingclaw",
        "username": "puddingclaw",
        "password": "puddingclaw",
        # Advanced escape hatch for deployments that need a full SQLAlchemy URL.
        # The frontend intentionally does not ask normal desktop users to write this.
        "url": "",
    },
    "knowledge": {
        # User-owned physical directory for local knowledge artifacts. Empty
        # means backend/knowledge for development; PUDDINGCLAW_KNOWLEDGE_DIR can
        # still override this temporarily.
        "root_dir": "",
        "mineru": {
            "base_url": "http://localhost:8002",
            # MinerU service writes its own runtime scratch files under
            # data/mineru-runtime/output when started by setup-mineru.py.
            # PuddingClaw copies final assets into the user knowledge directory,
            # so successful imports clean runtime output by default.
            "runtime_output_dir": "data/mineru-runtime/output",
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
            # Reuse app-level gateway/fallback config by default. Override only
            # when NL2SQL needs a dedicated model or endpoint.
            "reuse": "gateway_llm",
            "model": "",
            "base_url": "",
            "api_key": "",
            "temperature": 0.2,
            "max_tokens": 14000,
        },
        "embedding": {
            # Reuse fallback_embedding by default. For Qwen/OpenAI compatible
            # text embeddings, base_url is the /v1 root, not the /embeddings path.
            # Vanna training data is text-only, so it intentionally does not
            # reuse multimodal_embedding.
            "reuse": "fallback_embedding",
            "provider": "qwen",
            "model": "text-embedding-v4",
            "base_url": "",
            "api_key": "",
            "batch_size": 20,
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
            "entity_top_k_by_type": {},
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
            "query_timeout_ms": 30000,
            "result_store_enabled": True,
            "result_store_ttl_hours": 168,
            "default_page_size": 100,
            "max_page_size": 500,
            "export_enabled": False,
            "profile_enabled": True,
        },
    },
    "compression": {
        "ratio": 0.5,
        # Must be less than MAX_HISTORY_MESSAGES (50) so compression fires before truncation
        "trigger_count": 15,
        # Agent/DeepAgents uses its own built-in history offload + summarization
        # lifecycle. Keep this separate from the legacy Chat middleware stack.
        "deepagents": {
            "summarization": {
                "enabled": True,
                "trigger_tokens": 160000,
                "keep_messages": 20,
            },
            "tool_context": {
                "enabled": True,
                "immediate_compaction_enabled": False,
                "single_tool_trigger_tokens": 8000,
                "background_min_result_tokens": 1000,
                "keep_recent_tool_results": 12,
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
            "enabled": False,
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
        "model_call_limit": {
            "enabled": True,
            "run_limit": 50,
            "thread_limit": None,
            "exit_behavior": "end",
        },
        "completion": {
            "rubric": {
                "enabled": False,
                # Task classification, permission review and completion grading
                # share this non-thinking model unless a deployment overrides it.
                "model": "deepseek-v4-pro",
                "max_iterations": 2,
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
            "docker_enabled": False,
            "external_directory_writable_enabled": False,
            "on_unavailable": "fallback",
            "default_timeout_seconds": 120,
            "docker": {
                "connection": "",
                "context": "",
                "image": "puddingclaw/sandbox:python3.12-node22-chromium-v4",
                "cpu_limit": "2",
                "memory_limit_mb": 2048,
                "pids_limit": 256,
                "network_enabled": False,
                "dependency_setup_enabled": False,
                "dependency_setup_opt_in_version": 1,
                "lifecycle": "project",
                "idle_stop_minutes": 30,
            },
        },
    },
    "mem0": {
        "user_id": "default_user",
        "llm": {
            "provider": "openai",
            "config": {
                "model": "deepseek-chat",
                "openai_base_url": "https://api.deepseek.com/v1",
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": "text-embedding-3-small",
            },
        },
        "version": "v1.1",
    },
    "smart_extractor": {
        "throttle_every": 3,
        "score_threshold": 0.1,
        "stale_days": 30,
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
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base, preserving nested defaults."""
    # Callers mutate the returned settings object. A shallow copy would retain
    # references into _DEFAULT_CONFIG whenever an override omits a nested key,
    # making one settings save silently change process-wide defaults.
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _deepagents_summary_input_tokens(config: dict[str, Any]) -> int:
    """Derive the summary-call input ceiling from the configured model window."""

    raw_context_window = config.get("fallback_llm", {}).get("context_window", 1000000)
    try:
        context_window = max(1, int(raw_context_window))
    except (TypeError, ValueError):
        context_window = 1000000
    return max(1, int(context_window * DEEPAGENTS_SUMMARY_INPUT_CONTEXT_RATIO))


def _migrate_legacy_config(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """将旧版 llm / embedding 顶层键迁移为 fallback_llm / fallback_embedding。

    仅迁移一次：若存在旧键且新键不存在，则复制后删除旧键。
    """
    migrated = False
    if "llm" in data:
        if "fallback_llm" not in data:
            data["fallback_llm"] = data["llm"]
        del data["llm"]
        migrated = True
    if "embedding" in data:
        if "fallback_embedding" not in data:
            data["fallback_embedding"] = data["embedding"]
        del data["embedding"]
        migrated = True
    if isinstance(data.get("subagent"), dict) and "subagents" not in data:
        data["subagents"] = data["subagent"]
        del data["subagent"]
        migrated = True
    if isinstance(data.get("subagents"), dict):
        canonical = _subagent_config_from_items(_subagent_items_for_display(data["subagents"]))
        if canonical != data["subagents"]:
            data["subagents"] = canonical
            migrated = True
    summarization = data.get("compression", {}).get("deepagents", {}).get("summarization", {})
    if isinstance(summarization, dict) and "summary_input_tokens" in summarization:
        summarization.pop("summary_input_tokens", None)
        migrated = True
    docker = data.get("harness", {}).get("terminal", {}).get("docker", {})
    if isinstance(docker, dict) and docker.get("image") in {
        "python:3.12-slim",
        "puddingclaw/sandbox:python3.12-node22-v1",
        "puddingclaw/sandbox:python3.12-node22-v2",
        "puddingclaw/sandbox:python3.12-node22-curl-v3",
    }:
        docker["image"] = "puddingclaw/sandbox:python3.12-node22-chromium-v4"
        docker.setdefault("dependency_setup_enabled", False)
        migrated = True
    if isinstance(docker, dict):
        if (
            docker.get("dependency_setup_enabled") is True
            and docker.get("dependency_setup_opt_in_version") != 1
        ):
            # Early development builds briefly defaulted project dependency
            # preparation to true. Reset that legacy implicit opt-in once so
            # existing users return to the clean Python+Node sandbox default.
            docker["dependency_setup_enabled"] = False
            migrated = True
        if docker.get("dependency_setup_opt_in_version") != 1:
            docker["dependency_setup_opt_in_version"] = 1
            migrated = True
    if migrated:
        logger.info("[config] 已完成 legacy 配置迁移")
    return data, migrated


def load_config() -> dict[str, Any]:
    """Load configuration from disk, returning defaults if missing."""
    if not CONFIG_FILE.exists():
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        data, migrated = _migrate_legacy_config(data)
        merged = _deep_merge(_DEFAULT_CONFIG, data)
        # One-time provider migration imports all effective credentials into
        # the user-local CredentialStore before stripping repository-local
        # plaintext copies. It never deletes Higress data; that remains a
        # read-only migration source for rollback/audit.
        from provider_registry import get_provider_registry

        get_provider_registry().ensure_migrated(merged)
        credentials_migrated = _strip_provider_credentials(merged)
        # 若发生迁移，立即回写，避免下次仍读取旧键/明文凭证
        if migrated or credentials_migrated:
            save_config(merged)
        return merged
    except Exception:
        return json.loads(json.dumps(_DEFAULT_CONFIG))


def save_config(config: dict[str, Any]) -> None:
    """Persist non-secret configuration atomically with owner-only permissions."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=".config.", dir=CONFIG_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(config, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw_path, CONFIG_FILE)
        if os.name != "nt":
            os.chmod(CONFIG_FILE, 0o600)
    finally:
        if os.path.exists(raw_path):
            os.unlink(raw_path)


def _strip_provider_credentials(config: dict[str, Any]) -> bool:
    """Remove only migrated provider credentials from the legacy config file."""
    changed = False
    paths = [
        ("fallback_llm",),
        ("fallback_embedding",),
        ("multimodal_embedding",),
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
    return int(config.get("compression", {}).get("trigger_count", 15))


def get_compress_ratio() -> float:
    """Get compression ratio (proportion of messages to compress)."""
    config = load_config()
    return float(config.get("compression", {}).get("ratio", 0.5))


def get_rag_mode() -> bool:
    """Get current RAG mode setting."""
    return bool(load_config().get("rag_mode", False))


def set_rag_mode(enabled: bool) -> None:
    """Set RAG mode on/off."""
    config = load_config()
    config["rag_mode"] = enabled
    from provider_registry import get_provider_registry

    get_provider_registry().ensure_migrated(config)
    _strip_provider_credentials(config)
    save_config(config)


def get_rag_config() -> dict[str, Any]:
    """Read general RAG retrieval settings from config.json."""

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
        "enabled": bool(config.get("rag_mode", False)),
        "top_k": _positive_int(rag.get("top_k"), 3),
        "similarity_threshold": _float(rag.get("similarity_threshold"), 0.7),
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
        "text_vector_weight": _weight(hybrid.get("text_vector_weight", hybrid.get("vector_weight")), 0.45),
        "image_vector_weight": _weight(hybrid.get("image_vector_weight"), 0.35),
        "bm25_weight": _weight(hybrid.get("bm25_weight"), 0.2),
        "candidate_top_k": _positive_int(hybrid.get("candidate_top_k"), 10),
    }


def get_memory_backend() -> str:
    """获取长期记忆后端类型：'markdown' 或 'mem0'。"""
    backend = load_config().get("memory_backend", "markdown")
    if backend not in ("markdown", "mem0"):
        return "markdown"
    return backend


def get_mem0_config() -> dict[str, Any]:
    """构建 mem0 Memory.from_config() 所需的配置字典。

    复用 fallback_llm 和 fallback_embedding 的 api_key，避免用户配置两套凭证。
    """
    import copy
    import os
    config = load_config()
    mem0_cfg = copy.deepcopy(config.get("mem0", {}))
    llm_cfg = config.get("fallback_llm", {})
    emb_cfg = config.get("fallback_embedding", {})

    # 复用已有的 api_key（llm → mem0.llm, embedding → mem0.embedder）
    mem0_llm = mem0_cfg.get("llm", {})
    mem0_llm_config = mem0_llm.get("config", {})
    mem0_llm_config["api_key"] = llm_cfg.get("api_key") or os.getenv("DEEPSEEK_API_KEY", "")

    mem0_emb = mem0_cfg.get("embedder", {})
    mem0_emb_config = mem0_emb.get("config", {})
    # 优先用 config.json 显式配置的 api_key；为空时才从环境变量读取
    if not mem0_emb_config.get("api_key"):
        openai_key = emb_cfg.get("api_key") or os.getenv("OPENAI_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")
        if openai_key:
            mem0_emb_config["api_key"] = openai_key
    # base_url 也支持环境覆盖
    if not mem0_emb_config.get("openai_base_url"):
        openai_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or emb_cfg.get("base_url")
        if openai_base:
            mem0_emb_config["openai_base_url"] = openai_base

    result = {
        "llm": {
            "provider": mem0_llm.get("provider", "openai"),
            "config": mem0_llm_config,
        },
        "embedder": {
            "provider": mem0_emb.get("provider", "openai"),
            "config": mem0_emb_config,
        },
        "version": mem0_cfg.get("version", "v1.1"),
    }

    # vector_store 配置透传（支持 Milvus / Qdrant 等）
    if "vector_store" in mem0_cfg:
        vs = mem0_cfg["vector_store"]
        result["vector_store"] = {
            "provider": vs.get("provider", "qdrant"),
            "config": dict(vs.get("config", {})),
        }

    return result


def get_mem0_user_id() -> str:
    """获取 mem0 的默认 user_id。"""
    return load_config().get("mem0", {}).get("user_id", "default_user")


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


def get_smart_extractor_config() -> dict[str, int | float]:
    """获取 SmartExtractor 配置：throttle_every / score_threshold / stale_days。"""
    config = load_config()
    se = config.get("smart_extractor", {})
    return {
        "throttle_every": int(se.get("throttle_every", 3)),
        "score_threshold": float(se.get("score_threshold", 0.1)),
        "stale_days": int(se.get("stale_days", 30)),
    }


def get_gateway_config() -> dict[str, Any]:
    """读取 AI Gateway 配置，环境变量优先于持久化设置。

    当 base_url 为空且未配置环境变量时，backend 会自动探测默认地址。
    """
    import os

    gateway = load_config().get("ai_gateway", {})
    env_url = os.getenv("AI_GATEWAY_URL", "").strip()
    configured_url = gateway.get("base_url", "").strip()
    return {
        "base_url": env_url or configured_url or "",
        "health_path": gateway.get("health_path", "/health"),
        "fallback_to_direct": bool(gateway.get("fallback_to_direct", True)),
    }


def get_fallback_llm_config(
    *,
    thinking_enabled_override: bool | None = None,
    binding: str = "agent",
    model_id_override: str | None = None,
    thinking_level: str | None = None,
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
        registry.resolve_model(model_id_override, legacy_config=config)
        if model_id_override
        else registry.resolve_binding(binding, legacy_config=config)
    )
    if model_id_override or thinking_level is not None:
        from llm.thinking_mapping import map_thinking_request

        mapped_thinking = map_thinking_request(
            resolved.get("thinking_profile", {}),
            thinking_level,
        )
        return {
            "provider": resolved.get("provider_id", "deepseek"),
            "model": resolved.get("name") or "deepseek-chat",
            "api_key": resolved.get("api_key", ""),
            "base_url": resolved.get("base_url", "https://api.deepseek.com"),
            "protocol": resolved.get("protocol", "deepseek"),
            "model_id": resolved.get("id", ""),
            "temperature": float(resolved.get("temperature", 0.7)),
            "max_tokens": int(resolved.get("max_tokens", 4096)),
            "context_window": int(resolved.get("context_window", 1000000)),
            **mapped_thinking,
        }
    thinking = resolved.get("thinking", {})
    thinking_enabled = (
        bool(config.get("thinking_mode", False))
        if thinking_enabled_override is None
        else thinking_enabled_override
    )
    base_model = resolved.get("name") or "deepseek-chat"
    effective_model = thinking["model"] if thinking_enabled and thinking.get("model") else base_model
    return {
        "provider": resolved.get("provider_id", "deepseek"),
        "model": effective_model,
        "api_key": resolved.get("api_key", ""),
        "base_url": resolved.get("base_url", "https://api.deepseek.com"),
        "protocol": resolved.get("protocol", "deepseek"),
        "model_id": resolved.get("id", ""),
        "temperature": float(resolved.get("temperature", 0.7)),
        "max_tokens": int(resolved.get("max_tokens", 4096)),
        "context_window": int(resolved.get("context_window", 1000000)),
        "reasoning_effort": thinking.get("reasoning_effort") if thinking_enabled else None,
        "extra_body": thinking.get("extra_body") if thinking_enabled else None,
        "thinking_enabled": thinking_enabled,
        "thinking_level": thinking.get("reasoning_effort") if thinking_enabled else None,
    }


def get_gateway_llm_config(
    *,
    thinking_enabled_override: bool | None = None,
) -> dict[str, Any]:
    """读取 Gateway 模式下的 LLM 模型配置。

    与 fallback_llm 配置分离，避免 fallback 直连参数和网关路由模型混淆。
    若 gateway_llm.model 未设置，向后兼容 fallback 到 fallback_llm.model。
    当 thinking_mode 开启时，使用 thinking 下的模型与参数。
    """
    config = load_config()
    gateway_llm = config.get("gateway_llm", {})
    thinking = gateway_llm.get("thinking", {})
    thinking_enabled = (
        bool(config.get("thinking_mode", False))
        if thinking_enabled_override is None
        else thinking_enabled_override
    )
    fallback_model = get_fallback_llm_config(
        thinking_enabled_override=thinking_enabled_override,
    ).get("model", "deepseek-chat")
    base_model = gateway_llm.get("model") or fallback_model
    effective_model = thinking["model"] if thinking_enabled and thinking.get("model") else base_model
    return {
        "model": effective_model,
        "reasoning_effort": thinking.get("reasoning_effort") if thinking_enabled else None,
        "extra_body": thinking.get("extra_body") if thinking_enabled else None,
    }


def get_fallback_embedding_config() -> dict[str, Any]:
    """从 config.json 读取 fallback Embedding 直连配置，fallback 到环境变量。

    返回 model/api_key/api_base 三个字段。
    注意：api_base 是 OpenAIEmbedding 的参数名，与 config.json 中的 base_url 做了映射。
    """
    config = load_config()
    from provider_registry import get_provider_registry

    resolved = get_provider_registry().resolve_binding("text_embedding", legacy_config=config)
    return {
        "provider": resolved.get("provider_id", "dashscope"),
        "model": resolved.get("name", "text-embedding-v4"),
        "api_key": resolved.get("api_key", ""),
        "api_base": resolved.get("base_url", "https://api.openai.com/v1"),
        "protocol": resolved.get("protocol", "openai_compatible"),
        "model_id": resolved.get("id", ""),
        "dimension": int(resolved.get("dimension", 1024)),
        "batch_size": max(1, int(resolved.get("batch_size", 20))),
    }


def get_multimodal_embedding_config() -> dict[str, Any]:
    """Resolve the Model Services binding plus knowledge-index concurrency."""

    config = load_config()
    from provider_registry import get_provider_registry

    resolved = get_provider_registry().resolve_binding("multimodal_embedding", legacy_config=config)
    # Model identity, endpoint, dimension and credential belong to Model
    # Services. Request concurrency is an indexing runtime concern and remains
    # configurable from Knowledge settings for compatibility with existing
    # installations.
    runtime = config.get("multimodal_embedding", {})
    return {
        "provider": resolved.get("provider_id", "dashscope"),
        "model": resolved.get("name", "qwen2.5-vl-embedding"),
        "dimension": int(resolved.get("dimension", 1024)),
        "batch_size": max(1, int(runtime.get("batch_size", resolved.get("concurrency", 10)))),
        "api_key": resolved.get("api_key", ""),
        "base_url": resolved.get("base_url", "https://dashscope.aliyuncs.com"),
        "route_path": resolved.get("route_path", "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"),
        "protocol": resolved.get("protocol", "dashscope_multimodal_embedding"),
        "model_id": resolved.get("id", ""),
        "prefer_gateway": False,
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
        "base_url": rerank.get("base_url", ""),
        "api_key": rerank.get("api_key", ""),
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
    output_dir = str(mineru.get("runtime_output_dir") or "data/mineru-runtime/output").strip()
    return {
        "base_url": str(mineru.get("base_url") or "http://localhost:8002").strip(),
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
    fallback_llm = get_fallback_llm_config()
    fallback_embedding = get_fallback_embedding_config()

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

    # ``gateway_llm`` is a legacy storage label. Both legacy reuse modes now
    # resolve through the same explicit direct Provider binding.
    llm_reuse = str(llm.get("reuse") or "vanna_llm")
    default_llm_model = fallback_llm.get("model", "")
    default_llm_base = fallback_llm.get("base_url", "")
    default_llm_key = fallback_llm.get("api_key", "")

    embedding_reuse = str(embedding.get("reuse") or "fallback_embedding")
    default_embedding_model = fallback_embedding.get("model", "")
    default_embedding_base = fallback_embedding.get("base_url", "")
    default_embedding_key = fallback_embedding.get("api_key", "")

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
            "provider": str(embedding.get("provider") or fallback_embedding.get("provider") or "qwen"),
            "model": str(embedding.get("model") or default_embedding_model),
            "base_url": str(embedding.get("base_url") or default_embedding_base),
            "api_key": str(embedding.get("api_key") or default_embedding_key),
            "batch_size": max(1, int(embedding.get("batch_size") or fallback_embedding.get("batch_size") or 10)),
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
        "query_timeout_ms": _positive_int(database_qa.get("query_timeout_ms"), 30000, minimum=1000, maximum=300000),
        "result_store_enabled": bool(database_qa.get("result_store_enabled", True)),
        "result_store_ttl_hours": _positive_int(database_qa.get("result_store_ttl_hours"), 168, maximum=24 * 365),
        "default_page_size": _positive_int(database_qa.get("default_page_size"), 100, maximum=5000),
        "max_page_size": _positive_int(database_qa.get("max_page_size"), 500, maximum=10000),
        "export_enabled": bool(database_qa.get("export_enabled", False)),
        "profile_enabled": bool(database_qa.get("profile_enabled", True)),
    }


def get_database_config() -> dict[str, Any]:
    """Read catalog database connection config.

    Settings page / config.json is the normal desktop source of truth. The only
    environment override is PUDDINGCLAW_DATABASE_URL, reserved for deployment or
    CI. Generic DATABASE_URL / POSTGRES_URL are intentionally ignored here:
    they are too easy to inherit from Docker shells and make the UI look wrong.
    """

    import os
    from urllib.parse import quote

    env_url = (os.getenv("PUDDINGCLAW_DATABASE_URL") or "").strip()
    database = load_config().get("database", {})
    configured_url = str(database.get("url", "") or "").strip()
    mode = str(database.get("mode", "bundled") or "bundled").strip() or "bundled"
    host = str(database.get("host", "127.0.0.1") or "127.0.0.1").strip()
    port = int(database.get("port") or 5432)
    db_name = str(database.get("database", "puddingclaw") or "puddingclaw").strip()
    username = str(database.get("username", "puddingclaw") or "puddingclaw").strip()
    raw_password = database.get("password")
    password = "puddingclaw" if raw_password is None else str(raw_password)
    assembled_url = ""
    if mode in {"bundled", "external"}:
        assembled_url = (
            "postgresql+asyncpg://"
            f"{quote(username)}:{quote(password)}@{host}:{port}/{quote(db_name)}"
        )
    effective_config_url = configured_url or assembled_url
    return {
        "mode": mode,
        "host": host,
        "port": port,
        "database": db_name,
        "username": username,
        "password": password,
        "url": env_url or effective_config_url,
        "configured_url": configured_url,
        "configured_by": (
            "environment"
            if env_url
            else "config.json"
            if effective_config_url
            else "default"
        ),
        "environment_override": bool(env_url),
    }


def mask_api_key(key: str) -> str:
    """Mask API key for display: sk-***...last4"""
    if not key or len(key) < 8:
        return "***"
    return f"{key[:3]}***...{key[-4:]}"


_SUBAGENT_RESERVED_KEYS = {"enabled", "items"}


def _subagent_items_for_display(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return display-friendly subagent items from canonical or legacy config."""
    if not raw:
        return []

    # Legacy UI format: {"items": [{"name": "...", ...}]}
    if "items" in raw:
        return [item for item in raw.get("items", []) if isinstance(item, dict)]

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
    import os
    from provider_registry import get_provider_registry

    config = load_config()
    effective_gateway = get_gateway_config()
    effective_llm = get_fallback_llm_config()
    effective_embedding = get_fallback_embedding_config()
    effective_multimodal_embedding = get_multimodal_embedding_config()
    effective_knowledge_index = get_knowledge_multimodal_index_config()
    effective_knowledge_root = get_knowledge_root_config()
    effective_knowledge_mineru = get_knowledge_mineru_config()
    effective_database = get_database_config()
    effective_vanna = get_vanna_config()
    effective_database_qa = get_database_qa_config()
    raw_vanna = config.get("vanna", {})
    provider_registry = get_provider_registry().display(legacy_config=config)
    result = {
        "memory_backend": config.get("memory_backend", "markdown"),
        "thinking_mode": bool(config.get("thinking_mode", False)),
        "ai_gateway": {
            **effective_gateway,
            "environment_override": bool(os.getenv("AI_GATEWAY_URL")),
            # 是否启用由 backend 自动探测决定，前端不再展示开关
            "enabled": bool(effective_gateway.get("base_url")),
            # Compatibility-only field; runtime and the new settings UI use
            # provider_registry instead of scanning Higress files.
            "routed_models": [],
        },
        "provider_registry": provider_registry,
        "gateway_llm": {
            **config.get("gateway_llm", {}),
            "model": get_gateway_llm_config().get("model", effective_llm.get("model", "deepseek-chat")),
        },
        "fallback_llm": {
            **config.get("fallback_llm", {}),
            "api_key_masked": mask_api_key(effective_llm.get("api_key", "")),
        },
        "fallback_embedding": {
            **config.get("fallback_embedding", {}),
            "api_key_masked": mask_api_key(effective_embedding.get("api_key", "")),
        },
        "multimodal_embedding": {
            **config.get("multimodal_embedding", {}),
            "api_key_masked": mask_api_key(effective_multimodal_embedding.get("api_key", "")),
            "effective_model": effective_multimodal_embedding.get("model"),
            "effective_dimension": effective_multimodal_embedding.get("dimension"),
            "gateway_route_required": True,
            "openai_compatible": False,
        },
        "rag": {
            "enabled": config.get("rag_mode", False),
            **config.get("rag", {}),
        },
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
        },
        "database": {
            **config.get("database", {}),
            **effective_database,
        },
        "compression": config.get("compression", {}),
        "harness": config.get("harness", {}),
        "subagents": _normalize_subagent_config(config.get("subagents", {})),
    }
    # Remove raw API keys from response
    result["fallback_llm"].pop("api_key", None)
    result["fallback_embedding"].pop("api_key", None)
    result["multimodal_embedding"].pop("api_key", None)
    return result


def update_settings(updates: dict[str, Any]) -> None:
    """Update settings from frontend, handling partial updates and API key logic."""
    config = load_config()

    if "ai_gateway" in updates:
        gateway_update = updates["ai_gateway"]
        if "ai_gateway" not in config:
            config["ai_gateway"] = {}
        for key in ("base_url", "health_path", "fallback_to_direct"):
            if key in gateway_update:
                config["ai_gateway"][key] = gateway_update[key]

    if "thinking_mode" in updates:
        config["thinking_mode"] = bool(updates["thinking_mode"])

    if "gateway_llm" in updates:
        gateway_llm_update = updates["gateway_llm"]
        if "gateway_llm" not in config:
            config["gateway_llm"] = {}
        if "model" in gateway_llm_update:
            config["gateway_llm"]["model"] = gateway_llm_update["model"]
        if "thinking" in gateway_llm_update:
            config["gateway_llm"]["thinking"] = gateway_llm_update["thinking"]

    if "fallback_llm" in updates:
        llm_update = updates["fallback_llm"]
        if "fallback_llm" not in config:
            config["fallback_llm"] = {}
        for key in ("provider", "model", "base_url", "temperature", "max_tokens", "context_window"):
            if key in llm_update:
                config["fallback_llm"][key] = llm_update[key]
        if "context_window" in llm_update:
            context_window = int(llm_update["context_window"])
            if not 10000 <= context_window <= 10000000:
                raise ValueError("模型上下文窗口必须在 10,000 到 10,000,000 tokens 之间")
            config["fallback_llm"]["context_window"] = context_window
        if "thinking" in llm_update:
            config["fallback_llm"]["thinking"] = llm_update["thinking"]
        # Only update API key if a non-empty value is provided
        if llm_update.get("api_key"):
            config["fallback_llm"]["api_key"] = llm_update["api_key"]

    if "fallback_embedding" in updates:
        emb_update = updates["fallback_embedding"]
        if "fallback_embedding" not in config:
            config["fallback_embedding"] = {}
        for key in ("provider", "model", "base_url", "dimension", "batch_size"):
            if key in emb_update:
                config["fallback_embedding"][key] = emb_update[key]
        if emb_update.get("api_key"):
            config["fallback_embedding"]["api_key"] = emb_update["api_key"]

    if "multimodal_embedding" in updates:
        mm_update = updates["multimodal_embedding"]
        if "multimodal_embedding" not in config:
            config["multimodal_embedding"] = {}
        for key in ("provider", "model", "base_url", "route_path", "dimension", "batch_size", "prefer_gateway"):
            if key in mm_update:
                config["multimodal_embedding"][key] = mm_update[key]
        if mm_update.get("api_key"):
            config["multimodal_embedding"]["api_key"] = mm_update["api_key"]

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
                    "query_timeout_ms",
                    "result_store_ttl_hours",
                    "default_page_size",
                    "max_page_size",
                ):
                    if key in database_qa_update:
                        config["analytics"]["database_qa"][key] = database_qa_update[key]
                for key in ("result_store_enabled", "export_enabled", "profile_enabled"):
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
                "vector_weight",
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
            for key in ("enabled", "provider", "model", "top_n", "candidate_top_k", "base_url"):
                if key in rag_update["rerank"]:
                    existing_rerank[key] = rag_update["rerank"][key]
            if rag_update["rerank"].get("api_key"):
                existing_rerank["api_key"] = rag_update["rerank"]["api_key"]
            config["rag"]["rerank"] = existing_rerank
        if "enabled" in rag_update:
            config["rag_mode"] = rag_update["enabled"]

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

    if "database" in updates:
        database_update = updates["database"]
        if "database" not in config:
            config["database"] = {}
        if isinstance(database_update, dict):
            if "mode" in database_update:
                mode = str(database_update.get("mode") or "bundled").strip() or "bundled"
                config["database"]["mode"] = "external" if mode == "external" else "bundled"
            for key in ("host", "database", "username", "password", "url"):
                if key in database_update:
                    config["database"][key] = str(database_update.get(key) or "").strip()
            if "port" in database_update:
                try:
                    config["database"]["port"] = int(database_update.get("port") or 5432)
                except (TypeError, ValueError):
                    config["database"]["port"] = 5432

    if "knowledge" in updates:
        knowledge_update = updates["knowledge"]
        if "knowledge" not in config:
            config["knowledge"] = {}
        if isinstance(knowledge_update, dict) and "root_dir" in knowledge_update:
            config["knowledge"]["root_dir"] = str(knowledge_update.get("root_dir") or "").strip()
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
            for key in ("enabled", "vector_store", "milvus_uri", "text_collection", "image_collection"):
                if key in mm_index_update:
                    existing[key] = mm_index_update[key]
            existing["overwrite"] = False
            config["knowledge"]["multimodal_index"] = existing

    if "compression" in updates:
        comp_update = updates["compression"]
        if "compression" not in config:
            config["compression"] = {}
        if "ratio" in comp_update:
            config["compression"]["ratio"] = comp_update["ratio"]
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
                if "trigger_tokens" in summary_update:
                    trigger = int(summary_update["trigger_tokens"])
                    if not 10000 <= trigger <= 1000000:
                        raise ValueError("全局摘要阈值必须在 10,000 到 1,000,000 tokens 之间")
                    summary_update["trigger_tokens"] = trigger
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
                keep_recent = int(merged_tool_context.get("keep_recent_tool_results", 12))
                if not 1000 <= single <= 20000:
                    raise ValueError("执行中单条工具阈值必须在 1,000 到 20,000 tokens 之间")
                if not 100 <= background <= 100000:
                    raise ValueError("静默压缩单条下限必须在 100 到 100,000 tokens 之间")
                if not 1 <= keep_recent <= 100:
                    raise ValueError("保留最近工具结果必须在 1 到 100 条之间")
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

    if "memory_backend" in updates:
        backend = updates["memory_backend"]
        if backend in ("markdown", "mem0"):
            config["memory_backend"] = backend

    if "write_middleware" in updates:
        existing = config.get("write_middleware", {})
        config["write_middleware"] = _deep_merge(existing, updates["write_middleware"])

    if "harness" in updates:
        existing = config.get("harness", {})
        config["harness"] = _deep_merge(
            existing,
            _normalize_harness_update(updates["harness"]),
        )

    sub_update = updates.get("subagents", updates.get("subagent"))
    if sub_update is not None:
        if isinstance(sub_update, dict):
            config["subagents"] = _subagent_config_from_items(_subagent_items_for_display(sub_update))
            config.pop("subagent", None)

    from provider_registry import get_provider_registry

    get_provider_registry().ensure_migrated(config)
    _strip_provider_credentials(config)
    save_config(config)


def _normalize_harness_update(value: Any) -> dict[str, Any]:
    """Validate user-editable Harness settings and freeze managed invariants."""

    if not isinstance(value, dict):
        raise ValueError("harness settings must be an object")
    result = copy.deepcopy(value)

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
        if terminal.get("on_unavailable", "fallback") not in {"fallback", "deny"}:
            raise ValueError(
                "harness.terminal.on_unavailable must be fallback or deny"
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
                "trigger_tokens": 160000,
                "keep_messages": 20,
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
                "keep_recent_tool_results": 12,
                "batch_size": 6,
                "max_concurrency": 4,
                "job_timeout_seconds": 120,
                "max_candidates_per_job": 48,
            },
        )
    )
