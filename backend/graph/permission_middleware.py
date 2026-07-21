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
    """Interrupt unauthorized external-file reads and exact-file writes."""

    _VIRTUAL_PREFIXES = (
        "/workspace/",
        "/knowledge/",
        "/semantic-assets/",
        "/sql-guardrails/",
        "/analytics-models/",
        "/skills/",
        "/large_tool_results/",
        "/scratch/",
    )

    @classmethod
    def _external_write_path(cls, raw_path: str, workspace_path: str) -> Path | None:
        normalized = raw_path.replace("\\", "/")
        if normalized.startswith(cls._VIRTUAL_PREFIXES):
            return None
        requested = Path(raw_path).expanduser()
        if not requested.is_absolute():
            return None
        resolved = requested.resolve()
        if workspace_path:
            workspace = Path(workspace_path).expanduser().resolve()
            try:
                resolved.relative_to(workspace)
                return None
            except ValueError:
                pass
        return resolved

    @staticmethod
    def _change_preview(tool_name: str, args: dict[str, Any]) -> dict[str, str]:
        if tool_name == "edit_file":
            values = {
                "old_string": str(args.get("old_string") or ""),
                "new_string": str(args.get("new_string") or ""),
            }
        elif tool_name == "patch_file":
            values = {
                "expected_sha256": str(args.get("expected_sha256") or ""),
                "replacements": str(args.get("replacements") or ""),
            }
        elif tool_name == "commit_external_artifact":
            values = {
                "lease_id": str(args.get("lease_id") or ""),
                "expected_source_sha256": str(args.get("expected_source_sha256") or ""),
            }
        else:
            values = {"content": str(args.get("content") or "")}
        return {key: value[:1000] + ("...[truncated]" if len(value) > 1000 else "") for key, value in values.items()}

    @staticmethod
    def _directory_change_preview(session_id: str, args: dict[str, Any]) -> dict[str, str]:
        lease = session_manager.get_external_directory_lease(
            session_id,
            str(args.get("lease_id") or ""),
        )
        plan = lease.get("commit_plan") if isinstance(lease, dict) else None
        if not isinstance(plan, dict):
            return {"状态": "尚未生成目录变更计划，提交将被拒绝。"}

        def lines(key: str) -> str:
            values = plan.get(key)
            if not isinstance(values, list) or not values:
                return "无"
            rendered = [str(item) for item in values[:100]]
            if len(values) > 100:
                rendered.append(f"…另有 {len(values) - 100} 项")
            return "\n".join(rendered)

        return {
            "新增文件": lines("added"),
            "修改文件": lines("modified"),
            "删除文件": lines("deleted"),
            "变更统计": (
                f"新增 {len(plan.get('added') or [])}，修改 {len(plan.get('modified') or [])}，"
                f"删除 {len(plan.get('deleted') or [])}"
            ),
        }

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
        run_id = str(context.get("run_id") or "")
        workspace_path = str(context.get("workspace_path") or "")
        if not session_id:
            return None

        for tool_call in last_ai_msg.tool_calls:
            tool_name = str(tool_call.get("name") or "")
            if tool_name not in {
                "read_external_file",
                "read_resource",
                "read_file",
                "edit_file",
                "write_file",
                "inspect_file_version",
                "patch_file",
                "stage_external_artifact",
                "commit_external_artifact",
                "stage_external_directory",
                "commit_external_directory",
            }:
                continue
            args = tool_call.get("args") or {}
            raw_path = str(
                args.get("path") or args.get("resource") or args.get("file_path") or args.get("directory_path") or ""
            ).strip()
            if not raw_path:
                continue
            if raw_path.startswith("att_"):
                continue
            if raw_path.replace("\\", "/").startswith(self._VIRTUAL_PREFIXES):
                continue

            if tool_name in {"stage_external_directory", "commit_external_directory"}:
                requested = Path(raw_path).expanduser().resolve()
                access = "write" if tool_name == "commit_external_directory" else "read"
                if session_manager.has_external_directory_permission(
                    session_id,
                    requested,
                    access=access,
                    run_id=run_id,
                ):
                    continue
                change_preview = (
                    self._directory_change_preview(session_id, args)
                    if access == "write"
                    else {
                        "授权范围": "当前 Run 递归只读",
                        "安全说明": "目录将复制为 Docker /scratch 快照；不会直接挂载或修改原目录。",
                    }
                )
                request = permission_resume_registry.create_external_directory_request(
                    session_id=session_id,
                    query_id=query_id,
                    run_id=run_id,
                    tool_call_id=str(tool_call.get("id") or ""),
                    path=requested,
                    access=access,
                    operation=tool_name,
                    change_preview=change_preview,
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
                                "type": request["type"],
                                "target_kind": "exact_directory",
                                "capabilities": request["capabilities"],
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
                # LangGraph resumes by re-running this middleware after the
                # API records the exact Run-scoped directory grant.
                return None

            if tool_name in {"edit_file", "write_file", "patch_file", "commit_external_artifact"}:
                requested = self._external_write_path(raw_path, workspace_path)
                if requested is None:
                    continue
                access = "write"
                if session_manager.has_external_file_write_permission(session_id, requested):
                    continue
                change_preview = self._change_preview(tool_name, args)
            else:
                requested = Path(raw_path).expanduser().resolve()
                if tool_name == "read_file":
                    if not Path(raw_path).expanduser().is_absolute():
                        continue
                    if workspace_path:
                        workspace = Path(workspace_path).expanduser().resolve()
                        try:
                            requested.relative_to(workspace)
                            continue
                        except ValueError:
                            pass
                access = "read"
                if session_manager.has_external_file_read_permission(session_id, requested):
                    continue
                change_preview = None

            request = permission_resume_registry.create_external_file_request(
                session_id=session_id,
                query_id=query_id,
                tool_call_id=str(tool_call.get("id") or ""),
                path=requested,
                access=access,
                operation=tool_name,
                change_preview=change_preview,
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
                            "type": request["type"],
                            "target_kind": "exact_file",
                            "capabilities": request["capabilities"],
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
