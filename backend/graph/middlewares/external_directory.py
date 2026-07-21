"""Goal-scoped external-directory snapshots and reviewed write-back."""

import hashlib
import json
import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from graph.session_manager import session_manager

MAX_DIRECTORY_FILES = 2_000
MAX_DIRECTORY_BYTES = 200 * 1024 * 1024
MAX_DIRECTORY_DEPTH = 20
LEASE_TTL_SECONDS = 6 * 60 * 60
UPLOAD_BATCH_SIZE = 100

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".cache", "dist", "build"}
)
_EXCLUDED_FILE_NAMES = frozenset({".env", "id_rsa", "id_ed25519"})
_EXCLUDED_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


class StageExternalDirectoryInput(BaseModel):
    directory_path: str = Field(description="Exact approved absolute external directory")


class PrepareExternalDirectoryCommitInput(BaseModel):
    lease_id: str
    directory_path: str = Field(description="Exact external directory bound to the lease")


class CommitExternalDirectoryInput(BaseModel):
    lease_id: str
    directory_path: str = Field(description="Exact external directory bound to the lease")
    plan_digest: str = Field(description="sha256 digest returned by prepare_external_directory_commit")


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _manifest_digest(manifest: dict[str, dict[str, Any]]) -> str:
    # A scratch backend does not preserve host mode bits.  Conflict identity is
    # therefore deliberately content-only; host modes remain in the source
    # manifest so modified files can retain their original permissions.
    comparable = {path: {"sha256": item.get("sha256"), "size": item.get("size")} for path, item in manifest.items()}
    payload = json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(payload.encode("utf-8"))


def _runtime_binding(runtime: ToolRuntime[Any, Any]) -> dict[str, Any]:
    context = runtime.context if isinstance(runtime.context, dict) else {}
    return {
        "session_id": str(context.get("session_id") or ""),
        "run_id": str(context.get("run_id") or ""),
        "query_id": str(context.get("query_id") or ""),
        "goal_id": str(context.get("goal_id") or ""),
        "goal_revision": context.get("goal_revision"),
    }


def _binding_matches(lease: dict[str, Any], binding: dict[str, Any]) -> bool:
    if str(lease.get("session_id") or "") != binding["session_id"]:
        return False
    if binding["goal_id"]:
        return (
            str(lease.get("goal_id") or "") == binding["goal_id"]
            and lease.get("goal_revision") == binding["goal_revision"]
        )
    return (
        not str(lease.get("goal_id") or "")
        and str(lease.get("run_id") or "") == binding["run_id"]
        and str(lease.get("query_id") or "") == binding["query_id"]
    )


def _tool_error(name: str, runtime: ToolRuntime[Any, Any], content: str) -> ToolMessage:
    return ToolMessage(
        content=f"Error: {content}",
        name=name,
        tool_call_id=runtime.tool_call_id,
        status="error",
    )


