"""Skills Management API — Import, File Tree, Read/Write Files, SSE Watch."""

import asyncio
import json
import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile

from services.skill_management import SkillManagementError, SkillManagementService, get_skill_management_service

router = APIRouter()

# Per-connection queues for SSE broadcast
_active_connections: list[asyncio.Queue] = []

# Active skills in current session
_active_skills: dict[str, dict] = {}


class FileContent(BaseModel):
    """Request body for saving files."""

    path: str
    content: str


class RenameSkill(BaseModel):
    """Request body for renaming skills."""

    new_name: str


class UploadPlanDecision(BaseModel):
    """Digest-bound decision for a staged local upload."""

    plan_sha256: str = Field(min_length=64, max_length=64)


class FileNode(BaseModel):
    """File tree node."""

    path: str
    type: str  # "file" or "directory"
    size: int | None = None
    modified: str | None = None
    children: list["FileNode"] | None = None


def _validate_path(skills_dir: Path, skill_name: str, file_path: str) -> Path:
    """Validate file path to prevent directory traversal attacks."""
    skill_dir = skills_dir / skill_name
    target = (skill_dir / file_path).resolve()

    if not target.is_relative_to(skill_dir.resolve()):
        raise HTTPException(status_code=403, detail="Access denied: path traversal attempt")

    return target


def _extract_frontmatter_description(content: str) -> str:
    """Extract description from YAML frontmatter in SKILL.md."""
    lines = content.split("\n")

    # Check if file starts with frontmatter delimiter
    if not lines or lines[0].strip() != "---":
        # No frontmatter, try to extract first heading or first line
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                return line
            elif line.startswith("# "):
                return line.lstrip("# ").strip()
        return "No description available"

    # Parse frontmatter
    description = ""

    for i in range(1, len(lines)):
        line = lines[i]

        # End of frontmatter
        if line.strip() == "---":
            break

        # Look for description field
        if line.startswith("description:"):
            # Extract description value (may be multi-line)
            description = line.split("description:", 1)[1].strip()

            # Handle multi-line descriptions (indented continuation)
            for j in range(i + 1, len(lines)):
                next_line = lines[j]
                if next_line.strip() == "---":
                    break
                if next_line.startswith(" ") or next_line.startswith("\t"):
                    description += " " + next_line.strip()
                elif next_line.strip() and ":" in next_line:
                    # Next field started
                    break

            break

    return description if description else "No description available"


def _build_file_tree(directory: Path, base_path: Path) -> list[FileNode]:
    """Recursively build file tree structure."""
    nodes = []

    try:
        for item in sorted(directory.iterdir()):
            rel_path = str(item.relative_to(base_path))

            if item.is_file():
                nodes.append(
                    FileNode(
                        path=rel_path, type="file", size=item.stat().st_size, modified=item.stat().st_mtime.__str__()
                    )
                )
            elif item.is_dir():
                children = _build_file_tree(item, base_path)
                nodes.append(FileNode(path=rel_path + "/", type="directory", children=children))
    except Exception as e:
        print(f"⚠️ Error building tree for {directory}: {e}")

    return nodes


async def _trigger_sse_event(event_type: str, skill_name: str):
    """Broadcast SSE event to all connected clients."""
    event_data = {"skill_name": skill_name, "timestamp": asyncio.get_event_loop().time()}

    dead_queues = []
    for queue in _active_connections:
        try:
            await asyncio.wait_for(queue.put((event_type, event_data)), timeout=1.0)
        except asyncio.TimeoutError:
            dead_queues.append(queue)

    # Clean up dead connections
    for queue in dead_queues:
        _active_connections.remove(queue)


def _require_trusted_upload_origin(request: Request) -> None:
    """Block browser CSRF while preserving direct Agent/API uploads."""

    origin = request.headers.get("origin")
    if not origin:
        return
    allowed = {
        item.strip().rstrip("/")
        for item in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
        if item.strip()
    }
    if origin.rstrip("/") not in allowed:
        raise HTTPException(status_code=403, detail="Untrusted upload origin")


