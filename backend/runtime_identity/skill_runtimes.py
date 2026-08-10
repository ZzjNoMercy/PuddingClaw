"""Durable explicit runtime choices for ordinary installed Skills."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock

from runtime_identity.paths import PuddingClawPaths, safe_identity_component


def _atomic_write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            descriptor = -1
            json.dump(value, destination, sort_keys=True, separators=(",", ":"))
            destination.write("\n")
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


class SkillRuntimeBindingStore:
    """Bind a Skill content digest to host or explicit Docker execution."""

    def __init__(self, paths: PuddingClawPaths) -> None:
        self.path = paths.skill_runtime_bindings()
        self.lock = FileLock(str(self.path.parent / ".skill-runtime-bindings.lock"), thread_local=False)

    def _read(self) -> dict[str, object]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "revision": 0, "bindings": {}}
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("revision"), int)
            or not isinstance(value.get("bindings"), dict)
        ):
            raise ValueError("Skill runtime binding registry is invalid")
        return value

    def runtime_for(self, *, skill_id: str, skill_version: str) -> str:
        skill = safe_identity_component(skill_id, field="skill_id")
        if not self.path.exists():
            return "host"
        with self.lock.acquire(timeout=30):
            value = self._read()
        binding = value["bindings"].get(skill)
        if not isinstance(binding, dict) or binding.get("skill_version") != skill_version:
            return "host"
        runtime = str(binding.get("runtime") or "")
        if runtime not in {"host", "docker"}:
            raise ValueError("Skill runtime binding is invalid")
        return runtime

    def bind(self, *, skill_id: str, skill_version: str, runtime: str) -> int:
        skill = safe_identity_component(skill_id, field="skill_id")
        if runtime not in {"host", "docker"}:
            raise ValueError("Skill runtime must be host or docker")
        with self.lock.acquire(timeout=30):
            value = self._read()
            bindings = dict(value["bindings"])
            bindings[skill] = {"skill_version": skill_version, "runtime": runtime}
            revision = int(value["revision"]) + 1
            _atomic_write(
                self.path,
                {"version": 1, "revision": revision, "bindings": bindings},
            )
            return revision
