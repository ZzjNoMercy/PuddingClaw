"""Knowledge base application service."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import mimetypes
import os
import posixpath
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import runtime_control
from knowledge.indexer import build_markdown_chunk_manifest, refresh_local_knowledge_index
from knowledge.mineru_client import MinerUClient, MinerUParseResult
from knowledge.models import KnowledgeBase, KnowledgeDocument, new_id
from knowledge.paths import get_knowledge_originals_dir, get_knowledge_root

logger = logging.getLogger(__name__)

MARKDOWN_SUFFIXES = {".md", ".markdown"}
PDF_SUFFIXES = {".pdf"}
GENERIC_UPLOAD_SUFFIXES = {".xlsx", ".xls", ".csv", ".tsv", ".txt", ".docx"}
KNOWLEDGE_FILE_SUFFIXES = {
    ".md",
    ".markdown",
    ".pdf",
    ".xlsx",
    ".xls",
    ".csv",
    ".tsv",
    ".txt",
    ".docx",
}
TEXT_PREVIEW_SUFFIXES = {
    ".csv",
    ".json",
    ".log",
    ".md",
    ".markdown",
    ".py",
    ".txt",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".yml",
    ".yaml",
}
FILE_TREE_SKIP_DIRS = {
    ".git",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
FILE_TREE_SKIP_FILES = {".DS_Store"}
DEFAULT_KNOWLEDGE_BASE_ID = "kb_default"
DEFAULT_MARKDOWN_GLOB = "**/*.md"


class KnowledgeServiceError(ValueError):
    pass


async def assert_writes_allowed_tolerant(session: AsyncSession) -> None:
    """维护期拒写门控：draining/maintenance 时抛 MaintenanceModeError。

    容错：runtime_control 表不存在或 DB 不可用等探测失败时放行，不阻塞正常
    服务（运行时状态只读探测，不污染调用方 session）。
    """

    try:
        await runtime_control.assert_writes_allowed(session)
    except runtime_control.MaintenanceModeError:
        raise
    except Exception:  # noqa: BLE001 - 探测失败不阻塞正常服务
        logger.warning("[knowledge] runtime_control 写入门控探测失败，放行本次写入", exc_info=True)


def _ensure_directory_readable(path: Path) -> None:
    """Fail loudly when an existing knowledge root cannot be enumerated.

    ``Path.rglob`` may silently yield no entries when macOS denies access to a
    protected directory.  Treating that case as an empty knowledge base makes
    the UI incorrectly claim that the user's files disappeared.
    """

    try:
        next(path.iterdir(), None)
    except OSError as exc:
        raise KnowledgeServiceError(
            f"无法读取知识库目录，请检查该目录的访问权限：{path}"
        ) from exc


def _slugify(value: str) -> str:
    name = Path(value or "").name.strip()
    if not name:
        return "document"
    # Keep Chinese / Unicode filenames for desktop users. Only remove path
    # separators and filesystem-hostile control characters; collapse whitespace
    # so URLs and UI paths stay readable.
    name = name.replace("\x00", "")
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r'[<>:"|?*]+', "-", name).strip(" .-_")
    if not name:
        return "document"
    if len(name) <= 120:
        return name
    suffix = Path(name).suffix
    stem = Path(name).stem
    max_stem = max(1, 120 - len(suffix))
    return f"{stem[:max_stem]}{suffix}"


def _unique_path(directory: Path, filename: str) -> Path:
    safe_name = _slugify(filename)
    candidate = directory / safe_name
    if not candidate.exists():
        return candidate
    suffix = candidate.suffix
    stem = candidate.stem
    index = 2
    while True:
        next_candidate = directory / f"{stem}-{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1


def _title_or_source_stem(title: str | None, fallback_name: str) -> str:
    candidate = (title or "").strip() or Path(fallback_name).stem
    return _slugify(candidate or "document")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_markdown_asset_ref(value: str) -> str:
    normalized = (value or "").replace("\\", "/").strip().strip("'\"").lstrip("/")
    if not normalized:
        return ""
    if re.match(r"^(?:https?:|data:)", normalized, flags=re.IGNORECASE):
        return normalized
    if normalized.startswith("/knowledge/"):
        return normalized
    return posixpath.normpath(normalized).lstrip("./")


def _plain_markdown_context_line(line: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line or "")
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _nearby_context_lines(lines: list[str], start: int, step: int, limit: int = 2) -> list[str]:
    collected: list[str] = []
    index = start
    while 0 <= index < len(lines) and len(collected) < limit:
        text = _plain_markdown_context_line(lines[index])
        if text:
            collected.append(text)
        index += step
    if step < 0:
        collected.reverse()
    return collected


def _extract_markdown_image_contexts(markdown: str) -> dict[str, dict[str, Any]]:
    """Return surrounding text for image links keyed by their markdown URL.

    MinerU puts image references inline in the Markdown. Capturing the nearest
    heading plus a couple of surrounding text lines gives the UI and later
    multimodal retrieval a lightweight "why this image matters" context.
    """

    contexts: dict[str, dict[str, Any]] = {}
    lines = (markdown or "").splitlines()
    current_heading = ""
    markdown_image_re = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
    html_image_re = re.compile(r"<img\b[^>]*?\bsrc=[\"'](?P<url>.*?)[\"'][^>]*>", flags=re.IGNORECASE)

    for index, line in enumerate(lines):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading:
            current_heading = _plain_markdown_context_line(heading.group(1))

        matches: list[tuple[str, str]] = []
        matches.extend((match.group("url"), match.group("alt").strip()) for match in markdown_image_re.finditer(line))
        matches.extend((match.group("url"), "") for match in html_image_re.finditer(line))
        if not matches:
            continue

        before = _nearby_context_lines(lines, index - 1, -1)
        after = _nearby_context_lines(lines, index + 1, 1)
        for url, alt in matches:
            normalized_url = _normalize_markdown_asset_ref(url)
            if not normalized_url:
                continue
            caption = alt or ""
            snippet_parts = [current_heading, *before, caption, *after]
            snippet = " / ".join(part for part in snippet_parts if part).strip()
            context = {
                "heading": current_heading,
                "caption": caption,
                "before": before,
                "after": after,
                "line_number": index + 1,
                "snippet": snippet[:800],
            }
            contexts[normalized_url] = context
            contexts[Path(normalized_url).name] = context

    return contexts


def _rewrite_markdown_asset_links(
    markdown: str,
    *,
    assets: list[dict[str, Any]],
    assets_virtual_prefix: str,
    markdown_asset_prefix: str | None = None,
    assets_dir: Path | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite MinerU-local image links while keeping stable metadata paths.

    The persisted Markdown should be friendly to local editors like Typora, so
    image links can use a filesystem-relative prefix. Asset metadata still keeps
    /knowledge/assets virtual paths for Agent/API use.
    """

    if not markdown or not assets:
        return markdown, assets

    prefix = assets_virtual_prefix.rstrip("/")
    enriched_assets: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}
    image_contexts = _extract_markdown_image_contexts(markdown)
    for asset in assets:
        original_relative_path = str(asset.get("relative_path") or asset.get("name") or "").replace("\\", "/").lstrip("/")
        relative_path = f"images/{Path(original_relative_path).name}" if original_relative_path else ""
        if not relative_path:
            enriched_assets.append(asset)
            continue
        next_asset = dict(asset)
        source_path = Path(str(next_asset.get("path") or ""))
        if assets_dir is not None:
            target_path = assets_dir / relative_path
            if source_path.exists() and source_path.resolve() != target_path.resolve():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                next_asset["path"] = str(target_path)
                next_asset["size_bytes"] = target_path.stat().st_size
        if original_relative_path and original_relative_path != relative_path:
            next_asset["original_relative_path"] = next_asset.get("original_relative_path") or original_relative_path
            aliases = list(next_asset.get("aliases") or [])
            aliases.append(original_relative_path)
            next_asset["aliases"] = aliases
        virtual_path = f"{prefix}/{relative_path}"
        markdown_path = (
            f"{markdown_asset_prefix.rstrip('/')}/{relative_path}" if markdown_asset_prefix else virtual_path
        )
        next_asset["relative_path"] = relative_path
        next_asset["virtual_path"] = virtual_path
        enriched_assets.append(next_asset)

        candidates = {relative_path, Path(relative_path).name}
        original_relative_path = next_asset.get("original_relative_path")
        if original_relative_path:
            candidates.add(str(original_relative_path).replace("\\", "/").lstrip("/"))
        aliases = next_asset.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if alias:
                    candidates.add(str(alias).replace("\\", "/").lstrip("/"))
        parts = relative_path.split("/")
        for index in range(1, len(parts)):
            candidates.add("/".join(parts[index:]))
        for candidate in list(candidates):
            candidate_parts = candidate.split("/")
            for index in range(1, len(candidate_parts)):
                candidates.add("/".join(candidate_parts[index:]))
        for candidate in candidates:
            normalized_candidate = _normalize_markdown_asset_ref(candidate)
            context = image_contexts.get(normalized_candidate) or image_contexts.get(Path(normalized_candidate).name)
            if context:
                next_asset["context"] = context
                break
        for candidate in candidates:
            replacements[candidate] = markdown_path

    def resolve_link(url: str) -> str:
        normalized = url.replace("\\", "/").strip()
        if not normalized or re.match(r"^(?:https?:|data:|/knowledge/)", normalized, flags=re.IGNORECASE):
            return url
        return replacements.get(normalized, url)

    def replace_markdown_image(match: re.Match[str]) -> str:
        return f"![{match.group('alt')}]({resolve_link(match.group('url'))}{match.group('suffix') or ''})"

    def replace_html_image(match: re.Match[str]) -> str:
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}{resolve_link(match.group('url'))}{quote}"

    rewritten = re.sub(
        r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?P<suffix>\s+\"[^\"]*\")?\)",
        replace_markdown_image,
        markdown,
    )
    rewritten = re.sub(
        r"(?P<prefix><img\b[^>]*?\bsrc=)(?P<quote>[\"'])(?P<url>.*?)(?P=quote)",
        replace_html_image,
        rewritten,
        flags=re.IGNORECASE,
    )
    return rewritten, enriched_assets


