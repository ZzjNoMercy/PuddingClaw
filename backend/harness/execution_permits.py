"""Immutable authorization handoff from Tool Gate to an execution runner."""

from __future__ import annotations

import hashlib
import hmac
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from harness.tool_execution import ExecutionRequirements


def command_digest(command: str) -> str:
    return "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionPermit:
    """One tool-call-scoped, runner-specific authorization snapshot."""

    tool_call_id: str
    command_digest: str
    requirements_digest: str
    permission_revision: int
    profile_digest: str
    selected_runner: str
    runner_binding_digest: str = ""

    _consumed: ClassVar[set[str]] = set()
    _consumed_lock: ClassVar[threading.Lock] = threading.Lock()

    def _identity(self) -> str:
        payload = "\0".join(
            (
                self.tool_call_id,
                self.command_digest,
                self.requirements_digest,
                str(self.permission_revision),
                self.profile_digest,
                self.selected_runner,
                self.runner_binding_digest,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def consume_at_spawn(self) -> bool:
        """Atomically consume this exact permit once.

        Permission grants may be reusable at their declared scope; the
        immutable execution handoff may not be replayed for a second process.
        """

        identity = self._identity()
        with self._consumed_lock:
            if identity in self._consumed:
                return False
            self._consumed.add(identity)
            return True

    @classmethod
    def issue(
        cls,
        *,
        tool_call_id: str,
        command: str,
        requirements: ExecutionRequirements,
        permission_revision: int,
        profile_digest: str,
        selected_runner: str,
        runner_binding_digest: str = "",
    ) -> ExecutionPermit:
        if not tool_call_id or not command or not profile_digest or not selected_runner:
            raise ValueError("Execution permit fields must be non-empty")
        if permission_revision < 0:
            raise ValueError("Permission revision must be non-negative")
        if not runner_binding_digest and selected_runner.startswith("kernel_"):
            from harness.kernel_sandbox import kernel_runner_binding_digest

            runner_binding_digest = kernel_runner_binding_digest()
        return cls(
            tool_call_id=tool_call_id,
            command_digest=command_digest(command),
            requirements_digest=requirements.digest,
            permission_revision=permission_revision,
            profile_digest=profile_digest,
            selected_runner=selected_runner,
            runner_binding_digest=runner_binding_digest,
        )

    def valid_at_spawn(
        self,
        *,
        tool_call_id: str,
        command: str,
        requirements: ExecutionRequirements,
        current_permission_revision: int,
        profile_digest: str,
        selected_runner: str,
        runner_binding_digest: str = "",
    ) -> bool:
        """Revalidate every mutable or replayable dimension before spawn."""

        if not runner_binding_digest and selected_runner.startswith("kernel_"):
            from harness.kernel_sandbox import kernel_runner_binding_digest

            runner_binding_digest = kernel_runner_binding_digest()

        return all(
            (
                hmac.compare_digest(self.tool_call_id, tool_call_id),
                hmac.compare_digest(self.command_digest, command_digest(command)),
                hmac.compare_digest(self.requirements_digest, requirements.digest),
                self.permission_revision == current_permission_revision,
                hmac.compare_digest(self.profile_digest, profile_digest),
                hmac.compare_digest(self.selected_runner, selected_runner),
                hmac.compare_digest(self.runner_binding_digest, runner_binding_digest),
            )
        )
