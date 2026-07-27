"""Managed CLI control-plane service."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_identity.adapters import ManagedCliMatch, ManagedCliRoute
from runtime_identity.paths import PuddingClawPaths, trusted_owner_user_id
from runtime_identity.profiles import CredentialProfileStore
from runtime_identity.toolchains import ToolchainManager

_SECRET_PATTERNS = (
    re.compile(r"(?i)(access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret)(\s*[=:]\s*)([^\s,}\]]+)"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s]+)"),
    re.compile(r'(?i)("(?:access_token|refresh_token|app_secret|client_secret)"\s*:\s*")([^"]+)(")'),
)


def redact_managed_cli_output(output: str) -> str:
    value = str(output or "")
    value = _SECRET_PATTERNS[0].sub(r"\1\2<redacted>", value)
    value = _SECRET_PATTERNS[1].sub(r"\1<redacted>", value)
    value = _SECRET_PATTERNS[2].sub(r"\1<redacted>\3", value)
    return value


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
            },
            ensure_ascii=False,
            sort_keys=True,
        )


class ManagedCliService:
    def __init__(self, backend: object, *, paths: PuddingClawPaths | None = None) -> None:
        self.backend = backend
        self.paths = paths or PuddingClawPaths.from_environment()
        self.toolchains = ToolchainManager(self.paths, backend.manager.runtime_contract)

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
                    str(context.get("credential_profile_id"))
                    if context.get("credential_profile_id")
                    else None
                ),
            )
            assert profile is not None
            if (
                match.argv[:3] == ("lark-cli", "config", "init")
                and profile.get("status")
                not in {"pending_configuration", "awaiting_user_browser", "expired", "revoked"}
            ):
                raise ValueError(
                    "config init --new cannot overwrite an existing shared Credential Profile; "
                    "select or create a new Profile first"
                )
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
        return self._provider(plan)

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
                "toolchain": (
                    "user://runtime/toolchains/node/"
                    f"{self.toolchains.resolve_node().runtime_contract}"
                ),
                "output": redact_managed_cli_output(result.output),
            },
            exit_code=result.exit_code,
        )

    def _provider(self, plan: ManagedCliExecutionPlan) -> ManagedCliServiceResult:
        match = plan.match
        executable = plan.toolchain_path / "bin" / "lark-cli"
        if not executable.exists():
            return self._error(
                "managed_cli_not_installed",
                "lark-cli is not installed in the shared Toolchain. Run npm install -g @larksuite/cli.",
            )
        owner_user_id = plan.owner_user_id
        store = CredentialProfileStore(self.paths, owner_user_id)
        profile_id = plan.profile_id or ""
        provider = match.provider or ""
        state = b""
        try:
            lock = store.profile_lock(provider, profile_id) if match.requires_profile else _NullContext()
            with lock:
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
                    state = store.read_state(provider, profile_id)
                result = self.backend.run_managed_provider_cli(
                    argv=list(match.argv),
                    environment=dict(match.env),
                    toolchain_path=plan.toolchain_path,
                    container_path="/opt/puddingclaw/toolchain/node",
                    credential_state=state,
                    network_enabled=match.requires_network,
                    workspace_writable=match.workspace_writable,
                )
                if match.requires_profile and result.credential_state is not None:
                    store.write_state(provider, profile_id, result.credential_state)
                    if result.exit_code == 0:
                        next_status: str | None = None
                        lowered = tuple(item.lower() for item in match.argv[1:])
                        if match.route == ManagedCliRoute.BROWSER_AUTH:
                            next_status = "awaiting_user_browser"
                        elif lowered[:2] == ("auth", "login") and "--device-code" in lowered:
                            next_status = "active"
                        elif lowered[:2] in {("auth", "logout"), ("config", "remove")}:
                            next_status = "revoked"
                        if next_status is not None:
                            store.update_status(profile_id, next_status)
        except Exception as exc:  # noqa: BLE001
            return self._error("managed_provider_failed", f"{type(exc).__name__}: {exc}")
        output = redact_managed_cli_output(result.output)
        awaiting = match.route == ManagedCliRoute.BROWSER_AUTH and result.exit_code == 0
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
        if awaiting:
            payload["next_action"] = (
                "Show the authorization URL/QR code and end this Agent turn. "
                "After the user confirms completion, verify with lark-cli config show or auth status --verify."
            )
        return ManagedCliServiceResult(payload=payload, exit_code=result.exit_code)

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
