"""Independent hybrid search for the knowledge portal.

This module deliberately does not import Agent tools, sessions, graphs, or
middleware.  Its local catalog is the source of truth for scope and metadata;
shared Milvus collections provide text, BM25, and image semantic retrieval.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from config import (
    get_knowledge_multimodal_index_config,
    get_rag_hybrid_config,
    get_rag_rerank_config,
    load_config,
    save_config,
)
from knowledge.paths import get_knowledge_root

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_DIRECTORIES: list[dict[str, Any]] = [
    {
        "id": "assets",
        "path": "assets",
        "enabled": True,
        "recursive": True,
        "content_types": ["image"],
        "referenced_images_only": True,
    },
    {
        "id": "imported",
        "path": "imported",
        "enabled": True,
        "recursive": True,
        "content_types": ["markdown", "pdf", "document"],
    },
    {
        "id": "originals",
        "path": "originals",
        "enabled": True,
        "recursive": True,
        "content_types": ["pdf", "document", "image"],
    },
    {
        "id": "llm-wiki",
        "path": "llm-wiki/wiki",
        "enabled": True,
        "recursive": True,
        "content_types": ["markdown"],
    },
    {
        "id": "source-code-updates",
        "path": "source-code-updates",
        "enabled": True,
        "recursive": True,
        "content_types": ["markdown"],
    },
]

DEFAULT_SEARCH_EXCLUDES = [
    "**/.DS_Store",
    "**/.git/**",
    "**/.puddingclaw/**",
    "llm-wiki/raw/**",
    "llm-wiki/wiki/index.md",
    "llm-wiki/wiki/log.md",
]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
MARKDOWN_SUFFIXES = {".md", ".markdown"}
PDF_SUFFIXES = {".pdf"}
DOCUMENT_SUFFIXES = {".doc", ".docx", ".txt", ".csv", ".tsv", ".xlsx", ".xls"}
MAX_INDEX_TEXT_BYTES = 2 * 1024 * 1024
MAX_SNIPPET_CHARS = 320
RRF_K = 60
_CATALOG_LOCK = threading.RLock()


class PortalSearchConfigError(ValueError):
    """Raised when portal configuration would escape the knowledge root."""


def _default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "directories": [dict(item) for item in DEFAULT_SEARCH_DIRECTORIES],
        "sources": {"read_later": {"enabled": True}},
        "exclude": list(DEFAULT_SEARCH_EXCLUDES),
    }


def _normalize_relative_path(raw: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise PortalSearchConfigError("搜索目录必须是知识库根目录内的相对路径")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PortalSearchConfigError("搜索目录不能包含 .、空路径或 ..")
    return path.as_posix()


def normalize_search_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = _default_config()
    if isinstance(raw, dict):
        # Portal search is a core knowledge-base capability, not an optional
        # runtime service. Keep the legacy field for config compatibility, but
        # never allow it to disable search.
        base["enabled"] = True
        base["exclude"] = [str(item).replace("\\", "/") for item in raw.get("exclude", base["exclude"]) if str(item).strip()]
        directories = raw.get("directories", base["directories"])
        if not isinstance(directories, list):
            raise PortalSearchConfigError("搜索目录配置必须是数组")
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in directories:
            if not isinstance(item, dict):
                raise PortalSearchConfigError("搜索目录配置项必须是对象")
            directory_id = str(item.get("id") or "").strip()
            if not directory_id or directory_id in seen_ids:
                raise PortalSearchConfigError("搜索目录 id 必须非空且唯一")
            seen_ids.add(directory_id)
            path = _normalize_relative_path(str(item.get("path") or ""))
            content_types = item.get("content_types", ["markdown"])
            if not isinstance(content_types, list) or not content_types:
                raise PortalSearchConfigError(f"搜索目录 {directory_id} 缺少 content_types")
            normalized.append({
                "id": directory_id,
                "path": path,
                "enabled": bool(item.get("enabled", True)),
                "recursive": bool(item.get("recursive", True)),
                "content_types": sorted({str(value).strip().lower() for value in content_types if str(value).strip()}),
                "referenced_images_only": bool(item.get("referenced_images_only", False)),
            })
        base["directories"] = normalized
        sources = raw.get("sources", base["sources"])
        if isinstance(sources, dict):
            read_later = sources.get("read_later", {})
            base["sources"] = {"read_later": {"enabled": bool(read_later.get("enabled", True)) if isinstance(read_later, dict) else True}}
    return base


def get_search_config() -> dict[str, Any]:
    return normalize_search_config(load_config().get("knowledge", {}).get("search"))


def save_search_config(value: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    current = normalize_search_config(config.get("knowledge", {}).get("search"))
    merged = {**current, **value}
    normalized = normalize_search_config(merged)
    config.setdefault("knowledge", {})["search"] = normalized
    save_config(config)
    return normalized


def _path_is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_search_directory(root: Path, relative_path: str) -> Path:
    normalized = _normalize_relative_path(relative_path)
    candidate = (root / normalized).resolve()
    if not _path_is_inside(candidate, root):
        raise PortalSearchConfigError("搜索目录不能通过符号链接逃逸知识库根目录")
    return candidate


def _is_excluded(relative_path: str, excludes: Iterable[str]) -> bool:
    normalized = relative_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    return any(fnmatch.fnmatch(normalized, pattern) or path.match(pattern) for pattern in excludes)


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "document"


def _read_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_INDEX_TEXT_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if not match:
        return {}
    result: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.strip().strip("'\"")
        if value:
            result[key.strip().lower()] = value
    return result


def _title(text: str, path: Path) -> str:
    metadata = _frontmatter(text)
    if metadata.get("title"):
        return str(metadata["title"])
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


def _referenced_images(markdown_path: Path, root: Path) -> dict[Path, dict[str, Any]]:
    text = _read_text(markdown_path)
    if not text:
        return {}
    result: dict[Path, dict[str, Any]] = {}
    heading = ""
    for line_number, line in enumerate(text.splitlines(), start=1):
        heading_match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading_match:
            heading = heading_match.group(1).strip()
        urls = re.findall(r"!\[([^\]]*)\]\(([^)\s]+)", line)
        urls += [("", value) for value in re.findall(r"<img\b[^>]*?src=[\"'](.*?)[\"']", line, flags=re.IGNORECASE)]
        for alt, raw_url in urls:
            if re.match(r"^(?:https?:|data:)", raw_url, flags=re.IGNORECASE):
                continue
            candidate = (markdown_path.parent / raw_url.lstrip("/")).resolve()
            if raw_url.startswith("/knowledge/"):
                candidate = (root / raw_url.removeprefix("/knowledge/")).resolve()
            if not _path_is_inside(candidate, root) or not candidate.is_file() or candidate.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            result[candidate] = {
                "caption": alt.strip(),
                "heading": heading,
                "line_number": line_number,
                "linked_markdown": f"/knowledge/{markdown_path.relative_to(root).as_posix()}",
            }
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _record(path: Path, root: Path, directory_id: str, content_type: str, *, text: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    metadata = _frontmatter(text)
    return {
        "id": f"{content_type}:{relative}",
        "path": relative,
        "uri": f"/knowledge/{relative}",
        "directory_id": directory_id,
        "content_type": content_type,
        "title": str(metadata.get("title") or _title(text, path)),
        "aliases": metadata.get("aliases", ""),
        "page_type": str(metadata.get("page_type") or metadata.get("type") or ""),
        "platform": str(metadata.get("platform") or metadata.get("site") or ""),
        "text": text,
        "context": context or {},
        "content_sha256": _sha256(path),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def build_catalog(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    records: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    referenced: dict[Path, dict[str, Any]] = {}
    for directory in config["directories"]:
        if not directory["enabled"]:
            continue
        directory_root = resolve_search_directory(root, directory["path"])
        if not directory_root.exists():
            missing.append({"id": directory["id"], "path": directory["path"], "status": "missing"})
            continue
        paths = directory_root.rglob("*") if directory["recursive"] else directory_root.glob("*")
        for path in sorted(paths):
            if not path.is_file() or not _path_is_inside(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            if _is_excluded(relative, config["exclude"]):
                continue
            kind = _content_type(path)
            if kind not in directory["content_types"]:
                continue
            if kind == "image" and directory.get("referenced_images_only"):
                if path not in referenced:
                    continue
            text = _read_text(path) if kind == "markdown" or kind == "document" else ""
            record = _record(path, root, directory["id"], kind, text=text, context=referenced.get(path))
            records.append(record)
            if kind == "markdown":
                referenced.update(_referenced_images(path, root))

    # A markdown file may live in a directory processed after assets.  Add all
    # referenced images in a second deterministic pass, still respecting the
    # configured image directories and the root boundary.
    if referenced:
        existing = {record["path"] for record in records}
        for directory in config["directories"]:
            if not directory["enabled"] or "image" not in directory["content_types"]:
                continue
            directory_root = resolve_search_directory(root, directory["path"])
            for image_path, context in sorted(referenced.items(), key=lambda item: str(item[0])):
                if image_path.relative_to(root).as_posix() in existing or not _path_is_inside(image_path, directory_root):
                    continue
                if image_path.is_file() and not _is_excluded(image_path.relative_to(root).as_posix(), config["exclude"]):
                    records.append(_record(image_path, root, directory["id"], "image", context=context))

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records": records,
        "missing_directories": missing,
        "counts": {
            "records": len(records),
            "documents": sum(record["content_type"] != "image" for record in records),
            "images": sum(record["content_type"] == "image" for record in records),
        },
    }


def _catalog_path(base_dir: Path) -> Path:
    from runtime_identity.paths import PuddingClawPaths

    return PuddingClawPaths.from_environment().state() / "knowledge-search" / "catalog.json"


def load_catalog(base_dir: Path) -> dict[str, Any] | None:
    path = _catalog_path(base_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) and isinstance(payload.get("records"), list) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_catalog(base_dir: Path, config: dict[str, Any], catalog: dict[str, Any]) -> None:
    path = _catalog_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"config": config, **catalog}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _directory_for_path(path: Path, root: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    kind = _content_type(path)
    for directory in config["directories"]:
        if not directory["enabled"] or kind not in directory["content_types"]:
            continue
        directory_root = resolve_search_directory(root, directory["path"])
        if _path_is_inside(path, directory_root):
            return directory
    return None


def _catalog_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "records": len(records),
        "documents": sum(record.get("content_type") != "image" for record in records),
        "images": sum(record.get("content_type") == "image" for record in records),
    }


def _tokenize(value: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"[\w]+|[\u3400-\u9fff]", value, flags=re.UNICODE) if token.strip()]


def _snippet(text: str, query: str) -> tuple[str, list[str]]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return "", []
    folded = clean.casefold()
    position = folded.find(query.casefold())
    if position < 0:
        for token in _tokenize(query):
            position = folded.find(token)
            if position >= 0:
                break
    start = max(0, position - 100) if position >= 0 else 0
    value = clean[start : start + MAX_SNIPPET_CHARS]
    if start > 0:
        value = "…" + value
    if start + MAX_SNIPPET_CHARS < len(clean):
        value += "…"
    highlights = [token for token in _tokenize(query) if token in folded]
    return value, list(dict.fromkeys(highlights))


def _authority(record: dict[str, Any]) -> int:
    path = str(record.get("path") or "")
    if path.startswith("llm-wiki/wiki/"):
        return 4
    if path.startswith("imported/") or path.startswith("source-code-updates/"):
        return 3
    if record.get("source_type") == "read_later":
        return 2
    if path.startswith("originals/"):
        return 1
    return 0


def _deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = str(record.get("content_sha256") or record.get("id"))
        grouped[key].append(record)
    result: list[dict[str, Any]] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: (-_authority(item), str(item.get("path") or "")))
        primary = dict(ordered[0])
        primary["source_group"] = {
            "original": next((item["uri"] for item in ordered if item["path"].startswith("originals/")), None),
            "imported": next((item["uri"] for item in ordered if item["path"].startswith("imported/")), None),
            "wiki": next((item["uri"] for item in ordered if item["path"].startswith("llm-wiki/wiki/")), None),
            "versions": [item["uri"] for item in ordered[1:]],
        }
        result.append(primary)
    return result


def _decode_milvus_entity(result: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Decode a LlamaIndex Milvus row without importing an Agent tool."""

    entity = result.get("entity") if isinstance(result.get("entity"), dict) else result
    metadata: dict[str, Any] = {}
    text = str(entity.get("text") or "")
    node_content_raw = entity.get("_node_content") or entity.get("node_content")
    if isinstance(node_content_raw, str) and node_content_raw.strip().startswith("{"):
        try:
            node_content = json.loads(node_content_raw)
        except json.JSONDecodeError:
            node_content = None
        if isinstance(node_content, dict):
            inline = node_content.get("metadata")
            if isinstance(inline, dict):
                metadata.update(inline)
            text = text or str(
                node_content.get("text")
                or (node_content.get("text_resource") or {}).get("text")
                or ""
            )
            image_resource = node_content.get("image_resource")
            if isinstance(image_resource, dict):
                metadata.setdefault("file_path", image_resource.get("path") or image_resource.get("image_path"))
    inline_metadata = entity.get("metadata")
    if isinstance(inline_metadata, dict):
        metadata.update(inline_metadata)
    return text, metadata, entity


