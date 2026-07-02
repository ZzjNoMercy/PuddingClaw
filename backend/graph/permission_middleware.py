"""Permission middleware for PuddingClaw-owned HITL interrupts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ContextT, ResponseT, StateT
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from graph.trace_collector import get_current_trace_collector


class ExternalFilePermissionMiddleware(AgentMiddleware[StateT, ContextT, ResponseT]):
    """Interrupt only unauthorized external-file reads."""

    def after_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        messages = state["messages"]
        if not messages:
            return None

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        context = runtime.context if isinstance(runtime.context, dict) else {}
        session_id = str(context.get("session_id") or "")
        query_id = str(context.get("query_id") or "")
        if not session_id:
            return None

        for tool_call in last_ai_msg.tool_calls:
            if tool_call.get("name") not in {"read_external_file", "read_resource"}:
                continue
            args = tool_call.get("args") or {}
            raw_path = str(args.get("path") or args.get("resource") or "").strip()
            if not raw_path:
                continue
            if raw_path.startswith("att_"):
                continue

            requested = Path(raw_path).expanduser().resolve()
            if session_manager.has_external_file_read_permission(session_id, requested):
                continue

            request = permission_resume_registry.create_external_file_request(
                session_id=session_id,
                query_id=query_id,
                tool_call_id=str(tool_call.get("id") or ""),
                path=requested,
            )
            collector = get_current_trace_collector()
            if collector is not None:
                collector.add_custom_span(
                    "permission.request",
                    {"request": request},
                    span_type="permission",
                    metadata={
                        "harness": {
                            "mechanism": "permission",
                            "pillars": [{"name": "architectural_constraints", "role": "primary"}],
                        },
                        "permission": {
                            "request_id": request["id"],
                            "type": "external_file_read",
                            "target_kind": "exact_file",
                            "capabilities": ["read", "external_path"],
                            "outcome": "needs_user",
                        },
                    },
                )

            interrupt(
                {
                    "type": "permission_request",
                    "request": request,
                    "decisions": [{"type": "approve"}, {"type": "reject"}],
                }
            )
            # LangGraph re-runs this middleware after resume. The grant written
            # by PuddingClaw's permission API lets the second pass fall through.
            return None

        return None
