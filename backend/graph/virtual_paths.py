"""Canonical DeepAgents path authority classification.

Every permission and filesystem boundary must agree on whether a path belongs
to the current project, an internal scratch area, a managed read-only mount, or
the external host.  Keeping that decision here prevents virtual paths from
falling through to :class:`HostFileBroker` and being mistaken for host paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

VIRTUAL_NAMESPACE_ROOTS = (
    "/workspace",
    "/knowledge",
    "/semantic-assets",
    "/sql-guardrails",
    "/analytics-models",
    "/skills",
    "/large_tool_results",
    "/scratch",
)

WRITABLE_VIRTUAL_NAMESPACE_ROOTS = ("/workspace", "/scratch")
MANAGED_VIRTUAL_NAMESPACE_ROOTS = tuple(
    root for root in VIRTUAL_NAMESPACE_ROOTS if root not in WRITABLE_VIRTUAL_NAMESPACE_ROOTS
)


class PathAuthority(StrEnum):
    """The single security authority that owns one filesystem path."""

    WORKSPACE = "workspace"
    SCRATCH = "scratch"
    MANAGED = "managed"
    EXTERNAL = "external"
    ESCAPE = "escape"


@dataclass(frozen=True, slots=True)
class ClassifiedPath:
    """A normalized path and the authority responsible for it."""

    authority: PathAuthority
    original_path: str
    normalized_path: str
    virtual_path: str | None = None
    canonical_host_path: Path | None = None

    @property
    def internally_writable(self) -> bool:
        return self.authority in {PathAuthority.WORKSPACE, PathAuthority.SCRATCH}


def normalize_virtual_path(path: str) -> str:
    """Normalize separators without interpreting a virtual path on the host."""
    return str(path or "").strip().replace("\\", "/")


def is_virtual_path(path: str) -> bool:
    """Return whether path is a namespace root or one of its descendants."""
    normalized = normalize_virtual_path(path)
    return any(
        normalized == root or normalized.startswith(f"{root}/")
        for root in VIRTUAL_NAMESPACE_ROOTS
    )


def _virtual_relative_path(normalized: str, root: str) -> PurePosixPath | None:
    relative = normalized.removeprefix(root).lstrip("/")
    candidate = PurePosixPath(relative or ".")
    if any(part == ".." for part in candidate.parts):
        return None
    return candidate


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def classify_path_authority(
    path: str,
    *,
    workspace_root: str | Path | None = None,
) -> ClassifiedPath:
    """Classify a model-visible path without guessing in downstream layers.

    Rules are deliberately ordered and fail closed:

    * ``/workspace`` and relative paths belong to the current project;
    * ``/scratch`` belongs to the Goal-scoped internal scratch backend;
    * other registered virtual namespaces are managed by PuddingClaw;
    * host absolute paths resolving inside ``workspace_root`` are canonicalized
      back to ``/workspace``;
    * traversal or a symlink that escapes the workspace is ``ESCAPE``;
    * every remaining absolute path is external.

    Bare POSIX roots such as ``/report.html`` remain outside this classifier's
    workspace contract because they are indistinguishable from real host paths.
    The public canonical project namespace is ``/workspace``.
    """

    original = str(path or "")
    normalized = normalize_virtual_path(original)
    root = Path(workspace_root).expanduser().resolve() if workspace_root else None

    for virtual_root, authority, canonical_virtual_root in (
        ("/workspace", PathAuthority.WORKSPACE, "/workspace"),
        ("/scratch", PathAuthority.SCRATCH, "/scratch"),
    ):
        if normalized == virtual_root or normalized.startswith(f"{virtual_root}/"):
            relative = _virtual_relative_path(normalized, virtual_root)
            if relative is None:
                return ClassifiedPath(
                    PathAuthority.ESCAPE,
                    original,
                    normalized,
                )
            virtual_path = canonical_virtual_root + (
                f"/{relative.as_posix()}" if relative.as_posix() != "." else ""
            )
            canonical: Path | None = None
            if authority is PathAuthority.WORKSPACE and root is not None:
                canonical = (root / relative.as_posix()).resolve(strict=False)
                if not _is_relative_to(canonical, root):
                    return ClassifiedPath(
                        PathAuthority.ESCAPE,
                        original,
                        normalized,
                        virtual_path=virtual_path,
                        canonical_host_path=canonical,
                    )
            return ClassifiedPath(
                authority,
                original,
                normalized,
                virtual_path=virtual_path,
                canonical_host_path=canonical,
            )

    if is_virtual_path(normalized):
        return ClassifiedPath(
            PathAuthority.MANAGED,
            original,
            normalized,
            virtual_path=normalized,
        )

    requested = Path(normalized).expanduser()
    if not requested.is_absolute():
        if any(part == ".." for part in PurePosixPath(normalized).parts):
            return ClassifiedPath(
                PathAuthority.ESCAPE,
                original,
                normalized,
            )
        if root is None:
            return ClassifiedPath(
                PathAuthority.WORKSPACE,
                original,
                normalized,
                virtual_path=normalized,
            )
        canonical = (root / requested).resolve(strict=False)
        if not _is_relative_to(canonical, root):
            return ClassifiedPath(
                PathAuthority.ESCAPE,
                original,
                normalized,
                canonical_host_path=canonical,
            )
        relative = canonical.relative_to(root).as_posix()
        return ClassifiedPath(
            PathAuthority.WORKSPACE,
            original,
            normalized,
            virtual_path=f"/workspace/{relative}" if relative != "." else "/workspace",
            canonical_host_path=canonical,
        )

    lexical = Path(os.path.abspath(requested))
    canonical = requested.resolve(strict=False)
    if (
        root is not None
        and _is_relative_to(lexical, root)
        and not _is_relative_to(canonical, root)
    ):
        return ClassifiedPath(
            PathAuthority.ESCAPE,
            original,
            normalized,
            canonical_host_path=canonical,
        )
    if root is not None and _is_relative_to(canonical, root):
        relative = canonical.relative_to(root).as_posix()
        return ClassifiedPath(
            PathAuthority.WORKSPACE,
            original,
            normalized,
            virtual_path=f"/workspace/{relative}" if relative != "." else "/workspace",
            canonical_host_path=canonical,
        )
    return ClassifiedPath(
        PathAuthority.EXTERNAL,
        original,
        normalized,
        canonical_host_path=canonical,
    )
