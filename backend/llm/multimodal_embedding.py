"""Multimodal embedding adapters for LlamaIndex."""

from __future__ import annotations

import os
import base64
import mimetypes
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
from llama_index.core.embeddings import MultiModalEmbedding
from llama_index.core.schema import ImageType
from pydantic import PrivateAttr

import capabilities
from config import get_gateway_config, get_multimodal_embedding_config


class DashScopeMultiModalEmbedding(MultiModalEmbedding):
    """DashScope Qwen-VL embedding adapter for both text and image nodes.

    This follows the notebook pattern used for MinerU + LlamaIndex +
    Milvus: the same multimodal embedding model is passed as both
    `embed_model` and `image_embed_model`.
    """

    _api_key: str = PrivateAttr()
    _dimension: int = PrivateAttr(default=1024)
    _base_url: str = PrivateAttr(default="")
    _route_path: str = PrivateAttr(default="")
    _use_http: bool = PrivateAttr(default=False)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "qwen2.5-vl-embedding",
        dimension: int = 1024,
        base_url: str = "",
        route_path: str = "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
        use_http: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_name=model_name, **kwargs)
        self._api_key = api_key or os.getenv("DASHSCOPE_API_KEY") or os.getenv("EMBEDDING_API_KEY") or ""
        self._dimension = dimension
        self._base_url = base_url.rstrip("/")
        self._route_path = route_path if route_path.startswith("/") else f"/{route_path}"
        self._use_http = bool(use_http and self._base_url)
        if not self._api_key and not self._use_http:
            raise ValueError("DashScope multimodal embedding requires DASHSCOPE_API_KEY or EMBEDDING_API_KEY.")

    def _call_api(self, input_data: list[dict[str, str]]) -> list[float]:
        if self._use_http:
            return self._call_http_api(input_data)

        try:
            import dashscope
        except ImportError as exc:
            raise RuntimeError(
                "dashscope is not installed. Install the optional multimodal dependencies first."
            ) from exc

        dashscope.api_key = self._api_key
        response = dashscope.MultiModalEmbedding.call(model=self.model_name, input=input_data)
        if response.status_code != HTTPStatus.OK:
            message = getattr(response, "message", "") or str(response)
            raise RuntimeError(f"DashScope multimodal embedding failed: {message}")
        embeddings = response.output.get("embeddings", []) if getattr(response, "output", None) else []
        if not embeddings:
            raise RuntimeError(f"DashScope multimodal embedding returned no embeddings: {response}")
        return list(embeddings[0]["embedding"])

    def _call_http_api(self, input_data: list[dict[str, str]]) -> list[float]:
        """Call a DashScope-native endpoint, optionally exposed through Higress.

        This is intentionally not OpenAI-compatible. A Higress route for this
        mode should proxy DashScope's native multimodal embedding path.
        """

        url = f"{self._base_url}{self._route_path}"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            # For a user-managed Higress route that injects upstream auth.
            headers["Authorization"] = "Bearer puddingclaw-gateway"
        response = httpx.post(
            url,
            headers=headers,
            json={"model": self.model_name, "input": input_data},
            timeout=120.0,
        )
        response.raise_for_status()
        payload = response.json()
        output = payload.get("output", {}) if isinstance(payload, dict) else {}
        embeddings = output.get("embeddings", [])
        if not embeddings:
            raise RuntimeError(f"Multimodal embedding HTTP endpoint returned no embeddings: {payload}")
        return list(embeddings[0]["embedding"])

    @staticmethod
    def _image_payload_for_http(img_file_path: ImageType) -> str:
        path = Path(str(img_file_path)).expanduser().resolve()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _get_image_embedding(self, img_file_path: ImageType) -> list[float]:
        abs_path = Path(str(img_file_path)).expanduser().resolve()
        image = self._image_payload_for_http(abs_path) if self._use_http else f"file://{abs_path}"
        return self._call_api([{"image": image}])

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._call_api([{"text": text}])

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._call_api([{"text": query}])

    async def _aget_image_embedding(self, img_file_path: ImageType) -> list[float]:
        return self._get_image_embedding(img_file_path)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)


def get_multimodal_embedding_model() -> DashScopeMultiModalEmbedding:
    cfg = get_multimodal_embedding_config()
    gateway = get_gateway_config()
    use_gateway = bool(cfg.get("prefer_gateway") and (cfg.get("base_url") or gateway.get("base_url")))
    if use_gateway:
        try:
            use_gateway = capabilities.detect_capabilities_sync().ai_gateway.available
        except Exception:
            use_gateway = bool(cfg.get("base_url"))

    base_url = cfg.get("base_url") or (gateway.get("base_url", "").removesuffix("/v1") if use_gateway else "")
    return DashScopeMultiModalEmbedding(
        model_name=cfg.get("model", "qwen2.5-vl-embedding"),
        dimension=int(cfg.get("dimension", 1024)),
        api_key=cfg.get("api_key", ""),
        base_url=base_url,
        route_path=cfg.get("route_path", "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"),
        use_http=use_gateway,
    )
