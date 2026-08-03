"""Optional LangSmith Dataset projection adapter.

The adapter imports LangSmith lazily. Local snapshots remain authoritative and
sync failures are recorded without mutating the Dataset.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from urllib3.util import Retry

from .contracts import DatasetBundle, DatasetStatus
from .privacy import DEFAULT_REDACTION_PROFILE, redact
from .repository import EvaluationRepository
from .settings import LangSmithSettings

_EXAMPLE_NAMESPACE = uuid.UUID("ca29ad55-5202-4f32-87d8-8b3742689bbc")


def _redact(
    value: Any,
    *,
    max_string: int = 8_000,
    profile: str = DEFAULT_REDACTION_PROFILE,
) -> Any:
    """Backward-compatible internal entry point for the named redaction registry."""

    return redact(value, profile=profile, max_string=max_string)


def langsmith_client_kwargs(settings: LangSmithSettings) -> dict[str, Any]:
    """Build bounded LangSmith client settings shared by every projection path."""

    retries = Retry(
        total=settings.max_retries,
        connect=settings.max_retries,
        read=settings.max_retries,
        status=settings.max_retries,
        backoff_factor=0.25,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=None,
        raise_on_status=False,
    )
    return {
        "api_url": settings.endpoint,
        "api_key": settings.api_key,
        "workspace_id": settings.workspace_id,
        "timeout_ms": settings.request_timeout_seconds * 1_000,
        "retry_config": retries,
    }


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-").lower()
    return cleaned[:80] or "dataset"


class LangSmithDatasetAdapter:
    def __init__(
        self,
        repository: EvaluationRepository,
        settings: LangSmithSettings,
        *,
        client: Any | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.settings.api_key:
                raise ValueError("LangSmith API Key is not configured")
            from langsmith import Client

            self._client = Client(**langsmith_client_kwargs(self.settings))
        return self._client

    def test_connection(self) -> dict[str, Any]:
        datasets = list(self.client.list_datasets(limit=1))
        return {
            "ok": True,
            "endpoint": self.settings.endpoint,
            "workspace_id": self.settings.workspace_id,
            "dataset_access": True,
            "visible_dataset_count_sample": len(datasets),
        }

    def sync_dataset(self, bundle: DatasetBundle) -> dict[str, Any]:
        dataset = bundle.dataset
        if dataset.status != DatasetStatus.PUBLISHED or not bundle.version_id:
            raise ValueError("Only a published Dataset version can be synchronized")
        classification = str(dataset.metadata.get("data_classification") or "internal").lower()
        if classification not in {"public", "internal", "sensitive", "restricted"}:
            classification = "restricted"
        if classification in {"sensitive", "restricted"} or any(
            case.data_classification in {"sensitive", "restricted"} for case in dataset.cases
        ):
            raise ValueError("Sensitive or Restricted Dataset cannot be projected to LangSmith")

        mapping = self.repository.get_remote_mapping("langsmith", "dataset", dataset.dataset_id, bundle.version_id)
        remote_name = f"puddingclaw/{_slug(dataset.name)}/{dataset.current_version}"
        projection = []
        enabled_cases = [case for case in dataset.cases if case.enabled]
        for case in enabled_cases:
            projection.append(
                {
                    "id": str(uuid.uuid5(_EXAMPLE_NAMESPACE, f"{bundle.version_id}:{case.revision_id}")),
                    "inputs": _redact(
                        {
                            "puddingclaw_case_id": case.case_id,
                            "input": case.input.model_dump(mode="json"),
                            "setup": {
                                "clock": case.setup.clock.isoformat() if case.setup.clock else None,
                                "timezone": case.setup.timezone,
                            },
                        },
                        profile=self.settings.redaction_profile,
                    ),
                    "outputs": _redact(
                        {"expectations": case.expectations.model_dump(mode="json")},
                        profile=self.settings.redaction_profile,
                    ),
                    "metadata": {
                        "puddingclaw_dataset_id": dataset.dataset_id,
                        "puddingclaw_version_id": bundle.version_id,
                        "puddingclaw_case_revision_id": case.revision_id,
                        "criticality": case.criticality,
                        "tags": case.tags,
                    },
                }
            )
        projection_hash = hashlib.sha256(
            json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if mapping and mapping.get("status") == "synced" and mapping.get("content_hash") == projection_hash:
            try:
                existing = self.client.read_dataset(dataset_id=mapping["remote_id"])
            except Exception:
                existing = None
            if existing is not None:
                return {**mapping, "idempotent": True, "projection_hash": projection_hash}

        remote_dataset = None
        if mapping and mapping.get("remote_id"):
            try:
                remote_dataset = self.client.read_dataset(dataset_id=mapping["remote_id"])
            except Exception:
                remote_dataset = None
        if remote_dataset is None:
            matches = list(
                self.client.list_datasets(
                    metadata={
                        "puddingclaw_dataset_id": dataset.dataset_id,
                        "puddingclaw_version_id": bundle.version_id,
                    },
                    limit=2,
                )
            )
            remote_dataset = matches[0] if matches else None
        if remote_dataset is None:
            remote_dataset = self.client.create_dataset(
                remote_name,
                description=_redact(
                    dataset.description, profile=self.settings.redaction_profile
                ),
                metadata={
                    "puddingclaw_dataset_id": dataset.dataset_id,
                    "puddingclaw_version_id": bundle.version_id,
                    "local_snapshot_hash": bundle.checksum,
                    "projection_hash": projection_hash,
                    "redaction_profile": self.settings.redaction_profile,
                    "protocol_version": dataset.protocol_version,
                },
            )
        try:
            self.client.create_examples(dataset_id=remote_dataset.id, examples=projection)
            self.repository.save_remote_mapping(
                provider="langsmith",
                local_type="dataset",
                local_id=dataset.dataset_id,
                version_id=bundle.version_id,
                remote_id=str(remote_dataset.id),
                remote_name=remote_dataset.name,
                content_hash=projection_hash,
                status="synced",
            )
            for case, example in zip(enabled_cases, projection, strict=True):
                self.repository.save_remote_mapping(
                    provider="langsmith",
                    local_type="case",
                    local_id=case.case_id,
                    version_id=bundle.version_id,
                    remote_id=example["id"],
                    remote_name=None,
                    content_hash=case.revision_id,
                    status="synced",
                )
        except Exception as exc:
            self.repository.save_remote_mapping(
                provider="langsmith",
                local_type="dataset",
                local_id=dataset.dataset_id,
                version_id=bundle.version_id,
                remote_id=str(remote_dataset.id),
                remote_name=getattr(remote_dataset, "name", remote_name),
                content_hash=projection_hash,
                status="sync_failed",
                error=str(_redact(str(exc)))[:1000],
            )
            raise
        return {
            "provider": "langsmith",
            "status": "synced",
            "remote_id": str(remote_dataset.id),
            "remote_name": remote_dataset.name,
            "local_snapshot_hash": bundle.checksum,
            "projection_hash": projection_hash,
            "case_count": len(projection),
            "idempotent": False,
        }
