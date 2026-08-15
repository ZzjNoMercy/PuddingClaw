"""Two-tool publication protocol for LLM-authored semantic Markdown."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from analytics.semantic_authoring.contracts import DefinitionKind
from analytics.semantic_authoring.discovery import SemanticDiscoveryError, discover_semantic_definitions
from analytics.semantic_authoring.service import (
    SemanticAuthoringError,
    prepare_semantic_markdown,
    publish_semantic_markdown,
)


class DiscoverSemanticDefinitionsInput(BaseModel):
    query: str = Field(
        default="",
        description=(
            "Business concept, exact id, or name to search. Leave blank only for a paginated inventory listing; "
            "prepare requires a targeted non-empty discovery receipt."
        ),
    )
    kinds: list[DefinitionKind] = Field(
        default_factory=lambda: ["measure", "dimension", "grain", "relation", "analytics_model"],
        description="Definition kinds to search.",
    )
    cursor: str = Field(default="", description="Opaque next_cursor from a prior inventory page.")
    limit: int = Field(default=20, ge=1, le=50, description="Maximum candidates to return per page.")


class PrepareSemanticMarkdownInput(BaseModel):
    logical_path: str = Field(
        description=(
            "Virtual definition path for a Measure, Grain, ordinary Dimension, Relation, or Analytics Model. "
            "Complex entity_lookup Dimensions must use build-semantic-dimension."
        )
    )
    candidate_markdown: str = Field(
        description="Complete candidate Markdown, including frontmatter and the user-auditable body."
    )
    baseline_digest: str = Field(
        default="",
        description="Optional sha256 digest from the previously read published file, or 'absent' for a create.",
    )
    discovery_receipt_id: str = Field(
        description=(
            "Receipt from a targeted discover_semantic_definitions call in this Agent session. "
            "It proves the relevant existing catalogue was checked before choosing reuse, edit, or create."
        )
    )
    authoring_brief: dict[str, Any] = Field(
        description=(
            "Required control-only LLM working notes with kind, goal, observed, confirmed, unresolved, evidence, "
            "reviewed_topics, and body_outline. It must explicitly record every unresolved business decision. "
            "Notes gate preparation but never become the published definition."
        ),
    )


class PublishSemanticMarkdownInput(BaseModel):
    plan_id: str = Field(description="Prepared plan id returned by prepare_semantic_markdown.")
    plan_digest: str = Field(
        description="Exact immutable plan digest that the user approved after reviewing the body and machine effects."
    )


def _error_payload(exc: Exception) -> str:
    if isinstance(exc, (SemanticAuthoringError, SemanticDiscoveryError)):
        safe_messages = {
            "baseline_changed": "Published Markdown changed; read it again and prepare a new plan.",
            "definition_dependencies_changed": (
                "A referenced definition or package resource changed; read dependencies and prepare a new plan."
            ),
            "discovery_required": "Search existing definitions before preparing a candidate.",
            "targeted_discovery_required": "Run a targeted non-empty discovery before preparing a candidate.",
            "discovery_incomplete": "Discovery has more matching candidates; narrow the query or retrieve a complete result.",
            "discovery_stale": "The semantic catalogue changed; discover again before preparing or publishing.",
            "discovery_expired": "The discovery receipt expired; discover again.",
            "discovery_session_mismatch": "The discovery receipt belongs to another Agent session.",
            "discovery_receipt_integrity_mismatch": "The discovery receipt failed its server integrity check.",
            "discovery_kind_mismatch": "Discovery did not cover the target definition kind.",
            "discovery_target_not_returned": "The existing target was not returned by discovery; search and review it.",
            "candidate_invalid": str(exc),
            "candidate_too_large": "Semantic Markdown exceeds the supported size limit.",
            "unsupported_definition_path": "The path is not a supported semantic definition target.",
            "entity_lookup_requires_dimension_builder": (
                "entity_lookup Dimensions must use the dedicated build-semantic-dimension workflow."
            ),
            "missing_authoring_brief": "A complete Authoring Brief is required before preparation.",
            "invalid_authoring_brief": "The Authoring Brief is incomplete or malformed.",
            "session_required": "A bound Agent session is required.",
        }
        payload = {
            "ok": False,
            "error_code": exc.code,
            "message": safe_messages.get(exc.code, "Semantic authoring request was rejected."),
        }
    else:
        payload = {
            "ok": False,
            "error_code": "semantic_authoring_failed",
            "message": "Semantic authoring failed without changing the published definition.",
        }
    return json.dumps(payload, ensure_ascii=False)


class DiscoverSemanticDefinitionsTool(BaseTool):
    name: str = "discover_semantic_definitions"
    description: str = (
        "List or search existing Measures, Dimensions, Grains, Relations, and Analytics Models through their "
        "Registries. Returns stable ids, logical paths, match reasons, definition digests, model backlinks, and "
        "a session-bound receipt. Use a blank query to answer inventory questions. Before creating or changing a "
        "definition, use a targeted business query, read plausible candidates' Markdown, and decide reuse, edit, "
        "or create; pass its receipt to prepare_semantic_markdown."
    )
    args_schema: type[BaseModel] = DiscoverSemanticDefinitionsInput
    risk_level: str = "safe"
    session_id: str = Field(default="", exclude=True, repr=False)

    def _run(self, **kwargs: Any) -> str:
        try:
            result = discover_semantic_definitions(
                query=str(kwargs.get("query") or ""),
                kinds=list(kwargs.get("kinds") or []),
                cursor=str(kwargs.get("cursor") or ""),
                limit=int(kwargs.get("limit") or 20),
                session_id=self.session_id,
            )
            return json.dumps({"ok": True, **result}, ensure_ascii=False)
        except Exception as exc:
            return _error_payload(exc)


class PrepareSemanticMarkdownTool(BaseTool):
    name: str = "prepare_semantic_markdown"
    description: str = (
        "Validate and freeze one complete semantic Markdown candidate without writing active definitions. "
        "It repairs only technical frontmatter derived from the explicit target path, returns the rendered body, "
        "a natural-language machine-effect summary, a technical diff, and a digest-bound plan. "
        "Use after discovery is complete and business decisions are resolved; the required Authoring Brief is a "
        "control gate, never publishable truth."
    )
    args_schema: type[BaseModel] = PrepareSemanticMarkdownInput
    risk_level: str = "moderate"
    session_id: str = Field(default="", exclude=True, repr=False)

    def _run(self, **kwargs: Any) -> str:
        try:
            result = prepare_semantic_markdown(
                logical_path=str(kwargs.get("logical_path") or ""),
                candidate_markdown=str(kwargs.get("candidate_markdown") or ""),
                baseline_digest=str(kwargs.get("baseline_digest") or ""),
                discovery_receipt_id=str(kwargs.get("discovery_receipt_id") or ""),
                brief=kwargs.get("authoring_brief"),
                session_id=self.session_id,
            )
            return json.dumps({"ok": True, **result}, ensure_ascii=False)
        except Exception as exc:
            return _error_payload(exc)


class PublishSemanticMarkdownTool(BaseTool):
    name: str = "publish_semantic_markdown"
    description: str = (
        "Request publication of exactly one frozen semantic Markdown plan. After surfacing its rendered body, "
        "machine-effect summary, risk, and plan digest, call this immediately in the same assistant turn; the "
        "Harness will pause the call and display the single digest-bound HITL approval card. Do not ask the user "
        "for a separate chat approval. On approval it performs baseline digest CAS, atomic file replacement, "
        "Registry refresh, and rollback on failure."
    )
    args_schema: type[BaseModel] = PublishSemanticMarkdownInput
    risk_level: str = "moderate"
    session_id: str = Field(default="", exclude=True, repr=False)

    def _run(self, **kwargs: Any) -> str:
        try:
            result = publish_semantic_markdown(
                plan_id=str(kwargs.get("plan_id") or ""),
                plan_digest=str(kwargs.get("plan_digest") or ""),
                session_id=self.session_id,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return _error_payload(exc)


def create_semantic_steward_tools() -> list[BaseTool]:
    return [DiscoverSemanticDefinitionsTool(), PrepareSemanticMarkdownTool(), PublishSemanticMarkdownTool()]
