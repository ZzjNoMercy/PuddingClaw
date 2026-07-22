"""Permission middleware for PuddingClaw-owned HITL interrupts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ContextT, ResponseT, StateT
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from graph.permission_policy import RunPermissionContext
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
        elif tool_name in {"patch_file", "patch_files"}:
            values = {
                "expected_sha256": str(args.get("expected_sha256") or ""),
                "replacements": str(args.get("replacements") or ""),
            }
        elif tool_name == "delete_file":
            values = {
                "expected_sha256": str(args.get("expected_sha256") or ""),
                "risk": "Delete one exact file; directory and bulk deletion are not permitted.",
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
    def _has_directory_permission_for_path(
        session_id: str,
        requested: Path,
        *,
        access: str,
        run_id: str,
        required_capability: str | None = None,
    ) -> bool:
        """Honor one exact-directory Grant for its normalized descendants."""

        for grant in session_manager.list_permission_grants(session_id):
            if grant.get("type") != f"external_directory_{access}":
                continue
            if required_capability and required_capability not in (
                grant.get("capabilities") or []
            ):
                continue
            target = grant.get("target")
            if not isinstance(target, str) or not target:
                continue
            root = Path(target).expanduser().resolve()
            try:
                requested.relative_to(root)
            except ValueError:
                continue
            if session_manager.has_external_directory_permission(
                session_id,
                root,
                access=access,
                run_id=run_id,
            ):
                return True
        return False

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

    @staticmethod
    def _automatic_directory_request_allowed(
        session_id: str,
        requested: Path,
    ) -> bool:
        """Keep Agent-initiated directory escalation narrow and non-sensitive.

        A user can still select an exact directory in the UI. This guard only
        prevents a file-tool call from silently turning an exact-file grant
        into a broad Home/credential/temp-cache prompt.
        """

        try:
            canonical = requested.expanduser().resolve(strict=True)
        except OSError:
            return False
        home = Path.home().resolve()
        broad_roots = {
            Path("/").resolve(),
            home,
            home.parent,
        }
        if canonical in broad_roots:
            return False
        parts = canonical.parts
        sensitive_names = {".ssh", ".gnupg", ".aws", ".azure", ".kube"}
        if any(part in sensitive_names for part in parts):
            return False
        normalized = canonical.as_posix()
        if "/.codex/attachments/" in f"{normalized.rstrip('/')}/":
            return False
        if (
            normalized.startswith(("/private/var/folders/", "/var/folders/"))
            and canonical.name == "T"
        ):
            return False

        # If this request broadens an existing exact-file grant, only the
        # direct parent is an admissible sibling-discovery root.
        for grant in session_manager.list_permission_grants(session_id):
            if grant.get("target_kind") != "exact_file":
                continue
            target = grant.get("target")
            if not isinstance(target, str) or not target:
                continue
            try:
                exact_file = Path(target).expanduser().resolve(strict=True)
                exact_file.relative_to(canonical)
            except (OSError, ValueError):
                continue
            if exact_file.parent != canonical:
                return False
        return True

    @staticmethod
    def _trace_permission_reuse(
        *,
        tool_name: str,
        target: Path,
        access: str,
    ) -> None:
        collector = get_current_trace_collector()
        if collector is None:
            return
        collector.add_custom_span(
            "permission.reused",
            {
                "operation": tool_name,
                "target": str(target),
                "access": access,
            },
            span_type="permission",
            metadata={
                "permission": {
                    "target_kind": "exact_directory",
                    "capabilities": [access, "recursive", "external_path"],
                    "outcome": "reused",
                }
            },
        )

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
                "patch_files",
                "delete_file",
                "execute_external_directory",
                "stage_external_artifact",
                "commit_external_artifact",
                "stage_external_directory",
                "commit_external_directory",
                "execute_external_directory",
                "grep",
                "glob",
                "ls",
            }:
                continue
            args = tool_call.get("args") or {}
            if tool_name == "patch_files":
                for item in args.get("files") or []:
                    if not isinstance(item, dict):
                        continue
                    requested = self._external_write_path(
                        str(item.get("file_path") or ""),
                        workspace_path,
                    )
                    if requested is None or (
                        session_manager.has_external_file_write_permission(
                            session_id,
                            requested,
                        )
                        or self._has_directory_permission_for_path(
                            session_id,
                            requested,
                            access="write",
                            run_id=run_id,
                        )
                    ):
                        continue
                    request = permission_resume_registry.create_external_file_request(
                        session_id=session_id,
                        query_id=query_id,
                        tool_call_id=str(tool_call.get("id") or ""),
                        path=requested,
                        access="write",
                        operation=tool_name,
                        change_preview=self._change_preview(tool_name, item),
                    )
                    interrupt(
                        {
                            "type": "permission_request",
                            "request": request,
                            "decisions": [
                                {"type": "approve"},
                                {"type": "reject"},
                            ],
                        }
                    )
                    return None
                continue
            raw_path = str(
                args.get("path") or args.get("resource") or args.get("file_path") or args.get("directory_path") or ""
            ).strip()
            if not raw_path:
                continue
            if raw_path.startswith("att_"):
                continue
            if raw_path.replace("\\", "/").startswith(self._VIRTUAL_PREFIXES):
                continue

            if tool_name in {
                "stage_external_directory",
                "commit_external_directory",
                "grep",
                "glob",
                "ls",
            }:
                requested = Path(raw_path).expanduser().resolve()
                access = "write" if tool_name == "commit_external_directory" else "read"
                if (
                    tool_name == "grep"
                    and requested.is_file()
                    and session_manager.has_external_file_read_permission(session_id, requested)
                ):
                    # A file Grant may authorize grep only when its path is an
                    # existing exact file. Directory grep is discovery and
                    # must continue through the exact-directory HITL path.
                    self._trace_permission_reuse(
                        tool_name=tool_name,
                        target=requested,
                        access="read",
                    )
                    continue
                permission_exists = (
                    self._has_directory_permission_for_path(
                        session_id,
                        requested,
                        access=access,
                        run_id=run_id,
                    )
                    if tool_name in {"grep", "glob", "ls"}
                    else session_manager.has_external_directory_permission(
                        session_id,
                        requested,
                        access=access,
                        run_id=run_id,
                    )
                )
                if permission_exists:
                    self._trace_permission_reuse(
                        tool_name=tool_name,
                        target=requested,
                        access=access,
                    )
                    continue
                if not self._automatic_directory_request_allowed(
                    session_id,
                    requested,
                ):
                    # Let WorkspacePathRouter return a deterministic
                    # permission_required result. Do not create an unsafe HITL
                    # card merely because the model guessed a broad ancestor.
                    continue
                change_preview = (
                    self._directory_change_preview(session_id, args)
                    if access == "write"
                    else {
                        "授权范围": "可选择本 Session 或当前 Run 的 exact-directory 递归只读",
                        "安全说明": (
                            "目录将复制为 Docker /scratch 快照；不会直接挂载或修改原目录。"
                            if tool_name == "stage_external_directory"
                            else (
                                "授权后仍需单独批准精确命令；目录只读挂载到一次性 docker run --rm，命令结束即销毁。"
                                if tool_name == "execute_external_directory"
                                else "授权后由 HostFileBroker 直接重放原文件搜索；不会授予 shell 访问。"
                            )
                        ),
                    }
                )
                run_state = session_manager.get_run_state(session_id, run_id)
                grant_bindings = (
                    RunPermissionContext.from_config_snapshot(
                        run_state.get("config_snapshot")
                    ).grant_bindings()
                    if isinstance(run_state, dict)
                    else None
                )
                request = permission_resume_registry.create_external_directory_request(
                    session_id=session_id,
                    query_id=query_id,
                    run_id=run_id,
                    tool_call_id=str(tool_call.get("id") or ""),
                    path=requested,
                    access=access,
                    operation=tool_name,
                    grant_bindings=grant_bindings,
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

            if tool_name in {
                "edit_file",
                "write_file",
                "patch_file",
                "delete_file",
                "commit_external_artifact",
            }:
                requested = self._external_write_path(raw_path, workspace_path)
                if requested is None:
                    continue
                access = "delete" if tool_name == "delete_file" else "write"
                exact_granted = (
                    session_manager.has_external_file_delete_permission(
                        session_id,
                        requested,
                    )
                    if access == "delete"
                    else session_manager.has_external_file_write_permission(
                        session_id,
                        requested,
                    )
                )
                directory_granted = self._has_directory_permission_for_path(
                    session_id,
                    requested,
                    access="write",
                    run_id=run_id,
                    required_capability="delete" if access == "delete" else None,
                )
                if exact_granted or directory_granted:
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
                if (
                    session_manager.has_external_file_read_permission(session_id, requested)
                    or self._has_directory_permission_for_path(
                        session_id,
                        requested,
                        access="read",
                        run_id=run_id,
                    )
                ):
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
