"""DeepAgents runtime manager for Agent mode."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import time
import traceback
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from deepagents import (
    DeepAgentState,
    HarnessProfile,
    RubricMiddleware,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.middleware.memory import MemoryMiddleware
from deepagents.middleware.rubric import RUBRIC_GRADER_MESSAGE_SOURCE, GraderResponse
from deepagents.middleware.subagents import SubAgent
from deepagents.middleware.summarization import SummarizationMiddleware as DeepAgentsSummarizationMiddleware
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import (
    ModelCallLimitExceededError,
)
from langchain.agents.middleware.types import (
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    convert_to_messages,
    message_to_dict,
    messages_from_dict,
)
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command
from langgraph.config import get_config, get_stream_writer
from typing_extensions import NotRequired

import config
from analytics.models import get_analytics_model_registry
from graph.attachment_store import attachment_store
from graph.citations import (
    dedupe_sources,
    format_sources_for_model,
    resolve_message_citations,
)
from graph.database_sql_revision_resume import database_sql_revision_resume_registry
from graph.deepagents_prompt_builder import build_deepagents_system_prompt
from graph.dimension_build_resume import dimension_build_resume_registry
from graph.logical_dataset_resume import logical_dataset_resume_registry
from graph.managed_paths import is_managed_resource_path
from graph.middleware_trace_proxy import wrap_middlewares_for_trace
from graph.middlewares.semantic_assets import SemanticAssetsMiddleware
from graph.middlewares.harness_todos import HarnessTodoMiddleware
from graph.middlewares.attachment_edit import AttachmentEditMiddleware
from graph.middlewares.versioned_patch import VersionedPatchMiddleware
from graph.middlewares.skill_intent_router import SkillIntentRouterMiddleware
from graph.middlewares.tool_context_compaction import (
    CONTEXT_METHOD_ARTIFACT_KEY,
    CONTEXT_OUTPUT_ARTIFACT_KEY,
    CONTEXT_POLICY_ARTIFACT_KEY,
    RAW_OUTPUT_ARTIFACT_KEY,
    ToolContextCompactionMiddleware,
    ToolContextConfig,
    tool_context_compaction_service,
)
from graph.middlewares.tool_context_compaction import (
    POLICY_VERSION as TOOL_CONTEXT_POLICY_VERSION,
)
from graph.middlewares.toolset import ToolsetMiddleware, discover_skill_toolsets
from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
from graph.permission_middleware import ExternalFilePermissionMiddleware
from graph.permission_policy import RunPermissionContext
from graph.permission_resume import permission_resume_registry
from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
from graph.session_manager import session_manager
from graph.tool_result_adapter import tool_result_adapter
from graph.trace_collector import TraceCollector, TraceSpan
from harness.coordinators import HarnessRunCoordinator
from harness.artifact_paths import extract_declared_artifact_targets, extract_local_resource_paths
from harness.dependency_setup import dependency_plan_prompt
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.models import (
    GoalRecord,
    GoalStatus,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunTaskProfile,
    RunVerificationContract,
    VerificationActivation,
    VerificationFailureKind,
    VerificationStatus,
)
from harness.rubric_compiler import RunRubricCompiler
from harness.tool_execution import ToolExecutionPipeline
from harness.verification_activations import (
    VerificationActivationMiddleware,
    resolve_published_attachment,
)
from harness.workspace_backends import build_workspace_execution_backend
from knowledge.paths import get_knowledge_root
from llm.model_client import INTERNAL_CALL_MARKER, ModelClientChatModel
from projects.registry import project_registry
from tools import get_all_tools
from tools.package_install import create_install_packages_tool
from tools.toolsets import agent_custom_tool_names

logger = logging.getLogger(__name__)

_INTERNAL_CONTROL_SOURCES = frozenset(
    {
        RUBRIC_GRADER_MESSAGE_SOURCE,
        "puddingclaw_completion_gate",
        "puddingclaw_goal_continuation",
    }
)


def _message_metadata(message: Any) -> tuple[str, str, dict[str, Any]]:
    """Normalize BaseMessage, role dict, and message_to_dict metadata."""

    if isinstance(message, dict):
        nested = message.get("data") if isinstance(message.get("data"), dict) else {}
        role = message.get("role") or message.get("type") or nested.get("role") or nested.get("type")
        name = message.get("name") or nested.get("name")
        extra = message.get("additional_kwargs") or nested.get("additional_kwargs") or {}
    else:
        role = getattr(message, "type", None)
        name = getattr(message, "name", None)
        extra = getattr(message, "additional_kwargs", None) or {}
    return str(role or ""), str(name or ""), dict(extra) if isinstance(extra, dict) else {}


def _is_internal_control_message(message: Any) -> bool:
    role, name, extra = _message_metadata(message)
    if role not in {"human", "user"}:
        return False
    source = str(extra.get("lc_source") or name or "")
    return source in _INTERNAL_CONTROL_SOURCES


def _harness_summary_envelope(session_id: str) -> str:
    """Build a bounded, deterministic control-plane snapshot for summaries."""

    if not session_id:
        return ""
    run = session_manager.get_run_state(session_id)
    goal = session_manager.get_active_goal_state(session_id)
    if not isinstance(goal, dict) and isinstance(run, dict):
        run_goal_id = str(run.get("goal_id") or "")
        if run_goal_id:
            goal = session_manager.get_goal_state(session_id, run_goal_id)
    goal_id = str(goal.get("goal_id") or "") if isinstance(goal, dict) else ""
    revision = int(goal.get("objective_revision") or 1) if isinstance(goal, dict) else None
    todos = session_manager.get_todos(
        session_id,
        goal_id=goal_id or None,
        goal_revision=revision,
        run_id=None if goal_id else str(run.get("run_id") or "") if isinstance(run, dict) else None,
    )
    raw_refs = goal.get("evidence_refs") if isinstance(goal, dict) else []
    raw_gaps = goal.get("gaps") if isinstance(goal, dict) else []
    report = run.get("verification_report") if isinstance(run, dict) else None
    goal_contract = goal.get("goal_contract") if isinstance(goal, dict) else None
    criteria = goal_contract.get("criteria") if isinstance(goal_contract, dict) else []
    evidence_keys = (
        "kind",
        "ref",
        "activation_id",
        "tool_call_id",
        "tool_name",
        "pack",
        "status",
        "source_id",
        "result_id",
        "generation_id",
        "trace_id",
        "artifact_id",
        "target_path",
        "path",
        "uri",
        "output_digest",
        "source_hash",
        "raw_output_ref",
        "run_id",
        "origin_run_id",
        "goal_id",
        "goal_revision",
    )
    permission_grants = session_manager.list_permission_grants(session_id)
    unresolved_todos = [
        item
        for item in todos
        if isinstance(item, dict)
        and str(item.get("status") or "pending") not in {"completed", "cancelled"}
    ]
    terminal_todos = [
        item
        for item in todos
        if isinstance(item, dict)
        and str(item.get("status") or "") in {"completed", "cancelled"}
    ]
    projected_todos = [*unresolved_todos, *terminal_todos[-40:]]
    terminal_todo_digest = hashlib.sha256(
        json.dumps(terminal_todos, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest() if terminal_todos else None
    payload = {
        "schema": "puddingclaw.harness-envelope/v1",
        "goal": {
            "goal_id": goal_id or None,
            "objective_revision": revision,
            "objective": goal.get("objective") if isinstance(goal, dict) else None,
            "status": goal.get("status") if isinstance(goal, dict) else None,
            "round": goal.get("round") if isinstance(goal, dict) else None,
            "max_rounds": goal.get("max_rounds") if isinstance(goal, dict) else None,
            "model_call_count": goal.get("model_call_count") if isinstance(goal, dict) else None,
            "budget_exhaustion_reason": goal.get("budget_exhaustion_reason")
            if isinstance(goal, dict)
            else None,
            "pending_revision": bool(goal.get("pending_revision"))
            if isinstance(goal, dict)
            else False,
            "latest_goal_decision": goal.get("latest_goal_decision")
            if isinstance(goal, dict)
            else None,
        },
        "run": {
            "run_id": run.get("run_id") if isinstance(run, dict) else None,
            "status": run.get("status") if isinstance(run, dict) else None,
            "outcome": run.get("outcome") if isinstance(run, dict) else None,
            "declared_artifact_targets": list(run.get("declared_artifact_targets") or [])
            if isinstance(run, dict)
            else [],
        },
        "todos": [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "content",
                    "status",
                    "position",
                    "parent_id",
                    "created_run_id",
                    "last_changed_run_id",
                )
                if key in item
            }
            for item in projected_todos
            if isinstance(item, dict)
        ],
        "todo_terminal_summary": {
            "completed_count": sum(
                1 for item in terminal_todos if item.get("status") == "completed"
            ),
            "cancelled_count": sum(
                1 for item in terminal_todos if item.get("status") == "cancelled"
            ),
            "sha256": terminal_todo_digest,
            "recent_items_included": min(40, len(terminal_todos)),
        },
        "evidence_refs": [
            {key: item.get(key) for key in evidence_keys if item.get(key) is not None}
            for item in (raw_refs if isinstance(raw_refs, list) else [])
            if isinstance(item, dict)
        ],
        "verification_contract": {
            "contract_id": goal_contract.get("contract_id"),
            "version": goal_contract.get("version"),
            "criteria": [
                {
                    "id": item.get("id"),
                    "required": item.get("required"),
                    "verifier": item.get("verifier"),
                }
                for item in criteria
                if isinstance(item, dict)
            ],
        }
        if isinstance(goal_contract, dict)
        else None,
        "known_gaps": [
            str(item)
            for item in (raw_gaps if isinstance(raw_gaps, list) else [])
            if str(item).strip()
        ],
        "control_notices": [
            str(item)
            for item in (
                goal.get("control_notices")
                if isinstance(goal, dict) and isinstance(goal.get("control_notices"), list)
                else []
            )
            if str(item).strip()
        ],
        "active_permissions": [
            {
                key: item.get(key)
                for key in ("id", "grant_type", "target_kind", "target", "capabilities", "scope")
                if item.get(key) is not None
            }
            for item in permission_grants[-30:]
            if isinstance(item, dict)
        ],
        "latest_verification": {
            "status": report.get("status"),
            "gaps": list(report.get("gaps") or []),
        }
        if isinstance(report, dict)
        else None,
    }
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    return (
        "\n\n<HARNESS_ENVELOPE authoritative=\"true\">\n"
        "该块由 Harness 从 Session JSON 确定性生成；Goal、Todo、产物、证据与缺口"
        "以此为准，不得被上方自然语言摘要覆盖。\n"
        f"{serialized}\n"
        "</HARNESS_ENVELOPE>"
    )


_HARNESS_ENVELOPE_RE = re.compile(
    r"\s*<\s*HARNESS_ENVELOPE\b[^>]*>.*?<\s*/\s*HARNESS_ENVELOPE\s*>\s*",
    flags=re.IGNORECASE | re.DOTALL,
)
_HARNESS_ENVELOPE_OPEN_RE = re.compile(
    r"<\s*HARNESS_ENVELOPE\b",
    flags=re.IGNORECASE,
)
_HARNESS_ENVELOPE_CLOSE_RE = re.compile(
    r"<\s*/\s*HARNESS_ENVELOPE\b[^>]*>?",
    flags=re.IGNORECASE,
)


def _strip_untrusted_harness_envelopes(summary: str) -> str:
    """Remove model-authored envelope lookalikes before appending authority."""

    cleaned = _HARNESS_ENVELOPE_RE.sub("\n", str(summary or ""))
    unmatched_open = _HARNESS_ENVELOPE_OPEN_RE.search(cleaned)
    if unmatched_open is not None:
        cleaned = cleaned[: unmatched_open.start()]
    cleaned = _HARNESS_ENVELOPE_CLOSE_RE.sub("", cleaned)
    return cleaned.strip()


class PuddingClawSummarizationMiddleware(DeepAgentsSummarizationMiddleware):
    """DeepAgents history offload configured independently from legacy Chat."""

    @property
    def name(self) -> str:
        return "PuddingClawSummarizationMiddleware"

    @staticmethod
    def _without_internal_controls(messages: list[Any]) -> list[Any]:
        return [message for message in messages if not _is_internal_control_message(message)]

    def _filter_summary_messages(self, messages: list[Any]) -> list[Any]:
        filtered = super()._filter_summary_messages(messages)
        return self._without_internal_controls(filtered)

    def _create_summary(self, messages_to_summarize: list[Any]) -> str:
        writer = None
        session_id = ""
        try:
            runnable_config = get_config()
            configurable = runnable_config.get("configurable", {})
            session_id = str(configurable.get("session_id") or "")
            writer = get_stream_writer()
            writer(
                {
                    "type": "context_maintenance",
                    "status": "start",
                    "phase": "global_summarization",
                    "message": "正在执行全局上下文压缩，并保留 Goal、Todo、产物与验收缺口…",
                }
            )
        except RuntimeError:
            writer = None
        summary = super()._create_summary(self._without_internal_controls(messages_to_summarize))
        summary = _strip_untrusted_harness_envelopes(summary)
        summary += _harness_summary_envelope(session_id)
        if writer is not None:
            writer(
                {
                    "type": "context_maintenance",
                    "status": "done",
                    "phase": "global_summarization_done",
                    "message": "全局上下文压缩完成，Harness 状态已保留。",
                }
            )
        return summary

    async def _acreate_summary(self, messages_to_summarize: list[Any]) -> str:
        writer = None
        session_id = ""
        try:
            runnable_config = get_config()
            configurable = runnable_config.get("configurable", {})
            session_id = str(configurable.get("session_id") or "")
            writer = get_stream_writer()
            writer(
                {
                    "type": "context_maintenance",
                    "status": "start",
                    "phase": "global_summarization",
                    "message": "正在执行全局上下文压缩，并保留 Goal、Todo、产物与验收缺口…",
                }
            )
        except RuntimeError:
            writer = None
        summary = await super()._acreate_summary(
            self._without_internal_controls(messages_to_summarize)
        )
        summary = _strip_untrusted_harness_envelopes(summary)
        summary += _harness_summary_envelope(session_id)
        if writer is not None:
            writer(
                {
                    "type": "context_maintenance",
                    "status": "done",
                    "phase": "global_summarization_done",
                    "message": "全局上下文压缩完成，Harness 状态已保留。",
                }
            )
        return summary


# ModelClientChatModel is a pre-built wrapper whose provider key resolves to
# ``modelclientchatmodel``. Remove DeepAgents' automatic 170K fallback only
# for this wrapper; legacy Chat does not use create_deep_agent and is untouched.
register_harness_profile(
    "modelclientchatmodel",
    HarnessProfile(
        excluded_middleware=frozenset(
            {"SummarizationMiddleware", "TodoListMiddleware"}
        ),
        extra_middleware=lambda: [HarnessTodoMiddleware()],
        tool_description_overrides={
            "edit_file": (
                "Deprecated by PuddingClaw Harness: direct exact-string editing is disabled. "
                "Call inspect_file_version and then patch_file with expected_sha256."
            )
        },
    ),
)


def _build_deepagents_summarization(model: BaseChatModel, backend: Any) -> PuddingClawSummarizationMiddleware | None:
    cfg = config.get_deepagents_summarization_config()
    if not cfg.get("enabled", True):
        return None
    return PuddingClawSummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=("tokens", max(1, int(cfg.get("trigger_tokens", 160000)))),
        keep=("messages", max(1, int(cfg.get("keep_messages", 20)))),
        trim_tokens_to_summarize=max(1, int(cfg.get("summary_input_tokens", 800000))),
        truncate_args_settings=None,
    )


def _effective_agent_messages(state: dict[str, Any]) -> list[Any]:
    """Return the messages DeepAgents actually presents after summarization."""
    messages = list(state.get("messages") or [])
    event = state.get("_summarization_event")
    if not isinstance(event, dict):
        return messages
    summary_message = event.get("summary_message")
    cutoff_index = event.get("cutoff_index")
    if summary_message is None or not isinstance(cutoff_index, int):
        return messages
    return [summary_message, *messages[max(0, cutoff_index) :]]


def _estimate_agent_context_tokens(messages: list[Any], system_prompt: str) -> int:
    """Estimate the current effective Agent context for the UI meter."""
    try:
        message_tokens = int(count_tokens_approximately(messages))
    except Exception:
        message_tokens = sum(max(0, len(str(getattr(msg, "content", ""))) // 4) for msg in messages)
    system_tokens = max(0, len(system_prompt) // 4)
    return message_tokens + system_tokens


def _serialize_agent_context_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Serialize model-only context without duplicating raw Tool evidence."""

    serialized: list[dict[str, Any]] = []
    for message in messages:
        if _is_internal_control_message(message):
            continue
        payload = message_to_dict(message)
        data = payload.get("data")
        artifact = data.get("artifact") if isinstance(data, dict) else None
        if isinstance(artifact, dict) and RAW_OUTPUT_ARTIFACT_KEY in artifact:
            sanitized_artifact = dict(artifact)
            sanitized_artifact.pop(RAW_OUTPUT_ARTIFACT_KEY, None)
            data["artifact"] = sanitized_artifact or None
        serialized.append(payload)
    return serialized


class PuddingClawAgentState(DeepAgentState):
    """DeepAgent state extended with the UI-selected analytics model."""

    analytics_model_id: NotRequired[str | None]
    semantic_assets_model_id: NotRequired[str]
    semantic_assets_metadata: NotRequired[list[dict[str, Any]]]
    allowed_semantic_asset_ids: NotRequired[list[str]]
    tool_context_enqueue: NotRequired[bool]
    rubric: NotRequired[str]
    task_profile: NotRequired[dict[str, Any]]
    verification_contract: NotRequired[dict[str, Any]]
    verification_activations: NotRequired[Annotated[list[dict[str, Any]], PrivateStateAttr]]
    _verification_attempts: NotRequired[Annotated[int, PrivateStateAttr]]
    _completion_gate_iterations: NotRequired[Annotated[int, PrivateStateAttr]]
    _completion_gate_status: NotRequired[Annotated[str | None, PrivateStateAttr]]
    _deterministic_evaluations: NotRequired[Annotated[list[dict[str, Any]], PrivateStateAttr]]
    _run_query_id: NotRequired[Annotated[str, PrivateStateAttr]]
    _run_objective: NotRequired[Annotated[str, PrivateStateAttr]]
    _goal_verification_context: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]
    run_model_call_count: NotRequired[Annotated[int, PrivateStateAttr]]
    thread_model_call_count: NotRequired[Annotated[int, PrivateStateAttr]]
    _model_call_limit_exceeded: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