def _rewrite_markdown_links_for_preview(markdown: str, *, base_virtual_path: str = "") -> str:
    def to_raw_url(url: str) -> str:
        normalized = url.strip()
        if not normalized or re.match(r"^(?:https?:|data:)", normalized, flags=re.IGNORECASE):
            return url
        virtual_path = normalized
        if not virtual_path.startswith("/knowledge/") and base_virtual_path:
            base_dir = posixpath.dirname(base_virtual_path)
            virtual_path = posixpath.normpath(posixpath.join(base_dir, virtual_path))
        if not virtual_path.startswith("/knowledge/"):
            return url
        return f"/api/knowledge/file/raw?virtual_path={quote(virtual_path, safe='')}"

    def replace_markdown_image(match: re.Match[str]) -> str:
        return f"![{match.group('alt')}]({to_raw_url(match.group('url'))}{match.group('suffix') or ''})"

    def replace_html_image(match: re.Match[str]) -> str:
        quote_char = match.group("quote")
        return f"{match.group('prefix')}{quote_char}{to_raw_url(match.group('url'))}{quote_char}"

    rewritten = re.sub(
        r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?P<suffix>\s+\"[^\"]*\")?\)",
        replace_markdown_image,
        markdown,
    )
    return re.sub(
        r"(?P<prefix><img\b[^>]*?\bsrc=)(?P<quote>[\"'])(?P<url>.*?)(?P=quote)",
        replace_html_image,
        rewritten,
        flags=re.IGNORECASE,
    )


