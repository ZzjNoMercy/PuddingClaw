"""LlamaIndex knowledge query tools."""

import json
from pathlib import Path
from typing import Any, Type, Optional

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from config import get_knowledge_multimodal_index_config
from knowledge.paths import get_knowledge_root


class LlamaIndexKnowledgeInput(BaseModel):
    query: str = Field(description="The question or retrieval query for the local LlamaIndex knowledge index.")


class LlamaIndexKnowledgeQueryTool(BaseTool):
    name: str = "llamaindex_knowledge_query"
    description: str = (
        "Query the local knowledge base through the project's LlamaIndex retrieval layer. "
        "Use this for RAG over uploaded PDFs, imported Markdown, and other indexed knowledge artifacts. "
        "For exact file-level Markdown lookup, use the built-in glob/grep tools under /knowledge/."
    )
    args_schema: Type[BaseModel] = LlamaIndexKnowledgeInput
    risk_level: str = "safe"
    base_dir: str = ""
    _index: Optional[object] = None
    _index_error: Optional[str] = None
    _knowledge_signature: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True

    def _compute_knowledge_signature(self) -> str:
        """Return a cheap signature for local knowledge files.

        The tool instance is cached by the tools registry. Without this check,
        Markdown files imported through the knowledge API would not be visible
        until the backend process restarts.
        """

        knowledge_dir = get_knowledge_root(Path(self.base_dir))
        if not knowledge_dir.exists():
            return ""

        parts: list[str] = []
        for path in sorted(knowledge_dir.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(knowledge_dir)
            parts.append(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}")
        return "|".join(parts)

    def _build_index(self, *, force_rebuild: bool = False):
        """Build or load LlamaIndex index from knowledge/ directory."""
        knowledge_dir = get_knowledge_root(Path(self.base_dir))
        storage_dir = Path(self.base_dir) / "storage" / "knowledge_index"

        if not knowledge_dir.exists() or not any(knowledge_dir.iterdir()):
            self._index_error = None
            return None

        try:
            from llama_index.core import (
                SimpleDirectoryReader,
                StorageContext,
                VectorStoreIndex,
                load_index_from_storage,
            )
            from llm.embed_client import get_embedding_model

            embed_model = get_embedding_model()

            # Try loading persisted index unless the local knowledge signature
            # changed. When files change, the persisted index is stale and must
            # be rebuilt from the Markdown artifacts.
            if not force_rebuild and storage_dir.exists() and any(storage_dir.iterdir()):
                try:
                    storage_context = StorageContext.from_defaults(
                        persist_dir=str(storage_dir)
                    )
                    return load_index_from_storage(storage_context, embed_model=embed_model)
                except Exception:
                    pass

            # Build fresh index
            documents = SimpleDirectoryReader(
                str(knowledge_dir), recursive=True
            ).load_data()

            if not documents:
                self._index_error = None
                return None

            index = VectorStoreIndex.from_documents(documents, embed_model=embed_model)
            storage_dir.mkdir(parents=True, exist_ok=True)
            index.storage_context.persist(persist_dir=str(storage_dir))
            self._index_error = None
            return index

        except ImportError as e:
            self._index_error = f"LlamaIndex not fully installed: {e}"
            print(f"⚠️ {self._index_error}")
            return None
        except Exception as e:
            self._index_error = f"Index build error: {e}"
            print(f"⚠️ {self._index_error}")
            return None

    @staticmethod
    def _entity_payload(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        entity = result.get("entity") if isinstance(result.get("entity"), dict) else result
        metadata: dict[str, Any] = {}
        text = str(entity.get("text") or "")
        node_content_raw = entity.get("_node_content") or entity.get("node_content")
        if isinstance(node_content_raw, str) and node_content_raw.strip().startswith("{"):
            try:
                node_content = json.loads(node_content_raw)
                if isinstance(node_content, dict):
                    metadata.update(node_content.get("metadata") if isinstance(node_content.get("metadata"), dict) else {})
                    text = text or str(
                        node_content.get("text")
                        or (node_content.get("text_resource") or {}).get("text")
                        or ""
                    )
                    image_resource = node_content.get("image_resource") if isinstance(node_content.get("image_resource"), dict) else {}
                    if image_resource:
                        metadata.setdefault("file_path", image_resource.get("path") or image_resource.get("image_path"))
            except json.JSONDecodeError:
                pass
        inline_metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        metadata.update(inline_metadata)
        for key, value in entity.items():
            if key.startswith("_") or key in {"entity", "vector", "embedding", "text", "metadata"}:
                continue
            if isinstance(value, (str, int, float, bool, type(None))):
                metadata.setdefault(key, value)
        return {"text": text, "metadata": metadata}, entity

    @staticmethod
    def _score(result: dict[str, Any]) -> float | None:
        value = result.get("distance", result.get("score"))
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _query_milvus_multimodal(self, query: str, *, top_k: int = 3) -> str | None:
        config = get_knowledge_multimodal_index_config()
        if not config.get("enabled") or str(config.get("vector_store") or "").lower() != "milvus":
            return None

        from graph.citations import encode_tool_result, normalize_source
        from llm.multimodal_embedding import get_multimodal_embedding_model
        from pymilvus import MilvusClient

        knowledge_dir = get_knowledge_root(Path(self.base_dir))
        embed_model = get_multimodal_embedding_model()
        query_embedding = embed_model.get_query_embedding(query)
        client = MilvusClient(uri=config.get("milvus_uri", "http://localhost:19530"), timeout=10.0)

        collections = [
            ("text", config.get("text_collection", "puddingclaw_knowledge_text")),
            ("image", config.get("image_collection", "puddingclaw_knowledge_image")),
        ]
        chunks: list[str] = []
        sources = []
        image_hits: list[dict[str, Any]] = []

        for modality, collection in collections:
            if not collection or not client.has_collection(collection):
                continue
            results = client.search(
                collection_name=collection,
                data=[query_embedding],
                limit=top_k,
                output_fields=["*"],
            )
            for index, result in enumerate(results[0] if results else []):
                payload, entity = self._entity_payload(result)
                metadata = dict(payload.get("metadata") or {})
                file_path = str(metadata.get("file_path") or metadata.get("path") or "")
                virtual_path = str(metadata.get("virtual_path") or "")
                text = str(payload.get("text") or "")
                title = Path(file_path).name if file_path else (virtual_path or f"{modality} result {index + 1}")
                score = self._score(result)

                if modality == "image":
                    if not file_path and virtual_path.startswith("/knowledge/"):
                        file_path = str((knowledge_dir / virtual_path.removeprefix("/knowledge/")).resolve())
                    if file_path:
                        image_hits.append({
                            "title": title,
                            "file_path": file_path,
                            "virtual_path": virtual_path,
                            "score": score,
                            "linked_markdown": metadata.get("linked_markdown"),
                        })
                    quote = f"图片命中：{title}\n路径：{file_path or virtual_path}"
                else:
                    quote = text.strip() or f"文本命中：{title}"
                    if quote:
                        chunks.append(quote)

                sources.append(normalize_source({
                    "title": title,
                    "uri": virtual_path or file_path,
                    "document_id": metadata.get("document_id") or metadata.get("doc_id") or file_path or virtual_path,
                    "chunk_id": metadata.get("chunk_id") or entity.get("id") or f"{modality}-{index}",
                    "source_type": "knowledge_image" if modality == "image" else "knowledge_base",
                    "quote": quote,
                    "score": score,
                    "metadata": {
                        "modality": modality,
                        "file_path": file_path,
                        "virtual_path": virtual_path,
                        "linked_markdown": metadata.get("linked_markdown"),
                    },
                }))

        if not chunks and not image_hits:
            return None

        sections: list[str] = []
        if chunks:
            sections.append("[文本命中]\n" + "\n\n---\n\n".join(chunks[:top_k]))
        if image_hits:
            lines = []
            for hit in image_hits[:top_k]:
                lines.append(
                    f"- {hit['title']}\n"
                    f"  图片路径：{hit['file_path']}\n"
                    f"  虚拟路径：{hit.get('virtual_path') or '无'}"
                )
            sections.append(
                "[图片命中]\n"
                + "\n".join(lines)
                + "\n\n如果这些图片和用户问题相关，下一步请先调用 read_resource(resource=图片路径)，"
                  "然后调用 native task 工具并设置 subagent_type=image_analyzer，让图片子 Agent 分析图片内容。"
            )
        result = "\n\n".join(sections)
        if len(result) > 6000:
            result = result[:6000] + "\n...[truncated]"
        return encode_tool_result(result, sources)

    def _run(self, query: str) -> str:
        try:
            multimodal_result = self._query_milvus_multimodal(query)
            if multimodal_result:
                return multimodal_result
        except Exception as exc:
            print(f"⚠️ Multimodal Milvus query failed, falling back to text index: {exc}")

        signature = self._compute_knowledge_signature()
        if self._index is None or signature != self._knowledge_signature:
            self._index = self._build_index(force_rebuild=signature != self._knowledge_signature)
            self._knowledge_signature = signature

        if self._index is None:
            kb_dir = get_knowledge_root(Path(self.base_dir))
            if self._index_error:
                return f"📭 Knowledge base index failed to build: {self._index_error}"
            return f"📭 Knowledge base is empty. Add Markdown documents to /knowledge/ (physical path: {kb_dir}/) to enable search."

        try:
            from graph.citations import encode_tool_result, normalize_source

            # Use retriever directly so we don't depend on llama-index-llms-openai
            # for response synthesis. The agent LLM can synthesize the raw chunks.
            retriever = self._index.as_retriever(similarity_top_k=3)
            retrieved_nodes = retriever.retrieve(query)

            chunks = []
            sources = []
            for index, item in enumerate(retrieved_nodes):
                node = getattr(item, "node", item)
                metadata = dict(getattr(node, "metadata", {}) or {})
                quote = ""
                try:
                    quote = node.get_content()
                except Exception:
                    quote = getattr(node, "text", "") or ""
                if quote:
                    chunks.append(quote)
                file_name = metadata.get("file_name") or metadata.get("filename")
                file_path = metadata.get("file_path") or metadata.get("source") or ""
                page = metadata.get("page_label") or metadata.get("page")
                document_id = getattr(node, "ref_doc_id", None) or metadata.get("document_id") or file_path
                chunk_id = getattr(node, "node_id", None) or metadata.get("chunk_id") or str(index)
                score = getattr(item, "score", None)
                sources.append(normalize_source({
                    "title": file_name or (Path(file_path).name if file_path else f"知识库来源 {index + 1}"),
                    "uri": file_path,
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "source_type": "knowledge_base",
                    "page": page,
                    "quote": quote,
                    "score": score,
                    "metadata": {
                        key: value for key, value in metadata.items()
                        if key not in {"file_path"} and isinstance(value, (str, int, float, bool, type(None)))
                    },
                }))

            result = "\n\n---\n\n".join(chunks) if chunks else "未找到相关内容。"
            if len(result) > 5000:
                result = result[:5000] + "\n...[truncated]"
            return encode_tool_result(result, sources)
        except Exception as e:
            return f"❌ Search error: {str(e)}"


def create_search_knowledge_tool(base_dir: Path) -> list[BaseTool]:
    return [LlamaIndexKnowledgeQueryTool(base_dir=str(base_dir))]
