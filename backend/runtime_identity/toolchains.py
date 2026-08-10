"""User-local, Adapter-isolated Toolchain revision management."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

from runtime_identity.adapters import ToolchainPackageSpec
from runtime_identity.paths import PuddingClawPaths
from runtime_identity.software_runtime import SoftwareRuntimeManager


@dataclass(frozen=True)
class ToolchainRef:
    ecosystem: str
    runtime_contract: str
    host_path: Path
    root_path: Path
    mount_path: Path
    container_path: str = "/opt/puddingclaw/toolchain/node"


@dataclass(frozen=True)
class ToolchainInstallResult:
    output: str
    exit_code: int
    truncated: bool = False
    release_id: str | None = None
    previous_revision: str | None = None
    active_revision: str | None = None
    resolved_version: str | None = None


_SEMVER = re.compile(r"(?<![0-9A-Za-z])v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")


def _semantic_version(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER.fullmatch(str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def version_satisfies(version: str, compatibility: str) -> bool:
    parsed = _semantic_version(version)
    if parsed is None:
        return False
    if "-" in version and "-" not in compatibility:
        # Stable compatibility ranges do not silently admit prereleases.
        return False
    for constraint in compatibility.split():
        match = re.fullmatch(r"(>=|<=|>|<|=)?(\d+\.\d+\.\d+)", constraint)
        if match is None:
            return False
        expected = _semantic_version(match.group(2))
        assert expected is not None
        operator = match.group(1) or "="
        if operator == ">=" and not parsed >= expected:
            return False
        if operator == "<=" and not parsed <= expected:
            return False
        if operator == ">" and not parsed > expected:
            return False
        if operator == "<" and not parsed < expected:
            return False
        if operator == "=" and not parsed == expected:
            return False
    return True



def _read_contained_text(root: Path, path: Path) -> str:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve())
    if not resolved.is_file():
        raise ValueError("Toolchain evidence is not a regular file")
    return resolved.read_text(encoding="utf-8")


def _atomic_write_private(path: Path, content: str) -> None:
    """Write without following an installer-created destination symlink."""

    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise



def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ToolchainManager:
    """Adapter-facing facade over the declarative shared Node runtime."""

    def __init__(
        self,
        paths: PuddingClawPaths,
        runtime_contract: str,
    ) -> None:
        self.paths = paths
        self.runtime_contract = runtime_contract
        self.software = SoftwareRuntimeManager(paths, runtime_contract)

    def resolve_node(self, adapter_id: str | None = None) -> ToolchainRef:
        del adapter_id
        self.paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.paths.root, 0o700)
        current_release = self.software.node_current()
        root = self.paths.shared_node_runtime(self.runtime_contract)
        current = root / "current"
        return ToolchainRef("node", self.runtime_contract, current_release, root, current)

    def install_package(
        self,
        backend: object,
        *,
        adapter_id: str,
        spec: ToolchainPackageSpec,
        distribution: str,
        expected_integrity: str,
        runtime_image_digest: str,
        adapter_contract_fingerprint: str,
        credential_state_fingerprint: str,
        expected_revision: str,
    ) -> ToolchainInstallResult:
        """Commit one Adapter package into the shared desired Node set."""

        distribution_pattern = re.compile(rf"^{re.escape(spec.package)}@(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
        if distribution_pattern.fullmatch(distribution) is None:
            raise ValueError("managed Toolchain installation requires an exact package version")
        if not re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", expected_integrity):
            raise ValueError("managed Toolchain installation requires frozen registry integrity")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image_digest):
            raise ValueError("managed Toolchain installation requires a runtime image digest")
        if spec.ecosystem != "node":
            raise ValueError("unsupported managed Toolchain ecosystem")
        resolved_version = distribution[len(spec.package) + 1 :]
        if spec.compatibility and not version_satisfies(resolved_version, spec.compatibility):
            return ToolchainInstallResult(
                output="Resolved CLI version is outside the trusted Adapter compatibility contract.",
                exit_code=65,
                previous_revision=self.resolve_node(adapter_id).host_path.name,
                active_revision=self.resolve_node(adapter_id).host_path.name,
                resolved_version=resolved_version,
            )
        if spec.expected_integrity and spec.expected_integrity != expected_integrity:
            return ToolchainInstallResult(
                output="Resolved CLI integrity does not match the trusted Adapter contract.",
                exit_code=65,
                previous_revision=self.resolve_node(adapter_id).host_path.name,
                active_revision=self.resolve_node(adapter_id).host_path.name,
                resolved_version=resolved_version,
            )
        del adapter_contract_fingerprint, credential_state_fingerprint
        result = self.software.install_node_owner(
            backend,
            owner=f"integration:{adapter_id}",
            distributions=[distribution],
            declared_bins={spec.package: (spec.executable,)},
            expected_integrities={spec.package: expected_integrity},
            expected_runtime_image_digest=runtime_image_digest,
            expected_base_revision=expected_revision,
        )
        return ToolchainInstallResult(
            output=result.output,
            exit_code=result.exit_code,
            release_id=result.revision,
            previous_revision=result.previous_revision,
            active_revision=result.revision or result.previous_revision,
            resolved_version=resolved_version,
        )

    def resolve_for_adapter(
        self,
        *,
        adapter_id: str,
        spec: ToolchainPackageSpec,
        adapter_contract_fingerprint: str,
        credential_state_fingerprint: str,
        runtime_image_digest: str,
    ) -> ToolchainRef:
        del adapter_contract_fingerprint, credential_state_fingerprint
        ref = self.resolve_node(adapter_id)
        manifest = self.software.validate_node_release(ref.host_path, runtime_image_digest)
        if manifest is None:
            return ref
        package = manifest.get("packages", {}).get(spec.package)
        if not isinstance(package, dict):
            raise ValueError("managed CLI package is not installed in the shared Node runtime")
        version = str(package.get("version") or "")
        integrity = str(package.get("registry_integrity") or "")
        owners = {str(item) for item in package.get("requested_by") or []}
        bins = package.get("declared_bins")
        if (
            f"integration:{adapter_id}" not in owners
            or not isinstance(bins, dict)
            or spec.executable not in bins
            or (spec.compatibility and not version_satisfies(version, spec.compatibility))
            or (spec.expected_integrity and integrity != spec.expected_integrity)
        ):
            raise ValueError("managed CLI package contract is incompatible with the shared Node runtime")
        return ref

    def inspect_current(
        self,
        *,
        adapter_id: str,
        spec: ToolchainPackageSpec,
        adapter_contract_fingerprint: str,
        credential_state_fingerprint: str,
        runtime_image_digest: str | None = None,
    ) -> dict[str, object] | None:
        """Validate and project the currently published Adapter release.

        This is the read-only catalog boundary.  A Connector must never infer
        availability from a launcher or an npm manifest alone: only a release
        that satisfies the same Toolchain contract used at execution time is
        projected as installed.
        """

        if runtime_image_digest is None:
            raise ValueError("shared Node inspection requires the current immutable runtime image digest")
        try:
            ref = self.resolve_for_adapter(
                adapter_id=adapter_id,
                spec=spec,
                adapter_contract_fingerprint=adapter_contract_fingerprint,
                credential_state_fingerprint=credential_state_fingerprint,
                runtime_image_digest=runtime_image_digest,
            )
            manifest = self.software.validate_node_release(ref.host_path, runtime_image_digest)
            if manifest is None:
                return None
            package = manifest["packages"][spec.package]
            return {
                "revision": ref.host_path.name,
                "version": package.get("version"),
                "integrity": package.get("registry_integrity"),
                "runtime_image_digest": runtime_image_digest,
            }
        except (OSError, KeyError, TypeError) as exc:
            raise ValueError("shared Node current release is invalid") from exc

    def list_revisions(
        self,
        *,
        adapter_id: str,
        spec: ToolchainPackageSpec,
        adapter_contract_fingerprint: str,
        credential_state_fingerprint: str,
        runtime_image_digest: str,
    ) -> list[dict[str, object]]:
        ref = self.resolve_node(adapter_id)
        current = (ref.root_path / "current").resolve().name
        revisions: list[dict[str, object]] = []
        for release in sorted((ref.root_path / "releases").iterdir(), reverse=True):
            if re.fullmatch(r"[0-9a-f]{64}", release.name) is None:
                continue
            try:
                manifest = self.software.validate_node_release(release, runtime_image_digest)
                assert manifest is not None
                package = manifest["packages"][spec.package]
                owners = {str(item) for item in package.get("requested_by") or []}
                bins = package.get("declared_bins")
                version = str(package.get("version") or "")
                integrity = str(package.get("registry_integrity") or "")
                if (
                    f"integration:{adapter_id}" not in owners
                    or not isinstance(bins, dict)
                    or spec.executable not in bins
                    or (spec.compatibility and not version_satisfies(version, spec.compatibility))
                    or (spec.expected_integrity and integrity != spec.expected_integrity)
                ):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            revisions.append(
                {
                    "revision": release.name,
                    "current": release.name == current,
                    "package": spec.package,
                    "version": package.get("version"),
                    "integrity": package.get("registry_integrity"),
                    "installed_at": manifest.get("published_at"),
                    "runtime_image_digest": runtime_image_digest,
                    "release_tree_sha256": manifest.get("release_tree_sha256"),
                }
            )
        return revisions

    def store_rollback_plan(self, adapter_id: str, plan: dict[str, object]) -> None:
        plan_id = str(plan.get("plan_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise ValueError("invalid Toolchain rollback plan id")
        ref = self.resolve_node(adapter_id)
        plans = ref.root_path / "rollback-plans"
        plans.mkdir(mode=0o700, exist_ok=True)
        _atomic_write_private(
            plans / f"{plan_id}.json",
            json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n",
        )
        descriptor = os.open(plans, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def consume_rollback_plan(
        self,
        adapter_id: str,
        plan_id: str,
        binding: str,
    ) -> dict[str, object]:
        if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise ValueError("invalid Toolchain rollback plan id")
        ref = self.resolve_node(adapter_id)
        plans = ref.root_path / "rollback-plans"
        source = plans / f"{plan_id}.json"
        consumed = plans / f".{plan_id}-{uuid.uuid4().hex}.consumed"
        try:
            preview = json.loads(_read_contained_text(plans, source))
            if not isinstance(preview, dict) or preview.get("binding") != binding:
                raise ValueError("Toolchain rollback approval binding is invalid")
            os.replace(source, consumed)
            value = json.loads(_read_contained_text(plans, consumed))
        except FileNotFoundError as exc:
            raise ValueError("Toolchain rollback plan is missing or already consumed") from exc
        finally:
            consumed.unlink(missing_ok=True)
        if not isinstance(value, dict):
            raise ValueError("Toolchain rollback plan is invalid")
        return value

    def record_event(self, adapter_id: str, event: dict[str, object]) -> None:
        ref = self.resolve_node(adapter_id)
        events = ref.root_path / "events"
        events.mkdir(mode=0o700, exist_ok=True)
        event_id = f"{time.time_ns()}-{uuid.uuid4().hex}"
        _atomic_write_private(
            events / f"{event_id}.json",
            json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
        )
        descriptor = os.open(events, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def acquire_revision_lease(
        self,
        *,
        adapter_id: str,
        revision: str,
        owner_kind: str,
        owner_id: str,
        contract_fingerprint: str,
        ttl_seconds: int = 1800,
    ) -> tuple[str, float]:
        if owner_kind not in {"plan", "execution", "runner", "rollback"}:
            raise ValueError("invalid Toolchain revision lease owner kind")
        if not owner_id or ttl_seconds < 30 or ttl_seconds > 86_400:
            raise ValueError("invalid Toolchain revision lease")
        ref = self.resolve_node(adapter_id)
        lock = FileLock(str(ref.root_path / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            release = ref.root_path / "releases" / revision
            if revision != "empty" and not (release / "runtime-manifest.json").is_file():
                raise ValueError("Toolchain revision lease target is unavailable")
            lease_id = uuid.uuid4().hex
            expires_at = time.time() + ttl_seconds
            leases = ref.root_path / "leases"
            leases.mkdir(mode=0o700, exist_ok=True)
            _fsync_directory(ref.root_path)
            _atomic_write_private(
                leases / f"{lease_id}.json",
                json.dumps(
                    {
                        "version": 1,
                        "lease_id": lease_id,
                        "adapter_id": adapter_id,
                        "revision": revision,
                        "owner_kind": owner_kind,
                        "owner_id": owner_id,
                        "contract_fingerprint": contract_fingerprint,
                        "created_at": time.time(),
                        "expires_at": expires_at,
                    },
                    sort_keys=True,
                )
                + "\n",
            )
            _fsync_directory(leases)
            return lease_id, expires_at

    def renew_revision_lease(
        self,
        *,
        adapter_id: str,
        lease_id: str,
        revision: str,
        owner_kind: str,
        owner_id: str,
        contract_fingerprint: str,
        ttl_seconds: int,
    ) -> float:
        if not re.fullmatch(r"[0-9a-f]{32}", lease_id):
            raise ValueError("invalid Toolchain revision lease id")
        ref = self.resolve_node(adapter_id)
        lock = FileLock(str(ref.root_path / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            leases = ref.root_path / "leases"
            path = leases / f"{lease_id}.json"
            try:
                current = json.loads(_read_contained_text(leases, path))
            except (OSError, ValueError) as exc:
                raise ValueError("Toolchain revision lease is missing") from exc
            if (
                not isinstance(current, dict)
                or current.get("adapter_id") != adapter_id
                or current.get("revision") != revision
                or current.get("contract_fingerprint") != contract_fingerprint
            ):
                raise ValueError("Toolchain revision lease contract changed")
            expires_at = time.time() + ttl_seconds
            current.update(
                {
                    "owner_kind": owner_kind,
                    "owner_id": owner_id,
                    "renewed_at": time.time(),
                    "expires_at": expires_at,
                }
            )
            _atomic_write_private(
                path,
                json.dumps(current, sort_keys=True) + "\n",
            )
            _fsync_directory(leases)
            return expires_at

    def release_revision_lease(self, *, adapter_id: str, lease_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", lease_id):
            return
        ref = self.resolve_node(adapter_id)
        lock = FileLock(str(ref.root_path / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            leases = ref.root_path / "leases"
            (leases / f"{lease_id}.json").unlink(missing_ok=True)
            if leases.exists():
                _fsync_directory(leases)

    def release_revision_leases_by_owner(
        self,
        *,
        adapter_id: str,
        owner_kind: str,
        owner_id: str,
    ) -> None:
        ref = self.resolve_node(adapter_id)
        lock = FileLock(str(ref.root_path / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            leases = ref.root_path / "leases"
            if not leases.exists():
                return
            changed = False
            for path in leases.iterdir():
                if path.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                    continue
                try:
                    value = json.loads(_read_contained_text(leases, path))
                except (OSError, ValueError):
                    continue
                if (
                    isinstance(value, dict)
                    and value.get("adapter_id") == adapter_id
                    and value.get("owner_kind") == owner_kind
                    and value.get("owner_id") == owner_id
                ):
                    path.unlink(missing_ok=True)
                    changed = True
            if changed:
                _fsync_directory(leases)

    def gc_revisions(self, adapter_id: str) -> list[str]:
        """Validate leases but retain every shared release.

        Shared Node revisions are also mounted by ordinary Project and Skill
        runners, which do not yet publish durable revision leases. Deleting a
        release here would therefore race a live bind mount. Physical GC stays
        disabled until every consumer participates in the same lease protocol.
        """

        ref = self.resolve_node(adapter_id)
        lock = FileLock(str(ref.root_path / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            (ref.root_path / "current").resolve(strict=True).relative_to(
                (ref.root_path / "releases").resolve(strict=True)
            )
            leases = ref.root_path / "leases"
            now = time.time()
            if leases.exists():
                changed = False
                for path in leases.iterdir():
                    if path.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                        raise ValueError("malformed Toolchain lease prevents revision GC")
                    try:
                        value = json.loads(_read_contained_text(leases, path))
                    except (OSError, ValueError) as exc:
                        raise ValueError("invalid Toolchain lease prevents revision GC") from exc
                    if not isinstance(value, dict):
                        raise ValueError("invalid Toolchain lease prevents revision GC")
                    if float(value.get("expires_at") or 0) <= now:
                        path.unlink(missing_ok=True)
                        changed = True
                if changed:
                    _fsync_directory(leases)
        return []

    def rollback_node(
        self,
        *,
        adapter_id: str,
        release_id: str,
        spec: ToolchainPackageSpec,
        adapter_contract_fingerprint: str,
        credential_state_fingerprint: str,
        runtime_image_digest: str,
        expected_revision: str,
    ) -> ToolchainRef:
        """Atomically reactivate one verified retained revision."""

        if not re.fullmatch(r"[0-9a-f]{64}", release_id):
            raise ValueError("invalid Toolchain release id")
        ref = self.resolve_node(adapter_id)
        lock = FileLock(str(ref.root_path / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            current_revision = (ref.root_path / "current").resolve().name
            if current_revision != expected_revision:
                raise ValueError("Toolchain changed while rollback approval was pending")
            release = ref.root_path / "releases" / release_id
            manifest = self.software.validate_node_release(release, runtime_image_digest)
            assert manifest is not None
            package = manifest.get("packages", {}).get(spec.package)
            if not isinstance(package, dict):
                raise ValueError("Toolchain rollback target lacks the Adapter package")
            owners = {str(item) for item in package.get("requested_by") or []}
            bins = package.get("declared_bins")
            version = str(package.get("version") or "")
            if (
                f"integration:{adapter_id}" not in owners
                or not isinstance(bins, dict)
                or spec.executable not in bins
                or (spec.compatibility and not version_satisfies(version, spec.compatibility))
                or (spec.expected_integrity and package.get("registry_integrity") != spec.expected_integrity)
            ):
                raise ValueError("Toolchain rollback target is incompatible with the Adapter contract")
            next_current = ref.root_path / f".current-{uuid.uuid4().hex}"
            next_current.symlink_to(Path("releases") / release_id, target_is_directory=True)
            os.replace(next_current, ref.root_path / "current")
            try:
                root_descriptor = os.open(ref.root_path, os.O_RDONLY)
                try:
                    os.fsync(root_descriptor)
                finally:
                    os.close(root_descriptor)
            except OSError:
                pass
        return ToolchainRef(
            ecosystem="node",
            runtime_contract=ref.runtime_contract,
            host_path=release,
            root_path=ref.root_path,
            mount_path=ref.root_path / "current",
            container_path=ref.container_path,
        )