class PuddingClawRubricMiddleware(RubricMiddleware):
    """Run deterministic completion gates before invoking the LLM grader."""

    _JSON_GRADER_SUFFIX = """

Return exactly one JSON object and no markdown. Do not call tools or functions.
The object must follow this shape:
{
  "result": "satisfied" | "needs_revision" | "failed",
  "explanation": "one or two sentence verdict",
  "criteria": [
    {"name": "criterion id or statement", "passed": true},
    {"name": "criterion id or statement", "passed": false, "gap": "missing evidence"}
  ]
}
Use "satisfied" only when every required criterion passes.
Return one criteria item for every required rubric criterion. Write explanation
and gaps in Chinese. Missing criteria are treated as failed by Harness.
""".strip()

    @staticmethod
    def _response_text(response: Any) -> str:
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content or "")

    @classmethod
    def _parse_grader_response(cls, response: Any) -> GraderResponse:
        text = cls._response_text(response).strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, count=1)
            text = re.sub(r"\s*```$", "", text, count=1)
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Rubric grader did not return a JSON object.")
        payload = json.loads(text[start : end + 1])
        return GraderResponse.model_validate(payload)

    def _plain_grader_model(self) -> BaseChatModel:
        model = getattr(self, "_pudding_plain_grader_model", None)
        if model is not None:
            return model
        from deepagents._models import resolve_model

        model = resolve_model(self._model)
        self._pudding_plain_grader_model = model
        return model

    @staticmethod
    def _prior_goal_candidate_excerpt(
        messages: list[dict[str, Any]],
        *,
        query_id: str,
        max_chars: int = 1800,
    ) -> str:
        """Return the durable candidate text for one prior Goal Run.

        Session display messages are not authority for completion, but their
        content is required to grade pure-text criteria across Runs.  The Run
        report and provenance decide whether the excerpt can be accepted.
        """

        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if str(message.get("query_id") or "") != query_id:
                continue
            if str(message.get("role") or "") != "assistant":
                continue
            segments = message.get("segments")
            if isinstance(segments, list):
                for segment in reversed(segments):
                    if not isinstance(segment, dict):
                        continue
                    content = str(segment.get("content") or "").strip()
                    if content:
                        return content[:max_chars]
            content = str(message.get("content") or "").strip()
            if content:
                return content[:max_chars]
        return ""

    @staticmethod
    def _runtime_run_scope_update(state: Any, runtime: Any) -> dict[str, Any]:
        """Copy trusted Run identity from runtime context into graph state.

        ``PrivateStateAttr`` fields are deliberately rejected from graph input,
        so they must be initialized by middleware after invocation begins.
        """

        runtime_context = getattr(runtime, "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        update: dict[str, Any] = {}
        query_id = context.get("query_id")
        objective = context.get("run_objective")
        if isinstance(query_id, str) and query_id and state.get("_run_query_id") != query_id:
            update["_run_query_id"] = query_id
        if isinstance(objective, str) and objective and state.get("_run_objective") != objective:
            update["_run_objective"] = objective
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        if session_id and run_id:
            persisted_run = session_manager.get_run_state(session_id, run_id)
            goal_id = str(persisted_run.get("goal_id") or "") if isinstance(persisted_run, dict) else ""
            goal_revision = (
                int(persisted_run.get("goal_revision") or 1)
                if isinstance(persisted_run, dict)
                else 1
            )
            persisted_goal = (
                session_manager.get_goal_state(session_id, goal_id)
                if goal_id
                else None
            )
            if (
                isinstance(persisted_goal, dict)
                and int(persisted_goal.get("objective_revision") or 1) == goal_revision
            ):
                raw_refs = persisted_goal.get("evidence_refs")
                raw_gaps = persisted_goal.get("gaps")
                run_ids = persisted_goal.get("run_ids")
                prior_runs: list[dict[str, Any]] = []
                prior_candidates: list[dict[str, Any]] = []
                display_messages = session_manager.load_session(session_id)
                if isinstance(run_ids, list):
                    for prior_run_id in run_ids:
                        if str(prior_run_id) == run_id:
                            continue
                        prior = session_manager.get_run_state(session_id, str(prior_run_id))
                        if not isinstance(prior, dict):
                            continue
                        if (
                            str(prior.get("goal_id") or "") != goal_id
                            or int(prior.get("goal_revision") or 1) != goal_revision
                        ):
                            continue
                        prior_report = prior.get("verification_report")
                        prior_runs.append(
                            {
                                "run_id": prior.get("run_id"),
                                "status": prior.get("status"),
                                "outcome": prior.get("outcome"),
                                "model_call_count": prior.get("model_call_count"),
                                "verification": {
                                    "status": prior_report.get("status"),
                                    "report_id": prior_report.get("report_id"),
                                    "accepted_for_goal_revision": prior_report.get(
                                        "accepted_for_goal_revision"
                                    ),
                                    "evaluations": [
                                        {
                                            "criterion_id": item.get("criterion_id"),
                                            "passed": item.get("passed"),
                                            "verifier": item.get("verifier"),
                                            "gap": item.get("gap"),
                                        }
                                        for item in prior_report.get("evaluations") or []
                                        if isinstance(item, dict)
                                    ],
                                }
                                if isinstance(prior_report, dict)
                                else None,
                            }
                        )
                        prior_query_id = str(prior.get("query_id") or "")
                        excerpt = PuddingClawRubricMiddleware._prior_goal_candidate_excerpt(
                            display_messages,
                            query_id=prior_query_id,
                        )
                        if excerpt:
                            prior_candidates.append(
                                {
                                    "run_id": prior.get("run_id"),
                                    "query_id": prior_query_id,
                                    "candidate_excerpt": excerpt,
                                    "candidate_only": True,
                                }
                            )
                update["_goal_verification_context"] = {
                    "goal_id": goal_id,
                    "objective_revision": goal_revision,
                    "objective": persisted_goal.get("objective"),
                    "evidence_refs": [
                        {
                            key: item.get(key)
                            for key in (
                                "kind",
                                "ref",
                                "activation_id",
                                "tool_call_id",
                                "tool_name",
                                "source_id",
                                "result_id",
                                "artifact_id",
                                "target_path",
                                "path",
                                "uri",
                                "output_digest",
                                "source_hash",
                                "raw_output_ref",
                                "origin_run_id",
                                "run_id",
                                "goal_id",
                                "goal_revision",
                            )
                            if item.get(key) is not None
                        }
                        for item in (raw_refs if isinstance(raw_refs, list) else [])
                        if isinstance(item, dict)
                    ],
                    "todos": session_manager.get_todos(
                        session_id,
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                    ),
                    "known_gaps": [
                        str(item)
                        for item in (raw_gaps if isinstance(raw_gaps, list) else [])
                        if str(item).strip()
                    ],
                    "prior_runs": prior_runs,
                    "prior_run_candidates": prior_candidates,
                    "latest_goal_decision": persisted_goal.get("latest_goal_decision"),
                }
        return update

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        update = dict(super().before_agent(state, runtime) or {})
        update.update(self._runtime_run_scope_update(state, runtime))
        return update or None

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        update = dict(await super().abefore_agent(state, runtime) or {})
        update.update(self._runtime_run_scope_update(state, runtime))
        return update or None

    @staticmethod
    def _is_external_user_message(message: Any) -> bool:
        role, name, extra = _message_metadata(message)
        if role not in {"human", "user"}:
            return False
        source = str(extra.get("lc_source") or "")
        if source:
            return False
        return str(name or "") not in {
            RUBRIC_GRADER_MESSAGE_SOURCE,
            "puddingclaw_completion_gate",
        }

    @classmethod
    def _run_message_start(cls, state: Any, messages: list[Any]) -> int | None:
        run_query_id = state.get("_run_query_id")
        if isinstance(run_query_id, str) and run_query_id:
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                role, _, extra = _message_metadata(message)
                if role not in {"human", "user"}:
                    continue
                if extra.get("puddingclaw_query_id") == run_query_id:
                    return index
        # Fail closed to the latest real user turn. Rubric revision prompts are
        # HumanMessages too, while summaries carry an internal source marker.
        for index in range(len(messages) - 1, -1, -1):
            if cls._is_external_user_message(messages[index]):
                return index
        return None

    @staticmethod
    def _is_summary_message(message: Any) -> bool:
        _, _, extra = _message_metadata(message)
        return extra.get("lc_source") == "summarization"

    @classmethod
    def _scoped_run_messages(cls, state: Any, messages: list[Any]) -> list[Any]:
        start = cls._run_message_start(state, messages)
        if start is not None:
            scoped = messages[start:]
            _, _, first_extra = _message_metadata(scoped[0])
            if first_extra.get("lc_source") == "puddingclaw_goal_continuation":
                objective = state.get("_run_objective")
                if isinstance(objective, str) and objective:
                    return [
                        HumanMessage(
                            content=objective,
                            name="puddingclaw_run_objective",
                            additional_kwargs={"lc_source": "puddingclaw_run_objective"},
                        ),
                        *scoped[1:],
                    ]
            return scoped

        # A long current Run can summarize away its opening user message. The
        # objective is durable runtime metadata, so reconstruct the grader view
        # from it and retain only messages after the latest summary boundary.
        objective = state.get("_run_objective")
        if isinstance(objective, str) and objective:
            tail: list[Any] = []
            for index in range(len(messages) - 1, -1, -1):
                if cls._is_summary_message(messages[index]):
                    tail = messages[index + 1 :]
                    break
            if not tail:
                # With no trustworthy message boundary, do not re-admit the
                # whole session. The last AI answer is enough for the LLM grader;
                # deterministic checks own current-Run tool evidence.
                tail = next(
                    ([message] for message in reversed(messages) if isinstance(message, AIMessage)),
                    [],
                )
            return [
                HumanMessage(
                    content=objective,
                    name="puddingclaw_run_objective",
                    additional_kwargs={"lc_source": "puddingclaw_run_objective"},
                ),
                *tail,
            ]
        return messages

    def _build_grader_payload(self, state: Any, iteration: int) -> str:
        """Grade only messages produced for the current Harness Run.

        The main agent intentionally receives cross-Run session context, but a
        completion verdict belongs to one Run.  DeepAgents' default rubric
        payload grades the entire transcript, which can make an earlier user
        request contaminate the current verdict. Run identity comes from trusted
        runtime context and remains stable across revision and summary loops.
        """

        messages = list(state.get("messages") or [])
        scoped_state = dict(state)
        scoped_messages = self._scoped_run_messages(state, messages)
        normalized_messages: list[Any] = []
        for message in scoped_messages:
            if not isinstance(message, dict):
                normalized_messages.append(message)
            elif isinstance(message.get("data"), dict) and message.get("type"):
                normalized_messages.extend(messages_from_dict([message]))
            else:
                normalized_messages.extend(convert_to_messages([message]))
        scoped_state["messages"] = normalized_messages
        payload = super()._build_grader_payload(scoped_state, iteration)
        goal_context = state.get("_goal_verification_context")
        if not isinstance(goal_context, dict) or not goal_context.get("goal_id"):
            return payload
        # Goal verification is not transcript replay.  Append a bounded,
        # deterministic aggregate of authoritative cross-Run evidence while
        # keeping the natural-language conversation scoped to this Run.
        serialized = json.dumps(goal_context, ensure_ascii=False, default=str)
        return (
            f"{payload}\n\n"
            "<goal_aggregate_verification_context>\n"
            "以下内容来自 Session JSON 的当前 Goal 修订版，是跨 Run 验收证据索引，"
            "不是新的用户消息，也不能替代缺失的证据。允许用它确认先前 Run 已完成且"
            "仍属于当前 Goal 修订版的工作。\n"
            f"{serialized}\n"
            "</goal_aggregate_verification_context>"
        )

    def _grade(self, state: Any, iteration: int) -> GraderResponse:
        payload = self._build_grader_payload(state, iteration)
        response = self._plain_grader_model().invoke(
            [
                SystemMessage(content=f"{self._system_prompt}\n\n{self._JSON_GRADER_SUFFIX}"),
                HumanMessage(content=payload),
            ]
        )
        return self._parse_grader_response(response)

    async def _agrade(self, state: Any, iteration: int) -> GraderResponse:
        payload = self._build_grader_payload(state, iteration)
        response = await self._plain_grader_model().ainvoke(
            [
                SystemMessage(content=f"{self._system_prompt}\n\n{self._JSON_GRADER_SUFFIX}"),
                HumanMessage(content=payload),
            ]
        )
        return self._parse_grader_response(response)

    @staticmethod
    def _last_ai_text(state: dict[str, Any]) -> str:
        for message in reversed(list(state.get("messages") or [])):
            if isinstance(message, AIMessage):
                content = message.content
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "\n".join(
                        str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item)
                        for item in content
                    )
        return ""

    def _completion_gate_update(
        self,
        state: dict[str, Any],
        runtime: Any,
        *,
        attempt: int | None = None,
    ) -> dict[str, Any] | None:
        payload = state.get("verification_contract")
        if not isinstance(payload, dict):
            return None
        contract = RunVerificationContract.model_validate(payload)
        runtime_context = getattr(runtime, "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        check_state = dict(state)
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        goal_evidence_refs: list[dict[str, Any]] = []
        persisted_run = session_manager.get_run_state(session_id, run_id) if session_id and run_id else None
        persisted_activations = (
            persisted_run.get("verification_activations")
            if isinstance(persisted_run, dict)
            else None
        )
        goal_id = str(persisted_run.get("goal_id") or "") if isinstance(persisted_run, dict) else ""
        goal_revision = persisted_run.get("goal_revision") if isinstance(persisted_run, dict) else None
        run_objective = str(persisted_run.get("objective") or "") if isinstance(persisted_run, dict) else ""
        execution = (
            persisted_run.get("config_snapshot", {}).get("execution", {})
            if isinstance(persisted_run, dict)
            and isinstance(persisted_run.get("config_snapshot"), dict)
            else {}
        )
        if goal_id:
            persisted_goal = session_manager.get_goal_state(session_id, goal_id)
            raw_goal_refs = persisted_goal.get("evidence_refs") if isinstance(persisted_goal, dict) else None
            if isinstance(raw_goal_refs, list):
                goal_evidence_refs = [dict(item) for item in raw_goal_refs if isinstance(item, dict)]
        check_state["_harness_context"] = {
            "todos": list(state.get("todos") or []),
            "final_content": self._last_ai_text(state),
            "workspace_path": str(context.get("workspace_path") or ""),
            "run_id": run_id,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "workspace_id": str(execution.get("workspace_id") or ""),
            "backend_id": str(execution.get("backend_id") or ""),
            "declared_artifact_targets": list(
                persisted_run.get("declared_artifact_targets")
                if isinstance(persisted_run, dict)
                and isinstance(persisted_run.get("declared_artifact_targets"), list)
                else extract_declared_artifact_targets(run_objective)
            ),
            "active_permission_grant_ids": [
                str(item.get("id"))
                for item in session_manager.list_permission_grants(session_id)
                if item.get("id")
            ] if session_id else [],
            "permission_grants_authoritative": bool(session_id),
            "verification_activations": list(
                persisted_activations
                if isinstance(persisted_activations, list)
                else state.get("verification_activations") or []
            ),
            "goal_evidence_refs": goal_evidence_refs,
        }
        evaluations = evaluate_deterministic_criteria(contract, check_state)
        required_by_id = {criterion.id: criterion.required for criterion in contract.criteria}
        failures = [item for item in evaluations if not item.passed and required_by_id.get(item.criterion_id, True)]
        infrastructure_failures = [
            item
            for item in failures
            if item.failure_kind == VerificationFailureKind.INFRASTRUCTURE_ERROR
        ]
        previous_attempts = max(
            int(state.get("_verification_attempts") or 0),
            int(state.get("_completion_gate_iterations") or 0),
            int(state.get("_rubric_iterations") or 0),
        )
        current_attempt = attempt if attempt is not None else previous_attempts + 1
        gate_status = "satisfied"
        if infrastructure_failures:
            gate_status = VerificationStatus.INFRASTRUCTURE_ERROR.value
        elif failures:
            gate_status = (
                "max_iterations_reached"
                if current_attempt >= self.max_iterations
                else "needs_revision"
            )
        writer = getattr(runtime, "stream_writer", None)
        if writer is not None:
            writer(
                {
                    "type": "deterministic_checks_completed",
                    "iteration": current_attempt,
                    "attempt": current_attempt,
                    "status": gate_status,
                    "evaluations": [item.model_dump(mode="json") for item in evaluations],
                }
            )
        update: dict[str, Any] = {
            "_deterministic_evaluations": [item.model_dump(mode="json") for item in evaluations],
            "_verification_attempts": current_attempt,
            "_completion_gate_iterations": current_attempt,
            "_completion_gate_status": gate_status,
        }
        if not failures:
            return update
        if infrastructure_failures:
            update["_rubric_status"] = VerificationStatus.INFRASTRUCTURE_ERROR.value
            return update
        if current_attempt >= self.max_iterations:
            update["_rubric_status"] = "max_iterations_reached"
            return update
        feedback = [
            "Harness 的确定性完成检查尚未通过，请先修正后再结束：",
            *[f"- {item.criterion_id}: {item.gap or '缺少可验证证据'}" for item in failures],
        ]
        update["messages"] = [
            HumanMessage(
                content="\n".join(feedback),
                name="puddingclaw_completion_gate",
                additional_kwargs={"lc_source": "puddingclaw_completion_gate"},
            )
        ]
        update["jump_to"] = "model"
        return update

    @staticmethod
    def _effective_contract_update(
        state: dict[str, Any],
        runtime: Any,
    ) -> dict[str, Any]:
        runtime_context = getattr(runtime, "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        query_id = str(context.get("query_id") or "")
        if not all((session_id, run_id, query_id)):
            return {}
        persisted = session_manager.get_run_state(session_id, run_id)
        if not isinstance(persisted, dict):
            return {}
        if persisted.get("verification_enabled") is False:
            return {}
        profile_payload = persisted.get("task_profile")
        profile = RunTaskProfile.model_validate(profile_payload or {})
        raw_activations = persisted.get("verification_activations")
        activations = (
            [
                VerificationActivation.model_validate(item)
                for item in raw_activations
                if isinstance(item, dict) and item.get("query_id") == query_id
            ]
            if isinstance(raw_activations, list)
            else []
        )
        contract_payload = persisted.get("verification_contract") or persisted.get("declared_verification_contract")
        contract = (
            RunVerificationContract.model_validate(contract_payload) if isinstance(contract_payload, dict) else None
        )
        effective = RunRubricCompiler.expand_for_activations(
            contract=contract,
            profile=profile,
            message=str(persisted.get("objective") or ""),
            activations=activations,
        )
        if effective is None:
            return {
                "task_profile": profile.model_dump(mode="json"),
                "verification_activations": [item.model_dump(mode="json") for item in activations],
            }
        changed = contract is None or (effective.model_dump(mode="json") != contract.model_dump(mode="json"))
        if changed:
            session_manager.update_run_verification_contract(
                session_id,
                run_id,
                effective.model_dump(mode="json"),
            )
            writer = getattr(runtime, "stream_writer", None)
            if writer is not None:
                writer(
                    {
                        "type": "verification_contract_updated",
                        "run_id": run_id,
                        "query_id": query_id,
                        "contract": effective.model_dump(mode="json"),
                    }
                )
        return {
            "task_profile": profile.model_dump(mode="json"),
            "verification_contract": effective.model_dump(mode="json"),
            "verification_activations": [item.model_dump(mode="json") for item in activations],
            "rubric": effective.rubric,
        }

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        effective_update = self._effective_contract_update(dict(state), runtime)
        effective_state = {**dict(state), **effective_update}
        if not isinstance(effective_state.get("verification_contract"), dict):
            return super().after_agent(effective_state, runtime)
        previous_attempts = max(
            int(effective_state.get("_verification_attempts") or 0),
            int(effective_state.get("_completion_gate_iterations") or 0),
            int(effective_state.get("_rubric_iterations") or 0),
        )
        if previous_attempts >= self.max_iterations:
            return {
                **effective_update,
                "_verification_attempts": previous_attempts,
                "_completion_gate_status": "max_iterations_reached",
                "_rubric_status": "max_iterations_reached",
            }
        attempt = previous_attempts + 1
        effective_state["_rubric_iterations"] = previous_attempts
        gate_update = self._completion_gate_update(
            effective_state,
            runtime,
            attempt=attempt,
        )
        if gate_update and gate_update.get("jump_to") == "model":
            return {**effective_update, **gate_update}
        if gate_update and gate_update.get("_completion_gate_status") == VerificationStatus.INFRASTRUCTURE_ERROR.value:
            return {**effective_update, **gate_update}
        grading_state = {**effective_state, **(gate_update or {})}
        rubric_update = super().after_agent(grading_state, runtime)
        if gate_update and gate_update.get("_completion_gate_status") == "max_iterations_reached":
            rubric_update = dict(rubric_update or {})
            rubric_update.pop("jump_to", None)
            rubric_update.pop("messages", None)
            if rubric_update.get("_rubric_status") != "grader_error":
                rubric_update["_rubric_status"] = "max_iterations_reached"
        return {
            **effective_update,
            **(gate_update or {}),
            **(rubric_update or {}),
        } or None

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(
        self,
        state: Any,
        runtime: Any,
    ) -> dict[str, Any] | None:
        effective_update = self._effective_contract_update(dict(state), runtime)
        effective_state = {**dict(state), **effective_update}
        if not isinstance(effective_state.get("verification_contract"), dict):
            return await super().aafter_agent(effective_state, runtime)
        previous_attempts = max(
            int(effective_state.get("_verification_attempts") or 0),
            int(effective_state.get("_completion_gate_iterations") or 0),
            int(effective_state.get("_rubric_iterations") or 0),
        )
        if previous_attempts >= self.max_iterations:
            return {
                **effective_update,
                "_verification_attempts": previous_attempts,
                "_completion_gate_status": "max_iterations_reached",
                "_rubric_status": "max_iterations_reached",
            }
        attempt = previous_attempts + 1
        effective_state["_rubric_iterations"] = previous_attempts
        gate_update = self._completion_gate_update(
            effective_state,
            runtime,
            attempt=attempt,
        )
        if gate_update and gate_update.get("jump_to") == "model":
            return {**effective_update, **gate_update}
        if gate_update and gate_update.get("_completion_gate_status") == VerificationStatus.INFRASTRUCTURE_ERROR.value:
            return {**effective_update, **gate_update}
        grading_state = {**effective_state, **(gate_update or {})}
        rubric_update = await super().aafter_agent(grading_state, runtime)
        if gate_update and gate_update.get("_completion_gate_status") == "max_iterations_reached":
            rubric_update = dict(rubric_update or {})
            rubric_update.pop("jump_to", None)
            rubric_update.pop("messages", None)
            if rubric_update.get("_rubric_status") != "grader_error":
                rubric_update["_rubric_status"] = "max_iterations_reached"
        return {
            **effective_update,
            **(gate_update or {}),
            **(rubric_update or {}),
        } or None


class ObservableModelCallLimitMiddleware(ModelCallLimitMiddleware):
    """Model-call circuit breaker that exposes a typed Harness event."""

    def _limit_detail(self, state: Any) -> dict[str, Any] | None:
        thread_count = int(state.get("thread_model_call_count") or 0)
        run_count = int(state.get("run_model_call_count") or 0)
        thread_exceeded = self.thread_limit is not None and thread_count >= self.thread_limit
        run_exceeded = self.run_limit is not None and run_count >= self.run_limit
        if not thread_exceeded and not run_exceeded:
            return None
        reason = "run_model_call_limit" if run_exceeded else "thread_model_call_limit"
        return {
            "reason": reason,
            "run_count": run_count,
            "run_limit": self.run_limit,
            "thread_count": thread_count,
            "thread_limit": self.thread_limit,
        }

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        detail = self._limit_detail(state)
        if detail is None:
            return None
        writer = getattr(runtime, "stream_writer", None)
        if writer is not None:
            writer({"type": "model_call_limit_exceeded", **detail})
        update = super().before_model(state, runtime)
        if update is not None:
            return {**update, "_model_call_limit_exceeded": detail}
        return update

    @hook_config(can_jump_to=["end"])
    async def abefore_model(
        self,
        state: Any,
        runtime: Any,
    ) -> dict[str, Any] | None:
        return self.before_model(state, runtime)


HISTORICAL_TOOL_OUTPUT_PREFIX = "[历史工具结果，仅供上下文，不是本轮新调用]\n"
MISSING_TOOL_OUTPUT_PLACEHOLDER = "[工具结果缺失：上一轮在工具开始后被中断，未收到工具返回。]"

DEFAULT_IMAGE_ANALYZER_PROMPT = (
    "You are an image analysis specialist. When given an image, describe its contents in detail "
    "and answer any questions about it. Return your findings as concise, structured text."
)

IMAGE_PATH_RE = re.compile(
    r"(?P<path>(?:~|/|[A-Za-z]:[\\/])(?:[^\s'\"<>]|\\ )+\.(?:png|jpe?g|webp|gif|bmp|tiff?))",
    re.IGNORECASE,
)
VIRTUAL_RESOURCE_PREFIXES = (
    "/workspace/",
    "/knowledge/",
    "/semantic-assets/",
    "/sql-guardrails/",
    "/analytics-models/",
    "/skills/",
    "/large_tool_results/",
)
IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _resolve_subagent_model(model_name: str) -> str | BaseChatModel:
    """Resolve a subagent model spec.

    - Strings containing a colon (e.g. "qwen:qwen3.7") are passed through for
      LangChain init_chat_model resolution (direct provider).
    - Bare model names that match a Higress AI route (e.g. "qwen3.7-plus") are
      wired to the gateway via ChatOpenAI.
    - Anything else is returned as-is.
    """
    if ":" in model_name:
        return model_name

    try:
        from langchain_openai import ChatOpenAI

        from capabilities import get_effective_gateway_url
        from higress_config_reader import get_higress_routed_models

        routed = set(get_higress_routed_models())
        if model_name in routed:
            gateway_url = get_effective_gateway_url()
            if gateway_url:
                return ChatOpenAI(
                    model=model_name,
                    api_key="puddingclaw-gateway",
                    base_url=gateway_url,
                    streaming=True,
                )
    except Exception as exc:
        logger.debug("[subagent] failed to resolve gateway model %r: %s", model_name, exc)
    return model_name


def _is_image_analyzer_item(item: dict[str, Any]) -> bool:
    return str(item.get("name") or "") == "image_analyzer" or str(item.get("route_trigger") or "") == "image_input"


class AttachmentImageContentMiddleware(AgentMiddleware[Any, Any, Any]):
    """Materialize image resources only after the subagent reads them with a tool."""

    IMAGE_ATTACHMENT_RE = re.compile(r"^PuddingClaw-Resource-Image:\s*(att_[A-Za-z0-9_-]+)\s*$", re.MULTILINE)
    IMAGE_PATH_RE = re.compile(r"^PuddingClaw-Resource-Image-Path:\s*(.+?)\s*$", re.MULTILINE)

    @staticmethod
    def _session_id_from_text(text: str) -> str:
        match = re.search(r"harness_attachment_session_id:\s*([A-Za-z0-9_.:-]+)", text)
        if not match:
            match = re.search(r"harness_image_session_id:\s*([A-Za-z0-9_.:-]+)", text)
        return match.group(1) if match else ""

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return str(content or "")

    def _image_inputs(self, request: ModelRequest[Any]) -> list[dict[str, Any]]:
        all_text = "\n".join(
            self._content_text(getattr(message, "content", "")) for message in getattr(request, "messages", [])
        )
        state = getattr(request, "state", {}) or {}
        session_id = (
            str(state.get("harness_attachment_session_id") or "")
            or str(state.get("harness_image_session_id") or "")
            or self._session_id_from_text(all_text)
        )
        tool_text = "\n".join(
            self._content_text(getattr(message, "content", ""))
            for message in getattr(request, "messages", [])
            if isinstance(message, ToolMessage)
        )

        image_inputs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for attachment_id in self.IMAGE_ATTACHMENT_RE.findall(tool_text):
            if not session_id or attachment_id in seen:
                continue
            seen.add(attachment_id)
            item = attachment_store.get(session_id, attachment_id)
            if not item or item.get("type") != "image":
                continue
            block = DeepAgentsAgentManager._image_content_from_attachment(
                {"id": attachment_id, "type": "image"},
                session_id=session_id,
            )
            url = str((block or {}).get("image_url", {}).get("url") or "")
            if not url:
                continue
            image_inputs.append(
                {
                    "ref": f"image_{len(image_inputs) + 1}",
                    "id": attachment_id,
                    "name": str(item.get("name") or attachment_id),
                    "url": url,
                    "source": "attachment",
                }
            )
        for raw_path in self.IMAGE_PATH_RE.findall(tool_text):
            path_key = raw_path.strip()
            if not path_key or path_key in seen:
                continue
            seen.add(path_key)
            block = DeepAgentsAgentManager._image_content_from_path(path_key, session_id=session_id)
            url = str((block or {}).get("image_url", {}).get("url") or "")
            if not url:
                continue
            image_inputs.append(
                {
                    "ref": f"image_{len(image_inputs) + 1}",
                    "name": Path(path_key).name,
                    "path": path_key,
                    "url": url,
                    "source": "local_path",
                }
            )
        return image_inputs

    def _request_with_images(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        image_inputs = self._image_inputs(request)
        if not image_inputs:
            return request
        messages = list(request.messages)
        if not messages:
            return request

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "你刚刚通过 read_resource 打开了图片资源。请分析随附图片，并返回给主 Agent "
                    "可直接使用的中文结构化结果。"
                ),
            }
        ]
        for image in image_inputs:
            url = str(image.get("url") or "")
            if url.startswith("data:image/"):
                content.append({"type": "image_url", "image_url": {"url": url}})

        messages.append(HumanMessage(content=content))
        return request.override(messages=messages)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._request_with_images(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._request_with_images(request))


def _build_subagent_item(
    item: dict[str, Any],
    default_tools: list[Any],
    default_skills: list[str],
    middleware_factory: Callable[[], list[Any]] | None = None,
) -> SubAgent:
    """Build a single SubAgent spec from a settings item."""
    name = item.get("name", "subagent") or "subagent"
    model_name = item.get("model", "") or ""
    model = _resolve_subagent_model(model_name) if model_name else None
    description = item.get("description") or f"Subagent `{name}`."
    route_trigger = str(item.get("route_trigger") or "").strip()
    if route_trigger and route_trigger not in description:
        description = (
            f"{description} Use this subagent when the main request matches this routing hint: `{route_trigger}`."
        )
    is_image_analyzer = _is_image_analyzer_item(item)
    native_hint = (
        "Use this subagent via the native task tool whenever the user provides image attachments "
        "or local image paths. Preserve the harness_attachment_session_id and image refs from the user "
        "message in the task description."
    )
    if is_image_analyzer and native_hint not in description:
        description = f"{description} {native_hint}"
    system_prompt = item.get("system_prompt") or DEFAULT_IMAGE_ANALYZER_PROMPT
    if is_image_analyzer:
        system_prompt = (
            f"{system_prompt}\n\n"
            "When the task description includes attachment refs like `att_xxx` or local image paths, "
            "first call `read_resource` for each image resource. Do not answer image-content questions "
            "until the resource has been read. After `read_resource` returns an image resource marker, "
            "continue with visual analysis and return concise structured findings to the main Agent."
        )

    tools_cfg = item.get("tools", {})
    tools = default_tools if tools_cfg.get("mode", "inherit") == "inherit" else []

    skills_cfg = item.get("skills", {})
    if skills_cfg.get("mode", "inherit") == "inherit":
        skills = list(default_skills)
    else:
        configured_paths = skills_cfg.get("paths", [])
        skills = [str(path) for path in configured_paths if str(path).strip()]

    spec: SubAgent = {
        "name": name,
        "description": description,
        "system_prompt": system_prompt,
        "tools": tools,
    }
    if model:
        spec["model"] = model
    if skills:
        spec["skills"] = skills
    middlewares = middleware_factory() if middleware_factory else []
    if model:
        # Explicit-model subagents keep their provider-native DeepAgents
        # summarizer. The configured PuddingClaw policy belongs to inherited
        # ModelClientChatModel agents only.
        middlewares = [
            middleware for middleware in middlewares if not isinstance(middleware, PuddingClawSummarizationMiddleware)
        ]
    if is_image_analyzer:
        middlewares.append(AttachmentImageContentMiddleware())
    if middlewares:
        spec["middleware"] = middlewares
    return spec


