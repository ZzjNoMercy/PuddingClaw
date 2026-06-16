"""Skills Management API — Import, File Tree, Read/Write Files, SSE Watch."""

import asyncio
import json
import shutil
import zipfile
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, File, HTTPException, UploadFile, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from typing import List, Optional

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
    in_frontmatter = True
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
                nodes.append(FileNode(
                    path=rel_path,
                    type="file",
                    size=item.stat().st_size,
                    modified=item.stat().st_mtime.__str__()
                ))
            elif item.is_dir():
                children = _build_file_tree(item, base_path)
                nodes.append(FileNode(
                    path=rel_path + "/",
                    type="directory",
                    children=children
                ))
    except Exception as e:
        print(f"⚠️ Error building tree for {directory}: {e}")

    return nodes


async def _trigger_sse_event(event_type: str, skill_name: str):
    """Broadcast SSE event to all connected clients."""
    event_data = {
        "skill_name": skill_name,
        "timestamp": asyncio.get_event_loop().time()
    }

    dead_queues = []
    for queue in _active_connections:
        try:
            await asyncio.wait_for(queue.put((event_type, event_data)), timeout=1.0)
        except asyncio.TimeoutError:
            dead_queues.append(queue)

    # Clean up dead connections
    for queue in dead_queues:
        _active_connections.remove(queue)


@router.post("/skills/import")
async def import_skill(
    files: List[UploadFile] = File(...),
    skill_name: Optional[str] = Form(None)
):
    """Import a skill from ZIP file, .skill file, or multiple files (folder upload)."""
    from app import BASE_DIR
    skills_dir = BASE_DIR / "skills"

    # Single file upload
    if len(files) == 1:
        file = files[0]
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")

        content = await file.read()
        if len(content) > 50 * 1024 * 1024:  # 50MB limit
            raise HTTPException(status_code=413, detail="File too large (max 50MB)")

        # Handle .skill file
        if file.filename.endswith(".skill"):
            if not skill_name:
                skill_name = Path(file.filename).stem

            target_dir = skills_dir / skill_name
            target_dir.mkdir(parents=True, exist_ok=True)

            skill_md = target_dir / "SKILL.md"
            skill_md.write_bytes(content)

            await _trigger_sse_event("skill_created", skill_name)

            return {
                "success": True,
                "skill_name": skill_name,
                "message": "Skill imported successfully"
            }

        # Handle ZIP file
        elif file.filename.endswith(".zip"):
            temp_zip = skills_dir / f"temp_{file.filename}"

            try:
                temp_zip.write_bytes(content)

                with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
                    # Check for ZIP bomb
                    if len(zip_ref.namelist()) > 1000:
                        raise HTTPException(status_code=400, detail="Too many files in ZIP (max 1000)")

                    # Find root directory name and validate paths
                    root_dirs = set()
                    for name in zip_ref.namelist():
                        parts = Path(name).parts
                        if parts:
                            root_dirs.add(parts[0])

                        # Validate each file path for security
                        normalized_path = Path(name).as_posix()
                        if ".." in normalized_path or normalized_path.startswith("/"):
                            raise HTTPException(status_code=400, detail=f"Invalid path in ZIP: {name}")

                        # Check for forbidden file extensions
                        file_ext = Path(name).suffix.lower()
                        forbidden_exts = {".exe", ".bat", ".cmd", ".ps1", ".dll", ".so"}
                        if file_ext in forbidden_exts:
                            raise HTTPException(status_code=400, detail=f"Forbidden file type: {name}")

                    if len(root_dirs) != 1:
                        raise HTTPException(status_code=400, detail="ZIP must contain exactly one root directory")

                    extracted_skill_name = list(root_dirs)[0]

                    # Check for SKILL.md
                    skill_md_path = f"{extracted_skill_name}/SKILL.md"
                    if skill_md_path not in zip_ref.namelist():
                        raise HTTPException(status_code=400, detail="Invalid skill: missing SKILL.md")

                    # Extract to skills directory
                    target_dir = skills_dir / extracted_skill_name
                    if target_dir.exists():
                        shutil.rmtree(target_dir)

                    zip_ref.extractall(skills_dir)

                await _trigger_sse_event("skill_created", extracted_skill_name)

                return {
                    "success": True,
                    "skill_name": extracted_skill_name,
                    "message": "Skill imported successfully"
                }

            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid ZIP file")
            finally:
                if temp_zip.exists():
                    temp_zip.unlink()

        else:
            raise HTTPException(status_code=400, detail="Only ZIP and .skill files are supported")

    # Multiple files upload (folder)
    else:
        if not skill_name:
            raise HTTPException(status_code=400, detail="skill_name is required for folder upload")

        target_dir = skills_dir / skill_name
        target_dir.mkdir(parents=True, exist_ok=True)

        has_skill_md = False

        for file in files:
            if not file.filename:
                continue

            # Extract relative path (remove folder prefix if present)
            # Browser sends: "folder-name/subfolder/file.txt"
            # We need: "subfolder/file.txt"
            file_relative_path = file.filename
            parts = Path(file.filename).parts
            if len(parts) > 1 and parts[0] == skill_name:
                # Remove skill_name prefix if it matches
                file_relative_path = str(Path(*parts[1:]))
            elif len(parts) > 1:
                # Remove first folder level (browser folder name)
                file_relative_path = str(Path(*parts[1:]))

            # Validate path
            normalized_path = Path(file_relative_path).as_posix()
            if ".." in normalized_path or normalized_path.startswith("/"):
                raise HTTPException(status_code=400, detail=f"Invalid path: {file.filename}")

            # Check for forbidden extensions
            file_ext = Path(file_relative_path).suffix.lower()
            forbidden_exts = {".exe", ".bat", ".cmd", ".ps1", ".dll", ".so"}
            if file_ext in forbidden_exts:
                raise HTTPException(status_code=400, detail=f"Forbidden file type: {file.filename}")

            # Save file with relative path
            file_path = target_dir / file_relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)

            content = await file.read()
            file_path.write_bytes(content)

            if file_relative_path == "SKILL.md" or file_relative_path.endswith("/SKILL.md"):
                has_skill_md = True

        if not has_skill_md:
            shutil.rmtree(target_dir)
            raise HTTPException(status_code=400, detail="Invalid skill: missing SKILL.md")

        await _trigger_sse_event("skill_created", skill_name)

        return {
            "success": True,
            "skill_name": skill_name,
            "message": "Skill imported successfully"
        }


