"""Managed CLI control-plane service."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import threading
import time
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
    is_lark_destructive_argv,
)
from runtime_identity.authorization import (
    LARK_APP_CONFIGURATION_PHASE,
    LARK_USER_CONSENT_PHASE,
    LARK_USER_REAUTHORIZATION_PHASE,
    AuthorizationFlowStatus,
    AuthorizationFlowStore,
)
from runtime_identity.paths import PuddingClawPaths, trusted_owner_user_id
from runtime_identity.profiles import CredentialProfileStore
from runtime_identity.toolchains import ToolchainManager

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
    candidate = str(raw_url or "").strip().strip('"\'')
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
        or any(
            marker in normalized for marker in ("invalid_request", "invalid request", "请求不合法")
        )
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
    if any(
        marker in normalized
        for marker in ("timeout", "timed out", "econnreset", "connection reset", "dns", "tls", "rate limit")
    ) or provider_status == 429 or (provider_status is not None and 500 <= provider_status <= 599):
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

    def approval_preview(self) -> str:
        return json.dumps(
            {
                "adapter_id": self.match.adapter_id,
                "action": self.match.action.value,
                "route": self.match.route.value,
                "argv": list(self.match.argv),
                "owner_user_id": self.owner_user_id,
                "profile_id": self.profile_id,
                "profile_revision": self.profile_revision,
                "toolchain_revision": self.toolchain_revision,
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
            "toolchain_revision": self.toolchain_revision,
            "action": action,
            "risk": risk,
        }
        return hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class ManagedCliService:
    def __init__(self, backend: object, *, paths: PuddingClawPaths | None = None) -> None:
        self.backend = backend
        self.paths = paths or PuddingClawPaths.from_environment()
        runtime_owner = getattr(backend, "manager", backend)
        runtime_contract = str(getattr(runtime_owner, "runtime_contract", "")).strip()
        if not runtime_contract:
            raise ValueError("managed CLI backend does not expose a runtime contract")
        self.toolchains = ToolchainManager(self.paths, runtime_contract)
        self._recover_browser_watchers()

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
        flow_store = self._authorization_flow_store(store, owner_user_id)
        live_jobs: set[tuple[str, str, str]] = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            provider = str(job.get("provider") or "")
            try:
                credential_state = ManagedCliRegistry.credential_state_for_provider(provider)
            except ValueError:
                continue
            profile_id = str(job.get("profile_id") or "")
            browser_job_id = str(job.get("browser_job_id") or "")
            if not profile_id or not browser_job_id:
                continue
            if job.get("credential_state_fingerprint") != credential_state.fingerprint:
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
                    phase_one_staged = bool(
                        flow is not None
                        and LARK_APP_CONFIGURATION_PHASE.phase_id
                        in flow.get("completed_phase_ids", [])
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
                            and flow.get("phase_id") == LARK_APP_CONFIGURATION_PHASE.phase_id
                            and LARK_APP_CONFIGURATION_PHASE.phase_id
                            not in flow.get("completed_phase_ids", [])
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
                        refreshed_jobs = self.backend.list_managed_browser_auth_jobs(
                            owner_user_id=owner_user_id
                        )
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
                            credential_state = ManagedCliRegistry.credential_state_for_provider(provider)
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
        ref = self.toolchains.resolve_node()
        owner_user_id = trusted_owner_user_id()
        profile_id: str | None = None
        profile_revision: float | None = None
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
        return ManagedCliExecutionPlan(
            match=match,
            owner_user_id=owner_user_id,
            profile_id=profile_id,
            profile_revision=profile_revision,
            toolchain_path=ref.host_path,
            toolchain_revision=ref.host_path.name,
        )

    def execute(
        self,
        plan: ManagedCliExecutionPlan | ManagedCliMatch,
        context: dict[str, Any] | None = None,
    ) -> ManagedCliServiceResult:
        if isinstance(plan, ManagedCliMatch):
            plan = self.plan(plan, context or {})
        if plan.match.route == ManagedCliRoute.INSTALLER:
            return self._install(plan.match)
        return self._provider(plan, context or {})

    def _install(self, match: ManagedCliMatch) -> ManagedCliServiceResult:
        if match.adapter_id != "lark-cli" or not match.distribution:
            return self._error("unsupported_managed_install", "No trusted installer owns this request.")
        result = self.toolchains.install_lark(self.backend, match.distribution)
        return ManagedCliServiceResult(
            payload={
                "ok": result.exit_code == 0,
                "managed_by": "managed_cli",
                "adapter_id": match.adapter_id,
                "route": match.route.value,
                "action": match.action.value,
                "distribution": match.distribution,
                "toolchain": (f"user://runtime/toolchains/node/{self.toolchains.resolve_node().runtime_contract}"),
                "output": redact_managed_cli_output(result.output),
            },
            exit_code=result.exit_code,
        )

    def _lark_authorization_provider(self, plan: ManagedCliExecutionPlan) -> ManagedCliServiceResult:
        """Execute the two-phase Lark authorization state machine.

        Public browser material is projected as structured data. Provider
        continuation secrets and staged credentials never enter the returned
        ToolMessage; the existing Profile Vault is replaced only after phase 2
        passes an independent identity verification.
        """

        match = plan.match
        if match.authorization_phase == LARK_APP_CONFIGURATION_PHASE.phase_id:
            return self._start_lark_app_configuration(plan)
        if match.action == ManagedCliAction.AUTHORIZATION_RESUME:
            return self._resume_lark_user_consent(plan)
        if match.authorization_phase == LARK_USER_CONSENT_PHASE.phase_id:
            return self._start_lark_user_consent(plan)
        return self._error("unsupported_authorization_phase", "The Lark authorization phase is not supported.")

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
        return AuthorizationFlowStore(self.paths, owner_user_id, vault=store.vault)

    @staticmethod
    def _reconcile_authorization_flow_locked(
        *,
        flow_store: AuthorizationFlowStore,
        current: dict[str, Any],
        provider: str,
        profile_id: str,
    ) -> dict[str, Any] | None:
        """Reconcile one Flow using its Adapter-declared evidence contract."""

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

    def _start_lark_app_configuration(self, plan: ManagedCliExecutionPlan) -> ManagedCliServiceResult:
        match = plan.match
        owner_user_id = plan.owner_user_id
        provider = match.provider or "lark"
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
                    active.get("purpose") != "lark_full_authorization"
                    or active_base_revision not in {"", base_revision}
                    # An explicit full replacement while phase 2 is active
                    # starts over from an empty staging state. Reusing the
                    # phase-2 Flow would mix two replacement transactions.
                    or active.get("phase_id") != LARK_APP_CONFIGURATION_PHASE.phase_id
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
                        current = store.resolve(
                            provider,
                            explicit_profile_id=profile_id,
                            create_default=False,
                        ) or current
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

                if active is not None and active.get("phase_id") == LARK_APP_CONFIGURATION_PHASE.phase_id:
                    if LARK_APP_CONFIGURATION_PHASE.phase_id in active.get("completed_phase_ids", []):
                        return ManagedCliServiceResult(
                            payload={
                                "ok": True,
                                "managed_by": "managed_cli",
                                "adapter_id": match.adapter_id,
                                "profile_id": profile_id,
                                "status": "authorization_phase_completed",
                                "phase": LARK_APP_CONFIGURATION_PHASE.projection(),
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
                    purpose="lark_full_authorization",
                    phase=LARK_APP_CONFIGURATION_PHASE,
                    profile_revision=float(current.get("updated_at") or 0),
                    base_state_revision=base_revision,
                    adapter_contract_fingerprint=credential_state.fingerprint,
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
                            "phase": LARK_APP_CONFIGURATION_PHASE.projection(),
                            "output": "飞书应用配置已验证，可以进入第 2/2 步用户授权。",
                        },
                        exit_code=0,
                    )
                if result.browser_status != "awaiting_user_browser":
                    flow_store.fail(provider, profile_id, status=AuthorizationFlowStatus.FAILED.value, error="app setup failed")
                    store.finish_browser_job(profile_id, browser_job_id, "repair_required", credential_state.fingerprint)
                    return self._error(
                        "lark_app_configuration_failed",
                        "飞书应用配置未完成；旧 Credential Profile 保持不变，可重新发起配置。",
                    )
                verification_url = _lark_config_authorization_url(result.output)
                if verification_url is None:
                    raise RuntimeError("Lark app configuration did not return a trusted verification URL")
                qr_ascii = _terminal_qr(result.output)
                if not qr_ascii:
                    qr = self.backend.run_managed_provider_cli(
                        argv=["lark-cli", "auth", "qrcode", verification_url, "--ascii"],
                        environment=dict(match.env),
                        credential_state_spec=None,
                        toolchain_path=plan.toolchain_path,
                        container_path="/opt/puddingclaw/toolchain/node",
                        credential_state=b"",
                        network_enabled=False,
                        workspace_writable=False,
                    )
                    qr_ascii = _terminal_qr(qr.output)
                flow = flow_store.begin_or_advance(
                    provider=provider,
                    adapter_id=match.adapter_id,
                    profile_id=profile_id,
                    purpose="lark_full_authorization",
                    phase=LARK_APP_CONFIGURATION_PHASE,
                    profile_revision=float(current.get("updated_at") or 0),
                    base_state_revision=base_revision,
                    adapter_contract_fingerprint=credential_state.fingerprint,
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

    def _issue_lark_user_attempt_locked(
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
    ) -> ManagedCliServiceResult:
        """Create one device-code attempt without mutating the durable Profile."""

        match = plan.match
        provider = match.provider or "lark"
        profile_id = plan.profile_id or ""
        purpose = str(active.get("purpose") or "lark_full_authorization")
        staged_state = flow_store.read_staged_state(active)
        if purpose != "lark_user_reauthorization":
            preflight = self.backend.run_managed_provider_cli(
                argv=["lark-cli", "auth", "status", "--json", "--verify"],
                environment=dict(match.env),
                credential_state_spec=credential_state,
                toolchain_path=plan.toolchain_path,
                container_path="/opt/puddingclaw/toolchain/node",
                credential_state=staged_state,
                network_enabled=True,
                workspace_writable=False,
            )
            if preflight.credential_state is not None:
                flow_store.write_staged_state(active, preflight.credential_state)
                staged_state = preflight.credential_state
            if not _lark_bot_ready(_lark_identity_status(preflight.output)):
                return self._error(
                    "authorization_prerequisite_failed",
                    "飞书应用配置未通过 Bot 身份验证；请完整重新配置，而不是继续重试用户授权。",
                )
        if reset_staged_user:
            # This logout runs only against the encrypted staging copy. The
            # durable Profile bytes are untouched until independent verify and
            # CAS commit. Provider adapters must keep this operation local-only.
            reset = self.backend.run_managed_provider_cli(
                argv=["lark-cli", "auth", "logout", "--json"],
                environment=dict(match.env),
                credential_state_spec=credential_state,
                toolchain_path=plan.toolchain_path,
                container_path="/opt/puddingclaw/toolchain/node",
                credential_state=staged_state,
                network_enabled=False,
                workspace_writable=False,
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
            list(match.argv)
            if match.action == ManagedCliAction.BROWSER_AUTH
            else ["lark-cli", "auth", "login", "--domain", "all", "--no-wait", "--json"]
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
        )
        if result.exit_code != 0:
            return self._error(
                "lark_user_authorization_failed",
                "飞书用户授权无法启动；应用配置和旧 Credential Profile 保持不变。",
            )
        device = _lark_device_authorization(result.output)
        if device is None:
            return self._error(
                "lark_user_authorization_invalid_response",
                "飞书用户授权启动结果缺少完整的设备授权数据；应用配置和旧 Credential Profile 保持不变。",
            )
        verification_url = _validated_lark_authorization_url(
            str(device["verification_url"]), phase_id=LARK_USER_CONSENT_PHASE.phase_id
        )
        if verification_url is None:
            return self._error(
                "lark_user_authorization_untrusted_url",
                "飞书返回的用户授权地址未通过官方域名、路径和公开参数校验；应用配置和旧 Credential Profile 保持不变。",
            )
        if result.credential_state is not None:
            flow_store.write_staged_state(active, result.credential_state)
        qr = self.backend.run_managed_provider_cli(
            argv=["lark-cli", "auth", "qrcode", verification_url, "--ascii"],
            environment=dict(match.env),
            credential_state_spec=None,
            toolchain_path=plan.toolchain_path,
            container_path="/opt/puddingclaw/toolchain/node",
            credential_state=b"",
            network_enabled=False,
            workspace_writable=False,
        )
        expires_in = _positive_float(device.get("expires_in"))
        phase = (
            LARK_USER_REAUTHORIZATION_PHASE
            if purpose == "lark_user_reauthorization"
            else LARK_USER_CONSENT_PHASE
        )
        flow = flow_store.begin_or_advance(
            provider=provider,
            adapter_id=match.adapter_id,
            profile_id=profile_id,
            purpose=purpose,
            phase=phase,
            profile_revision=float(current.get("updated_at") or 0),
            base_state_revision=str(
                active.get("base_state_revision") or store.state_revision(provider, profile_id)
            ),
            adapter_contract_fingerprint=credential_state.fingerprint,
            public={
                "verification_url": verification_url,
                "user_code": device.get("user_code"),
                "qr_ascii": _terminal_qr(qr.output),
            },
            secret={"device_code": str(device["device_code"])},
            expires_at=(time.time() + expires_in if expires_in is not None else None),
            new_attempt=renewal,
        )
        if renewal:
            last_attempt = active.get("last_user_attempt")
            last_reason = (
                str(last_attempt.get("reason") or "")
                if isinstance(last_attempt, dict)
                else ""
            )
            output = (
                "上一飞书授权链接已过期，已生成新的二维码；旧连接保持不变。"
                if last_reason in {"", "authorization_expired"}
                else "上一飞书用户授权尝试未完成，已生成新的二维码；应用配置和旧连接保持不变。"
            )
        elif purpose == "lark_user_reauthorization":
            output = "第 1/1 步已开始：请重新授权访问你的飞书数据。"
        else:
            output = "第 2/2 步已开始：请授权 CLI 应用访问你的飞书数据。"
        return self._authorization_payload(plan, flow, output=output)

    def _start_lark_user_consent(self, plan: ManagedCliExecutionPlan) -> ManagedCliServiceResult:
        match = plan.match
        owner_user_id = plan.owner_user_id
        provider = match.provider or "lark"
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
                bot_status = (
                    str(bot_assessment.get("status") or "").lower()
                    if isinstance(bot_assessment, dict)
                    else ""
                )
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
                    durable_bot_ready
                    and active is not None
                    and active.get("purpose") == "lark_full_authorization"
                    and active.get("phase_id") == LARK_APP_CONFIGURATION_PHASE.phase_id
                    and LARK_APP_CONFIGURATION_PHASE.phase_id
                    not in active.get("completed_phase_ids", [])
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
                        current = store.resolve(
                            provider,
                            explicit_profile_id=profile_id,
                            create_default=False,
                        ) or current
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
                if active is None:
                    # Existing Profiles may already contain a verified Bot/App
                    # configuration (for example after an earlier login). Seed
                    # the transaction from that last-known-good state so a
                    # user-only reauthorization does not repeat step 1.
                    if durable_state:
                        if bot_status in {"ready", "active"} or current.get("status") == "active":
                            if str(current.get("status") or "").startswith("awaiting_"):
                                store.update_status(profile_id, "active")
                                current = store.resolve(
                                    provider,
                                    explicit_profile_id=profile_id,
                                    create_default=False,
                                ) or current
                            active = flow_store.begin_or_advance(
                                provider=provider,
                                adapter_id=match.adapter_id,
                                profile_id=profile_id,
                                purpose="lark_user_reauthorization",
                                phase=LARK_APP_CONFIGURATION_PHASE,
                                profile_revision=float(current.get("updated_at") or 0),
                                base_state_revision=store.state_revision(provider, profile_id),
                                adapter_contract_fingerprint=credential_state.fingerprint,
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
                                LARK_APP_CONFIGURATION_PHASE.phase_id,
                            )
                            active = flow_store.active(provider, profile_id)
                        else:
                            store.update_status(profile_id, "repair_required")
                if active is None or LARK_APP_CONFIGURATION_PHASE.phase_id not in active.get(
                    "completed_phase_ids", []
                ):
                    return self._error(
                        "authorization_prerequisite_failed",
                        "第 1/2 步应用配置尚未通过 Backend 验证，不能提前发起用户授权。",
                    )
                if (
                    active.get("phase_id") == LARK_USER_CONSENT_PHASE.phase_id
                    and active.get("status") == AuthorizationFlowStatus.AWAITING_USER.value
                ):
                    expires_at = _positive_float(active.get("expires_at"))
                    if expires_at is None or expires_at > time.time():
                        return self._authorization_payload(
                            plan,
                            active,
                            output="正在等待浏览器完成飞书用户授权。",
                        )
                    return self._issue_lark_user_attempt_locked(
                        plan=plan,
                        store=store,
                        flow_store=flow_store,
                        current=current,
                        active=active,
                        credential_state=credential_state,
                        reset_staged_user=(
                            active.get("purpose") == "lark_user_reauthorization"
                            and active.get("staged_user_cleared") is not True
                        ),
                        renewal=True,
                    )
                return self._issue_lark_user_attempt_locked(
                    plan=plan,
                    store=store,
                    flow_store=flow_store,
                    current=current,
                    active=active,
                    credential_state=credential_state,
                    reset_staged_user=active.get("purpose") == "lark_user_reauthorization",
                    renewal=active.get("retry_user_consent") is True,
                )
        except Exception as exc:  # noqa: BLE001
            return self._error(
                "managed_authorization_failed",
                f"{type(exc).__name__}: 托管授权事务未能继续；Credential Profile 未被提交。",
            )

    def _resume_lark_user_consent(self, plan: ManagedCliExecutionPlan) -> ManagedCliServiceResult:
        match = plan.match
        owner_user_id = plan.owner_user_id
        provider = match.provider or "lark"
        profile_id = plan.profile_id or ""
        credential_state = match.credential_state
        assert credential_state is not None
        store = CredentialProfileStore(self.paths, owner_user_id)
        try:
            with store.profile_lock(provider, profile_id):
                current = self._ensure_plan_is_current(store, plan, provider, profile_id)
                flow_store = self._authorization_flow_store(store, owner_user_id)
                active = flow_store.active(provider, profile_id)
                if (
                    active is None
                    or active.get("phase_id") != LARK_USER_CONSENT_PHASE.phase_id
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
                    return self._issue_lark_user_attempt_locked(
                        plan=plan,
                        store=store,
                        flow_store=flow_store,
                        current=current,
                        active=active,
                        credential_state=credential_state,
                        reset_staged_user=(
                            active.get("purpose") == "lark_user_reauthorization"
                            and active.get("staged_user_cleared") is not True
                        ),
                        renewal=True,
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
                            argv=["lark-cli", "auth", "login"],
                            environment=dict(match.env),
                            credential_state_spec=credential_state,
                            toolchain_path=plan.toolchain_path,
                            container_path="/opt/puddingclaw/toolchain/node",
                            credential_state=staged_state,
                            network_enabled=True,
                            workspace_writable=False,
                            continuation_secret=device_code.encode("utf-8"),
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
                            argv=["lark-cli", "auth", "status", "--json", "--verify"],
                            environment=dict(match.env),
                            credential_state_spec=credential_state,
                            toolchain_path=plan.toolchain_path,
                            container_path="/opt/puddingclaw/toolchain/node",
                            credential_state=candidate_state,
                            network_enabled=True,
                            workspace_writable=False,
                        )
                        if candidate_verify.credential_state is not None:
                            candidate_state = candidate_verify.credential_state
                            flow_store.write_candidate_state(active, candidate_state)
                        candidate_identity = _lark_identity_status(candidate_verify.output)
                        if candidate_verify.exit_code == 0 and _lark_full_identity_ready(candidate_identity):
                            # Only independently verified bytes may replace the
                            # Step-1 staging baseline.
                            flow_store.write_staged_state(active, candidate_state)
                            staged_state = candidate_state
                            verify = candidate_verify
                            identity = candidate_identity
                        elif candidate_verify.exit_code != 0:
                            candidate_failure = _lark_authorization_failure(candidate_verify.output)
                            diagnostic = _safe_authorization_diagnostic(
                                candidate_verify.output,
                                candidate_failure,
                                exit_code=candidate_verify.exit_code,
                                candidate_state_exported=True,
                                candidate_identity_verified=False,
                            )
                            if candidate_failure.retryable:
                                origin_failure = (
                                    _lark_authorization_failure(result.output)
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
                                failure = _lark_authorization_failure(result.output)
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
                            failure = _lark_authorization_failure(result.output)
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
                            return self._issue_lark_user_attempt_locked(
                                plan=plan,
                                store=store,
                                flow_store=flow_store,
                                current=current,
                                active=active,
                                credential_state=credential_state,
                                reset_staged_user=False,
                                renewal=True,
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
                        argv=["lark-cli", "auth", "status", "--json", "--verify"],
                        environment=dict(match.env),
                        credential_state_spec=credential_state,
                        toolchain_path=plan.toolchain_path,
                        container_path="/opt/puddingclaw/toolchain/node",
                        credential_state=staged_state,
                        network_enabled=True,
                        workspace_writable=False,
                    )
                    identity = _lark_identity_status(verify.output)
                if verify.exit_code != 0 or not _lark_full_identity_ready(identity):
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
                    verification_failure = _lark_authorization_failure(verify.output)
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
                    store.write_state(
                        provider,
                        profile_id,
                        staged_state,
                        credential_state=credential_state,
                    )
                if store.read_state(provider, profile_id, credential_state=credential_state) != staged_state:
                    raise RuntimeError("final credential Vault readback verification failed")
                store.update_status(profile_id, "active")
                store.update_identity_status(profile_id, "bot", "ready", verified=True)
                store.update_identity_status(
                    profile_id,
                    "user",
                    "active",
                    verified=True,
                    token_status="valid",
                )
                flow_store.complete(provider, profile_id, LARK_USER_CONSENT_PHASE.phase_id)
                user_only = active.get("purpose") == "lark_user_reauthorization"
                completed_phases = (
                    [LARK_USER_REAUTHORIZATION_PHASE.projection()]
                    if user_only
                    else [
                        LARK_APP_CONFIGURATION_PHASE.projection(),
                        LARK_USER_CONSENT_PHASE.projection(),
                    ]
                )
                return ManagedCliServiceResult(
                    payload={
                        "ok": True,
                        "managed_by": "managed_cli",
                        "adapter_id": match.adapter_id,
                        "profile_id": profile_id,
                        "status": "completed",
                        "authorization_completed": True,
                        "completed_phases": completed_phases,
                        "identity": _safe_lark_identity_projection(identity),
                        "output": (
                            "飞书用户授权已验证，Credential Profile 已原子替换。"
                            if user_only
                            else "飞书应用配置和用户授权均已验证，Credential Profile 已原子更新。"
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
        executable = plan.toolchain_path / "bin" / "lark-cli"
        if not executable.exists():
            return self._error(
                "managed_cli_not_installed",
                "lark-cli is not installed in the shared Toolchain. Run npm install -g @larksuite/cli.",
            )
        if match.adapter_id == "lark-cli" and match.authorization_phase:
            return self._lark_authorization_provider(plan)
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
                if match.adapter_id == "lark-cli" and current is not None:
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
                    if match.adapter_id == "lark-cli":
                        active_authorization = self._authorization_flow_store(
                            store,
                            owner_user_id,
                        ).active(provider, profile_id)
                    if pending.browser_status == "completed" and pending.credential_state is not None:
                        if match.argv == ("lark-cli", "config", "init", "--new"):
                            result = pending
                    elif (
                        pending.browser_status == "awaiting_user_browser"
                        and active_authorization is not None
                    ):
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
                    elif (
                        pending.browser_status in {"failed", "missing"}
                        and active_authorization is None
                    ):
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
                # _lark_authorization_provider before reaching this branch.
                if active_authorization is not None and result is None:
                    if match.action == ManagedCliAction.CREDENTIAL_READ:
                        return self._profile_status_during_authorization_payload(
                            plan,
                            current or {},
                            active_authorization,
                        )
                    completed_phases = active_authorization.get("completed_phase_ids", [])
                    if (
                        active_authorization.get("phase_id") == LARK_APP_CONFIGURATION_PHASE.phase_id
                        and LARK_APP_CONFIGURATION_PHASE.phase_id in completed_phases
                    ):
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
                                "phase": LARK_APP_CONFIGURATION_PHASE.projection(),
                                "output": "飞书应用配置已验证，可以进入第 2/2 步用户授权。",
                                "next_action": (
                                    "Run exactly: lark-cli auth login --domain all --no-wait --json"
                                ),
                            },
                            exit_code=0,
                        )
                    return self._authorization_payload(
                        plan,
                        active_authorization,
                        output="飞书授权仍在等待当前浏览器步骤完成。",
                    )

                if result is None:
                    if match.argv == ("lark-cli", "config", "init", "--new"):
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
                        )

                confirmation = _lark_confirmation_required(result.output) if result.exit_code == 10 else None
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
                    action_argv = ["lark-cli", *action_tokens]
                    destructive = match.destructive or is_lark_destructive_argv(action_argv)
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
                        )
                        auto_confirmed = not destructive
                        confirmation = None
                if result.exit_code != 0 and match.action == ManagedCliAction.PROVIDER_OPERATION:
                    user_authorization_repair = _lark_user_credential_failure(result.output)
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
                    store.write_state(
                        provider,
                        profile_id,
                        result.credential_state,
                        credential_state=credential_state,
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
                    match.adapter_id == "lark-cli"
                    and match.requires_profile
                    and result.exit_code == 0
                    and lowered[:2] == ("auth", "status")
                    and "--verify" in lowered
                ):
                    verified_status = _lark_identity_status(result.output)
                    identities = (
                        verified_status.get("identities")
                        if isinstance(verified_status, dict)
                        and isinstance(verified_status.get("identities"), dict)
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
                            "active" if _lark_full_identity_ready(verified_status) else "authorization_required",
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
                if (
                    match.requires_profile
                    and result.exit_code != 0
                    and _MISSING_CLIENT_SECRET.search(result.output)
                ):
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
            consent_match = ManagedCliRegistry().match(
                "lark-cli auth login --domain all --no-wait --json"
            )
            assert consent_match is not None
            consent_plan = self.plan(
                consent_match,
                {
                    "credential_profile_id": profile_id,
                    "project_id": context.get("project_id"),
                },
            )
            started = self._start_lark_user_consent(consent_plan)
            if started.payload.get("status") == "awaiting_user_browser":
                started.payload["trigger"] = {
                    "reason": user_authorization_repair.reason,
                    "identity": "user",
                    "safe_to_retry": True,
                    "interrupted_action_sha256": hashlib.sha256(
                        "\0".join(match.argv).encode("utf-8")
                    ).hexdigest(),
                }
                started.payload["output"] = (
                    "原飞书操作因用户授权失效而暂停。Bot 身份保持可用；"
                    "请完成用户授权，成功后重试原操作。"
                )
            return started
        output = redact_managed_cli_output(result.output)
        awaiting = result.browser_status == "awaiting_user_browser" or (
            match.route == ManagedCliRoute.BROWSER_AUTH
            and match.argv[:2] == ("lark-cli", "auth")
            and result.exit_code == 0
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

        if result.browser_status == "completed" and result.credential_state is not None:
            flow_store = self._authorization_flow_store(store, owner_user_id)
            flow = flow_store.active(provider, profile_id)
            if flow is None or flow.get("phase_id") != LARK_APP_CONFIGURATION_PHASE.phase_id:
                raise RuntimeError("browser authorization flow identity is missing")
            flow_store.write_staged_state(flow, result.credential_state)
            if flow_store.read_staged_state(flow) != result.credential_state:
                raise RuntimeError("staged credential readback verification failed")
            flow_store.mark_phase_verified(provider, profile_id, LARK_APP_CONFIGURATION_PHASE.phase_id)
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
            if (
                flow is not None
                and flow.get("phase_id") == LARK_APP_CONFIGURATION_PHASE.phase_id
                and LARK_APP_CONFIGURATION_PHASE.phase_id
                not in flow.get("completed_phase_ids", [])
            ):
                flow_store.fail(
                    provider,
                    profile_id,
                    status=(
                        AuthorizationFlowStatus.EXPIRED.value
                        if result.browser_status == "missing"
                        else AuthorizationFlowStatus.FAILED.value
                    ),
                    error=(
                        "browser_job_missing"
                        if result.browser_status == "missing"
                        else "browser_job_failed"
                    ),
                )
            if result.browser_status == "failed":
                self.backend.finalize_managed_browser_auth_cli(
                    owner_user_id=owner_user_id,
                    provider=provider,
                    profile_id=profile_id,
                    browser_job_id=browser_job_id,
                )
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
        result = self.backend.collect_managed_browser_auth_cli(
            owner_user_id=owner_user_id,
            provider=provider,
            profile_id=profile_id,
            credential_state_spec=credential_state,
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