def _excluded_relative(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if any(part in _EXCLUDED_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    lower = name.lower()
    return name in _EXCLUDED_FILE_NAMES or lower.startswith(".env.") or lower.endswith(_EXCLUDED_SECRET_SUFFIXES)


def _safe_relative(relative: str) -> str | None:
    normalized = relative.replace("\\", "/").lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return None
    if len(path.parts) > MAX_DIRECTORY_DEPTH:
        return None
    return path.as_posix()


def _scan_source_directory(
    root: Path,
    *,
    include_content: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes], list[str], str | None]:
    try:
        resolved_root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        return {}, {}, [], str(exc)
    if not resolved_root.is_dir():
        return {}, {}, [], f"not a directory: {resolved_root}"

    manifest: dict[str, dict[str, Any]] = {}
    contents: dict[str, bytes] = {}
    skipped: list[str] = []
    total_bytes = 0
    try:
        for current, dirs, files in os.walk(resolved_root, topdown=True, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.relative_to(resolved_root).parts)
            kept_dirs: list[str] = []
            for name in sorted(dirs):
                candidate = current_path / name
                relative = candidate.relative_to(resolved_root).as_posix()
                if name in _EXCLUDED_DIRECTORY_NAMES or candidate.is_symlink():
                    skipped.append(f"{relative}/")
                elif depth + 1 >= MAX_DIRECTORY_DEPTH:
                    skipped.append(f"{relative}/ (depth-limit)")
                else:
                    kept_dirs.append(name)
            dirs[:] = kept_dirs

            for name in sorted(files):
                candidate = current_path / name
                relative = candidate.relative_to(resolved_root).as_posix()
                if _excluded_relative(relative) or candidate.is_symlink():
                    skipped.append(relative)
                    continue
                file_stat = candidate.lstat()
                if not stat.S_ISREG(file_stat.st_mode):
                    skipped.append(relative)
                    continue
                resolved_candidate = candidate.resolve(strict=True)
                try:
                    resolved_candidate.relative_to(resolved_root)
                except ValueError:
                    skipped.append(f"{relative} (outside-root)")
                    continue
                data = candidate.read_bytes()
                if len(data) != file_stat.st_size:
                    return {}, {}, skipped, f"source changed while snapshotting: {relative}"
                total_bytes += len(data)
                if len(manifest) + 1 > MAX_DIRECTORY_FILES:
                    return {}, {}, skipped, f"directory exceeds {MAX_DIRECTORY_FILES} file limit"
                if total_bytes > MAX_DIRECTORY_BYTES:
                    return {}, {}, skipped, f"directory exceeds {MAX_DIRECTORY_BYTES} byte limit"
                manifest[relative] = {
                    "sha256": _sha256(data),
                    "size": len(data),
                    "mode": stat.S_IMODE(file_stat.st_mode),
                }
                if include_content:
                    contents[relative] = data
    except OSError as exc:
        return {}, {}, skipped, str(exc)
    return manifest, contents, skipped, None


def _scan_staged_directory(
    backend: Any,
    staged_dir: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes], list[str], str | None]:
    directory = backend.ls(staged_dir)
    if directory.error:
        return {}, {}, [], str(directory.error)
    result = backend.glob(f"{staged_dir.rstrip('/')}/**/*")
    if result.error:
        return {}, {}, [], str(result.error)
    prefix = staged_dir.rstrip("/") + "/"
    paths: list[tuple[str, str]] = []
    skipped: list[str] = []
    for item in result.matches or []:
        if item.get("is_dir"):
            continue
        virtual_path = str(item.get("path") or "")
        if not virtual_path.startswith(prefix):
            return {}, {}, skipped, f"staged path escaped lease root: {virtual_path}"
        relative = _safe_relative(virtual_path.removeprefix(prefix))
        if relative is None:
            return {}, {}, skipped, f"invalid staged relative path: {virtual_path}"
        if _excluded_relative(relative):
            skipped.append(relative)
            continue
        paths.append((virtual_path, relative))
    if len(paths) > MAX_DIRECTORY_FILES:
        return {}, {}, skipped, f"staged directory exceeds {MAX_DIRECTORY_FILES} file limit"

    manifest: dict[str, dict[str, Any]] = {}
    contents: dict[str, bytes] = {}
    total_bytes = 0
    for start in range(0, len(paths), UPLOAD_BATCH_SIZE):
        batch = paths[start : start + UPLOAD_BATCH_SIZE]
        responses = backend.download_files([path for path, _relative in batch])
        if len(responses) != len(batch):
            return {}, {}, skipped, "backend returned incomplete staged-directory download"
        for response, (_path, relative) in zip(responses, batch, strict=True):
            if response.error is not None or response.content is None:
                return {}, {}, skipped, f"unable to read staged file {relative}: {response.error}"
            data = response.content
            total_bytes += len(data)
            if total_bytes > MAX_DIRECTORY_BYTES:
                return {}, {}, skipped, f"staged directory exceeds {MAX_DIRECTORY_BYTES} byte limit"
            manifest[relative] = {"sha256": _sha256(data), "size": len(data)}
            contents[relative] = data
    return manifest, contents, skipped, None


