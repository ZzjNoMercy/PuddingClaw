"""Provider model limits shared by embedding clients."""

from __future__ import annotations


def clamp_embedding_batch_size(model: str, batch_size: int) -> int:
    requested = max(1, int(batch_size))
    normalized_model = str(model or "").strip().lower()
    if normalized_model in {"text-embedding-v3", "text-embedding-v4"}:
        return min(requested, 10)
    if normalized_model == "qwen3.7-text-embedding":
        return min(requested, 20)
    if normalized_model in {"text-embedding-v1", "text-embedding-v2"}:
        return min(requested, 25)
    return requested
