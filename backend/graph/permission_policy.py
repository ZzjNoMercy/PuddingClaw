"""Immutable permission policy values shared by Session, Run, and Tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ApprovalMode(StrEnum):
    """How much deterministic low-risk work Harness may approve automatically."""

    STRICT = "strict"
    SMART = "smart"
    FULL_ACCESS = "full_access"


DEFAULT_APPROVAL_MODE = ApprovalMode.STRICT
PERMISSION_POLICY_VERSION = "tool-execution-v4"
PERMISSION_BINDING_SCHEMA_VERSION = 2
SHELL_PERMISSION_BINDING_SCHEMA_VERSION = 3
SHELL_ISOLATION_POLICY_ID = "kernel-docker-shared-v1"
SHELL_PROFILE_SCHEMA = "sandbox-grant-profile-v1"


@dataclass(frozen=True)
class ShellDirectoryGrantSpec:
    """One server-authored member of an atomic shell directory grant set."""

    target: str
    access: str
    delete: bool = False

    @property
    def capabilities(self) -> list[str]:
        return [
            self.access,
            *(["delete"] if self.delete else []),
            "recursive",
            "external_path",
            "shell_access",
        ]


class PermissionBindingPolicy:
    """Define the stable authority boundary for reusable grants."""

    _COMMON_KEYS = (
        "approval_mode",
        "backend_mode",
        "policy_epoch",
        "policy_version",
        "workspace_id",
    )

    @classmethod
    def project(
        cls,
        *,
        grant_type: str,
        scope: str,
        target_kind: str,
        target: str,
        runtime_bindings: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        bindings = runtime_bindings if isinstance(runtime_bindings, Mapping) else {}
        if scope == "session" and (
            target_kind in {"network_origin", "network_profile"}
            or target == "session_network_access"
            or grant_type.startswith("external_directory_")
        ):
            return {key: bindings.get(key) for key in cls._COMMON_KEYS}
        return {str(key): value for key, value in bindings.items()}

    @classmethod
    def semantic_key(
        cls,
        *,
        session_id: str,
        grant_type: str,
        scope: str,
        target_kind: str,
        target: str,
        capabilities: list[str],
        runtime_bindings: Mapping[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        stable = cls.project(
            grant_type=grant_type,
            scope=scope,
            target_kind=target_kind,
            target=target,
            runtime_bindings=runtime_bindings,
        )
        payload = {
            "binding_schema_version": PERMISSION_BINDING_SCHEMA_VERSION,
            "session_id": session_id,
            "grant_type": grant_type,
            "scope": scope,
            "target_kind": target_kind,
            "target": target,
            "capabilities": sorted(set(capabilities)),
            "stable_bindings": stable,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}", stable

    @classmethod
    def equivalent(
        cls,
        *,
        grant_type: str,
        scope: str,
        target_kind: str,
        target: str,
        left: Mapping[str, Any] | None,
        right: Mapping[str, Any] | None,
    ) -> bool:
        return cls.project(
            grant_type=grant_type,
            scope=scope,
            target_kind=target_kind,
            target=target,
            runtime_bindings=left,
        ) == cls.project(
            grant_type=grant_type,
            scope=scope,
            target_kind=target_kind,
            target=target,
            runtime_bindings=right,
        )

    @staticmethod
    def shell_v3_equivalent(
        left: Mapping[str, Any] | None,
        right: Mapping[str, Any] | None,
    ) -> bool:
        """Strictly compare native shell authority bindings."""

        required = (
            "approval_mode",
            "policy_epoch",
            "policy_version",
            "workspace_id",
            "isolation_policy_id",
            "profile_schema",
        )
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if any(left.get(key) in {None, ""} or right.get(key) in {None, ""} for key in required):
            return False
        return {key: left.get(key) for key in required} == {key: right.get(key) for key in required}

    @staticmethod
    def shell_v3_semantic_key(
        *,
        session_id: str,
        scope: str,
        run_id: str,
        grant_type: str,
        target: str,
        capabilities: list[str],
        bindings: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if not PermissionBindingPolicy.shell_v3_equivalent(bindings, bindings):
            raise ValueError("Native shell grant bindings are incomplete")
        stable = dict(bindings)
        payload = {
            "binding_schema_version": SHELL_PERMISSION_BINDING_SCHEMA_VERSION,
            "session_id": session_id,
            "scope": scope,
            "run_id": run_id if scope == "run" else "",
            "grant_type": grant_type,
            "target_kind": "exact_directory",
            "target": target,
            "capabilities": sorted(set(capabilities)),
            "stable_bindings": stable,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}", stable


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
            policy_version=str(frozen_permissions.get("policy_version") or current_permissions["policy_version"]),
            backend_mode=str(execution.get("backend_mode") or "restricted_host"),
            backend_id=str(execution.get("backend_id") or ""),
            workspace_id=str(execution.get("workspace_id") or ""),
        )

    @property
    def smart(self) -> bool:
        return self.approval_mode == ApprovalMode.SMART

    @property
    def full_access(self) -> bool:
        """Whether the caller requested the separately-scoped trusted mode."""

        return self.approval_mode == ApprovalMode.FULL_ACCESS

    def grant_bindings(self) -> dict[str, Any]:
        return {
            "approval_mode": self.approval_mode.value,
            "policy_epoch": self.policy_epoch,
            "policy_version": self.policy_version,
            "backend_mode": self.backend_mode,
            "backend_id": self.backend_id,
            "workspace_id": self.workspace_id,
        }

    def shell_grant_bindings(self) -> dict[str, Any]:
        """Return runner-neutral bindings required by native shell grants."""

        return {
            "approval_mode": self.approval_mode.value,
            "policy_epoch": self.policy_epoch,
            "policy_version": self.policy_version,
            "workspace_id": self.workspace_id,
            "isolation_policy_id": SHELL_ISOLATION_POLICY_ID,
            "profile_schema": SHELL_PROFILE_SCHEMA,
        }
