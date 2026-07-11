"""Exact, active-Crosswalk lookup for cross-source semantic dimensions."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from analytics.semantic_assets.registry import SemanticAssetError, get_semantic_asset_registry

from .models import SemanticEntityLookupInput


_CROSSWALK_CACHE: dict[Path, tuple[int, int, dict[str, Any], dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]]]] = {}

def _normalize(value: object) -> str:
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", str(value or "")).lower())


def _key_signature(values: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), _normalize(value)) for key, value in values.items()))


class SemanticEntityLookupTool(BaseTool):
    name: str = "semantic_entity_lookup"
    description: str = (
        "Resolve source keys through a semantic dimension's active Crosswalk JSON. "
        "Use for cross-source entity joins; it returns only requested mappings and never loads the full Crosswalk into context."
    )
    args_schema: Type[BaseModel] = SemanticEntityLookupInput
    risk_level: str = "safe"

    class Config:
        arbitrary_types_allowed = True

    def _run(self, **kwargs: Any) -> str:
        return self._lookup(**kwargs)

    async def _arun(self, **kwargs: Any) -> str:
        return self._lookup(**kwargs)

    def _lookup(self, dimension_id: str, source_ref: str, keys: list[dict[str, str]], include_non_joinable: bool = False) -> str:
        asset_id = dimension_id if dimension_id.startswith("dimension:") else f"dimension:{dimension_id}"
        try:
            registry = get_semantic_asset_registry()
            detail = registry.get_asset(asset_id)
            resolution = (detail.get("frontmatter") or {}).get("resolution") or {}
            if str(resolution.get("mode") or "") != "entity_lookup":
                raise SemanticAssetError(f"{asset_id} is not an entity_lookup dimension")
            reference_path = str(resolution.get("reference_path") or "").strip()
            asset_path = Path(str(detail.get("path") or ""))
            if asset_path.parts and asset_path.parts[0] == "semantic-assets":
                asset_path = Path(*asset_path.parts[1:])
            reference = (registry.root_dir / asset_path.parent / reference_path).resolve()
            asset_dir = (registry.root_dir / asset_path.parent).resolve()
            if not reference_path or asset_dir not in reference.parents or not reference.is_file():
                raise SemanticAssetError("Active Crosswalk reference is missing or outside the dimension directory")
            stat = reference.stat()
            cached = _CROSSWALK_CACHE.get(reference)
            if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                payload, index = cached[2], cached[3]
            else:
                payload = json.loads(reference.read_text(encoding="utf-8"))
                index = {}
                for record in [*(payload.get("records") or []), *(payload.get("source_diagnostics") or [])]:
                    for binding in record.get("bindings") or []:
                        if isinstance(binding.get("key_fields"), dict):
                            index[(str(binding.get("source_ref") or ""), _key_signature(binding["key_fields"]))] = record
                _CROSSWALK_CACHE[reference] = (stat.st_mtime_ns, stat.st_size, payload, index)
        except Exception as exc:
            return f"🧩 实体匹配失败：{type(exc).__name__}: {exc}"

        matched: list[dict[str, Any]] = []
        candidate: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []
        for source_key in keys[:500]:
            record = index.get((source_ref, _key_signature(source_key)))
            if not record:
                unmatched.append({"source_key": source_key})
                continue
            resolution_info = record.get("resolution") or {}
            entity = record.get("entity") or {}
            item = {
                "source_key": source_key,
                "entity_key": entity.get("entity_key"),
                "status": resolution_info.get("status"),
                "join_eligible": bool(resolution_info.get("join_eligible")),
                "confidence": resolution_info.get("confidence"),
            }
            if item["join_eligible"] and item["status"] in {"auto_matched", "accepted", "manual_override"}:
                matched.append(item)
            elif include_non_joinable:
                candidate.append(item)
            else:
                unmatched.append({"source_key": source_key})

        return json.dumps({
            "dimension_id": asset_id,
            "reference_path": reference_path,
            "reference_version": payload.get("version"),
            "matched": matched,
            "candidate": candidate,
            "unmatched": unmatched,
            "coverage": {"requested": len(keys[:500]), "matched": len(matched), "joinable": len(matched)},
        }, ensure_ascii=False)
