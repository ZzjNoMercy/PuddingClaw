"""In-process pending permission decisions for active Agent streams."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graph.permission_policy import PermissionBindingPolicy
from observability import emit_harness_metric

if TYPE_CHECKING:
    from harness.shell_access import ShellAccessPlan

logger = logging.getLogger(__name__)


class PermissionResumeRegistry:
    """Bridge permission API decisions back into active LangGraph streams."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._requests: dict[str, dict[str, Any]] = {}

    @staticmethod
    def tool_action_fingerprint(
        *,
        tool_name: str,
        command: str,
        reason: str,
    ) -> str:
        # Preserve whitespace inside quoted/script content. Collapsing all
        # whitespace can make semantically different commands share a grant.
        normalized = command.strip()
        return (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "tool": tool_name,
                        "command": normalized,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        )

    def create_external_file_request(
        self,
        *,
        session_id: str,
        query_id: str,
        tool_call_id: str,
        path: Path,
        access: str = "read",
        operation: str = "",
        change_preview: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if access not in {"read", "write", "delete"}:
            raise ValueError(f"Unsupported external file access: {access}")
        request_id = f"perm-req-{uuid.uuid4().hex[:12]}"
        request_type = f"external_file_{access}"
        request = {
            "id": request_id,
            "type": request_type,
            "session_id": session_id,
            "query_id": query_id,
            "tool_call_id": tool_call_id,
            "path": str(path),
            "target_kind": "exact_file",
            "capabilities": [access, "external_path"],
            "status": "pending",
            "created_at": time.time(),
            "options": (
                ["exact_file_session", "all_external_files_session"]
                if access == "read"
                else ["exact_file_session"]
            ),
        }
        if operation:
            request["operation"] = operation
        if change_preview:
            request["change_preview"] = change_preview
        self._requests[request_id] = request
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        emit_harness_metric(
            logger,
            "permission_prompt_count",
            session_id=session_id,
            type=request_type,
            target=str(path),
        )
        return dict(request)

    def create_external_directory_request(
        self,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        tool_call_id: str,
        path: Path,
        access: str,
        operation: str,
        require_delete: bool = False,
        grant_bindings: dict[str, Any] | None = None,
        change_preview: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create one exact-directory request, deduplicated within a Run."""

        if access not in {"read", "write"}:
            raise ValueError(f"Unsupported external directory access: {access}")
        replay_key = "\0".join(
            [
                session_id,
                query_id,
                run_id,
                access,
                "delete" if require_delete else "no-delete",
                str(path),
            ]
        )
        request_id = f"perm-req-{hashlib.sha256(replay_key.encode('utf-8')).hexdigest()[:16]}"
        semantic_payload = {
            "session_id": session_id,
            "access": access,
            "path": str(path),
            "grant_bindings": dict(grant_bindings or {}),
        }
        semantic_key = "sha256:" + hashlib.sha256(
            json.dumps(
                semantic_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = self._requests.get(request_id)
        if existing is not None and existing.get("status") == "pending":
            emit_harness_metric(
                logger,
                "permission_reuse_count",
                session_id=session_id,
                scope="pending_request",
            )
            return dict(existing)
        if existing is not None:
            # A revoked/expired capability can legitimately require a new
            # decision inside the same Run. Never resurrect a resolved request.
            request_id = f"{request_id}-{uuid.uuid4().hex[:6]}"
        request = {
            "id": request_id,
            "type": f"external_directory_{access}",
            "session_id": session_id,
            "query_id": query_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "path": str(path),
            "target_kind": "exact_directory",
            "capabilities": [
                access,
                *(["delete"] if access == "write" and require_delete else []),
                "recursive",
                "external_path",
            ],
            "status": "pending",
            "created_at": time.time(),
            "operation": operation,
            "semantic_key": semantic_key,
            "options": (
                ["exact_directory_session", "exact_directory_run"]
                if access == "read"
                else ["exact_directory_run", "exact_directory_session"]
            ),
        }
        if grant_bindings:
            request["grant_bindings"] = dict(grant_bindings)
        if change_preview:
            request["change_preview"] = dict(change_preview)
        self._requests[request_id] = request
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        emit_harness_metric(
            logger,
            "permission_prompt_count",
            session_id=session_id,
            type=request["type"],
            target=str(path),
        )
        return dict(request)

    def create_shell_access_request(
        self,
        *,
        session_id: str,
        query_id: str,
        run_id: str,
        tool_call_id: str,
        command: str,
        plan: ShellAccessPlan,
        grant_bindings: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one atomic shell-plane request for all command directories."""

        if not plan.required:
            raise ValueError("Shell access request requires at least one directory")
        specs = [
            {
                "target": spec.target,
                "access": spec.access,
                "delete": spec.delete,
                "capabilities": spec.capabilities,
            }
            for spec in plan.grant_specs
        ]
        semantic_payload = {
            "authority_plane": "shell",
            "session_id": session_id,
            "run_id": run_id,
            "specs": specs,
            "grant_bindings": dict(grant_bindings),
        }
        semantic_key = "sha256:" + hashlib.sha256(
            json.dumps(
                semantic_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        replay_key = "\0".join(
            [session_id, query_id, run_id, tool_call_id, semantic_key]
        )
        request_id = (
            "perm-req-shell-"
            + hashlib.sha256(replay_key.encode("utf-8")).hexdigest()[:16]
        )
        existing = self._requests.get(request_id)
        if existing is not None and existing.get("status") == "pending":
            emit_harness_metric(
                logger,
                "permission_reuse_count",
                session_id=session_id,
                scope="pending_request",
            )
            return dict(existing)
        if existing is not None:
            request_id = f"{request_id}-{uuid.uuid4().hex[:6]}"
        capabilities = sorted(
            {
                capability
                for spec in plan.grant_specs
                for capability in spec.capabilities
            }
        )
        request = {
            "id": request_id,
            "type": "shell_directory_access",
            "authority_plane": "shell",
            "session_id": session_id,
            "query_id": query_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "command": command,
            "path": plan.directories[0],
            "paths": list(plan.directories),
            "grant_specs": specs,
            "target_kind": "exact_directory_set",
            "capabilities": capabilities,
            "grant_bindings": dict(grant_bindings),
            "semantic_key": semantic_key,
            "status": "pending",
            "created_at": time.time(),
            "options": ["exact_directory_run", "exact_directory_session"],
        }
        self._requests[request_id] = request
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        emit_harness_metric(
            logger,
            "permission_prompt_count",
            session_id=session_id,
            type=request["type"],
            target=" | ".join(plan.directories),
        )
        return dict(request)

    def resolve_compatible_session_external_directories(
        self,
        *,
        session_id: str,
        path: str,
        access: str,
        capabilities: list[str],
        decision: dict[str, Any],
        grant_bindings: dict[str, Any],
        exclude_request_id: str = "",
        binding_resolver: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> list[str]:
        """Resolve pending peers covered by one exact Session directory Grant."""

        granted = set(capabilities)
        resolved: list[str] = []
        for request_id, request in list(self._requests.items()):
            if request_id == exclude_request_id:
                continue
            if (
                request.get("type") != f"external_directory_{access}"
                or request.get("status") != "pending"
                or request.get("session_id") != session_id
                or str(request.get("path") or "") != path
            ):
                continue
            required = set(request.get("capabilities") or [])
            request_bindings = request.get("grant_bindings")
            if not isinstance(request_bindings, dict) and binding_resolver is not None:
                request_bindings = binding_resolver(request)
            if not required.issubset(granted) or not PermissionBindingPolicy.equivalent(
                grant_type=f"external_directory_{access}",
                scope="session",
                target_kind="exact_directory",
                target=path,
                left=request_bindings if isinstance(request_bindings, dict) else None,
                right=grant_bindings,
            ):
                continue
            if self.resolve(request_id, dict(decision)):
                resolved.append(request_id)
                emit_harness_metric(
                    logger,
                    "permission_reuse_count",
                    session_id=session_id,
                    scope="session",
                )
        return resolved

    def create_tool_action_request(
        self,
        *,
        session_id: str,
        query_id: str,
        tool_call_id: str,
        tool_name: str,
        command: str,
        reason: str,
        risk: str,
        session_target_kind: str | None = None,
        session_target: str | None = None,
        session_scope_label: str | None = None,
        run_id: str = "",
        grant_bindings: dict[str, Any] | None = None,
        required_capabilities: list[str] | None = None,
        change_preview: dict[str, str] | None = None,
        policy_source: str = "deterministic",
        policy_explanation: str = "",
        control_descriptor: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        fingerprint = self.tool_action_fingerprint(
            tool_name=tool_name,
            command=command,
            reason=reason,
        )
        semantic_key = ""
        if session_target_kind and session_target:
            semantic_key, _stable = PermissionBindingPolicy.semantic_key(
                session_id=session_id,
                grant_type="tool_action",
                scope="session",
                target_kind=session_target_kind,
                target=session_target,
                capabilities=list(required_capabilities or ["execute"]),
                runtime_bindings=grant_bindings,
            )
            for existing in self._requests.values():
                if (
                    existing.get("status") == "pending"
                    and existing.get("semantic_key") == semantic_key
                ):
                    emit_harness_metric(
                        logger,
                        "permission_reuse_count",
                        session_id=session_id,
                        scope="pending_request",
                    )
                    return dict(existing)
        replay_key = "\0".join([session_id, query_id, run_id, tool_call_id, fingerprint])
        request_id = f"perm-req-{hashlib.sha256(replay_key.encode('utf-8')).hexdigest()[:16]}"
        existing = self._requests.get(request_id)
        if existing is not None:
            return dict(existing)
        request = {
            "id": request_id,
            "type": "tool_action",
            "session_id": session_id,
            "query_id": query_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "command": command,
            "reason": reason,
            "risk": risk,
            "fingerprint": fingerprint,
            "target_kind": "fingerprint",
            "capabilities": list(required_capabilities or ["execute"]),
            "status": "pending",
            "created_at": time.time(),
            "options": ["once", "session"] if session_target_kind and session_target else ["once"],
            "policy_source": policy_source,
        }
        if semantic_key:
            request["semantic_key"] = semantic_key
        if session_target_kind and session_target:
            request["session_target_kind"] = session_target_kind
            request["session_target"] = session_target
        if session_scope_label:
            request["session_scope_label"] = session_scope_label
        if grant_bindings:
            request["grant_bindings"] = dict(grant_bindings)
        if change_preview:
            request["change_preview"] = dict(change_preview)
        if policy_explanation:
            request["policy_explanation"] = policy_explanation
        if control_descriptor:
            request["control_descriptor"] = dict(control_descriptor)
        self._requests[request_id] = request
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        emit_harness_metric(
            logger,
            "permission_prompt_count",
            session_id=session_id,
            type="tool_action",
            target=session_target or fingerprint,
        )
        return dict(request)

    async def wait(self, request_id: str) -> dict[str, Any]:
        future = self._pending.get(request_id)
        if future is None:
            return {"type": "reject", "message": "Permission request is no longer active."}
        return await future

    def resolve(self, request_id: str, decision: dict[str, Any]) -> bool:
        future = self._pending.pop(request_id, None)
        request = self._requests.get(request_id)
        if request is not None:
            request["status"] = "resolved"
            request["resolved_at"] = time.time()
            request["decision"] = decision
        if future is None:
            return False
        if not future.done():
            future.set_result(decision)
        return True

    def resolve_compatible_session_tool_actions(
        self,
        *,
        session_id: str,
        target_kind: str,
        target: str,
        capabilities: list[str],
        decision: dict[str, Any],
        grant_bindings: dict[str, Any] | None = None,
        exclude_request_id: str = "",
    ) -> list[str]:
        """Apply a newly granted Session capability to already-pending peers.

        Multiple tool calls can reach HITL before the user answers the first
        card.  Once a reusable Session grant exists, keeping compatible cards
        blocked would make approval order observable and force duplicate user
        decisions.  Compatibility is deliberately capability-based and does
        not broaden the grant to requests with additional powers or bindings.
        """

        granted = set(capabilities)
        expected_bindings = grant_bindings or None
        resolved: list[str] = []
        for request_id, request in list(self._requests.items()):
            if request_id == exclude_request_id:
                continue
            if (
                request.get("type") != "tool_action"
                or request.get("status") != "pending"
                or request.get("session_id") != session_id
                or request.get("session_target_kind") != target_kind
                or request.get("session_target") != target
            ):
                continue
            required = set(request.get("capabilities") or ["execute"])
            request_bindings = request.get("grant_bindings")
            normalized_bindings = request_bindings if isinstance(request_bindings, dict) else None
            if not required.issubset(granted) or not PermissionBindingPolicy.equivalent(
                grant_type="tool_action",
                scope="session",
                target_kind=target_kind,
                target=target,
                left=normalized_bindings,
                right=expected_bindings,
            ):
                continue
            if self.resolve(request_id, dict(decision)):
                resolved.append(request_id)
                emit_harness_metric(
                    logger,
                    "permission_reuse_count",
                    session_id=session_id,
                    scope="session",
                )
        return resolved

    def reject_session(self, session_id: str, message: str) -> int:
        """Reject all active permission requests for a cancelled session stream."""

        rejected = 0
        for request_id, request in list(self._requests.items()):
            if request.get("session_id") != session_id or request.get("status") != "pending":
                continue
            if self.resolve(request_id, {"type": "reject", "message": message}):
                rejected += 1
        return rejected

    def get(self, request_id: str) -> dict[str, Any] | None:
        request = self._requests.get(request_id)
        return dict(request) if request else None

    def find_tool_action_request(
        self,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        """Return the newest Tool-action request matching a persisted grant."""

        for request in reversed(list(self._requests.values())):
            if request.get("type") == "tool_action" and request.get("fingerprint") == fingerprint:
                return dict(request)
        return None


permission_resume_registry = PermissionResumeRegistry()
