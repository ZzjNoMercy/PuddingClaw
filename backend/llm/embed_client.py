"""统一 Embedding 模型入口。

禁止业务代码设置 LlamaIndex 全局 Settings.embed_model；
统一通过 get_embedding_model() 显式获取 embedding model 并注入到需要的地方。
"""

from __future__ import annotations

import logging
from llama_index.embeddings.openai import OpenAIEmbedding

import capabilities
from config import get_embedding_config, get_gateway_config

logger = logging.getLogger(__name__)


def get_embedding_model() -> OpenAIEmbedding:
    """获取配置好的 OpenAI-compatible Embedding 模型。

    如果 AI_GATEWAY_URL 可用，优先通过网关路由 embedding 请求；
    否则使用 config.json 中 embedding.base_url 直连。
    """
    cfg = get_embedding_config()
    gateway = get_gateway_config()
    use_gateway = False
    if gateway.get("base_url"):
        try:
            use_gateway = capabilities.detect_capabilities_sync().ai_gateway.available
        except Exception as exc:  # noqa: BLE001
            logger.warning("[EmbedClient] gateway detection failed: %s", exc)

    api_base = gateway.get("base_url") if use_gateway else cfg.get("api_base", "https://api.openai.com/v1")
    # Higress 不承担客户端鉴权；上下游始终使用所选 Embedding Provider 的凭证。
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "text-embedding-3-small")

    logger.debug("[EmbedClient] api_base=%s model=%s", api_base, model)
    return OpenAIEmbedding(
        model=model,
        api_key=api_key,
        api_base=api_base,
    )
