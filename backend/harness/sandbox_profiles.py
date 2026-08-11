"""Runner-neutral filesystem and resource authority for one command."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


def _canonical_directory(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"Sandbox root must be absolute: {path}")
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError(f"Sandbox root must be an existing non-symlink directory: {path}")
    canonical = candidate.resolve()
    if str(canonical) != str(candidate):
        raise ValueError(f"Sandbox root must already be canonical: {path}")
    return canonical


def _covered(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


@dataclass(frozen=True)
class SandboxGrantProfile:
    """The complete OS authority projected for one command.

    General shell write roots must also be readable because Docker and
    Seatbelt writable directory projections cannot provide portable
    write-only semantics. Exact write-only authority stays in HostFileBroker.
    """

    workspace_root: Path
    scratch_root: Path
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    delete_roots: tuple[Path, ...] = ()
    deny_roots: tuple[Path, ...] = ()
    workspace_writable: bool = True
    network_allowed: bool = False
    timeout_seconds: int = 120
    max_output_bytes: int = 100_000
    max_processes: int = 128
    profile_schema: str = "sandbox-grant-profile-v1"

    @classmethod
    def build(
        cls,
        *,
        workspace_root: str | Path,
        scratch_root: str | Path,
        external_read_roots: Iterable[str | Path] = (),
        external_write_roots: Iterable[str | Path] = (),
        external_delete_roots: Iterable[str | Path] = (),
        external_deny_roots: Iterable[str | Path] = (),
        workspace_writable: bool = True,
        network_allowed: bool = False,
        timeout_seconds: int = 120,
        max_output_bytes: int = 100_000,
        max_processes: int = 128,
    ) -> SandboxGrantProfile:
        workspace = _canonical_directory(workspace_root)
        scratch = _canonical_directory(scratch_root)
        reads = tuple(
            dict.fromkeys(
                (
                    workspace,
                    scratch,
                    *(_canonical_directory(path) for path in external_read_roots),
                )
            )
        )
        writes = tuple(
            dict.fromkeys(
                (
                    *((workspace,) if workspace_writable else ()),
                    scratch,
                    *(_canonical_directory(path) for path in external_write_roots),
                )
            )
        )
        deletes = tuple(dict.fromkeys(_canonical_directory(path) for path in external_delete_roots))
        denies = tuple(dict.fromkeys(_canonical_directory(path) for path in external_deny_roots))
        if any(not _covered(root, reads) for root in writes):
            raise ValueError("Every shell write root must be covered by explicit read authority")
        if any(not _covered(root, writes) for root in deletes):
            raise ValueError("Every shell delete root must be covered by explicit write authority")
        if timeout_seconds <= 0 or max_output_bytes <= 0 or max_processes <= 0:
            raise ValueError("Sandbox limits must be positive")
        return cls(
            workspace_root=workspace,
            scratch_root=scratch,
            read_roots=reads,
            write_roots=writes,
            delete_roots=deletes,
            deny_roots=denies,
            workspace_writable=workspace_writable,
            network_allowed=network_allowed,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_processes=max_processes,
        )

    @property
    def digest(self) -> str:
        payload = {
            "profile_schema": self.profile_schema,
            "workspace_root": str(self.workspace_root),
            "scratch_root": str(self.scratch_root),
            "read_roots": [str(path) for path in self.read_roots],
            "write_roots": [str(path) for path in self.write_roots],
            "delete_roots": [str(path) for path in self.delete_roots],
            "deny_roots": [str(path) for path in self.deny_roots],
            "workspace_writable": self.workspace_writable,
            "network_allowed": self.network_allowed,
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_processes": self.max_processes,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def valid_at_spawn(self) -> bool:
        """Reject roots replaced or redirected after profile compilation."""

        for root in (*self.read_roots, *self.write_roots, *self.delete_roots, *self.deny_roots):
            if root.is_symlink() or not root.is_dir() or root.resolve() != root:
                return False
        return all(_covered(root, self.read_roots) for root in self.write_roots) and all(
            _covered(root, self.write_roots) for root in self.delete_roots
        )
