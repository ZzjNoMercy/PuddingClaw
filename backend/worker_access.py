"""Worker Access Key storage and scoped Bearer authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any


WORKER_SCOPES = frozenset(
    {
        "worker:health",
        "worker:models:read",
        "worker:runs:create",
        "worker:runs:read",
        "worker:runs:cancel",
    }
)
AUTHORITY_PROFILES = frozenset({"smart", "workspace", "workspace_network", "workspace_package_install"})


class WorkerAccessError(ValueError):
    pass


class WorkerAccessStore:
    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._path: Path | None = None
        self._lock = threading.RLock()

    def initialize(self, base_dir: Path) -> None:
        with self._lock:
            self._base_dir = base_dir
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            self._path = data_dir / "worker-access-keys.json"
            if not self._path.exists():
                self._write({})

    def _ready(self) -> Path:
        if self._path is None:
            raise WorkerAccessError("Worker Access Key store is not initialized")
        return self._path

    def _read(self) -> dict[str, dict[str, Any]]:
        path = self._ready()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}

    def _write(self, payload: dict[str, dict[str, Any]]) -> None:
        path = self._ready() if self._path is not None else None
        if path is None:
            # initialize() creates the directory before the first write.
            raise WorkerAccessError("Worker Access Key store is not initialized")
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _hash(secret: str) -> str:
        return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "key_id",
                "prefix",
                "name",
                "scopes",
                "allowed_analytics_models",
                "authority_profile",
                "expires_at",
                "last_used_at",
                "revoked_at",
                "created_at",
            )
        }

    def create(
        self,
        *,
        name: str,
        scopes: list[str] | None = None,
        allowed_analytics_models: list[str] | None = None,
        authority_profile: str = "smart",
        expires_at: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        clean_name = str(name).strip()
        if not clean_name or len(clean_name) > 120:
            raise WorkerAccessError("name is required and must be at most 120 characters")
        selected_scopes = list(dict.fromkeys(str(item).strip() for item in (scopes or WORKER_SCOPES) if str(item).strip()))
        if not selected_scopes or not set(selected_scopes).issubset(WORKER_SCOPES):
            raise WorkerAccessError("unsupported Worker Access Key scope")
        profile = str(authority_profile or "smart").strip().lower()
        if profile not in AUTHORITY_PROFILES:
            raise WorkerAccessError("unsupported authority profile")
        if expires_at is not None and float(expires_at) <= time.time():
            raise WorkerAccessError("expires_at must be in the future")
        secret = "pck_" + secrets.token_urlsafe(32)
        item = {
            "key_id": "wak_" + uuid.uuid4().hex[:20],
            "prefix": secret[:12],
            "secret_hash": self._hash(secret),
            "name": clean_name,
            "scopes": selected_scopes,
            "allowed_analytics_models": list(dict.fromkeys(str(item).strip() for item in (allowed_analytics_models or []) if str(item).strip())),
            "authority_profile": profile,
            "expires_at": float(expires_at) if expires_at is not None else None,
            "last_used_at": None,
            "revoked_at": None,
            "created_at": time.time(),
        }
        with self._lock:
            records = self._read()
            records[item["key_id"]] = item
            self._write(records)
        return self._public(item), secret

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((self._public(item) for item in self._read().values()), key=lambda item: item["created_at"] or 0, reverse=True)

    def authenticate(self, secret: str, scope: str) -> dict[str, Any] | None:
        token = str(secret or "").strip()
        if not token:
            return None
        static = str(os.getenv("PUDDINGCLAW_HEADLESS_TOKEN") or "").strip()
        if static and hmac.compare_digest(token, static):
            return {
                "key_id": "static-dev-key",
                "scopes": sorted(WORKER_SCOPES),
                "allowed_analytics_models": [],
                "authority_profile": str(os.getenv("PUDDINGCLAW_HEADLESS_AUTHORITY_PROFILE", "smart")),
            }
        digest = self._hash(token)
        with self._lock:
            records = self._read()
            for key_id, item in records.items():
                if not hmac.compare_digest(str(item.get("secret_hash") or ""), digest):
                    continue
                if item.get("revoked_at") or (item.get("expires_at") and float(item["expires_at"]) <= time.time()):
                    return None
                if scope not in set(item.get("scopes") or []):
                    return None
                item["last_used_at"] = time.time()
                records[key_id] = item
                self._write(records)
                return dict(item)
        return None

    def rotate(self, key_id: str) -> tuple[dict[str, Any], str]:
        with self._lock:
            records = self._read()
            old = records.get(key_id)
            if not old:
                raise WorkerAccessError("Worker Access Key not found")
            old["revoked_at"] = time.time()
            self._write(records)
            return self.create(
                name=str(old.get("name") or "Worker"),
                scopes=list(old.get("scopes") or []),
                allowed_analytics_models=list(old.get("allowed_analytics_models") or []),
                authority_profile=str(old.get("authority_profile") or "smart"),
                expires_at=old.get("expires_at"),
            )

    def revoke(self, key_id: str) -> None:
        with self._lock:
            records = self._read()
            item = records.get(key_id)
            if not item:
                raise WorkerAccessError("Worker Access Key not found")
            item["revoked_at"] = time.time()
            self._write(records)


worker_access_store = WorkerAccessStore()
