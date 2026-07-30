"""Deterministic runtime for the LLM Wiki file protocol.

The LLM may propose pages, but it never writes the published Wiki directly.
This module owns immutable raw snapshots, candidate validation, publishing,
read-only query context, and the gbrain compile boundary.
"""

from __future__ import annotations

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

import yaml

from knowledge.brain_schema import BrainSchemaError, BrainSchemaService

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
WIKILINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]*)(?:\|[^\]]+)?\]\]")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORD_RE = re.compile(r"[\w\u4e00-\u9fff-]+", re.UNICODE)
SPECIAL_WIKI_FILES = frozenset({"index.md", "log.md"})


class LlmWikiError(RuntimeError):
    """Raised when a Wiki operation violates its deterministic contract."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
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

        with _file_lock(self.raw_lock_path):
            return self._snapshot_raw_unlocked(
                source_id=source_id,
                asset_id=asset_id,
                title=title,
                content=content,
                source_path=source_path,
            )

    def _snapshot_raw_unlocked(
        self,
        *,
        source_id: str,
        asset_id: str,
        title: str,
        content: str,
        source_path: str | None = None,
    ) -> dict[str, Any]:

        bundle = self._require_initialized()
        if not content.strip():
            raise LlmWikiError("raw content must not be empty")
        source = _identity_segment(source_id, label="source_id")
        asset = _identity_segment(asset_id, label="asset_id")
        normalized_content = content if content.endswith("\n") else f"{content}\n"
        encoded = normalized_content.encode("utf-8")
        digest = _sha256_bytes(encoded)
        relative = Path(source) / f"{asset}-{digest[:12]}.md"
        target = self.raw_dir / relative
        if target.exists() and target.read_bytes() != encoded:
            raise LlmWikiError(f"immutable raw snapshot collision: {relative}")
        if not target.exists():
            _atomic_write(target, normalized_content)

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
            "size_bytes": len(encoded),
            "bundle_hash": bundle["bundle_hash"],
            "created_at": datetime.now(UTC).isoformat(),
        }
        lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in [*existing, record]]
        _atomic_write(self.raw_manifest_path, "\n".join(lines) + "\n")
        return record

    def _resolve_raw_record(self, record: dict[str, Any]) -> tuple[str, Path, str]:
        relative = str(record.get("snapshot_path") or "")
        expected = str(record.get("sha256") or "")
        path = (self.raw_dir / relative).resolve()
        try:
            path.relative_to(self.raw_dir.resolve())
        except ValueError as exc:
            raise LlmWikiError(f"raw manifest path escapes raw/: {relative}") from exc
        return relative, path, expected

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
        required = list(wiki_contract["required_frontmatter"])
        bundle_version = str(brain_document["bundle_version"])
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        page_paths = sorted(path for path in directory.glob("*.md") if path.name not in SPECIAL_WIKI_FILES)
        pages = {path.stem: path.read_text(encoding="utf-8") for path in page_paths}
        raw_names = {str(item.get("snapshot_path")) for item in self._manifest_records()}

        def finding(collection: list[dict[str, str]], code: str, path: str, message: str) -> None:
            collection.append({"code": code, "path": path, "message": message})

        for slug, content in pages.items():
            path = f"wiki/{slug}.md"
            if not SLUG_RE.fullmatch(slug):
                finding(errors, "invalid_slug", path, "filename must be a lowercase hyphen slug")
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
                    if str(source) not in raw_names:
                        finding(errors, "unknown_source", path, f"source is not in raw manifest: {source}")
            for link in WIKILINK_RE.findall(body):
                if link not in pages:
                    finding(errors, "broken_wikilink", path, f"target does not exist: {link}")

        index_path = directory / "index.md"
        if not index_path.is_file():
            finding(errors, "missing_index", "wiki/index.md", "index.md is required")
        else:
            index_links = set(WIKILINK_RE.findall(index_path.read_text(encoding="utf-8")))
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
        unknown_raw = sorted(set(raw_paths) - known_raw)
        if unknown_raw:
            raise LlmWikiError(f"raw paths are not in the immutable manifest: {', '.join(unknown_raw)}")
        selected_records = [item for item in manifest_records if str(item.get("snapshot_path")) in raw_paths]
        authorized_sources = {str(record.get("snapshot_path") or "") for record in selected_records}
        consumed_by_page: dict[str, list[str]] = {}
        for item in pages:
            slug = str(item.get("slug") or "")
            content = str(item.get("content") or "")
            metadata, _body = _frontmatter(content, source=f"wiki/{slug}.md")
            sources = metadata.get("sources")
            if not isinstance(sources, list) or not sources:
                raise LlmWikiError(f"wiki/{slug}.md: sources must be a non-empty list")
            existing_path = self.wiki_dir / f"{slug}.md"
            page_authorized = set(authorized_sources)
            if existing_path.is_file():
                existing_metadata, _existing_body = _frontmatter(
                    existing_path.read_text(encoding="utf-8"),
                    source=f"wiki/{slug}.md",
                )
                existing_sources = existing_metadata.get("sources")
                if isinstance(existing_sources, list):
                    page_authorized.update(str(source) for source in existing_sources)
            unauthorized = sorted(str(source) for source in sources if str(source) not in page_authorized)
            if unauthorized:
                raise LlmWikiError(
                    f"wiki/{slug}.md: sources are not authorized by this Ingest context: {', '.join(unauthorized)}"
                )
            consumed = sorted({str(source) for source in sources} & authorized_sources)
            if not consumed:
                raise LlmWikiError(f"wiki/{slug}.md: must cite at least one raw selected for this Ingest")
            consumed_by_page[slug] = consumed
        before_raw = self._raw_hashes()
        if any(value == "missing" or value.startswith("mismatch:") for value in before_raw.values()):
            raise LlmWikiError("raw snapshot integrity check failed before publish")

        job_id = f"wiki-{uuid.uuid4().hex[:16]}"
        staging = self.root / ".puddingclaw" / "staging" / job_id / "wiki"
        staging.mkdir(parents=True, exist_ok=False)
        for existing in self.wiki_dir.glob("*.md"):
            shutil.copy2(existing, staging / existing.name)
        changed: list[str] = []
        for item in pages:
            slug = str(item.get("slug") or "")
            content = str(item.get("content") or "")
            if not SLUG_RE.fullmatch(slug):
                raise LlmWikiError(f"invalid Wiki slug: {slug!r}")
            if slug in {"index", "log"}:
                raise LlmWikiError(f"special file cannot be supplied as a page: {slug}")
            (staging / f"{slug}.md").write_text(
                content if content.endswith("\n") else f"{content}\n",
                encoding="utf-8",
            )
            changed.append(slug)

        candidate_pages = {
            path.stem: path.read_text(encoding="utf-8")
            for path in staging.glob("*.md")
            if path.name not in SPECIAL_WIKI_FILES
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

        for path in sorted(staging.glob("*.md"), key=lambda item: item.name in {"index.md", "log.md"}):
            _atomic_write(self.wiki_dir / path.name, path.read_text(encoding="utf-8"))
        receipt = {
            "job_id": job_id,
            "status": "published",
            "bundle_hash": bundle["bundle_hash"],
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

    def query(self, question: str, *, limit: int = 6) -> dict[str, Any]:
        """Read-only Wiki query context; never falls back to raw files."""

        self._require_initialized()
        terms = {term.lower() for term in WORD_RE.findall(question) if len(term) > 1}
        candidates: list[tuple[int, str, str]] = []
        for path in self.wiki_dir.glob("*.md"):
            if path.name in SPECIAL_WIKI_FILES:
                continue
            content = path.read_text(encoding="utf-8")
            lowered = content.lower()
            score = sum(lowered.count(term) for term in terms)
            if score or not terms:
                candidates.append((score, path.stem, content))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return {
            "question": question,
            "index_md": (self.wiki_dir / "index.md").read_text(encoding="utf-8"),
            "pages": [
                {"slug": slug, "score": score, "content": content}
                for score, slug, content in candidates[: max(1, min(limit, 20))]
            ],
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
        configured_home = os.getenv("PUDDINGCLAW_GBRAIN_HOME", "").strip()
        if not configured_home:
            raise LlmWikiError("PUDDINGCLAW_GBRAIN_HOME is required for PostgreSQL-backed import")
        runtime_home = Path(configured_home).expanduser().resolve()
        config_path = runtime_home / ".gbrain" / "config.json"
        if not config_path.is_file():
            raise LlmWikiError("configured gbrain home is not initialized; initialize it with the PostgreSQL URL first")
        pack_dir = runtime_home / ".gbrain" / "schema-packs" / "puddingclaw-wiki"
        pack_dir.mkdir(parents=True, exist_ok=True)
        lock_path = runtime_home / ".gbrain" / "locks" / "puddingclaw-schema-pack.lock"
        with _file_lock(lock_path):
            _atomic_write(pack_dir / "pack.yaml", self.schema.custom_pack_path.read_text(encoding="utf-8"))
            _atomic_write(pack_dir / ".puddingclaw-bundle", f"{bundle_hash}\n")
        environment = os.environ.copy()
        environment["GBRAIN_HOME"] = str(runtime_home)
        environment["GBRAIN_SCHEMA_PACK"] = "puddingclaw-wiki"
        return environment, runtime_home

    def _gbrain_source_dir(self) -> Path | None:
        """Materialize an immutable gbrain source without index.md/log.md."""

        pages = sorted(path for path in self.wiki_dir.glob("*.md") if path.name not in SPECIAL_WIKI_FILES)
        if not pages:
            return None
        digest_input = "\n".join(f"{path.name}:{_sha256_file(path)}" for path in pages)
        digest = _sha256_bytes(digest_input.encode("utf-8"))
        target = self.root / ".puddingclaw" / "gbrain-sources" / digest
        if not target.exists():
            target.mkdir(parents=True, exist_ok=False)
            for page in pages:
                shutil.copy2(page, target / page.name)
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
        binary = shutil.which(os.getenv("PUDDINGCLAW_GBRAIN_BIN", "gbrain"))
        if not binary:
            raise LlmWikiError("gbrain CLI is not installed")
        lint = self.lint()
        if not lint["ok"]:
            return {"ok": False, "phase": "wiki_contract", "lint": lint}
        source_dir = self._gbrain_source_dir()
        with self._isolated_gbrain_env() as environment:
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
        runtime_home: Path | None = None
        if import_pages:
            if source_dir is None:
                raise LlmWikiError("cannot import an empty Wiki")
            environment, runtime_home = self._production_gbrain_env(bundle_hash=bundle["bundle_hash"])
            imported = self._run([binary, "import", str(source_dir), "--no-embed"], environment=environment, timeout=180)
        return {
            "ok": all(item["ok"] for item in checks) and (imported is None or imported["ok"]),
            "phase": "import" if import_pages else "validate",
            "bundle_hash": bundle["bundle_hash"],
            "runtime_home": str(runtime_home) if runtime_home else "isolated-temporary-home",
            "checks": checks,
            "import": imported,
            "lint": lint,
        }


def get_llm_wiki_service(base_dir: Path) -> LlmWikiService:
    return LlmWikiService(base_dir)
