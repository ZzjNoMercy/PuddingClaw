"""LLM-readable authoring brief and explainable frontmatter contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .documents import MarkdownDocument

DefinitionKind = Literal["measure", "dimension", "grain", "relation", "analytics_model"]


class AuthoringBrief(BaseModel):
    """Temporary LLM working notes; never a publishable semantic definition."""

    kind: DefinitionKind
    goal: str
    observed: list[str] = Field(default_factory=list)
    confirmed: list[str]
    unresolved: list[str]
    evidence: list[str]
    reviewed_topics: list[str]
    body_outline: list[str] = Field(default_factory=list)

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("observed", "confirmed", "unresolved", "evidence", "reviewed_topics", "body_outline")
    @classmethod
    def _clean_items(cls, values: list[str]) -> list[str]:
        return [item for raw in values if (item := str(raw or "").strip())]


@dataclass(frozen=True)
class FrontmatterEffect:
    field: str
    owner: str
    effect: str
    consumers: tuple[str, ...]
    body_projection: Literal["none", "summary", "required"]
    safe_auto_repair: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "owner": self.owner,
            "effect": self.effect,
            "consumers": list(self.consumers),
            "body_projection": self.body_projection,
            "safe_auto_repair": self.safe_auto_repair,
        }


COMMON_EFFECTS = (
    FrontmatterEffect(
        "formatter",
        "backend",
        "Selects the Registry formatter and document loader.",
        ("analytics.semantic_assets.registry", "analytics.models.registry"),
        "none",
        True,
    ),
    FrontmatterEffect(
        "type",
        "agent_backend",
        "Selects runtime asset routing; relations are excluded from free-text retrieval and grains require explicit matches.",
        ("analytics.semantic_assets.registry", "analytics.semantic_assets.resolver", "analytics.models.registry"),
        "summary",
    ),
    FrontmatterEffect(
        "name",
        "agent_backend",
        "Provides the display and search name.",
        ("analytics.semantic_assets.registry", "analytics.models.registry"),
        "summary",
    ),
    FrontmatterEffect(
        "description",
        "agent_backend",
        "Provides catalogue summary text.",
        ("analytics.semantic_assets.registry", "analytics.models.registry"),
        "summary",
    ),
    FrontmatterEffect(
        "aliases",
        "agent_backend",
        "Adds alternate semantic match terms.",
        ("analytics.semantic_assets.resolver",),
        "summary",
    ),
    FrontmatterEffect(
        "tags",
        "agent_backend",
        "Adds catalogue filtering and retrieval hints.",
        ("analytics.semantic_assets.registry", "analytics.models.registry"),
        "none",
    ),
    FrontmatterEffect(
        "version",
        "backend",
        "Labels the business definition version for display and audit.",
        ("analytics.semantic_assets.registry", "analytics.models.registry"),
        "summary",
    ),
)

KIND_EFFECTS: dict[DefinitionKind, tuple[FrontmatterEffect, ...]] = {
    "measure": (),
    "grain": (),
    "dimension": (
        FrontmatterEffect(
            "resolution_mode",
            "agent_backend",
            "Selects source-field, derived, entity lookup, or calendar resolution.",
            ("analytics.semantic_assets.registry", "analytics.semantic_assets.resolver"),
            "required",
        ),
        FrontmatterEffect(
            "resolution",
            "agent_backend",
            "Controls dimension bindings, canonical keys, and lookup references.",
            ("analytics.semantic_assets.registry", "analytics.semantic_assets.resolver"),
            "required",
        ),
    ),
    "relation": (
        FrontmatterEffect(
            "relation_type",
            "agent_backend",
            "Selects dimension binding or direct join behavior.",
            ("analytics.semantic_assets.registry", "analytics.models.registry"),
            "required",
        ),
        FrontmatterEffect(
            "relation",
            "agent_backend",
            "Controls endpoints, join keys, cardinality, and output keys.",
            ("analytics.semantic_assets.registry", "analytics.models.registry"),
            "required",
        ),
    ),
    "analytics_model": (
        FrontmatterEffect(
            "id",
            "backend",
            "Overrides the directory-derived analytics model id.",
            ("analytics.models.registry",),
            "none",
            True,
        ),
        FrontmatterEffect(
            "data_assets",
            "agent_backend",
            "Limits the tables available to the analytics model.",
            ("analytics.models.registry", "analytics.semantic_runtime.compiler"),
            "required",
        ),
        FrontmatterEffect(
            "semantic_assets",
            "agent_backend",
            "Selects measures, dimensions, and grains available to the model.",
            ("analytics.models.registry", "analytics.semantic_runtime.compiler"),
            "required",
        ),
        FrontmatterEffect(
            "asset_relations",
            "agent_backend",
            "Selects approved relation paths for multi-asset analysis.",
            ("analytics.models.registry", "analytics.semantic_runtime.compiler"),
            "required",
        ),
        FrontmatterEffect(
            "guardrails",
            "agent_backend",
            "Activates deterministic SQL guardrails for the model.",
            ("analytics.models.registry", "analytics.nl2sql.guardrail_runtime"),
            "required",
        ),
        FrontmatterEffect(
            "templates",
            "agent_backend",
            "Registers optional output templates and their semantic scopes.",
            ("analytics.models.registry",),
            "required",
        ),
        FrontmatterEffect(
            "default_template",
            "agent_backend",
            "Selects the default registered output template.",
            ("analytics.models.registry",),
            "required",
        ),
    ),
}

_PATH_KINDS = {
    ("semantic-assets", "measures", "measure.md"): "measure",
    ("semantic-assets", "dimensions", "dimension.md"): "dimension",
    ("semantic-assets", "grains", "grain.md"): "grain",
    ("semantic-assets", "relations", "relation.md"): "relation",
    ("analytics-models", "model.md"): "analytics_model",
}


def kind_from_logical_path(logical_path: str) -> DefinitionKind:
    path = PurePosixPath(str(logical_path or "").lstrip("/"))
    parts = path.parts
    if len(parts) == 4:
        key = (parts[0], parts[1], parts[3])
    elif len(parts) == 3:
        key = (parts[0], parts[2])
    else:
        raise ValueError("definition path must point to one semantic Markdown definition")
    kind = _PATH_KINDS.get(key)
    if kind is None:
        raise ValueError("unsupported semantic definition path")
    return kind  # type: ignore[return-value]


def inspect_frontmatter_contract(kind: DefinitionKind | None = None) -> list[dict[str, Any]]:
    effects = list(COMMON_EFFECTS)
    if kind is not None:
        effects.extend(KIND_EFFECTS[kind])
    else:
        for values in KIND_EFFECTS.values():
            effects.extend(values)
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for effect in effects:
        key = (effect.field, effect.effect)
        if key not in seen:
            seen.add(key)
            result.append(effect.as_dict())
    return result


def repair_technical_frontmatter(
    document: MarkdownDocument,
    *,
    kind: DefinitionKind,
    logical_path: str,
) -> tuple[MarkdownDocument, list[str]]:
    """Normalize fields determined by the explicit typed target operation."""

    meta = dict(document.frontmatter)
    repaired: list[str] = []
    expected_formatter = {
        "relation": "asset-relation",
        "analytics_model": "analytics-model",
    }.get(kind, "semantic-asset")
    expected_type = "analysis_model" if kind == "analytics_model" else kind
    for field, expected in (("formatter", expected_formatter), ("type", expected_type)):
        existing = str(meta.get(field) or "").strip()
        if existing and existing != expected:
            raise ValueError(
                f"frontmatter field '{field}' conflicts with the explicit target path: "
                f"expected '{expected}', got '{existing}'"
            )
    defaults: dict[str, Any] = {
        "formatter": expected_formatter,
        # The target namespace is an explicit Agent operation, not prose
        # inference.  Fill a missing type but never overwrite a conflict.
        "type": expected_type,
    }
    for field, value in defaults.items():
        if meta.get(field) != value:
            meta[field] = value
            repaired.append(field)
    return MarkdownDocument(frontmatter=meta, body=document.body), repaired
