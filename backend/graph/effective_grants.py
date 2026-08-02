"""Resolve persisted permission grants into one Run-scoped authority view.

The persisted grant list is audit state.  Callers must not independently
decide which entries are current authority: scope, Run identity, stable
bindings, and capabilities need the same interpretation everywhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph.permission_policy import (
    SHELL_PERMISSION_BINDING_SCHEMA_VERSION,
    PermissionBindingPolicy,
)


@dataclass(frozen=True)
class EffectiveGrant:
    """One active grant after Run/scope/binding projection."""

    grant_id: str
    grant_type: str
    scope: str
    target_kind: str
    target: str
    capabilities: frozenset[str]
    binding_schema_version: int

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "grant_type": self.grant_type,
            "scope": self.scope,
            "target_kind": self.target_kind,
            "target": self.target,
            "capabilities": sorted(self.capabilities),
        }


@dataclass(frozen=True)
class EffectiveGrantSet:
    """Immutable authority snapshot for one Run.

    Exact-file grants are intentionally session authority without Run
    bindings. Reusable directory and tool-action grants require stable
    bindings. Run-scoped directory grants are already isolated by their
    server-authored ``metadata.run_id`` and remain valid when legacy records
    do not carry bindings.
    """

    grants: tuple[EffectiveGrant, ...]
    run_id: str
    permission_revision: int = 0

    @classmethod
    def resolve(
        cls,
        grants: Iterable[Mapping[str, Any]],
        *,
        run_id: str,
        current_bindings: Mapping[str, Any] | None,
        current_shell_bindings: Mapping[str, Any] | None = None,
        permission_revision: int = 0,
    ) -> EffectiveGrantSet:
        effective: list[EffectiveGrant] = []
        for raw in grants:
            if raw.get("revoked_at") or raw.get("superseded_at"):
                continue
            grant_type = str(raw.get("type") or "")
            scope = str(raw.get("scope") or "session")
            target_kind = str(raw.get("target_kind") or "")
            target = str(raw.get("target") or "")
            if not grant_type or not target_kind or not target:
                continue

            metadata = raw.get("metadata")
            grant_run_id = str(metadata.get("run_id") or "") if isinstance(metadata, Mapping) else ""
            if scope in {"once", "run"} and (not run_id or grant_run_id != run_id):
                continue

            bindings = raw.get("bindings")
            has_bindings = isinstance(bindings, Mapping)
            capabilities = frozenset(str(item) for item in raw.get("capabilities") or [] if str(item))
            binding_schema_version = int(raw.get("binding_schema_version") or 0)
            native_shell_authority = "shell_access" in capabilities
            if native_shell_authority and (
                binding_schema_version != SHELL_PERMISSION_BINDING_SCHEMA_VERSION
                or not PermissionBindingPolicy.shell_v3_equivalent(
                    bindings if isinstance(bindings, Mapping) else None,
                    current_shell_bindings,
                )
            ):
                continue
            exact_file_authority = (
                scope == "session"
                and grant_type.startswith("external_file_")
                and target_kind in {"exact_file", "all_external_files"}
            )
            legacy_run_directory = scope == "run" and grant_type.startswith("external_directory_")
            if native_shell_authority:
                pass
            elif not exact_file_authority and not legacy_run_directory:
                if not has_bindings or current_bindings is None:
                    continue
                if not PermissionBindingPolicy.equivalent(
                    grant_type=grant_type,
                    scope=scope,
                    target_kind=target_kind,
                    target=target,
                    left=bindings,
                    right=current_bindings,
                ):
                    continue
            elif has_bindings and current_bindings is not None:
                if not PermissionBindingPolicy.equivalent(
                    grant_type=grant_type,
                    scope=scope,
                    target_kind=target_kind,
                    target=target,
                    left=bindings,
                    right=current_bindings,
                ):
                    continue

            effective.append(
                EffectiveGrant(
                    grant_id=str(raw.get("id") or ""),
                    grant_type=grant_type,
                    scope=scope,
                    target_kind=target_kind,
                    target=target,
                    capabilities=capabilities,
                    binding_schema_version=binding_schema_version,
                )
            )
        effective.sort(
            key=lambda item: (
                item.grant_type,
                item.scope,
                item.target_kind,
                item.target,
                tuple(sorted(item.capabilities)),
                item.grant_id,
            )
        )
        return cls(
            grants=tuple(effective),
            run_id=run_id,
            permission_revision=max(0, int(permission_revision)),
        )

    def manifest_entries(self) -> list[dict[str, Any]]:
        return [grant.manifest_entry() for grant in self.grants]

    def allows_directory(
        self,
        path: Path,
        *,
        access: str,
        required_capabilities: Iterable[str] = (),
    ) -> bool:
        """Return whether one Broker directory grant covers ``path`` recursively."""

        if access not in {"read", "write"}:
            return False
        requested = path.expanduser().resolve()
        required = {access, *(str(item) for item in required_capabilities)}
        for grant in self.grants:
            if (
                grant.grant_type != f"external_directory_{access}"
                or grant.target_kind != "exact_directory"
                or not required.issubset(grant.capabilities)
            ):
                continue
            root = Path(grant.target).expanduser().resolve()
            try:
                requested.relative_to(root)
            except ValueError:
                continue
            return True
        return False

    def allows_shell_directory(
        self,
        path: Path,
        *,
        access: str,
        delete: bool = False,
    ) -> bool:
        """Return whether ``path`` may be exposed to a general shell.

        Docker writable binds are inherently readable, so shell write access
        requires explicit read and write grants carrying ``shell_access``.
        Broker write-only grants remain valid for exact server-side mutation
        but never become shell mounts.
        """

        if access == "read":
            return self._allows_shell_directory_capability(
                path,
                access="read",
                required_capabilities={"shell_access"},
            )
        if access != "write":
            return False
        write_capabilities = {"shell_access"}
        if delete:
            write_capabilities.add("delete")
        return self._allows_shell_directory_capability(
            path,
            access="read",
            required_capabilities={"shell_access"},
        ) and self._allows_shell_directory_capability(
            path,
            access="write",
            required_capabilities=write_capabilities,
        )

    def _allows_shell_directory_capability(
        self,
        path: Path,
        *,
        access: str,
        required_capabilities: Iterable[str],
    ) -> bool:
        requested = path.expanduser().resolve()
        required = {access, *(str(item) for item in required_capabilities)}
        for grant in self.grants:
            if (
                grant.grant_type != f"external_directory_{access}"
                or grant.target_kind != "exact_directory"
                or not required.issubset(grant.capabilities)
            ):
                continue
            persisted_root = Path(grant.target).expanduser()
            if not persisted_root.is_absolute() or persisted_root.is_symlink() or not persisted_root.is_dir():
                continue
            canonical_root = persisted_root.resolve()
            if str(canonical_root) != str(persisted_root):
                # Grants are stored canonically. A later path substitution or
                # symlink must not silently redirect shell authority.
                continue
            try:
                requested.relative_to(canonical_root)
            except ValueError:
                continue
            return True
        return False

    def _matching_shell_grants(
        self,
        path: Path,
        *,
        access: str,
        required_capabilities: Iterable[str] = (),
    ) -> tuple[tuple[EffectiveGrant, Path], ...]:
        requested = path.expanduser().resolve()
        required = {access, "shell_access", *(str(item) for item in required_capabilities)}
        matches: list[tuple[EffectiveGrant, Path]] = []
        for grant in self.grants:
            if (
                grant.grant_type != f"external_directory_{access}"
                or grant.target_kind != "exact_directory"
                or not required.issubset(grant.capabilities)
            ):
                continue
            root = Path(grant.target).expanduser()
            if (
                not root.is_absolute()
                or root.is_symlink()
                or not root.is_dir()
                or root.resolve() != root
            ):
                continue
            try:
                requested.relative_to(root)
            except ValueError:
                continue
            matches.append((grant, root))
        matches.sort(key=lambda item: (-len(item[1].parts), str(item[1]), item[0].grant_id))
        return tuple(matches)


@dataclass(frozen=True)
class SelectedGrantSet:
    """Minimum shell roots selected for one non-opaque command."""

    grant_ids: tuple[str, ...]
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    delete_roots: tuple[Path, ...]
    permission_revision: int

    @classmethod
    def select(
        cls,
        effective: EffectiveGrantSet,
        requirements: Any,
    ) -> SelectedGrantSet:
        if bool(getattr(requirements, "opaque", True)):
            raise ValueError("Opaque shell requirements cannot select directory authority")
        selected_ids: set[str] = set()
        read_roots: set[Path] = set()
        write_roots: set[Path] = set()
        delete_roots: set[Path] = set()
        for intent in tuple(getattr(requirements, "filesystem_intents", ())):
            path = Path(str(getattr(intent, "path", ""))).expanduser().resolve()
            access = str(getattr(intent, "access", ""))
            if access == "read":
                matches = effective._matching_shell_grants(path, access="read")
                if not matches:
                    raise PermissionError(f"Missing shell read authority for {path}")
                grant, root = matches[0]
                selected_ids.add(grant.grant_id)
                read_roots.add(root)
                continue
            if access not in {"write", "delete"}:
                raise ValueError(f"Unsupported filesystem intent: {access}")
            write_matches = effective._matching_shell_grants(
                path,
                access="write",
                required_capabilities={"delete"} if access == "delete" else (),
            )
            if not write_matches:
                raise PermissionError(f"Missing shell {access} authority for {path}")
            write_grant, write_root = write_matches[0]
            # The runner exposes the complete writable root. Its entire mount,
            # not just the requested child, must be explicitly readable.
            read_matches = effective._matching_shell_grants(write_root, access="read")
            if not read_matches:
                raise PermissionError(f"Missing matching shell read authority for {write_root}")
            read_grant, read_root = read_matches[0]
            selected_ids.update((write_grant.grant_id, read_grant.grant_id))
            read_roots.add(read_root)
            write_roots.add(write_root)
            if access == "delete":
                delete_roots.add(write_root)
        return cls(
            grant_ids=tuple(sorted(selected_ids)),
            read_roots=tuple(sorted(read_roots, key=str)),
            write_roots=tuple(sorted(write_roots, key=str)),
            delete_roots=tuple(sorted(delete_roots, key=str)),
            permission_revision=effective.permission_revision,
        )

    @classmethod
    def all_shell_authority(cls, effective: EffectiveGrantSet) -> SelectedGrantSet:
        """Project every active shell-directory Grant into one sandbox profile.

        A ``shell_access`` directory Grant authorizes general shell processes,
        not one parser-recognized command family.  Keeping the projection tied
        to statically inferred operands made the parser an accidental command
        whitelist: Python, shell scripts, sed and ordinary tools could not use
        authority the user had already granted.  The OS sandbox remains the
        enforcement boundary; this method never invents a root or capability.
        """

        selected_ids: set[str] = set()
        read_roots: set[Path] = set()
        write_roots: set[Path] = set()
        delete_roots: set[Path] = set()
        for grant in effective.grants:
            if (
                grant.target_kind != "exact_directory"
                or "shell_access" not in grant.capabilities
            ):
                continue
            root = Path(grant.target).expanduser()
            if (
                not root.is_absolute()
                or root.is_symlink()
                or not root.is_dir()
                or root.resolve() != root
            ):
                continue
            if grant.grant_type == "external_directory_read" and "read" in grant.capabilities:
                selected_ids.add(grant.grant_id)
                read_roots.add(root)
                continue
            if grant.grant_type != "external_directory_write" or "write" not in grant.capabilities:
                continue
            read_matches = effective._matching_shell_grants(root, access="read")
            if not read_matches:
                # General writable mounts are also readable.  An unpaired
                # Broker-style write Grant must never become shell authority.
                continue
            read_grant, read_root = read_matches[0]
            selected_ids.update((grant.grant_id, read_grant.grant_id))
            read_roots.add(read_root)
            write_roots.add(root)
            if "delete" in grant.capabilities:
                delete_roots.add(root)
        return cls(
            grant_ids=tuple(sorted(selected_ids)),
            read_roots=tuple(sorted(read_roots, key=str)),
            write_roots=tuple(sorted(write_roots, key=str)),
            delete_roots=tuple(sorted(delete_roots, key=str)),
            permission_revision=effective.permission_revision,
        )
