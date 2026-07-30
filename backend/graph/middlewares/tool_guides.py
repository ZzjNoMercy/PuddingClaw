"""Request-scoped progressive disclosure for DeepAgents Tool Guides."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langgraph.config import get_stream_writer

from graph.trace_collector import get_current_trace_collector


@dataclass(frozen=True)
class ToolGuideSpec:
    """One deterministic Tool Guide activation rule."""

    guide_id: str
    path: Path
    content: str
    content_sha256: str
    skills: frozenset[str]
    skill_prefixes: tuple[str, ...]
    tools: frozenset[str]
    tool_prefixes: tuple[str, ...]


class ToolGuideMiddleware(AgentMiddleware[Any, Any, Any]):
    """Inject only Tool Guides relevant to the current Run capability set."""

    def __init__(self, *, base_dir: Path) -> None:
        super().__init__()
        self.guide_dir = (base_dir / "prompts" / "deepagents" / "tool_guides").resolve()
        self.specs = self._load_specs()

    @property
    def name(self) -> str:
        return "ToolGuideMiddleware"

    @staticmethod
    def _strings(value: Any, *, field: str, guide_id: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"Tool Guide {guide_id!r} field {field!r} must be a list of non-empty strings")
        return tuple(item.strip() for item in value)

    def _load_specs(self) -> tuple[ToolGuideSpec, ...]:
        manifest_path = self.guide_dir / "manifest.yaml"
        if not manifest_path.exists():
            return ()
        try:
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Unable to load Tool Guide manifest: {manifest_path}") from exc
        guides = payload.get("guides") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Tool Guide manifest version must be 1")
        if not isinstance(guides, list):
            raise ValueError("Tool Guide manifest must contain a guides list")

        specs: list[ToolGuideSpec] = []
        seen: set[str] = set()
        for item in guides:
            if not isinstance(item, dict):
                raise ValueError("Each Tool Guide manifest entry must be an object")
            guide_id = str(item.get("id") or "").strip()
            relative_file = str(item.get("file") or "").strip()
            if not guide_id or guide_id in seen:
                raise ValueError(f"Tool Guide id must be non-empty and unique: {guide_id!r}")
            if not relative_file:
                raise ValueError(f"Tool Guide {guide_id!r} is missing file")
            path = (self.guide_dir / relative_file).resolve()
            if path.parent != self.guide_dir or not path.is_file():
                raise ValueError(f"Tool Guide {guide_id!r} references an invalid file: {relative_file!r}")
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                raise ValueError(f"Tool Guide {guide_id!r} is empty")
            skills = self._strings(item.get("skills"), field="skills", guide_id=guide_id)
            skill_prefixes = self._strings(
                item.get("skill_prefixes"), field="skill_prefixes", guide_id=guide_id
            )
            tools = self._strings(item.get("tools"), field="tools", guide_id=guide_id)
            tool_prefixes = self._strings(
                item.get("tool_prefixes"), field="tool_prefixes", guide_id=guide_id
            )
            if not any((skills, skill_prefixes, tools, tool_prefixes)):
                raise ValueError(f"Tool Guide {guide_id!r} has no activation rule")
            seen.add(guide_id)
            specs.append(
                ToolGuideSpec(
                    guide_id=guide_id,
                    path=path,
                    content=content,
                    content_sha256="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    skills=frozenset(skills),
                    skill_prefixes=skill_prefixes,
                    tools=frozenset(tools),
                    tool_prefixes=tool_prefixes,
                )
            )
        declared_paths = {spec.path for spec in specs}
        reserved_paths = {(self.guide_dir / "core.md").resolve()}
        orphaned = sorted(
            path.name
            for path in self.guide_dir.glob("*.md")
            if path.resolve() not in declared_paths | reserved_paths
        )
        if orphaned:
            raise ValueError(f"Tool Guide files are not registered in manifest.yaml: {', '.join(orphaned)}")
        return tuple(specs)

    @staticmethod
    def _tool_name(tool: Any) -> str:
        if isinstance(tool, dict):
            return str(tool.get("name") or tool.get("function", {}).get("name") or "")
        return str(getattr(tool, "name", "") or "")

    @staticmethod
    def _matches_prefix(values: set[str], prefixes: tuple[str, ...]) -> bool:
        return any(value.startswith(prefix) for value in values for prefix in prefixes)

    def _activation_reasons(self, request: ModelRequest, spec: ToolGuideSpec) -> list[str]:
        active_skills = {str(item) for item in request.state.get("active_skill_ids") or [] if str(item)}
        visible_tools = {name for tool in request.tools if (name := self._tool_name(tool))}
        reasons: list[str] = []
        matched_skills = sorted(active_skills & spec.skills)
        if matched_skills:
            reasons.append(f"skills:{','.join(matched_skills)}")
        if self._matches_prefix(active_skills, spec.skill_prefixes):
            reasons.append("skill_prefix")
        matched_tools = sorted(visible_tools & spec.tools)
        if matched_tools:
            reasons.append(f"tools:{','.join(matched_tools)}")
        if self._matches_prefix(visible_tools, spec.tool_prefixes):
            reasons.append("tool_prefix")
        return reasons

    def _request_with_guides(self, request: ModelRequest) -> ModelRequest:
        activated: list[tuple[ToolGuideSpec, list[str]]] = []
        for spec in self.specs:
            reasons = self._activation_reasons(request, spec)
            if not reasons:
                continue
            activated.append((spec, reasons))
        if not activated:
            return request

        guide_ids = [spec.guide_id for spec, _ in activated]
        section = (
            "## Activated Tool Guides (request-scoped)\n\n"
            "The following protocols are active because their Skills or gated tools are active in this Run.\n\n"
            + "\n\n".join(spec.content for spec, _ in activated)
        )
        content_hash = "sha256:" + hashlib.sha256(section.encode("utf-8")).hexdigest()
        payload = {
            "type": "tool_guides_activated",
            "guide_ids": guide_ids,
            "content_hash": content_hash,
            "guide_hashes": {spec.guide_id: spec.content_sha256 for spec, _ in activated},
            "reasons": {spec.guide_id: reasons for spec, reasons in activated},
        }
        try:
            get_stream_writer()(payload)
        except (KeyError, RuntimeError):
            pass
        collector = get_current_trace_collector()
        if collector is not None:
            collector.add_custom_span(
                "tool_guides_activated",
                payload,
                span_type="middleware",
                metadata={"guide_ids": guide_ids, "content_hash": content_hash},
            )
        return request.override(
            system_message=append_to_system_message(request.system_message, section)
        )

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        return handler(self._request_with_guides(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._request_with_guides(request))
