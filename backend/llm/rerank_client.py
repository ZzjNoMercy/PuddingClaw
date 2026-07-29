"""Rerank client helpers for knowledge retrieval."""

from __future__ import annotations

import logging
import httpx
from dataclasses import dataclass
from typing import Any

from config import get_rag_rerank_config, load_config
from provider_registry import get_provider_registry

logger = logging.getLogger(__name__)


@dataclass
class RerankItem:
    index: int
    score: float | None = None


def rerank_documents(
    *,
    query: str,
    documents: list[str | dict[str, Any]],
    top_n: int | None = None,
    instruct: str | None = None,
) -> list[RerankItem]:
    """Rerank text or text-shaped multimodal candidates with DashScope.

    The official qwen3-vl-rerank API supports mixed text/image/video documents,
    but our first integration intentionally passes image candidates as textual
    context (title + linked Markdown context) so query-time rerank does not
    upload local image files again.
    """

    if not documents:
        return []

    config = get_rag_rerank_config()
    if not config.get("enabled"):
        return []
    if str(config.get("provider") or "dashscope").lower() != "dashscope":
        logger.warning("[rerank] unsupported provider=%s", config.get("provider"))
        return []

    resolved = get_provider_registry().resolve_binding("rerank", legacy_config=load_config())
    api_key = str(resolved.get("api_key") or "")
    if not api_key:
        logger.warning("[rerank] skipped because api key is not configured")
        return []

    if resolved.get("protocol") != "dashscope_multimodal_embedding":
        logger.warning("[rerank] selected endpoint does not support DashScope native rerank")
        return []
    model = str(resolved.get("name") or config.get("model") or "qwen3-vl-rerank")
    top_n = max(1, min(top_n or int(config.get("top_n") or 5), len(documents)))
    try:
        if model == "qwen3-vl-rerank":
            query_payload: str | dict[str, str] = {"text": query}
            document_payloads: list[str | dict[str, Any]] = [
                doc if isinstance(doc, dict) else {"text": str(doc)} for doc in documents
            ]
        else:
            query_payload = query
            document_payloads = [str(doc.get("text") if isinstance(doc, dict) else doc) for doc in documents]

        base_url = str(resolved.get("base_url") or "").rstrip("/")
        route_path = str(resolved.get("route_path") or "/api/v1/services/rerank/text-rerank/text-rerank")
        # Rerank has a native DashScope contract, not the OpenAI embeddings
        # contract. The client is request-scoped so concurrent jobs never
        # mutate shared SDK configuration.
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{base_url}{route_path if route_path.startswith('/') else '/' + route_path}",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "input": {"query": query_payload, "documents": document_payloads},
                    "parameters": {"top_n": top_n, "return_documents": True, **({"instruct": instruct} if instruct and model != "qwen3-vl-rerank" else {})},
                },
            )
            response.raise_for_status()
            output = response.json().get("output", {})
        results = output.get("results", []) if isinstance(output, dict) else []
        reranked: list[RerankItem] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            score = item.get("relevance_score")
            try:
                numeric_score = float(score)
            except (TypeError, ValueError):
                numeric_score = None
            reranked.append(RerankItem(index=index, score=numeric_score))
        return reranked
    except Exception as exc:  # noqa: BLE001
        logger.warning("[rerank] failed: %s: %s", type(exc).__name__, exc)
        return []
