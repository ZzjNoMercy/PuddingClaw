"""Immutable source references and deterministic artifact materialization."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from graph.session_manager import session_manager

_SLOT_RE = re.compile(
    r"/\*\{\{SLOT:(?P<slot>[A-Za-z0-9_-]+)\|(?P<renderer>[a-z_]+)\}\}\*/"
    r"\s*(?P<placeholder>\[\]|\{\}|\"\"|'')"
)
_RENDERERS = frozenset({"identity", "json", "csv", "js_array", "text"})


class SourceMaterializationError(RuntimeError):
    """Fail-closed source registration or materialization error."""

    def __init__(self, code: str, detail: str, *, next_action: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.next_action = next_action

    def as_dict(self) -> dict[str, str]:
        return {
            "status": "error",
            "error_code": self.code,
            "error": self.detail,
            "next_action": self.next_action,
        }


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _expiry_timestamp(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceMaterializationError(
            "invalid_source_expiry",
            f"invalid source expiry: {value}",
            next_action="reissue_source_reference",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def public_source_reference(source: dict[str, Any]) -> dict[str, Any]:
    """Project model-safe source metadata without its server locator."""

    return {
        key: value
        for key, value in source.items()
        if key not in {"locator", "owner"}
    }


def register_file_source_reference(
    *,
    session_id: str,
    kind: str,
    file_path: str | Path,
    media_type: str,
    schema_ref: str = "",
    row_count: int | None = None,
    producer_receipt_ids: list[str] | None = None,
    expires_at: str | float | None = None,
    source_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register one server-owned immutable file as a SourceReference."""

    supplied_path = Path(file_path).expanduser()
    if supplied_path.is_symlink():
        raise SourceMaterializationError(
            "invalid_source_locator",
            f"source locator must not be a symlink: {supplied_path}",
            next_action="register_regular_source_file",
        )
    path = supplied_path.resolve(strict=True)
    if not path.is_file():
        raise SourceMaterializationError(
            "invalid_source_locator",
            f"source is not a regular server file: {path}",
            next_action="register_regular_source_file",
        )
    content = path.read_bytes()
    content_sha256 = _sha256(content)
    ref = source_ref or (
        "source-"
        + hashlib.sha256(
            (
                f"{session_id}\0{kind}\0{path}\0{content_sha256}\0"
                f"{schema_ref}\0{row_count}"
            ).encode("utf-8")
        ).hexdigest()[:24]
    )
    source = {
        "source_ref": ref,
        "kind": str(kind),
        "content_sha256": content_sha256,
        "media_type": str(media_type),
        "schema_ref": str(schema_ref),
        "size_bytes": len(content),
        "row_count": row_count,
        "producer_receipt_ids": sorted(
            {str(item) for item in producer_receipt_ids or [] if str(item)}
        ),
        "expires_at": expires_at,
        "created_at": time.time(),
        "locator": {
            "type": "server_file",
            "path": str(path),
        },
        "metadata": dict(metadata or {}),
    }
    _expiry_timestamp(expires_at)
    existing = session_manager.get_source_reference(session_id, ref)
    if isinstance(existing, dict):
        comparable_keys = {
            "source_ref",
            "kind",
            "content_sha256",
            "media_type",
            "schema_ref",
            "size_bytes",
            "row_count",
            "producer_receipt_ids",
            "expires_at",
            "locator",
            "metadata",
        }
        if all(existing.get(key) == source.get(key) for key in comparable_keys):
            return existing
    return session_manager.register_source_reference(session_id, source)


