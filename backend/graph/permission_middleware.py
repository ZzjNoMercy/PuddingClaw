"""Permission middleware for PuddingClaw-owned HITL interrupts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ContextT, ResponseT, StateT
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from graph.effective_grants import EffectiveGrantSet
from graph.host_read_policy import is_sensitive_host_read_path
from graph.managed_paths import is_managed_resource_path
from graph.permission_policy import RunPermissionContext
from graph.permission_resume import permission_resume_registry
from graph.session_manager import session_manager
from graph.trace_collector import get_current_trace_collector
from graph.virtual_paths import PathAuthority, classify_path_authority, is_virtual_path


class ExternalFilePermissionMiddleware(AgentMiddleware[StateT, ContextT, ResponseT]):
    """Interrupt unauthorized external-file reads and exact-file writes."""

    def __init__(
        self,
        *,
        backend_mode: str = "kernel",
        approval_mode: str = "strict",
    ) -> None:
        # Spawn is a host execution product mode, not a workspace sandbox.
        # Keep the default fail-closed for isolated tests and callers that do
        # not provide an execution snapshot; production always passes the
        # selected backend mode explicitly.
        self.backend_mode = str(backend_mode or "kernel")
        self.approval_mode = str(approval_mode or "strict")

    @property
    def _spawn_host_reads_enabled(self) -> bool:
        # Preserve the legacy Strict Spawn contract. Smart uses the shared
        # ordinary/sensitive read boundary below for both Spawn and Kernel.
        return self.backend_mode == "spawn" and self.approval_mode != "smart"

    @property
    def _smart_host_reads_enabled(self) -> bool:
        return (
            self.approval_mode == "smart"
            and self.backend_mode in {"spawn", "kernel"}
        )

    @classmethod
    def _external_write_path(cls, raw_path: str, workspace_path: str) -> Path | None:
        classified = classify_path_authority(
            raw_path,
            workspace_root=workspace_path or None,
        )
        if classified.authority is not PathAuthority.EXTERNAL:
            return None
        return classified.canonical_host_path

    @classmethod
    def _external_read_path(cls, raw_path: str, workspace_path: str) -> Path | None:
        """Return a host path only when external read authority is required."""
        requested = cls._external_write_path(raw_path, workspace_path)
        if requested is None:
            return None

        # The configured knowledge root and attachment store are
        # PuddingClaw-managed read-only resources. Their physical host paths
        # may live outside the active workspace, but reading them must have the
        # same authority as their virtual `/knowledge` / attachment aliases.
        # Keep this exception read-only: writes continue through
        # `_external_write_path` and therefore still require explicit consent.
        base_dir = Path(__file__).resolve().parent.parent
        if is_managed_resource_path(requested, base_dir):
            return None
        return requested

    @staticmethod
    def _change_preview(tool_name: str, args: dict[str, Any]) -> dict[str, str]:
        if tool_name == "edit_file":
            values = {
                "old_string": str(args.get("old_string") or ""),
                "new_string": str(args.get("new_string") or ""),
            }
        elif tool_name in {"patch_file", "patch_files", "replace_file"}:
            values = {
                "expected_sha256": str(args.get("expected_sha256") or ""),
                "replacements": str(args.get("replacements") or ""),
            }
            if tool_name == "replace_file":
                values["content_sha256"] = hashlib.sha256(
                    str(args.get("content") or "").encode("utf-8")
                ).hexdigest()
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
        elif tool_name == "materialize_source_ref":
            destination = (
                args.get("destination")
                if isinstance(args.get("destination"), dict)
                else {}
            )
            values = {
                "source_ref": str(args.get("source_ref") or ""),
                "renderer": str(args.get("renderer") or ""),
                "destination_kind": str(destination.get("kind") or ""),
                "mode": str(
                    destination.get("mode")
                    or destination.get("output_mode")
                    or ""
                ),
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

        run = session_manager.get_run_state(session_id, run_id)
        if not isinstance(run, dict):
            return False
        current_bindings = RunPermissionContext.from_config_snapshot(run.get("config_snapshot")).grant_bindings()
        grants, grants_revision = session_manager.permission_grants_snapshot(session_id)
        effective = EffectiveGrantSet.resolve(
            grants,
            run_id=run_id,
            current_bindings=current_bindings,
            current_shell_bindings=RunPermissionContext.from_config_snapshot(
                run.get("config_snapshot")
            ).shell_grant_bindings(),
            permission_revision=grants_revision,
        )
        return effective.allows_directory(
            requested,
            access=access,
            required_capabilities=((required_capability,) if required_capability else ()),
        )

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

    @staticmethod
    def _is_declared_artifact_target(
        session_id: str,
        run_id: str,
        requested: Path,
    ) -> bool:
        run = session_manager.get_run_state(session_id, run_id)
        targets = (
            run.get("declared_artifact_targets")
            if isinstance(run, dict)
            else None
        )
        for raw_target in targets or []:
            try:
                candidate = Path(str(raw_target)).expanduser().resolve()
            except OSError:
                continue
            if candidate == requested:
                return True
        return False

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
                "copy_file",
                "materialize_source_ref",
                "replace_file",
                "patch_file",
                "patch_files",
                "delete_file",
                "execute_external_directory",
                "validate_html_report",
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
            if tool_name == "copy_file":
                source_raw = str(args.get("source_path") or "").strip()
                target_raw = str(args.get("target_path") or "").strip()
                if not source_raw or not target_raw:
                    continue
                source = self._external_read_path(source_raw, workspace_path)
                target = self._external_write_path(target_raw, workspace_path)
                source_granted = source is None or self._spawn_host_reads_enabled or (
                    self._smart_host_reads_enabled
                    and not is_sensitive_host_read_path(source)
                ) or (
                    session_manager.has_external_file_read_permission(session_id, source)
                    or self._has_directory_permission_for_path(
                        session_id, source, access="read", run_id=run_id
                    )
                )
                if not source_granted:
                    assert source is not None
                    request = permission_resume_registry.create_external_file_request(
                        session_id=session_id,
                        query_id=query_id,
                        tool_call_id=str(tool_call.get("id") or ""),
                        path=source,
                        access="read",
                        operation=tool_name,
                    )
                    interrupt(
                        {
                            "type": "permission_request",
                            "request": request,
                            "decisions": [{"type": "approve"}, {"type": "reject"}],
                        }
                    )
                    return None
                if target is None or (
                    session_manager.has_external_file_write_permission(
                        session_id,
                        target,
                    )
                    or self._has_directory_permission_for_path(
                        session_id,
                        target,
                        access="write",
                        run_id=run_id,
                    )
                    or self._is_declared_artifact_target(
                        session_id,
                        run_id,
                        target,
                    )
                ):
                    continue
                request = permission_resume_registry.create_external_file_request(
                    session_id=session_id,
                    query_id=query_id,
                    tool_call_id=str(tool_call.get("id") or ""),
                    path=target,
                    access="write",
                    operation=tool_name,
                    change_preview={
                        "source_path": source_raw,
                        "mode": "atomic_create_only",
                    },
                )
                interrupt(
                    {
                        "type": "permission_request",
                        "request": request,
                        "decisions": [{"type": "approve"}, {"type": "reject"}],
                    }
                )
                return None
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
                        or self._is_declared_artifact_target(
                            session_id,
                            run_id,
                            requested,
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
            destination = (
                args.get("destination")
                if isinstance(args.get("destination"), dict)
                else {}
            )
            raw_path = str(
                args.get("path")
                or args.get("resource")
                or args.get("file_path")
                or args.get("html_file_path")
                or args.get("directory_path")
                or destination.get("target_path")
                or destination.get("output_path")
                or ""
            ).strip()
            if tool_name == "validate_html_report" and raw_path:
                html_path = Path(raw_path).expanduser().resolve()
                if workspace_path:
                    workspace = Path(workspace_path).expanduser().resolve()
                    try:
                        html_path.relative_to(workspace)
                        continue
                    except ValueError:
                        pass
                raw_path = str(html_path.parent)
            if not raw_path:
                continue
            if raw_path.startswith("att_"):
                continue
            if is_virtual_path(raw_path):
                continue

            if tool_name in {
                "stage_external_directory",
                "commit_external_directory",
                "execute_external_directory",
                "validate_html_report",
                "grep",
                "glob",
                "ls",
            }:
                requested = Path(raw_path).expanduser().resolve()
                access = (
                    "write"
                    if tool_name == "commit_external_directory"
                    or (
                        tool_name == "execute_external_directory"
                        and str(args.get("mode") or "read_only")
                        == "writable_draft"
                    )
                    else "read"
                )
                if (
                    access == "read"
                    and self._spawn_host_reads_enabled
                ):
                    continue
                if (
                    access == "read"
                    and tool_name in {"grep", "glob", "ls"}
                    and is_managed_resource_path(
                        requested,
                        Path(__file__).resolve().parent.parent,
                    )
                ):
                    # Absolute aliases of the configured knowledge directory
                    # are as trusted for read-only discovery as `/knowledge`.
                    # Execution/staging tools intentionally remain permission
                    # gated even when their source happens to be managed.
                    continue
                deletion_required = False
                if tool_name == "commit_external_directory":
                    lease = session_manager.get_external_directory_lease(
                        session_id,
                        str(args.get("lease_id") or ""),
                    )
                    plan = (
                        lease.get("commit_plan")
                        if isinstance(lease, dict)
                        else None
                    )
                    deletion_required = bool(
                        plan.get("deleted")
                        if isinstance(plan, dict)
                        else False
                    )
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
                    else self._has_directory_permission_for_path(
                        session_id,
                        requested,
                        access="write",
                        run_id=run_id,
                        required_capability="delete",
                    )
                    if access == "write" and deletion_required
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
                                (
                                    "命令只写服务端隔离草稿；原目录不以可写方式挂载。"
                                    "执行后生成差异计划，删除项仍需二次批准。"
                                    if access == "write"
                                    else "目录只读挂载到一次性 docker run --rm，命令结束即销毁。"
                                )
                                if tool_name == "execute_external_directory"
                                else (
                                    "目录只读授权仅用于验证目标及其本地资源；普通模式 "
                                    "只做结构与本地引用检查，只有合同明确要求 "
                                    "E2E 时才启动平台固定的离线 Chromium，且不授予模型 "
                                    "shell 能力。"
                                    if tool_name == "validate_html_report"
                                    else "授权后由 HostFileBroker 直接重放原文件搜索；不会授予 shell 访问。"
                                )
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
                    require_delete=deletion_required,
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
                "materialize_source_ref",
                "replace_file",
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
                if (
                    exact_granted
                    or directory_granted
                    or (
                        access == "write"
                        and self._is_declared_artifact_target(
                            session_id,
                            run_id,
                            requested,
                        )
                    )
                ):
                    continue
                change_preview = self._change_preview(tool_name, args)
            else:
                requested = self._external_read_path(raw_path, workspace_path)
                if requested is None:
                    continue
                access = "read"
                if self._spawn_host_reads_enabled or (
                    self._smart_host_reads_enabled
                    and not is_sensitive_host_read_path(requested)
                ):
                    continue
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
