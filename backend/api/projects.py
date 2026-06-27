"""Project registry API for Agent mode."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from projects.registry import project_registry

router = APIRouter()


class RegisterProjectRequest(BaseModel):
    path: str
    name: str | None = None


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