def resolve_source_bytes(
    session_id: str,
    source_ref: str,
) -> tuple[dict[str, Any], bytes]:
    source = session_manager.get_source_reference(session_id, source_ref)
    if not isinstance(source, dict):
        raise SourceMaterializationError(
            "source_not_found",
            f"unknown source reference: {source_ref}",
            next_action="obtain_fresh_source_reference",
        )
    expires_at = _expiry_timestamp(source.get("expires_at"))
    if expires_at is not None and expires_at <= time.time():
        raise SourceMaterializationError(
            "source_expired",
            f"source reference expired: {source_ref}",
            next_action="obtain_fresh_source_reference",
        )
    locator = source.get("locator")
    if not isinstance(locator, dict) or locator.get("type") != "server_file":
        raise SourceMaterializationError(
            "unsupported_source_locator",
            f"source reference has no registered resolver: {source_ref}",
            next_action="install_source_adapter",
        )
    try:
        supplied_path = Path(str(locator.get("path") or "")).expanduser()
        if supplied_path.is_symlink():
            raise OSError("registered source locator became a symlink")
        path = supplied_path.resolve(strict=True)
        content = path.read_bytes()
    except OSError as exc:
        raise SourceMaterializationError(
            "source_payload_unavailable",
            f"source payload is unavailable: {exc}",
            next_action="obtain_fresh_source_reference",
        ) from exc
    actual = _sha256(content)
    expected = str(source.get("content_sha256") or "")
    if actual != expected:
        raise SourceMaterializationError(
            "source_hash_mismatch",
            f"source payload changed; expected {expected}, current {actual}",
            next_action="obtain_fresh_source_reference",
        )
    return source, content


def _records_from_source(
    source: dict[str, Any],
    content: bytes,
) -> list[dict[str, Any]]:
    media_type = str(source.get("media_type") or "").lower()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceMaterializationError(
            "source_not_utf8",
            "structured renderer requires UTF-8 source bytes",
            next_action="use_identity_renderer",
        ) from exc
    if "jsonl" in media_type or "ndjson" in media_type:
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif "json" in media_type:
        decoded = json.loads(text)
        values = decoded if isinstance(decoded, list) else [decoded]
    elif "csv" in media_type:
        values = list(csv.DictReader(io.StringIO(text)))
    else:
        raise SourceMaterializationError(
            "unsupported_structured_media_type",
            f"structured renderer does not support {media_type or 'unknown'}",
            next_action="use_identity_or_text_renderer",
        )
    if not all(isinstance(item, dict) for item in values):
        raise SourceMaterializationError(
            "source_schema_mismatch",
            "structured source must contain objects/rows",
            next_action="choose_compatible_renderer",
        )
    return [dict(item) for item in values]