def _relative_knowledge_path(value: Any, root: Path) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("/knowledge/"):
        return raw.removeprefix("/knowledge/").lstrip("/")
    path = Path(raw).expanduser()
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            return ""
    return raw.lstrip("/")


def _record_path_map(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for record in records:
        paths = [str(record.get("path") or "")]
        source_group = record.get("source_group")
        if isinstance(source_group, dict):
            paths.extend(
                str(value or "").removeprefix("/knowledge/")
                for key in ("original", "imported", "wiki")
                if (value := source_group.get(key))
            )
            paths.extend(str(value or "").removeprefix("/knowledge/") for value in source_group.get("versions", []))
        for path in paths:
            if path:
                mapping[path] = record
    return mapping


def _semantic_retrieval(
    query: str,
    *,
    root: Path,
    records: list[dict[str, Any]],
    limit: int,
    include_images: bool,
) -> dict[str, Any]:
    """Query shared Milvus infrastructure while keeping Portal runtime independent."""

    index_config = get_knowledge_multimodal_index_config()
    if not index_config.get("enabled") or str(index_config.get("vector_store") or "").lower() != "milvus":
        return {"enabled": False, "channels": {}, "errors": {"semantic": "Milvus semantic index is disabled"}}

    try:
        from pymilvus import MilvusClient

        from llm.embed_client import get_embedding_model
        from llm.multimodal_embedding import get_multimodal_embedding_model
    except ImportError as exc:
        return {"enabled": False, "channels": {}, "errors": {"semantic": f"{type(exc).__name__}: {exc}"}}

    hybrid = get_rag_hybrid_config()
    candidate_limit = max(limit, int(hybrid.get("candidate_top_k") or limit))
    errors: dict[str, str] = {}
    text_collection = str(index_config.get("text_collection") or "puddingclaw_knowledge_text")
    image_collection = str(index_config.get("image_collection") or "puddingclaw_knowledge_image")
    try:
        client = MilvusClient(uri=index_config.get("milvus_uri", "http://localhost:19530"), timeout=2.0)
        has_text_collection = bool(client.has_collection(text_collection))
        has_image_collection = bool(include_images and client.has_collection(image_collection))
    except Exception as exc:  # noqa: BLE001
        return {"enabled": False, "channels": {}, "errors": {"milvus": f"{type(exc).__name__}: {exc}"}}
    if not has_text_collection and not has_image_collection:
        return {
            "enabled": False,
            "channels": {},
            "errors": {"milvus": "Configured semantic collections do not exist; publish the knowledge vector index first"},
        }

    text_embedding: list[float] | None = None
    image_embedding: list[float] | None = None
    if has_text_collection:
        try:
            text_embedding = get_embedding_model().get_query_embedding(query)
        except Exception as exc:  # noqa: BLE001 - lexical/BM25 fallback must remain usable
            errors["text_embedding"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Portal text query embedding failed: %s", exc)
    if has_image_collection:
        try:
            image_embedding = get_multimodal_embedding_model().get_query_embedding(query)
        except Exception as exc:  # noqa: BLE001
            errors["image_embedding"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Portal image query embedding failed: %s", exc)
    routes: list[dict[str, Any]] = []
    if text_embedding is not None:
        routes.append({"channel": "text_vector", "collection": text_collection, "data": [text_embedding], "field": "embedding"})
    if include_images and image_embedding is not None:
        routes.append({"channel": "image_vector", "collection": image_collection, "data": [image_embedding], "field": "embedding"})

    path_map = _record_path_map(records)
    channels: dict[str, list[dict[str, Any]]] = {}
    for route in routes:
        channel = str(route["channel"])
        collection = str(route["collection"])
        try:
            if not client.has_collection(collection):
                errors[channel] = f"Milvus collection does not exist: {collection}"
                continue
            kwargs: dict[str, Any] = {
                "collection_name": collection,
                "data": route["data"],
                "anns_field": route["field"],
                "limit": candidate_limit,
                "output_fields": ["text", "_node_content", "_node_type", "doc_id"],
            }
            if route.get("search_params"):
                kwargs["search_params"] = route["search_params"]
            result_groups = client.search(**kwargs)
        except Exception as exc:  # noqa: BLE001
            errors[channel] = f"{type(exc).__name__}: {exc}"
            logger.warning("Portal %s retrieval failed: %s", channel, exc)
            continue

        seen_records: set[str] = set()
        channel_hits: list[dict[str, Any]] = []
        for result in result_groups[0] if result_groups else []:
            text, metadata, entity = _decode_milvus_entity(result)
            path = _relative_knowledge_path(metadata.get("virtual_path") or metadata.get("file_path"), root)
            record = path_map.get(path)
            if record is None:
                continue
            record_id = str(record.get("id") or path)
            if record_id in seen_records:
                continue
            seen_records.add(record_id)
            context = metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
            quote = text.strip()
            if not quote:
                quote = str(context.get("snippet") or context.get("caption") or context.get("heading") or record.get("title") or "")
            score_value = result.get("distance", result.get("score"))
            try:
                raw_score = float(score_value)
            except (TypeError, ValueError):
                raw_score = None
            channel_hits.append({
                "record": record,
                "record_id": record_id,
                "quote": quote,
                "raw_score": raw_score,
                "metadata": metadata,
                "entity_id": entity.get("id"),
            })
        channels[channel] = channel_hits

    return {"enabled": any(channels.values()), "channels": channels, "errors": errors}


def _rrf_fuse(rankings: dict[str, list[str]], weights: dict[str, float]) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for channel, ranked_ids in rankings.items():
        weight = max(0.0, float(weights.get(channel, 1.0)))
        for rank, record_id in enumerate(ranked_ids, start=1):
            scores[record_id] += weight / float(RRF_K + rank)
    return dict(scores)


def _rerank_scored_records(
    query: str,
    scored: list[tuple[float, dict[str, Any]]],
    *,
    semantic_hits: dict[str, list[tuple[str, dict[str, Any]]]],
    requested_limit: int,
) -> tuple[list[tuple[float, dict[str, Any]]], bool, str | None]:
    """Rerank the fused Portal candidates without entering Agent runtime."""

    config = get_rag_rerank_config()
    if not config.get("enabled") or len(scored) < 2:
        return scored, False, None
    candidate_limit = min(len(scored), max(requested_limit, int(config.get("candidate_top_k") or requested_limit)))
    candidates = scored[:candidate_limit]
    documents: list[str] = []
    for _, record in candidates:
        record_id = str(record.get("id") or "")
        semantic_quote = next(
            (str(hit.get("quote") or "") for _, hit in semantic_hits.get(record_id, []) if hit.get("quote")),
            "",
        )
        context = record.get("context") if isinstance(record.get("context"), dict) else {}
        body = semantic_quote or str(record.get("text") or "") or " ".join(str(value) for value in context.values())
        documents.append("\n".join(part for part in [str(record.get("title") or ""), str(record.get("path") or ""), body[:2000]] if part))
    try:
        from llm.rerank_client import rerank_documents

        reranked = rerank_documents(
            query=query,
            documents=documents,
            top_n=min(candidate_limit, max(requested_limit, int(config.get("top_n") or requested_limit))),
        )
    except Exception as exc:  # noqa: BLE001 - fused order remains a safe fallback
        logger.warning("Portal rerank failed: %s", exc)
        return scored, False, f"{type(exc).__name__}: {exc}"
    if not reranked:
        return scored, False, None
    ordered: list[tuple[float, dict[str, Any]]] = []
    selected_indices: set[int] = set()
    for item in reranked:
        index = int(item.index)
        if 0 <= index < len(candidates) and index not in selected_indices:
            selected_indices.add(index)
            score, record = candidates[index]
            rerank_score = float(item.score) if item.score is not None else score
            ordered.append((rerank_score, record))
    ordered.extend(candidate for index, candidate in enumerate(candidates) if index not in selected_indices)
    ordered.extend(scored[candidate_limit:])
    return ordered, bool(ordered), None


class KnowledgePortalSearchService:
    """Portal-only search service with explicit filesystem boundaries."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.root = get_knowledge_root(base_dir).resolve()

    @property
    def config(self) -> dict[str, Any]:
        return get_search_config()

    def _records(self) -> list[dict[str, Any]]:
        catalog = load_catalog(self.base_dir)
        if catalog is not None:
            return [record for record in catalog.get("records", []) if isinstance(record, dict)]
        # This is a read-only bootstrap view, not a vector/index rebuild.  It
        # keeps a new installation usable until the user runs index refresh.
        return build_catalog(self.root, self.config)["records"] if self.root.exists() else []

    def refresh(self, *, rebuild: bool = False) -> dict[str, Any]:
        del rebuild  # catalog refresh is deterministic; vectors are owned by the import pipeline
        config = self.config
        with _CATALOG_LOCK:
            catalog = build_catalog(self.root, config) if self.root.exists() else {"records": [], "counts": {"records": 0, "documents": 0, "images": 0}, "missing_directories": []}
            _write_catalog(self.base_dir, config, catalog)
        return self.status(catalog=catalog)

    def refresh_paths(self, changed_paths: Iterable[str | Path]) -> dict[str, Any]:
        """Incrementally update catalog rows affected by filesystem events."""

        config = self.config
        with _CATALOG_LOCK:
            catalog = load_catalog(self.base_dir)
            if catalog is None:
                return self.refresh()
            records = [record for record in catalog.get("records", []) if isinstance(record, dict)]
            by_path = {str(record.get("path") or ""): record for record in records}
            affected_images: set[Path] = set()
            changed = []
            for raw_path in changed_paths:
                path = Path(raw_path).expanduser().resolve()
                if not _path_is_inside(path, self.root):
                    continue
                relative = path.relative_to(self.root).as_posix()
                if _is_excluded(relative, config["exclude"]):
                    by_path.pop(relative, None)
                    continue
                changed.append((path, relative))

            # Markdown changes own their referenced-image context. Remove the
            # previous dependency rows before adding the new references.
            for path, relative in changed:
                previous = by_path.pop(relative, None)
                is_markdown = path.suffix.lower() in MARKDOWN_SUFFIXES or (
                    isinstance(previous, dict) and previous.get("content_type") == "markdown"
                )
                if not is_markdown:
                    continue
                virtual_path = f"/knowledge/{relative}"
                for image_relative, record in list(by_path.items()):
                    context = record.get("context") if isinstance(record.get("context"), dict) else {}
                    if context.get("linked_markdown") == virtual_path:
                        by_path.pop(image_relative, None)
                        affected_images.add((self.root / image_relative).resolve())
                if not path.is_file():
                    continue
                directory = _directory_for_path(path, self.root, config)
                if directory is None:
                    continue
                text = _read_text(path)
                by_path[relative] = _record(path, self.root, directory["id"], "markdown", text=text)
                for image_path, context in _referenced_images(path, self.root).items():
                    affected_images.add(image_path)
                    image_directory = _directory_for_path(image_path, self.root, config)
                    if image_directory is not None:
                        image_relative = image_path.relative_to(self.root).as_posix()
                        if not _is_excluded(image_relative, config["exclude"]):
                            by_path[image_relative] = _record(
                                image_path,
                                self.root,
                                image_directory["id"],
                                "image",
                                context=context,
                            )

            # Non-Markdown paths are independent records unless their directory
            # requires a Markdown reference. Re-resolve affected images against
            # all currently indexed Markdown documents.
            for path, relative in changed:
                if path.suffix.lower() in MARKDOWN_SUFFIXES:
                    continue
                by_path.pop(relative, None)
                if path.suffix.lower() in IMAGE_SUFFIXES:
                    affected_images.add(path)
                    continue
                if not path.is_file():
                    continue
                directory = _directory_for_path(path, self.root, config)
                if directory is not None:
                    by_path[relative] = _record(
                        path,
                        self.root,
                        directory["id"],
                        _content_type(path),
                        text=_read_text(path),
                    )

            markdown_paths = [
                self.root / relative
                for relative, record in by_path.items()
                if record.get("content_type") == "markdown" and (self.root / relative).is_file()
            ]
            for image_path in affected_images:
                try:
                    image_relative = image_path.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                by_path.pop(image_relative, None)
                if not image_path.is_file() or _is_excluded(image_relative, config["exclude"]):
                    continue
                directory = _directory_for_path(image_path, self.root, config)
                if directory is None:
                    continue
                context = None
                if directory.get("referenced_images_only"):
                    for markdown_path in markdown_paths:
                        context = _referenced_images(markdown_path, self.root).get(image_path)
                        if context:
                            break
                    if context is None:
                        continue
                by_path[image_relative] = _record(
                    image_path,
                    self.root,
                    directory["id"],
                    "image",
                    context=context,
                )

            next_records = sorted(by_path.values(), key=lambda record: str(record.get("path") or ""))
            next_catalog = {
                "version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "records": next_records,
                "missing_directories": catalog.get("missing_directories", []),
                "counts": _catalog_counts(next_records),
            }
            _write_catalog(self.base_dir, config, next_catalog)
        return self.status(catalog=next_catalog)

    def status(self, *, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
        config = self.config
        catalog = catalog or load_catalog(self.base_dir)
        records = catalog.get("records", []) if catalog else []
        directory_status = []
        for directory in config["directories"]:
            path = resolve_search_directory(self.root, directory["path"])
            directory_records = [record for record in records if record.get("directory_id") == directory["id"]]
            directory_status.append({
                **directory,
                "status": "disabled" if not directory["enabled"] else "ready" if path.is_dir() else "missing",
                "indexed_documents": sum(record.get("content_type") != "image" for record in directory_records),
                "indexed_images": sum(record.get("content_type") == "image" for record in directory_records),
            })
        return {
            "enabled": bool(config["enabled"]),
            "generated_at": catalog.get("generated_at") if catalog else None,
            "status": "ready" if catalog else "not_indexed",
            "counts": {
                "records": len(records),
                "documents": sum(record.get("content_type") != "image" for record in records),
                "images": sum(record.get("content_type") == "image" for record in records),
            },
            "directories": directory_status,
        }

    def suggestions(self, prefix: str, limit: int = 8) -> list[str]:
        needle = prefix.strip().casefold()
        if not needle:
            return []
        values = {str(record.get("title") or "").strip() for record in self._records()}
        return sorted(value for value in values if value and needle in value.casefold())[: max(1, min(limit, 20))]

    def search(
        self,
        query: str,
        *,
        category: str = "all",
        directory_ids: list[str] | None = None,
        page_types: list[str] | None = None,
        platforms: list[str] | None = None,
        file_types: list[str] | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        query = query.strip()
        if not query:
            return {"query": "", "total": 0, "took_ms": 0, "hits": [], "facets": {}}
        tokens = _tokenize(query)
        records = _deduplicate(self._records())
        allowed_categories = {"all", "wiki", "article", "image", "file"}
        category = category if category in allowed_categories else "all"
        directory_filter = set(directory_ids or [])
        page_type_filter = {value.casefold() for value in (page_types or [])}
        platform_filter = {value.casefold() for value in (platforms or [])}
        file_type_filter = {value.casefold() for value in (file_types or [])}
        eligible_records: list[dict[str, Any]] = []
        for record in records:
            if directory_filter and record.get("directory_id") not in directory_filter:
                continue
            if page_type_filter and str(record.get("page_type") or "").casefold() not in page_type_filter:
                continue
            if platform_filter and str(record.get("platform") or "").casefold() not in platform_filter:
                continue
            if file_type_filter and Path(str(record.get("path") or "")).suffix.lower().lstrip(".") not in file_type_filter:
                continue
            eligible_records.append(record)

        lexical_scored: list[tuple[float, dict[str, Any]]] = []
        for record in eligible_records:
            kind = record.get("content_type")
            context = record.get("context") if isinstance(record.get("context"), dict) else {}
            haystack = " ".join(str(record.get(key) or "") for key in ("title", "aliases", "path", "page_type", "platform", "text"))
            if kind == "image":
                haystack += " " + " ".join(str(value) for value in context.values())
            folded = haystack.casefold()
            if query.casefold() not in folded and not all(token in folded for token in tokens):
                continue
            title_folded = str(record.get("title") or "").casefold()
            path_folded = str(record.get("path") or "").casefold()
            score = 1.0
            if query.casefold() == title_folded:
                score += 12
            elif query.casefold() in title_folded:
                score += 8
            if query.casefold() in path_folded:
                score += 4
            score += sum(2 for token in tokens if token in title_folded)
            score += min(5, sum(1 for token in tokens if token in folded))
            score += _authority(record) * 0.15
            lexical_scored.append((score, record))
        lexical_scored.sort(key=lambda item: (-item[0], str(item[1].get("title") or "").casefold(), str(item[1].get("path") or "")))

        semantic = _semantic_retrieval(
            query,
            root=self.root,
            records=eligible_records,
            limit=max(limit, 20),
            include_images=category in {"all", "image"},
        )
        semantic_channels = semantic.get("channels") if isinstance(semantic.get("channels"), dict) else {}
        rankings: dict[str, list[str]] = {
            "lexical": [str(record.get("id")) for _, record in lexical_scored],
        }
        semantic_hits_by_record: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for channel, channel_hits in semantic_channels.items():
            rankings[channel] = [str(hit.get("record_id")) for hit in channel_hits]
            for hit in channel_hits:
                semantic_hits_by_record[str(hit.get("record_id"))].append((channel, hit))

        hybrid = get_rag_hybrid_config()
        weights = {
            "lexical": 0.35,
            "text_vector": float(hybrid.get("text_vector_weight") or 0.45),
            "image_vector": float(hybrid.get("image_vector_weight") or 0.35),
        }
        fused_scores = _rrf_fuse(rankings, weights)
        record_by_id = {str(record.get("id")): record for record in eligible_records}
        if semantic.get("enabled"):
            all_scored = [
                (score, record_by_id[record_id])
                for record_id, score in fused_scores.items()
                if record_id in record_by_id
            ]
        else:
            all_scored = lexical_scored
        all_scored.sort(key=lambda item: (-item[0], str(item[1].get("title") or "").casefold(), str(item[1].get("path") or "")))

        def matches_category(record: dict[str, Any]) -> bool:
            kind = record.get("content_type")
            path = str(record.get("path", ""))
            if category == "wiki":
                return path.startswith("llm-wiki/wiki/")
            if category == "article":
                return kind != "image" and not path.startswith("originals/")
            if category == "image":
                return kind == "image"
            if category == "file":
                return kind != "markdown"
            return True

        scored = [item for item in all_scored if matches_category(item[1])]
        scored.sort(key=lambda item: (-item[0], str(item[1].get("title") or "").casefold(), str(item[1].get("path") or "")))
        scored, rerank_enabled, rerank_error = _rerank_scored_records(
            query,
            scored,
            semantic_hits=semantic_hits_by_record,
            requested_limit=max(1, limit),
        )
        page = scored[max(0, offset) : max(0, offset) + max(1, min(limit, 100))]
        hits: list[dict[str, Any]] = []
        for rank, (score, record) in enumerate(page, start=max(0, offset) + 1):
            context = record.get("context") if isinstance(record.get("context"), dict) else {}
            text = str(record.get("text") or "")
            if record.get("content_type") == "image":
                text = " ".join(str(value) for value in context.values())
            record_semantic_hits = semantic_hits_by_record.get(str(record.get("id")), [])
            semantic_quote = next((str(hit.get("quote") or "") for _, hit in record_semantic_hits if hit.get("quote")), "")
            snippet, highlights = _snippet(semantic_quote or text or str(record.get("path") or ""), query)
            matched_by = []
            matched_fields = {
                "title": str(record.get("title") or ""),
                "path": str(record.get("path") or ""),
                "content": str(record.get("text") or ""),
                "image_context": " ".join(str(value) for value in context.values()),
            }
            for field, value in matched_fields.items():
                if query.casefold() in value.casefold() or any(token in value.casefold() for token in tokens):
                    matched_by.append(field)
            matched_by.extend(channel for channel, _ in record_semantic_hits if channel not in matched_by)
            semantic_raw_score = next((hit.get("raw_score") for _, hit in record_semantic_hits if hit.get("raw_score") is not None), None)
            normalized_score = round(max(0.0, 1.0 - ((rank - 1) / max(1, len(scored))) * 0.5), 4)
            hits.append({
                "id": record["id"],
                "result_type": "wiki" if str(record.get("path", "")).startswith("llm-wiki/wiki/") else "image" if record.get("content_type") == "image" else "file" if record.get("content_type") != "markdown" else "article",
                "rank": rank,
                "modality": "image" if record.get("content_type") == "image" else "text",
                "title": record.get("title") or Path(record["path"]).name,
                "uri": record["uri"],
                "display_path": record["path"],
                "quote": snippet,
                "snippet": snippet,
                "highlights": highlights,
                "score": normalized_score,
                "raw_score": semantic_raw_score if semantic_raw_score is not None else score,
                "normalized_score": normalized_score,
                "retrieval_channel": "+".join(channel for channel, _ in record_semantic_hits) or "portal_lexical",
                "matched_by": matched_by,
                "source": {
                    "title": record.get("title"),
                    "uri": record["uri"],
                    "source_type": "knowledge_image" if record.get("content_type") == "image" else "knowledge_base",
                    "quote": snippet,
                    "metadata": {"directory_id": record.get("directory_id"), "page_type": record.get("page_type"), "platform": record.get("platform"), "source_group": record.get("source_group")},
                },
                "source_group": record.get("source_group"),
                "preview": {"kind": "image" if record.get("content_type") == "image" else "markdown" if record.get("content_type") == "markdown" else "file", "heading": context.get("heading"), "line_number": context.get("line_number")},
                "image_hit": {"title": record.get("title"), "virtual_path": record["uri"], "linked_markdown_virtual_path": context.get("linked_markdown"), "context": context} if record.get("content_type") == "image" else None,
            })
        counts = Counter("wiki" if str(record.get("path", "")).startswith("llm-wiki/wiki/") else "image" if record.get("content_type") == "image" else "file" if record.get("content_type") != "markdown" else "article" for _, record in all_scored)
        text_vector_count = len(semantic_channels.get("text_vector", []))
        image_vector_count = len(semantic_channels.get("image_vector", []))
        return {
            "query": query,
            "total": len(scored),
            "took_ms": round((time.perf_counter() - started) * 1000, 2),
            "top_k": len(hits),
            "candidate_top_k": len(all_scored),
            "retrieval": {
                "text_vector": text_vector_count,
                "bm25": 0,
                "image_vector": image_vector_count,
                "selected": len(hits),
                "hybrid_enabled": bool(semantic.get("enabled")),
                "rerank_enabled": rerank_enabled,
                "errors": {**(semantic.get("errors") or {}), **({"rerank": rerank_error} if rerank_error else {})},
            },
            "fusion": {
                "method": "rrf" if semantic.get("enabled") else "lexical_score",
                "lexical_weight": weights["lexical"],
                "text_vector_weight": weights["text_vector"],
                "bm25_weight": 0.0,
                "image_vector_weight": weights["image_vector"],
                "rerank_enabled": rerank_enabled,
            },
            "hits": hits,
            "sources": [hit["source"] for hit in hits],
            "chunks": [hit["quote"] for hit in hits if hit["modality"] == "text"],
            "image_hits": [hit["image_hit"] for hit in hits if hit.get("image_hit")],
            "facets": {"categories": dict(counts), "directories": dict(Counter(str(record.get("directory_id")) for _, record in all_scored))},
        }


class KnowledgeCatalogWatcher:
    """Debounced filesystem watcher for automatic catalog maintenance."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    def start(self, base_dir: Path) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(base_dir), name="knowledge-catalog-watcher")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._stop_event = None

    async def _run(self, base_dir: Path) -> None:
        from watchfiles import awatch

        service = KnowledgePortalSearchService(base_dir)
        if not service.root.is_dir():
            logger.info("Knowledge catalog watcher skipped; root does not exist: %s", service.root)
            return
        try:
            if load_catalog(base_dir) is None:
                await asyncio.to_thread(service.refresh)
            async for changes in awatch(
                service.root,
                debounce=750,
                step=150,
                stop_event=self._stop_event,
            ):
                paths = [path for _change, path in changes]
                if paths:
                    await asyncio.to_thread(service.refresh_paths, paths)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - watcher failure must not stop backend
            logger.warning("Knowledge catalog watcher stopped unexpectedly: %s", exc)


knowledge_catalog_watcher = KnowledgeCatalogWatcher()
