"""Declarative user-global runtimes for Skill and integration software.

This module deliberately owns software bytes only.  It never reads credential
profiles, Vault state, project dependency manifests, or the host PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock

from runtime_identity.paths import PuddingClawPaths, safe_identity_component

_NODE_EXACT = re.compile(
    r"^((?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)"
    r"@(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$"
)
_NODE_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_PYTHON_EXACT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?)"
    r"==([A-Za-z0-9][A-Za-z0-9.+!_-]*)$"
)
_INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]+={0,2}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_BIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def parse_exact_node_distribution(value: str) -> tuple[str, str]:
    match = _NODE_EXACT.fullmatch(str(value or "").strip())
    if match is None:
        raise ValueError("Node Skill dependencies require an exact package@version selector")
    return match.group(1), match.group(2)


def parse_exact_python_requirement(value: str) -> tuple[str, str]:
    match = _PYTHON_EXACT.fullmatch(str(value or "").strip())
    if match is None:
        raise ValueError("Python Skill dependencies require an exact package==version selector")
    return match.group(1), match.group(2)


def _validate_python_lock(lock_text: str) -> dict[str, str]:
    """Accept only hash-locked exact requirements from the fixed PyPI index."""

    logical_entries: list[str] = []
    pending = ""
    for raw_line in lock_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("--index-url "):
            if pending or logical_entries or stripped != "--index-url https://pypi.org/simple":
                raise ValueError("Python lock contains an unsupported index")
            continue
        if not pending and stripped.startswith("--"):
            raise ValueError("Python lock contains an unsupported resolver option")
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_entries.append(pending)
        pending = ""
    if pending or not logical_entries:
        raise ValueError("Python lock is incomplete")
    locked: dict[str, str] = {}
    for entry in logical_entries:
        parts = entry.split()
        name_with_extras, version = parse_exact_python_requirement(parts[0])
        hashes = parts[1:]
        if not hashes or any(re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", value) is None for value in hashes):
            raise ValueError("Python lock entry is not fully hash-locked")
        identity = name_with_extras.split("[", 1)[0].lower().replace("_", "-").replace(".", "-")
        if identity in locked and locked[identity] != version:
            raise ValueError("Python lock contains multiple versions of one package")
        locked[identity] = version
    return locked


def skill_content_version(skill_root: Path) -> str:
    """Fingerprint the complete immutable Skill input consumed by its env."""

    skill_root = skill_root.expanduser()
    if skill_root.is_symlink():
        raise ValueError("Skill root is invalid")
    skill_root = skill_root.resolve(strict=True)
    if not (skill_root / "SKILL.md").is_file():
        raise ValueError("Skill root is invalid")
    digest = hashlib.sha256()
    for path in sorted(skill_root.rglob("*"), key=lambda item: item.relative_to(skill_root).as_posix()):
        if path.is_symlink():
            raise ValueError("Skill contains an unsupported symlink")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(skill_root).as_posix().encode() + b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return f"sha256-{digest.hexdigest()[:24]}"


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            directories.append(path)
            continue
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)
    _fsync_directory(root.parent)


def _tree_digest(root: Path, *, excluded: frozenset[str]) -> str:
    digest = hashlib.sha256()
    canonical_root = root.resolve()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        digest.update(relative.encode() + b"\0")
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            resolved.relative_to(canonical_root)
            digest.update(b"L\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file():
            digest.update(b"F\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"D\0")
        else:
            raise ValueError("runtime release contains an unsupported filesystem entry")
    return digest.hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"runtime evidence is missing: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"runtime evidence is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"runtime evidence must be an object: {path.name}")
    return value


def _switch_current(root: Path, release_name: str) -> None:
    temporary = root / f".current-{uuid.uuid4().hex}"
    temporary.symlink_to(Path("releases") / release_name, target_is_directory=True)
    os.replace(temporary, root / "current")
    _fsync_directory(root)


def _switch_target(root: Path, target: Path) -> None:
    temporary = root / f".current-{uuid.uuid4().hex}"
    temporary.symlink_to(os.path.relpath(target, root), target_is_directory=True)
    os.replace(temporary, root / "current")
    _fsync_directory(root)


def _best_effort_event(root: Path, value: dict[str, Any]) -> None:
    try:
        events = root / "events"
        events.mkdir(mode=0o700, exist_ok=True)
        _atomic_write(
            events / f"{time.time_ns()}-{uuid.uuid4().hex}.json",
            _canonical_json(value),
        )
        _fsync_directory(events)
    except OSError:
        # Publication already committed.  An audit I/O failure must not turn
        # a known-successful transaction into a caller-visible failure.
        return


@dataclass(frozen=True)
class RuntimeInstallResult:
    output: str
    exit_code: int
    runtime_kind: str
    revision: str | None = None
    previous_revision: str | None = None
    runtime_image_digest: str | None = None
    changed: bool = False


class SoftwareRuntimeManager:
    """Publish Node desired sets and isolated Python Skill environments."""

    def __init__(self, paths: PuddingClawPaths, runtime_contract: str) -> None:
        self.paths = paths
        self.runtime_contract = str(runtime_contract or "").strip()
        if not self.runtime_contract:
            raise ValueError("runtime_contract must be non-empty")

    def _initialize_node_root(self) -> Path:
        root = self.paths.shared_node_runtime(self.runtime_contract)
        if root.is_symlink():
            raise ValueError("shared Node runtime root must not be a symlink")
        releases = root / "releases"
        releases.mkdir(parents=True, exist_ok=True, mode=0o700)
        if releases.is_symlink():
            raise ValueError("shared Node releases root must not be a symlink")
        current = root / "current"
        if not os.path.lexists(current):
            if any(releases.iterdir()):
                raise ValueError("shared Node runtime is missing its current pointer")
            empty = releases / "empty"
            empty.mkdir(mode=0o700, exist_ok=True)
            for directory in (empty / "bin", empty / "public-bin", empty / "node_modules", empty / "lib"):
                directory.mkdir(mode=0o700, exist_ok=True)
            modules_alias = empty / "lib" / "node_modules"
            if not os.path.lexists(modules_alias):
                modules_alias.symlink_to(Path("..") / "node_modules", target_is_directory=True)
            desired = {
                "schema_version": 1,
                "revision": 0,
                "runtime_contract": self.runtime_contract,
                "packages": {},
            }
            _atomic_write(empty / "desired-packages.json", _canonical_json(desired))
            _fsync_tree(empty)
            _switch_current(root, "empty")
        elif not current.is_symlink() or not current.exists():
            raise ValueError("shared Node current pointer is invalid")
        alias = root / "desired-packages.json"
        if os.path.lexists(alias):
            if not alias.is_symlink() or os.readlink(alias) != "current/desired-packages.json":
                raise ValueError("shared Node desired registry alias is invalid")
        else:
            alias.symlink_to(Path("current") / "desired-packages.json")
            _fsync_directory(root)
        return root

    def _initialize_python_skill_root(self, skill_id: str, skill_version: str) -> Path:
        root = self.paths.python_skill_runtime(self.runtime_contract, skill_id, skill_version)
        if root.is_symlink():
            raise ValueError("Python Skill runtime root must not be a symlink")
        releases = root / "releases"
        releases.mkdir(parents=True, exist_ok=True, mode=0o700)
        if releases.is_symlink():
            raise ValueError("Python Skill releases root must not be a symlink")
        current = root / "current"
        if not os.path.lexists(current):
            if any(releases.iterdir()):
                raise ValueError("Python Skill runtime is missing its current pointer")
            empty = releases / "empty"
            empty.mkdir(mode=0o700, exist_ok=False)
            _fsync_tree(empty)
            _switch_current(root, "empty")
        elif not current.is_symlink() or not current.exists():
            raise ValueError("Python Skill current pointer is invalid")
        release = current.resolve(strict=True)
        if release.name == "empty":
            release.relative_to(releases.resolve(strict=True))
        else:
            environment_root = self._initialize_python_environment_root()
            release.relative_to((environment_root / "releases").resolve(strict=True))
        return root

    def _initialize_python_environment_root(self) -> Path:
        root = self.paths.python_environment_runtime(self.runtime_contract)
        if root.is_symlink():
            raise ValueError("Python environment store must not be a symlink")
        for name in ("releases", "plans"):
            directory = root / name
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise ValueError("Python environment store contains a symlinked control root")
        return root

    def node_current(self, runtime_image_digest: str | None = None) -> Path:
        """Return the fully validated shared Node release used by runners."""

        root = self._initialize_node_root()
        release = (root / "current").resolve(strict=True)
        self.validate_node_release(release, runtime_image_digest)
        return release

    def validate_node_release(
        self,
        release: Path,
        runtime_image_digest: str | None = None,
    ) -> dict[str, Any] | None:
        """Validate one shared Node revision and return its manifest."""

        root = self._initialize_node_root()
        release = release.expanduser().resolve(strict=True)
        release.relative_to((root / "releases").resolve(strict=True))
        if release.name == "empty":
            return None
        manifest = _read_json_object(release / "runtime-manifest.json")
        desired = _read_json_object(release / "desired-packages.json")
        lock_path = release / "package-lock.json"
        if (
            manifest.get("version") != 1
            or manifest.get("kind") != "shared-node-runtime"
            or manifest.get("runtime_contract") != self.runtime_contract
            or (runtime_image_digest is not None and manifest.get("runtime_image_digest") != runtime_image_digest)
            or manifest.get("desired_sha256") != hashlib.sha256(_canonical_json(desired)).hexdigest()
            or manifest.get("lock_sha256") != hashlib.sha256(lock_path.read_bytes()).hexdigest()
            or manifest.get("release_tree_sha256")
            != _tree_digest(release, excluded=frozenset({"runtime-manifest.json"}))
        ):
            raise ValueError("shared Node current release failed validation")
        packages = desired.get("packages")
        if not isinstance(packages, dict):
            raise ValueError("shared Node desired registry is invalid")
        self._validate_node_lock(release, packages)
        declared_bins = {
            name: tuple(str(item) for item in value.get("declared_bins") or [])
            for name, value in packages.items()
            if isinstance(value, dict)
        }
        projected = manifest.get("packages")
        if not isinstance(projected, dict) or set(projected) != set(packages):
            raise ValueError("shared Node package projection is invalid")
        expected_bin_names: set[str] = set()
        expected_public_bin_names: set[str] = set()
        for package_name, bins in declared_bins.items():
            recorded = projected.get(package_name)
            desired_package = packages[package_name]
            if (
                not isinstance(recorded, dict)
                or not isinstance(recorded.get("declared_bins"), dict)
                or recorded.get("version") != desired_package.get("version")
                or recorded.get("registry_integrity") != desired_package.get("registry_integrity")
                or sorted(recorded.get("requested_by") or [])
                != sorted(desired_package.get("requested_by") or [])
                or set(recorded.get("declared_bins") or {}) != set(bins)
            ):
                raise ValueError("shared Node executable projection is invalid")
            package_root = release / "node_modules" / Path(*package_name.split("/"))
            package_manifest = _read_json_object(package_root / "package.json")
            if (
                package_manifest.get("name") != package_name
                or package_manifest.get("version") != desired_package.get("version")
            ):
                raise ValueError("shared Node installed package identity is invalid")
            package_bins = self._package_bin_map(package_name, package_manifest)
            for executable in bins:
                relative = package_bins.get(executable)
                if not relative:
                    raise ValueError("shared Node package bin contract is invalid")
                package_target = (package_root / relative).resolve(strict=True)
                package_target.relative_to(package_root.resolve())
                launcher = release / "bin" / executable
                launcher_target = launcher.resolve(strict=True)
                if (
                    not launcher.is_symlink()
                    or launcher_target != package_target
                    or not launcher_target.is_file()
                    or recorded["declared_bins"].get(executable)
                    != package_target.relative_to(release).as_posix()
                ):
                    raise ValueError("shared Node executable projection is unavailable")
                expected_bin_names.add(executable)
                if any(str(owner).startswith("cli:") for owner in desired_package.get("requested_by") or []):
                    public_launcher = release / "public-bin" / executable
                    if (
                        not public_launcher.is_symlink()
                        or public_launcher.resolve(strict=True) != package_target
                    ):
                        raise ValueError("shared Node public executable projection is unavailable")
                    expected_public_bin_names.add(executable)
        bin_root = release / "bin"
        if bin_root.is_symlink() or {entry.name for entry in bin_root.iterdir()} != expected_bin_names:
            raise ValueError("shared Node executable directory contains an undeclared entry")
        public_bin_root = release / "public-bin"
        if (
            public_bin_root.is_symlink()
            or {entry.name for entry in public_bin_root.iterdir()} != expected_public_bin_names
        ):
            raise ValueError("shared Node public executable directory contains an undeclared entry")
        return manifest

    @staticmethod
    def _package_bin_map(package_name: str, package_manifest: dict[str, Any]) -> dict[str, str]:
        declared = package_manifest.get("bin")
        if isinstance(declared, str):
            return {package_name.rsplit("/", 1)[-1]: declared}
        if isinstance(declared, dict):
            return {str(key): str(value) for key, value in declared.items()}
        return {}

    @staticmethod
    def _node_desired(root: Path) -> tuple[str, dict[str, Any]]:
        current = (root / "current").resolve(strict=True)
        current.relative_to((root / "releases").resolve(strict=True))
        desired = _read_json_object(current / "desired-packages.json")
        if (
            desired.get("schema_version") != 1
            or not isinstance(desired.get("revision"), int)
            or not isinstance(desired.get("packages"), dict)
        ):
            raise ValueError("current Node desired registry is invalid")
        return current.name, desired

    @staticmethod
    def _apply_node_owner(
        desired: dict[str, Any],
        *,
        owner: str,
        owner_revision: str | None,
        exact_packages: dict[str, str],
        declared_bins: dict[str, tuple[str, ...]],
    ) -> dict[str, Any]:
        packages = json.loads(json.dumps(desired.get("packages") or {}))
        previous_owned = {
            package_name: json.loads(json.dumps(value))
            for package_name, value in packages.items()
            if isinstance(value, dict) and owner in {str(item) for item in value.get("requested_by") or []}
        }
        for package_name, value in list(packages.items()):
            if not isinstance(value, dict):
                raise ValueError("Node desired package entry is invalid")
            owners = [str(item) for item in value.get("requested_by") or [] if str(item) != owner]
            revisions = dict(value.get("owner_revisions") or {})
            revisions.pop(owner, None)
            if owners:
                value["requested_by"] = sorted(set(owners))
                value["owner_revisions"] = revisions
            else:
                packages.pop(package_name)
        for package_name, version in sorted(exact_packages.items()):
            existing = packages.get(package_name)
            if isinstance(existing, dict) and existing.get("version") != version:
                owners = ", ".join(str(item) for item in existing.get("requested_by") or [])
                raise ValueError(
                    f"shared Node dependency conflict for {package_name}: {version} conflicts with "
                    f"{existing.get('version')} requested by {owners or 'another owner'}"
                )
            bins = tuple(declared_bins.get(package_name) or ())
            if any(_BIN_NAME.fullmatch(item) is None for item in bins):
                raise ValueError("declared Node executable name is invalid")
            if existing is None:
                previous = previous_owned.get(package_name)
                previous_integrity = (
                    str(previous.get("registry_integrity") or "")
                    if isinstance(previous, dict) and previous.get("version") == version
                    else ""
                )
                packages[package_name] = {
                    "version": version,
                    "registry_integrity": previous_integrity,
                    "declared_bins": sorted(set(bins)),
                    "requested_by": [owner],
                    "owner_revisions": ({owner: owner_revision} if owner_revision else {}),
                }
            else:
                old_bins = {str(item) for item in existing.get("declared_bins") or []}
                if old_bins and old_bins != set(bins):
                    raise ValueError(f"declared executable contract changed for shared package {package_name}")
                existing["declared_bins"] = sorted(old_bins | set(bins))
                existing["requested_by"] = sorted(
                    set(str(item) for item in existing.get("requested_by") or []) | {owner}
                )
                revisions = dict(existing.get("owner_revisions") or {})
                if owner_revision:
                    revisions[owner] = owner_revision
                existing["owner_revisions"] = revisions
        claimed_bins: dict[str, str] = {}
        for package_name, value in packages.items():
            for executable in value.get("declared_bins") or []:
                previous = claimed_bins.setdefault(str(executable), package_name)
                if previous != package_name:
                    raise ValueError(
                        f"shared Node executable collision: {executable} is declared by {previous} and {package_name}"
                    )
        return packages

    @staticmethod
    def _validate_node_lock(
        resolution: Path,
        packages: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        package_json = _read_json_object(resolution / "package.json")
        lock_path = resolution / "package-lock.json"
        lock = _read_json_object(lock_path)
        dependencies = package_json.get("dependencies")
        expected = {name: str(value["version"]) for name, value in packages.items()}
        if dependencies != expected:
            raise ValueError("resolved Node package.json does not match desired exact versions")
        lock_packages = lock.get("packages")
        if not isinstance(lock_packages, dict) or int(lock.get("lockfileVersion") or 0) < 2:
            raise ValueError("resolved Node lockfile is incomplete")
        root_value = lock_packages.get("")
        if not isinstance(root_value, dict) or root_value.get("dependencies") != expected:
            raise ValueError("resolved Node lock root does not match desired packages")
        for install_path, locked in lock_packages.items():
            if install_path == "":
                continue
            if not isinstance(locked, dict) or locked.get("link") is True:
                raise ValueError(f"resolved Node lock contains an unsupported entry: {install_path}")
            version = str(locked.get("version") or "")
            integrity = str(locked.get("integrity") or "")
            resolved = str(locked.get("resolved") or "")
            if (
                _NODE_VERSION.fullmatch(version) is None
                or _INTEGRITY.fullmatch(integrity) is None
                or not resolved.startswith("https://registry.npmjs.org/")
            ):
                raise ValueError(f"resolved Node lock entry is not reproducible: {install_path}")
        for package_name, value in packages.items():
            package_value = lock_packages.get(f"node_modules/{package_name}")
            if not isinstance(package_value, dict) or package_value.get("version") != value["version"]:
                raise ValueError(f"resolved Node package identity mismatch for {package_name}")
            integrity = str(package_value.get("integrity") or "")
            if _INTEGRITY.fullmatch(integrity) is None:
                raise ValueError(f"resolved Node package integrity is missing for {package_name}")
            value["registry_integrity"] = integrity
        lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        return lock, lock_digest

    @staticmethod
    def _verify_node_release(
        release: Path,
        packages: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        canonical_release = release.resolve(strict=True)
        package_results: dict[str, dict[str, Any]] = {}
        bin_dir = release / "bin"
        bin_dir.mkdir(mode=0o700, exist_ok=True)
        public_bin_dir = release / "public-bin"
        public_bin_dir.mkdir(mode=0o700, exist_ok=True)
        modules_alias = release / "lib" / "node_modules"
        modules_alias.parent.mkdir(mode=0o700, exist_ok=True)
        if not os.path.lexists(modules_alias):
            modules_alias.symlink_to(Path("..") / "node_modules", target_is_directory=True)
        lock = _read_json_object(release / "package-lock.json")
        lock_packages = lock.get("packages")
        if not isinstance(lock_packages, dict):
            raise ValueError("installed Node lockfile is invalid")
        for package_name, desired in sorted(packages.items()):
            package_root = release / "node_modules" / Path(*package_name.split("/"))
            package_manifest = _read_json_object(package_root / "package.json")
            if package_manifest.get("name") != package_name or package_manifest.get("version") != desired["version"]:
                raise ValueError(f"installed Node package identity mismatch for {package_name}")
            lock_value = lock_packages.get(f"node_modules/{package_name}")
            if (
                not isinstance(lock_value, dict)
                or lock_value.get("version") != desired["version"]
                or lock_value.get("integrity") != desired["registry_integrity"]
            ):
                raise ValueError(f"installed Node integrity mismatch for {package_name}")
            declared_map = SoftwareRuntimeManager._package_bin_map(package_name, package_manifest)
            verified_bins: dict[str, str] = {}
            for executable in desired.get("declared_bins") or []:
                relative = declared_map.get(str(executable))
                if not relative:
                    raise ValueError(f"package {package_name} does not declare executable {executable}")
                target = (package_root / relative).resolve(strict=True)
                target.relative_to(package_root.resolve())
                if not target.is_file():
                    raise ValueError(f"declared executable {executable} is not a regular file")
                link = bin_dir / str(executable)
                link.symlink_to(os.path.relpath(target, link.parent.resolve(strict=True)))
                if any(str(owner).startswith("cli:") for owner in desired.get("requested_by") or []):
                    public_link = public_bin_dir / str(executable)
                    public_link.symlink_to(
                        os.path.relpath(target, public_link.parent.resolve(strict=True))
                    )
                verified_bins[str(executable)] = target.relative_to(canonical_release).as_posix()
            package_results[package_name] = {
                "version": desired["version"],
                "registry_integrity": desired["registry_integrity"],
                "declared_bins": verified_bins,
                "requested_by": list(desired.get("requested_by") or []),
            }
        return package_results

    def install_node_owner(
        self,
        backend: object,
        *,
        owner: str,
        distributions: list[str],
        declared_bins: dict[str, tuple[str, ...]] | None = None,
        owner_revision: str | None = None,
        expected_integrities: dict[str, str] | None = None,
        expected_runtime_image_digest: str | None = None,
        expected_base_revision: str | None = None,
        merge_owner: bool = False,
    ) -> RuntimeInstallResult:
        """Replace one owner's complete Node desired set and rebuild the tree."""

        owner = str(owner or "").strip()
        if not re.fullmatch(r"(?:skill|integration|cli|mcp):[A-Za-z0-9_.@/-]{1,220}", owner):
            raise ValueError("software runtime owner is invalid")
        exact: dict[str, str] = {}
        for distribution in distributions:
            package, version = parse_exact_node_distribution(distribution)
            if package in exact and exact[package] != version:
                raise ValueError(f"multiple exact versions were requested for {package}")
            exact[package] = version
        runtime_image_digest = str(backend.managed_runtime_image_digest())
        if _IMAGE_DIGEST.fullmatch(runtime_image_digest) is None:
            raise ValueError("managed runtime image did not resolve to an immutable digest")
        if expected_runtime_image_digest is not None and runtime_image_digest != expected_runtime_image_digest:
            return RuntimeInstallResult(
                output="Managed runtime image changed after installation approval.",
                exit_code=75,
                runtime_kind="node",
                runtime_image_digest=runtime_image_digest,
            )
        root = self._initialize_node_root()
        lock = FileLock(str(root / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            validated_current = self.node_current(runtime_image_digest)
            base_revision, current_desired = self._node_desired(root)
            if validated_current.name != base_revision:
                raise ValueError("shared Node current changed during validation")
            if expected_base_revision is not None and base_revision != expected_base_revision:
                return RuntimeInstallResult(
                    output="Shared Node runtime changed while installation approval was pending; re-plan.",
                    exit_code=75,
                    runtime_kind="node",
                    previous_revision=base_revision,
                )
            if merge_owner:
                for package_name, value in current_desired.get("packages", {}).items():
                    if not isinstance(value, dict) or owner not in value.get("requested_by", []):
                        continue
                    revisions = value.get("owner_revisions") or {}
                    if owner_revision and revisions.get(owner) != owner_revision:
                        continue
                    previous_version = str(value.get("version") or "")
                    requested_version = exact.get(package_name)
                    if requested_version is not None and requested_version != previous_version:
                        raise ValueError(
                            f"Skill dependency {package_name} was already discovered at {previous_version}; "
                            "changing it requires a new Skill content version"
                        )
                    exact.setdefault(package_name, previous_version)
            packages = self._apply_node_owner(
                current_desired,
                owner=owner,
                owner_revision=owner_revision,
                exact_packages=exact,
                declared_bins=declared_bins or {},
            )
            unchanged = packages == current_desired.get("packages")
            next_desired = {
                "schema_version": 1,
                "revision": int(current_desired["revision"]) + 1,
                "runtime_contract": self.runtime_contract,
                "packages": packages,
            }
        if unchanged:
            release = self.node_current(runtime_image_digest)
            desired = _read_json_object(release / "desired-packages.json")
            for package_name, expected_integrity in (expected_integrities or {}).items():
                package_value = desired.get("packages", {}).get(package_name)
                if not isinstance(package_value, dict) or package_value.get("registry_integrity") != expected_integrity:
                    raise ValueError(f"current Node integrity does not match the frozen plan for {package_name}")
            return RuntimeInstallResult(
                output="Shared Node runtime already matches the requested dependency set.",
                exit_code=0,
                runtime_kind="node",
                revision=base_revision,
                previous_revision=base_revision,
                runtime_image_digest=runtime_image_digest,
                changed=False,
            )
        plan_id = uuid.uuid4().hex
        plans = root / "plans"
        resolution = plans / plan_id
        resolution.mkdir(parents=True, mode=0o700, exist_ok=False)
        result = backend.resolve_shared_node_runtime(
            dependencies={name: value["version"] for name, value in packages.items()},
            expected_runtime_image_digest=runtime_image_digest,
            resolution_path=resolution,
        )
        if int(result.exit_code or 0) != 0:
            shutil.rmtree(resolution, ignore_errors=True)
            return RuntimeInstallResult(
                output=str(result.output),
                exit_code=int(result.exit_code or 1),
                runtime_kind="node",
                previous_revision=base_revision,
                runtime_image_digest=runtime_image_digest,
            )
        _lock_value, lock_digest = self._validate_node_lock(resolution, packages)
        for package_name, expected_integrity in (expected_integrities or {}).items():
            package_value = packages.get(package_name)
            if not isinstance(package_value, dict) or package_value.get("registry_integrity") != expected_integrity:
                shutil.rmtree(resolution, ignore_errors=True)
                raise ValueError(f"resolved Node integrity changed for {package_name}")
        next_desired["packages"] = packages
        _atomic_write(resolution / "desired-packages.json", _canonical_json(next_desired))
        plan_fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "base_revision": base_revision,
                    "desired": next_desired,
                    "lock_digest": lock_digest,
                    "runtime_image_digest": runtime_image_digest,
                }
            )
        ).hexdigest()
        _atomic_write(
            resolution / "runtime-plan.json",
            _canonical_json(
                {
                    "version": 1,
                    "plan_id": plan_id,
                    "plan_fingerprint": plan_fingerprint,
                    "base_revision": base_revision,
                    "lock_digest": lock_digest,
                    "runtime_image_digest": runtime_image_digest,
                }
            ),
        )
        with lock.acquire(timeout=300):
            observed_revision, _observed = self._node_desired(root)
            if observed_revision != base_revision:
                shutil.rmtree(resolution, ignore_errors=True)
                return RuntimeInstallResult(
                    output="Shared Node runtime changed while the dependency plan was being resolved; retry.",
                    exit_code=75,
                    runtime_kind="node",
                    previous_revision=observed_revision,
                    runtime_image_digest=runtime_image_digest,
                )
            release_name = hashlib.sha256(
                _canonical_json(next_desired) + (resolution / "package-lock.json").read_bytes()
            ).hexdigest()
            release = root / "releases" / release_name
            if release.exists():
                shutil.rmtree(resolution, ignore_errors=True)
                raise ValueError("a Node release already exists for this lock but was not current")
            build = backend.build_shared_node_runtime(
                expected_runtime_image_digest=runtime_image_digest,
                runtime_path=resolution,
                container_path="/opt/puddingclaw/runtime/node",
            )
            if int(build.exit_code or 0) != 0:
                _atomic_write(resolution / "INSTALL_FAILED", str(build.output).encode()[-20_000:])
                return RuntimeInstallResult(
                    output=str(build.output),
                    exit_code=int(build.exit_code or 1),
                    runtime_kind="node",
                    previous_revision=base_revision,
                    runtime_image_digest=runtime_image_digest,
                )
            if os.path.lexists(resolution / "INSTALL_FAILED"):
                raise ValueError("installer wrote a reserved runtime control path")
            if hashlib.sha256((resolution / "package-lock.json").read_bytes()).hexdigest() != lock_digest:
                raise ValueError("Node lockfile changed during isolated build")
            if _read_json_object(resolution / "desired-packages.json") != next_desired:
                raise ValueError("Node desired registry changed during isolated build")
            self._validate_node_lock(resolution, packages)
            if (
                os.path.lexists(resolution / "bin")
                or os.path.lexists(resolution / "public-bin")
                or os.path.lexists(resolution / "lib")
            ):
                raise ValueError("installer wrote a reserved executable projection path")
            verified_packages = self._verify_node_release(resolution, packages)
            manifest = {
                "version": 1,
                "kind": "shared-node-runtime",
                "runtime_contract": self.runtime_contract,
                "runtime_image_digest": runtime_image_digest,
                "desired_revision": next_desired["revision"],
                "desired_sha256": hashlib.sha256(_canonical_json(next_desired)).hexdigest(),
                "lock_sha256": lock_digest,
                "plan_fingerprint": plan_fingerprint,
                "packages": verified_packages,
                "published_at": time.time(),
            }
            manifest["release_tree_sha256"] = _tree_digest(
                resolution,
                excluded=frozenset({"runtime-manifest.json", "runtime-plan.json"}),
            )
            _atomic_write(resolution / "runtime-manifest.json", _canonical_json(manifest))
            (resolution / "runtime-plan.json").unlink(missing_ok=True)
            _fsync_tree(resolution)
            os.replace(resolution, release)
            _fsync_directory(release.parent)
            _switch_current(root, release_name)
        _best_effort_event(
            root,
            {
                "event": "node_runtime_published",
                "owner": owner,
                "from_revision": base_revision,
                "to_revision": release_name,
                "lock_sha256": lock_digest,
                "runtime_image_digest": runtime_image_digest,
                "completed_at": time.time(),
            },
        )
        return RuntimeInstallResult(
            output=str(build.output),
            exit_code=0,
            runtime_kind="node",
            revision=release_name,
            previous_revision=base_revision,
            runtime_image_digest=runtime_image_digest,
            changed=True,
        )

    def install_python_skill(
        self,
        backend: object,
        *,
        skill_id: str,
        skill_version: str,
        requirements: list[str],
    ) -> RuntimeInstallResult:
        """Build and publish one isolated, hash-locked uv Skill environment."""

        skill_id = safe_identity_component(skill_id, field="skill_id")
        skill_version = safe_identity_component(skill_version, field="skill_version")
        exact: dict[str, str] = {}
        normalized_requirements: list[str] = []
        for requirement in requirements:
            name_with_extras, version = parse_exact_python_requirement(requirement)
            normalized = f"{name_with_extras}=={version}"
            identity = name_with_extras.split("[", 1)[0].lower().replace("_", "-").replace(".", "-")
            if identity in exact and exact[identity] != normalized:
                raise ValueError(f"multiple exact versions were requested for Python package {identity}")
            exact[identity] = normalized
            normalized_requirements.append(normalized)
        normalized_requirements = sorted(set(normalized_requirements), key=str.lower)
        root = self._initialize_python_skill_root(skill_id, skill_version)
        current = root / "current"
        base_revision = current.resolve(strict=True).name
        python_runtime_identity = getattr(
            backend,
            "managed_python_runtime_image_digest",
            backend.managed_runtime_image_digest,
        )
        runtime_image_digest = str(python_runtime_identity())
        if _IMAGE_DIGEST.fullmatch(runtime_image_digest) is None:
            raise ValueError("managed runtime image did not resolve to an immutable digest")
        if base_revision != "empty":
            manifest = self._validate_python_environment_release(
                current.resolve(strict=True),
                runtime_image_digest=runtime_image_digest,
            )
            for previous in manifest.get("requirements") or []:
                previous_name, previous_version = parse_exact_python_requirement(str(previous))
                identity = previous_name.split("[", 1)[0].lower().replace("_", "-").replace(".", "-")
                requested = exact.get(identity)
                if requested is not None and requested != str(previous):
                    raise ValueError(
                        f"Skill dependency {identity} was already discovered at {previous_version}; "
                        "changing it requires a new Skill content version"
                    )
                if requested is None:
                    exact[identity] = str(previous)
                    normalized_requirements.append(str(previous))
            normalized_requirements = sorted(set(normalized_requirements), key=str.lower)
        if not normalized_requirements:
            return RuntimeInstallResult(
                output="No new Python Skill dependencies were supplied.",
                exit_code=0,
                runtime_kind="python-skill",
                revision=base_revision,
                previous_revision=base_revision,
                runtime_image_digest=runtime_image_digest,
                changed=False,
            )
        environment_root = self._initialize_python_environment_root()
        plan_id = uuid.uuid4().hex
        resolution = environment_root / "plans" / plan_id
        resolution.mkdir(parents=True, mode=0o700, exist_ok=False)
        _atomic_write(
            resolution / "requirements.in",
            ("\n".join(normalized_requirements) + "\n").encode(),
        )
        resolved = backend.resolve_python_skill_runtime(
            expected_runtime_image_digest=runtime_image_digest,
            resolution_path=resolution,
        )
        if int(resolved.exit_code or 0) != 0:
            shutil.rmtree(resolution, ignore_errors=True)
            return RuntimeInstallResult(
                output=str(resolved.output),
                exit_code=int(resolved.exit_code or 1),
                runtime_kind="python-skill",
                previous_revision=base_revision,
                runtime_image_digest=runtime_image_digest,
            )
        lock_path = resolution / "requirements.lock"
        lock_text = lock_path.read_text(encoding="utf-8")
        locked = _validate_python_lock(lock_text)
        for requirement in normalized_requirements:
            name, version = parse_exact_python_requirement(requirement)
            canonical_name = name.split("[", 1)[0].lower().replace("_", "-").replace(".", "-")
            if locked.get(canonical_name) != version:
                raise ValueError(f"Python lock does not contain requested exact package {requirement}")
        lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        release_name = lock_digest
        lock = FileLock(str(environment_root / ".install.lock"), thread_local=False)
        with lock.acquire(timeout=300):
            observed = current.resolve(strict=True).name
            if observed != base_revision:
                shutil.rmtree(resolution, ignore_errors=True)
                return RuntimeInstallResult(
                    output="Python Skill runtime changed while its lock was being resolved; retry.",
                    exit_code=75,
                    runtime_kind="python-skill",
                    previous_revision=observed,
                    runtime_image_digest=runtime_image_digest,
                )
            release = environment_root / "releases" / release_name
            if release.exists():
                shutil.rmtree(resolution, ignore_errors=True)
                manifest = self._validate_python_environment_release(
                    release,
                    runtime_image_digest=runtime_image_digest,
                )
                if manifest.get("lock_sha256") != lock_digest:
                    raise ValueError("existing Python Skill release contract is incompatible")
                _switch_target(root, release)
                return RuntimeInstallResult(
                    output="Python Skill runtime already exists and was activated.",
                    exit_code=0,
                    runtime_kind="python-skill",
                    revision=release_name,
                    previous_revision=base_revision,
                    runtime_image_digest=runtime_image_digest,
                    changed=base_revision != release_name,
                )
            built = backend.build_python_skill_runtime(
                expected_runtime_image_digest=runtime_image_digest,
                runtime_path=resolution,
                container_path="/opt/puddingclaw/runtime/python-skill",
                uv_cache_path=self.paths.python_uv_cache(),
            )
            if int(built.exit_code or 0) != 0:
                _atomic_write(resolution / "INSTALL_FAILED", str(built.output).encode()[-20_000:])
                return RuntimeInstallResult(
                    output=str(built.output),
                    exit_code=int(built.exit_code or 1),
                    runtime_kind="python-skill",
                    previous_revision=base_revision,
                    runtime_image_digest=runtime_image_digest,
                )
            if os.path.lexists(resolution / "INSTALL_FAILED"):
                raise ValueError("installer wrote a reserved runtime control path")
            if hashlib.sha256((resolution / "requirements.lock").read_bytes()).hexdigest() != lock_digest:
                raise ValueError("Python lockfile changed during isolated build")
            python = resolution / ".venv" / "bin" / "python"
            python_target = python.resolve(strict=True)
            python_target.relative_to(resolution.resolve())
            if python.is_symlink() or not python_target.is_file() or not os.access(python_target, os.X_OK):
                raise ValueError("published Python Skill environment has no executable interpreter")
            manifest = {
                "version": 1,
                "kind": "python-environment-runtime",
                "runtime_contract": self.runtime_contract,
                "runtime_image_digest": runtime_image_digest,
                "requirements": normalized_requirements,
                "lock_sha256": lock_digest,
                "published_at": time.time(),
            }
            manifest["release_tree_sha256"] = _tree_digest(
                resolution,
                excluded=frozenset({"runtime-manifest.json"}),
            )
            _atomic_write(resolution / "runtime-manifest.json", _canonical_json(manifest))
            _fsync_tree(resolution)
            os.replace(resolution, release)
            _fsync_directory(release.parent)
            _switch_target(root, release)
        _best_effort_event(
            root,
            {
                "event": "python_skill_runtime_published",
                "skill_id": skill_id,
                "skill_version": skill_version,
                "from_revision": base_revision,
                "to_revision": release_name,
                "runtime_image_digest": runtime_image_digest,
                "completed_at": time.time(),
            },
        )
        return RuntimeInstallResult(
            output=str(built.output),
            exit_code=0,
            runtime_kind="python-skill",
            revision=release_name,
            previous_revision=base_revision,
            runtime_image_digest=runtime_image_digest,
            changed=True,
        )

    def python_skill_current(
        self,
        skill_id: str,
        skill_version: str,
        runtime_image_digest: str | None = None,
    ) -> Path | None:
        root = self._initialize_python_skill_root(skill_id, skill_version)
        current = root / "current"
        release = current.resolve(strict=True)
        if release.name == "empty":
            return None
        self._validate_python_environment_release(
            release,
            runtime_image_digest=runtime_image_digest,
        )
        return release

    def _validate_python_environment_release(
        self,
        release: Path,
        *,
        runtime_image_digest: str | None,
    ) -> dict[str, Any]:
        release = release.expanduser().resolve(strict=True)
        environment_root = self._initialize_python_environment_root()
        release.relative_to((environment_root / "releases").resolve(strict=True))
        manifest = _read_json_object(release / "runtime-manifest.json")
        lock_path = release / "requirements.lock"
        requirements = manifest.get("requirements")
        if (
            manifest.get("version") != 1
            or manifest.get("kind") != "python-environment-runtime"
            or manifest.get("runtime_contract") != self.runtime_contract
            or (runtime_image_digest is not None and manifest.get("runtime_image_digest") != runtime_image_digest)
            or not isinstance(requirements, list)
            or manifest.get("lock_sha256") != hashlib.sha256(lock_path.read_bytes()).hexdigest()
            or _tree_digest(release, excluded=frozenset({"runtime-manifest.json"}))
            != manifest.get("release_tree_sha256")
        ):
            raise ValueError("Python Skill current release failed validation")
        lock_text = lock_path.read_text(encoding="utf-8")
        locked = _validate_python_lock(lock_text)
        for requirement in requirements:
            name, version = parse_exact_python_requirement(str(requirement))
            canonical_name = name.split("[", 1)[0].lower().replace("_", "-").replace(".", "-")
            if locked.get(canonical_name) != version:
                raise ValueError("Python Skill lock does not match its manifest")
        python = release / ".venv" / "bin" / "python"
        python_target = python.resolve(strict=True)
        python_target.relative_to(release)
        if python.is_symlink() or not python_target.is_file() or not os.access(python_target, os.X_OK):
            raise ValueError("Python Skill interpreter contract is invalid")
        return manifest
