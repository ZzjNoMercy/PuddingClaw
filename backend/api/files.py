"""GET/POST /api/files + Skills CRUD — File read/write for Monaco editor."""

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent

# Whitelist of editable directories (relative to backend/)
ALLOWED_PREFIXES = ["workspace/", "memory/", "skills/", "knowledge/"]

# Whitelist of specific root-level files that can be accessed
ALLOWED_ROOT_FILES = {"SKILLS_SNAPSHOT.md"}


def _validate_path(rel_path: str) -> Path:
    """Validate and resolve file path within allowed directories."""
    normalized = rel_path.replace("\\", "/").lstrip("./")
    if not (
        any(normalized.startswith(prefix) for prefix in ALLOWED_PREFIXES)
        or normalized in ALLOWED_ROOT_FILES
    ):
        raise HTTPException(status_code=403, detail=f"Access denied: {rel_path}")
    full_path = (BASE_DIR / normalized).resolve()
    if not str(full_path).startswith(str(BASE_DIR)):
        raise HTTPException(status_code=403, detail="Path traversal detected")
    return full_path


def _safe_read_text(file_path: Path) -> str:
    """Read file with encoding fallback: UTF-8 -> GBK -> latin-1."""
    raw = file_path.read_bytes()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


@router.get("/files")
async def read_file(path: str):
    file_path = _validate_path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    content = _safe_read_text(file_path)
    return {"path": path, "content": content}


class FileSaveRequest(BaseModel):
    path: str
    content: str


@router.post("/files")
async def save_file(request: FileSaveRequest):
    file_path = _validate_path(request.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(request.content, encoding="utf-8")

    # Trigger memory index rebuild when MEMORY.md is saved
    normalized = request.path.replace("\\", "/").lstrip("./")
    if normalized == "memory/MEMORY.md":
        try:
            from graph.memory_indexer import get_memory_indexer

            indexer = get_memory_indexer(BASE_DIR)
            indexer.rebuild_index()
        except Exception:
            pass

    return {"path": request.path, "status": "saved"}


@router.get("/skills")
async def list_skills():
    """Scan skills/ directory and return skill list with name, path, description."""
    skills_dir = BASE_DIR / "skills"
    if not skills_dir.exists():
        return {"skills": []}

    skills: list[dict[str, str]] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        name = skill_dir.name
        description = ""
        rel_path = f"skills/{name}/SKILL.md"

        # Parse YAML frontmatter
        try:
            text = skill_md.read_text(encoding="utf-8")
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1])
                    if isinstance(meta, dict):
                        name = meta.get("name", name)
                        description = meta.get("description", "")
        except Exception:
            pass

        skills.append({"name": name, "path": rel_path, "description": description})

    return {"skills": skills}


# ---------------------------------------------------------------------------
# Skill name validation helper
# ---------------------------------------------------------------------------

_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_skill_name(name: str) -> Path:
    """Validate skill name and return the resolved skill directory path."""
    if not _SKILL_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Invalid skill name. Only letters, digits, hyphens and underscores are allowed.",
        )
    skill_dir = (BASE_DIR / "skills" / name).resolve()
    # Path traversal guard
    if not str(skill_dir).startswith(str(BASE_DIR / "skills")):
        raise HTTPException(status_code=403, detail="Path traversal detected")
    return skill_dir


def _parse_skill_meta(skill_dir: Path) -> dict:
    """Read SKILL.md and extract YAML frontmatter metadata."""
    skill_md = skill_dir / "SKILL.md"
    name = skill_dir.name
    description = ""
    content = ""

    if skill_md.exists():
        try:
            content = skill_md.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1])
                    if isinstance(meta, dict):
                        name = meta.get("name", name)
                        description = meta.get("description", "")
        except Exception:
            pass

    return {"name": name, "description": description, "content": content}