@router.post("/skills/import")
async def import_skill(
    files: list[UploadFile] | None = File(None),
    file: UploadFile | None = File(None),
    skill_name: str | None = Form(None),
    _trusted_origin: None = Depends(_require_trusted_upload_origin),
):
    """Stage and safely install a ZIP, .skill file, or uploaded folder.

    ``file`` remains accepted for clients built against the original upload
    contract; current clients should use the repeatable ``files`` field.
    """
    # FastAPI replaces these ``File`` defaults during request handling. Keeping
    # the guard explicit also makes direct function-level tests behave like an
    # HTTP request when one of the compatibility fields is omitted.
    uploads = list(files) if isinstance(files, list) else []
    if isinstance(file, StarletteUploadFile):
        uploads.append(file)
    if not uploads or any(not upload.filename for upload in uploads):
        raise HTTPException(status_code=400, detail="No filename provided")

    service = _upload_service()
    try:
        single = uploads[0]
        single_suffix = Path(single.filename or "").suffix.lower()
        if len(uploads) == 1 and single_suffix in {".zip", ".skill"}:
            limit = 50 * 1024 * 1024 if single_suffix == ".zip" else 20 * 1024 * 1024
            content = await _read_upload_bounded(single, limit)
            plan = await run_in_threadpool(
                service.prepare_upload,
                filename=single.filename,
                content=content,
                skill_name=skill_name,
            )
        else:
            if not skill_name:
                raise HTTPException(status_code=400, detail="skill_name is required for folder upload")
            remaining = 20 * 1024 * 1024
            uploaded_files: list[tuple[str, bytes]] = []
            for upload in uploads:
                content = await _read_upload_bounded(upload, remaining)
                remaining -= len(content)
                uploaded_files.append((str(upload.filename), content))
            plan = await run_in_threadpool(
                service.prepare_upload,
                uploaded_files=uploaded_files,
                skill_name=skill_name,
                filename=f"{skill_name}.folder",
            )
    except SkillManagementError as error:
        _raise_upload_error(error)

    if plan["action"] == "update":
        return {
            "success": True,
            "requires_confirmation": True,
            "skill_name": plan["skill_name"],
            "plan": plan,
            "message": "Existing Skill requires update confirmation",
        }

    try:
        installed = await run_in_threadpool(
            service.commit,
            action="install",
            plan_id=plan["plan_id"],
            plan_sha256=plan["plan_sha256"],
        )
    except SkillManagementError as error:
        _raise_upload_error(error)
    await _trigger_sse_event("skill_created", str(installed["skill_name"]))
    return _upload_result(installed)


@router.post("/skills/import/{plan_id}/commit")
async def commit_uploaded_skill(
    plan_id: str,
    decision: UploadPlanDecision,
    _trusted_origin: None = Depends(_require_trusted_upload_origin),
):
    """Commit a user-confirmed update produced by the upload endpoint."""
    service = _upload_service()
    plan = service.preview(plan_id)
    if not plan or not str(plan.get("source") or "").startswith("upload:"):
        raise HTTPException(status_code=404, detail="Upload plan not found")
    if plan.get("action") != "update":
        raise HTTPException(status_code=409, detail="Only staged upload updates require confirmation")
    try:
        installed = await run_in_threadpool(
            service.commit,
            action="update",
            plan_id=plan_id,
            plan_sha256=decision.plan_sha256,
        )
    except SkillManagementError as error:
        _raise_upload_error(error)
    await _trigger_sse_event("skill_updated", str(installed["skill_name"]))
    return _upload_result(installed)


@router.post("/skills/import/{plan_id}/cancel")
async def cancel_uploaded_skill(
    plan_id: str,
    decision: UploadPlanDecision,
    _trusted_origin: None = Depends(_require_trusted_upload_origin),
):
    """Discard a staged upload when the user declines an overwrite."""
    service = _upload_service()
    plan = service.preview(plan_id)
    if not plan or not str(plan.get("source") or "").startswith("upload:"):
        raise HTTPException(status_code=404, detail="Upload plan not found")
    try:
        cancelled = await run_in_threadpool(
            service.cancel,
            plan_id=plan_id,
            plan_sha256=decision.plan_sha256,
        )
    except SkillManagementError as error:
        _raise_upload_error(error)
    return {"success": True, "plan": cancelled}


