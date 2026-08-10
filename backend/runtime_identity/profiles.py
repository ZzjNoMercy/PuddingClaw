"""User credential profile registry and encrypted provider state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from filelock import FileLock

from runtime_identity.adapters import CredentialStateSpec
from runtime_identity.paths import PuddingClawPaths, safe_identity_component

MAX_CREDENTIAL_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_CREDENTIAL_ARCHIVE_FILES = 2_000


def validate_credential_archive(
    payload: bytes,
    *,
    allowed_roots: tuple[str, ...],
) -> bytes:
    """Reject entries outside one Adapter's exact credential-state roots."""

    if not allowed_roots:
        raise ValueError("credential state archive requires an Adapter path allow-list")
    normalized_roots: tuple[tuple[str, ...], ...] = tuple(PurePosixPath(root).parts for root in allowed_roots)
    if any(
        not root
        or root.startswith("/")
        or "\\" in root
        or any(ord(character) < 32 or ord(character) == 127 for character in root)
        or not parts
        or parts == (".",)
        or any(part in {"", ".", ".."} for part in parts)
        or any(part.startswith("-") for part in parts)
        or PurePosixPath(*parts).as_posix() != root
        for root, parts in zip(allowed_roots, normalized_roots, strict=True)
    ):
        raise ValueError("credential state archive has an invalid Adapter path allow-list")
    if len(set(normalized_roots)) != len(normalized_roots):
        raise ValueError("credential state archive Adapter path allow-list contains duplicates")
    for index, root in enumerate(normalized_roots):
        for other in normalized_roots[index + 1 :]:
            if root[: len(other)] == other or other[: len(root)] == root:
                raise ValueError("credential state archive Adapter path allow-list overlaps")
    if not payload:
        return payload
    if len(payload) > MAX_CREDENTIAL_ARCHIVE_BYTES:
        raise ValueError("credential state archive exceeds the size limit")
    with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_CREDENTIAL_ARCHIVE_FILES:
            raise ValueError("credential state archive contains too many entries")
        total_size = 0
        seen_names: set[str] = set()
        for member in members:
            parts = PurePosixPath(member.name).parts
            allowed = any(parts[: len(root)] == root for root in normalized_roots)
            is_root = any(parts == root for root in normalized_roots)
            if (
                not parts
                or not allowed
                or member.name.startswith("/")
                or "\\" in member.name
                or any(ord(character) < 32 or ord(character) == 127 for character in member.name)
                or PurePosixPath(*parts).as_posix() != member.name
                or ".." in parts
                or member.name in seen_names
                or not (member.isdir() or member.isfile())
                or (is_root and not member.isdir())
            ):
                raise ValueError("credential state archive contains an unsafe entry")
            seen_names.add(member.name)
            total_size += max(0, int(member.size))
            if total_size > MAX_CREDENTIAL_ARCHIVE_BYTES:
                raise ValueError("credential state archive expands beyond the size limit")
    return payload


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class MasterKeyProvider:
    """Load a per-user vault key from Keychain, with a 0600 fallback."""

    service = "PuddingClaw Credential Vault"

    def __init__(self, paths: PuddingClawPaths, owner_user_id: str) -> None:
        self.paths = paths
        self.owner_user_id = safe_identity_component(owner_user_id, field="owner_user_id")

    def get_or_create(self) -> bytes:
        lock_root = self.paths.root / ".vault-keys"
        lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(lock_root, 0o700)
        lock = FileLock(str(lock_root / f".{self.owner_user_id}.master-key.lock"), thread_local=False)
        with lock.acquire(timeout=30):
            return self._get_or_create_locked()

    def _get_or_create_locked(self) -> bytes:
        if sys.platform == "darwin":
            try:
                return self._get_or_create_keychain_key()
            except (OSError, subprocess.TimeoutExpired):
                # Headless macOS services may not have an unlocked Keychain.
                # The local fallback remains private to this owner and never
                # shares a directory with encrypted provider state.
                pass
        fallback = self.paths.root / ".vault-keys" / f"{self.owner_user_id}.key"
        fallback.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(fallback.parent, 0o700)
        try:
            value = fallback.read_bytes()
        except FileNotFoundError:
            value = secrets.token_bytes(32)
            _atomic_write(fallback, value)
        if len(value) != 32:
            raise ValueError("credential vault master key has an invalid length")
        os.chmod(fallback, 0o600)
        return value

    def _get_or_create_keychain_key(self) -> bytes:
        existing = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                self.owner_user_id,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            try:
                value = base64.urlsafe_b64decode(existing.stdout.strip().encode("ascii"))
            except (ValueError, UnicodeError):
                value = b""
            if len(value) == 32:
                return value
        generated = secrets.token_bytes(32)
        encoded = base64.urlsafe_b64encode(generated).decode("ascii")
        stored = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                self.owner_user_id,
                "-w",
                encoded,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if stored.returncode == 0:
            return generated
        raise OSError("macOS Keychain is unavailable")


