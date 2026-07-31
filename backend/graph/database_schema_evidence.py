"""Server-owned receipts for database schema inspection evidence."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from copy import deepcopy
from typing import Any


class DatabaseSchemaEvidenceRegistry:
    """Bind live schema facts to the Run that observed them."""

    def __init__(self, *, ttl_seconds: int = 900) -> None:
        self.ttl_seconds = ttl_seconds
        self._receipts: dict[str, dict[str, Any]] = {}

    def register(
        self,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
        parent_generation_id: str,
        database_source_id: str,
        table_name: str,
        mode: str,
        search: str,
        rows: list[dict[str, Any]],
        parent_sql_sha256: str = "",
        parent_type_names: list[str] | None = None,
    ) -> dict[str, Any]:
        created_at = time.time()
        evidence = {
            "database_source_id": database_source_id,
            "table_name": self._normalize_table(table_name),
            "mode": mode,
            "search": search,
            "rows": deepcopy(rows),
            "parent_sql_sha256": parent_sql_sha256,
            "parent_type_names": sorted(set(parent_type_names or [])),
        }
        digest = hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        receipt = {
            "id": f"schema-evidence-{uuid.uuid4().hex[:16]}",
            "session_id": session_id,
            "query_id": query_id,
            "run_id": run_id,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "parent_generation_id": parent_generation_id,
            "created_at": created_at,
            "expires_at": created_at + self.ttl_seconds,
            "evidence": evidence,
            "sha256": f"sha256:{digest}",
        }
        self._receipts[receipt["id"]] = deepcopy(receipt)
        return deepcopy(receipt)

    def get(
        self,
        receipt_id: str,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
        parent_generation_id: str,
        database_source_id: str,
        allowed_tables: list[str],
        parent_sql_sha256: str = "",
    ) -> dict[str, Any] | None:
        receipt = self._receipts.get(str(receipt_id or ""))
        if not receipt or float(receipt.get("expires_at") or 0) < time.time():
            return None
        if not parent_generation_id or not str(receipt.get("parent_generation_id") or ""):
            return None
        if str(receipt.get("session_id") or "") != str(session_id or ""):
            return None
        if str(receipt.get("query_id") or "") != str(query_id or ""):
            return None
        if str(receipt.get("run_id") or "") != str(run_id or ""):
            return None
        if str(receipt.get("goal_id") or "") != str(goal_id or ""):
            return None
        if receipt.get("goal_revision") != goal_revision:
            return None
        if str(receipt.get("parent_generation_id") or "") != parent_generation_id:
            return None
        evidence = receipt.get("evidence") or {}
        expected_digest = hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if str(receipt.get("sha256") or "") != f"sha256:{expected_digest}":
            return None
        if str(evidence.get("mode") or "") != "type_names":
            return None
        if not parent_sql_sha256 or str(evidence.get("parent_sql_sha256") or "") != parent_sql_sha256:
            return None
        if not evidence.get("parent_type_names"):
            return None
        if str(evidence.get("database_source_id") or "") != database_source_id:
            return None
        normalized_allowed = {self._normalize_table(item) for item in allowed_tables}
        if str(evidence.get("table_name") or "") not in normalized_allowed:
            return None
        return deepcopy(receipt)

    @staticmethod
    def _normalize_table(value: str) -> str:
        parts = [part.strip().strip('"').lower() for part in str(value or "").split(".") if part.strip()]
        if not parts:
            return ""
        if len(parts) == 1:
            return f"public.{parts[0]}"
        return ".".join(parts[-2:])


database_schema_evidence_registry = DatabaseSchemaEvidenceRegistry()
