"""Recoverable migration from package-relative runtime state to user Home.

The migrator is deliberately copy/verify/switch/retain.  It never deletes a
legacy source and it never lets a conflicting source overwrite a target.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from runtime_identity.paths import PuddingClawPaths

MIGRATION_ID = "runtime-home-v1"
DEFINITIONS_DATA_MIGRATION_ID = "definitions-data-v1"
PROJECTS_MEMORY_MIGRATION_ID = "projects-memory-v1"
PROJECT_TRUST_MIGRATION_ID = "project-trust-v1"
RUNTIME_ARTIFACTS_MIGRATION_ID = "runtime-artifacts-v1"
WORKSPACE_ARTIFACTS_MIGRATION_ID = "workspace-artifacts-v1"
HOME_LAYOUT_MIGRATION_ID = "home-layout-v3"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_json(path: Path) -> dict[str, Any] | list[Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, (dict, list)) else None


def _copy_verified(source: Path, target: Path, conflict_root: Path, report: dict[str, Any]) -> None:
    if target.exists() or target.is_symlink():
        if target.is_file() and source.is_file() and _sha256(target) == _sha256(source):
            report["skipped"] += 1
            return
        conflict = conflict_root / source.name
        conflict.parent.mkdir(parents=True, exist_ok=True)
        if not conflict.exists():
            shutil.copy2(source, conflict)
        report["conflicts"].append({"source": str(source), "target": str(target), "conflict": str(conflict)})
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    shutil.copy2(source, temporary)
    if _sha256(temporary) != _sha256(source):
        temporary.unlink(missing_ok=True)
        raise IOError(f"migration verification failed for {source}")
    os.replace(temporary, target)
    report["copied"] += 1


def migrate_runtime_home(package_root: Path, paths: PuddingClawPaths) -> dict[str, Any]:
    """Migrate Sessions and user-managed Skills once, preserving old sources."""

    paths.ensure_layout()
    marker = paths.migrations() / f"{MIGRATION_ID}.json"
    lock = paths.migrations() / f"{MIGRATION_ID}.lock"
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"migration already running: {lock}") from exc
    os.close(fd)
    report: dict[str, Any] = {
        "version": 1,
        "migration": MIGRATION_ID,
        "started_at": time.time(),
        "source": str(package_root),
        "target": str(paths.root),
        "copied": 0,
        "skipped": 0,
        "conflicts": [],
        "diagnostics": [],
    }
    try:
        legacy_sessions = package_root / "sessions"
        if legacy_sessions.is_dir():
            for source in sorted(legacy_sessions.iterdir()):
                if source.name in {"traces", "archive"}:
                    continue
                if not source.is_file() or source.is_symlink() or source.suffix != ".json":
                    report["diagnostics"].append({"path": str(source), "reason": "ignored_non_session_entry"})
                    continue
                if _safe_json(source) is None:
                    report["diagnostics"].append({"path": str(source), "reason": "invalid_json"})
                    continue
                _copy_verified(source, paths.sessions() / source.name, paths.migrations() / f"{MIGRATION_ID}-conflicts/sessions", report)
            for kind, target_root in (("traces", paths.session_traces()), ("archive", paths.session_archive())):
                source_root = legacy_sessions / kind
                if not source_root.is_dir():
                    continue
                for source in sorted(source_root.iterdir()):
                    if source.is_file() and not source.is_symlink() and source.suffix == ".json" and _safe_json(source) is not None:
                        _copy_verified(source, target_root / source.name, paths.migrations() / f"{MIGRATION_ID}-conflicts/sessions/{kind}", report)

        legacy_skills = package_root / "skills"
        if legacy_skills.is_dir():
            for source in sorted(legacy_skills.iterdir()):
                if not source.is_dir() or source.is_symlink() or not (source / "SKILL.md").is_file():
                    continue
                # The legacy tree was a mixed package/user namespace.  The
                # registry is the only authoritative signal for user-owned
                # content; anything else is retained for explicit review.
            legacy_registry = package_root / "data" / "skill-management" / "registry.json"
            registered: set[str] = set()
            payload = _safe_json(legacy_registry)
            if isinstance(payload, dict):
                raw = payload.get("skills") or payload.get("installed") or payload
                if isinstance(raw, dict):
                    registered = {str(key) for key in raw}
                elif isinstance(raw, list):
                    registered = {str(item.get("skill_name") or item.get("name")) for item in raw if isinstance(item, dict)}
            legacy_skill_ids = {
                source.name
                for source in legacy_skills.iterdir()
                if source.is_dir() and not source.is_symlink() and (source / "SKILL.md").is_file()
            }
            for skill_id in sorted(legacy_skill_ids - registered):
                source = legacy_skills / skill_id
                conflict = paths.migrations() / f"{MIGRATION_ID}-conflicts/skills/unclassified" / skill_id
                if not conflict.exists():
                    shutil.copytree(source, conflict, symlinks=False)
                report["diagnostics"].append({
                    "path": str(source),
                    "reason": "unclassified_legacy_skill",
                    "review_copy": str(conflict),
                })
            for skill_id in sorted(registered):
                source = legacy_skills / skill_id
                if skill_id not in legacy_skill_ids:
                    report["diagnostics"].append({"skill_id": skill_id, "reason": "registered_skill_missing"})
                    continue
                if source.is_dir() and not source.is_symlink():
                    target = paths.user_skills() / skill_id
                    if target.exists():
                        if target.is_dir() and not target.is_symlink() and (target / "SKILL.md").is_file() and _sha256(target / "SKILL.md") == _sha256(source / "SKILL.md"):
                            report["skipped"] += 1
                        else:
                            report["conflicts"].append({"source": str(source), "target": str(target), "reason": "skill_conflict"})
                    else:
                        shutil.copytree(source, target, symlinks=False)
                        report["copied"] += 1
        report["completed_at"] = time.time()
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, marker)
        return report
    finally:
        lock.unlink(missing_ok=True)


def _copy_tree_verified(
    source_root: Path,
    target_root: Path,
    conflict_root: Path,
    report: dict[str, Any],
    *,
    include_file: Any | None = None,
) -> None:
    """Copy a runtime tree without replacing user-owned files.

    This is intentionally a physical migration, not an overlay.  The source
    tree remains untouched as a rollback copy, while all future reads/writes
    use ``target_root``.
    """

    if not source_root.is_dir():
        return
    for source in sorted(source_root.rglob("*")):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(source_root)
        if include_file is not None and not include_file(relative):
            continue
        _copy_verified(source, target_root / relative, conflict_root / relative.parent, report)


def migrate_definitions_and_data(package_root: Path, paths: PuddingClawPaths) -> dict[str, Any]:
    """Move all smart-query definitions and runtime data into user Home.

    Definitions are copied as complete trees because the user explicitly owns
    the post-migration namespace.  Runtime databases, query artifacts, vector
    indexes, checkpoints and job state are copied as well.  No source is
    deleted, so an interrupted migration is recoverable and repeatable.
    """

    paths.ensure_layout()
    marker = paths.migrations() / f"{DEFINITIONS_DATA_MIGRATION_ID}.json"
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    lock = paths.migrations() / f"{DEFINITIONS_DATA_MIGRATION_ID}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"migration already running: {lock}") from exc
    os.close(fd)

    report: dict[str, Any] = {
        "version": 1,
        "migration": DEFINITIONS_DATA_MIGRATION_ID,
        "started_at": time.time(),
        "source": str(package_root),
        "target": str(paths.root),
        "copied": 0,
        "skipped": 0,
        "conflicts": [],
        "diagnostics": [],
        "mappings": [],
    }
    try:
        mappings = [
            (package_root / "semantic-assets", paths.user_definitions() / "semantic-assets"),
            (package_root / "analytics-models", paths.user_definitions() / "analytics-models"),
            (package_root / "sql-guardrails", paths.user_definitions() / "sql-guardrails"),
            (package_root / "data", paths.data()),
            (package_root / "storage" / "knowledge_index", paths.state() / "knowledge_index"),
            (package_root / "storage" / "knowledge_search", paths.state() / "knowledge-search"),
            (package_root / "storage" / "memory_index", paths.state() / "memory_index"),
        ]
        for source, target in mappings:
            before = report["copied"]
            _copy_tree_verified(
                source,
                target,
                paths.migrations() / f"{DEFINITIONS_DATA_MIGRATION_ID}-conflicts" / target.name,
                report,
            )
            report["mappings"].append({
                "source": str(source),
                "target": str(target),
                "copied": report["copied"] - before,
            })

        # backend/knowledge is a Python package in this checkout, but it also
        # historically held imported Markdown/JSON knowledge documents.  Only
        # data-like files cross that boundary; executable source remains code.
        _copy_tree_verified(
            package_root / "knowledge",
            paths.knowledge(),
            paths.migrations() / f"{DEFINITIONS_DATA_MIGRATION_ID}-conflicts/knowledge",
            report,
            include_file=lambda relative: (
                "__pycache__" not in relative.parts
                and relative.suffix.lower() not in {".py", ".pyc", ".pyo"}
            ),
        )
        report["completed_at"] = time.time()
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, marker)
        return report
    finally:
        lock.unlink(missing_ok=True)


def migrate_projects_and_memory(package_root: Path, paths: PuddingClawPaths) -> dict[str, Any]:
    """Migrate the legacy project registry and all durable Memory sources."""

    paths.ensure_layout()
    marker = paths.migrations() / f"{PROJECTS_MEMORY_MIGRATION_ID}.json"
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    lock = paths.migrations() / f"{PROJECTS_MEMORY_MIGRATION_ID}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"migration already running: {lock}") from exc
    os.close(fd)

    report: dict[str, Any] = {
        "version": 1,
        "migration": PROJECTS_MEMORY_MIGRATION_ID,
        "started_at": time.time(),
        "copied": 0,
        "skipped": 0,
        "conflicts": [],
        "diagnostics": [],
    }
    try:
        legacy_projects = package_root / "data" / "projects.json"
        target_projects = paths.project_registry()
        if legacy_projects.is_file():
            legacy_payload = _safe_json(legacy_projects)
            target_payload = _safe_json(target_projects)
            if isinstance(legacy_payload, dict):
                if not isinstance(target_payload, dict) or not target_payload:
                    target_projects.parent.mkdir(parents=True, exist_ok=True)
                    target_projects.write_text(
                        json.dumps(legacy_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    report["copied"] += 1
                else:
                    merged = dict(legacy_payload)
                    merged.update(target_payload)
                    if merged != target_payload:
                        conflict = paths.migrations() / f"{PROJECTS_MEMORY_MIGRATION_ID}-conflicts/projects.json"
                        conflict.parent.mkdir(parents=True, exist_ok=True)
                        conflict.write_text(
                            json.dumps(legacy_payload, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        report["conflicts"].append({
                            "source": str(legacy_projects),
                            "target": str(target_projects),
                            "conflict": str(conflict),
                        })

        # The old global memory had two locations.  Prefer the authored
        # backend/memory/MEMORY.md over the generated DeepAgents placeholder.
        memory_sources = [
            package_root / "memory" / "MEMORY.md",
            package_root / "data" / "deepagents-memory" / "global" / "MEMORY.md",
        ]
        global_source = next((path for path in memory_sources if path.is_file() and path.stat().st_size > 0), None)
        global_target = paths.memory() / "global" / "MEMORY.md"
        if global_source is not None:
            _copy_verified(
                global_source,
                global_target,
                paths.migrations() / f"{PROJECTS_MEMORY_MIGRATION_ID}-conflicts/memory",
                report,
            )

        # Project-scoped memory was previously stored beside the package data;
        # the runtime now reads the user-owned memory/projects namespace.
        legacy_project_memory = package_root / "data" / "deepagents-memory" / "projects"
        _copy_tree_verified(
            legacy_project_memory,
            paths.memory() / "projects",
            paths.migrations() / f"{PROJECTS_MEMORY_MIGRATION_ID}-conflicts/project-memory",
            report,
        )

        report["completed_at"] = time.time()
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, marker)
        return report
    finally:
        lock.unlink(missing_ok=True)


def _project_identity_digest(path: Path) -> str:
    """Match ProjectRegistry's stable local-directory identity contract."""

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


