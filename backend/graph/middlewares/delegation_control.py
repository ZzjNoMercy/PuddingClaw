"""Bounded, observable control plane for DeepAgents' native ``task`` tool."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from graph.session_manager import session_manager
from harness.models import (
    DelegationContract,
    DelegationLimits,
    DelegationResultEnvelope,
)

_SQL_GENERATION_RE = re.compile(r"\bsql-gen-[A-Za-z0-9_-]+\b")
_SQL_RECEIPT_RE = re.compile(r"\bsql-validation-[A-Za-z0-9_-]+\b")


@dataclass(slots=True)
class _ActiveDelegation:
    contract: DelegationContract
    last_activity_at: float
    active_operation: str = ""
    event_sequence: int = 0
    model_calls: int = 0
    tool_calls: int = 0


_ACTIVE_DELEGATION: ContextVar[_ActiveDelegation | None] = ContextVar(
    "puddingclaw_active_delegation",
    default=None,
)


class _DelegationLimitExceeded(TimeoutError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _runtime_context(request: ToolCallRequest) -> dict[str, Any]:
    runtime = request.runtime
    context = runtime.context if runtime is not None else None
    return context if isinstance(context, dict) else {}


def _tool_result_content(result: ToolMessage | Command[Any]) -> str:
    if isinstance(result, ToolMessage):
        return str(result.content or "")
    update = result.update if isinstance(result.update, dict) else {}
    for message in reversed(update.get("messages") or []):
        if isinstance(message, ToolMessage):
            return str(message.content or "")
    return ""


def _todos_from_state(state: Any) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    return [item for item in state.get("todos") or [] if isinstance(item, dict)]


def _replace_tool_result(
    result: ToolMessage | Command[Any],
    *,
    content: str,
    tool_call_id: str,
) -> ToolMessage | Command[Any]:
    message = ToolMessage(content=content, tool_call_id=tool_call_id)
    if isinstance(result, ToolMessage):
        return message
    update = dict(result.update) if isinstance(result.update, dict) else {}
    update["messages"] = [message]
    return Command(
        graph=result.graph,
        update=update,
        resume=result.resume,
        goto=result.goto,
    )


class DelegationControlMiddleware(AgentMiddleware[Any, Any, Any]):
    """Wrap native subagents in a persisted contract and bounded handoff."""

    def __init__(self, *, limits: DelegationLimits | None = None) -> None:
        self.limits = limits or DelegationLimits()

    @staticmethod
    def _write_event(request: ToolCallRequest, event: dict[str, Any]) -> None:
        runtime = request.runtime
        writer = getattr(runtime, "stream_writer", None) if runtime is not None else None
        if writer is not None:
            writer(event)

    @staticmethod
    def _persist_event(contract: DelegationContract, event: dict[str, Any]) -> None:
        if not session_manager.is_initialized:
            return
        try:
            session_manager.record_delegation_event(
                contract.session_id,
                contract.parent_run_id,
                event,
            )
        except (FileNotFoundError, RuntimeError, ValueError):
            return

    def _event(
        self,
        request: ToolCallRequest,
        contract: DelegationContract,
        event_type: str,
        **payload: Any,
    ) -> dict[str, Any]:
        event = {
            "type": event_type,
            "event_id": f"{contract.subagent_run_id}:{event_type}:{payload.get('sequence', 0)}",
            "subagent_run_id": contract.subagent_run_id,
            "parent_run_id": contract.parent_run_id,
            "subagent_type": contract.subagent_type,
            "objective": contract.objective,
            "timestamp": time.time(),
            **payload,
        }
        self._persist_event(contract, event)
        self._write_event(request, event)
        return event

    def _contract(self, request: ToolCallRequest) -> DelegationContract:
        context = _runtime_context(request)
        state = request.state if isinstance(request.state, dict) else {}
        args = request.tool_call.get("args") or {}
        run_id = str(context.get("run_id") or "")
        session_id = str(context.get("session_id") or "")
        persisted = (
            session_manager.get_run_state(session_id, run_id)
            if session_manager.is_initialized and session_id and run_id
            else None
        )
        parent_tool_call_id = str(request.tool_call.get("id") or "")
        if isinstance(persisted, dict):
            existing = next(
                (
                    item
                    for item in persisted.get("delegation_contracts") or []
                    if isinstance(item, dict)
                    and str(item.get("parent_tool_call_id") or "")
                    == parent_tool_call_id
                ),
                None,
            )
            if isinstance(existing, dict):
                return DelegationContract.model_validate(existing)
        manifest = persisted.get("capability_manifest") if isinstance(persisted, dict) else None
        activations = persisted.get("skill_activations") if isinstance(persisted, dict) else None
        profile = persisted.get("task_profile") if isinstance(persisted, dict) else None
        contract = persisted.get("verification_contract") if isinstance(persisted, dict) else None
        todos = _todos_from_state(state)
        todo_slice = [
            str(item.get("id"))
            for item in todos
            if item.get("id") and str(item.get("status") or "") in {"pending", "in_progress"}
        ]
        allowed_toolsets = [
            str(item)
            for item in (manifest.get("enabled_toolsets") if isinstance(manifest, dict) else []) or []
        ]
        permission_context: dict[str, Any] = {}
        declared_artifact_targets: list[str] = []
        if isinstance(persisted, dict):
            from graph.permission_policy import RunPermissionContext

            permission_context = RunPermissionContext.from_config_snapshot(
                persisted.get("config_snapshot")
            ).grant_bindings()
            declared_artifact_targets = sorted(
                {
                    str(item)
                    for item in persisted.get("declared_artifact_targets") or []
                    if str(item)
                }
            )
        effective_limits = self._derive_limits(
            objective=str(args.get("description") or "").strip(),
            todo_count=len(todo_slice),
        )
        delegation_seed = (
            f"{session_id}\0{run_id}\0{parent_tool_call_id}\0"
            f"{str(args.get('subagent_type') or 'general-purpose')}"
        )
        return DelegationContract(
            subagent_run_id=(
                "subrun-"
                + hashlib.sha256(delegation_seed.encode("utf-8")).hexdigest()[:16]
            ),
            parent_run_id=run_id,
            parent_tool_call_id=parent_tool_call_id,
            session_id=session_id,
            goal_id=str(context.get("goal_id") or "") or None,
            goal_revision=context.get("goal_revision"),
            subagent_type=str(args.get("subagent_type") or "general-purpose"),
            objective=str(args.get("description") or "").strip(),
            todo_slice=todo_slice,
            selected_analytics_model=str(state.get("analytics_model_id") or "") or None,
            semantic_context_refs=[
                str(item)
                for item in (profile.get("available_context_refs") if isinstance(profile, dict) else []) or []
            ],
            allowed_skill_activations=[
                str(item.get("activation_id"))
                for item in (activations if isinstance(activations, list) else [])
                if isinstance(item, dict) and item.get("activation_id")
            ],
            allowed_toolsets=allowed_toolsets,
            permission_context=permission_context,
            declared_artifact_targets=declared_artifact_targets,
            expected_output_schema=(
                "DatabaseEvidenceBatch/v1"
                if "database_analysis" in allowed_toolsets
                else "DelegationResultEnvelope/v1"
            ),
            completion_conditions=[
                str(item.get("criterion_id") or item.get("statement") or "")
                for item in (contract.get("criteria") if isinstance(contract, dict) else []) or []
                if isinstance(item, dict)
            ],
            limits=effective_limits,
        )

    def _derive_limits(
        self,
        *,
        objective: str,
        todo_count: int,
    ) -> DelegationLimits:
        """Allocate bounded resources from observable task complexity."""

        normalized = objective.lower()
        data_or_template = any(
            marker in normalized
            for marker in (
                "database",
                "sql",
                "查询",
                "数据",
                "export",
                "materialize",
                "source_ref",
                "template",
                "模板",
                "slot",
                "填充",
            )
        )
        model_calls = max(
            self.limits.model_calls,
            min(32, 10 + todo_count * 2 + (8 if data_or_template else 0)),
        )
        tool_calls = max(
            self.limits.tool_calls,
            min(100, 24 + todo_count * 5 + (24 if data_or_template else 0)),
        )
        wall_clock_seconds = max(
            self.limits.wall_clock_seconds,
            min(1_800, 300 + todo_count * 90 + (300 if data_or_template else 0)),
        )
        return DelegationLimits(
            wall_clock_seconds=wall_clock_seconds,
            model_calls=model_calls,
            tool_calls=tool_calls,
            idle_seconds=self.limits.idle_seconds,
        )

    @staticmethod
    def _same_nonretryable_delegation_exists(contract: DelegationContract) -> bool:
        if not session_manager.is_initialized or not contract.session_id or not contract.parent_run_id:
            return False
        run = session_manager.get_run_state(contract.session_id, contract.parent_run_id)
        if not isinstance(run, dict):
            return False
        normalized = " ".join(contract.objective.lower().split())
        current_todos = set(contract.todo_slice)
        contracts = {
            str(item.get("subagent_run_id") or ""): item
            for item in run.get("delegation_contracts") or []
            if isinstance(item, dict)
        }
        for result in run.get("delegation_results") or []:
            if (
                not isinstance(result, dict)
                or result.get("status")
                not in {"timed_out", "failed", "blocked", "cancelled"}
            ):
                continue
            previous = contracts.get(str(result.get("subagent_run_id") or ""))
            if not isinstance(previous, dict):
                continue
            if (
                str(previous.get("subagent_type") or "") == contract.subagent_type
                and (
                    " ".join(str(previous.get("objective") or "").lower().split()) == normalized
                    or (
                        current_todos
                        and current_todos == {
                            str(item) for item in previous.get("todo_slice") or [] if str(item)
                        }
                    )
                )
                and not bool(result.get("retry_same_delegation_allowed"))
            ):
                return True
        return False

    def _persist_contract(self, contract: DelegationContract) -> None:
        if not session_manager.is_initialized or not contract.session_id or not contract.parent_run_id:
            return
        session_manager.record_delegation_contract(
            contract.session_id,
            contract.parent_run_id,
            contract.model_dump(mode="json"),
        )

    @staticmethod
    async def _run_bounded(
        awaitable: Awaitable[ToolMessage | Command[Any]],
        active: _ActiveDelegation,
    ) -> ToolMessage | Command[Any]:
        """Enforce wall and genuine-idle limits without killing active work."""

        task = asyncio.create_task(awaitable)
        started_at = time.monotonic()
        try:
            while True:
                wall_remaining = active.contract.limits.wall_clock_seconds - (
                    time.monotonic() - started_at
                )
                if wall_remaining <= 0:
                    raise _DelegationLimitExceeded("wall_clock_limit")
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=min(1.0, wall_remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    return await task
                if (
                    not active.active_operation
                    and time.monotonic() - active.last_activity_at
                    >= active.contract.limits.idle_seconds
                ):
                    raise _DelegationLimitExceeded("idle_limit")
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    def _declared_blocker(result: ToolMessage | Command[Any]) -> str | None:
        content = _tool_result_content(result).strip()
        if not content.startswith("{"):
            return None
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("status") != "blocked":
            return None
        question = str(payload.get("question_for_parent") or payload.get("summary") or "").strip()
        return question or "Subagent is blocked and requires parent review."

    @staticmethod
    def _envelope(
        contract: DelegationContract,
        result: ToolMessage | Command[Any] | None,
        *,
        status: str,
        reason: str | None = None,
    ) -> DelegationResultEnvelope:
        content = _tool_result_content(result) if result is not None else ""
        update = result.update if isinstance(result, Command) and isinstance(result.update, dict) else {}
        todos = _todos_from_state(update)
        completed = [str(item.get("id")) for item in todos if item.get("id") and item.get("status") == "completed"]
        remaining = [
            str(item.get("id"))
            for item in todos
            if item.get("id") and item.get("status") in {"pending", "in_progress"}
        ]
        if not todos:
            remaining = list(contract.todo_slice) if status != "completed" else []
        question = None
        if status == "blocked":
            question = DelegationControlMiddleware._declared_blocker(result) if result is not None else None
            question = question or content.strip()
        candidate_generation_ids = sorted(set(_SQL_GENERATION_RE.findall(content)))
        candidate_receipt_ids = sorted(set(_SQL_RECEIPT_RE.findall(content)))
        from graph.database_sql_revision_resume import database_sql_revision_resume_registry

        generation_ids = [
            generation_id
            for generation_id in candidate_generation_ids
            if database_sql_revision_resume_registry.get_generation(
                generation_id,
                session_id=contract.session_id,
                run_id=contract.parent_run_id,
                goal_id=contract.goal_id or "",
                goal_revision=contract.goal_revision,
            )
            is not None
        ]
        receipt_ids = [
            receipt_id
            for receipt_id in candidate_receipt_ids
            if database_sql_revision_resume_registry.get_validation_receipt(
                receipt_id,
                session_id=contract.session_id,
                run_id=contract.parent_run_id,
                goal_id=contract.goal_id or "",
                goal_revision=contract.goal_revision,
            )
            is not None
        ]
        generation_ids = sorted(
            set(generation_ids)
            | {
                item.id
                for item in database_sql_revision_resume_registry.list_generations(
                    session_id=contract.session_id,
                    run_id=contract.parent_run_id,
                    goal_id=contract.goal_id or "",
                    goal_revision=contract.goal_revision,
                    created_after=contract.created_at,
                )
            }
        )
        receipt_ids = sorted(
            set(receipt_ids)
            | {
                item.id
                for item in database_sql_revision_resume_registry.list_validation_receipts(
                    session_id=contract.session_id,
                    run_id=contract.parent_run_id,
                    goal_id=contract.goal_id or "",
                    goal_revision=contract.goal_revision,
                    created_after=contract.created_at,
                )
            }
        )
        activation_refs = [
            str(item.get("activation_id"))
            for item in update.get("verification_activations") or []
            if isinstance(item, dict) and item.get("activation_id")
        ]
        last_successful_action: str | None = None
        if session_manager.is_initialized and contract.session_id and contract.parent_run_id:
            run = session_manager.get_run_state(contract.session_id, contract.parent_run_id)
            events = run.get("delegation_events") if isinstance(run, dict) else []
            successful_events = [
                item
                for item in events or []
                if isinstance(item, dict)
                and item.get("subagent_run_id") == contract.subagent_run_id
                and item.get("status") == "completed"
            ]
            if successful_events:
                latest = max(successful_events, key=lambda item: float(item.get("timestamp") or 0))
                last_successful_action = str(
                    latest.get("tool") or latest.get("stage") or latest.get("type") or ""
                ) or None
        authoritative_database_handoff = bool(generation_ids or receipt_ids)
        return DelegationResultEnvelope(
            status=status,  # type: ignore[arg-type]
            subagent_run_id=contract.subagent_run_id,
            summary=(
                "Database evidence is available only through the registered generation and validation receipt IDs "
                "in this envelope. The subagent narrative was intentionally discarded; resolve exact values from "
                "the server-side Ledger instead of copying prose."
                if authoritative_database_handoff
                else content[:4000]
            ),
            completed_todo_ids=completed,
            remaining_todo_ids=remaining,
            evidence_refs=sorted(set(activation_refs)),
            sql_generation_ids=generation_ids,
            validation_receipt_ids=receipt_ids,
            question_for_parent=question,
            last_successful_action=last_successful_action,
            blocking_or_timeout_reason=reason,
            recommended_parent_action=(
                "continue_directly"
                if status in {"timed_out", "failed", "cancelled"}
                or (
                    status == "blocked"
                    and reason == "permission_denied"
                )
                else "ask_user"
                if status == "blocked"
                else "accept_result"
            ),
            retry_same_delegation_allowed=False,
        )

    @staticmethod
    def _persist_result(contract: DelegationContract, envelope: DelegationResultEnvelope) -> None:
        if not session_manager.is_initialized or not contract.session_id or not contract.parent_run_id:
            return
        session_manager.record_delegation_result(
            contract.session_id,
            contract.parent_run_id,
            envelope.model_dump(mode="json"),
        )

    @staticmethod
    def _completion_contract_failure(
        contract: DelegationContract,
        envelope: DelegationResultEnvelope,
    ) -> str | None:
        """Reject narrative-only database handoffs and unfinished delegated work."""

        if contract.expected_output_schema != "DatabaseEvidenceBatch/v1":
            return None
        failures: list[str] = []
        if not envelope.sql_generation_ids:
            failures.append("missing_registered_sql_generation")
        if not envelope.validation_receipt_ids:
            failures.append("missing_registered_validation_receipt")
        assigned = set(contract.todo_slice)
        completed = set(envelope.completed_todo_ids)
        remaining = set(envelope.remaining_todo_ids)
        incomplete = sorted((assigned - completed) | remaining)
        if incomplete:
            failures.append(f"incomplete_todos={','.join(incomplete)}")
        return ";".join(failures) or None

    def _finalize(
        self,
        request: ToolCallRequest,
        contract: DelegationContract,
        result: ToolMessage | Command[Any] | None,
        *,
        status: str,
        reason: str | None = None,
    ) -> ToolMessage | Command[Any]:
        envelope = self._envelope(contract, result, status=status, reason=reason)
        if status == "completed":
            completion_failure = self._completion_contract_failure(contract, envelope)
            if completion_failure:
                status = "failed"
                reason = f"delegation_contract_unsatisfied:{completion_failure}"
                envelope = self._envelope(contract, result, status=status, reason=reason)
        self._persist_result(contract, envelope)
        event_type = f"subagent_{status}"
        if status == "timed_out":
            event_type = "subagent_timed_out"
        self._event(
            request,
            contract,
            event_type,
            status=status,
            reason=reason,
            recommended_parent_action=envelope.recommended_parent_action,
            completed_todo_ids=envelope.completed_todo_ids,
            remaining_todo_ids=envelope.remaining_todo_ids,
        )
        content = envelope.model_dump_json()
        if result is None:
            return ToolMessage(
                content=content,
                tool_call_id=str(request.tool_call.get("id") or ""),
            )
        return _replace_tool_result(
            result,
            content=content,
            tool_call_id=str(request.tool_call.get("id") or ""),
        )

    @staticmethod
    def _permission_blocker(result: ToolMessage | Command[Any]) -> bool:
        content = _tool_result_content(result).strip()
        lowered = content.lower()
        if "permission_denied" in lowered or "permission required" in lowered:
            return True
        if not content.startswith("{"):
            return False
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return False
        return (
            isinstance(payload, dict)
            and str(payload.get("status") or "") in {
                "permission_required",
                "permission_denied",
            }
        )

    def _emit_parent_takeover_if_needed(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
    ) -> None:
        if _ACTIVE_DELEGATION.get() is not None or not session_manager.is_initialized:
            return
        context = _runtime_context(request)
        session_id = str(context.get("session_id") or "")
        run_id = str(context.get("run_id") or "")
        run = (
            session_manager.get_run_state(session_id, run_id)
            if session_id and run_id
            else None
        )
        if not isinstance(run, dict):
            return
        events = [
            item
            for item in run.get("delegation_events") or []
            if isinstance(item, dict)
        ]
        fallback_ids = {
            str(item.get("subagent_run_id") or "")
            for item in events
            if item.get("type") == "subagent_fallback_to_parent"
        }
        candidates = [
            item
            for item in run.get("delegation_results") or []
            if isinstance(item, dict)
            and item.get("status") in {"timed_out", "failed", "cancelled", "blocked"}
            and item.get("recommended_parent_action") == "continue_directly"
            and item.get("remaining_todo_ids")
            and str(item.get("subagent_run_id") or "") not in fallback_ids
        ]
        if not candidates:
            return
        result = max(candidates, key=lambda item: float(item.get("created_at") or 0))
        contract_raw = next(
            (
                item
                for item in run.get("delegation_contracts") or []
                if isinstance(item, dict)
                and str(item.get("subagent_run_id") or "")
                == str(result.get("subagent_run_id") or "")
            ),
            None,
        )
        if not isinstance(contract_raw, dict):
            return
        if not self._parent_work_started(
            request,
            result,
            remaining_todo_ids=[
                str(item)
                for item in result.get("remaining_todo_ids") or []
                if str(item)
            ],
        ):
            return
        contract = DelegationContract.model_validate(contract_raw)
        self._event(
            request,
            contract,
            "subagent_fallback_to_parent",
            status="running",
            reason=str(result.get("blocking_or_timeout_reason") or ""),
            recommended_parent_action="continue_directly",
            remaining_todo_ids=list(result.get("remaining_todo_ids") or []),
            parent_tool=str(request.tool_call.get("name") or ""),
            parent_tool_call_id=str(request.tool_call.get("id") or ""),
        )

    @staticmethod
    def _parent_work_started(
        request: ToolCallRequest,
        result: ToolMessage | Command[Any],
        *,
        remaining_todo_ids: list[str],
    ) -> bool:
        """Require one successful parent action before announcing takeover.

        A tool request alone is not evidence that the parent actually resumed
        work: permission denial, validation failure, or an exception may stop
        it before execution. The semantic link to the remaining Todo is carried
        by the pending delegation result; this gate proves the first concrete
        parent action returned successfully.
        """

        if isinstance(result, ToolMessage) and str(result.status or "") == "error":
            return False
        content = _tool_result_content(result).strip()
        if content.startswith("{"):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and str(
                payload.get("status") or ""
            ).lower() in {
                "error",
                "failed",
                "permission_required",
                "permission_denied",
                "blocked",
                "cancelled",
                "timed_out",
                "validation_failed",
                "conflict",
                "io_error",
            }:
                return False

        # A successful arbitrary tool call is not evidence that the parent is
        # taking over the delegated work. Require an explicit Todo lifecycle
        # operation against one of the IDs the subagent returned as remaining.
        if str(request.tool_call.get("name") or "") != "update_todos":
            return False
        remaining = set(remaining_todo_ids)
        if not remaining:
            return False
        operations = (request.tool_call.get("args") or {}).get("operations")
        return any(
            isinstance(operation, dict)
            and str(operation.get("todo_id") or "") in remaining
            and str(operation.get("action") or "")
            in {"start", "update", "complete", "reopen"}
            for operation in (operations if isinstance(operations, list) else [])
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if str(request.tool_call.get("name") or "") != "task":
            result = await handler(request)
            self._emit_parent_takeover_if_needed(request, result)
            return result
        contract = self._contract(request)
        if self._same_nonretryable_delegation_exists(contract):
            return self._finalize(
                request,
                contract,
                None,
                status="failed",
                reason="duplicate_timed_out_delegation; continue_directly",
            )
        self._persist_contract(contract)
        self._event(
            request,
            contract,
            "subagent_started",
            status="running",
            limits=contract.limits.model_dump(mode="json"),
        )
        self._event(
            request,
            contract,
            "context_mounted",
            status="completed",
            analytics_model_id=contract.selected_analytics_model,
            semantic_context_refs=contract.semantic_context_refs,
            skill_activation_ids=contract.allowed_skill_activations,
        )
        active = _ActiveDelegation(contract=contract, last_activity_at=time.monotonic())
        token: Token[_ActiveDelegation | None] = _ACTIVE_DELEGATION.set(active)
        try:
            result = await self._run_bounded(handler(request), active)
        except GraphInterrupt:
            self._event(
                request,
                contract,
                "subagent_waiting_for_permission",
                status="waiting_for_permission",
            )
            raise
        except _DelegationLimitExceeded as exc:
            return self._finalize(
                request,
                contract,
                None,
                status="timed_out",
                reason=exc.reason,
            )
        except asyncio.CancelledError:
            self._finalize(request, contract, None, status="cancelled", reason="parent_cancelled")
            raise
        except Exception as exc:
            return self._finalize(
                request,
                contract,
                None,
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
            )
        finally:
            _ACTIVE_DELEGATION.reset(token)
        blocker = self._declared_blocker(result)
        if blocker:
            return self._finalize(request, contract, result, status="blocked", reason="question_for_parent")
        if self._permission_blocker(result):
            return self._finalize(
                request,
                contract,
                result,
                status="blocked",
                reason="permission_denied",
            )
        return self._finalize(request, contract, result, status="completed")

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        if str(request.tool_call.get("name") or "") != "task":
            result = handler(request)
            self._emit_parent_takeover_if_needed(request, result)
            return result
        contract = self._contract(request)
        if self._same_nonretryable_delegation_exists(contract):
            return self._finalize(
                request,
                contract,
                None,
                status="failed",
                reason="duplicate_timed_out_delegation; continue_directly",
            )
        self._persist_contract(contract)
        self._event(request, contract, "subagent_started", status="running")
        self._event(
            request,
            contract,
            "context_mounted",
            status="completed",
            analytics_model_id=contract.selected_analytics_model,
            semantic_context_refs=contract.semantic_context_refs,
            skill_activation_ids=contract.allowed_skill_activations,
        )
        active = _ActiveDelegation(contract=contract, last_activity_at=time.monotonic())
        token = _ACTIVE_DELEGATION.set(active)
        try:
            result = handler(request)
        except GraphInterrupt:
            self._event(
                request,
                contract,
                "subagent_waiting_for_permission",
                status="waiting_for_permission",
            )
            raise
        except Exception as exc:
            return self._finalize(
                request,
                contract,
                None,
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
            )
        finally:
            _ACTIVE_DELEGATION.reset(token)
        blocker = self._declared_blocker(result)
        if blocker:
            return self._finalize(
                request,
                contract,
                result,
                status="blocked",
                reason="question_for_parent",
            )
        if self._permission_blocker(result):
            return self._finalize(
                request,
                contract,
                result,
                status="blocked",
                reason="permission_denied",
            )
        return self._finalize(request, contract, result, status="completed")


class SubagentProgressMiddleware(AgentMiddleware[Any, Any, Any]):
    """Emit nested model/tool progress for the currently active delegation."""

    @staticmethod
    def _emit(runtime: Any, event_type: str, **payload: Any) -> None:
        active = _ACTIVE_DELEGATION.get()
        if active is None:
            return
        active.last_activity_at = time.monotonic()
        active.event_sequence += 1
        event = {
            "type": event_type,
            "event_id": f"{active.contract.subagent_run_id}:{event_type}:{active.event_sequence}",
            "subagent_run_id": active.contract.subagent_run_id,
            "parent_run_id": active.contract.parent_run_id,
            "subagent_type": active.contract.subagent_type,
            "objective": active.contract.objective,
            "sequence": active.event_sequence,
            "timestamp": time.time(),
            **payload,
        }
        if session_manager.is_initialized:
            try:
                session_manager.record_delegation_event(
                    active.contract.session_id,
                    active.contract.parent_run_id,
                    event,
                )
            except (FileNotFoundError, RuntimeError, ValueError):
                pass
        writer = getattr(runtime, "stream_writer", None)
        if writer is not None:
            writer(event)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        active = _ACTIVE_DELEGATION.get()
        if active is not None:
            active.model_calls += 1
            if active.model_calls > active.contract.limits.model_calls:
                raise _DelegationLimitExceeded("model_call_limit")
            active.active_operation = "model"
        self._emit(request.runtime, "subagent_stage_changed", status="running", stage="model")
        try:
            return await handler(request)
        finally:
            if active is not None:
                active.active_operation = ""

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        active = _ACTIVE_DELEGATION.get()
        tool_name = str(request.tool_call.get("name") or "")
        tool_call_id = str(request.tool_call.get("id") or "")
        if active is not None:
            active.tool_calls += 1
            if active.tool_calls > active.contract.limits.tool_calls:
                raise _DelegationLimitExceeded("tool_call_limit")
            active.active_operation = f"tool:{tool_name}"
        self._emit(
            request.runtime,
            "subagent_tool_started",
            status="running",
            tool=tool_name,
            tool_call_id=tool_call_id,
        )
        try:
            result = await handler(request)
        except Exception as exc:
            self._emit(
                request.runtime,
                "subagent_tool_failed",
                status="failed",
                tool=tool_name,
                tool_call_id=tool_call_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if active is not None:
                active.active_operation = ""
        self._emit(
            request.runtime,
            "subagent_tool_completed",
            status="completed",
            tool=tool_name,
            tool_call_id=tool_call_id,
        )
        return result


def delegation_contract_fingerprint(contract: DelegationContract) -> str:
    """Stable semantic fingerprint used by adversarial repeat tests."""

    payload = {
        "parent_run_id": contract.parent_run_id,
        "subagent_type": contract.subagent_type,
        "objective": " ".join(contract.objective.lower().split()),
        "todo_slice": sorted(contract.todo_slice),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