def _build_subagents(
    default_tools: list[Any],
    default_skills: list[str],
    middleware_factory: Callable[[], list[Any]] | None = None,
) -> list[SubAgent]:
    """Build declarative subagents from normalized settings config."""
    items = config.get_settings_for_display().get("subagents", {}).get("items", [])
    subagents: list[SubAgent] = []
    # Declare this ourselves rather than rely on DeepAgents' implicit
    # general-purpose subagent. It receives the same Toolset boundary as the
    # parent, so delegation cannot bypass Skill-gated business tools.
    subagents.append(
        {
            "name": "general-purpose",
            "description": "General-purpose subagent for isolated multi-step work.",
            "system_prompt": "Complete the delegated task concisely. Read an applicable project Skill before using its business tools.",
            "tools": default_tools,
            "skills": list(default_skills),
            "middleware": middleware_factory() if middleware_factory else [],
        }
    )
    for item in items:
        if not item.get("enabled", False):
            continue
        subagents.append(_build_subagent_item(item, default_tools, default_skills, middleware_factory))
    return subagents


async def _generate_title(session_id: str) -> str | None:
    """Generate a short title for Agent-mode sessions using the same title role."""

    try:
        messages = session_manager.load_session_for_agent(session_id)
        first_user = ""
        first_assistant = ""
        for msg in messages:
            if msg.get("role") == "user" and not first_user:
                first_user = str(msg.get("content") or "")[:200]
            elif msg.get("role") == "assistant" and not first_assistant:
                first_assistant = str(msg.get("content") or "")[:200]
            if first_user and first_assistant:
                break

        if not first_user:
            return None

        from langchain_core.messages import HumanMessage

        from llm.model_client import ModelClient

        llm = ModelClient(role="title", temperature=0.3)
        prompt = (
            "根据以下对话内容，生成一个不超过10个字的中文标题，只输出标题文本，不要加引号或标点。\n\n"
            f"用户: {first_user}\n"
            f"助手: {first_assistant}"
        )

        result = await llm.ainvoke([HumanMessage(content=prompt)])
        title = str(result.content).strip().strip('"\'""')[:20]
        if not title:
            return None
        session_manager.update_title(session_id, title)
        return title
    except Exception:
        traceback.print_exc()
        return None


def _provisional_title(message: str) -> str:
    """Create an immediate durable title before any Run work can fail."""

    normalized = re.sub(r"\s+", " ", str(message or "")).strip()
    normalized = normalized.split("\n\n[附件]", 1)[0].strip()
    return (normalized[:20] or "新对话").rstrip("，。！？,.!?；;：:") or "新对话"


