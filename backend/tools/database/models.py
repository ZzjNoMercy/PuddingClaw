"""Input models for database Agent tools."""

from __future__ import annotations

from langchain.tools import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field


class DatabaseKnowledgeInput(BaseModel):
    question: str = Field(
        description=(
            "Natural-language business question about configured PostgreSQL database tables. "
            "For business analytics questions, pass the user's original question directly; "
            "do not first ask this tool to list tables, inspect schemas, enumerate brands, or discover columns. "
            "The tool routes tables, loads DDL/docs/entities, and generates SQL internally."
        )
    )
    database_source_id: str | None = Field(
        default=None,
        description="Optional configured database source id. If omitted, the router picks from configured sources.",
    )
    table_names: list[str] = Field(
        default_factory=list,
        description="Optional table names such as ['vehicle_params'] or ['public.vehicle_params']. Explicit table names win.",
    )
    model_id: str | None = Field(
        default=None,
        description="Optional analytics data model id. Reserved for BI semantic-model routing.",
    )
    measure_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional semantic asset ids such as ['measure:config_rate', 'dimension:launch_time']. "
            "When supplied, their Markdown definitions are forced into SQL-generation context."
        ),
    )
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum result rows returned from read-only SQL.")


class DatabaseSqlGenerateInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    question: str = Field(
        description=(
            "Business question used to generate SQL. For a standalone request, preserve the user's intent and shorthand. "
            "For a Goal, decompose it into a focused sub-question without changing its business semantics. The Agent may "
            "select tables and columns only from the active analytics model's declared data assets, but must not embed an "
            "Agent-written SELECT/JOIN/CTE implementation. EAV values and entity mappings remain subject to semantic "
            "and schema evidence checks."
        )
    )
    database_source_id: str | None = Field(default=None, description="Optional configured database source id.")
    table_names: list[str] = Field(
        default_factory=list,
        description=(
            "Optional physical table names or full <database_source_id>.<table_name> data-asset references selected by "
            "the Agent from the active analytics model. The router intersects them with both model-declared tables and "
            "the database source allowlist; explicit names never broaden authorization."
        ),
    )
    model_id: str | None = Field(
        default=None,
        description=(
            "Optional analytics model fallback for non-Agent callers. In Agent mode, the UI-selected model from "
            "trusted runtime state takes precedence."
        ),
    )
    measure_ids: list[str] = Field(
        default_factory=list,
        description="Deprecated alias for selected_semantic_asset_ids.",
    )
    semantic_asset_ids: list[str] = Field(
        default_factory=list,
        description="Deprecated alias for selected_semantic_asset_ids.",
    )
    selected_semantic_asset_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Exact ids selected from the current analytics model's semantic-asset metadata index. "
            "Pass only assets relevant to the current question."
        ),
    )
    parent_generation_id: str | None = Field(
        default=None,
        description=(
            "Generation id returned by an earlier database_sql_generate call. Set this only when proposing "
            "a semantic change to that SQL; the user must approve the natural-language change before regeneration."
        ),
    )
    revision_instruction: str | None = Field(
        default=None,
        description=(
            "Natural-language feedback about an observed SQL problem or a user-requested semantic change. "
            "Report the symptom/error; do not prescribe fields, tables, entities, SQL fragments, JOIN/CTE shape, "
            "or replacement implementation. Requires parent_generation_id. Semantic changes trigger the "
            "agree/reject/modify HITL flow."
        ),
    )
    schema_evidence_receipt_id: str | None = Field(
        default=None,
        description=(
            "Optional server-issued receipt returned by database_schema_inspect. "
            "Use it for an evidence-backed physical EAV repair; never copy schema rows into revision text."
        ),
    )
    runtime: ToolRuntime


class DatabaseSqlValidateInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sql: str = Field(
        default="",
        description=(
            "Optional read-only SQL for non-Agent callers. In Agent mode, omit this field; the authoritative SQL "
            "is loaded server-side from generation_id."
        ),
    )
    generation_id: str = Field(
        default="",
        description="Generation id returned by database_sql_generate. Required in Agent mode.",
    )
    database_source_id: str | None = Field(default=None, description="Optional configured database source id.")
    table_names: list[str] = Field(default_factory=list, description="Optional authorized table names.")
    runtime: ToolRuntime


class DatabaseSqlExecuteInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sql: str = Field(
        default="",
        description=(
            "Optional explicit SQL for non-Agent callers. In Agent mode, omit this field; the authoritative SQL "
            "is loaded server-side from generation_id."
        ),
    )
    generation_id: str = Field(
        default="",
        description="Generation id returned by database_sql_generate. Required in Agent mode.",
    )
    validation_receipt_id: str = Field(
        default="",
        description=(
            "Receipt id returned by database_sql_validate. Required in Agent mode and bound to the exact "
            "generation SQL hash."
        ),
    )
    database_source_id: str | None = Field(default=None, description="Optional configured database source id.")
    table_names: list[str] = Field(default_factory=list, description="Optional authorized table names.")
    limit: int = Field(default=100, ge=1, le=5000, description="Maximum preview rows returned to the model.")
    timeout_ms: int | None = Field(default=None, ge=1000, le=300000, description="Optional statement timeout in ms.")
    runtime: ToolRuntime


class DatabaseSchemaInspectInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    mode: str = Field(
        default="tables",
        description="One of: tables, columns, type_names, sample.",
    )
    database_source_id: str | None = Field(default=None, description="Optional configured database source id.")
    table_name: str | None = Field(default=None, description="Table name for columns/type_names/sample modes.")
    search: str | None = Field(default=None, description="Optional fuzzy search text for type_names.")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum rows to return.")
    parent_generation_id: str | None = Field(
        default=None,
        description="Optional SQL generation id whose physical mapping is being diagnosed.",
    )
    runtime: ToolRuntime


class DatabaseQueryTraceInspectInput(BaseModel):
    session_id: str = Field(description="Session id, with or without the session- prefix.")
    latest: bool = Field(default=True, description="Return the latest database-related tool calls first.")
    limit: int = Field(default=5, ge=1, le=50, description="Maximum tool calls to summarize.")


class DatabaseQueryResultPageInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    result_id: str = Field(
        description=(
            "A persisted qr_* result_id explicitly returned by database_knowledge_query or "
            "database_sql_execute. Do not pass a sql-gen-* generation_id. If execution returned "
            "no result_id because the materialization row cap was exceeded, adjust/rerun the query first."
        )
    )
    page: int = Field(default=1, ge=1, description="1-based page number.")
    page_size: int | None = Field(default=None, ge=1, le=5000, description="Optional page size.")
    runtime: ToolRuntime


class DatabaseQueryResultSourceInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    result_id: str = Field(
        description=(
            "A persisted qr_* result_id explicitly returned by database_knowledge_query "
            "or database_sql_execute. Do not pass a sql-gen-* generation_id or retry a missing/expired ID; "
            "rerun the database query after narrowing/aggregating it or raising the materialization row cap."
        )
    )
    runtime: ToolRuntime


class SemanticEntityLookupInput(BaseModel):
    dimension_id: str = Field(
        description="Entity-lookup dimension id, for example 'vehicle_series' or 'dimension:vehicle_series'."
    )
    source_ref: str = Field(description="Source asset reference declared by the active Crosswalk binding.")
    keys: list[dict[str, str]] = Field(description="One or more source key objects using the binding field names.")
    include_non_joinable: bool = Field(
        default=False, description="Include candidate and unmatched mappings for diagnosis only."
    )
