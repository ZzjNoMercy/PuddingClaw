"""Compatibility entrypoint for database Agent tools.

The implementation lives in `tools.database.*`; this module keeps historical
imports stable for tool discovery and tests.
"""

from __future__ import annotations

from tools.database import (
    DatabaseKnowledgeInput,
    DatabaseEvidenceSearchInput,
    DatabaseEvidenceSearchTool,
    DatabaseKnowledgeQueryTool,
    DatabaseQueryResultPageInput,
    DatabaseQueryResultPageTool,
    DatabaseQueryResultSourceInput,
    DatabaseQueryResultSourceTool,
    DatabaseQueryTraceInspectInput,
    DatabaseQueryTraceInspectTool,
    DatabaseSchemaInspectInput,
    DatabaseSchemaInspectTool,
    DatabaseSqlExecuteInput,
    DatabaseSqlExecuteTool,
    DatabaseSqlGenerateInput,
    DatabaseSqlGenerateTool,
    LegacyDatabaseSqlValidateInput,
    LegacyDatabaseSqlValidateTool,
    DatabaseSqlValidateInput,
    DatabaseSqlValidateTool,
    _extract_sql_block,
    create_database_knowledge_tool,
    format_query_error as _format_query_error,
)

__all__ = [
    "DatabaseKnowledgeInput",
    "DatabaseEvidenceSearchInput",
    "DatabaseEvidenceSearchTool",
    "DatabaseKnowledgeQueryTool",
    "DatabaseQueryResultPageInput",
    "DatabaseQueryResultPageTool",
    "DatabaseQueryResultSourceInput",
    "DatabaseQueryResultSourceTool",
    "DatabaseQueryTraceInspectInput",
    "DatabaseQueryTraceInspectTool",
    "DatabaseSchemaInspectInput",
    "DatabaseSchemaInspectTool",
    "DatabaseSqlExecuteInput",
    "DatabaseSqlExecuteTool",
    "DatabaseSqlGenerateInput",
    "DatabaseSqlGenerateTool",
    "LegacyDatabaseSqlValidateInput",
    "LegacyDatabaseSqlValidateTool",
    "DatabaseSqlValidateInput",
    "DatabaseSqlValidateTool",
    "_extract_sql_block",
    "_format_query_error",
    "create_database_knowledge_tool",
]
