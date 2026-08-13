"""Two-tool vertical slice for LLM-authored semantic Markdown."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from analytics.semantic_authoring.service import (
    SemanticAuthoringError,
    prepare_semantic_markdown,
    publish_semantic_markdown,
)


class PrepareSemanticMarkdownInput(BaseModel):
    logical_path: str = Field(
        description=(
            "Virtual definition path. The first vertical slice accepts only "
            "semantic-assets/measures/<id>/measure.md."
        )
    )
    candidate_markdown: str = Field(
        description="Complete candidate Markdown, including frontmatter and the user-auditable body."
    )
    baseline_digest: str = Field(
        default="",
        description="Optional sha256 digest from the previously read published file, or 'absent' for a create.",
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
    if isinstance(exc, SemanticAuthoringError):
        safe_messages = {
            "baseline_changed": "Published Markdown changed; read it again and prepare a new plan.",
            "candidate_invalid": str(exc),
            "candidate_too_large": "Semantic Markdown exceeds the supported size limit.",
            "measure_vertical_only": "The current publishing vertical supports Measure Markdown only.",
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


class PrepareSemanticMarkdownTool(BaseTool):
    name: str = "prepare_semantic_markdown"
    description: str = (
        "Validate and freeze one complete semantic Markdown candidate without writing active definitions. "
        "It repairs only technical frontmatter derived from the explicit target path, returns the rendered body, "
        "a natural-language machine-effect summary, a technical diff, and a digest-bound plan. "
        "Use after business decisions are resolved; never treat the optional Authoring Brief as publishable truth."
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
                brief=kwargs.get("authoring_brief"),
                session_id=self.session_id,
            )
            return json.dumps({"ok": True, **result}, ensure_ascii=False)
        except Exception as exc:
            return _error_payload(exc)


class PublishSemanticMarkdownTool(BaseTool):
    name: str = "publish_semantic_markdown"
    description: str = (
        "Publish exactly one frozen semantic Markdown plan after the user explicitly approves its rendered body, "
        "machine-effect summary, risk, and plan digest. Performs baseline digest CAS, atomic file replacement, "
        "Registry refresh, and rollback on failure. Never call in the same turn that prepared the plan unless "
        "the user had already approved that exact digest."
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
    return [PrepareSemanticMarkdownTool(), PublishSemanticMarkdownTool()]
