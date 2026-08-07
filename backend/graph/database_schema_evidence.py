"""Server-owned receipts for database schema inspection evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _json_safe(value: Any) -> Any:
    """Convert driver-specific scalar values into stable receipt primitives."""

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: str(item))
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class DatabaseSchemaDiscoveryCoordinator:
    """Serialize same-query discovery before SQL generation consumes receipts.

    The Agent may accidentally schedule inspect and generate in one tool batch.
    A short coalescing yield lets inspection declare itself, after which generate
    waits for the server-owned receipt instead of snapshotting an empty registry.
    """

    def __init__(self) -> None:
        self._active: dict[tuple[str, str], int] = {}

    @staticmethod
    def _key(session_id: str, query_id: str) -> tuple[str, str] | None:
        session = str(session_id or "").strip()
        query = str(query_id or "").strip()
        return (session, query) if session and query else None

    def begin(self, *, session_id: str, query_id: str) -> None:
        key = self._key(session_id, query_id)
        if key is not None:
            self._active[key] = self._active.get(key, 0) + 1

    def finish(self, *, session_id: str, query_id: str) -> None:
        key = self._key(session_id, query_id)
        if key is None:
            return
        remaining = self._active.get(key, 0) - 1
        if remaining > 0:
            self._active[key] = remaining
        else:
            self._active.pop(key, None)

    async def wait_until_idle(
        self,
        *,
        session_id: str,
        query_id: str,
        coalesce_seconds: float = 0.05,
        timeout_seconds: float = 30.0,
    ) -> bool:
        key = self._key(session_id, query_id)
        if key is None:
            return True
        await asyncio.sleep(max(0.0, coalesce_seconds))
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while self._active.get(key, 0) > 0:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True


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
        parent_generation_id: str = "",
        database_source_id: str,
        table_name: str,
        mode: str,
        search: str,
        rows: list[dict[str, Any]],
        parent_sql_sha256: str = "",
        parent_type_names: list[str] | None = None,
        type_name: str = "",
        profile: dict[str, Any] | None = None,
        profile_revision: str = "",
    ) -> dict[str, Any]:
        created_at = time.time()
        receipt_kind = "repair" if parent_generation_id else "discovery"
        evidence = _json_safe({
            "database_source_id": database_source_id,
            "table_name": self._normalize_table(table_name),
            "mode": mode,
            "search": search,
            "rows": deepcopy(rows),
            "type_name": str(type_name or ""),
            "profile": deepcopy(profile or {}),
            "profile_revision": str(profile_revision or ""),
            "parent_sql_sha256": parent_sql_sha256,
            "parent_type_names": sorted(set(parent_type_names or [])),
        })
        inspection_digest = hashlib.sha256(
            json.dumps(
                {
                    "database_source_id": evidence["database_source_id"],
                    "table_name": evidence["table_name"],
                    "mode": evidence["mode"],
                    "search": evidence["search"],
                    "type_name": evidence["type_name"],
                    "profile_revision": evidence["profile_revision"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
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
            "receipt_kind": receipt_kind,
            "inspection_sha256": f"sha256:{inspection_digest}",
            "created_at": created_at,
            "expires_at": created_at + self.ttl_seconds,
            "evidence": evidence,
            "sha256": f"sha256:{digest}",
        }
        self._receipts[receipt["id"]] = deepcopy(receipt)
        return deepcopy(receipt)

    @staticmethod
    def _digest_valid(receipt: dict[str, Any]) -> bool:
        evidence = receipt.get("evidence") or {}
        expected_digest = hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return str(receipt.get("sha256") or "") == f"sha256:{expected_digest}"

    def get_discovery(
        self,
        receipt_id: str,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
    ) -> dict[str, Any] | None:
        receipt = self._receipts.get(str(receipt_id or ""))
        if not receipt or float(receipt.get("expires_at") or 0) < time.time():
            return None
        if str(receipt.get("receipt_kind") or "") != "discovery":
            return None
        expected = {
            "session_id": session_id,
            "query_id": query_id,
            "run_id": run_id,
            "goal_id": goal_id,
        }
        if any(str(receipt.get(key) or "") != str(value or "") for key, value in expected.items()):
            return None
        if receipt.get("goal_revision") != goal_revision or not self._digest_valid(receipt):
            return None
        return deepcopy(receipt)

    def list_discovery(
        self,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        goal_id: str,
        goal_revision: int | None,
    ) -> list[dict[str, Any]]:
        return sorted(
            (
                receipt
                for receipt_id in list(self._receipts)
                if (
                    receipt := self.get_discovery(
                        receipt_id,
                        session_id=session_id,
                        query_id=query_id,
                        run_id=run_id,
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                    )
                )
                is not None
            ),
            key=lambda item: float(item.get("created_at") or 0),
        )

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
        if str(receipt.get("receipt_kind") or "repair") != "repair" or not self._digest_valid(receipt):
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
database_schema_discovery_coordinator = DatabaseSchemaDiscoveryCoordinator()