async def _read_upload_bounded(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"Upload exceeds the {limit // 1024 // 1024}MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _raise_upload_error(error: SkillManagementError) -> None:
    if error.code in {"skill_size_limit_exceeded", "skill_file_limit_exceeded"}:
        status = 413
    elif error.code in {"installed_skill_changed", "plan_expired", "plan_already_consumed"}:
        status = 409
    else:
        status = 400
    raise HTTPException(
        status_code=status,
        detail={"code": error.code, "message": error.message},
    ) from error


def _upload_result(plan: dict) -> dict:
    return {
        "success": True,
        "requires_confirmation": False,
        "skill_name": plan["skill_name"],
        "plan": plan,
        "message": "Skill imported successfully",
    }


def _upload_service() -> SkillManagementService:
    from app import BASE_DIR

    return get_skill_management_service(BASE_DIR)


# ── Static routes (must come before dynamic routes) ────────


@router.get("/skills/active")
async def get_active_skills():
    """Get currently loaded skills in session."""
    return {
        "skills": [{"name": name, "description": info.get("description", "")} for name, info in _active_skills.items()]
    }


@router.post("/skills/load")
async def load_skill(data: dict):
    """Load a skill into current session."""
    from app import BASE_DIR

    skill_name = data.get("skill_name")

    if not skill_name:
        raise HTTPException(status_code=400, detail="skill_name is required")

    skills_dir = BASE_DIR / "skills"
    skill_dir = skills_dir / skill_name

    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    # Read SKILL.md for description
    skill_md = skill_dir / "SKILL.md"
    description = ""
    if skill_md.exists():
        try:
            content = skill_md.read_text(encoding="utf-8")
            description = _extract_frontmatter_description(content)
        except Exception:
            description = "No description available"

    _active_skills[skill_name] = {"name": skill_name, "description": description, "path": str(skill_dir)}

    return {"success": True, "skill": skill_name, "description": description}


@router.post("/skills/unload")
async def unload_skill(data: dict):
    """Unload a skill from current session."""
    skill_name = data.get("skill_name")

    if not skill_name:
        raise HTTPException(status_code=400, detail="skill_name is required")

    if skill_name in _active_skills:
        del _active_skills[skill_name]

    return {"success": True, "skill": skill_name}


@router.get("/skills/watch")
async def watch_skills():
    """SSE endpoint for skill change events."""
    return EventSourceResponse(_event_generator())


# ── Dynamic routes ────────────────────────────────────────


@router.get("/skills/{skill_name}/tree")
async def get_skill_tree(skill_name: str):
    """Get recursive file tree for a skill."""
    from app import BASE_DIR

    skills_dir = BASE_DIR / "skills"
    skill_dir = skills_dir / skill_name

    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail="Skill not found")

    files = _build_file_tree(skill_dir, skill_dir)

    return {"name": skill_name, "files": [node.model_dump() for node in files]}


@router.get("/skills/{skill_name}/file")
async def read_skill_file(skill_name: str, path: str):
    """Read any file in a skill directory."""
    from app import BASE_DIR

    skills_dir = BASE_DIR / "skills"

    target = _validate_path(skills_dir, skill_name, path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    # Detect language from extension
    ext = target.suffix.lower()
    language_map = {
        ".py": "python",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".txt": "text",
        ".sh": "bash",
    }
    language = language_map.get(ext, "text")

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Fallback for non-UTF8 files
        content = target.read_text(encoding="latin-1")

    return {"path": path, "content": content, "language": language}


@router.post("/skills/{skill_name}/file")
async def save_skill_file(skill_name: str, data: FileContent):
    """Save any file in a skill directory with atomic write."""
    import tempfile

    from app import BASE_DIR

    skills_dir = BASE_DIR / "skills"

    target = _validate_path(skills_dir, skill_name, data.path)

    # Create parent directories if needed
    target.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: write to temp file then move
    temp_fd, temp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp", text=True)
    try:
        with open(temp_fd, "w", encoding="utf-8") as f:
            f.write(data.content)

        # Atomic move (replaces existing file)
        shutil.move(temp_path, target)

        # Trigger SSE event
        await _trigger_sse_event("skill_updated", skill_name)

        return {"success": True, "message": "File saved"}
    except Exception as e:
        # Clean up temp file on error
        if Path(temp_path).exists():
            Path(temp_path).unlink()
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")


@router.post("/skills/{skill_name}/rename")
async def rename_skill(skill_name: str, data: RenameSkill):
    """Rename a skill directory."""
    from app import BASE_DIR

    skills_dir = BASE_DIR / "skills"

    old_dir = skills_dir / skill_name
    new_dir = skills_dir / data.new_name

    # Validate old directory exists
    if not old_dir.exists():
        raise HTTPException(status_code=404, detail="Skill not found")

    # Validate new name
    if not data.new_name or not data.new_name.strip():
        raise HTTPException(status_code=400, detail="New name cannot be empty")

    # Check if new name already exists
    if new_dir.exists():
        raise HTTPException(status_code=400, detail=f"Skill '{data.new_name}' already exists")

    # Validate new name doesn't contain path traversal
    if ".." in data.new_name or "/" in data.new_name or "\\" in data.new_name:
        raise HTTPException(status_code=400, detail="Invalid skill name")

    try:
        # Rename directory
        old_dir.rename(new_dir)

        # Trigger SSE event
        await _trigger_sse_event("skill_renamed", data.new_name)

        return {
            "success": True,
            "old_name": skill_name,
            "new_name": data.new_name,
            "message": "Skill renamed successfully",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rename skill: {str(e)}")


async def _event_generator() -> AsyncGenerator[dict, None]:
    """SSE event generator with per-connection queue."""
    queue = asyncio.Queue()
    _active_connections.append(queue)

    try:
        while True:
            try:
                event_type, event_data = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield {"event": event_type, "data": json.dumps(event_data)}
            except asyncio.TimeoutError:
                # Send keepalive ping
                yield {"event": "ping", "data": json.dumps({"timestamp": asyncio.get_event_loop().time()})}
    finally:
        # Clean up connection on disconnect
        if queue in _active_connections:
            _active_connections.remove(queue)