def _markdown_has_local_image_refs(markdown: str) -> bool:
    return bool(
        re.search(r"!\[[^\]]*\]\((?!/knowledge/|https?://|data:)[^)\s]+\)", markdown or "")
        or re.search(r"<img\b[^>]*?\bsrc=[\"'](?!/knowledge/|https?://|data:).*?[\"']", markdown or "", flags=re.IGNORECASE)
    )


def _document_needs_asset_repair(document: KnowledgeDocument) -> bool:
    if document.source_type != "pdf_mineru":
        return False
    storage_path = Path(document.storage_path)
    if not storage_path.exists():
        return True
    try:
        text = storage_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if _markdown_has_local_image_refs(text):
        return True
    for asset in document.doc_metadata.get("assets", []) if isinstance(document.doc_metadata, dict) else []:
        path = asset.get("path") if isinstance(asset, dict) else None
        if path and not Path(str(path)).exists():
            return True
    return False


def _document_has_storage(document: KnowledgeDocument) -> bool:
    return bool(document.storage_path) and Path(document.storage_path).exists()


def _document_owned_artifacts(document: KnowledgeDocument | None) -> set[str]:
    if document is None:
        return set()
    paths = {str(document.storage_path or "")}
    metadata = document.doc_metadata if isinstance(document.doc_metadata, dict) else {}
    if document.source_type == "pdf_mineru":
        paths.add(str(document.source_path or ""))
        paths.add(str(metadata.get("original_path") or ""))
        multimodal = metadata.get("multimodal") if isinstance(metadata.get("multimodal"), dict) else {}
        paths.add(str(multimodal.get("image_assets_dir") or ""))
    return {path for path in paths if path}


def _cleanup_replaced_artifacts(knowledge_dir: Path, old_paths: set[str], keep_paths: set[Path]) -> None:
    root = knowledge_dir.resolve()
    keep = {path.resolve() for path in keep_paths}
    for raw_path in old_paths:
        path = Path(raw_path).expanduser().resolve()
        if path in keep or not path.is_relative_to(root):
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("[knowledge] failed to clean replaced artifact path=%s error=%s", path, exc)


def _document_has_llamaindex_chunks(document: KnowledgeDocument) -> bool:
    metadata = document.doc_metadata if isinstance(document.doc_metadata, dict) else {}
    chunks = metadata.get("llamaindex_chunks")
    if not isinstance(chunks, dict):
        return False
    return bool(chunks.get("chunks"))


def _document_ready_for_dedup(document: KnowledgeDocument) -> bool:
    return _document_has_storage(document) and not _document_needs_asset_repair(document) and _document_has_llamaindex_chunks(document)


def _asset_context_candidates(asset: dict[str, Any]) -> set[str]:
    candidates: set[str] = set()
    for key in ("virtual_path", "relative_path", "original_relative_path", "name", "path"):
        value = asset.get(key)
        if value:
            normalized = _normalize_markdown_asset_ref(str(value))
            candidates.add(normalized)
            candidates.add(Path(normalized).name)
    aliases = asset.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if alias:
                normalized = _normalize_markdown_asset_ref(str(alias))
                candidates.add(normalized)
                candidates.add(Path(normalized).name)
    return {candidate for candidate in candidates if candidate}


def _build_llamaindex_chunk_metadata(
    knowledge_dir: Path,
    markdown_path: Path,
    image_paths: list[Path] | None = None,
) -> dict[str, Any]:
    try:
        return build_markdown_chunk_manifest(knowledge_dir, [markdown_path], image_paths or [])
    except Exception as exc:  # noqa: BLE001
        return {
            "parser": "MarkdownNodeParser",
            "chunk_count": 0,
            "chunks": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _build_or_update_document(existing: KnowledgeDocument | None = None, **fields: Any) -> KnowledgeDocument:
    if existing is None:
        return KnowledgeDocument(id=new_id("doc"), **fields)
    for key, value in fields.items():
        setattr(existing, key, value)
    return existing


def _document_identity_stmt(
    *,
    knowledge_base_id: str,
    content_sha256: str,
    source_item_id: str | None,
):
    stmt = select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == knowledge_base_id)
    if source_item_id:
        return stmt.where(KnowledgeDocument.source_item_id == source_item_id)
    # Compatibility callers without a Source identity keep the old idempotent
    # upload behavior. Connector callers are never deduplicated across items.
    return stmt.where(KnowledgeDocument.content_sha256 == content_sha256)


def _safe_read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _decode_text_preview(raw: bytes, *, truncated: bool) -> str:
    """Decode preview bytes without turning UTF-8 truncation into mojibake."""

    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            # The preview endpoint intentionally reads only the first chunk of
            # large files. If the chunk ends in the middle of a multi-byte
            # character, decoding the whole chunk fails even though the file is
            # valid UTF-8/GBK. In that case keep the valid prefix instead of
            # falling through to latin-1 and corrupting every Chinese character.
            if truncated and exc.start >= max(0, len(raw) - 8):
                return raw[: exc.start].decode(encoding, errors="ignore")
            continue
    try:
        return raw.decode("latin-1")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _matches_glob(path: Path, root: Path, pattern: str) -> bool:
    rel = path.relative_to(root).as_posix()
    virtual_path = f"/knowledge/{rel}"
    normalized_pattern = (pattern or DEFAULT_MARKDOWN_GLOB).strip()
    normalized_pattern = normalized_pattern.removeprefix("/knowledge/")
    if normalized_pattern.startswith("knowledge/"):
        normalized_pattern = normalized_pattern.removeprefix("knowledge/")

    if "/" not in normalized_pattern:
        return fnmatch.fnmatch(path.name, normalized_pattern)
    if normalized_pattern.startswith("**/") and fnmatch.fnmatch(rel, normalized_pattern[3:]):
        return True
    return fnmatch.fnmatch(rel, normalized_pattern) or fnmatch.fnmatch(virtual_path, pattern)


