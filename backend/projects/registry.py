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

from projects.project_context import ensure_project_context


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    name: str
    path: str
    created_at: float
    updated_at: float
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "path": self.path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pinned": self.pinned,
        }


class ProjectRegistry:
    """JSON-backed registry for user-approved project directories."""

    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._projects_file: Path | None = None
        self._workspaces_dir: Path | None = None

    def initialize(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        data_dir = base_dir / "data"
        data_dir.mkdir(exist_ok=True)
        self._projects_file = data_dir / "projects.json"
        self._workspaces_dir = data_dir / "agent-workspaces"
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

    def register(self, path: str, name: str | None = None) -> ProjectRecord:
        """Register a local directory and return its stable project record."""

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
        record = ProjectRecord(
            project_id=project_id,
            name=(name or resolved.name or project_id).strip(),
            path=str(resolved),
            created_at=created_at,
            updated_at=now,
            pinned=bool(existing.get("pinned") or False),
        )
        assert self._base_dir is not None
        ensure_project_context(resolved, self._base_dir)
        records[project_id] = record.to_dict()
        self._write_all(records)
        return record

    def list_projects(self) -> list[ProjectRecord]:
        records = self._read_all()
        projects: list[ProjectRecord] = []
        for project_id, raw in records.items():
            try:
                project_path = Path(str(raw["path"])).expanduser().resolve()
                if project_path.exists() and project_path.is_dir():
                    ensure_project_context(project_path, self.base_dir)
                projects.append(
                    ProjectRecord(
                        project_id=project_id,
                        name=str(raw.get("name") or project_id),
                        path=str(project_path),
                        created_at=float(raw.get("created_at") or 0),
                        updated_at=float(raw.get("updated_at") or 0),
                        pinned=bool(raw.get("pinned") or False),
                    )
                )
            except Exception:
                continue
        return sorted(projects, key=lambda item: (not item.pinned, -item.updated_at))

    def update(self, project_id: str, *, name: str | None = None, pinned: bool | None = None) -> ProjectRecord:
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
        )

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
