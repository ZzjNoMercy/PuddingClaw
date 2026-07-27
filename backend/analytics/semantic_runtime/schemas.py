"""Typed contracts shared by SQL, Pandas, and future analytics adapters."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SemanticQueryContext:
    """Immutable top-level contract compiled once from model and asset state.

    Nested registry payloads are treated as read-only. Adapters receive this
    value and render only the fields relevant to their execution language.
    """

    question: str
    model_id: str
    model_version: str
    model_context: dict[str, Any]
    resolution: dict[str, Any]
    trace: dict[str, Any]
    context_id: str
    semantic_hash: str

    @property
    def content_hash(self) -> str:
        """Backward-friendly name for the binding-independent semantic hash."""

        return self.semantic_hash

    def to_trace(self) -> dict[str, Any]:
        """Return a detached trace so adapters cannot mutate shared state."""

        return deepcopy(self.trace)

    @property
    def source_asset_ids(self) -> tuple[str, ...]:
        asset_ids: list[str] = []
        for item in self.model_context.get("data_assets") or []:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or "").strip()
            if asset_id and asset_id not in asset_ids:
                asset_ids.append(asset_id)
        return tuple(asset_ids)

    @property
    def semantic_asset_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for key in ("matched", "references"):
            for item in self.trace.get(key) or []:
                asset_id = str((item or {}).get("id") or "").strip()
                if asset_id and asset_id not in ids:
                    ids.append(asset_id)
        return tuple(ids)
