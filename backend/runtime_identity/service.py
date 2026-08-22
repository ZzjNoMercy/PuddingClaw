"""Managed CLI control-plane service."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from runtime_identity.adapters import (
    CredentialStateSpec,
    ManagedCliAction,
    ManagedCliMatch,
    ManagedCliRegistry,
    ManagedCliRoute,
    UnsupportedManagedCliCommand,
    generic_node_cli_install,
)
from runtime_identity.authorization import (
    AuthorizationFlowStatus,
    AuthorizationFlowStore,
)
from runtime_identity.authorization_drivers import (
    AuthorizationDriver,
    AuthorizationDriverRegistry,
    AuthorizationPhaseKind,
    AuthorizationPhaseNode,
)
from runtime_identity.paths import PuddingClawPaths, trusted_owner_user_id
from runtime_identity.profiles import CredentialProfileStore
from runtime_identity.toolchains import ToolchainManager, version_satisfies

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|device[_-]?code)"
        r"(\s*[=:]\s*)([^\s,}\]]+)"
    ),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s]+)"),
    re.compile(
        r'(?i)("(?:access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|device[_-]?code)"'
        r'\s*:\s*")([^"]+)(")'
    ),
    re.compile(r"(?i)(--device-code(?:\s+|=))(?:\"[^\"]+\"|'[^']+'|\S+)"),
)
_MISSING_CLIENT_SECRET = re.compile(
    r"missing a required parameter:\s*client_secret|appsecret is (?:missing|empty)",
    re.IGNORECASE,
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def redact_managed_cli_output(output: str) -> str:
    value = str(output or "")
    value = _SECRET_PATTERNS[0].sub(r"\1\2<redacted>", value)
    value = _SECRET_PATTERNS[1].sub(r"\1<redacted>", value)
    value = _SECRET_PATTERNS[2].sub(r"\1<redacted>\3", value)
    value = _SECRET_PATTERNS[3].sub(r"\1<redacted>", value)
    return value


def _safe_authorization_diagnostic(
    output: str,
    failure: _LarkAuthorizationFailure,
    *,
    exit_code: int,
    candidate_state_exported: bool,
    candidate_identity_verified: bool,
) -> dict[str, Any]:
    """Build a durable diagnostic without persisting provider secrets."""

    # Diagnostics are persisted and projected to the UI. Keep them strictly
    # structural: provider output is untrusted and may contain an unlabeled
    # access token, device code, URL query, or secret-dependent fingerprint.
    diagnostic: dict[str, Any] = {
        "reason": failure.reason,
        "exit_code": int(exit_code),
        "candidate_state_exported": bool(candidate_state_exported),
        "candidate_identity_verified": bool(candidate_identity_verified),
    }
    provider_code = str(failure.provider_code or "")
    if provider_code.isdigit() and len(provider_code) <= 10:
        diagnostic["provider_code"] = int(provider_code)
    # Persist only enum-like provider classifications.  Free-form message and
    # hint fields are deliberately excluded: provider CLIs may echo URLs,
    # device codes, or unlabeled tokens there, so regex redaction cannot make
    # an arbitrary stderr blob safe for a durable flow registry.
    for value in reversed(_json_objects(output)):
        error = value.get("error")
        if not isinstance(error, dict):
            continue
        for source_key, target_key in (
            ("type", "provider_error_type"),
            ("subtype", "provider_error_subtype"),
        ):
            classification = str(error.get(source_key) or "").strip().lower()
            if re.fullmatch(r"[a-z0-9_.-]{1,64}", classification):
                diagnostic[target_key] = classification
        break
    return diagnostic


def _json_objects(output: str) -> list[dict[str, Any]]:
    cleaned = "\n".join(line.removeprefix("[stderr] ") for line in str(output or "").splitlines())
    decoder = json.JSONDecoder()
    values: list[dict[str, Any]] = []
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _lark_identity_status(output: str) -> dict[str, Any] | None:
    objects = _json_objects(output)
    if any(value.get("ok") is False or "error" in value for value in objects):
        return None
    candidates = [value for value in objects if isinstance(value.get("identities"), dict)]
    if not candidates:
        return None
    # Multiple identical envelopes are harmless (some CLIs mirror stdout to a
    # structured trailer). Conflicting envelopes are ambiguous and must never
    # authorize a commit based on whichever happened to be printed first.
    canonical = {json.dumps(value, sort_keys=True, separators=(",", ":")) for value in candidates}
    return candidates[-1] if len(canonical) == 1 else None


def _lark_device_authorization(output: str) -> dict[str, Any] | None:
    objects = _json_objects(output)
    if any(value.get("ok") is False or "error" in value for value in objects):
        return None
    candidates = [
        value
        for value in objects
        if (
            isinstance(value.get("device_code"), str)
            and isinstance(value.get("verification_url"), str)
            and value.get("device_code")
            and value.get("verification_url")
        )
    ]
    if not candidates:
        return None
    canonical = {json.dumps(value, sort_keys=True, separators=(",", ":")) for value in candidates}
    return candidates[-1] if len(canonical) == 1 else None


def _validated_lark_authorization_url(raw_url: str, *, phase_id: str) -> str | None:
    candidate = str(raw_url or "").strip().strip("\"'")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    if parsed.fragment:
        return None
    query_keys = {str(key).lower() for key in parse_qs(parsed.query, keep_blank_values=True)}
    if phase_id == "app_configuration":
        allowed = (
            host in {"open.feishu.cn", "open.larksuite.com"}
            and parsed.path == "/page/cli"
            and query_keys <= {"user_code", "lpv", "ocv", "from"}
        )
    else:
        allowed = (
            host in {"accounts.feishu.cn", "accounts.larksuite.com"}
            and parsed.path == "/oauth/v1/device/verify"
            # Lark CLI 1.0.78 includes an opaque public flow_id alongside
            # user_code.  Both are browser-navigation material; continuation
            # secrets such as device_code remain forbidden from the URL and
            # stay exclusively in Backend-encrypted flow state.
            and query_keys <= {"flow_id", "user_code"}
        )
    return candidate if allowed else None


def _lark_config_authorization_url(output: str) -> str | None:
    for candidate in re.findall(r"https://[^\s\"'<>]+", str(output or "")):
        validated = _validated_lark_authorization_url(candidate, phase_id="app_configuration")
        if validated:
            return validated
    return None


def _terminal_qr(output: str) -> str:
    cleaned = _ANSI_ESCAPE.sub("", str(output or ""))
    lines = [line.rstrip() for line in cleaned.splitlines() if re.search(r"[█▀▄]", line)]
    return "\n".join(lines) if len(lines) >= 10 else ""


def _query_user_code(url: str) -> str | None:
    try:
        values = parse_qs(urlsplit(url).query).get("user_code", [])
    except ValueError:
        return None
    return str(values[0]) if values else None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _identity_ready(identity: Any, *, require_token: bool) -> bool:
    if not isinstance(identity, dict):
        return False
    status = str(identity.get("status") or "").lower()
    ready = status in {"ready", "active", "configured", "authenticated"}
    verified = identity.get("verified") is True
    if not (ready and verified):
        return False
    if not require_token:
        return True
    return str(identity.get("tokenStatus") or identity.get("token_status") or "").lower() in {
        "valid",
        "active",
    }


def _lark_bot_ready(status: dict[str, Any] | None) -> bool:
    identities = status.get("identities") if isinstance(status, dict) else None
    bot = identities.get("bot") if isinstance(identities, dict) else None
    return _identity_ready(bot, require_token=False)


def _lark_full_identity_ready(status: dict[str, Any] | None) -> bool:
    identities = status.get("identities") if isinstance(status, dict) else None
    if not isinstance(identities, dict):
        return False
    return _identity_ready(identities.get("bot"), require_token=False) and _identity_ready(
        identities.get("user"), require_token=True
    )


def _safe_lark_identity_projection(status: dict[str, Any] | None) -> dict[str, Any]:
    identities = status.get("identities") if isinstance(status, dict) else None
    if not isinstance(identities, dict):
        return {}
    projected: dict[str, Any] = {}
    for identity_name in ("bot", "user"):
        identity = identities.get(identity_name)
        if not isinstance(identity, dict):
            continue
        projected[identity_name] = {
            key: identity.get(key)
            for key in ("status", "verified", "tokenStatus", "userName", "openId", "appName")
            if identity.get(key) is not None
        }
    return projected


@dataclass(frozen=True)
class _LarkAuthorizationFailure:
    reason: str
    flow_status: str
    retryable: bool
    identity: str | None = None
    provider_code: int | str | None = None
    message: str = ""


def _candidate_origin_projection(failure: _LarkAuthorizationFailure | None) -> dict[str, Any] | None:
    if failure is None or not failure.retryable:
        return None
    if failure.reason not in {
        "authorization_pending",
        "provider_slow_down",
        "provider_retryable_error",
        "provider_authorization_error",
    }:
        return None
    return {
        "reason": failure.reason,
        "flow_status": failure.flow_status,
        "retryable": True,
    }


def _candidate_origin_failure(flow: dict[str, Any]) -> _LarkAuthorizationFailure | None:
    value = flow.get("candidate_origin")
    if not isinstance(value, dict) or value.get("retryable") is not True:
        return None
    reason = str(value.get("reason") or "")
    if reason not in {
        "authorization_pending",
        "provider_slow_down",
        "provider_retryable_error",
        "provider_authorization_error",
    }:
        return None
    flow_status = str(value.get("flow_status") or "")
    if flow_status not in {
        AuthorizationFlowStatus.AWAITING_USER.value,
        AuthorizationFlowStatus.FAILED.value,
    }:
        return None
    return _LarkAuthorizationFailure(reason, flow_status, True, "user")


def _lark_authorization_failure(output: str) -> _LarkAuthorizationFailure:
    """Classify a failed OAuth continuation without leaking provider secrets.

    Only explicit provider evidence may end an attempt. Unknown, transport, and
    provider-side failures remain retryable because they do not prove that the
    browser consent was rejected or expired.
    """

    value = next(
        (
            candidate
            for candidate in reversed(_json_objects(output))
            if isinstance(candidate.get("error"), (dict, str)) or candidate.get("ok") is False
        ),
        {},
    )
    error_value = value.get("error")
    error = error_value if isinstance(error_value, dict) else {}
    error_type = str(error.get("type") or value.get("error") or "").lower()
    subtype = str(error.get("subtype") or value.get("error_subtype") or "").lower()
    message = str(error.get("message") or value.get("message") or "")
    hint = str(error.get("hint") or value.get("hint") or "")
    provider_code = error.get("code", value.get("code"))
    identity = str(value.get("identity") or error.get("identity") or "").lower() or None
    normalized = " ".join(
        str(item).lower()
        for item in (error_type, subtype, message, hint, provider_code, output)
        if item not in {None, ""}
    )

    if any(marker in normalized for marker in ("authorization_pending", "authorization pending")):
        return _LarkAuthorizationFailure(
            "authorization_pending",
            AuthorizationFlowStatus.AWAITING_USER.value,
            True,
            identity,
            provider_code,
            message,
        )
    if "slow_down" in normalized or "slow down" in normalized:
        return _LarkAuthorizationFailure(
            "provider_slow_down",
            AuthorizationFlowStatus.AWAITING_USER.value,
            True,
            identity,
            provider_code,
            message,
        )
    if any(marker in normalized for marker in ("access_denied", "authorization denied", "user denied")):
        return _LarkAuthorizationFailure(
            "access_denied",
            AuthorizationFlowStatus.CANCELLED.value,
            False,
            identity,
            provider_code,
            message,
        )
    if any(
        marker in normalized
        for marker in (
            "expired_token",
            "device code is invalid",
            "invalid_device_code",
            "device_code_expired",
            "device authorization expired",
        )
    ):
        return _LarkAuthorizationFailure(
            "authorization_expired",
            AuthorizationFlowStatus.EXPIRED.value,
            False,
            identity,
            provider_code,
            message,
        )
    if (
        (error_type == "validation" and subtype == "invalid_argument")
        or str(provider_code) == "20001"
        or any(marker in normalized for marker in ("invalid_request", "invalid request", "请求不合法"))
    ):
        return _LarkAuthorizationFailure(
            "provider_invalid_request",
            AuthorizationFlowStatus.FAILED.value,
            False,
            identity,
            provider_code,
            message,
        )
    provider_status = int(provider_code) if str(provider_code).isdigit() else None
    if (
        any(
            marker in normalized
            for marker in ("timeout", "timed out", "econnreset", "connection reset", "dns", "tls", "rate limit")
        )
        or provider_status == 429
        or (provider_status is not None and 500 <= provider_status <= 599)
    ):
        return _LarkAuthorizationFailure(
            "provider_retryable_error",
            AuthorizationFlowStatus.FAILED.value,
            True,
            identity,
            provider_code,
            message,
        )
    return _LarkAuthorizationFailure(
        "provider_authorization_error",
        AuthorizationFlowStatus.FAILED.value,
        True,
        identity,
        provider_code,
        message,
    )


def _lark_user_credential_failure(output: str) -> _LarkAuthorizationFailure | None:
    """Return a repair trigger only for a proven User credential failure."""

    for value in reversed(_json_objects(output)):
        error = value.get("error")
        if not isinstance(error, dict) or str(value.get("identity") or "").lower() != "user":
            continue
        error_type = str(error.get("type") or "").lower()
        subtype = str(error.get("subtype") or "").lower()
        message = str(error.get("message") or "")
        hint = str(error.get("hint") or "")
        provider_code = error.get("code")
        normalized = " ".join((error_type, subtype, message.lower(), hint.lower(), str(provider_code).lower()))
        if any(marker in normalized for marker in ("missing_scope", "insufficient_scope", "permission denied")):
            return None
        credential_markers = (
            "token_expired",
            "expired_token",
            "refresh_token_expired",
            "invalid_token",
            "invalid_grant",
            "login_required",
            "not_logged_in",
            "token expired",
            "token is invalid",
            "unauthorized",
        )
        if error_type in {"authorization", "authentication", "oauth"} and any(
            marker in normalized for marker in credential_markers
        ):
            return _LarkAuthorizationFailure(
                "user_token_expired",
                AuthorizationFlowStatus.FAILED.value,
                False,
                "user",
                provider_code,
                message,
            )
    return None


def _lark_confirmation_required(output: str) -> dict[str, Any] | None:
    """Extract lark-cli's structured exit-10 confirmation envelope."""

    cleaned = "\n".join(line.removeprefix("[stderr] ") for line in str(output or "").splitlines())
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        error = value.get("error")
        if not isinstance(error, dict):
            continue
        if (
            value.get("ok") is False
            and error.get("type") == "confirmation"
            and error.get("subtype") == "confirmation_required"
            and error.get("risk") == "high-risk-write"
            and isinstance(error.get("action"), str)
            and str(error.get("action")).strip()
        ):
            return error
    return None


_BROWSER_WATCHERS: set[str] = set()
_BROWSER_WATCHERS_LOCK = threading.Lock()


def _browser_job_id(owner_user_id: str, provider: str, profile_id: str) -> str:
    return hashlib.sha256(f"{owner_user_id}\0{provider}\0{profile_id}".encode()).hexdigest()[:24]


