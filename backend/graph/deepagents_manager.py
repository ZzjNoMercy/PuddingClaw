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
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextvars import ContextVar
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
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
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
from langgraph.config import get_config, get_stream_writer
from langgraph.types import Command
from typing_extensions import NotRequired

import config
from analytics.models import get_analytics_model_registry
from graph.attachment_store import attachment_store
from graph.citations import (
    dedupe_sources,
    format_sources_for_model,
    resolve_message_citations,
    sanitize_citation_markdown,
)
from graph.database_sql_revision_resume import database_sql_revision_resume_registry
from graph.deepagents_prompt_builder import build_deepagents_system_prompt
from graph.dimension_build_resume import dimension_build_resume_registry
from graph.live_tool_output import project_live_tool_output
from graph.logical_dataset_resume import logical_dataset_resume_registry
from graph.managed_paths import is_managed_resource_path
from graph.middleware_trace_proxy import wrap_middlewares_for_trace
from graph.middlewares.analysis_templates import AnalysisTemplateMiddleware
from graph.middlewares.attachment_edit import AttachmentEditMiddleware
from graph.middlewares.delegation_control import (
    DelegationControlMiddleware,
    SubagentProgressMiddleware,
)
from graph.middlewares.goal_completion import (
    GOAL_COMPLETION_REMINDER_SOURCE,
    GoalCompletionMiddleware,
)
from graph.middlewares.harness_todos import HarnessTodoMiddleware
from graph.middlewares.semantic_assets import SemanticAssetsMiddleware
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
from graph.middlewares.tool_guides import ToolGuideMiddleware
from graph.middlewares.tool_protocol import (
    ToolProtocolIntegrityMiddleware,
    pending_executable_tool_call_ids,
    repair_tool_message_protocol,
)
from graph.middlewares.toolset import (
    ToolsetMiddleware,
    discover_skill_catalog,
    discover_skill_toolsets,
)
from graph.middlewares.user_input_boundary import UserInputBoundaryMiddleware
from graph.middlewares.workspace_path_router import WorkspacePathRouterMiddleware
from graph.permission_middleware import ExternalFilePermissionMiddleware
from graph.permission_policy import RunPermissionContext
from graph.permission_resume import permission_resume_registry
from graph.permissioned_filesystem_backend import PermissionedCompositeBackend
from graph.prompt_cache import CONTROL_SOURCE, is_prompt_control_message
from graph.session_manager import session_manager
from graph.skill_plan_resume import skill_plan_resume_registry
from graph.tool_result_adapter import tool_result_adapter
from graph.trace_collector import TraceCollector, TraceSpan
from graph.user_input_resume import user_input_resume_registry
from harness.artifact_paths import (
    extract_local_directory_paths,
    extract_local_resource_paths,
    resolve_declared_artifact_targets,
)
from harness.coordinators import HarnessRunCoordinator
from harness.dependency_setup import dependency_plan_prompt
from harness.deterministic_checks import evaluate_deterministic_criteria
from harness.evidence_ledger import EvidenceRef, is_evidence_ref
from harness.goal_turn_router import GoalTurnDecision, GoalTurnRouter
from harness.models import (
    GoalCompletionPolicy,
    GoalRecord,
    GoalStatus,
    GoalTurnIntent,
    RunKind,
    RunOutcome,
    RunRecord,
    RunStatus,
    RunTaskProfile,
    RunVerificationContract,
    VerificationActivation,
    VerificationFailureKind,
    VerificationStatus,
    VerifierKind,
)
from harness.permission_reviewer import ModelPermissionReviewer, PermissionReviewer
from harness.rubric_compiler import RunRubricCompiler
from harness.task_profiles import SemanticTaskProfileClassifier, TaskProfileClassifier
from harness.tool_execution import ToolExecutionPipeline
from harness.verification_activations import (
    VerificationActivationMiddleware,
    resolve_published_attachment,
)
from harness.workspace_backends import build_workspace_execution_backend
from knowledge.paths import get_knowledge_root
from llm.model_client import (
    INTERNAL_CALL_MARKER,
    ModelClientChatModel,
    ModelTransportInterruptedError,
)
from observability import emit_harness_metric
from projects.registry import project_registry
from tools import get_all_tools
from tools.filesystem.factory import VersionedPatchMiddleware
from tools.package_install import create_install_packages_tool
from tools.request_user_input_tool import create_request_user_input_tool
from tools.toolsets import agent_custom_tool_names, tool_control_descriptor
from tools.update_goal_tool import create_update_goal_tool
from utils.json_serialization import to_json_compatible

logger = logging.getLogger(__name__)

_TASK_ROUTER_TIMEOUT_SECONDS = 15.0
_GOAL_TURN_ROUTER_TIMEOUT_SECONDS = 8.0
_VERIFICATION_CRITERION_LABELS = {
    "artifact_delivery": "产物交付",
    "code_validation": "代码验证",
    "todo_reconciliation": "Todo 收口",
    "task_fulfillment": "任务完成度",
    "report_integrity": "报告完整性",
    "metric_consistency": "指标口径一致性",
    "time_scope": "数据时间范围",
    "analysis_traceability": "分析证据可追溯",
}


def _verification_gap_detail(payload: dict[str, Any]) -> str:
    """Render structured failed criteria as a useful timeline explanation."""

    raw_items = payload.get("evaluations")
    if not isinstance(raw_items, list):
        raw_items = payload.get("criteria")
    issues: list[str] = []
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict) or item.get("passed") is not False:
            continue
        criterion_id = str(item.get("criterion_id") or item.get("name") or "未命名验收项")
        label = _VERIFICATION_CRITERION_LABELS.get(criterion_id, criterion_id)
        reason = str(item.get("gap") or item.get("reason") or item.get("explanation") or "未提供具体判定依据").strip()
        if len(reason) > 180:
            reason = reason[:177].rstrip() + "…"
        issues.append(f"{label}：{reason}")
        if len(issues) == 3:
            break
    return "待处理：" + "；".join(issues) if issues else ""


_TRIVIAL_TASK_ROUTER_RE = re.compile(
    r"^\s*(?:你好|您好|嗨|哈喽|hello|hi|hey|谢谢|多谢|好的|好|ok|okay|在吗|早上好|下午好|晚上好)[!！。,.，?？\s]*$",
    re.IGNORECASE,
)


def _should_start_semantic_task_router(message: str) -> bool:
    """Skip provider work that cannot add routing value for trivial chat."""

    return _TRIVIAL_TASK_ROUTER_RE.fullmatch(str(message or "")) is None


_TASK_PROFILE_CONTINUATION_RE = re.compile(
    r"^(?:继续|继续处理|继续执行|继续完成|接着做|接着处理|再试一次|重试|重试一次|继续吧)[。.!！?？\s]*$",
    flags=re.IGNORECASE,
)
_EXPLICIT_CONTINUATION_RE = re.compile(
    r"(?:^|[\s，,。.!！?？]|请)(?:继续|接着|承接|恢复|从中断处)"
    r"|(?:上次|刚才).{0,24}(?:未完成|中断|剩余|任务|报告|查询|SQL|图表|继续|接着|恢复)"
    r"|(?:^|[\s,。.!！?？])(?:continue|resume|pick\s+up|carry\s+on)\b",
    flags=re.IGNORECASE,
)


def _is_explicit_continuation(message: str) -> bool:
    normalized = str(message or "").strip()
    return bool(_TASK_PROFILE_CONTINUATION_RE.fullmatch(normalized) or _EXPLICIT_CONTINUATION_RE.search(normalized))


_INTERNAL_CONTROL_SOURCES = frozenset(
    {
        RUBRIC_GRADER_MESSAGE_SOURCE,
        "puddingclaw_completion_gate",
        "puddingclaw_goal_continuation",
        GOAL_COMPLETION_REMINDER_SOURCE,
        CONTROL_SOURCE,
    }
)

_SUMMARIZATION_USAGE_CONTEXT: ContextVar[dict[str, int] | None] = ContextVar(
    "puddingclaw_summarization_usage",
    default=None,
)
_HISTORICAL_TOOL_INLINE_BUDGET_CHARS = 80_000
_HISTORICAL_MESSAGE_INLINE_BUDGET_CHARS = 40_000
_HISTORICAL_TOOL_ARGS_BUDGET_CHARS = 24_000
_HISTORICAL_MAX_MESSAGE_COUNT = 120


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
    if is_prompt_control_message(message):
        return True
    role, name, extra = _message_metadata(message)
    if role not in {"human", "user"}:
        return False
    source = str(extra.get("lc_source") or name or "")
    return source in _INTERNAL_CONTROL_SOURCES


def _user_facing_verification_summary(detail: str) -> str:
    """Keep the published verdict brief and free of control-plane narration."""

    cleaned = sanitize_citation_markdown(detail).strip()
    if not cleaned:
        return ""
    parts = [
        part.strip().lstrip("-*• ") for part in re.split(r"(?<=[。！？!?；;])\s*|[\r\n]+", cleaned) if part.strip()
    ]
    selected = [part for part in parts if not _INTERNAL_TERMS_RE.search(part)][:2]
    if not selected:
        return "已核对最终交付及其关键证据，任务结果满足本次要求。"
    summary = "".join(selected)
    if len(summary) <= 240:
        return summary
    clipped = summary[:240]
    boundary = max(clipped.rfind(mark) for mark in "。！？；;")
    return clipped[: boundary + 1] if boundary >= 80 else clipped.rstrip() + "…"


_INTERNAL_TERMS_RE = re.compile(
    r"SKILL\.md|ToolMessage|\bexecute\b|\bTodo\b|reconciliation|source_id|"
    r"criterion|grader|\bRun\b|Harness|required",
    re.IGNORECASE,
)

_FAILURE_EXPLANATION_PREFIXES = (
    "验收基础设施异常：",
    "确定性检查失败：",
    "验收未通过：",
)


def _user_facing_failure_detail(explanation: str) -> str:
    """Reduce a verification failure explanation to plain, safe sentences."""

    cleaned = sanitize_citation_markdown(str(explanation or "")).strip()
    for prefix in _FAILURE_EXPLANATION_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    if not cleaned:
        return ""
    parts = [
        part.strip().lstrip("-*• ") for part in re.split(r"(?<=[。！？!?；;])\s*|[\r\n]+", cleaned) if part.strip()
    ]
    selected = [part for part in parts if not _INTERNAL_TERMS_RE.search(part)][:2]
    if not selected:
        return ""
    summary = "".join(selected)
    if len(summary) <= 240:
        return summary
    clipped = summary[:240]
    boundary = max(clipped.rfind(mark) for mark in "。！？；;")
    return clipped[: boundary + 1] if boundary >= 80 else clipped.rstrip() + "…"