class DeepAgentsAgentManager:
    """Build and run DeepAgents agents for project-scoped Agent sessions."""

    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._checkpointer: Any | None = None
        self._checkpointer_info: dict[str, Any] | None = None
        self._run_coordinator = HarnessRunCoordinator(session_manager)
        self._active_goal_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}

    def initialize(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def cancel_active_goal_run(self, session_id: str, goal_id: str) -> bool:
        """Cooperatively stop the in-process SSE task for a controlled Goal."""

        task = self._active_goal_tasks.get((session_id, goal_id))
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def _resolve_workspace(
        self,
        *,
        session_id: str,
        project_id: str | None,
    ) -> tuple[Path, dict[str, Any]]:
        if project_id:
            project_path = project_registry.resolve(project_id)
            return project_path, {
                "runtime_mode": "agent",
                "project_id": project_id,
                "project_path": str(project_path),
                "workspace_type": "project",
                "workspace_path": str(project_path),
            }

        workspace_path = project_registry.ensure_unscoped_workspace(session_id)
        return workspace_path, {
            "runtime_mode": "agent",
            "project_id": None,
            "project_path": None,
            "workspace_type": "unscoped_agent",
            "workspace_path": str(workspace_path),
        }

    def _memory_dir_for(self, project_id: str | None) -> Path:
        """Return the on-disk directory that holds runtime project memory."""

        assert self._base_dir is not None
        memory_root = self._base_dir / "data" / "deepagents-memory"
        if project_id:
            return memory_root / "projects" / project_id
        return memory_root / "global"

    def _ensure_memory_md(self, memory_dir: Path) -> Path:
        """Create or migrate the runtime MEMORY.md file for a project."""

        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_md = memory_dir / "MEMORY.md"
        legacy_agents_md = memory_dir / "AGENTS.md"
        if not memory_md.exists() and legacy_agents_md.exists():
            legacy_agents_md.replace(memory_md)
        if not memory_md.exists():
            memory_md.write_text(
                "# Project Memory\n\n"
                "<!--\n"
                "This file is injected into the Agent's system prompt via DeepAgents MemoryMiddleware.\n"
                "Put stable, long-lived project conventions here (tech stack, coding style, naming rules).\n"
                "Do NOT put session-specific or frequently changing data here — it hurts prompt caching.\n"
                "-->\n",
                encoding="utf-8",
            )
        return memory_md

    def _build_backend(
        self,
        workspace_path: Path,
        session_id: str = "",
        query_id: str = "",
    ):
        assert self._base_dir is not None
        skills_dir = self._base_dir / "skills"
        semantic_assets_dir = self._base_dir / "semantic-assets"
        sql_guardrails_dir = self._base_dir / "sql-guardrails"
        analytics_models_dir = self._base_dir / "analytics-models"
        knowledge_dir = get_knowledge_root(self._base_dir)
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        semantic_assets_dir.mkdir(parents=True, exist_ok=True)
        sql_guardrails_dir.mkdir(parents=True, exist_ok=True)
        analytics_models_dir.mkdir(parents=True, exist_ok=True)
        large_tool_results_dir = (
            workspace_path
            / ".puddingclaw"
            / "large_tool_results"
            / (session_id or "anonymous-session")
            / (query_id or "unscoped-query")
        )
        large_tool_results_dir.mkdir(parents=True, exist_ok=True)
        workspace_digest = hashlib.sha256(str(workspace_path.resolve()).encode("utf-8")).hexdigest()[:20]
        safe_session = re.sub(r"[^A-Za-z0-9_-]+", "_", session_id or "anonymous-session")
        safe_query = re.sub(r"[^A-Za-z0-9_-]+", "_", query_id or "unscoped-query")
        scratch_project_root = self._base_dir / "data" / "harness-scratch" / "projects" / workspace_digest
        scratch_relative = f"{safe_session}/{safe_query}"
        scratch_query_dir = scratch_project_root / safe_session / safe_query
        scratch_query_dir.mkdir(parents=True, exist_ok=True)
        terminal_config = config.load_config().get("harness", {}).get("terminal", {})
        terminal_config = {
            **terminal_config,
            "_scratch_host_path": str(scratch_query_dir.resolve()),
            "docker": {
                **dict(terminal_config.get("docker") or {}),
                "_managed_readonly_mounts": [
                    {
                        "source": str(skills_dir.resolve()),
                        "target": "/skills",
                    }
                ],
                "_managed_writable_mounts": [
                    {
                        "source": str(scratch_project_root.resolve()),
                        "target": "/harness-scratch",
                    }
                ],
                "_scratch_relative": scratch_relative,
            },
        }
        selection = build_workspace_execution_backend(
            workspace_path,
            terminal_config,
        )
        workspace_backend = selection.backend
        routes: dict[str, Any] = {
            "/workspace/": workspace_backend,
            "/knowledge/": FilesystemBackend(root_dir=knowledge_dir, virtual_mode=True),
            "/semantic-assets/": FilesystemBackend(root_dir=semantic_assets_dir, virtual_mode=True),
            "/sql-guardrails/": FilesystemBackend(root_dir=sql_guardrails_dir, virtual_mode=True),
            "/analytics-models/": FilesystemBackend(root_dir=analytics_models_dir, virtual_mode=True),
            "/large_tool_results/": FilesystemBackend(
                root_dir=large_tool_results_dir,
                virtual_mode=True,
            ),
            "/scratch/": FilesystemBackend(root_dir=scratch_query_dir, virtual_mode=True),
        }
        workspace_host_prefix = f"{workspace_path.resolve().as_posix().rstrip('/')}/"
        routes[workspace_host_prefix] = workspace_backend
        if skills_dir.exists():
            routes["/skills/"] = FilesystemBackend(root_dir=skills_dir, virtual_mode=True)
        backend = PermissionedCompositeBackend(
            default=workspace_backend,
            routes=routes,
            session_id=session_id,
            managed_readonly_roots=(
                knowledge_dir,
                semantic_assets_dir,
                sql_guardrails_dir,
                analytics_models_dir,
                skills_dir,
            ),
            workspace_root=workspace_path,
        )
        backend.execution_mode = selection.mode
        backend.execution_backend_id = workspace_backend.id
        backend.execution_backend = workspace_backend
        backend.execution_fallback_reason = selection.fallback_reason
        backend.execution_dependency_plan = selection.dependency_plan
        backend.execution_scratch_host_path = str(scratch_query_dir.resolve())
        return backend

    def _build_middlewares(
        self,
        project_id: str | None,
        *,
        rubric_model: BaseChatModel | None = None,
        skill_toolsets: dict[str, set[str]] | None = None,
        known_tools: set[str] | None = None,
        backend_mode: str = "restricted_host",
        permission_context: RunPermissionContext | None = None,
        workspace_backend: Any | None = None,
    ) -> list[Any]:
        """Build user-provided DeepAgents middlewares.

        create_deep_agent() automatically injects TodoListMiddleware and other
        base middleware. We only supply project-specific MemoryMiddleware here;
        passing TodoListMiddleware again would trigger the duplicate-instance
        assertion.
        """

        assert self._base_dir is not None

        # 1) Project-scoped or global runtime MEMORY.md
        memory_dir = self._memory_dir_for(project_id)
        self._ensure_memory_md(memory_dir)

        # 2) Optional gstack skill index
        gstack_path = (self._base_dir / "gstack" / "AGENTS.md").resolve()
        gstack_sources: list[str] = []
        gstack_route: FilesystemBackend | None = None
        if gstack_path.exists():
            gstack_route = FilesystemBackend(
                root_dir=str(gstack_path.parent),
                virtual_mode=True,
            )
            gstack_sources.append("/gstack/AGENTS.md")

        # Use a single MemoryMiddleware with a composite backend to avoid
        # DeepAgents' "duplicate middleware instances" assertion.
        sources = ["/MEMORY.md", *gstack_sources]
        if gstack_route is not None:
            memory_backend: FilesystemBackend | CompositeBackend = CompositeBackend(
                default=FilesystemBackend(root_dir=memory_dir, virtual_mode=True),
                routes={"/gstack/": gstack_route},
            )
        else:
            memory_backend = FilesystemBackend(root_dir=memory_dir, virtual_mode=True)

        toolset_mapping = skill_toolsets or discover_skill_toolsets(self._base_dir / "skills")
        middlewares: list[Any] = [
            MemoryMiddleware(backend=memory_backend, sources=sources),
            SemanticAssetsMiddleware(base_dir=self._base_dir),
            ExternalFilePermissionMiddleware(),
            WorkspacePathRouterMiddleware(),
            VerificationActivationMiddleware(),
            *([VersionedPatchMiddleware(workspace_backend)] if workspace_backend is not None else []),
            *([AttachmentEditMiddleware(workspace_backend)] if workspace_backend is not None else []),
            ToolExecutionPipeline(
                known_tools=set(known_tools or ()),
                backend_mode=backend_mode,
                permission_context=permission_context,
                base_dir=self._base_dir,
            ),
            SkillIntentRouterMiddleware(),
            ToolsetMiddleware(
                skills_dir=self._base_dir / "skills",
                toolsets_by_skill=toolset_mapping,
            ),
        ]
        tool_context_cfg = ToolContextConfig.from_mapping(config.get_deepagents_tool_context_config())
        if tool_context_cfg.enabled:
            middlewares.append(ToolContextCompactionMiddleware(tool_context_cfg))
        rubric_cfg = config.load_config().get("harness", {}).get("completion", {}).get("rubric", {})
        if rubric_cfg.get("enabled", True) and rubric_model is not None:
            max_iterations = rubric_cfg.get("max_iterations", 2)
            if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or not 1 <= max_iterations <= 20:
                max_iterations = 2
            middlewares.append(
                PuddingClawRubricMiddleware(
                    model=rubric_model,
                    max_iterations=max_iterations,
                )
            )
        model_call_limit_cfg = config.load_config().get("harness", {}).get("model_call_limit", {})
        if model_call_limit_cfg.get("enabled", True):
            run_limit = model_call_limit_cfg.get("run_limit")
            thread_limit = model_call_limit_cfg.get("thread_limit")
            exit_behavior = str(model_call_limit_cfg.get("exit_behavior") or "end")
            if exit_behavior not in {"end", "error"}:
                exit_behavior = "end"
            if isinstance(run_limit, int) and run_limit <= 0:
                run_limit = None
            if isinstance(thread_limit, int) and thread_limit <= 0:
                thread_limit = None
            if run_limit is not None or thread_limit is not None:
                middlewares.append(
                    ObservableModelCallLimitMiddleware(
                        run_limit=run_limit if isinstance(run_limit, int) else None,
                        thread_limit=thread_limit if isinstance(thread_limit, int) else None,
                        exit_behavior=exit_behavior,  # type: ignore[arg-type]
                    )
                )
        return middlewares

    async def _build_checkpointer(self) -> Any:
        """Return the process-local checkpointer used by live HITL runs.

        Conversation continuity is rebuilt from Session history, and PuddingClaw
        does not resume an interrupted graph from a later HTTP request. Keeping
        every Run in SQLite therefore only accumulated unused checkpoint rows.
        A process-local saver is sufficient for pause/resume inside the active
        SSE generator, and each thread is deleted when that Run terminates.
        """

        if self._checkpointer is not None:
            return self._checkpointer

        from langgraph.checkpoint.memory import InMemorySaver

        self._checkpointer = InMemorySaver()
        self._checkpointer_info = {
            "type": "memory",
            "scope": "active_sse_run",
        }
        return self._checkpointer

    async def _delete_checkpoint_thread(self, thread_id: str) -> None:
        """Delete a terminal Run's temporary LangGraph checkpoint thread."""

        checkpointer = self._checkpointer
        if checkpointer is None:
            return
        for method_name in ("adelete_thread", "delete_thread"):
            method = getattr(checkpointer, method_name, None)
            if not callable(method):
                continue
            try:
                result = method(thread_id)
                if hasattr(result, "__await__"):
                    await result
                logger.info("Deleted terminal DeepAgents checkpoint thread: %s", thread_id)
                return
            except Exception:
                logger.warning(
                    "Failed to delete DeepAgents checkpoint thread %s via %s",
                    thread_id,
                    method_name,
                    exc_info=True,
                )
                return

    def _build_tools(
        self,
        workspace_path: Path,
        session_id: str = "",
        query_id: str = "",
        execution_backend: Any | None = None,
    ) -> list[Any]:
        """Return PuddingClaw tools that do not overlap DeepAgents built-ins."""

        assert self._base_dir is not None
        tools = []
        registered_tool_names = agent_custom_tool_names()
        for tool in get_all_tools(self._base_dir):
            if getattr(tool, "name", "") not in registered_tool_names:
                continue
            if getattr(tool, "name", "") == "terminal":
                # In Agent mode, terminal should follow the same workspace
                # boundary as the DeepAgents filesystem backend. Map virtual
                # prefixes to real host directories so shell commands use the
                # same paths as read_file/write_file.
                terminal_updates = {
                    "root_dir": str(workspace_path),
                    "path_aliases": {
                        "/workspace": str(workspace_path),
                        "/knowledge": str(get_knowledge_root(self._base_dir)),
                        "/skills": str(self._base_dir / "skills"),
                        "/semantic-assets": str(self._base_dir / "semantic-assets"),
                        "/sql-guardrails": str(self._base_dir / "sql-guardrails"),
                        "/analytics-models": str(self._base_dir / "analytics-models"),
                    },
                }
                try:
                    tool = tool.model_copy(update=terminal_updates)
                except Exception:
                    for key, value in terminal_updates.items():
                        setattr(tool, key, value)
            elif getattr(tool, "name", "") == "read_resource":
                resource_updates = {
                    "session_id": session_id,
                    "workspace_path": str(workspace_path),
                }
                try:
                    tool = tool.model_copy(update=resource_updates)
                except Exception:
                    for key, value in resource_updates.items():
                        setattr(tool, key, value)
            elif getattr(tool, "name", "") in {
                "request_dimension_build_rule",
                "inspect_dimension_build_input",
                "enqueue_semantic_dimension_build",
                "request_logical_dataset_rule",
                "ensure_attachment_table_asset",
            }:
                request_updates = {"session_id": session_id}
                try:
                    tool = tool.model_copy(update=request_updates)
                except Exception:
                    for key, value in request_updates.items():
                        setattr(tool, key, value)
            elif getattr(tool, "name", "") in {
                "database_sql_generate",
                "database_sql_validate",
                "database_sql_execute",
            }:
                database_updates = {"session_id": session_id, "query_id": query_id}
                try:
                    tool = tool.model_copy(update=database_updates)
                except Exception:
                    for key, value in database_updates.items():
                        setattr(tool, key, value)

            tools.append(tool)
        installer = getattr(execution_backend, "install_packages", None)
        if callable(installer):
            tools.append(create_install_packages_tool(installer))
        return tools

    @staticmethod
    def _middleware_hooks(middleware: Any) -> list[str]:
        try:
            from langchain.agents.middleware import AgentMiddleware
        except Exception:
            AgentMiddleware = object  # type: ignore[assignment]

        hooks: list[str] = []
        for hook in (
            "before_agent",
            "before_model",
            "after_model",
            "after_agent",
            "wrap_model_call",
            "wrap_tool_call",
        ):
            for cls in type(middleware).mro():
                if cls is AgentMiddleware:
                    break
                if hook in cls.__dict__ or f"a{hook}" in cls.__dict__:
                    hooks.append(hook)
                    break
        return hooks

    @classmethod
    def _middleware_entry(
        cls,
        middleware: Any,
        *,
        order: int,
        source: str,
        note: str = "",
    ) -> dict[str, Any]:
        return {
            "name": middleware if isinstance(middleware, str) else middleware.__class__.__name__,
            "order": order,
            "source": source,
            "hooks": cls._middleware_hooks(middleware) if not isinstance(middleware, str) else [],
            "note": note,
        }

    @classmethod
    def _middleware_inventory(cls, user_middlewares: list[Any], skills: list[str]) -> dict[str, Any]:
        """Describe the DeepAgents/LangChain middleware stack in execution order."""

        stack: list[dict[str, Any]] = []

        def add(name: str, source: str, hooks: list[str], note: str = "") -> None:
            stack.append(
                {
                    "name": name,
                    "order": len(stack) + 1,
                    "source": source,
                    "hooks": hooks,
                    "note": note,
                }
            )

        add(
            "TodoListMiddleware",
            "deepagents.base",
            ["wrap_model_call", "after_model"],
            "注入稳定 ID 的 update_todos 提示并检查增量 Todo 操作",
        )
        if skills:
            add("SkillsMiddleware", "deepagents.base", ["before_agent"], "将 skills snapshot 注入系统上下文")
        add(
            "FilesystemMiddleware",
            "deepagents.base",
            ["wrap_tool_call"],
            "提供 /workspace、/knowledge、/semantic-assets、/sql-guardrails、/analytics-models 与 /skills 文件系统能力",
        )
        add(
            "SubAgentMiddleware",
            "deepagents.base",
            ["wrap_model_call"],
            "向 system message 注入 task/subagent 使用说明",
        )
        add("SummarizationMiddleware", "deepagents.base", ["before_model"], "DeepAgents 基础上下文压缩")
        add("PatchToolCallsMiddleware", "deepagents.base", ["after_model"], "修正/补齐工具调用")

        for middleware in user_middlewares:
            entry = cls._middleware_entry(
                middleware,
                order=len(stack) + 1,
                source="puddingclaw.user",
                note="PuddingClaw 运行时显式挂载",
            )
            if not entry["hooks"] and entry["name"] == "MemoryMiddleware":
                entry["hooks"] = ["before_agent"]
            stack.append(entry)

        add(
            "AnthropicPromptCachingMiddleware",
            "deepagents.tail",
            ["wrap_model_call"],
            "DeepAgents tail stack，非 Anthropic 模型下通常 no-op",
        )

        hook_order: dict[str, list[dict[str, Any]]] = {}
        for hook in (
            "before_agent",
            "before_model",
            "after_model",
            "after_agent",
            "wrap_model_call",
            "wrap_tool_call",
        ):
            entries = [entry for entry in stack if hook in entry.get("hooks", [])]
            if hook in {"after_model", "after_agent"}:
                entries = list(reversed(entries))
            hook_order[hook] = [
                {
                    "name": entry["name"],
                    "stack_order": entry["order"],
                    "execution_order": idx + 1,
                    "source": entry["source"],
                    "note": entry.get("note", ""),
                }
                for idx, entry in enumerate(entries)
            ]

        return {
            "stack": stack,
            "hooks": hook_order,
            "order_rule": {
                "before": "before_agent / before_model 按 stack order 正序执行",
                "after": "after_model / after_agent 按 stack order 反序执行",
                "wrap": "wrap_model_call / wrap_tool_call 按 stack order 进入，外层包住内层",
            },
        }

    @staticmethod
    def _tool_inventory(tools: list[Any]) -> list[dict[str, Any]]:
        mounted = [
            {"name": "update_todos", "source": "puddingclaw.harness", "description": "按稳定 ID 增量管理 Todo ledger"},
            {"name": "ls", "source": "deepagents.builtin", "description": "列出文件"},
            {"name": "read_file", "source": "deepagents.builtin", "description": "读取文件"},
            {"name": "write_file", "source": "deepagents.builtin", "description": "写入文件"},
            {"name": "edit_file", "source": "deepagents.builtin", "description": "编辑文件"},
            {"name": "glob", "source": "deepagents.builtin", "description": "按 glob 查找文件"},
            {"name": "grep", "source": "deepagents.builtin", "description": "全文检索文件"},
            {"name": "execute", "source": "deepagents.builtin", "description": "执行 shell 命令"},
            {"name": "task", "source": "deepagents.builtin", "description": "调用 subagent"},
        ]
        seen = {item["name"] for item in mounted}
        for tool in tools:
            name = str(getattr(tool, "name", "") or tool.__class__.__name__)
            if not name or name in seen:
                continue
            mounted.append(
                {
                    "name": name,
                    "source": "puddingclaw.tool",
                    "description": str(getattr(tool, "description", "") or "")[:240],
                }
            )
            seen.add(name)
        return mounted

    @staticmethod
    def _subagent_inventory() -> list[dict[str, Any]]:
        try:
            items = config.get_settings_for_display().get("subagents", {}).get("items", [])
        except Exception:
            items = []
        mounted: list[dict[str, Any]] = [
            {
                "name": "general-purpose",
                "enabled": True,
                "model": "inherits main agent",
                "description": (
                    "DeepAgents default general-purpose subagent, automatically injected "
                    "through SubAgentMiddleware when not overridden by config."
                ),
                "route_trigger": "complex independent task",
                "tools_mode": "inherit",
                "skills_mode": "inherit",
                "source": "deepagents.default",
                "href": "",
            }
        ]
        for index, item in enumerate(items):
            name = str(item.get("name") or f"subagent_{index + 1}")
            if name == "general-purpose":
                mounted = [entry for entry in mounted if entry.get("source") != "deepagents.default"]
            tools_cfg = item.get("tools") or {}
            skills_cfg = item.get("skills") or {}
            mounted.append(
                {
                    "name": name,
                    "enabled": bool(item.get("enabled", False)),
                    "model": str(item.get("model") or ""),
                    "description": str(item.get("description") or "")[:240],
                    "route_trigger": str(item.get("route_trigger") or ""),
                    "tools_mode": str(tools_cfg.get("mode") or "inherit"),
                    "skills_mode": str(skills_cfg.get("mode") or "inherit"),
                    "source": "config",
                    "href": f"/settings?category=harness&tab=subagent&subagent={quote(name)}",
                }
            )
        return mounted

    @staticmethod
    def _extract_skill_frontmatter(text: str, fallback_name: str) -> dict[str, str]:
        name = fallback_name
        description = ""
        if text.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", text, flags=re.S)
            if match:
                for line in match.group(1).splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip("'\"") or name
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("'\"")
        if not description:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                    description = stripped[:240]
                    break
        return {"name": name, "description": description}

    def _skills_inventory(self) -> list[dict[str, Any]]:
        assert self._base_dir is not None
        skills_dir = self._base_dir / "skills"
        if not skills_dir.exists():
            return []
        skills: list[dict[str, Any]] = []
        seen: set[str] = set()
        for skill_md in sorted(skills_dir.rglob("SKILL.md")):
            try:
                text = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue
            info = self._extract_skill_frontmatter(text, skill_md.parent.name)
            if info["name"] in seen:
                continue
            seen.add(info["name"])
            location = str(skill_md.relative_to(self._base_dir))
            skills.append(
                {
                    "name": info["name"],
                    "description": info["description"],
                    "location": location,
                    "system_prompt_source": "/skills/",
                    "in_system_prompt": True,
                    "href": f"/skills?skill={info['name']}",
                }
            )
        return skills

    def _runtime_inventory(
        self,
        *,
        tools: list[Any],
        middleware: list[Any],
        skills: list[str],
        workspace_path: Path,
        checkpointer: dict[str, Any] | None = None,
        execution_backend: Any | None = None,
    ) -> dict[str, Any]:
        inventory = {
            "middleware": self._middleware_inventory(middleware, skills),
            "filesystem": self._filesystem_inventory(workspace_path),
            "tools": self._tool_inventory(tools),
            "skills": self._skills_inventory(),
            "subagents": self._subagent_inventory(),
            "package_versions": self._package_versions(),
            "checkpointer": checkpointer or {},
        }
        if execution_backend is not None:
            inventory["execution"] = {
                "mode": str(getattr(execution_backend, "execution_mode", "restricted_host")),
                "backend_id": str(getattr(execution_backend, "execution_backend_id", "")),
                "fallback_reason": getattr(
                    execution_backend,
                    "execution_fallback_reason",
                    None,
                ),
                "workspace_path": str(workspace_path),
                "policy": "ToolExecutionPipeline",
                "authorization_independent_from_sandbox": True,
            }
            dependency_plan = getattr(
                execution_backend,
                "execution_dependency_plan",
                None,
            )
            if dependency_plan is not None:
                inventory["execution"]["dependency_plan"] = dependency_plan.to_dict()
        return inventory

    def _filesystem_inventory(self, workspace_path: Path) -> dict[str, Any]:
        assert self._base_dir is not None
        mounts = [
            {
                "virtual_path": "/workspace/",
                "root_dir": str(workspace_path),
                "exists": workspace_path.exists(),
                "role": "session workspace",
            },
            {
                "virtual_path": "/knowledge/",
                "root_dir": str(get_knowledge_root(self._base_dir)),
                "exists": get_knowledge_root(self._base_dir).exists(),
                "role": "knowledge resources",
            },
            {
                "virtual_path": "/semantic-assets/",
                "root_dir": str(self._base_dir / "semantic-assets"),
                "exists": (self._base_dir / "semantic-assets").exists(),
                "role": "semantic assets",
            },
            {
                "virtual_path": "/sql-guardrails/",
                "root_dir": str(self._base_dir / "sql-guardrails"),
                "exists": (self._base_dir / "sql-guardrails").exists(),
                "role": "sql guardrail assets",
            },
            {
                "virtual_path": "/analytics-models/",
                "root_dir": str(self._base_dir / "analytics-models"),
                "exists": (self._base_dir / "analytics-models").exists(),
                "role": "analytics model playbooks",
            },
            {
                "virtual_path": "/skills/",
                "root_dir": str(self._base_dir / "skills"),
                "exists": (self._base_dir / "skills").exists(),
                "role": "skills",
            },
        ]
        return {"mounts": mounts}

    @staticmethod
    def _package_versions() -> dict[str, str]:
        packages = {
            "deepagents": "deepagents",
            "langchain": "langchain",
            "langchain_core": "langchain-core",
            "langgraph": "langgraph",
        }
        versions: dict[str, str] = {}
        for key, package_name in packages.items():
            try:
                versions[key] = version(package_name)
            except PackageNotFoundError:
                versions[key] = "not-installed"
        return versions

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @classmethod
    def _can_read_local_image_path(
        cls,
        path: Path,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> bool:
        workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
        if workspace is not None and cls._is_relative_to(path, workspace):
            return True
        if is_managed_resource_path(path, Path(__file__).resolve().parent.parent):
            return True
        if not workspace:
            return True
        if not session_id:
            return False
        try:
            return session_manager.has_external_file_read_permission(session_id, path)
        except AssertionError:
            return False

    @classmethod
    def _image_content_from_path(
        cls,
        path_text: str,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Convert an explicitly provided local image path into an OpenAI image_url block."""

        raw = path_text.replace("\\ ", " ").strip().strip("'\"")
        path = Path(raw).expanduser().resolve()
        try:
            if not path.is_file():
                return None
            mime = IMAGE_MIME_BY_EXT.get(path.suffix.lower())
            if not mime:
                return None
            if not cls._can_read_local_image_path(path, session_id=session_id, workspace_path=workspace_path):
                return None
            # Keep inline images bounded; very large screenshots should be
            # added through project files/tools instead of bloating a model call.
            if path.stat().st_size > 8 * 1024 * 1024:
                return None
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        except Exception:
            logger.warning("Failed to read image path for Agent input: %s", path_text, exc_info=True)
            return None

    @staticmethod
    def _image_content_from_attachment(
        attachment: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        attachment_id = str(attachment.get("id") or "").strip()
        if not attachment_id or not session_id:
            return None
        item = attachment_store.get(session_id, attachment_id)
        if not item or item.get("type") != "image":
            return None
        path = Path(str(item.get("path") or ""))
        try:
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                return None
            mime = str(item.get("mime_type") or IMAGE_MIME_BY_EXT.get(path.suffix.lower()) or "image/png")
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        except Exception:
            logger.warning("Failed to read attachment image for Agent input: %s", attachment_id, exc_info=True)
            return None

    @classmethod
    def _collect_image_inputs(
        cls,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """Collect authorized image payloads for the native task-launched image subagent."""

        inputs: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for attachment in attachments or []:
            if attachment.get("type") != "image":
                continue
            block = cls._image_content_from_attachment(attachment, session_id=session_id)
            url = str((block or {}).get("image_url", {}).get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            inputs.append(
                {
                    "ref": f"image_{len(inputs) + 1}",
                    "id": str(attachment.get("id") or ""),
                    "name": str(attachment.get("name") or f"image_{len(inputs) + 1}"),
                    "url": url,
                    "source": "attachment",
                }
            )

        for match in IMAGE_PATH_RE.finditer(message):
            raw_path = match.group("path")
            block = cls._image_content_from_path(raw_path, session_id=session_id, workspace_path=workspace_path)
            if not block:
                continue
            url = str(block.get("image_url", {}).get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            inputs.append(
                {
                    "ref": f"image_{len(inputs) + 1}",
                    "name": Path(raw_path.replace("\\ ", " ").strip().strip("'\"")).name,
                    "path": raw_path,
                    "url": url,
                    "source": "local_path",
                }
            )
        return inputs

    @classmethod
    def _build_user_content(
        cls,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
        allow_multimodal: bool = False,
    ) -> str | list[dict[str, Any]]:
        """Build user content.

        Main-agent calls default to text-only so providers that do not support
        OpenAI multimodal content parts never receive `image_url` blocks. The
        Harness image analyzer explicitly opts in with `allow_multimodal=True`.
        """

        if not allow_multimodal:
            attachment_refs = [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or item.get("path") or item.get("id") or "attachment"),
                    "type": str(item.get("type") or "file"),
                }
                for item in attachments or []
            ]
            image_names = [
                str(item.get("name") or item.get("path") or "image")
                for item in attachments or []
                if item.get("type") == "image"
            ]
            image_paths = [match.group("path") for match in IMAGE_PATH_RE.finditer(message)]
            local_resource_paths = [
                path
                for path in extract_local_resource_paths(message)
                if not path.replace("\\", "/").startswith(VIRTUAL_RESOURCE_PREFIXES)
            ]
            non_image_resource_paths = [path for path in local_resource_paths if path not in set(image_paths)]
            external_paths_needing_permission: list[str] = []
            external_resource_paths: list[str] = []
            workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
            for raw_path in local_resource_paths:
                path = Path(raw_path.replace("\\ ", " ").strip().strip("'\"")).expanduser().resolve()
                if (
                    workspace is not None
                    and not cls._is_relative_to(path, workspace)
                    and not cls._can_read_local_image_path(path, session_id=session_id, workspace_path=workspace)
                ):
                    external_resource_paths.append(str(path))
                    if raw_path in image_paths:
                        external_paths_needing_permission.append(str(path))

            if not attachment_refs and not local_resource_paths:
                return message

            notes = [
                "[系统提示] 检测到附件输入。主 Agent 请求保持纯文本，不会直接接收文件 bytes/base64。"
                "文本、Markdown、CSV、JSON 等非图片附件请调用 read_resource 读取 att_xxx；"
                "图片附件请通过原生 task 子代理处理，并在 task description 中"
                "原样保留下面的 harness_attachment_session_id 与 attachment refs。"
            ]
            if session_id:
                notes.append(f"[harness_attachment_session_id: {session_id}]")
            if attachment_refs:
                notes.append(
                    "[attachment refs]\n"
                    + "\n".join(
                        f"- {item['id'] or f'attachment_{index + 1}'}: {item['name']} ({item['type']})"
                        for index, item in enumerate(attachment_refs)
                    )
                )
            if image_names:
                notes.append("图片附件请优先调用 subagent_type=image_analyzer。")
            if image_paths:
                notes.append("[本地图片路径]\n" + "\n".join(f"- {path}" for path in image_paths))
            if non_image_resource_paths:
                notes.append(
                    "[本地文件路径]\n"
                    + "\n".join(f"- {path}" for path in non_image_resource_paths)
                    + "\n以上非 workspace 本地路径请调用 read_resource(resource=原始路径) 读取；不要调用 read_file、glob 或 grep。"
                )
            if external_resource_paths and not external_paths_needing_permission:
                paths = "\n".join(f"- {path}" for path in external_resource_paths)
                notes.append(
                    "[外部文件授权] 检测到 workspace 外的本地文件路径。主 Agent 必须通过 "
                    f"read_resource 触发授权并读取：\n{paths}\n"
                    "若用户要求修改该文件，获得写权限后必须对上面的原始绝对路径调用 "
                    "edit_file/write_file。临时转换或验证只能写入 /scratch；禁止复制到 /workspace，"
                    "也不能把 attachment、/scratch 或 /workspace 副本冒充原始交付目标。"
                )
            if external_paths_needing_permission:
                paths = "\n".join(f"- {path}" for path in external_paths_needing_permission)
                notes.append(
                    "[外部文件授权] 检测到 workspace 外的本地图片路径。若尚未完成授权，主 Agent 必须先通过 "
                    f"read_resource 触发授权后再分析：\n{paths}"
                )
            return f"{message}\n\n" + "\n\n".join(notes)

        content: list[dict[str, Any]] = [{"type": "text", "text": message}]
        seen_urls: set[str] = set()
        external_paths_needing_permission: list[str] = []
        for attachment in attachments or []:
            if attachment.get("type") != "image":
                continue
            block = cls._image_content_from_attachment(attachment, session_id=session_id)
            url = str((block or {}).get("image_url", {}).get("url") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            content.append({"type": "image_url", "image_url": {"url": url}})

        for match in IMAGE_PATH_RE.finditer(message):
            raw_path = match.group("path")
            if raw_path.replace("\\", "/").startswith(VIRTUAL_RESOURCE_PREFIXES):
                continue
            block = cls._image_content_from_path(raw_path, session_id=session_id, workspace_path=workspace_path)
            if block:
                url = block["image_url"]["url"]
                if url not in seen_urls:
                    seen_urls.add(url)
                    content.append(block)
                continue
            path = Path(raw_path.replace("\\ ", " ").strip().strip("'\"")).expanduser().resolve()
            workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
            if (
                workspace is not None
                and not cls._is_relative_to(path, workspace)
                and not cls._can_read_local_image_path(path, session_id=session_id, workspace_path=workspace)
            ):
                external_paths_needing_permission.append(str(path))

        if external_paths_needing_permission:
            paths = "\n".join(f"- {path}" for path in external_paths_needing_permission)
            content[0]["text"] = (
                f"{message}\n\n"
                "[系统提示] 检测到 workspace 外且不属于 PuddingClaw 托管资源的本地图片路径，"
                "当前不会直接读取或内联给子代理。"
                "只有这种未授权外部图片，才需要主 Agent 先通过 read_resource 触发外部文件授权；"
                "授权完成后再重试该图片分析。\n"
                f"{paths}"
            )

        if len(content) == 1:
            return content[0]["text"]
        return content

    @staticmethod
    def _display_message_with_attachments(message: str, attachments: list[dict[str, Any]] | None = None) -> str:
        attachment_names = [
            str(item.get("name") or item.get("path") or item.get("id") or "attachment") for item in attachments or []
        ]
        if not attachment_names:
            return message
        suffix = "\n\n[附件]\n" + "\n".join(f"- {name}" for name in attachment_names)
        return f"{message}{suffix}"

    @staticmethod
    def _artifact_links(
        activations: list[dict[str, Any]],
        workspace_path: Path,
    ) -> str:
        """Render Tool-authoritative artifacts without parsing model prose."""

        seen: set[str] = set()
        links: list[str] = []
        for activation in activations:
            if not isinstance(activation, dict) or activation.get("status") != "succeeded":
                continue
            for ref in activation.get("evidence_refs") or []:
                if not isinstance(ref, dict) or ref.get("kind") != "artifact_write":
                    continue
                artifact_id = str(ref.get("artifact_id") or ref.get("host_path") or ref.get("path") or "")
                if not artifact_id or artifact_id in seen:
                    continue
                seen.add(artifact_id)
                host_raw = str(ref.get("host_path") or "")
                relative = str(ref.get("workspace_relative_path") or "")
                if host_raw:
                    local_path = Path(host_raw).expanduser().resolve()
                elif relative and ".." not in Path(relative).parts:
                    local_path = (workspace_path / relative).resolve()
                else:
                    continue
                if not local_path.exists():
                    continue
                label_path = str(ref.get("virtual_path") or ref.get("path") or local_path)
                links.append(
                    f"- [打开 {local_path.name}]({local_path.as_uri()})  \n  `{label_path}`"
                )
        if not links:
            return ""
        return "\n\n产物：\n" + "\n".join(links)

    def _analytics_model_context(self, analytics_model_id: str | None) -> tuple[str, dict[str, Any] | None]:
        if not analytics_model_id:
            return "", None
        assert self._base_dir is not None
        try:
            model = get_analytics_model_registry(self._base_dir).get_model_context(analytics_model_id)
        except Exception as exc:
            payload = {
                "id": analytics_model_id,
                "loaded": False,
                "error": str(exc),
            }
            return (
                "\n\n"
                "<analytics_model_context>\n"
                f"请求的分析模型 `{analytics_model_id}` 加载失败：{exc}\n"
                "本轮必须明确告知用户模型加载失败，并按通用 Agent 行为继续。\n"
                "</analytics_model_context>\n",
                payload,
            )

        frontmatter = model.get("frontmatter") or {}
        body = str(model.get("body") or "").strip()
        body_preview = body[:12000] + ("\n...[truncated]" if len(body) > 12000 else "")
        yaml_text = json.dumps(frontmatter, ensure_ascii=False, indent=2)
        rel_path = str(model.get("path") or "").strip()
        virtual_path = f"/{rel_path}" if rel_path.startswith("analytics-models/") else rel_path
        payload = {
            "id": model.get("id"),
            "name": model.get("name"),
            "version": model.get("version"),
            "path": model.get("path"),
            "loaded": True,
            "missing_references": model.get("missing_references") or [],
            "missing_data_assets": model.get("missing_data_assets") or [],
            "data_assets": model.get("data_assets") or [],
            "semantic_assets": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "path": item.get("path"),
                    "frontmatter": item.get("frontmatter") or {},
                }
                for item in model.get("semantic_assets") or []
            ],
            "asset_relations": model.get("asset_relations") or [],
            "derived_dimension_paths": model.get("derived_dimension_paths") or [],
            "logical_datasets": model.get("logical_datasets") or [],
        }
        relation_context = model.get("asset_relations") or []
        relation_text = json.dumps(relation_context, ensure_ascii=False, indent=2)
        derived_path_text = json.dumps(model.get("derived_dimension_paths") or [], ensure_ascii=False, indent=2)
        data_asset_text = json.dumps(model.get("data_assets") or [], ensure_ascii=False, indent=2)
        prompt = (
            "\n\n"
            "<analytics_model_context>\n"
            "当前用户已选择一个分析模型。它是本轮任务的强上下文，不是底层 LLM 模型。\n"
            "你必须优先遵守该模型的业务边界、Playbook、数据资产、语义资产、守卫和输出要求。\n"
            "如果用户问题与该模型冲突或缺少关键参数，先说明冲突或追问，不要静默忽略模型。\n\n"
            "跨资产分析只能沿已发布的资产关联，或由已选资产共同绑定的维度路径进行；不得仅凭同名字段猜测 Join。\n\n"
            f"模型 ID：{model.get('id')}\n"
            f"模型名称：{model.get('name')}\n"
            f"版本：{model.get('version')}\n"
            f"文件路径：{virtual_path}\n\n"
            "机器可读 metadata：\n"
            f"```json\n{yaml_text}\n```\n\n"
            "已解析资产关联：\n"
            f"```json\n{relation_text}\n```\n\n"
            "已推导共同维度路径：\n"
            f"```json\n{derived_path_text}\n```\n\n"
            "已选数据资产摘要：\n"
            "这里统一列出数据库表、普通导入表和虚拟逻辑数据集。跨期趋势、环比、同比或跨来源汇总"
            "优先使用覆盖范围合适的逻辑数据集；逻辑数据集未覆盖目标期间时，使用模型已选的原始资产补足，"
            "不得因为某一个数据集缺少年份就断言模型没有该年份数据。\n"
            f"```json\n{data_asset_text}\n```\n\n"
            "模型 Playbook：\n"
            f"{body_preview}\n"
            "</analytics_model_context>\n"
        )
        return prompt, payload

    @classmethod
    def _build_messages(
        cls,
        history: list[dict[str, Any]],
        message: str,
        attachments: list[dict[str, Any]] | None = None,
        *,
        session_id: str | None = None,
        workspace_path: str | Path | None = None,
        query_id: str | None = None,
    ) -> list[Any]:
        messages: list[Any] = []
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant", "system"} or content is None:
                continue
            if role == "system":
                messages.append(SystemMessage(content=content))
                continue
            if role == "user":
                messages.append(
                    HumanMessage(
                        content=cls._build_user_content(
                            str(content),
                            item.get("attachments") if isinstance(item.get("attachments"), list) else None,
                            session_id=session_id,
                            workspace_path=workspace_path,
                        )
                    )
                )
                continue

            tool_calls = item.get("tool_calls")
            if role == "assistant" and tool_calls:
                # Rebuild the protocol-correct transcript used by the old Agent:
                # AIMessage(tool_calls) followed by matching ToolMessage entries.
                # This keeps tool facts available without storing a separate
                # frontend-facing tool role in session.json.
                normalized_tool_calls: list[tuple[dict[str, Any], str, str]] = []
                for index, tc in enumerate(tool_calls):
                    tc_id = tc.get("id") or ""
                    if not tc_id:
                        tc_id = f"historical_tool_{index}"
                    tool_name = tc.get("tool") or tc.get("name") or "unknown_tool"
                    normalized_tool_calls.append((tc, tool_name, tc_id))

                lc_tool_calls = []
                for tc, tool_name, tc_id in normalized_tool_calls:
                    tool_input = tc.get("input") or tc.get("args") or {}
                    if isinstance(tool_input, dict):
                        parsed_args = dict(tool_input)
                    else:
                        parsed_args = cls._safe_parse_tool_args(str(tool_input))
                    lc_tool_calls.append({"name": tool_name, "args": parsed_args, "id": tc_id})

                ai_kwargs: dict[str, Any] = {"content": content, "tool_calls": lc_tool_calls}
                if item.get("reasoning_content"):
                    ai_kwargs["reasoning_content"] = item["reasoning_content"]
                messages.append(AIMessage(**ai_kwargs))

                for tc, tool_name, tc_id in normalized_tool_calls:
                    stored_output = tc.get("output", "")
                    stored_raw_output = tc.get("raw_output", stored_output)
                    if stored_raw_output is None or str(stored_raw_output).strip() == "":
                        stored_raw_output = MISSING_TOOL_OUTPUT_PLACEHOLDER
                    if tc.get("summary_source") in {"single_tool_overflow", "tool_result_clear"}:
                        model_output = str(stored_output or stored_raw_output)
                        model_sources = list(tc.get("sources", []) or [])
                    else:
                        adapted = tool_result_adapter.adapt(
                            str(stored_raw_output),
                            tool_name=tool_name,
                            tool_input=str(tc.get("input", tc.get("args", ""))),
                            tool_call_id=tc_id,
                        )
                        model_output = adapted.answer_context
                        model_sources = adapted.sources or list(tc.get("sources", []) or [])
                    messages.append(
                        ToolMessage(
                            content=(
                                f"{HISTORICAL_TOOL_OUTPUT_PREFIX}"
                                f"{format_sources_for_model(model_output, model_sources)}"
                            ),
                            tool_call_id=tc_id,
                            name=tool_name,
                            additional_kwargs={
                                "puddingclaw_query_id": str(item.get("query_id") or ""),
                                "puddingclaw_tool_source_hash": str(
                                    (tc.get("context_compaction") or {}).get("source_hash")
                                    or session_manager._tool_context_source_hash(str(stored_raw_output))
                                ),
                            },
                        )
                    )
            elif role == "assistant":
                messages.append(AIMessage(content=content))

        messages.append(
            HumanMessage(
                content=cls._build_user_content(
                    message,
                    attachments,
                    session_id=session_id,
                    workspace_path=workspace_path,
                ),
                additional_kwargs=(
                    {"puddingclaw_query_id": query_id} if query_id else {}
                ),
            )
        )
        return messages

    @staticmethod
    def _safe_parse_tool_args(value: str) -> dict[str, Any]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
        try:
            import ast

            parsed = ast.literal_eval(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _extract_content_text(payload: Any) -> str:
        """Extract final-answer content from common LangGraph/DeepAgents payload shapes."""
        candidate = payload
        if isinstance(payload, tuple) and payload:
            candidate = payload[0]

        content = getattr(candidate, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            text = "".join(parts)
            if text:
                return text

        return ""

    @staticmethod
    def _extract_reasoning_text(payload: Any) -> str:
        """Extract reasoning deltas without mixing them into final answer text.

        Handles multiple provider conventions:
        - DeepSeek: ``additional_kwargs["reasoning_content"]``
        - OpenAI reasoning models: ``content`` blocks of type ``thinking``
        - Responses API: ``additional_kwargs["reasoning"]`` object/summary
        """
        candidate = payload
        if isinstance(payload, tuple) and payload:
            candidate = payload[0]

        # Direct attribute (some wrappers expose reasoning_content directly)
        reasoning = getattr(candidate, "reasoning_content", None)
        if isinstance(reasoning, str):
            return reasoning

        additional = getattr(candidate, "additional_kwargs", None) or {}

        # DeepSeek-style reasoning_content
        reasoning = additional.get("reasoning_content")
        if isinstance(reasoning, str):
            return reasoning
        if isinstance(reasoning, list):
            parts = []
            for item in reasoning:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)

        # Responses API / OpenAI-style reasoning object
        reasoning = additional.get("reasoning")
        if isinstance(reasoning, str):
            return reasoning
        if isinstance(reasoning, dict):
            summary = reasoning.get("summary")
            if isinstance(summary, str):
                return summary
            if reasoning:
                return json.dumps(reasoning, ensure_ascii=False)
        if isinstance(reasoning, list):
            parts = []
            for item in reasoning:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)

        # Content blocks: OpenAI reasoning models emit thinking blocks in content
        content = getattr(candidate, "content", None)
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                    parts.append(block["thinking"])
                elif block.get("type") == "reasoning_content":
                    if isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif isinstance(block.get("reasoning"), str):
                        parts.append(block["reasoning"])
            return "".join(parts)

        return ""

    @staticmethod
    def _detect_reasoning_source(payload: Any) -> str:
        """Report which field/provider convention produced the reasoning delta."""
        candidate = payload
        if isinstance(payload, tuple) and payload:
            candidate = payload[0]

        reasoning = getattr(candidate, "reasoning_content", None)
        if isinstance(reasoning, str):
            return "attribute.reasoning_content"

        additional = getattr(candidate, "additional_kwargs", None) or {}

        reasoning = additional.get("reasoning_content")
        if isinstance(reasoning, str):
            return "additional_kwargs.reasoning_content"
        if isinstance(reasoning, list):
            return "additional_kwargs.reasoning_content[]"

        reasoning = additional.get("reasoning")
        if reasoning is not None:
            return "additional_kwargs.reasoning"

        content = getattr(candidate, "content", None)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "thinking":
                        return "content.thinking"
                    if block.get("type") == "reasoning_content":
                        return "content.reasoning_content"

        return "unknown"

    @staticmethod
    def _sse(event: str, payload: dict[str, Any]) -> dict[str, str]:
        return {
            "event": event,
            "data": json.dumps(payload, ensure_ascii=False),
        }

    @staticmethod
    def _extract_hitl_interrupts(item: Any) -> list[tuple[str, dict[str, Any], str]]:
        payload = item[1] if isinstance(item, tuple) and len(item) == 2 else item
        if not isinstance(payload, dict):
            return []
        interrupts = payload.get("__interrupt__")
        if not interrupts:
            return []
        if not isinstance(interrupts, (list, tuple)):
            interrupts = [interrupts]
        extracted: list[tuple[str, dict[str, Any], str]] = []
        for interrupt_item in interrupts:
            value = getattr(interrupt_item, "value", interrupt_item)
            if isinstance(value, dict) and value.get("type") in {
                "permission_request",
                "dimension_build_rule_request",
                "logical_dataset_rule_request",
                "database_sql_revision_request",
            }:
                request = value.get("request")
                if isinstance(request, dict):
                    interrupt_id = str(getattr(interrupt_item, "id", "") or "")
                    extracted.append((str(value["type"]), request, interrupt_id))
        return extracted

    @classmethod
    def _extract_hitl_interrupt(cls, item: Any) -> tuple[str, dict[str, Any]] | None:
        """Backward-compatible single-interrupt view used by older callers."""

        interrupts = cls._extract_hitl_interrupts(item)
        if not interrupts:
            return None
        interrupt_type, request, _interrupt_id = interrupts[0]
        return interrupt_type, request

    async def _astream_with_hitl_resume(
        self,
        agent: Any,
        initial_input: Any,
        *,
        stream_mode: list[str],
        config: dict[str, Any],
        context: dict[str, Any],
        trace_collector: TraceCollector,
    ) -> AsyncGenerator[Any, None]:
        graph_input = initial_input
        while True:
            # Parallel graph nodes may publish their interrupts in separate
            # stream items. Drain this invocation completely and deduplicate
            # the pause set before asking the user or constructing a resume.
            pending_by_key: dict[str, tuple[str, dict[str, Any], str]] = {}
            async for item in agent.astream(
                graph_input,
                stream_mode=stream_mode,
                config=config,
                context=context,
            ):
                hitl_items = self._extract_hitl_interrupts(item)
                if hitl_items:
                    for interrupted_type, interrupted_request, interrupt_id in hitl_items:
                        request_id = str(interrupted_request.get("id") or "")
                        key = interrupt_id or f"{interrupted_type}:{request_id}"
                        pending_by_key.setdefault(
                            key,
                            (interrupted_type, interrupted_request, interrupt_id),
                        )
                    continue
                yield item

            pending_interrupts = list(pending_by_key.values())
            if not pending_interrupts:
                return

            required_events = {
                "permission_request": "permission_required",
                "dimension_build_rule_request": "dimension_build_rule_required",
                "logical_dataset_rule_request": "logical_dataset_rule_required",
                "database_sql_revision_request": "database_sql_revision_required",
            }
            for interrupted_type, interrupted_request, _interrupt_id in pending_interrupts:
                yield self._sse(required_events[interrupted_type], interrupted_request)

            resume_registries = {
                "permission_request": permission_resume_registry,
                "dimension_build_rule_request": dimension_build_resume_registry,
                "logical_dataset_rule_request": logical_dataset_resume_registry,
                "database_sql_revision_request": database_sql_revision_resume_registry,
            }
            span_names = {
                "permission_request": "permission.decision",
                "dimension_build_rule_request": "dimension_build_rule.decision",
                "logical_dataset_rule_request": "logical_dataset_rule.decision",
                "database_sql_revision_request": "database_sql_revision.decision",
            }
            resolved_events = {
                "permission_request": "permission_resolved",
                "dimension_build_rule_request": "dimension_build_rule_resolved",
                "logical_dataset_rule_request": "logical_dataset_rule_resolved",
                "database_sql_revision_request": "database_sql_revision_resolved",
            }
            decisions = await asyncio.gather(
                *(
                    resume_registries[interrupted_type].wait(str(interrupted_request.get("id") or ""))
                    for interrupted_type, interrupted_request, _interrupt_id in pending_interrupts
                )
            )
            resolved: list[tuple[str, dict[str, Any], str, dict[str, Any]]] = []
            for (interrupted_type, interrupted_request, interrupt_id), decision in zip(
                pending_interrupts,
                decisions,
                strict=True,
            ):
                request_id = str(interrupted_request.get("id") or "")
                approved = decision.get("type") == "approve" or decision.get("action") in {"confirm", "agree", "modify"}
                trace_collector.add_custom_span(
                    span_names[interrupted_type],
                    {"request_id": request_id, "interrupt_id": interrupt_id, "decision": decision},
                    span_type="permission" if interrupted_type == "permission_request" else "hitl",
                    metadata={
                        "harness": {
                            "mechanism": "permission",
                            "pillars": [{"name": "architectural_constraints", "role": "primary"}],
                        },
                        "hitl": {
                            "request_id": request_id,
                            "interrupt_id": interrupt_id,
                            "type": interrupted_request.get("type"),
                            "decision": decision.get("type") or decision.get("action"),
                            "outcome": "approved" if approved else "rejected",
                        },
                    },
                )
                yield self._sse(
                    resolved_events[interrupted_type],
                    {"request_id": request_id, "interrupt_id": interrupt_id, "decision": decision},
                )
                resolved.append((interrupted_type, interrupted_request, interrupt_id, decision))

            if all(interrupt_id for _, _, interrupt_id, _ in resolved):
                resume_by_interrupt_id: dict[str, Any] = {}
                for interrupted_type, _request, interrupt_id, decision in resolved:
                    resume_by_interrupt_id[interrupt_id] = (
                        {"decisions": [decision]} if interrupted_type == "permission_request" else decision
                    )
                graph_input = Command(resume=resume_by_interrupt_id)
                continue

            if len(resolved) == 1:
                interrupted_type, _request, _interrupt_id, decision = resolved[0]
                resume_value = {"decisions": [decision]} if interrupted_type == "permission_request" else decision
                graph_input = Command(resume=resume_value)
                continue

            raise RuntimeError("并行 HITL 恢复缺少 LangGraph interrupt id。")

    @staticmethod
    def _tool_call_id(tool_call: Any) -> str:
        if isinstance(tool_call, dict):
            return str(tool_call.get("id") or "")
        return str(getattr(tool_call, "id", "") or "")

    @staticmethod
    def _tool_call_name(tool_call: Any) -> str:
        if isinstance(tool_call, dict):
            return str(tool_call.get("name") or tool_call.get("tool") or "unknown_tool")
        return str(getattr(tool_call, "name", None) or getattr(tool_call, "tool", None) or "unknown_tool")

    @staticmethod
    def _tool_call_args(tool_call: Any) -> Any:
        if isinstance(tool_call, dict):
            return tool_call.get("args", tool_call.get("input", {}))
        return getattr(tool_call, "args", getattr(tool_call, "input", {}))

    @staticmethod
    def _format_tool_input(value: Any, *, limit: int = 2000) -> str:
        try:
            if isinstance(value, str):
                text = value
            else:
                text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        return text[:limit]

    @staticmethod
    def _tool_message_name(tool_msg: Any, pending: dict[str, dict[str, str]]) -> str:
        name = getattr(tool_msg, "name", None)
        if name:
            return str(name)
        tc_id = str(getattr(tool_msg, "tool_call_id", "") or "")
        return pending.get(tc_id, {}).get("tool", "unknown_tool")

    @staticmethod
    def _tool_message_output(tool_msg: Any) -> str:
        content = getattr(tool_msg, "content", "")
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            return str(content)

    @staticmethod
    def _tool_message_artifact(tool_msg: Any) -> dict[str, Any]:
        artifact = getattr(tool_msg, "artifact", None)
        return dict(artifact) if isinstance(artifact, dict) else {}

    @classmethod
    def _tool_message_original_output(cls, tool_msg: Any) -> str:
        artifact = cls._tool_message_artifact(tool_msg)
        value = artifact.get(RAW_OUTPUT_ARTIFACT_KEY)
        return str(value) if value is not None else cls._tool_message_output(tool_msg)

    @classmethod
    def _tool_message_context_fields(
        cls,
        tool_msg: Any,
        *,
        session_id: str,
        tool_call_id: str,
        original_output: str,
    ) -> dict[str, Any]:
        artifact = cls._tool_message_artifact(tool_msg)
        context_output = artifact.get(CONTEXT_OUTPUT_ARTIFACT_KEY)
        if not context_output or not tool_call_id:
            return {}
        source_hash = session_manager._tool_context_source_hash(original_output)
        return {
            "context_output": str(context_output),
            "raw_output_ref": session_manager._tool_context_raw_ref(
                session_id,
                tool_call_id,
                original_output,
                source_hash,
            ),
            "context_compaction": {
                "status": "ready",
                "source_hash": source_hash,
                "policy_version": str(artifact.get(CONTEXT_POLICY_ARTIFACT_KEY) or TOOL_CONTEXT_POLICY_VERSION),
                "method": str(artifact.get(CONTEXT_METHOD_ARTIFACT_KEY) or "immediate_head_tail"),
                "compacted_at": time.time(),
            },
        }

    @staticmethod
    def _is_tool_error(tool_msg: Any, output: str) -> bool:
        status = getattr(tool_msg, "status", None)
        if status == "error":
            return True
        return output.lstrip().lower().startswith(("error:", "exception:", "traceback"))

    @staticmethod
    def _build_segment_trace_spans(
        collector: TraceCollector,
        segments: list[dict[str, Any]],
        accumulated_reasoning: str,
    ) -> list[Any]:
        """Convert UI segments into trace spans under the collector root."""

        segment_spans: list[Any] = []
        for seg_idx, segment in enumerate(segments):
            content = segment.get("content", "")
            timeline = segment.get("timeline") or []
            has_payload = bool(content or timeline)
            if not has_payload:
                continue

            llm_span = TraceSpan(
                span_id=f"{collector.trace_id}-segment-{seg_idx}",
                parent_id=collector.root.id,
                span_type="llm",
                name=f"model.{seg_idx}",
                started_at=collector.started_at,
                input_data=None,
                metadata={"segment_index": seg_idx},
            )
            llm_span.output = content
            llm_span.completed_at = time.time()
            llm_span.status = "completed"

            for item in timeline:
                item_type = item.get("type")
                if item_type == "reasoning":
                    reasoning_span = TraceSpan(
                        span_id=f"{collector.trace_id}-segment-{seg_idx}-reasoning-{len(llm_span.children)}",
                        parent_id=llm_span.id,
                        span_type="reasoning",
                        name="reasoning",
                        started_at=collector.started_at,
                    )
                    reasoning_span.output = item.get("content", "")
                    reasoning_span.completed_at = time.time()
                    reasoning_span.status = "completed"
                    llm_span.children.append(reasoning_span)
                elif item_type == "tool":
                    tc = item.get("tool_call") or {}
                    tool_span = TraceSpan(
                        span_id=f"{collector.trace_id}-segment-{seg_idx}-tool-{len(llm_span.children)}",
                        parent_id=llm_span.id,
                        span_type=DeepAgentsAgentManager._trace_type_for_tool(
                            str(tc.get("tool", "unknown_tool")),
                            str(tc.get("input", "")),
                        ),
                        name=tc.get("tool", "unknown_tool"),
                        started_at=collector.started_at,
                        input_data=tc.get("input"),
                        metadata={"tool_call_id": tc.get("id")},
                    )
                    tool_span.output = tc.get("output")
                    tool_span.completed_at = time.time()
                    tool_span.status = "error" if tc.get("is_error") else "completed"
                    llm_span.children.append(tool_span)

            segment_spans.append(llm_span)

        return segment_spans

    @staticmethod
    def _trace_type_for_tool(tool_name: str, tool_input: str = "") -> str:
        lower_name = tool_name.lower()
        lower_input = tool_input.lower()
        if lower_name in {"task", "delegate"} or "subagent" in lower_name:
            return "subagent"
        if lower_name == "execute_skill" or "skill" in lower_name:
            return "skill"
        if lower_name.startswith("save_") and lower_name.endswith("_memory"):
            return "memory"
        if lower_name in {
            "search_user_memories",
            "search_feedback_memories",
            "search_project_memories",
            "search_reference_memories",
        }:
            return "memory"
        if lower_name in {"read_file", "write_file"} and "memory" in lower_input:
            return "memory"
        if lower_name == "read_external_file":
            return "permission"
        if lower_name == "read_resource" and "att_" not in str(tool_input):
            return "permission"
        return "tool"

    @staticmethod
    def _tool_harness_metadata(span_type: str) -> dict[str, Any]:
        mechanism_by_type = {
            "skill": "tool_use",
            "memory": "context_management",
            "subagent": "subagents",
            "tool": "tool_use",
            "permission": "permission",
        }
        pillars_by_type = {
            "memory": [
                {"name": "context_engineering", "role": "primary"},
                {"name": "garbage_collection", "role": "supporting"},
            ],
            "subagent": [
                {"name": "context_engineering", "role": "primary"},
                {"name": "architectural_constraints", "role": "supporting"},
                {"name": "garbage_collection", "role": "supporting"},
            ],
            "tool": [{"name": "architectural_constraints", "role": "primary"}],
            "permission": [{"name": "architectural_constraints", "role": "primary"}],
            "skill": [{"name": "architectural_constraints", "role": "primary"}],
        }
        return {
            "harness": {
                "mechanism": mechanism_by_type.get(span_type, "tool_use"),
                "pillars": pillars_by_type.get(
                    span_type,
                    [{"name": "architectural_constraints", "role": "primary"}],
                ),
            }
        }

    @staticmethod
    def _todo_diff(
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        before = {str(item.get("id", idx)): item for idx, item in enumerate(previous)}
        after = {str(item.get("id", idx)): item for idx, item in enumerate(current)}
        added = [after[key] for key in after.keys() - before.keys()]
        removed = [before[key] for key in before.keys() - after.keys()]
        updated: list[dict[str, Any]] = []
        for key in before.keys() & after.keys():
            if before[key] != after[key]:
                updated.append({"id": key, "before": before[key], "after": after[key]})
        return {"added": added, "updated": updated, "removed": removed}

    @staticmethod
    def _langsmith_callbacks() -> list[Any] | None:
        """Return LangSmith tracer callbacks only when explicitly enabled.

        Local white-box trace collection is always on via TraceCollector;
        LangSmith is an optional cloud complement controlled by env vars.
        """

        if os.environ.get("LANGSMITH_TRACING", "").lower() not in {"true", "1", "yes"}:
            return None
        if not os.environ.get("LANGSMITH_API_KEY"):
            return None
        try:
            from langchain.callbacks.tracers import LangChainTracer

            return [LangChainTracer()]
        except Exception:
            return None

    @staticmethod
    def _normalize_todos(raw_todos: list[Any]) -> list[dict[str, Any]]:
        """Normalize DeepAgents todo state into a plain JSON-serializable list."""

        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_todos):
            if not item:
                continue
            if isinstance(item, dict):
                todo = dict(item)
            else:
                # TodoListMiddleware may use dataclasses or pydantic models.
                todo = {
                    "content": getattr(item, "content", str(item)),
                    "status": getattr(item, "status", "pending"),
                }
            todo.setdefault("id", f"todo-{idx}")
            todo.setdefault("content", "")
            todo.setdefault("status", "pending")
            todo.setdefault("created_at", time.time())
            todo.setdefault("updated_at", time.time())
            normalized.append(todo)
        return normalized

    @staticmethod
    def _metadata_node(metadata: Any) -> str:
        if isinstance(metadata, dict):
            return str(metadata.get("langgraph_node") or "")
        return ""

    @staticmethod
    def _graph_structure(agent: Any) -> dict[str, Any] | None:
        """Extract LangGraph nodes and edges for frontend visualization.

        Falls back gracefully when the graph object is not available or uses
        an unexpected API shape.
        """
        try:
            get_graph = getattr(agent, "get_graph", lambda **_kwargs: None)
            try:
                graph = get_graph(xray=True)
            except TypeError:
                graph = get_graph()
            if graph is None:
                return None
            mermaid: str | None = None
            mermaid_png_data_url: str | None = None
            try:
                draw_mermaid = getattr(graph, "draw_mermaid", None)
                if callable(draw_mermaid):
                    mermaid = draw_mermaid()
            except Exception:
                logger.debug("Unable to draw LangGraph mermaid source", exc_info=True)
            try:
                draw_mermaid_png = getattr(graph, "draw_mermaid_png", None)
                if callable(draw_mermaid_png):
                    png_bytes = draw_mermaid_png(max_retries=1, retry_delay=0.2)
                    if isinstance(png_bytes, bytes) and png_bytes:
                        encoded = base64.b64encode(png_bytes).decode("ascii")
                        mermaid_png_data_url = f"data:image/png;base64,{encoded}"
            except Exception:
                logger.debug("Unable to draw LangGraph mermaid PNG", exc_info=True)
            nodes: list[dict[str, Any]] = []
            edges: list[dict[str, Any]] = []
            for node in getattr(graph, "nodes", []) or []:
                node_id = ""
                node_type = "normal"
                node_data = node
                if isinstance(node, tuple):
                    node_id = str(node[0]) if node else ""
                    node_data = node[1] if len(node) > 1 else None
                elif isinstance(node, dict):
                    node_id = str(node.get("id", ""))
                    node_type = str(node.get("type", "normal"))
                else:
                    node_id = str(getattr(node, "id", getattr(node, "name", str(node))))
                    node_type = str(getattr(node, "type", "normal"))
                nodes.append({"id": node_id, "type": node_type, "data": node_data})
            for edge in getattr(graph, "edges", []) or []:
                if isinstance(edge, tuple):
                    if len(edge) >= 2:
                        edges.append({"source": str(edge[0]), "target": str(edge[1])})
                elif isinstance(edge, dict):
                    source = str(edge.get("source", ""))
                    target = str(edge.get("target", ""))
                    if source and target:
                        edges.append({"source": source, "target": target})
                else:
                    source = str(getattr(edge, "source", ""))
                    target = str(getattr(edge, "target", ""))
                    if source and target:
                        edges.append({"source": source, "target": target})
            result: dict[str, Any] = {"nodes": nodes, "edges": edges}
            if mermaid:
                result["mermaid"] = mermaid
            if mermaid_png_data_url:
                result["mermaid_png_data_url"] = mermaid_png_data_url
            return result
        except Exception:
            logger.debug("Unable to extract LangGraph structure", exc_info=True)
            return None

    @staticmethod
    def _segment_has_payload(segment: dict[str, Any]) -> bool:
        return bool(segment.get("content") or segment.get("tool_calls") or segment.get("sources"))

    @staticmethod
    def _append_reasoning_to_timeline(segment: dict[str, Any], text: str) -> None:
        """Append reasoning text to the current reasoning item, or create one."""
        if not text:
            return
        timeline = segment.setdefault("timeline", [])
        current = segment.get("_current_reasoning")
        if current is None:
            current = {
                "type": "reasoning",
                "content": "",
                "id": f"reasoning-{len(timeline)}",
            }
            timeline.append(current)
            segment["_current_reasoning"] = current
        current["content"] += text

    @staticmethod
    def _finalize_reasoning_timeline(segment: dict[str, Any]) -> None:
        """Close the current reasoning chunk so the next reasoning starts a new item."""
        segment["_current_reasoning"] = None

    @staticmethod
    def _add_tool_start_to_timeline(
        segment: dict[str, Any],
        tool_call_id: str,
        tool_name: str,
        tool_input: str,
    ) -> None:
        """Add a tool_start item to the timeline."""
        timeline = segment.setdefault("timeline", [])
        timeline.append(
            {
                "type": "tool",
                "tool_call": {
                    "id": tool_call_id,
                    "tool": tool_name,
                    "input": tool_input,
                    "status": "running",
                },
                "id": tool_call_id or f"tool-{len(timeline)}",
            }
        )

    @staticmethod
    def _update_tool_end_in_timeline(
        segment: dict[str, Any],
        tool_call_id: str,
        output: str,
        is_error: bool,
    ) -> None:
        """Update the matching tool item in the timeline with its result."""
        timeline = segment.get("timeline", [])
        for item in reversed(timeline):
            if item.get("type") == "tool":
                tc = item.get("tool_call", {})
                if tc.get("id") == tool_call_id:
                    tc["output"] = output
                    tc["is_error"] = is_error
                    tc["status"] = "error" if is_error else "completed"
                    break

    @staticmethod
    def _mark_pending_tools_interrupted(
        segment: dict[str, Any],
        pending_tool_starts: dict[str, dict[str, str]],
        output: str,
    ) -> None:
        """Persist started-but-unfinished tools as interrupted, never running."""

        for tc_id, pending in list(pending_tool_starts.items()):
            matched = False
            for tc in segment.get("tool_calls", []):
                if tc.get("id") == tc_id:
                    tc.setdefault("tool", pending.get("tool", "unknown_tool"))
                    tc.setdefault("input", pending.get("input", ""))
                    tc["output"] = tc.get("output") or output
                    tc["summary_source"] = tc.get("summary_source") or "stream_cancelled"
                    tc["is_error"] = True
                    tc["completed_at"] = tc.get("completed_at") or time.time()
                    matched = True
                    break
            if not matched:
                segment.setdefault("tool_calls", []).append(
                    {
                        "tool": pending.get("tool", "unknown_tool"),
                        "input": pending.get("input", ""),
                        "id": tc_id,
                        "output": output,
                        "summary_source": "stream_cancelled",
                        "is_error": True,
                        "completed_at": time.time(),
                    }
                )
            DeepAgentsAgentManager._update_tool_end_in_timeline(segment, tc_id or "", output, True)
        pending_tool_starts.clear()

    @staticmethod
    def _strip_runtime_segment_fields(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for segment in segments:
            next_segment = dict(segment)
            next_segment.pop("_current_reasoning", None)
            next_segment["content"] = DeepAgentsAgentManager._strip_model_call_limit_notice(
                str(next_segment.get("content") or "")
            )
            cleaned.append(next_segment)
        return cleaned

    @staticmethod
    def _mark_terminal_candidate_segments(
        segments: list[dict[str, Any]],
        final_state: str | None,
    ) -> None:
        """Project one terminal candidate; every earlier segment is progress."""

        final_candidate_index = next(
            (
                index
                for index in range(len(segments) - 1, -1, -1)
                if str(segments[index].get("content") or "").strip()
                or segments[index].get("tool_calls")
            ),
            None,
        )
        for index, segment in enumerate(segments):
            if final_state is None:
                segment.pop("verification_state", None)
            elif index == final_candidate_index:
                segment["verification_state"] = final_state
            else:
                segment["verification_state"] = "progress"

    def _persist_partial_run(
        self,
        *,
        session_id: str,
        query_id: str,
        user_message: str,
        attachments: list[dict[str, Any]] | None,
        segments: list[dict[str, Any]],
        active_segment: dict[str, Any],
        pending_tool_starts: dict[str, dict[str, str]],
        accumulated_reasoning: str,
        turn_sources: list[dict[str, Any]],
        output_attachments: list[dict[str, Any]] | None = None,
        user_message_persisted: bool = False,
        status: str = "cancelled",
        interruption_notice: str | None = None,
        error_notice: str | None = None,
        pending_tool_output: str = "Tool execution was interrupted because the user stopped the run.",
    ) -> None:
        """Save the visible partial run after client cancellation.

        This is the durable user-facing record. Checkpoints remain an execution
        detail for HITL, not the source of truth for chat continuity.
        """

        self._mark_pending_tools_interrupted(active_segment, pending_tool_starts, pending_tool_output)
        self._mark_terminal_candidate_segments(segments, "unverified")
        if status == "cancelled":
            interruption_notice = interruption_notice or "本轮已被用户停止，以上为中断前已完成的部分结果。"

        if not user_message_persisted:
            session_manager.save_message(
                session_id,
                "user",
                self._display_message_with_attachments(user_message, attachments),
                attachments=attachments,
            )
        self._persist_assistant_snapshot(
            session_id=session_id,
            query_id=query_id,
            segments=segments,
            accumulated_reasoning=accumulated_reasoning,
            turn_sources=turn_sources,
            output_attachments=output_attachments,
            interrupted=status == "cancelled",
            interruption_notice=interruption_notice,
            error_notice=error_notice,
            status=status,
        )

    def _persist_assistant_snapshot(
        self,
        *,
        session_id: str,
        query_id: str,
        segments: list[dict[str, Any]],
        accumulated_reasoning: str,
        turn_sources: list[dict[str, Any]],
        output_attachments: list[dict[str, Any]] | None = None,
        session_sources: list[dict[str, Any]] | None = None,
        interrupted: bool = False,
        interruption_notice: str | None = None,
        error_notice: str | None = None,
        status: str = "running",
    ) -> bool:
        """Persist the current assistant draft without duplicating the turn."""

        cleaned_segments = self._strip_runtime_segment_fields(segments)
        full_content = "\n\n".join(str(seg.get("content") or "") for seg in cleaned_segments if seg.get("content"))
        all_tool_calls = [tc for seg in cleaned_segments for tc in seg.get("tool_calls", [])]
        all_timeline = [item for seg in cleaned_segments for item in seg.get("timeline", [])]
        if not (
            full_content
            or all_tool_calls
            or accumulated_reasoning
            or all_timeline
            or turn_sources
            or output_attachments
        ):
            return False
        message_sources, final_citations = resolve_message_citations(
            full_content,
            turn_sources,
            session_sources,
        )
        session_manager.upsert_assistant_message(
            session_id,
            query_id=query_id,
            content=full_content,
            tool_calls=all_tool_calls or None,
            sources=message_sources or None,
            citations=final_citations or None,
            reasoning_content=accumulated_reasoning or None,
            timeline=all_timeline or None,
            segments=cleaned_segments or None,
            interrupted=interrupted,
            interruption_notice=interruption_notice,
            error_notice=error_notice,
            output_attachments=output_attachments,
            status=status,
        )
        return True

    @staticmethod
    def _last_ai_content(state: dict[str, Any] | None) -> str:
        if not state:
            return ""
        messages = state.get("messages") or []
        for msg in reversed(messages):
            msg_type = getattr(msg, "type", None)
            if msg_type not in {None, "ai"}:
                continue
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content:
                return DeepAgentsAgentManager._strip_model_call_limit_notice(content)
        return ""

    _MODEL_CALL_LIMIT_NOTICE_RE = re.compile(
        r"(?:\r?\n){0,2}Model call limits exceeded:\s*"
        r"(?:run|thread) limit\s*\(\d+\s*/\s*\d+\)\.?\s*$",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_model_call_limit_notice(cls, content: str) -> str:
        """Remove DeepAgents' internal circuit-breaker sentinel from user text."""

        if not content:
            return content
        return cls._MODEL_CALL_LIMIT_NOTICE_RE.sub("", content).rstrip()

    @staticmethod
    def _filter_model_limit_stream_delta(
        buffer: str,
        delta: str,
        suppressing: bool,
    ) -> tuple[str, str, bool]:
        """Hold a possible split sentinel until it is safe to emit as text."""

        if suppressing:
            return "", "", True
        marker = "model call limits exceeded:"
        combined = f"{buffer}{delta}"
        lowered = combined.lower()
        marker_index = lowered.find(marker)
        if marker_index >= 0:
            return combined[:marker_index].rstrip(), "", True
        keep = 0
        max_keep = min(len(combined), len(marker) - 1)
        for size in range(max_keep, 0, -1):
            if marker.startswith(lowered[-size:]):
                keep = size
                break
        if keep:
            buffer_start = len(combined) - keep
            newline_chars = 0
            while (
                buffer_start > 0
                and combined[buffer_start - 1] in {"\r", "\n"}
                and newline_chars < 4
            ):
                buffer_start -= 1
                newline_chars += 1
            return combined[:buffer_start], combined[buffer_start:], False
        return combined, "", False

    @staticmethod
    def _parse_sse_payload(event: dict[str, str]) -> dict[str, Any]:
        try:
            payload = json.loads(event.get("data") or "{}")
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _goal_auto_continue_reason(
        *,
        outcome: dict[str, Any],
        verification: dict[str, Any],
        goal: dict[str, Any],
    ) -> str | None:
        """Return a recoverable cross-Run reason, never a generic retry reason."""

        if (
            not goal
            or goal.get("status") != "active"
            or goal.get("requested_status") in {"paused", "cancelled"}
        ):
            return None
        round_number = int(goal.get("round") or 0)
        max_rounds = int(goal.get("max_rounds") or 0)
        if max_rounds <= 0 or round_number >= max_rounds:
            return None
        if goal.get("pending_revision"):
            return "goal_revised"
        if (
            outcome.get("outcome") == RunOutcome.BUDGET_EXCEEDED.value
            and outcome.get("budget_exhaustion_reason") == "run_model_call_limit"
        ):
            return "run_model_call_limit"
        report = verification.get("report")
        report_status = report.get("status") if isinstance(report, dict) else None
        if report_status in {
            VerificationStatus.INCOMPLETE.value,
            VerificationStatus.GRADER_ERROR.value,
            VerificationStatus.INFRASTRUCTURE_ERROR.value,
        }:
            retry_count = int(goal.get("consecutive_control_failure_count") or 0)
            retry_limit = int(goal.get("max_control_retries") or 2)
            total_retry_count = int(goal.get("total_control_retry_count") or 0)
            total_retry_limit = int(goal.get("max_total_control_retries") or 4)
            if (
                retry_count < max(1, retry_limit)
                and total_retry_count < max(1, total_retry_limit)
            ):
                return "verification_control_retry"
        if (
            outcome.get("outcome") == RunOutcome.VERIFICATION_FAILED.value
            and report_status
            in {
                VerificationStatus.NEEDS_REVISION.value,
                VerificationStatus.FAILED.value,
                VerificationStatus.MAX_ITERATIONS_REACHED.value,
            }
        ):
            return "verification_failed"
        return None

    @staticmethod
    def _goal_continuation_prompt(
        *,
        reason: str,
        goal: dict[str, Any],
        verification: dict[str, Any],
    ) -> str:
        report = verification.get("report")
        # A single-Run model-call boundary is control-plane state, not a
        # failed acceptance criterion. Continue from the Goal's real gaps.
        if reason == "run_model_call_limit":
            gaps = goal.get("gaps") if isinstance(goal.get("gaps"), list) else []
        else:
            gaps = report.get("gaps") if isinstance(report, dict) else None
            if not isinstance(gaps, list):
                gaps = goal.get("gaps") if isinstance(goal.get("gaps"), list) else []
        gap_text = "\n".join(f"- {str(gap)}" for gap in gaps if str(gap).strip())
        reason_text = {
            "run_model_call_limit": "上一 Run 已达到单轮模型调用上限",
            "goal_revised": "用户已更新 Goal 描述，旧 Run 的结果不再作为新目标的完成判定",
            "verification_control_retry": "上一 Run 的验收控制流程发生可恢复异常，Harness 正在有限自动重试",
        }.get(reason, "上一 Run 的验收仍有待修正项")
        objective = str(goal.get("objective") or "").strip()
        return (
            "继续完成当前 Goal，不要把这条内部续跑指令当作新的用户需求。\n"
            + (f"当前 Goal（最新修订）：\n{objective}\n" if objective else "")
            + f"{reason_text}。读取当前 workspace、Todo、Goal 验收缺口和已有产物，"
            "从未完成处继续，避免重复已经完成的工作。"
            + (f"\n当前待补齐项：\n{gap_text}" if gap_text else "")
        )

    async def astream(
        self,
        *,
        message: str,
        session_id: str,
        project_id: str | None = None,
        analytics_model_id: str | None = None,
        user_id: str = "default_user",
        attachments: list[dict[str, Any]] | None = None,
        user_message_already_persisted: bool = False,
        goal_mode: bool = False,
        goal_id: str | None = None,
    ) -> AsyncGenerator[dict[str, str], None]:
        """Stream one user request and autonomously advance recoverable Goal Runs."""

        existing_goal = (
            session_manager.get_goal_state(session_id, goal_id)
            if goal_id
            else session_manager.get_active_goal_state(session_id) if goal_mode else None
        )
        current_objective = (
            str(existing_goal.get("objective") or message)
            if isinstance(existing_goal, dict)
            else message
        )
        current_message = message
        current_goal_id = (
            str(existing_goal.get("goal_id"))
            if isinstance(existing_goal, dict) and existing_goal.get("goal_id")
            else goal_id
        )
        internal_continuation = False
        while True:
            if internal_continuation and current_goal_id:
                latest_goal = session_manager.get_goal_state(session_id, current_goal_id)
                if isinstance(latest_goal, dict):
                    current_objective = str(
                        latest_goal.get("objective") or current_objective
                    )
            last_done: dict[str, str] | None = None
            outcome_payload: dict[str, Any] = {}
            verification_payload: dict[str, Any] = {}
            goal_payload: dict[str, Any] = {}
            run_limit_payload: dict[str, Any] = {}
            async for event in self._astream_single_run(
                message=current_message,
                session_id=session_id,
                project_id=project_id,
                analytics_model_id=analytics_model_id,
                user_id=user_id,
                attachments=[] if internal_continuation else attachments,
                user_message_already_persisted=(
                    user_message_already_persisted or internal_continuation
                ),
                goal_mode=goal_mode,
                goal_id=current_goal_id,
                run_objective=current_objective,
                internal_continuation=internal_continuation,
            ):
                event_name = event.get("event")
                payload = self._parse_sse_payload(event)
                if event_name == "done":
                    last_done = event
                    continue
                if event_name == "run_outcome":
                    outcome_payload = payload
                elif event_name == "verification_report":
                    verification_payload = payload
                elif event_name == "run_limit_reached":
                    run_limit_payload = payload
                elif event_name in {"goal_created", "goal_updated", "goal_status_changed"}:
                    candidate = payload.get("goal")
                    if isinstance(candidate, dict):
                        goal_payload = candidate
                yield event

            if goal_payload.get("goal_id"):
                # A pause/cancel can race with the small boundary between Runs.
                # Re-read session JSON, the cross-Run authority, before advancing.
                authoritative = session_manager.get_goal_state(
                    session_id,
                    str(goal_payload["goal_id"]),
                )
                if isinstance(authoritative, dict):
                    goal_payload = authoritative
            continuation_reason = self._goal_auto_continue_reason(
                outcome=outcome_payload,
                verification=verification_payload,
                goal=goal_payload,
            )
            previous_query_id = str(outcome_payload.get("query_id") or "")
            boundary_notice: dict[str, Any] | None = None
            if continuation_reason is not None:
                completed_round = int(goal_payload.get("round") or 0)
                next_round = completed_round + 1
                max_rounds = int(goal_payload.get("max_rounds") or 0)
                boundary_notice = {
                    "reason": continuation_reason,
                    "message": (
                        f"本轮达到模型调用上限，Goal 已自动进入第 {next_round}/{max_rounds} 轮。"
                        if continuation_reason == "run_model_call_limit"
                        else f"目标描述已更新，Goal 将按最新版本进入第 {next_round}/{max_rounds} 轮。"
                        if continuation_reason == "goal_revised"
                        else f"本轮验收仍有待修正项，Goal 已自动进入第 {next_round}/{max_rounds} 轮。"
                    ),
                    "model_call_count": run_limit_payload.get("model_call_count"),
                    "limit": run_limit_payload.get("limit"),
                    "completed_round": completed_round,
                    "next_round": next_round,
                    "max_rounds": max_rounds,
                    "auto_continued": True,
                }
            elif run_limit_payload:
                boundary_notice = {
                    "reason": run_limit_payload.get("reason"),
                    "message": run_limit_payload.get("message"),
                    "model_call_count": run_limit_payload.get("model_call_count"),
                    "limit": run_limit_payload.get("limit"),
                    "auto_continued": False,
                }
            if boundary_notice is not None and previous_query_id:
                try:
                    session_manager.set_assistant_run_boundary_notice(
                        session_id,
                        previous_query_id,
                        boundary_notice,
                    )
                except (FileNotFoundError, ValueError):
                    logger.warning(
                        "Unable to persist Run boundary notice for session=%s query=%s",
                        session_id,
                        previous_query_id,
                        exc_info=True,
                    )
            if continuation_reason is None:
                if last_done is not None:
                    yield last_done
                break

            next_round = int(goal_payload.get("round") or 0) + 1
            max_rounds = int(goal_payload.get("max_rounds") or 0)
            yield self._sse(
                "goal_run_continued",
                {
                    "session_id": session_id,
                    "goal_id": goal_payload.get("goal_id"),
                    "previous_run_id": outcome_payload.get("run_id"),
                    "reason": continuation_reason,
                    "completed_round": int(goal_payload.get("round") or 0),
                    "next_round": next_round,
                    "max_rounds": max_rounds,
                    "model_call_count": int(goal_payload.get("model_call_count") or 0),
                    "message": (
                        f"第 {next_round}/{max_rounds} 轮将自动继续。"
                        if max_rounds > 0
                        else "Goal 将自动进入下一轮。"
                    ),
                },
            )
            current_goal_id = str(goal_payload["goal_id"])
            goal_mode = True
            internal_continuation = True
            current_message = self._goal_continuation_prompt(
                reason=continuation_reason,
                goal=goal_payload,
                verification=verification_payload,
            )

    async def _astream_single_run(
        self,
        *,
        message: str,
        session_id: str,
        project_id: str | None = None,
        analytics_model_id: str | None = None,
        user_id: str = "default_user",
        attachments: list[dict[str, Any]] | None = None,
        user_message_already_persisted: bool = False,
        goal_mode: bool = False,
        goal_id: str | None = None,
        run_objective: str | None = None,
        internal_continuation: bool = False,
    ) -> AsyncGenerator[dict[str, str], None]:
        """Stream exactly one Harness Run; the public method owns Goal looping."""

        query_id = f"query-{uuid.uuid4().hex[:12]}"
        run_record: RunRecord | None = None
        goal_record: GoalRecord | None = None
        trace_collector: TraceCollector | None = None
        trace_context_active = False
        segments: list[dict[str, Any]] = []
        active_segment: dict[str, Any] = {}
        pending_tool_starts: dict[str, dict[str, str]] = {}
        accumulated_reasoning = ""
        turn_sources: list[dict[str, Any]] = []
        published_attachments: list[dict[str, Any]] = []
        session_sources: list[dict[str, Any]] = []
        run_messages_persisted = False
        user_message_persisted = user_message_already_persisted
        title_task: asyncio.Task[str | None] | None = None
        title_event_emitted = False
        checkpoint_thread_id = f"{session_id}:{query_id}"
        try:
            thinking_enabled = bool(config.load_config().get("thinking_mode", False))
            logger.info("Agent stream thinking_mode=%s for session=%s", thinking_enabled, session_id)

            initial_history = session_manager.load_session(session_id)
            initial_user_count = sum(1 for item in initial_history if item.get("role") == "user")
            initial_assistant_count = sum(1 for item in initial_history if item.get("role") == "assistant")
            is_first_query = (
                not internal_continuation
                and initial_assistant_count == 0
                and initial_user_count <= (1 if user_message_persisted else 0)
            )
            if not user_message_persisted and not internal_continuation:
                session_manager.save_message(
                    session_id,
                    "user",
                    self._display_message_with_attachments(message, attachments),
                    attachments=attachments,
                )
                user_message_persisted = True
            if is_first_query:
                provisional_title = _provisional_title(message)
                session_manager.update_title(session_id, provisional_title)
                # Title generation is Session metadata work, independent from
                # Run success. Start semantic refinement immediately and show
                # the durable provisional title without waiting for the Agent.
                title_result = _generate_title(session_id)
                if isinstance(title_result, Awaitable):
                    title_task = asyncio.create_task(title_result)
                elif title_result:
                    session_manager.update_title(session_id, str(title_result))
                yield self._sse(
                    "title",
                    {
                        "session_id": session_id,
                        "title": provisional_title,
                        "provisional": True,
                    },
                )

            workspace_path, metadata = self._resolve_workspace(
                session_id=session_id,
                project_id=project_id,
            )
            # Persist the request value even when it is None so choosing
            # "不使用分析模型" clears a model saved by an earlier turn.
            metadata["analytics_model_id"] = analytics_model_id
            session_manager.update_metadata(session_id, metadata)

            harness_config = config.load_config().get("harness", {})
            goals_config = harness_config.get("goals", {})
            rubric_config = harness_config.get("completion", {}).get("rubric", {})
            goal_max_rounds = goals_config.get("max_rounds", 8)
            if not isinstance(goal_max_rounds, int) or isinstance(goal_max_rounds, bool) or goal_max_rounds <= 0:
                goal_max_rounds = 8
            if goal_mode and not goals_config.get("enabled", True):
                raise ValueError("Goal Mode is disabled by Harness Settings.")
            run_record, goal_record = self._run_coordinator.start_run(
                session_id=session_id,
                query_id=query_id,
                objective=run_objective or message,
                goal_mode=goal_mode,
                goal_id=goal_id,
                project_id=project_id,
                analytics_model_id=analytics_model_id,
                config_snapshot={
                    "completion": harness_config.get("completion", {}),
                    "goals": goals_config,
                    "model_call_limit": harness_config.get("model_call_limit", {}),
                },
                verification_enabled=rubric_config.get("enabled", True),
                goal_max_rounds=goal_max_rounds,
                custom_rubric_rules=(
                    list(rubric_config.get("custom_rules") or [])
                    if rubric_config.get("custom_rules_enabled", False)
                    else []
                ),
            )
            if goal_record is not None:
                current_task = asyncio.current_task()
                if current_task is not None:
                    self._active_goal_tasks[(session_id, goal_record.goal_id)] = current_task
            yield self._sse(
                "run_started",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run": run_record.model_dump(mode="json"),
                },
            )
            if goal_record is not None:
                goal_event = "goal_created" if len(goal_record.run_ids) == 1 else "goal_updated"
                yield self._sse(
                    goal_event,
                    {
                        "session_id": session_id,
                        "query_id": query_id,
                        "goal": goal_record.model_dump(mode="json"),
                    },
                )

            session_manager.ensure_tool_call_ids(session_id)
            raw_history = session_manager.load_session(session_id)
            session_sources = dedupe_sources(
                [
                    source
                    for historical_message in raw_history
                    for source in historical_message.get("sources", []) or []
                    if isinstance(source, dict)
                ]
            )
            history = session_manager.load_session_for_agent(session_id)
            history_for_build = raw_history
            if user_message_persisted and raw_history:
                persisted_display = self._display_message_with_attachments(message, attachments)
                last_message = raw_history[-1]
                if last_message.get("role") == "user" and last_message.get("content") == persisted_display:
                    # The API persists the user turn before opening SSE so an
                    # immediate disconnect cannot lose it. _build_messages adds
                    # the current turn itself, therefore exclude that persisted
                    # copy here or the model receives the prompt twice.
                    history_for_build = raw_history[:-1]
            saved_agent_context = session_manager.get_agent_context_messages(session_id)
            using_saved_agent_context = False
            if saved_agent_context:
                try:
                    messages = [
                        restored
                        for restored in messages_from_dict(saved_agent_context)
                        if not _is_internal_control_message(restored)
                    ]
                    context_attachments = list(attachments or [])
                    if goal_record is not None:
                        known_attachment_ids = {
                            str(item.get("id") or "")
                            for item in context_attachments
                            if isinstance(item, dict)
                        }
                        for historical_message in raw_history:
                            for item in historical_message.get("attachments") or []:
                                if not isinstance(item, dict):
                                    continue
                                attachment_id = str(item.get("id") or "")
                                if attachment_id and attachment_id not in known_attachment_ids:
                                    context_attachments.append(dict(item))
                                    known_attachment_ids.add(attachment_id)
                    messages.append(
                        HumanMessage(
                            content=self._build_user_content(
                                message,
                                context_attachments,
                                session_id=session_id,
                                workspace_path=workspace_path,
                            ),
                            additional_kwargs={"puddingclaw_query_id": query_id},
                        )
                    )
                    using_saved_agent_context = True
                except Exception:
                    logger.warning(
                        "Failed to restore compact Agent context for session=%s; rebuilding from transcript",
                        session_id,
                        exc_info=True,
                    )
                    messages = self._build_messages(
                        history_for_build,
                        message,
                        attachments,
                        session_id=session_id,
                        workspace_path=workspace_path,
                        query_id=query_id,
                    )
            else:
                messages = self._build_messages(
                    history_for_build,
                    message,
                    attachments,
                    session_id=session_id,
                    workspace_path=workspace_path,
                    query_id=query_id,
                )
            if internal_continuation and messages:
                current_human = messages[-1]
                if isinstance(current_human, HumanMessage):
                    current_human.additional_kwargs = {
                        **(current_human.additional_kwargs or {}),
                        "lc_source": "puddingclaw_goal_continuation",
                    }
            historical_tool_call_ids = {
                tc.get("id") for msg in history_for_build for tc in msg.get("tool_calls") or [] if tc.get("id")
            }
            if not user_message_persisted:
                session_manager.save_message(
                    session_id,
                    "user",
                    self._display_message_with_attachments(message, attachments),
                    attachments=attachments,
                )
                user_message_persisted = True

            # Restore persisted todos so the Agent resumes from white-box state
            # instead of relying on checkpoint black-box.
            persisted_todos = session_manager.get_todos(
                session_id,
                goal_id=run_record.goal_id,
                goal_revision=run_record.goal_revision,
                run_id=run_record.run_id,
            )

            model = ModelClientChatModel(role="agent", streaming=True)
            rubric_cfg = config.load_config().get("harness", {}).get("completion", {}).get("rubric", {})
            rubric_model_name = str(rubric_cfg.get("model") or "").strip()
            if not rubric_model_name:
                rubric_model_name = str(
                    config.get_fallback_llm_config(
                        thinking_enabled_override=False,
                    ).get("model")
                    or ""
                ).strip()
            rubric_model = ModelClientChatModel(
                role="rubric",
                streaming=False,
                thinking_enabled=False,
                model_override=rubric_model_name or None,
            )
            agent_skills = ["/skills/"]
            skill_toolsets = discover_skill_toolsets(self._base_dir / "skills")
            agent_backend = self._build_backend(
                workspace_path,
                session_id=session_id,
                query_id=query_id,
            )
            agent_tools = self._build_tools(
                workspace_path,
                session_id=session_id,
                query_id=query_id,
                execution_backend=getattr(
                    agent_backend,
                    "execution_backend",
                    None,
                ),
            )
            backend_mode = str(getattr(agent_backend, "execution_mode", "restricted_host"))
            workspace_id = "sha256:" + hashlib.sha256(str(workspace_path.resolve()).encode("utf-8")).hexdigest()
            self._run_coordinator.bind_execution_snapshot(
                run_record,
                {
                    "backend_mode": backend_mode,
                    "backend_id": str(getattr(agent_backend, "execution_backend_id", "")),
                    "workspace_id": workspace_id,
                    "scratch_host_path": str(
                        getattr(agent_backend, "execution_scratch_host_path", "")
                    ),
                    "fallback_reason": getattr(
                        agent_backend,
                        "execution_fallback_reason",
                        None,
                    ),
                },
            )
            permission_context = RunPermissionContext.from_config_snapshot(run_record.config_snapshot)
            agent_middlewares = self._build_middlewares(
                project_id,
                rubric_model=rubric_model,
                skill_toolsets=skill_toolsets,
                known_tools={str(getattr(tool, "name", "")) for tool in agent_tools if getattr(tool, "name", "")},
                backend_mode=backend_mode,
                permission_context=permission_context,
                workspace_backend=agent_backend,
            )
            main_summarization = _build_deepagents_summarization(model, agent_backend)
            if main_summarization is not None:
                agent_middlewares.append(main_summarization)
            checkpointer = await self._build_checkpointer()
            runtime_inventory = self._runtime_inventory(
                tools=agent_tools,
                skills=agent_skills,
                middleware=agent_middlewares,
                workspace_path=workspace_path,
                checkpointer=self._checkpointer_info,
                execution_backend=agent_backend,
            )
            analytics_contract_active = (
                run_record.verification_contract is not None
                and "analytics" in run_record.verification_contract.verification_packs
            )
            if analytics_contract_active:
                analytics_model_prompt, analytics_model_payload = self._analytics_model_context(analytics_model_id)
            else:
                analytics_model_prompt = ""
                analytics_model_payload = (
                    {
                        "id": analytics_model_id,
                        "loaded": False,
                        "deferred": True,
                    }
                    if analytics_model_id
                    else None
                )
            if analytics_model_payload:
                runtime_inventory["analytics_model"] = analytics_model_payload
            traced_middlewares = wrap_middlewares_for_trace(agent_middlewares)
            logger.info("Building DeepAgents agent for session=%s project=%s", session_id, project_id)

            def build_subagent_middlewares() -> list[Any]:
                middlewares: list[Any] = [
                    ExternalFilePermissionMiddleware(),
                    VerificationActivationMiddleware(),
                    ToolExecutionPipeline(
                        known_tools={
                            str(getattr(tool, "name", "")) for tool in agent_tools if getattr(tool, "name", "")
                        },
                        backend_mode=backend_mode,
                        permission_context=permission_context,
                        base_dir=self._base_dir,
                    ),
                    SkillIntentRouterMiddleware(),
                    ToolsetMiddleware(
                        skills_dir=self._base_dir / "skills",
                        toolsets_by_skill=skill_toolsets,
                    ),
                ]
                summarization = _build_deepagents_summarization(model, agent_backend)
                if summarization is not None:
                    middlewares.append(summarization)
                return middlewares

            subagents = _build_subagents(
                agent_tools,
                agent_skills,
                middleware_factory=build_subagent_middlewares,
            )
            system_prompt = build_deepagents_system_prompt(self._base_dir, workspace_path)
            dependency_prompt = dependency_plan_prompt(getattr(agent_backend, "execution_dependency_plan", None))
            if dependency_prompt:
                system_prompt += f"\n\n{dependency_prompt}"
            if analytics_model_prompt and analytics_contract_active:
                system_prompt += analytics_model_prompt
            agent = create_deep_agent(
                model=model,
                tools=agent_tools,
                skills=agent_skills,
                middleware=traced_middlewares,
                subagents=subagents,
                checkpointer=checkpointer,
                backend=agent_backend,
                system_prompt=system_prompt,
                state_schema=PuddingClawAgentState,
            )
            logger.info("DeepAgents agent built successfully for session=%s", session_id)
            self._run_coordinator.transition(run_record, RunStatus.RUNNING)
            yield self._sse(
                "run_status_changed",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run_id": run_record.run_id,
                    "goal_id": run_record.goal_id or "",
                    "goal_revision": run_record.goal_revision,
                    "status": run_record.status.value,
                },
            )

            graph_structure = self._graph_structure(agent)
            if graph_structure:
                session_manager.update_graph(session_id, graph_structure)
                yield self._sse("graph_structure", graph_structure)

            emitted_text = ""
            model_limit_text_buffer = ""
            suppressing_model_limit_notice = False
            final_state: dict[str, Any] | None = None
            tools_just_finished = False
            emitted_tool_starts: set[str] = set()
            emitted_tool_ends: set[str] = set()
            pending_tool_starts = {}
            turn_sources = []
            # Buffer trace events emitted synchronously by TraceCollector so they
            # can be yielded asynchronously through the SSE stream.
            pending_trace_events: list[dict[str, str]] = []
            trace_collector: TraceCollector | None = None

            def _trace_emit(event: str, payload: dict[str, Any]) -> None:
                pending_trace_events.append(self._sse(event, payload))
                # The live trace already reaches the UI over SSE. Persisting a
                # growing snapshot for every span used to rewrite tens of MB on
                # the event loop and stall unrelated session reads. Completed,
                # cancelled and failed traces are persisted once below.

            trace_collector = TraceCollector(
                session_id=session_id,
                query_id=query_id,
                emit_callback=_trace_emit,
                runtime_inventory=runtime_inventory,
            )
            trace_collector.__enter__()
            trace_context_active = True
            active_llm_span: str | None = None
            model_call_index = 0
            active_graph_node: str | None = None

            def new_segment() -> dict[str, Any]:
                return {
                    "content": "",
                    "tool_calls": [],
                    "timeline": [],
                    "reasoning_content": "",
                    "run_id": run_record.run_id,
                    "goal_id": run_record.goal_id,
                    **(
                        {"verification_state": "pending"}
                        if run_record.verification_enabled
                        else {}
                    ),
                    "_current_reasoning": None,
                }

            segments = [new_segment()]
            active_segment = segments[0]
            chunk_count = 0
            emitted_reasoning = False
            accumulated_reasoning = ""
            reasoning_log_chars = 0
            REASONING_LOG_INTERVAL = 500
            # Track todo state across stream values for white-box persistence.
            previous_todos: list[dict[str, Any]] = list(persisted_todos)
            last_graph_todos: list[dict[str, Any]] = list(persisted_todos)
            rubric_evaluations: list[dict[str, Any]] = []
            deterministic_check_events: list[dict[str, Any]] = []
            last_goal_control_poll = 0.0
            model_call_limit_events: list[dict[str, Any]] = []
            last_snapshot_at = 0.0
            last_snapshot_signature = ""
            last_context_usage = -1
            persisted_summary_cutoff: int | None = None

            def persist_assistant_snapshot(
                *,
                force: bool = False,
                status: str = "running",
                interrupted: bool = False,
                interruption_notice: str | None = None,
                error_notice: str | None = None,
            ) -> None:
                nonlocal last_snapshot_at, last_snapshot_signature
                now = time.time()
                cleaned = self._strip_runtime_segment_fields(segments)
                tool_fingerprint = [
                    (
                        tc.get("id"),
                        tc.get("tool"),
                        bool(tc.get("output")),
                        bool(tc.get("is_error")),
                    )
                    for seg in cleaned
                    for tc in seg.get("tool_calls", [])
                ]
                signature = json.dumps(
                    {
                        "content_lengths": [len(str(seg.get("content") or "")) for seg in cleaned],
                        "reasoning_len": len(accumulated_reasoning),
                        "tools": tool_fingerprint,
                        "sources": len(turn_sources),
                        "output_attachments": [
                            str(item.get("id") or "") for item in published_attachments
                        ],
                        "status": status,
                        "interrupted": interrupted,
                        "interruption_notice": interruption_notice,
                        "error_notice": error_notice,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if not force and signature == last_snapshot_signature:
                    return
                if not force and now - last_snapshot_at < 2:
                    return
                if self._persist_assistant_snapshot(
                    session_id=session_id,
                    query_id=query_id,
                    segments=segments,
                    accumulated_reasoning=accumulated_reasoning,
                    turn_sources=turn_sources,
                    output_attachments=published_attachments,
                    session_sources=session_sources,
                    interrupted=interrupted,
                    interruption_notice=interruption_notice,
                    error_notice=error_notice,
                    status=status,
                ):
                    last_snapshot_at = now
                    last_snapshot_signature = signature

            agent_config: dict[str, Any] = {
                "configurable": {
                    "thread_id": checkpoint_thread_id,
                    "session_id": session_id,
                    "user_id": user_id,
                }
            }
            langsmith_callbacks = self._langsmith_callbacks()
            if langsmith_callbacks:
                agent_config["callbacks"] = langsmith_callbacks

            initial_state: dict[str, Any] = {
                "messages": messages,
                "todos": persisted_todos,
                "analytics_model_id": analytics_model_id,
                "task_profile": run_record.task_profile.model_dump(mode="json"),
            }
            if run_record.verification_contract is not None and run_record.verification_contract.required:
                initial_state["rubric"] = run_record.verification_contract.rubric
                initial_state["verification_contract"] = run_record.verification_contract.model_dump(mode="json")
                yield self._sse(
                    "rubric_compiled",
                    {
                        "session_id": session_id,
                        "query_id": query_id,
                        "run_id": run_record.run_id,
                        "goal_id": run_record.goal_id,
                        "contract": run_record.verification_contract.model_dump(mode="json"),
                    },
                )

            async for item in self._astream_with_hitl_resume(
                agent,
                initial_state,
                stream_mode=["messages", "updates", "custom", "values"],
                config=agent_config,
                context={
                    "session_id": session_id,
                    "query_id": query_id,
                    "run_id": run_record.run_id,
                    "goal_id": run_record.goal_id or "",
                    "goal_revision": run_record.goal_revision,
                    "user_id": user_id,
                    "workspace_path": str(workspace_path),
                    "permission_policy": permission_context.grant_bindings(),
                    # The main agent receives cross-Run history, but the grader
                    # owns exactly one Run. The middleware copies this trusted
                    # objective into private state and matches query_id against
                    # the marker on the current HumanMessage.
                    "run_objective": run_record.objective,
                },
                trace_collector=trace_collector,
            ):
                if title_task is not None and title_task.done() and not title_event_emitted:
                    refined_title = title_task.result()
                    title_event_emitted = True
                    if refined_title:
                        yield self._sse(
                            "title",
                            {
                                "session_id": session_id,
                                "title": refined_title,
                                "provisional": False,
                            },
                        )
                if run_record.goal_id and time.monotonic() - last_goal_control_poll >= 0.25:
                    last_goal_control_poll = time.monotonic()
                    authoritative_goal = session_manager.get_goal_state(
                        session_id,
                        run_record.goal_id,
                    )
                    if isinstance(authoritative_goal, dict) and authoritative_goal.get(
                        "requested_status"
                    ) in {GoalStatus.PAUSED.value, GoalStatus.CANCELLED.value}:
                        # Cooperative cross-worker cancellation: the API owner
                        # may not share this process's asyncio task registry,
                        # but Session JSON remains authoritative at every
                        # model/tool/HITL stream boundary.
                        raise asyncio.CancelledError
                if isinstance(item, dict) and "event" in item and "data" in item:
                    yield item
                    continue
                # Drain any trace events emitted synchronously by the collector
                # (span_start/span_end) before processing the next stream item.
                while pending_trace_events:
                    yield pending_trace_events.pop(0)

                chunk_count += 1
                if logger.isEnabledFor(logging.DEBUG) and (chunk_count <= 5 or chunk_count % 20 == 0):
                    logger.debug(
                        "Received stream chunk #%d for session=%s: %s",
                        chunk_count,
                        session_id,
                        type(item).__name__,
                    )
                mode: str | None = None
                payload: Any = item
                if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                    mode, payload = item

                if mode == "messages" or mode is None:
                    message_payload = payload[0] if isinstance(payload, tuple) and payload else payload
                    metadata = payload[1] if isinstance(payload, tuple) and len(payload) > 1 else {}
                    additional_kwargs = getattr(message_payload, "additional_kwargs", None) or {}
                    if additional_kwargs.get(INTERNAL_CALL_MARKER):
                        logger.debug(
                            "Ignoring internal %s model output in user SSE stream",
                            additional_kwargs[INTERNAL_CALL_MARKER],
                        )
                        continue
                    text = self._extract_content_text(payload)
                    reasoning_text = self._extract_reasoning_text(payload)
                    text, model_limit_text_buffer, suppressing_model_limit_notice = (
                        self._filter_model_limit_stream_delta(
                            model_limit_text_buffer,
                            text,
                            suppressing_model_limit_notice,
                        )
                    )

                    # Track active LangGraph node for frontend graph highlight.
                    node = self._metadata_node(metadata)
                    if node and node != active_graph_node:
                        active_graph_node = node
                        trace_collector.add_graph_node_span(node)
                        yield self._sse(
                            "graph_node_active",
                            {"node": node, "trace_id": trace_collector.trace_id},
                        )

                    # Lifecycle of an LLM span: start on the first model or
                    # reasoning chunk, finish when the model node changes or a
                    # tool-call follows.
                    is_model_node = not node or node == "model"
                    if is_model_node and (text or reasoning_text):
                        if active_llm_span is None:
                            current_model_call_index = model_call_index
                            model_call_index += 1
                            active_llm_span = trace_collector.start_llm_span(
                                name=f"model.{current_model_call_index}",
                                input_data=None,
                                metadata={
                                    "graph_node": node or "model",
                                    "model_call_index": current_model_call_index,
                                },
                            )
                    elif active_llm_span is not None and not is_model_node:
                        trace_collector.finish_llm_span(output=emitted_text)
                        active_llm_span = None

                    if reasoning_text and thinking_enabled:
                        if not node or node == "model":
                            emitted_reasoning = True
                            accumulated_reasoning += reasoning_text
                            trace_collector.add_reasoning_span(reasoning_text)
                            active_segment["reasoning_content"] += reasoning_text
                            self._append_reasoning_to_timeline(active_segment, reasoning_text)
                            source = self._detect_reasoning_source(payload)
                            prev_logged_chars = reasoning_log_chars
                            reasoning_log_chars = len(accumulated_reasoning)
                            if (
                                reasoning_log_chars // REASONING_LOG_INTERVAL
                                != prev_logged_chars // REASONING_LOG_INTERVAL
                            ):
                                logger.info(
                                    "Emitting reasoning delta for session=%s (node=%s, source=%s, accumulated=%d): %s...",
                                    session_id,
                                    node,
                                    source,
                                    reasoning_log_chars,
                                    accumulated_reasoning[-120:].replace("\n", " "),
                                )
                            yield self._sse(
                                "reasoning",
                                {
                                    "status": "delta",
                                    "content": reasoning_text,
                                    "chars": len(reasoning_text),
                                },
                            )
                            persist_assistant_snapshot()
                    if text:
                        if isinstance(message_payload, AIMessageChunk) and getattr(message_payload, "tool_calls", None):
                            # Tool-call chunks are rendered as tool cards via
                            # the following `updates` stream, not assistant text.
                            # Close the current LLM span before handing off to tools.
                            if active_llm_span is not None:
                                trace_collector.finish_llm_span(output=emitted_text)
                                active_llm_span = None
                            continue
                        if node and node != "model":
                            continue
                        self._finalize_reasoning_timeline(active_segment)
                        if tools_just_finished:
                            tools_just_finished = False
                            # The model has been re-invoked after tool calls.
                            # Start a new segment so the frontend can render each
                            # model invocation + its tools as a separate block.
                            active_segment = new_segment()
                            segments.append(active_segment)
                            if active_llm_span is not None:
                                trace_collector.finish_llm_span(output=emitted_text)
                                active_llm_span = None
                            yield self._sse("segment_break", {})
                        active_segment["content"] += text
                        emitted_text += text
                        yield self._sse("token", {"content": text})
                        persist_assistant_snapshot()
                elif mode == "updates" and isinstance(payload, dict):
                    for node_name, node_data in payload.items():
                        node_messages = node_data.get("messages") if isinstance(node_data, dict) else None
                        if not node_messages:
                            continue

                        if node_name == "tools":
                            for tool_msg in node_messages:
                                tc_id = str(getattr(tool_msg, "tool_call_id", "") or "")
                                if tc_id and tc_id in historical_tool_call_ids:
                                    # LangGraph may echo tool messages that came
                                    # from the input history/checkpoint. They are
                                    # model context, not new work in this turn.
                                    pending_tool_starts.pop(tc_id, None)
                                    continue
                                if tc_id and tc_id in emitted_tool_ends:
                                    # LangGraph tool-node updates are cumulative: when
                                    # parallel sibling tools finish, an already completed
                                    # ToolMessage can be emitted again. Consuming that
                                    # replay twice used to append a second persisted call
                                    # with the same protocol ID and poison the next turn.
                                    logger.debug("Ignoring replayed tool result: %s", tc_id)
                                    continue
                                tool_name = self._tool_message_name(tool_msg, pending_tool_starts)
                                model_tool_output = self._tool_message_output(tool_msg)
                                original_output = self._tool_message_original_output(tool_msg)
                                tool_artifact = self._tool_message_artifact(tool_msg)
                                pending_tool = pending_tool_starts.get(tc_id, {})
                                adapted = tool_result_adapter.adapt(
                                    original_output,
                                    tool_name=tool_name,
                                    tool_input=pending_tool.get("input", ""),
                                    tool_call_id=tc_id,
                                )
                                raw_output = adapted.answer_context
                                sources = adapted.sources
                                logger.info(
                                    "Tool %s adapted sources: %d (output preview: %s)",
                                    tool_name,
                                    len(sources),
                                    raw_output[:100].replace("\n", " "),
                                )
                                if sources:
                                    try:
                                        tool_msg.content = format_sources_for_model(
                                            model_tool_output if model_tool_output != original_output else raw_output,
                                            sources,
                                        )
                                    except Exception:
                                        pass
                                    turn_sources = dedupe_sources(turn_sources + sources)
                                    for source in sources:
                                        logger.info(
                                            "Emitting source_found event: source_id=%s",
                                            source.get("source_id"),
                                        )
                                        yield self._sse(
                                            "source_found",
                                            {
                                                "tool_call_id": tc_id,
                                                "source": source,
                                            },
                                        )
                                published_attachment = resolve_published_attachment(
                                    tool_artifact,
                                    session_id=session_id,
                                    run_id=run_record.run_id,
                                    query_id=query_id,
                                    tool_call_id=tc_id,
                                    goal_id=run_record.goal_id,
                                    goal_revision=run_record.goal_revision,
                                )
                                if (
                                    tool_name == "publish_attachment"
                                    and isinstance(published_attachment, dict)
                                    and published_attachment.get("id")
                                    and not any(
                                        item.get("id") == published_attachment.get("id")
                                        for item in published_attachments
                                    )
                                ):
                                    published_attachments.append(dict(published_attachment))
                                    yield self._sse(
                                        "attachment_published",
                                        {
                                            "tool_call_id": tc_id,
                                            "attachment": published_attachment,
                                        },
                                    )
                                is_error = self._is_tool_error(tool_msg, raw_output)
                                self._update_tool_end_in_timeline(active_segment, tc_id or "", raw_output, is_error)
                                pending_tool_starts.pop(tc_id, None)

                                matched = False
                                context_fields = self._tool_message_context_fields(
                                    tool_msg,
                                    session_id=session_id,
                                    tool_call_id=tc_id,
                                    original_output=original_output,
                                )
                                if tc_id:
                                    for tc in active_segment["tool_calls"]:
                                        if tc.get("id") == tc_id and "output" not in tc:
                                            # Execution middleware may route a model-selected
                                            # tool to a safer effective tool (for example,
                                            # external read_file -> read_resource).
                                            tc["tool"] = tool_name
                                            tc["output"] = raw_output
                                            if original_output != raw_output:
                                                tc["raw_output"] = original_output
                                            else:
                                                tc.pop("raw_output", None)
                                            tc["is_error"] = is_error
                                            tc["completed_at"] = time.time()
                                            if sources:
                                                tc["sources"] = sources
                                            tc.update(context_fields)
                                            matched = True
                                            break
                                if not matched:
                                    active_segment["tool_calls"].append(
                                        {
                                            "tool": tool_name,
                                            "input": "",
                                            "id": tc_id,
                                            "output": raw_output,
                                            "is_error": is_error,
                                            "completed_at": time.time(),
                                            **context_fields,
                                            **(
                                                {"raw_output": original_output} if original_output != raw_output else {}
                                            ),
                                            **({"sources": sources} if sources else {}),
                                        }
                                    )
                                # Close any running LLM span before recording tool execution.
                                if active_llm_span is not None:
                                    trace_collector.finish_llm_span(output=emitted_text)
                                    active_llm_span = None
                                trace_collector.finish_tool_span(
                                    tool_call_id=tc_id or "",
                                    output=raw_output,
                                    is_error=is_error,
                                )
                                if tc_id:
                                    emitted_tool_ends.add(tc_id)
                                yield self._sse(
                                    "tool_end",
                                    {
                                        "tool": tool_name,
                                        "id": tc_id,
                                        "output": raw_output[:4000],
                                        "output_full_length": len(raw_output),
                                        "summary_source": None,
                                        "is_error": is_error,
                                        "sources": sources,
                                    },
                                )
                                persist_assistant_snapshot(force=True)
                                tools_just_finished = True
                        else:
                            for agent_msg in node_messages:
                                tool_calls = getattr(agent_msg, "tool_calls", None) or []
                                for tool_call in tool_calls:
                                    tc_id = self._tool_call_id(tool_call)
                                    if tc_id and tc_id in historical_tool_call_ids:
                                        # Skip tool calls that originate from input history;
                                        # they should not appear in the current turn timeline.
                                        continue
                                    if tc_id and tc_id in emitted_tool_starts:
                                        continue
                                    tool_name = self._tool_call_name(tool_call)
                                    tool_input = self._format_tool_input(self._tool_call_args(tool_call))
                                    if tc_id:
                                        emitted_tool_starts.add(tc_id)
                                        pending_tool_starts[tc_id] = {
                                            "tool": tool_name,
                                            "input": tool_input,
                                        }
                                    self._finalize_reasoning_timeline(active_segment)
                                    active_segment["tool_calls"].append(
                                        {
                                            "tool": tool_name,
                                            "input": tool_input,
                                            "id": tc_id,
                                        }
                                    )
                                    self._add_tool_start_to_timeline(active_segment, tc_id or "", tool_name, tool_input)
                                    span_type = self._trace_type_for_tool(tool_name, tool_input)
                                    tool_metadata = {
                                        "graph_node": node_name,
                                        **self._tool_harness_metadata(span_type),
                                    }
                                    trace_collector.start_tool_span(
                                        name=tool_name,
                                        tool_call_id=tc_id or "",
                                        input_data=tool_input,
                                        span_type=span_type,
                                        metadata=tool_metadata,
                                    )
                                    yield self._sse(
                                        "tool_start",
                                        {
                                            "tool": tool_name,
                                            "input": tool_input,
                                            "id": tc_id,
                                        },
                                    )
                                    persist_assistant_snapshot(force=True)
                elif mode == "custom" and isinstance(payload, dict):
                    event_type = str(payload.get("type") or "")
                    if event_type:
                        trace_collector.add_custom_span(event_type, payload)
                        yield self._sse(event_type, payload)
                        if event_type == "rubric_evaluation_start":
                            yield self._sse(
                                "verification_started",
                                {
                                    **payload,
                                    "session_id": session_id,
                                    "query_id": query_id,
                                    "run_id": run_record.run_id,
                                    "goal_id": run_record.goal_id,
                                },
                            )
                        elif event_type == "rubric_evaluation_end":
                            rubric_evaluations.append(
                                {
                                    "grading_run_id": payload.get("grading_run_id"),
                                    "iteration": payload.get("iteration", 0),
                                    "result": payload.get("result"),
                                    "explanation": payload.get("explanation") or "",
                                    "criteria": list(payload.get("criteria") or []),
                                }
                            )
                            for criterion in payload.get("criteria") or []:
                                if not isinstance(criterion, dict):
                                    continue
                                yield self._sse(
                                    "criterion_evaluated",
                                    {
                                        "session_id": session_id,
                                        "query_id": query_id,
                                        "run_id": run_record.run_id,
                                        "goal_id": run_record.goal_id,
                                        "grading_run_id": payload.get("grading_run_id"),
                                        "iteration": payload.get("iteration"),
                                        "criterion": criterion,
                                    },
                                )
                        elif event_type == "deterministic_checks_completed":
                            deterministic_check_events.append(dict(payload))
                        elif event_type == "model_call_limit_exceeded":
                            model_call_limit_events.append(dict(payload))
                elif mode == "values" and isinstance(payload, dict):
                    final_state = payload
                    effective_messages = _effective_agent_messages(payload)
                    current_context_usage = _estimate_agent_context_tokens(
                        effective_messages,
                        system_prompt,
                    )
                    if current_context_usage != last_context_usage:
                        last_context_usage = current_context_usage
                        trigger_tokens = int(config.get_deepagents_summarization_config().get("trigger_tokens", 160000))
                        yield self._sse(
                            "context_usage",
                            {
                                "used_tokens": current_context_usage,
                                "total_tokens": trigger_tokens,
                                "percentage": round(
                                    current_context_usage / max(1, trigger_tokens) * 100,
                                    1,
                                ),
                            },
                        )
                    summary_event = payload.get("_summarization_event")
                    summary_cutoff = summary_event.get("cutoff_index") if isinstance(summary_event, dict) else None
                    if isinstance(summary_cutoff, int) and summary_cutoff != persisted_summary_cutoff:
                        try:
                            session_manager.update_agent_context_state(
                                session_id,
                                used_tokens=current_context_usage,
                                messages=_serialize_agent_context_messages(effective_messages),
                            )
                            persisted_summary_cutoff = summary_cutoff
                        except Exception:
                            logger.warning(
                                "Failed to persist summarized Agent context for session=%s",
                                session_id,
                                exc_info=True,
                            )
                    # White-box todo persistence: TodoListMiddleware injects a
                    # `todos` field into graph state. Sync it to session.json and
                    # notify the frontend whenever it changes.
                    current_todos = payload.get("todos")
                    if isinstance(current_todos, list) and current_todos != last_graph_todos:
                        normalized = self._normalize_todos(current_todos)
                        previous_by_id = {
                            str(item.get("id")): item
                            for item in previous_todos
                            if isinstance(item, dict) and item.get("id") is not None
                        }
                        for todo in normalized:
                            previous = previous_by_id.get(str(todo.get("id"))) or {}
                            for field in (
                                "created_at",
                                "created_run_id",
                                "last_changed_query_id",
                                "last_changed_run_id",
                            ):
                                if field not in todo and field in previous:
                                    todo[field] = previous[field]
                        diff = self._todo_diff(previous_todos, normalized)
                        now = time.time()
                        normalized_ids = {
                            str(item.get("id"))
                            for item in normalized
                            if isinstance(item, dict) and item.get("id") is not None
                        }
                        # Removing a Todo from the middleware array is not a
                        # completion signal. Keep an auditable tombstone so
                        # TodoGate cannot be bypassed by omission; the model
                        # must first mark the item completed or cancelled.
                        for removed in diff["removed"]:
                            removed_id = str(removed.get("id") or "")
                            if not removed_id or removed_id in normalized_ids:
                                continue
                            tombstone = dict(removed)
                            prior_status = str(tombstone.get("status") or "").lower()
                            if prior_status not in {"completed", "cancelled"}:
                                tombstone["status"] = "removed_unresolved"
                            tombstone["tombstone"] = True
                            tombstone["removed_at"] = now
                            normalized.append(tombstone)
                        changed_ids = {str(item.get("id")) for item in diff["added"] if item.get("id") is not None}
                        changed_ids.update(
                            str(item.get("id")) for item in diff["removed"] if item.get("id") is not None
                        )
                        changed_ids.update(str(item.get("id")) for item in diff["updated"])
                        for todo in normalized:
                            if str(todo.get("id")) in changed_ids:
                                todo["last_changed_query_id"] = query_id
                                todo["last_changed_run_id"] = run_record.run_id
                                todo["updated_at"] = now
                            todo["goal_id"] = run_record.goal_id
                            todo["goal_revision"] = run_record.goal_revision
                            todo.setdefault("created_run_id", run_record.run_id)
                        session_manager.update_todos(
                            session_id,
                            normalized,
                            goal_id=run_record.goal_id,
                            goal_revision=run_record.goal_revision,
                            run_id=run_record.run_id,
                        )
                        previous_todos = list(normalized)
                        last_graph_todos = list(current_todos)
                        trace_collector.add_todo_span(normalized, diff=diff)
                        yield self._sse(
                            "todos_updated",
                            {"todos": normalized, "session_id": session_id, "query_id": query_id},
                        )

            # Close any still-running LLM span at the end of the stream.
            if active_llm_span is not None:
                trace_collector.finish_llm_span(output=emitted_text)
                active_llm_span = None

            if model_limit_text_buffer and not suppressing_model_limit_notice:
                active_segment["content"] += model_limit_text_buffer
                emitted_text += model_limit_text_buffer
                yield self._sse("token", {"content": model_limit_text_buffer})
                model_limit_text_buffer = ""

            final_content = self._strip_model_call_limit_notice(
                self._last_ai_content(final_state) or emitted_text
            )
            if final_content:
                current_text = active_segment.get("content", "")
                if not current_text.strip():
                    active_segment["content"] = final_content
                    emitted_text = final_content
                    yield self._sse("token", {"content": final_content})
                elif final_content.strip() not in current_text:
                    # The authoritative final answer differs from the streamed
                    # text (e.g. only intermediate planning was streamed before
                    # tools). Replace with the final answer.
                    active_segment["content"] = final_content
                    emitted_text = final_content
                    yield self._sse(
                        "segment_content_replaced",
                        {"content": final_content},
                    )
            elif emitted_reasoning and not final_content:
                diagnostic = (
                    "模型本轮只返回了 reasoning_content，没有返回正式回答 content。"
                    "请检查 Higress 路由模型是否应切换为非推理模型，或确认 provider 是否会在流结束前输出 content。"
                )
                active_segment["content"] += diagnostic
                final_content = diagnostic
                yield self._sse("token", {"content": diagnostic})

            persisted_run = session_manager.get_run_state(session_id, run_record.run_id)
            persisted_activations = (
                persisted_run.get("verification_activations")
                if isinstance(persisted_run, dict)
                and isinstance(persisted_run.get("verification_activations"), list)
                else []
            )
            artifact_links = self._artifact_links(persisted_activations, workspace_path)
            if artifact_links and artifact_links not in active_segment.get("content", ""):
                active_segment["content"] += artifact_links
                emitted_text += artifact_links
                final_content = f"{final_content or ''}{artifact_links}"
                yield self._sse("token", {"content": artifact_links})

            for tc_id, pending in list(pending_tool_starts.items()):
                failed_output = "Tool execution did not return a result before the agent finished."
                active_segment["tool_calls"].append(
                    {
                        "tool": pending.get("tool", "unknown_tool"),
                        "input": pending.get("input", ""),
                        "id": tc_id,
                        "output": failed_output,
                        "summary_source": "missing_tool_output",
                        "is_error": True,
                        "completed_at": time.time(),
                    }
                )
                trace_collector.finish_tool_span(
                    tool_call_id=tc_id,
                    output=failed_output,
                    is_error=True,
                )
                yield self._sse(
                    "tool_end",
                    {
                        "tool": pending.get("tool", "unknown_tool"),
                        "id": tc_id,
                        "output": failed_output,
                        "output_full_length": len(failed_output),
                        "summary_source": "missing_tool_output",
                        "is_error": True,
                        "sources": [],
                    },
                )
            if pending_tool_starts:
                persist_assistant_snapshot(force=True)

            if run_record.status == RunStatus.RUNNING:
                self._run_coordinator.transition(run_record, RunStatus.EVALUATING)
                yield self._sse(
                    "run_status_changed",
                    {
                        "session_id": session_id,
                        "query_id": query_id,
                        "run_id": run_record.run_id,
                        "status": run_record.status.value,
                    },
                )
            verification_state = dict(final_state or {})
            run_record.model_call_count = max(
                int(verification_state.get("run_model_call_count") or 0),
                model_call_index,
            )
            verification_state["_harness_context"] = {
                # Session-scoped ledger is authoritative. It includes
                # tombstones for omitted pending Todos, which raw graph state
                # is allowed to forget but verification is not.
                "todos": list(previous_todos),
                "final_content": active_segment.get("content", "") or final_content or "",
                "workspace_path": str(workspace_path),
            }
            if run_record.verification_contract is not None and rubric_evaluations:
                verification_state.setdefault(
                    "_rubric_status",
                    rubric_evaluations[-1].get("result"),
                )
                verification_state.setdefault(
                    "_rubric_evaluations",
                    rubric_evaluations,
                )
            if deterministic_check_events:
                last_gate_event = deterministic_check_events[-1]
                verification_state.setdefault(
                    "_deterministic_evaluations",
                    list(last_gate_event.get("evaluations") or []),
                )
                gate_status = str(last_gate_event.get("status") or "")
                if gate_status:
                    verification_state.setdefault("_completion_gate_status", gate_status)
                event_attempt = int(last_gate_event.get("attempt") or last_gate_event.get("iteration") or 0)
                if event_attempt:
                    verification_state.setdefault("_verification_attempts", event_attempt)
                if gate_status == "max_iterations_reached":
                    # A deterministic max is authoritative over any earlier
                    # grader event observed in the same natural-stop attempt.
                    verification_state["_rubric_status"] = "max_iterations_reached"
            model_limit_detail = verification_state.get("_model_call_limit_exceeded")
            if not isinstance(model_limit_detail, dict) and model_call_limit_events:
                model_limit_detail = model_call_limit_events[-1]
            run_limit_payload: dict[str, Any] | None = None
            if isinstance(model_limit_detail, dict):
                reason = str(model_limit_detail.get("reason") or "run_model_call_limit")
                run_count = int(model_limit_detail.get("run_count") or run_record.model_call_count)
                effective_limit = (
                    model_limit_detail.get("run_limit")
                    if reason == "run_model_call_limit"
                    else model_limit_detail.get("thread_limit")
                )
                detail = (
                    f"本轮已达到模型调用上限（{run_count}/{effective_limit}）。"
                    if reason == "run_model_call_limit"
                    else f"当前会话已达到模型调用总上限（{run_count}/{effective_limit}）。"
                )
                run_record, goal_record, verification_report = self._run_coordinator.complete_budget_exceeded(
                    run_record,
                    goal_record,
                    reason=reason,
                    model_call_count=run_count,
                    detail=detail,
                )
                run_limit_payload = {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run_id": run_record.run_id,
                    "goal_id": run_record.goal_id,
                    "reason": reason,
                    "model_call_count": run_count,
                    "limit": effective_limit,
                    "message": detail,
                }
            else:
                run_record, goal_record, verification_report = self._run_coordinator.complete_from_final_state(
                    run_record,
                    goal_record,
                    verification_state,
                )
            yield self._sse(
                "verification_report",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run_id": run_record.run_id,
                    "goal_id": run_record.goal_id,
                    "report": verification_report.model_dump(mode="json"),
                },
            )
            yield self._sse(
                "run_outcome",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run_id": run_record.run_id,
                    "goal_id": run_record.goal_id,
                    "status": run_record.status.value,
                    "outcome": run_record.outcome.value if run_record.outcome else None,
                    "budget_exhaustion_reason": run_record.budget_exhaustion_reason,
                    "model_call_count": run_record.model_call_count,
                },
            )
            if run_limit_payload is not None:
                yield self._sse("run_limit_reached", run_limit_payload)
            if goal_record is not None:
                yield self._sse(
                    "goal_status_changed",
                    {
                        "session_id": session_id,
                        "query_id": query_id,
                        "goal": goal_record.model_dump(mode="json"),
                    },
                )

            control_only_verification = {
                VerificationStatus.NOT_REQUIRED,
                VerificationStatus.INCOMPLETE,
                VerificationStatus.GRADER_ERROR,
                VerificationStatus.INFRASTRUCTURE_ERROR,
                VerificationStatus.BUDGET_EXCEEDED,
            }
            persisted_verification_state = (
                None
                if verification_report.status == VerificationStatus.NOT_REQUIRED
                else "unverified"
                if (
                    verification_report.status in control_only_verification
                    or verification_report.accepted_for_goal_revision is False
                    and verification_report.status == VerificationStatus.SATISFIED
                )
                else "passed"
                if verification_report.status == VerificationStatus.SATISFIED
                else "failed"
            )
            # Only one terminal candidate exists per Run. Earlier model/tool
            # narration remains progress for streaming and reload alike.
            self._mark_terminal_candidate_segments(
                segments,
                persisted_verification_state,
            )

            # Drain any remaining synchronous trace events before building the final trace.
            while pending_trace_events:
                yield pending_trace_events.pop(0)

            # Build the single assistant message content by concatenating segment
            # text, and persist the segments array for the UI.
            for seg in segments:
                seg["content"] = self._strip_model_call_limit_notice(
                    str(seg.get("content") or "")
                )
            full_content = "\n\n".join(seg["content"] for seg in segments if seg.get("content"))
            all_tool_calls = [tc for seg in segments for tc in seg.get("tool_calls", [])]
            all_timeline = [item for seg in segments for item in seg.get("timeline", [])]
            message_sources, final_citations = resolve_message_citations(
                full_content,
                turn_sources,
                session_sources,
            )
            for seg in segments:
                seg.pop("_current_reasoning", None)

            # Streaming already emitted model/tool/reasoning spans. Avoid
            # rebuilding segment spans here because that duplicates simple
            # turns such as "你好" as two model calls.
            trace = trace_collector.finish(status=run_record.outcome.value if run_record.outcome else "completed")
            await asyncio.to_thread(
                session_manager.update_trace,
                session_id,
                trace,
                query_id,
            )
            yield self._sse(
                "trace_updated",
                {"trace": trace, "session_id": session_id, "query_id": query_id},
            )
            if trace_context_active:
                trace_collector.__exit__(None, None, None)
                trace_context_active = False

            logger.info(
                "Stream summary for session=%s: chunks=%d, reasoning_emitted=%s, reasoning_len=%d, text_len=%d, segments=%d",
                session_id,
                chunk_count,
                emitted_reasoning,
                len(accumulated_reasoning),
                len(emitted_text),
                len(segments),
            )
            if final_state:
                effective_context_messages = _effective_agent_messages(final_state)
                final_context_usage = _estimate_agent_context_tokens(
                    effective_context_messages,
                    system_prompt,
                )
                serialized_context: list[dict[str, Any]] | None = None
                if using_saved_agent_context or isinstance(final_state.get("_summarization_event"), dict):
                    try:
                        serialized_context = _serialize_agent_context_messages(effective_context_messages)
                    except Exception:
                        logger.warning(
                            "Failed to serialize compact Agent context for session=%s",
                            session_id,
                            exc_info=True,
                        )
                session_manager.update_agent_context_state(
                    session_id,
                    used_tokens=final_context_usage,
                    messages=serialized_context,
                )
            if (
                self._segment_has_payload({"content": full_content, "tool_calls": all_tool_calls})
                or published_attachments
            ):
                session_manager.upsert_assistant_message(
                    session_id,
                    query_id=query_id,
                    content=full_content,
                    tool_calls=all_tool_calls or None,
                    sources=message_sources or None,
                    citations=final_citations or None,
                    reasoning_content=accumulated_reasoning or None,
                    timeline=all_timeline or None,
                    segments=segments or None,
                    output_attachments=published_attachments or None,
                    status="completed",
                )
            run_messages_persisted = True
            if final_state and final_state.get("tool_context_enqueue"):
                tool_context_cfg = ToolContextConfig.from_mapping(config.get_deepagents_tool_context_config())
                job_id = await tool_context_compaction_service.enqueue(
                    session_id,
                    tool_context_cfg,
                )
                if job_id:
                    yield self._sse(
                        "context_maintenance",
                        {
                            "status": "start",
                            "phase": "tool_context_compaction",
                            "message": "正在压缩本轮工具上下文；完成后再进入下一 Run…",
                            "job_id": job_id,
                            "session_id": session_id,
                        },
                    )
                    try:
                        tool_context_status = await tool_context_compaction_service.wait(
                            session_id,
                            timeout=max(10.0, float(tool_context_cfg.job_timeout_seconds) + 30.0),
                        )
                        terminal_status = str(tool_context_status.get("status") or "")
                        if terminal_status not in {"completed", "completed_with_errors"}:
                            raise RuntimeError(
                                f"Tool Context ended with non-terminal status: {terminal_status or 'unknown'}"
                            )
                        yield self._sse(
                            "context_maintenance",
                            {
                                "status": "done",
                                "phase": "tool_context_compaction_done",
                                "message": (
                                    "本轮工具上下文已部分压缩；失败项继续保留原始工具结果，"
                                    "证据引用未被丢弃。"
                                    if terminal_status == "completed_with_errors"
                                    else "本轮工具上下文压缩完成，证据引用已对账。"
                                ),
                                "job_id": job_id,
                                "session_id": session_id,
                                "tool_context_status": tool_context_status,
                            },
                        )
                    except Exception as exc:
                        logger.warning(
                            "Tool Context Run-boundary barrier failed for session=%s job=%s: %s",
                            session_id,
                            job_id,
                            exc,
                        )
                        yield self._sse(
                            "context_maintenance",
                            {
                                "status": "error",
                                "phase": "tool_context_compaction_error",
                                "message": "工具上下文压缩未形成可用终态；下一 Run 将继续使用原始工具上下文。",
                                "job_id": job_id,
                                "session_id": session_id,
                            },
                        )
            if title_task is not None and not title_event_emitted:
                try:
                    refined_title = await asyncio.wait_for(
                        asyncio.shield(title_task),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    refined_title = None
                title_event_emitted = True
                if refined_title:
                    yield self._sse(
                        "title",
                        {
                            "session_id": session_id,
                            "title": refined_title,
                            "provisional": False,
                        },
                    )
            yield self._sse(
                "citations_finalized",
                {
                    "citations": final_citations,
                    "sources": message_sources,
                    "cited_source_ids": list(dict.fromkeys(citation["source_id"] for citation in final_citations)),
                },
            )
            yield self._sse(
                "done",
                {
                    "content": final_content,
                    "session_id": session_id,
                    "project_id": project_id,
                    "workspace_path": str(workspace_path),
                    "run_id": run_record.run_id,
                    "run_outcome": (run_record.outcome.value if run_record.outcome else None),
                    "goal_id": run_record.goal_id,
                    "goal_status": goal_record.status.value if goal_record else None,
                },
            )
            logger.info("Stream finished for session=%s with %d chunks", session_id, chunk_count)
        except asyncio.CancelledError:
            logger.info("Agent stream cancelled for session=%s", session_id)
            try:
                if run_record is not None and not run_record.terminal:
                    self._run_coordinator.fail(
                        run_record,
                        outcome=RunOutcome.CANCELLED,
                        error="client_cancelled",
                    )
                    if goal_record is not None:
                        self._run_coordinator.goals.release_run(
                            goal_record,
                            run=run_record,
                            gap="当前 Run 已停止。",
                        )
            except Exception:
                logger.warning(
                    "Failed to persist cancelled Run state for session=%s",
                    session_id,
                    exc_info=True,
                )
            try:
                if not run_messages_persisted and segments and active_segment:
                    self._persist_partial_run(
                        session_id=session_id,
                        query_id=query_id,
                        user_message=message,
                        attachments=attachments,
                        segments=segments,
                        active_segment=active_segment,
                        pending_tool_starts=pending_tool_starts,
                        accumulated_reasoning=accumulated_reasoning,
                        turn_sources=turn_sources,
                        output_attachments=published_attachments,
                        user_message_persisted=user_message_persisted,
                        status="cancelled",
                        interruption_notice="本轮已被用户停止，以上为中断前已完成的部分结果。",
                        pending_tool_output="Tool execution was interrupted because the user stopped the run.",
                    )
            except Exception:
                logger.warning("Failed to persist partial cancelled run for session=%s", session_id, exc_info=True)
            try:
                cancelled_tool_context_cfg = ToolContextConfig.from_mapping(config.get_deepagents_tool_context_config())
                if cancelled_tool_context_cfg.enabled:
                    await asyncio.shield(
                        tool_context_compaction_service.enqueue(
                            session_id,
                            cancelled_tool_context_cfg,
                        )
                    )
            except Exception:
                logger.debug(
                    "Failed to enqueue Tool Context maintenance after cancellation for session=%s",
                    session_id,
                    exc_info=True,
                )
            try:
                permission_resume_registry.reject_session(
                    session_id,
                    "Agent stream was cancelled by the client.",
                )
                dimension_build_resume_registry.reject_session(
                    session_id,
                    "Agent stream was cancelled by the client.",
                )
                logical_dataset_resume_registry.reject_session(
                    session_id,
                    "Agent stream was cancelled by the client.",
                )
                database_sql_revision_resume_registry.reject_session(
                    session_id,
                    "Agent stream was cancelled by the client.",
                )
            except Exception:
                logger.debug("Failed to reject pending permission requests for session=%s", session_id, exc_info=True)
            try:
                if trace_collector is not None:
                    trace = trace_collector.finish(status="cancelled", error="client_cancelled")
                    await asyncio.to_thread(
                        session_manager.update_trace,
                        session_id,
                        trace,
                        query_id,
                    )
            except Exception:
                pass
            if trace_context_active and trace_collector is not None:
                trace_collector.__exit__(asyncio.CancelledError, None, None)
            raise
        except Exception as exc:
            if isinstance(exc, ModelCallLimitExceededError):
                logger.info(
                    "Agent Run reached its model-call boundary for session=%s: %s",
                    session_id,
                    exc,
                )
            else:
                logger.exception("Agent stream failed for session=%s: %s", session_id, exc)
                traceback.print_exc()
            error_msg = str(exc) or exc.__class__.__name__
            error_notice = f"本轮执行中断：{error_msg}。已保留中断前完成的内容，可修复连接后输入“继续”。"
            if isinstance(exc, ModelCallLimitExceededError) and run_record is not None:
                reason = (
                    "run_model_call_limit"
                    if exc.run_limit is not None and exc.run_count >= exc.run_limit
                    else "thread_model_call_limit"
                )
                effective_limit = exc.run_limit if reason == "run_model_call_limit" else exc.thread_limit
                detail = (
                    f"本轮已达到模型调用上限（{exc.run_count}/{effective_limit}）。"
                    if reason == "run_model_call_limit"
                    else f"当前会话已达到模型调用总上限（{exc.run_count}/{effective_limit}）。"
                )
                try:
                    if not run_record.terminal:
                        run_record, goal_record, verification_report = (
                            self._run_coordinator.complete_budget_exceeded(
                                run_record,
                                goal_record,
                                reason=reason,
                                model_call_count=exc.run_count,
                                detail=detail,
                            )
                        )
                    else:
                        verification_report = run_record.verification_report
                    if verification_report is not None:
                        yield self._sse(
                            "verification_report",
                            {
                                "session_id": session_id,
                                "query_id": query_id,
                                "run_id": run_record.run_id,
                                "goal_id": run_record.goal_id,
                                "report": verification_report.model_dump(mode="json"),
                            },
                        )
                    yield self._sse(
                        "run_outcome",
                        {
                            "session_id": session_id,
                            "query_id": query_id,
                            "run_id": run_record.run_id,
                            "goal_id": run_record.goal_id,
                            "status": run_record.status.value,
                            "outcome": run_record.outcome.value if run_record.outcome else None,
                            "budget_exhaustion_reason": reason,
                            "model_call_count": run_record.model_call_count,
                        },
                    )
                    if goal_record is not None:
                        yield self._sse(
                            "goal_status_changed",
                            {
                                "session_id": session_id,
                                "query_id": query_id,
                                "goal": goal_record.model_dump(mode="json"),
                            },
                        )
                    yield self._sse(
                        "run_limit_reached",
                        {
                            "session_id": session_id,
                            "query_id": query_id,
                            "run_id": run_record.run_id,
                            "goal_id": run_record.goal_id,
                            "reason": reason,
                            "model_call_count": exc.run_count,
                            "limit": effective_limit,
                            "message": detail,
                        },
                    )

                    self._mark_pending_tools_interrupted(
                        active_segment,
                        pending_tool_starts,
                        "本轮达到模型调用上限前，该工具尚未返回结果。",
                    )
                    for seg in segments:
                        seg["content"] = self._strip_model_call_limit_notice(
                            str(seg.get("content") or "")
                        )
                    full_content = "\n\n".join(
                        str(seg.get("content") or "")
                        for seg in segments
                        if seg.get("content")
                    )
                    self._persist_assistant_snapshot(
                        session_id=session_id,
                        query_id=query_id,
                        segments=segments,
                        accumulated_reasoning=accumulated_reasoning,
                        turn_sources=turn_sources,
                        output_attachments=published_attachments,
                        session_sources=session_sources,
                        status="completed",
                    )
                    run_messages_persisted = True
                    if trace_collector is not None:
                        trace = trace_collector.finish(
                            status=RunOutcome.BUDGET_EXCEEDED.value,
                            error=detail,
                        )
                        await asyncio.to_thread(
                            session_manager.update_trace,
                            session_id,
                            trace,
                            query_id,
                        )
                        yield self._sse(
                            "trace_updated",
                            {"trace": trace, "session_id": session_id, "query_id": query_id},
                        )
                    if trace_context_active and trace_collector is not None:
                        trace_collector.__exit__(None, None, None)
                        trace_context_active = False
                    yield self._sse(
                        "done",
                        {
                            "content": full_content,
                            "session_id": session_id,
                            "project_id": project_id,
                            "workspace_path": str(workspace_path),
                            "run_id": run_record.run_id,
                            "run_outcome": RunOutcome.BUDGET_EXCEEDED.value,
                            "goal_id": run_record.goal_id,
                            "goal_status": goal_record.status.value if goal_record else None,
                        },
                    )
                except Exception:
                    logger.exception(
                        "Failed to normalize model-call limit for session=%s",
                        session_id,
                    )
                    yield self._sse(
                        "error",
                        {
                            "error": error_msg,
                            "message": "模型调用上限处理失败，请重试当前任务。",
                        },
                    )
                return
            try:
                if run_record is not None and not run_record.terminal:
                    self._run_coordinator.fail(
                        run_record,
                        outcome=RunOutcome.FAILED,
                        error=error_msg,
                    )
                    yield self._sse(
                        "run_outcome",
                        {
                            "session_id": session_id,
                            "query_id": query_id,
                            "run_id": run_record.run_id,
                            "goal_id": run_record.goal_id,
                            "status": run_record.status.value,
                            "outcome": run_record.outcome.value,
                            "error": error_msg,
                            "budget_exhaustion_reason": (run_record.budget_exhaustion_reason),
                            "model_call_count": run_record.model_call_count,
                        },
                    )
                    if goal_record is not None:
                        goal_record = self._run_coordinator.goals.release_run(
                            goal_record,
                            run=run_record,
                            gap=error_notice,
                        )
                    if goal_record is not None:
                        yield self._sse(
                            "goal_status_changed",
                            {
                                "session_id": session_id,
                                "query_id": query_id,
                                "goal": goal_record.model_dump(mode="json"),
                            },
                        )
            except Exception:
                logger.warning(
                    "Failed to persist failed Run state for session=%s",
                    session_id,
                    exc_info=True,
                )
            try:
                if not run_messages_persisted and segments and active_segment:
                    self._persist_partial_run(
                        session_id=session_id,
                        query_id=query_id,
                        user_message=message,
                        attachments=attachments,
                        segments=segments,
                        active_segment=active_segment,
                        pending_tool_starts=pending_tool_starts,
                        accumulated_reasoning=accumulated_reasoning,
                        turn_sources=turn_sources,
                        output_attachments=published_attachments,
                        user_message_persisted=user_message_persisted,
                        status="error",
                        interruption_notice=None,
                        error_notice=error_notice,
                        pending_tool_output="Tool execution was interrupted because the agent run failed.",
                    )
            except Exception:
                logger.warning("Failed to persist partial failed run for session=%s", session_id, exc_info=True)
            try:
                failed_tool_context_cfg = ToolContextConfig.from_mapping(config.get_deepagents_tool_context_config())
                failed_job_id = await tool_context_compaction_service.enqueue(
                    session_id,
                    failed_tool_context_cfg,
                )
                if failed_job_id:
                    yield self._sse(
                        "context_maintenance",
                        {
                            "status": "start",
                            "phase": "tool_context_compaction",
                            "message": "正在后台优化历史工具上下文…",
                            "job_id": failed_job_id,
                            "session_id": session_id,
                        },
                    )
            except Exception:
                logger.debug(
                    "Failed to enqueue Tool Context maintenance after error for session=%s",
                    session_id,
                    exc_info=True,
                )
            try:
                if trace_collector is not None:
                    trace = trace_collector.finish(status="error", error=error_msg)
                    await asyncio.to_thread(
                        session_manager.update_trace,
                        session_id,
                        trace,
                        query_id,
                    )
            except Exception:
                pass
            if trace_context_active and trace_collector is not None:
                trace_collector.__exit__(type(exc), exc, exc.__traceback__)
            yield self._sse("error", {"error": error_msg, "message": error_notice})
        finally:
            if goal_record is not None:
                key = (session_id, goal_record.goal_id)
                if self._active_goal_tasks.get(key) is asyncio.current_task():
                    self._active_goal_tasks.pop(key, None)
            scratch_path = getattr(locals().get("agent_backend"), "execution_scratch_host_path", None)
            if scratch_path:
                try:
                    shutil.rmtree(str(scratch_path), ignore_errors=True)
                except Exception:
                    logger.debug(
                        "Failed to clean Harness scratch for session=%s query=%s",
                        session_id,
                        query_id,
                        exc_info=True,
                    )
            # This checkpointer is only a live-Run HITL scratchpad. Session
            # history is authoritative for future user turns, so success,
            # failure, cancellation, and generator close all release the
            # unique session_id:query_id checkpoint thread here.
            try:
                await self._delete_checkpoint_thread(checkpoint_thread_id)
            except Exception:
                logger.debug(
                    "Failed to clean terminal checkpoint for session=%s query=%s",
                    session_id,
                    query_id,
                    exc_info=True,
                )


deepagents_agent_manager = DeepAgentsAgentManager()
