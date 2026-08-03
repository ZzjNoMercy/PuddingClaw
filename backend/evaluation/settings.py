"""Evaluation-only provider settings with secret-safe reads."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LangSmithSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    endpoint: str = "https://api.smith.langchain.com"
    project: str = "puddingclaw-evaluation"
    workspace_id: str | None = None
    api_key: str = Field(default="", repr=False)
    redaction_profile: Literal["default-v1"] = "default-v1"
    request_timeout_seconds: int = Field(default=10, ge=1, le=120)
    max_retries: int = Field(default=2, ge=0, le=5)
    trace_finalize_timeout_seconds: int = Field(default=5, ge=1, le=60)
    projection_timeout_seconds: int = Field(default=120, ge=5, le=600)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LangSmith endpoint must be an absolute HTTP(S) URL")
        return value.rstrip("/")

    @field_validator("project")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()


class EvaluationSettingsStore:
    _CREDENTIAL_ID = "puddingclaw-langsmith"

    def __init__(self, path: Path | str | None = None) -> None:
        default = Path(__file__).resolve().parent.parent / "data" / "evaluation-settings.json"
        self.path = Path(path or os.getenv("PUDDINGCLAW_EVALUATION_SETTINGS") or default)

    def _load_file(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.path.is_file():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
        return payload

    def load(self) -> LangSmithSettings:
        payload = self._load_file()
        from provider_registry import LocalCredentialStore

        credential_store = LocalCredentialStore()
        reference = str(payload.pop("api_key_ref", "") or f"local-file://{self._CREDENTIAL_ID}")
        legacy_key = str(payload.pop("api_key", "") or "")
        if legacy_key:
            reference = credential_store.put(self._CREDENTIAL_ID, legacy_key)
        stored_key = credential_store.get(reference)
        if stored_key:
            payload["api_key"] = stored_key
        env_key = os.getenv("LANGSMITH_API_KEY")
        env_endpoint = os.getenv("LANGSMITH_ENDPOINT")
        env_project = os.getenv("LANGSMITH_PROJECT")
        if env_key:
            payload["api_key"] = env_key
        if env_endpoint:
            payload["endpoint"] = env_endpoint
        if env_project:
            payload["project"] = env_project
        return LangSmithSettings.model_validate(payload)

    def public(self) -> dict[str, Any]:
        settings = self.load()
        data = settings.model_dump(exclude={"api_key"})
        data["api_key_configured"] = bool(settings.api_key)
        data["api_key_masked"] = f"••••{settings.api_key[-4:]}" if settings.api_key else None
        return data

    def update(self, updates: dict[str, Any], *, clear_api_key: bool = False) -> dict[str, Any]:
        from provider_registry import LocalCredentialStore

        raw = self._load_file()
        raw.pop("api_key_ref", None)
        raw.pop("api_key", None)
        current = LangSmithSettings.model_validate(raw).model_dump()
        api_key = updates.pop("api_key", None)
        current.update(updates)
        credential_store = LocalCredentialStore()
        if clear_api_key:
            current["api_key"] = ""
            credential_store.delete(self._CREDENTIAL_ID)
        elif api_key:
            current["api_key"] = api_key
            credential_store.put(self._CREDENTIAL_ID, api_key)
        validated = LangSmithSettings.model_validate(current)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="eval-settings-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                payload = validated.model_dump(exclude={"api_key"})
                payload["api_key_ref"] = f"local-file://{self._CREDENTIAL_ID}"
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return self.public()


_store: EvaluationSettingsStore | None = None


def get_evaluation_settings_store() -> EvaluationSettingsStore:
    global _store
    if _store is None:
        _store = EvaluationSettingsStore()
    return _store