@dataclass(frozen=True)
class ManagedCliServiceResult:
    payload: dict[str, Any]
    exit_code: int

    @property
    def content(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class ManagedCliExecutionPlan:
    match: ManagedCliMatch
    owner_user_id: str
    profile_id: str | None
    profile_revision: float | None
    toolchain_path: Path
    toolchain_revision: str
    adapter_contract_fingerprint: str = ""
    package_contract_fingerprint: str = ""
    resolved_distribution: str = ""
    resolved_version: str = ""
    resolved_integrity: str = ""
    runtime_image_digest: str = ""
    resolution_fingerprint: str = ""
    toolchain_lease_id: str = ""
    toolchain_lease_expires_at: float = 0
    profile_state_revision: str = ""
    executable_path: Path | None = None

    def approval_preview(self) -> str:
        if self.match.adapter_id == "lark-cli" and self.match.route == ManagedCliRoute.INSTALLER:
            return json.dumps(
                {
                    "adapter_id": self.match.adapter_id,
                    "action": self.match.action.value,
                    "route": self.match.route.value,
                    "installation_scope": "host_user_global",
                    "requested_distribution": self.match.distribution,
                    "resolved_distribution": self.resolved_distribution,
                    "resolved_version": self.resolved_version,
                    "resolved_integrity": self.resolved_integrity,
                    "current_executable_path": (
                        str(self.executable_path) if self.executable_path else None
                    ),
                    "current_version": self.toolchain_revision.removeprefix("host-global:"),
                    "resolution_fingerprint": self.resolution_fingerprint,
                    "destructive": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        if self.match.adapter_id == "lark-cli":
            return json.dumps(
                {
                    "adapter_id": self.match.adapter_id,
                    "action": self.match.action.value,
                    "route": self.match.route.value,
                    "runtime": "host",
                    "argv": list(self.match.argv),
                    "owner_user_id": self.owner_user_id,
                    "profile_id": self.profile_id,
                    "profile_revision": self.profile_revision,
                    "profile_state_revision": self.profile_state_revision,
                    "executable_path": str(self.executable_path) if self.executable_path else None,
                    "version": self.toolchain_revision.removeprefix("host-global:"),
                    "adapter_contract_fingerprint": self.adapter_contract_fingerprint,
                    "package_contract_fingerprint": self.package_contract_fingerprint,
                    "destructive": self.match.destructive,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return json.dumps(
            {
                "adapter_id": self.match.adapter_id,
                "action": self.match.action.value,
                "route": self.match.route.value,
                "argv": list(self.match.argv),
                "owner_user_id": self.owner_user_id,
                "profile_id": self.profile_id,
                "profile_revision": self.profile_revision,
                "profile_state_revision": self.profile_state_revision,
                "toolchain_revision": self.toolchain_revision,
                "executable_path": str(self.executable_path) if self.executable_path else None,
                "adapter_contract_fingerprint": self.adapter_contract_fingerprint,
                "package_contract_fingerprint": self.package_contract_fingerprint,
                "resolved_distribution": self.resolved_distribution,
                "resolved_version": self.resolved_version,
                "resolved_integrity": self.resolved_integrity,
                "runtime_image_digest": self.runtime_image_digest,
                "resolution_fingerprint": self.resolution_fingerprint,
                "destructive": self.match.destructive,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def destructive_approval_binding(
        self,
        *,
        action: str | None = None,
        risk: str | None = None,
    ) -> str:
        frozen = {
            "adapter": self.match.adapter_id,
            "argv": list(self.match.argv),
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "profile_state_revision": self.profile_state_revision,
            "toolchain_revision": self.toolchain_revision,
            "executable_path": str(self.executable_path) if self.executable_path else None,
            "adapter_contract_fingerprint": self.adapter_contract_fingerprint,
            "package_contract_fingerprint": self.package_contract_fingerprint,
            "resolution_fingerprint": self.resolution_fingerprint,
            "runtime_image_digest": self.runtime_image_digest,
            "toolchain_lease_id": self.toolchain_lease_id,
            "action": action,
            "risk": risk,
        }
        return hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GenericNodeCliInstallMatch:
    """One user-approved npm CLI install with no Provider semantics."""

    package: str
    distribution: str
    argv: tuple[str, ...]
    adapter_id: str = "generic-node-cli"
    action: ManagedCliAction = ManagedCliAction.INSTALL
    route: ManagedCliRoute = ManagedCliRoute.INSTALLER
    requires_network: bool = True
    workspace_writable: bool = False
    destructive: bool = False


@dataclass(frozen=True)
class GenericNodeCliInstallPlan:
    match: GenericNodeCliInstallMatch
    resolved_distribution: str
    resolved_version: str
    resolved_integrity: str
    executables: tuple[str, ...]
    runtime_image_digest: str
    toolchain_revision: str
    resolution_fingerprint: str

    def approval_preview(self) -> str:
        return json.dumps(
            {
                "kind": "generic_node_cli_install",
                "package": self.match.package,
                "requested_distribution": self.match.distribution,
                "resolved_distribution": self.resolved_distribution,
                "resolved_version": self.resolved_version,
                "resolved_integrity": self.resolved_integrity,
                "executables": list(self.executables),
                "runtime_image_digest": self.runtime_image_digest,
                "toolchain_revision": self.toolchain_revision,
                "resolution_fingerprint": self.resolution_fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


@dataclass(frozen=True)
class ToolchainRollbackPlan:
    plan_id: str
    owner_user_id: str
    adapter_id: str
    target_revision: str
    expected_current_revision: str
    adapter_contract_fingerprint: str
    package_contract_fingerprint: str
    credential_state_fingerprint: str
    runtime_image_digest: str
    target_version: str
    target_integrity: str
    expires_at: float
    toolchain_lease_id: str
    binding: str

    def approval_preview(self) -> str:
        return json.dumps(
            {
                "action": "managed_toolchain_rollback",
                "plan_id": self.plan_id,
                "adapter_id": self.adapter_id,
                "from_revision": self.expected_current_revision,
                "target_revision": self.target_revision,
                "target_version": self.target_version,
                "target_integrity": self.target_integrity,
                "runtime_image_digest": self.runtime_image_digest,
                "expires_at": self.expires_at,
                "binding": self.binding,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def record(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}

    @classmethod
    def from_record(cls, value: dict[str, object]) -> ToolchainRollbackPlan:
        return cls(
            plan_id=str(value.get("plan_id") or ""),
            owner_user_id=str(value.get("owner_user_id") or ""),
            adapter_id=str(value.get("adapter_id") or ""),
            target_revision=str(value.get("target_revision") or ""),
            expected_current_revision=str(value.get("expected_current_revision") or ""),
            adapter_contract_fingerprint=str(value.get("adapter_contract_fingerprint") or ""),
            package_contract_fingerprint=str(value.get("package_contract_fingerprint") or ""),
            credential_state_fingerprint=str(value.get("credential_state_fingerprint") or ""),
            runtime_image_digest=str(value.get("runtime_image_digest") or ""),
            target_version=str(value.get("target_version") or ""),
            target_integrity=str(value.get("target_integrity") or ""),
            expires_at=float(value.get("expires_at") or 0),
            toolchain_lease_id=str(value.get("toolchain_lease_id") or ""),
            binding=str(value.get("binding") or ""),
        )


class ManagedCliService:
    def __init__(
        self,
        backend: object,
        *,
        paths: PuddingClawPaths | None = None,
        registry: ManagedCliRegistry | None = None,
        authorization_drivers: AuthorizationDriverRegistry | None = None,
    ) -> None:
        self.backend = backend
        self.paths = paths or PuddingClawPaths.from_environment()
        self.registry = registry or ManagedCliRegistry()
        if authorization_drivers is None:
            default_drivers = AuthorizationDriverRegistry().drivers()
            registered_ids = {adapter.adapter_id for adapter in self.registry.adapters()}
            authorization_drivers = AuthorizationDriverRegistry(
                tuple(driver for driver in default_drivers if driver.adapter_id in registered_ids)
            )
        self.authorization_drivers = authorization_drivers
        for driver in self.authorization_drivers.drivers():
            adapter = self.registry.adapter(driver.adapter_id)
            if adapter.provider != driver.provider or not re.fullmatch(r"[0-9a-f]{64}", driver.contract_fingerprint):
                raise ValueError("authorization Driver does not match its registered Adapter contract")
        runtime_owner = getattr(backend, "manager", backend)
        runtime_contract = str(getattr(runtime_owner, "runtime_contract", "")).strip()
        if not runtime_contract:
            raise ValueError("managed CLI backend does not expose a runtime contract")
        self.toolchains = ToolchainManager(self.paths, runtime_contract)
        self._recover_browser_watchers()

    def _authorization_driver(
        self,
        match: ManagedCliMatch,
        *,
        required: bool = False,
    ) -> AuthorizationDriver | None:
        driver = self.authorization_drivers.for_adapter(match.adapter_id, required=required)
        if driver is not None and not driver.handles(match):
            raise ValueError("authorization Driver rejected the Adapter/provider identity")
        return driver

    def _adapter_contract_fingerprint(self, adapter_id: str) -> str:
        adapter_fingerprint = self.registry.adapter_contract_fingerprint(adapter_id)
        driver = self.authorization_drivers.for_adapter(adapter_id, required=False)
        if driver is None:
            return adapter_fingerprint
        return hashlib.sha256(f"{adapter_fingerprint}\0{driver.contract_fingerprint}".encode()).hexdigest()

    def _runtime_image_digest(self) -> str:
        resolver = getattr(self.backend, "managed_runtime_image_digest", None)
        if not callable(resolver):
            raise ValueError("managed CLI backend does not expose an immutable runtime image digest")
        value = str(resolver() or "")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            return value
        raise ValueError("managed CLI backend returned an invalid runtime image digest")

    @staticmethod
    def _resolution_fingerprint(
        *,
        adapter_id: str,
        package_fingerprint: str,
        distribution: str,
        version: str,
        integrity: str,
        runtime_image_digest: str,
    ) -> str:
        payload = {
            "adapter_id": adapter_id,
            "package_fingerprint": package_fingerprint,
            "distribution": distribution,
            "version": version,
            "integrity": integrity,
            "runtime_image_digest": runtime_image_digest,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _generic_cli_resolution_fingerprint(
        *,
        package: str,
        distribution: str,
        version: str,
        integrity: str,
        executables: tuple[str, ...],
        runtime_image_digest: str,
        toolchain_revision: str,
    ) -> str:
        payload = {
            "kind": "generic_node_cli_install",
            "package": package,
            "distribution": distribution,
            "version": version,
            "integrity": integrity,
            "executables": list(executables),
            "runtime_image_digest": runtime_image_digest,
            "toolchain_revision": toolchain_revision,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def plan_command(
        self,
        command: str,
        context: dict[str, Any],
    ) -> ManagedCliExecutionPlan | GenericNodeCliInstallPlan | None:
        """Plan an Adapter command or a generic credentialless npm CLI install."""

        try:
            managed = self.registry.match(command)
        except UnsupportedManagedCliCommand:
            generic = generic_node_cli_install(command)
            if generic is None:
                raise
            requested_distribution, package = generic
            return self._plan_generic_node_cli_install(
                package=package,
                distribution=requested_distribution,
            )
        if managed is None:
            return None
        return self.plan(managed, context)

    def _plan_generic_node_cli_install(
        self,
        *,
        package: str,
        distribution: str,
    ) -> GenericNodeCliInstallPlan:
        resolver = getattr(self.backend, "resolve_generic_node_cli", None)
        if not callable(resolver):
            raise ValueError("host CLI runtime cannot resolve package metadata")
        resolution = resolver(distribution=distribution, package=package)
        resolved_version = str(getattr(resolution, "version", "") or "")
        resolved_distribution = str(getattr(resolution, "distribution", "") or "")
        resolved_integrity = str(getattr(resolution, "integrity", "") or "")
        runtime_image_digest = str(getattr(resolution, "runtime_image_digest", "") or "")
        executables = tuple(sorted(set(str(item) for item in getattr(resolution, "executables", ()))))
        if (
            getattr(resolution, "package", None) != package
            or resolved_distribution != f"{package}@{resolved_version}"
            or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", resolved_version)
            or not re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", resolved_integrity)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image_digest)
            or not executables
            or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item) is None for item in executables)
        ):
            raise ValueError("npm package does not expose a reproducible top-level CLI contract")
        current_resolver = getattr(self.backend, "generic_node_runtime_current", None)
        if not callable(current_resolver):
            raise ValueError("host CLI runtime cannot inspect its current revision")
        current = current_resolver(runtime_image_digest)
        fingerprint = self._generic_cli_resolution_fingerprint(
            package=package,
            distribution=resolved_distribution,
            version=resolved_version,
            integrity=resolved_integrity,
            executables=executables,
            runtime_image_digest=runtime_image_digest,
            toolchain_revision=current.name,
        )
        return GenericNodeCliInstallPlan(
            match=GenericNodeCliInstallMatch(
                package=package,
                distribution=distribution,
                argv=("npm", "install", "--global", distribution),
            ),
            resolved_distribution=resolved_distribution,
            resolved_version=resolved_version,
            resolved_integrity=resolved_integrity,
            executables=executables,
            runtime_image_digest=runtime_image_digest,
            toolchain_revision=current.name,
            resolution_fingerprint=fingerprint,
        )

    def _recover_browser_watchers(self) -> None:
        """Reconcile Runner inventory, durable leases, and Flow evidence.

        Docker is execution infrastructure, not the source of truth. A live
        Runner may be rebound to its deterministic Profile lease after a crash;
        a lease with no live Runner is released; and an active Flow with no
        phase-specific recovery evidence is reset or terminalized.
        """

        if not hasattr(self.backend, "list_managed_browser_auth_jobs"):
            return
        owner_user_id = trusted_owner_user_id()
        try:
            jobs = self.backend.list_managed_browser_auth_jobs(owner_user_id=owner_user_id)
        except Exception:  # noqa: BLE001
            # Failure to inventory the runtime is unknown state, not evidence
            # that every Runner disappeared. Never expire durable work here.
            return
        if not isinstance(jobs, list):
            return
        store = CredentialProfileStore(self.paths, owner_user_id)
        if not jobs and not store.list_profiles():
            return
        flow_store = self._authorization_flow_store(store, owner_user_id)
        live_jobs: set[tuple[str, str, str]] = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            provider = str(job.get("provider") or "")
            try:
                adapter = self.registry.for_provider(provider)
                credential_state = adapter.credential_state
                driver = self.authorization_drivers.for_adapter(adapter.adapter_id)
            except ValueError:
                continue
            assert driver is not None
            profile_id = str(job.get("profile_id") or "")
            browser_job_id = str(job.get("browser_job_id") or "")
            if not profile_id or not browser_job_id:
                continue
            if (
                job.get("adapter_id") != adapter.adapter_id
                or job.get("authorization_contract_fingerprint")
                != self._adapter_contract_fingerprint(adapter.adapter_id)
                or job.get("credential_state_fingerprint") != credential_state.fingerprint
            ):
                try:
                    with store.profile_lock(provider, profile_id):
                        current = store.resolve(
                            provider,
                            explicit_profile_id=profile_id,
                            create_default=False,
                        )
                        store.finish_browser_job(
                            profile_id,
                            browser_job_id,
                            "expired",
                            str((current or {}).get("credential_state_fingerprint") or ""),
                        )
                    self.backend.finalize_managed_browser_auth_cli(
                        owner_user_id=owner_user_id,
                        provider=provider,
                        profile_id=profile_id,
                        browser_job_id=browser_job_id,
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue
            live_jobs.add((provider, profile_id, browser_job_id))
            try:
                with store.profile_lock(provider, profile_id):
                    current = store.resolve(
                        provider,
                        explicit_profile_id=profile_id,
                        create_default=False,
                    )
                    if current is None:
                        self.backend.finalize_managed_browser_auth_cli(
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=browser_job_id,
                        )
                        continue
                    flow = flow_store.active(provider, profile_id)
                    try:
                        browser_phase = driver.graph.node(str((flow or {}).get("phase_id") or ""))
                    except ValueError:
                        browser_phase = None
                    if browser_phase is None or browser_phase.kind != AuthorizationPhaseKind.BROWSER_CONFIGURATION:
                        self.backend.finalize_managed_browser_auth_cli(
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=browser_job_id,
                        )
                        continue
                    phase_one_staged = bool(
                        browser_phase.phase.phase_id in flow.get("completed_phase_ids", [])
                        and flow_store.has_staged_state(flow)
                    )
                    leased_job_id = str(current.get("browser_job_id") or "")
                    if not leased_job_id and phase_one_staged:
                        # Crash window after staging and lease release but
                        # before container ACK: the Runner is now redundant.
                        self.backend.finalize_managed_browser_auth_cli(
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=browser_job_id,
                        )
                        continue
                    if not leased_job_id:
                        app_setup_waiting = bool(
                            flow is not None
                            and flow.get("status") == AuthorizationFlowStatus.AWAITING_USER.value
                            and flow.get("phase_id") == browser_phase.phase.phase_id
                            and browser_phase.phase.phase_id not in flow.get("completed_phase_ids", [])
                        )
                        if app_setup_waiting:
                            # Deterministic job identity and Adapter
                            # fingerprint prove this is the Flow's lost lease.
                            store.begin_browser_job(
                                profile_id,
                                browser_job_id,
                                credential_state.fingerprint,
                            )
                        else:
                            self.backend.finalize_managed_browser_auth_cli(
                                owner_user_id=owner_user_id,
                                provider=provider,
                                profile_id=profile_id,
                                browser_job_id=browser_job_id,
                            )
                            continue
                    elif leased_job_id != browser_job_id:
                        # Never replace another durable lease merely because a
                        # stale container happens to be alive.
                        self.backend.finalize_managed_browser_auth_cli(
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=browser_job_id,
                        )
                        continue
            except Exception:  # noqa: BLE001
                continue
            self._start_browser_watcher(
                owner_user_id=owner_user_id,
                provider=provider,
                profile_id=profile_id,
                browser_job_id=browser_job_id,
                credential_state=credential_state,
            )

        # The runtime inventory succeeded, so absence is now meaningful. Clear
        # leases whose Runner no longer exists before reconciling their Flows.
        for profile in store.list_profiles():
            provider = str(profile.get("provider") or "")
            profile_id = str(profile.get("profile_id") or "")
            browser_job_id = str(profile.get("browser_job_id") or "")
            fingerprint = str(profile.get("credential_state_fingerprint") or "")
            if not browser_job_id or (provider, profile_id, browser_job_id) in live_jobs:
                continue
            try:
                with store.profile_lock(provider, profile_id):
                    current = store.resolve(
                        provider,
                        explicit_profile_id=profile_id,
                        create_default=False,
                    )
                    if current is None or current.get("browser_job_id") != browser_job_id:
                        continue
                    # The first inventory predates this lock. Another request
                    # may have created the Runner while we were waiting, so
                    # absence must be re-proven at the mutation boundary.
                    try:
                        refreshed_jobs = self.backend.list_managed_browser_auth_jobs(owner_user_id=owner_user_id)
                    except Exception:  # noqa: BLE001
                        continue
                    if not isinstance(refreshed_jobs, list):
                        continue
                    runner_now_live = any(
                        isinstance(item, dict)
                        and item.get("provider") == provider
                        and item.get("profile_id") == profile_id
                        and item.get("browser_job_id") == browser_job_id
                        for item in refreshed_jobs
                    )
                    if runner_now_live:
                        try:
                            credential_state = self.registry.state_for_provider(provider)
                        except ValueError:
                            continue
                        self._start_browser_watcher(
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=browser_job_id,
                            credential_state=credential_state,
                        )
                        continue
                    store.finish_browser_job(
                        profile_id,
                        browser_job_id,
                        "expired",
                        fingerprint,
                    )
                    current = store.resolve(
                        provider,
                        explicit_profile_id=profile_id,
                        create_default=False,
                    )
                    if current is not None:
                        self._reconcile_authorization_flow_locked(
                            flow_store=flow_store,
                            current=current,
                            provider=provider,
                            profile_id=profile_id,
                        )
            except (OSError, ValueError):
                continue

        # Flows may outlive both Profile leases and containers if the Backend
        # crashed between their separate durable writes. Reconcile every one,
        # including User-consent and commit-recovery states.
        for flow in flow_store.active_flows():
            provider = str(flow.get("provider") or "")
            profile_id = str(flow.get("profile_id") or "")
            try:
                with store.profile_lock(provider, profile_id):
                    current = store.resolve(
                        provider,
                        explicit_profile_id=profile_id,
                        create_default=False,
                    )
                    if current is not None:
                        self._reconcile_authorization_flow_locked(
                            flow_store=flow_store,
                            current=current,
                            provider=provider,
                            profile_id=profile_id,
                        )
            except (OSError, ValueError):
                continue

    def plan(self, match: ManagedCliMatch, context: dict[str, Any]) -> ManagedCliExecutionPlan:
        self.registry.validate_match(match)
        adapter = self.registry.adapter(match.adapter_id)
        if match.authorization_phase:
            driver = self._authorization_driver(match, required=True)
            assert driver is not None
            driver.graph.node(match.authorization_phase)
        adapter_contract_fingerprint = self._adapter_contract_fingerprint(match.adapter_id)
        resolved_distribution = ""
        resolved_version = ""
        resolved_integrity = ""
        resolution_fingerprint = ""
        host_lark_resolution = None
        if match.adapter_id == "lark-cli" and match.route != ManagedCliRoute.INSTALLER:
            resolver = getattr(self.backend, "resolve_host_lark_cli", None)
            if callable(resolver):
                host_lark_resolution = resolver()
            else:
                # Lightweight test/control-plane backends need not re-expose a
                # pure host probe. Execution still remains backend-owned.
                from runtime_identity.host_lark_cli import HostLarkCliRuntime

                host_lark_resolution = HostLarkCliRuntime(self.paths).resolve()
            executable = str(getattr(host_lark_resolution, "executable", "") or "")
            version = str(getattr(host_lark_resolution, "version", "") or "missing")
            runtime_image_digest = "sha256:" + hashlib.sha256(
                f"host-lark-cli-v1\0{executable}\0{version}".encode()
            ).hexdigest()
        elif match.route == ManagedCliRoute.INSTALLER:
            resolver_name = (
                "resolve_host_lark_package" if match.adapter_id == "lark-cli" else "resolve_managed_node_cli"
            )
            resolver = getattr(self.backend, resolver_name, None)
            if not callable(resolver) or not match.distribution:
                raise ValueError("managed CLI backend cannot resolve package metadata")
            # This is a trusted, credentialless control-plane lookup against
            # the Adapter-fixed npm package. It accepts no Agent URL, registry,
            # environment, or executable input. The resolved immutable tuple
            # is shown in the subsequent installation approval.
            resolution = resolver(
                distribution=match.distribution,
                package=adapter.toolchain_package.package,
            )
            resolved_distribution = str(getattr(resolution, "distribution", "") or "")
            resolved_version = str(getattr(resolution, "version", "") or "")
            resolved_integrity = str(getattr(resolution, "integrity", "") or "")
            runtime_image_digest = (
                ""
                if match.adapter_id == "lark-cli"
                else str(getattr(resolution, "runtime_image_digest", "") or "")
            )
            if (
                getattr(resolution, "package", None) != adapter.toolchain_package.package
                or resolved_distribution != f"{adapter.toolchain_package.package}@{resolved_version}"
                or not re.fullmatch(
                    r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",
                    resolved_version,
                )
                or not re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", resolved_integrity)
                or (
                    match.adapter_id != "lark-cli"
                    and not re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image_digest)
                )
                or (
                    adapter.toolchain_package.compatibility
                    and not version_satisfies(
                        resolved_version,
                        adapter.toolchain_package.compatibility,
                    )
                )
                or (
                    adapter.toolchain_package.expected_integrity
                    and resolved_integrity != adapter.toolchain_package.expected_integrity
                )
            ):
                raise ValueError("resolved managed package metadata violates the Adapter contract")
            resolution_fingerprint = self._resolution_fingerprint(
                adapter_id=match.adapter_id,
                package_fingerprint=adapter.toolchain_package.fingerprint,
                distribution=resolved_distribution,
                version=resolved_version,
                integrity=resolved_integrity,
                runtime_image_digest=runtime_image_digest,
            )
            self.toolchains.record_event(
                match.adapter_id,
                {
                    "event": "package_resolution_prepared",
                    "requested_distribution": match.distribution,
                    "resolved_distribution": resolved_distribution,
                    "resolved_integrity": resolved_integrity,
                    "runtime_image_digest": runtime_image_digest,
                    "resolution_fingerprint": resolution_fingerprint,
                    "created_at": time.time(),
                },
            )
            # An install/update is also the repair path for an obsolete or
            # incompatible current revision. Freeze its identity for CAS, but
            # do not require the old contract to validate before replacement.
            if match.adapter_id == "lark-cli":
                host_resolver = getattr(self.backend, "resolve_host_lark_cli", None)
                if not callable(host_resolver):
                    raise ValueError("managed CLI backend cannot resolve the host lark-cli")
                host_lark_resolution = host_resolver()
            else:
                ref = self.toolchains.resolve_node(match.adapter_id)
        else:
            runtime_image_digest = self._runtime_image_digest()
            ref = self.toolchains.resolve_for_adapter(
                adapter_id=match.adapter_id,
                spec=adapter.toolchain_package,
                adapter_contract_fingerprint=adapter_contract_fingerprint,
                credential_state_fingerprint=adapter.credential_state.fingerprint,
                runtime_image_digest=runtime_image_digest,
            )
        owner_user_id = trusted_owner_user_id()
        profile_id: str | None = None
        profile_revision: float | None = None
        profile_state_revision = ""
        if match.requires_profile:
            store = CredentialProfileStore(self.paths, owner_user_id)
            profile = store.resolve(
                match.provider or "",
                project_id=(str(context.get("project_id")) if context.get("project_id") else None),
                explicit_profile_id=(
                    str(context.get("credential_profile_id")) if context.get("credential_profile_id") else None
                ),
            )
            assert profile is not None
            # Reconfiguration is transactionally staged by AuthorizationFlowStore.
            # It is therefore safe to repair an active Profile without deleting or
            # overwriting its last-known-good Vault before final verification.
            profile_id = str(profile["profile_id"])
            profile_revision = float(profile.get("updated_at") or 0)
            profile_state_revision = store.state_revision(match.provider or "", profile_id)
        if host_lark_resolution is not None:
            executable_path = getattr(host_lark_resolution, "executable", None)
            version = str(getattr(host_lark_resolution, "version", "") or "missing")
            # Keep the legacy plan field inside PuddingClaw's own data root.
            # The real host executable has its own explicit field and must not
            # be mistaken for a writable Toolchain publication directory.
            toolchain_path = self.paths.root / "runtime" / "host-lark-cli"
            toolchain_revision = f"host-global:{version}"
            lease_id = ""
            lease_expires_at = 0.0
        else:
            executable_path = None
            toolchain_path = ref.host_path
            toolchain_revision = ref.host_path.name
            lease_id, lease_expires_at = self.toolchains.acquire_revision_lease(
                adapter_id=match.adapter_id,
                revision=ref.host_path.name,
                owner_kind="plan",
                owner_id=str(context.get("run_id") or context.get("query_id") or uuid.uuid4().hex),
                contract_fingerprint=adapter_contract_fingerprint,
            )
        return ManagedCliExecutionPlan(
            match=match,
            owner_user_id=owner_user_id,
            profile_id=profile_id,
            profile_revision=profile_revision,
            toolchain_path=toolchain_path,
            toolchain_revision=toolchain_revision,
            adapter_contract_fingerprint=adapter_contract_fingerprint,
            package_contract_fingerprint=adapter.toolchain_package.fingerprint,
            resolved_distribution=resolved_distribution,
            resolved_version=resolved_version,
            resolved_integrity=resolved_integrity,
            runtime_image_digest=runtime_image_digest,
            resolution_fingerprint=resolution_fingerprint,
            toolchain_lease_id=lease_id,
            toolchain_lease_expires_at=lease_expires_at,
            profile_state_revision=profile_state_revision,
            executable_path=executable_path,
        )

    @staticmethod
    def _rollback_binding(payload: dict[str, object]) -> str:
        frozen = {key: value for key, value in payload.items() if key != "binding"}
        return hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def list_toolchain_revisions(self, adapter_id: str) -> list[dict[str, object]]:
        adapter = self.registry.adapter(adapter_id)
        runtime_image_digest = self._runtime_image_digest()
        return self.toolchains.list_revisions(
            adapter_id=adapter_id,
            spec=adapter.toolchain_package,
            adapter_contract_fingerprint=self._adapter_contract_fingerprint(adapter_id),
            credential_state_fingerprint=adapter.credential_state.fingerprint,
            runtime_image_digest=runtime_image_digest,
        )

    def plan_toolchain_rollback(
        self,
        adapter_id: str,
        target_revision: str,
        *,
        ttl_seconds: int = 300,
    ) -> ToolchainRollbackPlan:
        if ttl_seconds < 30 or ttl_seconds > 900:
            raise ValueError("Toolchain rollback approval TTL is invalid")
        adapter = self.registry.adapter(adapter_id)
        adapter_fingerprint = self._adapter_contract_fingerprint(adapter_id)
        runtime_image_digest = self._runtime_image_digest()
        ref = self.toolchains.resolve_for_adapter(
            adapter_id=adapter_id,
            spec=adapter.toolchain_package,
            adapter_contract_fingerprint=adapter_fingerprint,
            credential_state_fingerprint=adapter.credential_state.fingerprint,
            runtime_image_digest=runtime_image_digest,
        )
        revisions = self.list_toolchain_revisions(adapter_id)
        target = next(
            (item for item in revisions if item.get("revision") == target_revision),
            None,
        )
        if target is None:
            raise ValueError("Toolchain rollback target is unavailable or incompatible")
        if target_revision == ref.host_path.name:
            raise ValueError("Toolchain rollback target is already current")
        plan_id = uuid.uuid4().hex
        lease_id, _lease_expires_at = self.toolchains.acquire_revision_lease(
            adapter_id=adapter_id,
            revision=target_revision,
            owner_kind="rollback",
            owner_id=plan_id,
            contract_fingerprint=adapter_fingerprint,
            ttl_seconds=ttl_seconds,
        )
        payload: dict[str, object] = {
            "plan_id": plan_id,
            "owner_user_id": trusted_owner_user_id(),
            "adapter_id": adapter_id,
            "target_revision": target_revision,
            "expected_current_revision": ref.host_path.name,
            "adapter_contract_fingerprint": adapter_fingerprint,
            "package_contract_fingerprint": adapter.toolchain_package.fingerprint,
            "credential_state_fingerprint": adapter.credential_state.fingerprint,
            "runtime_image_digest": runtime_image_digest,
            "target_version": str(target.get("version") or ""),
            "target_integrity": str(target.get("integrity") or ""),
            "expires_at": time.time() + ttl_seconds,
            "toolchain_lease_id": lease_id,
        }
        payload["binding"] = self._rollback_binding(payload)
        plan = ToolchainRollbackPlan.from_record(payload)
        self.toolchains.store_rollback_plan(adapter_id, plan.record())
        self.toolchains.record_event(
            adapter_id,
            {
                "event": "rollback_planned",
                "plan_id": plan.plan_id,
                "from_revision": plan.expected_current_revision,
                "target_revision": plan.target_revision,
                "binding": plan.binding,
                "created_at": time.time(),
            },
        )
        return plan

    def execute_toolchain_rollback(
        self,
        adapter_id: str,
        plan_id: str,
        binding: str,
        *,
        confirmed: bool = False,
    ) -> ManagedCliServiceResult:
        consumed_plan: ToolchainRollbackPlan | None = None
        if not confirmed:
            return self._error(
                "managed_toolchain_rollback_confirmation_required",
                "Toolchain rollback requires an explicit user confirmation.",
            )
        try:
            record = self.toolchains.consume_rollback_plan(adapter_id, plan_id, binding)
            plan = ToolchainRollbackPlan.from_record(record)
            consumed_plan = plan
            adapter = self.registry.adapter(adapter_id)
            if (
                plan.adapter_id != adapter_id
                or plan.owner_user_id != trusted_owner_user_id()
                or plan.binding != self._rollback_binding(record)
                or time.time() > plan.expires_at
                or plan.adapter_contract_fingerprint != self._adapter_contract_fingerprint(adapter_id)
                or plan.package_contract_fingerprint != adapter.toolchain_package.fingerprint
                or plan.credential_state_fingerprint != adapter.credential_state.fingerprint
                or plan.runtime_image_digest != self._runtime_image_digest()
            ):
                raise ValueError("Toolchain rollback plan is stale or incompatible")
            ref = self.toolchains.rollback_node(
                adapter_id=adapter_id,
                release_id=plan.target_revision,
                spec=adapter.toolchain_package,
                adapter_contract_fingerprint=plan.adapter_contract_fingerprint,
                credential_state_fingerprint=plan.credential_state_fingerprint,
                runtime_image_digest=plan.runtime_image_digest,
                expected_revision=plan.expected_current_revision,
            )
            audit_status = "recorded"
            try:
                self.toolchains.record_event(
                    adapter_id,
                    {
                        "event": "rollback_succeeded",
                        "plan_id": plan.plan_id,
                        "from_revision": plan.expected_current_revision,
                        "target_revision": plan.target_revision,
                        "completed_at": time.time(),
                    },
                )
            except (OSError, ValueError):
                # ``rollback_node`` already crossed the atomic current
                # symlink commit point. Audit storage degradation must be
                # surfaced without lying that the rollback itself failed.
                audit_status = "failed"
            return ManagedCliServiceResult(
                payload={
                    "ok": True,
                    "managed_by": "managed_cli",
                    "action": "toolchain_rollback",
                    "adapter_id": adapter_id,
                    "previous_revision": plan.expected_current_revision,
                    "active_revision": ref.host_path.name,
                    "version": plan.target_version,
                    "audit_status": audit_status,
                },
                exit_code=0,
            )
        except (OSError, ValueError) as exc:
            try:
                self.toolchains.record_event(
                    adapter_id,
                    {
                        "event": "rollback_failed",
                        "plan_id": plan_id,
                        "error_type": type(exc).__name__,
                        "completed_at": time.time(),
                    },
                )
            except (OSError, ValueError):
                pass
            return self._error(
                "managed_toolchain_rollback_failed",
                "Toolchain rollback approval is invalid, stale, or no longer compatible.",
            )
        finally:
            if consumed_plan is not None and consumed_plan.toolchain_lease_id:
                try:
                    self.toolchains.release_revision_lease(
                        adapter_id=adapter_id,
                        lease_id=consumed_plan.toolchain_lease_id,
                    )
                    self.toolchains.gc_revisions(adapter_id)
                except (OSError, ValueError):
                    pass

    def execute(
        self,
        plan: ManagedCliExecutionPlan | ManagedCliMatch | GenericNodeCliInstallPlan,
        context: dict[str, Any] | None = None,
    ) -> ManagedCliServiceResult:
        if isinstance(plan, ManagedCliMatch):
            plan = self.plan(plan, context or {})
        if isinstance(plan, GenericNodeCliInstallPlan):
            return self._install_generic_node_cli(plan)
        promoted = False
        try:
            if plan.match.route == ManagedCliRoute.INSTALLER:
                result = self._install(plan)
            else:
                result = self._provider(plan, context or {})
            if plan.toolchain_lease_id and result.payload.get("status") == "awaiting_user_browser":
                runner_id = str(
                    result.payload.get("browser_job_id")
                    or _browser_job_id(
                        plan.owner_user_id,
                        plan.match.provider or "",
                        plan.profile_id or "",
                    )
                )
                self.toolchains.renew_revision_lease(
                    adapter_id=plan.match.adapter_id,
                    lease_id=plan.toolchain_lease_id,
                    revision=plan.toolchain_revision,
                    owner_kind="runner",
                    owner_id=runner_id,
                    contract_fingerprint=plan.adapter_contract_fingerprint,
                    ttl_seconds=3600,
                )
                promoted = True
            return result
        finally:
            if plan.toolchain_lease_id and not promoted:
                try:
                    self.toolchains.release_revision_lease(
                        adapter_id=plan.match.adapter_id,
                        lease_id=plan.toolchain_lease_id,
                    )
                    self.toolchains.gc_revisions(plan.match.adapter_id)
                except (OSError, ValueError):
                    # Lease corruption fails GC closed but never changes the
                    # already-known command result.
                    pass

    def _install_generic_node_cli(
        self,
        plan: GenericNodeCliInstallPlan,
    ) -> ManagedCliServiceResult:
        expected_fingerprint = self._generic_cli_resolution_fingerprint(
            package=plan.match.package,
            distribution=plan.resolved_distribution,
            version=plan.resolved_version,
            integrity=plan.resolved_integrity,
            executables=plan.executables,
            runtime_image_digest=plan.runtime_image_digest,
            toolchain_revision=plan.toolchain_revision,
        )
        if (
            plan.resolution_fingerprint != expected_fingerprint
            or plan.resolved_distribution != f"{plan.match.package}@{plan.resolved_version}"
        ):
            return self._error(
                "managed_install_plan_stale",
                "Generic CLI package resolution changed while installation approval was pending.",
            )
        try:
            installer = getattr(self.backend, "install_generic_node_cli", None)
            if not callable(installer):
                raise ValueError("host CLI runtime cannot publish package bytes")
            installed = installer(
                package=plan.match.package,
                distribution=plan.resolved_distribution,
                executables=plan.executables,
                integrity=plan.resolved_integrity,
                owner_revision=plan.resolution_fingerprint,
                runtime_digest=plan.runtime_image_digest,
                base_revision=plan.toolchain_revision,
            )
        except (OSError, ValueError) as exc:
            return self._error(
                "managed_install_failed",
                f"Generic CLI staging failed before publication: {type(exc).__name__}: {exc}",
            )
        return ManagedCliServiceResult(
            payload={
                "ok": installed.exit_code == 0,
                "managed_by": "software_runtime",
                "kind": "generic_node_cli",
                "route": plan.match.route.value,
                "action": plan.match.action.value,
                "package": plan.match.package,
                "requested_distribution": plan.match.distribution,
                "resolved_distribution": plan.resolved_distribution,
                "resolved_version": plan.resolved_version,
                "resolved_integrity": plan.resolved_integrity,
                "executables": list(plan.executables),
                "previous_revision": installed.previous_revision,
                "active_revision": installed.revision or installed.previous_revision,
                "runtime_image_digest": plan.runtime_image_digest,
                "reproducible_request": True,
                "credentials": "none",
                "output": redact_managed_cli_output(installed.output),
            },
            exit_code=installed.exit_code,
        )

    def _install(self, plan: ManagedCliExecutionPlan) -> ManagedCliServiceResult:
        match = plan.match
        if not match.distribution:
            return self._error("unsupported_managed_install", "No trusted installer owns this request.")
        try:
            adapter = self.registry.adapter(match.adapter_id)
        except ValueError:
            return self._error("unsupported_managed_install", "No trusted installer owns this request.")
        adapter_contract_fingerprint = self._adapter_contract_fingerprint(match.adapter_id)
        if (
            plan.adapter_contract_fingerprint != adapter_contract_fingerprint
            or plan.package_contract_fingerprint != adapter.toolchain_package.fingerprint
        ):
            return self._error(
                "managed_install_plan_stale",
                "Managed CLI Adapter contract changed while installation approval was pending.",
            )
        expected_resolution_fingerprint = self._resolution_fingerprint(
            adapter_id=match.adapter_id,
            package_fingerprint=adapter.toolchain_package.fingerprint,
            distribution=plan.resolved_distribution,
            version=plan.resolved_version,
            integrity=plan.resolved_integrity,
            runtime_image_digest=plan.runtime_image_digest,
        )
        if (
            not plan.resolution_fingerprint
            or plan.resolution_fingerprint != expected_resolution_fingerprint
            or plan.resolved_distribution != f"{adapter.toolchain_package.package}@{plan.resolved_version}"
        ):
            return self._error(
                "managed_install_plan_stale",
                "Managed package resolution is missing or changed; prepare a new installation approval.",
            )
        if match.adapter_id == "lark-cli":
            installer = getattr(self.backend, "install_host_lark_cli", None)
            if not callable(installer):
                return self._error(
                    "managed_install_failed",
                    "The host runtime cannot install the official lark-cli.",
                )
            try:
                installed = installer(
                    plan.resolved_distribution,
                    expected_version=plan.resolved_version,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return self._error(
                    "managed_install_failed",
                    f"Global lark-cli installation failed: {type(exc).__name__}: {exc}",
                )
            return ManagedCliServiceResult(
                payload={
                    "ok": installed.exit_code == 0,
                    "managed_by": "managed_cli",
                    "adapter_id": match.adapter_id,
                    "route": match.route.value,
                    "action": match.action.value,
                    "distribution": match.distribution,
                    "resolved_distribution": plan.resolved_distribution,
                    "resolved_version": plan.resolved_version,
                    "installation_scope": "host_user_global",
                    "reproducible_request": True,
                    "output": redact_managed_cli_output(installed.output),
                },
                exit_code=installed.exit_code,
            )
        try:
            result = self.toolchains.install_package(
                self.backend,
                adapter_id=adapter.adapter_id,
                spec=adapter.toolchain_package,
                distribution=plan.resolved_distribution,
                expected_integrity=plan.resolved_integrity,
                runtime_image_digest=plan.runtime_image_digest,
                adapter_contract_fingerprint=adapter_contract_fingerprint,
                credential_state_fingerprint=adapter.credential_state.fingerprint,
                expected_revision=plan.toolchain_revision,
            )
        except (OSError, ValueError) as exc:
            diagnostic = redact_managed_cli_output(str(exc)).strip()
            return self._error(
                "managed_install_failed",
                (
                    f"Managed Toolchain staging failed before publication: {type(exc).__name__}: "
                    f"{diagnostic or 'no diagnostic'}"
                ),
            )
        try:
            self.toolchains.record_event(
                match.adapter_id,
                {
                    "event": ("installation_succeeded" if result.exit_code == 0 else "installation_failed"),
                    "requested_distribution": match.distribution,
                    "resolved_distribution": plan.resolved_distribution,
                    "resolved_integrity": plan.resolved_integrity,
                    "runtime_image_digest": plan.runtime_image_digest,
                    "from_revision": result.previous_revision,
                    "active_revision": result.active_revision,
                    "completed_at": time.time(),
                },
            )
        except OSError:
            pass
        return ManagedCliServiceResult(
            payload={
                "ok": result.exit_code == 0,
                "managed_by": "managed_cli",
                "adapter_id": match.adapter_id,
                "route": match.route.value,
                "action": match.action.value,
                "distribution": match.distribution,
                "resolved_distribution": plan.resolved_distribution,
                "resolved_version": result.resolved_version,
                "previous_revision": result.previous_revision,
                "active_revision": result.active_revision,
                "reproducible_request": True,
                "toolchain": f"user://runtime/node/{self.toolchains.runtime_contract}",
                "output": redact_managed_cli_output(result.output),
            },
            exit_code=result.exit_code,
        )

    def _authorization_provider(self, plan: ManagedCliExecutionPlan) -> ManagedCliServiceResult:
        """Execute the Driver-selected authorization state machine.

        Public browser material is projected as structured data. Provider
        continuation secrets and staged credentials never enter the returned
        ToolMessage; the existing Profile Vault is replaced only after phase 2
        passes an independent identity verification.
        """

        match = plan.match
        driver = self._authorization_driver(match, required=True)
        assert driver is not None
        if match.action == ManagedCliAction.AUTHORIZATION_RESUME:
            return self._resume_user_consent(plan)
        try:
            node = driver.graph.node(str(match.authorization_phase or ""))
        except ValueError:
            return self._error("unsupported_authorization_phase", "The provider authorization phase is not supported.")
        if node.kind == AuthorizationPhaseKind.BROWSER_CONFIGURATION:
            return self._start_app_configuration(plan, node=node)
        if node.kind == AuthorizationPhaseKind.DEVICE_AUTHORIZATION:
            return self._start_user_consent(plan, node=node)
        return self._error("unsupported_authorization_phase", "The Driver phase kind is not supported.")

    def _authorization_payload(
        self,
        plan: ManagedCliExecutionPlan,
        flow: dict[str, Any],
        *,
        output: str,
    ) -> ManagedCliServiceResult:
        request = AuthorizationFlowStore.projection(flow)
        return ManagedCliServiceResult(
            payload={
                "ok": True,
                "managed_by": "managed_cli",
                "adapter_id": plan.match.adapter_id,
                "route": plan.match.route.value,
                "action": plan.match.action.value,
                "profile_id": plan.profile_id,
                "status": "awaiting_user_browser",
                "authorization_completed": False,
                "authorization_request": request,
                "output": output,
                "next_action": str(request.get("completion_hint") or "完成后告诉我，我会继续验证。"),
            },
            exit_code=0,
        )

    @staticmethod
    def _profile_status_during_authorization_payload(
        plan: ManagedCliExecutionPlan,
        current: dict[str, Any],
        flow: dict[str, Any],
    ) -> ManagedCliServiceResult:
        """Return the durable Profile read model without touching Flow staging.

        Provider CLIs may migrate local state or rotate tokens even for commands
        named ``status`` or ``show``. While an authorization write transaction
        owns the Profile, expose the last independently verified assessment and
        a separate non-secret Flow summary instead of running unsafe inspection
        or replacing the user's read request with an authorization card.
        """

        identities = current.get("identities") if isinstance(current.get("identities"), dict) else {}
        safe_identities: dict[str, dict[str, Any]] = {}
        for identity_name, raw in identities.items():
            if not isinstance(identity_name, str) or not isinstance(raw, dict):
                continue
            safe_identities[identity_name] = {
                key: raw.get(key)
                for key in ("status", "reason", "verified", "token_status", "updated_at")
                if raw.get(key) is not None
            }
        phase = flow.get("phase") if isinstance(flow.get("phase"), dict) else {}
        return ManagedCliServiceResult(
            payload={
                "ok": True,
                "managed_by": "managed_cli",
                "adapter_id": plan.match.adapter_id,
                "route": plan.match.route.value,
                "action": plan.match.action.value,
                "profile_id": plan.profile_id,
                "status": "completed",
                "profile_status": {
                    "health": current.get("status"),
                    "identities": safe_identities,
                    "freshness": "cached",
                    "reason": "authorization_write_in_progress",
                },
                "authorization_flow": {
                    "flow_id": flow.get("flow_id"),
                    "purpose": flow.get("purpose"),
                    "status": flow.get("status"),
                    "phase": {
                        key: phase.get(key)
                        for key in ("id", "step", "total", "title", "description")
                        if phase.get(key) is not None
                    },
                    "completed_phase_ids": list(flow.get("completed_phase_ids", [])),
                    "expires_at": flow.get("expires_at"),
                },
                "output": (
                    "当前存在未完成的授权写事务；已返回 durable Profile 最近一次独立验证状态，"
                    "未执行可能改写凭证的 Provider CLI。"
                ),
            },
            exit_code=0,
        )

    def _authorization_failure_payload(
        self,
        plan: ManagedCliExecutionPlan,
        flow: dict[str, Any],
        failure: _LarkAuthorizationFailure,
        *,
        request_status: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> ManagedCliServiceResult:
        request = AuthorizationFlowStore.projection(flow)
        if request_status is not None:
            request["status"] = request_status
        if failure.flow_status == AuthorizationFlowStatus.EXPIRED.value:
            message = "飞书用户授权链接已过期，请重新发起用户授权。"
        elif failure.flow_status == AuthorizationFlowStatus.CANCELLED.value:
            message = "飞书用户授权已被拒绝或取消。"
        elif failure.reason == "authorization_verification_failed":
            message = "飞书 token 兑换后的独立身份验证未通过；应用配置已保留，可重新发起第 2 步。"
        elif failure.retryable:
            message = "飞书授权校验暂时失败；应用配置和当前授权进度均已保留。"
        else:
            message = "本次飞书用户授权没有完成；应用配置已保留，可重新发起第 2 步。"
        payload: dict[str, Any] = {
            "ok": False,
            "managed_by": "managed_cli",
            "adapter_id": plan.match.adapter_id,
            "route": plan.match.route.value,
            "action": plan.match.action.value,
            "profile_id": plan.profile_id,
            "status": failure.flow_status,
            "authorization_completed": False,
            "authorization_request": request,
            "reason": failure.reason,
            "retryable": failure.retryable,
            "message": message,
            "next_action": "重新执行 lark-cli auth login --domain all --no-wait --json 生成新链接。",
        }
        if diagnostic:
            payload["diagnostic"] = dict(diagnostic)
        provider_code = str(failure.provider_code or "")
        if provider_code.isdigit() and len(provider_code) <= 10:
            payload["provider_code"] = int(provider_code)
        return ManagedCliServiceResult(payload=payload, exit_code=1)

    def _authorization_flow_store(
        self,
        store: CredentialProfileStore,
        owner_user_id: str,
    ) -> AuthorizationFlowStore:
        del store
        # AuthorizationFlowStore creates its master key lazily only when a
        # continuation secret is actually written/read. Ordinary provider
        # commands and connector status checks must not create secret state.
        return AuthorizationFlowStore(self.paths, owner_user_id)

    def _reconcile_authorization_flow_locked(
        self,
        *,
        flow_store: AuthorizationFlowStore,
        current: dict[str, Any],
        provider: str,
        profile_id: str,
    ) -> dict[str, Any] | None:
        """Reconcile one Flow using its Adapter-declared evidence contract."""

        active = flow_store.active(provider, profile_id)
        if active is not None:
            adapter = self.registry.for_provider(provider)
            expected_fingerprint = self._adapter_contract_fingerprint(adapter.adapter_id)
            stored_fingerprint = str(active.get("adapter_contract_fingerprint") or "")
            if stored_fingerprint != expected_fingerprint:
                # Legacy flows froze only the credential-state fingerprint.
                # Driver/argv semantics can no longer be proven, so never
                # resume them under the current authorization implementation.
                flow_store.cancel_active(
                    provider,
                    profile_id,
                    reason="authorization_contract_changed",
                )
                return None

        return flow_store.reconcile_recovery(
            provider,
            profile_id,
            runner_lease_present=bool(current.get("browser_job_id")),
        )

    @staticmethod
    def _ensure_plan_is_current(
        store: CredentialProfileStore,
        plan: ManagedCliExecutionPlan,
        provider: str,
        profile_id: str,
    ) -> dict[str, Any]:
        current = store.resolve(provider, explicit_profile_id=profile_id, create_default=False)
        if current is None or float(current.get("updated_at") or 0) != plan.profile_revision:
            raise ValueError("Credential Profile changed while the managed command was being prepared; retry it.")
        return current

    def _start_app_configuration(
        self,
        plan: ManagedCliExecutionPlan,
        *,
        node: AuthorizationPhaseNode | None = None,
    ) -> ManagedCliServiceResult:
        match = plan.match
        driver = self._authorization_driver(match, required=True)
        assert driver is not None
        node = node or driver.graph.node(driver.graph.app_configuration.phase_id)
        if node.kind != AuthorizationPhaseKind.BROWSER_CONFIGURATION:
            return self._error("unsupported_authorization_phase", "Authorization phase is not browser-driven.")
        app_phase = node.phase
        owner_user_id = plan.owner_user_id
        provider = match.provider or driver.provider
        profile_id = plan.profile_id or ""
        credential_state = match.credential_state
        assert credential_state is not None
        store = CredentialProfileStore(self.paths, owner_user_id)
        start_watcher = False
        try:
            with store.profile_lock(provider, profile_id):
                current = self._ensure_plan_is_current(store, plan, provider, profile_id)
                flow_store = self._authorization_flow_store(store, owner_user_id)
                active = flow_store.active(provider, profile_id)
                base_revision = store.state_revision(provider, profile_id)
                active_base_revision = str(active.get("base_state_revision") or "") if active else ""
                restart_active = active is not None and (
                    active.get("purpose") != driver.graph.full_purpose
                    or active_base_revision not in {"", base_revision}
                    # An explicit full replacement while phase 2 is active
                    # starts over from an empty staging state. Reusing the
                    # phase-2 Flow would mix two replacement transactions.
                    or active.get("phase_id") != app_phase.phase_id
                )
                if restart_active:
                    if current.get("browser_job_id"):
                        old_browser_job_id = str(current.get("browser_job_id") or "")
                        finalized = self.backend.finalize_managed_browser_auth_cli(
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=old_browser_job_id,
                        )
                        if not finalized:
                            return self._error(
                                "browser_auth_cleanup_failed",
                                "无法终止旧的飞书浏览器授权 Runner，请重试完整重新配置。",
                            )
                        if not store.finish_browser_job(
                            profile_id,
                            old_browser_job_id,
                            "superseded",
                            credential_state.fingerprint,
                        ):
                            return self._error(
                                "authorization_profile_conflict",
                                "旧的飞书浏览器授权 lease 已变化，请刷新状态后重试。",
                            )
                        current = (
                            store.resolve(
                                provider,
                                explicit_profile_id=profile_id,
                                create_default=False,
                            )
                            or current
                        )
                    cancel_reason = (
                        "stale_base_state"
                        if active_base_revision not in {"", base_revision}
                        else "superseded_by_full_replacement"
                    )
                    flow_store.cancel_active(
                        provider,
                        profile_id,
                        reason=cancel_reason,
                    )
                    active = None

                active = self._reconcile_authorization_flow_locked(
                    flow_store=flow_store,
                    current=current,
                    provider=provider,
                    profile_id=profile_id,
                )

                if current.get("browser_job_id"):
                    result = self._collect_browser_job_locked(
                        store=store,
                        owner_user_id=owner_user_id,
                        provider=provider,
                        profile_id=profile_id,
                        browser_job_id=str(current.get("browser_job_id") or ""),
                        credential_state=credential_state,
                    )
                    active = flow_store.active(provider, profile_id)
                    if result.browser_status == "awaiting_user_browser" and active is not None:
                        return self._authorization_payload(
                            plan,
                            active,
                            output="正在等待第 1/2 步的浏览器操作完成。",
                        )

                if active is not None and active.get("phase_id") == app_phase.phase_id:
                    if app_phase.phase_id in active.get("completed_phase_ids", []):
                        return ManagedCliServiceResult(
                            payload={
                                "ok": True,
                                "managed_by": "managed_cli",
                                "adapter_id": match.adapter_id,
                                "profile_id": profile_id,
                                "status": "authorization_phase_completed",
                                "phase": app_phase.projection(),
                                "output": "飞书应用配置已验证，可以进入第 2/2 步用户授权。",
                            },
                            exit_code=0,
                        )
                    if active.get("status") == AuthorizationFlowStatus.AWAITING_USER.value:
                        return self._authorization_payload(
                            plan,
                            active,
                            output="正在等待第 1/2 步的浏览器操作完成。",
                        )

                base_revision = store.state_revision(provider, profile_id)
                flow = flow_store.begin_or_advance(
                    provider=provider,
                    adapter_id=match.adapter_id,
                    profile_id=profile_id,
                    purpose=driver.graph.full_purpose,
                    phase=app_phase,
                    profile_revision=float(current.get("updated_at") or 0),
                    base_state_revision=base_revision,
                    adapter_contract_fingerprint=plan.adapter_contract_fingerprint,
                    public={},
                    secret=None,
                    expires_at=None,
                )
                browser_job_id = _browser_job_id(owner_user_id, provider, profile_id)
                store.begin_browser_job(profile_id, browser_job_id, credential_state.fingerprint)
                result = self.backend.run_managed_browser_auth_cli(
                    argv=list(match.argv),
                    environment=dict(match.env),
                    credential_state_spec=credential_state,
                    toolchain_path=plan.toolchain_path,
                    container_path="/opt/puddingclaw/toolchain/node",
                    # Reconfiguration is isolated from the old Vault. The
                    # browser runner creates a complete replacement in staging.
                    credential_state=b"",
                    owner_user_id=owner_user_id,
                    provider=provider,
                    profile_id=profile_id,
                    adapter_id=match.adapter_id,
                    authorization_contract_fingerprint=plan.adapter_contract_fingerprint,
                    expected_runtime_image_digest=plan.runtime_image_digest,
                )
                if result.browser_job_id != browser_job_id:
                    raise RuntimeError("browser authorization runner returned the wrong job id")
                if result.browser_status == "completed" and result.credential_state is not None:
                    self._persist_browser_result_locked(
                        store=store,
                        owner_user_id=owner_user_id,
                        provider=provider,
                        profile_id=profile_id,
                        browser_job_id=browser_job_id,
                        result=result,
                        credential_state=credential_state,
                    )
                    return ManagedCliServiceResult(
                        payload={
                            "ok": True,
                            "managed_by": "managed_cli",
                            "adapter_id": match.adapter_id,
                            "profile_id": profile_id,
                            "status": "authorization_phase_completed",
                            "phase": app_phase.projection(),
                            "output": "飞书应用配置已验证，可以进入第 2/2 步用户授权。",
                        },
                        exit_code=0,
                    )
                if result.browser_status != "awaiting_user_browser":
                    flow_store.fail(
                        provider, profile_id, status=AuthorizationFlowStatus.FAILED.value, error="app setup failed"
                    )
                    store.finish_browser_job(
                        profile_id, browser_job_id, "repair_required", credential_state.fingerprint
                    )
                    return self._error(
                        "lark_app_configuration_failed",
                        "飞书应用配置未完成；旧 Credential Profile 保持不变，可重新发起配置。",
                    )
                verification_url = driver.config_authorization_url(result.output)
                if verification_url is None:
                    raise RuntimeError("Lark app configuration did not return a trusted verification URL")
                qr_ascii = _terminal_qr(result.output)
                if not qr_ascii:
                    qr_argv = driver.qrcode_argv(verification_url)
                    if qr_argv is not None:
                        qr = self.backend.run_managed_provider_cli(
                            argv=list(qr_argv),
                            environment=dict(match.env),
                            credential_state_spec=None,
                            toolchain_path=plan.toolchain_path,
                            container_path="/opt/puddingclaw/toolchain/node",
                            credential_state=b"",
                            network_enabled=False,
                            workspace_writable=False,
                            expected_runtime_image_digest=plan.runtime_image_digest,
                            owner_user_id=owner_user_id,
                            profile_id=profile_id,
                        )
                        qr_ascii = _terminal_qr(qr.output)
                flow = flow_store.begin_or_advance(
                    provider=provider,
                    adapter_id=match.adapter_id,
                    profile_id=profile_id,
                    purpose=driver.graph.full_purpose,
                    phase=app_phase,
                    profile_revision=float(current.get("updated_at") or 0),
                    base_state_revision=base_revision,
                    adapter_contract_fingerprint=plan.adapter_contract_fingerprint,
                    public={
                        "verification_url": verification_url,
                        "user_code": _query_user_code(verification_url),
                        "qr_ascii": qr_ascii,
                    },
                    secret=None,
                    expires_at=None,
                )
                start_watcher = True
            if start_watcher:
                self._start_browser_watcher(
                    owner_user_id=owner_user_id,
                    provider=provider,
                    profile_id=profile_id,
                    browser_job_id=_browser_job_id(owner_user_id, provider, profile_id),
                    credential_state=credential_state,
                )
            return self._authorization_payload(
                plan,
                flow,
                output="第 1/2 步已开始：请创建或绑定飞书 CLI 应用。",
            )
        except Exception as exc:  # noqa: BLE001
            return self._error(
                "managed_authorization_failed",
                f"{type(exc).__name__}: 托管授权事务未能继续；Credential Profile 未被提交。",
            )

    def _issue_user_attempt_locked(
        self,
        *,
        plan: ManagedCliExecutionPlan,
        store: CredentialProfileStore,
        flow_store: AuthorizationFlowStore,
        current: dict[str, Any],
        active: dict[str, Any],
        credential_state: CredentialStateSpec,
        reset_staged_user: bool,
        renewal: bool,
        node: AuthorizationPhaseNode | None = None,
    ) -> ManagedCliServiceResult:
        """Create one device-code attempt without mutating the durable Profile."""

        match = plan.match
        driver = self._authorization_driver(match, required=True)
        assert driver is not None
        node = node or driver.graph.node(str(active.get("phase_id") or match.authorization_phase or ""))
        if node.kind != AuthorizationPhaseKind.DEVICE_AUTHORIZATION:
            return self._error("unsupported_authorization_phase", "Authorization phase is not device-driven.")
        provider = match.provider or driver.provider
        profile_id = plan.profile_id or ""
        purpose = str(active.get("purpose") or driver.graph.full_purpose)
        staged_state = flow_store.read_staged_state(active)
        if node.requires_prerequisite_identity and purpose != driver.graph.reauthorization_purpose:
            preflight = self.backend.run_managed_provider_cli(
                argv=list(driver.identity_status_argv),
                environment=dict(match.env),
                credential_state_spec=credential_state,
                toolchain_path=plan.toolchain_path,
                container_path="/opt/puddingclaw/toolchain/node",
                credential_state=staged_state,
                network_enabled=True,
                workspace_writable=False,
                expected_runtime_image_digest=plan.runtime_image_digest,
                owner_user_id=plan.owner_user_id,
                profile_id=profile_id,
            )
            if preflight.credential_state is not None:
                flow_store.write_staged_state(active, preflight.credential_state)
                staged_state = preflight.credential_state
            if not driver.bot_ready(driver.identity_status(preflight.output)):
                return self._error(
                    "authorization_prerequisite_failed",
                    "飞书应用配置未通过 Bot 身份验证；请完整重新配置，而不是继续重试用户授权。",
                )
        if reset_staged_user:
            # This logout runs only against the encrypted staging copy. The
            # durable Profile bytes are untouched until independent verify and
            # CAS commit. Provider adapters must keep this operation local-only.
            reset = self.backend.run_managed_provider_cli(
                argv=list(driver.logout_argv),
                environment=dict(match.env),
                credential_state_spec=credential_state,
                toolchain_path=plan.toolchain_path,
                container_path="/opt/puddingclaw/toolchain/node",
                credential_state=staged_state,
                network_enabled=False,
                workspace_writable=False,
                expected_runtime_image_digest=plan.runtime_image_digest,
                owner_user_id=plan.owner_user_id,
                profile_id=profile_id,
            )
            if reset.exit_code != 0 or reset.credential_state is None:
                return self._error(
                    "authorization_staging_reset_failed",
                    "无法在隔离副本中清除旧用户登录态；旧连接保持不变，本次重新授权已停止。",
                )
            flow_store.write_staged_state(active, reset.credential_state)
            staged_state = reset.credential_state
            active = flow_store.mark_staging_user_cleared(provider, profile_id) or active
        initiation_argv = (
            list(match.argv) if match.action == ManagedCliAction.BROWSER_AUTH else list(driver.user_login_argv)
        )
        result = self.backend.run_managed_provider_cli(
            argv=initiation_argv,
            environment=dict(match.env),
            credential_state_spec=credential_state,
            toolchain_path=plan.toolchain_path,
            container_path="/opt/puddingclaw/toolchain/node",
            credential_state=staged_state,
            network_enabled=True,
            workspace_writable=False,
            expected_runtime_image_digest=plan.runtime_image_digest,
            owner_user_id=plan.owner_user_id,
            profile_id=profile_id,
        )
        if result.exit_code != 0:
            return self._error(
                "lark_user_authorization_failed",
                "飞书用户授权无法启动；应用配置和旧 Credential Profile 保持不变。",
            )
        device = driver.device_authorization(result.output)
        if device is None:
            return self._error(
                "lark_user_authorization_invalid_response",
                "飞书用户授权启动结果缺少完整的设备授权数据；应用配置和旧 Credential Profile 保持不变。",
            )
        verification_url = driver.validated_authorization_url(
            str(device["verification_url"]), phase_id=node.phase.phase_id
        )
        if verification_url is None:
            return self._error(
                "lark_user_authorization_untrusted_url",
                "飞书返回的用户授权地址未通过官方域名、路径和公开参数校验；应用配置和旧 Credential Profile 保持不变。",
            )
        if result.credential_state is not None:
            flow_store.write_staged_state(active, result.credential_state)
        qr_argv = driver.qrcode_argv(verification_url)
        qr = None
        if qr_argv is not None:
            qr = self.backend.run_managed_provider_cli(
                argv=list(qr_argv),
                environment=dict(match.env),
                credential_state_spec=None,
                toolchain_path=plan.toolchain_path,
                container_path="/opt/puddingclaw/toolchain/node",
                credential_state=b"",
                network_enabled=False,
                workspace_writable=False,
                expected_runtime_image_digest=plan.runtime_image_digest,
                owner_user_id=plan.owner_user_id,
                profile_id=profile_id,
            )
        expires_in = _positive_float(device.get("expires_in"))
        phase = driver.graph.phase(node.phase.phase_id, purpose_id=purpose)
        flow = flow_store.begin_or_advance(
            provider=provider,
            adapter_id=match.adapter_id,
            profile_id=profile_id,
            purpose=purpose,
            phase=phase,
            profile_revision=float(current.get("updated_at") or 0),
            base_state_revision=str(active.get("base_state_revision") or store.state_revision(provider, profile_id)),
            adapter_contract_fingerprint=plan.adapter_contract_fingerprint,
            public={
                "verification_url": verification_url,
                "user_code": device.get("user_code"),
                "qr_ascii": _terminal_qr(qr.output) if qr is not None else "",
            },
            secret={"device_code": str(device["device_code"])},
            expires_at=(time.time() + expires_in if expires_in is not None else None),
            new_attempt=renewal,
        )
        if renewal:
            last_attempt = active.get("last_user_attempt")
            last_reason = str(last_attempt.get("reason") or "") if isinstance(last_attempt, dict) else ""
            output = (
                "上一飞书授权链接已过期，已生成新的二维码；旧连接保持不变。"
                if last_reason in {"", "authorization_expired"}
                else "上一飞书用户授权尝试未完成，已生成新的二维码；应用配置和旧连接保持不变。"
            )
        elif purpose == driver.graph.reauthorization_purpose:
            output = "第 1/1 步已开始：请重新授权访问你的飞书数据。"
        else:
            output = "第 2/2 步已开始：请授权 CLI 应用访问你的飞书数据。"
        return self._authorization_payload(plan, flow, output=output)

    def _start_user_consent(
        self,
        plan: ManagedCliExecutionPlan,
        *,
        node: AuthorizationPhaseNode | None = None,
    ) -> ManagedCliServiceResult:
        match = plan.match
        driver = self._authorization_driver(match, required=True)
        assert driver is not None
        node = node or driver.graph.node(str(match.authorization_phase or ""))
        if node.kind != AuthorizationPhaseKind.DEVICE_AUTHORIZATION:
            return self._error("unsupported_authorization_phase", "Authorization phase is not device-driven.")
        browser_node = next(
            (
                candidate
                for candidate in driver.graph.phases
                if candidate.kind == AuthorizationPhaseKind.BROWSER_CONFIGURATION
                and candidate.phase.phase_id in node.prerequisites
            ),
            None,
        )
        owner_user_id = plan.owner_user_id
        provider = match.provider or driver.provider
        profile_id = plan.profile_id or ""
        credential_state = match.credential_state
        assert credential_state is not None
        store = CredentialProfileStore(self.paths, owner_user_id)
        try:
            with store.profile_lock(provider, profile_id):
                current = self._ensure_plan_is_current(store, plan, provider, profile_id)
                flow_store = self._authorization_flow_store(store, owner_user_id)
                active = self._reconcile_authorization_flow_locked(
                    flow_store=flow_store,
                    current=current,
                    provider=provider,
                    profile_id=profile_id,
                )
                durable_state = store.read_state(
                    provider,
                    profile_id,
                    credential_state=credential_state,
                )
                identities = current.get("identities") if isinstance(current.get("identities"), dict) else {}
                bot_assessment = identities.get("bot") if isinstance(identities, dict) else None
                bot_status = str(bot_assessment.get("status") or "").lower() if isinstance(bot_assessment, dict) else ""
                durable_bot_ready = bool(durable_state) and (
                    bot_status in {"ready", "active"} or current.get("status") == "active"
                )

                # `auth login` explicitly asks for User authorization. If an
                # earlier Agent mistakenly started a full replacement but its
                # App-configuration browser step is still incomplete, prefer
                # the last-known-good durable Bot/App and replace that orphaned
                # flow with a 1/1 User reauthorization. A legitimately
                # completed phase 1 is deliberately excluded: in that case the
                # same command must continue the intended full setup to phase 2.
                supersede_incomplete_full_setup = bool(
                    browser_node is not None
                    and durable_bot_ready
                    and active is not None
                    and active.get("purpose") == driver.graph.full_purpose
                    and active.get("phase_id") == browser_node.phase.phase_id
                    and browser_node.phase.phase_id not in active.get("completed_phase_ids", [])
                )
                if supersede_incomplete_full_setup:
                    pending_job_id = str(current.get("browser_job_id") or "")
                    if pending_job_id:
                        finalized = self.backend.finalize_managed_browser_auth_cli(
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=pending_job_id,
                        )
                        if not finalized:
                            return self._error(
                                "browser_auth_cleanup_failed",
                                "无法终止误发起的飞书应用配置 Runner，请重试用户授权。",
                            )
                        if not store.finish_browser_job(
                            profile_id,
                            pending_job_id,
                            "superseded",
                            credential_state.fingerprint,
                        ):
                            return self._error(
                                "authorization_profile_conflict",
                                "飞书授权状态在切换为用户续权时发生变化，请刷新后重试。",
                            )
                        current = (
                            store.resolve(
                                provider,
                                explicit_profile_id=profile_id,
                                create_default=False,
                            )
                            or current
                        )
                    flow_store.cancel_active(
                        provider,
                        profile_id,
                        reason="superseded_by_user_reauthorization",
                    )
                    active = None
                if current.get("browser_job_id"):
                    result = self._collect_browser_job_locked(
                        store=store,
                        owner_user_id=owner_user_id,
                        provider=provider,
                        profile_id=profile_id,
                        browser_job_id=str(current.get("browser_job_id") or ""),
                        credential_state=credential_state,
                    )
                    active = flow_store.active(provider, profile_id)
                    if result.browser_status == "awaiting_user_browser" and active is not None:
                        return self._authorization_payload(plan, active, output="第 1/2 步尚未完成，请先完成应用配置。")
                created_entry = False
                if active is None:
                    if not node.prerequisites:
                        purpose = driver.graph.purpose_for_entry(node.phase.phase_id)
                        phase = driver.graph.phase(node.phase.phase_id, purpose_id=purpose.purpose_id)
                        active = flow_store.begin_or_advance(
                            provider=provider,
                            adapter_id=match.adapter_id,
                            profile_id=profile_id,
                            purpose=purpose.purpose_id,
                            phase=phase,
                            profile_revision=float(current.get("updated_at") or 0),
                            base_state_revision=store.state_revision(provider, profile_id),
                            adapter_contract_fingerprint=plan.adapter_contract_fingerprint,
                            public={},
                            secret=None,
                            expires_at=None,
                        )
                        flow_store.write_staged_state(active, durable_state)
                        created_entry = True
                    # Existing Profiles may already contain a verified Bot/App
                    # configuration (for example after an earlier login). Seed
                    # the transaction from that last-known-good state so a
                    # user-only reauthorization does not repeat step 1.
                    elif durable_state and browser_node is not None:
                        if bot_status in {"ready", "active"} or current.get("status") == "active":
                            if str(current.get("status") or "").startswith("awaiting_"):
                                store.update_status(profile_id, "active")
                                current = (
                                    store.resolve(
                                        provider,
                                        explicit_profile_id=profile_id,
                                        create_default=False,
                                    )
                                    or current
                                )
                            active = flow_store.begin_or_advance(
                                provider=provider,
                                adapter_id=match.adapter_id,
                                profile_id=profile_id,
                                purpose=driver.graph.reauthorization_purpose,
                                phase=browser_node.phase,
                                profile_revision=float(current.get("updated_at") or 0),
                                base_state_revision=store.state_revision(provider, profile_id),
                                adapter_contract_fingerprint=plan.adapter_contract_fingerprint,
                                public={},
                                secret=None,
                                expires_at=None,
                            )
                            flow_store.write_staged_state(
                                active,
                                durable_state,
                            )
                            flow_store.mark_phase_verified(
                                provider,
                                profile_id,
                                browser_node.phase.phase_id,
                            )
                            active = flow_store.active(provider, profile_id)
                        else:
                            store.update_status(profile_id, "repair_required")
                completed_ids = set(active.get("completed_phase_ids", [])) if active is not None else set()
                if active is None or not set(node.prerequisites).issubset(completed_ids):
                    return self._error(
                        "authorization_prerequisite_failed",
                        "第 1/2 步应用配置尚未通过 Backend 验证，不能提前发起用户授权。",
                    )
                if (
                    not created_entry
                    and
                    active.get("phase_id") == node.phase.phase_id
                    and active.get("status") == AuthorizationFlowStatus.AWAITING_USER.value
                ):
                    expires_at = _positive_float(active.get("expires_at"))
                    if expires_at is None or expires_at > time.time():
                        return self._authorization_payload(
                            plan,
                            active,
                            output="正在等待浏览器完成飞书用户授权。",
                        )
                    return self._issue_user_attempt_locked(
                        plan=plan,
                        store=store,
                        flow_store=flow_store,
                        current=current,
                        active=active,
                        credential_state=credential_state,
                        reset_staged_user=(
                            active.get("purpose") == driver.graph.reauthorization_purpose
                            and active.get("staged_user_cleared") is not True
                        ),
                        renewal=True,
                        node=node,
                    )
                return self._issue_user_attempt_locked(
                    plan=plan,
                    store=store,
                    flow_store=flow_store,
                    current=current,
                    active=active,
                    credential_state=credential_state,
                    reset_staged_user=active.get("purpose") == driver.graph.reauthorization_purpose,
                    renewal=active.get("retry_user_consent") is True,
                    node=node,
                )
        except Exception as exc:  # noqa: BLE001
            return self._error(
                "managed_authorization_failed",
                f"{type(exc).__name__}: 托管授权事务未能继续；Credential Profile 未被提交。",
            )

    def _resume_user_consent(self, plan: ManagedCliExecutionPlan) -> ManagedCliServiceResult:
        match = plan.match
        driver = self._authorization_driver(match, required=True)
        assert driver is not None
        owner_user_id = plan.owner_user_id
        provider = match.provider or driver.provider
        profile_id = plan.profile_id or ""
        credential_state = match.credential_state
        assert credential_state is not None
        store = CredentialProfileStore(self.paths, owner_user_id)
        try:
            with store.profile_lock(provider, profile_id):
                current = self._ensure_plan_is_current(store, plan, provider, profile_id)
                flow_store = self._authorization_flow_store(store, owner_user_id)
                active = self._reconcile_authorization_flow_locked(
                    flow_store=flow_store,
                    current=current,
                    provider=provider,
                    profile_id=profile_id,
                )
                purpose = None
                try:
                    node = driver.graph.node(str((active or {}).get("phase_id") or ""))
                    purpose = driver.graph.purpose(str((active or {}).get("purpose") or ""))
                except ValueError:
                    node = None
                    if active is not None:
                        flow_store.cancel_active(
                            provider,
                            profile_id,
                            reason="authorization_graph_changed",
                        )
                if (
                    active is None
                    or node is None
                    or purpose is None
                    or node.phase.phase_id not in purpose.phase_ids
                    or node.kind != AuthorizationPhaseKind.DEVICE_AUTHORIZATION
                    or active.get("status")
                    not in {
                        AuthorizationFlowStatus.AWAITING_USER.value,
                        AuthorizationFlowStatus.COLLECTING.value,
                        AuthorizationFlowStatus.VERIFYING.value,
                    }
                ):
                    return self._error("authorization_flow_missing", "没有等待续跑的飞书用户授权流程。")
                expires_at = _positive_float(active.get("expires_at"))
                recoverable_candidate = flow_store.read_candidate_state(active)
                if (
                    active.get("status") == AuthorizationFlowStatus.AWAITING_USER.value
                    and expires_at is not None
                    and expires_at <= time.time()
                    and recoverable_candidate is None
                ):
                    return self._issue_user_attempt_locked(
                        plan=plan,
                        store=store,
                        flow_store=flow_store,
                        current=current,
                        active=active,
                        credential_state=credential_state,
                        reset_staged_user=(
                            active.get("purpose") == driver.graph.reauthorization_purpose
                            and active.get("staged_user_cleared") is not True
                        ),
                        renewal=True,
                        node=node,
                    )
                staged_state = flow_store.read_staged_state(active)
                recovering_commit = active.get("status") == AuthorizationFlowStatus.VERIFYING.value
                verify = None
                identity = None
                if not recovering_commit:
                    # A previous process may have persisted a candidate and
                    # crashed before verification. Verify it first; never
                    # consume the one-time device code twice when recovery can
                    # proceed entirely from the encrypted candidate slot.
                    result = None
                    candidate_state = recoverable_candidate
                    if candidate_state is None:
                        continuation = flow_store.read_secret(active)
                        device_code = str(continuation.get("device_code") or "")
                        if not device_code:
                            raise RuntimeError("authorization continuation is incomplete")
                        result = self.backend.run_managed_provider_cli(
                            argv=list(driver.continuation_argv),
                            environment=dict(match.env),
                            credential_state_spec=credential_state,
                            toolchain_path=plan.toolchain_path,
                            container_path="/opt/puddingclaw/toolchain/node",
                            credential_state=staged_state,
                            network_enabled=True,
                            workspace_writable=False,
                            expected_runtime_image_digest=plan.runtime_image_digest,
                            continuation_secret=device_code.encode("utf-8"),
                            continuation_argument=driver.continuation.argument,
                            continuation_trailing_argv=driver.continuation.trailing_argv,
                            owner_user_id=plan.owner_user_id,
                            profile_id=profile_id,
                        )
                        if result.credential_state is not None:
                            candidate_state = result.credential_state
                            # Persist before any network verification. The
                            # verified Step-1 baseline remains untouched.
                            flow_store.write_candidate_state(active, candidate_state)

                    failure = None
                    diagnostic = None
                    if candidate_state is not None:
                        candidate_verify = self.backend.run_managed_provider_cli(
                            argv=list(driver.identity_status_argv),
                            environment=dict(match.env),
                            credential_state_spec=credential_state,
                            toolchain_path=plan.toolchain_path,
                            container_path="/opt/puddingclaw/toolchain/node",
                            credential_state=candidate_state,
                            network_enabled=True,
                            workspace_writable=False,
                            expected_runtime_image_digest=plan.runtime_image_digest,
                            owner_user_id=plan.owner_user_id,
                            profile_id=profile_id,
                        )
                        if candidate_verify.credential_state is not None:
                            candidate_state = candidate_verify.credential_state
                            flow_store.write_candidate_state(active, candidate_state)
                        candidate_identity = driver.identity_status(candidate_verify.output)
                        if candidate_verify.exit_code == 0 and driver.full_identity_ready(candidate_identity):
                            # Only independently verified bytes may replace the
                            # Step-1 staging baseline.
                            flow_store.write_staged_state(active, candidate_state)
                            staged_state = candidate_state
                            verify = candidate_verify
                            identity = candidate_identity
                        elif candidate_verify.exit_code != 0:
                            candidate_failure = driver.authorization_failure(candidate_verify.output)
                            diagnostic = _safe_authorization_diagnostic(
                                candidate_verify.output,
                                candidate_failure,
                                exit_code=candidate_verify.exit_code,
                                candidate_state_exported=True,
                                candidate_identity_verified=False,
                            )
                            if candidate_failure.retryable:
                                origin_failure = (
                                    driver.authorization_failure(result.output)
                                    if result is not None and result.exit_code != 0
                                    else None
                                )
                                preserved = flow_store.record_retryable_user_error(
                                    provider,
                                    profile_id,
                                    error=candidate_failure.reason,
                                    diagnostic=diagnostic,
                                    collecting_candidate=True,
                                    candidate_origin=_candidate_origin_projection(origin_failure),
                                )
                                assert preserved is not None
                                pending = self._authorization_payload(
                                    plan,
                                    preserved,
                                    output="已收到飞书授权结果，但 Backend 的独立验证暂时未完成；稍后继续即可，无需重新授权。",
                                )
                                pending.payload["reason"] = candidate_failure.reason
                                pending.payload["retryable"] = True
                                pending.payload["diagnostic"] = diagnostic
                                return pending
                            failure = candidate_failure
                            flow_store.remove_candidate_state(active)
                        else:
                            # A successful status command that explicitly does
                            # not report a full Bot+User identity is proof that
                            # this candidate cannot be committed. It does not,
                            # however, override an original pending/transient
                            # continuation result: provider runners export the
                            # unchanged baseline archive on every exit.
                            if result is not None and result.exit_code != 0:
                                failure = driver.authorization_failure(result.output)
                                diagnostic_output = result.output
                                diagnostic_exit_code = result.exit_code
                            elif (origin_failure := _candidate_origin_failure(active)) is not None:
                                failure = origin_failure
                                diagnostic_output = ""
                                diagnostic_exit_code = 1
                            else:
                                failure = _LarkAuthorizationFailure(
                                    "authorization_verification_failed",
                                    AuthorizationFlowStatus.FAILED.value,
                                    False,
                                    "user",
                                )
                                diagnostic_output = candidate_verify.output
                                diagnostic_exit_code = candidate_verify.exit_code
                            diagnostic = _safe_authorization_diagnostic(
                                diagnostic_output,
                                failure,
                                exit_code=diagnostic_exit_code,
                                candidate_state_exported=True,
                                candidate_identity_verified=False,
                            )
                            flow_store.remove_candidate_state(active)

                    if verify is None:
                        if failure is None and result is not None and result.exit_code != 0:
                            failure = driver.authorization_failure(result.output)
                        elif failure is None and candidate_state is None:
                            failure = _LarkAuthorizationFailure(
                                "provider_authorization_error",
                                AuthorizationFlowStatus.FAILED.value,
                                True,
                                "user",
                            )
                        if failure is None:
                            failure = _LarkAuthorizationFailure(
                                "authorization_verification_failed",
                                AuthorizationFlowStatus.FAILED.value,
                                False,
                                "user",
                            )
                        if diagnostic is None:
                            diagnostic = _safe_authorization_diagnostic(
                                result.output if result is not None else "",
                                failure,
                                exit_code=result.exit_code if result is not None else 1,
                                candidate_state_exported=candidate_state is not None,
                                candidate_identity_verified=False,
                            )
                        if failure.flow_status == AuthorizationFlowStatus.EXPIRED.value:
                            return self._issue_user_attempt_locked(
                                plan=plan,
                                store=store,
                                flow_store=flow_store,
                                current=current,
                                active=active,
                                credential_state=credential_state,
                                reset_staged_user=False,
                                renewal=True,
                                node=node,
                            )
                        if failure.flow_status == AuthorizationFlowStatus.AWAITING_USER.value or failure.retryable:
                            preserved = flow_store.record_retryable_user_error(
                                provider,
                                profile_id,
                                error=failure.reason,
                                diagnostic=diagnostic,
                            )
                            assert preserved is not None
                            pending = self._authorization_payload(
                                plan,
                                preserved,
                                output=(
                                    "飞书尚未返回授权结果，请完成浏览器授权后再告诉我。"
                                    if failure.flow_status == AuthorizationFlowStatus.AWAITING_USER.value
                                    else "Backend 尚未完成飞书 token 兑换；当前授权和应用配置均已保留，稍后直接继续即可。"
                                ),
                            )
                            pending.payload["reason"] = failure.reason
                            pending.payload["retryable"] = True
                            pending.payload["diagnostic"] = diagnostic
                            return pending
                        reset_flow = flow_store.reset_user_attempt(
                            provider,
                            profile_id,
                            error=failure.reason,
                            diagnostic=diagnostic,
                        )
                        assert reset_flow is not None
                        return self._authorization_failure_payload(
                            plan,
                            reset_flow,
                            failure,
                            request_status=failure.flow_status,
                            diagnostic=diagnostic,
                        )
                if verify is None:
                    verify = self.backend.run_managed_provider_cli(
                        argv=list(driver.identity_status_argv),
                        environment=dict(match.env),
                        credential_state_spec=credential_state,
                        toolchain_path=plan.toolchain_path,
                        container_path="/opt/puddingclaw/toolchain/node",
                        credential_state=staged_state,
                        network_enabled=True,
                        workspace_writable=False,
                        expected_runtime_image_digest=plan.runtime_image_digest,
                        owner_user_id=plan.owner_user_id,
                        profile_id=profile_id,
                    )
                    identity = driver.identity_status(verify.output)
                if verify.exit_code != 0 or not driver.full_identity_ready(identity):
                    if not recovering_commit:
                        failure = _LarkAuthorizationFailure(
                            "authorization_verification_failed",
                            AuthorizationFlowStatus.FAILED.value,
                            False,
                            "user",
                            None,
                            "Independent identity verification did not confirm an active user token.",
                        )
                        diagnostic = _safe_authorization_diagnostic(
                            verify.output,
                            failure,
                            exit_code=verify.exit_code,
                            candidate_state_exported=verify.credential_state is not None,
                            candidate_identity_verified=False,
                        )
                        retryable_flow = flow_store.reset_user_attempt(
                            provider,
                            profile_id,
                            error=failure.reason,
                            diagnostic=diagnostic,
                        )
                        assert retryable_flow is not None
                        return self._authorization_failure_payload(
                            plan,
                            retryable_flow,
                            failure,
                            request_status=AuthorizationFlowStatus.FAILED.value,
                            diagnostic=diagnostic,
                        )
                    verification_failure = driver.authorization_failure(verify.output)
                    if verification_failure.retryable:
                        return self._error(
                            "authorization_verification_retryable",
                            "飞书授权已经通过，Backend 最终验证暂时失败；提交状态已保留，稍后继续即可。",
                        )
                    return self._error(
                        "authorization_verification_failed",
                        "飞书返回成功后，Backend 的独立身份验证仍未通过；旧 Profile 保持不变。",
                    )
                if verify.credential_state is not None and verify.credential_state != staged_state:
                    # A successful online verification may refresh token bytes.
                    # Persist only after the identity envelope is proven valid.
                    staged_state = verify.credential_state
                    flow_store.write_staged_state(active, staged_state)
                if recovering_commit:
                    # Rebind the digest after every successful recovery verify,
                    # even when its exported bytes equal staged_state. This
                    # closes a crash window between staged-state write and the
                    # previous digest update.
                    active = (
                        flow_store.mark_verifying(
                            provider,
                            profile_id,
                            staged_sha256=hashlib.sha256(staged_state).hexdigest(),
                        )
                        or active
                    )
                purpose_id = str(active.get("purpose") or "")
                completed_phases = driver.graph.completed_projections(purpose_id)
                user_only = purpose_id == driver.graph.reauthorization_purpose
                staged_sha256 = hashlib.sha256(staged_state).hexdigest()
                current_revision = store.state_revision(provider, profile_id)
                base_revision = str(active.get("base_state_revision") or "")
                if recovering_commit:
                    if active.get("commit_staged_sha256") != staged_sha256:
                        raise RuntimeError("authorization commit staging digest changed")
                    if current_revision != base_revision:
                        current_state = store.read_state(provider, profile_id, credential_state=credential_state)
                        if current_state != staged_state:
                            return self._error(
                                "authorization_profile_conflict",
                                "授权期间 Credential Profile 已被其他流程更新；为避免覆盖，当前结果未提交。",
                            )
                else:
                    if current_revision != base_revision:
                        return self._error(
                            "authorization_profile_conflict",
                            "授权期间 Credential Profile 已被其他流程更新；为避免覆盖，当前结果未提交。",
                        )
                    flow_store.mark_verifying(provider, profile_id, staged_sha256=staged_sha256)
                if current_revision == base_revision:
                    committed_revision = store.write_state_if_revision(
                        provider,
                        profile_id,
                        staged_state,
                        expected_revision=base_revision,
                        credential_state=credential_state,
                    )
                    if committed_revision is None:
                        return self._error(
                            "authorization_profile_conflict",
                            "授权提交时 Credential Profile 已变化；当前结果未覆盖其他流程。",
                        )
                if store.read_state(provider, profile_id, credential_state=credential_state) != staged_state:
                    raise RuntimeError("final credential Vault readback verification failed")
                store.update_status(profile_id, "active")
                for identity_name, identity_status, verified, token_status in driver.profile_identity_updates(identity):
                    store.update_identity_status(
                        profile_id,
                        identity_name,
                        identity_status,
                        verified=verified,
                        token_status=token_status or None,
                    )
                assert node is not None
                flow_store.complete(provider, profile_id, node.phase.phase_id)
                return ManagedCliServiceResult(
                    payload={
                        "ok": True,
                        "managed_by": "managed_cli",
                        "adapter_id": match.adapter_id,
                        "profile_id": profile_id,
                        "status": "completed",
                        "authorization_completed": True,
                        "completed_phases": completed_phases,
                        "identity": driver.safe_identity_projection(identity),
                        "output": (
                            f"{driver.display_name} 用户授权已验证，Credential Profile 已原子替换。"
                            if user_only
                            else f"{driver.display_name} 授权已验证，Credential Profile 已原子更新。"
                        ),
                    },
                    exit_code=0,
                )
        except Exception as exc:  # noqa: BLE001
            return self._error(
                "managed_authorization_failed",
                f"{type(exc).__name__}: 托管授权事务未能继续；Credential Profile 未被提交。",
            )

    def _provider(
        self,
        plan: ManagedCliExecutionPlan,
        context: dict[str, Any],
    ) -> ManagedCliServiceResult:
        match = plan.match
        if match.adapter_id == "lark-cli" and plan.toolchain_revision.startswith("host-global:"):
            resolver = getattr(self.backend, "resolve_host_lark_cli", None)
            if callable(resolver):
                current_host_cli = resolver()
            else:
                from runtime_identity.host_lark_cli import HostLarkCliRuntime

                current_host_cli = HostLarkCliRuntime(self.paths).resolve()
            current_path = getattr(current_host_cli, "executable", None)
            current_version = str(getattr(current_host_cli, "version", "") or "missing")
            if current_path != plan.executable_path or plan.toolchain_revision != f"host-global:{current_version}":
                return self._error(
                    "managed_host_cli_changed",
                    "The global lark-cli path or version changed while approval was pending; prepare a new plan.",
                )
        else:
            try:
                runtime_image_digest = self._runtime_image_digest()
            except (OSError, ValueError) as exc:
                return self._error(
                    "managed_runtime_unavailable", f"Managed runtime validation failed: {type(exc).__name__}."
                )
            if runtime_image_digest != plan.runtime_image_digest:
                return self._error(
                    "managed_runtime_image_changed",
                    "Managed runtime image changed while approval was pending; prepare a new plan.",
                )
        adapter = self.registry.adapter(match.adapter_id)
        executable = plan.executable_path or (plan.toolchain_path / "bin" / adapter.toolchain_package.executable)
        if not executable.exists():
            package = adapter.toolchain_package
            return ManagedCliServiceResult(
                payload={
                    "ok": False,
                    "managed_by": "managed_cli",
                    "error": "managed_cli_not_installed",
                    "message": (
                        f"{package.executable} is not installed in its managed Toolchain. "
                        f"Install {package.package} through the trusted Adapter."
                    ),
                    "installation": {
                        "adapter_id": match.adapter_id,
                        "ecosystem": package.ecosystem,
                        "package": package.package,
                        "command_argv": ["npm", "install", "--global", package.package],
                    },
                },
                exit_code=1,
            )
        driver = self._authorization_driver(match, required=bool(match.authorization_phase))
        if match.authorization_phase:
            return self._authorization_provider(plan)
        owner_user_id = plan.owner_user_id
        store = CredentialProfileStore(self.paths, owner_user_id)
        profile_id = plan.profile_id or ""
        provider = match.provider or ""
        credential_state = match.credential_state
        if match.requires_profile and credential_state is None:
            return self._error(
                "managed_credential_contract_missing",
                "Managed CLI Adapter did not declare its credential-state contract.",
            )
        state = b""
        auto_confirmed = False
        confirmation: dict[str, Any] | None = None
        credential_profile_incomplete = False
        user_authorization_repair: _LarkAuthorizationFailure | None = None
        start_browser_watcher: tuple[str, str, str, str] | None = None
        try:
            lock = store.profile_lock(provider, profile_id) if match.requires_profile else _NullContext()
            with lock:
                current: dict[str, Any] | None = None
                if match.requires_profile:
                    # Re-check only after taking the per-Profile lock. Two
                    # approved commands may otherwise both pass a pre-lock
                    # revision check and then serialize against different
                    # credential states.
                    current = store.resolve(
                        provider,
                        explicit_profile_id=profile_id,
                        create_default=False,
                    )
                    if current is None or float(current.get("updated_at") or 0) != plan.profile_revision:
                        return self._error(
                            "managed_plan_stale",
                            "Credential Profile changed while approval was pending; retry the command.",
                        )
                    if store.state_revision(provider, profile_id) != plan.profile_state_revision:
                        return self._error(
                            "managed_plan_stale",
                            "Credential state changed while approval was pending; retry the command.",
                        )
                    state = store.read_state(
                        provider,
                        profile_id,
                        credential_state=credential_state,
                    )
                result = None
                lowered = tuple(item.lower() for item in match.argv[1:])
                approved_profile_revoke = (
                    current is not None
                    and lowered[:2] == ("config", "remove")
                    and str(context.get("_managed_cli_destructive_approval") or "")
                    == plan.destructive_approval_binding()
                )
                if approved_profile_revoke:
                    # A confirmed disconnect owns the whole Profile lifecycle:
                    # it must not be trapped behind an older browser-auth lease.
                    # Tear down the ephemeral runner first, release the durable
                    # lease with CAS-style identity checks, then execute the
                    # provider's own revoke/remove command against the current
                    # credential snapshot.
                    pending_job_id = str(current.get("browser_job_id") or "")
                    if pending_job_id:
                        finalized = self.backend.finalize_managed_browser_auth_cli(
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=pending_job_id,
                        )
                        if not finalized:
                            return self._error(
                                "browser_auth_cleanup_failed",
                                "Could not stop the active browser authorization runner; retry disconnect.",
                            )
                        released = store.finish_browser_job(
                            profile_id,
                            pending_job_id,
                            "cancelled",
                            credential_state.fingerprint,
                        )
                        if not released:
                            return self._error(
                                "managed_plan_stale",
                                "Browser authorization changed while disconnect was pending; retry disconnect.",
                            )
                    self._authorization_flow_store(store, owner_user_id).cancel_active(
                        provider,
                        profile_id,
                        reason="profile_revoked",
                    )
                    current = store.resolve(
                        provider,
                        explicit_profile_id=profile_id,
                        create_default=False,
                    )
                active_authorization: dict[str, Any] | None = None
                if driver is not None and current is not None:
                    flow_store = self._authorization_flow_store(
                        store,
                        owner_user_id,
                    )
                    active_authorization = self._reconcile_authorization_flow_locked(
                        flow_store=flow_store,
                        current=current,
                        provider=provider,
                        profile_id=profile_id,
                    )
                if current is not None and current.get("browser_job_id"):
                    pending_job_id = str(current.get("browser_job_id") or "")
                    pending = self._collect_browser_job_locked(
                        store=store,
                        owner_user_id=owner_user_id,
                        provider=provider,
                        profile_id=profile_id,
                        browser_job_id=pending_job_id,
                        credential_state=credential_state,
                    )
                    if driver is not None:
                        active_authorization = self._authorization_flow_store(
                            store,
                            owner_user_id,
                        ).active(provider, profile_id)
                    if pending.browser_status == "completed" and pending.credential_state is not None:
                        if driver is not None and match.argv == driver.app_configuration_argv:
                            result = pending
                    elif pending.browser_status == "awaiting_user_browser" and active_authorization is not None:
                        if match.action == ManagedCliAction.CREDENTIAL_READ:
                            return self._profile_status_during_authorization_payload(
                                plan,
                                current,
                                active_authorization,
                            )
                        return self._authorization_payload(
                            plan,
                            active_authorization,
                            output="飞书授权仍在等待当前浏览器步骤完成。",
                        )
                    elif pending.browser_status in {"failed", "missing"} and active_authorization is None:
                        # Collection has already released the dead Runner lease
                        # and terminalized its Flow. Continue the originally
                        # requested Provider command against the durable Profile
                        # instead of replacing a status/read request with stale
                        # browser-job diagnostics.
                        result = None
                    else:
                        result = pending

                # A managed authorization transaction exclusively owns the
                # Profile until it completes or is cancelled.  In particular,
                # never let a seemingly read-only provider command run against
                # phase-1 candidate bytes: provider CLIs can rewrite their
                # archives even for `config show`/`auth status`, which would
                # mutate the durable Vault and invalidate the transaction's CAS
                # baseline.  Authorization entry/resume commands are routed to
                # _authorization_provider before reaching this branch.
                if active_authorization is not None and result is None:
                    if match.action == ManagedCliAction.CREDENTIAL_READ:
                        return self._profile_status_during_authorization_payload(
                            plan,
                            current or {},
                            active_authorization,
                        )
                    completed_phases = active_authorization.get("completed_phase_ids", [])
                    active_phase_id = str(active_authorization.get("phase_id") or "")
                    if driver is not None and active_phase_id in completed_phases:
                        phase = driver.graph.phase(
                            active_phase_id,
                            purpose_id=str(active_authorization.get("purpose") or driver.graph.full_purpose),
                        )
                        return ManagedCliServiceResult(
                            payload={
                                "ok": True,
                                "managed_by": "managed_cli",
                                "adapter_id": match.adapter_id,
                                "route": match.route.value,
                                "action": match.action.value,
                                "profile_id": profile_id,
                                "status": "authorization_phase_completed",
                                "authorization_completed": False,
                                "phase": phase.projection(),
                                "output": f"{driver.display_name} 授权阶段已验证，可以进入下一阶段。",
                                "next_action": f"Run exactly: {shlex.join(driver.user_login_argv)}",
                            },
                            exit_code=0,
                        )
                    return self._authorization_payload(
                        plan,
                        active_authorization,
                        output=f"{driver.display_name if driver is not None else 'Provider'} 授权仍在等待当前浏览器步骤完成。",
                    )

                if result is None:
                    if driver is not None and match.argv == driver.app_configuration_argv:
                        expected_job_id = _browser_job_id(owner_user_id, provider, profile_id)
                        store.begin_browser_job(
                            profile_id,
                            expected_job_id,
                            credential_state.fingerprint,
                        )
                        result = self.backend.run_managed_browser_auth_cli(
                            argv=list(match.argv),
                            environment=dict(match.env),
                            credential_state_spec=credential_state,
                            toolchain_path=plan.toolchain_path,
                            container_path="/opt/puddingclaw/toolchain/node",
                            credential_state=state,
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            adapter_id=match.adapter_id,
                            authorization_contract_fingerprint=plan.adapter_contract_fingerprint,
                            expected_runtime_image_digest=plan.runtime_image_digest,
                        )
                        if result.browser_job_id != expected_job_id:
                            raise RuntimeError("browser authorization runner returned the wrong job id")
                        if result.browser_status == "awaiting_user_browser":
                            start_browser_watcher = (
                                owner_user_id,
                                provider,
                                profile_id,
                                expected_job_id,
                            )
                        elif result.browser_status == "completed" and result.credential_state is not None:
                            result = self._persist_browser_result_locked(
                                store=store,
                                owner_user_id=owner_user_id,
                                provider=provider,
                                profile_id=profile_id,
                                browser_job_id=expected_job_id,
                                result=result,
                                credential_state=credential_state,
                            )
                        elif result.browser_status in {"failed", "missing"}:
                            store.finish_browser_job(
                                profile_id,
                                expected_job_id,
                                "expired",
                                credential_state.fingerprint,
                            )
                    else:
                        result = self.backend.run_managed_provider_cli(
                            argv=list(match.argv),
                            environment=dict(match.env),
                            credential_state_spec=credential_state,
                            toolchain_path=plan.toolchain_path,
                            container_path="/opt/puddingclaw/toolchain/node",
                            credential_state=state,
                            network_enabled=match.requires_network,
                            workspace_writable=match.workspace_writable,
                            expected_runtime_image_digest=plan.runtime_image_digest,
                            owner_user_id=owner_user_id,
                            profile_id=profile_id,
                        )

                confirmation = (
                    driver.confirmation_required(result.output)
                    if driver is not None and result.exit_code == 10
                    else None
                )
                if confirmation is not None:
                    action = str(confirmation["action"])
                    try:
                        action_tokens = shlex.split(action, posix=True)
                    except ValueError:
                        action_tokens = []
                    original_tokens = list(match.argv[1:])
                    action_matches = any(
                        original_tokens[index : index + len(action_tokens)] == action_tokens
                        for index in range(max(0, len(original_tokens) - len(action_tokens) + 1))
                    )
                    if not action_tokens or not action_matches:
                        return self._error(
                            "invalid_confirmation_envelope",
                            "lark-cli confirmation action does not match the frozen command.",
                        )
                    action_argv = [match.argv[0], *action_tokens]
                    destructive = match.destructive or (
                        driver.destructive_argv(action_argv) if driver is not None else True
                    )
                    supplied_binding = str(context.get("_managed_cli_destructive_approval") or "")
                    approved = supplied_binding in {
                        plan.destructive_approval_binding(),
                        plan.destructive_approval_binding(
                            action=action,
                            risk="high-risk-write",
                        ),
                    }
                    if not destructive or approved:
                        retry_state = result.credential_state or state
                        result = self.backend.run_managed_provider_cli(
                            argv=[*match.argv, "--yes"],
                            environment=dict(match.env),
                            credential_state_spec=credential_state,
                            toolchain_path=plan.toolchain_path,
                            container_path="/opt/puddingclaw/toolchain/node",
                            credential_state=retry_state,
                            network_enabled=match.requires_network,
                            workspace_writable=match.workspace_writable,
                            expected_runtime_image_digest=plan.runtime_image_digest,
                            owner_user_id=owner_user_id,
                            profile_id=profile_id,
                        )
                        auto_confirmed = not destructive
                        confirmation = None
                if result.exit_code != 0 and match.action == ManagedCliAction.PROVIDER_OPERATION:
                    user_authorization_repair = (
                        driver.user_credential_failure(result.output) if driver is not None else None
                    )
                    if user_authorization_repair is not None:
                        store.update_identity_status(
                            profile_id,
                            "user",
                            "authorization_required",
                            reason=user_authorization_repair.reason,
                        )
                if (
                    user_authorization_repair is None
                    and match.requires_profile
                    and result.credential_state is not None
                    and result.browser_status is None
                ):
                    committed_revision = store.write_state_if_revision(
                        provider,
                        profile_id,
                        result.credential_state,
                        expected_revision=plan.profile_state_revision,
                        credential_state=credential_state,
                    )
                    if committed_revision is None:
                        return self._error(
                            "credential_writeback_conflict",
                            "Credential state changed during execution; rotated token bytes were not committed.",
                        )
                    if result.exit_code == 0:
                        next_status: str | None = None
                        lowered = tuple(item.lower() for item in match.argv[1:])
                        if lowered[:2] == ("auth", "login") and "--device-code" in lowered:
                            next_status = "active"
                        elif lowered[:2] in {("auth", "logout"), ("config", "remove")}:
                            next_status = "revoked"
                        if next_status is not None:
                            store.update_status(profile_id, next_status)
                if (
                    driver is not None
                    and match.requires_profile
                    and match.action == ManagedCliAction.PROVIDER_OPERATION
                    and result.exit_code == 0
                ):
                    for identity_name, identity_status, verified, token_status in (
                        driver.successful_operation_identity_updates(result.output)
                    ):
                        store.update_identity_status(
                            profile_id,
                            identity_name,
                            identity_status,
                            verified=verified,
                            token_status=token_status or None,
                        )
                    refreshed_profile = store.resolve(
                        provider,
                        explicit_profile_id=profile_id,
                        create_default=False,
                    )
                    refreshed_identities = (
                        refreshed_profile.get("identities")
                        if isinstance(refreshed_profile, dict)
                        and isinstance(refreshed_profile.get("identities"), dict)
                        else {}
                    )
                    if driver.durable_profile_ready(refreshed_identities):
                        store.update_status(profile_id, "active")
                if (
                    driver is not None
                    and match.requires_profile
                    and result.exit_code == 0
                    and lowered[:2] == ("auth", "status")
                    and "--verify" in lowered
                ):
                    verified_status = driver.identity_status(result.output)
                    identities = (
                        verified_status.get("identities")
                        if isinstance(verified_status, dict) and isinstance(verified_status.get("identities"), dict)
                        else {}
                    )
                    for identity_name in ("bot", "user"):
                        assessment = identities.get(identity_name)
                        if not isinstance(assessment, dict):
                            continue
                        identity_status = str(assessment.get("status") or "unknown").lower()
                        token_status = assessment.get("tokenStatus", assessment.get("token_status"))
                        store.update_identity_status(
                            profile_id,
                            identity_name,
                            identity_status,
                            verified=(assessment.get("verified") is True),
                            token_status=(str(token_status).lower() if token_status else None),
                        )
                    if verified_status is not None:
                        store.update_status(
                            profile_id,
                            "active" if driver.full_identity_ready(verified_status) else "authorization_required",
                        )
                if (
                    match.requires_profile
                    and result.exit_code == 0
                    and lowered[:2] in {("auth", "logout"), ("config", "remove")}
                ):
                    if lowered[:2] == ("config", "remove"):
                        store.update_status(profile_id, "revoked")
                        store.update_identity_status(profile_id, "bot", "revoked", verified=False)
                        store.update_identity_status(
                            profile_id,
                            "user",
                            "revoked",
                            verified=False,
                            token_status="revoked",
                        )
                    else:
                        store.update_status(profile_id, "active")
                        store.update_identity_status(
                            profile_id,
                            "user",
                            "authorization_required",
                            reason="user_logged_out",
                            verified=False,
                            token_status="revoked",
                        )
                    self._authorization_flow_store(store, owner_user_id).cancel_active(
                        provider,
                        profile_id,
                        reason="profile_revoked",
                    )
                if match.requires_profile and result.exit_code != 0 and _MISSING_CLIENT_SECRET.search(result.output):
                    store.update_status(profile_id, "expired")
                    credential_profile_incomplete = True
        except Exception as exc:  # noqa: BLE001
            return self._error("managed_provider_failed", f"{type(exc).__name__}: {exc}")
        if start_browser_watcher is not None:
            self._start_browser_watcher(
                owner_user_id=start_browser_watcher[0],
                provider=start_browser_watcher[1],
                profile_id=start_browser_watcher[2],
                browser_job_id=start_browser_watcher[3],
                credential_state=credential_state,
            )
        if user_authorization_repair is not None:
            assert driver is not None
            consent_match = self.registry.match(shlex.join(driver.user_login_argv))
            assert consent_match is not None
            consent_plan = self.plan(
                consent_match,
                {
                    "credential_profile_id": profile_id,
                    "project_id": context.get("project_id"),
                },
            )
            started = self._start_user_consent(consent_plan)
            if started.payload.get("status") == "awaiting_user_browser":
                started.payload["trigger"] = {
                    "reason": user_authorization_repair.reason,
                    "identity": "user",
                    "safe_to_retry": True,
                    "interrupted_action_sha256": hashlib.sha256("\0".join(match.argv).encode("utf-8")).hexdigest(),
                }
                started.payload["output"] = (
                    "原飞书操作因用户授权失效而暂停。Bot 身份保持可用；请完成用户授权，成功后重试原操作。"
                )
            return started
        output = redact_managed_cli_output(result.output)
        awaiting = result.browser_status == "awaiting_user_browser" or (
            match.route == ManagedCliRoute.BROWSER_AUTH and driver is not None and result.exit_code == 0
        )
        payload: dict[str, Any] = {
            "ok": result.exit_code == 0,
            "managed_by": "managed_cli",
            "adapter_id": match.adapter_id,
            "route": match.route.value,
            "action": match.action.value,
            "profile_id": profile_id or None,
            "status": "awaiting_user_browser" if awaiting else ("completed" if result.exit_code == 0 else "failed"),
            "authorization_completed": False if awaiting else None,
            "output": output,
        }
        if auto_confirmed:
            payload["confirmation"] = "auto_approved_non_delete"
        if confirmation is not None:
            action = str(confirmation["action"])
            payload.update(
                {
                    "ok": False,
                    "status": "confirmation_required",
                    "confirmation": {
                        "action": confirmation.get("action"),
                        "risk": confirmation.get("risk"),
                        "destructive": True,
                        "approval_binding": plan.destructive_approval_binding(
                            action=action,
                            risk="high-risk-write",
                        ),
                    },
                }
            )
        if credential_profile_incomplete:
            payload.update(
                {
                    "ok": False,
                    "status": "credential_profile_incomplete",
                    "error": "credential_profile_incomplete",
                    "next_action": (
                        "The shared Credential Profile lost provider-native secret state. "
                        "Do not retry auth login. Reconfigure this Profile once with "
                        "lark-cli config init --new, then start auth login."
                    ),
                }
            )
        if awaiting:
            payload["next_action"] = (
                "Show the authorization URL/QR code and end this Agent turn. "
                "After the user confirms completion, continue with the exact Backend-owned command for the next phase; "
                "do not probe with config show or auth status."
            )
        return ManagedCliServiceResult(payload=payload, exit_code=result.exit_code)

    def _persist_browser_result_locked(
        self,
        *,
        store: CredentialProfileStore,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        browser_job_id: str,
        result: Any,
        credential_state: CredentialStateSpec,
    ) -> Any:
        """Persist phase-1 output to staging before ACKing its tmpfs container."""

        adapter = self.registry.for_provider(provider)
        driver = self.authorization_drivers.for_adapter(adapter.adapter_id)
        assert driver is not None
        if result.browser_status == "completed" and result.credential_state is not None:
            flow_store = self._authorization_flow_store(store, owner_user_id)
            flow = flow_store.active(provider, profile_id)
            try:
                node = driver.graph.node(str((flow or {}).get("phase_id") or ""))
            except ValueError as exc:
                raise RuntimeError("browser authorization flow identity is missing") from exc
            if flow is None or node.kind != AuthorizationPhaseKind.BROWSER_CONFIGURATION:
                raise RuntimeError("browser authorization flow identity is missing")
            flow_store.write_staged_state(flow, result.credential_state)
            if flow_store.read_staged_state(flow) != result.credential_state:
                raise RuntimeError("staged credential readback verification failed")
            flow_store.mark_phase_verified(provider, profile_id, node.phase.phase_id)
            if not store.finish_browser_job(
                profile_id,
                browser_job_id,
                "completed",
                credential_state.fingerprint,
            ):
                raise RuntimeError("browser authorization Profile lease changed")
            self.backend.finalize_managed_browser_auth_cli(
                owner_user_id=owner_user_id,
                provider=provider,
                profile_id=profile_id,
                browser_job_id=browser_job_id,
            )
        elif result.browser_status in {"failed", "missing"}:
            store.finish_browser_job(
                profile_id,
                browser_job_id,
                "expired" if result.browser_status == "missing" else "failed",
                credential_state.fingerprint,
            )
            flow_store = self._authorization_flow_store(store, owner_user_id)
            flow = flow_store.active(provider, profile_id)
            try:
                node = driver.graph.node(str((flow or {}).get("phase_id") or ""))
            except ValueError:
                node = None
            if (
                flow is not None
                and node is not None
                and node.kind == AuthorizationPhaseKind.BROWSER_CONFIGURATION
                and flow.get("phase_id") == node.phase.phase_id
                and node.phase.phase_id not in flow.get("completed_phase_ids", [])
            ):
                flow_store.fail(
                    provider,
                    profile_id,
                    status=(
                        AuthorizationFlowStatus.EXPIRED.value
                        if result.browser_status == "missing"
                        else AuthorizationFlowStatus.FAILED.value
                    ),
                    error=("browser_job_missing" if result.browser_status == "missing" else "browser_job_failed"),
                )
            if result.browser_status == "failed":
                self.backend.finalize_managed_browser_auth_cli(
                    owner_user_id=owner_user_id,
                    provider=provider,
                    profile_id=profile_id,
                    browser_job_id=browser_job_id,
                )
        if result.browser_status in {"completed", "failed", "missing"}:
            try:
                adapter = self.registry.for_provider(provider)
                self.toolchains.release_revision_leases_by_owner(
                    adapter_id=adapter.adapter_id,
                    owner_kind="runner",
                    owner_id=browser_job_id,
                )
                self.toolchains.gc_revisions(adapter.adapter_id)
            except (OSError, ValueError):
                pass
        return result

    def _collect_browser_job_locked(
        self,
        *,
        store: CredentialProfileStore,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        browser_job_id: str,
        credential_state: CredentialStateSpec,
    ) -> Any:
        if not browser_job_id:
            raise RuntimeError("browser authorization Profile lease is missing its job id")
        adapter = self.registry.for_provider(provider)
        result = self.backend.collect_managed_browser_auth_cli(
            owner_user_id=owner_user_id,
            provider=provider,
            profile_id=profile_id,
            credential_state_spec=credential_state,
            adapter_id=adapter.adapter_id,
            authorization_contract_fingerprint=self._adapter_contract_fingerprint(adapter.adapter_id),
        )
        if result.browser_job_id != browser_job_id:
            raise RuntimeError("browser authorization job identity changed")
        return self._persist_browser_result_locked(
            store=store,
            owner_user_id=owner_user_id,
            provider=provider,
            profile_id=profile_id,
            browser_job_id=browser_job_id,
            result=result,
            credential_state=credential_state,
        )

    def _start_browser_watcher(
        self,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        browser_job_id: str,
        credential_state: CredentialStateSpec,
    ) -> None:
        if not all((owner_user_id, provider, profile_id, browser_job_id)):
            return
        watcher_key = f"{self.paths.root}:{owner_user_id}:{provider}:{profile_id}:{browser_job_id}"
        with _BROWSER_WATCHERS_LOCK:
            if watcher_key in _BROWSER_WATCHERS:
                return
            _BROWSER_WATCHERS.add(watcher_key)

        def watch() -> None:
            store = CredentialProfileStore(self.paths, owner_user_id)
            try:
                while True:
                    with store.profile_lock(provider, profile_id):
                        current = store.resolve(
                            provider,
                            explicit_profile_id=profile_id,
                            create_default=False,
                        )
                        if (
                            current is None
                            or current.get("browser_job_id") != browser_job_id
                            or current.get("credential_state_fingerprint") != credential_state.fingerprint
                        ):
                            return
                        result = self._collect_browser_job_locked(
                            store=store,
                            owner_user_id=owner_user_id,
                            provider=provider,
                            profile_id=profile_id,
                            browser_job_id=browser_job_id,
                            credential_state=credential_state,
                        )
                    if result.browser_status != "awaiting_user_browser":
                        return
                    time.sleep(0.5)
            except Exception:  # noqa: BLE001
                # The container retains its tmpfs for recovery. A later service
                # construction or managed command restarts this lifecycle worker.
                return
            finally:
                with _BROWSER_WATCHERS_LOCK:
                    _BROWSER_WATCHERS.discard(watcher_key)

        threading.Thread(
            target=watch,
            name=f"puddingclaw-browser-{browser_job_id[:8]}",
            daemon=True,
        ).start()

    @staticmethod
    def _error(code: str, message: str) -> ManagedCliServiceResult:
        return ManagedCliServiceResult(
            payload={
                "ok": False,
                "managed_by": "managed_cli",
                "error": code,
                "message": message,
            },
            exit_code=1,
        )


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, traceback):
        return False
