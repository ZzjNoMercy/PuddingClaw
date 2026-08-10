"""Pure provider semantics for managed CLI authorization.

Drivers may build trusted argv and interpret bounded provider output. They do
not receive Profile, Vault, Flow, Toolchain, or filesystem handles; mutation
and compare-and-swap commits remain owned by :mod:`runtime_identity.service`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from runtime_identity.adapters import ManagedCliMatch, is_lark_destructive_argv
from runtime_identity.authorization import (
    LARK_APP_CONFIGURATION_PHASE,
    LARK_USER_CONSENT_PHASE,
    LARK_USER_REAUTHORIZATION_PHASE,
    AuthorizationFlowStatus,
    AuthorizationPhaseSpec,
)


@dataclass(frozen=True)
class ProviderAuthorizationFailure:
    reason: str
    flow_status: str
    retryable: bool
    identity: str | None = None
    provider_code: int | str | None = None
    message: str = ""


@dataclass(frozen=True)
class ContinuationInjectionSpec:
    """Trusted in-container secret injection contract.

    The secret value is supplied through stdin by the Service and never
    appears in host argv, logs, a Match, or an approval preview.
    """

    argument: str
    placement: str = "append"
    transport: str = "stdin"
    trailing_argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"--[a-z0-9][a-z0-9-]*", self.argument):
            raise ValueError("authorization continuation argument is invalid")
        if self.placement != "append" or self.transport != "stdin":
            raise ValueError("authorization continuation transport is unsupported")
        if any(not value or "\x00" in value for value in self.trailing_argv):
            raise ValueError("authorization continuation trailing argv is invalid")


class AuthorizationPhaseKind(StrEnum):
    BROWSER_CONFIGURATION = "browser_configuration"
    DEVICE_AUTHORIZATION = "device_authorization"


@dataclass(frozen=True)
class AuthorizationPhaseNode:
    phase: AuthorizationPhaseSpec
    kind: AuthorizationPhaseKind
    prerequisites: tuple[str, ...] = ()
    requires_prerequisite_identity: bool = False


@dataclass(frozen=True)
class AuthorizationPurposeSpec:
    purpose_id: str
    phase_ids: tuple[str, ...]
    entry_phase_id: str
    phase_overrides: tuple[AuthorizationPhaseSpec, ...] = ()
    preverified_phase_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorizationGraph:
    """Driver-owned phase graph consumed by the provider-neutral Coordinator."""

    phases: tuple[AuthorizationPhaseNode, ...]
    purposes: tuple[AuthorizationPurposeSpec, ...]
    default_purpose: str
    reauthorization_purpose_id: str | None = None

    def __post_init__(self) -> None:
        nodes = {node.phase.phase_id: node for node in self.phases}
        if not nodes or len(nodes) != len(self.phases):
            raise ValueError("authorization graph phase ids must be non-empty and unique")
        purpose_ids = {purpose.purpose_id for purpose in self.purposes}
        if len(purpose_ids) != len(self.purposes) or self.default_purpose not in purpose_ids:
            raise ValueError("authorization graph purposes are invalid")
        if self.reauthorization_purpose_id is not None and self.reauthorization_purpose_id not in purpose_ids:
            raise ValueError("authorization graph reauthorization purpose is invalid")
        for node in self.phases:
            if any(value not in nodes for value in node.prerequisites):
                raise ValueError("authorization phase prerequisite is unknown")
        for purpose in self.purposes:
            if (
                not purpose.phase_ids
                or purpose.entry_phase_id not in purpose.phase_ids
                or any(value not in nodes for value in purpose.phase_ids)
                or len(set(purpose.phase_ids)) != len(purpose.phase_ids)
            ):
                raise ValueError("authorization purpose phase path is invalid")
            if any(value not in nodes for value in purpose.preverified_phase_ids):
                raise ValueError("authorization purpose preverified phase is unknown")
            completed: set[str] = set(purpose.preverified_phase_ids)
            for phase_id in purpose.phase_ids:
                if not set(nodes[phase_id].prerequisites).issubset(completed):
                    raise ValueError("authorization purpose violates phase prerequisites")
                completed.add(phase_id)
            override_ids = {phase.phase_id for phase in purpose.phase_overrides}
            if len(override_ids) != len(purpose.phase_overrides) or not override_ids.issubset(purpose.phase_ids):
                raise ValueError("authorization purpose phase overrides are invalid")

    def node(self, phase_id: str) -> AuthorizationPhaseNode:
        for node in self.phases:
            if node.phase.phase_id == phase_id:
                return node
        raise ValueError("authorization phase is not declared by the Driver")

    def purpose(self, purpose_id: str) -> AuthorizationPurposeSpec:
        for purpose in self.purposes:
            if purpose.purpose_id == purpose_id:
                return purpose
        raise ValueError("authorization purpose is not declared by the Driver")

    def phase(self, phase_id: str, *, purpose_id: str | None = None) -> AuthorizationPhaseSpec:
        if purpose_id is not None:
            purpose = self.purpose(purpose_id)
            for override in purpose.phase_overrides:
                if override.phase_id == phase_id:
                    return override
        return self.node(phase_id).phase

    def purpose_for_entry(self, phase_id: str) -> AuthorizationPurposeSpec:
        candidates = [purpose for purpose in self.purposes if purpose.entry_phase_id == phase_id]
        if len(candidates) == 1:
            return candidates[0]
        for purpose in candidates:
            if purpose.purpose_id == self.default_purpose:
                return purpose
        raise ValueError("authorization phase does not have an unambiguous entry purpose")

    def completed_projections(self, purpose_id: str) -> list[dict[str, Any]]:
        purpose = self.purpose(purpose_id)
        return [self.phase(phase_id, purpose_id=purpose_id).projection() for phase_id in purpose.phase_ids]

    # Compatibility accessors keep existing Lark call sites stable while the
    # Coordinator itself routes exclusively through nodes and purpose paths.
    @property
    def full_purpose(self) -> str:
        return self.default_purpose

    @property
    def reauthorization_purpose(self) -> str:
        return self.reauthorization_purpose_id or ""

    @property
    def app_configuration(self) -> AuthorizationPhaseSpec:
        return next(
            node.phase for node in self.phases if node.kind == AuthorizationPhaseKind.BROWSER_CONFIGURATION
        )

    @property
    def user_consent(self) -> AuthorizationPhaseSpec:
        return next(node.phase for node in self.phases if node.kind == AuthorizationPhaseKind.DEVICE_AUTHORIZATION)

    @property
    def user_reauthorization(self) -> AuthorizationPhaseSpec:
        return self.phase(self.user_consent.phase_id, purpose_id=self.reauthorization_purpose)

    @property
    def fingerprint(self) -> str:
        def phase_contract(phase: AuthorizationPhaseSpec) -> dict[str, Any]:
            return {
                **phase.projection(),
                "completion_hint": phase.completion_hint,
                "recovery_evidence": phase.recovery_evidence.value,
                "missing_evidence_action": phase.missing_evidence_action.value,
            }

        value = {
            "full_purpose": self.full_purpose,
            "reauthorization_purpose": self.reauthorization_purpose,
            "phases": [
                {
                    "phase": phase_contract(node.phase),
                    "kind": node.kind.value,
                    "prerequisites": list(node.prerequisites),
                    "requires_prerequisite_identity": node.requires_prerequisite_identity,
                }
                for node in self.phases
            ],
            "purposes": [
                {
                    "purpose_id": purpose.purpose_id,
                    "phase_ids": list(purpose.phase_ids),
                    "entry_phase_id": purpose.entry_phase_id,
                    "phase_overrides": [phase_contract(phase) for phase in purpose.phase_overrides],
                    "preverified_phase_ids": list(purpose.preverified_phase_ids),
                }
                for purpose in self.purposes
            ],
        }
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@runtime_checkable
class AuthorizationDriver(Protocol):
    adapter_id: str
    provider: str
    contract_fingerprint: str
    continuation: ContinuationInjectionSpec
    graph: AuthorizationGraph
    display_name: str
    app_configuration_argv: tuple[str, ...]
    identity_status_argv: tuple[str, ...]
    logout_argv: tuple[str, ...]
    user_login_argv: tuple[str, ...]
    continuation_argv: tuple[str, ...]
    resume_argv: tuple[str, ...]
    revoke_argv: tuple[str, ...]

    def handles(self, match: ManagedCliMatch) -> bool: ...

    def identity_status(self, output: str) -> dict[str, Any] | None: ...

    def device_authorization(self, output: str) -> dict[str, Any] | None: ...

    def validated_authorization_url(self, raw_url: str, *, phase_id: str) -> str | None: ...

    def config_authorization_url(self, output: str) -> str | None: ...

    def bot_ready(self, status: dict[str, Any] | None) -> bool: ...

    def full_identity_ready(self, status: dict[str, Any] | None) -> bool: ...

    def safe_identity_projection(self, status: dict[str, Any] | None) -> dict[str, Any]: ...

    def authorization_failure(self, output: str) -> ProviderAuthorizationFailure: ...

    def user_credential_failure(self, output: str) -> ProviderAuthorizationFailure | None: ...

    def successful_operation_identity_updates(
        self,
        output: str,
    ) -> tuple[tuple[str, str, bool, str], ...]: ...

    def confirmation_required(self, output: str) -> dict[str, Any] | None: ...

    def destructive_argv(self, argv: tuple[str, ...] | list[str]) -> bool: ...

    def qrcode_argv(self, verification_url: str) -> tuple[str, ...] | None: ...

    def profile_identity_updates(self, status: dict[str, Any] | None) -> tuple[tuple[str, str, bool, str], ...]: ...

    def durable_profile_ready(self, identities: dict[str, Any]) -> bool: ...


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


def _identity_ready(identity: Any, *, require_token: bool) -> bool:
    if not isinstance(identity, dict):
        return False
    ready = str(identity.get("status") or "").lower() in {
        "ready",
        "active",
        "configured",
        "authenticated",
    }
    if not (ready and identity.get("verified") is True):
        return False
    if not require_token:
        return True
    return str(identity.get("tokenStatus") or identity.get("token_status") or "").lower() in {
        "valid",
        "active",
    }


class LarkAuthorizationDriver:
    """Deterministic Lark provider semantics with no state mutation access."""

    adapter_id = "lark-cli"
    provider = "lark"
    display_name = "飞书"
    graph = AuthorizationGraph(
        phases=(
            AuthorizationPhaseNode(
                phase=LARK_APP_CONFIGURATION_PHASE,
                kind=AuthorizationPhaseKind.BROWSER_CONFIGURATION,
            ),
            AuthorizationPhaseNode(
                phase=LARK_USER_CONSENT_PHASE,
                kind=AuthorizationPhaseKind.DEVICE_AUTHORIZATION,
                prerequisites=(LARK_APP_CONFIGURATION_PHASE.phase_id,),
                requires_prerequisite_identity=True,
            ),
        ),
        purposes=(
            AuthorizationPurposeSpec(
                purpose_id="lark_full_authorization",
                phase_ids=(LARK_APP_CONFIGURATION_PHASE.phase_id, LARK_USER_CONSENT_PHASE.phase_id),
                entry_phase_id=LARK_APP_CONFIGURATION_PHASE.phase_id,
            ),
            AuthorizationPurposeSpec(
                purpose_id="lark_user_reauthorization",
                phase_ids=(LARK_USER_CONSENT_PHASE.phase_id,),
                entry_phase_id=LARK_USER_CONSENT_PHASE.phase_id,
                phase_overrides=(LARK_USER_REAUTHORIZATION_PHASE,),
                preverified_phase_ids=(LARK_APP_CONFIGURATION_PHASE.phase_id,),
            ),
        ),
        default_purpose="lark_full_authorization",
        reauthorization_purpose_id="lark_user_reauthorization",
    )
    continuation = ContinuationInjectionSpec(argument="--device-code", trailing_argv=("--json",))
    app_configuration_argv = ("lark-cli", "config", "init", "--new")
    identity_status_argv = ("lark-cli", "auth", "status", "--json", "--verify")
    logout_argv = ("lark-cli", "auth", "logout", "--json")
    user_login_argv = ("lark-cli", "auth", "login", "--domain", "all", "--no-wait", "--json")
    continuation_argv = ("lark-cli", "auth", "login")
    resume_argv = ("lark-cli", "auth", "resume")
    revoke_argv = ("lark-cli", "config", "remove")
    contract_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "adapter_id": adapter_id,
                "provider": provider,
                "app_configuration_argv": app_configuration_argv,
                "identity_status_argv": identity_status_argv,
                "logout_argv": logout_argv,
                "user_login_argv": user_login_argv,
                "continuation_argv": continuation_argv,
                "resume_argv": resume_argv,
                "revoke_argv": revoke_argv,
                "continuation": {
                    "argument": continuation.argument,
                    "placement": continuation.placement,
                    "transport": continuation.transport,
                    "trailing_argv": continuation.trailing_argv,
                },
                "authorization_graph": graph.fingerprint,
                "url_contract": 1,
                "identity_contract": 1,
                "failure_contract": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    def handles(self, match: ManagedCliMatch) -> bool:
        return match.adapter_id == self.adapter_id and match.provider in {None, self.provider}

    def identity_status(self, output: str) -> dict[str, Any] | None:
        objects = _json_objects(output)
        if any(value.get("ok") is False or "error" in value for value in objects):
            return None
        candidates = [value for value in objects if isinstance(value.get("identities"), dict)]
        canonical = {json.dumps(value, sort_keys=True, separators=(",", ":")) for value in candidates}
        return candidates[-1] if candidates and len(canonical) == 1 else None

    def device_authorization(self, output: str) -> dict[str, Any] | None:
        objects = _json_objects(output)
        if any(value.get("ok") is False or "error" in value for value in objects):
            return None
        candidates = [
            value
            for value in objects
            if isinstance(value.get("device_code"), str)
            and isinstance(value.get("verification_url"), str)
            and value.get("device_code")
            and value.get("verification_url")
        ]
        canonical = {json.dumps(value, sort_keys=True, separators=(",", ":")) for value in candidates}
        return candidates[-1] if candidates and len(canonical) == 1 else None

    def validated_authorization_url(self, raw_url: str, *, phase_id: str) -> str | None:
        candidate = str(raw_url or "").strip().strip("\"'")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
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
                and query_keys <= {"flow_id", "user_code"}
            )
        return candidate if allowed else None

    def config_authorization_url(self, output: str) -> str | None:
        for candidate in re.findall(r"https://[^\s\"'<>]+", str(output or "")):
            validated = self.validated_authorization_url(candidate, phase_id="app_configuration")
            if validated:
                return validated
        return None

    def bot_ready(self, status: dict[str, Any] | None) -> bool:
        identities = status.get("identities") if isinstance(status, dict) else None
        bot = identities.get("bot") if isinstance(identities, dict) else None
        return _identity_ready(bot, require_token=False)

    def full_identity_ready(self, status: dict[str, Any] | None) -> bool:
        identities = status.get("identities") if isinstance(status, dict) else None
        return (
            isinstance(identities, dict)
            and _identity_ready(identities.get("bot"), require_token=False)
            and _identity_ready(identities.get("user"), require_token=True)
        )

    def safe_identity_projection(self, status: dict[str, Any] | None) -> dict[str, Any]:
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

    def authorization_failure(self, output: str) -> ProviderAuthorizationFailure:
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
        if "authorization_pending" in normalized or "authorization pending" in normalized:
            return ProviderAuthorizationFailure(
                "authorization_pending",
                AuthorizationFlowStatus.AWAITING_USER.value,
                True,
                identity,
                provider_code,
                message,
            )
        if "slow_down" in normalized or "slow down" in normalized:
            return ProviderAuthorizationFailure(
                "provider_slow_down",
                AuthorizationFlowStatus.AWAITING_USER.value,
                True,
                identity,
                provider_code,
                message,
            )
        if any(marker in normalized for marker in ("access_denied", "authorization denied", "user denied")):
            return ProviderAuthorizationFailure(
                "access_denied", AuthorizationFlowStatus.CANCELLED.value, False, identity, provider_code, message
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
            return ProviderAuthorizationFailure(
                "authorization_expired", AuthorizationFlowStatus.EXPIRED.value, False, identity, provider_code, message
            )
        if (
            (error_type == "validation" and subtype == "invalid_argument")
            or str(provider_code) == "20001"
            or any(marker in normalized for marker in ("invalid_request", "invalid request", "请求不合法"))
        ):
            return ProviderAuthorizationFailure(
                "provider_invalid_request",
                AuthorizationFlowStatus.FAILED.value,
                False,
                identity,
                provider_code,
                message,
            )
        provider_status = int(provider_code) if str(provider_code).isdigit() else None
        retryable = (
            any(
                marker in normalized
                for marker in ("timeout", "timed out", "econnreset", "connection reset", "dns", "tls", "rate limit")
            )
            or provider_status == 429
            or (provider_status is not None and 500 <= provider_status <= 599)
        )
        return ProviderAuthorizationFailure(
            "provider_retryable_error" if retryable else "provider_authorization_error",
            AuthorizationFlowStatus.FAILED.value,
            True,
            identity,
            provider_code,
            message,
        )

    def user_credential_failure(self, output: str) -> ProviderAuthorizationFailure | None:
        for value in reversed(_json_objects(output)):
            error = value.get("error")
            if not isinstance(error, dict) or str(value.get("identity") or "").lower() != "user":
                continue
            error_type = str(error.get("type") or "").lower()
            normalized = " ".join(
                (
                    error_type,
                    str(error.get("subtype") or "").lower(),
                    str(error.get("message") or "").lower(),
                    str(error.get("hint") or "").lower(),
                    str(error.get("code") or "").lower(),
                )
            )
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
                return ProviderAuthorizationFailure(
                    "user_token_expired",
                    AuthorizationFlowStatus.FAILED.value,
                    False,
                    "user",
                    error.get("code"),
                    str(error.get("message") or ""),
                )
        return None

    def successful_operation_identity_updates(
        self,
        output: str,
    ) -> tuple[tuple[str, str, bool, str], ...]:
        """Project a successful API envelope into durable identity health.

        A user-scoped provider operation is also direct evidence that the CLI
        acquired a usable UAT.  In particular, lark-cli may transparently
        refresh an expired access token while executing the operation.  The
        refreshed credential archive is committed by the Service; this
        projection keeps the non-secret Profile assessment in sync with it.
        """

        objects = _json_objects(output)
        if any(value.get("ok") is False or "error" in value for value in objects):
            return ()
        identities = {
            str(value.get("identity") or "").lower()
            for value in objects
            if value.get("ok") is True and isinstance(value.get("identity"), str)
        }
        if identities == {"user"}:
            return (("user", "active", True, "valid"),)
        if identities == {"bot"}:
            return (("bot", "ready", True, ""),)
        return ()

    def confirmation_required(self, output: str) -> dict[str, Any] | None:
        for value in _json_objects(output):
            error = value.get("error")
            if (
                isinstance(error, dict)
                and value.get("ok") is False
                and error.get("type") == "confirmation"
                and error.get("subtype") == "confirmation_required"
                and error.get("risk") == "high-risk-write"
                and isinstance(error.get("action"), str)
                and str(error.get("action")).strip()
            ):
                return error
        return None

    def destructive_argv(self, argv: tuple[str, ...] | list[str]) -> bool:
        return is_lark_destructive_argv(argv)

    def qrcode_argv(self, verification_url: str) -> tuple[str, ...] | None:
        return ("lark-cli", "auth", "qrcode", verification_url, "--ascii")

    def profile_identity_updates(self, status: dict[str, Any] | None) -> tuple[tuple[str, str, bool, str], ...]:
        if not self.full_identity_ready(status):
            return ()
        return (("bot", "ready", True, ""), ("user", "active", True, "valid"))

    def durable_profile_ready(self, identities: dict[str, Any]) -> bool:
        bot = identities.get("bot") if isinstance(identities.get("bot"), dict) else {}
        user = identities.get("user") if isinstance(identities.get("user"), dict) else {}
        return bool(
            bot.get("status") in {"ready", "active"}
            and bot.get("verified") is True
            and user.get("status") in {"ready", "active"}
            and user.get("verified") is True
            and user.get("token_status") == "valid"
        )


class AuthorizationDriverRegistry:
    """Frozen process-owned registry; Skills cannot register Drivers."""

    def __init__(self, drivers: tuple[AuthorizationDriver, ...] | None = None) -> None:
        configured = (LarkAuthorizationDriver(),) if drivers is None else drivers
        adapter_ids = [driver.adapter_id for driver in configured]
        providers = [driver.provider for driver in configured]
        if len(set(adapter_ids)) != len(adapter_ids) or len(set(providers)) != len(providers):
            raise ValueError("authorization Driver ids and providers must be unique")
        self._by_adapter = {driver.adapter_id: driver for driver in configured}
        self._by_provider = {driver.provider: driver for driver in configured}

    def for_adapter(self, adapter_id: str, *, required: bool = True) -> AuthorizationDriver | None:
        driver = self._by_adapter.get(adapter_id)
        if driver is None and required:
            raise ValueError("managed CLI Adapter has no authorization Driver")
        return driver

    def for_provider(self, provider: str, *, required: bool = True) -> AuthorizationDriver | None:
        driver = self._by_provider.get(provider)
        if driver is None and required:
            raise ValueError("managed CLI provider has no authorization Driver")
        return driver

    def drivers(self) -> tuple[AuthorizationDriver, ...]:
        return tuple(self._by_adapter.values())
