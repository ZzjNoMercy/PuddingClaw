"""Project registry for Agent mode.

The frontend may submit a local directory once during registration. After that,
Agent requests should use project_id only. This keeps filesystem access
server-side and auditable.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any



@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    name: str
    path: str
    created_at: float
    updated_at: float
    pinned: bool = False
    execution_mode: str | None = None
    permission_rules: tuple[dict[str, Any], ...] = ()
    permission_rules_revision: int = 0
    trust_state: str = "pending"
    identity_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "path": self.path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pinned": self.pinned,
            "execution_mode": self.execution_mode,
            "permission_rules": [dict(rule) for rule in self.permission_rules],
            "permission_rules_revision": self.permission_rules_revision,
            "trust_state": self.trust_state,
            "identity_digest": self.identity_digest,
        }


class ProjectRegistry:
    """JSON-backed registry for user-approved project directories."""

    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._projects_file: Path | None = None
        self._workspaces_dir: Path | None = None

    def initialize(self, base_dir: Path) -> None:
        if base_dir.expanduser().resolve().name == "backend":
            from runtime_identity.paths import PuddingClawPaths

            base_dir = PuddingClawPaths.from_environment().root
        self._base_dir = base_dir
        data_dir = base_dir / "projects"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._projects_file = data_dir / "registry.json"
        self._workspaces_dir = base_dir / "data" / "agent-workspaces"
        self._workspaces_dir.mkdir(parents=True, exist_ok=True)
        if not self._projects_file.exists():
            self._write_all({})

    def _assert_ready(self) -> None:
        assert self._base_dir is not None
        assert self._projects_file is not None
        assert self._workspaces_dir is not None

    @property
    def base_dir(self) -> Path:
        self._assert_ready()
        assert self._base_dir is not None
        return self._base_dir

    def _read_all(self) -> dict[str, dict[str, Any]]:
        self._assert_ready()
        assert self._projects_file is not None
        try:
            raw = json.loads(self._projects_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            pass
        return {}

    def _write_all(self, records: dict[str, dict[str, Any]]) -> None:
        self._assert_ready()
        assert self._projects_file is not None
        self._projects_file.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _project_id_for_path(path: Path) -> str:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
        return f"proj_{digest}"

    @staticmethod
    def _identity_digest(path: Path) -> str:
        """Bind trust to stable project identity, not generated runtime files."""

        try:
            stat = path.stat()
            parts = [f"directory:{stat.st_dev}:{stat.st_ino}"]
        except FileNotFoundError:
            parts = ["directory:missing"]
        git_head = path / ".git" / "HEAD"
        if git_head.is_file():
            try:
                parts.append(f"git-head:{git_head.read_text(encoding='utf-8').strip()}")
            except OSError:
                pass
        return hashlib.sha256((str(path) + "\0" + "\0".join(parts)).encode()).hexdigest()

    def register(
        self,
        path: str,
        name: str | None = None,
        *,
        trusted: bool = False,
    ) -> ProjectRecord:
        """Register a directory, optionally recording an explicit local authorization."""

        self._assert_ready()
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Project path does not exist: {path}")
        if not resolved.is_dir():
            raise NotADirectoryError(f"Project path is not a directory: {path}")

        now = time.time()
        project_id = self._project_id_for_path(resolved)
        records = self._read_all()
        existing = records.get(project_id, {})
        created_at = float(existing.get("created_at") or now)
        existing_trust_state = existing.get("trust_state")
        trust_state = "trusted" if trusted else (
            str(existing_trust_state)
            if existing_trust_state in {"pending", "trusted", "denied"}
            else "pending"
        )
        record = ProjectRecord(
            project_id=project_id,
            name=(name or resolved.name or project_id).strip(),
            path=str(resolved),
            created_at=created_at,
            updated_at=now,
            pinned=bool(existing.get("pinned") or False),
            execution_mode=(str(existing.get("execution_mode")) if existing.get("execution_mode") in {"spawn", "kernel"} else None),
            permission_rules=self._normalize_permission_rules(existing.get("permission_rules")),
            permission_rules_revision=int(existing.get("permission_rules_revision") or 0),
            trust_state=trust_state,
            identity_digest=(
                self._identity_digest(resolved)
                if trusted
                else str(existing.get("identity_digest") or self._identity_digest(resolved))
            ),
        )
        records[project_id] = record.to_dict()
        self._write_all(records)
        return record

    def list_projects(self) -> list[ProjectRecord]:
        records = self._read_all()
        projects: list[ProjectRecord] = []
        for project_id, raw in records.items():
            try:
                project_path = Path(str(raw["path"])).expanduser().resolve()
                projects.append(
                    ProjectRecord(
                        project_id=project_id,
                        name=str(raw.get("name") or project_id),
                        path=str(project_path),
                        created_at=float(raw.get("created_at") or 0),
                        updated_at=float(raw.get("updated_at") or 0),
                        pinned=bool(raw.get("pinned") or False),
                        execution_mode=(str(raw.get("execution_mode")) if raw.get("execution_mode") in {"spawn", "kernel"} else None),
                        permission_rules=self._normalize_permission_rules(raw.get("permission_rules")),
                        permission_rules_revision=int(raw.get("permission_rules_revision") or 0),
                        trust_state=str(raw.get("trust_state") or "pending") if raw.get("trust_state") in {"pending", "trusted", "denied"} else "pending",
                        identity_digest=str(raw.get("identity_digest") or ""),
                    )
                )
            except Exception:
                continue
        return sorted(projects, key=lambda item: (not item.pinned, -item.updated_at))

    def update(self, project_id: str, *, name: str | None = None, pinned: bool | None = None, execution_mode: str | None = None) -> ProjectRecord:
        records = self._read_all()
        raw = records.get(project_id)
        if not raw:
            raise KeyError(f"Unknown project_id: {project_id}")

        next_raw = dict(raw)
        if name is not None:
            next_name = name.strip()
            if not next_name:
                raise ValueError("Project name cannot be empty")
            next_raw["name"] = next_name
        if pinned is not None:
            next_raw["pinned"] = pinned
        if execution_mode is not None:
            if execution_mode not in {"spawn", "kernel"}:
                raise ValueError("execution_mode must be spawn or kernel")
            next_raw["execution_mode"] = execution_mode
        next_raw["updated_at"] = time.time()
        records[project_id] = next_raw
        self._write_all(records)
        return ProjectRecord(
            project_id=project_id,
            name=str(next_raw.get("name") or project_id),
            path=str(next_raw["path"]),
            created_at=float(next_raw.get("created_at") or 0),
            updated_at=float(next_raw.get("updated_at") or 0),
            pinned=bool(next_raw.get("pinned") or False),
            execution_mode=(str(next_raw.get("execution_mode")) if next_raw.get("execution_mode") in {"spawn", "kernel"} else None),
            permission_rules=self._normalize_permission_rules(next_raw.get("permission_rules")),
            permission_rules_revision=int(next_raw.get("permission_rules_revision") or 0),
            trust_state=str(next_raw.get("trust_state") or "pending"),
            identity_digest=str(next_raw.get("identity_digest") or ""),
        )

    @staticmethod
    def _normalize_permission_rules(raw_rules: Any) -> tuple[dict[str, Any], ...]:
        """Validate project rules without importing the policy module at load time."""

        if raw_rules in (None, []):
            return ()
        from graph.permission_policy import compile_permission_rules

        compiled = compile_permission_rules(raw_rules, source="project")
        return tuple(
            {
                "tool": rule.tool,
                "pattern": rule.pattern,
                "decision": rule.decision.value,
                "scope": "project",
                "constraints": rule.constraint_map(),
                "source": "project",
                "revision": rule.revision,
            }
            for rule in compiled
        )

    def get_permission_rules(self, project_id: str | None) -> dict[str, Any]:
        if not project_id:
            return {"rules": [], "revision": 0}
        raw = self._read_all().get(project_id)
        if not isinstance(raw, dict):
            raise KeyError(f"Unknown project_id: {project_id}")
        return {
            "rules": [dict(rule) for rule in self._normalize_permission_rules(raw.get("permission_rules"))],
            "revision": int(raw.get("permission_rules_revision") or 0),
        }

    def set_permission_rules(self, project_id: str, rules: list[dict[str, Any]]) -> ProjectRecord:
        """Replace the project rule set and advance its invalidation revision."""

        normalized = self._normalize_permission_rules(rules)
        records = self._read_all()
        raw = records.get(project_id)
        if not isinstance(raw, dict):
            raise KeyError(f"Unknown project_id: {project_id}")
        revision = int(raw.get("permission_rules_revision") or 0) + 1
        persisted = []
        for rule in normalized:
            item = dict(rule)
            item["revision"] = revision
            persisted.append(item)
        raw = dict(raw)
        raw["permission_rules"] = persisted
        raw["permission_rules_revision"] = revision
        raw["updated_at"] = time.time()
        records[project_id] = raw
        self._write_all(records)
        return self._record_from_raw(project_id, raw)

    def _record_from_raw(self, project_id: str, raw: dict[str, Any]) -> ProjectRecord:
        return ProjectRecord(
            project_id=project_id,
            name=str(raw.get("name") or project_id),
            path=str(raw["path"]),
            created_at=float(raw.get("created_at") or 0),
            updated_at=float(raw.get("updated_at") or 0),
            pinned=bool(raw.get("pinned") or False),
            execution_mode=(str(raw.get("execution_mode")) if raw.get("execution_mode") in {"spawn", "kernel"} else None),
            permission_rules=self._normalize_permission_rules(raw.get("permission_rules")),
            permission_rules_revision=int(raw.get("permission_rules_revision") or 0),
            trust_state=str(raw.get("trust_state") or "pending") if raw.get("trust_state") in {"pending", "trusted", "denied"} else "pending",
            identity_digest=str(raw.get("identity_digest") or ""),
        )

    def get_execution_mode(self, project_id: str | None) -> str | None:
        if not project_id:
            return None
        raw = self._read_all().get(project_id)
        value = raw.get("execution_mode") if isinstance(raw, dict) else None
        return str(value) if value in {"spawn", "kernel"} else None

    def set_execution_mode(self, project_id: str, execution_mode: str) -> ProjectRecord:
        return self.update(project_id, execution_mode=execution_mode)

    def set_trust(self, project_id: str, trust_state: str) -> ProjectRecord:
        if trust_state not in {"pending", "trusted", "denied"}:
            raise ValueError("trust_state must be pending, trusted, or denied")
        records = self._read_all()
        raw = records.get(project_id)
        if not isinstance(raw, dict):
            raise KeyError(f"Unknown project_id: {project_id}")
        current_path = Path(str(raw.get("path") or "")).expanduser().resolve()
        raw = dict(raw)
        # This is an explicit decision about the path as it exists now. Record
        # that current identity with the requested state. Future identity
        # changes are still rejected by is_trusted().
        raw["trust_state"] = trust_state
        raw["identity_digest"] = self._identity_digest(current_path)
        raw["updated_at"] = time.time()
        records[project_id] = raw
        self._write_all(records)
        return self._record_from_raw(project_id, raw)

    def is_trusted(self, project_id: str | None) -> bool:
        if not project_id:
            return False
        raw = self._read_all().get(project_id)
        if not isinstance(raw, dict) or raw.get("trust_state") != "trusted":
            return False
        path = Path(str(raw.get("path") or "")).expanduser().resolve()
        return str(raw.get("identity_digest") or "") == self._identity_digest(path)

    def remove(self, project_id: str) -> None:
        records = self._read_all()
        if project_id not in records:
            raise KeyError(f"Unknown project_id: {project_id}")
        records.pop(project_id, None)
        self._write_all(records)

    def resolve(self, project_id: str) -> Path:
        records = self._read_all()
        raw = records.get(project_id)
        if not raw:
            raise KeyError(f"Unknown project_id: {project_id}")
        path = Path(str(raw["path"])).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(f"Registered project path unavailable: {path}")
        return path

    @property
    def unscoped_workspaces_dir(self) -> Path:
        """Return the server-owned root for unscoped Agent workspaces."""

        self._assert_ready()
        assert self._workspaces_dir is not None
        return (self._workspaces_dir / "unscoped").resolve()

    def ensure_unscoped_workspace(self, session_id: str) -> Path:
        """Return the shared default workspace for Agents without a project.

        Docker sandboxes are scoped by workspace path.  Keeping this path
        stable makes every unscoped Session reuse the same default project
        container instead of creating one container per Session.
        """

        self._assert_ready()
        del session_id  # Unscoped Sessions intentionally share one workspace/container.
        workspace = (self.unscoped_workspaces_dir / "default").resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace


project_registry = ProjectRegistry()
