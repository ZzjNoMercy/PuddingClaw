"""
嵌入向量提供者（多模型支持）
统一接口设计，支持 Jina、Qwen、BGE 等向量模型
"""

import logging
logger = logging.getLogger(__name__)
import os
import json
import requests
import numpy as np
from typing import List, Optional, Dict, Any

from llm.embedding_limits import clamp_embedding_batch_size


# ==================== 嵌入向量基类 ====================

class EmbeddingBase:
    """
    统一的嵌入向量基类
    
    子类只需实现 _embed(texts) 方法即可
    支持任意向量模型服务（Jina、Qwen、BGE、OpenAI等）
    
    使用示例:
        class CustomEmbedding(EmbeddingBase):
            def _embed(self, texts: List[str]) -> np.ndarray:
                # 调用自定义服务
                return np.array(...)
    """
    def __init__(
        self,
        api_url: str,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout: int = 30,
        extra_headers: Optional[Dict[str, str]] = None,
        batch_size: int = 20,
    ):
        """
        初始化嵌入向量客户端
        
        Args:
            api_url: API 服务地址
            api_key: API 密钥（可选）
            model_name: 模型名称（可选，不传则不在请求中携带）
            timeout: 请求超时时间（秒）
            extra_headers: 额外的 HTTP 请求头
        """
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.batch_size = max(1, int(batch_size or 20))
        self.embedding_dim = None  # 子类可在首次调用后更新

    # ==================== 统一的公开接口 ====================
    
    def encode_documents(self, documents: List[str]) -> np.ndarray:
        """
        编码文档（用于训练集向量化）
        
        Args:
            documents: 文档文本列表
            
        Returns:
            np.ndarray: shape (len(documents), embedding_dim)
        """
        if not documents:
            return np.zeros((0, self.embedding_dim or 768), dtype=np.float32)
        return self._embed_in_batches(documents)

    def encode_queries(self, queries: List[str]) -> np.ndarray:
        """
        编码查询（用于检索相似问题）
        
        Args:
            queries: 查询文本列表
            
        Returns:
            np.ndarray: shape (len(queries), embedding_dim)
        """
        if not queries:
            return np.zeros((0, self.embedding_dim or 768), dtype=np.float32)
        return self._embed_in_batches(queries)

    def _embed_in_batches(self, texts: List[str]) -> np.ndarray:
        chunks: list[np.ndarray] = []
        for start in range(0, len(texts), self.batch_size):
            chunks.append(self._embed(texts[start : start + self.batch_size]))
        if not chunks:
            return np.zeros((0, self.embedding_dim or 768), dtype=np.float32)
        return np.vstack(chunks)

    # ==================== 子类必须实现 ====================
    
    def _embed(self, texts: List[str]) -> np.ndarray:
        """
        核心嵌入方法（子类实现）
        
        Args:
            texts: 文本列表
            
        Returns:
            np.ndarray: shape (len(texts), embedding_dim)
        """
        raise NotImplementedError("子类必须实现 _embed() 方法")

    # ==================== 工具方法：安全请求 ====================
    
    def _request_json(self, method: str, url: str, **kwargs) -> Any:
        """
        统一的 HTTP 请求封装
        
        Args:
            method: HTTP 方法（GET/POST）
            url: 请求URL
            **kwargs: requests 参数（json, data, params等）
            
        Returns:
            解析后的 JSON 响应
            
        Raises:
            RuntimeError: 请求失败或响应格式错误
        """
        headers = dict(self.extra_headers)
        if self.api_key:
            # 通用 Bearer Token 授权（子类可覆盖）
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        headers.setdefault("Content-Type", "application/json")

        resp = requests.request(
            method, 
            url, 
            headers=headers, 
            timeout=self.timeout, 
            **kwargs
        )
        
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}") from e
        
        try:
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Invalid JSON response: {resp.text[:400]}") from e


# ==================== Jina 向量模型 ====================