def _is_skipped_tree_path(path: Path) -> bool:
    if path.name in FILE_TREE_SKIP_FILES:
        return True
    if path.name.startswith("."):
        return True
    if path.is_dir() and path.name in FILE_TREE_SKIP_DIRS:
        return True
    return False


def _resolve_virtual_knowledge_path(root: Path, virtual_path: str) -> Path:
    normalized = (virtual_path or "").strip()
    if not normalized:
        raise KnowledgeServiceError("File path is required.")
    if normalized.startswith("/knowledge/"):
        normalized = normalized.removeprefix("/knowledge/")
    elif normalized == "/knowledge":
        normalized = ""
    elif normalized.startswith("knowledge/"):
        normalized = normalized.removeprefix("knowledge/")
    else:
        raise KnowledgeServiceError("Only files inside the knowledge directory can be opened.")

    target = (root / normalized).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise KnowledgeServiceError("Only files inside the knowledge directory can be opened.")
    return target


def document_to_dict(document: KnowledgeDocument) -> dict[str, Any]:
    return {
        "id": document.id,
        "knowledge_base_id": document.knowledge_base_id,
        "title": document.title,
        "source_type": document.source_type,
        "source_path": document.source_path,
        "storage_path": document.storage_path,
        "virtual_path": document.virtual_path,
        "mime_type": document.mime_type,
        "content_sha256": document.content_sha256,
        "size_bytes": document.size_bytes,
        "status": document.status,
        "source_connection_id": document.source_connection_id,
        "source_item_id": document.source_item_id,
        "origin_url": document.origin_url,
        "source_revision": document.source_revision,
        "publish_targets": document.publish_targets,
        "metadata": document.doc_metadata,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
    }