def _terminal_verification_guidance(
    status: VerificationStatus,
    *,
    has_goal: bool,
    goal_status: GoalStatus | None,
    explanation: str = "",
) -> str:
    """Give a rejected Run an actionable, plain-language user-visible ending."""

    detail = _user_facing_failure_detail(explanation)
    detail_block = f"{detail}\n\n" if detail else ""
    if status == VerificationStatus.INFRASTRUCTURE_ERROR:
        return (
            "**验证工具发生故障，本次验收未能完成。**\n\n"
            + detail_block
            + "这不是产物不达标；如果同一错误重复出现，重试不会改变结果，"
            "请先修复验证工具。技术明细见右侧“验收”。"
        )
    if status in {
        VerificationStatus.INCOMPLETE,
        VerificationStatus.GRADER_ERROR,
    }:
        return (
            "**验收执行异常，未能形成验收结论。**\n\n"
            + detail_block
            + "当前进度和证据已保留，可发送 **“重试验收”** 再试一次；"
            "如果重复出现，可在右侧“验收”中查看技术明细。"
        )
    if status == VerificationStatus.BUDGET_EXCEEDED or goal_status == GoalStatus.BUDGET_EXCEEDED:
        return (
            "**本次执行已达到预算边界。**\n\n"
            "当前进度、Todo、产物和证据均已保留。请在右侧 **“目标”** "
            "中输入要追加的轮数；可以仅追加，也可以选择 **“追加并继续”**。"
        )
    if has_goal:
        return (
            "**还有未完成项，但当前进度没有丢失。**\n\n"
            "Goal、Todo、产物和证据均已保留。请发送 "
            "**“继续完成剩余工作”**，系统会启动新的 Goal Run 并从当前进度继续。"
        )
    return "**还有未完成项。**\n\n请打开右侧 **“验收”** 查看具体缺口，然后发送对应的修复要求。"


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
    if goal_id and any(not is_evidence_ref(item) for item in (raw_refs or [])):
        session_manager.migrate_goal_evidence_refs(session_id, goal_id)
        goal = session_manager.get_goal_state(session_id, goal_id)
        raw_refs = goal.get("evidence_refs") if isinstance(goal, dict) else []
    raw_gaps = goal.get("gaps") if isinstance(goal, dict) else []
    report = run.get("verification_report") if isinstance(run, dict) else None
    handoff = run.get("handoff_summary") if isinstance(run, dict) else None
    goal_contract = goal.get("goal_contract") if isinstance(goal, dict) else None
    criteria = goal_contract.get("criteria") if isinstance(goal_contract, dict) else []
    permission_grants = session_manager.list_permission_grants(session_id)

    def project_lease(lease: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        projected = {key: lease.get(key) for key in fields if lease.get(key) is not None}
        if str(lease.get("status") or "") in {"abandoned", "committed"}:
            for ephemeral in ("staged_path", "staged_dir", "validation_scratch"):
                projected.pop(ephemeral, None)
        return projected

    artifact_leases = session_manager.list_external_artifact_leases(session_id)
    directory_leases = session_manager.list_external_directory_leases(session_id)

    def project_permission(item: dict[str, Any]) -> dict[str, Any]:
        projected = {
            key: item.get(key)
            for key in ("id", "target_kind", "target", "capabilities", "scope")
            if item.get(key) is not None
        }
        permission_type = item.get("type") or item.get("grant_type")
        if permission_type is not None:
            projected["type"] = permission_type
        return projected

    def project_handoff(item: dict[str, Any]) -> dict[str, Any]:
        projected = {
            key: item.get(key)
            for key in (
                "source_run_id",
                "goal_id",
                "goal_revision",
                "terminal_status",
                "objective",
                "completed_todos",
                "durable_facts",
                "unresolved_gaps",
                "created_at",
            )
            if item.get(key) is not None
        }
        for field in ("evidence_refs", "artifact_refs", "sql_generation_refs"):
            projected[field] = [
                EvidenceRef.model_validate(ref).model_dump(mode="json")
                for ref in item.get(field) or []
                if is_evidence_ref(ref)
            ]
        return projected

    def project_goal_decision(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        projected = {
            key: item.get(key)
            for key in (
                "decision_id",
                "goal_id",
                "objective_revision",
                "status",
                "accepted",
                "supporting_run_ids",
                "evidence_ref_count",
                "gaps",
                "accepted_run_id",
                "report_id",
                "created_at",
            )
            if item.get(key) is not None
        }
        projected["criterion_provenance"] = [
            {
                **{key: value for key, value in provenance.items() if key != "evidence_refs"},
                "evidence_refs": [
                    EvidenceRef.model_validate(ref).model_dump(mode="json")
                    for ref in provenance.get("evidence_refs") or []
                    if is_evidence_ref(ref)
                ],
            }
            for provenance in item.get("criterion_provenance") or []
            if isinstance(provenance, dict)
        ]
        return projected

    unresolved_todos = [
        item
        for item in todos
        if isinstance(item, dict) and str(item.get("status") or "pending") not in {"completed", "cancelled"}
    ]
    terminal_todos = [
        item for item in todos if isinstance(item, dict) and str(item.get("status") or "") in {"completed", "cancelled"}
    ]
    projected_todos = [*unresolved_todos, *terminal_todos[-40:]]
    terminal_todo_digest = (
        hashlib.sha256(
            json.dumps(terminal_todos, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if terminal_todos
        else None
    )
    payload = {
        "schema": "puddingclaw.harness-envelope/v2",
        "goal": {
            "goal_id": goal_id or None,
            "objective_revision": revision,
            "objective": goal.get("objective") if isinstance(goal, dict) else None,
            "status": goal.get("status") if isinstance(goal, dict) else None,
            "round": goal.get("round") if isinstance(goal, dict) else None,
            "max_rounds": goal.get("max_rounds") if isinstance(goal, dict) else None,
            "model_call_count": goal.get("model_call_count") if isinstance(goal, dict) else None,
            "budget_exhaustion_reason": goal.get("budget_exhaustion_reason") if isinstance(goal, dict) else None,
            "pending_revision": bool(goal.get("pending_revision")) if isinstance(goal, dict) else False,
            "latest_goal_decision": project_goal_decision(
                goal.get("latest_goal_decision") if isinstance(goal, dict) else None
            ),
        },
        "run": {
            "run_id": run.get("run_id") if isinstance(run, dict) else None,
            "status": run.get("status") if isinstance(run, dict) else None,
            "outcome": run.get("outcome") if isinstance(run, dict) else None,
            "declared_artifact_targets": list(run.get("declared_artifact_targets") or [])
            if isinstance(run, dict)
            else [],
            "follow_up_of_goal_id": run.get("follow_up_of_goal_id") if isinstance(run, dict) else None,
            "follow_up_of_artifact_ids": list(run.get("follow_up_of_artifact_ids") or [])
            if isinstance(run, dict)
            else [],
            "execution_mode": run.get("execution_mode") if isinstance(run, dict) else "native",
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
            "completed_count": sum(1 for item in terminal_todos if item.get("status") == "completed"),
            "cancelled_count": sum(1 for item in terminal_todos if item.get("status") == "cancelled"),
            "sha256": terminal_todo_digest,
            "recent_items_included": min(40, len(terminal_todos)),
        },
        "external_artifact_leases": [
            project_lease(
                lease,
                (
                    "lease_id",
                    "status",
                    "target_path",
                    "staged_path",
                    "expected_source_sha256",
                    "committed_sha256",
                    "run_id",
                    "query_id",
                    "goal_id",
                    "goal_revision",
                    "expires_at",
                    "committed_at",
                ),
            )
            for lease in artifact_leases
            if isinstance(lease, dict)
        ],
        "external_directory_leases": [
            project_lease(
                lease,
                (
                    "lease_id",
                    "status",
                    "directory_path",
                    "staged_dir",
                    "source_manifest_sha256",
                    "plan_digest",
                    "run_id",
                    "query_id",
                    "goal_id",
                    "goal_revision",
                    "expires_at",
                    "committed_at",
                ),
            )
            for lease in directory_leases
            if isinstance(lease, dict)
        ],
        "evidence_refs": [
            EvidenceRef.model_validate(item).model_dump(mode="json")
            for item in (raw_refs if isinstance(raw_refs, list) else [])
            if is_evidence_ref(item)
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
        "known_gaps": [str(item) for item in (raw_gaps if isinstance(raw_gaps, list) else []) if str(item).strip()],
        "control_notices": [
            str(item)
            for item in (
                goal.get("control_notices")
                if isinstance(goal, dict) and isinstance(goal.get("control_notices"), list)
                else []
            )
            if str(item).strip()
        ],
        "active_permissions": [project_permission(item) for item in permission_grants[-30:] if isinstance(item, dict)],
        "latest_verification": {
            "status": report.get("status"),
            "gaps": list(report.get("gaps") or []),
        }
        if isinstance(report, dict)
        else None,
        "latest_run_handoff": project_handoff(handoff) if isinstance(handoff, dict) else None,
    }
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    return (
        '\n\n<HARNESS_ENVELOPE authoritative="true">\n'
        "该块由 Harness 从 Session JSON 确定性生成；Goal、Todo、产物、证据与缺口"
        "以此为准，不得被上方自然语言摘要覆盖。\n"
        f"{serialized}\n"
        "</HARNESS_ENVELOPE>"
    )


def _run_artifact_continuity_prompt(run: Any) -> str:
    """Expose formal follow-up artifacts without restoring old execution state."""

    artifact_ids = [str(item) for item in (getattr(run, "follow_up_of_artifact_ids", None) or []) if str(item)]
    if not artifact_ids:
        return ""
    session_id = str(getattr(run, "session_id", "") or "")
    registry = {
        str(item.get("artifact_id") or ""): item
        for item in session_manager.list_delivered_artifacts(
            session_id,
            verify_freshness=True,
            include_inactive=False,
        )
        if isinstance(item, dict) and str(item.get("artifact_id") or "")
    }
    artifacts = [registry[item] for item in artifact_ids if item in registry]
    if not artifacts:
        return ""
    payload = {
        "execution_mode": str(getattr(run, "execution_mode", "native") or "native"),
        "follow_up_of_goal_id": getattr(run, "follow_up_of_goal_id", None),
        "artifacts": artifacts,
    }
    return (
        '\n\n<ARTIFACT_CONTINUITY authoritative="true">\n'
        "这些是本次追问关联的正式交付物，只提供 target_path/hash/contract 连续性；"
        "它们不恢复旧 scratch、Goal、Skill 或写权限。若 execution_mode=delta_repair，"
        "先并行读取至多两个明确文件并比较最小差异，不要先扫目录；已有数据能证明无需查询时，"
        "不要重新激活数据库能力。最小 patch 已唯一确定后立即结束 discovery，验证并提交。\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        + "\n</ARTIFACT_CONTINUITY>"
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

    def _observed_usage(self, request: ModelRequest) -> dict[str, int]:
        effective_messages = self._get_effective_messages(request)
        counted_messages = (
            [request.system_message, *effective_messages] if request.system_message is not None else effective_messages
        )
        try:
            used_tokens = int(self.token_counter(counted_messages, tools=request.tools))
        except TypeError:
            used_tokens = int(self.token_counter(counted_messages))
        trigger = self._lc_helper.trigger
        trigger_tokens = int(trigger[1]) if trigger and trigger[0] == "tokens" else 0
        return {"used_tokens_before": used_tokens, "trigger_tokens": trigger_tokens}

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> Any:
        token = _SUMMARIZATION_USAGE_CONTEXT.set(self._observed_usage(request))
        try:
            return super().wrap_model_call(request, handler)
        finally:
            _SUMMARIZATION_USAGE_CONTEXT.reset(token)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> Any:
        token = _SUMMARIZATION_USAGE_CONTEXT.set(self._observed_usage(request))
        try:
            return await super().awrap_model_call(request, handler)
        finally:
            _SUMMARIZATION_USAGE_CONTEXT.reset(token)

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
                    **(_SUMMARIZATION_USAGE_CONTEXT.get() or {}),
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
                    **(_SUMMARIZATION_USAGE_CONTEXT.get() or {}),
                }
            )
        except RuntimeError:
            writer = None
        summary = await super()._acreate_summary(self._without_internal_controls(messages_to_summarize))
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
        excluded_middleware=frozenset({"SummarizationMiddleware", "TodoListMiddleware"}),
        extra_middleware=lambda: [HarnessTodoMiddleware()],
        tool_description_overrides={
            "edit_file": (
                "Deprecated by PuddingClaw Harness: direct exact-string editing is disabled. "
                "Call patch_file with unique replacement anchors; expected_sha256 is optional."
            ),
            "ls": (
                "List entries in a directory only when the task genuinely requires "
                "discovering unknown children. Never call ls as a prerequisite for "
                "read_file, grep, materialize_source_ref, patch_file, "
                "or write_file when "
                "an exact path is already known from the user, system context, a Tool "
                "result, or a persisted artifact reference; operate on that path directly."
            ),
            "glob": (
                "Discover files by pattern only when the exact path or file name is "
                "unknown. Do not use glob to confirm a known path, inspect a known "
                "file's parent, infer project identity, or repeat discovery after a "
                "matching path has already been returned. Keep the pattern and search "
                "root as narrow as the task permits."
            ),
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


def _estimate_agent_context_tokens(
    messages: list[Any],
    system_prompt: str,
    tools: list[Any] | None = None,
) -> int:
    """Estimate the current effective Agent context for the UI meter."""
    try:
        counted_messages = [SystemMessage(content=system_prompt), *messages]
        message_tokens = int(count_tokens_approximately(counted_messages, tools=tools or []))
    except Exception:
        message_tokens = sum(max(0, len(str(getattr(msg, "content", ""))) // 4) for msg in messages)
        message_tokens += max(0, len(system_prompt) // 4)
    return message_tokens


def _serialize_agent_context_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Serialize model-only context without duplicating raw Tool evidence."""

    serialized: list[dict[str, Any]] = []
    protocol_messages, protocol_report = repair_tool_message_protocol(messages)
    if any(protocol_report.values()):
        logger.warning("Repaired Agent tool protocol before context persistence: %s", protocol_report)
    for message in protocol_messages:
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


def _serialize_protocol_closed_agent_context(
    messages: list[Any],
) -> list[dict[str, Any]] | None:
    """Serialize only after the tools node has closed live parsed calls."""

    if pending_executable_tool_call_ids(messages):
        return None
    return _serialize_agent_context_messages(messages)


def _agent_context_fingerprint(messages: list[dict[str, Any]] | None) -> str:
    if not messages:
        return ""
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    _completion_gate_failure_signature: NotRequired[Annotated[str, PrivateStateAttr]]
    _completion_gate_stagnation_count: NotRequired[Annotated[int, PrivateStateAttr]]
    _deterministic_evaluations: NotRequired[Annotated[list[dict[str, Any]], PrivateStateAttr]]
    _run_query_id: NotRequired[Annotated[str, PrivateStateAttr]]
    _run_objective: NotRequired[Annotated[str, PrivateStateAttr]]
    _active_analysis_template: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]
    _goal_verification_context: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]
    _goal_completion_reminder_count: NotRequired[Annotated[int, PrivateStateAttr]]
    run_model_call_count: NotRequired[Annotated[int, PrivateStateAttr]]
    thread_model_call_count: NotRequired[Annotated[int, PrivateStateAttr]]
    _model_call_limit_exceeded: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


class RunScopeMiddleware(AgentMiddleware):
    """Initialize immutable Run authorization in private graph state.

    Graph input rejects ``PrivateStateAttr`` fields, and DeepAgents subagents
    do not receive the parent's runtime context. The main Run therefore copies
    server-owned context here; delegated subagents inherit the resulting state
    without treating their delegated HumanMessage as user authorization.
    """

    @staticmethod
    def _update(state: Any, runtime: Any) -> dict[str, Any]:
        runtime_context = getattr(runtime, "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        if not context:
            return {}
        update: dict[str, Any] = {}
        query_id = context.get("query_id")
        objective = context.get("run_objective")
        if isinstance(query_id, str) and query_id and state.get("_run_query_id") != query_id:
            update["_run_query_id"] = query_id
        if isinstance(objective, str) and objective and state.get("_run_objective") != objective:
            update["_run_objective"] = objective
        return update

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._update(state, runtime) or None

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._update(state, runtime) or None


class PuddingClawRubricMiddleware(RubricMiddleware):
    """Run deterministic completion gates before invoking the LLM grader."""

    def __init__(
        self,
        *,
        model: Any,
        system_prompt: str | None = None,
        tools: Sequence[Any] | None = None,
        max_iterations: int = 3,
        on_evaluation: Callable[[Any], None] | None = None,
        max_stagnant_repairs: int = 2,
    ) -> None:
        super().__init__(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            max_iterations=max_iterations,
            on_evaluation=on_evaluation,
        )
        if (
            not isinstance(max_stagnant_repairs, int)
            or isinstance(max_stagnant_repairs, bool)
            or not 1 <= max_stagnant_repairs <= 20
        ):
            raise ValueError("PuddingClawRubricMiddleware: max_stagnant_repairs must be in [1, 20]")
        self.max_stagnant_repairs = max_stagnant_repairs

    _JSON_GRADER_SUFFIX = """

Return exactly one JSON object and no markdown. Do not call tools or functions.
The object must follow this shape:
{
  "result": "satisfied" | "needs_revision" | "failed",
  "explanation": "evidence-grounded user-facing verification summary",
  "criteria": [
    {"name": "criterion id or statement", "passed": true},
    {"name": "criterion id or statement", "passed": false, "gap": "missing evidence"}
  ]
}
Use "satisfied" only when every required criterion passes.
Return one criteria item for every required rubric criterion. Write explanation
and gaps in Chinese. Missing criteria are treated as failed by Harness.
Criteria whose verifier is deterministic have already been checked by Harness.
Treat the supplied deterministic evaluation context as authoritative: copy its
verdict and do not independently fail it from the transcript.
When result is "satisfied", explanation is published in the final answer.
For a satisfied result, write only 1-2 short natural-language sentences (at most
160 Chinese characters) and mention no more than three user-relevant outcomes,
such as delivered content, test/build outcome, data scope, or usable source
links. Do not merely say "验证通过" or "全部标准满足". Do not mention SKILL.md,
tool names, execute, ToolMessage, Todo, reconciliation, source_id, internal
criterion ids, grader rounds, Run ids, required rules, or Harness implementation
details. Never invent evidence that is absent from the supplied context.
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
            goal_revision = int(persisted_run.get("goal_revision") or 1) if isinstance(persisted_run, dict) else 1
            persisted_goal = session_manager.get_goal_state(session_id, goal_id) if goal_id else None
            if isinstance(persisted_goal, dict) and int(persisted_goal.get("objective_revision") or 1) == goal_revision:
                raw_refs = persisted_goal.get("evidence_refs")
                if any(not is_evidence_ref(item) for item in (raw_refs or [])):
                    session_manager.migrate_goal_evidence_refs(session_id, goal_id)
                    persisted_goal = session_manager.get_goal_state(session_id, goal_id) or persisted_goal
                    raw_refs = persisted_goal.get("evidence_refs")
                raw_gaps = persisted_goal.get("gaps")
                run_ids = persisted_goal.get("run_ids")
                prior_runs: list[dict[str, Any]] = []
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
                                    "accepted_for_goal_revision": prior_report.get("accepted_for_goal_revision"),
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
                update["_goal_verification_context"] = {
                    "goal_id": goal_id,
                    "objective_revision": goal_revision,
                    "objective": persisted_goal.get("objective"),
                    "evidence_refs": [
                        EvidenceRef.model_validate(item).model_dump(mode="json")
                        for item in (raw_refs if isinstance(raw_refs, list) else [])
                        if is_evidence_ref(item)
                    ],
                    "todos": session_manager.get_todos(
                        session_id,
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                    ),
                    "known_gaps": [
                        str(item) for item in (raw_gaps if isinstance(raw_gaps, list) else []) if str(item).strip()
                    ],
                    "prior_runs": prior_runs,
                    "latest_goal_decision": persisted_goal.get("latest_goal_decision"),
                }
        return update

    @staticmethod
    def _semantic_completion_failure(item: Any) -> dict[str, Any]:
        """Return the stable meaning of a completion failure.

        Evidence receipts may legitimately grow while the same requirement is
        still missing. They must not reset the stagnation breaker.
        """

        gap = re.sub(r"\s+", " ", str(getattr(item, "gap", "") or "")).strip()
        stable_requirements: dict[str, list[str]] = {}
        for evidence in getattr(item, "evidence", []) or []:
            if not isinstance(evidence, dict):
                continue
            for key in (
                "duplicate_call_ids",
                "missing_call_ids",
                "missing",
                "uncovered_targets",
            ):
                raw = evidence.get(key)
                if not isinstance(raw, list):
                    continue
                normalized: list[str] = []
                for value in raw:
                    if isinstance(value, dict):
                        value = (
                            value.get("reason")
                            or value.get("path")
                            or value.get("host_path")
                            or value.get("artifact_id")
                        )
                    text = re.sub(r"\s+", " ", str(value or "")).strip()
                    if text:
                        normalized.append(text)
                if normalized:
                    stable_requirements[key] = sorted(set(normalized))
        failure_kind = getattr(item, "failure_kind", None)
        return {
            "criterion_id": str(getattr(item, "criterion_id", "") or ""),
            "failure_kind": str(getattr(failure_kind, "value", failure_kind) or ""),
            "gap": gap,
            "missing_requirements": stable_requirements,
        }

    @staticmethod
    def _completion_repair_instruction(item: Any) -> dict[str, Any]:
        """Translate one failed criterion into a bounded, executable protocol."""

        criterion_id = str(getattr(item, "criterion_id", "") or "")
        failure_kind = getattr(item, "failure_kind", None)
        normalized_kind = str(getattr(failure_kind, "value", failure_kind) or VerificationFailureKind.TASK_GAP.value)
        closure_methods = {
            "todo_reconciliation": [
                "Use update_todos to complete or explicitly cancel each remaining Todo.",
                "For a Todo with completion_contract, attach a known structured evidence ID.",
            ],
            "tool_protocol_integrity": [
                "Let every parsed ToolCall finish with its matching ToolMessage before ending.",
            ],
            "artifact_delivery": [
                "Write or commit every declared target and preserve its ArtifactReceipt.",
                "When producing the terminal answer, cite the delivered local artifact path.",
            ],
            "web_evidence_traceability": [
                "Use an approved web tool and cite its structured source IDs in the answer.",
            ],
            "analytics_evidence_traceability": [
                "Use the activated analytics tools and preserve the structured query-result evidence IDs.",
            ],
            "code_validation": [
                "Run a validator that matches the target artifact (pytest/ruff/npm build/node --check or a named validate/check/test Python script).",
                "Do not edit the artifact after the ValidationReceipt is issued.",
            ],
        }.get(criterion_id, ["Produce structured evidence that directly satisfies this criterion."])
        if normalized_kind == VerificationFailureKind.VALIDATOR_PROTOCOL_ERROR.value:
            closure_methods = [
                "Stop modifying the business artifact.",
                "Report the missing ValidationReceipt as a Harness control-plane error.",
            ]

        observed: list[dict[str, Any]] = []
        targets: list[dict[str, str]] = []
        for evidence in getattr(item, "evidence", []) or []:
            if not isinstance(evidence, dict):
                continue
            compact = {
                key: str(evidence[key])
                for key in (
                    "kind",
                    "artifact_id",
                    "validation_receipt_id",
                    "tool_call_id",
                    "output_digest",
                )
                if evidence.get(key)
            }
            if compact and compact not in observed:
                observed.append(compact)
            if evidence.get("kind") == "artifact_write":
                target = {
                    key: str(evidence[key])
                    for key in ("artifact_id", "path", "host_path", "content_sha256")
                    if evidence.get(key)
                }
                if target and target not in targets:
                    targets.append(target)
        return {
            "criterion_id": criterion_id,
            "failure_kind": normalized_kind,
            "missing_condition": str(getattr(item, "gap", "") or "缺少可验证证据"),
            "observed_evidence": observed[:20],
            "accepted_closure_methods": closure_methods,
            "target_artifacts": targets[:20],
        }

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
        deterministic = state.get("_deterministic_evaluations")
        if isinstance(deterministic, list) and deterministic:
            payload = (
                f"{payload}\n\n"
                "<authoritative_deterministic_evaluations>\n"
                "以下逐项结果已由 Harness 从结构化证据完成检查。请直接采用，"
                "不要根据对话文本再次判断或推翻。\n"
                f"{json.dumps(deterministic, ensure_ascii=False, default=str)}\n"
                "</authoritative_deterministic_evaluations>"
            )
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

    @staticmethod
    def _reconcile_deterministic_grader_response(
        state: Any,
        graded: GraderResponse,
    ) -> GraderResponse:
        """Make structured deterministic checks authoritative for their criteria."""

        contract_payload = state.get("verification_contract")
        deterministic_payload = state.get("_deterministic_evaluations")
        if not isinstance(contract_payload, dict) or not isinstance(deterministic_payload, list):
            return graded
        contract = RunVerificationContract.model_validate(contract_payload)
        deterministic_criteria = {
            criterion.id: criterion
            for criterion in contract.criteria
            if criterion.verifier == VerifierKind.DETERMINISTIC
        }
        if not deterministic_criteria:
            return graded
        authoritative = {
            str(item.get("criterion_id") or ""): item
            for item in deterministic_payload
            if isinstance(item, dict) and item.get("criterion_id") in deterministic_criteria
        }
        if not authoritative:
            return graded

        by_statement = {criterion.statement: criterion.id for criterion in deterministic_criteria.values()}
        rebuilt: list[dict[str, Any]] = []
        emitted: set[str] = set()
        for raw_item in graded.criteria:
            item = dict(raw_item)
            name = str(item.get("name") or "")
            criterion_id = name if name in deterministic_criteria else by_statement.get(name)
            if criterion_id and criterion_id in authoritative:
                if criterion_id in emitted:
                    continue
                verdict = authoritative[criterion_id]
                normalized: dict[str, Any] = {
                    "name": criterion_id,
                    "passed": bool(verdict.get("passed")),
                }
                if not normalized["passed"]:
                    normalized["gap"] = str(verdict.get("gap") or "缺少可验证证据")
                rebuilt.append(normalized)
                emitted.add(criterion_id)
                continue
            rebuilt.append(item)
        for criterion_id, verdict in authoritative.items():
            if criterion_id in emitted:
                continue
            normalized = {
                "name": criterion_id,
                "passed": bool(verdict.get("passed")),
            }
            if not normalized["passed"]:
                normalized["gap"] = str(verdict.get("gap") or "缺少可验证证据")
            rebuilt.append(normalized)

        result = graded.result
        has_failure = any(not bool(item.get("passed")) for item in rebuilt)
        if result == "needs_revision" and not has_failure:
            result = "satisfied"
        elif result == "satisfied" and has_failure:
            result = "needs_revision"
        explanation = graded.explanation
        if result == "satisfied" and graded.result != "satisfied":
            explanation = "已核对最终交付与结构化证据，任务结果及必需的来源或产物均可验证。"
        return GraderResponse.model_validate(
            {
                "result": result,
                "explanation": explanation,
                "criteria": rebuilt,
            }
        )

    def _grade(self, state: Any, iteration: int) -> GraderResponse:
        payload = self._build_grader_payload(state, iteration)
        response = self._plain_grader_model().invoke(
            [
                SystemMessage(content=f"{self._system_prompt}\n\n{self._JSON_GRADER_SUFFIX}"),
                HumanMessage(content=payload),
            ]
        )
        return self._reconcile_deterministic_grader_response(
            state,
            self._parse_grader_response(response),
        )

    async def _agrade(self, state: Any, iteration: int) -> GraderResponse:
        payload = self._build_grader_payload(state, iteration)
        response = await self._plain_grader_model().ainvoke(
            [
                SystemMessage(content=f"{self._system_prompt}\n\n{self._JSON_GRADER_SUFFIX}"),
                HumanMessage(content=payload),
            ]
        )
        return self._reconcile_deterministic_grader_response(
            state,
            self._parse_grader_response(response),
        )

    def _compose_update(
        self,
        state: Any,
        evaluation: Any,
        graded_result: Any,
    ) -> dict[str, Any]:
        """Keep business revisions inside the current Harness Run.

        DeepAgents' stock Rubric middleware treats ``max_iterations`` and a
        grader ``failed`` verdict as agent termination.  In PuddingClaw those
        are completion gaps, not Run-boundary decisions.  The model-call
        limiter is the single execution budget and remains the only automatic
        circuit breaker for repeated business revisions.
        """

        next_iteration = int(evaluation.get("iteration") or 0) + 1
        evaluations = [*state.get("_rubric_evaluations", []), evaluation]
        update: dict[str, Any] = {
            "_rubric_evaluations": evaluations,
            "_rubric_iterations": next_iteration,
            "_rubric_status": evaluation.get("result"),
        }
        if graded_result == "satisfied":
            return update
        return {
            **update,
            "messages": [
                HumanMessage(
                    content=self._revision_prompt(evaluation),
                    name=RUBRIC_GRADER_MESSAGE_SOURCE,
                    additional_kwargs={"lc_source": RUBRIC_GRADER_MESSAGE_SOURCE},
                )
            ],
            "jump_to": "model",
        }

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
        goal_evidence_records: list[dict[str, Any]] = []
        persisted_run = session_manager.get_run_state(session_id, run_id) if session_id and run_id else None
        persisted_run_kind = str(persisted_run.get("run_kind") or "") if isinstance(persisted_run, dict) else ""
        if not persisted_run_kind and isinstance(persisted_run, dict):
            persisted_run_kind = (
                RunKind.GOAL_EXECUTION.value if persisted_run.get("goal_id") else RunKind.STANDALONE.value
            )
        explicit_context_run_kind = str(context.get("run_kind") or "")
        if (persisted_run_kind and persisted_run_kind == RunKind.GOAL_INSPECTION.value) or (
            not persisted_run_kind
            and explicit_context_run_kind
            and explicit_context_run_kind == RunKind.GOAL_INSPECTION.value
        ):
            return None
        persisted_activations = (
            persisted_run.get("verification_activations") if isinstance(persisted_run, dict) else None
        )
        goal_id = str(persisted_run.get("goal_id") or "") if isinstance(persisted_run, dict) else ""
        goal_revision = persisted_run.get("goal_revision") if isinstance(persisted_run, dict) else None
        run_objective = str(persisted_run.get("objective") or "") if isinstance(persisted_run, dict) else ""
        execution = (
            persisted_run.get("config_snapshot", {}).get("execution", {})
            if isinstance(persisted_run, dict) and isinstance(persisted_run.get("config_snapshot"), dict)
            else {}
        )
        if goal_id:
            session_manager.backfill_goal_declared_artifact_writes(
                session_id,
                goal_id,
                int(goal_revision or 1),
            )
            persisted_goal = session_manager.get_goal_state(session_id, goal_id)
            raw_goal_refs = persisted_goal.get("evidence_refs") if isinstance(persisted_goal, dict) else None
            if any(not is_evidence_ref(item) for item in (raw_goal_refs or [])):
                session_manager.migrate_goal_evidence_refs(session_id, goal_id)
                persisted_goal = session_manager.get_goal_state(session_id, goal_id)
                raw_goal_refs = persisted_goal.get("evidence_refs") if isinstance(persisted_goal, dict) else None
            if isinstance(raw_goal_refs, list):
                goal_evidence_refs = [
                    EvidenceRef.model_validate(item).model_dump(mode="json")
                    for item in raw_goal_refs
                    if is_evidence_ref(item)
                ]
            for record in session_manager.resolve_goal_evidence_records(
                session_id,
                goal_id,
                int(goal_revision or 1),
            ):
                raw_payload = record.get("payload")
                projected = dict(raw_payload) if isinstance(raw_payload, dict) else {}
                projected.update(
                    {
                        "evidence_ref": {
                            "type": record.get("kind"),
                            "id": record.get("id"),
                        },
                        "verification_pack": record.get("verification_pack"),
                        "origin_run_id": record.get("source_run_id"),
                        "run_id": record.get("source_run_id"),
                        "tool_call_id": record.get("origin_tool_call_id"),
                        "output_digest": record.get("output_digest"),
                        "result_id": record.get("result_id"),
                        "query_trace_id": record.get("query_trace_id"),
                    }
                )
                goal_evidence_records.append(projected)
        persisted_targets = (
            list(persisted_run.get("declared_artifact_targets") or []) if isinstance(persisted_run, dict) else []
        )
        if (
            session_id
            and isinstance(persisted_run, dict)
            and int(persisted_run.get("declared_artifact_targets_version") or 1) < 2
        ):
            resolved_targets = resolve_declared_artifact_targets(run_objective)
            if resolved_targets:
                persisted_targets = session_manager.migrate_run_declared_artifact_targets(
                    session_id,
                    run_id,
                    resolved_targets,
                )
        check_state["_harness_context"] = {
            "todos": list(state.get("todos") or []),
            "final_content": self._last_ai_text(state),
            "workspace_path": str(context.get("workspace_path") or ""),
            "run_id": run_id,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "workspace_id": str(execution.get("workspace_id") or ""),
            "backend_id": str(execution.get("backend_id") or ""),
            "declared_artifact_targets": persisted_targets,
            "active_permission_grant_ids": [
                str(item.get("id")) for item in session_manager.list_permission_grants(session_id) if item.get("id")
            ]
            if session_id
            else [],
            "permission_grants_authoritative": bool(session_id),
            "verification_activations": list(
                persisted_activations
                if isinstance(persisted_activations, list)
                else state.get("verification_activations") or []
            ),
            "goal_evidence_refs": goal_evidence_refs,
            "goal_evidence_records": goal_evidence_records,
            "evaluation_phase": "revision",
        }
        evaluations = evaluate_deterministic_criteria(contract, check_state)
        required_by_id = {criterion.id: criterion.required for criterion in contract.criteria}
        failures = [item for item in evaluations if not item.passed and required_by_id.get(item.criterion_id, True)]
        if not failures:
            # Publication-reference checks depend on the terminal answer and
            # are intentionally deferred until all actionable completion gates
            # have passed.  This prevents them oscillating during repair loops.
            check_state["_harness_context"]["evaluation_phase"] = "terminal"
            evaluations = evaluate_deterministic_criteria(contract, check_state)
            failures = [item for item in evaluations if not item.passed and required_by_id.get(item.criterion_id, True)]
        infrastructure_failures = [
            item
            for item in failures
            if item.failure_kind
            in {
                VerificationFailureKind.INFRASTRUCTURE_ERROR,
                VerificationFailureKind.VALIDATOR_PROTOCOL_ERROR,
            }
        ]
        actionable_failures = [item for item in failures if item not in infrastructure_failures]
        previous_attempts = max(
            int(state.get("_verification_attempts") or 0),
            int(state.get("_completion_gate_iterations") or 0),
            int(state.get("_rubric_iterations") or 0),
        )
        current_attempt = attempt if attempt is not None else previous_attempts + 1
        # Stagnation is meaningful only for gaps the model can repair. Control
        # plane and validator-protocol failures terminate as infrastructure
        # errors immediately and never consume a repair iteration.
        failure_payload = [self._semantic_completion_failure(item) for item in actionable_failures]
        failure_signature = (
            hashlib.sha256(
                json.dumps(
                    failure_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if failure_payload
            else ""
        )
        previous_signature = str(state.get("_completion_gate_failure_signature") or "")
        stagnation_count = (
            int(state.get("_completion_gate_stagnation_count") or 0) + 1
            if failure_signature and failure_signature == previous_signature
            else 0
        )
        gate_status = "satisfied"
        if infrastructure_failures:
            gate_status = VerificationStatus.INFRASTRUCTURE_ERROR.value
        elif actionable_failures and stagnation_count >= self.max_stagnant_repairs:
            # Stop after the configured number of directed repairs produced no
            # semantic change in the failing criteria or their evidence.
            gate_status = VerificationStatus.FAILED.value
        elif actionable_failures:
            gate_status = "needs_revision"
        repair_contract = {
            "version": "repair-contract/v1",
            "run_id": run_id,
            "goal_id": goal_id or None,
            "goal_revision": goal_revision,
            "attempt": current_attempt,
            "instructions": [self._completion_repair_instruction(item) for item in actionable_failures],
        }
        writer = getattr(runtime, "stream_writer", None)
        if writer is not None:
            writer(
                {
                    "type": "deterministic_checks_completed",
                    "iteration": current_attempt,
                    "attempt": current_attempt,
                    "status": gate_status,
                    "session_id": session_id,
                    "run_id": run_id,
                    "goal_id": goal_id,
                    "will_continue": bool(actionable_failures and gate_status == "needs_revision"),
                    "terminal": gate_status
                    in {
                        "satisfied",
                        VerificationStatus.FAILED.value,
                        VerificationStatus.INFRASTRUCTURE_ERROR.value,
                    },
                    "evaluations": [item.model_dump(mode="json") for item in evaluations],
                    "repair_contract": (repair_contract if actionable_failures else None),
                }
            )
        update: dict[str, Any] = {
            "_deterministic_evaluations": [item.model_dump(mode="json") for item in evaluations],
            "_verification_attempts": current_attempt,
            "_completion_gate_iterations": current_attempt,
            "_completion_gate_status": gate_status,
            "_completion_gate_failure_signature": failure_signature,
            "_completion_gate_stagnation_count": stagnation_count,
        }
        if not failures:
            return update
        if infrastructure_failures:
            update["_rubric_status"] = VerificationStatus.INFRASTRUCTURE_ERROR.value
            return update
        if gate_status == VerificationStatus.FAILED.value:
            update["_rubric_status"] = VerificationStatus.FAILED.value
            return update
        feedback = [
            "Harness 返回了结构化修复协议。只处理列出的缺口；不要重查无关数据或重做已完成工作。",
            "<harness_repair_contract>",
            json.dumps(repair_contract, ensure_ascii=False, sort_keys=True),
            "</harness_repair_contract>",
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
        verification_mode = str(
            persisted.get("verification_mode") or ("rubric" if persisted.get("goal_id") else "agent")
        )
        if persisted.get("verification_enabled") is False or verification_mode != "rubric":
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
        # Rebuild from the immutable declared contract on every natural stop.
        # The previous effective contract is an output, never the next input.
        contract_payload = persisted.get("declared_verification_contract")
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
        persisted_effective_payload = persisted.get("verification_contract")
        persisted_effective = (
            RunVerificationContract.model_validate(persisted_effective_payload)
            if isinstance(persisted_effective_payload, dict)
            else None
        )
        changed = persisted_effective is None or (
            effective.model_dump(mode="json") != persisted_effective.model_dump(mode="json")
        )
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
        runtime_context = getattr(runtime, "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        persisted = None
        if session_id and run_id:
            try:
                persisted = session_manager.get_run_state(session_id, run_id)
            except (AssertionError, FileNotFoundError, ValueError):
                persisted = None
        verification_mode = (
            str(persisted.get("verification_mode") or ("rubric" if persisted.get("goal_id") else "agent"))
            if isinstance(persisted, dict)
            else ("rubric" if isinstance(dict(state).get("verification_contract"), dict) else "agent")
        )
        persisted_run_kind = str(persisted.get("run_kind") or "") if isinstance(persisted, dict) else ""
        if not persisted_run_kind and isinstance(persisted, dict):
            persisted_run_kind = RunKind.GOAL_EXECUTION.value if persisted.get("goal_id") else RunKind.STANDALONE.value
        if verification_mode != "rubric" or (persisted_run_kind and persisted_run_kind != RunKind.GOAL_EXECUTION.value):
            return None
        request_id = str(persisted.get("completion_request_id") or "") if isinstance(persisted, dict) else ""
        request = (
            session_manager.get_harness_state(session_id).get("completion_requests", {}).get(request_id)
            if session_id and request_id
            else None
        )
        if not isinstance(request, dict) or request.get("status") != "requested":
            return None
        effective_update = self._effective_contract_update(dict(state), runtime)
        effective_state = {**dict(state), **effective_update}
        if not isinstance(effective_state.get("verification_contract"), dict):
            return super().after_agent(effective_state, runtime)
        previous_attempts = max(
            int(effective_state.get("_verification_attempts") or 0),
            int(effective_state.get("_completion_gate_iterations") or 0),
            int(effective_state.get("_rubric_iterations") or 0),
        )
        attempt = previous_attempts + 1
        effective_state["_rubric_iterations"] = previous_attempts
        gate_update = self._completion_gate_update(
            effective_state,
            runtime,
            attempt=attempt,
        )
        if gate_update and gate_update.get("jump_to") == "model":
            session_manager.update_goal_completion_request_status(
                session_id, request_id, "needs_revision", reason="deterministic_revision_requested"
            )
            return {**effective_update, **gate_update}
        if gate_update and gate_update.get("_completion_gate_status") in {
            VerificationStatus.FAILED.value,
            VerificationStatus.INFRASTRUCTURE_ERROR.value,
        }:
            return {**effective_update, **gate_update}
        grading_state = {**effective_state, **(gate_update or {})}
        rubric_update = super().after_agent(grading_state, runtime)
        if rubric_update and rubric_update.get("jump_to") == "model":
            session_manager.update_goal_completion_request_status(
                session_id, request_id, "needs_revision", reason="rubric_revision_requested"
            )
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
        runtime_context = getattr(runtime, "context", None)
        context = runtime_context if isinstance(runtime_context, dict) else {}
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        persisted = None
        if session_id and run_id:
            try:
                persisted = await asyncio.to_thread(
                    session_manager.get_run_state,
                    session_id,
                    run_id,
                )
            except (AssertionError, FileNotFoundError, ValueError):
                persisted = None
        verification_mode = (
            str(persisted.get("verification_mode") or ("rubric" if persisted.get("goal_id") else "agent"))
            if isinstance(persisted, dict)
            else ("rubric" if isinstance(dict(state).get("verification_contract"), dict) else "agent")
        )
        persisted_run_kind = str(persisted.get("run_kind") or "") if isinstance(persisted, dict) else ""
        if not persisted_run_kind and isinstance(persisted, dict):
            persisted_run_kind = RunKind.GOAL_EXECUTION.value if persisted.get("goal_id") else RunKind.STANDALONE.value
        if verification_mode != "rubric" or (persisted_run_kind and persisted_run_kind != RunKind.GOAL_EXECUTION.value):
            return None
        request_id = str(persisted.get("completion_request_id") or "") if isinstance(persisted, dict) else ""
        request = (
            (await asyncio.to_thread(session_manager.get_harness_state, session_id))
            .get("completion_requests", {})
            .get(request_id)
            if session_id and request_id
            else None
        )
        if not isinstance(request, dict) or request.get("status") != "requested":
            return None
        effective_update = self._effective_contract_update(dict(state), runtime)
        effective_state = {**dict(state), **effective_update}
        if not isinstance(effective_state.get("verification_contract"), dict):
            return await super().aafter_agent(effective_state, runtime)
        previous_attempts = max(
            int(effective_state.get("_verification_attempts") or 0),
            int(effective_state.get("_completion_gate_iterations") or 0),
            int(effective_state.get("_rubric_iterations") or 0),
        )
        attempt = previous_attempts + 1
        effective_state["_rubric_iterations"] = previous_attempts
        gate_update = self._completion_gate_update(
            effective_state,
            runtime,
            attempt=attempt,
        )
        if gate_update and gate_update.get("jump_to") == "model":
            await asyncio.to_thread(
                session_manager.update_goal_completion_request_status,
                session_id,
                request_id,
                "needs_revision",
                reason="deterministic_revision_requested",
            )
            return {**effective_update, **gate_update}
        if gate_update and gate_update.get("_completion_gate_status") in {
            VerificationStatus.FAILED.value,
            VerificationStatus.INFRASTRUCTURE_ERROR.value,
        }:
            return {**effective_update, **gate_update}
        grading_state = {**effective_state, **(gate_update or {})}
        rubric_update = await super().aafter_agent(grading_state, runtime)
        if rubric_update and rubric_update.get("jump_to") == "model":
            await asyncio.to_thread(
                session_manager.update_goal_completion_request_status,
                session_id,
                request_id,
                "needs_revision",
                reason="rubric_revision_requested",
            )
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


def _resolve_subagent_model(model_name: str, *, binding: str = "agent") -> str | BaseChatModel:
    """Resolve a subagent model spec.

    - Strings containing a colon (e.g. "qwen:qwen3.7") are passed through for
      LangChain init_chat_model resolution (direct provider).
    - Provider Registry-backed model bindings are used by the
      application-owned ModelClient; a bare model name is
      wrapped with the currently bound direct Provider rather than inferred
      from a legacy Higress route.
    """
    if binding != "agent":
        from llm.model_client import ModelClientChatModel

        return ModelClientChatModel(
            role="subagent",
            streaming=True,
            binding=binding,
        )
    if ":" not in model_name:
        from llm.model_client import ModelClientChatModel

        return ModelClientChatModel(
            role="subagent",
            streaming=True,
            model_override=model_name,
        )
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
    context_prompt: str = "",
) -> SubAgent:
    """Build a single SubAgent spec from a settings item."""
    name = item.get("name", "subagent") or "subagent"
    is_image_analyzer = _is_image_analyzer_item(item)
    model_name = item.get("model", "") or ""
    # ``subagents.image_analyzer.model`` is retained in config.json for
    # backwards-compatible display/import only. The built-in image workload
    # has one authoritative Provider binding shared by both settings entries.
    model = (
        _resolve_subagent_model(model_name, binding="image_analyzer")
        if is_image_analyzer
        else _resolve_subagent_model(model_name) if model_name else None
    )
    description = item.get("description") or f"Subagent `{name}`."
    route_trigger = str(item.get("route_trigger") or "").strip()
    if route_trigger and route_trigger not in description:
        description = (
            f"{description} Use this subagent when the main request matches this routing hint: `{route_trigger}`."
        )
    native_hint = (
        "Use this subagent via the native task tool whenever the user provides image attachments "
        "or local image paths. Preserve the harness_attachment_session_id and image refs from the user "
        "message in the task description."
    )
    if is_image_analyzer and native_hint not in description:
        description = f"{description} {native_hint}"
    system_prompt = item.get("system_prompt") or DEFAULT_IMAGE_ANALYZER_PROMPT
    system_prompt = (
        f"{system_prompt}\n\n"
        "Never ask the user directly. If blocked, return only one JSON object with "
        '`{"status":"blocked","question_for_parent":"...","summary":"..."}` so the parent Agent can decide. '
        "Do not invent Evidence IDs or exact numeric results in prose."
    )
    if context_prompt:
        system_prompt = f"{system_prompt}\n\n{context_prompt}"
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
    context_prompt: str = "",
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
            "system_prompt": (
                "Complete the delegated task concisely. Read an applicable project Skill before using its business tools. "
                "Use standard shell cp/mv/mkdir for directory-authorized filesystem operations, "
                "write_file for full writes, patch_file for anchored local edits, and "
                "materialize_source_ref for server data references. Harness permissions are inherited from the "
                "parent Run, so never ask the user for a separate subagent permission. Return registered Evidence IDs, "
                "SQL generation/validation receipt IDs and Artifact hashes instead of copying exact data through prose. "
                "Never ask the user directly; return a concise blocker and question to the parent Agent instead."
                " If blocked, return only one JSON object with status=blocked, question_for_parent and summary."
                f"{context_prompt}"
            ),
            "tools": default_tools,
            "skills": list(default_skills),
            "middleware": middleware_factory() if middleware_factory else [],
        }
    )
    for item in items:
        if not item.get("enabled", False):
            continue
        subagents.append(
            _build_subagent_item(
                item,
                default_tools,
                default_skills,
                middleware_factory,
                context_prompt,
            )
        )
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

        llm = ModelClient(role="title", temperature=0.3, thinking_enabled=False)
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
        run_id: str = "",
        query_id: str = "",
        goal_id: str = "",
        goal_revision: int | None = None,
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
        safe_goal = re.sub(r"[^A-Za-z0-9_-]+", "_", goal_id)
        scratch_scope = f"goal-{safe_goal}-r{int(goal_revision or 1)}" if safe_goal else safe_query
        scratch_project_root = self._base_dir / "data" / "harness-scratch" / "projects" / workspace_digest
        scratch_relative = f"{safe_session}/{scratch_scope}"
        scratch_scope_dir = scratch_project_root / safe_session / scratch_scope
        scratch_scope_dir.mkdir(parents=True, exist_ok=True)
        terminal_config = config.load_config().get("harness", {}).get("terminal", {})
        managed_readonly_mounts = [
            {
                "source": str(skills_dir.resolve()),
                "target": "/skills",
            }
        ]
        terminal_config = {
            **terminal_config,
            "_scratch_host_path": str(scratch_scope_dir.resolve()),
            "docker": {
                **dict(terminal_config.get("docker") or {}),
                "_managed_user_toolchain": True,
                "_managed_readonly_mounts": managed_readonly_mounts,
                "_managed_writable_mounts": [
                    {
                        "source": str(scratch_project_root.resolve()),
                        "target": "/harness-scratch",
                    },
                    {
                        # Commands and scripts receive the same Goal-scoped
                        # namespace exposed by file tools.  This makes a literal
                        # /scratch path work even when it lives inside a Python
                        # or Node file and cannot be shell-rewritten.
                        "source": str(scratch_scope_dir.resolve()),
                        "target": "/scratch",
                    },
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
            "/scratch/": FilesystemBackend(root_dir=scratch_scope_dir, virtual_mode=True),
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
            run_id=run_id,
            query_id=query_id,
        )
        backend.execution_mode = selection.mode
        backend.execution_backend_id = workspace_backend.id
        backend.execution_backend = workspace_backend
        backend.execution_fallback_reason = selection.fallback_reason
        backend.execution_dependency_plan = selection.dependency_plan
        backend.execution_scratch_host_path = str(scratch_scope_dir.resolve())
        backend.execution_scratch_goal_id = goal_id
        backend.execution_scratch_goal_revision = goal_revision
        backend.managed_host_path_aliases = {
            "/knowledge": str(knowledge_dir.resolve()),
            "/semantic-assets": str(semantic_assets_dir.resolve()),
            "/sql-guardrails": str(sql_guardrails_dir.resolve()),
            "/analytics-models": str(analytics_models_dir.resolve()),
            "/skills": str(skills_dir.resolve()),
            "/large_tool_results": str(large_tool_results_dir.resolve()),
        }
        backend.external_directory_writable_enabled = bool(
            terminal_config.get("external_directory_writable_enabled", False)
        )
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
        permission_reviewer: PermissionReviewer | None = None,
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
            RunScopeMiddleware(),
            AnalysisTemplateMiddleware(base_dir=self._base_dir),
            SemanticAssetsMiddleware(base_dir=self._base_dir),
            ExternalFilePermissionMiddleware(),
            WorkspacePathRouterMiddleware(workspace_backend),
            VerificationActivationMiddleware(),
            *(
                [VersionedPatchMiddleware(workspace_backend, compact_model_surface=True)]
                if workspace_backend is not None
                else []
            ),
            *([AttachmentEditMiddleware(workspace_backend)] if workspace_backend is not None else []),
            DelegationControlMiddleware(),
            GoalCompletionMiddleware(),
            UserInputBoundaryMiddleware(),
            SkillIntentRouterMiddleware(),
            ToolsetMiddleware(
                skills_dir=self._base_dir / "skills",
                toolsets_by_skill=toolset_mapping,
            ),
            ToolGuideMiddleware(base_dir=self._base_dir),
            # Keep execution policy in the Agent middleware chain: its
            # before_model hook owns durable HITL boundaries such as a prepared
            # Skill Manager batch and must run before another model turn.
            ToolExecutionPipeline(
                known_tools=set(known_tools or ()),
                backend_mode=backend_mode,
                permission_context=permission_context,
                base_dir=self._base_dir,
                reviewer=permission_reviewer,
                workspace_backend=getattr(workspace_backend, "execution_backend", workspace_backend),
            ),
        ]
        prompt_cache_cfg = config.load_config().get("harness", {}).get("prompt_cache", {})
        if bool(prompt_cache_cfg.get("ordered_system_sections", False)):
            # Memory is versioned context, not stable core.  Put it after the
            # project/semantic sections so a memory edit invalidates only its
            # suffix.  The list order is also reflected in runtime inventory.
            memory_index = next(
                (index for index, item in enumerate(middlewares) if isinstance(item, MemoryMiddleware)),
                None,
            )
            semantic_index = next(
                (index for index, item in enumerate(middlewares) if isinstance(item, SemanticAssetsMiddleware)),
                None,
            )
            if memory_index is not None and semantic_index is not None and memory_index < semantic_index:
                memory = middlewares.pop(memory_index)
                semantic_index -= 1
                middlewares.insert(semantic_index + 1, memory)
        tool_context_cfg = ToolContextConfig.from_mapping(config.get_deepagents_tool_context_config())
        if tool_context_cfg.enabled:
            middlewares.append(ToolContextCompactionMiddleware(tool_context_cfg))
        rubric_cfg = config.load_config().get("harness", {}).get("completion", {}).get("rubric", {})
        if rubric_cfg.get("enabled", True) and rubric_model is not None:
            max_iterations = rubric_cfg.get("max_iterations", 2)
            if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or not 1 <= max_iterations <= 20:
                max_iterations = 2
            max_stagnant_repairs = rubric_cfg.get("max_stagnant_repairs", 2)
            if (
                not isinstance(max_stagnant_repairs, int)
                or isinstance(max_stagnant_repairs, bool)
                or not 1 <= max_stagnant_repairs <= 20
            ):
                max_stagnant_repairs = 2
            middlewares.append(
                PuddingClawRubricMiddleware(
                    model=rubric_model,
                    max_iterations=max_iterations,
                    max_stagnant_repairs=max_stagnant_repairs,
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

        from langgraph.checkpoint.memory import InMemorySaver

        if self._checkpointer is not None:
            return self._checkpointer

        self._checkpointer = InMemorySaver()
        self._checkpointer_info = {
            "type": "memory",
            "scope": "active_sse_run",
        }
        logger.info(
            "Initialized live DeepAgents checkpointer: type=memory scope=active_sse_run"
        )
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
        run_id: str = "",
        current_message: str = "",
        current_attachments: list[dict[str, Any]] | None = None,
        goal_id: str = "",
        goal_revision: int | None = None,
        execution_backend: Any | None = None,
    ) -> list[Any]:
        """Return PuddingClaw tools that do not overlap DeepAgents built-ins."""

        assert self._base_dir is not None
        tools = []
        registered_tool_names = agent_custom_tool_names()
        for tool in get_all_tools(self._base_dir):
            if getattr(tool, "name", "") not in registered_tool_names:
                continue
            if getattr(tool, "name", "") in {
                "install_skill",
                "update_skill",
                "request_user_input",
            }:
                # DeepAgents binds these controls explicitly below or handles
                # them through the Session-bound frontend API. Keeping the
                # loader-created copies out avoids unbound/duplicate tools.
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
            elif getattr(tool, "name", "") == "read_evidence":
                evidence_updates = {
                    "session_id": session_id,
                    "workspace_path": str(workspace_path),
                }
                try:
                    tool = tool.model_copy(update=evidence_updates)
                except Exception:
                    for key, value in evidence_updates.items():
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
                "database_schema_inspect",
                "database_query_result_source",
            }:
                database_updates = {"session_id": session_id, "query_id": query_id}
                try:
                    tool = tool.model_copy(update=database_updates)
                except Exception:
                    for key, value in database_updates.items():
                        setattr(tool, key, value)
            elif getattr(tool, "name", "") == "llm_wiki_create_raw":
                wiki_intake_updates = {
                    "session_id": session_id,
                    "query_id": query_id,
                    "current_message": current_message,
                    "current_attachments": list(current_attachments or []),
                }
                try:
                    tool = tool.model_copy(update=wiki_intake_updates)
                except Exception:
                    for key, value in wiki_intake_updates.items():
                        setattr(tool, key, value)
            elif getattr(tool, "name", "") == "llm_wiki_context":
                try:
                    tool = tool.model_copy(update={"allow_ingest": False})
                except Exception:
                    setattr(tool, "allow_ingest", False)
            elif getattr(tool, "name", "") == "llm_wiki_start_ingest":
                wiki_job_updates = {
                    "session_id": session_id,
                    "query_id": query_id,
                }
                try:
                    tool = tool.model_copy(update=wiki_job_updates)
                except Exception:
                    for key, value in wiki_job_updates.items():
                        setattr(tool, key, value)
            elif getattr(tool, "name", "") in {
                "prepare_skill_install",
                "prepare_skill_update",
            }:
                skill_plan_updates = {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run_id": run_id,
                }
                try:
                    tool = tool.model_copy(update=skill_plan_updates)
                except Exception:
                    for key, value in skill_plan_updates.items():
                        setattr(tool, key, value)

            tools.append(tool)
        installer = getattr(execution_backend, "install_packages", None)
        if callable(installer):
            tools.append(create_install_packages_tool(installer))
        # This tool is attached only to the main Agent.  The middleware owns
        # its trusted Run binding; subagents never receive the declaration.
        tools.append(create_update_goal_tool())
        tools.append(
            create_request_user_input_tool(
                session_id=session_id,
                query_id=query_id,
                run_id=run_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
            )
        )
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
            "HarnessTodoMiddleware",
            "puddingclaw.harness_profile",
            ["wrap_model_call"],
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
            {
                "name": "read_evidence",
                "source": "puddingclaw.harness",
                "description": "按稳定 Evidence ID 读取历史原始结果",
            },
            {"name": "write_file", "source": "deepagents.builtin", "description": "写入文件"},
            {
                "name": "materialize_source_ref",
                "source": "puddingclaw.harness",
                "description": "将不可变 SourceReference 直接物化为文件或类型化 Slot",
            },
            {"name": "patch_file", "source": "puddingclaw.harness", "description": "按文件版本原子应用补丁"},
            {"name": "delete_file", "source": "puddingclaw.harness", "description": "删除精确授权的文件"},
            {
                "name": "validate_html_report",
                "source": "puddingclaw.harness",
                "description": (
                    "常规检查 HTML 结构与本地引用；合同明确要求 E2E 时才使用离线 Chromium，并生成 hash 绑定 Receipt"
                ),
            },
            {
                "name": "prepare_attachment_edit",
                "source": "puddingclaw.harness",
                "description": "准备不可变附件的可编辑副本",
            },
            {"name": "publish_attachment", "source": "puddingclaw.harness", "description": "发布附件编辑结果为新附件"},
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
        for item in mounted:
            descriptor = tool_control_descriptor(str(item.get("name") or ""))
            if descriptor is not None:
                item["control"] = descriptor.as_dict()
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

    def _skills_inventory(self) -> list[dict[str, Any]]:
        assert self._base_dir is not None
        return [
            {
                "name": item["name"],
                "description": item["description"],
                "location": item["path"].lstrip("/"),
                "system_prompt_source": "/skills/",
                "in_system_prompt": True,
                "href": f"/skills?skill={item['skill_id']}",
            }
            for item in discover_skill_catalog(self._base_dir / "skills")
        ]

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
            "prompt_cache": config.load_config().get("harness", {}).get("prompt_cache", {}),
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
            local_directory_paths = [
                path
                for path in extract_local_directory_paths(message)
                if not path.replace("\\", "/").startswith(VIRTUAL_RESOURCE_PREFIXES)
            ]
            external_paths_needing_permission: list[str] = []
            external_resource_paths: list[str] = []
            external_directory_paths: list[str] = []
            directory_raw_paths: set[str] = set()
            workspace = Path(workspace_path).expanduser().resolve() if workspace_path else None
            for directory_path in local_directory_paths:
                path = Path(directory_path).expanduser().resolve()
                if workspace is None or not cls._is_relative_to(path, workspace):
                    external_directory_paths.append(str(path))
            for raw_path in local_resource_paths:
                path = Path(raw_path.replace("\\ ", " ").strip().strip("'\"")).expanduser().resolve()
                if path.is_dir():
                    directory_raw_paths.add(raw_path)
                    if workspace is None or not cls._is_relative_to(path, workspace):
                        external_directory_paths.append(str(path))
                    continue
                if (
                    workspace is not None
                    and not cls._is_relative_to(path, workspace)
                    and not cls._can_read_local_image_path(path, session_id=session_id, workspace_path=workspace)
                ):
                    external_resource_paths.append(str(path))
                    if raw_path in image_paths:
                        external_paths_needing_permission.append(str(path))
            non_image_resource_paths = [
                path
                for path in local_resource_paths
                if path not in set(image_paths) and path not in directory_raw_paths
            ]

            if not attachment_refs and not local_resource_paths and not local_directory_paths:
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
                    + "\n以上非 workspace 本地路径请直接调用 read_file(file_path=原始绝对路径)；"
                    "未授权时系统会请求精确权限并自动重放。"
                )
            if external_resource_paths and not external_paths_needing_permission:
                paths = "\n".join(f"- {path}" for path in external_resource_paths)
                notes.append(
                    "[外部文件授权] 检测到 workspace 外的本地文件路径。直接对原始绝对路径使用 "
                    f"read_file/write_file/materialize_source_ref/patch_file：\n{paths}\n"
                    "未授权时系统会请求精确文件权限并重放原调用。若确认必须发现同目录依赖，"
                    "对直接父目录调用 ls/glob/grep；系统只请求该 exact directory，不得猜测兄弟路径或提升到更高祖先目录。"
                    "获批后的读写由 HostFileBroker 原子落到正式路径；不要创建 /workspace 或 /scratch 影子副本。"
                    "文件授权不授予 execute 对宿主绝对路径的访问。"
                )
            if external_paths_needing_permission:
                paths = "\n".join(f"- {path}" for path in external_paths_needing_permission)
                notes.append(
                    "[外部文件授权] 检测到 workspace 外的本地图片路径。若尚未完成授权，主 Agent 必须先通过 "
                    f"read_resource 触发授权后再分析：\n{paths}"
                )
            if external_directory_paths:
                paths = "\n".join(f"- {path}" for path in external_directory_paths)
                notes.append(
                    "[外部目录授权] 检测到 workspace 外的本地目录。读取可直接使用 "
                    f"ls/glob/grep/read_file；复制、移动、建目录直接使用 execute 中的标准 cp/mv/mkdir：\n{paths}\n"
                    "首次 shell 访问会一次性请求所需目录及 read/write/delete 能力；授权后命令原样重放，"
                    "默认由内核沙箱执行。write_file/patch_file 的精确或事务写入仍由内部 HostFileBroker 原子提交，"
                    "模型无需处理 lease、staged path 或 hash 编排。"
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
        *,
        existing_content: str = "",
    ) -> str:
        """Render only Tool-authoritative artifacts not already published.

        Artifact identity is the canonical local path, not the artifact receipt
        id or the exact Markdown produced by either the model or this renderer.
        This keeps retries and differently formatted links from publishing the
        same file more than once.
        """

        seen: set[str] = set()
        links: list[str] = []
        for activation in activations:
            if not isinstance(activation, dict) or activation.get("status") != "succeeded":
                continue
            for ref in activation.get("evidence_refs") or []:
                if not isinstance(ref, dict) or ref.get("kind") != "artifact_write":
                    continue
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
                artifact_identity = str(local_path)
                if artifact_identity in seen:
                    continue
                seen.add(artifact_identity)
                label_path = str(ref.get("virtual_path") or ref.get("path") or local_path)
                literal_file_uri = f"file://{artifact_identity}"
                published_references = {
                    local_path.as_uri(),
                    literal_file_uri,
                    f"]({artifact_identity})",
                    f"](<{artifact_identity}>)",
                }
                if existing_content and any(
                    candidate and candidate in existing_content for candidate in published_references
                ):
                    continue
                links.append(f"- [打开 {local_path.name}]({local_path.as_uri()})  \n  `{label_path}`")
        if not links:
            return ""

        # When the model already ended with an artifact list, extend that list
        # instead of creating a second identically titled section. The links
        # themselves remain Tool-authoritative.
        artifact_heading = re.compile(r"(?m)^\s*(?:#{1,6}\s+)?产物\s*[：:]\s*$")
        matches = list(artifact_heading.finditer(existing_content))
        if matches:
            trailing_lines = [
                line.strip() for line in existing_content[matches[-1].end() :].splitlines() if line.strip()
            ]
            if trailing_lines and all(line.startswith(("- [", "- `", "`")) for line in trailing_lines):
                return "\n" + "\n".join(links)

        return "\n\n产物：\n" + "\n".join(links)

    def _build_preflight_task_profile(
        self,
        *,
        objective: str,
        analytics_model_id: str | None,
        skill_catalog: list[dict[str, Any]],
        explicit_skill_hints: list[str] | None = None,
    ) -> RunTaskProfile:
        """Build non-semantic Run safety state before the main Agent starts.

        Installed Skill selection deliberately does not happen here.  The main
        Agent receives the dynamic catalog and selects a Skill by reading its
        authoritative SKILL.md, which ToolsetMiddleware records on the Run.
        """

        return TaskProfileClassifier.classify(
            message=objective,
            analytics_model_id=analytics_model_id,
            skill_catalog=skill_catalog,
            explicit_skill_hints=explicit_skill_hints,
        )

    async def _classify_task_profile(
        self,
        *,
        objective: str,
        analytics_model_id: str | None,
        model_override: str | None,
        skill_catalog: list[dict[str, Any]],
        explicit_skill_hints: list[str] | None = None,
        independent_skill_review: bool = False,
    ) -> RunTaskProfile:
        """Run the semantic Router as a bounded soft enhancement."""

        router_model = ModelClientChatModel(
            role="task_classifier",
            temperature=0,
            streaming=False,
            thinking_enabled=False,
            model_override=model_override or None,
        )
        primary = SemanticTaskProfileClassifier.classify(
            message=objective,
            analytics_model_id=analytics_model_id,
            model=router_model,
            skill_catalog=skill_catalog,
            explicit_skill_hints=explicit_skill_hints,
        )
        if not independent_skill_review:
            return await asyncio.wait_for(
                primary,
                timeout=_TASK_ROUTER_TIMEOUT_SECONDS,
            )
        skeptic_model = ModelClientChatModel(
            role="task_classifier",
            temperature=0,
            streaming=False,
            thinking_enabled=False,
            model_override=model_override or None,
        )
        primary_task = asyncio.create_task(primary)
        skeptic_task = asyncio.create_task(
            SemanticTaskProfileClassifier.classify_as_skill_skeptic(
                message=objective,
                analytics_model_id=analytics_model_id,
                model=skeptic_model,
                skill_catalog=skill_catalog,
                explicit_skill_hints=explicit_skill_hints,
            )
        )
        router_tasks = (primary_task, skeptic_task)
        try:
            done, _ = await asyncio.wait(
                router_tasks,
                timeout=_TASK_ROUTER_TIMEOUT_SECONDS,
            )
        finally:
            unfinished = [task for task in router_tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)
        completed: list[RunTaskProfile] = []
        for task in (primary_task, skeptic_task):
            if task not in done or task.cancelled():
                continue
            try:
                completed.append(task.result())
            except Exception:
                logger.warning(
                    "One independent Task Router branch failed; using the other branch",
                    exc_info=True,
                )
        if not completed:
            raise TimeoutError("All independent Task Router branches timed out or failed")
        merged = completed[0]
        for enhancement in completed[1:]:
            merged = TaskProfileClassifier.merge_semantic_enhancement(
                merged,
                enhancement,
                analytics_model_id=analytics_model_id,
            )
        return merged

    async def _classify_goal_turn(
        self,
        *,
        session_id: str,
        message: str,
        goal: dict[str, Any],
        model_override: str | None = None,
    ) -> GoalTurnDecision:
        """Classify lifecycle ownership before a Run or contract is created."""

        deterministic = GoalTurnRouter.deterministic(
            message,
            goal_id=str(goal.get("goal_id") or ""),
        )
        if deterministic is not None:
            return deterministic
        recent_execution_context = self._goal_turn_recent_execution_context(
            session_id=session_id,
            goal=goal,
        )
        router_model = ModelClientChatModel(
            role="task_classifier",
            temperature=0,
            streaming=False,
            thinking_enabled=False,
            model_override=model_override or None,
        )
        try:
            return await asyncio.wait_for(
                GoalTurnRouter.classify(
                    message=message,
                    goal=goal,
                    model=router_model,
                    recent_execution_context=recent_execution_context,
                ),
                timeout=_GOAL_TURN_ROUTER_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return GoalTurnRouter.contextual_fallback(
                message=message,
                goal=goal,
                recent_execution_context=recent_execution_context,
                reason="classifier_timeout",
            )

    @staticmethod
    def _goal_turn_recent_execution_context(
        *,
        session_id: str,
        goal: dict[str, Any],
    ) -> dict[str, Any]:
        """Project bounded recent execution state for contextual Goal routing."""

        latest_run = session_manager.get_run_state(session_id)
        run_context: dict[str, Any] = {}
        if isinstance(latest_run, dict) and str(latest_run.get("goal_id") or "") == str(goal.get("goal_id") or ""):
            run_context = {
                "run_id": str(latest_run.get("run_id") or ""),
                "status": str(latest_run.get("status") or ""),
                "outcome": str(latest_run.get("outcome") or ""),
                "error": str(latest_run.get("error") or "")[:300],
                "handoff_summary": (
                    latest_run.get("handoff_summary") if isinstance(latest_run.get("handoff_summary"), dict) else None
                ),
            }

        recent_tools: list[dict[str, str]] = []
        recent_assistant_actions: list[dict[str, str]] = []
        seen_tool_calls: set[str] = set()
        messages = session_manager.load_session(session_id)
        for persisted in reversed(messages[-20:]):
            if not isinstance(persisted, dict) or persisted.get("role") != "assistant":
                continue
            if len(recent_assistant_actions) < 2:
                action_text = str(persisted.get("content") or "").strip()
                if not action_text:
                    for segment in reversed(persisted.get("segments") or []):
                        if not isinstance(segment, dict):
                            continue
                        action_text = str(segment.get("content") or "").strip()
                        if action_text:
                            break
                if action_text:
                    # The tail contains the last announced execution choice,
                    # which is what a short user correction most often refers
                    # to. Do not project reasoning or tool output.
                    recent_assistant_actions.append(
                        {
                            "content": action_text[-1_200:],
                            "status": str(persisted.get("status") or ""),
                            "interrupted": str(bool(persisted.get("interrupted"))).lower(),
                        }
                    )
            tool_calls = list(persisted.get("tool_calls") or [])
            for segment in persisted.get("segments") or []:
                if isinstance(segment, dict):
                    tool_calls.extend(segment.get("tool_calls") or [])
            for tool_call in reversed(tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                call_id = str(tool_call.get("id") or "")
                if call_id and call_id in seen_tool_calls:
                    continue
                if call_id:
                    seen_tool_calls.add(call_id)
                tool_name = str(tool_call.get("tool") or tool_call.get("name") or "")
                raw_input = tool_call.get("input")
                parsed_input: dict[str, Any] = {}
                if isinstance(raw_input, dict):
                    parsed_input = raw_input
                elif isinstance(raw_input, str):
                    try:
                        candidate = json.loads(raw_input)
                        if isinstance(candidate, dict):
                            parsed_input = candidate
                    except (TypeError, ValueError):
                        parsed_input = {}
                target = ""
                for key in (
                    "target_path",
                    "destination_path",
                    "file_path",
                    "path",
                    "source_path",
                ):
                    if parsed_input.get(key):
                        target = str(parsed_input[key])[:500]
                        break
                recent_tools.append(
                    {
                        "tool": tool_name,
                        "target": target,
                        "status": str(tool_call.get("status") or ""),
                        "is_error": str(bool(tool_call.get("is_error"))).lower(),
                    }
                )
                if len(recent_tools) >= 8:
                    break
            if len(recent_tools) >= 8:
                break
        recent_tools.reverse()
        return {
            "latest_run": run_context,
            "recent_tools": recent_tools,
            "recent_assistant_actions": recent_assistant_actions,
        }

    @staticmethod
    def _reusable_task_profile(
        *,
        session_id: str,
        message: str,
        analytics_model_id: str | None,
        internal_continuation: bool,
    ) -> RunTaskProfile | None:
        """Reuse the latest task understanding for explicit continuations."""

        if not internal_continuation and not _TASK_PROFILE_CONTINUATION_RE.fullmatch(message.strip()):
            return None
        previous = session_manager.get_run_state(session_id)
        profile_payload = previous.get("task_profile") if isinstance(previous, dict) else None
        if not isinstance(profile_payload, dict):
            return None
        try:
            profile = RunTaskProfile.model_validate(profile_payload)
        except Exception:
            return None
        profile.available_context_refs = [f"analytics_model:{analytics_model_id}"] if analytics_model_id else []
        if "reused_for_continuation" not in profile.reasons:
            profile.reasons.append("reused_for_continuation")
        profile.classifier = "session_continuation"
        return profile

    def _analytics_model_context(
        self,
        analytics_model_id: str | None,
        *,
        query: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        if not analytics_model_id:
            return "", None
        assert self._base_dir is not None
        try:
            model = get_analytics_model_registry(self._base_dir).get_model_context(
                analytics_model_id,
                query=query,
            )
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
        resolved_templates = model.get("resolved_templates") or {}
        model_visible_templates = {
            str(template_id): {
                key: value
                for key, value in template.items()
                if key not in {"guide_frontmatter", "compiled_semantic_scope"}
            }
            for template_id, template in resolved_templates.items()
            if isinstance(template, dict)
        }
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
            "resolved_references": model.get("resolved_references") or {},
            "resolved_templates": model_visible_templates,
        }
        relation_context = model.get("asset_relations") or []
        relation_text = json.dumps(relation_context, ensure_ascii=False, indent=2)
        derived_path_text = json.dumps(model.get("derived_dimension_paths") or [], ensure_ascii=False, indent=2)
        data_asset_text = json.dumps(model.get("data_assets") or [], ensure_ascii=False, indent=2)
        resolved_reference_text = json.dumps(model.get("resolved_references") or {}, ensure_ascii=False, indent=2)
        resolved_template_text = json.dumps(model_visible_templates, ensure_ascii=False, indent=2)
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
            "服务端已解析 Reference（virtual_path 可直接用于 read_file）：\n"
            f"```json\n{resolved_reference_text}\n```\n\n"
            "服务端已解析模板（virtual_path/guide_virtual_path 可直接用于 read_file）：\n"
            f"```json\n{resolved_template_text}\n```\n"
            "你必须结合当前用户意图与对话上下文，自主比较模板的 use_when/do_not_use_when。"
            "决定使用某个模板后，先 read_file 读取它的 guide_virtual_path；成功读取会把模板 manifest "
            "渐进写入本轮可信 state，供 SQL 等后续工具使用。然后按 guide 继续读取入口与所需 assets。"
            "不使用模板时不要读取其 guide。必须使用上述完整路径，不得从原始 metadata 手工拼接，也不得用 glob 猜测。\n\n"
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
        omitted_history_count = max(
            0,
            len(history) - _HISTORICAL_MAX_MESSAGE_COUNT,
        )
        if omitted_history_count:
            history = history[-_HISTORICAL_MAX_MESSAGE_COUNT:]
            messages.append(
                SystemMessage(
                    content=(
                        "[Historical context projection] "
                        f"{omitted_history_count} older messages were omitted by the "
                        "deterministic request budget. Durable Goal, Todo, Artifact and "
                        "Evidence state remains available through control-plane tools."
                    )
                )
            )
        detailed_tool_calls: set[tuple[int, int]] = set()
        remaining_tool_budget = _HISTORICAL_TOOL_INLINE_BUDGET_CHARS
        for history_index in range(len(history) - 1, -1, -1):
            history_item = history[history_index]
            for call_index in range(len(history_item.get("tool_calls") or []) - 1, -1, -1):
                historical_call = (history_item.get("tool_calls") or [])[call_index]
                if not isinstance(historical_call, dict):
                    continue
                raw_value = str(historical_call.get("raw_output", historical_call.get("output", "")) or "")
                raw_ref = historical_call.get("raw_output_ref")
                ref_kind = str(raw_ref.get("kind") or "") if isinstance(raw_ref, dict) else ""
                projected_cost = (
                    1_200
                    if ref_kind in {"deepagents_large_tool_result", "sql_query_result"}
                    else min(len(raw_value), 4_000 if len(raw_value) > 16_000 else len(raw_value))
                )
                if projected_cost <= remaining_tool_budget:
                    detailed_tool_calls.add((history_index, call_index))
                    remaining_tool_budget -= projected_cost

        detailed_text_messages: set[int] = set()
        remaining_text_budget = _HISTORICAL_MESSAGE_INLINE_BUDGET_CHARS
        for history_index in range(len(history) - 1, -1, -1):
            history_item = history[history_index]
            if history_item.get("tool_calls"):
                continue
            historical_content = str(history_item.get("content") or "")
            projected_cost = min(len(historical_content), 8_000)
            if projected_cost <= remaining_text_budget:
                detailed_text_messages.add(history_index)
                remaining_text_budget -= projected_cost

        remaining_args_budget = _HISTORICAL_TOOL_ARGS_BUDGET_CHARS
        used_protocol_ids: set[str] = set()
        for message_index, item in enumerate(history):
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant", "system"} or content is None:
                continue
            if not item.get("tool_calls") and message_index not in detailed_text_messages:
                raw_content = str(content)
                content = f"[Historical message minimal projection]\n{raw_content[:240]}" + (
                    f"\n... [omitted {len(raw_content) - 240} chars]" if len(raw_content) > 240 else ""
                )
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
                    persisted_tc_id = str(tc.get("id") or f"missing-{message_index}-{index}")
                    tc_id = persisted_tc_id
                    if tc.get("historical"):
                        identity = "\0".join(
                            (
                                str(session_id or ""),
                                str(item.get("query_id") or ""),
                                str(tc.get("source_run_id") or ""),
                                persisted_tc_id,
                            )
                        )
                        tc_id = f"historical_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
                    collision = 1
                    base_tc_id = tc_id
                    while tc_id in used_protocol_ids:
                        collision += 1
                        tc_id = f"{base_tc_id}_{collision}"
                    used_protocol_ids.add(tc_id)
                    tool_name = tc.get("tool") or tc.get("name") or "unknown_tool"
                    normalized_tool_calls.append((tc, tool_name, tc_id))

                lc_tool_calls = []
                for tc, tool_name, tc_id in normalized_tool_calls:
                    tool_input = tc.get("input") or tc.get("args") or {}
                    if isinstance(tool_input, dict):
                        parsed_args = dict(tool_input)
                    else:
                        parsed_args = cls._safe_parse_tool_args(str(tool_input))
                    serialized_args = json.dumps(
                        parsed_args,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    if len(serialized_args) > remaining_args_budget:
                        parsed_args = {
                            "_historical_input_omitted": True,
                            "evidence_id": str(tc.get("evidence_id") or ""),
                            "original_size_chars": len(serialized_args),
                        }
                    else:
                        remaining_args_budget -= len(serialized_args)
                    lc_tool_calls.append({"name": tool_name, "args": parsed_args, "id": tc_id})

                is_historical = any(bool(tc.get("historical")) for tc, _, _ in normalized_tool_calls)
                ai_kwargs: dict[str, Any] = {
                    "content": content,
                    "tool_calls": lc_tool_calls,
                    "additional_kwargs": {
                        "puddingclaw_historical": is_historical,
                        "puddingclaw_source_query_id": str(item.get("query_id") or ""),
                    },
                }
                if item.get("reasoning_content"):
                    ai_kwargs["reasoning_content"] = item["reasoning_content"]
                messages.append(AIMessage(**ai_kwargs))

                for tc, tool_name, tc_id in normalized_tool_calls:
                    stored_output = tc.get("output", "")
                    stored_raw_output = tc.get("raw_output", stored_output)
                    if stored_raw_output is None or str(stored_raw_output).strip() == "":
                        stored_raw_output = MISSING_TOOL_OUTPUT_PLACEHOLDER
                    raw_ref = tc.get("raw_output_ref") if isinstance(tc.get("raw_output_ref"), dict) else {}
                    evidence_id = str(tc.get("evidence_id") or "")
                    if raw_ref.get("kind") == "deepagents_large_tool_result":
                        model_output = (
                            "The complete historical result is stored outside the current Query namespace. "
                            f"Use read_evidence(evidence_id={json.dumps(evidence_id)}, offset=0, limit=20000) "
                            "to inspect it; do not use the historical /large_tool_results path directly.\n\n"
                            f"Historical pointer/preview:\n{stored_output}"
                        )
                        model_sources = list(tc.get("sources", []) or [])
                    elif raw_ref.get("kind") == "sql_query_result":
                        result_id = str(raw_ref.get("result_id") or "")
                        model_output = (
                            f"Historical SQL result is materialized as result_id={result_id}. "
                            f"Use read_evidence(evidence_id={json.dumps(evidence_id)}, page=1, page_size=100) "
                            "to read the saved artifact without rerunning SQL.\n\n"
                            f"Historical preview/profile:\n{stored_output}"
                        )
                        model_sources = list(tc.get("sources", []) or [])
                    elif bool(tc.get("historical")) and (message_index, index) not in detailed_tool_calls:
                        raw_text = str(stored_raw_output)
                        model_output = (
                            "[Historical Evidence minimal projection]\n"
                            f"evidence_id={evidence_id}; tool={tool_name}; status={tc.get('status') or 'unknown'}; "
                            "use read_evidence for exact details.\n"
                            f"preview={raw_text[:320]}"
                        )
                        model_sources = list(tc.get("sources", []) or [])
                    elif bool(tc.get("historical")) and len(str(stored_raw_output)) > 16_000:
                        context_metadata = tc.get("context_compaction")
                        ready_projection = (
                            str(tc.get("context_output") or "")
                            if isinstance(context_metadata, dict)
                            and context_metadata.get("status") == "ready"
                            and context_metadata.get("source_hash") == tc.get("source_hash")
                            else ""
                        )
                        if ready_projection:
                            model_output = ready_projection
                        else:
                            raw_text = str(stored_raw_output)
                            model_output = (
                                "[Historical Evidence projection; raw result is preserved]\n"
                                f"evidence_id={evidence_id}; use read_evidence with offset/limit for exact details.\n"
                                f"{raw_text[:2800]}\n\n... [projection omitted {len(raw_text) - 4000} chars] ...\n\n"
                                f"{raw_text[-1200:]}"
                            )
                        model_sources = list(tc.get("sources", []) or [])
                    elif (
                        isinstance(tc.get("context_compaction"), dict)
                        and tc["context_compaction"].get("status") == "ready"
                        and tc["context_compaction"].get("source_hash") == tc.get("source_hash")
                        and tc.get("context_output")
                    ):
                        # A ready post-hoc Tool Context projection is the one
                        # durable choice for model history, independent of the
                        # current Run's remaining token budget.
                        model_output = str(tc.get("context_output"))
                        model_sources = list(tc.get("sources", []) or [])
                    elif tc.get("summary_source") in {"single_tool_overflow", "tool_result_clear"}:
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
                                "puddingclaw_historical": bool(tc.get("historical")),
                                "puddingclaw_evidence_id": evidence_id,
                                "puddingclaw_source_run_id": str(tc.get("source_run_id") or ""),
                                "puddingclaw_projection_profile": "detailed",
                                "puddingclaw_projection_version": "evidence-projection-v1",
                                "puddingclaw_query_id": str(item.get("query_id") or ""),
                                "puddingclaw_tool_source_hash": str(
                                    tc.get("source_hash")
                                    or (tc.get("context_compaction") or {}).get("source_hash")
                                    or session_manager._tool_context_source_hash(str(stored_raw_output))
                                ),
                            },
                            status=(
                                "error"
                                if tc.get("is_error")
                                or tc.get("status") in {"error", "failed", "interrupted"}
                                or tc.get("output_complete") is False
                                else "success"
                            ),
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
                additional_kwargs=({"puddingclaw_query_id": query_id} if query_id else {}),
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
            "data": json.dumps(to_json_compatible(payload), ensure_ascii=False),
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
        supported_types = {
            "permission_request",
            "dimension_build_rule_request",
            "logical_dataset_rule_request",
            "database_sql_revision_request",
            "user_input_request",
            "skill_plan_confirmation_request",
        }
        extracted: list[tuple[str, dict[str, Any], str]] = []
        for interrupt_item in interrupts:
            value = getattr(interrupt_item, "value", interrupt_item)
            if not isinstance(value, dict):
                raise RuntimeError("Unsupported HITL interrupt payload: expected an object")
            interrupt_type = str(value.get("type") or "")
            if interrupt_type not in supported_types:
                raise RuntimeError(f"Unsupported HITL interrupt type: {interrupt_type or '<missing>'}")
            request = value.get("request")
            if not isinstance(request, dict):
                raise RuntimeError(f"Invalid {interrupt_type} interrupt: request must be an object")
            interrupt_id = str(getattr(interrupt_item, "id", "") or "")
            extracted.append((interrupt_type, request, interrupt_id))
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

            session_id = str(context.get("session_id") or "")
            run_id = str(context.get("run_id") or "")
            if session_id and run_id:
                session_manager.transition_run_status(
                    session_id,
                    run_id,
                    "waiting_hitl",
                    expected_statuses={"running", "waiting_hitl"},
                )
                yield self._sse(
                    "run_status_changed",
                    {
                        "session_id": session_id,
                        "query_id": str(context.get("query_id") or ""),
                        "run_id": run_id,
                        "goal_id": str(context.get("goal_id") or ""),
                        "goal_revision": context.get("goal_revision"),
                        "status": "waiting_hitl",
                    },
                )

            required_events = {
                "permission_request": "permission_required",
                "dimension_build_rule_request": "dimension_build_rule_required",
                "logical_dataset_rule_request": "logical_dataset_rule_required",
                "database_sql_revision_request": "database_sql_revision_required",
                "user_input_request": "user_input_required",
                "skill_plan_confirmation_request": "skill_plan_confirmation_required",
            }
            for interrupted_type, interrupted_request, _interrupt_id in pending_interrupts:
                yield self._sse(required_events[interrupted_type], interrupted_request)

            resume_registries = {
                "permission_request": permission_resume_registry,
                "dimension_build_rule_request": dimension_build_resume_registry,
                "logical_dataset_rule_request": logical_dataset_resume_registry,
                "database_sql_revision_request": database_sql_revision_resume_registry,
                "user_input_request": user_input_resume_registry,
                "skill_plan_confirmation_request": skill_plan_resume_registry,
            }
            span_names = {
                "permission_request": "permission.decision",
                "dimension_build_rule_request": "dimension_build_rule.decision",
                "logical_dataset_rule_request": "logical_dataset_rule.decision",
                "database_sql_revision_request": "database_sql_revision.decision",
                "user_input_request": "user_input.decision",
                "skill_plan_confirmation_request": "skill_plan_confirmation.decision",
            }
            resolved_events = {
                "permission_request": "permission_resolved",
                "dimension_build_rule_request": "dimension_build_rule_resolved",
                "logical_dataset_rule_request": "logical_dataset_rule_resolved",
                "database_sql_revision_request": "database_sql_revision_resolved",
                "user_input_request": "user_input_resolved",
                "skill_plan_confirmation_request": "skill_plan_confirmation_resolved",
            }
            goal_id = str(context.get("goal_id") or "")
            decision_tasks = [
                asyncio.create_task(resume_registries[interrupted_type].wait(str(interrupted_request.get("id") or "")))
                for interrupted_type, interrupted_request, _interrupt_id in pending_interrupts
            ]
            try:
                while not all(task.done() for task in decision_tasks):
                    await asyncio.wait(decision_tasks, timeout=0.25)
                    if not goal_id:
                        continue
                    authoritative_goal = session_manager.get_goal_state(session_id, goal_id)
                    if (
                        not isinstance(authoritative_goal, dict)
                        or str(authoritative_goal.get("status") or "") != "active"
                        or bool(authoritative_goal.get("requested_status"))
                        or str(authoritative_goal.get("current_run_id") or "") != run_id
                        or int(authoritative_goal.get("objective_revision") or 1)
                        != int(context.get("goal_revision") or 1)
                    ):
                        raise asyncio.CancelledError("Goal control changed while waiting for user input")
                decisions = [task.result() for task in decision_tasks]
            finally:
                for task in decision_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*decision_tasks, return_exceptions=True)
            if goal_id:
                authoritative_goal = session_manager.get_goal_state(session_id, goal_id)
                if (
                    not isinstance(authoritative_goal, dict)
                    or str(authoritative_goal.get("status") or "") != "active"
                    or bool(authoritative_goal.get("requested_status"))
                    or str(authoritative_goal.get("current_run_id") or "") != run_id
                    or int(authoritative_goal.get("objective_revision") or 1) != int(context.get("goal_revision") or 1)
                ):
                    raise asyncio.CancelledError("Goal control changed while waiting for user input")
            resolved: list[tuple[str, dict[str, Any], str, dict[str, Any]]] = []
            for (interrupted_type, interrupted_request, interrupt_id), decision in zip(
                pending_interrupts,
                decisions,
                strict=True,
            ):
                request_id = str(interrupted_request.get("id") or "")
                approved = decision.get("type") == "approve" or decision.get("action") in {
                    "confirm",
                    "agree",
                    "modify",
                    "submit",
                    "agent_decide",
                }
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
                if session_id and run_id:
                    try:
                        session_manager.resume_run_from_hitl(
                            session_id,
                            run_id,
                            goal_id=goal_id,
                            goal_revision=context.get("goal_revision"),
                        )
                    except ValueError as exc:
                        raise asyncio.CancelledError(str(exc)) from exc
                    yield self._sse(
                        "run_status_changed",
                        {
                            "session_id": session_id,
                            "query_id": str(context.get("query_id") or ""),
                            "run_id": run_id,
                            "goal_id": str(context.get("goal_id") or ""),
                            "goal_revision": context.get("goal_revision"),
                            "status": "running",
                        },
                    )
                continue

            if len(resolved) == 1:
                interrupted_type, _request, _interrupt_id, decision = resolved[0]
                resume_value = {"decisions": [decision]} if interrupted_type == "permission_request" else decision
                graph_input = Command(resume=resume_value)
                if session_id and run_id:
                    try:
                        session_manager.resume_run_from_hitl(
                            session_id,
                            run_id,
                            goal_id=goal_id,
                            goal_revision=context.get("goal_revision"),
                        )
                    except ValueError as exc:
                        raise asyncio.CancelledError(str(exc)) from exc
                    yield self._sse(
                        "run_status_changed",
                        {
                            "session_id": session_id,
                            "query_id": str(context.get("query_id") or ""),
                            "run_id": run_id,
                            "goal_id": str(context.get("goal_id") or ""),
                            "goal_revision": context.get("goal_revision"),
                            "status": "running",
                        },
                    )
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
        workspace_path: str | Path | None = None,
        source_query_id: str = "",
    ) -> dict[str, Any]:
        artifact = cls._tool_message_artifact(tool_msg)
        context_output = artifact.get(CONTEXT_OUTPUT_ARTIFACT_KEY)
        if not tool_call_id:
            return {}
        extra = getattr(tool_msg, "additional_kwargs", None)
        extra = extra if isinstance(extra, dict) else {}
        source_hash = str(extra.get("puddingclaw_tool_source_hash") or "")
        source_hash_scope = "raw_result" if source_hash else "pointer"
        if not source_hash:
            source_hash = session_manager._tool_context_source_hash(original_output)
        source_query_id = str(extra.get("puddingclaw_query_id") or source_query_id or "")
        raw_ref = session_manager._tool_context_raw_ref(
            session_id,
            tool_call_id,
            original_output,
            source_hash,
            tool_name=str(getattr(tool_msg, "name", "") or ""),
            source_query_id=source_query_id,
            source_hash_scope=source_hash_scope,
            workspace_path=str(Path(workspace_path).expanduser().resolve()) if workspace_path else "",
        )
        if raw_ref.get("kind") == "deepagents_large_tool_result":
            origin_workspace = str(raw_ref.get("workspace_path") or "")
            artifact_name = str(raw_ref.get("artifact_name") or "")
            if origin_workspace and artifact_name:
                artifact = (
                    Path(origin_workspace)
                    / ".puddingclaw"
                    / "large_tool_results"
                    / str(raw_ref.get("session_id") or "")
                    / str(raw_ref.get("source_query_id") or "")
                    / artifact_name
                )
                if artifact.is_file():
                    digest = hashlib.sha256()
                    with artifact.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    source_hash = f"sha256:{digest.hexdigest()}"
                    source_hash_scope = "raw_bytes"
                    raw_ref["source_hash"] = source_hash
                    raw_ref["source_hash_scope"] = source_hash_scope
        fields: dict[str, Any] = {
            "source_hash": source_hash,
            "raw_output_ref": raw_ref,
        }
        if not context_output:
            return fields
        fields.update(
            {
                "context_output": str(context_output),
                "context_compaction": {
                    "status": "ready",
                    "source_hash": source_hash,
                    "policy_version": str(artifact.get(CONTEXT_POLICY_ARTIFACT_KEY) or TOOL_CONTEXT_POLICY_VERSION),
                    "method": str(artifact.get(CONTEXT_METHOD_ARTIFACT_KEY) or "immediate_head_tail"),
                    "context_profile": "detailed",
                    "projection_version": "evidence-projection-v1",
                    "compacted_at": time.time(),
                },
            }
        )
        return fields

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
                    tc["status"] = "interrupted"
                    tc["output_complete"] = False
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
                        "status": "interrupted",
                        "output_complete": False,
                        "completed_at": time.time(),
                    }
                )
            DeepAgentsAgentManager._update_tool_end_in_timeline(segment, tc_id or "", output, True)
            for item in reversed(segment.get("timeline", [])):
                timeline_call = item.get("tool_call") if isinstance(item, dict) else None
                if isinstance(timeline_call, dict) and timeline_call.get("id") == tc_id:
                    timeline_call["status"] = "interrupted"
                    timeline_call["output_complete"] = False
                    break
        pending_tool_starts.clear()

    @staticmethod
    def _strip_runtime_segment_fields(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for segment in segments:
            next_segment = dict(segment)
            next_segment.pop("_current_reasoning", None)
            next_segment.pop("_process_content_published", None)
            next_segment["content"] = DeepAgentsAgentManager._strip_model_call_limit_notice(
                str(next_segment.get("content") or "")
            )
            next_segment["content"] = sanitize_citation_markdown(next_segment["content"])
            cleaned.append(next_segment)
        return cleaned

    @staticmethod
    def _strip_legacy_segment_verification_state(
        segments: list[dict[str, Any]],
    ) -> None:
        """Keep verification lifecycle state out of display-message segments."""

        for segment in segments:
            segment.pop("verification_state", None)

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
        suppress_terminal_content: bool = False,
    ) -> None:
        """Save the visible partial run after client cancellation.

        This is the durable user-facing record. Checkpoints remain an execution
        detail for HITL, not the source of truth for chat continuity.
        """

        self._mark_pending_tools_interrupted(active_segment, pending_tool_starts, pending_tool_output)
        self._strip_legacy_segment_verification_state(segments)
        if suppress_terminal_content:
            for segment in segments:
                # A model turn that proceeds to tools is observable process,
                # not a terminal answer candidate. Preserve that narration on
                # cancellation/error while withholding only tool-less drafts.
                if not segment.get("tool_calls"):
                    segment["content"] = ""
            output_attachments = []
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
            while buffer_start > 0 and combined[buffer_start - 1] in {"\r", "\n"} and newline_chars < 4:
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

        if not goal or goal.get("status") != "active" or goal.get("requested_status") in {"paused", "cancelled"}:
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
            if retry_count < max(1, retry_limit) and total_retry_count < max(1, total_retry_limit):
                return "verification_control_retry"
        # Business acceptance gaps never justify a fresh Run without new
        # information. The current Run already received one bounded repair
        # attempt and a semantic stagnation check; starting another Run would
        # merely reset those breakers and recreate the same loop. A new Run is
        # allowed only after an explicit user continuation/revision, or for a
        # bounded control-plane retry handled above.
        if outcome.get("outcome") == RunOutcome.VERIFICATION_FAILED.value and report_status in {
            VerificationStatus.NEEDS_REVISION.value,
            VerificationStatus.FAILED.value,
            VerificationStatus.MAX_ITERATIONS_REACHED.value,
        }:
            return None
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
        latest_handoff: dict[str, Any] | None = None
        session_id = str(goal.get("session_id") or "")
        run_ids = goal.get("run_ids") if isinstance(goal.get("run_ids"), list) else []
        for prior_run_id in reversed(run_ids):
            prior = session_manager.get_run_state(session_id, str(prior_run_id)) if session_id else None
            candidate = prior.get("handoff_summary") if isinstance(prior, dict) else None
            if (
                isinstance(candidate, dict)
                and candidate.get("goal_id") == goal.get("goal_id")
                and candidate.get("goal_revision") == goal.get("objective_revision")
            ):
                latest_handoff = candidate
                break
        handoff_text = (
            "\n上一 Run 的结构化交接（不是原始工具日志）：\n"
            + json.dumps(latest_handoff, ensure_ascii=False, sort_keys=True, default=str)
            if latest_handoff
            else ""
        )
        return (
            "继续完成当前 Goal，不要把这条内部续跑指令当作新的用户需求。\n"
            + (f"当前 Goal（最新修订）：\n{objective}\n" if objective else "")
            + f"{reason_text}。读取当前 workspace、Todo、Goal 验收缺口和已有产物，"
            "从未完成处继续，避免重复已经完成的工作。"
            + (f"\n当前待补齐项：\n{gap_text}" if gap_text else "")
            + handoff_text
        )

    @staticmethod
    def _goal_inspection_prompt(
        *,
        session_id: str,
        run: RunRecord,
        todos: list[dict[str, Any]],
    ) -> str:
        """Project bounded read-only Goal state without replaying private logs."""

        goal = session_manager.get_goal_state(session_id, str(run.context_goal_id)) if run.context_goal_id else None
        goal = goal if isinstance(goal, dict) else {}
        recent_runs: list[dict[str, Any]] = []
        for prior_run_id in list(goal.get("run_ids") or [])[-5:]:
            prior = session_manager.get_run_state(session_id, str(prior_run_id))
            if not isinstance(prior, dict):
                continue
            handoff = prior.get("handoff_summary")
            recent_runs.append(
                {
                    "run_id": prior.get("run_id"),
                    "status": prior.get("status"),
                    "outcome": prior.get("outcome"),
                    "handoff_summary": handoff if isinstance(handoff, dict) else None,
                }
            )
        projection = {
            "goal_id": goal.get("goal_id"),
            "objective": goal.get("objective"),
            "status": goal.get("status"),
            "revision": goal.get("objective_revision"),
            "gaps": list(goal.get("gaps") or [])[:20],
            "todos": [dict(item) for item in todos[:100] if isinstance(item, dict)],
            "recent_runs": recent_runs,
            "evidence_refs": list(goal.get("evidence_refs") or [])[:100],
        }
        return (
            "\n\n## Goal 回合控制\n"
            "当前用户消息是本轮最高优先级指令；standing Goal 仅为只读背景。\n"
            "本轮已被控制面判定为 Goal 进度/证据查询。只回答当前问题，不得继续执行 Goal，"
            "不得修改 Todo、文件、数据库或外部状态，不得启动子任务。\n"
            "不要声称未提供的工作已经完成；区分 durable evidence、Todo 状态和计划。\n"
            "<goal_read_only_context>\n"
            + json.dumps(projection, ensure_ascii=False, sort_keys=True, default=str)
            + "\n</goal_read_only_context>"
        )

    async def astream(
        self,
        *,
        message: str,
        session_id: str,
        project_id: str | None = None,
        analytics_model_id: str | None = None,
        llm_model_id: str | None = None,
        thinking_level: str | None = None,
        user_id: str = "default_user",
        attachments: list[dict[str, Any]] | None = None,
        skill_hints: list[str] | None = None,
        user_message_already_persisted: bool = False,
        goal_mode: bool = False,
        goal_id: str | None = None,
        context_goal_id: str | None = None,
        goal_control_action: str | None = None,
    ) -> AsyncGenerator[dict[str, str], None]:
        """Stream one user request and autonomously advance recoverable Goal Runs."""

        existing_goal = (
            session_manager.get_goal_state(session_id, context_goal_id)
            if context_goal_id
            else session_manager.get_goal_state(session_id, goal_id)
            if goal_id
            else session_manager.get_active_goal_state(session_id)
            if goal_mode
            else None
        )
        current_objective = message
        current_message = message
        current_goal_id: str | None = None
        run_context_goal_id: str | None = None
        context_goal_revision: int | None = None
        turn_decision: GoalTurnDecision | None = None
        run_kind = RunKind.STANDALONE
        if isinstance(existing_goal, dict):
            if goal_control_action == "start":
                if str(existing_goal.get("status") or "") != GoalStatus.ACTIVE.value:
                    raise ValueError("Goal must be active before it can be started")
                if existing_goal.get("requested_status"):
                    raise ValueError("Goal has a pending control request")
                turn_decision = GoalTurnDecision(
                    intent=GoalTurnIntent.CONTINUE_GOAL,
                    target_goal_id=str(existing_goal.get("goal_id") or ""),
                    confidence=1.0,
                    reason="explicit_goal_start_control",
                    classifier="product_control",
                )
            else:
                turn_decision = await self._classify_goal_turn(
                    session_id=session_id,
                    message=message,
                    goal=existing_goal,
                )
            if turn_decision.classifier == "fallback":
                emit_harness_metric(
                    logger,
                    "goal_turn_router_fallback_total",
                    session_id=session_id,
                    goal_id=str(existing_goal.get("goal_id") or ""),
                    reason=turn_decision.reason,
                )
            emit_harness_metric(
                logger,
                "goal_turn_route_total",
                session_id=session_id,
                goal_id=str(existing_goal.get("goal_id") or ""),
                intent=turn_decision.intent.value,
                classifier=turn_decision.classifier,
                reason=turn_decision.reason,
            )
            yield self._sse(
                "goal_turn_routed",
                {
                    "session_id": session_id,
                    "intent": turn_decision.intent.value,
                    "target_goal_id": turn_decision.target_goal_id,
                    "confidence": turn_decision.confidence,
                    "reason": turn_decision.reason,
                    "classifier": turn_decision.classifier,
                },
            )
            if turn_decision.intent == GoalTurnIntent.CONTROL_GOAL:
                action = str(turn_decision.control_action or "")
                method = {
                    "pause": self._run_coordinator.goals.pause,
                    "resume": self._run_coordinator.goals.resume,
                    "cancel": self._run_coordinator.goals.cancel,
                }[action]
                controlled = await asyncio.to_thread(
                    method,
                    session_id,
                    str(existing_goal.get("goal_id") or ""),
                )
                if action in {"pause", "cancel"} and controlled.requested_status is not None:
                    self.cancel_active_goal_run(
                        session_id,
                        str(existing_goal.get("goal_id") or ""),
                    )
                content = {
                    "pause": "当前 Goal 已请求暂停。",
                    "resume": "当前 Goal 已恢复，可以继续执行。",
                    "cancel": "当前 Goal 已请求取消。",
                }[action]
                if not user_message_already_persisted:
                    session_manager.save_message(session_id, "user", message, attachments=attachments)
                control_query_id = f"query-{uuid.uuid4().hex[:12]}"
                self._persist_assistant_snapshot(
                    session_id=session_id,
                    query_id=control_query_id,
                    segments=[{"content": content, "tool_calls": [], "timeline": []}],
                    accumulated_reasoning="",
                    turn_sources=[],
                    status="completed",
                )
                yield self._sse(
                    "goal_status_changed",
                    {"session_id": session_id, "goal": controlled.model_dump(mode="json")},
                )
                yield self._sse("token", {"content": content})
                yield self._sse("done", {"content": content, "session_id": session_id})
                return
            if turn_decision.intent == GoalTurnIntent.CLARIFY:
                content = "我不确定你是想查看当前 Goal 的进度，还是继续执行它。请明确说“总结进度”或“继续执行”。"
                if not user_message_already_persisted:
                    session_manager.save_message(session_id, "user", message, attachments=attachments)
                clarify_query_id = f"query-{uuid.uuid4().hex[:12]}"
                self._persist_assistant_snapshot(
                    session_id=session_id,
                    query_id=clarify_query_id,
                    segments=[{"content": content, "tool_calls": [], "timeline": []}],
                    accumulated_reasoning="",
                    turn_sources=[],
                    status="completed",
                )
                yield self._sse("token", {"content": content})
                yield self._sse("done", {"content": content, "session_id": session_id})
                return
            if turn_decision.intent == GoalTurnIntent.REVISE_GOAL:
                revised = await asyncio.to_thread(
                    self._run_coordinator.goals.update_objective,
                    session_id,
                    str(existing_goal.get("goal_id") or ""),
                    objective=str(turn_decision.revised_objective or ""),
                    expected_revision=int(existing_goal.get("objective_revision") or 1),
                )
                existing_goal = revised.model_dump(mode="json")
                current_objective = revised.objective
                current_goal_id = revised.goal_id
                goal_mode = True
                run_kind = RunKind.GOAL_EXECUTION
            elif turn_decision.intent == GoalTurnIntent.CONTINUE_GOAL:
                current_objective = str(existing_goal.get("objective") or message)
                current_goal_id = str(existing_goal.get("goal_id") or "") or None
                goal_mode = True
                run_kind = RunKind.GOAL_EXECUTION
            elif turn_decision.intent == GoalTurnIntent.INSPECT_GOAL:
                current_objective = message
                goal_mode = False
                run_context_goal_id = str(existing_goal.get("goal_id") or "") or None
                context_goal_revision = int(existing_goal.get("objective_revision") or 1)
                run_kind = RunKind.GOAL_INSPECTION
            else:
                current_objective = message
                goal_mode = False
                run_kind = RunKind.STANDALONE
        elif goal_mode:
            # Explicit Goal Mode without a standing Goal creates the first
            # execution Run. This is product state, not a semantic guess.
            current_goal_id = goal_id
            run_kind = RunKind.GOAL_EXECUTION
            turn_decision = GoalTurnDecision(
                intent=GoalTurnIntent.CONTINUE_GOAL,
                target_goal_id=goal_id,
                confidence=1.0,
                reason="explicit_goal_mode_creation",
                classifier="product_state",
            )
        internal_continuation = False
        while True:
            if internal_continuation and current_goal_id:
                latest_goal = session_manager.get_goal_state(session_id, current_goal_id)
                if isinstance(latest_goal, dict):
                    current_objective = str(latest_goal.get("objective") or current_objective)
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
                llm_model_id=llm_model_id,
                thinking_level=thinking_level,
                user_id=user_id,
                attachments=[] if internal_continuation else attachments,
                skill_hints=[] if internal_continuation else skill_hints,
                user_message_already_persisted=(user_message_already_persisted or internal_continuation),
                goal_mode=goal_mode,
                goal_id=current_goal_id,
                run_objective=current_objective,
                internal_continuation=internal_continuation,
                run_kind=run_kind,
                context_goal_id=run_context_goal_id,
                context_goal_revision=context_goal_revision,
                goal_turn_decision=turn_decision,
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
            continuation_reason = (
                self._goal_auto_continue_reason(
                    outcome=outcome_payload,
                    verification=verification_payload,
                    goal=goal_payload,
                )
                if run_kind == RunKind.GOAL_EXECUTION
                else None
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
                        # Auto-continuation is already the single call to
                        # action on this message; drop any terminal
                        # verification guidance that would contradict it.
                        clear_verification_summary=continuation_reason is not None,
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
                        f"第 {next_round}/{max_rounds} 轮将自动继续。" if max_rounds > 0 else "Goal 将自动进入下一轮。"
                    ),
                },
            )
            current_goal_id = str(goal_payload["goal_id"])
            goal_mode = True
            internal_continuation = True
            run_kind = RunKind.GOAL_EXECUTION
            run_context_goal_id = None
            context_goal_revision = None
            turn_decision = GoalTurnDecision(
                intent=GoalTurnIntent.CONTINUE_GOAL,
                target_goal_id=current_goal_id,
                confidence=1.0,
                reason=continuation_reason,
                classifier="control_plane",
            )
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
        llm_model_id: str | None = None,
        thinking_level: str | None = None,
        user_id: str = "default_user",
        attachments: list[dict[str, Any]] | None = None,
        skill_hints: list[str] | None = None,
        user_message_already_persisted: bool = False,
        goal_mode: bool = False,
        goal_id: str | None = None,
        run_objective: str | None = None,
        internal_continuation: bool = False,
        run_kind: RunKind = RunKind.STANDALONE,
        context_goal_id: str | None = None,
        context_goal_revision: int | None = None,
        goal_turn_decision: GoalTurnDecision | None = None,
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
        terminal_authority_committed = False
        completion_accepted = False
        user_message_persisted = user_message_already_persisted
        title_task: asyncio.Task[str | None] | None = None
        title_event_emitted = False
        task_router_task: asyncio.Task[dict[str, Any]] | None = None
        task_router_event_emitted = False
        task_router_trace_recorded = False
        task_router_cancel_reason: str | None = None
        router_skipped_trivial = False
        checkpoint_thread_id = f"{session_id}:{query_id}"
        try:
            effective_llm = config.get_fallback_llm_config(
                model_id_override=llm_model_id,
                thinking_level=thinking_level,
            )
            thinking_enabled = bool(effective_llm.get("thinking_enabled", False))
            logger.info(
                "Agent stream model_id=%s thinking_level=%s for session=%s",
                effective_llm.get("model_id"),
                effective_llm.get("thinking_level"),
                session_id,
            )

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
            completion_policy = "rubric" if rubric_config.get("enabled", False) else "standard"
            # Policy is frozen when a Goal is created; a later settings change
            # applies to new Goals without changing an active Goal mid-run.
            if goal_id:
                persisted_goal = session_manager.get_goal_state(session_id, goal_id)
                if isinstance(persisted_goal, dict):
                    completion_policy = str(persisted_goal.get("completion_policy") or "standard")
            rubric_model_name = str(rubric_config.get("model") or "").strip()
            if not rubric_model_name:
                rubric_model_name = str(
                    config.get_fallback_llm_config(
                        thinking_enabled_override=False,
                    ).get("model")
                    or ""
                ).strip()
            goal_max_rounds = goals_config.get("max_rounds", 8)
            if not isinstance(goal_max_rounds, int) or isinstance(goal_max_rounds, bool) or goal_max_rounds <= 0:
                goal_max_rounds = 8
            if goal_mode and not goals_config.get("enabled", True):
                raise ValueError("Goal Mode is disabled by Harness Settings.")
            yield self._sse(
                "task_preflight_started",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "label": "正在准备权限、附件与任务上下文",
                },
            )
            skill_catalog = discover_skill_catalog(self._base_dir / "skills")
            reused_task_profile = self._reusable_task_profile(
                session_id=session_id,
                message=message,
                analytics_model_id=analytics_model_id,
                internal_continuation=internal_continuation,
            )
            task_profile = reused_task_profile or self._build_preflight_task_profile(
                objective=run_objective or message,
                analytics_model_id=analytics_model_id,
                skill_catalog=skill_catalog,
                explicit_skill_hints=skill_hints,
            )
            explicit_skills = [
                candidate.skill_id
                for candidate in task_profile.skill_candidates
                if candidate.skill_id and candidate.explicit
            ]
            yield self._sse(
                "task_preflight_completed",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "label": "任务上下文已准备",
                    "execution_route": task_profile.execution_route,
                    "explicit_skill_ids": explicit_skills,
                    "missing_skill_ids": list(task_profile.missing_explicit_skill_ids),
                },
            )
            if task_profile.missing_explicit_skill_ids:
                yield self._sse(
                    "skill_install_required",
                    {
                        "session_id": session_id,
                        "query_id": query_id,
                        "skill_ids": list(task_profile.missing_explicit_skill_ids),
                        "label": "指定的 Skill 尚未安装，Agent 将引导安装或选择通用执行",
                    },
                )
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
                completion_policy=completion_policy,
                goal_max_rounds=goal_max_rounds,
                custom_rubric_rules=(
                    list(rubric_config.get("custom_rules") or [])
                    if rubric_config.get("custom_rules_enabled", False)
                    else []
                ),
                task_profile=task_profile,
                run_kind=run_kind,
                context_goal_id=context_goal_id,
                context_goal_revision=context_goal_revision,
                goal_turn_intent=(goal_turn_decision.intent if goal_turn_decision else None),
                goal_turn_confidence=(goal_turn_decision.confidence if goal_turn_decision else None),
                goal_turn_classifier=(goal_turn_decision.classifier if goal_turn_decision else None),
            )

            async def route_task_in_background() -> dict[str, Any]:
                started_at = time.monotonic()
                try:
                    router_kwargs: dict[str, Any] = {
                        "objective": run_record.objective,
                        "analytics_model_id": analytics_model_id,
                        "model_override": rubric_model_name or None,
                        "skill_catalog": skill_catalog,
                    }
                    if skill_hints is not None:
                        router_kwargs["explicit_skill_hints"] = skill_hints
                    router_kwargs["independent_skill_review"] = bool(explicit_skills)
                    semantic_profile = await self._classify_task_profile(**router_kwargs)
                    saved_run, applied = await asyncio.to_thread(
                        session_manager.enhance_run_task_profile,
                        session_id,
                        run_record.run_id,
                        semantic_profile.model_dump(mode="json"),
                    )
                    saved_profile = saved_run.get("task_profile")
                    return {
                        "status": "completed",
                        "applied": applied,
                        "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                        "task_profile": (saved_profile if isinstance(saved_profile, dict) else {}),
                    }
                except TimeoutError:
                    logger.warning(
                        "Task Router timed out after %.1fs for session=%s run=%s; "
                        "continuing with deterministic baseline",
                        _TASK_ROUTER_TIMEOUT_SECONDS,
                        session_id,
                        run_record.run_id,
                    )
                    return {
                        "status": "timed_out",
                        "applied": False,
                        "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                        "task_profile": run_record.task_profile.model_dump(mode="json"),
                    }
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Task Router failed for session=%s run=%s: %s; continuing with deterministic baseline",
                        session_id,
                        run_record.run_id,
                        exc,
                        exc_info=True,
                    )
                    return {
                        "status": "failed",
                        "applied": False,
                        "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                        "error": str(exc),
                        "task_profile": run_record.task_profile.model_dump(mode="json"),
                    }

            router_skipped_trivial = reused_task_profile is None and not _should_start_semantic_task_router(
                run_record.objective
            )
            if reused_task_profile is None and not router_skipped_trivial:
                task_router_task = asyncio.create_task(route_task_in_background())
                # Give a cache-fast Router one scheduling turn. Slow provider
                # work remains cancellable before the user-visible model call.
                await asyncio.sleep(0)

            def task_router_completion_event() -> dict[str, str] | None:
                if task_router_task is None or not task_router_task.done():
                    return None
                if task_router_task.cancelled():
                    result: dict[str, Any] = {
                        "status": "cancelled",
                        "applied": False,
                        "task_profile": run_record.task_profile.model_dump(mode="json"),
                    }
                else:
                    try:
                        result = task_router_task.result()
                    except Exception as exc:
                        result = {
                            "status": "failed",
                            "applied": False,
                            "error": str(exc),
                            "task_profile": run_record.task_profile.model_dump(mode="json"),
                        }
                profile_payload = result.get("task_profile")
                profile = profile_payload if isinstance(profile_payload, dict) else {}
                candidates = [
                    str(item.get("skill_id") or "")
                    for item in profile.get("skill_candidates") or []
                    if isinstance(item, dict) and str(item.get("skill_id") or "")
                ]
                status = str(result.get("status") or "failed")
                label = (
                    "任务理解与 Skill 候选已补充"
                    if status == "completed" and result.get("applied")
                    else "任务路由无需补充，继续使用当前任务画像"
                    if status == "completed"
                    else "任务路由超时，已使用确定性任务画像继续执行"
                    if status == "timed_out"
                    else "任务路由不可用，已使用确定性任务画像继续执行"
                )
                return self._sse(
                    "task_routing_completed",
                    {
                        "session_id": session_id,
                        "query_id": query_id,
                        "run_id": run_record.run_id,
                        "label": label,
                        "status": status,
                        "applied": bool(result.get("applied")),
                        "blocking": bool(explicit_skills),
                        "duration_ms": result.get("duration_ms"),
                        "execution_route": profile.get("execution_route", "native"),
                        "skill_candidates": candidates,
                    },
                )

            def record_task_router_trace(
                collector: TraceCollector,
                *,
                settle_pending: bool = False,
            ) -> None:
                nonlocal task_router_trace_recorded
                if task_router_trace_recorded or task_router_task is None:
                    return
                if not task_router_task.done():
                    if not settle_pending:
                        return
                    task_router_task.cancel()
                    result: dict[str, Any] = {
                        "status": "cancelled",
                        "applied": False,
                        "error": "run_finished_before_router",
                    }
                elif task_router_task.cancelled():
                    result: dict[str, Any] = {
                        "status": "cancelled",
                        "applied": False,
                        "error": task_router_cancel_reason,
                    }
                else:
                    try:
                        result = task_router_task.result()
                    except Exception as exc:
                        result = {
                            "status": "failed",
                            "applied": False,
                            "error": str(exc),
                        }
                collector.add_custom_span(
                    "task_router",
                    {
                        "status": result.get("status"),
                        "applied": bool(result.get("applied")),
                        "duration_ms": result.get("duration_ms"),
                        "timeout_seconds": _TASK_ROUTER_TIMEOUT_SECONDS,
                        "error": result.get("error"),
                    },
                    metadata={
                        "role": "task_classifier",
                        "blocking": bool(explicit_skills),
                        "fallback": "deterministic_task_profile",
                        "harness": {
                            "mechanism": "task_routing",
                            "pillars": [
                                {"name": "context_engineering", "role": "primary"},
                                {"name": "architectural_constraints", "role": "supporting"},
                            ],
                        },
                    },
                )
                task_router_trace_recorded = True

            yield self._sse(
                "task_routing_started",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run_id": run_record.run_id,
                    "label": (
                        "已复用上一轮任务理解"
                        if reused_task_profile is not None
                        else "普通对话无需语义路由"
                        if router_skipped_trivial
                        else "Agent 正在确认显式 Skill 并补充复合任务所需 Skill"
                        if explicit_skills
                        else "Agent 正在后台理解任务并匹配 Skill"
                    ),
                    "blocking": bool(explicit_skills),
                    "timeout_seconds": _TASK_ROUTER_TIMEOUT_SECONDS,
                },
            )
            if reused_task_profile is not None:
                task_router_event_emitted = True
                yield self._sse(
                    "task_routing_completed",
                    {
                        "session_id": session_id,
                        "query_id": query_id,
                        "run_id": run_record.run_id,
                        "label": "已复用上一轮任务理解，无需重新路由",
                        "status": "reused",
                        "applied": True,
                        "blocking": False,
                        "duration_ms": 0,
                        "execution_route": reused_task_profile.execution_route,
                        "skill_candidates": TaskProfileClassifier.skill_ids(reused_task_profile),
                    },
                )
            elif router_skipped_trivial:
                task_router_event_emitted = True
                yield self._sse(
                    "task_routing_completed",
                    {
                        "session_id": session_id,
                        "query_id": query_id,
                        "run_id": run_record.run_id,
                        "label": "普通对话已直接交给 Agent",
                        "status": "not_required",
                        "applied": False,
                        "blocking": False,
                        "duration_ms": 0,
                        "execution_route": "native",
                        "skill_candidates": [],
                    },
                )
            if goal_record is not None:
                current_task = asyncio.current_task()
                if current_task is not None:
                    self._active_goal_tasks[(session_id, goal_record.goal_id)] = current_task
            todo_goal_id = run_record.goal_id or run_record.context_goal_id
            todo_goal_revision = run_record.goal_revision if run_record.goal_id else run_record.context_goal_revision
            run_started_snapshot = session_manager.get_todo_snapshot(
                session_id,
                goal_id=todo_goal_id,
                goal_revision=todo_goal_revision,
                run_id=(run_record.run_id if not todo_goal_id else None),
            )
            run_started_todos = list(run_started_snapshot.get("todos") or [])
            run_started_todo_authority = dict(run_started_snapshot.get("authority") or {})
            yield self._sse(
                "run_started",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run": run_record.model_dump(mode="json"),
                    "todos": run_started_todos,
                    "todos_authority": run_started_todo_authority,
                    "todo_ledger_revision": int(run_started_snapshot.get("ledger_revision") or 0),
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
            history = session_manager.load_session_for_agent(
                session_id,
                current_run_id=run_record.run_id,
            )
            # Cross-Run model context is rebuilt as protocol-valid historical
            # evidence. It remains context only and carries no current authority.
            history_for_build = history
            if user_message_persisted and raw_history:
                persisted_display = self._display_message_with_attachments(message, attachments)
                last_message = raw_history[-1]
                if last_message.get("role") == "user" and last_message.get("content") == persisted_display:
                    # The API persists the user turn before opening SSE so an
                    # immediate disconnect cannot lose it. _build_messages adds
                    # the current turn itself, therefore exclude that persisted
                    # copy here or the model receives the prompt twice.
                    history_for_build = history[:-1]
            saved_agent_context = session_manager.get_agent_context_messages(
                session_id,
                run_id=run_record.run_id,
            )
            using_saved_agent_context = False
            if saved_agent_context:
                try:
                    restored_messages = [
                        restored
                        for restored in messages_from_dict(saved_agent_context)
                        if not _is_internal_control_message(restored)
                    ]
                    messages, protocol_report = repair_tool_message_protocol(restored_messages)
                    if any(protocol_report.values()):
                        logger.warning(
                            "Repaired saved Agent tool protocol for session=%s: %s",
                            session_id,
                            protocol_report,
                        )
                    context_attachments = list(attachments or [])
                    if goal_record is not None:
                        known_attachment_ids = {
                            str(item.get("id") or "") for item in context_attachments if isinstance(item, dict)
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
                goal_id=todo_goal_id,
                goal_revision=todo_goal_revision,
                run_id=(run_record.run_id if not todo_goal_id else None),
            )
            if not persisted_todos and run_record.goal_id is None:
                persisted_todos = session_manager.inherit_unfinished_todos_for_run(
                    session_id,
                    run_record.run_id,
                    continuation_requested=(internal_continuation or _is_explicit_continuation(message)),
                )

            model = ModelClientChatModel(
                role="agent",
                streaming=True,
                model_id_override=llm_model_id,
                thinking_level=thinking_level,
            )
            rubric_model = ModelClientChatModel(
                role="rubric",
                streaming=False,
                thinking_enabled=False,
                model_override=rubric_model_name or None,
            )
            permission_reviewer = ModelPermissionReviewer(
                ModelClientChatModel(
                    role="permission_reviewer",
                    temperature=0,
                    streaming=False,
                    thinking_enabled=False,
                    model_override=rubric_model_name or None,
                )
            )
            agent_skills = ["/skills/"]
            skill_toolsets = discover_skill_toolsets(self._base_dir / "skills")
            agent_backend = self._build_backend(
                workspace_path,
                session_id=session_id,
                run_id=run_record.run_id,
                query_id=query_id,
                goal_id=str(run_record.goal_id or ""),
                goal_revision=run_record.goal_revision,
            )
            agent_tools = self._build_tools(
                workspace_path,
                session_id=session_id,
                query_id=query_id,
                run_id=run_record.run_id,
                current_message=message,
                current_attachments=attachments,
                goal_id=str(run_record.goal_id or ""),
                goal_revision=run_record.goal_revision,
                execution_backend=getattr(
                    agent_backend,
                    "execution_backend",
                    None,
                ),
            )
            mcp_config = config.load_config().get("mcp", {})
            from mcp_clients.servers import effective_mcp_server_names

            enabled_mcp = effective_mcp_server_names(
                mcp_config.get("enabled", []),
                auto_enable_gbrain=bool(mcp_config.get("auto_enable_gbrain", False)),
            )
            if enabled_mcp:
                try:
                    from mcp_clients import load_filtered_mcp_tools

                    agent_tools.extend(await load_filtered_mcp_tools(enabled_mcp))
                except Exception:
                    logger.warning(
                        "Failed to load filtered MCP tools for DeepAgents; continuing with local tools",
                        exc_info=True,
                    )
            if not run_record.executes_goal:
                agent_tools = [tool for tool in agent_tools if str(getattr(tool, "name", "")) != "update_goal"]
            backend_mode = str(getattr(agent_backend, "execution_mode", "restricted_host"))
            workspace_id = "sha256:" + hashlib.sha256(str(workspace_path.resolve()).encode("utf-8")).hexdigest()
            self._run_coordinator.bind_execution_snapshot(
                run_record,
                {
                    "backend_mode": backend_mode,
                    "backend_id": str(getattr(agent_backend, "execution_backend_id", "")),
                    "workspace_id": workspace_id,
                    "scratch_host_path": str(getattr(agent_backend, "execution_scratch_host_path", "")),
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
                permission_reviewer=permission_reviewer,
            )
            main_summarization = _build_deepagents_summarization(model, agent_backend)
            if main_summarization is not None:
                agent_middlewares.append(main_summarization)
            # Keep this last so every model request is protocol-valid even if
            # an upstream compaction/summarization boundary lost a ToolMessage.
            context_trigger_tokens = int(config.get_deepagents_summarization_config().get("trigger_tokens", 160000))
            agent_middlewares.append(
                ToolProtocolIntegrityMiddleware(
                    context_trigger_tokens=context_trigger_tokens,
                )
            )
            checkpointer = await self._build_checkpointer()
            runtime_inventory = self._runtime_inventory(
                tools=agent_tools,
                skills=agent_skills,
                middleware=agent_middlewares,
                workspace_path=workspace_path,
                checkpointer=self._checkpointer_info,
                execution_backend=agent_backend,
            )
            analytics_model_prompt, analytics_model_payload = self._analytics_model_context(
                analytics_model_id,
                query=run_record.objective,
            )
            if analytics_model_payload:
                runtime_inventory["analytics_model"] = analytics_model_payload
            traced_middlewares = wrap_middlewares_for_trace(agent_middlewares)
            logger.info("Building DeepAgents agent for session=%s project=%s", session_id, project_id)
            subagent_tools = [
                tool
                for tool in agent_tools
                if str(getattr(tool, "name", "")) not in {"update_goal", "request_user_input"}
            ]

            def build_subagent_middlewares() -> list[Any]:
                middlewares: list[Any] = [
                    SubagentProgressMiddleware(),
                    RunScopeMiddleware(),
                    AnalysisTemplateMiddleware(base_dir=self._base_dir),
                    SemanticAssetsMiddleware(base_dir=self._base_dir),
                    ExternalFilePermissionMiddleware(),
                    WorkspacePathRouterMiddleware(agent_backend),
                    VerificationActivationMiddleware(),
                    VersionedPatchMiddleware(agent_backend, compact_model_surface=True),
                    SkillIntentRouterMiddleware(),
                    ToolsetMiddleware(
                        skills_dir=self._base_dir / "skills",
                        toolsets_by_skill=skill_toolsets,
                    ),
                    ToolGuideMiddleware(base_dir=self._base_dir),
                    ToolExecutionPipeline(
                        known_tools={
                            str(getattr(tool, "name", "")) for tool in subagent_tools if getattr(tool, "name", "")
                        },
                        backend_mode=backend_mode,
                        permission_context=permission_context,
                        base_dir=self._base_dir,
                        reviewer=permission_reviewer,
                        workspace_backend=getattr(agent_backend, "execution_backend", agent_backend),
                    ),
                    ObservableModelCallLimitMiddleware(
                        run_limit=12,
                        thread_limit=None,
                        exit_behavior="end",
                    ),
                    ToolCallLimitMiddleware(
                        run_limit=30,
                        thread_limit=None,
                        exit_behavior="continue",
                    ),
                ]
                summarization = _build_deepagents_summarization(model, agent_backend)
                if summarization is not None:
                    middlewares.append(summarization)
                middlewares.append(
                    ToolProtocolIntegrityMiddleware(
                        context_trigger_tokens=context_trigger_tokens,
                        emit_context_usage=False,
                    )
                )
                return middlewares

            subagents = _build_subagents(
                subagent_tools,
                agent_skills,
                middleware_factory=build_subagent_middlewares,
                context_prompt=analytics_model_prompt,
            )
            system_prompt = build_deepagents_system_prompt(self._base_dir, workspace_path)
            dependency_prompt = dependency_plan_prompt(getattr(agent_backend, "execution_dependency_plan", None))
            if dependency_prompt:
                system_prompt += f"\n\n## Current Run Delta\n\n{dependency_prompt}"
            if analytics_model_prompt:
                system_prompt += f"\n\n## Versioned Analytics / Semantics\n{analytics_model_prompt}"
            system_prompt += f"\n\n## Current Run Delta\n{_run_artifact_continuity_prompt(run_record)}"
            if run_record.executes_goal:
                system_prompt += (
                    "\n\n## Current Run Delta\n\n## Goal 完成协议\n"
                    "Goal 不会因自然停止而完成。完成前请从原始 Goal 和用户明确要求推导全部必需结果，"
                    "核对真实状态，并执行与改动风险相称且实际需要的检查；只能报告实际执行过的检查。"
                    "只有全部必需项完成且没有已知遗留工作时，才调用 update_goal(completed=true)。"
                    "调用成功后只生成最终回复；若还要调用任何工具或修改产物，必须先继续工作并在最后重新提交完成声明。"
                )
            if run_record.run_kind == RunKind.GOAL_INSPECTION:
                system_prompt += "\n\n## Current Run Delta\n" + self._goal_inspection_prompt(
                    session_id=session_id,
                    run=run_record,
                    todos=persisted_todos,
                )
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
            # A slash hint must improve routing without becoming its only
            # source. For an explicit hint, wait for the already bounded
            # semantic Router before the first Agent decision so compound
            # requirements (for example design + database analysis) are both
            # visible. Requests without an explicit hint remain non-blocking.
            if explicit_skills and task_router_task is not None and not task_router_task.done():
                await task_router_task
            if explicit_skills and task_router_task is not None and task_router_task.done():
                self._run_coordinator._refresh_runtime_fields(run_record)
            self._run_coordinator.transition(run_record, RunStatus.RUNNING)
            yield self._sse(
                "run_status_changed",
                {
                    "session_id": session_id,
                    "query_id": query_id,
                    "run_id": run_record.run_id,
                    "run_kind": run_record.run_kind.value,
                    "goal_id": run_record.goal_id or "",
                    "context_goal_id": run_record.context_goal_id or "",
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
            record_task_router_trace(trace_collector)
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
            verification_retry_pending_segment = False
            last_goal_control_poll = 0.0
            model_call_limit_events: list[dict[str, Any]] = []
            last_snapshot_at = 0.0
            last_snapshot_signature = ""
            last_context_usage = -1
            last_persisted_context_usage = session_manager.get_agent_context_usage(session_id)
            received_context_usage_event = False
            last_fallback_context_usage = -1
            persisted_agent_context_fingerprint = _agent_context_fingerprint(saved_agent_context)
            # A natural stop without a declaration is still allowed to publish
            # this Run's progress response. The final transaction—not stream
            # buffering—is the authority for Goal completion.
            defer_final_publication = False

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
                        "output_attachments": [str(item.get("id") or "") for item in published_attachments],
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
                snapshot_segments = segments
                if defer_final_publication and status == "running":
                    snapshot_segments = [
                        {
                            **segment,
                            "content": (str(segment.get("content") or "") if segment.get("tool_calls") else ""),
                        }
                        for segment in segments
                    ]
                if self._persist_assistant_snapshot(
                    session_id=session_id,
                    query_id=query_id,
                    segments=snapshot_segments,
                    accumulated_reasoning=accumulated_reasoning,
                    turn_sources=turn_sources,
                    output_attachments=(
                        [] if defer_final_publication and status == "running" else published_attachments
                    ),
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

            if task_router_task is not None and task_router_task.done() and not task_router_event_emitted:
                self._run_coordinator._refresh_runtime_fields(run_record)
                router_event = task_router_completion_event()
                if router_event is not None:
                    task_router_event_emitted = True
                    yield router_event

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
                    "project_id": project_id,
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
                if task_router_task is not None and task_router_task.done() and not task_router_event_emitted:
                    self._run_coordinator._refresh_runtime_fields(run_record)
                    record_task_router_trace(trace_collector)
                    router_event = task_router_completion_event()
                    if router_event is not None:
                        task_router_event_emitted = True
                        yield router_event
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
                    if isinstance(authoritative_goal, dict) and authoritative_goal.get("requested_status") in {
                        GoalStatus.PAUSED.value,
                        GoalStatus.CANCELLED.value,
                    }:
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
                    if verification_retry_pending_segment and is_model_node and (text or reasoning_text):
                        # Completion-gate and rubric retries jump straight back
                        # to the model without passing through the tools node.
                        # Start a new process segment so revision text cannot
                        # be glued to the rejected terminal text.
                        self._finalize_reasoning_timeline(active_segment)
                        active_segment = new_segment()
                        segments.append(active_segment)
                        verification_retry_pending_segment = False
                        if active_llm_span is not None:
                            trace_collector.finish_llm_span(output=emitted_text)
                            active_llm_span = None
                        yield self._sse("segment_break", {"reason": "verification_retry"})
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
                        if not defer_final_publication:
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
                                tool_extra = getattr(tool_msg, "additional_kwargs", None)
                                tool_extra = tool_extra if isinstance(tool_extra, dict) else {}
                                historical_stream_item = bool(
                                    tool_extra.get("puddingclaw_historical")
                                    or (
                                        tool_extra.get("puddingclaw_query_id")
                                        and str(tool_extra.get("puddingclaw_query_id")) != query_id
                                    )
                                )
                                if historical_stream_item:
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
                                    if not defer_final_publication:
                                        yield self._sse(
                                            "attachment_published",
                                            {
                                                "tool_call_id": tc_id,
                                                "query_id": query_id,
                                                "attachment": published_attachment,
                                            },
                                        )
                                is_error = self._is_tool_error(tool_msg, raw_output)
                                control_plane = tool_extra.get("puddingclaw_control_plane")
                                if (
                                    isinstance(control_plane, dict)
                                    and control_plane.get("type") == "skill_cache_loaded"
                                ):
                                    # The requested business tool did not run,
                                    # but the control-plane recovery succeeded.
                                    # Keep the ToolMessage error semantics for
                                    # the model/verifier while presenting the
                                    # internal lazy-load as a completed context
                                    # operation instead of a user-facing tool
                                    # failure.
                                    is_error = False
                                self._update_tool_end_in_timeline(active_segment, tc_id or "", raw_output, is_error)
                                pending_tool_starts.pop(tc_id, None)

                                matched = False
                                context_fields = self._tool_message_context_fields(
                                    tool_msg,
                                    session_id=session_id,
                                    tool_call_id=tc_id,
                                    original_output=original_output,
                                    workspace_path=workspace_path,
                                    source_query_id=query_id,
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
                                # Control-plane tool results (Skill plans,
                                # browser authorization requests) must be
                                # durable before the frontend can act on them.
                                # Persist all tool ends in this order so a
                                # crash after SSE publication cannot create a
                                # card that disappears on history reload.
                                persist_assistant_snapshot(force=True)
                                yield self._sse(
                                    "tool_end",
                                    {
                                        "tool": tool_name,
                                        "id": tc_id,
                                        "output": project_live_tool_output(
                                            tool_name=tool_name,
                                            raw_output=raw_output,
                                            fallback_output=raw_output[:4000],
                                        ),
                                        "output_full_length": len(raw_output),
                                        "summary_source": None,
                                        "is_error": is_error,
                                        "sources": sources,
                                    },
                                )
                                tools_just_finished = True
                        else:
                            for agent_msg in node_messages:
                                agent_extra = getattr(agent_msg, "additional_kwargs", None)
                                agent_extra = agent_extra if isinstance(agent_extra, dict) else {}
                                historical_agent_message = bool(
                                    agent_extra.get("puddingclaw_historical")
                                    or (
                                        agent_extra.get("puddingclaw_source_query_id")
                                        and str(agent_extra.get("puddingclaw_source_query_id")) != query_id
                                    )
                                )
                                if historical_agent_message:
                                    continue
                                tool_calls = getattr(agent_msg, "tool_calls", None) or []
                                for tool_call in tool_calls:
                                    tc_id = self._tool_call_id(tool_call)
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
                                    if (
                                        defer_final_publication
                                        and str(active_segment.get("content") or "").strip()
                                        and not active_segment.get("_process_content_published")
                                    ):
                                        # Tool calls prove this model turn is
                                        # process narration rather than a
                                        # terminal answer. Reveal the buffered
                                        # text as soon as that boundary is
                                        # authoritative.
                                        active_segment["_process_content_published"] = True
                                        yield self._sse(
                                            "segment_content_replaced",
                                            {"content": str(active_segment.get("content") or "")},
                                        )
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
                        lifecycle_label = ""
                        lifecycle_detail = ""
                        lifecycle_display_status = ""
                        lifecycle_status = str(payload.get("status") or payload.get("result") or "")
                        if event_type == "rubric_evaluation_start":
                            lifecycle_label = "正在核对完成质量"
                            lifecycle_status = "running"
                            lifecycle_display_status = "running"
                        elif event_type == "deterministic_checks_completed":
                            will_continue = bool(payload.get("will_continue"))
                            if lifecycle_status == "satisfied":
                                lifecycle_label = "完成条件检查通过"
                                lifecycle_display_status = "satisfied"
                            elif will_continue:
                                lifecycle_label = "发现完成条件缺口，正在自动继续修复"
                                lifecycle_detail = _verification_gap_detail(payload) or (
                                    "验收事件未附具体缺口；Agent 正在重新核对结构化验收结果。"
                                )
                                lifecycle_display_status = "running"
                            elif lifecycle_status == VerificationStatus.INFRASTRUCTURE_ERROR.value:
                                lifecycle_label = "验收服务异常，自动处理已停止"
                                lifecycle_detail = "这不代表业务产物未通过。请发送“重试验收”继续。"
                                lifecycle_display_status = "failed"
                            elif lifecycle_status in {"needs_revision", "failed"}:
                                lifecycle_label = "完成条件仍有缺口，自动处理已停止"
                                lifecycle_detail = _verification_gap_detail(payload) or (
                                    "Goal、Todo 和证据已保留。发送“继续完成剩余工作”即可从当前进度继续。"
                                    if run_record.goal_id
                                    else "请查看右侧验收明细，再发送具体修复要求。"
                                )
                                lifecycle_display_status = "failed"
                            else:
                                lifecycle_label = "完成条件检查异常"
                                lifecycle_display_status = "failed"
                        elif event_type == "rubric_evaluation_end":
                            if lifecycle_status == "satisfied":
                                lifecycle_label = "完成质量检查通过"
                                lifecycle_display_status = "satisfied"
                            elif lifecycle_status == "needs_revision":
                                lifecycle_label = "发现完成质量缺口，正在自动继续修复"
                                lifecycle_detail = _verification_gap_detail(payload) or (
                                    "验收事件未附具体缺口；Agent 正在重新核对结构化验收结果。"
                                )
                                lifecycle_display_status = "running"
                            elif lifecycle_status == VerificationStatus.INFRASTRUCTURE_ERROR.value:
                                lifecycle_label = "验收服务异常，自动处理已停止"
                                lifecycle_detail = "这不代表业务产物未通过。请发送“重试验收”继续。"
                                lifecycle_display_status = "failed"
                            elif lifecycle_status == "failed":
                                lifecycle_label = "完成质量仍有缺口，自动处理已停止"
                                lifecycle_detail = _verification_gap_detail(payload) or (
                                    "Goal、Todo 和证据已保留。发送“继续完成剩余工作”即可从当前进度继续。"
                                    if run_record.goal_id
                                    else "请查看右侧验收明细，再发送具体修复要求。"
                                )
                                lifecycle_display_status = "failed"
                            else:
                                lifecycle_label = "完成质量检查异常"
                                lifecycle_display_status = "failed"
                        if lifecycle_label:
                            self._finalize_reasoning_timeline(active_segment)
                            timeline = active_segment.setdefault("timeline", [])
                            activity_id = f"{event_type}-{len(timeline)}"
                            if event_type in {"rubric_evaluation_start", "rubric_evaluation_end"}:
                                grading_key = str(
                                    payload.get("grading_run_id") or payload.get("iteration") or "current"
                                )
                                activity_id = f"verification-quality-{grading_key}"
                            elif event_type == "deterministic_checks_completed":
                                run_key = str(
                                    payload.get("run_id")
                                    or (run_record.run_id if run_record is not None else "")
                                    or "current"
                                )
                                activity_id = f"verification-completion-{run_key}"
                            activity = {
                                "type": "activity",
                                "label": lifecycle_label,
                                "status": lifecycle_display_status or lifecycle_status,
                                "id": activity_id,
                            }
                            if lifecycle_detail:
                                activity["detail"] = lifecycle_detail
                            existing_index = next(
                                (
                                    index
                                    for index, item in enumerate(timeline)
                                    if isinstance(item, dict) and item.get("id") == activity_id
                                ),
                                None,
                            )
                            if existing_index is None:
                                timeline.append(activity)
                            else:
                                timeline[existing_index] = activity
                        trace_collector.add_custom_span(event_type, payload)
                        yield self._sse(event_type, payload)
                        if event_type == "context_usage":
                            observed_usage = payload.get("used_tokens")
                            if isinstance(observed_usage, int):
                                last_context_usage = observed_usage
                                received_context_usage_event = True
                                if observed_usage != last_persisted_context_usage:
                                    try:
                                        session_manager.update_agent_context_usage(session_id, observed_usage)
                                        last_persisted_context_usage = observed_usage
                                    except Exception:
                                        logger.warning(
                                            "Failed to persist streamed Agent context usage for session=%s",
                                            session_id,
                                            exc_info=True,
                                        )
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
                            if str(payload.get("result") or "") in {"needs_revision", "failed"}:
                                verification_retry_pending_segment = True
                                persist_assistant_snapshot(force=True)
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
                            if str(payload.get("status") or "") == "needs_revision":
                                verification_retry_pending_segment = True
                                persist_assistant_snapshot(force=True)
                        elif event_type == "model_call_limit_exceeded":
                            model_call_limit_events.append(dict(payload))
                        if lifecycle_label:
                            persist_assistant_snapshot(force=True)
                elif mode == "values" and isinstance(payload, dict):
                    final_state = payload
                    effective_messages = _effective_agent_messages(payload)
                    current_context_usage = _estimate_agent_context_tokens(
                        effective_messages,
                        system_prompt,
                        agent_tools,
                    )
                    if received_context_usage_event and last_context_usage >= 0:
                        current_context_usage = last_context_usage
                    summary_event = payload.get("_summarization_event")
                    compact_context_active = using_saved_agent_context or isinstance(summary_event, dict)
                    try:
                        if compact_context_active:
                            serialized_context = _serialize_protocol_closed_agent_context(effective_messages)
                            if serialized_context is None:
                                # A model value is emitted before the tools node.
                                # Persisting here would freeze a synthetic error
                                # response and discard the real ToolMessage that
                                # arrives in the next value event.
                                session_manager.update_agent_context_state(
                                    session_id,
                                    used_tokens=current_context_usage,
                                    run_id=run_record.run_id,
                                )
                            else:
                                context_fingerprint = _agent_context_fingerprint(serialized_context)
                                if context_fingerprint != persisted_agent_context_fingerprint:
                                    session_manager.update_agent_context_state(
                                        session_id,
                                        used_tokens=current_context_usage,
                                        messages=serialized_context,
                                        run_id=run_record.run_id,
                                    )
                                    persisted_agent_context_fingerprint = context_fingerprint
                                elif current_context_usage != last_persisted_context_usage:
                                    session_manager.update_agent_context_usage(
                                        session_id,
                                        current_context_usage,
                                    )
                        elif current_context_usage != last_persisted_context_usage:
                            # A normal, uncompacted Run must expose its effective
                            # context while it is still running. Previously this
                            # value was only persisted for summarized contexts or
                            # at terminal completion, leaving the UI at
                            # ``pending measurement`` throughout long tool Runs.
                            session_manager.update_agent_context_usage(
                                session_id,
                                current_context_usage,
                            )
                        last_persisted_context_usage = current_context_usage
                    except Exception:
                        logger.warning(
                            "Failed to persist Agent context usage for session=%s",
                            session_id,
                            exc_info=True,
                        )
                    if (
                        not received_context_usage_event
                        and current_context_usage != last_fallback_context_usage
                    ):
                        # ToolProtocolIntegrityMiddleware normally publishes an
                        # exact model-boundary event. Graph values are a reliable
                        # fallback for providers/runtimes that do not propagate
                        # custom middleware events.
                        yield self._sse(
                            "context_usage",
                            {
                                "used_tokens": current_context_usage,
                                "total_tokens": context_trigger_tokens,
                                "percentage": round(
                                    current_context_usage / max(1, context_trigger_tokens) * 100,
                                    1,
                                ),
                                "source": "graph_values_fallback",
                            },
                        )
                        last_fallback_context_usage = current_context_usage
                    # HarnessTodoMiddleware has already committed the patch to
                    # Session JSON before returning success. Graph values are a
                    # projection only: they may notify the frontend, but must
                    # never become a second list-replacement write path.
                    current_todos = payload.get("todos")
                    if isinstance(current_todos, list) and current_todos != last_graph_todos:
                        todo_snapshot = session_manager.get_todo_snapshot(
                            session_id,
                            goal_id=run_record.goal_id,
                            goal_revision=run_record.goal_revision,
                            run_id=(run_record.run_id if not run_record.goal_id else None),
                        )
                        authoritative_todos = list(todo_snapshot.get("todos") or [])
                        normalized_graph_todos = self._normalize_todos(current_todos)
                        if normalized_graph_todos != authoritative_todos:
                            emit_harness_metric(
                                logger,
                                "todo_graph_projection_mismatch_total",
                                session_id=session_id,
                                run_id=run_record.run_id,
                                ledger_revision=int(todo_snapshot.get("ledger_revision") or 0),
                            )
                        diff = self._todo_diff(previous_todos, authoritative_todos)
                        previous_todos = authoritative_todos
                        last_graph_todos = list(current_todos)
                        trace_collector.add_todo_span(authoritative_todos, diff=diff)
                        yield self._sse(
                            "todos_updated",
                            {
                                "todos": authoritative_todos,
                                "session_id": session_id,
                                "query_id": query_id,
                                "source_run_id": run_record.run_id,
                                "operation_id": todo_snapshot.get("operation_id"),
                                "authority": todo_snapshot.get("authority"),
                                "ledger_revision": int(todo_snapshot.get("ledger_revision") or 0),
                                "persisted_at": todo_snapshot.get("persisted_at"),
                            },
                        )

            if task_router_task is not None and task_router_task.done() and not task_router_event_emitted:
                self._run_coordinator._refresh_runtime_fields(run_record)
                record_task_router_trace(trace_collector)
                router_event = task_router_completion_event()
                if router_event is not None:
                    task_router_event_emitted = True
                    yield router_event

            # Close any still-running LLM span at the end of the stream.
            if active_llm_span is not None:
                trace_collector.finish_llm_span(output=emitted_text)
                active_llm_span = None

            if model_limit_text_buffer and not suppressing_model_limit_notice:
                active_segment["content"] += model_limit_text_buffer
                emitted_text += model_limit_text_buffer
                if not defer_final_publication:
                    yield self._sse("token", {"content": model_limit_text_buffer})
                model_limit_text_buffer = ""

            final_content = self._strip_model_call_limit_notice(self._last_ai_content(final_state) or emitted_text)
            if final_content:
                current_text = active_segment.get("content", "")
                if not current_text.strip():
                    active_segment["content"] = final_content
                    emitted_text = final_content
                    if not defer_final_publication:
                        yield self._sse("token", {"content": final_content})
                elif final_content.strip() not in current_text:
                    # The authoritative final answer differs from the streamed
                    # text (e.g. only intermediate planning was streamed before
                    # tools). Replace with the final answer.
                    active_segment["content"] = final_content
                    emitted_text = final_content
                    if not defer_final_publication:
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
                if not defer_final_publication:
                    yield self._sse("token", {"content": diagnostic})

            persisted_run = session_manager.get_run_state(session_id, run_record.run_id)
            persisted_activations = (
                persisted_run.get("verification_activations")
                if isinstance(persisted_run, dict) and isinstance(persisted_run.get("verification_activations"), list)
                else []
            )
            artifact_links = self._artifact_links(
                persisted_activations,
                workspace_path,
                existing_content=active_segment.get("content", ""),
            )
            if artifact_links:
                active_segment["content"] += artifact_links
                emitted_text += artifact_links
                final_content = f"{final_content or ''}{artifact_links}"
                if not defer_final_publication:
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
                        "status": "interrupted",
                        "output_complete": False,
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

            self._run_coordinator._refresh_runtime_fields(run_record)
            pending_completion_request = None
            if run_record.completion_request_id:
                pending_completion_request = (
                    session_manager.get_harness_state(session_id)
                    .get("completion_requests", {})
                    .get(run_record.completion_request_id)
                )
            if (
                run_record.status == RunStatus.RUNNING
                and run_record.requires_goal_verification
                and isinstance(pending_completion_request, dict)
                and pending_completion_request.get("status") == "requested"
            ):
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
            completion_accepted = bool(
                run_record.outcome == RunOutcome.COMPLETED
                and (
                    goal_record is not None
                    and goal_record.status == GoalStatus.COMPLETED
                    and run_record.completion_request_id
                    or goal_record is None
                    and verification_report is not None
                    and verification_report.status in {VerificationStatus.NOT_REQUIRED, VerificationStatus.SATISFIED}
                )
                and (
                    goal_record is None
                    or goal_record.completion_policy == GoalCompletionPolicy.STANDARD
                    or verification_report is not None
                    and verification_report.accepted_for_goal_revision is True
                )
            )

            # Session JSON contains the published answer and process metadata,
            # never rejected terminal text.  Full internal model messages stay
            # in the checkpoint and Trace for audit.
            if defer_final_publication:
                final_segment_index = next(
                    (
                        index
                        for index in range(len(segments) - 1, -1, -1)
                        if str(segments[index].get("content") or "").strip() or segments[index].get("tool_calls")
                    ),
                    len(segments) - 1,
                )
                for index, segment in enumerate(segments):
                    segment.pop("verification_state", None)
                    if completion_accepted and index == final_segment_index:
                        segment["content"] = final_content or ""
                    elif segment.get("tool_calls"):
                        # Preserve user-visible narration from non-terminal
                        # model turns. Only tool-less terminal candidates are
                        # rejected by the publication gate.
                        segment["content"] = str(segment.get("content") or "")
                    else:
                        segment["content"] = ""
            else:
                for segment in segments:
                    segment.pop("verification_state", None)

            # Build the single assistant message content by concatenating segment
            # text, and persist the segments array for the UI.
            for seg in segments:
                seg["content"] = sanitize_citation_markdown(
                    self._strip_model_call_limit_notice(str(seg.get("content") or ""))
                )
            full_content = (
                (final_content or "")
                if defer_final_publication and completion_accepted
                else ""
                if defer_final_publication
                else "\n\n".join(seg["content"] for seg in segments if seg.get("content"))
            )
            all_tool_calls = [tc for seg in segments for tc in seg.get("tool_calls", [])]
            all_timeline = [item for seg in segments for item in seg.get("timeline", [])]
            message_sources, final_citations = resolve_message_citations(
                full_content,
                turn_sources,
                session_sources,
            )
            for seg in segments:
                seg.pop("_current_reasoning", None)
            verification_detail = _user_facing_verification_summary(
                str(verification_report.explanation or "") if verification_report is not None else ""
            )
            verification_summary = (
                "**验证结果：**\n\n"
                + (verification_detail or "已依据本次验收标准核对最终交付及其证据，所有必需项均已满足。")
                if completion_accepted
                and verification_report is not None
                and verification_report.status == VerificationStatus.SATISFIED
                else _terminal_verification_guidance(
                    verification_report.status if verification_report is not None else VerificationStatus.NOT_REQUIRED,
                    has_goal=goal_record is not None,
                    goal_status=(goal_record.status if goal_record is not None else None),
                    explanation=str(verification_report.explanation or "") if verification_report is not None else "",
                )
                if not completion_accepted
                else ""
            )
            if completion_accepted:
                session_manager.commit_accepted_completion(
                    session_id,
                    run=run_record.model_dump(mode="json"),
                    goal=(goal_record.model_dump(mode="json") if goal_record is not None else None),
                    query_id=query_id,
                    content=full_content,
                    tool_calls=all_tool_calls or None,
                    sources=message_sources or None,
                    citations=final_citations or None,
                    reasoning_content=accumulated_reasoning or None,
                    timeline=all_timeline or None,
                    segments=segments or None,
                    output_attachments=published_attachments or None,
                    verification_summary=verification_summary or None,
                )
            elif (
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
                    verification_summary=verification_summary or None,
                    status=(run_record.outcome.value if run_record.outcome else "failed"),
                )
            # From this point on cancellation/error handling must not overwrite
            # the authoritative terminal Session transaction above.
            run_messages_persisted = True
            terminal_authority_committed = True

            if run_record.run_kind != RunKind.GOAL_INSPECTION and verification_report is not None:
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

            # Everything below is diagnostic or resumable context. It is
            # deliberately best-effort and occurs only after Run/Goal/message
            # authority has been committed and announced.
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
                try:
                    effective_context_messages = _effective_agent_messages(final_state)
                    final_context_usage = _estimate_agent_context_tokens(
                        effective_context_messages,
                        system_prompt,
                        agent_tools,
                    )
                    if last_context_usage >= 0:
                        final_context_usage = last_context_usage
                    serialized_context: list[dict[str, Any]] | None = None
                    if using_saved_agent_context or isinstance(final_state.get("_summarization_event"), dict):
                        serialized_context = _serialize_protocol_closed_agent_context(effective_context_messages)
                    if serialized_context is None:
                        session_manager.update_agent_context_state(
                            session_id,
                            used_tokens=final_context_usage,
                            run_id=run_record.run_id,
                        )
                    else:
                        session_manager.update_agent_context_state(
                            session_id,
                            used_tokens=final_context_usage,
                            messages=serialized_context,
                            run_id=run_record.run_id,
                        )
                except Exception:
                    logger.warning(
                        "Failed to persist final Agent context for session=%s",
                        session_id,
                        exc_info=True,
                    )

            # Streaming already emitted model/tool/reasoning spans. Avoid
            # rebuilding segment spans here because that duplicates simple
            # turns such as "你好" as two model calls.
            trace: dict[str, Any] | None = None
            try:
                record_task_router_trace(trace_collector, settle_pending=True)
                trace = trace_collector.finish(
                    status=run_record.outcome.value if run_record.outcome else "completed"
                )
                await asyncio.to_thread(
                    session_manager.update_trace,
                    session_id,
                    trace,
                    query_id,
                )
            except Exception:
                logger.warning(
                    "Failed to finalize trace for session=%s query=%s",
                    session_id,
                    query_id,
                    exc_info=True,
                )
            finally:
                if trace_context_active:
                    trace_collector.__exit__(None, None, None)
                    trace_context_active = False

            while pending_trace_events:
                yield pending_trace_events.pop(0)
            if trace is not None:
                yield self._sse(
                    "trace_updated",
                    {"trace": trace, "session_id": session_id, "query_id": query_id},
                )
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
                                    "本轮工具上下文已部分压缩；失败项继续保留原始工具结果，证据引用未被丢弃。"
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
            if completion_accepted:
                if defer_final_publication:
                    for attachment in published_attachments:
                        yield self._sse(
                            "attachment_published",
                            {
                                "tool_call_id": attachment.get("tool_call_id"),
                                "query_id": query_id,
                                "attachment": attachment,
                            },
                        )
                yield self._sse(
                    "final_response",
                    {
                        "content": full_content,
                        "session_id": session_id,
                        "query_id": query_id,
                        "run_id": run_record.run_id,
                        "goal_id": run_record.goal_id,
                        "verification_summary": verification_summary,
                    },
                )
            yield self._sse(
                "done",
                {
                    "content": full_content if completion_accepted else "",
                    "verification_summary": verification_summary,
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
            if terminal_authority_committed:
                # The client disconnected after the authoritative terminal
                # transaction. Preserve the committed result exactly as-is.
                raise
            try:
                if run_record is not None:
                    persisted_run = session_manager.get_run_state(session_id, run_record.run_id)
                    if isinstance(persisted_run, dict):
                        run_record = RunRecord.model_validate(persisted_run)
                    if run_record.goal_id:
                        persisted_goal = session_manager.get_goal_state(session_id, run_record.goal_id)
                        if isinstance(persisted_goal, dict):
                            goal_record = GoalRecord.model_validate(persisted_goal)
            except Exception:
                logger.warning(
                    "Failed to reload authoritative state after cancellation for session=%s",
                    session_id,
                    exc_info=True,
                )
            if (
                completion_accepted
                and run_record is not None
                and run_record.terminal
                and (goal_record is None or goal_record.status == GoalStatus.COMPLETED)
            ):
                # Covers the narrow case where the atomic write succeeded but
                # the caller was cancelled before the commit call returned.
                run_messages_persisted = True
                terminal_authority_committed = True
                raise
            try:
                if run_record is not None and not run_record.terminal:
                    # Exceptions bypass the normal final-state projection. Use
                    # the provider input boundary as the authoritative count so
                    # failed Runs do not incorrectly report zero model calls.
                    run_record.model_call_count = max(
                        run_record.model_call_count,
                        int(locals().get("model_call_index") or 0),
                        int(
                            getattr(
                                locals().get("trace_collector"),
                                "model_input_count",
                                0,
                            )
                            or 0
                        ),
                    )
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
                        suppress_terminal_content=bool(run_record.requires_goal_verification),
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
                if run_record is not None:
                    user_input_resume_registry.reject_run(
                        session_id,
                        run_record.run_id,
                        "Agent stream was cancelled by the client.",
                    )
            except Exception:
                logger.debug("Failed to reject pending permission requests for session=%s", session_id, exc_info=True)
            try:
                if trace_collector is not None:
                    record_task_router_trace(trace_collector, settle_pending=True)
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
            error_traceback = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            if terminal_authority_committed:
                # Post-commit maintenance is best-effort. Never turn a
                # committed success into a user-visible Run error.
                logger.warning(
                    "Post-commit maintenance failed for session=%s: %s",
                    session_id,
                    exc,
                    exc_info=True,
                )
                yield self._sse(
                    "done",
                    {
                        "content": full_content if completion_accepted else "",
                        "verification_summary": verification_summary,
                        "session_id": session_id,
                        "project_id": project_id,
                        "workspace_path": str(workspace_path),
                        "run_id": run_record.run_id if run_record is not None else None,
                        "run_outcome": (
                            run_record.outcome.value if run_record is not None and run_record.outcome else None
                        ),
                        "goal_id": run_record.goal_id if run_record is not None else None,
                        "goal_status": goal_record.status.value if goal_record else None,
                    },
                )
                return
            try:
                if run_record is not None:
                    persisted_run = session_manager.get_run_state(session_id, run_record.run_id)
                    if isinstance(persisted_run, dict):
                        run_record = RunRecord.model_validate(persisted_run)
                    if run_record.goal_id:
                        persisted_goal = session_manager.get_goal_state(session_id, run_record.goal_id)
                        if isinstance(persisted_goal, dict):
                            goal_record = GoalRecord.model_validate(persisted_goal)
            except Exception:
                logger.warning(
                    "Failed to reload authoritative state after stream error for session=%s",
                    session_id,
                    exc_info=True,
                )
            if (
                completion_accepted
                and run_record is not None
                and run_record.terminal
                and (goal_record is None or goal_record.status == GoalStatus.COMPLETED)
            ):
                logger.warning(
                    "Completion commit succeeded despite a caller-side exception for session=%s: %s",
                    session_id,
                    exc,
                    exc_info=True,
                )
                run_messages_persisted = True
                terminal_authority_committed = True
                return
            transport_interrupted = isinstance(exc, ModelTransportInterruptedError)
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
            error_notice = (
                "模型连接在有限重试后仍未恢复。本轮已按基础设施故障停止，"
                "中断内容未作为最终回答或工具调用；已保留此前完成的 Todo、证据和产物，可输入“继续”恢复。"
                if transport_interrupted
                else f"本轮执行中断：{error_msg}。已保留中断前完成的内容，可修复连接后输入“继续”。"
            )
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
                        run_record, goal_record, verification_report = self._run_coordinator.complete_budget_exceeded(
                            run_record,
                            goal_record,
                            reason=reason,
                            model_call_count=exc.run_count,
                            detail=detail,
                        )
                    else:
                        verification_report = run_record.verification_report
                    if verification_report is not None and run_record.run_kind != RunKind.GOAL_INSPECTION:
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
                        seg["content"] = self._strip_model_call_limit_notice(str(seg.get("content") or ""))
                        if defer_final_publication and not seg.get("tool_calls"):
                            seg["content"] = ""
                    full_content = "\n\n".join(str(seg.get("content") or "") for seg in segments if seg.get("content"))
                    self._persist_assistant_snapshot(
                        session_id=session_id,
                        query_id=query_id,
                        segments=segments,
                        accumulated_reasoning=accumulated_reasoning,
                        turn_sources=turn_sources,
                        output_attachments=([] if defer_final_publication else published_attachments),
                        session_sources=session_sources,
                        status="completed",
                    )
                    run_messages_persisted = True
                    if trace_collector is not None:
                        record_task_router_trace(trace_collector, settle_pending=True)
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
                            "content": full_content if not defer_final_publication else "",
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
                    # This path also bypasses the normal final-state projection.
                    # Count provider input boundaries, including a failed call
                    # that produced no user-visible token.
                    run_record.model_call_count = max(
                        run_record.model_call_count,
                        int(locals().get("model_call_index") or 0),
                        int(
                            getattr(
                                locals().get("trace_collector"),
                                "model_input_count",
                                0,
                            )
                            or 0
                        ),
                    )
                    self._run_coordinator.fail(
                        run_record,
                        outcome=(RunOutcome.INFRASTRUCTURE_ERROR if transport_interrupted else RunOutcome.FAILED),
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
                        suppress_terminal_content=bool(run_record.requires_goal_verification),
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
                    # Keep a bounded server-side traceback in the diagnostic
                    # Trace. The user-facing SSE remains sanitized, while a
                    # pre-provider failure can be located without asking the
                    # user to reproduce it repeatedly or scrape a terminal.
                    trace_collector.root.metadata["error_type"] = type(exc).__name__
                    trace_collector.root.metadata["error_traceback"] = error_traceback[-20000:]
                    record_task_router_trace(trace_collector, settle_pending=True)
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
            if run_record is not None:
                try:
                    user_input_resume_registry.reject_run(
                        session_id,
                        run_record.run_id,
                        "Owning Run reached a terminal boundary.",
                    )
                except Exception:
                    logger.debug(
                        "Failed to reject terminal user-input requests for session=%s run=%s",
                        session_id,
                        run_record.run_id,
                        exc_info=True,
                    )
            if task_router_task is not None and not task_router_task.done():
                task_router_task.cancel()
                try:
                    await task_router_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug(
                        "Task router cleanup failed for session=%s query=%s",
                        session_id,
                        query_id,
                        exc_info=True,
                    )
            if goal_record is not None:
                key = (session_id, goal_record.goal_id)
                if self._active_goal_tasks.get(key) is asyncio.current_task():
                    self._active_goal_tasks.pop(key, None)
            scratch_path = getattr(locals().get("agent_backend"), "execution_scratch_host_path", None)
            scratch_goal_id = str(getattr(locals().get("agent_backend"), "execution_scratch_goal_id", "") or "")
            scratch_goal_revision = getattr(
                locals().get("agent_backend"),
                "execution_scratch_goal_revision",
                None,
            )
            # Goal revisions deliberately share writable draft scratch across
            # continuation Runs. Read-only external search snapshots are
            # different: their authority belongs to the concrete Run that
            # held the grant. Remove only terminalized search subtrees here so
            # a later Run cannot read stale host contents through /scratch.
            if scratch_path:
                scratch_root = Path(str(scratch_path)).resolve()
                terminal_run_id = str(getattr(locals().get("run_record"), "run_id", "") or "")
                try:
                    search_leases = [
                        *session_manager.list_external_artifact_leases(session_id),
                        *session_manager.list_external_directory_leases(session_id),
                    ]
                except Exception:
                    logger.debug(
                        "Failed to load terminal search leases for session=%s",
                        session_id,
                        exc_info=True,
                    )
                    search_leases = []
                for lease in search_leases:
                    if (
                        not isinstance(lease, dict)
                        or lease.get("search_only") is not True
                        or str(lease.get("status") or "") != "abandoned"
                        or str(lease.get("abandoned_reason") or "") != "run_search_snapshot_terminal"
                        or str(lease.get("run_id") or "") != terminal_run_id
                        or str(lease.get("query_id") or "") != str(query_id or "")
                    ):
                        continue
                    virtual_path = str(lease.get("staged_dir") or lease.get("staged_path") or "").replace("\\", "/")
                    if not virtual_path.startswith("/scratch/"):
                        continue
                    relative = virtual_path.removeprefix("/scratch/")
                    candidate = (scratch_root / relative).resolve()
                    try:
                        candidate.relative_to(scratch_root)
                    except ValueError:
                        continue
                    if lease.get("staged_path") and not lease.get("staged_dir"):
                        candidate = candidate.parent
                    try:
                        shutil.rmtree(candidate, ignore_errors=True)
                    except Exception:
                        logger.debug(
                            "Failed to clean Run search snapshot %s for session=%s run=%s",
                            virtual_path,
                            session_id,
                            terminal_run_id,
                            exc_info=True,
                        )
            cleanup_scratch = True
            if scratch_goal_id:
                try:
                    persisted_goal = session_manager.get_goal_state(
                        session_id,
                        scratch_goal_id,
                    )
                    cleanup_scratch = (
                        not isinstance(persisted_goal, dict)
                        or str(persisted_goal.get("status") or "") in {"completed", "cancelled", "budget_exceeded"}
                        or int(persisted_goal.get("objective_revision") or 1) != int(scratch_goal_revision or 1)
                    )
                except Exception:
                    # Preserve scratch on uncertain authority rather than
                    # deleting resumable Goal work during cleanup.
                    logger.debug(
                        "Failed to resolve scratch Goal state for session=%s goal=%s",
                        session_id,
                        scratch_goal_id,
                        exc_info=True,
                    )
                    cleanup_scratch = False
            if scratch_path and cleanup_scratch:
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

def build_model_messages(
    session_json: dict[str, Any] | list[dict[str, Any]],
    current_query: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
    workspace_path: str | Path | None = None,
    query_id: str | None = None,
) -> list[Any]:
    """Pure model-message projection from the durable Session fact.

    This public wrapper makes the cache contract explicit: callers provide the
    same Session JSON and query and receive the same protocol-valid messages;
    it never reads a checkpoint, generates a new persisted record, or mutates
    the supplied object.
    """

    history = session_json.get("messages", []) if isinstance(session_json, dict) else session_json
    if not isinstance(history, list):
        history = []
    return DeepAgentsAgentManager._build_messages(
        history,
        current_query,
        attachments,
        session_id=session_id,
        workspace_path=workspace_path,
        query_id=query_id,
    )


deepagents_agent_manager = DeepAgentsAgentManager()
