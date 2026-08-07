"""Durable, provider-neutral authorization-flow state.

The model and frontend may see the public projection of a flow. Provider
continuation material (device codes, PKCE verifiers, temporary registration
tokens) is encrypted at rest and is never returned from this module's public
projection helpers.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from filelock import FileLock

from runtime_identity.paths import PuddingClawPaths, safe_identity_component
from runtime_identity.profiles import CredentialVault, MasterKeyProvider, _atomic_write


class AuthorizationFlowStatus(StrEnum):
    STARTING = "starting"
    AWAITING_USER = "awaiting_user"
    COLLECTING = "collecting"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AuthorizationRecoveryEvidence(StrEnum):
    RUNNER_LEASE = "runner_lease"
    STAGING_AND_CONTINUATION = "staging_and_continuation"


class AuthorizationMissingEvidenceAction(StrEnum):
    EXPIRE_FLOW = "expire_flow"
    RESET_ATTEMPT = "reset_attempt"


_TERMINAL_FLOW_STATUSES = {
    AuthorizationFlowStatus.COMPLETED.value,
    AuthorizationFlowStatus.FAILED.value,
    AuthorizationFlowStatus.EXPIRED.value,
    AuthorizationFlowStatus.CANCELLED.value,
}


@dataclass(frozen=True)
class AuthorizationPhaseSpec:
    phase_id: str
    step: int
    total: int
    title: str
    description: str
    completion_hint: str
    recovery_evidence: AuthorizationRecoveryEvidence
    missing_evidence_action: AuthorizationMissingEvidenceAction

    def __post_init__(self) -> None:
        safe_identity_component(self.phase_id, field="phase_id")
        if self.step < 1 or self.total < self.step:
            raise ValueError("authorization phase step is invalid")
        if not self.title.strip() or not self.description.strip() or not self.completion_hint.strip():
            raise ValueError("authorization phase copy must be non-empty")

    def projection(self) -> dict[str, Any]:
        return {
            "id": self.phase_id,
            "step": self.step,
            "total": self.total,
            "title": self.title,
            "description": self.description,
        }


LARK_APP_CONFIGURATION_PHASE = AuthorizationPhaseSpec(
    phase_id="app_configuration",
    step=1,
    total=2,
    title="创建或绑定飞书应用",
    description="选择或创建 CLI Bot，并安全保存应用凭证。",
    completion_hint="完成后告诉我，我会验证应用配置并进入下一步。",
    recovery_evidence=AuthorizationRecoveryEvidence.RUNNER_LEASE,
    missing_evidence_action=AuthorizationMissingEvidenceAction.EXPIRE_FLOW,
)

LARK_USER_CONSENT_PHASE = AuthorizationPhaseSpec(
    phase_id="user_consent",
    step=2,
    total=2,
    title="授权应用访问你的飞书数据",
    description="这是用户身份授权，与上一步的应用配置不同。",
    completion_hint="完成后告诉我，我会验证最终授权状态。",
    recovery_evidence=AuthorizationRecoveryEvidence.STAGING_AND_CONTINUATION,
    missing_evidence_action=AuthorizationMissingEvidenceAction.RESET_ATTEMPT,
)

LARK_USER_REAUTHORIZATION_PHASE = AuthorizationPhaseSpec(
    phase_id="user_consent",
    step=1,
    total=1,
    title="重新授权访问你的飞书数据",
    description="保留现有应用/Bot 配置，只替换用户身份授权。",
    completion_hint="完成后告诉我，我会验证新授权并原子替换原连接。",
    recovery_evidence=AuthorizationRecoveryEvidence.STAGING_AND_CONTINUATION,
    missing_evidence_action=AuthorizationMissingEvidenceAction.RESET_ATTEMPT,
)


class AuthorizationFlowStore:
    """Store public flow metadata separately from encrypted continuation state."""

    def __init__(
        self,
        paths: PuddingClawPaths,
        owner_user_id: str,
        *,
        vault: CredentialVault | None = None,
    ) -> None:
        self.paths = paths
        self.owner_user_id = safe_identity_component(owner_user_id, field="owner_user_id")
        self.root = paths.credentials_root(self.owner_user_id) / "authorization-flows"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.registry_path = self.root / "flows.json"
        self._lock = FileLock(str(self.root / ".flows.lock"), thread_local=False)
        self._vault = vault

    @property
    def vault(self) -> CredentialVault:
        if self._vault is None:
            self._vault = CredentialVault(MasterKeyProvider(self.paths, self.owner_user_id).get_or_create())
        return self._vault

    def active(self, provider: str, profile_id: str) -> dict[str, Any] | None:
        provider = safe_identity_component(provider, field="provider")
        profile_id = safe_identity_component(profile_id, field="profile_id")
        with self._lock:
            registry = self._read_registry()
            candidates = [
                item
                for item in registry["flows"]
                if isinstance(item, dict)
                and item.get("owner_user_id") == self.owner_user_id
                and item.get("provider") == provider
                and item.get("profile_id") == profile_id
                and item.get("status") not in _TERMINAL_FLOW_STATUSES
            ]
            if not candidates:
                return None
            return dict(max(candidates, key=lambda item: float(item.get("updated_at") or 0)))

    def active_flows(self) -> list[dict[str, Any]]:
        """Return every non-terminal Flow owned by this user.

        Startup reconciliation needs a complete durable inventory; deriving it
        from currently running jobs would make an orphaned Flow invisible.
        Secret continuation and staged credential bytes remain in their
        separate encrypted files and are never included here.
        """

        with self._lock:
            registry = self._read_registry()
            return [
                dict(item)
                for item in registry["flows"]
                if isinstance(item, dict)
                and item.get("owner_user_id") == self.owner_user_id
                and item.get("status") not in _TERMINAL_FLOW_STATUSES
            ]

    def has_continuation(self, flow: dict[str, Any]) -> bool:
        """Whether the current phase/attempt has resumable encrypted state."""

        self._validate_flow_identity(flow)
        phase_id = safe_identity_component(str(flow.get("phase_id") or ""), field="phase_id")
        attempt = max(1, int(flow.get("attempt") or 0))
        return self._secret_path(str(flow["flow_id"]), phase_id, attempt).exists() or self._legacy_secret_path(
            str(flow["flow_id"])
        ).exists()

    def has_staged_state(self, flow: dict[str, Any]) -> bool:
        """Whether a Flow still owns its provider-native staging archive."""

        self._validate_flow_identity(flow)
        return self._state_path(str(flow["flow_id"])).exists()

    def has_candidate_state(self, flow: dict[str, Any]) -> bool:
        """Whether the current attempt has an unverified candidate archive."""

        self._validate_flow_identity(flow)
        attempt = max(1, int(flow.get("attempt") or 0))
        return self._candidate_state_path(str(flow["flow_id"]), attempt).exists()

    def reconcile_recovery(
        self,
        provider: str,
        profile_id: str,
        *,
        runner_lease_present: bool,
    ) -> dict[str, Any] | None:
        """Repair or terminalize one Flow from its declared evidence contract.

        The core understands only generic evidence types. Provider Drivers
        choose the evidence and missing-evidence action when defining a phase;
        adding another Provider therefore does not add a branch here.
        """

        active = self.active(provider, profile_id)
        if active is None:
            return None
        status = str(active.get("status") or "")
        completed = [str(value) for value in active.get("completed_phase_ids", [])]
        evidence = str(active.get("recovery_evidence") or "")
        missing_action = str(active.get("missing_evidence_action") or "")

        # Once a phase has verified staging, STARTING means that the next phase
        # can be issued. VERIFYING is commit recovery. Neither still depends on
        # the old Runner or continuation.
        staging_only = status == AuthorizationFlowStatus.VERIFYING.value or (
            status == AuthorizationFlowStatus.STARTING.value and bool(completed)
        )
        if staging_only:
            if self.has_staged_state(active):
                return active
            self.fail(
                provider,
                profile_id,
                status=AuthorizationFlowStatus.FAILED.value,
                error="authorization_staged_state_missing",
            )
            return None

        if evidence == AuthorizationRecoveryEvidence.RUNNER_LEASE.value:
            if runner_lease_present:
                return active
            self.fail(
                provider,
                profile_id,
                status=(
                    AuthorizationFlowStatus.EXPIRED.value
                    if missing_action == AuthorizationMissingEvidenceAction.EXPIRE_FLOW.value
                    else AuthorizationFlowStatus.FAILED.value
                ),
                error="browser_job_missing",
            )
            return None

        if evidence == AuthorizationRecoveryEvidence.STAGING_AND_CONTINUATION.value:
            if not self.has_staged_state(active):
                self.fail(
                    provider,
                    profile_id,
                    status=AuthorizationFlowStatus.FAILED.value,
                    error="authorization_staged_state_missing",
                )
                return None
            if status not in {
                AuthorizationFlowStatus.AWAITING_USER.value,
                AuthorizationFlowStatus.COLLECTING.value,
            }:
                return active
            if self.has_continuation(active) or self.has_candidate_state(active):
                return active
            if missing_action == AuthorizationMissingEvidenceAction.RESET_ATTEMPT.value:
                return self.reset_user_attempt(
                    provider,
                    profile_id,
                    error="authorization_continuation_missing",
                    diagnostic={
                        "reason": "authorization_continuation_missing",
                        "exit_code": 1,
                        "candidate_state_exported": False,
                        "candidate_identity_verified": False,
                    },
                )
            self.fail(
                provider,
                profile_id,
                status=AuthorizationFlowStatus.EXPIRED.value,
                error="authorization_continuation_missing",
            )
            return None

        # Old or corrupt non-terminal records have no provable recovery
        # contract. Fail closed rather than letting an inert public card own a
        # Profile forever.
        self.fail(
            provider,
            profile_id,
            status=AuthorizationFlowStatus.FAILED.value,
            error="authorization_recovery_contract_missing",
        )
        return None

    def begin_or_advance(
        self,
        *,
        provider: str,
        adapter_id: str,
        profile_id: str,
        purpose: str,
        phase: AuthorizationPhaseSpec,
        profile_revision: float,
        base_state_revision: str,
        adapter_contract_fingerprint: str,
        public: dict[str, Any],
        secret: dict[str, Any] | None,
        expires_at: float | None,
        new_attempt: bool = False,
    ) -> dict[str, Any]:
        provider = safe_identity_component(provider, field="provider")
        adapter_id = safe_identity_component(adapter_id, field="adapter_id")
        profile_id = safe_identity_component(profile_id, field="profile_id")
        purpose = safe_identity_component(purpose, field="purpose")
        now = time.time()
        with self._lock:
            registry = self._read_registry()
            active = next(
                (
                    item
                    for item in registry["flows"]
                    if isinstance(item, dict)
                    and item.get("owner_user_id") == self.owner_user_id
                    and item.get("provider") == provider
                    and item.get("profile_id") == profile_id
                    and item.get("status") not in _TERMINAL_FLOW_STATUSES
                ),
                None,
            )
            if active is None:
                active = {
                    "flow_id": f"auth-{secrets.token_hex(12)}",
                    "owner_user_id": self.owner_user_id,
                    "provider": provider,
                    "adapter_id": adapter_id,
                    "profile_id": profile_id,
                    "purpose": purpose,
                    "completed_phase_ids": [],
                    "revision": 0,
                    "attempt": 0,
                    "created_at": now,
                }
                registry["flows"].append(active)
            elif active.get("adapter_id") != adapter_id:
                raise ValueError("authorization flow Adapter changed")
            elif active.get("purpose") != purpose:
                raise ValueError("authorization flow purpose changed")
            elif active.get("adapter_contract_fingerprint") not in {
                None,
                adapter_contract_fingerprint,
            }:
                raise ValueError("authorization flow Adapter contract changed")
            elif active.get("base_state_revision") not in {None, str(base_state_revision)}:
                raise ValueError("authorization flow base Profile state changed")
            previous_phase = active.get("phase_id")
            active.update(
                {
                    "status": AuthorizationFlowStatus.AWAITING_USER.value,
                    "phase_id": phase.phase_id,
                    "phase": phase.projection(),
                    "completion_hint": phase.completion_hint,
                    "profile_revision": float(profile_revision),
                    "base_state_revision": str(base_state_revision),
                    "adapter_contract_fingerprint": adapter_contract_fingerprint,
                    "recovery_evidence": phase.recovery_evidence.value,
                    "missing_evidence_action": phase.missing_evidence_action.value,
                    "public": self._safe_public(public),
                    "expires_at": float(expires_at) if expires_at is not None else None,
                    "updated_at": now,
                    "revision": int(active.get("revision") or 0) + 1,
                    "attempt": (
                        1
                        if previous_phase != phase.phase_id
                        else max(1, int(active.get("attempt") or 0)) + (1 if new_attempt else 0)
                    ),
                }
            )
            if new_attempt:
                active.pop("retry_user_consent", None)
                active.pop("candidate_origin", None)
                # A fresh browser attempt must not inherit the previous
                # attempt's failure badge/diagnostic. The historical attempt
                # remains auditable in prior Session output, while the active
                # card reflects only the new URL and continuation.
                active.pop("error", None)
                active.pop("last_user_attempt", None)
            flow = dict(active)
            if secret is not None:
                # Persist secret continuation before publishing the public
                # awaiting state. A crash may leave an unreferenced encrypted
                # blob, but can never expose a resumable flow with no secret.
                self._write_secret(flow, secret)
            self._write_registry(registry)
            return flow

    def cancel_active(
        self,
        provider: str,
        profile_id: str,
        *,
        reason: str,
    ) -> dict[str, Any] | None:
        """Cancel one active Flow and remove all continuation/staging state."""

        return self._update_active(
            provider,
            profile_id,
            status=AuthorizationFlowStatus.CANCELLED.value,
            error=reason,
            clear_public=False,
            remove_continuation=True,
            remove_staged=True,
            remove_candidate=True,
            extra={"cancel_reason": str(reason)[:256]},
        )

    def mark_staging_user_cleared(self, provider: str, profile_id: str) -> dict[str, Any] | None:
        """Record that the staging copy no longer contains the old User login."""

        active = self.active(provider, profile_id)
        if active is None:
            return None
        return self._update_active(
            provider,
            profile_id,
            status=str(active.get("status") or AuthorizationFlowStatus.STARTING.value),
            clear_public=False,
            remove_continuation=False,
            remove_staged=False,
            extra={"staged_user_cleared": True},
        )

    def read_secret(self, flow: dict[str, Any]) -> dict[str, Any]:
        self._validate_flow_identity(flow)
        phase_id = safe_identity_component(str(flow.get("phase_id") or ""), field="phase_id")
        attempt = max(1, int(flow.get("attempt") or 0))
        path = self._secret_path(str(flow["flow_id"]), phase_id, attempt)
        legacy = False
        try:
            envelope = path.read_bytes()
        except FileNotFoundError as exc:
            # Read-only compatibility for flows created before continuation
            # files were bound to phase+attempt. New writes never use this
            # ambiguous legacy path.
            try:
                envelope = self._legacy_secret_path(str(flow["flow_id"])).read_bytes()
                legacy = True
            except FileNotFoundError:
                raise ValueError("authorization flow continuation state is missing") from exc
        plaintext = self.vault.open_flow(
            envelope,
            owner_user_id=self.owner_user_id,
            provider=str(flow["provider"]),
            profile_id=str(flow["profile_id"]),
            flow_id=(str(flow["flow_id"]) if legacy else f"{flow['flow_id']}.{phase_id}.{attempt}"),
        )
        value = json.loads(plaintext.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("authorization flow continuation state is invalid")
        return value

    def write_staged_state(self, flow: dict[str, Any], payload: bytes) -> None:
        """Persist provider-native staged state without touching the Profile Vault."""

        self._validate_flow_identity(flow)
        envelope = self.vault.seal_flow(
            payload,
            owner_user_id=self.owner_user_id,
            provider=str(flow["provider"]),
            profile_id=str(flow["profile_id"]),
            flow_id=f"{flow['flow_id']}.state",
        )
        _atomic_write(self._state_path(str(flow["flow_id"])), envelope)

    def write_candidate_state(self, flow: dict[str, Any], payload: bytes) -> None:
        """Persist an unverified provider result separately from step 1."""

        self._validate_flow_identity(flow)
        attempt = max(1, int(flow.get("attempt") or 0))
        envelope = self.vault.seal_flow(
            payload,
            owner_user_id=self.owner_user_id,
            provider=str(flow["provider"]),
            profile_id=str(flow["profile_id"]),
            flow_id=f"{flow['flow_id']}.candidate.{attempt}",
        )
        _atomic_write(self._candidate_state_path(str(flow["flow_id"]), attempt), envelope)

    def read_candidate_state(self, flow: dict[str, Any]) -> bytes | None:
        self._validate_flow_identity(flow)
        attempt = max(1, int(flow.get("attempt") or 0))
        try:
            envelope = self._candidate_state_path(str(flow["flow_id"]), attempt).read_bytes()
        except FileNotFoundError:
            return None
        return self.vault.open_flow(
            envelope,
            owner_user_id=self.owner_user_id,
            provider=str(flow["provider"]),
            profile_id=str(flow["profile_id"]),
            flow_id=f"{flow['flow_id']}.candidate.{attempt}",
        )

    def remove_candidate_state(self, flow: dict[str, Any]) -> None:
        self._validate_flow_identity(flow)
        attempt = max(1, int(flow.get("attempt") or 0))
        try:
            self._candidate_state_path(str(flow["flow_id"]), attempt).unlink()
        except FileNotFoundError:
            pass

    def read_staged_state(self, flow: dict[str, Any]) -> bytes:
        self._validate_flow_identity(flow)
        try:
            envelope = self._state_path(str(flow["flow_id"])).read_bytes()
        except FileNotFoundError as exc:
            raise ValueError("authorization flow staged credential state is missing") from exc
        return self.vault.open_flow(
            envelope,
            owner_user_id=self.owner_user_id,
            provider=str(flow["provider"]),
            profile_id=str(flow["profile_id"]),
            flow_id=f"{flow['flow_id']}.state",
        )

    def mark_phase_verified(self, provider: str, profile_id: str, phase_id: str) -> dict[str, Any] | None:
        return self._update_active(
            provider,
            profile_id,
            status=AuthorizationFlowStatus.STARTING.value,
            completed_phase_id=phase_id,
            clear_public=True,
            remove_continuation=True,
            remove_staged=False,
        )

    def complete(self, provider: str, profile_id: str, phase_id: str) -> dict[str, Any] | None:
        return self._update_active(
            provider,
            profile_id,
            status=AuthorizationFlowStatus.COMPLETED.value,
            completed_phase_id=phase_id,
            clear_public=True,
            remove_continuation=True,
            remove_staged=True,
            remove_candidate=True,
        )

    def mark_verifying(self, provider: str, profile_id: str, *, staged_sha256: str) -> dict[str, Any] | None:
        if not isinstance(staged_sha256, str) or len(staged_sha256) != 64:
            raise ValueError("authorization staged-state digest is invalid")
        return self._update_active(
            provider,
            profile_id,
            status=AuthorizationFlowStatus.VERIFYING.value,
            clear_public=True,
            remove_continuation=False,
            remove_staged=False,
            extra={"commit_staged_sha256": staged_sha256},
        )

    def fail(self, provider: str, profile_id: str, *, status: str, error: str) -> dict[str, Any] | None:
        if status not in {
            AuthorizationFlowStatus.FAILED.value,
            AuthorizationFlowStatus.EXPIRED.value,
            AuthorizationFlowStatus.CANCELLED.value,
        }:
            raise ValueError("authorization flow terminal status is invalid")
        return self._update_active(
            provider,
            profile_id,
            status=status,
            error=error,
            clear_public=False,
            remove_continuation=True,
            remove_staged=True,
            remove_candidate=True,
        )

    def record_retryable_user_error(
        self,
        provider: str,
        profile_id: str,
        *,
        error: str,
        diagnostic: dict[str, Any],
        collecting_candidate: bool = False,
        candidate_origin: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Keep one device continuation alive after an unproven failure.

        Unknown provider/runner failures are not evidence that the browser
        link expired or that the user denied consent. Preserve both the
        continuation and the verified App/Bot staging state so a later resume
        can verify the same attempt without asking the user to repeat step 1.
        """

        extra: dict[str, Any] = {"last_user_attempt": dict(diagnostic)}
        if candidate_origin is not None:
            extra["candidate_origin"] = dict(candidate_origin)
        return self._update_active(
            provider,
            profile_id,
            status=(
                AuthorizationFlowStatus.COLLECTING.value
                if collecting_candidate
                else AuthorizationFlowStatus.AWAITING_USER.value
            ),
            error=error,
            clear_public=False,
            remove_continuation=False,
            remove_staged=False,
            extra=extra,
        )

    def reset_user_attempt(
        self,
        provider: str,
        profile_id: str,
        *,
        error: str,
        diagnostic: dict[str, Any],
    ) -> dict[str, Any] | None:
        """End only the current user-consent attempt, preserving step 1.

        The next explicit auth-login command issues a fresh device code from
        the same verified staging archive. Durable Profile bytes remain
        untouched until the normal verify-and-CAS commit path succeeds.
        """

        return self._update_active(
            provider,
            profile_id,
            status=AuthorizationFlowStatus.STARTING.value,
            error=error,
            clear_public=True,
            remove_continuation=True,
            remove_staged=False,
            remove_candidate=True,
            extra={
                "last_user_attempt": dict(diagnostic),
                "retry_user_consent": True,
            },
        )

    @staticmethod
    def projection(flow: dict[str, Any]) -> dict[str, Any]:
        phase = flow.get("phase") if isinstance(flow.get("phase"), dict) else {}
        public = flow.get("public") if isinstance(flow.get("public"), dict) else {}
        return {
            "type": "managed_authorization_request",
            "flow_id": flow.get("flow_id"),
            "revision": flow.get("revision"),
            "attempt": flow.get("attempt"),
            "provider": flow.get("provider"),
            "profile_id": flow.get("profile_id"),
            "purpose": flow.get("purpose"),
            "status": flow.get("status"),
            "phase": dict(phase),
            "completed_phase_ids": [
                str(value)
                for value in flow.get("completed_phase_ids", [])
                if isinstance(value, str)
            ],
            "verification_url": public.get("verification_url"),
            "user_code": public.get("user_code"),
            "qr_ascii": public.get("qr_ascii"),
            "expires_at": flow.get("expires_at"),
            "completion_hint": flow.get("completion_hint"),
            "diagnostic": (
                dict(flow["last_user_attempt"])
                if isinstance(flow.get("last_user_attempt"), dict)
                else None
            ),
        }

    def _update_active(
        self,
        provider: str,
        profile_id: str,
        *,
        status: str,
        completed_phase_id: str | None = None,
        error: str | None = None,
        clear_public: bool,
        remove_continuation: bool,
        remove_staged: bool,
        remove_candidate: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        provider = safe_identity_component(provider, field="provider")
        profile_id = safe_identity_component(profile_id, field="profile_id")
        now = time.time()
        updated: dict[str, Any] | None = None
        with self._lock:
            registry = self._read_registry()
            for item in registry["flows"]:
                if (
                    isinstance(item, dict)
                    and item.get("owner_user_id") == self.owner_user_id
                    and item.get("provider") == provider
                    and item.get("profile_id") == profile_id
                    and item.get("status") not in _TERMINAL_FLOW_STATUSES
                ):
                    item["status"] = status
                    item["updated_at"] = now
                    item["revision"] = int(item.get("revision") or 0) + 1
                    if completed_phase_id:
                        completed = [str(value) for value in item.get("completed_phase_ids", [])]
                        if completed_phase_id not in completed:
                            completed.append(completed_phase_id)
                        item["completed_phase_ids"] = completed
                    if clear_public:
                        item["public"] = {}
                        item["expires_at"] = None
                    if error:
                        item["error"] = str(error)[:1000]
                    if extra:
                        item.update(extra)
                    updated = dict(item)
                    break
            self._write_registry(registry)
        if updated is not None:
            paths: list[Path] = []
            if remove_continuation:
                paths.extend(self._secret_paths(str(updated["flow_id"])))
            if remove_staged:
                paths.append(self._state_path(str(updated["flow_id"])))
            if remove_candidate:
                paths.extend(self._candidate_state_paths(str(updated["flow_id"])))
            for path in paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        return updated

    def _write_secret(self, flow: dict[str, Any], secret: dict[str, Any]) -> None:
        self._validate_flow_identity(flow)
        phase_id = safe_identity_component(str(flow.get("phase_id") or ""), field="phase_id")
        attempt = max(1, int(flow.get("attempt") or 0))
        payload = json.dumps(secret, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = self.vault.seal_flow(
            payload,
            owner_user_id=self.owner_user_id,
            provider=str(flow["provider"]),
            profile_id=str(flow["profile_id"]),
            flow_id=f"{flow['flow_id']}.{phase_id}.{attempt}",
        )
        _atomic_write(self._secret_path(str(flow["flow_id"]), phase_id, attempt), envelope)

    def _validate_flow_identity(self, flow: dict[str, Any]) -> None:
        if flow.get("owner_user_id") != self.owner_user_id:
            raise ValueError("authorization flow owner mismatch")
        for field in ("flow_id", "provider", "profile_id"):
            safe_identity_component(str(flow.get(field) or ""), field=field)

    def _secret_path(self, flow_id: str, phase_id: str, attempt: int) -> Path:
        flow_id = safe_identity_component(flow_id, field="flow_id")
        phase_id = safe_identity_component(phase_id, field="phase_id")
        attempt = int(attempt)
        if attempt < 1:
            raise ValueError("authorization continuation attempt is invalid")
        return self.root / f"{flow_id}.{phase_id}.attempt-{attempt}.continuation.enc"

    def _secret_paths(self, flow_id: str) -> list[Path]:
        flow_id = safe_identity_component(flow_id, field="flow_id")
        paths = list(self.root.glob(f"{flow_id}.*.attempt-*.continuation.enc"))
        legacy = self._legacy_secret_path(flow_id)
        if legacy.exists():
            paths.append(legacy)
        return paths

    def _legacy_secret_path(self, flow_id: str) -> Path:
        flow_id = safe_identity_component(flow_id, field="flow_id")
        return self.root / f"{flow_id}.enc"

    def _state_path(self, flow_id: str) -> Path:
        flow_id = safe_identity_component(flow_id, field="flow_id")
        return self.root / f"{flow_id}.state.enc"

    def _candidate_state_path(self, flow_id: str, attempt: int) -> Path:
        flow_id = safe_identity_component(flow_id, field="flow_id")
        attempt = int(attempt)
        if attempt < 1:
            raise ValueError("authorization candidate attempt is invalid")
        return self.root / f"{flow_id}.attempt-{attempt}.candidate.state.enc"

    def _candidate_state_paths(self, flow_id: str) -> list[Path]:
        flow_id = safe_identity_component(flow_id, field="flow_id")
        return list(self.root.glob(f"{flow_id}.attempt-*.candidate.state.enc"))

    @staticmethod
    def _safe_public(public: dict[str, Any]) -> dict[str, Any]:
        allowed = {"verification_url", "user_code", "qr_ascii"}
        return {key: value for key, value in public.items() if key in allowed and value is not None}

    def _read_registry(self) -> dict[str, Any]:
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            value = {"version": 1, "flows": []}
        if not isinstance(value, dict) or not isinstance(value.get("flows"), list):
            raise ValueError("authorization flow registry is invalid")
        return value

    def _write_registry(self, value: dict[str, Any]) -> None:
        _atomic_write(
            self.registry_path,
            (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
