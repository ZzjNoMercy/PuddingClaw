"""Hard tool visibility and execution boundary driven by loaded Skills.

The SkillIntentRouter only recommends a Skill.  This middleware is the one
place that decides which PuddingClaw business tools the model can see and call.
DeepAgents native workspace, write, execution and delegation tools remain
available in every call.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import yaml
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from typing_extensions import TypedDict

from graph.session_manager import session_manager
from tools.toolsets import UNCONDITIONAL_TOOL_NAMES, tools_for_toolsets, validate_toolset_names

_SKILL_PATH_RE = re.compile(r"^/skills/([^/]+)/SKILL\.md$")


def _merge_skill_ids(current: list[str], update: list[str]) -> list[str]:
    return sorted({str(item) for item in [*current, *update] if str(item)})


class ToolsetState(TypedDict, total=False):
    """Checkpoint-visible record of Skills that have actually been read."""

    active_skill_ids: Annotated[list[str], _merge_skill_ids]


class ToolsetMiddleware(AgentMiddleware):
    """Expose and execute business toolsets only after their Skill is read.

    Loaded skills are derived from successful ``read_file`` ToolMessages, then
    persisted as ``active_skill_ids`` for trace/checkpoint inspection. Historical
    successful reads belong to the same session and remain authoritative across
    follow-up turns, so the Agent does not need to re-read a Skill on every user
    message. The derivation is idempotent and also survives HITL resume.
    """

    state_schema = ToolsetState

    def __init__(self, *, skills_dir: Path, toolsets_by_skill: dict[str, set[str]]) -> None:
        super().__init__()
        self.skills_dir = skills_dir.resolve()
        self.toolsets_by_skill = {key: frozenset(value) for key, value in toolsets_by_skill.items()}
        for item in discover_skill_catalog(self.skills_dir):
            self.toolsets_by_skill.setdefault(str(item["skill_id"]), frozenset())

    def _refresh_installed_skill(self, skill_id: str) -> bool:
        """Refresh one Skill after a successful read, including same-Run installs."""

        path = (self.skills_dir / skill_id / "SKILL.md").resolve()
        try:
            path.relative_to(self.skills_dir)
        except ValueError:
            return False
        if not path.is_file():
            # The constructor mapping is an already validated installed-Skill
            # snapshot. Keep it authoritative for the current process; the
            # filesystem branch below additionally discovers same-Run installs.
            return skill_id in self.toolsets_by_skill
        declared = _skill_toolsets(path)
        self.toolsets_by_skill[skill_id] = frozenset(declared)
        return True

    def _loaded_skill_ids(self, messages: list[Any]) -> list[str]:
        """Find successful ``read_file(/skills/<id>/SKILL.md)`` calls."""
        calls: dict[str, str] = {}
        succeeded: set[str] = set()
        for message in messages:
            tool_calls = getattr(message, "tool_calls", None) or []
            for call in tool_calls:
                if call.get("name") != "read_file":
                    continue
                call_id = str(call.get("id") or "")
                if not call_id:
                    continue
                args = call.get("args") or {}
                raw_path = str(args.get("file_path") or args.get("path") or "")
                matched = _SKILL_PATH_RE.fullmatch(raw_path.replace("\\", "/"))
                if matched:
                    calls[call_id] = matched.group(1)
            if isinstance(message, ToolMessage) and message.name == "read_file" and message.status == "success":
                call_id = str(message.tool_call_id or "")
                if call_id in calls:
                    succeeded.add(calls[call_id])
        return sorted(
            skill_id
            for skill_id in succeeded
            if self._refresh_installed_skill(skill_id)
        )

    def before_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        context = runtime.context if runtime is not None and isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        cached = (
            [
                skill_id
                for skill_id in session_manager.get_loaded_skill_ids(session_id)
                if self._refresh_installed_skill(skill_id)
            ]
            if session_id
            else []
        )
        loaded = _merge_skill_ids(cached, self._loaded_skill_ids(list(state.get("messages") or [])))
        return {"active_skill_ids": loaded} if loaded else None

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        loaded = self._loaded_skill_ids(list(state.get("messages") or []))
        active = _merge_skill_ids(list(state.get("active_skill_ids") or []), loaded)
        if active == sorted(state.get("active_skill_ids") or []):
            return None
        return {"active_skill_ids": active}

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        return handler(request.override(tools=self._visible_tools(request)))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(request.override(tools=self._visible_tools(request)))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        denied = self._denied_tool_message(request)
        if denied is not None:
            return denied
        result = handler(request)
        self._remember_successful_skill_read(request, result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        denied = self._denied_tool_message(request)
        if denied is not None:
            return denied
        result = await handler(request)
        self._remember_successful_skill_read(request, result)
        return result

    def _remember_successful_skill_read(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> None:
        if not isinstance(result, ToolMessage) or result.status != "success":
            return
        if str(request.tool_call.get("name") or "") != "read_file":
            return
        args = request.tool_call.get("args") or {}
        raw_path = str(args.get("file_path") or args.get("path") or "").replace("\\", "/")
        matched = _SKILL_PATH_RE.fullmatch(raw_path)
        if matched is None or not self._refresh_installed_skill(matched.group(1)):
            return
        runtime = request.runtime
        context = runtime.context if runtime is not None and isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        if session_id:
            session_manager.add_loaded_skill_ids(session_id, [matched.group(1)])
        if session_id and run_id:
            try:
                session_manager.record_run_skill_selection(
                    session_id,
                    run_id,
                    matched.group(1),
                )
            except ValueError:
                # A late Tool result may race terminalization. The successful
                # read remains in the Session cache, while terminal Run state
                # stays immutable.
                pass

    def _active_skill_ids(self, state: dict[str, Any]) -> list[str]:
        return _merge_skill_ids(
            list(state.get("active_skill_ids") or []),
            self._loaded_skill_ids(list(state.get("messages") or [])),
        )

    def _allowed_tool_names(self, state: dict[str, Any]) -> set[str]:
        enabled_toolsets: set[str] = set()
        for skill_id in self._active_skill_ids(state):
            enabled_toolsets.update(self.toolsets_by_skill.get(skill_id, ()))
        return set(UNCONDITIONAL_TOOL_NAMES) | set(tools_for_toolsets(enabled_toolsets))

    def _denied_tool_message(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name in self._allowed_tool_names(request.state):
            return None
        return ToolMessage(
            content=f"Tool `{tool_name}` is not enabled. Read its authoritative /skills/<id>/SKILL.md first.",
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=tool_name,
            status="error",
        )

    def _visible_tools(self, request: ModelRequest) -> list[Any]:
        allowed = self._allowed_tool_names(request.state)
        return [
            tool
            for tool in request.tools
            if self._tool_name(tool) in allowed
        ]

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", ""))


def _skill_toolsets(path: Path) -> set[str]:
    """Read and validate one Skill's optional ``toolsets`` frontmatter."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    if not text.startswith("---\n"):
        return set()
    _, _, remainder = text.partition("---\n")
    frontmatter, separator, _ = remainder.partition("---\n")
    if not separator:
        return set()
    try:
        raw = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return set()
    toolsets = raw.get("toolsets") if isinstance(raw, dict) else None
    if not isinstance(toolsets, list):
        return set()
    declared = {str(item).strip() for item in toolsets if str(item).strip()}
    unknown = validate_toolset_names(declared)
    if unknown:
        raise ValueError(
            f"Skill {path.parent.name} declares unknown toolsets: {', '.join(unknown)}"
        )
    return declared


def discover_skill_toolsets(skills_dir: Path) -> dict[str, set[str]]:
    """Read optional ``toolsets`` frontmatter from every project Skill."""
    result: dict[str, set[str]] = {}
    for path in skills_dir.glob("*/SKILL.md"):
        declared = _skill_toolsets(path)
        if declared:
            result[path.parent.name] = declared
    return result


def discover_skill_catalog(skills_dir: Path) -> list[dict[str, Any]]:
    """Build the task router's catalog from installed Skill frontmatter."""

    catalog: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return catalog
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        raw: dict[str, Any] = {}
        if text.startswith("---\n"):
            _, _, remainder = text.partition("---\n")
            frontmatter, separator, _ = remainder.partition("---\n")
            if separator:
                try:
                    parsed = yaml.safe_load(frontmatter) or {}
                except yaml.YAMLError:
                    parsed = {}
                if isinstance(parsed, dict):
                    raw = parsed
        description = str(raw.get("description") or "").strip()
        catalog.append(
            {
                "skill_id": path.parent.name,
                "name": str(raw.get("name") or path.parent.name).strip(),
                "description": description[:600],
                "path": f"/skills/{path.parent.name}/SKILL.md",
            }
        )
    return catalog