def _upload_snapshot(backend: Any, staged_dir: str, contents: dict[str, bytes]) -> str | None:
    items = [(f"{staged_dir.rstrip('/')}/{relative}", data) for relative, data in contents.items()]
    for start in range(0, len(items), UPLOAD_BATCH_SIZE):
        batch = items[start : start + UPLOAD_BATCH_SIZE]
        responses = backend.upload_files(batch)
        if len(responses) != len(batch):
            return "backend returned incomplete directory upload"
        errors = [str(item.error) for item in responses if item.error is not None]
        if errors:
            return errors[0]
    return None


def _change_plan(
    source: dict[str, dict[str, Any]],
    staged: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    source_paths = set(source)
    staged_paths = set(staged)
    return {
        "added": sorted(staged_paths - source_paths),
        "modified": sorted(
            path for path in source_paths & staged_paths if source[path].get("sha256") != staged[path].get("sha256")
        ),
        "deleted": sorted(source_paths - staged_paths),
    }


def _apply_directory_plan(
    root: Path,
    lease_id: str,
    plan: dict[str, list[str]],
    staged_contents: dict[str, bytes],
    source_manifest: dict[str, dict[str, Any]],
) -> str | None:
    backups: dict[str, bytes] = {}
    try:
        for relative in [*plan["modified"], *plan["deleted"]]:
            backups[relative] = (root / relative).read_bytes()
        for relative in [*plan["added"], *plan["modified"]]:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{lease_id}.tmp")
            temporary.write_bytes(staged_contents[relative])
            if relative in source_manifest:
                temporary.chmod(int(source_manifest[relative].get("mode") or 0o644))
            os.replace(temporary, target)
        for relative in plan["deleted"]:
            (root / relative).unlink()
        return None
    except OSError as exc:
        for relative in plan["added"]:
            try:
                (root / relative).unlink(missing_ok=True)
            except OSError:
                pass
        for relative, data in backups.items():
            try:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(int(source_manifest[relative].get("mode") or 0o644))
            except OSError:
                pass
        return str(exc)


class ExternalDirectoryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Expose a safe Docker scratch workflow for user-authorized directories."""

    def __init__(self, backend: Any) -> None:
        super().__init__()
        self.backend = backend

        def stage_external_directory(
            directory_path: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            binding = _runtime_binding(runtime)
            if not binding["session_id"] or not binding["run_id"] or not binding["query_id"]:
                return _tool_error(
                    "stage_external_directory",
                    runtime,
                    "directory staging requires an active Session, Run, and query",
                )
            root = Path(directory_path).expanduser().resolve()
            if not session_manager.has_external_directory_permission(
                binding["session_id"], root, access="read", run_id=binding["run_id"]
            ):
                return _tool_error(
                    "stage_external_directory",
                    runtime,
                    "current Run lacks recursive read permission for this exact directory",
                )
            manifest, contents, skipped, error = _scan_source_directory(root, include_content=True)
            if error is not None:
                return _tool_error("stage_external_directory", runtime, error)
            source_digest = _manifest_digest(manifest)
            active = session_manager.find_staged_external_directory_lease(
                binding["session_id"],
                run_id=binding["run_id"],
                query_id=binding["query_id"],
                directory_path=str(root),
                goal_id=binding["goal_id"],
                goal_revision=binding["goal_revision"],
            )
            if isinstance(active, dict):
                if active.get("source_manifest_sha256") != source_digest:
                    staged_manifest, _staged_contents, _staged_skipped, staged_error = (
                        _scan_staged_directory(
                            self.backend,
                            str(active.get("staged_dir") or ""),
                        )
                    )
                    original_manifest = active.get("source_manifest")
                    draft_is_unchanged = (
                        staged_error is None
                        and isinstance(original_manifest, dict)
                        and _manifest_digest(staged_manifest) == _manifest_digest(original_manifest)
                    )
                    if staged_error is None and not draft_is_unchanged:
                        return _tool_error(
                            "stage_external_directory",
                            runtime,
                            "source directory changed after staging and the Goal draft also has edits; "
                            "review and rebase the draft before write-back",
                        )
                    # Missing/unchanged drafts contain no user work to preserve.
                    # Supersede the stale snapshot and create a fresh lease from
                    # the current source instead of forcing another permission
                    # round-trip after this Goal's own exact-file commits.
                    active.update(
                        {
                            "status": "superseded",
                            "superseded_at": time.time(),
                            "superseded_by_source_manifest_sha256": source_digest,
                        }
                    )
                    session_manager.upsert_external_directory_lease(
                        binding["session_id"],
                        active,
                    )
                    active = None
            if isinstance(active, dict):
                _manifest, _contents, _skipped, staged_error = _scan_staged_directory(
                    self.backend,
                    str(active.get("staged_dir") or ""),
                )
                rehydrated = staged_error is not None
                if staged_error is not None:
                    upload_error = _upload_snapshot(
                        self.backend,
                        str(active.get("staged_dir") or ""),
                        contents,
                    )
                    if upload_error is not None:
                        return _tool_error(
                            "stage_external_directory",
                            runtime,
                            f"unable to rehydrate missing Goal draft: {upload_error}",
                        )
                now = time.time()
                rebound = (
                    str(active.get("run_id") or "") != binding["run_id"]
                    or str(active.get("query_id") or "") != binding["query_id"]
                )
                expired = float(active.get("expires_at") or 0) < now
                active.update(
                    {
                        "run_id": binding["run_id"],
                        "query_id": binding["query_id"],
                        "status": "staged",
                        "expires_at": now + LEASE_TTL_SECONDS,
                        "renewed_at": now,
                    }
                )
                active.pop("commit_plan", None)
                active.pop("prepared_at", None)
                active.pop("prepare_tool_call_id", None)
                session_manager.upsert_external_directory_lease(
                    binding["session_id"],
                    active,
                )
                disposition = (
                    "rehydrated from the current source; the previous uncommitted draft was unavailable"
                    if rehydrated
                    else "rebound to this Run"
                    if rebound
                    else "renewed"
                    if expired
                    else "reused"
                )
                return ToolMessage(
                    content=(
                        f"ExternalDirectoryLease {disposition}. lease_id={active['lease_id']}; "
                        f"staged_dir={active['staged_dir']}; source_manifest_sha256={source_digest}. "
                        "Continue from the preserved Goal draft and prepare a new commit plan before write-back."
                    ),
                    name="stage_external_directory",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                    artifact={"external_directory_lease": active},
                )
            lease_seed = f"{binding['session_id']}:{binding['run_id']}:{runtime.tool_call_id}:{root}"
            lease_id = "directory-lease-" + hashlib.sha256(lease_seed.encode("utf-8")).hexdigest()[:16]
            staged_dir = f"/scratch/external-directories/{lease_id}"
            existing = session_manager.get_external_directory_lease(binding["session_id"], lease_id)
            if isinstance(existing, dict):
                replay_ok = (
                    _binding_matches(existing, binding)
                    and existing.get("directory_path") == str(root)
                    and existing.get("source_manifest_sha256") == source_digest
                    and existing.get("status") in {"staged", "prepared", "committed"}
                )
                if not replay_ok:
                    return _tool_error(
                        "stage_external_directory",
                        runtime,
                        "tool-call identity already owns a conflicting directory snapshot",
                    )
                return ToolMessage(
                    content=(
                        f"ExternalDirectoryLease already {existing['status']}. lease_id={lease_id}; "
                        f"staged_dir={staged_dir}; source_manifest_sha256={source_digest}."
                    ),
                    name="stage_external_directory",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                    artifact={"external_directory_lease": existing},
                )
            upload_error = _upload_snapshot(self.backend, staged_dir, contents)
            if upload_error is not None:
                return _tool_error("stage_external_directory", runtime, upload_error)
            now = time.time()
            lease = {
                "lease_id": lease_id,
                **binding,
                "directory_path": str(root),
                "staged_dir": staged_dir,
                "source_manifest": manifest,
                "source_manifest_sha256": source_digest,
                "file_count": len(manifest),
                "total_bytes": sum(int(item["size"]) for item in manifest.values()),
                "skipped": skipped,
                "status": "staged",
                "created_at": now,
                "expires_at": now + LEASE_TTL_SECONDS,
            }
            session_manager.upsert_external_directory_lease(binding["session_id"], lease)
            return ToolMessage(
                content=(
                    f"ExternalDirectoryLease created. lease_id={lease_id}; staged_dir={staged_dir}; "
                    f"files={len(manifest)}; bytes={lease['total_bytes']}; skipped={len(skipped)}; "
                    f"source_manifest_sha256={source_digest}. Work only inside staged_dir. "
                    "For write-back, call prepare_external_directory_commit, review its plan, then "
                    "call commit_external_directory. Opening the directory as the project remains recommended."
                ),
                name="stage_external_directory",
                tool_call_id=runtime.tool_call_id,
                status="success",
                artifact={"external_directory_lease": lease},
            )

        def prepare_external_directory_commit(
            lease_id: str,
            directory_path: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            binding = _runtime_binding(runtime)
            lease = session_manager.get_external_directory_lease(binding["session_id"], lease_id)
            if not isinstance(lease, dict):
                return _tool_error(
                    "prepare_external_directory_commit", runtime, f"unknown ExternalDirectoryLease {lease_id}"
                )
            root = str(Path(directory_path).expanduser().resolve())
            if not _binding_matches(lease, binding) or lease.get("directory_path") != root:
                return _tool_error(
                    "prepare_external_directory_commit",
                    runtime,
                    "directory lease belongs to a different Run/query/Goal revision or path",
                )
            if float(lease.get("expires_at") or 0) < time.time():
                return _tool_error("prepare_external_directory_commit", runtime, "directory lease expired")
            if lease.get("status") == "committed":
                return _tool_error("prepare_external_directory_commit", runtime, "directory lease is already committed")
            staged_manifest, _contents, skipped, error = _scan_staged_directory(
                self.backend, str(lease.get("staged_dir") or "")
            )
            if error is not None:
                return _tool_error("prepare_external_directory_commit", runtime, error)
            source_manifest = dict(lease.get("source_manifest") or {})
            plan = _change_plan(source_manifest, staged_manifest)
            plan_payload = {
                **plan,
                "source_manifest_sha256": lease.get("source_manifest_sha256"),
                "staged_manifest_sha256": _manifest_digest(staged_manifest),
            }
            plan_digest = _sha256(json.dumps(plan_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            plan_payload["plan_digest"] = plan_digest
            plan_payload["skipped"] = skipped
            lease.update(
                {
                    "status": "prepared",
                    "commit_plan": plan_payload,
                    "prepared_at": time.time(),
                    "prepare_tool_call_id": runtime.tool_call_id,
                }
            )
            session_manager.upsert_external_directory_lease(binding["session_id"], lease)
            return ToolMessage(
                content=(
                    f"ExternalDirectoryCommitPlan ready. lease_id={lease_id}; plan_digest={plan_digest}; "
                    f"added={len(plan['added'])}; modified={len(plan['modified'])}; "
                    f"deleted={len(plan['deleted'])}; excluded={len(skipped)}. "
                    f"added_files={plan['added']}; modified_files={plan['modified']}; "
                    f"deleted_files={plan['deleted']}. Call commit_external_directory only after validation."
                ),
                name="prepare_external_directory_commit",
                tool_call_id=runtime.tool_call_id,
                status="success",
                artifact={"external_directory_commit_plan": plan_payload},
            )

        def commit_external_directory(
            lease_id: str,
            directory_path: str,
            plan_digest: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            binding = _runtime_binding(runtime)
            lease = session_manager.get_external_directory_lease(binding["session_id"], lease_id)
            if not isinstance(lease, dict):
                return _tool_error("commit_external_directory", runtime, f"unknown ExternalDirectoryLease {lease_id}")
            root = Path(directory_path).expanduser().resolve()
            plan = lease.get("commit_plan")
            if (
                not _binding_matches(lease, binding)
                or lease.get("directory_path") != str(root)
                or not isinstance(plan, dict)
            ):
                return _tool_error(
                    "commit_external_directory",
                    runtime,
                    "directory lease has no valid prepared plan for this Run/path",
                )
            if plan_digest != plan.get("plan_digest"):
                return _tool_error("commit_external_directory", runtime, "plan_digest does not match reviewed plan")
            if lease.get("status") == "committed":
                return ToolMessage(
                    content=(
                        f"ExternalDirectoryLease already committed. lease_id={lease_id}; "
                        f"directory_path={root}; committed_manifest_sha256={lease.get('committed_manifest_sha256')}"
                    ),
                    name="commit_external_directory",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                )
            if lease.get("status") != "prepared" or float(lease.get("expires_at") or 0) < time.time():
                return _tool_error("commit_external_directory", runtime, "directory lease is not committable")
            if not session_manager.has_external_directory_permission(
                binding["session_id"], root, access="write", run_id=binding["run_id"]
            ):
                return _tool_error(
                    "commit_external_directory",
                    runtime,
                    "current Run lacks recursive write permission for this exact directory",
                )
            current_manifest, _current_contents, _skipped, error = _scan_source_directory(root, include_content=False)
            if error is not None:
                return _tool_error("commit_external_directory", runtime, error)
            if _manifest_digest(current_manifest) != lease.get("source_manifest_sha256"):
                return _tool_error(
                    "commit_external_directory",
                    runtime,
                    "source directory changed after staging; create a new lease and rebase",
                )
            staged_manifest, staged_contents, _staged_skipped, staged_error = _scan_staged_directory(
                self.backend, str(lease.get("staged_dir") or "")
            )
            if staged_error is not None:
                return _tool_error("commit_external_directory", runtime, staged_error)
            if _manifest_digest(staged_manifest) != plan.get("staged_manifest_sha256"):
                return _tool_error(
                    "commit_external_directory",
                    runtime,
                    "staged directory changed after review; prepare a new commit plan",
                )
            normalized_plan = {
                "added": list(plan.get("added") or []),
                "modified": list(plan.get("modified") or []),
                "deleted": list(plan.get("deleted") or []),
            }
            apply_error = _apply_directory_plan(
                root,
                lease_id,
                normalized_plan,
                staged_contents,
                dict(lease.get("source_manifest") or {}),
            )
            if apply_error is not None:
                return _tool_error(
                    "commit_external_directory", runtime, f"write-back failed and rollback was attempted: {apply_error}"
                )
            committed_manifest, _contents, _skipped, verify_error = _scan_source_directory(root, include_content=False)
            if verify_error is not None or _manifest_digest(committed_manifest) != _manifest_digest(staged_manifest):
                return _tool_error(
                    "commit_external_directory",
                    runtime,
                    f"post-commit verification failed: {verify_error or 'manifest mismatch'}",
                )
            lease.update(
                {
                    "status": "committed",
                    "committed_at": time.time(),
                    "commit_tool_call_id": runtime.tool_call_id,
                    "committed_manifest_sha256": _manifest_digest(committed_manifest),
                }
            )
            session_manager.upsert_external_directory_lease(binding["session_id"], lease)
            return ToolMessage(
                content=(
                    f"External directory committed. lease_id={lease_id}; directory_path={root}; "
                    f"added={len(normalized_plan['added'])}; modified={len(normalized_plan['modified'])}; "
                    f"deleted={len(normalized_plan['deleted'])}; "
                    f"committed_manifest_sha256={lease['committed_manifest_sha256']}"
                ),
                name="commit_external_directory",
                tool_call_id=runtime.tool_call_id,
                status="success",
                artifact={"external_directory_lease": lease},
            )

        self.tools = [
            StructuredTool.from_function(
                name="stage_external_directory",
                description=(
                    "Snapshot one user-authorized external directory into a Goal-revision-scoped Docker /scratch lease. "
                    "Use this for directory-wide reading, search, execution, or testing when the user did not open it as the project."
                ),
                func=stage_external_directory,
                args_schema=StageExternalDirectoryInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="prepare_external_directory_commit",
                description=(
                    "Compare an external-directory scratch lease with its source snapshot and produce the exact "
                    "added/modified/deleted plan required before write permission can be requested."
                ),
                func=prepare_external_directory_commit,
                args_schema=PrepareExternalDirectoryCommitInput,
                infer_schema=False,
            ),
            StructuredTool.from_function(
                name="commit_external_directory",
                description=(
                    "After explicit recursive-write approval, commit the reviewed directory plan back to the exact "
                    "source directory with source and staged manifest conflict checks."
                ),
                func=commit_external_directory,
                args_schema=CommitExternalDirectoryInput,
                infer_schema=False,
            ),
        ]
