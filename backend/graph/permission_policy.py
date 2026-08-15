"""Immutable permission policy values shared by Session, Run, and Tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from enum import StrEnum
from typing import Any


class ApprovalMode(StrEnum):
    """How much deterministic low-risk work Harness may approve automatically."""

    STRICT = "strict"
    SMART = "smart"


DEFAULT_APPROVAL_MODE = ApprovalMode.SMART
PERMISSION_POLICY_VERSION = "tool-execution-v4"
PERMISSION_BINDING_SCHEMA_VERSION = 2
SHELL_PERMISSION_BINDING_SCHEMA_VERSION = 3
SHELL_ISOLATION_POLICY_ID = "spawn-kernel-shared-v1"
SHELL_PROFILE_SCHEMA = "sandbox-grant-profile-v1"


class PermissionRuleDecision(StrEnum):
    """User-configurable outcome for a matching effect pattern."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionRuleError(ValueError):
    """Raised when a persisted permission rule is malformed."""


@dataclass(frozen=True)
class PermissionRule:
    """Typed allow/ask/deny rule shared by config and project policy."""

    tool: str
    pattern: str
    decision: PermissionRuleDecision
    scope: str = "session"
    constraints: tuple[tuple[str, Any], ...] = ()
    source: str = "session"
    revision: int = 0

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        source: str = "session",
    ) -> "PermissionRule":
        if not isinstance(value, Mapping):
            raise PermissionRuleError("permission rule must be an object")
        tool = str(value.get("tool") or "*").strip().lower()
        pattern = str(value.get("pattern") or "*").strip()
        raw_decision = str(value.get("decision") or "").strip().lower()
        scope = str(value.get("scope") or "session").strip().lower()
        if not tool or not pattern:
            raise PermissionRuleError("permission rule tool and pattern are required")
        try:
            decision = PermissionRuleDecision(raw_decision)
        except ValueError as exc:
            raise PermissionRuleError(
                f"unsupported permission rule decision: {raw_decision!r}"
            ) from exc
        if decision is PermissionRuleDecision.ALLOW and _is_unbounded_interpreter_pattern(tool, pattern):
            raise PermissionRuleError(
                "allow rules may not persist arbitrary interpreter or shell-wrapper patterns"
            )
        if scope not in {"global", "project", "session"}:
            raise PermissionRuleError(f"unsupported permission rule scope: {scope!r}")
        constraints = value.get("constraints") or {}
        if not isinstance(constraints, Mapping):
            raise PermissionRuleError("permission rule constraints must be an object")
        normalized_constraints = tuple(
            sorted((str(key), _normalize_rule_constraint(key, item)) for key, item in constraints.items())
        )
        revision = value.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise PermissionRuleError("permission rule revision must be a non-negative integer")
        return cls(
            tool=tool,
            pattern=pattern,
            decision=decision,
            scope=scope,
            constraints=normalized_constraints,
            source=str(value.get("source") or source),
            revision=revision,
        )

    def matches(self, *, tool: str, pattern: str) -> bool:
        return fnmatchcase(tool.lower(), self.tool) and fnmatchcase(pattern, self.pattern)

    def constraint_map(self) -> dict[str, Any]:
        return dict(self.constraints)

    def allows_effects(self, effects: Mapping[str, Any]) -> bool:
        constraints = self.constraint_map()
        if bool(effects.get("network")) and constraints.get("network", False) is not True:
            return False
        if bool(effects.get("credentials")) and constraints.get("credentials", False) is not True:
            return False
        if bool(effects.get("destructive")) and constraints.get("destructive", False) is not True:
            return False
        if bool(effects.get("package_install")) and constraints.get("package_install", False) is not True:
            return False
        write_scope = constraints.get("write_scope")
        actual_scope = str(effects.get("write_scope") or "none")
        if actual_scope != "none" and write_scope is None:
            return False
        if write_scope == "workspace_or_scratch" and actual_scope not in {"none", "workspace", "scratch"}:
            return False
        if write_scope == "none" and actual_scope != "none":
            return False
        if write_scope == "external" and actual_scope != "external":
            return False
        return True


def _normalize_rule_constraint(key: Any, value: Any) -> Any:
    key = str(key)
    if key in {"network", "credentials", "destructive", "package_install"}:
        if not isinstance(value, bool):
            raise PermissionRuleError(f"permission rule constraint {key!r} must be boolean")
        return value
    if key == "write_scope":
        normalized = str(value).strip().lower()
        if normalized not in {"none", "workspace_or_scratch", "external"}:
            raise PermissionRuleError(f"unsupported write_scope constraint: {normalized!r}")
        return normalized
    raise PermissionRuleError(f"unsupported permission rule constraint: {key!r}")