class JinaEmbedding(EmbeddingBase):
    """
    Jina Embeddings 适配器
    
    适配 Jina 本地/远程服务：
    POST {api_url}
    body: { "inputs": [{"text": "..."}], "normalize": bool, "pooling": "mean|max" }
    返回: { "embeddings": [[...], ...] }
    
    说明：Jina 服务通常是单模型端点，无需 model_name
    """
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:8603/v1/embeddings",
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout: int = 30,
        normalize: bool = True,
        pooling: str = "mean",
        extra_headers: Optional[Dict[str, str]] = None,
        batch_size: int = 20,
        skip_test: bool = False,
    ):
        """
        初始化 Jina Embeddings 客户端
        
        Args:
            api_url: Jina 服务地址
            api_key: API 密钥（可选）
            model_name: 模型名称（可选，Jina 通常不需要）
            timeout: 请求超时时间
            normalize: 是否归一化向量
            pooling: 池化策略（mean/max）
            extra_headers: 额外请求头
            batch_size: 批量请求大小
            skip_test: 是否跳过连接测试
        """
        super().__init__(api_url, api_key, model_name, timeout, extra_headers, batch_size=batch_size)
        self.normalize = normalize
        self.pooling = pooling
        
        if not skip_test:
            # 可选的健康检查
            try:
                test_emb = self._embed(["ping"])
                self.embedding_dim = test_emb.shape[1]
                logger.info(f"Jina 连接成功: {api_url} (维度: {self.embedding_dim})")
            except Exception as e:
                logger.warning(f"Jina 连接测试失败: {e}")

    def _embed(self, texts: List[str]) -> np.ndarray:
        """调用 Jina API 获取嵌入向量"""
        payload = {
            "inputs": [{"text": t} for t in texts],
            "normalize": self.normalize,
            "pooling": self.pooling,
        }
        
        # 如果提供了 model_name，则在请求中携带
        if self.model_name:
            payload["model"] = self.model_name

        data = self._request_json("POST", self.api_url, json=payload)
        embs = data.get("embeddings")
        if not embs:
            raise RuntimeError(f"Empty embeddings from Jina: {data}")
        
        result = np.array(embs, dtype=np.float32)
        
        # 首次调用时更新维度
        if self.embedding_dim is None:
            self.embedding_dim = result.shape[1]
        
        return result


# ==================== Qwen 向量模型（DashScope 兼容）====================

class QwenEmbedding(EmbeddingBase):
    """
    Qwen Embeddings 适配器（DashScope OpenAI-兼容接口）
    
    适配 DashScope 的 OpenAI-兼容 Embeddings 接口：
    POST {api_url}/embeddings
    body: { "input": [...], "model": "text-embedding-v3" (可选) }
    返回: { "data": [{"embedding": [...]}, ...] }
    
    注意：官方通常需要 model，但若未传 model_name，就不带此字段
    """
    def __init__(
        self,
        api_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,  # 例如 "text-embedding-v3"
        timeout: int = 30,
        extra_headers: Optional[Dict[str, str]] = None,
        batch_size: int = 20,
    ):
        """
        初始化 Qwen Embeddings 客户端
        
        Args:
            api_url: DashScope API 基础地址
            api_key: DashScope API Key
            model_name: 嵌入模型名称（可选）
            timeout: 请求超时时间
            extra_headers: 额外请求头
            batch_size: 批量请求大小
        """
        super().__init__(
            api_url,
            api_key,
            model_name,
            timeout,
            extra_headers,
            batch_size=clamp_embedding_batch_size(model_name or "", batch_size),
        )

    def _embed(self, texts: List[str]) -> np.ndarray:
        """调用 Qwen API 获取嵌入向量"""
        url = f"{self.api_url}/embeddings"
        payload = {"input": texts}
        
        # 如果提供了 model_name，则在请求中携带
        if self.model_name:
            payload["model"] = self.model_name

        data = self._request_json("POST", url, json=payload)
        items = data.get("data")
        if not items:
            raise RuntimeError(f"Empty embeddings from Qwen: {data}")
        
        embs = [it.get("embedding") for it in items]
        result = np.array(embs, dtype=np.float32)
        
        # 首次调用时更新维度
        if self.embedding_dim is None:
            self.embedding_dim = result.shape[1]
        
        return result


# ==================== BGE 向量模型 ====================