def render_source(
    source: dict[str, Any],
    content: bytes,
    *,
    renderer: str,
    projection: list[str] | None = None,
    expected_schema_ref: str | None = None,
    expected_item_count: int | None = None,
) -> tuple[bytes, int | None]:
    """Render source bytes deterministically without business inference."""

    if renderer not in _RENDERERS:
        raise SourceMaterializationError(
            "unknown_renderer",
            f"renderer is not registered: {renderer}",
            next_action="choose_registered_renderer",
        )
    actual_schema = str(source.get("schema_ref") or "")
    if expected_schema_ref and expected_schema_ref != actual_schema:
        raise SourceMaterializationError(
            "source_schema_mismatch",
            f"expected schema {expected_schema_ref}, source has {actual_schema}",
            next_action="choose_compatible_source",
        )
    if renderer == "identity":
        item_count = source.get("row_count")
        rendered = content
    elif renderer == "text":
        try:
            rendered = content.decode("utf-8").encode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceMaterializationError(
                "source_not_utf8",
                "text renderer requires UTF-8 source bytes",
                next_action="use_identity_renderer",
            ) from exc
        item_count = source.get("row_count")
    else:
        records = _records_from_source(source, content)
        if projection:
            missing = sorted(
                {
                    field
                    for field in projection
                    if any(field not in row for row in records)
                }
            )
            if missing:
                raise SourceMaterializationError(
                    "projection_field_missing",
                    f"projection fields are missing: {missing}",
                    next_action="fix_projection",
                )
            records = [
                {field: row.get(field) for field in projection}
                for row in records
            ]
        item_count = len(records)
        if renderer in {"json", "js_array"}:
            serialized = json.dumps(
                records,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if renderer == "js_array":
                # Keep the array safe in both external JavaScript and inline
                # HTML script contexts without changing its JSON semantics.
                serialized = (
                    serialized.replace("&", "\\u0026")
                    .replace("<", "\\u003c")
                    .replace(">", "\\u003e")
                    .replace("\u2028", "\\u2028")
                    .replace("\u2029", "\\u2029")
                )
            rendered = (serialized + "\n").encode("utf-8")
        else:
            fields = list(projection or (records[0].keys() if records else []))
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(
                stream,
                fieldnames=fields,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(records)
            rendered = stream.getvalue().encode("utf-8")
    if expected_item_count is not None and item_count != expected_item_count:
        raise SourceMaterializationError(
            "source_item_count_mismatch",
            f"expected {expected_item_count} items, rendered {item_count}",
            next_action="recheck_source_query_or_expectation",
        )
    return rendered, int(item_count) if item_count is not None else None


def fill_typed_slot(
    template: str,
    *,
    slot_id: str,
    renderer: str,
    rendered: bytes,
) -> str:
    try:
        replacement = rendered.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise SourceMaterializationError(
            "slot_requires_utf8",
            "typed slot materialization requires UTF-8 renderer output",
            next_action="use_file_destination",
        ) from exc
    matches = [
        match
        for match in _SLOT_RE.finditer(template)
        if match.group("slot") == slot_id
    ]
    if len(matches) != 1:
        raise SourceMaterializationError(
            "slot_cardinality_mismatch",
            f"slot {slot_id!r} must occur exactly once; found {len(matches)}",
            next_action="fix_template_slot",
        )
    match = matches[0]
    declared_renderer = match.group("renderer")
    if declared_renderer != renderer:
        raise SourceMaterializationError(
            "slot_renderer_mismatch",
            f"slot requires {declared_renderer}, received {renderer}",
            next_action="choose_declared_slot_renderer",
        )
    return template[: match.start()] + replacement + template[match.end() :]


def persist_materialization_receipt(
    *,
    session_id: str,
    run_id: str,
    query_id: str,
    source: dict[str, Any],
    renderer: str,
    target_path: str,
    target_sha256: str,
    item_count: int | None,
    mutation_receipt_id: str,
    template_sha256: str | None = None,
    slot_id: str | None = None,
    validation_receipt_ids: list[str] | None = None,
) -> dict[str, Any]:
    normalized_validation_ids = sorted(
        {str(item) for item in validation_receipt_ids or [] if str(item)}
    )
    stable_payload = {
        "kind": "materialization_receipt",
        "session_id": session_id,
        "run_id": run_id,
        "query_id": query_id,
        "source_ref": source.get("source_ref"),
        "source_kind": source.get("kind"),
        "source_sha256": source.get("content_sha256"),
        "schema_ref": source.get("schema_ref"),
        "renderer": f"{renderer}/v1",
        "item_count": item_count,
        "target_path": target_path,
        "target_sha256": target_sha256,
        "template_sha256": template_sha256,
        "slot_id": slot_id,
        "mutation_receipt_id": mutation_receipt_id,
        "producer_receipt_ids": list(source.get("producer_receipt_ids") or []),
        "validation_receipt_ids": normalized_validation_ids,
        "status": "completed",
    }
    seed = json.dumps(stable_payload, sort_keys=True, separators=(",", ":"))
    receipt = {
        **stable_payload,
        "materialization_receipt_id": (
            "materialization-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        ),
        "created_at": time.time(),
    }
    return session_manager.append_materialization_receipt(session_id, receipt)


__all__ = [
    "SourceMaterializationError",
    "fill_typed_slot",
    "persist_materialization_receipt",
    "public_source_reference",
    "register_file_source_reference",
    "render_source",
    "resolve_source_bytes",
]
