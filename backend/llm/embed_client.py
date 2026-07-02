"""统一 Embedding 模型入口。

禁止业务代码设置 LlamaIndex 全局 Settings.embed_model；
统一通过 get_embedding_model() 显式获取 embedding model 并注入到需要的地方。
"""

from __future__ import annotations

import logging
from llama_index.embeddings.openai import OpenAIEmbedding

import capabilities
from config import get_fallback_embedding_config, get_gateway_config

logger = logging.getLogger(__name__)


def get_embedding_model() -> OpenAIEmbedding:
    """获取配置好的 OpenAI-compatible Embedding 模型。

    如果 AI_GATEWAY_URL 可用，优先通过网关路由 embedding 请求；
    否则使用 config.json 中 embedding.base_url 直连。
    """
    cfg = get_fallback_embedding_config()
    gateway = get_gateway_config()
    use_gateway = False
    if gateway.get("base_url"):
        try:
            use_gateway = capabilities.detect_capabilities_sync().ai_gateway.available
        except Exception as exc:  # noqa: BLE001
            logger.warning("[EmbedClient] gateway detection failed: %s", exc)

    api_base = gateway.get("base_url") if use_gateway else cfg.get("api_base", "https://api.openai.com/v1")
    model = cfg.get("model", "text-embedding-3-small")

    # When routing through Higress AI Gateway, the gateway's ai-proxy plugin
    # replaces the Authorization header with the configured upstream provider
    # token. The client only needs a non-empty placeholder unless Higress
    # consumer auth is enabled on the route.
    api_key = cfg.get("api_key", "")
    if use_gateway and not api_key:
        api_key = "higress-placeholder"

    logger.debug("[EmbedClient] api_base=%s model=%s", api_base, model)

    # LlamaIndex's OpenAIEmbedding validates the model name against OpenAI's
    # enum. For OpenAI-compatible providers (e.g. DashScope) that expose custom
    # names such as "text-embedding-v4", we initialize with a known-valid name
    # and override the engine names that are actually sent to the API.
    from llama_index.embeddings.openai import OpenAIEmbeddingModelType

    valid_models = {m.value for m in OpenAIEmbeddingModelType}
    init_model = model if model in valid_models else "text-embedding-3-small"
    embed_model = OpenAIEmbedding(
        model=init_model,
        api_key=api_key,
        api_base=api_base,
    )
    if init_model != model:
        embed_model._query_engine = model
        embed_model._text_engine = model
    return embed_model
