"""Progressive semantic-asset index for the selected analytics model."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NotRequired, TypedDict

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest, ModelResponse, PrivateStateAttr

from analytics.models import get_analytics_model_registry


class SemanticAssetsState(AgentState):
    """Private model-scoped semantic metadata exposed to tools through State."""

    semantic_assets_model_id: NotRequired[Annotated[str, PrivateStateAttr]]
    semantic_assets_metadata: NotRequired[Annotated[list[dict[str, Any]], PrivateStateAttr]]
    allowed_semantic_asset_ids: NotRequired[Annotated[list[str], PrivateStateAttr]]


class SemanticAssetsStateUpdate(TypedDict):
    semantic_assets_model_id: str
    semantic_assets_metadata: list[dict[str, Any]]
    allowed_semantic_asset_ids: list[str]


SEMANTIC_ASSETS_SYSTEM_PROMPT = """

## Analytics Model Semantic Assets

The selected analytics model exposes the following semantic-asset metadata.
Use this index to choose only the measures, dimensions, and grains relevant to
the user's current analytics question. Pass their exact ids in
`database_sql_generate.selected_semantic_asset_ids` for database work or
`pandas_knowledge_query.selected_semantic_asset_ids` for spreadsheet work.
Do not pass every available asset. Both tools load the selected assets' complete
authoritative definitions through the shared semantic runtime; do not copy
their bodies into the question.

Selected model: `{model_id}`

{asset_list}
"""


class SemanticAssetsMiddleware(AgentMiddleware[SemanticAssetsState, Any, Any]):
    """Expose selected-model semantic frontmatter with progressive disclosure."""

    state_schema = SemanticAssetsState

    def __init__(self, *, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()

    def _load(self, model_id: str) -> list[dict[str, Any]]:
        if not model_id:
            return []
        try:
            model = get_analytics_model_registry(self._base_dir).get_model_context(
                model_id
            )
        except Exception:
            # The main analytics-model context path reports a missing model to
            # the user. This progressive index must remain a best-effort
            # enhancement and must never crash an otherwise valid Run.
            return []
        metadata: list[dict[str, Any]] = []
        for item in model.get("semantic_assets") or []:
            asset_id = str(item.get("id") or "").strip()
            if not asset_id:
                continue
            frontmatter = item.get("frontmatter") or {}
            aliases = frontmatter.get("aliases") if isinstance(frontmatter, dict) else []
            tags = frontmatter.get("tags") if isinstance(frontmatter, dict) else []
            metadata.append(
                {
                    "id": asset_id,
                    "name": str(item.get("name") or ""),
                    "type": str(item.get("type") or ""),
                    "description": str(item.get("description") or ""),
                    "path": f"/{str(item.get('path') or '').lstrip('/')}",
                    # State retains the complete registry metadata for audit and
                    # non-prompt consumers. _format_assets deliberately exposes
                    # only the stable selection index below.
                    "frontmatter": frontmatter,
                    "aliases": self._string_list(aliases),
                    "tags": self._string_list(tags),
                }
            )
        return metadata

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, set):
            candidates = sorted(value, key=lambda item: str(item))
        elif isinstance(value, (list, tuple)):
            candidates = list(value)
        else:
            candidates = []
        return list(
            dict.fromkeys(
                normalized
                for item in candidates
                for normalized in [str(item or "").strip()]
                if normalized
            )
        )

    def before_agent(self, state: SemanticAssetsState, runtime: Any) -> SemanticAssetsStateUpdate:
        # Rebuild on every run so model switches and registry edits cannot leave
        # a stale session-scoped id range behind.
        model_id = str(state.get("analytics_model_id") or "").strip()
        metadata = self._load(model_id) if model_id else []
        return SemanticAssetsStateUpdate(
            semantic_assets_model_id=model_id,
            semantic_assets_metadata=metadata,
            allowed_semantic_asset_ids=[item["id"] for item in metadata],
        )

    def before_model(
        self,
        state: SemanticAssetsState,
        runtime: Any,
    ) -> SemanticAssetsStateUpdate | None:
        """Refresh the selected model's metadata index after a model switch."""

        model_id = str(state.get("analytics_model_id") or "").strip()
        if not model_id:
            return None
        if (
            str(state.get("semantic_assets_model_id") or "") == model_id
            and isinstance(state.get("semantic_assets_metadata"), list)
        ):
            return None
        metadata = self._load(model_id)
        return SemanticAssetsStateUpdate(
            semantic_assets_model_id=model_id,
            semantic_assets_metadata=metadata,
            allowed_semantic_asset_ids=[item["id"] for item in metadata],
        )

    @staticmethod
    def _format_assets(metadata: list[dict[str, Any]]) -> str:
        if not metadata:
            return "(The selected model declares no semantic assets.)"
        blocks: list[str] = []
        for item in metadata:
            aliases = ", ".join(item.get("aliases") or []) or "<none>"
            tags = ", ".join(item.get("tags") or []) or "<none>"
            blocks.append(
                "\n".join(
                    [
                        f"### {item['id']} | {item['name']}",
                        f"Type: {item['type']} | Canonical path: `{item['path']}`",
                        f"Description: {item['description'] or '<none>'}",
                        f"Aliases: {aliases}",
                        f"Tags: {tags}",
                    ]
                )
            )
        return "\n\n---\n\n".join(blocks)

    def _modify_request(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        metadata = request.state.get("semantic_assets_metadata", [])
        model_id = str(request.state.get("semantic_assets_model_id") or "").strip()
        if not model_id:
            return request
        section = SEMANTIC_ASSETS_SYSTEM_PROMPT.format(
            model_id=model_id,
            asset_list=self._format_assets(metadata),
        )
        return request.override(system_message=append_to_system_message(request.system_message, section))

    def wrap_model_call(self, request: ModelRequest[Any], handler: Any) -> ModelResponse[Any]:
        return handler(self._modify_request(request))

    async def awrap_model_call(self, request: ModelRequest[Any], handler: Any) -> ModelResponse[Any]:
        return await handler(self._modify_request(request))


__all__ = ["SemanticAssetsMiddleware", "SemanticAssetsState"]
