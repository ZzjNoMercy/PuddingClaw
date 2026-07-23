"""Database Agent tool package."""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool

from .formatting import format_query_error
from .legacy_query_tool import DatabaseKnowledgeQueryTool
from .models import (
    DatabaseKnowledgeInput,
    DatabaseQueryResultPageInput,
    DatabaseQueryResultSourceInput,
    DatabaseQueryTraceInspectInput,
    DatabaseSchemaInspectInput,
    DatabaseSqlExecuteInput,
    DatabaseSqlGenerateInput,
    DatabaseSqlValidateInput,
    SemanticEntityLookupInput,
)
from .result_page_tool import DatabaseQueryResultPageTool
from .result_source_tool import DatabaseQueryResultSourceTool
from .schema_inspect_tool import DatabaseSchemaInspectTool
from .semantic_entity_lookup_tool import SemanticEntityLookupTool
from .sql_execute_tool import DatabaseSqlExecuteTool
from .sql_generate_tool import DatabaseSqlGenerateTool
from .sql_validate_tool import DatabaseSqlValidateTool
from .trace_inspect_tool import DatabaseQueryTraceInspectTool
from .trace_inspect_tool import extract_sql_block as _extract_sql_block


def create_database_knowledge_tool(base_dir: Path) -> list[BaseTool]:
    return [
        DatabaseKnowledgeQueryTool(base_dir=str(base_dir)),
        DatabaseSqlGenerateTool(),
        DatabaseSqlValidateTool(),
        DatabaseSqlExecuteTool(),
        DatabaseSchemaInspectTool(),
        SemanticEntityLookupTool(),
        DatabaseQueryTraceInspectTool(base_dir=str(base_dir)),
        DatabaseQueryResultPageTool(),
        DatabaseQueryResultSourceTool(),
    ]


__all__ = [
    "DatabaseKnowledgeInput",
    "DatabaseKnowledgeQueryTool",
    "DatabaseQueryResultPageInput",
    "DatabaseQueryResultPageTool",
    "DatabaseQueryResultSourceInput",
    "DatabaseQueryResultSourceTool",
    "DatabaseQueryTraceInspectInput",
    "DatabaseQueryTraceInspectTool",
    "DatabaseSchemaInspectInput",
    "DatabaseSchemaInspectTool",
    "SemanticEntityLookupInput",
    "SemanticEntityLookupTool",
    "DatabaseSqlExecuteInput",
    "DatabaseSqlExecuteTool",
    "DatabaseSqlGenerateInput",
    "DatabaseSqlGenerateTool",
    "DatabaseSqlValidateInput",
    "DatabaseSqlValidateTool",
    "_extract_sql_block",
    "create_database_knowledge_tool",
    "format_query_error",
]
