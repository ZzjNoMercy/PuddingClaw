"""统一 Embedding 模型入口。

禁止业务代码设置 LlamaIndex 全局 Settings.embed_model；
统一通过 get_embedding_model() 显式获取 embedding model 并注入到需要的地方。
"""

from __future__ import annotations

import logging

from llama_index.embeddings.openai import OpenAIEmbedding

from config import get_fallback_embedding_config
from llm.embedding_limits import clamp_embedding_batch_size

logger = logging.getLogger(__name__)


def get_embedding_model() -> OpenAIEmbedding:
    """获取配置好的 OpenAI-compatible Embedding 模型。

    Endpoint、凭证与模型由 Provider Registry 的 ``text_embedding``
    binding 一次性解析，调用过程中不会切换网关或其他 Provider。
    """
    cfg = get_fallback_embedding_config()
    if cfg.get("protocol") != "openai_compatible":
        raise ValueError(f"Text embedding requires an OpenAI-compatible endpoint, got {cfg.get('protocol')}")
    api_base = cfg.get("api_base", "https://api.openai.com/v1")
    model = cfg.get("model", "text-embedding-3-small")
    embed_batch_size = clamp_embedding_batch_size(model, int(cfg.get("batch_size", 10)))

    api_key = cfg.get("api_key", "")
    if not api_key:
        raise ValueError("Text embedding credential is not configured for the selected Provider endpoint")

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
        dimensions=int(cfg.get("dimension", 1536)) if cfg.get("dimension") else None,
        embed_batch_size=embed_batch_size,
    )
    if init_model != model:
        embed_model._query_engine = model
        embed_model._text_engine = model
    return embed_model
