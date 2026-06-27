"""SearchKnowledgeBaseTool — LlamaIndex hybrid search (BM25 + Vector)."""

from pathlib import Path
from typing import Type, Optional

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class SearchKnowledgeInput(BaseModel):
    query: str = Field(description="The search query to find relevant knowledge")


class SearchKnowledgeBaseTool(BaseTool):
    name: str = "search_knowledge_base"
    description: str = (
        "Search the local knowledge base using hybrid retrieval (keyword + semantic). "
        "Use this when the user asks about specific knowledge or documents. "
        "Returns the most relevant passages from the knowledge base."
    )
    args_schema: Type[BaseModel] = SearchKnowledgeInput
    risk_level: str = "safe"
    base_dir: str = ""
    _index: Optional[object] = None

    class Config:
        arbitrary_types_allowed = True

    def _build_index(self):
        """Build or load LlamaIndex index from backend/knowledge/ directory."""
        knowledge_dir = Path(self.base_dir) / "backend" / "knowledge"
        storage_dir = Path(self.base_dir) / "backend" / "storage"

        if not knowledge_dir.exists() or not any(knowledge_dir.iterdir()):
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

            # Try loading persisted index
            if storage_dir.exists() and any(storage_dir.iterdir()):
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
                return None

            index = VectorStoreIndex.from_documents(documents, embed_model=embed_model)
            storage_dir.mkdir(parents=True, exist_ok=True)
            index.storage_context.persist(persist_dir=str(storage_dir))
            return index

        except ImportError as e:
            print(f"⚠️ LlamaIndex not fully installed: {e}")
            return None
        except Exception as e:
            print(f"⚠️ Index build error: {e}")
            return None

    def _run(self, query: str) -> str:
        if self._index is None:
            self._index = self._build_index()

        if self._index is None:
            kb_dir = Path(self.base_dir) / "backend" / "knowledge"
            return f"📭 Knowledge base is empty. Add Markdown documents to /knowledge/ (physical path: {kb_dir}/) to enable search."

        try:
            query_engine = self._index.as_query_engine(similarity_top_k=3)
            response = query_engine.query(query)
            result = str(response)
            if len(result) > 5000:
                result = result[:5000] + "\n...[truncated]"
            from graph.citations import encode_tool_result, normalize_source

            sources = []
            for index, item in enumerate(getattr(response, "source_nodes", []) or []):
                node = getattr(item, "node", item)
                metadata = dict(getattr(node, "metadata", {}) or {})
                quote = ""
                try:
                    quote = node.get_content()
                except Exception:
                    quote = getattr(node, "text", "") or ""
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
            return encode_tool_result(result, sources)
        except Exception as e:
            return f"❌ Search error: {str(e)}"


def create_search_knowledge_tool(base_dir: Path) -> SearchKnowledgeBaseTool:
    return SearchKnowledgeBaseTool(base_dir=str(base_dir))
