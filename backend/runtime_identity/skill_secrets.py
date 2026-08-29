"""Encrypted environment Secret registry with Skill-content bindings."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

from runtime_identity.paths import PuddingClawPaths, safe_identity_component
from runtime_identity.profiles import (
    CredentialEnvelopeDecryptionError,
    CredentialVault,
    MasterKeyProvider,
)

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_DENIED_EXACT = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "NODE_PATH",
        "HOME",
        "TMPDIR",
        "SHELL",
        "IFS",
        "ENV",
        "BASH_ENV",
        "LD_PRELOAD",
    }
)


def validate_skill_secret_name(value: str) -> str:
    name = str(value or "").strip()
    if (
        _ENV_NAME.fullmatch(name) is None
        or name in _DENIED_EXACT
        or name.startswith(("DYLD_", "LD_", "PYTHON", "npm_config_", "NPM_CONFIG_"))
    ):
        raise ValueError("environment variable is not eligible for Skill Secret injection")
    return name


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class SkillSecretProjection:
    environment: dict[str, str]
    registry_revision: str
    binding_digest: str


class SkillSecretStore:
    """Keep values and bindings in one encrypted atomic owner envelope."""

    schema_version = 1
    provider = "skill-env"
    profile_id = "registry"

    def __init__(self, paths: PuddingClawPaths, owner_user_id: str) -> None:
        self.paths = paths
        self.owner_user_id = safe_identity_component(owner_user_id, field="owner_user_id")
        self.path = paths.skill_secret_registry(self.owner_user_id)
        self.lock = FileLock(str(self.path.parent / ".registry.lock"), thread_local=False)
        self.key_provider = MasterKeyProvider(self.paths, self.owner_user_id)
        self._vault: CredentialVault | None = None

    @property
    def vault(self) -> CredentialVault:
        if self._vault is None:
            key = self.key_provider.get_or_create()
            self._vault = CredentialVault(key)
        return self._vault

    @staticmethod
    def _empty() -> dict[str, object]:
        return {"version": 1, "revision": 0, "secrets": {}, "bindings": {}}

    def _read(self) -> tuple[dict[str, object], str]:
        try:
            envelope = self.path.read_bytes()
        except FileNotFoundError:
            return self._empty(), "missing"
        plaintext = None
        candidates = [self.vault]
        candidates.extend(CredentialVault(key) for key in self.key_provider.existing_keys())
        for candidate in candidates:
            try:
                plaintext = candidate.open(
                    envelope,
                    owner_user_id=self.owner_user_id,
                    provider=self.provider,
                    profile_id=self.profile_id,
                )
            except CredentialEnvelopeDecryptionError:
                continue
            self._vault = candidate
            break
        if plaintext is None:
            raise CredentialEnvelopeDecryptionError(
                "Skill Secret 无法解密，请重新录入该 Skill 所需的密钥。"
            )
        value = json.loads(plaintext.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("version") != self.schema_version
            or not isinstance(value.get("revision"), int)
            or not isinstance(value.get("secrets"), dict)
            or not isinstance(value.get("bindings"), dict)
        ):
            raise ValueError("Skill Secret registry is invalid")
        return value, hashlib.sha256(envelope).hexdigest()

    def _write(self, value: dict[str, object]) -> str:
        payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        envelope = self.vault.seal(
            payload,
            owner_user_id=self.owner_user_id,
            provider=self.provider,
            profile_id=self.profile_id,
        )
        _atomic_write(self.path, envelope)
        revision = hashlib.sha256(envelope).hexdigest()
        if self.path.read_bytes() != envelope:
            raise RuntimeError("Skill Secret registry write was not durable")
        return revision

    def _quarantine_unreadable_registry(self) -> None:
        if not self.path.is_file():
            return
        self.path.replace(self.path.with_name(f"{self.path.name}.unreadable-{time.time_ns()}"))

    def status(self, *, skill_id: str, skill_version: str, env_name: str) -> str:
        skill = safe_identity_component(skill_id, field="skill_id")
        name = validate_skill_secret_name(env_name)
        if not self.path.exists():
            return "missing"
        with self.lock.acquire(timeout=30):
            try:
                value, _revision = self._read()
            except CredentialEnvelopeDecryptionError:
                return "unreadable"
        secrets = value["secrets"]
        bindings = value["bindings"]
        binding = bindings.get(skill) if isinstance(bindings, dict) else None
        if isinstance(binding, dict) and binding.get("skill_version") == skill_version:
            names = binding.get("env_names")
            if isinstance(names, list) and name in names and isinstance(secrets, dict) and name in secrets:
                return "bound"
        return "reusable" if isinstance(secrets, dict) and name in secrets else "missing"

    def set_and_bind(
        self,
        *,
        skill_id: str,
        skill_version: str,
        env_name: str,
        secret_value: str,
    ) -> str:
        skill = safe_identity_component(skill_id, field="skill_id")
        name = validate_skill_secret_name(env_name)
        if not isinstance(secret_value, str) or not secret_value or "\x00" in secret_value:
            raise ValueError("Skill Secret value must be a non-empty string without NUL bytes")
        if len(secret_value.encode("utf-8")) > 64 * 1024:
            raise ValueError("Skill Secret value exceeds the size limit")
        with self.lock.acquire(timeout=30):
            try:
                value, _revision = self._read()
            except CredentialEnvelopeDecryptionError:
                self._quarantine_unreadable_registry()
                value = self._empty()
            secrets = dict(value["secrets"])
            bindings = dict(value["bindings"])
            secrets[name] = secret_value
            previous = bindings.get(skill)
            names = {
                str(item)
                for item in (previous.get("env_names") if isinstance(previous, dict) else []) or []
            }
            names.add(name)
            bindings[skill] = {
                "skill_version": skill_version,
                "env_names": sorted(names),
            }
            value.update(
                {
                    "revision": int(value["revision"]) + 1,
                    "secrets": secrets,
                    "bindings": bindings,
                }
            )
            return self._write(value)

    def bind_existing(
        self,
        *,
        skill_id: str,
        skill_version: str,
        env_name: str,
    ) -> str:
        skill = safe_identity_component(skill_id, field="skill_id")
        name = validate_skill_secret_name(env_name)
        with self.lock.acquire(timeout=30):
            value, _revision = self._read()
            secrets = value["secrets"]
            if not isinstance(secrets, dict) or name not in secrets:
                raise ValueError("Skill Secret is unavailable for reuse")
            bindings = dict(value["bindings"])
            previous = bindings.get(skill)
            names = {
                str(item)
                for item in (previous.get("env_names") if isinstance(previous, dict) else []) or []
            }
            names.add(name)
            bindings[skill] = {
                "skill_version": skill_version,
                "env_names": sorted(names),
            }
            value.update({"revision": int(value["revision"]) + 1, "bindings": bindings})
            return self._write(value)

    def revoke_binding(self, *, skill_id: str, env_name: str) -> str:
        skill = safe_identity_component(skill_id, field="skill_id")
        name = validate_skill_secret_name(env_name)
        if not self.path.exists():
            return "missing"
        with self.lock.acquire(timeout=30):
            value, revision = self._read()
            bindings = dict(value["bindings"])
            binding = bindings.get(skill)
            if not isinstance(binding, dict):
                return revision
            names = [str(item) for item in binding.get("env_names") or [] if str(item) != name]
            if names:
                bindings[skill] = {**binding, "env_names": sorted(set(names))}
            else:
                bindings.pop(skill, None)
            value.update({"revision": int(value["revision"]) + 1, "bindings": bindings})
            return self._write(value)

    def projection(self, *, skill_id: str, skill_version: str) -> SkillSecretProjection:
        skill = safe_identity_component(skill_id, field="skill_id")
        if not self.path.exists():
            return SkillSecretProjection({}, "missing", "missing")
        with self.lock.acquire(timeout=30):
            value, revision = self._read()
        binding = value["bindings"].get(skill)
        if not isinstance(binding, dict) or binding.get("skill_version") != skill_version:
            return SkillSecretProjection({}, revision, "missing")
        names = binding.get("env_names")
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise ValueError("Skill Secret binding is invalid")
        secrets = value["secrets"]
        environment: dict[str, str] = {}
        for raw_name in names:
            name = validate_skill_secret_name(raw_name)
            secret = secrets.get(name)
            if not isinstance(secret, str) or not secret:
                raise ValueError("Skill Secret binding references a missing value")
            environment[name] = secret
        binding_digest = hashlib.sha256(
            json.dumps(
                {
                    "registry_revision": revision,
                    "skill_id": skill,
                    "skill_version": skill_version,
                    "env_names": sorted(environment),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return SkillSecretProjection(environment, revision, binding_digest)

    def revision_is_current(self, expected_revision: str) -> bool:
        if not self.path.exists():
            return expected_revision == "missing"
        with self.lock.acquire(timeout=30):
            _value, revision = self._read()
        return revision == expected_revision