class CredentialVault:
    version = 1

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._cipher = AESGCM(key)

    def seal(self, plaintext: bytes, *, owner_user_id: str, provider: str, profile_id: str) -> bytes:
        nonce = secrets.token_bytes(12)
        aad = self._aad(owner_user_id, provider, profile_id)
        ciphertext = self._cipher.encrypt(nonce, plaintext, aad)
        envelope = {
            "version": self.version,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def open(self, envelope: bytes, *, owner_user_id: str, provider: str, profile_id: str) -> bytes:
        value = json.loads(envelope.decode("utf-8"))
        if value.get("version") != self.version or value.get("algorithm") != "AES-256-GCM":
            raise ValueError("unsupported credential vault envelope")
        return self._cipher.decrypt(
            base64.b64decode(value["nonce"], validate=True),
            base64.b64decode(value["ciphertext"], validate=True),
            self._aad(owner_user_id, provider, profile_id),
        )

    def seal_flow(
        self,
        plaintext: bytes,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        flow_id: str,
    ) -> bytes:
        return self._seal_with_aad(
            plaintext,
            self._flow_aad(owner_user_id, provider, profile_id, flow_id),
        )

    def open_flow(
        self,
        envelope: bytes,
        *,
        owner_user_id: str,
        provider: str,
        profile_id: str,
        flow_id: str,
    ) -> bytes:
        return self._open_with_aad(
            envelope,
            self._flow_aad(owner_user_id, provider, profile_id, flow_id),
        )

    def _seal_with_aad(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext, aad)
        envelope = {
            "version": self.version,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _open_with_aad(self, envelope: bytes, aad: bytes) -> bytes:
        value = json.loads(envelope.decode("utf-8"))
        if value.get("version") != self.version or value.get("algorithm") != "AES-256-GCM":
            raise ValueError("unsupported credential vault envelope")
        return self._cipher.decrypt(
            base64.b64decode(value["nonce"], validate=True),
            base64.b64decode(value["ciphertext"], validate=True),
            aad,
        )

    @staticmethod
    def _aad(owner_user_id: str, provider: str, profile_id: str) -> bytes:
        return f"puddingclaw:v1:{owner_user_id}:{provider}:{profile_id}".encode()

    @staticmethod
    def _flow_aad(owner_user_id: str, provider: str, profile_id: str, flow_id: str) -> bytes:
        return f"puddingclaw:auth-flow:v1:{owner_user_id}:{provider}:{profile_id}:{flow_id}".encode()


class CredentialProfileStore:
    """Persist non-secret profile metadata separately from encrypted state."""

    def __init__(
        self,
        paths: PuddingClawPaths,
        owner_user_id: str,
        *,
        vault: CredentialVault | None = None,
    ) -> None:
        self.paths = paths
        self.owner_user_id = safe_identity_component(owner_user_id, field="owner_user_id")
        self.root = paths.credentials_root(self.owner_user_id)
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            paths.root,
            paths.root / "users",
            paths.root / "users" / self.owner_user_id,
            self.root,
        ):
            os.chmod(directory, 0o700)
        self.registry_path = self.root / "credential-profiles.json"
        self.bindings_path = self.root / "project-bindings.json"
        self._registry_lock = FileLock(str(self.root / ".registry.lock"))
        self._vault = vault

    @property
    def vault(self) -> CredentialVault:
        if self._vault is None:
            self._vault = CredentialVault(MasterKeyProvider(self.paths, self.owner_user_id).get_or_create())
        return self._vault

    def resolve(
        self,
        provider: str,
        *,
        project_id: str | None = None,
        explicit_profile_id: str | None = None,
        create_default: bool = True,
    ) -> dict[str, Any] | None:
        provider = safe_identity_component(provider, field="provider")
        with self._registry_lock:
            registry = self._read_json(self.registry_path, {"version": 1, "profiles": [], "defaults": {}})
            profiles = [item for item in registry.get("profiles", []) if isinstance(item, dict)]
            requested = explicit_profile_id
            if requested is None and project_id:
                bindings = self._read_json(self.bindings_path, {"version": 1, "projects": {}})
                project = bindings.get("projects", {}).get(project_id, {})
                if isinstance(project, dict):
                    requested = project.get(provider)
            if requested is None:
                requested = registry.get("defaults", {}).get(provider)
            if requested is not None:
                requested = safe_identity_component(str(requested), field="profile_id")
                match = next(
                    (
                        item
                        for item in profiles
                        if item.get("profile_id") == requested
                        and item.get("provider") == provider
                        and item.get("owner_user_id") == self.owner_user_id
                    ),
                    None,
                )
                if match is None:
                    raise ValueError("credential profile is missing or belongs to another user/provider")
                return dict(match)
            if not create_default:
                return None
            profile_id = safe_identity_component(f"{provider}_default", field="profile_id")
            now = time.time()
            profile = {
                "profile_id": profile_id,
                "owner_user_id": self.owner_user_id,
                "provider": provider,
                "label": "我的飞书" if provider == "lark" else f"{provider} default",
                "sharing_policy": "user",
                "status": "pending_configuration",
                "identities": {},
                "created_at": now,
                "updated_at": now,
            }
            profiles.append(profile)
            registry["profiles"] = profiles
            registry.setdefault("defaults", {})[provider] = profile_id
            self._write_json(self.registry_path, registry)
            return dict(profile)

    def list_profiles(self) -> list[dict[str, Any]]:
        """Return this owner's durable Profile inventory without creating one."""

        with self._registry_lock:
            registry = self._read_json(self.registry_path, {"version": 1, "profiles": [], "defaults": {}})
            return [
                dict(item)
                for item in registry.get("profiles", [])
                if isinstance(item, dict) and item.get("owner_user_id") == self.owner_user_id
            ]

    def bind_project(self, project_id: str, provider: str, profile_id: str) -> None:
        project = safe_identity_component(project_id, field="project_id")
        provider = safe_identity_component(provider, field="provider")
        profile = self.resolve(provider, explicit_profile_id=profile_id, create_default=False)
        if profile is None:
            raise ValueError("credential profile does not exist")
        with self._registry_lock:
            bindings = self._read_json(self.bindings_path, {"version": 1, "projects": {}})
            bindings.setdefault("projects", {}).setdefault(project, {})[provider] = profile["profile_id"]
            self._write_json(self.bindings_path, bindings)

    def create_profile(self, provider: str, profile_id: str, label: str) -> dict[str, Any]:
        provider = safe_identity_component(provider, field="provider")
        profile_id = safe_identity_component(profile_id, field="profile_id")
        with self._registry_lock:
            registry = self._read_json(self.registry_path, {"version": 1, "profiles": [], "defaults": {}})
            if any(
                isinstance(item, dict) and item.get("profile_id") == profile_id for item in registry.get("profiles", [])
            ):
                raise ValueError("credential profile already exists")
            now = time.time()
            profile = {
                "profile_id": profile_id,
                "owner_user_id": self.owner_user_id,
                "provider": provider,
                "label": str(label).strip() or profile_id,
                "sharing_policy": "user",
                "status": "pending_configuration",
                "identities": {},
                "created_at": now,
                "updated_at": now,
            }
            registry.setdefault("profiles", []).append(profile)
            self._write_json(self.registry_path, registry)
            return dict(profile)

    def update_status(self, profile_id: str, status: str) -> None:
        profile_id = safe_identity_component(profile_id, field="profile_id")
        with self._registry_lock:
            registry = self._read_json(self.registry_path, {"version": 1, "profiles": [], "defaults": {}})
            found = False
            for item in registry.get("profiles", []):
                if isinstance(item, dict) and item.get("profile_id") == profile_id:
                    if item.get("owner_user_id") != self.owner_user_id:
                        raise ValueError("credential profile owner mismatch")
                    item["status"] = str(status)
                    item["updated_at"] = time.time()
                    found = True
                    break
            if not found:
                raise ValueError("credential profile does not exist")
            self._write_json(self.registry_path, registry)

    def update_identity_status(
        self,
        profile_id: str,
        identity: str,
        status: str,
        *,
        reason: str | None = None,
        verified: bool | None = None,
        token_status: str | None = None,
    ) -> None:
        """Persist a non-secret assessment for one provider identity.

        Bot/App readiness and User OAuth readiness have different lifetimes.
        Keeping them separate prevents a failed user reauthorization Flow from
        disabling an otherwise healthy Bot identity (or vice versa).
        """

        profile_id = safe_identity_component(profile_id, field="profile_id")
        identity = safe_identity_component(identity, field="identity")
        # Identity keys are Driver-owned safe identifiers (for example
        # ``bot``/``user`` or a single ``account``).  The Store persists
        # assessments but does not impose one Provider's identity topology.
        normalized_status = safe_identity_component(status, field="identity_status")
        now = time.time()
        with self._registry_lock:
            registry = self._read_json(self.registry_path, {"version": 1, "profiles": [], "defaults": {}})
            for item in registry.get("profiles", []):
                if not isinstance(item, dict) or item.get("profile_id") != profile_id:
                    continue
                if item.get("owner_user_id") != self.owner_user_id:
                    raise ValueError("credential profile owner mismatch")
                identities = item.setdefault("identities", {})
                if not isinstance(identities, dict):
                    identities = {}
                    item["identities"] = identities
                assessment: dict[str, Any] = {
                    "status": normalized_status,
                    "updated_at": now,
                }
                if reason:
                    assessment["reason"] = str(reason)[:256]
                if verified is not None:
                    assessment["verified"] = bool(verified)
                if token_status:
                    assessment["token_status"] = safe_identity_component(
                        token_status,
                        field="token_status",
                    )
                identities[identity] = assessment
                item["updated_at"] = now
                self._write_json(self.registry_path, registry)
                return
        raise ValueError("credential profile does not exist")

    def begin_browser_job(
        self,
        profile_id: str,
        browser_job_id: str,
        credential_state_fingerprint: str,
    ) -> dict[str, Any]:
        """Acquire a durable Profile lease for one browser authorization job."""

        profile_id = safe_identity_component(profile_id, field="profile_id")
        browser_job_id = safe_identity_component(browser_job_id, field="browser_job_id")
        if not re.fullmatch(r"[0-9a-f]{64}", credential_state_fingerprint):
            raise ValueError("credential state fingerprint is invalid")
        with self._registry_lock:
            registry = self._read_json(self.registry_path, {"version": 1, "profiles": [], "defaults": {}})
            for item in registry.get("profiles", []):
                if not isinstance(item, dict) or item.get("profile_id") != profile_id:
                    continue
                if item.get("owner_user_id") != self.owner_user_id:
                    raise ValueError("credential profile owner mismatch")
                existing = item.get("browser_job_id")
                if existing and existing != browser_job_id:
                    raise ValueError("credential profile already has another browser authorization job")
                item["browser_job_id"] = browser_job_id
                item["browser_job_status"] = "awaiting_user_browser"
                item["credential_state_fingerprint"] = credential_state_fingerprint
                item["updated_at"] = time.time()
                self._write_json(self.registry_path, registry)
                return dict(item)
        raise ValueError("credential profile does not exist")

    def finish_browser_job(
        self,
        profile_id: str,
        browser_job_id: str,
        status: str,
        credential_state_fingerprint: str,
    ) -> bool:
        """Release a browser lease only when its durable identity still matches."""

        profile_id = safe_identity_component(profile_id, field="profile_id")
        browser_job_id = safe_identity_component(browser_job_id, field="browser_job_id")
        with self._registry_lock:
            registry = self._read_json(self.registry_path, {"version": 1, "profiles": [], "defaults": {}})
            for item in registry.get("profiles", []):
                if not isinstance(item, dict) or item.get("profile_id") != profile_id:
                    continue
                if item.get("owner_user_id") != self.owner_user_id:
                    raise ValueError("credential profile owner mismatch")
                if item.get("browser_job_id") != browser_job_id:
                    return False
                if item.get("credential_state_fingerprint") != credential_state_fingerprint:
                    return False
                item.pop("browser_job_id", None)
                item.pop("browser_job_status", None)
                item.pop("credential_state_fingerprint", None)
                item["last_browser_job_status"] = str(status)
                item["updated_at"] = time.time()
                self._write_json(self.registry_path, registry)
                return True
        return False

    def read_state(
        self,
        provider: str,
        profile_id: str,
        *,
        credential_state: CredentialStateSpec,
    ) -> bytes:
        directory = self.paths.provider_profile(self.owner_user_id, provider, profile_id)
        try:
            envelope = (directory / "vault.enc").read_bytes()
        except FileNotFoundError:
            return b""
        metadata = self._read_json(directory / "profile.json", {})
        stored_fingerprint = metadata.get("credential_state_fingerprint")
        if stored_fingerprint is not None and stored_fingerprint != credential_state.fingerprint:
            raise ValueError("credential profile state contract does not match the active Adapter")
        return validate_credential_archive(
            self.vault.open(
                envelope,
                owner_user_id=self.owner_user_id,
                provider=provider,
                profile_id=profile_id,
            ),
            allowed_roots=credential_state.paths,
        )

    def write_state(
        self,
        provider: str,
        profile_id: str,
        payload: bytes,
        *,
        credential_state: CredentialStateSpec,
    ) -> None:
        payload = validate_credential_archive(payload, allowed_roots=credential_state.paths)
        directory = self.paths.provider_profile(self.owner_user_id, provider, profile_id)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        envelope = self.vault.seal(
            payload,
            owner_user_id=self.owner_user_id,
            provider=provider,
            profile_id=profile_id,
        )
        metadata = {
            "profile_id": profile_id,
            "owner_user_id": self.owner_user_id,
            "provider": provider,
            "vault_format": "tar.gz+AES-256-GCM",
            "credential_state_paths": list(credential_state.paths),
            "credential_state_schema_version": credential_state.schema_version,
            "credential_state_fingerprint": credential_state.fingerprint,
            "updated_at": time.time(),
        }
        # Metadata contains no secret and is prepared first. ``vault.enc`` is
        # the commit point: a metadata failure therefore cannot replace token
        # bytes while reporting that the write failed.
        self._write_json(directory / "profile.json", metadata)
        _atomic_write(directory / "vault.enc", envelope)

    def write_state_if_revision(
        self,
        provider: str,
        profile_id: str,
        payload: bytes,
        *,
        expected_revision: str,
        credential_state: CredentialStateSpec,
    ) -> str | None:
        """Replace encrypted state only when the caller's snapshot is current.

        Callers must hold ``profile_lock(provider, profile_id)`` across the
        provider execution and this commit. The comparison is repeated here so
        every writeback path gets the same fail-closed CAS boundary.
        """

        if self.state_revision(provider, profile_id) != expected_revision:
            return None
        self.write_state(
            provider,
            profile_id,
            payload,
            credential_state=credential_state,
        )
        revision = self.state_revision(provider, profile_id)
        if revision == "missing":
            raise RuntimeError("credential Vault writeback was not durable")
        return revision

    def read_state_metadata(self, provider: str, profile_id: str) -> dict[str, Any] | None:
        """Return non-secret Vault metadata when a durable state archive exists."""

        directory = self.paths.provider_profile(self.owner_user_id, provider, profile_id)
        if not (directory / "vault.enc").is_file() or not (directory / "profile.json").is_file():
            return None
        return self._read_json(directory / "profile.json", {})

    def state_revision(self, provider: str, profile_id: str) -> str:
        """Return a stable CAS token for the encrypted Profile Vault contents."""

        directory = self.paths.provider_profile(self.owner_user_id, provider, profile_id)
        try:
            envelope = (directory / "vault.enc").read_bytes()
        except FileNotFoundError:
            return "missing"
        return hashlib.sha256(envelope).hexdigest()

    @contextmanager
    def profile_lock(self, provider: str, profile_id: str, *, timeout: float = 30) -> Iterator[None]:
        directory = self.paths.provider_profile(self.owner_user_id, provider, profile_id)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        lock = FileLock(str(directory / ".profile.lock"), thread_local=False)
        with lock.acquire(timeout=timeout):
            yield

    @staticmethod
    def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return json.loads(json.dumps(default))
        if not isinstance(value, dict):
            raise ValueError(f"invalid JSON object in {path.name}")
        return value

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        _atomic_write(
            path,
            (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