def migrate_project_trust_registry(paths: PuddingClawPaths) -> dict[str, Any]:
    """Preserve trust for projects registered before the trust schema existed.

    The one-shot marker is important: records imported after this upgrade do
    not inherit trust merely because they omit the new fields.
    """

    paths.ensure_layout()
    marker = paths.migrations() / f"{PROJECT_TRUST_MIGRATION_ID}.json"
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    report: dict[str, Any] = {
        "version": 1,
        "migration": PROJECT_TRUST_MIGRATION_ID,
        "started_at": time.time(),
        "upgraded": 0,
        "unchanged": 0,
        "unavailable": 0,
    }
    registry_path = paths.project_registry()
    payload = _safe_json(registry_path)
    records = dict(payload) if isinstance(payload, dict) else {}
    changed = False

    for project_id, raw in list(records.items()):
        if not isinstance(raw, dict) or "trust_state" in raw:
            report["unchanged"] += 1
            continue
        next_raw = dict(raw)
        project_path = Path(str(raw.get("path") or "")).expanduser().resolve()
        if project_path.is_dir():
            next_raw["trust_state"] = "trusted"
            next_raw["identity_digest"] = _project_identity_digest(project_path)
            next_raw["trust_source"] = "legacy_registry_migration"
            report["upgraded"] += 1
        else:
            next_raw["trust_state"] = "pending"
            next_raw["identity_digest"] = ""
            report["unavailable"] += 1
        records[str(project_id)] = next_raw
        changed = True

    if changed:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_registry = registry_path.with_name(f".{registry_path.name}.{os.getpid()}.tmp")
        temporary_registry.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_registry, registry_path)

    report["completed_at"] = time.time()
    temporary_marker = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary_marker.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary_marker, marker)
    return report


