"""Deterministic, fail-closed resolution for unattended Worker Runs.

The interactive Agent path intentionally waits on the same registries.  This
module is the only place where unattended ``auto``/``external`` modes may turn
a pending interrupt into a decision. ``external`` leaves permission decisions
to the consumer and uses this resolver only for fail-closed business HITL. It
never invents business confirmation payloads.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from graph.database_sql_revision_resume import database_sql_revision_resume_registry
from graph.dimension_build_resume import dimension_build_resume_registry
from graph.logical_dataset_resume import logical_dataset_resume_registry
from graph.permission_resume import permission_resume_registry
from graph.kernel_fallback_resume import kernel_fallback_resume_registry
from graph.skill_plan_resume import skill_plan_resume_registry
from graph.skill_secret_resume import skill_secret_resume_registry
from graph.user_input_resume import user_input_resume_registry


class HeadlessInterruptResolver:
    """Resolve one interrupt through its owning registry, never via a fake payload."""

    _REGISTRIES = {
        "permission_request": permission_resume_registry,
        "dimension_build_rule_request": dimension_build_resume_registry,
        "logical_dataset_rule_request": logical_dataset_resume_registry,
        "database_sql_revision_request": database_sql_revision_resume_registry,
        "user_input_request": user_input_resume_registry,
        "skill_plan_confirmation_request": skill_plan_resume_registry,
        "skill_secret_request": skill_secret_resume_registry,
        "kernel_fallback_request": kernel_fallback_resume_registry,
    }

    def __init__(self, *, context: dict[str, Any]) -> None:
        self.context = context
        summary = context.setdefault(
            "_headless_interrupt_summary",
            {"total": 0, "auto_approved": 0, "auto_rejected": 0, "by_type": {}},
        )
        context.setdefault("_headless_auto_resolved", [])
        self.summary = summary

    def resolve(
        self,
        interrupt_type: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = str(request.get("id") or "")
        registry = self._REGISTRIES.get(interrupt_type)
        if registry is None or not request_id:
            raise ValueError(f"Unsupported headless interrupt: {interrupt_type}")

        decision = self._decision(interrupt_type, request)
        normalized = self._resolve_registry(registry, request_id, decision, request)
        if normalized is None:
            # A request can be resolved by cancellation cleanup between stream
            # drain and this method. Treat it as an explicit reject, not a wait.
            normalized = dict(decision)
        action = str(normalized.get("type") or normalized.get("action") or "")
        if interrupt_type in {
            "database_sql_revision_request",
            "dimension_build_rule_request",
            "logical_dataset_rule_request",
            "skill_plan_confirmation_request",
        } or (interrupt_type == "user_input_request" and action == "cancel"):
            self.context["_headless_needs_input"] = {
                "type": interrupt_type,
                "request_id": request_id,
            }
        approved = action in {"approve", "confirm", "agree", "modify", "submit", "agent_decide"}
        self.summary["total"] = int(self.summary.get("total") or 0) + 1
        if approved:
            self.summary["auto_approved"] = int(self.summary.get("auto_approved") or 0) + 1
        else:
            self.summary["auto_rejected"] = int(self.summary.get("auto_rejected") or 0) + 1
        by_type = self.summary.setdefault("by_type", {})
        bucket = by_type.setdefault(
            interrupt_type,
            {"total": 0, "auto_approved": 0, "auto_rejected": 0},
        )
        bucket["total"] += 1
        bucket["auto_approved" if approved else "auto_rejected"] += 1
        self.context["_headless_auto_resolved"].append(
            {
                "request_id": request_id,
                "type": interrupt_type,
                "decision": action or "unknown",
            }
        )
        return normalized

    def _decision(self, interrupt_type: str, request: dict[str, Any]) -> dict[str, Any]:
        if interrupt_type == "permission_request":
            # A live browser action is always human-in-the-loop. A headless
            # Worker never turns an unresolved permission request into a
            # standing filesystem/network authority.
            if str(request.get("tool_name") or "") == "browser":
                return {"type": "reject", "reason": "browser_action_requires_interactive_user"}
            return {"type": "reject", "reason": "headless_permission_boundary"}
        if interrupt_type == "user_input_request":
            if bool(request.get("allow_agent_decide", True)):
                return {"action": "agent_decide"}
            return {"action": "cancel", "reason": "headless_user_input_required"}
        if interrupt_type == "kernel_fallback_request":
            return {"action": "reject", "reason": "headless_kernel_fallback_requires_explicit_user_choice"}
        if interrupt_type == "skill_secret_request":
            return {"action": "cancel", "reason": "interactive_secret_entry_required"}
        if interrupt_type == "database_sql_revision_request":
            return {"action": "reject", "reason": "headless_business_confirmation_required"}
        if interrupt_type in {
            "dimension_build_rule_request",
            "logical_dataset_rule_request",
            "skill_plan_confirmation_request",
        }:
            return {"action": "cancel", "reason": "headless_business_confirmation_required"}
        return {"type": "reject", "reason": "headless_unsupported_interrupt"}

    @staticmethod
    def _resolve_registry(
        registry: Any,
        request_id: str,
        decision: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        if registry is kernel_fallback_resume_registry:
            normalized, _resumed = registry.resolve(
                request_id,
                str(decision.get("action") or "reject"),
                request_version=int(request.get("version") or 1),
            )
            return normalized
        if registry is skill_plan_resume_registry:
            # Skill plans expose a status-based registry API because committing
            # a plan has side effects. The registry's cancel path resolves the
            # owning future without pretending that any plan was committed.
            return registry.cancel(request_id, "headless_business_confirmation_required")
        result = registry.resolve(request_id, decision)
        if registry is skill_secret_resume_registry:
            normalized, _resumed = result
            return normalized
        return result if isinstance(result, dict) else (dict(decision) if result else None)

    def _within_authority_scope(self, request: dict[str, Any]) -> bool:
        """Allow only server-configured workspace/network/package sub-scopes."""

        profile = str(self.context.get("authority_profile") or "smart").strip().lower()
        if profile in {"", "smart", "none", "restricted"}:
            return False
        target_kind = str(request.get("session_target_kind") or request.get("target_kind") or "")
        target = str(request.get("path") or request.get("session_target") or "")
        if target_kind in {"exact_directory", "path", "directory"} or request.get("path"):
            try:
                candidate = Path(target).expanduser().resolve()
                roots = [Path(str(self.context.get("workspace_path") or "")).expanduser().resolve()]
                roots.extend(
                    Path(item).expanduser().resolve()
                    for item in self.context.get("authority_directories") or []
                    if str(item).strip()
                )
                return any(candidate == root or root in candidate.parents for root in roots if str(root) != ".")
            except (OSError, RuntimeError, ValueError):
                return False
        if target_kind in {"network_origin", "network_profile"}:
            if "network" not in profile:
                return False
            try:
                origin = urlsplit(target)
                normalized = f"{origin.scheme.lower()}://{origin.hostname.lower()}:{origin.port or (443 if origin.scheme.lower() == 'https' else 80)}"
            except (AttributeError, ValueError):
                return False
            allowed = {
                str(item).strip().lower().rstrip("/")
                for item in self.context.get("authority_network_origins") or []
                if str(item).strip()
            }
            return normalized.rstrip("/") in allowed
        if target_kind == "capability":
            return target == "docker_package_install" and "package" in profile
        # A raw fingerprint is deliberately not enough to prove command
        # authority. A fingerprint is an identity check, not a capability.
        return False


def headless_authority_from_environment() -> dict[str, Any]:
    """Return safe, non-secret authority settings for a Worker Run."""

    raw_dirs = os.getenv("PUDDINGCLAW_HEADLESS_ALLOWED_DIRECTORIES", "")
    raw_origins = os.getenv("PUDDINGCLAW_HEADLESS_ALLOWED_NETWORK_ORIGINS", "")
    return {
        "profile": os.getenv("PUDDINGCLAW_HEADLESS_AUTHORITY_PROFILE", "smart").strip().lower(),
        "directories": [item.strip() for item in raw_dirs.split(",") if item.strip()],
        "network_origins": [item.strip() for item in raw_origins.split(",") if item.strip()],
    }
