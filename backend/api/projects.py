"""Project registry API for Agent mode."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from graph.session_manager import session_manager
from projects.project_context import ensure_project_context, read_project_context, write_project_context
from projects.registry import project_registry

router = APIRouter()


class RegisterProjectRequest(BaseModel):
    path: str
    name: str | None = None


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    pinned: bool | None = None
    execution_mode: str | None = None


class UpdateProjectContextRequest(BaseModel):
    content: str


class UpdateProjectPermissionRulesRequest(BaseModel):
    rules: list[dict[str, Any]]


class OpenLocalFileRequest(BaseModel):
    path: str
    session_id: str


@router.get("/projects")
async def list_projects():
    projects = [project.to_dict() for project in project_registry.list_projects()]
    return {"projects": projects}


@router.post("/projects/register")
async def register_project(request: RegisterProjectRequest):
    try:
        project = project_registry.register(request.path, request.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return project.to_dict()


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, request: UpdateProjectRequest):
    try:
        project = project_registry.update(
            project_id,
            name=request.name,
            pinned=request.pinned,
            execution_mode=request.execution_mode,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return project.to_dict()


@router.put("/projects/{project_id}/permissions/rules")
async def update_project_permission_rules(
    project_id: str,
    request: UpdateProjectPermissionRulesRequest,
):
    """Replace the Project Registry rule set and invalidate old Run grants."""

    try:
        project = project_registry.set_permission_rules(project_id, request.rules)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return project.to_dict()


@router.delete("/projects/{project_id}")
async def remove_project(project_id: str):
    try:
        project_registry.remove(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "project_id": project_id}


@router.post("/projects/{project_id}/open")
async def open_project(project_id: str):
    """Open a registered project directory in the host file manager."""

    try:
        project_path = project_registry.resolve(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        _open_in_file_manager(project_path)
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to open project: {exc}") from exc

    return {"ok": True, "project_id": project_id, "path": str(project_path)}


@router.post("/local-files/open")
async def open_local_file(request: OpenLocalFileRequest):
    """Open a file generated inside the current Agent workspace."""

    metadata = session_manager.get_metadata(request.session_id)
    workspace_path = metadata.get("workspace_path")
    if not workspace_path:
        raise HTTPException(status_code=400, detail="Session has no workspace_path")

    workspace = Path(str(workspace_path)).expanduser().resolve()
    target = Path(request.path.removeprefix("file://")).expanduser().resolve()
    if not _is_relative_to(target, workspace):
        raise HTTPException(status_code=403, detail="File is outside the session workspace")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {target}")

    try:
        _open_in_file_manager(target)
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to open file: {exc}") from exc

    return {"ok": True, "path": str(target)}


@router.get("/projects/{project_id}/context")
async def get_project_context(project_id: str):
    try:
        project_path = project_registry.resolve(project_id)
        ensure_project_context(project_path, project_registry.base_dir)
        content, source_path, is_project_local = read_project_context(project_path, project_registry.base_dir)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "project_id": project_id,
        "content": content,
        "path": str(source_path),
        "is_project_local": is_project_local,
    }


@router.put("/projects/{project_id}/context")
async def update_project_context(project_id: str, request: UpdateProjectContextRequest):
    try:
        project_path = project_registry.resolve(project_id)
        context_path = write_project_context(project_path, project_registry.base_dir, request.content)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "project_id": project_id,
        "content": request.content,
        "path": str(context_path),
        "is_project_local": True,
    }


def _open_in_file_manager(path: Path) -> None:
    if sys.platform == "darwin":
        command = ["open", str(path)]
    elif sys.platform.startswith("win"):
        command = ["explorer", str(path)]
    elif sys.platform.startswith("linux"):
        command = ["xdg-open", str(path)]
    else:
        raise RuntimeError(f"Opening folders is not supported on this platform: {sys.platform}")

    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(command, **popen_kwargs)
    except FileNotFoundError as exc:
        raise RuntimeError(f"System file manager command not found: {command[0]}") from exc


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
