"""Compile missing external shell authority into one user-facing plan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from graph.effective_grants import EffectiveGrantSet
from graph.permission_policy import ShellDirectoryGrantSpec

if TYPE_CHECKING:
    from harness.tool_execution import ExecutionRequirements


def _direct_authorization_root(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    root = candidate if candidate.is_dir() else candidate.parent
    while not root.is_dir() and root != root.parent:
        root = root.parent
    if root == root.parent or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError(f"External shell path has no canonical directory ancestor: {path}")
    return root


@dataclass(frozen=True)
class ShellAccessPlan:
    """One atomic directory permission prompt for a single shell command."""

    grant_specs: tuple[ShellDirectoryGrantSpec, ...]

    @property
    def required(self) -> bool:
        return bool(self.grant_specs)

    @property
    def directories(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(spec.target for spec in self.grant_specs))

    @classmethod
    def compile(
        cls,
        requirements: ExecutionRequirements,
        effective: EffectiveGrantSet,
    ) -> ShellAccessPlan:
        if requirements.opaque:
            raise ValueError("Opaque commands cannot request external shell authority")
        requested: dict[str, dict[str, bool]] = {}
        for intent in requirements.filesystem_intents:
            path = Path(intent.path)
            if intent.access == "read":
                if effective.allows_shell_directory(path, access="read"):
                    continue
                root = _direct_authorization_root(path)
                requested.setdefault(str(root), {"read": False, "write": False, "delete": False})[
                    "read"
                ] = True
                continue
            if intent.access not in {"write", "delete"}:
                raise ValueError(f"Unsupported external shell access: {intent.access}")
            if effective.allows_shell_directory(
                path,
                access="write",
                delete=intent.access == "delete",
            ):
                continue
            root = _direct_authorization_root(path)
            entry = requested.setdefault(
                str(root),
                {"read": False, "write": False, "delete": False},
            )
            # A general shell writable projection is also readable on every
            # supported runner, so the UI and persisted grants must say so.
            entry["read"] = True
            entry["write"] = True
            entry["delete"] = entry["delete"] or intent.access == "delete"

        specs: list[ShellDirectoryGrantSpec] = []
        for target in sorted(requested):
            access = requested[target]
            if access["read"]:
                specs.append(ShellDirectoryGrantSpec(target=target, access="read"))
            if access["write"]:
                specs.append(
                    ShellDirectoryGrantSpec(
                        target=target,
                        access="write",
                        delete=access["delete"],
                    )
                )
        return cls(grant_specs=tuple(specs))