class KnowledgeService:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.knowledge_dir = get_knowledge_root(base_dir)
        self.imported_dir = self.knowledge_dir / "imported"
        self.originals_dir = get_knowledge_originals_dir(base_dir, self.knowledge_dir)

    async def ensure_default_knowledge_base(self, session: AsyncSession) -> KnowledgeBase:
        existing = await session.get(KnowledgeBase, DEFAULT_KNOWLEDGE_BASE_ID)
        if existing is not None:
            return existing

        kb = KnowledgeBase(
            id=DEFAULT_KNOWLEDGE_BASE_ID,
            name="Default Knowledge Base",
            description="Default local Markdown knowledge base exposed to DeepAgents as /knowledge/.",
        )
        session.add(kb)
        await session.flush()
        return kb

    async def list_documents(
        self,
        session: AsyncSession,
        *,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
        limit: int = 100,
    ) -> list[KnowledgeDocument]:
        await self.ensure_default_knowledge_base(session)
        stmt = (
            select(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == knowledge_base_id)
            .order_by(KnowledgeDocument.created_at.desc())
            .limit(max(1, min(limit, 500)))
        )
        result = await session.execute(stmt)
        return list(result.scalars())

    def glob_markdown_files(
        self,
        *,
        pattern: str = DEFAULT_MARKDOWN_GLOB,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if not self.knowledge_dir.exists():
            return []

        max_items = max(1, min(limit, 1000))
        files: list[dict[str, Any]] = []
        for path in sorted(self.knowledge_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES:
                continue
            if path.name.startswith(".") or not _matches_glob(path, self.knowledge_dir, pattern):
                continue
            stat = path.stat()
            rel = path.relative_to(self.knowledge_dir).as_posix()
            files.append(
                {
                    "name": path.name,
                    "path": f"knowledge/{rel}",
                    "virtual_path": f"/knowledge/{rel}",
                    "storage_path": str(path),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
            if len(files) >= max_items:
                break
        return files

    def list_directory_files(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """List real files under the configured local knowledge directory."""

        if not self.knowledge_dir.exists():
            return []
        _ensure_directory_readable(self.knowledge_dir)

        max_items = max(1, min(limit, 1000))
        files: list[dict[str, Any]] = []
        for path in sorted(
            (item for item in self.knowledge_dir.rglob("*") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            rel_parts = path.relative_to(self.knowledge_dir).parts
            if any(part.startswith(".") or part in FILE_TREE_SKIP_DIRS for part in rel_parts):
                continue
            if path.name in FILE_TREE_SKIP_FILES:
                continue
            stat = path.stat()
            rel = path.relative_to(self.knowledge_dir).as_posix()
            files.append(
                {
                    "name": path.name,
                    "extension": path.suffix.lower().lstrip("."),
                    "virtual_path": f"/knowledge/{rel}",
                    "storage_path": str(path),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
            if len(files) >= max_items:
                break
        return files

    def list_directory_tree(self, *, max_depth: int = 5, max_entries: int = 500) -> dict[str, Any]:
        """Return a file tree for the configured local knowledge directory."""

        root = self.knowledge_dir
        depth_limit = max(1, min(max_depth, 8))
        entry_limit = max(1, min(max_entries, 2000))
        entry_count = 0
        truncated = False

        def virtual_path_for(path: Path) -> str:
            if path == root:
                return "/knowledge/"
            return f"/knowledge/{path.relative_to(root).as_posix()}"

        def node_for_file(path: Path) -> dict[str, Any] | None:
            try:
                stat = path.stat()
            except OSError:
                return None
            return {
                "name": path.name,
                "type": "file",
                "extension": path.suffix.lower().lstrip("."),
                "virtual_path": virtual_path_for(path),
                "storage_path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }

        def visit(path: Path, depth: int) -> dict[str, Any] | None:
            nonlocal entry_count, truncated
            if path != root and _is_skipped_tree_path(path):
                return None

            if path.is_file():
                entry_count += 1
                return node_for_file(path)

            children: list[dict[str, Any]] = []
            child_count = 0
            if depth < depth_limit:
                try:
                    items = sorted(
                        (item for item in path.iterdir() if not _is_skipped_tree_path(item)),
                        key=lambda item: (not item.is_dir(), item.name.lower()),
                    )
                except OSError:
                    items = []

                for item in items:
                    child_count += 1
                    if entry_count >= entry_limit:
                        truncated = True
                        break
                    child = visit(item, depth + 1)
                    if child is not None:
                        children.append(child)
            elif path != root:
                truncated = True

            node: dict[str, Any] = {
                "name": path.name or "knowledge",
                "type": "directory",
                "virtual_path": virtual_path_for(path),
                "storage_path": str(path),
                "children": children,
                "child_count": child_count,
            }
            if depth >= depth_limit and path != root:
                node["truncated"] = True
            return node

        if not root.exists():
            return {
                "name": root.name or "knowledge",
                "type": "directory",
                "virtual_path": "/knowledge/",
                "storage_path": str(root),
                "children": [],
                "child_count": 0,
                "truncated": False,
            }

        _ensure_directory_readable(root)

        tree = visit(root, 0)
        if tree is None:
            tree = {
                "name": root.name or "knowledge",
                "type": "directory",
                "virtual_path": "/knowledge/",
                "storage_path": str(root),
                "children": [],
                "child_count": 0,
            }
        tree["truncated"] = truncated
        tree["file_count"] = entry_count
        return tree

    def preview_file(self, *, virtual_path: str, max_bytes: int = 200_000) -> dict[str, Any]:
        target = _resolve_virtual_knowledge_path(self.knowledge_dir, virtual_path)
        if not target.exists():
            raise KnowledgeServiceError("File not found.")
        if not target.is_file():
            raise KnowledgeServiceError("Please select a file.")
        rel_parts = target.relative_to(self.knowledge_dir).parts
        if _is_skipped_tree_path(target) or any(part.startswith(".") or part in FILE_TREE_SKIP_DIRS for part in rel_parts):
            raise KnowledgeServiceError("This file cannot be previewed.")

        stat = target.stat()
        suffix = target.suffix.lower()
        rel = target.relative_to(self.knowledge_dir).as_posix()
        base_payload: dict[str, Any] = {
            "name": target.name,
            "extension": suffix.lstrip("."),
            "virtual_path": f"/knowledge/{rel}",
            "storage_path": str(target),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }

        if suffix not in TEXT_PREVIEW_SUFFIXES:
            return {
                **base_payload,
                "preview_type": "unsupported",
                "content": "",
                "truncated": False,
                "message": "这个文件暂时不能直接预览，可以先导入知识库。",
            }

        raw = target.read_bytes()
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        text = _decode_text_preview(raw, truncated=truncated)
        if suffix in MARKDOWN_SUFFIXES:
            text = _rewrite_markdown_links_for_preview(text, base_virtual_path=f"/knowledge/{rel}")

        return {
            **base_payload,
            "preview_type": "text",
            "content": text,
            "truncated": truncated,
            "message": None,
        }

    def repair_document_metadata(self, document: KnowledgeDocument) -> bool:
        """Backfill derived metadata from local artifacts without re-running MinerU."""

        if not _document_has_storage(document):
            return False
        storage_path = Path(document.storage_path)
        metadata = dict(document.doc_metadata or {})
        changed = False

        if storage_path.suffix.lower() in MARKDOWN_SUFFIXES and not _document_has_llamaindex_chunks(document):
            metadata["llamaindex_chunks"] = _build_llamaindex_chunk_metadata(self.knowledge_dir, storage_path)
            changed = True

        assets = metadata.get("assets")
        if isinstance(assets, list) and assets:
            try:
                markdown = storage_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                markdown = ""
            contexts = _extract_markdown_image_contexts(markdown)
            repaired_assets: list[Any] = []
            for asset in assets:
                if not isinstance(asset, dict):
                    repaired_assets.append(asset)
                    continue
                next_asset = dict(asset)
                context = next_asset.get("context")
                has_context = isinstance(context, dict) and bool(context.get("snippet") or context.get("caption") or context.get("heading"))
                if not has_context:
                    for candidate in _asset_context_candidates(next_asset):
                        found = contexts.get(candidate) or contexts.get(Path(candidate).name)
                        if found:
                            next_asset["context"] = found
                            changed = True
                            break
                repaired_assets.append(next_asset)
            if changed:
                metadata["assets"] = repaired_assets

        if changed:
            document.doc_metadata = metadata
        return changed

    def resolve_raw_file(self, *, virtual_path: str) -> Path:
        target = _resolve_virtual_knowledge_path(self.knowledge_dir, virtual_path)
        if not target.exists() or not target.is_file():
            raise KnowledgeServiceError("File not found.")
        rel_parts = target.relative_to(self.knowledge_dir).parts
        if _is_skipped_tree_path(target) or any(part.startswith(".") or part in FILE_TREE_SKIP_DIRS for part in rel_parts):
            raise KnowledgeServiceError("This file cannot be opened.")
        return target

    def grep_markdown_files(
        self,
        *,
        query: str,
        pattern: str = DEFAULT_MARKDOWN_GLOB,
        case_sensitive: bool = False,
        context_lines: int = 1,
        max_matches: int = 50,
    ) -> list[dict[str, Any]]:
        needle = query or ""
        if not needle:
            raise KnowledgeServiceError("grep query is required.")

        context = max(0, min(context_lines, 5))
        limit = max(1, min(max_matches, 500))
        compare_needle = needle if case_sensitive else needle.lower()
        matches: list[dict[str, Any]] = []

        for file_info in self.glob_markdown_files(pattern=pattern, limit=1000):
            path = Path(file_info["storage_path"])
            text = _safe_read_text(path)
            lines = text.splitlines()
            for index, line in enumerate(lines):
                haystack = line if case_sensitive else line.lower()
                if compare_needle not in haystack:
                    continue
                start = max(0, index - context)
                end = min(len(lines), index + context + 1)
                matches.append(
                    {
                        "virtual_path": file_info["virtual_path"],
                        "path": file_info["path"],
                        "storage_path": file_info["storage_path"],
                        "line_number": index + 1,
                        "line": line,
                        "context": lines[start:end],
                    }
                )
                if len(matches) >= limit:
                    return matches
        return matches

    async def import_local_markdown(
        self,
        session: AsyncSession,
        *,
        source_path: str,
        title: str | None = None,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
    ) -> KnowledgeDocument:
        await assert_writes_allowed_tolerant(session)
        source = Path(source_path).expanduser().resolve()
        if not source.exists():
            raise KnowledgeServiceError(f"File not found: {source}")
        if not source.is_file():
            raise KnowledgeServiceError(f"Path is not a file: {source}")
        if source.suffix.lower() not in MARKDOWN_SUFFIXES:
            raise KnowledgeServiceError("Only Markdown files (.md, .markdown) are supported in this MVP.")

        kb = await self.ensure_default_knowledge_base(session)
        if knowledge_base_id != kb.id:
            found = await session.get(KnowledgeBase, knowledge_base_id)
            if found is None:
                raise KnowledgeServiceError(f"Knowledge base not found: {knowledge_base_id}")

        content_sha256 = _sha256(source)
        existing_stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.content_sha256 == content_sha256,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        rebuild_document = None
        if existing is not None and _document_has_storage(existing):
            return existing
        if existing is not None:
            rebuild_document = existing
        old_artifacts = _document_owned_artifacts(rebuild_document)

        date_part = datetime.now().strftime("%Y%m%d")
        target_dir = self.imported_dir / date_part
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(target_dir, f"{_title_or_source_stem(title, source.name)}{source.suffix}")
        filename = target.name
        shutil.copy2(source, target)

        storage_path = str(target)
        virtual_path = f"/knowledge/imported/{date_part}/{filename}"
        llamaindex_chunks = _build_llamaindex_chunk_metadata(self.knowledge_dir, target)
        document = _build_or_update_document(
            rebuild_document,
            knowledge_base_id=knowledge_base_id,
            title=(title or source.stem).strip() or source.name,
            source_type="local_markdown",
            source_path=str(source),
            storage_path=storage_path,
            virtual_path=virtual_path,
            mime_type="text/markdown",
            content_sha256=content_sha256,
            size_bytes=target.stat().st_size,
            status="ready",
            publish_targets=["local_markdown"],
            doc_metadata={
                "deepagents_backend": "/knowledge/",
                "imported_from": str(source),
                "llamaindex_chunks": llamaindex_chunks,
            },
        )
        session.add(document)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (await session.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                return existing
            raise
        _cleanup_replaced_artifacts(self.knowledge_dir, old_artifacts, {target})
        return document

    async def ingest_markdown_upload(
        self,
        session: AsyncSession,
        *,
        filename: str,
        content: bytes,
        title: str | None = None,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
        publish_targets: list[str] | None = None,
        publish_vector_now: bool = True,
        source_connection_id: str | None = None,
        source_item_id: str | None = None,
        origin_url: str | None = None,
        source_revision: str | None = None,
    ) -> tuple[KnowledgeDocument, dict[str, Any]]:
        await assert_writes_allowed_tolerant(session)
        safe_name = _slugify(filename or "document.md")
        if Path(safe_name).suffix.lower() not in MARKDOWN_SUFFIXES:
            raise KnowledgeServiceError("Only Markdown files (.md, .markdown) are supported by this ingestion endpoint.")
        if not content:
            raise KnowledgeServiceError("Uploaded Markdown is empty.")

        publish_targets = publish_targets or ["local_markdown", "vector"]
        kb = await self.ensure_default_knowledge_base(session)
        if knowledge_base_id != kb.id:
            found = await session.get(KnowledgeBase, knowledge_base_id)
            if found is None:
                raise KnowledgeServiceError(f"Knowledge base not found: {knowledge_base_id}")

        content_sha256 = hashlib.sha256(content).hexdigest()
        existing_stmt = _document_identity_stmt(
            knowledge_base_id=knowledge_base_id,
            content_sha256=content_sha256,
            source_item_id=source_item_id,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        rebuild_document = None
        if (
            existing is not None
            and existing.content_sha256 == content_sha256
            and _document_ready_for_dedup(existing)
        ):
            return existing, {"deduplicated": True, "vector_index": {"refreshed": False, "reason": "document already exists"}}
        if existing is not None:
            rebuild_document = existing
        old_artifacts = _document_owned_artifacts(rebuild_document)

        date_part = datetime.now().strftime("%Y%m%d")
        target_dir = self.imported_dir / date_part
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(target_dir, f"{_title_or_source_stem(title, safe_name)}{Path(safe_name).suffix or '.md'}")
        target.write_bytes(content)

        virtual_path = f"/knowledge/imported/{date_part}/{target.name}"
        llamaindex_chunks = _build_llamaindex_chunk_metadata(self.knowledge_dir, target)
        document = _build_or_update_document(
            rebuild_document,
            knowledge_base_id=knowledge_base_id,
            title=(title or Path(safe_name).stem).strip() or safe_name,
            source_type="markdown_upload",
            source_path=filename,
            storage_path=str(target),
            virtual_path=virtual_path,
            mime_type="text/markdown",
            content_sha256=content_sha256,
            size_bytes=target.stat().st_size,
            status="ready",
            publish_targets=publish_targets,
            source_connection_id=source_connection_id,
            source_item_id=source_item_id,
            origin_url=origin_url,
            source_revision=source_revision,
            doc_metadata={
                "mode": "markdown_upload",
                "original_filename": filename,
                "markdown_sha256": _sha256(target),
                "deepagents_backend": "/knowledge/",
                "llamaindex_chunks": llamaindex_chunks,
            },
        )
        session.add(document)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (await session.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                return existing, {"deduplicated": True, "vector_index": {"refreshed": False, "reason": "document already exists"}}
            raise
        _cleanup_replaced_artifacts(self.knowledge_dir, old_artifacts, {target})

        vector_result = {"refreshed": False, "reason": "vector publish not requested"}
        if publish_vector_now and ("vector" in publish_targets or "local_vector" in publish_targets):
            vector_result = refresh_local_knowledge_index(self.base_dir)

        return document, {
            "deduplicated": False,
            "markdown_path": str(target),
            "vector_index": vector_result,
        }

    def replace_markdown_document_content(
        self,
        *,
        document: KnowledgeDocument,
        content: bytes,
        title: str,
        publish_targets: list[str],
    ) -> KnowledgeDocument:
        """Replace an owned Markdown document without allocating a new filename.

        This is intentionally separate from upload ingestion: uploads are
        immutable catalog additions, while sources such as Read Later refresh
        the capture they already own.
        """

        if not content:
            raise KnowledgeServiceError("Markdown content is empty.")
        target = Path(document.storage_path).resolve()
        knowledge_root = self.knowledge_dir.resolve()
        if not target.is_relative_to(knowledge_root) or target.suffix.lower() not in MARKDOWN_SUFFIXES:
            raise KnowledgeServiceError("The existing Markdown document is outside the knowledge directory.")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        content_sha256 = hashlib.sha256(content).hexdigest()
        document.title = title.strip() or document.title
        document.content_sha256 = content_sha256
        document.size_bytes = target.stat().st_size
        document.status = "ready"
        document.publish_targets = publish_targets
        document.doc_metadata = {
            **(document.doc_metadata or {}),
            "markdown_sha256": _sha256(target),
            "llamaindex_chunks": _build_llamaindex_chunk_metadata(self.knowledge_dir, target),
        }
        return document

    async def ingest_generic_upload(
        self,
        session: AsyncSession,
        *,
        filename: str,
        content: bytes,
        title: str | None = None,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
        publish_targets: list[str] | None = None,
        source_connection_id: str | None = None,
        source_item_id: str | None = None,
        origin_url: str | None = None,
        source_revision: str | None = None,
    ) -> tuple[KnowledgeDocument, dict[str, Any]]:
        await assert_writes_allowed_tolerant(session)
        safe_name = _slugify(filename or "document")
        suffix = Path(safe_name).suffix.lower()
        if suffix not in GENERIC_UPLOAD_SUFFIXES:
            raise KnowledgeServiceError(
                f"Unsupported knowledge file type: {suffix or 'unknown'}. Supported: .pdf, .md, .markdown, .xlsx, .xls, .csv, .tsv, .txt, .docx"
            )
        if not content:
            raise KnowledgeServiceError("Uploaded file is empty.")

        publish_targets = publish_targets or ["local_file"]
        kb = await self.ensure_default_knowledge_base(session)
        if knowledge_base_id != kb.id:
            found = await session.get(KnowledgeBase, knowledge_base_id)
            if found is None:
                raise KnowledgeServiceError(f"Knowledge base not found: {knowledge_base_id}")

        content_sha256 = hashlib.sha256(content).hexdigest()
        existing_stmt = _document_identity_stmt(
            knowledge_base_id=knowledge_base_id,
            content_sha256=content_sha256,
            source_item_id=source_item_id,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        rebuild_document = None
        if existing is not None and existing.content_sha256 == content_sha256 and _document_has_storage(existing):
            return existing, {"deduplicated": True, "vector_index": {"refreshed": False, "reason": "document already exists"}}
        if existing is not None:
            rebuild_document = existing
        old_artifacts = _document_owned_artifacts(rebuild_document)

        date_part = datetime.now().strftime("%Y%m%d")
        target_dir = self.imported_dir / date_part
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(target_dir, f"{_title_or_source_stem(title, safe_name)}{Path(safe_name).suffix}")
        target.write_bytes(content)

        virtual_path = f"/knowledge/imported/{date_part}/{target.name}"
        mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        document = _build_or_update_document(
            rebuild_document,
            knowledge_base_id=knowledge_base_id,
            title=(title or Path(safe_name).stem).strip() or safe_name,
            source_type="file_upload",
            source_path=filename,
            storage_path=str(target),
            virtual_path=virtual_path,
            mime_type=mime_type,
            content_sha256=content_sha256,
            size_bytes=target.stat().st_size,
            status="ready",
            publish_targets=publish_targets,
            source_connection_id=source_connection_id,
            source_item_id=source_item_id,
            origin_url=origin_url,
            source_revision=source_revision,
            doc_metadata={
                "mode": "generic_upload",
                "original_filename": filename,
                "deepagents_backend": "/knowledge/",
                "pandas_engine_ready": suffix in {".xlsx", ".xls", ".csv", ".tsv"},
            },
        )
        session.add(document)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (await session.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                return existing, {"deduplicated": True, "vector_index": {"refreshed": False, "reason": "document already exists"}}
            raise
        _cleanup_replaced_artifacts(self.knowledge_dir, old_artifacts, {target})

        return document, {
            "deduplicated": False,
            "file_path": str(target),
            "vector_index": {"refreshed": False, "reason": "generic file stored for file/pandas tools; no vector refresh"},
        }

    async def ingest_pdf_upload(
        self,
        session: AsyncSession,
        *,
        filename: str,
        content: bytes,
        title: str | None = None,
        knowledge_base_id: str = DEFAULT_KNOWLEDGE_BASE_ID,
        publish_targets: list[str] | None = None,
        mineru_client: MinerUClient | None = None,
        publish_vector_now: bool = True,
        source_connection_id: str | None = None,
        source_item_id: str | None = None,
        origin_url: str | None = None,
        source_revision: str | None = None,
    ) -> tuple[KnowledgeDocument, dict[str, Any]]:
        await assert_writes_allowed_tolerant(session)
        safe_name = _slugify(filename or "document.pdf")
        if Path(safe_name).suffix.lower() not in PDF_SUFFIXES:
            raise KnowledgeServiceError("Only PDF files are supported by this ingestion endpoint.")
        if not content:
            raise KnowledgeServiceError("Uploaded PDF is empty.")

        publish_targets = publish_targets or ["local_markdown", "vector"]
        kb = await self.ensure_default_knowledge_base(session)
        if knowledge_base_id != kb.id:
            found = await session.get(KnowledgeBase, knowledge_base_id)
            if found is None:
                raise KnowledgeServiceError(f"Knowledge base not found: {knowledge_base_id}")

        original_sha256 = hashlib.sha256(content).hexdigest()
        existing_stmt = _document_identity_stmt(
            knowledge_base_id=knowledge_base_id,
            content_sha256=original_sha256,
            source_item_id=source_item_id,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        rebuild_document = None
        if (
            existing is not None
            and existing.content_sha256 == original_sha256
            and _document_ready_for_dedup(existing)
        ):
            return existing, {"deduplicated": True, "vector_index": {"refreshed": False, "reason": "document already exists"}}
        if existing is not None:
            rebuild_document = existing
        old_artifacts = _document_owned_artifacts(rebuild_document)

        date_part = datetime.now().strftime("%Y%m%d")
        import_id = new_id("pdf")

        original_dir = self.originals_dir / date_part
        original_dir.mkdir(parents=True, exist_ok=True)
        original_path = original_dir / f"{import_id}-{safe_name}"
        original_path.write_bytes(content)

        assets_dir = self.knowledge_dir / "assets" / date_part / import_id
        client = mineru_client or MinerUClient()
        parse_result: MinerUParseResult = await client.parse_pdf_bytes(
            filename=safe_name,
            content=content,
            assets_dir=assets_dir,
        )
        target_dir = self.imported_dir / date_part
        target_dir.mkdir(parents=True, exist_ok=True)
        md_path = _unique_path(target_dir, f"{_title_or_source_stem(title, safe_name)}.md")
        md_name = md_path.name

        assets_virtual_prefix = f"/knowledge/assets/{date_part}/{import_id}"
        markdown_asset_prefix = Path(os.path.relpath(assets_dir, start=md_path.parent)).as_posix()
        markdown, rewritten_assets = _rewrite_markdown_asset_links(
            parse_result.markdown,
            assets=parse_result.assets or [],
            assets_virtual_prefix=assets_virtual_prefix,
            markdown_asset_prefix=markdown_asset_prefix,
            assets_dir=assets_dir,
        )
        markdown = markdown.strip()
        if not markdown:
            raise KnowledgeServiceError("MinerU returned empty markdown.")

        md_path.write_text(markdown + "\n", encoding="utf-8")

        virtual_path = f"/knowledge/imported/{date_part}/{md_name}"
        image_paths = [Path(str(asset.get("path"))) for asset in rewritten_assets if isinstance(asset, dict) and asset.get("path")]
        llamaindex_chunks = _build_llamaindex_chunk_metadata(self.knowledge_dir, md_path, image_paths)
        document = _build_or_update_document(
            rebuild_document,
            knowledge_base_id=knowledge_base_id,
            title=(title or Path(safe_name).stem).strip() or safe_name,
            source_type="pdf_mineru",
            source_path=str(original_path),
            storage_path=str(md_path),
            virtual_path=virtual_path,
            mime_type="text/markdown",
            content_sha256=original_sha256,
            size_bytes=md_path.stat().st_size,
            status="ready",
            publish_targets=publish_targets,
            source_connection_id=source_connection_id,
            source_item_id=source_item_id,
            origin_url=origin_url,
            source_revision=source_revision,
            doc_metadata={
                "mode": "multimodal_pdf",
                "parser": "mineru",
                "original_filename": filename,
                "original_path": str(original_path),
                "original_sha256": original_sha256,
                "markdown_sha256": _sha256(md_path),
                "mineru": parse_result.raw_response,
                "assets": rewritten_assets,
                "llamaindex_chunks": llamaindex_chunks,
                "multimodal": {
                    "llamaindex": True,
                    "text_artifact": virtual_path,
                    "image_asset_count": len(rewritten_assets),
                    "image_assets_dir": str(assets_dir),
                    "image_assets_virtual_prefix": assets_virtual_prefix,
                    "markdown_asset_link_mode": "relative",
                },
                "deepagents_backend": "/knowledge/",
            },
        )
        session.add(document)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (await session.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                return existing, {"deduplicated": True, "vector_index": {"refreshed": False, "reason": "document already exists"}}
            raise
        _cleanup_replaced_artifacts(self.knowledge_dir, old_artifacts, {md_path, original_path, assets_dir})

        vector_result = {"refreshed": False, "reason": "vector publish not requested"}
        if publish_vector_now and ("vector" in publish_targets or "local_vector" in publish_targets):
            vector_result = refresh_local_knowledge_index(self.base_dir)

        return document, {
            "deduplicated": False,
            "markdown_path": str(md_path),
            "original_path": str(original_path),
            "vector_index": vector_result,
        }
