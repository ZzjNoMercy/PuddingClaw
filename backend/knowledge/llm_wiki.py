"""Deterministic runtime for the LLM Wiki file protocol.

The LLM may propose pages, but it never writes the published Wiki directly.
This module owns immutable raw snapshots, candidate validation, publishing,
read-only query context, and the gbrain compile boundary.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from gbrain_runtime import (
    apply_gbrain_ai_environment,
    gbrain_subprocess_environment,
    resolve_gbrain_ai_runtime,
    resolve_gbrain_binary,
)
from knowledge.brain_schema import BrainSchemaError, BrainSchemaService, workspace_page_prefixes
from postgres_dependencies import inspect_pgvector_dsn_sync

SLUG_SEGMENT_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
SLUG_PATTERN = rf"{SLUG_SEGMENT_PATTERN}(?:/{SLUG_SEGMENT_PATTERN})*"
TYPED_SLUG_PATTERN = rf"{SLUG_SEGMENT_PATTERN}(?:/{SLUG_SEGMENT_PATTERN})+"
SLUG_RE = re.compile(rf"^{SLUG_PATTERN}$")
WIKILINK_TARGET_RE = re.compile(rf"^(?P<slug>{TYPED_SLUG_PATTERN})$")
WIKILINK_RE = re.compile(rf"\[\[({TYPED_SLUG_PATTERN})(?:\|[^\]]+)?\]\]")
ANY_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
LEGACY_WORKSPACE_WIKILINK_RE = re.compile(
    rf"\[\[wiki/(?P<slug>{TYPED_SLUG_PATTERN})(?P<label>\|[^\]]+)?\]\]"
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORD_RE = re.compile(r"[\w\u4e00-\u9fff-]+", re.UNICODE)
SPECIAL_WIKI_FILES = frozenset({"index.md", "log.md"})


def _empty_gbrain_import_status(*, available: bool = False) -> dict[str, Any]:
    return {
        "available": available,
        "counts": {"pages": 0, "links": 0, "chunks": 0, "imports": 0},
        "records": [],
    }


def _gbrain_postgres_summary(config_path: Path) -> dict[str, Any]:
    """Return non-secret PostgreSQL connection metadata for settings UI."""

    summary: dict[str, Any] = {
        "configured": False,
        "host": "",
        "port": 5432,
        "database": "",
        "username": "",
    }
    if not config_path.is_file():
        return summary
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        database_url = str(config.get("database_url") or config.get("url") or "").strip()
        parsed = urlsplit(database_url)
        if parsed.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
            return summary
        database = unquote(parsed.path.lstrip("/").split("/", 1)[0])
        if not parsed.hostname or not database:
            return summary
        summary.update(
            {
                "configured": True,
                "host": parsed.hostname,
                "port": parsed.port or 5432,
                "database": database,
                "username": unquote(parsed.username or ""),
            }
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return summary
    return summary


async def _read_gbrain_import_status(database_url: str, *, limit: int = 10) -> dict[str, Any]:
    """Read the gbrain runtime audit log without exposing its database URL."""

    import asyncpg

    normalized_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn=normalized_url, timeout=2)
    try:
        counts = await connection.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM pages) AS pages,
              (SELECT count(*) FROM links) AS links,
              (SELECT count(*) FROM content_chunks) AS chunks,
              (SELECT count(*) FROM ingest_log) AS imports
            """
        )
        rows = await connection.fetch(
            """
            SELECT id, source_id, source_type, pages_updated, summary, created_at
            FROM ingest_log
            ORDER BY created_at DESC, id DESC
            LIMIT $1
            """,
            max(1, min(int(limit), 50)),
        )
    finally:
        await connection.close()

    records: list[dict[str, Any]] = []
    for row in rows:
        pages_updated: Any = row["pages_updated"]
        if isinstance(pages_updated, str):
            try:
                pages_updated = json.loads(pages_updated)
            except json.JSONDecodeError:
                pages_updated = []
        if not isinstance(pages_updated, list):
            pages_updated = []
        created_at = row["created_at"]
        records.append(
            {
                "id": int(row["id"]),
                "source_id": str(row["source_id"] or ""),
                "source_type": str(row["source_type"] or ""),
                "pages_updated": [str(item) for item in pages_updated],
                "summary": str(row["summary"] or ""),
                "created_at": (
                    created_at.isoformat()
                    if isinstance(created_at, datetime)
                    else str(created_at or "")
                ),
            }
        )
    return {
        "available": True,
        "counts": {
            "pages": int(counts["pages"] or 0),
            "links": int(counts["links"] or 0),
            "chunks": int(counts["chunks"] or 0),
            "imports": int(counts["imports"] or 0),
        },
        "records": records,
    }


def _wiki_page_paths(directory: Path) -> list[Path]:
    """Return Wiki pages recursively, excluding only root generated files."""

    return sorted(
        path
        for path in directory.rglob("*.md")
        if path.relative_to(directory).as_posix() not in SPECIAL_WIKI_FILES
    )


def _wiki_slug(directory: Path, path: Path) -> str:
    return path.relative_to(directory).with_suffix("").as_posix()


