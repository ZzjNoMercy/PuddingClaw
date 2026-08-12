"""Bundled/user Skill discovery with a stable virtual namespace."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

PROTECTED_SKILLS = frozenset({"skill-management", "skill-creator", "skill-creator-pro"})
_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest.update(str(path.relative_to(root)).replace(os.sep, "/").encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _skill(root: Path, *, origin: str, package_root: Path | None = None) -> dict[str, Any] | None:
    skill_md = root / "SKILL.md"
    if not root.is_dir() or root.is_symlink() or not skill_md.is_file() or skill_md.is_symlink():
        return None
    try:
        content = skill_md.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        metadata = yaml.safe_load(parts[1]) if content.startswith("---") and len(parts) == 3 else {}
        metadata = metadata if isinstance(metadata, dict) else {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    skill_id = root.name
    if not _SKILL_ID_RE.fullmatch(skill_id):
        return None
    name = str(metadata.get("name") or skill_id).strip() or skill_id
    physical = root.resolve()
    return {
        "skill_id": skill_id,
        "name": name,
        "description": str(metadata.get("description") or ""),
        "origin": origin,
        "physical_root": str(physical),
        "location": f"/skills/{skill_id}",
        "content_digest": _digest(root),
        "mutable": origin == "user",
        "override_policy": "deny" if name in PROTECTED_SKILLS else "allow",
        "effective": False,
        "project_id": None,
    }


def scan_skill_registry(
    package_root: Path,
    *,
    user_root: Path | None = None,
    project_roots: list[tuple[str, Path]] | None = None,
) -> list[dict[str, Any]]:
    """Return auditable bundled/user/project candidates without writing files."""

    candidates: list[dict[str, Any]] = []
    project_candidates: list[tuple[str, Path, str]] = []
    for project_id, root in (project_roots or []):
        try:
            from projects.registry import project_registry

            if project_registry.is_trusted(project_id):
                project_candidates.append(("project", root, project_id))
        except Exception:
            # Uninitialized or unavailable project registries fail closed.
            continue
    for origin, root, project_id in (
        [("bundled", package_root / "skills", None)]
        + ([ ("user", user_root, None) ] if user_root is not None else [])
        + project_candidates
    ):
        if root is None or not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            item = _skill(child, origin=origin, package_root=package_root)
            if item is None:
                continue
            item["project_id"] = project_id
            candidates.append(item)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        grouped.setdefault(str(item["skill_id"]), []).append(item)
    for skill_id, items in grouped.items():
        if skill_id in PROTECTED_SKILLS and any(item["origin"] != "bundled" for item in items):
            for item in items:
                item["conflict"] = "protected_name_conflict"
            continue
        rank = {"project": 3, "user": 2, "bundled": 1}
        effective = max(items, key=lambda item: rank.get(str(item["origin"]), 0))
        for item in items:
            item["effective"] = item is effective
            item["shadowed_sources"] = [
                {"origin": other["origin"], "physical_root": other["physical_root"], "content_digest": other["content_digest"]}
                for other in items if other is not item
            ]
    return candidates


def resolve_effective_skill_root(
    package_root: Path,
    user_root: Path,
    skill_id: str,
) -> Path | None:
    """Resolve an installed Skill through the same bundled/user precedence as runtime."""

    for item in scan_skill_registry(package_root, user_root=user_root):
        if item["skill_id"] != skill_id or not item.get("effective"):
            continue
        if item.get("conflict"):
            return None
        return Path(str(item["physical_root"]))
    return None


def _write_snapshot(snapshot_path: Path, snapshot: str) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{snapshot_path.name}.", dir=snapshot_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(snapshot)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, snapshot_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def scan_skills(
    base_dir: Path,
    *,
    user_root: Path | None = None,
    snapshot_path: Path | None = None,
) -> str:
    """Scan bundled and user roots; emit only virtual paths in the snapshot.

    The one-argument form remains compatible with focused tests and legacy
    callers. Runtime startup always supplies a separate user root.
    """

    records = scan_skill_registry(base_dir, user_root=user_root)
    visible: dict[str, dict[str, Any]] = {}
    for item in records:
        if item.get("conflict") == "protected_name_conflict":
            continue
        if item.get("effective"):
            visible[str(item["skill_id"])] = item
    lines = ["<available_skills>"]
    for item in sorted(visible.values(), key=lambda value: str(value["skill_id"]).casefold()):
        lines.extend(
            [
                "  <skill>",
                f"    <name>{html.escape(str(item['name']))}</name>",
                f"    <description>{html.escape(str(item['description']))}</description>",
                f"    <location>{html.escape(str(item['location']))}/SKILL.md</location>",
                f"    <origin>{html.escape(str(item['origin']))}</origin>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    snapshot = "\n".join(lines)
    if snapshot_path is None:
        from runtime_identity.paths import PuddingClawPaths

        snapshot_path = PuddingClawPaths.from_environment().skill_management() / "SKILLS_SNAPSHOT.md"
    _write_snapshot(snapshot_path, snapshot)
    return snapshot


def write_skill_registry_manifest(path: Path, records: list[dict[str, Any]]) -> None:
    """Persist non-sensitive registry metadata for diagnostics and UI."""

    payload = {"version": 1, "skills": records}
    _write_snapshot(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def materialize_skill_view(
    package_root: Path,
    user_root: Path,
    target: Path,
) -> list[dict[str, Any]]:
    """Build a disposable read-only overlay containing effective Skills."""

    records = scan_skill_registry(package_root, user_root=user_root)
    target.mkdir(parents=True, exist_ok=True)
    for child in list(target.iterdir()):
        if child.is_dir() and not child.is_symlink():
            import shutil

            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    import shutil

    for record in records:
        if not record.get("effective"):
            continue
        source = Path(str(record["physical_root"]))
        destination = (target / str(record["skill_id"])).resolve()
        if not destination.is_relative_to(target.resolve()):
            raise ValueError(f"skill runtime destination escapes target: {destination}")
        shutil.copytree(source, destination, symlinks=False)
    return records
