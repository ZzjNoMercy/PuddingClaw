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


class LegacyDatabaseSqlValidateInput(BaseModel):
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


class DatabaseSqlValidateInput(BaseModel):
    """Untrusted Agent SQL plus server-owned evidence references."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sql: str = Field(description="Agent-authored read-only SQL to validate and register.")
    database_source_id: str | None = Field(default=None, description="Optional source subset; never broadens runtime scope.")
    table_names: list[str] = Field(default_factory=list, description="Optional table subset; never broadens model/source scope.")
    selected_semantic_asset_ids: list[str] = Field(
        default_factory=list,
        description="Optional semantic hints used for advisory diagnostics; they do not authorize or block SQL execution.",
    )
    evidence_search_id: str = Field(
        default="",
        description="Optional database_evidence_search provenance id used for advisory diagnostics only.",
    )
    schema_evidence_receipt_ids: list[str] = Field(default_factory=list, description="Server-issued inspect receipts.")
    runtime: ToolRuntime


class DatabaseEvidenceSearchInput(BaseModel):
    """Agent-facing, evidence-only database search contract."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    question: str = Field(description="The user's business question. This tool retrieves evidence and never writes SQL.")
    database_source_id: str | None = Field(default=None, description="Optional configured database source id.")
    table_names: list[str] = Field(default_factory=list, description="Optional table subset; it can only narrow scope.")
    selected_semantic_asset_ids: list[str] = Field(default_factory=list, description="Semantic asset ids selected from the active model.")
    focus_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Optional physical column names used only to filter retrieved entity candidates; "
            "this does not select EAV type_name values or business enums."
        ),
    )
    entity_types: list[str] = Field(default_factory=list, description="Optional Vanna entity types to retrieve.")
    include_similar_sql: bool = Field(default=True, description="Whether to include reference-only similar SQL examples.")
    reference_top_k: int = Field(default=5, ge=1, le=20)
    value_profile_limit: int = Field(default=50, ge=1, le=200)
    runtime: ToolRuntime


class DatabaseSqlExecuteInput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sql: str = Field(
        default="",
        description=(
            "Optional explicit SQL for non-Agent callers. For Agent SQL, omit this field; the authoritative SQL is "
            "loaded server-side from sql_submission_id."
        ),
    )
    generation_id: str = Field(
        default="",
        description="Legacy generation id returned by database_sql_generate.",
    )
    sql_submission_id: str = Field(
        default="",
        description="Agent-authored SQL submission id returned by database_sql_validate.",
    )
    agent_validation_receipt_id: str = Field(
        default="",
        description="Validation Receipt id returned by database_sql_validate for sql_submission_id.",
    )
    validation_receipt_id: str = Field(
        default="",
        description=(
            "Legacy Receipt id returned by database_sql_validate_legacy, bound to the exact generation SQL hash."
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
        description="One of: tables, columns, type_names, type_values, value_profile, sample.",
    )
    database_source_id: str | None = Field(default=None, description="Optional configured database source id.")
    table_name: str | None = Field(default=None, description="Table name for columns/type_names/sample modes.")
    search: str | None = Field(default=None, description="Optional fuzzy search text for type_names.")
    type_name: str | None = Field(
        default=None,
        description="Exact EAV type_name required by type_values/value_profile modes.",
    )
    limit: int = Field(default=100, ge=1, le=200, description="Maximum rows to return (hard cap: 200).")
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
