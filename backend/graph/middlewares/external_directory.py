"""Goal-scoped external-directory snapshots and reviewed write-back."""

import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from graph.session_manager import session_manager
from observability import emit_harness_metric

logger = logging.getLogger(__name__)

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
    declared_delivery_files: list[str] = Field(
        default_factory=list,
        description=(
            "New staged files intentionally declared as deliverables. Temporary validation files belong under "
            "the lease validation_scratch path and must not be declared."
        ),
    )


class CommitExternalDirectoryInput(BaseModel):
    lease_id: str
    directory_path: str = Field(description="Exact external directory bound to the lease")
    plan_digest: str = Field(description="sha256 digest returned by prepare_external_directory_commit")
    validation_receipt_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Server-persisted ValidationReceipt ids covering every added or modified "
            "code-like file at its exact formal target path and staged content hash."
        ),
    )


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


def _read_snapshot_file(root_fd: int, relative: str) -> tuple[bytes, os.stat_result]:
    """Read one snapshot file through no-follow dirfds and detect mutations."""

    parts = tuple(PurePosixPath(relative).parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError(f"unsafe snapshot path: {relative}")
    current_fd = os.dup(root_fd)
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
        with os.fdopen(file_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError(f"snapshot target is not a regular file: {relative}")
            data = stream.read()
            after = os.fstat(stream.fileno())
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or len(data) != after.st_size:
                raise OSError(f"source changed while snapshotting: {relative}")
            return data, after
    finally:
        os.close(current_fd)


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
    root_fd: int | None = None
    try:
        root_fd = os.open(
            resolved_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
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
                try:
                    data, file_stat = _read_snapshot_file(root_fd, relative)
                except OSError as exc:
                    return {}, {}, skipped, str(exc)
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
    finally:
        if root_fd is not None:
            os.close(root_fd)
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


def _plan_path_parts(relative: str) -> tuple[str, ...]:
    raw = relative.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise OSError(f"unsafe directory commit path: {relative}")
    return path.parts


def _open_plan_parent(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> tuple[int, str]:
    """Open a plan target's parent without ever following a symlink.

    Path.resolve()/relative_to() checks are vulnerable to a rename/symlink race
    between validation and mutation.  Directory-fd traversal keeps every lookup
    beneath the already-opened authorized root and applies O_NOFOLLOW at each
    existing component.
    """

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("platform cannot provide no-follow directory commits")
    current_fd = os.dup(root_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        for component in parts[:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1]
    except BaseException:
        os.close(current_fd)
        raise


def _safe_plan_read(root_fd: int, relative: str) -> bytes:
    parent_fd, name = _open_plan_parent(
        root_fd, _plan_path_parts(relative), create=False
    )
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        with os.fdopen(file_fd, "rb") as stream:
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError(f"directory commit target is not a regular file: {relative}")
            return stream.read()
    finally:
        os.close(parent_fd)


def _safe_plan_write(
    root_fd: int,
    relative: str,
    data: bytes,
    *,
    lease_id: str,
    mode: int,
) -> None:
    parent_fd, name = _open_plan_parent(
        root_fd, _plan_path_parts(relative), create=True
    )
    temporary = f".{name}.{lease_id}.tmp"
    file_fd: int | None = None
    try:
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        with os.fdopen(file_fd, "wb") as stream:
            file_fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    finally:
        if file_fd is not None:
            os.close(file_fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _safe_plan_unlink(root_fd: int, relative: str) -> None:
    parent_fd, name = _open_plan_parent(
        root_fd, _plan_path_parts(relative), create=False
    )
    try:
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _apply_directory_plan(
    root: Path,
    lease_id: str,
    plan: dict[str, list[str]],
    staged_contents: dict[str, bytes],
    source_manifest: dict[str, dict[str, Any]],
) -> tuple[str | None, Callable[[], str | None] | None]:
    backups: dict[str, bytes] = {}
    root_fd: int | None = None

    def rollback() -> str | None:
        rollback_fd: int | None = None
        errors: list[str] = []
        try:
            rollback_fd = os.open(
                root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            for relative in plan["added"]:
                try:
                    _safe_plan_unlink(rollback_fd, relative)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    errors.append(f"remove {relative}: {exc}")
            for relative, data in backups.items():
                try:
                    _safe_plan_write(
                        rollback_fd,
                        relative,
                        data,
                        lease_id=f"{lease_id}.rollback",
                        mode=int(source_manifest[relative].get("mode") or 0o644),
                    )
                except OSError as exc:
                    errors.append(f"restore {relative}: {exc}")
        except OSError as exc:
            errors.append(str(exc))
        finally:
            if rollback_fd is not None:
                os.close(rollback_fd)
        return "; ".join(errors) or None

    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for relative in [*plan["modified"], *plan["deleted"]]:
            backups[relative] = _safe_plan_read(root_fd, relative)
        for relative in [*plan["added"], *plan["modified"]]:
            _safe_plan_write(
                root_fd,
                relative,
                staged_contents[relative],
                lease_id=lease_id,
                mode=int(source_manifest.get(relative, {}).get("mode") or 0o644),
            )
        for relative in plan["deleted"]:
            _safe_plan_unlink(root_fd, relative)
        return None, rollback
    except OSError as exc:
        rollback_error = rollback()
        detail = str(exc)
        if rollback_error:
            detail += f"; rollback errors: {rollback_error}"
        return detail, None
    finally:
        if root_fd is not None:
            os.close(root_fd)


class ExternalDirectoryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Expose a safe Docker scratch workflow for user-authorized directories."""

    def __init__(self, backend: Any) -> None:
        super().__init__()
        self.backend = backend

        # DEPRECATED COMPATIBILITY SURFACE: ordinary directory file operations
        # now route directly through HostFileBroker. Keep this implementation
        # frozen only for active historical checkpoint recovery.
        def stage_external_directory(
            directory_path: str,
            runtime: ToolRuntime[Any, Any],
        ) -> ToolMessage:
            started_at = time.monotonic()
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
            emit_harness_metric(
                logger,
                "external_snapshot_stage_ms",
                session_id=binding["session_id"],
                value=round((time.monotonic() - started_at) * 1000, 2),
                file_count=len(manifest),
            )
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
                    emit_harness_metric(
                        logger,
                        "external_snapshot_refresh_count",
                        session_id=binding["session_id"],
                        target=str(root),
                    )
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
                validation_scratch = str(
                    active.get("validation_scratch")
                    or f"/scratch/validation/{active.get('lease_id')}"
                )
                active["validation_scratch"] = validation_scratch
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
                        f"Write temporary validators only under validation_scratch={validation_scratch}. "
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
            validation_scratch = f"/scratch/validation/{lease_id}"
            existing = session_manager.get_external_directory_lease(binding["session_id"], lease_id)
            if isinstance(existing, dict):
                existing.setdefault("validation_scratch", validation_scratch)
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
                session_manager.upsert_external_directory_lease(binding["session_id"], existing)
                return ToolMessage(
                    content=(
                        f"ExternalDirectoryLease already {existing['status']}. lease_id={lease_id}; "
                        f"staged_dir={staged_dir}; source_manifest_sha256={source_digest}; "
                        f"validation_scratch={validation_scratch}."
                    ),
                    name="stage_external_directory",
                    tool_call_id=runtime.tool_call_id,
                    status="success",
                    artifact={"external_directory_lease": existing},
                )
            now = time.time()
            lease = {
                "lease_id": lease_id,
                **binding,
                "directory_path": str(root),
                "staged_dir": staged_dir,
                "validation_scratch": validation_scratch,
                "source_manifest": manifest,
                "source_manifest_sha256": source_digest,
                "file_count": len(manifest),
                "total_bytes": sum(int(item["size"]) for item in manifest.values()),
                "skipped": skipped,
                "status": "claiming",
                "created_at": now,
                "expires_at": now + LEASE_TTL_SECONDS,
            }
            try:
                session_manager.claim_external_draft(
                    binding["session_id"],
                    lease_kind="exact_directory",
                    lease=lease,
                )
            except RuntimeError as exc:
                emit_harness_metric(
                    logger,
                    "authoritative_draft_conflict_count",
                    session_id=binding["session_id"],
                    target=str(root),
                )
                return _tool_error(
                    "stage_external_directory",
                    runtime,
                    f"directory staging conflict: {exc}",
                )
            upload_error = _upload_snapshot(self.backend, staged_dir, contents)
            if upload_error is not None:
                lease.update(
                    {
                        "status": "abandoned",
                        "abandoned_at": time.time(),
                        "abandoned_reason": "snapshot_upload_failed",
                    }
                )
                session_manager.upsert_external_directory_lease(
                    binding["session_id"], lease
                )
                return _tool_error("stage_external_directory", runtime, upload_error)
            lease["status"] = "staged"
            session_manager.upsert_external_directory_lease(binding["session_id"], lease)
            return ToolMessage(
                content=(
                    f"ExternalDirectoryLease created. lease_id={lease_id}; staged_dir={staged_dir}; "
                    f"files={len(manifest)}; bytes={lease['total_bytes']}; skipped={len(skipped)}; "
                    f"source_manifest_sha256={source_digest}. Work only inside staged_dir. "
                    f"Write temporary validators only under validation_scratch={validation_scratch}. "
                    "For write-back, call prepare_external_directory_commit, review its plan, then "
                    "call commit_external_directory. Opening the directory as the project remains recommended."
                ),
                name="stage_external_directory",
                tool_call_id=runtime.tool_call_id,
                status="success",
                artifact={"external_directory_lease": lease},
            )

        # DEPRECATED COMPATIBILITY SURFACE. Do not add new product behavior to
        # the lease protocol; multi-file atomicity belongs inside the Broker.
        def prepare_external_directory_commit(
            lease_id: str,
            directory_path: str,
            runtime: ToolRuntime[Any, Any],
            declared_delivery_files: list[str] | None = None,
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
            requested_declarations = [str(item) for item in (declared_delivery_files or [])]
            invalid_declarations = [
                item for item in requested_declarations if _safe_relative(item) is None
            ]
            if invalid_declarations:
                return _tool_error(
                    "prepare_external_directory_commit",
                    runtime,
                    "declared_delivery_files contains unsafe or non-relative paths: "
                    f"{invalid_declarations}. Use normalized paths relative to staged_dir.",
                )
            declared = {_safe_relative(item) for item in requested_declarations}
            declared.discard(None)
            undeclared_added = sorted(set(plan["added"]) - declared)
            if undeclared_added:
                return _tool_error(
                    "prepare_external_directory_commit",
                    runtime,
                    "staged directory contains undeclared new files and cannot be prepared. "
                    f"undeclared_added={undeclared_added}. Recovery actions: move temporary files to "
                    f"{lease.get('validation_scratch') or f'/scratch/validation/{lease_id}'}, remove/exclude them "
                    "from the staged delivery root, or retry with declared_delivery_files for intentional deliverables",
                )
            plan_payload = {
                **plan,
                "declared_delivery_files": sorted(declared),
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

        # DEPRECATED COMPATIBILITY SURFACE. Retirement is gated by durable
        # usage telemetry and active-lease migration audit.
        def commit_external_directory(
            lease_id: str,
            directory_path: str,
            plan_digest: str,
            runtime: ToolRuntime[Any, Any],
            validation_receipt_ids: list[str] | None = None,
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
            if lease.get("status") not in {"prepared", "committing"} or float(
                lease.get("expires_at") or 0
            ) < time.time():
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
            current_manifest_sha256 = _manifest_digest(current_manifest)
            source_manifest_sha256 = str(lease.get("source_manifest_sha256") or "")
            staged_manifest_sha256 = str(plan.get("staged_manifest_sha256") or "")
            already_applied = (
                lease.get("status") == "committing"
                and current_manifest_sha256 == staged_manifest_sha256
            )
            if not already_applied and current_manifest_sha256 != source_manifest_sha256:
                return _tool_error(
                    "commit_external_directory",
                    runtime,
                    "source directory changed after staging or during commit recovery; "
                    "create a new lease and rebase",
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
            from graph.middlewares.versioned_patch import (
                _accepted_receipts_for_target,
                _code_like_target,
                _persisted_validation_receipts,
            )

            selected_receipt_ids = {
                str(item) for item in (validation_receipt_ids or []) if str(item)
            }
            code_targets: list[tuple[str, str]] = []
            accepted_receipt_ids_by_target: dict[str, list[str]] = {}
            for relative in sorted(
                set(normalized_plan["added"]) | set(normalized_plan["modified"])
            ):
                target_path = str((root / relative).resolve())
                if not _code_like_target(target_path):
                    continue
                manifest_item = staged_manifest.get(relative)
                draft_sha256 = str(
                    manifest_item.get("sha256") if isinstance(manifest_item, dict) else ""
                )
                if not draft_sha256.startswith("sha256:"):
                    return _tool_error(
                        "commit_external_directory",
                        runtime,
                        f"code-like staged file has no content hash: {relative}",
                    )
                code_targets.append((target_path, draft_sha256))
            if code_targets:
                persisted_receipts = _persisted_validation_receipts(
                    session_manager,
                    session_id=binding["session_id"],
                    run_id=binding["run_id"],
                    goal_id=binding["goal_id"],
                    goal_revision=binding["goal_revision"],
                )
                receipt_errors: list[str] = []
                for target_path, draft_sha256 in code_targets:
                    selected_successes, blocking_failures = (
                        _accepted_receipts_for_target(
                            persisted_receipts,
                            target_path=target_path,
                            content_sha256=draft_sha256,
                            selected_receipt_ids=selected_receipt_ids,
                        )
                    )
                    if blocking_failures:
                        emit_harness_metric(
                            logger,
                            "commit_blocked_by_failed_validation_count",
                            session_id=binding["session_id"],
                            target=target_path,
                        )
                        receipt_errors.append(
                            f"{target_path}: blocking_failed_receipts="
                            f"{[str(item.get('validation_receipt_id') or '') for item in blocking_failures]}"
                        )
                    elif not selected_successes:
                        emit_harness_metric(
                            logger,
                            "validation_receipt_target_mismatch_count",
                            session_id=binding["session_id"],
                            target=target_path,
                        )
                        receipt_errors.append(
                            f"{target_path}: no supplied successful receipt for {draft_sha256}"
                        )
                    else:
                        accepted_receipt_ids_by_target[target_path] = sorted(
                            {
                                str(item.get("validation_receipt_id") or "")
                                for item in selected_successes
                                if str(item.get("validation_receipt_id") or "")
                            }
                        )
                if receipt_errors:
                    return _tool_error(
                        "commit_external_directory",
                        runtime,
                        "directory commit validation gate rejected code-like changes. "
                        + "; ".join(receipt_errors)
                        + ". Validate the exact staged files, then retry with validation_receipt_ids.",
                    )
            lease.update(
                {
                    "status": "committing",
                    "commit_started_at": time.time(),
                    "commit_tool_call_id": runtime.tool_call_id,
                    "commit_intent": {
                        "plan_digest": plan_digest,
                        "source_manifest_sha256": source_manifest_sha256,
                        "staged_manifest_sha256": staged_manifest_sha256,
                        "validation_receipt_ids": sorted(selected_receipt_ids),
                    },
                }
            )
            session_manager.upsert_external_directory_lease(
                binding["session_id"], lease
            )
            apply_error: str | None = None
            rollback = None
            if not already_applied:
                apply_error, rollback = _apply_directory_plan(
                    root,
                    lease_id,
                    normalized_plan,
                    staged_contents,
                    dict(lease.get("source_manifest") or {}),
                )
            if apply_error is not None:
                lease.update(
                    status="prepared",
                    last_commit_error=apply_error,
                    commit_intent=None,
                )
                session_manager.upsert_external_directory_lease(
                    binding["session_id"], lease
                )
                return _tool_error(
                    "commit_external_directory", runtime, f"write-back failed and rollback was attempted: {apply_error}"
                )
            committed_manifest, _contents, _skipped, verify_error = _scan_source_directory(root, include_content=False)
            if verify_error is not None or _manifest_digest(committed_manifest) != _manifest_digest(staged_manifest):
                rollback_error = rollback() if rollback is not None else None
                if rollback is not None:
                    lease.update(
                        status="prepared",
                        last_commit_error=verify_error or "manifest mismatch",
                        commit_intent=None,
                    )
                    session_manager.upsert_external_directory_lease(
                        binding["session_id"], lease
                    )
                return _tool_error(
                    "commit_external_directory",
                    runtime,
                    "post-commit verification failed and write-back was rolled back: "
                    f"{verify_error or 'manifest mismatch'}"
                    + (f"; rollback errors: {rollback_error}" if rollback_error else ""),
                )
            changed_targets = [
                (
                    str((root / relative).resolve()),
                    str(committed_manifest[relative]["sha256"]),
                )
                for relative in sorted(
                    set(normalized_plan["added"]) | set(normalized_plan["modified"])
                )
                if relative in committed_manifest
            ]
            changed_artifact_ids = {
                target_path: "artifact-"
                + hashlib.sha256(f"external\0{target_path}".encode()).hexdigest()[:20]
                for target_path, _content_sha256 in changed_targets
            }
            affected_artifact_ids = {
                *changed_artifact_ids.values(),
                *(
                    "artifact-"
                    + hashlib.sha256(
                        f"external\0{str((root / relative).resolve())}".encode()
                    ).hexdigest()[:20]
                    for relative in normalized_plan["deleted"]
                ),
            }
            prior_registry = {
                str(item.get("artifact_id") or ""): item
                for item in session_manager.list_delivered_artifacts(
                    binding["session_id"]
                )
                if str(item.get("artifact_id") or "") in affected_artifact_ids
            }
            registry_snapshot = {
                artifact_id: prior_registry.get(artifact_id)
                for artifact_id in affected_artifact_ids
            }
            try:
                deliveries = [
                    session_manager.register_delivered_artifact(
                        binding["session_id"],
                        target_path=target_path,
                        content_sha256=content_sha256,
                        source_run_id=binding["run_id"],
                        source_query_id=binding["query_id"],
                        source_goal_id=binding["goal_id"] or None,
                        source_goal_revision=(
                            int(binding["goal_revision"])
                            if binding["goal_revision"] is not None
                            else None
                        ),
                        related_artifact_ids=[
                            artifact_id
                            for other_path, artifact_id in changed_artifact_ids.items()
                            if other_path != target_path
                        ],
                        validation_receipt_ids=accepted_receipt_ids_by_target.get(
                            target_path, []
                        ),
                    )
                    for target_path, content_sha256 in changed_targets
                ]
                deletion_tombstones = [
                    session_manager.mark_delivered_artifact_deleted(
                        binding["session_id"],
                        target_path=str((root / relative).resolve()),
                        source_run_id=binding["run_id"],
                        source_query_id=binding["query_id"],
                    )
                    for relative in sorted(normalized_plan["deleted"])
                ]
                deletion_tombstones = [
                    item for item in deletion_tombstones if item is not None
                ]
            except Exception as exc:
                rollback_error = rollback() if rollback is not None else None
                session_manager.restore_delivered_artifact_registry_entries(
                    binding["session_id"], registry_snapshot
                )
                if rollback is not None:
                    lease.update(
                        status="prepared",
                        last_commit_error=f"artifact registry update failed: {exc}",
                        commit_intent=None,
                    )
                    session_manager.upsert_external_directory_lease(
                        binding["session_id"], lease
                    )
                return _tool_error(
                    "commit_external_directory",
                    runtime,
                    "artifact registry update failed"
                    + (f" and rollback failed: {rollback_error}" if rollback_error else ""),
                )
            lease.update(
                {
                    "status": "committed",
                    "commit_intent": None,
                    "committed_at": time.time(),
                    "commit_tool_call_id": runtime.tool_call_id,
                    "committed_manifest_sha256": _manifest_digest(committed_manifest),
                    "validation_receipt_ids": sorted(
                        {
                            receipt_id
                            for receipt_ids in accepted_receipt_ids_by_target.values()
                            for receipt_id in receipt_ids
                        }
                    ),
                    "delivered_artifact_ids": [
                        item["artifact_id"] for item in deliveries
                    ],
                    "delivery_receipt_ids": [
                        item["delivery_receipt_id"] for item in deliveries
                    ],
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
                artifact={
                    "external_directory_lease": lease,
                    "delivered_artifacts": deliveries,
                    "deleted_artifact_tombstones": deletion_tombstones,
                },
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
