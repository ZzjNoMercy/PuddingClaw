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

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "path": self.path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
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
        )
        records[project_id] = record.to_dict()
        self._write_all(records)
        return record

    def list_projects(self) -> list[ProjectRecord]:
        records = self._read_all()
        projects: list[ProjectRecord] = []
        for project_id, raw in records.items():
            try:
                projects.append(
                    ProjectRecord(
                        project_id=project_id,
                        name=str(raw.get("name") or project_id),
                        path=str(raw["path"]),
                        created_at=float(raw.get("created_at") or 0),
                        updated_at=float(raw.get("updated_at") or 0),
                    )
                )
            except Exception:
                continue
        return sorted(projects, key=lambda item: item.updated_at, reverse=True)

    def resolve(self, project_id: str) -> Path:
        records = self._read_all()
        raw = records.get(project_id)
        if not raw:
            raise KeyError(f"Unknown project_id: {project_id}")
        path = Path(str(raw["path"])).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(f"Registered project path unavailable: {path}")
        return path

    def ensure_unscoped_workspace(self, session_id: str) -> Path:
        """Return a private workspace for an Agent session without project_id."""

        self._assert_ready()
        assert self._workspaces_dir is not None
        safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_") or "default"
        workspace = (self._workspaces_dir / "unscoped" / safe_id).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace


project_registry = ProjectRegistry()

