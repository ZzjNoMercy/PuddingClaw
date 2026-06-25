"""Global configuration management — JSON-based persistence."""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"

# Ch5 迁移提示标记：legacy summarization 阈值检测只提示一次（防日志噪声）
_LEGACY_WARN_SHOWN: bool = False

_DEFAULT_CONFIG: dict[str, Any] = {
    "rag_mode": False,
    "memory_backend": "markdown",  # "markdown" = MEMORY.md 原生方案, "mem0" = mem0 框架
    "ai_gateway": {
        # 覆盖地址：为空时由 backend 自动探测 Docker full profile 中的 Higress
        "base_url": "",
        "health_path": "/health",
        "fallback_to_direct": True,
    },
    "gateway_llm": {
        # Higress 可用时实际使用的模型；与 fallback_llm 分离，避免和 fallback 直连配置混淆
        "model": "deepseek-v4-flash",
    },
    "fallback_llm": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "temperature": 0.7,
        "max_tokens": 4096,
        "context_window": 1000000,
    },
    "fallback_embedding": {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
    },
    "rag": {
        "top_k": 3,
        "similarity_threshold": 0.7,
    },
    "compression": {
        "ratio": 0.5,
        # Must be less than MAX_HISTORY_MESSAGES (50) so compression fires before truncation
        "trigger_count": 15,
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
    "skills_router": {
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
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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
    if migrated:
        logger.info("[config] 已迁移 legacy llm/embedding -> fallback_llm/fallback_embedding")
    return data, migrated


def load_config() -> dict[str, Any]:
    """Load configuration from disk, returning defaults if missing."""
    if not CONFIG_FILE.exists():
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        data, migrated = _migrate_legacy_config(data)
        merged = _deep_merge(_DEFAULT_CONFIG, data)
        # 若发生迁移，立即回写，避免下次仍读取旧键
        if migrated:
            save_config(merged)
        return merged
    except Exception:
        return json.loads(json.dumps(_DEFAULT_CONFIG))


def save_config(config: dict[str, Any]) -> None:
    """Persist configuration to disk."""
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
    save_config(config)


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


def get_skills_router_config() -> dict:
    """Get skills router middleware configuration."""
    config = load_config()
    return config.get("skills_router", {
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


def get_fallback_llm_config() -> dict[str, Any]:
    """从 config.json 读取 fallback LLM 直连配置，fallback 到环境变量。

    返回 model/api_key/base_url 三个字段。
    temperature 由调用方自行指定（不同场景需要不同值）。
    """
    import os
    config = load_config()
    llm = config.get("fallback_llm", {})
    return {
        "provider": llm.get("provider", "deepseek"),
        "model": llm.get("model") or os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "api_key": llm.get("api_key") or os.getenv("DEEPSEEK_API_KEY", ""),
        "base_url": llm.get("base_url") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "temperature": float(llm.get("temperature", 0.7)),
        "max_tokens": int(llm.get("max_tokens", 4096)),
    }


def get_gateway_llm_config() -> dict[str, Any]:
    """读取 Gateway 模式下的 LLM 模型配置。

    与 fallback_llm 配置分离，避免 fallback 直连参数和网关路由模型混淆。
    若 gateway_llm.model 未设置，向后兼容 fallback 到 fallback_llm.model。
    """
    config = load_config()
    gateway_llm = config.get("gateway_llm", {})
    fallback_model = get_fallback_llm_config().get("model", "deepseek-chat")
    return {
        "model": gateway_llm.get("model") or fallback_model,
    }


def get_fallback_embedding_config() -> dict[str, Any]:
    """从 config.json 读取 fallback Embedding 直连配置，fallback 到环境变量。

    返回 model/api_key/api_base 三个字段。
    注意：api_base 是 OpenAIEmbedding 的参数名，与 config.json 中的 base_url 做了映射。
    """
    import os
    config = load_config()
    emb = config.get("fallback_embedding", {})
    return {
        "provider": emb.get("provider", "openai"),
        "model": emb.get("model") or os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        "api_key": emb.get("api_key") or os.getenv("OPENAI_API_KEY", ""),
        "api_base": emb.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://ai.devtool.tech/proxy/v1"),
    }


def mask_api_key(key: str) -> str:
    """Mask API key for display: sk-***...last4"""
    if not key or len(key) < 8:
        return "***"
    return f"{key[:3]}***...{key[-4:]}"


def get_settings_for_display() -> dict[str, Any]:
    """Get settings with masked API keys for frontend display."""
    import os

    from higress_config_reader import get_higress_routed_models

    config = load_config()
    effective_gateway = get_gateway_config()
    effective_llm = get_fallback_llm_config()
    effective_embedding = get_fallback_embedding_config()
    result = {
        "memory_backend": config.get("memory_backend", "markdown"),
        "ai_gateway": {
            **effective_gateway,
            "environment_override": bool(os.getenv("AI_GATEWAY_URL")),
            # 是否启用由 backend 自动探测决定，前端不再展示开关
            "enabled": bool(effective_gateway.get("base_url")),
            "routed_models": get_higress_routed_models(),
        },
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
        "rag": {
            "enabled": config.get("rag_mode", False),
            **config.get("rag", {}),
        },
        "compression": config.get("compression", {}),
    }
    # Remove raw API keys from response
    result["fallback_llm"].pop("api_key", None)
    result["fallback_embedding"].pop("api_key", None)
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

    if "gateway_llm" in updates:
        gateway_llm_update = updates["gateway_llm"]
        if "gateway_llm" not in config:
            config["gateway_llm"] = {}
        if "model" in gateway_llm_update:
            config["gateway_llm"]["model"] = gateway_llm_update["model"]

    if "fallback_llm" in updates:
        llm_update = updates["fallback_llm"]
        if "fallback_llm" not in config:
            config["fallback_llm"] = {}
        for key in ("provider", "model", "base_url", "temperature", "max_tokens"):
            if key in llm_update:
                config["fallback_llm"][key] = llm_update[key]
        # Only update API key if a non-empty value is provided
        if llm_update.get("api_key"):
            config["fallback_llm"]["api_key"] = llm_update["api_key"]

    if "fallback_embedding" in updates:
        emb_update = updates["fallback_embedding"]
        if "fallback_embedding" not in config:
            config["fallback_embedding"] = {}
        for key in ("provider", "model", "base_url"):
            if key in emb_update:
                config["fallback_embedding"][key] = emb_update[key]
        if emb_update.get("api_key"):
            config["fallback_embedding"]["api_key"] = emb_update["api_key"]

    if "rag" in updates:
        rag_update = updates["rag"]
        if "rag" not in config:
            config["rag"] = {}
        for key in ("top_k", "similarity_threshold"):
            if key in rag_update:
                config["rag"][key] = rag_update[key]
        if "enabled" in rag_update:
            config["rag_mode"] = rag_update["enabled"]

    if "compression" in updates:
        comp_update = updates["compression"]
        if "compression" not in config:
            config["compression"] = {}
        if "ratio" in comp_update:
            config["compression"]["ratio"] = comp_update["ratio"]
        if "trigger_count" in comp_update:
            config["compression"]["trigger_count"] = comp_update["trigger_count"]
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

    save_config(config)


def get_max_history_messages() -> int:
    """获取最大历史消息条数。"""
    return load_config().get("compression", {}).get("max_history_messages", 100)


def get_context_window() -> int:
    """获取当前模型的上下文窗口大小。"""
    return load_config().get("fallback_llm", {}).get("context_window", 1000000)


def get_compaction_trigger_tokens() -> int:
    """获取 CompactionMiddleware 触发阈值（前端进度条分母）。"""
    return load_config().get("compression", {}).get("middleware", {}).get("compaction", {}).get("trigger_tokens", 500000)
