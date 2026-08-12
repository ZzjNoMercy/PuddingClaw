"""Typed, read-only inspection for locally installed Skills."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_FILES = 512
_MAX_TOTAL_BYTES = 20 * 1024 * 1024


class InspectSkillInput(BaseModel):
    skill_name: str = Field(description="已安装 Skill 的目录名称")


class InspectSkillTool(BaseTool):
    """Return a deterministic manifest without executing Skill code."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "inspect_skill"
    description: str = (
        "Inspect one locally installed Skill without executing it. Returns the declared "
        "name/version, file list, per-file SHA-256 and a stable aggregate SHA-256. "
        "Use this for version or integrity checks instead of running Python/shell commands."
    )
    args_schema: type[BaseModel] = InspectSkillInput
    risk_level: str = "safe"
    skills_dir: str

    def _run(self, skill_name: str) -> str:
        if not _SKILL_NAME.fullmatch(skill_name):
            return json.dumps(
                {"ok": False, "error": "invalid_skill_name"},
                ensure_ascii=False,
            )
        root = Path(self.skills_dir).expanduser().resolve()
        skill_dir = (root / skill_name).resolve()
        try:
            skill_dir.relative_to(root)
        except ValueError:
            return json.dumps(
                {"ok": False, "error": "skill_outside_managed_root"},
                ensure_ascii=False,
            )
        if not skill_dir.is_dir() or skill_dir.is_symlink():
            return json.dumps(
                {"ok": False, "error": "skill_not_found", "skill_name": skill_name},
                ensure_ascii=False,
            )

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file() or skill_md.is_symlink():
            return json.dumps(
                {"ok": False, "error": "skill_manifest_missing", "skill_name": skill_name},
                ensure_ascii=False,
            )

        files: list[dict[str, object]] = []
        total_bytes = 0
        aggregate = hashlib.sha256()
        for path in sorted(skill_dir.rglob("*"), key=lambda item: item.as_posix()):
            relative_path = path.relative_to(skill_dir)
            if (
                "versions" in relative_path.parts
                or "__pycache__" in relative_path.parts
                or path.suffix.lower() in {".pyc", ".pyo"}
            ):
                continue
            if len(files) >= _MAX_FILES:
                return json.dumps(
                    {"ok": False, "error": "skill_file_limit_exceeded", "limit": _MAX_FILES},
                    ensure_ascii=False,
                )
            if path.is_symlink():
                return json.dumps(
                    {
                        "ok": False,
                        "error": "skill_symlink_not_supported",
                        "path": relative_path.as_posix(),
                    },
                    ensure_ascii=False,
                )
            if not path.is_file():
                continue
            size = path.stat().st_size
            total_bytes += size
            if total_bytes > _MAX_TOTAL_BYTES:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "skill_size_limit_exceeded",
                        "limit_bytes": _MAX_TOTAL_BYTES,
                    },
                    ensure_ascii=False,
                )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            relative = relative_path.as_posix()
            aggregate.update(relative.encode("utf-8"))
            aggregate.update(b"\0")
            aggregate.update(digest.encode("ascii"))
            aggregate.update(b"\n")
            files.append({"path": relative, "size": size, "sha256": digest})

        metadata: dict[str, object] = {}
        content = skill_md.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
        if match:
            try:
                parsed = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                parsed = None
            if isinstance(parsed, dict):
                for key in ("name", "version", "description", "license", "homepage", "source"):
                    value = parsed.get(key)
                    if isinstance(value, (str, int, float, bool)):
                        metadata[key] = value

        return json.dumps(
            {
                "ok": True,
                "skill_name": skill_name,
                "path": f"/skills/{skill_name}",
                "metadata": metadata,
                "file_count": len(files),
                "total_bytes": total_bytes,
                "sha256": aggregate.hexdigest(),
                "files": files,
                "executed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def create_skill_inspection_tool(base_dir: Path) -> InspectSkillTool:
    skills_root = base_dir / "skills"
    if base_dir.name == "backend":
        from runtime_identity.paths import PuddingClawPaths
        from tools.skills_scanner import materialize_skill_view

        paths = PuddingClawPaths.from_environment()
        skills_root = paths.data() / "skill-runtime-view"
        materialize_skill_view(base_dir, paths.user_skills(), skills_root)
    return InspectSkillTool(skills_dir=str(skills_root))
