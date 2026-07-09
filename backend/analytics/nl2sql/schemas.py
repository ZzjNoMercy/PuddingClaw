"""Typed contracts for database-backed natural language analytics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class DatabaseQueryRequest:
    """Input contract for the internal database knowledge query service."""

    question: str
    database_source_id: str | None = None
    table_names: list[str] = field(default_factory=list)
    model_id: str | None = None
    measure_ids: list[str] = field(default_factory=list)
    limit: int = 100
    allow_llm_to_see_data: bool = False


@dataclass(slots=True)
class TableCandidate:
    """A database table candidate considered by the router."""

    name: str
    columns: list[str] = field(default_factory=list)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TableRoute:
    """Resolved database/table scope for a question."""

    database_source_id: str
    source_name: str
    database: str
    dialect: str
    table_names: list[str]
    available_tables: list[str]
    candidates: list[TableCandidate]
    confidence: float
    reason: str
    prompt_context: str


@dataclass(slots=True)
class SqlExecutionResult:
    """Read-only SQL execution output."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    limited: bool
    total_row_count: int | None = None
    preview_count: int | None = None
    omitted_count: int = 0
    is_complete: bool = True
    estimated_tokens: int = 0
    profile: dict[str, Any] = field(default_factory=dict)
    result_id: str | None = None
    result_store: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    llm_guardrail: str = ""
    materialized_rows: list[dict[str, Any]] = field(default_factory=list, repr=False)
    materialized_all: bool = True


@dataclass(slots=True)
class DatabaseQueryResult:
    """Full NL2SQL result returned to tools and API layers."""

    question: str
    sql: str
    source: dict[str, Any]
    route: TableRoute
    execution: SqlExecutionResult
    references: dict[str, Any] = field(default_factory=dict)
    semantic_assets: dict[str, Any] = field(default_factory=dict)
    stage_timings: dict[str, float] = field(default_factory=dict)
    warning: str | None = None


@dataclass(slots=True)
class DatabaseSqlGenerationResult:
    """SQL generation output before read-only execution."""

    question: str
    sql: str
    source: dict[str, Any]
    route: TableRoute
    references: dict[str, Any] = field(default_factory=dict)
    semantic_assets: dict[str, Any] = field(default_factory=dict)
    stage_timings: dict[str, float] = field(default_factory=dict)
    guardrail_note: str = ""


def to_plain_dict(value: Any) -> Any:
    """Convert dataclass contracts to JSON-serializable Python structures."""

    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value
