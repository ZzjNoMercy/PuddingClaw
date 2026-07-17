"""Immutable permission policy values shared by Session, Run, and Tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ApprovalMode(StrEnum):
    """How much deterministic low-risk work Harness may approve automatically."""

    STRICT = "strict"
    SMART = "smart"


DEFAULT_APPROVAL_MODE = ApprovalMode.STRICT
PERMISSION_POLICY_VERSION = "tool-execution-v2"


def normalize_approval_mode(value: Any) -> ApprovalMode:
    """Return one supported mode or raise instead of silently weakening policy."""

    if isinstance(value, ApprovalMode):
        return value
    try:
        return ApprovalMode(str(value or DEFAULT_APPROVAL_MODE.value).strip().lower())
    except ValueError as exc:
        raise ValueError(f"Unsupported approval mode: {value!r}") from exc


def permission_policy_snapshot(
    permissions: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compile mutable Session permissions into an immutable Run snapshot."""

    payload = permissions if isinstance(permissions, Mapping) else {}
    epoch = payload.get("policy_epoch", 1)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        epoch = 1
    return {
        "approval_mode": normalize_approval_mode(payload.get("approval_mode")).value,
        "policy_epoch": epoch,
        "policy_version": PERMISSION_POLICY_VERSION,
    }


@dataclass(frozen=True)
class RunPermissionContext:
    """The only permission context a Tool pipeline may use during one Run."""

    approval_mode: ApprovalMode
    policy_epoch: int
    policy_version: str
    backend_mode: str
    backend_id: str
    workspace_id: str

    @classmethod
    def from_config_snapshot(
        cls,
        snapshot: Mapping[str, Any] | None,
    ) -> RunPermissionContext:
        payload = snapshot if isinstance(snapshot, Mapping) else {}
        frozen_permissions = payload.get("permissions")
        frozen_permissions = frozen_permissions if isinstance(frozen_permissions, Mapping) else {}
        current_permissions = permission_policy_snapshot(frozen_permissions)
        execution = payload.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        return cls(
            approval_mode=normalize_approval_mode(current_permissions["approval_mode"]),
            policy_epoch=int(current_permissions["policy_epoch"]),
            # A Run snapshot is historical authority. Preserve the version it
            # actually started with instead of upgrading it during restore.
            policy_version=str(
                frozen_permissions.get("policy_version") or current_permissions["policy_version"]
            ),
            backend_mode=str(execution.get("backend_mode") or "restricted_host"),
            backend_id=str(execution.get("backend_id") or ""),
            workspace_id=str(execution.get("workspace_id") or ""),
        )

    @property
    def smart(self) -> bool:
        return self.approval_mode == ApprovalMode.SMART

    def grant_bindings(self) -> dict[str, Any]:
        return {
            "approval_mode": self.approval_mode.value,
            "policy_epoch": self.policy_epoch,
            "policy_version": self.policy_version,
            "backend_mode": self.backend_mode,
            "backend_id": self.backend_id,
            "workspace_id": self.workspace_id,
        }
