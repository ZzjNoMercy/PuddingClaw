"""Runtime entrypoints for PuddingClaw's Vanna-backed NL2SQL capability.

This module intentionally wraps the migrated Vanna fork instead of exposing the
standalone NL2SQL FastAPI service from the reference project. PuddingClaw owns
configuration, database source selection, tool execution, and SQL safety checks.
The Vanna client is only the SQL-generation/training runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import get_vanna_config

from .improve.clients.vanna_client import MyVanna, create_vanna_client


EmbeddingProvider = Literal["jina", "qwen", "bge"]


@dataclass(slots=True)
class VannaRuntimeConfig:
    """Configuration required to create a Vanna client.

    Values should be resolved by PuddingClaw from Settings/config.json,
    database-source catalog, and existing Milvus / LLM gateway configuration.
    The runtime does not read `.env` on its own.
    """

    openai_api_key: str
    openai_base_url: str
    model: str
    milvus_uri: str
    embedding_api_url: str
    embedding_provider: EmbeddingProvider = "qwen"
    embedding_api_key: str | None = None
    embedding_model_name: str | None = None
    embedding_batch_size: int = 20
    metric_type: str = "COSINE"
    sql_collection: str = "puddingclaw_vanna_sql"
    ddl_collection: str = "puddingclaw_vanna_ddl"
    doc_collection: str = "puddingclaw_vanna_doc"
    entity_collection: str = "puddingclaw_vanna_entity"
    temperature: float = 0.2
    max_tokens: int = 14000
    dialect: str = "PostgreSQL"
    language: str = "zh-CN"


def build_vanna_client(config: VannaRuntimeConfig) -> MyVanna:
    """Build the migrated Vanna client from resolved PuddingClaw config."""

    return create_vanna_client(
        openai_api_key=config.openai_api_key,
        openai_base_url=config.openai_base_url,
        model=config.model,
        milvus_uri=config.milvus_uri,
        embedding_api_url=config.embedding_api_url,
        embedding_provider=config.embedding_provider,
        embedding_api_key=config.embedding_api_key,
        embedding_model_name=config.embedding_model_name,
        embedding_batch_size=config.embedding_batch_size,
        metric_type=config.metric_type,
        sql_collection=config.sql_collection,
        ddl_collection=config.ddl_collection,
        doc_collection=config.doc_collection,
        entity_collection=config.entity_collection,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        dialect=config.dialect,
        language=config.language,
    )


def build_vanna_client_from_app_config() -> MyVanna:
    """Build Vanna from PuddingClaw's global config.json settings."""

    raw = get_vanna_config()
    llm = raw.get("llm", {})
    embedding = raw.get("embedding", {})
    milvus = raw.get("milvus", {})
    return build_vanna_client(
        VannaRuntimeConfig(
            openai_api_key=str(llm.get("api_key") or ""),
            openai_base_url=str(llm.get("base_url") or ""),
            model=str(llm.get("model") or ""),
            milvus_uri=str(milvus.get("uri") or ""),
            embedding_api_url=str(embedding.get("base_url") or ""),
            embedding_provider=str(embedding.get("provider") or "qwen"),  # type: ignore[arg-type]
            embedding_api_key=str(embedding.get("api_key") or ""),
            embedding_model_name=str(embedding.get("model") or ""),
            embedding_batch_size=int(embedding.get("batch_size") or 20),
            metric_type=str(milvus.get("metric_type") or "COSINE"),
            sql_collection=str(milvus.get("sql_collection") or "puddingclaw_vanna_sql"),
            ddl_collection=str(milvus.get("ddl_collection") or "puddingclaw_vanna_ddl"),
            doc_collection=str(milvus.get("doc_collection") or "puddingclaw_vanna_doc"),
            entity_collection=str(milvus.get("entity_collection") or "puddingclaw_vanna_entity"),
            temperature=float(llm.get("temperature", 0.2)),
            max_tokens=int(llm.get("max_tokens", 14000)),
            dialect=str(raw.get("default_dialect") or "PostgreSQL"),
        )
    )
