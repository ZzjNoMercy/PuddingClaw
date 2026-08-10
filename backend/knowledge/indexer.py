"""Local knowledge vector index publishing helpers."""

from __future__ import annotations

import json
import logging
import posixpath
import re
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_knowledge_multimodal_index_config
from knowledge.paths import get_knowledge_root

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VectorProgressCallback = Callable[[dict[str, Any]], None]
logger = logging.getLogger(__name__)


def _stable_node_id(*parts: object) -> str:
    raw = "::".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"puddingclaw-knowledge::{raw}"))


def _collect_markdown_files(knowledge_dir: Path) -> list[Path]:
    return sorted(path for path in knowledge_dir.rglob("*") if path.is_file() and path.suffix.lower() in {".md", ".markdown"})


def _collect_image_files(knowledge_dir: Path) -> list[Path]:
    return sorted(path for path in knowledge_dir.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)


def _write_multimodal_manifest(
    *,
    base_dir: Path,
    markdown_files: list[Path],
    image_files: list[Path],
    text_result: dict[str, Any],
    multimodal_result: dict[str, Any],
) -> str:
    manifest_path = base_dir / "storage" / "knowledge_index" / "multimodal_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_dir = get_knowledge_root(base_dir)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "knowledge_dir": str(knowledge_dir),
        "markdown_count": len(markdown_files),
        "image_count": len(image_files),
        "markdown_files": [path.relative_to(knowledge_dir).as_posix() for path in markdown_files],
        "image_files": [path.relative_to(knowledge_dir).as_posix() for path in image_files],
        "text_index": text_result,
        "multimodal_index": multimodal_result,
        "chunks": multimodal_result.get("chunks", []),
        "images": multimodal_result.get("images", []),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(manifest_path)


def refresh_local_knowledge_index(base_dir: Path, progress_callback: VectorProgressCallback | None = None) -> dict[str, Any]:
    """Backward-compatible wrapper for the multimodal-aware publisher."""

    return refresh_multimodal_knowledge_index(base_dir, progress_callback=progress_callback)


