"""Managed Skill installation and update plans.

Remote content is downloaded into a bounded staging area first.  The managed
``skills/`` directory is only changed by :meth:`commit`, after Harness has
approved the immutable plan id and digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import stat
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlsplit

import httpx
import yaml

from utils.network_safety import is_public_or_trusted_https_fake_ip

_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RELATIVE_ASSET = re.compile(r"(?<![A-Za-z0-9_./-])((?:scripts|assets|references|templates)/[A-Za-z0-9_./-]+)")
_MAX_FILES = 512
_MAX_TOTAL_BYTES = 20 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
_PLAN_TTL_SECONDS = 60 * 60
_SNAPSHOT_RETENTION = 10
_FORBIDDEN_SUFFIXES = {".exe", ".dll", ".dylib", ".so", ".bat", ".cmd", ".ps1"}
_DISCOVERY_SCHEMA_V2 = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"
_WELL_KNOWN_PATHS = (".well-known/agent-skills", ".well-known/skills")


class SkillManagementError(RuntimeError):
    """A stable, user-displayable managed Skill error."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message}


def _safe_relative(value: str) -> str:
    normalized = value.replace("\\", "/").strip().lstrip("./")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise SkillManagementError("invalid_relative_path", value)
    return path.as_posix()


def _json_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class SkillManagementService:
    """Prepare immutable plans and atomically commit them to ``skills/``."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.expanduser().resolve()
        self.skills_dir = self.base_dir / "skills"
        self.state_dir = self.base_dir / "data" / "skill-management"
        self.plans_dir = self.state_dir / "plans"
        self.snapshots_dir = self.state_dir / "snapshots"
        self._lock = threading.RLock()

    def prepare(
        self,
        *,
        action: Literal["install", "update"],
        source: str | None = None,
        skill_name: str | None = None,
        ref: str | None = None,
        subpath: str | None = None,
        files: list[str] | None = None,
        source_digest: str | None = None,
        request_context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        requested_source = (source or "").strip()
        provenance = (
            self._load_provenance(skill_name) if action == "update" and skill_name and not requested_source else None
        )
        if not requested_source and provenance:
            requested_source = str(provenance.get("source") or "").strip()
            ref = ref if ref is not None else str(provenance.get("ref") or "main")
            subpath = subpath if subpath is not None else str(provenance.get("subpath") or "")
            files = files if files is not None else list(provenance.get("files") or [])
            source_digest = (
                source_digest
                if source_digest is not None
                else str(provenance.get("source_digest") or "") or None
            )
        if not requested_source:
            raise SkillManagementError("source_required")
        resolved_ref = (ref or "main").strip() or "main"
        resolved_subpath = (subpath or "").strip()
        resolved_files = list(files or [])
        normalized_context = {
            key: str((request_context or {}).get(key) or "")
            for key in ("session_id", "query_id", "run_id")
            if str((request_context or {}).get(key) or "")
        }
        request_key = (
            _json_digest(
                {
                    "action": action,
                    "source": requested_source,
                    "skill_name": skill_name or "",
                    "ref": resolved_ref,
                    "subpath": resolved_subpath,
                    "files": resolved_files,
                    "source_digest": source_digest or "",
                    "request_context": normalized_context,
                }
            )
            if normalized_context.get("session_id") and normalized_context.get("query_id")
            else ""
        )
        if skill_name and not _SKILL_NAME.fullmatch(skill_name):
            raise SkillManagementError("invalid_skill_name", skill_name)

        self._cleanup_expired_plans()
        if request_key:
            existing = self._find_plan_by_request_key(request_key)
            if existing is not None:
                return self._public_plan(existing)
        plan_id = f"skill-plan-{uuid.uuid4().hex[:16]}"
        plan_dir = self.plans_dir / plan_id
        payload_dir = plan_dir / "payload"
        plan_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._stage_source(
                source=requested_source,
                target=payload_dir,
                ref=resolved_ref,
                subpath=resolved_subpath,
                files=resolved_files,
                source_digest=source_digest,
            )
            staged = self._manifest(payload_dir)
            declared_name = str(staged["metadata"].get("name") or "").strip()
            resolved_name = skill_name or declared_name
            if not resolved_name or not _SKILL_NAME.fullmatch(resolved_name):
                raise SkillManagementError(
                    "skill_name_missing",
                    "SKILL.md must declare a valid name or skill_name must be supplied",
                )
            if skill_name and declared_name and skill_name != declared_name:
                raise SkillManagementError(
                    "skill_name_mismatch",
                    f"requested {skill_name!r}, SKILL.md declares {declared_name!r}",
                )

            target = self.skills_dir / resolved_name
            exists = target.is_dir() and not target.is_symlink()
            if action == "install" and exists:
                raise SkillManagementError("skill_already_exists", resolved_name)
            if action == "update" and not exists:
                raise SkillManagementError("skill_not_found", resolved_name)
            current = self._manifest(target) if exists else None
            diff = self._diff(current, staged)
            now = time.time()
            plan: dict[str, Any] = {
                "plan_id": plan_id,
                "action": action,
                "skill_name": resolved_name,
                "source": requested_source,
                "ref": resolved_ref,
                "subpath": resolved_subpath,
                "files": resolved_files,
                "created_at": now,
                "expires_at": now + _PLAN_TTL_SECONDS,
                "status": "prepared",
                "baseline_sha256": current["sha256"] if current else None,
                "staged_sha256": staged["sha256"],
                "staged_metadata": staged["metadata"],
                "diff": diff,
            }
            if source_digest:
                plan["source_digest"] = source_digest
            if normalized_context:
                plan["request_context"] = normalized_context
            if request_key:
                plan["request_key"] = request_key
            plan["plan_sha256"] = _json_digest(plan)
            with self._lock:
                # Tool retries can replay the exact prepare call. Keep one
                # durable plan/card for the originating Agent request.
                existing = self._find_plan_by_request_key(request_key) if request_key else None
                if existing is not None:
                    shutil.rmtree(plan_dir, ignore_errors=True)
                    return self._public_plan(existing)
                self._write_json(plan_dir / "plan.json", plan)
            return self._public_plan(plan)
        except Exception:
            shutil.rmtree(plan_dir, ignore_errors=True)
            raise

    def prepare_npx_skills_add(
        self,
        *,
        source: str,
        skill_names: list[str] | None = None,
        yes: bool = False,
        install_all: bool = False,
        list_only: bool = False,
        request_context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Take over ``npx skills add`` without allowing it to write the workspace.

        The generic CLI's agent/scope/copy flags are intentionally irrelevant:
        PuddingClaw has one managed destination, ``base_dir / skills``.  Each
        discovered Skill receives its own immutable, Session-bound plan so the
        existing plan-card confirmation and atomic commit path remains the only
        write authority.
        """

        requested_source = source.strip()
        if not requested_source:
            raise SkillManagementError("source_required")
        discovered = self._discover_well_known_skills(requested_source)
        if not discovered:
            raise SkillManagementError(
                "unsupported_npx_skill_source",
                "Managed npx takeover currently requires an HTTP(S) well-known Agent Skills endpoint",
            )

        available = [str(item["name"]) for item in discovered]
        requested = [name.strip() for name in (skill_names or []) if name.strip()]
        requested_keys = {name.casefold() for name in requested if name != "*"}
        if "*" in requested or install_all:
            selected = discovered
        elif requested_keys:
            selected = [item for item in discovered if str(item["name"]).casefold() in requested_keys]
        elif len(discovered) == 1 or yes:
            selected = discovered
        else:
            return {
                "ok": True,
                "managed_by": "skill_management",
                "intercepted": True,
                "source": requested_source,
                "selection_required": True,
                "available_skills": available,
                "plans": [],
            }

        selected_keys = {str(item["name"]).casefold() for item in selected}
        missing = sorted(name for name in requested if name != "*" and name.casefold() not in selected_keys)
        if missing:
            raise SkillManagementError(
                "skill_not_found_in_source",
                f"No matching skills found for: {', '.join(missing)}",
            )
        if list_only:
            return {
                "ok": True,
                "managed_by": "skill_management",
                "intercepted": True,
                "source": requested_source,
                "list_only": True,
                "available_skills": available,
                "plans": [],
            }

        plans: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for item in selected:
            name = str(item["name"])
            target = self.skills_dir / name
            action: Literal["install", "update"] = (
                "update" if target.is_dir() and not target.is_symlink() else "install"
            )
            try:
                plans.append(
                    self.prepare(
                        action=action,
                        source=str(item["source"]),
                        skill_name=name,
                        files=list(item.get("files") or []),
                        source_digest=str(item.get("digest") or "") or None,
                        request_context=request_context,
                    )
                )
            except SkillManagementError as exc:
                errors.append({"skill_name": name, "error": exc.code, "message": exc.message})
        return {
            "ok": bool(plans) and not errors,
            "managed_by": "skill_management",
            "intercepted": True,
            "source": requested_source,
            "available_skills": available,
            "plans": plans,
            "errors": errors,
        }

    def _discover_well_known_skills(self, source: str) -> list[dict[str, Any]]:
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return []
        base_path = parsed.path.rstrip("/")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        candidates: list[tuple[str, str, str]] = []
        for well_known_path in _WELL_KNOWN_PATHS:
            candidates.append(
                (
                    f"{origin}{base_path}/{well_known_path}/index.json",
                    f"{origin}{base_path}",
                    well_known_path,
                )
            )
            if base_path:
                candidates.append(
                    (
                        f"{origin}/{well_known_path}/index.json",
                        origin,
                        well_known_path,
                    )
                )
        for index_url, resolved_base, well_known_path in candidates:
            try:
                raw = self._download(index_url)
                payload = json.loads(raw.decode("utf-8"))
                discovered = self._normalize_well_known_index(
                    payload,
                    index_url=index_url,
                    resolved_base=resolved_base,
                    well_known_path=well_known_path,
                )
            except (SkillManagementError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if discovered:
                return discovered
        return []

    @staticmethod
    def _normalize_well_known_index(
        payload: Any,
        *,
        index_url: str,
        resolved_base: str,
        well_known_path: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
            return []
        schema = payload.get("$schema")
        discovered: list[dict[str, Any]] = []
        if schema == _DISCOVERY_SCHEMA_V2:
            for raw in payload["skills"]:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "")
                kind = str(raw.get("type") or "")
                description = raw.get("description")
                digest = str(raw.get("digest") or "")
                url = str(raw.get("url") or "")
                if (
                    not _SKILL_NAME.fullmatch(name)
                    or kind not in {"skill-md", "archive"}
                    or not isinstance(description, str)
                    or not description
                    or len(description) > 1024
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                    or not url
                ):
                    continue
                artifact_url = urljoin(index_url, url)
                if kind == "archive" and not urlsplit(artifact_url).path.lower().endswith(".zip"):
                    continue
                discovered.append(
                    {
                        "name": name,
                        "source": artifact_url,
                        "files": [],
                        "digest": digest,
                    }
                )
            return discovered
        if schema is not None:
            return []
        for raw in payload["skills"]:
            if not isinstance(raw, dict):
                return []
            name = str(raw.get("name") or "")
            description = raw.get("description")
            files = raw.get("files")
            if (
                not _SKILL_NAME.fullmatch(name)
                or not isinstance(description, str)
                or not description
                or not isinstance(files, list)
                or not files
                or len(files) > _MAX_FILES
            ):
                return []
            try:
                normalized_files = [_safe_relative(str(path)) for path in files]
            except SkillManagementError:
                return []
            if not any(path.casefold() == "skill.md" for path in normalized_files):
                return []
            discovered.append(
                {
                    "name": name,
                    "source": f"{resolved_base.rstrip('/')}/{well_known_path}/{name}",
                    "files": [
                        path
                        for path in normalized_files
                        if path.casefold() not in {"skill.md", "readme.md"}
                    ],
                    "digest": "",
                }
            )
        return discovered

    def preview(self, plan_id: str) -> dict[str, Any] | None:
        try:
            plan = self._load_plan(plan_id)
        except SkillManagementError:
            return None
        return self._public_plan(plan)

    def preview_for_session(self, plan_id: str, session_id: str) -> dict[str, Any]:
        """Return a plan only to the Session that created it."""

        self._cleanup_expired_plans()
        plan = self._load_plan(plan_id)
        self._validate_plan_owner(plan, session_id)
        return self._public_plan(plan)

    def commit(
        self,
        *,
        action: Literal["install", "update"],
        plan_id: str,
        plan_sha256: str,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            plan = self._load_plan(plan_id)
            if expected_session_id is not None:
                self._validate_plan_owner(plan, expected_session_id)
            elif isinstance(plan.get("request_context"), dict) and plan["request_context"].get("session_id"):
                raise SkillManagementError(
                    "plan_requires_structured_commit",
                    "Session-bound plans must be confirmed through the structured plan card",
                )
            if plan.get("action") != action:
                raise SkillManagementError("plan_action_mismatch")
            if plan.get("status") != "prepared":
                raise SkillManagementError("plan_already_consumed")
            if not plan_sha256 or plan.get("plan_sha256") != plan_sha256:
                raise SkillManagementError("plan_digest_mismatch")
            if time.time() > float(plan.get("expires_at") or 0):
                raise SkillManagementError("plan_expired")

            plan_dir = self.plans_dir / plan_id
            payload_dir = plan_dir / "payload"
            staged = self._manifest(payload_dir)
            if staged["sha256"] != plan.get("staged_sha256"):
                raise SkillManagementError("staged_payload_changed")

            skill_name = str(plan["skill_name"])
            target = self.skills_dir / skill_name
            current = self._manifest(target) if target.is_dir() and not target.is_symlink() else None
            if action == "install" and current is not None:
                raise SkillManagementError("skill_already_exists", skill_name)
            if action == "update":
                if current is None:
                    raise SkillManagementError("skill_not_found", skill_name)
                if current["sha256"] != plan.get("baseline_sha256"):
                    raise SkillManagementError(
                        "installed_skill_changed",
                        "The installed Skill changed after this update plan was prepared",
                    )

            self.skills_dir.mkdir(parents=True, exist_ok=True)
            incoming = self.skills_dir / f".{skill_name}.incoming-{uuid.uuid4().hex[:10]}"
            old = self.skills_dir / f".{skill_name}.old-{uuid.uuid4().hex[:10]}"
            snapshot: Path | None = None
            shutil.copytree(payload_dir, incoming, symlinks=False)
            try:
                if action == "update":
                    snapshot = self._snapshot(skill_name, target, str(current["sha256"]))
                    os.replace(target, old)
                    try:
                        os.replace(incoming, target)
                    except Exception:
                        os.replace(old, target)
                        raise
                    shutil.rmtree(old, ignore_errors=True)
                else:
                    os.replace(incoming, target)
            finally:
                shutil.rmtree(incoming, ignore_errors=True)

            installed = self._manifest(target)
            plan["status"] = "committed"
            plan["committed_at"] = time.time()
            plan["installed_sha256"] = installed["sha256"]
            plan["installed_path"] = f"/skills/{skill_name}"
            if snapshot is not None:
                plan["snapshot_id"] = snapshot.name
            self._write_json(plan_dir / "plan.json", plan)
            shutil.rmtree(payload_dir, ignore_errors=True)
            if snapshot is not None:
                self._prune_snapshots(skill_name)
            self._refresh_skill_snapshot()
            provenance_recorded = self._record_provenance(plan)
            result = self._public_plan(plan)
            result.update(
                {
                    "ok": True,
                    "installed_path": f"/skills/{skill_name}",
                    "installed_sha256": installed["sha256"],
                    "snapshot_id": plan.get("snapshot_id"),
                    "provenance_recorded": provenance_recorded,
                    "note": "The new Skill is available to subsequent Agent runs.",
                }
            )
            return result

    def cancel(
        self,
        *,
        plan_id: str,
        plan_sha256: str,
        expected_session_id: str,
    ) -> dict[str, Any]:
        """Persist cancellation so a historical plan card never looks pending."""

        with self._lock:
            self._cleanup_expired_plans()
            plan = self._load_plan(plan_id)
            self._validate_plan_owner(plan, expected_session_id)
            if not plan_sha256 or plan.get("plan_sha256") != plan_sha256:
                raise SkillManagementError("plan_digest_mismatch")
            status = str(plan.get("status") or "")
            if status == "cancelled":
                return self._public_plan(plan)
            if status == "committed":
                raise SkillManagementError("plan_already_committed")
            if status == "expired":
                return self._public_plan(plan)
            if status != "prepared":
                raise SkillManagementError("plan_already_consumed")
            plan["status"] = "cancelled"
            plan["cancelled_at"] = time.time()
            plan_dir = self.plans_dir / plan_id
            self._write_json(plan_dir / "plan.json", plan)
            shutil.rmtree(plan_dir / "payload", ignore_errors=True)
            return self._public_plan(plan)

    def delete_session_plans(self, session_id: str) -> int:
        """Delete all staged/audit plans owned by a deleted Session."""

        removed = 0
        with self._lock:
            if not self.plans_dir.is_dir():
                return 0
            for item in list(self.plans_dir.iterdir()):
                if not item.is_dir() or item.is_symlink() or not item.name.startswith("skill-plan-"):
                    continue
                try:
                    plan = self._load_plan(item.name)
                except SkillManagementError:
                    continue
                context = plan.get("request_context")
                owner = str(context.get("session_id") or "") if isinstance(context, dict) else ""
                if owner != session_id:
                    continue
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        return removed

    def _stage_source(
        self,
        *,
        source: str,
        target: Path,
        ref: str,
        subpath: str,
        files: list[str],
        source_digest: str | None = None,
    ) -> None:
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"}:
            raise SkillManagementError("unsupported_source", "Only HTTP(S) and GitHub sources are supported")
        host = (parsed.hostname or "").lower()
        if host in {"github.com", "www.github.com"}:
            owner, repo, github_ref, github_subpath = self._parse_github(source, ref, subpath)
            archive_url = f"https://codeload.github.com/{owner}/{repo}/zip/{quote(github_ref, safe='')}"
            archive = self._download(archive_url)
            self._extract_archive(archive, target, subpath=github_subpath, github_archive=True)
            return
        if parsed.path.lower().endswith("/skill.md"):
            content = self._download(source)
            self._verify_source_digest(content, source_digest)
            target.mkdir(parents=True, exist_ok=False)
            (target / "SKILL.md").write_bytes(content)
            return
        if parsed.path.lower().endswith(".zip"):
            archive = self._download(source)
            self._verify_source_digest(archive, source_digest)
            self._extract_archive(archive, target, subpath=subpath, github_archive=False)
            return
        if source_digest:
            raise SkillManagementError(
                "unsupported_digested_source",
                "A digested well-known artifact must be SKILL.md or a ZIP archive",
            )
        self._stage_web_directory(source, target, files)

    @staticmethod
    def _verify_source_digest(content: bytes, source_digest: str | None) -> None:
        if not source_digest:
            return
        expected = source_digest.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise SkillManagementError("invalid_source_digest")
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise SkillManagementError("source_digest_mismatch")

    def _stage_web_directory(self, source: str, target: Path, files: list[str]) -> None:
        base = source.rstrip("/") + "/"
        target.mkdir(parents=True, exist_ok=False)
        pending = ["SKILL.md", "README.md", *files]
        visited: set[str] = set()
        skill_found = False
        while pending:
            relative = _safe_relative(pending.pop(0))
            if relative in visited:
                continue
            visited.add(relative)
            optional = relative == "README.md"
            try:
                content = self._download(urljoin(base, relative))
            except SkillManagementError as exc:
                if optional and exc.code == "http_error_404":
                    continue
                raise
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            skill_found = skill_found or relative == "SKILL.md"
            if len(visited) > _MAX_FILES:
                raise SkillManagementError("skill_file_limit_exceeded")
            if destination.suffix.lower() in {".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml"}:
                text = content.decode("utf-8", errors="ignore")
                for match in _RELATIVE_ASSET.finditer(text):
                    candidate = match.group(1).rstrip(".,:;)'\"`]")
                    if candidate not in visited:
                        pending.append(candidate)
        if not skill_found:
            raise SkillManagementError("skill_manifest_missing")

    @staticmethod
    def _parse_github(source: str, ref: str, subpath: str) -> tuple[str, str, str, str]:
        parts = [item for item in urlsplit(source).path.split("/") if item]
        if len(parts) < 2:
            raise SkillManagementError("invalid_github_source")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        resolved_ref = ref
        resolved_subpath = subpath
        if len(parts) >= 4 and parts[2] == "tree":
            resolved_ref = parts[3]
            if not resolved_subpath:
                resolved_subpath = "/".join(parts[4:])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            raise SkillManagementError("invalid_github_source")
        if not resolved_ref or any(char in resolved_ref for char in "\r\n"):
            raise SkillManagementError("invalid_git_ref")
        return owner, repo, resolved_ref, resolved_subpath

    def _extract_archive(self, content: bytes, target: Path, *, subpath: str, github_archive: bool) -> None:
        archive_path = target.parent / f".{target.name}.zip"
        unpacked = target.parent / f".{target.name}.unpacked"
        archive_path.write_bytes(content)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_FILES + 32:
                    raise SkillManagementError("skill_file_limit_exceeded")
                total = 0
                for info in infos:
                    relative = PurePosixPath(info.filename)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise SkillManagementError("archive_path_traversal")
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise SkillManagementError("skill_symlink_not_supported")
                    total += info.file_size
                    if total > _MAX_TOTAL_BYTES:
                        raise SkillManagementError("skill_size_limit_exceeded")
                archive.extractall(unpacked)

            root = unpacked
            if github_archive:
                roots = [item for item in unpacked.iterdir() if item.is_dir()]
                if len(roots) != 1:
                    raise SkillManagementError("invalid_github_archive")
                root = roots[0]
            if subpath:
                root = root / _safe_relative(subpath)
            if not (root / "SKILL.md").is_file():
                candidates = list(root.rglob("SKILL.md"))
                if len(candidates) != 1:
                    raise SkillManagementError("skill_manifest_ambiguous")
                root = candidates[0].parent
            shutil.copytree(root, target, symlinks=False)
        except zipfile.BadZipFile as exc:
            raise SkillManagementError("invalid_zip_archive") from exc
        finally:
            archive_path.unlink(missing_ok=True)
            shutil.rmtree(unpacked, ignore_errors=True)

    def _download(self, url: str) -> bytes:
        current = url
        for _ in range(6):
            self._validate_public_url(current)
            try:
                with httpx.Client(follow_redirects=False, timeout=30.0, trust_env=False) as client:
                    with client.stream(
                        "GET", current, headers={"User-Agent": "PuddingClaw-SkillManager/1.0"}
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise SkillManagementError("redirect_without_location")
                            current = urljoin(current, location)
                            continue
                        if response.status_code >= 400:
                            raise SkillManagementError(
                                f"http_error_{response.status_code}",
                                f"{response.status_code} while downloading {current}",
                            )
                        chunks: list[bytes] = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > _MAX_DOWNLOAD_BYTES:
                                raise SkillManagementError("download_size_limit_exceeded")
                            chunks.append(chunk)
                        return b"".join(chunks)
            except httpx.HTTPError as exc:
                raise SkillManagementError("download_failed", str(exc)) from exc
        raise SkillManagementError("too_many_redirects")

    @staticmethod
    def _validate_public_url(url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise SkillManagementError("invalid_source_url") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SkillManagementError("invalid_source_url")
        if parsed.username or parsed.password or port not in {None, 80, 443}:
            raise SkillManagementError("unsafe_source_url")
        hostname = parsed.hostname.lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            raise SkillManagementError("private_source_address")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise SkillManagementError("source_dns_failed", hostname) from exc
        if not addresses:
            raise SkillManagementError("source_dns_failed", hostname)
        for address in addresses:
            if not is_public_or_trusted_https_fake_ip(
                address,
                scheme=parsed.scheme,
                hostname=hostname,
            ):
                raise SkillManagementError("private_source_address", address)

    def _manifest(self, root: Path) -> dict[str, Any]:
        if not root.is_dir() or root.is_symlink():
            raise SkillManagementError("skill_not_found")
        skill_md = root / "SKILL.md"
        if not skill_md.is_file() or skill_md.is_symlink():
            raise SkillManagementError("skill_manifest_missing")
        files: list[dict[str, Any]] = []
        total = 0
        aggregate = hashlib.sha256()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix.lower() in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise SkillManagementError("skill_symlink_not_supported", relative.as_posix())
            if not path.is_file():
                continue
            if path.suffix.lower() in _FORBIDDEN_SUFFIXES:
                raise SkillManagementError("forbidden_skill_file", relative.as_posix())
            if len(files) >= _MAX_FILES:
                raise SkillManagementError("skill_file_limit_exceeded")
            data = path.read_bytes()
            total += len(data)
            if total > _MAX_TOTAL_BYTES:
                raise SkillManagementError("skill_size_limit_exceeded")
            digest = hashlib.sha256(data).hexdigest()
            name = relative.as_posix()
            aggregate.update(name.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")
            files.append({"path": name, "size": len(data), "sha256": digest})
        content = skill_md.read_text(encoding="utf-8")
        metadata: dict[str, Any] = {}
        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
        if match:
            try:
                parsed = yaml.safe_load(match.group(1))
            except yaml.YAMLError as exc:
                raise SkillManagementError("invalid_skill_frontmatter", str(exc)) from exc
            if isinstance(parsed, dict):
                for key in ("name", "version", "description", "license", "homepage", "source"):
                    value = parsed.get(key)
                    if isinstance(value, (str, int, float, bool)):
                        metadata[key] = value
        return {
            "sha256": aggregate.hexdigest(),
            "file_count": len(files),
            "total_bytes": total,
            "metadata": metadata,
            "files": files,
        }

    @staticmethod
    def _diff(current: dict[str, Any] | None, staged: dict[str, Any]) -> dict[str, Any]:
        before = {item["path"]: item["sha256"] for item in (current or {}).get("files", [])}
        after = {item["path"]: item["sha256"] for item in staged["files"]}
        added = sorted(after.keys() - before.keys())
        removed = sorted(before.keys() - after.keys())
        changed = sorted(path for path in before.keys() & after.keys() if before[path] != after[path])
        unchanged = len(before.keys() & after.keys()) - len(changed)
        return {
            "added": added,
            "changed": changed,
            "removed": removed,
            "unchanged_count": unchanged,
            "summary": f"+{len(added)} ~{len(changed)} -{len(removed)} ={unchanged}",
        }

    def _snapshot(self, skill_name: str, target: Path, digest: str) -> Path:
        root = self.snapshots_dir / skill_name
        root.mkdir(parents=True, exist_ok=True)
        snapshot = root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{digest[:12]}-{uuid.uuid4().hex[:6]}"
        shutil.copytree(target, snapshot, symlinks=False)
        return snapshot

    @property
    def registry_path(self) -> Path:
        return self.state_dir / "registry.json"

    def _load_registry(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "skills": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillManagementError("invalid_skill_registry", str(exc)) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("skills"), dict):
            raise SkillManagementError("invalid_skill_registry")
        return payload

    def _load_provenance(self, skill_name: str | None) -> dict[str, Any] | None:
        if not skill_name or not _SKILL_NAME.fullmatch(skill_name):
            return None
        entry = self._load_registry().get("skills", {}).get(skill_name)
        return dict(entry) if isinstance(entry, dict) else None

    def _record_provenance(self, plan: dict[str, Any]) -> bool:
        try:
            registry = self._load_registry()
            skills = registry.setdefault("skills", {})
            skills[str(plan["skill_name"])] = {
                "source": plan.get("source"),
                "ref": plan.get("ref"),
                "subpath": plan.get("subpath"),
                "files": list(plan.get("files") or []),
                "source_digest": plan.get("source_digest"),
                "installed_sha256": plan.get("installed_sha256"),
                "updated_at": plan.get("committed_at"),
            }
            self._write_json(self.registry_path, registry)
        except (OSError, SkillManagementError):
            return False
        return True

    def _prune_snapshots(self, skill_name: str) -> None:
        root = self.snapshots_dir / skill_name
        snapshots = (
            sorted(
                (item for item in root.iterdir() if item.is_dir() and not item.is_symlink()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if root.is_dir()
            else []
        )
        for stale in snapshots[_SNAPSHOT_RETENTION:]:
            shutil.rmtree(stale, ignore_errors=True)

    def _cleanup_expired_plans(self) -> None:
        if not self.plans_dir.is_dir():
            return
        now = time.time()
        for item in self.plans_dir.iterdir():
            if not item.is_dir() or item.is_symlink() or not item.name.startswith("skill-plan-"):
                continue
            try:
                payload = json.loads((item / "plan.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("status") == "prepared" and now > float(payload.get("expires_at") or 0):
                # Keep the small plan record so historical UI cards can show
                # the terminal state after reload. Only the staged payload is
                # discarded.
                payload["status"] = "expired"
                payload["expired_at"] = now
                self._write_json(item / "plan.json", payload)
                shutil.rmtree(item / "payload", ignore_errors=True)

    def _find_plan_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        if not request_key or not self.plans_dir.is_dir():
            return None
        for item in self.plans_dir.iterdir():
            if not item.is_dir() or item.is_symlink() or not item.name.startswith("skill-plan-"):
                continue
            try:
                plan = self._load_plan(item.name)
            except SkillManagementError:
                continue
            if plan.get("request_key") == request_key:
                return plan
        return None

    @staticmethod
    def _validate_plan_owner(plan: dict[str, Any], session_id: str) -> None:
        context = plan.get("request_context")
        owner = str(context.get("session_id") or "") if isinstance(context, dict) else ""
        if not owner:
            raise SkillManagementError(
                "plan_not_session_bound",
                "This legacy plan must be committed through the approval-gated Tool path",
            )
        if owner != session_id:
            raise SkillManagementError("plan_session_mismatch")

    def _load_plan(self, plan_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"skill-plan-[0-9a-f]{16}", plan_id):
            raise SkillManagementError("invalid_plan_id")
        path = self.plans_dir / plan_id / "plan.json"
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SkillManagementError("plan_not_found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillManagementError("invalid_plan") from exc
        expected = str(plan.get("plan_sha256") or "")
        unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
        # Committed plans gain audit fields after authorization; prepared plans
        # must remain byte-for-byte bound to the digest shown for approval.
        if plan.get("status") == "prepared" and expected != _json_digest(unsigned):
            raise SkillManagementError("plan_metadata_changed")
        return plan

    @staticmethod
    def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: plan.get(key)
            for key in (
                "plan_id",
                "plan_sha256",
                "action",
                "skill_name",
                "source",
                "ref",
                "subpath",
                "files",
                "source_digest",
                "created_at",
                "expires_at",
                "status",
                "baseline_sha256",
                "staged_sha256",
                "staged_metadata",
                "diff",
                "snapshot_id",
                "installed_path",
                "installed_sha256",
                "committed_at",
                "cancelled_at",
                "expired_at",
            )
            if key in plan
        }
        status = str(plan.get("status") or "")
        public.update(
            {
                "ok": True,
                "phase": {
                    "prepared": "awaiting_confirmation",
                    "committed": "installed",
                    "cancelled": "cancelled",
                    "expired": "expired",
                }.get(status, status or "unknown"),
                "requires_confirmation": status == "prepared",
                "installed": status == "committed",
                "ui_commit_supported": bool(
                    isinstance(plan.get("request_context"), dict)
                    and plan["request_context"].get("session_id")
                ),
            }
        )
        return public

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _refresh_skill_snapshot(self) -> None:
        try:
            from tools.skills_scanner import scan_skills

            scan_skills(self.base_dir)
        except Exception:
            # The installed directory is authoritative; catalogue refresh can
            # recover on process startup and must not roll back a valid commit.
            pass


_SERVICES: dict[str, SkillManagementService] = {}
_SERVICES_LOCK = threading.Lock()


def get_skill_management_service(base_dir: Path) -> SkillManagementService:
    key = str(base_dir.expanduser().resolve())
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = SkillManagementService(Path(key))
            _SERVICES[key] = service
        return service