def _file_info(p: Path) -> dict:
    """Return file metadata dict."""
    stat = p.stat()
    return {
        "name": p.name,
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/skills/:name — single skill detail
# ---------------------------------------------------------------------------

@router.get("/skills/{name}")
async def get_skill(name: str):
    """Return detailed information for a single skill."""
    skill_dir = _validate_skill_name(name)
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")

    meta = _parse_skill_meta(skill_dir)
    files = [_file_info(f) for f in sorted(skill_dir.iterdir()) if f.is_file()]

    return {
        "name": meta["name"],
        "description": meta["description"],
        "path": f"skills/{name}/SKILL.md",
        "files": files,
        "content": meta["content"],
    }


# ---------------------------------------------------------------------------
# POST /api/skills — create a new skill
# ---------------------------------------------------------------------------

class SkillCreateRequest(BaseModel):
    name: str
    description: str = ""


@router.post("/skills")
async def create_skill(request: SkillCreateRequest):
    """Create a new skill directory with a template SKILL.md."""
    skill_dir = _validate_skill_name(request.name)
    if skill_dir.exists():
        raise HTTPException(status_code=409, detail=f"Skill already exists: {request.name}")

    skill_dir.mkdir(parents=True, exist_ok=True)

    # Generate template SKILL.md with YAML frontmatter
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    template = (
        f"---\n"
        f"name: {request.name}\n"
        f"description: {request.description}\n"
        f"version: 0.1.0\n"
        f"created: {now_iso}\n"
        f"---\n\n"
        f"# {request.name}\n\n"
        f"{request.description}\n"
    )
    (skill_dir / "SKILL.md").write_text(template, encoding="utf-8")

    return JSONResponse(
        status_code=201,
        content={
            "name": request.name,
            "description": request.description,
            "path": f"skills/{request.name}/SKILL.md",
            "status": "created",
        },
    )


# ---------------------------------------------------------------------------
# DELETE /api/skills/:name — delete a skill
# ---------------------------------------------------------------------------

@router.delete("/skills/{name}")
async def delete_skill(name: str):
    """Delete a skill directory and all its contents."""
    skill_dir = _validate_skill_name(name)
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")

    shutil.rmtree(skill_dir)
    return {"name": name, "status": "deleted"}


# ---------------------------------------------------------------------------
# GET /api/skills/:name/files — list files in a skill directory
# ---------------------------------------------------------------------------

@router.get("/skills/{name}/files")
async def list_skill_files(name: str):
    """List all files inside a skill directory."""
    skill_dir = _validate_skill_name(name)
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")

    files = [_file_info(f) for f in sorted(skill_dir.iterdir()) if f.is_file()]
    return {"name": name, "files": files}


# ---------------------------------------------------------------------------
# Version label validation
# ---------------------------------------------------------------------------

_VERSION_LABEL_RE = re.compile(r"^[a-zA-Z0-9.\-]+$")


def _validate_version_label(label: str) -> None:
    """Raise 400 if version label contains invalid characters."""
    if not _VERSION_LABEL_RE.match(label):
        raise HTTPException(
            status_code=400,
            detail="Invalid version label. Only letters, digits, dots and hyphens are allowed.",
        )


# ---------------------------------------------------------------------------
# POST /api/skills/:name/versions — create a version snapshot
# ---------------------------------------------------------------------------

class VersionCreateRequest(BaseModel):
    label: str


@router.post("/skills/{name}/versions")
async def create_version(name: str, request: VersionCreateRequest):
    """Snapshot the current skill directory into versions/{label}/."""
    skill_dir = _validate_skill_name(name)
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")

    label = request.label.strip()
    _validate_version_label(label)

    versions_dir = skill_dir / "versions" / label
    if versions_dir.exists():
        raise HTTPException(status_code=409, detail=f"Version already exists: {label}")

    # Copy all files (excluding the versions/ subdirectory itself)
    versions_dir.mkdir(parents=True, exist_ok=True)
    for item in skill_dir.iterdir():
        if item.name == "versions":
            continue
        if item.is_file():
            shutil.copy2(item, versions_dir / item.name)
        elif item.is_dir():
            shutil.copytree(item, versions_dir / item.name)

    file_count = sum(1 for f in versions_dir.iterdir() if f.is_file())

    return JSONResponse(
        status_code=201,
        content={
            "label": label,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "file_count": file_count,
            "status": "created",
        },
    )


# ---------------------------------------------------------------------------
# GET /api/skills/:name/versions — list version snapshots
# ---------------------------------------------------------------------------

@router.get("/skills/{name}/versions")
async def list_versions(name: str):
    """List all version snapshots for a skill, newest first."""
    skill_dir = _validate_skill_name(name)
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")

    versions_base = skill_dir / "versions"
    if not versions_base.exists():
        return {"versions": []}

    versions: list[dict] = []
    for ver_dir in versions_base.iterdir():
        if not ver_dir.is_dir():
            continue
        stat = ver_dir.stat()
        file_count = sum(1 for f in ver_dir.iterdir() if f.is_file())
        versions.append({
            "label": ver_dir.name,
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "file_count": file_count,
        })

    # Sort by created_at descending
    versions.sort(key=lambda v: v["created_at"], reverse=True)
    return {"versions": versions}


# ---------------------------------------------------------------------------
# GET /api/skills/:name/versions/:label — get version content
# ---------------------------------------------------------------------------

@router.get("/skills/{name}/versions/{label}")
async def get_version_content(name: str, label: str):
    """Read the SKILL.md from a specific version snapshot."""
    skill_dir = _validate_skill_name(name)
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")

    _validate_version_label(label)

    version_dir = skill_dir / "versions" / label
    if not version_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Version not found: {label}")

    skill_md = version_dir / "SKILL.md"
    if not skill_md.exists():
        raise HTTPException(status_code=404, detail=f"SKILL.md not found in version: {label}")

    content = skill_md.read_text(encoding="utf-8")
    return {"label": label, "content": content}


# ---------------------------------------------------------------------------
# POST /api/skills/:name/diff — compare two versions
# ---------------------------------------------------------------------------

class DiffRequest(BaseModel):
    version_a: str
    version_b: str


@router.post("/skills/{name}/diff")
async def diff_versions(name: str, request: DiffRequest):
    """Compare SKILL.md content between two versions (or 'current')."""
    skill_dir = _validate_skill_name(name)
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")

    def _read_version(version: str) -> str:
        if version == "current":
            md_path = skill_dir / "SKILL.md"
        else:
            _validate_version_label(version)
            ver_dir = skill_dir / "versions" / version
            if not ver_dir.is_dir():
                raise HTTPException(status_code=404, detail=f"Version not found: {version}")
            md_path = ver_dir / "SKILL.md"
        if not md_path.exists():
            raise HTTPException(status_code=404, detail=f"SKILL.md not found for version: {version}")
        return md_path.read_text(encoding="utf-8")

    content_a = _read_version(request.version_a)
    content_b = _read_version(request.version_b)

    return {
        "version_a": request.version_a,
        "version_b": request.version_b,
        "content_a": content_a,
        "content_b": content_b,
    }