class BGEEmbedding(EmbeddingBase):
    """
    BGE (BAAI General Embedding) 适配器
    
    支持两种调用方式：
    1) HuggingFace Inference API：URL 中包含模型名（推荐传 model_name）
       POST https://api-inference.huggingface.co/pipeline/feature-extraction/{model_name}
       body: {"inputs": ["...","..."]}
       返回: [[...], [...]]  或 单条时 [...]
       
    2) 自建/单模型服务：直接 POST {api_url}，不需要 model_name（与 Jina 类似）
       body: {"inputs": ["...","..."]}
    """
    def __init__(
        self,
        api_url: str = "https://api-inference.huggingface.co/pipeline/feature-extraction",
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,  # 例如 "BAAI/bge-large-zh-v1.5"
        timeout: int = 60,
        extra_headers: Optional[Dict[str, str]] = None,
        batch_size: int = 20,
        hf_task_style: bool = True,  # True 表示使用 HF ".../feature-extraction/{model}" 风格
    ):
        """
        初始化 BGE Embeddings 客户端
        
        Args:
            api_url: API 服务地址
            api_key: HuggingFace API Token（可选）
            model_name: 模型名称（HF 风格必须提供）
            timeout: 请求超时时间
            extra_headers: 额外请求头
            batch_size: 批量请求大小
            hf_task_style: 是否使用 HF 的任务风格 URL
        """
        super().__init__(api_url, api_key, model_name, timeout, extra_headers, batch_size=batch_size)
        self.hf_task_style = hf_task_style

    def _embed(self, texts: List[str]) -> np.ndarray:
        """调用 BGE API 获取嵌入向量"""
        # 构造 URL
        if self.hf_task_style:
            # HF 风格：必须在路径中拼上模型
            if not self.model_name:
                # 如果没传 model_name，假设 api_url 已经包含完整路径
                url = self.api_url
            else:
                url = f"{self.api_url}/{self.model_name}"
        else:
            # 自建服务风格：api_url 就是单模型端点
            url = self.api_url

        # HF Inference API 的授权头
        headers = dict(self.extra_headers)
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        headers.setdefault("Content-Type", "application/json")

        resp = requests.post(
            url,
            headers=headers,
            json={"inputs": texts},
            timeout=self.timeout
        )
        
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}") from e

        try:
            data = resp.json()
        except Exception:
            # HF 可能直接返回矩阵，不是标准 JSON 对象
            try:
                data = json.loads(resp.text)
            except Exception as e:
                raise RuntimeError(f"Invalid response: {resp.text[:400]}") from e

        # HF 可能返回单条向量或多条矩阵
        if isinstance(data, list) and data and isinstance(data[0], list) and isinstance(data[0][0], (int, float)):
            embs = data  # 多条
        elif isinstance(data, list) and data and isinstance(data[0], (int, float)):
            embs = [data]  # 单条
        else:
            # 某些网关会包一层
            embs = data.get("embeddings") or data.get("vectors") or data.get("data")
            if not embs:
                raise RuntimeError(f"Empty embeddings from BGE: {str(data)[:400]}")

        result = np.array(embs, dtype=np.float32)
        
        # 首次调用时更新维度
        if self.embedding_dim is None:
            self.embedding_dim = result.shape[1]
        
        return result


def create_embedding_client(
    provider: str,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    **kwargs
) -> EmbeddingBase:
    """
    创建嵌入向量客户端（工厂函数）
    
    Args:
        provider: 提供商类型 ("jina" | "qwen" | "bge")
        api_url: API 服务地址（可选，使用默认值）
        api_key: API 密钥（可选）
        model_name: 模型名称（可选，不传则不在请求中携带）
        **kwargs: 其他参数传递给具体客户端
    
    Returns:
        EmbeddingBase: 嵌入向量客户端实例
        
    Examples:
        >>> # 使用 Jina（本地服务）
        >>> client = create_embedding_client("jina", api_url="http://localhost:8603/v1/embeddings")
        
        >>> # 使用 Qwen（DashScope）
        >>> client = create_embedding_client(
        ...     "qwen",
        ...     api_key=os.getenv("DASHSCOPE_API_KEY"),
        ...     model_name="text-embedding-v3"
        ... )
        
        >>> # 使用 BGE（HuggingFace）
        >>> client = create_embedding_client(
        ...     "bge",
        ...     api_key=os.getenv("HUGGINGFACE_API_KEY"),
        ...     model_name="BAAI/bge-large-zh-v1.5"
        ... )
    """
    provider = provider.lower()
    
    if provider == "jina":
        return JinaEmbedding(
            api_url=api_url or "http://127.0.0.1:8603/v1/embeddings",
            api_key=api_key,
            model_name=model_name,
            **kwargs
        )
    elif provider == "qwen":
        return QwenEmbedding(
            api_url=api_url or "https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=api_key or os.getenv("DASHSCOPE_API_KEY"),
            model_name=model_name,
            **kwargs
        )
    elif provider == "bge":
        return BGEEmbedding(
            api_url=api_url or "https://api-inference.huggingface.co/pipeline/feature-extraction",
            api_key=api_key or os.getenv("HUGGINGFACE_API_KEY"),
            model_name=model_name,
            **kwargs
        )
    else:
        raise ValueError(
            f"Unsupported provider: {provider}. "
            f"Supported: jina, qwen, bge"
        )
