"""Hard tool visibility and execution boundary driven by loaded Skills.

The SkillIntentRouter only recommends a Skill.  This middleware is the one
place that decides which PuddingClaw business tools the model can see and call.
DeepAgents native workspace, write, execution and delegation tools remain
available in every call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import yaml
from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.types import Command
from typing_extensions import TypedDict

from graph.permission_policy import PermissionBindingPolicy, RunPermissionContext
from graph.session_manager import session_manager
from graph.trace_collector import get_current_trace_collector
from harness.models import (
    CapabilityManifest,
    PermissionManifest,
    RunTaskProfile,
    SkillActivation,
    SkillRecommendation,
)
from observability import emit_harness_metric
from tools.toolsets import (
    UNCONDITIONAL_TOOL_NAMES,
    tool_control_descriptor,
    tools_for_toolsets,
    validate_toolset_names,
)

_SKILL_PATH_RE = re.compile(r"^/skills/([^/]+)/SKILL\.md$")
_LEGACY_EXTERNAL_LEASE_TOOLS = frozenset(
    {
        "stage_external_artifact",
        "commit_external_artifact",
        "stage_external_directory",
        "prepare_external_directory_commit",
        "commit_external_directory",
    }
)
_INSPECTION_BLOCKED_READISH_TOOLS = frozenset(
    {
        "stage_external_artifact",
        "stage_external_directory",
        "prepare_external_directory_commit",
    }
)
_GOAL_INSPECTION_ALLOWED_TOOLS = frozenset(
    {
        # Existing Session/Goal evidence and explicitly supplied resources.
        "ls",
        "read_file",
        "glob",
        "grep",
        "inspect_file_version",
        "read_resource",
        "read_evidence",
        # Existing database Evidence only; no generation, validation,
        # execution, or SourceReference registration.
        "database_query_trace_inspect",
        "database_query_result_page",
    }
)
_CAPABILITY_RECOMMENDATION_MARKER = "[系统 Capability 建议]"
logger = logging.getLogger(__name__)

_ARGUMENT_DEPENDENT_PERMISSION_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "inspect_file_version",
        "copy_file",
        "materialize_source_ref",
        "replace_file",
        "patch_file",
        "patch_files",
        "delete_file",
    }
)


def _merge_skill_ids(current: list[str], update: list[str]) -> list[str]:
    return sorted({str(item) for item in [*current, *update] if str(item)})


def _merge_skill_activations(
    current: list[dict[str, Any]],
    update: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {
        str(item.get("activation_id")): dict(item)
        for item in [*current, *update]
        if isinstance(item, dict) and item.get("activation_id")
    }
    return list(by_id.values())


class ToolsetState(TypedDict, total=False):
    """Checkpoint-visible record of Skills that have actually been read."""

    active_skill_ids: Annotated[list[str], _merge_skill_ids]
    skill_activations: Annotated[list[dict[str, Any]], _merge_skill_activations]
    capability_manifest: dict[str, Any]


class ToolsetMiddleware(AgentMiddleware):
    """Expose and execute business toolsets only after their Skill is read.

    A successful ``read_file`` creates a structured Run activation. Goal
    inheritance is allowed only for the same immutable Goal revision and an
    unchanged SKILL.md hash. Soft task routing ranks inactive recommendations;
    it is not authority to revoke an already verified Goal activation.
    Session history and old ToolMessages are never capability authority.
    """

    state_schema = ToolsetState

    def __init__(self, *, skills_dir: Path, toolsets_by_skill: dict[str, set[str]]) -> None:
        super().__init__()
        self.skills_dir = skills_dir.resolve()
        self.toolsets_by_skill = {key: frozenset(value) for key, value in toolsets_by_skill.items()}
        self._observed_recommendations: set[tuple[str, str]] = set()
        self._observed_activations: set[tuple[str, str]] = set()
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

    def _skill_digest(self, skill_id: str) -> str | None:
        path = (self.skills_dir / skill_id / "SKILL.md").resolve()
        try:
            path.relative_to(self.skills_dir)
            content = path.read_bytes()
        except (OSError, ValueError):
            return None
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def _activation_for_skill(
        self,
        skill_id: str,
        *,
        run_id: str,
        goal_id: str | None,
        goal_revision: int | None,
        tool_call_id: str,
        scope: str = "run",
    ) -> SkillActivation | None:
        if not self._refresh_installed_skill(skill_id):
            return None
        digest = self._skill_digest(skill_id)
        if digest is None:
            return None
        toolsets = sorted(self.toolsets_by_skill.get(skill_id, ()))
        unlocked = sorted(tools_for_toolsets(set(toolsets)))
        activation_digest = hashlib.sha256(
            f"{run_id}:{goal_id or ''}:{goal_revision or ''}:{skill_id}:{digest}".encode()
        ).hexdigest()[:20]
        return SkillActivation(
            activation_id=f"skill-activation-{activation_digest}",
            skill_id=skill_id,
            scope="goal" if scope == "goal" else "run",
            run_id=run_id or "unscoped",
            goal_id=goal_id,
            goal_revision=goal_revision,
            skill_content_sha256=digest,
            toolsets=toolsets,
            unlocked_tools=unlocked,
            source_tool_call_id=tool_call_id or "message-replay",
        )

    def _valid_activations(self, raw: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in raw:
            try:
                activation = SkillActivation.model_validate(item)
            except ValueError:
                continue
            if (
                self._refresh_installed_skill(activation.skill_id)
                and self._skill_digest(activation.skill_id)
                == activation.skill_content_sha256
            ):
                result.append(activation.model_dump(mode="json"))
        return _merge_skill_activations([], result)

    def _activations_from_messages(
        self,
        messages: list[Any],
        *,
        run_id: str,
        goal_id: str | None,
        goal_revision: int | None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for skill_id in self._loaded_skill_ids(messages):
            activation = self._activation_for_skill(
                skill_id,
                run_id=run_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                tool_call_id="message-replay",
            )
            if activation is not None:
                result.append(activation.model_dump(mode="json"))
        return result

    def _loaded_skill_ids(self, messages: list[Any]) -> list[str]:
        """Find successful ``read_file(/skills/<id>/SKILL.md)`` calls."""
        calls: dict[str, str] = {}
        succeeded: set[str] = set()
        for message in messages:
            extra = getattr(message, "additional_kwargs", None)
            if isinstance(extra, dict) and extra.get("puddingclaw_historical") is True:
                continue
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
        run_id = str(context.get("run_id") or "")
        goal_id = str(context.get("goal_id") or "") or None
        goal_revision = context.get("goal_revision")
        persisted = (
            session_manager.get_effective_run_skill_activations(session_id, run_id)
            if session_id and run_id
            else []
        )
        replayed = self._activations_from_messages(
            list(state.get("messages") or []),
            run_id=run_id or "unscoped",
            goal_id=goal_id,
            goal_revision=goal_revision,
        )
        activations = self._valid_activations([*persisted, *replayed])
        active = sorted(
            {str(item.get("skill_id")) for item in activations if item.get("skill_id")}
        )
        return {
            "skill_activations": activations,
            "active_skill_ids": active,
        }

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        context = runtime.context if runtime is not None and isinstance(runtime.context, dict) else {}
        run_id = str(context.get("run_id") or "unscoped")
        replayed = self._activations_from_messages(
            list(state.get("messages") or []),
            run_id=run_id,
            goal_id=str(context.get("goal_id") or "") or None,
            goal_revision=context.get("goal_revision"),
        )
        activations = self._valid_activations(
            [*(state.get("skill_activations") or []), *replayed]
        )
        active = sorted(
            {str(item.get("skill_id")) for item in activations if item.get("skill_id")}
        )
        if (
            activations == list(state.get("skill_activations") or [])
            and active == sorted(state.get("active_skill_ids") or [])
        ):
            return None
        return {"skill_activations": activations, "active_skill_ids": active}

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:
        return handler(self._request_with_capability_manifest(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._request_with_capability_manifest(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        denied = self._denied_tool_message(request)
        if denied is not None:
            return denied
        self._record_legacy_external_lease_use(request)
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
        self._record_legacy_external_lease_use(request)
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
        if session_id and run_id:
            try:
                session_manager.record_run_skill_selection(
                    session_id,
                    run_id,
                    matched.group(1),
                )
                activation = self._activation_for_skill(
                    matched.group(1),
                    run_id=run_id,
                    goal_id=str(context.get("goal_id") or "") or None,
                    goal_revision=context.get("goal_revision"),
                    tool_call_id=str(request.tool_call.get("id") or ""),
                )
                if activation is not None:
                    session_manager.record_run_skill_activation(
                        session_id,
                        run_id,
                        activation.model_dump(mode="json"),
                    )
                    metric_key = (run_id, activation.skill_id)
                    if metric_key not in self._observed_activations:
                        self._observed_activations.add(metric_key)
                        emit_harness_metric(
                            logger,
                            "skill_activated_from_recommendation_count",
                            session_id=session_id,
                            run_id=run_id,
                            skill_id=activation.skill_id,
                        )
                    try:
                        get_stream_writer()(
                            {
                                "type": "skill_activated",
                                "run_id": run_id,
                                "skill_id": activation.skill_id,
                                "scope": activation.scope,
                                "activation_id": activation.activation_id,
                                "toolsets": list(activation.toolsets),
                            }
                        )
                    except (KeyError, RuntimeError):
                        pass
            except ValueError:
                # A late Tool result may race terminalization. The successful
                # read remains in the Session cache, while terminal Run state
                # stays immutable.
                pass

    def _active_skill_ids(self, state: dict[str, Any]) -> list[str]:
        activations = self._valid_activations(list(state.get("skill_activations") or []))
        return sorted(
            {str(item.get("skill_id")) for item in activations if item.get("skill_id")}
        )

    def _allowed_tool_names(self, state: dict[str, Any]) -> set[str]:
        enabled_toolsets: set[str] = set()
        for skill_id in self._active_skill_ids(state):
            enabled_toolsets.update(self.toolsets_by_skill.get(skill_id, ()))
        return set(UNCONDITIONAL_TOOL_NAMES) | set(tools_for_toolsets(enabled_toolsets))

    def _denied_tool_message(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = str(request.tool_call.get("name") or "")
        runtime_context = (
            request.runtime.context
            if request.runtime is not None and isinstance(request.runtime.context, dict)
            else {}
        )
        if (
            str(runtime_context.get("run_kind") or "") == "goal_inspection"
            and not self._inspection_tool_allowed(tool_name)
        ):
            emit_harness_metric(
                logger,
                "goal_inspection_mutation_denied_total",
                session_id=str(runtime_context.get("session_id") or ""),
                run_id=str(runtime_context.get("run_id") or ""),
                tool=tool_name,
            )
            return ToolMessage(
                content=(
                    f"Tool `{tool_name}` is disabled because this is a read-only Goal "
                    "inspection Run. Ask the user to explicitly continue the Goal before "
                    "performing mutations or delegated execution."
                ),
                tool_call_id=str(request.tool_call.get("id") or ""),
                name=tool_name,
                status="error",
            )
        legacy_lease_denied = (
            tool_name in _LEGACY_EXTERNAL_LEASE_TOOLS
            and self._legacy_external_lease_context(request) is None
        )
        if tool_name in self._allowed_tool_names(request.state) and not legacy_lease_denied:
            return None
        if legacy_lease_denied:
            return ToolMessage(
                content=(
                    f"Tool `{tool_name}` is not enabled. This compatibility-only tool requires "
                    "the still-active legacy lease owner; start a fresh external workflow instead."
                ),
                tool_call_id=str(request.tool_call.get("id") or ""),
                name=tool_name,
                status="error",
            )
        providers = sorted(
            skill_id
            for skill_id, toolsets in self.toolsets_by_skill.items()
            if tool_name in tools_for_toolsets(set(toolsets))
            and self._refresh_installed_skill(skill_id)
        )
        recovery = (
            " Read one of these authoritative Skill files first: "
            + ", ".join(f"/skills/{skill_id}/SKILL.md" for skill_id in providers)
            + "."
            if providers
            else " No installed Skill currently declares this tool."
        )
        return ToolMessage(
            content=f"Tool `{tool_name}` is not enabled.{recovery}",
            tool_call_id=str(request.tool_call.get("id") or ""),
            name=tool_name,
            status="error",
        )

    def _visible_tools(self, request: ModelRequest) -> list[Any]:
        allowed = self._allowed_tool_names(request.state)
        legacy_enabled = self._legacy_external_lease_tools_enabled(request)
        context = (
            request.runtime.context
            if request.runtime is not None and isinstance(request.runtime.context, dict)
            else {}
        )
        inspection = str(context.get("run_kind") or "") == "goal_inspection"
        return [
            tool
            for tool in request.tools
            if self._tool_name(tool) in allowed
            and (not inspection or self._inspection_tool_allowed(self._tool_name(tool)))
            and (
                legacy_enabled
                or self._tool_name(tool) not in _LEGACY_EXTERNAL_LEASE_TOOLS
            )
        ]

    @staticmethod
    def _inspection_tool_allowed(tool_name: str) -> bool:
        if tool_name in _INSPECTION_BLOCKED_READISH_TOOLS:
            return False
        descriptor = tool_control_descriptor(tool_name)
        return (
            tool_name in _GOAL_INSPECTION_ALLOWED_TOOLS
            and descriptor is not None
            and descriptor.side_effect == "none"
        )

    @staticmethod
    def _legacy_external_lease_tools_enabled(request: ModelRequest) -> bool:
        """Expose deprecated lease tools only while resuming their owner."""

        return ToolsetMiddleware._legacy_external_lease_context(request) is not None

    @staticmethod
    def _legacy_external_lease_context(
        request: ModelRequest | ToolCallRequest,
    ) -> dict[str, Any] | None:
        """Resolve the active owner that authorizes a compatibility call."""

        context = (
            request.runtime.context
            if request.runtime is not None and isinstance(request.runtime.context, dict)
            else {}
        )
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        goal_id = str(context.get("goal_id") or "")
        goal_revision = context.get("goal_revision")
        if not session_id:
            return None
        try:
            session_manager.audit_legacy_external_leases(
                session_id,
                migrate=True,
            )
        except FileNotFoundError:
            return None
        active_statuses = {"staged", "prepared", "committing", "publishing"}
        leases = [
            *session_manager.list_external_artifact_leases(session_id),
            *session_manager.list_external_directory_leases(session_id),
        ]
        lease = next(
            (
                item
                for item in leases
                if isinstance(item, dict)
                and str(item.get("status") or "") in active_statuses
                and (
                    (run_id and str(item.get("run_id") or "") == run_id)
                    or (
                        goal_id
                        and str(item.get("goal_id") or "") == goal_id
                        and item.get("goal_revision") == goal_revision
                    )
                )
            ),
            None,
        )
        if not isinstance(lease, dict):
            return None
        return {
            "lease_id": str(lease.get("lease_id") or ""),
            "source_run_id": str(lease.get("run_id") or "") or None,
            "source_goal_id": str(lease.get("goal_id") or "") or None,
            "source_goal_revision": lease.get("goal_revision"),
            # New Runs never receive these schemas. Reaching this branch means
            # the Tool call came from a compatibility checkpoint/active owner.
            "from_old_checkpoint": True,
        }

    @staticmethod
    def _record_legacy_external_lease_use(request: ToolCallRequest) -> None:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name not in _LEGACY_EXTERNAL_LEASE_TOOLS:
            return
        context = ToolsetMiddleware._legacy_external_lease_context(request)
        if context is None:
            return
        runtime_context = (
            request.runtime.context
            if request.runtime is not None and isinstance(request.runtime.context, dict)
            else {}
        )
        session_id = str(runtime_context.get("session_id") or "")
        run_id = str(runtime_context.get("run_id") or "")
        record = {
            "tool": tool_name,
            "run_id": run_id or None,
            **context,
        }
        try:
            session_manager.record_legacy_external_lease_tool_use(
                session_id,
                record,
            )
        except FileNotFoundError:
            return
        emit_harness_metric(
            logger,
            "legacy_external_lease_tool_used",
            session_id=session_id,
            run_id=run_id,
            tool=tool_name,
            source_run_id=context.get("source_run_id"),
            from_old_checkpoint=context.get("from_old_checkpoint"),
        )
        collector = get_current_trace_collector()
        if collector is not None:
            collector.add_custom_span(
                "legacy_external_lease_tool_used",
                record,
                span_type="compatibility",
                metadata={
                    "compatibility": {
                        "protocol": "external_lease",
                        "deprecated": True,
                        **record,
                    }
                },
            )

    def _capability_manifest(
        self,
        request: ModelRequest,
        visible_tools: list[Any],
    ) -> CapabilityManifest:
        context = (
            request.runtime.context
            if request.runtime is not None and isinstance(request.runtime.context, dict)
            else {}
        )
        run_id = str(context.get("run_id") or "unscoped")
        active = self._active_skill_ids(request.state)
        recommendations = self._recommended_inactive_skills(
            request,
            active_skill_ids=set(active),
        )
        session_id = str(context.get("session_id") or "")
        for recommendation in recommendations:
            metric_key = (run_id, recommendation.skill_id)
            if metric_key in self._observed_recommendations:
                continue
            self._observed_recommendations.add(metric_key)
            emit_harness_metric(
                logger,
                "skill_recommended_count",
                session_id=session_id,
                run_id=run_id,
                skill_id=recommendation.skill_id,
                source=recommendation.source,
            )
        enabled_toolsets = sorted(
            {
                toolset
                for skill_id in active
                for toolset in self.toolsets_by_skill.get(skill_id, ())
            }
        )
        allowed = sorted(self._tool_name(tool) for tool in visible_tools if self._tool_name(tool))
        mounted = sorted(
            {self._tool_name(tool) for tool in request.tools if self._tool_name(tool)}
        )
        unavailable = [
            {
                "tool": name,
                "reason": (
                    "inspection_run_read_only"
                    if str(context.get("run_kind") or "") == "goal_inspection"
                    and not self._inspection_tool_allowed(name)
                    else
                    "skill_not_activated"
                    if any(
                        name in tools_for_toolsets(set(toolsets))
                        for toolsets in self.toolsets_by_skill.values()
                    )
                    else "policy_unavailable"
                ),
            }
            for name in mounted
            if name not in set(allowed)
        ]
        tool_schema_contracts = sorted(
            (self._tool_schema_contract(tool) for tool in visible_tools),
            key=lambda item: item["name"],
        )
        schema_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    tool_schema_contracts,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode()
            ).hexdigest()
        )
        manifest_digest = hashlib.sha256(
            json.dumps(
                {
                    "skills": active,
                    "toolsets": enabled_toolsets,
                    "allowed": allowed,
                    "unavailable": unavailable,
                    "schema_hash": schema_hash,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:20]
        return CapabilityManifest(
            manifest_id=f"capability-manifest-{manifest_digest}",
            run_id=run_id,
            active_skill_ids=active,
            recommended_inactive_skills=recommendations,
            enabled_toolsets=enabled_toolsets,
            allowed_tool_names=allowed,
            unavailable_tools=unavailable,
            tool_schema_hash=schema_hash,
            created_at=time.time(),
        )

    def _recommended_inactive_skills(
        self,
        request: ModelRequest,
        *,
        active_skill_ids: set[str],
    ) -> list[SkillRecommendation]:
        """Project soft routing candidates without changing capability authority."""

        profile_payload = (
            request.state.get("task_profile")
            if isinstance(request.state.get("task_profile"), dict)
            else None
        )
        context = (
            request.runtime.context
            if request.runtime is not None and isinstance(request.runtime.context, dict)
            else {}
        )
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        persisted = session_manager.get_run_state(session_id, run_id) if session_id and run_id else None
        if isinstance(persisted, dict) and isinstance(persisted.get("task_profile"), dict):
            profile_payload = persisted["task_profile"]
        try:
            profile = RunTaskProfile.model_validate(profile_payload)
        except (TypeError, ValueError):
            profile = None
        source = (
            "semantic_router"
            if profile is not None
            and profile.classifier not in {"deterministic_fallback", "agent_runtime"}
            else profile.classifier
            if profile is not None
            else "task_profile"
        )
        result: list[SkillRecommendation] = []
        for candidate in sorted(
            profile.skill_candidates if profile is not None else [],
            key=lambda item: (not item.explicit, -item.confidence, item.skill_id),
        ):
            if (
                candidate.skill_id in active_skill_ids
                or candidate.skill_id not in self.toolsets_by_skill
                or not self._refresh_installed_skill(candidate.skill_id)
            ):
                continue
            result.append(
                SkillRecommendation(
                    skill_id=candidate.skill_id,
                    confidence=candidate.confidence,
                    evidence=candidate.evidence,
                    source=source,
                    activation_instruction=f"read /skills/{candidate.skill_id}/SKILL.md",
                )
            )
        recommended_ids = {item.skill_id for item in result}
        follow_up_artifact_ids = (
            {
                str(item)
                for item in persisted.get("follow_up_of_artifact_ids") or []
                if str(item)
            }
            if isinstance(persisted, dict)
            else set()
        )
        if follow_up_artifact_ids and session_id:
            for artifact in session_manager.list_delivered_artifacts(
                session_id,
                verify_freshness=True,
                include_inactive=False,
            ):
                if str(artifact.get("artifact_id") or "") not in follow_up_artifact_ids:
                    continue
                for skill_id in artifact.get("source_skill_ids") or []:
                    skill_id = str(skill_id)
                    if (
                        not skill_id
                        or skill_id in active_skill_ids
                        or skill_id in recommended_ids
                        or skill_id not in self.toolsets_by_skill
                        or not self._refresh_installed_skill(skill_id)
                    ):
                        continue
                    result.append(
                        SkillRecommendation(
                            skill_id=skill_id,
                            confidence=0.8,
                            evidence=(
                                "本次追问关联的正式交付物由启用该 Skill 的 Run 生成；"
                                "仅建议重新读取，不继承旧能力。"
                            ),
                            source="durable_artifact",
                            activation_instruction=f"read /skills/{skill_id}/SKILL.md",
                        )
                    )
                    recommended_ids.add(skill_id)
        return result

    def _request_with_capability_manifest(self, request: ModelRequest) -> ModelRequest:
        visible_tools = self._visible_tools(request)
        manifest = self._capability_manifest(request, visible_tools)
        permission_manifest = self._permission_manifest(request, visible_tools)
        # Keep audit identity and soft routing metadata out of the authoritative
        # model-visible boundary. ``run_id`` and ``created_at`` do not change
        # capability or permission semantics, while SkillIntentRouter already
        # projects recommendations next to the current user turn. Persistence
        # and stream events retain the complete manifests for observability.
        audit_payload = manifest.model_dump(mode="json")
        model_payload = manifest.model_dump(
            mode="json",
            exclude={"created_at", "run_id", "recommended_inactive_skills"},
        )
        permission_audit_payload = permission_manifest.model_dump(mode="json")
        permission_model_payload = permission_manifest.model_dump(
            mode="json", exclude={"created_at", "run_id"}
        )
        context = (
            request.runtime.context
            if request.runtime is not None and isinstance(request.runtime.context, dict)
            else {}
        )
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        if session_id and run_id:
            try:
                session_manager.record_run_capability_manifest(
                    session_id,
                    run_id,
                    audit_payload,
                )
                session_manager.record_run_permission_manifest(
                    session_id,
                    run_id,
                    permission_audit_payload,
                )
            except ValueError:
                pass
        try:
            get_stream_writer()({"type": "capability_manifest", **audit_payload})
            get_stream_writer()({"type": "permission_manifest", **permission_audit_payload})
        except (KeyError, RuntimeError):
            pass
        section = (
            "## Current Capability Manifest (authoritative)\n\n"
            "Only tools listed below are callable in this model turn. Soft routing hints do not grant tools.\n\n"
            f"```json\n{json.dumps(model_payload, ensure_ascii=False, sort_keys=True)}\n```"
        )
        section += (
            "\n\n## Current Permission Manifest (authoritative)\n\n"
            "This describes authorization for the current Run only. Historical grants and Evidence "
            "do not grant permission; the Tool Gate makes the final per-call decision.\n\n"
            f"```json\n{json.dumps(permission_model_payload, ensure_ascii=False, sort_keys=True)}\n```"
        )
        updated = request.override(
            tools=visible_tools,
            system_message=append_to_system_message(request.system_message, section),
        )
        return self._request_with_recommendation_hints(
            updated,
            manifest.recommended_inactive_skills,
        )

    @staticmethod
    def _request_with_recommendation_hints(
        request: ModelRequest,
        recommendations: list[SkillRecommendation],
    ) -> ModelRequest:
        """Put missing soft recommendations beside the current user turn."""

        if not recommendations:
            return request
        messages = list(request.messages or [])
        index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if isinstance(messages[index], HumanMessage)
            ),
            None,
        )
        if index is None or not isinstance(messages[index].content, str):
            return request
        original = messages[index]
        missing = [
            item
            for item in recommendations
            if f"/skills/{item.skill_id}/SKILL.md" not in original.content
        ]
        if not missing:
            return request
        instructions = " ".join(
            (
                f"建议先读取 /skills/{item.skill_id}/SKILL.md"
                f"（来源：{item.source}）。建议本身不授予工具权限；"
                "只有成功读取权威 SKILL.md 后，下一模型轮次才会出现对应工具。"
            )
            for item in missing
        )
        messages[index] = original.model_copy(
            update={
                "content": (
                    f"{original.content}\n\n{_CAPABILITY_RECOMMENDATION_MARKER} "
                    f"{instructions}"
                )
            }
        )
        return request.override(messages=messages)

    def _permission_manifest(
        self,
        request: ModelRequest,
        visible_tools: list[Any],
    ) -> PermissionManifest:
        context = (
            request.runtime.context
            if request.runtime is not None and isinstance(request.runtime.context, dict)
            else {}
        )
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "unscoped")
        run = session_manager.get_run_state(session_id, run_id) if session_id and run_id else None
        config_snapshot = run.get("config_snapshot") if isinstance(run, dict) else {}
        permissions = (
            config_snapshot.get("permissions")
            if isinstance(config_snapshot, dict) and isinstance(config_snapshot.get("permissions"), dict)
            else {}
        )
        approval_mode = str(permissions.get("approval_mode") or "strict")
        if approval_mode not in {"strict", "smart"}:
            approval_mode = "strict"
        allowed: list[dict[str, Any]] = []
        hitl_required: list[dict[str, Any]] = []
        for tool in visible_tools:
            name = self._tool_name(tool)
            descriptor = tool_control_descriptor(name)
            if name in _ARGUMENT_DEPENDENT_PERMISSION_TOOLS:
                hitl_required.append(
                    {
                        "tool": name,
                        "approval_scope": "argument_dependent",
                        "reason": "workspace_paths_are_allowed_but_external_paths_require_realtime_gate",
                    }
                )
            elif descriptor is None or descriptor.approval_scope == "none":
                allowed.append({"tool": name, "reason": "policy_allows_without_hitl"})
            else:
                hitl_required.append(
                    {
                        "tool": name,
                        "approval_scope": descriptor.approval_scope,
                        "reason": "arguments_require_realtime_tool_gate_evaluation",
                    }
                )
        grants = session_manager.list_permission_grants(session_id) if session_id else []
        current_bindings = RunPermissionContext.from_config_snapshot(
            config_snapshot if isinstance(config_snapshot, dict) else {}
        ).grant_bindings()
        for grant in grants:
            scope = str(grant.get("scope") or "session")
            metadata = grant.get("metadata")
            grant_run_id = str(metadata.get("run_id") or "") if isinstance(metadata, dict) else ""
            if scope in {"once", "run"} and grant_run_id != run_id:
                continue
            bindings = grant.get("bindings")
            if not isinstance(bindings, dict):
                # Legacy grants without a policy/workspace binding are audit
                # history, not current authority. The runtime Gate likewise
                # refuses to reuse them across a Run boundary.
                continue
            if not PermissionBindingPolicy.equivalent(
                grant_type=str(grant.get("type") or ""),
                scope=scope,
                target_kind=str(grant.get("target_kind") or ""),
                target=str(grant.get("target") or ""),
                left=bindings,
                right=current_bindings,
            ):
                continue
            allowed.append(
                {
                    "grant_type": str(grant.get("type") or ""),
                    "scope": str(grant.get("scope") or ""),
                    "target_kind": str(grant.get("target_kind") or ""),
                    "target": str(grant.get("target") or ""),
                    "capabilities": sorted(str(item) for item in grant.get("capabilities") or []),
                }
            )
        allowed = sorted(
            allowed,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
        hitl_required = sorted(
            hitl_required,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
        boundary = {
            "approval_mode": approval_mode,
            "allowed": allowed,
            "hitl_required": hitl_required,
            "blocked": [],
            "policy_epoch": int(permissions.get("policy_epoch") or 1),
            "policy_version": str(permissions.get("policy_version") or ""),
        }
        digest = hashlib.sha256(
            json.dumps(boundary, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        return PermissionManifest(
            manifest_id=f"permission-manifest-{digest}",
            run_id=run_id,
            created_at=time.time(),
            **boundary,
        )

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", ""))

    @classmethod
    def _tool_schema_contract(cls, tool: Any) -> dict[str, Any]:
        if isinstance(tool, dict):
            schema = tool.get("parameters") or tool.get("args_schema") or {}
            description = str(tool.get("description") or "")
        else:
            args_schema = getattr(tool, "args_schema", None)
            if args_schema is not None and hasattr(args_schema, "model_json_schema"):
                try:
                    schema = args_schema.model_json_schema()
                except Exception:
                    schema = {}
            else:
                schema = getattr(tool, "args", {}) or {}
            description = str(getattr(tool, "description", "") or "")
        return {
            "name": cls._tool_name(tool),
            "description": description,
            "parameters": schema,
        }


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