def migrate_runtime_artifacts(package_root: Path, paths: PuddingClawPaths) -> dict[str, Any]:
    """Copy legacy logs into Home before the package runtime tree is retired."""

    paths.ensure_layout()
    marker = paths.migrations() / f"{RUNTIME_ARTIFACTS_MIGRATION_ID}.json"
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    lock = paths.migrations() / f"{RUNTIME_ARTIFACTS_MIGRATION_ID}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"migration already running: {lock}") from exc
    os.close(fd)
    report: dict[str, Any] = {
        "version": 1,
        "migration": RUNTIME_ARTIFACTS_MIGRATION_ID,
        "started_at": time.time(),
        "source": str(package_root / "logs"),
        "target": str(paths.logs()),
        "copied": 0,
        "skipped": 0,
        "conflicts": [],
        "diagnostics": [],
    }
    try:
        _copy_tree_verified(
            package_root / "logs",
            paths.logs(),
            paths.migrations() / f"{RUNTIME_ARTIFACTS_MIGRATION_ID}-conflicts/logs",
            report,
        )
        report["completed_at"] = time.time()
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, marker)
        return report
    finally:
        lock.unlink(missing_ok=True)


def migrate_workspace_artifacts(package_root: Path, paths: PuddingClawPaths) -> dict[str, Any]:
    """Copy authored workspace files into the user-owned default workspace."""

    paths.ensure_layout()
    marker = paths.migrations() / f"{WORKSPACE_ARTIFACTS_MIGRATION_ID}.json"
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    lock = paths.migrations() / f"{WORKSPACE_ARTIFACTS_MIGRATION_ID}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"migration already running: {lock}") from exc
    os.close(fd)
    report: dict[str, Any] = {
        "version": 1,
        "migration": WORKSPACE_ARTIFACTS_MIGRATION_ID,
        "started_at": time.time(),
        "source": str(package_root / "workspace"),
        "target": str(paths.agent_workspaces() / "unscoped" / "default"),
        "copied": 0,
        "skipped": 0,
        "conflicts": [],
        "diagnostics": [],
    }
    try:
        source_root = package_root / "workspace"
        target_root = paths.agent_workspaces() / "unscoped" / "default"
        _copy_tree_verified(
            source_root,
            target_root,
            paths.migrations() / f"{WORKSPACE_ARTIFACTS_MIGRATION_ID}-conflicts/workspace",
            report,
            include_file=lambda relative: relative.name != "SKILLS_SNAPSHOT.md",
        )
        snapshot = source_root / "SKILLS_SNAPSHOT.md"
        if snapshot.is_file():
            _copy_verified(
                snapshot,
                paths.skill_management() / snapshot.name,
                paths.migrations() / f"{WORKSPACE_ARTIFACTS_MIGRATION_ID}-conflicts/skill-management",
                report,
            )
        report["completed_at"] = time.time()
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, marker)
        return report
    finally:
        lock.unlink(missing_ok=True)


