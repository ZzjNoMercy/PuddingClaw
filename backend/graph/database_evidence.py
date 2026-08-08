"""Server-owned, scope-bound receipts for Agent database evidence searches."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from copy import deepcopy
from typing import Any


def _normalized_table_scope(values: list[str] | None) -> set[str]:
    normalized: set[str] = set()
    for value in values or []:
        parts = [part.strip().strip('"').lower() for part in str(value).split(".") if part.strip()]
        if parts:
            normalized.add(".".join(parts))
    return normalized


class DatabaseEvidenceRegistry:
    """Keep evidence references opaque and prevent cross-Run reuse."""

    def __init__(self, *, ttl_seconds: int = 900) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, dict[str, Any]] = {}

    def register(
        self,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
        database_source_id: str,
        allowed_tables: list[str],
        payload: dict[str, Any],
        trusted_question_sha256: str = "",
        analytics_model_id: str = "",
        analytics_model_revision: str = "",
        semantic_context_hash: str = "",
        selected_semantic_asset_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        created_at = time.time()
        item = {
            "id": f"database-evidence-{uuid.uuid4().hex[:16]}",
            "session_id": str(session_id or ""),
            "query_id": str(query_id or ""),
            "run_id": str(run_id or ""),
            "goal_id": str(goal_id or ""),
            "goal_revision": goal_revision,
            "database_source_id": str(database_source_id or ""),
            "allowed_tables": sorted({str(value) for value in allowed_tables if str(value).strip()}),
            "trusted_question_sha256": str(trusted_question_sha256 or ""),
            "analytics_model_id": str(analytics_model_id or ""),
            "analytics_model_revision": str(analytics_model_revision or ""),
            "semantic_context_hash": str(semantic_context_hash or ""),
            "selected_semantic_asset_ids": sorted({str(value) for value in selected_semantic_asset_ids or [] if str(value).strip()}),
            "created_at": created_at,
            "expires_at": created_at + self.ttl_seconds,
            "payload": deepcopy(payload),
        }
        item["sha256"] = "sha256:" + hashlib.sha256(
            json.dumps(item["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        self._items[item["id"]] = deepcopy(item)
        if item["session_id"]:
            # The in-memory index is the fast path.  Persist the immutable
            # envelope as well so a resumed worker cannot manufacture a new
            # evidence context after a process restart.
            try:
                from graph.session_manager import session_manager

                if session_manager.is_initialized:
                    session_manager.record_database_evidence(item["session_id"], item["id"], item)
            except Exception:
                # Retrieval must remain usable in unit/CLI contexts where the
                # SessionManager has not been bootstrapped yet.  The validator
                # still fails closed if the receipt is not available in scope.
                pass
        return deepcopy(item)

    def get(
        self,
        evidence_id: str,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
        database_source_id: str,
        allowed_tables: list[str],
        trusted_question_sha256: str = "",
        analytics_model_id: str = "",
        analytics_model_revision: str = "",
        semantic_context_hash: str = "",
        selected_semantic_asset_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        normalized_id = str(evidence_id or "")
        item = self._items.get(normalized_id)
        if item is None and str(session_id or ""):
            try:
                from graph.session_manager import session_manager

                if session_manager.is_initialized:
                    item = session_manager.get_database_evidence(str(session_id), normalized_id)
                    if item:
                        self._items[normalized_id] = deepcopy(item)
            except Exception:
                item = None
        if not item or float(item.get("expires_at") or 0) < time.time():
            return None
        expected = {
            "session_id": str(session_id or ""),
            "query_id": str(query_id or ""),
            "run_id": str(run_id or ""),
            "goal_id": str(goal_id or ""),
            "database_source_id": str(database_source_id or ""),
        }
        if any(str(item.get(key) or "") != value for key, value in expected.items()):
            return None
        if item.get("goal_revision") != goal_revision:
            return None
        # Evidence search establishes an upper bound. A later SQL submission
        # may intentionally use only one of the routed tables, but it may
        # never expand beyond the tables covered by the evidence receipt.
        evidence_tables = _normalized_table_scope(item.get("allowed_tables") or [])
        requested_tables = _normalized_table_scope(allowed_tables)
        if not requested_tables.issubset(evidence_tables):
            return None
        expected_context = {
            "trusted_question_sha256": str(trusted_question_sha256 or ""),
            "analytics_model_id": str(analytics_model_id or ""),
            "analytics_model_revision": str(analytics_model_revision or ""),
            "semantic_context_hash": str(semantic_context_hash or ""),
        }
        for key, value in expected_context.items():
            if value and str(item.get(key) or "") != value:
                return None
        expected_assets = sorted({str(value) for value in selected_semantic_asset_ids or [] if str(value).strip()})
        if expected_assets and list(item.get("selected_semantic_asset_ids") or []) != expected_assets:
            return None
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        expected_hash = "sha256:" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        if item.get("sha256") != expected_hash:
            return None
        return deepcopy(item)


database_evidence_registry = DatabaseEvidenceRegistry()
