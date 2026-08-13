"""Deterministic discovery and receipts for semantic authoring decisions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from analytics.models import get_analytics_model_registry
from analytics.semantic_assets import get_semantic_asset_registry
from runtime_identity.paths import PuddingClawPaths, safe_identity_component

from .contracts import DefinitionKind
from .documents import parse_markdown_document

_DISCOVERY_TTL_SECONDS = 60 * 60
_ALL_KINDS: tuple[DefinitionKind, ...] = (
    "measure",
    "dimension",
    "grain",
    "relation",
    "analytics_model",
)


class SemanticDiscoveryError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _digest_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _normalize(value: object) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", str(value or "")).lower())


def _receipt_root(paths: PuddingClawPaths) -> Path:
    return paths.state() / "semantic-steward" / "discoveries"


def _receipt_key(paths: PuddingClawPaths) -> bytes:
    key_path = paths.state() / "semantic-steward" / "receipt-signing.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        with tempfile.NamedTemporaryFile(delete=False, dir=key_path.parent, prefix=".receipt-key.") as handle:
            handle.write(os.urandom(32))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        try:
            os.link(temporary, key_path)
            key_path.chmod(0o600)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
    try:
        key = key_path.read_bytes()
    except OSError as exc:
        raise SemanticDiscoveryError("discovery_signing_unavailable") from exc
    if len(key) < 32:
        raise SemanticDiscoveryError("discovery_signing_unavailable")
    return key


def _receipt_signature(payload: dict[str, Any], paths: PuddingClawPaths) -> str:
    canonical = {key: value for key, value in payload.items() if key != "signature"}
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "hmac-sha256:" + hmac.new(_receipt_key(paths), encoded, hashlib.sha256).hexdigest()


def _receipt_path(paths: PuddingClawPaths, receipt_id: str) -> Path:
    safe = safe_identity_component(receipt_id, field="discovery_receipt_id")
    return _receipt_root(paths) / safe / "receipt.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _definition_digest(root: Path, logical_path: str) -> str:
    target = root / Path(*Path(logical_path).parts)
    try:
        return _digest_bytes(target.read_bytes())
    except OSError as exc:
        raise SemanticDiscoveryError("catalog_unreadable", f"Unable to read definition: {logical_path}") from exc


def _catalog(paths: PuddingClawPaths) -> list[dict[str, Any]]:
    root = paths.user_definitions()
    semantic_snapshot = get_semantic_asset_registry(root).refresh()
    model_snapshot = get_analytics_model_registry(root).refresh()
    models = list(model_snapshot.get("models") or [])
    referenced_by: dict[str, list[dict[str, str]]] = {}
    for model in models:
        model_ref = {
            "id": str(model.get("id") or ""),
            "name": str(model.get("name") or ""),
            "logical_path": str(model.get("path") or ""),
        }
        semantic_assets = model.get("semantic_assets") if isinstance(model.get("semantic_assets"), dict) else {}
        selected = [
            *list(semantic_assets.get("measures") or []),
            *list(semantic_assets.get("dimensions") or []),
            *list(semantic_assets.get("grains") or []),
            *list(model.get("asset_relations") or []),
        ]
        for asset_id in selected:
            referenced_by.setdefault(str(asset_id), []).append(model_ref)

    entries: list[dict[str, Any]] = []
    for asset in semantic_snapshot.get("assets") or []:
        kind = str(asset.get("type") or "")
        if kind not in _ALL_KINDS:
            continue
        logical_path = str(asset.get("path") or "")
        raw_text = (root / Path(*Path(logical_path).parts)).read_text(encoding="utf-8")
        document = parse_markdown_document(raw_text)
        entries.append(
            {
                "kind": kind,
                "id": str(asset.get("id") or ""),
                "name": str(asset.get("name") or ""),
                "description": str(asset.get("description") or ""),
                "aliases": list(asset.get("aliases") or []),
                "tags": list(asset.get("tags") or []),
                "version": str(document.frontmatter.get("version") or ""),
                "logical_path": logical_path,
                "resolution_mode": str(asset.get("resolution_mode") or ""),
                "relation_type": str(asset.get("relation_type") or ""),
                "referenced_by": referenced_by.get(str(asset.get("id") or ""), []),
                "definition_digest": _definition_digest(root, logical_path),
                "_search_text": raw_text,
            }
        )
    for model in models:
        logical_path = str(model.get("path") or "")
        raw_text = (root / Path(*Path(logical_path).parts)).read_text(encoding="utf-8")
        entries.append(
            {
                "kind": "analytics_model",
                "id": str(model.get("id") or ""),
                "name": str(model.get("name") or ""),
                "description": str(model.get("description") or ""),
                "aliases": [],
                "tags": list(model.get("tags") or []),
                "version": str(model.get("version") or ""),
                "logical_path": logical_path,
                "resolution_mode": "",
                "relation_type": "",
                "referenced_by": [],
                "definition_digest": _definition_digest(root, logical_path),
                "_search_text": raw_text,
            }
        )
    return entries


def _scope_digest(entries: list[dict[str, Any]], kinds: tuple[DefinitionKind, ...]) -> str:
    included_kinds = set(kinds)
    if "analytics_model" in included_kinds:
        # A model decision depends on the semantic definitions it can select.
        included_kinds.update(_ALL_KINDS)
    elif included_kinds:
        included_kinds.add("analytics_model")  # backlinks are part of semantic discovery output
    values = sorted(
        (item["kind"], item["id"], item["logical_path"], item["definition_digest"])
        for item in entries
        if item["kind"] in included_kinds
    )
    return _digest_bytes(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _score(entry: dict[str, Any], query: str) -> tuple[int, list[str]] | None:
    normalized_query = _normalize(query)
    if not normalized_query:
        return 0, ["inventory"]
    normalized_id = _normalize(entry["id"])
    normalized_name = _normalize(entry["name"])
    normalized_aliases = [_normalize(item) for item in entry["aliases"]]
    normalized_description = _normalize(entry["description"])
    normalized_body = _normalize(entry["_search_text"])
    reasons: list[str] = []
    score = 0
    if normalized_query == normalized_id:
        score, reasons = 120, ["exact_id"]
    elif normalized_query == normalized_name:
        score, reasons = 110, ["exact_name"]
    elif normalized_query in normalized_aliases:
        score, reasons = 100, ["exact_alias"]
    else:
        if normalized_query in normalized_name or normalized_name in normalized_query:
            score += 80
            reasons.append("name_overlap")
        if any(normalized_query in alias or alias in normalized_query for alias in normalized_aliases if alias):
            score += 70
            reasons.append("alias_overlap")
        if normalized_query in normalized_description:
            score += 45
            reasons.append("description_match")
        if normalized_query in normalized_body:
            score += 25
            reasons.append("body_match")
    return (score, reasons) if score > 0 else None


def discover_semantic_definitions(
    *,
    query: str = "",
    kinds: list[str] | None = None,
    cursor: str = "",
    limit: int = 20,
    session_id: str = "",
    paths: PuddingClawPaths | None = None,
) -> dict[str, Any]:
    """List or search the full semantic catalogue and issue a discovery receipt."""

    if not str(session_id or "").strip():
        raise SemanticDiscoveryError("session_required")
    selected = tuple(dict.fromkeys(str(item or "").strip() for item in (kinds or _ALL_KINDS)))
    if not selected or any(item not in _ALL_KINDS for item in selected):
        raise SemanticDiscoveryError("invalid_discovery_kinds")
    selected_kinds = tuple(selected)  # type: ignore[assignment]
    page_size = max(1, min(int(limit or 20), 50))
    user_paths = paths or PuddingClawPaths.from_environment()
    entries = _catalog(user_paths)
    catalog_digest = _scope_digest(entries, selected_kinds)
    offset = 0
    if cursor:
        parts = str(cursor).split(":")
        if len(parts) != 3 or parts[0] != "offset" or parts[2] != catalog_digest[7:19]:
            raise SemanticDiscoveryError("discovery_cursor_stale")
        try:
            offset = max(0, int(parts[1]))
        except ValueError as exc:
            raise SemanticDiscoveryError("invalid_discovery_cursor") from exc
    ranked: list[tuple[int, str, dict[str, Any], list[str]]] = []
    for entry in entries:
        if entry["kind"] not in selected_kinds:
            continue
        scored = _score(entry, query)
        if scored is None:
            continue
        score, reasons = scored
        ranked.append((score, str(entry["name"]), entry, reasons))
    ranked.sort(key=lambda item: (-item[0], item[2]["kind"], item[1], item[2]["id"]))
    page = ranked[offset : offset + page_size]
    candidates = [
        {
            key: entry[key]
            for key in (
                "kind",
                "id",
                "name",
                "description",
                "aliases",
                "tags",
                "version",
                "logical_path",
                "resolution_mode",
                "relation_type",
                "referenced_by",
                "definition_digest",
            )
        }
        | {"match_score": score, "match_reasons": reasons}
        for score, _name, entry, reasons in page
    ]
    next_offset = offset + len(page)
    next_cursor = (
        f"offset:{next_offset}:{catalog_digest[7:19]}" if next_offset < len(ranked) else None
    )
    created_at = time.time()
    receipt_id = f"semantic-discovery-{uuid.uuid4().hex[:16]}"
    query_text = str(query or "").strip()
    receipt = {
        "receipt_id": receipt_id,
        "query": query_text,
        "mode": "targeted" if _normalize(query_text) else "inventory",
        "kinds": list(selected_kinds),
        "catalog_digest": catalog_digest,
        "catalog_count": sum(1 for item in entries if item["kind"] in selected_kinds),
        "match_count": len(ranked),
        "returned_ids": [item["id"] for item in candidates],
        "offset": offset,
        "complete": next_cursor is None,
        "created_at": created_at,
        "expires_at": created_at + _DISCOVERY_TTL_SECONDS,
        "session_id": str(session_id),
    }
    receipt["signature"] = _receipt_signature(receipt, user_paths)
    _write_json(_receipt_path(user_paths, receipt_id), receipt)
    return {
        "receipt_id": receipt_id,
        "query": receipt["query"],
        "mode": receipt["mode"],
        "kinds": receipt["kinds"],
        "catalog_digest": catalog_digest,
        "catalog_count": receipt["catalog_count"],
        "match_count": len(ranked),
        "candidates": candidates,
        "next_cursor": next_cursor,
        "complete": receipt["complete"],
        "decision_required": bool(receipt["mode"] == "targeted" and candidates),
    }


def validate_discovery_receipt(
    *,
    receipt_id: str,
    target_kind: DefinitionKind,
    session_id: str,
    paths: PuddingClawPaths,
) -> dict[str, Any]:
    if not str(receipt_id or "").strip():
        raise SemanticDiscoveryError("discovery_required")
    try:
        path = _receipt_path(paths, receipt_id)
    except ValueError as exc:
        raise SemanticDiscoveryError("discovery_receipt_unreadable") from exc
    if not path.is_file():
        raise SemanticDiscoveryError("discovery_required")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticDiscoveryError("discovery_receipt_unreadable") from exc
    if not isinstance(receipt, dict):
        raise SemanticDiscoveryError("discovery_receipt_unreadable")
    signature = str(receipt.get("signature") or "")
    if not signature or not hmac.compare_digest(signature, _receipt_signature(receipt, paths)):
        raise SemanticDiscoveryError("discovery_receipt_integrity_mismatch")
    if str(receipt.get("session_id") or "") != str(session_id or ""):
        raise SemanticDiscoveryError("discovery_session_mismatch")
    if time.time() > float(receipt.get("expires_at") or 0):
        raise SemanticDiscoveryError("discovery_expired")
    if target_kind not in set(receipt.get("kinds") or []):
        raise SemanticDiscoveryError("discovery_kind_mismatch")
    if int(receipt.get("offset") or 0) != 0 or receipt.get("mode") != "targeted":
        raise SemanticDiscoveryError("targeted_discovery_required")
    if not bool(receipt.get("complete")):
        raise SemanticDiscoveryError("discovery_incomplete")
    current_entries = _catalog(paths)
    kinds = tuple(receipt.get("kinds") or [])
    if _scope_digest(current_entries, kinds) != receipt.get("catalog_digest"):
        raise SemanticDiscoveryError("discovery_stale")
    return receipt