def migrate_home_layout(paths: PuddingClawPaths) -> dict[str, Any]:
    """Normalize early Home layouts without retaining runtime read fallbacks."""

    paths.ensure_layout()
    marker = paths.migrations() / f"{HOME_LAYOUT_MIGRATION_ID}.json"
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    lock = paths.migrations() / f"{HOME_LAYOUT_MIGRATION_ID}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"migration already running: {lock}") from exc
    os.close(fd)
    report: dict[str, Any] = {
        "version": 3,
        "migration": HOME_LAYOUT_MIGRATION_ID,
        "started_at": time.time(),
        "source": str(paths.root),
        "target": str(paths.root),
        "copied": 0,
        "skipped": 0,
        "conflicts": [],
        "diagnostics": [],
        "mappings": [],
    }
    try:
        directory_mappings = (
            (paths.data() / "stats", paths.usage()),
            (paths.data() / "database-query-results", paths.query_results()),
            (paths.data() / "checkpoints", paths.state() / "checkpoints"),
            (paths.cache() / "memory-index", paths.memory_index()),
            (paths.root / "storage" / "knowledge_index", paths.knowledge_index()),
            (paths.root / "storage" / "knowledge_search", paths.knowledge_search()),
            (paths.root / "storage" / "memory_index", paths.memory_index()),
        )
        for source, target in directory_mappings:
            before = report["copied"]
            _copy_tree_verified(
                source,
                target,
                paths.migrations() / f"{HOME_LAYOUT_MIGRATION_ID}-conflicts" / target.name,
                report,
            )
            report["mappings"].append({
                "source": str(source),
                "target": str(target),
                "copied": report["copied"] - before,
            })

        file_mappings = (
            (paths.data() / "evaluation-settings.json", paths.evaluation_settings()),
            (paths.data() / "evaluation.db", paths.databases() / "evaluation.sqlite3"),
            (paths.data() / "puddingclaw.db", paths.databases() / "catalog.sqlite3"),
            (paths.data() / "token_usage.db", paths.usage() / "token_usage.db"),
            (paths.logs() / "token_usage.db", paths.usage() / "token_usage.db"),
            (paths.data() / "projects.json", paths.project_registry()),
            (paths.data() / "headless-idempotency.json", paths.state() / "headless-idempotency.json"),
            (paths.data() / "worker-access-logs.sqlite3", paths.databases() / "worker-access-logs.sqlite3"),
        )
        for source, target in file_mappings:
            if not source.is_file() or source.is_symlink():
                continue
            before = report["copied"]
            _copy_verified(
                source,
                target,
                paths.migrations() / f"{HOME_LAYOUT_MIGRATION_ID}-conflicts" / target.parent.name,
                report,
            )
            report["mappings"].append({
                "source": str(source),
                "target": str(target),
                "copied": report["copied"] - before,
            })

        legacy_global_memory = paths.memory() / "MEMORY.md"
        global_memory = paths.memory() / "global" / "MEMORY.md"
        if legacy_global_memory.is_file() and not legacy_global_memory.is_symlink():
            generated_placeholder = False
            if global_memory.is_file() and not global_memory.is_symlink():
                try:
                    placeholder_text = global_memory.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    placeholder_text = ""
                generated_placeholder = (
                    placeholder_text.startswith("# Global Memory")
                    and "This file is injected into the Agent's system prompt" in placeholder_text
                    and "##" not in placeholder_text
                )
            before = report["copied"]
            if generated_placeholder:
                temporary = global_memory.with_name(f".{global_memory.name}.{os.getpid()}.tmp")
                shutil.copy2(legacy_global_memory, temporary)
                if _sha256(temporary) != _sha256(legacy_global_memory):
                    temporary.unlink(missing_ok=True)
                    raise IOError(f"migration verification failed for {legacy_global_memory}")
                os.replace(temporary, global_memory)
                report["copied"] += 1
            else:
                _copy_verified(
                    legacy_global_memory,
                    global_memory,
                    paths.migrations() / f"{HOME_LAYOUT_MIGRATION_ID}-conflicts/memory",
                    report,
                )
            report["mappings"].append({
                "source": str(legacy_global_memory),
                "target": str(global_memory),
                "copied": report["copied"] - before,
            })

        from runtime_identity.paths import trusted_owner_user_id

        legacy_access = paths.data() / "worker-access-keys.json"
        owner_access = paths.owner_access(trusted_owner_user_id()) / "worker-access-keys.json"
        if legacy_access.is_file():
            owner_access.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            owner_access.parent.chmod(0o700)
            _copy_verified(
                legacy_access,
                owner_access,
                paths.migrations() / f"{HOME_LAYOUT_MIGRATION_ID}-conflicts/access",
                report,
            )
            if owner_access.is_file():
                owner_access.chmod(0o600)

        report["completed_at"] = time.time()
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, marker)
        return report
    finally:
        lock.unlink(missing_ok=True)