def _is_unbounded_interpreter_pattern(tool: str, pattern: str) -> bool:
    if tool not in {"execute", "*"}:
        return False
    tokens = pattern.split()
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable not in {"python", "python3", "node", "ruby", "perl", "php", "sh", "bash", "zsh"}:
        return False
    # Until command-pattern identity includes script digest/runtime binding,
    # no interpreter allow rule is safely reusable. Smart mode can still
    # auto-approve a deterministically safe invocation, while an explicit
    # approval remains one-time.
    return True


def compile_permission_rules(
    raw_rules: Any,
    *,
    source: str = "session",
) -> tuple[PermissionRule, ...]:
    """Validate and freeze persisted rules; malformed policy fails closed."""

    if raw_rules in (None, []):
        return ()
    if not isinstance(raw_rules, (list, tuple)):
        raise PermissionRuleError("permissions.rules must be an array")
    return tuple(
        PermissionRule.from_mapping(rule, source=source)
        for rule in raw_rules
    )


def evaluate_permission_rules(
    rules: tuple[PermissionRule, ...],
    *,
    tool: str,
    pattern: str,
    effects: Mapping[str, Any],
) -> PermissionRuleDecision | None:
    """Evaluate rules with hard deny and risk constraints before allow."""

    matching = [rule for rule in rules if rule.matches(tool=tool, pattern=pattern)]
    if not matching:
        return None
    deny = [rule for rule in matching if rule.decision == PermissionRuleDecision.DENY]
    if deny:
        return PermissionRuleDecision.DENY
    ask = [rule for rule in matching if rule.decision == PermissionRuleDecision.ASK]
    if ask:
        return PermissionRuleDecision.ASK
    allow = [
        rule
        for rule in matching
        if rule.decision == PermissionRuleDecision.ALLOW and rule.allows_effects(effects)
    ]
    if allow:
        return PermissionRuleDecision.ALLOW
    return None


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
        "filesystem_mode",
        "policy_epoch",
        "policy_version",
        "permission_rules_revision",
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
            "permission_rules_revision",
            "workspace_id",
            "filesystem_mode",
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
    compiled_rules = compile_permission_rules(payload.get("rules"))
    declared_rules_revision = payload.get("rules_revision", 0)
    if isinstance(declared_rules_revision, bool) or not isinstance(declared_rules_revision, int):
        declared_rules_revision = 0
    rules_revision = max(
        declared_rules_revision,
        max((rule.revision for rule in compiled_rules), default=0),
    )
    return {
        "approval_mode": normalize_approval_mode(payload.get("approval_mode")).value,
        "policy_epoch": epoch,
        "policy_version": PERMISSION_POLICY_VERSION,
        "permission_rules_revision": rules_revision,
        "rules": [
            {
                "tool": rule.tool,
                "pattern": rule.pattern,
                "decision": rule.decision.value,
                "scope": rule.scope,
                "constraints": rule.constraint_map(),
                "source": rule.source,
                "revision": rule.revision,
            }
            for rule in compiled_rules
        ],
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
    filesystem_mode: str = "restricted"
    rules: tuple[PermissionRule, ...] = ()
    permission_rules_revision: int = 0

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
        configured_filesystem = str(execution.get("filesystem_mode") or "").strip().lower()
        if configured_filesystem not in {"restricted", "unrestricted"}:
            # Backward-compatible snapshots from the pre-alignment schema
            # derive the interactive smart-local contract once, at restore.
            configured_filesystem = (
                "unrestricted"
                if normalize_approval_mode(current_permissions["approval_mode"]) is ApprovalMode.SMART
                and str(execution.get("backend_mode") or "spawn") in {"spawn", "kernel"}
                else "restricted"
            )
        return cls(
            approval_mode=normalize_approval_mode(current_permissions["approval_mode"]),
            policy_epoch=int(current_permissions["policy_epoch"]),
            # A Run snapshot is historical authority. Preserve the version it
            # actually started with instead of upgrading it during restore.
            policy_version=str(frozen_permissions.get("policy_version") or current_permissions["policy_version"]),
            backend_mode=str(execution.get("backend_mode") or "spawn"),
            backend_id=str(execution.get("backend_id") or ""),
            workspace_id=str(execution.get("workspace_id") or ""),
            filesystem_mode=configured_filesystem,
            rules=compile_permission_rules(frozen_permissions.get("rules")),
            permission_rules_revision=int(current_permissions.get("permission_rules_revision") or 0),
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
            "filesystem_mode": self.filesystem_mode,
            "permission_rules_revision": self.permission_rules_revision,
        }

    def shell_grant_bindings(self) -> dict[str, Any]:
        """Return runner-neutral bindings required by native shell grants."""

        return {
            "approval_mode": self.approval_mode.value,
            "policy_epoch": self.policy_epoch,
            "policy_version": self.policy_version,
            "permission_rules_revision": self.permission_rules_revision,
            "workspace_id": self.workspace_id,
            "filesystem_mode": self.filesystem_mode,
            "isolation_policy_id": SHELL_ISOLATION_POLICY_ID,
            "profile_schema": SHELL_PROFILE_SCHEMA,
        }
