"""Task-local handoff between Tool Gate and an execution backend."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harness.execution_permits import ExecutionPermit
from harness.sandbox_profiles import SandboxGrantProfile

if TYPE_CHECKING:
    from harness.tool_execution import ExecutionRequirements


@dataclass(frozen=True)
class AuthorizedExecution:
    permit: ExecutionPermit
    command: str
    requirements: ExecutionRequirements
    profile: SandboxGrantProfile
    current_permission_revision: Callable[[], int]
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False, compare=False)
    secret_values: tuple[str, ...] = field(default=(), repr=False, compare=False)
    environment_current: Callable[[], bool] = field(
        default=lambda: True,
        repr=False,
        compare=False,
    )

    @property
    def execution_command(self) -> str:
        """Canonical command bound into the immutable requirements digest."""

        return self.requirements.execution_command or self.command

    def valid_at_spawn(
        self,
        *,
        command: str,
        selected_runner: str,
        runner_binding_digest: str = "",
    ) -> bool:
        return self.environment_current() and self.profile.valid_at_spawn() and self.permit.valid_at_spawn(
            tool_call_id=self.permit.tool_call_id,
            command=command,
            requirements=self.requirements,
            current_permission_revision=self.current_permission_revision(),
            profile_digest=self.profile.digest,
            selected_runner=selected_runner,
            runner_binding_digest=runner_binding_digest,
        )

    def consume_at_spawn(self, *, command: str, selected_runner: str, runner_binding_digest: str = "") -> bool:
        """Revalidate and atomically consume the one-process execution handoff."""

        return self.valid_at_spawn(
            command=command,
            selected_runner=selected_runner,
            runner_binding_digest=runner_binding_digest,
        ) and self.permit.consume_at_spawn()


@dataclass(frozen=True)
class AuthorizedBrowserAction:
    """Opaque proof that Harness approved this exact browser invocation."""

    session_id: str
    run_id: str
    tool_call_id: str
    action: str
    args_digest: str


def browser_action_digest(action: str, args: object) -> str:
    return hashlib.sha256(
        json.dumps(
            {"action": action, "args": args},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


_CURRENT_EXECUTION: ContextVar[AuthorizedExecution | None] = ContextVar(
    "puddingclaw_authorized_execution",
    default=None,
)
_CURRENT_BROWSER_ACTION: ContextVar[AuthorizedBrowserAction | None] = ContextVar(
    "puddingclaw_authorized_browser_action",
    default=None,
)


def current_authorized_execution() -> AuthorizedExecution | None:
    return _CURRENT_EXECUTION.get()


def current_authorized_browser_action() -> AuthorizedBrowserAction | None:
    return _CURRENT_BROWSER_ACTION.get()


@contextmanager
def bind_authorized_execution(execution: AuthorizedExecution) -> Iterator[None]:
    token = _CURRENT_EXECUTION.set(execution)
    try:
        yield
    finally:
        _CURRENT_EXECUTION.reset(token)


@contextmanager
def bind_authorized_browser_action(action: AuthorizedBrowserAction) -> Iterator[None]:
    token = _CURRENT_BROWSER_ACTION.set(action)
    try:
        yield
    finally:
        _CURRENT_BROWSER_ACTION.reset(token)