# ── Static routes (must come before dynamic routes) ────────

@router.get("/skills/active")
async def get_active_skills():
    """Get currently loaded skills in session."""
    return {
        "skills": [
            {"name": name, "description": info.get("description", "")}
            for name, info in _active_skills.items()
        ]
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

    _active_skills[skill_name] = {
        "name": skill_name,
        "description": description,
        "path": str(skill_dir)
    }

    return {
        "success": True,
        "skill": skill_name,
        "description": description
    }


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

    return {
        "name": skill_name,
        "files": [node.model_dump() for node in files]
    }


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

    return {
        "path": path,
        "content": content,
        "language": language
    }


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
        with open(temp_fd, 'w', encoding='utf-8') as f:
            f.write(data.content)

        # Atomic move (replaces existing file)
        shutil.move(temp_path, target)

        # Trigger SSE event
        await _trigger_sse_event("skill_updated", skill_name)

        return {
            "success": True,
            "message": "File saved"
        }
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
            "message": "Skill renamed successfully"
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
                yield {
                    "event": event_type,
                    "data": json.dumps(event_data)
                }
            except asyncio.TimeoutError:
                # Send keepalive ping
                yield {
                    "event": "ping",
                    "data": json.dumps({"timestamp": asyncio.get_event_loop().time()})
                }
    finally:
        # Clean up connection on disconnect
        if queue in _active_connections:
            _active_connections.remove(queue)
