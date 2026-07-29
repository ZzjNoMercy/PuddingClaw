"""Multimodal embedding adapters for LlamaIndex."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import httpx
from llama_index.core.embeddings import MultiModalEmbedding
from llama_index.core.schema import ImageType
from pydantic import PrivateAttr

from config import get_multimodal_embedding_config

EmbeddingProgressCallback = Callable[[str, int, int], None]


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
    _progress_callback: EmbeddingProgressCallback | None = PrivateAttr(default=None)
    _progress_totals: dict[str, int] = PrivateAttr(default_factory=dict)
    _progress_done: dict[str, int] = PrivateAttr(default_factory=dict)
    _progress_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = "qwen2.5-vl-embedding",
        dimension: int = 1024,
        base_url: str = "",
        route_path: str = "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
        use_http: bool = True,
        progress_callback: EmbeddingProgressCallback | None = None,
        progress_totals: dict[str, int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_name=model_name, **kwargs)
        self._api_key = api_key or ""
        self._dimension = dimension
        self._base_url = base_url.rstrip("/")
        self._route_path = route_path if route_path.startswith("/") else f"/{route_path}"
        self._progress_callback = progress_callback
        self._progress_totals = progress_totals or {}
        self._progress_done = {"text": 0, "image": 0}
        self._progress_lock = threading.Lock()
        if not self._api_key:
            raise ValueError("DashScope multimodal embedding credential is not configured for the selected Provider endpoint.")
        if not self._base_url:
            raise ValueError("DashScope multimodal embedding endpoint is not configured.")

    def set_progress_callback(
        self,
        callback: EmbeddingProgressCallback | None,
        *,
        text_total: int = 0,
        image_total: int = 0,
    ) -> None:
        self._progress_callback = callback
        self._progress_totals = {"text": max(0, text_total), "image": max(0, image_total)}
        self._progress_done = {"text": 0, "image": 0}

    def _notify_progress(self, modality: str, count: int) -> None:
        if count <= 0:
            return
        with self._progress_lock:
            self._progress_done[modality] = self._progress_done.get(modality, 0) + count
            done = self._progress_done[modality]
        callback = self._progress_callback
        if callback:
            callback(modality, done, self._progress_totals.get(modality, 0))

    def _request_concurrency(self, total: int) -> int:
        value = max(1, int(getattr(self, "embed_batch_size", 1) or 1))
        return max(1, min(value, total))

    def _call_items_concurrently(self, input_items: list[dict[str, str]], *, modality: str) -> list[list[float]]:
        if not input_items:
            return []
        results: list[list[float] | None] = [None] * len(input_items)
        max_workers = self._request_concurrency(len(input_items))
        if max_workers == 1:
            for index, item in enumerate(input_items):
                results[index] = self._call_api([item])[0]
                self._notify_progress(modality, 1)
            return [embedding for embedding in results if embedding is not None]

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._call_api, [item]): index for index, item in enumerate(input_items)}
            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()[0]
                self._notify_progress(modality, 1)
        return [embedding for embedding in results if embedding is not None]

    def _call_api(self, input_data: list[dict[str, str]]) -> list[list[float]]:
        # Use a per-request client rather than DashScope's module globals.
        # This prevents different projects/providers from crossing keys or URLs
        # when multimodal indexing runs concurrently.
        return self._call_http_api(input_data)

    def _call_http_api(self, input_data: list[dict[str, str]]) -> list[list[float]]:
        """Call the explicit DashScope-native Provider endpoint."""

        url = f"{self._base_url}{self._route_path}"
        headers = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {self._api_key}"
        with httpx.Client(timeout=120.0) as client:
            response = client.post(url, headers=headers, json={"model": self.model_name, "input": input_data})
            response.raise_for_status()
            payload = response.json()
        output = payload.get("output", {}) if isinstance(payload, dict) else {}
        embeddings = output.get("embeddings", [])
        if not embeddings:
            raise RuntimeError(f"Multimodal embedding HTTP endpoint returned no embeddings: {payload}")
        return [list(item["embedding"]) for item in embeddings]

    @staticmethod
    def _image_payload_for_http(img_file_path: ImageType) -> str:
        path = Path(str(img_file_path)).expanduser().resolve()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _get_image_embedding(self, img_file_path: ImageType) -> list[float]:
        embedding = self._get_image_embeddings([img_file_path])[0]
        return embedding

    def _get_image_embeddings(self, img_file_paths: list[ImageType]) -> list[list[float]]:
        input_items: list[dict[str, str]] = []
        for img_file_path in img_file_paths:
            abs_path = Path(str(img_file_path)).expanduser().resolve()
            image = self._image_payload_for_http(abs_path)
            input_items.append({"image": image})
        # DashScope multimodal embedding rejects repeated input types in one
        # request, e.g. [{"image": ...}, {"image": ...}]. Keep the LlamaIndex
        # batch interface, but fan out provider calls with bounded concurrency.
        return self._call_items_concurrently(input_items, modality="image")

    def _get_text_embedding(self, text: str) -> list[float]:
        embedding = self._get_text_embeddings([text])[0]
        return embedding

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        # DashScope multimodal embedding allows only one "text" input per
        # request. LlamaIndex may still pass us batches; fan them out here.
        return self._call_items_concurrently([{"text": text} for text in texts], modality="text")

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._call_api([{"text": query}])[0]

    async def _aget_image_embedding(self, img_file_path: ImageType) -> list[float]:
        return await asyncio.to_thread(self._get_image_embedding, img_file_path)

    async def _aget_image_embeddings(self, img_file_paths: list[ImageType]) -> list[list[float]]:
        return await asyncio.to_thread(self._get_image_embeddings, img_file_paths)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._get_text_embedding, text)

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._get_text_embeddings, texts)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return await asyncio.to_thread(self._get_query_embedding, query)


def get_multimodal_embedding_model() -> DashScopeMultiModalEmbedding:
    cfg = get_multimodal_embedding_config()
    if cfg.get("protocol") != "dashscope_multimodal_embedding":
        raise ValueError(f"Multimodal embedding requires a DashScope native endpoint, got {cfg.get('protocol')}")
    return DashScopeMultiModalEmbedding(
        model_name=cfg.get("model", "qwen2.5-vl-embedding"),
        dimension=int(cfg.get("dimension", 1024)),
        embed_batch_size=int(cfg.get("batch_size", 10)),
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", ""),
        route_path=cfg.get("route_path", "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"),
        use_http=True,
    )