def refresh_document_knowledge_index(
    base_dir: Path,
    document_path: Path,
    progress_callback: VectorProgressCallback | None = None,
    *,
    include_linked_images: bool = True,
) -> dict[str, Any]:
    """Update one Markdown document and its linked images in Milvus."""

    knowledge_dir = get_knowledge_root(base_dir).resolve()
    markdown_path = document_path.resolve()
    if knowledge_dir not in markdown_path.parents:
        return {"refreshed": False, "reason": "document is outside the knowledge directory"}
    if not markdown_path.is_file() or markdown_path.suffix.lower() not in {".md", ".markdown"}:
        return {"refreshed": False, "reason": "document Markdown does not exist"}

    index_config = get_knowledge_multimodal_index_config()
    if not index_config.get("enabled") or str(index_config.get("vector_store") or "local").lower() != "milvus":
        return {"refreshed": False, "reason": "single-document rebuild requires the Milvus multimodal index"}

    all_images = _collect_image_files(knowledge_dir) if include_linked_images else []
    image_files = [Path(path) for path in _linked_images_for_markdown(markdown_path, all_images)]
    virtual_path = f"/knowledge/{markdown_path.relative_to(knowledge_dir).as_posix()}"
    result = _try_build_multimodal_index(
        base_dir=base_dir,
        knowledge_dir=knowledge_dir,
        markdown_files=[markdown_path],
        image_files=image_files,
        index_config=index_config,
        progress_callback=progress_callback,
        replace_document_virtual_path=virtual_path,
    )
    if not result.get("enabled"):
        return {
            "refreshed": False,
            "mode": "llamaindex_multimodal_document",
            "document_virtual_path": virtual_path,
            "multimodal": result,
            "error": result.get("error") or result.get("reason") or "document index failed",
        }
    return {
        "refreshed": True,
        "mode": "llamaindex_multimodal_document",
        "document_virtual_path": virtual_path,
        "document_count": result.get("document_count", 1 + len(image_files)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "multimodal": result,
    }


def refresh_multimodal_knowledge_index(base_dir: Path, progress_callback: VectorProgressCallback | None = None) -> dict[str, Any]:
    """Publish local knowledge artifacts through LlamaIndex.

    The default vector publishing path is unified multimodal indexing:
    Markdown text and extracted images are embedded by the same multimodal
    embedding model and, when configured, written to dual Milvus collections.
    The legacy text-only local VectorStoreIndex is only used when
    knowledge.multimodal_index.enabled=false.
    """

    knowledge_dir = get_knowledge_root(base_dir)
    storage_dir = base_dir / "storage" / "knowledge_index"
    markdown_files = _collect_markdown_files(knowledge_dir) if knowledge_dir.exists() else []
    image_files = _collect_image_files(knowledge_dir) if knowledge_dir.exists() else []
    if not knowledge_dir.exists() or not markdown_files:
        return {"refreshed": False, "reason": "knowledge directory has no markdown"}

    index_config = get_knowledge_multimodal_index_config()
    if index_config["enabled"]:
        text_result: dict[str, Any] = {
            "refreshed": False,
            "reason": "text-only fallback disabled; unified multimodal index is the vector publishing path",
        }
        multimodal_result = _try_build_multimodal_index(
            base_dir=base_dir,
            knowledge_dir=knowledge_dir,
            markdown_files=markdown_files,
            image_files=image_files,
            index_config=index_config,
            progress_callback=progress_callback,
        )
        manifest_path = _write_multimodal_manifest(
            base_dir=base_dir,
            markdown_files=markdown_files,
            image_files=image_files,
            text_result=text_result,
            multimodal_result=multimodal_result,
        )
        if not multimodal_result.get("enabled"):
            return {
                "refreshed": False,
                "mode": "llamaindex_multimodal",
                "manifest_path": manifest_path,
                "multimodal": multimodal_result,
                "error": multimodal_result.get("error") or multimodal_result.get("reason") or "multimodal index failed",
            }
        return {
            "refreshed": True,
            "mode": "llamaindex_multimodal",
            "manifest_path": manifest_path,
            "document_count": multimodal_result.get("document_count", len(markdown_files) + len(image_files)),
            "multimodal": multimodal_result,
        }

    try:
        from llama_index.core import SimpleDirectoryReader, VectorStoreIndex

        from llm.embed_client import get_embedding_model

        documents = SimpleDirectoryReader(str(knowledge_dir), recursive=True).load_data()
        if not documents:
            return {"refreshed": False, "reason": "no documents loaded"}

        if storage_dir.exists():
            shutil.rmtree(storage_dir)
        storage_dir.mkdir(parents=True, exist_ok=True)

        index = VectorStoreIndex.from_documents(documents, embed_model=get_embedding_model())
        index.storage_context.persist(persist_dir=str(storage_dir))
        text_result = {
            "refreshed": True,
            "storage_path": str(storage_dir),
            "document_count": len(documents),
        }
        multimodal_result: dict[str, Any] = {
            "enabled": False,
            "reason": "knowledge.multimodal_index.enabled=false; using legacy text-only local index",
            "image_count": len(image_files),
        }
        manifest_path = _write_multimodal_manifest(
            base_dir=base_dir,
            markdown_files=markdown_files,
            image_files=image_files,
            text_result=text_result,
            multimodal_result=multimodal_result,
        )
        return {
            **text_result,
            "mode": "llamaindex_multimodal_ready",
            "manifest_path": manifest_path,
            "multimodal": multimodal_result,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "refreshed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _try_build_multimodal_index(
    *,
    base_dir: Path,
    knowledge_dir: Path,
    markdown_files: list[Path],
    image_files: list[Path],
    index_config: dict[str, Any],
    progress_callback: VectorProgressCallback | None = None,
    replace_document_virtual_path: str | None = None,
) -> dict[str, Any]:
    multimodal_storage_dir = base_dir / "storage" / "knowledge_multimodal_index"
    try:
        from llama_index.core import StorageContext
        from llama_index.core.indices.multi_modal import MultiModalVectorStoreIndex

        from llm.embed_client import get_embedding_model
        from llm.multimodal_embedding import get_multimodal_embedding_model

        nodes, node_manifest = _build_multimodal_nodes(knowledge_dir, markdown_files, image_files)
        if not nodes:
            return {
                "enabled": False,
                "reason": "no LlamaIndex nodes built from knowledge files",
                "image_count": len(image_files),
                "storage_path": str(multimodal_storage_dir),
            }
        text_embed_model = get_embedding_model()
        image_embed_model = get_multimodal_embedding_model()
        text_total = len(node_manifest["chunks"])
        image_total = len(node_manifest["images"])
        if progress_callback:
            progress_callback({
                "stage": "nodes_built",
                "text_total": text_total,
                "text_done": 0,
                "image_total": image_total,
                "image_done": 0,
                "total": text_total + image_total,
                "done": 0,
            })
        if hasattr(image_embed_model, "set_progress_callback"):
            def _embedding_progress(modality: str, done: int, total: int) -> None:
                if not progress_callback:
                    return
                other_modality = "image" if modality == "text" else "text"
                other_done = int(getattr(image_embed_model, "_progress_done", {}).get(other_modality, 0))
                progress_callback({
                    "stage": "embedding",
                    "modality": modality,
                    f"{modality}_done": done,
                    f"{modality}_total": total,
                    f"{other_modality}_done": other_done,
                    f"{other_modality}_total": image_total if other_modality == "image" else text_total,
                    "text_done": int(getattr(image_embed_model, "_progress_done", {}).get("text", 0)),
                    "text_total": text_total,
                    "image_done": int(getattr(image_embed_model, "_progress_done", {}).get("image", 0)),
                    "image_total": image_total,
                    "done": int(getattr(image_embed_model, "_progress_done", {}).get("text", 0))
                    + int(getattr(image_embed_model, "_progress_done", {}).get("image", 0)),
                    "total": text_total + image_total,
                })

            image_embed_model.set_progress_callback(_embedding_progress, text_total=0, image_total=image_total)
        vector_store = str(index_config.get("vector_store") or "local").strip().lower()
        storage_context: StorageContext
        extra: dict[str, Any] = {"vector_store": vector_store}
        if vector_store == "milvus":
            storage_context, milvus_extra = _build_milvus_storage_context(index_config)
            extra.update(milvus_extra)
            existing_node_ids = _document_node_ids(storage_context, replace_document_virtual_path)
        else:
            existing_node_ids = {}
            if multimodal_storage_dir.exists():
                shutil.rmtree(multimodal_storage_dir)
            multimodal_storage_dir.mkdir(parents=True, exist_ok=True)
            storage_context = StorageContext.from_defaults()

        if progress_callback:
            progress_callback({
                "stage": "indexing",
                "text_done": 0,
                "text_total": text_total,
                "image_done": 0,
                "image_total": image_total,
                "done": 0,
                "total": text_total + image_total,
            })
        index = MultiModalVectorStoreIndex(
            nodes=nodes,
            storage_context=storage_context,
            embed_model=text_embed_model,
            image_embed_model=image_embed_model,
            show_progress=False,
            image_vector_store_key="image",
        )
        if replace_document_virtual_path:
            _delete_stale_document_nodes(storage_context, existing_node_ids, nodes)
        if vector_store != "milvus":
            index.storage_context.persist(persist_dir=str(multimodal_storage_dir))
        if progress_callback:
            progress_callback({
                "stage": "done",
                "text_done": text_total,
                "text_total": text_total,
                "image_done": image_total,
                "image_total": image_total,
                "done": text_total + image_total,
                "total": text_total + image_total,
            })
        return {
            "enabled": True,
            "storage_path": str(multimodal_storage_dir) if vector_store != "milvus" else None,
            "document_count": len(nodes),
            "text_count": len(node_manifest["chunks"]),
            "image_count": len(node_manifest["images"]),
            "markdown_count": len(markdown_files),
            "parser": "MarkdownNodeParser",
            "scope": "document" if replace_document_virtual_path else "knowledge_base",
            "chunks": node_manifest["chunks"],
            "images": node_manifest["images"],
            "text_embed_model": getattr(text_embed_model, "model_name", None) or getattr(text_embed_model, "model", None),
            "image_embed_model": image_embed_model.model_name,
            **extra,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "enabled": False,
            "error": f"{type(exc).__name__}: {exc}",
            "image_count": len(image_files),
            "storage_path": str(multimodal_storage_dir),
        }


def _normalize_asset_ref(value: str) -> str:
    normalized = (value or "").replace("\\", "/").strip().strip("'\"").lstrip("/")
    if not normalized:
        return ""
    if re.match(r"^(?:https?:|data:)", normalized, flags=re.IGNORECASE):
        return normalized
    if normalized.startswith("/knowledge/"):
        return normalized
    return posixpath.normpath(normalized).lstrip("./")


def _plain_context_line(line: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line or "")
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _nearby_lines(lines: list[str], start: int, step: int, limit: int = 2) -> list[str]:
    collected: list[str] = []
    index = start
    while 0 <= index < len(lines) and len(collected) < limit:
        text = _plain_context_line(lines[index])
        if text:
            collected.append(text)
        index += step
    if step < 0:
        collected.reverse()
    return collected


def _resolve_markdown_asset_path(knowledge_dir: Path, markdown_path: Path, url: str) -> Path | None:
    normalized = _normalize_asset_ref(url)
    if not normalized or re.match(r"^(?:https?:|data:)", normalized, flags=re.IGNORECASE):
        return None
    if normalized.startswith("/knowledge/"):
        return (knowledge_dir / normalized.removeprefix("/knowledge/")).resolve()
    return (markdown_path.parent / normalized).resolve()


def _extract_markdown_image_contexts(markdown_path: Path, knowledge_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        markdown = markdown_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    contexts: dict[str, dict[str, Any]] = {}
    lines = markdown.splitlines()
    current_heading = ""
    markdown_image_re = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
    html_image_re = re.compile(r"<img\b[^>]*?\bsrc=[\"'](?P<url>.*?)[\"'][^>]*>", flags=re.IGNORECASE)

    for index, line in enumerate(lines):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading:
            current_heading = _plain_context_line(heading.group(1))

        matches: list[tuple[str, str]] = []
        matches.extend((match.group("url"), match.group("alt").strip()) for match in markdown_image_re.finditer(line))
        matches.extend((match.group("url"), "") for match in html_image_re.finditer(line))
        if not matches:
            continue

        before = _nearby_lines(lines, index - 1, -1)
        after = _nearby_lines(lines, index + 1, 1)
        for url, alt in matches:
            resolved = _resolve_markdown_asset_path(knowledge_dir, markdown_path, url)
            if resolved is None:
                continue
            caption = alt or ""
            snippet = " / ".join(part for part in [current_heading, *before, caption, *after] if part).strip()
            context = {
                "heading": current_heading,
                "caption": caption,
                "before": before,
                "after": after,
                "line_number": index + 1,
                "snippet": snippet[:800],
                "linked_markdown": str(markdown_path),
                "linked_markdown_virtual_path": f"/knowledge/{markdown_path.relative_to(knowledge_dir).as_posix()}",
            }
            contexts[str(resolved)] = context
            contexts[resolved.name] = context

    return contexts


def _all_image_contexts(knowledge_dir: Path, markdown_files: list[Path]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for markdown_path in markdown_files:
        contexts.update(_extract_markdown_image_contexts(markdown_path, knowledge_dir))
    return contexts


def _title_from_node_text(text: str) -> str:
    for line in (text or "").splitlines():
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if match:
            return match.group(2).strip()
    return _plain_context_line((text or "").splitlines()[0] if text else "")[:80] or "文档片段"


def _level_from_node_text(text: str) -> str:
    for line in (text or "").splitlines():
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if match:
            return f"H{len(match.group(1))}"
    return "正文"


def _node_preview(text: str, limit: int = 260) -> str:
    preview = re.sub(r"\s+", " ", text or "").strip()
    return preview[:limit]


def _build_multimodal_nodes(
    knowledge_dir: Path,
    markdown_files: list[Path],
    image_files: list[Path],
) -> tuple[list[Any], dict[str, list[dict[str, Any]]]]:
    from llama_index.core import Document
    from llama_index.core.node_parser import MarkdownNodeParser
    from llama_index.core.schema import ImageNode

    nodes: list[Any] = []
    chunks: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    parser = MarkdownNodeParser()
    image_contexts = _all_image_contexts(knowledge_dir, markdown_files)
    for path in markdown_files:
        rel = path.relative_to(knowledge_dir).as_posix()
        linked_images = _linked_images_for_markdown(path, image_files)
        markdown_text = path.read_text(encoding="utf-8", errors="replace")
        document_nodes = parser.get_nodes_from_documents([
            Document(
                id_=_stable_node_id("document", rel),
                text=markdown_text,
                metadata={
                    "file_path": str(path),
                    "virtual_path": f"/knowledge/{rel}",
                    "modality": "text",
                    "parser": "MarkdownNodeParser",
                    "linked_images": linked_images,
                },
            )
        ])
        for index, node in enumerate(document_nodes, start=1):
            node.id_ = _stable_node_id("chunk", rel, index)
            text = node.get_content(metadata_mode="none")
            title = _title_from_node_text(text)
            level = _level_from_node_text(text)
            node.metadata.update({
                "file_path": str(path),
                "virtual_path": f"/knowledge/{rel}",
                "modality": "text",
                "parser": "MarkdownNodeParser",
                "chunk_index": index,
                "chunk_title": title,
                "chunk_level": level,
                "linked_images": _linked_images_for_markdown_text(text, linked_images),
            })
            nodes.append(node)
            chunks.append({
                "index": len(chunks) + 1,
                "node_id": node.node_id,
                "title": title,
                "level": level,
                "preview": _node_preview(text),
                "header_path": node.metadata.get("header_path"),
                "file_path": str(path),
                "virtual_path": f"/knowledge/{rel}",
                "linked_images": node.metadata.get("linked_images") or [],
            })
    for path in image_files:
        rel = path.relative_to(knowledge_dir).as_posix()
        context = image_contexts.get(str(path.resolve())) or image_contexts.get(path.name) or {}
        caption = str(context.get("caption") or context.get("snippet") or path.name)
        linked_markdown = context.get("linked_markdown") or _nearest_markdown(path, markdown_files)
        linked_markdown_virtual_path = context.get("linked_markdown_virtual_path")
        if not linked_markdown_virtual_path and linked_markdown:
            try:
                linked_markdown_virtual_path = f"/knowledge/{Path(str(linked_markdown)).relative_to(knowledge_dir).as_posix()}"
            except ValueError:
                linked_markdown_virtual_path = None
        image_doc = ImageNode(
            id_=_stable_node_id("image", rel),
            text="",
            image_path=str(path),
            metadata={
                "file_path": str(path),
                "virtual_path": f"/knowledge/{rel}",
                "modality": "image",
                "caption": caption,
                "linked_markdown": linked_markdown,
                "linked_markdown_virtual_path": linked_markdown_virtual_path,
                "context": context,
            },
        )
        nodes.append(image_doc)
        images.append({
            "index": len(images) + 1,
            "node_id": image_doc.node_id,
            "title": path.name,
            "file_path": str(path),
            "virtual_path": f"/knowledge/{rel}",
            "linked_markdown": linked_markdown,
            "linked_markdown_virtual_path": linked_markdown_virtual_path,
            "context": context,
        })
    return nodes, {"chunks": chunks, "images": images}


def build_markdown_chunk_manifest(
    knowledge_dir: Path,
    markdown_files: list[Path],
    image_files: list[Path] | None = None,
) -> dict[str, Any]:
    """Build LlamaIndex Markdown chunks without embedding or vector publishing."""

    from llama_index.core import Document
    from llama_index.core.node_parser import MarkdownNodeParser

    image_files = image_files or []
    chunks: list[dict[str, Any]] = []
    parser = MarkdownNodeParser()
    for path in markdown_files:
        rel = path.relative_to(knowledge_dir).as_posix()
        linked_images = _linked_images_for_markdown(path, image_files)
        markdown_text = path.read_text(encoding="utf-8", errors="replace")
        document_nodes = parser.get_nodes_from_documents([
            Document(
                text=markdown_text,
                metadata={
                    "file_path": str(path),
                    "virtual_path": f"/knowledge/{rel}",
                    "modality": "text",
                    "parser": "MarkdownNodeParser",
                    "linked_images": linked_images,
                },
            )
        ])
        for index, node in enumerate(document_nodes, start=1):
            text = node.get_content(metadata_mode="none")
            title = _title_from_node_text(text)
            level = _level_from_node_text(text)
            chunks.append({
                "index": len(chunks) + 1,
                "node_id": node.node_id,
                "title": title,
                "level": level,
                "preview": _node_preview(text),
                "header_path": node.metadata.get("header_path"),
                "file_path": str(path),
                "virtual_path": f"/knowledge/{rel}",
                "linked_images": _linked_images_for_markdown_text(text, linked_images),
            })

    return {
        "parser": "MarkdownNodeParser",
        "chunk_count": len(chunks),
        "chunks": chunks,
    }


def _linked_images_for_markdown_text(text: str, linked_images: list[str]) -> list[str]:
    if not linked_images:
        return []
    matched = [path for path in linked_images if Path(path).name in text or path in text]
    return matched[:20]


def _linked_images_for_markdown(markdown_path: Path, image_files: list[Path]) -> list[str]:
    try:
        text = markdown_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    directly_linked = [
        image_path
        for image_path in image_files
        if image_path.name in text or str(image_path) in text
    ]
    if not directly_linked:
        return []

    # MinerU keeps every image extracted from one PDF in a dedicated directory,
    # while its Markdown may omit decorative or low-confidence images. Once a
    # document links that directory, rebuild the complete PDF image set.
    linked_directories = {image_path.parent.resolve() for image_path in directly_linked}
    return [
        str(image_path)
        for image_path in image_files
        if image_path.parent.resolve() in linked_directories
    ]


def _nearest_markdown(image_path: Path, markdown_files: list[Path]) -> str | None:
    if not markdown_files:
        return None
    same_day = [path for path in markdown_files if len(path.parts) >= 2 and path.parent.name in image_path.parts]
    return str((same_day or markdown_files)[0])


def _build_milvus_storage_context(index_config: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    from llama_index.core import StorageContext
    from llama_index.vector_stores.milvus import MilvusVectorStore
    from llama_index.vector_stores.milvus.utils import BM25BuiltInFunction

    uri = index_config.get("milvus_uri", "http://localhost:19530")
    from config import get_fallback_embedding_config, get_multimodal_embedding_config

    text_dim = int(get_fallback_embedding_config().get("dimension", 1536))
    image_dim = int(get_multimodal_embedding_config().get("dimension", 1024))
    text_collection = index_config.get("text_collection", "puddingclaw_knowledge_text")
    image_collection = index_config.get("image_collection", "puddingclaw_knowledge_image")
    bm25_enabled = bool(index_config.get("bm25_enabled", True))
    sparse_embedding_function = None
    if bm25_enabled:
        sparse_embedding_function = BM25BuiltInFunction(
            input_field_names="text",
            output_field_names="sparse_embedding",
            function_name="puddingclaw_knowledge_bm25",
            analyzer_params={
                "tokenizer": "jieba",
                "filter": ["lowercase"],
            },
        )

    text_store = MilvusVectorStore(
        uri=uri,
        collection_name=text_collection,
        dim=text_dim,
        overwrite=False,
        upsert_mode=True,
        text_key="text",
        enable_sparse=bm25_enabled,
        sparse_embedding_field="sparse_embedding",
        sparse_embedding_function=sparse_embedding_function,
    )
    image_store = MilvusVectorStore(
        uri=uri,
        collection_name=image_collection,
        dim=image_dim,
        overwrite=False,
        upsert_mode=True,
        text_key="text",
        enable_sparse=False,
    )
    _delete_misrouted_image_rows_from_text_store(text_store)
    storage_context = StorageContext.from_defaults(vector_store=text_store)
    storage_context.vector_stores["image"] = image_store
    return storage_context, {
        "milvus_uri": uri,
        "text_collection": text_collection,
        "image_collection": image_collection,
        "text_dimension": text_dim,
        "image_dimension": image_dim,
        "bm25_enabled": bm25_enabled,
        "bm25_analyzer": "jieba",
        "sparse_embedding_field": "sparse_embedding" if bm25_enabled else None,
        "overwrite": False,
        "upsert": True,
    }


def _delete_misrouted_image_rows_from_text_store(text_store: Any) -> None:
    """Remove historical image nodes accidentally inserted into text store."""

    try:
        text_store.client.delete(
            collection_name=text_store.collection_name,
            filter='modality == "image"',
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Skip cleaning misrouted image rows from text collection: %s", exc)


def _document_node_ids(storage_context: Any, virtual_path: str | None) -> dict[str, set[str]]:
    if not virtual_path:
        return {}
    filters = {
        "text": f"virtual_path == {json.dumps(virtual_path, ensure_ascii=False)}",
        "image": f"linked_markdown_virtual_path == {json.dumps(virtual_path, ensure_ascii=False)}",
    }
    result: dict[str, set[str]] = {}
    for store_key, filter_expression in filters.items():
        store = storage_context.vector_stores.get("default" if store_key == "text" else store_key)
        if store is None:
            continue
        try:
            rows = store.client.query(
                collection_name=store.collection_name,
                filter=filter_expression,
                output_fields=["id"],
                limit=16_384,
            )
            result[store_key] = {str(row["id"]) for row in rows if row.get("id")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unable to inspect existing %s nodes for %s: %s", store_key, virtual_path, exc)
    return result


def _delete_stale_document_nodes(
    storage_context: Any,
    existing_node_ids: dict[str, set[str]],
    nodes: list[Any],
) -> None:
    current_ids: dict[str, set[str]] = {"text": set(), "image": set()}
    for node in nodes:
        modality = str((node.metadata or {}).get("modality") or "text")
        current_ids["image" if modality == "image" else "text"].add(str(node.node_id))

    for store_key, old_ids in existing_node_ids.items():
        stale_ids = sorted(old_ids - current_ids.get(store_key, set()))
        store = storage_context.vector_stores.get("default" if store_key == "text" else store_key)
        if store is None:
            continue
        for offset in range(0, len(stale_ids), 100):
            batch = stale_ids[offset : offset + 100]
            if not batch:
                continue
            try:
                store.client.delete(
                    collection_name=store.collection_name,
                    filter=f"id in {json.dumps(batch)}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Unable to delete stale %s nodes: %s", store_key, exc)


def reset_multimodal_collections(index_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drop the configured Milvus text/image collections immediately.

    This is intentionally separate from ingestion. Normal imports must never
    rebuild collections implicitly; reset is a user-triggered maintenance
    action from Settings.
    """

    from pymilvus import MilvusClient

    config = index_config or get_knowledge_multimodal_index_config()
    if config.get("vector_store") != "milvus":
        raise ValueError("Only Milvus vector_store supports collection reset")

    uri = config.get("milvus_uri", "http://localhost:19530")
    collections = [
        config.get("text_collection", "puddingclaw_knowledge_text"),
        config.get("image_collection", "puddingclaw_knowledge_image"),
    ]
    client = MilvusClient(uri=uri, timeout=10.0)
    dropped: list[str] = []
    missing: list[str] = []
    for collection in collections:
        if client.has_collection(collection):
            client.drop_collection(collection)
            dropped.append(collection)
        else:
            missing.append(collection)
    return {
        "ok": True,
        "milvus_uri": uri,
        "dropped": dropped,
        "missing": missing,
    }
