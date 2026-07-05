"""Rerank client helpers for knowledge retrieval."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from config import get_fallback_embedding_config, get_multimodal_embedding_config, get_rag_rerank_config

logger = logging.getLogger(__name__)


@dataclass
class RerankItem:
    index: int
    score: float | None = None


def _dashscope_api_key(config: dict[str, Any]) -> str:
    return (
        str(config.get("api_key") or "")
        or str(get_multimodal_embedding_config().get("api_key") or "")
        or str(get_fallback_embedding_config().get("api_key") or "")
    )


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

    api_key = _dashscope_api_key(config)
    if not api_key:
        logger.warning("[rerank] skipped because api key is not configured")
        return []

    try:
        import dashscope
    except ImportError:
        logger.warning("[rerank] skipped because dashscope is not installed")
        return []

    base_url = str(config.get("base_url") or "").strip()
    previous_base_url = getattr(dashscope, "base_http_api_url", None)
    if base_url:
        dashscope.base_http_api_url = base_url.rstrip("/")

    model = str(config.get("model") or "qwen3-vl-rerank")
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

        response = dashscope.TextReRank.call(
            model=model,
            api_key=api_key,
            query=query_payload,  # type: ignore[arg-type]
            documents=document_payloads,  # type: ignore[arg-type]
            top_n=top_n,
            return_documents=True,
            **({"instruct": instruct} if instruct and model != "qwen3-vl-rerank" else {}),
        )
        if response.status_code != HTTPStatus.OK:
            logger.warning("[rerank] model=%s failed: %s", model, getattr(response, "message", response))
            return []
        output = getattr(response, "output", None) or {}
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
    finally:
        if base_url and previous_base_url:
            dashscope.base_http_api_url = previous_base_url
