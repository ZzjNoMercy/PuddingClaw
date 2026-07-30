"""Task-local handoff between Tool Gate and an execution backend."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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

    @property
    def execution_command(self) -> str:
        """Canonical command bound into the immutable requirements digest."""

        return self.requirements.execution_command or self.command

    def valid_at_spawn(self, *, command: str, selected_runner: str) -> bool:
        return self.profile.valid_at_spawn() and self.permit.valid_at_spawn(
            tool_call_id=self.permit.tool_call_id,
            command=command,
            requirements=self.requirements,
            current_permission_revision=self.current_permission_revision(),
            profile_digest=self.profile.digest,
            selected_runner=selected_runner,
        )


_CURRENT_EXECUTION: ContextVar[AuthorizedExecution | None] = ContextVar(
    "puddingclaw_authorized_execution",
    default=None,
)


def current_authorized_execution() -> AuthorizedExecution | None:
    return _CURRENT_EXECUTION.get()


@contextmanager
def bind_authorized_execution(execution: AuthorizedExecution) -> Iterator[None]:
    token = _CURRENT_EXECUTION.set(execution)
    try:
        yield
    finally:
        _CURRENT_EXECUTION.reset(token)
