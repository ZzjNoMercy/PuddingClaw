"""Revision-bound persistent catalog for EAV value profiles.

The catalog is an optimization and maintenance ledger, never a source of
physical truth.  Callers must compute the current live ``source_revision``
before a record can be reused.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from threading import RLock
from typing import Any

from provider_registry import user_data_dir

CATALOG_SCHEMA_VERSION = "eav-profile-catalog/v1"


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EavProfileCatalog:
    """Persist exact-revision profiles and semantic maintenance reminders."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or user_data_dir() / "analytics" / "eav-profile-catalog.json"
        self._lock = RLock()

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict) or payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
            return {"schema_version": CATALOG_SCHEMA_VERSION, "profiles": {}, "maintenance_reminders": {}}
        payload.setdefault("profiles", {})
        payload.setdefault("maintenance_reminders", {})
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        fd, raw_path = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                if os.name != "nt":
                    os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _scope_key(
        *,
        source_id: str,
        table: str,
        type_name: str,
        source_revision: str,
        grain_contract_hash: str,
        semantic_hash: str,
        permission_epoch: int,
    ) -> str:
        return _stable_hash(
            {
                "source_id": source_id,
                "table": table,
                "type_name": type_name,
                "source_revision": source_revision,
                "grain_contract_hash": grain_contract_hash,
                "semantic_hash": semantic_hash,
                "permission_epoch": permission_epoch,
            }
        )

    def get(
        self,
        *,
        source_id: str,
        table: str,
        type_name: str,
        source_revision: str,
        grain_contract_hash: str,
        semantic_hash: str,
        permission_epoch: int,
    ) -> dict[str, Any] | None:
        key = self._scope_key(
            source_id=source_id,
            table=table,
            type_name=type_name,
            source_revision=source_revision,
            grain_contract_hash=grain_contract_hash,
            semantic_hash=semantic_hash,
            permission_epoch=permission_epoch,
        )
        with self._lock:
            item = self._read().get("profiles", {}).get(key)
        if not isinstance(item, dict):
            return None
        profile = item.get("profile")
        expected_hash = str(item.get("value_profile_hash") or "")
        if not isinstance(profile, dict) or expected_hash != _stable_hash(profile):
            return None
        return copy.deepcopy(item)

    def put(
        self,
        *,
        source_id: str,
        table: str,
        type_name: str,
        source_revision: str,
        grain_contract_hash: str,
        semantic_hash: str,
        permission_epoch: int,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        key = self._scope_key(
            source_id=source_id,
            table=table,
            type_name=type_name,
            source_revision=source_revision,
            grain_contract_hash=grain_contract_hash,
            semantic_hash=semantic_hash,
            permission_epoch=permission_epoch,
        )
        now = time.time()
        record = {
            "id": f"eav-profile-{key.removeprefix('sha256:')[:20]}",
            "source_id": source_id,
            "table": table,
            "type_name": type_name,
            "source_revision": source_revision,
            "grain_contract_hash": grain_contract_hash,
            "semantic_hash": semantic_hash,
            "permission_epoch": permission_epoch,
            "value_profile_hash": _stable_hash(profile),
            "sampled_at": now,
            "profile": copy.deepcopy(profile),
        }
        with self._lock:
            payload = self._read()
            existing = payload["profiles"].get(key)
            if isinstance(existing, dict):
                record["sampled_at"] = float(existing.get("sampled_at") or now)
                record["last_verified_at"] = now
                record["reuse_count"] = int(existing.get("reuse_count") or 0)
            else:
                record["last_verified_at"] = now
                record["reuse_count"] = 0
            payload["profiles"][key] = record
            self._write(payload)
        return copy.deepcopy(record)

    def record_reuse(self, record_id: str) -> None:
        with self._lock:
            payload = self._read()
            for item in payload["profiles"].values():
                if isinstance(item, dict) and item.get("id") == record_id:
                    item["reuse_count"] = int(item.get("reuse_count") or 0) + 1
                    item["last_verified_at"] = time.time()
                    self._write(payload)
                    return

    def mark_semantic_stale_candidate(
        self,
        *,
        source_id: str,
        table: str,
        semantic_asset_id: str,
        bound_type_names: list[str],
        missing_type_names: list[str],
        source_revision: str,
    ) -> dict[str, Any]:
        reminder_key = _stable_hash(
            {
                "source_id": source_id,
                "table": table,
                "semantic_asset_id": semantic_asset_id,
                "bound_type_names": sorted(bound_type_names),
                "missing_type_names": sorted(missing_type_names),
                "source_revision": source_revision,
            }
        )
        reminder = {
            "id": f"semantic-maintenance-{reminder_key.removeprefix('sha256:')[:20]}",
            "kind": "semantic_asset_stale_candidate",
            "status": "open",
            "source_id": source_id,
            "table": table,
            "semantic_asset_id": semantic_asset_id,
            "bound_type_names": sorted(set(bound_type_names)),
            "missing_type_names": sorted(set(missing_type_names)),
            "source_revision": source_revision,
            "detected_at": time.time(),
        }
        with self._lock:
            payload = self._read()
            existing = payload["maintenance_reminders"].get(reminder_key)
            if isinstance(existing, dict):
                reminder["detected_at"] = float(existing.get("detected_at") or reminder["detected_at"])
                reminder["last_seen_at"] = time.time()
                reminder["occurrences"] = int(existing.get("occurrences") or 1) + 1
            else:
                reminder["last_seen_at"] = reminder["detected_at"]
                reminder["occurrences"] = 1
            payload["maintenance_reminders"][reminder_key] = reminder
            self._write(payload)
        return copy.deepcopy(reminder)


eav_profile_catalog = EavProfileCatalog()


__all__ = ["EavProfileCatalog", "eav_profile_catalog"]