class LlmWikiError(RuntimeError):
    """Raised when a Wiki operation violates its deterministic contract."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_segment(value: str, *, label: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    if not normalized or normalized in {".", ".."}:
        raise LlmWikiError(f"{label} must contain a safe identifier")
    return normalized[:100]


def _identity_segment(value: str, *, label: str) -> str:
    """Keep paths readable without allowing normalized identifiers to collide."""

    safe = _safe_segment(value, label=label)
    identity = _sha256_bytes(value.encode("utf-8"))[:10]
    return f"{safe[:88]}-{identity}"


def _single_line(value: str, *, fallback: str) -> str:
    normalized = " ".join(value.splitlines()).strip()
    return normalized or fallback


def _frontmatter(content: str, *, source: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        raise LlmWikiError(f"{source}: missing YAML frontmatter")
    end = content.find("\n---\n", 4)
    if end < 0:
        raise LlmWikiError(f"{source}: unterminated YAML frontmatter")
    try:
        value = yaml.safe_load(content[4:end])
    except yaml.YAMLError as exc:
        raise LlmWikiError(f"{source}: invalid YAML frontmatter: {exc}") from exc
    if not isinstance(value, dict):
        raise LlmWikiError(f"{source}: frontmatter must be an object")
    return value, content[end + 5 :]


def _canonical_raw_source(value: Any, known_sources: set[str] | None = None) -> str:
    """Return the manifest-relative source path used by the Wiki contract."""

    source = str(value or "").strip()
    if known_sources is not None and source in known_sources:
        return source
    legacy_candidate = source[4:] if source.startswith("raw/") else source
    if known_sources is None or legacy_candidate in known_sources:
        return legacy_candidate
    return source


def _render_frontmatter(metadata: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{header}\n---\n{body}"


def _rewrite_legacy_workspace_wikilinks(content: str) -> str:
    return LEGACY_WORKSPACE_WIKILINK_RE.sub(
        lambda match: f"[[{match.group('slug')}{match.group('label') or ''}]]",
        content,
    )


class LlmWikiService:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self.schema = BrainSchemaService(self.base_dir)

    @property
    def root(self) -> Path:
        return self.schema.brain_root

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    @property
    def raw_manifest_path(self) -> Path:
        return self.raw_dir / "manifest.jsonl"

    @property
    def raw_lock_path(self) -> Path:
        return self.root / ".puddingclaw" / "locks" / "raw-manifest.lock"

    @property
    def publish_lock_path(self) -> Path:
        return self.root / ".puddingclaw" / "locks" / "wiki-publish.lock"

    @property
    def brain_write_lock_path(self) -> Path:
        return self.root / ".puddingclaw" / "locks" / "brain-write.lock"

    @property
    def gbrain_runtime_home(self) -> Path:
        configured = os.getenv("PUDDINGCLAW_GBRAIN_HOME", "").strip()
        return Path(configured).expanduser().resolve() if configured else self.root / ".puddingclaw" / "gbrain-home"

    def _require_initialized(self) -> dict[str, Any]:
        try:
            return self.schema.bundle()
        except BrainSchemaError as exc:
            raise LlmWikiError(str(exc)) from exc

    def _manifest_records(self) -> list[dict[str, Any]]:
        if not self.raw_manifest_path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line_number, line in enumerate(self.raw_manifest_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LlmWikiError(f"raw/manifest.jsonl:{line_number}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise LlmWikiError(f"raw/manifest.jsonl:{line_number}: record must be an object")
            result.append(value)
        return result

    def snapshot_raw(
        self,
        *,
        source_id: str,
        asset_id: str,
        title: str,
        content: str,
        source_path: str | None = None,
    ) -> dict[str, Any]:
        """Materialize an immutable normalized raw snapshot and manifest row."""

        normalized_content = content if content.endswith("\n") else f"{content}\n"
        return self.snapshot_raw_bytes(
            source_id=source_id,
            asset_id=asset_id,
            title=title,
            content=normalized_content.encode("utf-8"),
            source_path=source_path,
        )

    def snapshot_raw_bytes(
        self,
        *,
        source_id: str,
        asset_id: str,
        title: str,
        content: bytes,
        source_path: str | None = None,
    ) -> dict[str, Any]:
        """Copy an immutable UTF-8 Markdown snapshot byte-for-byte into raw/."""

        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LlmWikiError("raw Markdown must be UTF-8 encoded") from exc
        if not decoded.strip():
            raise LlmWikiError("raw content must not be empty")
        with _file_lock(self.raw_lock_path):
            return self._snapshot_raw_bytes_unlocked(
                source_id=source_id,
                asset_id=asset_id,
                title=title,
                content=content,
                source_path=source_path,
            )

    def snapshot_raw_file(
        self,
        *,
        source_id: str,
        asset_id: str,
        title: str,
        path: Path,
        source_path: str | None = None,
    ) -> dict[str, Any]:
        """Copy a finalized Markdown file into raw/ without changing its bytes."""

        resolved = path.resolve()
        if resolved.suffix.lower() not in {".md", ".markdown"}:
            raise LlmWikiError("only Markdown files can be copied into LLM Wiki Raw")
        if not resolved.is_file():
            raise LlmWikiError(f"Markdown source file not found: {resolved}")
        return self.snapshot_raw_bytes(
            source_id=source_id,
            asset_id=asset_id,
            title=title,
            content=resolved.read_bytes(),
            source_path=source_path or str(resolved),
        )

    def _snapshot_raw_bytes_unlocked(
        self,
        *,
        source_id: str,
        asset_id: str,
        title: str,
        content: bytes,
        source_path: str | None = None,
    ) -> dict[str, Any]:
        bundle = self._require_initialized()
        source = _identity_segment(source_id, label="source_id")
        asset = _identity_segment(asset_id, label="asset_id")
        digest = _sha256_bytes(content)
        relative = Path(source) / f"{asset}-{digest[:12]}.md"
        target = self.raw_dir / relative
        if target.exists() and target.read_bytes() != content:
            raise LlmWikiError(f"immutable raw snapshot collision: {relative}")
        if not target.exists():
            _atomic_write_bytes(target, content)

        existing = self._manifest_records()
        for record in existing:
            if record.get("snapshot_path") == relative.as_posix() and record.get("sha256") == digest:
                return record
        record = {
            "source_id": source_id,
            "asset_id": asset_id,
            "title": title.strip() or asset_id,
            "source_path": source_path,
            "snapshot_path": relative.as_posix(),
            "sha256": digest,
            "size_bytes": len(content),
            "bundle_hash": bundle["bundle_hash"],
            "created_at": datetime.now(UTC).isoformat(),
        }
        lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in [*existing, record]]
        _atomic_write(self.raw_manifest_path, "\n".join(lines) + "\n")
        return record

    def raw_status_for_source(self, *, source_path: str, content_sha256: str = "") -> dict[str, Any]:
        """Return whether the current source bytes already have an immutable Raw snapshot."""

        matching = [
            record
            for record in self._manifest_records()
            if str(record.get("source_path") or "") == source_path
        ]
        current = next(
            (
                record
                for record in reversed(matching)
                if content_sha256 and str(record.get("sha256") or "") == content_sha256
            ),
            None,
        )
        latest = matching[-1] if matching else None
        return {
            "available": current is not None,
            "snapshot": current,
            "latest_snapshot": latest,
            "changed_since_snapshot": bool(latest and current is None),
        }

    def _resolve_raw_record(self, record: dict[str, Any]) -> tuple[str, Path, str]:
        relative = str(record.get("snapshot_path") or "")
        expected = str(record.get("sha256") or "")
        path = (self.raw_dir / relative).resolve()
        try:
            path.relative_to(self.raw_dir.resolve())
        except ValueError as exc:
            raise LlmWikiError(f"raw manifest path escapes raw/: {relative}") from exc
        return relative, path, expected

    def freeze_ingest_inputs(self, raw_paths: list[str]) -> dict[str, Any]:
        """Validate immutable Raw metadata for a queued job without loading document bodies."""

        bundle = self._require_initialized()
        selected_paths = list(dict.fromkeys(str(path).strip() for path in raw_paths if str(path).strip()))
        records_by_path = {
            str(record.get("snapshot_path") or ""): record
            for record in self._manifest_records()
        }
        missing = [path for path in selected_paths if path not in records_by_path]
        if missing:
            raise LlmWikiError(f"raw snapshots are not in the immutable manifest: {', '.join(missing)}")
        selected: list[dict[str, Any]] = []
        for path in selected_paths:
            record = records_by_path[path]
            _relative, source, expected = self._resolve_raw_record(record)
            if not source.is_file():
                raise LlmWikiError(f"raw snapshot is missing: {path}")
            actual = _sha256_file(source)
            if actual != expected:
                raise LlmWikiError(f"raw snapshot hash drift: {path}")
            selected.append(record)
        return {
            "schema_bundle": bundle,
            "raw_manifest": selected,
        }

    def _raw_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for record in self._manifest_records():
            relative, path, expected = self._resolve_raw_record(record)
            if not path.is_file():
                hashes[relative] = "missing"
            else:
                actual = _sha256_file(path)
                hashes[relative] = actual
                if actual != expected:
                    hashes[relative] = f"mismatch:{expected}:{actual}"
        return hashes

    def _compiled_raw_receipts(self, *, bundle_hash: str) -> dict[str, dict[str, Any]]:
        """Project successful publish receipts into current-Bundle Raw coverage.

        A selected Raw is not necessarily consumed, so the receipt's
        ``consumed_raw_by_page`` map is the authority. Corrupt, failed, stale-
        Bundle, or hash-mismatched receipts never hide a Raw from the queue.
        """

        compiled: dict[str, dict[str, Any]] = {}
        jobs_dir = self.root / ".puddingclaw" / "jobs"
        if not jobs_dir.is_dir():
            return compiled
        for path in sorted(jobs_dir.glob("wiki-*.json")):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if receipt.get("status") != "published" or receipt.get("bundle_hash") != bundle_hash:
                continue
            consumed = receipt.get("consumed_raw_by_page")
            raw_hashes = receipt.get("raw_hashes")
            if not isinstance(consumed, dict) or not isinstance(raw_hashes, dict):
                continue
            job_id = str(receipt.get("job_id") or path.stem)
            published_at = str(receipt.get("published_at") or "")
            for page_slug, raw_paths in consumed.items():
                if not isinstance(raw_paths, list):
                    continue
                for raw_path in raw_paths:
                    snapshot_path = str(raw_path)
                    entry = compiled.setdefault(
                        snapshot_path,
                        {"sha256": raw_hashes.get(snapshot_path), "pages": set(), "job_ids": set(), "compiled_at": ""},
                    )
                    if entry["sha256"] != raw_hashes.get(snapshot_path):
                        continue
                    entry["pages"].add(str(page_slug))
                    entry["job_ids"].add(job_id)
                    entry["compiled_at"] = max(str(entry["compiled_at"]), published_at)
        return compiled

    def workspace_status(self) -> dict[str, Any]:
        """Return the small, user-facing state needed by the LLM Wiki workbench."""

        bundle = self._require_initialized()
        raw_hashes = self._raw_hashes()
        compiled_receipts = self._compiled_raw_receipts(bundle_hash=bundle["bundle_hash"])
        raw_records: list[dict[str, Any]] = []
        for record in self._manifest_records():
            snapshot_path = str(record.get("snapshot_path") or "")
            digest = raw_hashes.get(snapshot_path, "missing")
            compiled = compiled_receipts.get(snapshot_path)
            is_compiled = bool(
                digest == record.get("sha256")
                and compiled
                and compiled.get("sha256") == record.get("sha256")
            )
            raw_records.append(
                {
                    "source_id": record.get("source_id"),
                    "asset_id": record.get("asset_id"),
                    "title": record.get("title"),
                    "snapshot_path": snapshot_path,
                    "sha256": record.get("sha256"),
                    "size_bytes": record.get("size_bytes"),
                    "created_at": record.get("created_at"),
                    "integrity": "ok" if digest == record.get("sha256") else digest,
                    "compiled": is_compiled,
                    "compiled_at": compiled.get("compiled_at") if is_compiled else None,
                    "compiled_pages": sorted(compiled["pages"]) if is_compiled else [],
                    "compiled_job_ids": sorted(compiled["job_ids"]) if is_compiled else [],
                }
            )

        wiki_pages: list[dict[str, Any]] = []
        for path in _wiki_page_paths(self.wiki_dir):
            slug = _wiki_slug(self.wiki_dir, path)
            content = path.read_text(encoding="utf-8")
            try:
                metadata, _body = _frontmatter(content, source=f"wiki/{slug}.md")
                wiki_pages.append(
                    {
                        "slug": slug,
                        "title": str(metadata.get("title") or slug),
                        "type": str(metadata.get("type") or "unknown"),
                        "updated": metadata.get("updated"),
                        "valid": True,
                    }
                )
            except LlmWikiError as exc:
                wiki_pages.append(
                    {"slug": slug, "title": slug, "type": "unknown", "valid": False, "error": str(exc)}
                )

        try:
            gbrain_ai = resolve_gbrain_ai_runtime()
            gbrain_models = {
                "configured": True,
                "embedding": gbrain_ai["embedding"],
                "think": gbrain_ai["think"],
                "error": "",
            }
        except (OSError, ValueError) as exc:
            gbrain_models = {
                "configured": False,
                "embedding": None,
                "think": None,
                "error": str(exc),
            }

        gbrain_config_path = self.gbrain_runtime_home / ".gbrain" / "config.json"
        import_status = self._gbrain_import_status()
        return {
            "brain_root": str(self.root),
            "bundle_hash": bundle["bundle_hash"],
            "schema_version": bundle["custom"]["manifest"]["version"],
            "agents": {
                "path": bundle["agents"]["path"],
                "sha256": bundle["agents"]["sha256"],
                "content": (self.root / "AGENTS.md").read_text(encoding="utf-8"),
            },
            "raw": raw_records,
            "wiki": wiki_pages,
            "files": {
                "index": (self.wiki_dir / "index.md").is_file(),
                "log": (self.wiki_dir / "log.md").is_file(),
            },
            "gbrain": {
                "cli_installed": resolve_gbrain_binary() is not None,
                "postgres_configured": gbrain_config_path.is_file(),
                "postgres": _gbrain_postgres_summary(gbrain_config_path),
                "runtime_home": str(self.gbrain_runtime_home),
                "models": gbrain_models,
                "imports": import_status,
            },
        }

    def _gbrain_import_status(self) -> dict[str, Any]:
        config_path = self.gbrain_runtime_home / ".gbrain" / "config.json"
        if not config_path.is_file():
            return _empty_gbrain_import_status()
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            database_url = str(config.get("database_url") or config.get("url") or "").strip()
            if not database_url.startswith(("postgresql://", "postgres://")):
                return _empty_gbrain_import_status()
            return asyncio.run(_read_gbrain_import_status(database_url))
        except Exception:
            # Workspace status must remain available while PostgreSQL is offline;
            # compile/import endpoints still surface the actionable database error.
            return _empty_gbrain_import_status()

    def operation_context(self, operation: str, *, raw_paths: list[str] | None = None) -> dict[str, Any]:
        """Return the bounded context an Agent receives for Ingest/Query/Lint."""

        normalized = operation.strip().lower()
        if normalized not in {"ingest", "query", "lint"}:
            raise LlmWikiError("operation must be ingest, query, or lint")
        bundle = self._require_initialized()
        context: dict[str, Any] = {
            "operation": normalized,
            "agents_md": (self.root / "AGENTS.md").read_text(encoding="utf-8"),
            "schema_bundle": bundle,
            "index_md": (self.wiki_dir / "index.md").read_text(encoding="utf-8"),
        }
        if normalized == "ingest":
            allowed = set(raw_paths or [])
            records = self._manifest_records()
            known = {str(item.get("snapshot_path")) for item in records}
            unknown = sorted(allowed - known)
            if unknown:
                raise LlmWikiError(f"raw paths are not in the immutable manifest: {', '.join(unknown)}")
            selected = [item for item in records if item.get("snapshot_path") in allowed]
            context["raw_manifest"] = selected
            raw_files: dict[str, str] = {}
            for item in selected:
                relative, path, expected = self._resolve_raw_record(item)
                if not path.is_file() or _sha256_file(path) != expected:
                    raise LlmWikiError(f"raw snapshot integrity check failed: {relative}")
                raw_files[relative] = path.read_text(encoding="utf-8")
            context["raw_files"] = raw_files
        elif normalized == "lint":
            context["raw_manifest"] = self._manifest_records()
        return context

    @staticmethod
    def _page_summary(body: str) -> str:
        paragraphs = re.split(r"\n\s*\n", body)
        for paragraph in paragraphs:
            plain = re.sub(r"^#+\s+.*$", "", paragraph, flags=re.MULTILINE)
            plain = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", plain)
            plain = re.sub(r"[`*_>#-]", "", plain)
            plain = " ".join(plain.split())
            if plain:
                return plain[:180]
        return ""

    def _build_index(self, pages: dict[str, str]) -> str:
        grouped: dict[str, list[tuple[str, str, str]]] = {}
        for slug, content in sorted(pages.items()):
            metadata, body = _frontmatter(content, source=f"{slug}.md")
            page_type = str(metadata.get("type") or "unknown")
            grouped.setdefault(page_type, []).append(
                (slug, str(metadata.get("title") or slug), self._page_summary(body))
            )
        lines = ["# Wiki Index", ""]
        for page_type in sorted(grouped):
            lines.extend([f"## {page_type}", ""])
            for slug, title, summary in grouped[page_type]:
                suffix = f" — {summary}" if summary else ""
                lines.append(f"- [[{slug}|{title}]]{suffix}")
            lines.append("")
        return "\n".join(lines)

    def _lint_directory(self, directory: Path) -> dict[str, Any]:
        bundle = self._require_initialized()
        brain_document = bundle["brain_schema"]["document"]
        wiki_contract = brain_document["wiki"]
        allowed_types = set(wiki_contract["allowed_page_types"])
        prefixes_by_type = {
            str(page_type["name"]): workspace_page_prefixes(page_type)
            for page_type in bundle["resolved"]["manifest"]["page_types"]
        }
        required = list(wiki_contract["required_frontmatter"])
        bundle_version = str(brain_document["bundle_version"])
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        page_paths = _wiki_page_paths(directory)
        pages = {_wiki_slug(directory, path): path.read_text(encoding="utf-8") for path in page_paths}
        raw_names = {str(item.get("snapshot_path")) for item in self._manifest_records()}

        def finding(collection: list[dict[str, str]], code: str, path: str, message: str) -> None:
            collection.append({"code": code, "path": path, "message": message})

        for slug, content in pages.items():
            path = f"wiki/{slug}.md"
            if not SLUG_RE.fullmatch(slug):
                finding(errors, "invalid_slug", path, "filename must be a lowercase hyphen slug")
            elif slug.startswith("wiki/"):
                finding(
                    errors,
                    "duplicate_wiki_root",
                    path,
                    "page slug is already relative to the wiki/ root and must not start with wiki/",
                )
            try:
                metadata, body = _frontmatter(content, source=path)
            except LlmWikiError as exc:
                finding(errors, "invalid_frontmatter", path, str(exc))
                continue
            for field in required:
                if field not in metadata or metadata[field] in (None, "", []):
                    finding(errors, "missing_frontmatter", path, f"required field is missing: {field}")
            page_type = str(metadata.get("type") or "")
            if page_type and page_type not in allowed_types:
                finding(errors, "unknown_page_type", path, f"type {page_type!r} is not in the resolved Schema")
            elif page_type:
                prefixes = prefixes_by_type.get(page_type, [])
                relative_page_path = f"{slug}.md"
                if prefixes and not any(relative_page_path.startswith(prefix) for prefix in prefixes):
                    finding(
                        errors,
                        "page_path_type_mismatch",
                        path,
                        f"type {page_type!r} requires one of these path prefixes: {', '.join(prefixes)}",
                    )
            updated = str(metadata.get("updated") or "")
            if updated and not DATE_RE.fullmatch(updated):
                finding(errors, "invalid_updated", path, "updated must be YYYY-MM-DD")
            schema_version = str(metadata.get("schema_version") or "")
            if schema_version and schema_version != bundle_version:
                finding(errors, "schema_drift", path, f"expected schema_version {bundle_version!r}")
            sources = metadata.get("sources")
            if not isinstance(sources, list):
                finding(errors, "invalid_sources", path, "sources must be a list")
            else:
                for source in sources:
                    canonical_source = _canonical_raw_source(source, raw_names)
                    if canonical_source not in raw_names:
                        finding(errors, "unknown_source", path, f"source is not in raw manifest: {source}")
                    elif canonical_source != str(source):
                        finding(
                            warnings,
                            "legacy_source_prefix",
                            path,
                            f"source should use snapshot_path without the raw/ prefix: {canonical_source}",
                        )
            for target in ANY_WIKILINK_RE.findall(body):
                match = WIKILINK_TARGET_RE.fullmatch(target)
                if match is None:
                    finding(
                        errors,
                        "invalid_wikilink",
                        path,
                        f"target must include its type directory: [[<type-directory>/<slug>]], got [[{target}]]",
                    )
                elif match.group("slug").startswith("wiki/"):
                    finding(
                        errors,
                        "duplicate_wiki_root_link",
                        path,
                        f"wikilink is already relative to the wiki/ root: [[{target}]]",
                    )
                elif match.group("slug") not in pages:
                    finding(errors, "broken_wikilink", path, f"target does not exist: {target}")

        index_path = directory / "index.md"
        if not index_path.is_file():
            finding(errors, "missing_index", "wiki/index.md", "index.md is required")
        else:
            index_raw = index_path.read_text(encoding="utf-8")
            for target in ANY_WIKILINK_RE.findall(index_raw):
                match = WIKILINK_TARGET_RE.fullmatch(target)
                if match is None:
                    finding(
                        errors,
                        "invalid_index_wikilink",
                        "wiki/index.md",
                        f"target must include its type directory: [[<type-directory>/<slug>]], got [[{target}]]",
                    )
                elif match.group("slug").startswith("wiki/"):
                    finding(
                        errors,
                        "duplicate_wiki_root_link",
                        "wiki/index.md",
                        f"wikilink is already relative to the wiki/ root: [[{target}]]",
                    )
            index_links = set(WIKILINK_RE.findall(index_raw))
            for slug in sorted(set(pages) - index_links):
                finding(errors, "index_omission", "wiki/index.md", f"page is not indexed: {slug}")
            for slug in sorted(index_links - set(pages)):
                finding(errors, "index_broken_link", "wiki/index.md", f"index target does not exist: {slug}")

        log_path = directory / "log.md"
        if not log_path.is_file():
            finding(errors, "missing_log", "wiki/log.md", "log.md is required")
        raw_hashes = self._raw_hashes()
        for path, digest in raw_hashes.items():
            if digest == "missing" or digest.startswith("mismatch:"):
                finding(errors, "raw_hash_mismatch", f"raw/{path}", digest)

        inbound = Counter(WIKILINK_RE.findall("\n".join(pages.values())))
        for slug in pages:
            if inbound[slug] == 0 and len(pages) > 1:
                finding(warnings, "orphan_page", f"wiki/{slug}.md", "page has no inbound Wiki link")
        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "counts": {"pages": len(pages), "errors": len(errors), "warnings": len(warnings)},
            "bundle_hash": bundle["bundle_hash"],
            "raw_manifest_sha256": _sha256_file(self.raw_manifest_path) if self.raw_manifest_path.exists() else None,
        }

    def lint(self) -> dict[str, Any]:
        return self._lint_directory(self.wiki_dir)

    def publish(
        self,
        *,
        pages: list[dict[str, str]],
        expected_bundle_hash: str,
        summary: str,
        model: str,
        raw_paths: list[str],
    ) -> dict[str, Any]:
        """Validate and publish complete agent-proposed Wiki pages."""

        with _file_lock(self.brain_write_lock_path):
            with _file_lock(self.publish_lock_path):
                return self._publish_unlocked(
                    pages=pages,
                    expected_bundle_hash=expected_bundle_hash,
                    summary=summary,
                    model=model,
                    raw_paths=raw_paths,
                )

    def _publish_unlocked(
        self,
        *,
        pages: list[dict[str, str]],
        expected_bundle_hash: str,
        summary: str,
        model: str,
        raw_paths: list[str],
    ) -> dict[str, Any]:

        bundle = self._require_initialized()
        if bundle["bundle_hash"] != expected_bundle_hash:
            raise LlmWikiError("Schema Bundle changed since the Ingest context was loaded")
        if not pages:
            raise LlmWikiError("publish requires at least one page")
        if not raw_paths:
            raise LlmWikiError("publish requires explicit immutable raw_paths")
        manifest_records = self._manifest_records()
        known_raw = {str(item.get("snapshot_path")) for item in manifest_records}
        raw_paths = list(dict.fromkeys(_canonical_raw_source(path, known_raw) for path in raw_paths))
        unknown_raw = sorted(set(raw_paths) - known_raw)
        if unknown_raw:
            raise LlmWikiError(f"raw paths are not in the immutable manifest: {', '.join(unknown_raw)}")
        selected_records = [item for item in manifest_records if str(item.get("snapshot_path")) in raw_paths]
        authorized_sources = {str(record.get("snapshot_path") or "") for record in selected_records}
        consumed_by_page: dict[str, list[str]] = {}
        prepared_pages: list[dict[str, str]] = []
        for item in pages:
            slug = str(item.get("slug") or "")
            content = str(item.get("content") or "")
            metadata, body = _frontmatter(content, source=f"wiki/{slug}.md")
            sources = metadata.get("sources")
            if not isinstance(sources, list) or not sources:
                raise LlmWikiError(f"wiki/{slug}.md: sources must be a non-empty list")
            canonical_sources = [_canonical_raw_source(source, known_raw) for source in sources]
            existing_path = self.wiki_dir / f"{slug}.md"
            page_authorized = set(authorized_sources)
            if existing_path.is_file():
                existing_metadata, _existing_body = _frontmatter(
                    existing_path.read_text(encoding="utf-8"),
                    source=f"wiki/{slug}.md",
                )
                existing_sources = existing_metadata.get("sources")
                if isinstance(existing_sources, list):
                    page_authorized.update(_canonical_raw_source(source, known_raw) for source in existing_sources)
            unauthorized = sorted(source for source in canonical_sources if source not in page_authorized)
            if unauthorized:
                raise LlmWikiError(
                    f"wiki/{slug}.md: sources are not authorized by this Ingest context: {', '.join(unauthorized)}"
                )
            consumed = sorted(set(canonical_sources) & authorized_sources)
            if not consumed:
                raise LlmWikiError(f"wiki/{slug}.md: must cite at least one raw selected for this Ingest")
            consumed_by_page[slug] = consumed
            if canonical_sources != [str(source) for source in sources]:
                metadata = dict(metadata)
                metadata["sources"] = canonical_sources
                content = _render_frontmatter(metadata, body)
            prepared_pages.append({"slug": slug, "content": content})
        pages = prepared_pages
        before_raw = self._raw_hashes()
        if any(value == "missing" or value.startswith("mismatch:") for value in before_raw.values()):
            raise LlmWikiError("raw snapshot integrity check failed before publish")

        job_id = f"wiki-{uuid.uuid4().hex[:16]}"
        staging = self.root / ".puddingclaw" / "staging" / job_id / "wiki"
        staging.mkdir(parents=True, exist_ok=False)
        for existing in self.wiki_dir.rglob("*.md"):
            relative = existing.relative_to(self.wiki_dir)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(existing, destination)
        changed: list[str] = []
        for item in pages:
            slug = str(item.get("slug") or "")
            content = str(item.get("content") or "")
            if not SLUG_RE.fullmatch(slug):
                raise LlmWikiError(f"invalid Wiki slug: {slug!r}")
            if slug in {"index", "log"}:
                raise LlmWikiError(f"special file cannot be supplied as a page: {slug}")
            destination = staging / f"{slug}.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                content if content.endswith("\n") else f"{content}\n",
                encoding="utf-8",
            )
            changed.append(slug)

        candidate_pages = {
            _wiki_slug(staging, path): path.read_text(encoding="utf-8")
            for path in _wiki_page_paths(staging)
        }
        (staging / "index.md").write_text(self._build_index(candidate_pages), encoding="utf-8")
        prior_log = (self.wiki_dir / "log.md").read_text(encoding="utf-8")
        today = datetime.now(UTC).date().isoformat()
        safe_summary = _single_line(summary, fallback="Wiki compile")
        safe_model = _single_line(model, fallback="unknown")
        log_entry = (
            f"\n## [{today}] ingest | {safe_summary}\n\n"
            f"- job_id: {job_id}\n"
            f"- schema: {bundle['brain_schema']['document']['schema_id']}@{bundle['brain_schema']['document']['bundle_version']}\n"
            f"- bundle_hash: {bundle['bundle_hash']}\n"
            f"- model: {safe_model}\n"
            f"- raw: {', '.join(raw_paths) if raw_paths else 'none'}\n"
            f"- added_or_updated: {', '.join(f'[[{slug}]]' for slug in changed)}\n"
        )
        log_separator = "" if prior_log.endswith("\n") else "\n"
        candidate_log = prior_log + log_separator + log_entry.lstrip("\n")
        if not candidate_log.startswith(prior_log):
            raise LlmWikiError("candidate log does not preserve the append-only prefix")
        (staging / "log.md").write_text(candidate_log, encoding="utf-8")
        report = self._lint_directory(staging)
        after_raw = self._raw_hashes()
        if before_raw != after_raw:
            raise LlmWikiError("raw snapshots changed while the Ingest candidate was being validated")
        if not report["ok"]:
            return {"published": False, "job_id": job_id, "lint": report}

        for path in sorted(
            staging.rglob("*.md"),
            key=lambda item: item.relative_to(staging).as_posix() in {"index.md", "log.md"},
        ):
            relative = path.relative_to(staging)
            _atomic_write(self.wiki_dir / relative, path.read_text(encoding="utf-8"))
        receipt = {
            "job_id": job_id,
            "status": "published",
            "bundle_hash": bundle["bundle_hash"],
            "schema_id": bundle["brain_schema"]["document"]["schema_id"],
            "schema_version": bundle["brain_schema"]["document"]["bundle_version"],
            "raw_hashes": before_raw,
            "pages": changed,
            "consumed_raw_by_page": consumed_by_page,
            "published_at": datetime.now(UTC).isoformat(),
            "lint": report,
        }
        _atomic_write(
            self.root / ".puddingclaw" / "jobs" / f"{job_id}.json",
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {"published": True, **receipt}

    def migrate_legacy_wiki_prefixes(self) -> dict[str, Any]:
        """Remove a duplicated workspace ``wiki/`` segment from pages and links."""

        with _file_lock(self.brain_write_lock_path):
            with _file_lock(self.publish_lock_path):
                bundle = self._require_initialized()
                original_pages = {
                    _wiki_slug(self.wiki_dir, path): path.read_text(encoding="utf-8")
                    for path in _wiki_page_paths(self.wiki_dir)
                }
                desired_pages: dict[str, str] = {}
                moved: dict[str, str] = {}
                changed: set[str] = set()
                for slug, content in original_pages.items():
                    canonical_slug = slug.removeprefix("wiki/") if slug.startswith("wiki/") else slug
                    rewritten = _rewrite_legacy_workspace_wikilinks(content)
                    existing = desired_pages.get(canonical_slug)
                    if existing is not None and existing != rewritten:
                        raise LlmWikiError(
                            f"cannot migrate duplicate wiki root: both {canonical_slug!r} and {slug!r} exist"
                        )
                    desired_pages[canonical_slug] = rewritten
                    if canonical_slug != slug:
                        moved[slug] = canonical_slug
                    if canonical_slug != slug or rewritten != content:
                        changed.add(canonical_slug)

                job_id = f"wiki-migrate-{uuid.uuid4().hex[:16]}"
                if not changed:
                    report = self._lint_directory(self.wiki_dir)
                    if not report["ok"]:
                        raise LlmWikiError("cannot revalidate legacy wiki prefix migration while Wiki Lint fails")
                    return self._record_workspace_prefix_migration(
                        job_id=job_id,
                        bundle=bundle,
                        pages=desired_pages,
                        moved={},
                        changed=set(),
                        report=report,
                        migrated=False,
                    )

                staging = self.root / ".puddingclaw" / "staging" / job_id / "wiki"
                staging.mkdir(parents=True, exist_ok=False)
                for slug, content in desired_pages.items():
                    destination = staging / f"{slug}.md"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content if content.endswith("\n") else f"{content}\n", encoding="utf-8")
                (staging / "index.md").write_text(self._build_index(desired_pages), encoding="utf-8")

                prior_log = (self.wiki_dir / "log.md").read_text(encoding="utf-8")
                today = datetime.now(UTC).date().isoformat()
                move_lines = ", ".join(f"`{source}` -> [[{target}]]" for source, target in sorted(moved.items()))
                log_entry = (
                    f"\n## [{today}] migrate | remove duplicate wiki root prefix\n\n"
                    f"- job_id: {job_id}\n"
                    f"- schema: {bundle['brain_schema']['document']['schema_id']}@{bundle['brain_schema']['document']['bundle_version']}\n"
                    f"- bundle_hash: {bundle['bundle_hash']}\n"
                    f"- moved: {move_lines or 'none'}\n"
                    f"- links_updated_in: {', '.join(f'[[{slug}]]' for slug in sorted(changed))}\n"
                )
                separator = "" if prior_log.endswith("\n") else "\n"
                candidate_log = prior_log + separator + log_entry.lstrip("\n")
                if not candidate_log.startswith(prior_log):
                    raise LlmWikiError("migration log does not preserve the append-only prefix")
                (staging / "log.md").write_text(candidate_log, encoding="utf-8")

                report = self._lint_directory(staging)
                if not report["ok"]:
                    raise LlmWikiError(
                        "legacy wiki prefix migration failed validation: "
                        + "; ".join(f"{item['path']}: {item['message']}" for item in report["errors"][:5])
                    )

                for slug, content in desired_pages.items():
                    _atomic_write(self.wiki_dir / f"{slug}.md", content if content.endswith("\n") else f"{content}\n")
                _atomic_write(self.wiki_dir / "index.md", (staging / "index.md").read_text(encoding="utf-8"))
                _atomic_write(self.wiki_dir / "log.md", candidate_log)
                for legacy_slug in sorted(moved, key=lambda value: value.count("/"), reverse=True):
                    legacy_path = self.wiki_dir / f"{legacy_slug}.md"
                    if legacy_path.is_file():
                        legacy_path.unlink()
                    parent = legacy_path.parent
                    while parent != self.wiki_dir:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent

                return self._record_workspace_prefix_migration(
                    job_id=job_id,
                    bundle=bundle,
                    pages=desired_pages,
                    moved=moved,
                    changed=changed,
                    report=report,
                    migrated=True,
                )

    def _record_workspace_prefix_migration(
        self,
        *,
        job_id: str,
        bundle: dict[str, Any],
        pages: dict[str, str],
        moved: dict[str, str],
        changed: set[str],
        report: dict[str, Any],
        migrated: bool,
    ) -> dict[str, Any]:
        raw_hashes = self._raw_hashes()
        known_raw = set(raw_hashes)
        consumed_raw_by_page: dict[str, list[str]] = {}
        for slug, content in pages.items():
            metadata, _body = _frontmatter(content, source=f"wiki/{slug}.md")
            sources = metadata.get("sources")
            source_values = sources if isinstance(sources, list) else []
            consumed_raw_by_page[slug] = sorted(
                {
                    canonical
                    for source in source_values
                    if (canonical := _canonical_raw_source(source, known_raw)) in known_raw
                }
            )
        published_at = datetime.now(UTC).isoformat()
        receipt = {
            "job_id": job_id,
            "status": "published",
            "operation": "workspace-prefix-migration",
            "bundle_hash": bundle["bundle_hash"],
            "schema_id": bundle["brain_schema"]["document"]["schema_id"],
            "schema_version": bundle["brain_schema"]["document"]["bundle_version"],
            "raw_hashes": raw_hashes,
            "pages": sorted(pages),
            "consumed_raw_by_page": consumed_raw_by_page,
            "moved": moved,
            "updated_pages": sorted(changed),
            "published_at": published_at,
            "migrated_at": published_at,
            "lint": report,
        }
        _atomic_write(
            self.root / ".puddingclaw" / "jobs" / f"{job_id}.json",
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {"migrated": migrated, **receipt}

    def query(self, question: str, *, limit: int = 6) -> dict[str, Any]:
        """Read-only Wiki query context; never falls back to raw files."""

        self._require_initialized()
        terms = {term.lower() for term in WORD_RE.findall(question) if len(term) > 1}
        candidates: list[tuple[int, str, str]] = []
        for path in _wiki_page_paths(self.wiki_dir):
            content = path.read_text(encoding="utf-8")
            lowered = content.lower()
            score = sum(lowered.count(term) for term in terms)
            if score or not terms:
                candidates.append((score, _wiki_slug(self.wiki_dir, path), content))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        selected = candidates[: max(1, min(limit, 20))]
        pages: list[dict[str, Any]] = []
        references: list[dict[str, Any]] = []
        for score, slug, content in selected:
            metadata, body = _frontmatter(content, source=f"wiki/{slug}.md")
            raw_sources = metadata.get("sources")
            if not isinstance(raw_sources, list):
                raw_sources = []
            reference = {
                "slug": slug,
                "title": str(metadata.get("title") or slug),
                "type": str(metadata.get("type") or "unknown"),
                "uri": f"/knowledge/llm-wiki/wiki/{slug}.md",
                "score": score,
                "excerpt": body.strip()[:1200],
                "sources": [str(item) for item in raw_sources if str(item).strip()],
            }
            pages.append({"slug": slug, "score": score, "content": content})
            references.append(reference)
        return {
            "question": question,
            "index_md": (self.wiki_dir / "index.md").read_text(encoding="utf-8"),
            "pages": pages,
            "references": references,
            "knowledge_gap": not candidates,
            "source_policy": "wiki-only",
        }

    @contextmanager
    def _isolated_gbrain_env(self):
        """Install the active pack in a disposable home for validate-only runs."""

        with tempfile.TemporaryDirectory(prefix="puddingclaw-gbrain-validate-") as temporary:
            runtime_home = Path(temporary)
            pack_dir = runtime_home / ".gbrain" / "schema-packs" / "puddingclaw-wiki"
            pack_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.schema.custom_pack_path, pack_dir / "pack.yaml")
            environment = os.environ.copy()
            environment["GBRAIN_HOME"] = str(runtime_home)
            environment["GBRAIN_SCHEMA_PACK"] = "puddingclaw-wiki"
            yield environment

    def _production_gbrain_env(self, *, bundle_hash: str) -> tuple[dict[str, str], Path]:
        runtime_home = self.gbrain_runtime_home
        config_path = runtime_home / ".gbrain" / "config.json"
        if not config_path.is_file():
            raise LlmWikiError("gbrain runtime is not initialized; configure the PostgreSQL URL first")
        pack_dir = runtime_home / ".gbrain" / "schema-packs" / "puddingclaw-wiki"
        pack_dir.mkdir(parents=True, exist_ok=True)
        lock_path = runtime_home / ".gbrain" / "locks" / "puddingclaw-schema-pack.lock"
        with _file_lock(lock_path):
            _atomic_write(pack_dir / "pack.yaml", self.schema.custom_pack_path.read_text(encoding="utf-8"))
            _atomic_write(pack_dir / ".puddingclaw-bundle", f"{bundle_hash}\n")
        try:
            environment, _runtime = apply_gbrain_ai_environment(os.environ.copy())
        except ValueError as exc:
            raise LlmWikiError(str(exc)) from exc
        environment["GBRAIN_HOME"] = str(runtime_home)
        environment["GBRAIN_SCHEMA_PACK"] = "puddingclaw-wiki"
        return environment, runtime_home

    @staticmethod
    def _write_gbrain_provider_urls(config_path: Path, runtime: dict[str, Any]) -> None:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LlmWikiError(f"gbrain config.json is invalid after init: {exc}") from exc
        configured_urls = config.get("provider_base_urls")
        if not isinstance(configured_urls, dict):
            configured_urls = {}
        configured_urls.update(runtime.get("provider_base_urls", {}))
        config["provider_base_urls"] = configured_urls
        _atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")

    def initialize_gbrain_runtime(self, database_url: str) -> dict[str, Any]:
        """Initialize the dedicated gbrain home against an existing PostgreSQL server."""

        self._require_initialized()
        normalized_url = database_url.strip()
        if not normalized_url.startswith(("postgresql://", "postgres://")):
            raise LlmWikiError("database_url must use postgresql:// or postgres://")
        try:
            pgvector = inspect_pgvector_dsn_sync(normalized_url)
        except Exception as exc:
            raise LlmWikiError(f"PostgreSQL pgvector preflight failed: {exc}") from exc
        if not pgvector["available"]:
            raise LlmWikiError(
                "PostgreSQL server is missing required pgvector extension files. "
                f"Install it first: {pgvector['install_command']}"
            )
        binary = resolve_gbrain_binary()
        if not binary:
            raise LlmWikiError("gbrain CLI is not installed")
        runtime_home = self.gbrain_runtime_home
        runtime_home.mkdir(parents=True, exist_ok=True)
        try:
            environment, ai_runtime = apply_gbrain_ai_environment(os.environ.copy())
        except ValueError as exc:
            raise LlmWikiError(str(exc)) from exc
        environment.pop("DATABASE_URL", None)
        environment.pop("GBRAIN_DATABASE_URL", None)
        environment["GBRAIN_HOME"] = str(runtime_home)
        environment = gbrain_subprocess_environment(binary, environment)
        result = subprocess.run(
            [
                binary,
                "init",
                "--url",
                normalized_url,
                "--non-interactive",
                "--embedding-model",
                ai_runtime["embedding_model"],
                "--embedding-dimensions",
                str(ai_runtime["embedding_dimensions"]),
                "--chat-model",
                ai_runtime["chat_model"],
                "--skip-embed-check",
                "--schema-pack",
                "gbrain-base-v2",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise LlmWikiError(f"gbrain PostgreSQL initialization failed: {detail}")
        self._write_gbrain_provider_urls(runtime_home / ".gbrain" / "config.json", ai_runtime)
        bundle = self._require_initialized()
        self._production_gbrain_env(bundle_hash=bundle["bundle_hash"])
        return {
            "ok": True,
            "runtime_home": str(runtime_home),
            "schema_pack": "puddingclaw-wiki",
            "postgresql": "configured",
            "pgvector": pgvector,
            "embedding": ai_runtime["embedding"],
            "think": ai_runtime["think"],
        }

    def _gbrain_source_dir(self) -> Path | None:
        """Materialize an immutable gbrain source without index.md/log.md."""

        pages = _wiki_page_paths(self.wiki_dir)
        if not pages:
            return None
        digest_input = "source-layout:wiki-root-v2\n" + "\n".join(
            f"{path.relative_to(self.wiki_dir).as_posix()}:{_sha256_file(path)}" for path in pages
        )
        digest = _sha256_bytes(digest_input.encode("utf-8"))
        target = self.root / ".puddingclaw" / "gbrain-sources" / digest
        if not target.exists():
            target.mkdir(parents=True, exist_ok=False)
            for page in pages:
                relative = page.relative_to(self.wiki_dir)
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(page, destination)
        return target

    @staticmethod
    def _run(command: list[str], *, environment: dict[str, str], timeout: int = 60) -> dict[str, Any]:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=environment,
            timeout=timeout,
            check=False,
        )
        return {
            "command": command,
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout[-12000:],
            "stderr": result.stderr[-12000:],
        }

    def compile_gbrain(self, *, import_pages: bool = False) -> dict[str, Any]:
        """Validate with real gbrain and optionally import into configured PostgreSQL brain."""

        bundle = self._require_initialized()
        binary = resolve_gbrain_binary()
        if not binary:
            raise LlmWikiError("gbrain CLI is not installed")
        lint = self.lint()
        if not lint["ok"]:
            return {"ok": False, "phase": "wiki_contract", "lint": lint}
        source_dir = self._gbrain_source_dir()
        with self._isolated_gbrain_env() as environment:
            environment = gbrain_subprocess_environment(binary, environment)
            checks: list[dict[str, Any]] = [
                self._run([binary, "schema", "validate", "puddingclaw-wiki", "--json"], environment=environment),
                self._run([binary, "schema", "lint", "puddingclaw-wiki", "--json"], environment=environment),
            ]
            if source_dir is not None:
                page_lint = self._run([binary, "lint", str(source_dir)], environment=environment)
                issue_match = re.search(r"\b(\d+) issue\(s\)", page_lint["stdout"])
                if issue_match and int(issue_match.group(1)) > 0:
                    page_lint["ok"] = False
                checks.append(page_lint)
        if not all(item["ok"] for item in checks):
            return {"ok": False, "phase": "gbrain_validate", "checks": checks, "lint": lint}
        imported: dict[str, Any] | None = None
        extracted: dict[str, Any] | None = None
        runtime_home: Path | None = None
        if import_pages:
            if source_dir is None:
                raise LlmWikiError("cannot import an empty Wiki")
            environment, runtime_home = self._production_gbrain_env(bundle_hash=bundle["bundle_hash"])
            environment = gbrain_subprocess_environment(binary, environment)
            imported = self._run([binary, "import", str(source_dir)], environment=environment, timeout=600)
            if imported["ok"]:
                extracted = self._run(
                    [binary, "extract", "links", "--source", "db", "--json"],
                    environment=environment,
                    timeout=300,
                )
        return {
            "ok": (
                all(item["ok"] for item in checks)
                and (imported is None or imported["ok"])
                and (extracted is None or extracted["ok"])
            ),
            "phase": "import" if import_pages else "validate",
            "bundle_hash": bundle["bundle_hash"],
            "runtime_home": str(runtime_home) if runtime_home else "isolated-temporary-home",
            "checks": checks,
            "import": imported,
            "extract_links": extracted,
            "lint": lint,
        }


def get_llm_wiki_service(base_dir: Path) -> LlmWikiService:
    return LlmWikiService(base_dir)
