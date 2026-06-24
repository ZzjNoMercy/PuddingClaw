"""统一模型接入层。

- ModelClient: LLM 调用入口，支持 Higress AI Gateway fallback 与直连 provider
- get_embedding_model: Embedding 模型入口，显式传参，不污染全局 Settings
"""

from llm.embed_client import get_embedding_model
from llm.model_client import ModelClient

__all__ = ["ModelClient", "get_embedding_model"]
