"""SessionManager — 短期记忆管理器，基于 JSON 文件持久化会话历史"""

import hashlib
import json
import logging
import posixpath
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any

from graph.permission_policy import (
    DEFAULT_APPROVAL_MODE,
    PERMISSION_BINDING_SCHEMA_VERSION,
    SHELL_PERMISSION_BINDING_SCHEMA_VERSION,
    PermissionBindingPolicy,
    RunPermissionContext,
    ShellDirectoryGrantSpec,
    normalize_approval_mode,
    permission_policy_snapshot,
)
from graph.virtual_paths import PathAuthority, classify_path_authority
from harness.evidence_ledger import (
    EvidenceRef,
    is_evidence_ref,
    migrate_legacy_refs,
    ref_key,
    register_activation_evidence,
    repair_legacy_tool_execution_records,
    repair_legacy_validation_wrapper_records,
    resolve_evidence_ref,
)
from observability import emit_harness_metric

logger = logging.getLogger(__name__)

# 压缩摘要的固定前缀标识，agent.py 和本模块共用，用于识别摘要消息
COMPRESSED_CONTEXT_PREFIX = "[历史对话摘要]"
MIDDLE_TRIM_CONTEXT_PREFIX = "[中段历史摘要]"


def _session_write_locked(method):
    """Serialize a complete read-modify-write transaction per Session."""

    @wraps(method)
    def wrapped(self, session_id: str, *args, **kwargs):
        with self._tool_context_lock(session_id):
            return method(self, session_id, *args, **kwargs)

    return wrapped


class SessionManager:
    """短期记忆核心类：将每个会话的消息历史存为 sessions/{id}.json 文件"""

    # Background services may receive distinct SessionManager instances in
    # tests or future in-process workers. Key locks by the persisted file path
    # so the complete read-modify-write transaction remains serialized.
    _shared_session_locks: dict[str, threading.RLock] = {}
    _shared_session_locks_guard = threading.Lock()
    _AGENT_CONTEXT_COMPACTION_TTL_SECONDS = 15 * 60

    @classmethod
    def _agent_context_compaction_is_active(
        cls,
        record: Any,
        *,
        now: float | None = None,
    ) -> bool:
        if not isinstance(record, dict) or record.get("status") != "running":
            return False
        started_at = float(record.get("started_at") or 0)
        return started_at > 0 and (now if now is not None else time.time()) - started_at < cls._AGENT_CONTEXT_COMPACTION_TTL_SECONDS

    @staticmethod
    def _agent_context_transcript_fingerprint(transcript: list[Any]) -> str:
        payload = json.dumps(transcript, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._sessions_dir: Path | None = None  # 会话文件存储目录，initialize() 时设置
        self._traces_dir: Path | None = None

    def _tool_context_lock(self, session_id: str) -> threading.RLock:
        """Return the per-session lock used by background context maintenance."""

        if self._sessions_dir is None:
            key = session_id
        else:
            key = str(self._session_path(session_id).resolve())
        with type(self)._shared_session_locks_guard:
            return type(self)._shared_session_locks.setdefault(key, threading.RLock())

    def initialize(self, base_dir: Path) -> None:
        """初始化：设置存储目录为 base_dir/sessions/，不存在则创建"""
        self._base_dir = base_dir
        self._sessions_dir = base_dir / "sessions"  # 拼接会话目录路径
        self._sessions_dir.mkdir(exist_ok=True)  # 目录不存在时自动创建
        self._traces_dir = self._sessions_dir / "traces"
        self._traces_dir.mkdir(exist_ok=True)

    @property
    def is_initialized(self) -> bool:
        """Whether durable Session storage is ready for use.

        Registries are imported before application bootstrap in a few unit and
        CLI contexts.  They may still provide their in-memory semantics there,
        but must not infer that a missing persistence directory is corruption.
        """

        return self._sessions_dir is not None

    def _session_path(self, session_id: str) -> Path:
        """根据 session_id 生成对应的 JSON 文件路径"""
        assert self._sessions_dir is not None  # 确保已初始化
        safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")  # 过滤特殊字符防路径注入
        return self._sessions_dir / f"{safe_id}.json"  # 返回完整文件路径

    def _trace_path(self, session_id: str) -> Path:
        """Return the sidecar path used for heavyweight execution traces."""
        assert self._traces_dir is not None
        safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return self._traces_dir / f"{safe_id}.json"

    @staticmethod
    def _atomic_write_json(path: Path, data: Any, *, indent: int | None = None) -> None:
        """Atomically replace a JSON file.

        Trace sidecars deliberately use compact JSON: trace payloads contain many
        nested snapshots and indentation alone previously consumed tens of MB.
        """
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    indent=indent,
                    separators=None if indent is not None else (",", ":"),
                    # Session payloads are predominantly Pydantic JSON dumps,
                    # but tool/trace extensions may still contribute a native
                    # datetime.  A persistence failure must not discard an
                    # otherwise recoverable Agent run.
                    default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
                ),
                encoding="utf-8",
            )
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _migrate_legacy_traces(self, session_id: str, data: dict[str, Any]) -> bool:
        """Move embedded v2 traces into one non-duplicated sidecar.

        Returns True when the main session object was changed. Migration is lazy,
        so existing installations pay the large-file parse cost only once.
        """
        legacy_traces = data.get("traces")
        legacy_latest = data.get("trace")
        if not isinstance(legacy_traces, dict) and not isinstance(legacy_latest, dict):
            return False

        sidecar = self._read_trace_file(session_id, migrate=False)
        sidecar_traces = sidecar.get("traces")
        traces: dict[str, Any] = {}
        if isinstance(legacy_traces, dict):
            traces.update(
                {str(query_id): trace for query_id, trace in legacy_traces.items() if isinstance(trace, dict)}
            )
        if isinstance(sidecar_traces, dict):
            # A sidecar may already contain a newer completed trace if a prior
            # migration was interrupted before the main-file rewrite.
            traces.update(
                {str(query_id): trace for query_id, trace in sidecar_traces.items() if isinstance(trace, dict)}
            )

        latest_query_id = data.get("latest_query_id") or sidecar.get("latest_query_id")
        if isinstance(legacy_latest, dict):
            fallback_id = legacy_latest.get("query_id") or data.get("latest_trace_id")
            if not latest_query_id and isinstance(fallback_id, str):
                latest_query_id = fallback_id
            if isinstance(latest_query_id, str) and latest_query_id not in traces:
                traces[latest_query_id] = legacy_latest

        if "loaded_skill_ids" not in data:
            inferred_skill_ids = self._loaded_skill_ids_from_traces({"traces": traces})
            if inferred_skill_ids:
                data["loaded_skill_ids"] = sorted(inferred_skill_ids)

        migrated = {"traces": traces}
        if isinstance(latest_query_id, str):
            migrated["latest_query_id"] = latest_query_id
        latest_trace_id = data.get("latest_trace_id") or sidecar.get("latest_trace_id")
        if isinstance(latest_trace_id, str):
            migrated["latest_trace_id"] = latest_trace_id
        self._write_trace_file(session_id, migrated)

        for key in ("trace", "traces", "latest_query_id", "latest_trace_id"):
            data.pop(key, None)
        return True

    def _read_trace_file(self, session_id: str, *, migrate: bool = True) -> dict[str, Any]:
        path = self._trace_path(session_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        if migrate:
            # Reading the main file here is only a legacy fallback. New sessions
            # never touch session.json when a trace is requested or updated.
            self._read_file(session_id)
            if path.exists():
                return self._read_trace_file(session_id, migrate=False)
        return {}

    def _write_trace_file(self, session_id: str, data: dict[str, Any]) -> None:
        self._atomic_write_json(self._trace_path(session_id), data)

    @_session_write_locked
    def _read_file(self, session_id: str) -> dict[str, Any]:
        """从磁盘读取会话文件，自动兼容 v1(纯列表) → v2(带元数据的字典) 格式"""
        path = self._session_path(session_id)  # 获取文件路径
        if not path.exists():  # 文件不存在返回空字典
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))  # 读取并解析 JSON
            if isinstance(data, list):  # v1 格式：纯消息列表
                now = time.time()  # 获取当前时间戳
                return {  # 转换为 v2 格式
                    "title": session_id,  # 用 session_id 作为默认标题
                    "created_at": path.stat().st_ctime,  # 用文件创建时间作为会话创建时间
                    "updated_at": now,  # 更新时间设为当前
                    "messages": data,  # 原始消息列表保留
                }
            if isinstance(data, dict) and self._migrate_legacy_traces(session_id, data):
                self._write_file(session_id, data)
            return data  # v2 格式直接返回
        except (json.JSONDecodeError, Exception):  # JSON 解析失败返回空
            return {}

    def _write_file(self, session_id: str, data: dict[str, Any]) -> None:
        """原子写入会话数据，避免读者观察到半截 JSON。"""
        data["updated_at"] = time.time()  # 每次写入都刷新更新时间
        path = self._session_path(session_id)  # 获取文件路径
        self._atomic_write_json(path, data, indent=2)

    @_session_write_locked
    def create_session(
        self,
        session_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        approval_mode: str | None = None,
    ) -> dict[str, Any]:
        """创建空会话，返回元数据（id/title/时间戳）"""
        now = time.time()  # 当前时间戳
        data: dict[str, Any] = {  # 初始会话结构
            "title": "New Chat",  # 默认标题
            "created_at": now,  # 创建时间
            "updated_at": now,  # 更新时间
            "runtime_mode": "agent",  # Chat 已退役；新会话统一使用 Agent
            "messages": [],  # 空消息列表
            "permissions": {
                "approval_mode": normalize_approval_mode(approval_mode or DEFAULT_APPROVAL_MODE).value,
                "policy_epoch": 1,
                "grants_revision": 0,
                "grants": [],
            },
        }
        if metadata:
            # Permission state is a control-plane authority and may not be
            # injected through generic metadata.
            data.update({key: value for key, value in metadata.items() if key != "permissions"})
        self._write_file(session_id, data)  # 写入磁盘
        return self._metadata_from_data(session_id, data)

    def _metadata_from_data(self, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Build a stable metadata object for list/create responses."""
        meta = {
            "id": session_id,
            "title": data.get("title", session_id),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "runtime_mode": data.get("runtime_mode", "chat"),
        }
        for key in (
            "project_id",
            "project_path",
            "workspace_type",
            "workspace_path",
            "analytics_model_id",
            "llm_model_id",
            "thinking_level",
            "credential_name",
            "headless_enabled",
            "worker_key_id",
            "platform_id",
            "worker_id",
            "interaction_mode",
            "headless_pending_input",
        ):
            if key in data:
                meta[key] = data.get(key)
        policy = permission_policy_snapshot(data.get("permissions"))
        meta["approval_mode"] = policy["approval_mode"]
        meta["policy_epoch"] = policy["policy_epoch"]
        meta["policy_version"] = policy["policy_version"]
        return meta

    def get_permission_policy(self, session_id: str) -> dict[str, Any]:
        """Return the effective Session permission policy without Run state."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        return permission_policy_snapshot(data.get("permissions"))

    @_session_write_locked
    def set_approval_mode_if_idle(
        self,
        session_id: str,
        mode: str,
        *,
        expected_epoch: int | None = None,
    ) -> dict[str, Any]:
        """Atomically change mode only when no non-terminal Run exists."""

        requested = normalize_approval_mode(mode)
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        current = permission_policy_snapshot(data.get("permissions"))
        if expected_epoch is not None and expected_epoch != current["policy_epoch"]:
            raise ValueError("Permission policy changed concurrently; reload before retrying.")

        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        terminal_statuses = {
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }
        active = next(
            (
                run
                for run in (runs.values() if isinstance(runs, dict) else ())
                if isinstance(run, dict) and run.get("status") not in terminal_statuses
            ),
            None,
        )
        if active is not None:
            raise RuntimeError(
                f"Session {session_id} has active Run {active.get('run_id')}; "
                "approval mode is frozen until the Run finishes."
            )
        if requested.value == current["approval_mode"]:
            return current

        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
            data["permissions"] = permissions
        permissions["approval_mode"] = requested.value
        permissions["policy_epoch"] = current["policy_epoch"] + 1
        now = time.time()
        grants = permissions.get("grants")
        if isinstance(grants, list):
            revoked_any = False
            for grant in grants:
                grant_type = str(grant.get("type") or "") if isinstance(grant, dict) else ""
                invalidated_session_capability = (
                    (
                        grant_type == "tool_action"
                        or (grant.get("scope") == "session" and grant_type.startswith("external_directory_"))
                    )
                    if isinstance(grant, dict)
                    else False
                )
                if invalidated_session_capability and not grant.get("revoked_at"):
                    grant["revoked_at"] = now
                    grant["revocation_reason"] = "permission_policy_changed"
                    revoked_any = True
            if revoked_any:
                permissions["grants_revision"] = int(permissions.get("grants_revision") or 0) + 1
        self._write_file(session_id, data)
        return permission_policy_snapshot(permissions)

    @_session_write_locked
    def update_metadata(self, session_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """Merge non-authoritative metadata into an existing Session."""
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        allowed_keys = {
            "runtime_mode",
            "project_id",
            "project_path",
            "workspace_type",
            "workspace_path",
            "analytics_model_id",
            "llm_model_id",
            "thinking_level",
            "credential_name",
            "headless_enabled",
            "worker_key_id",
            "platform_id",
            "worker_id",
            "interaction_mode",
            "headless_pending_input",
        }
        forbidden = set(metadata) - allowed_keys
        if forbidden:
            raise ValueError(f"Unsupported Session metadata fields: {sorted(forbidden)}")
        data.update({key: metadata[key] for key in allowed_keys if key in metadata})
        self._write_file(session_id, data)
        return self._metadata_from_data(session_id, data)

    def get_metadata(self, session_id: str) -> dict[str, Any]:
        """Return session metadata without mutating the session."""

        data = self._read_file(session_id)
        if not data:
            # Missing runtime metadata identifies pre-Agent/legacy Chat data;
            # all newly created Sessions are stamped as Agent above.
            return {"id": session_id, "title": session_id, "runtime_mode": "chat"}
        return self._metadata_from_data(session_id, data)

    def session_exists(self, session_id: str) -> bool:
        """Return whether an authoritative Session JSON exists."""

        safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return bool(safe_id == session_id and self._session_path(session_id).is_file())

    @staticmethod
    def _loaded_skill_ids_from_traces(data: dict[str, Any]) -> set[str]:
        """Recover successful authoritative Skill reads from legacy trace snapshots."""

        loaded: set[str] = set()
        traces = data.get("traces")
        if not isinstance(traces, dict):
            return loaded
        for trace in traces.values():
            if not isinstance(trace, dict):
                continue
            effects = trace.get("middleware_effects")
            if not isinstance(effects, list):
                continue
            for effect in effects:
                if not isinstance(effect, dict):
                    continue
                for boundary_name in ("before", "after"):
                    boundary = effect.get(boundary_name)
                    recent = boundary.get("recent_messages") if isinstance(boundary, dict) else None
                    if not isinstance(recent, list):
                        continue
                    for index, message in enumerate(recent[:-1]):
                        if not isinstance(message, dict) or message.get("role") != "ai":
                            continue
                        next_message = recent[index + 1]
                        if (
                            not isinstance(next_message, dict)
                            or next_message.get("role") != "tool"
                            or next_message.get("name") != "read_file"
                        ):
                            continue
                        preview = str(next_message.get("preview") or "").lower()
                        if any(marker in preview for marker in ("error", "not found", "失败", "不存在")):
                            continue
                        for tool_call in message.get("tool_calls") or []:
                            if not isinstance(tool_call, dict) or tool_call.get("name") != "read_file":
                                continue
                            args = tool_call.get("args") or {}
                            path = str(args.get("file_path") or args.get("path") or "").replace("\\", "/")
                            parts = path.split("/")
                            if len(parts) == 4 and parts[1] == "skills" and parts[3] == "SKILL.md":
                                loaded.add(parts[2])
        return loaded

    @_session_write_locked
    def get_loaded_skill_ids(self, session_id: str) -> list[str]:
        """Return the legacy Session Skill index, never capability authority."""

        data = self._read_file(session_id)
        if not data:
            return []
        stored = {str(item) for item in data.get("loaded_skill_ids") or [] if str(item)}
        if "loaded_skill_ids" in data:
            return sorted(stored)
        inferred = self._loaded_skill_ids_from_traces(data)
        loaded = sorted(stored | inferred)
        if loaded != sorted(stored):
            data["loaded_skill_ids"] = loaded
            self._write_file(session_id, data)
        return loaded

    @_session_write_locked
    def add_loaded_skill_ids(self, session_id: str, skill_ids: list[str]) -> list[str]:
        """Persist legacy discovery metadata without activating any tools."""

        data = self._read_file(session_id)
        if not data:
            return []
        current = {str(item) for item in data.get("loaded_skill_ids") or [] if str(item)}
        current.update(str(item) for item in skill_ids if str(item))
        loaded = sorted(current)
        data["loaded_skill_ids"] = loaded
        self._write_file(session_id, data)
        return loaded

    def get_skill_cache_entry(
        self,
        session_id: str,
        skill_id: str,
        *,
        content_sha256: str | None = None,
        policy_epoch: int | None = None,
    ) -> dict[str, Any] | None:
        """Return one Session Skill cache entry when its bindings still match."""

        from harness.models import SkillCacheEntry

        data = self._read_file(session_id)
        cache = data.get("skill_cache") if isinstance(data, dict) else None
        raw = cache.get(skill_id) if isinstance(cache, dict) else None
        if not isinstance(raw, dict):
            return None
        try:
            entry = SkillCacheEntry.model_validate(raw)
        except ValueError:
            return None
        if content_sha256 is not None and entry.skill_content_sha256 != content_sha256:
            return None
        if policy_epoch is not None and entry.policy_epoch != policy_epoch:
            return None
        return entry.model_dump(mode="json")

    @_session_write_locked
    def record_skill_cache_entry(
        self,
        session_id: str,
        entry: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist canonical Skill instructions without granting capability."""

        from harness.models import SkillCacheEntry

        parsed = SkillCacheEntry.model_validate(entry)
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        cache = data.setdefault("skill_cache", {})
        if not isinstance(cache, dict):
            cache = {}
            data["skill_cache"] = cache
        cache[parsed.skill_id] = parsed.model_dump(mode="json")
        loaded = {str(item) for item in data.get("loaded_skill_ids") or [] if str(item)}
        loaded.add(parsed.skill_id)
        data["loaded_skill_ids"] = sorted(loaded)
        self._write_file(session_id, data)
        return parsed.model_dump(mode="json")

    def load_session(self, session_id: str) -> list[dict[str, Any]]:
        """加载指定会话的消息列表，自动合并 archive/ 中的归档消息。

        前端通过 /history 调用本方法时，始终看到完整历史（archive + 当前 messages）。
        """
        data = self._read_file(session_id)
        if not data:
            return []

        if isinstance(data.get("display_messages"), list):
            return self._project_message_timestamps(
                data,
                list(data.get("display_messages", [])),
            )

        messages = list(data.get("messages", []))

        # 合并 archive/ 中的归档消息（按 archived_at 升序）
        archive_dir = self._sessions_dir / "archive"
        if archive_dir.exists():
            archived: list[tuple[float, list[dict[str, Any]]]] = []
            for f in sorted(archive_dir.glob(f"{session_id}_*.json")):
                try:
                    arc = json.loads(f.read_text(encoding="utf-8"))
                    archived.append((arc.get("archived_at", 0), arc.get("messages", [])))
                except Exception:
                    continue
            # 按归档时间升序拼接
            archived_messages: list[dict[str, Any]] = []
            for _, arc_messages in sorted(archived, key=lambda x: x[0]):
                archived_messages.extend(arc_messages)
            messages = archived_messages + messages

        return self._project_message_timestamps(data, messages)

    @staticmethod
    def _valid_timestamp(value: Any) -> float | None:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None
        return timestamp if timestamp > 0 else None

    @classmethod
    def _query_created_at_by_id(cls, data: dict[str, Any]) -> dict[str, float]:
        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        if not isinstance(runs, dict):
            return {}
        result: dict[str, float] = {}
        for run in runs.values():
            if not isinstance(run, dict):
                continue
            query_id = str(run.get("query_id") or "")
            timestamp = cls._valid_timestamp(run.get("created_at"))
            if query_id and timestamp is not None:
                result[query_id] = timestamp
        return result

    @classmethod
    def _assistant_created_at_by_query_id(cls, data: dict[str, Any]) -> dict[str, float]:
        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        if not isinstance(runs, dict):
            return {}
        result: dict[str, float] = {}
        for run in runs.values():
            if not isinstance(run, dict):
                continue
            query_id = str(run.get("query_id") or "")
            timestamp = (
                cls._valid_timestamp(run.get("completed_at"))
                or cls._valid_timestamp(run.get("updated_at"))
                or cls._valid_timestamp(run.get("created_at"))
            )
            if query_id and timestamp is not None:
                result[query_id] = timestamp
        return result

    @classmethod
    def _project_message_timestamps(
        cls,
        data: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return display messages with stable event times, including legacy Sessions."""

        session_created_at = cls._valid_timestamp(data.get("created_at")) or 0.0
        query_created_at_by_id = cls._query_created_at_by_id(data)
        assistant_created_at_by_query_id = cls._assistant_created_at_by_query_id(data)
        projected: list[dict[str, Any]] = []
        previous_timestamp = session_created_at
        for index, raw_message in enumerate(messages):
            if not isinstance(raw_message, dict):
                continue
            message = dict(raw_message)
            timestamp = cls._valid_timestamp(message.get("created_at"))
            query_id = str(message.get("query_id") or "")
            if timestamp is None and query_id:
                timestamp = (
                    assistant_created_at_by_query_id.get(query_id)
                    if message.get("role") == "assistant"
                    else query_created_at_by_id.get(query_id)
                )
            if timestamp is None and message.get("role") == "user":
                # Legacy user messages predate query_id persistence. Pair the
                # turn with the next assistant message and use its Run start.
                for candidate in messages[index + 1 :]:
                    if not isinstance(candidate, dict):
                        continue
                    if candidate.get("role") == "user":
                        break
                    candidate_query_id = str(candidate.get("query_id") or "")
                    if candidate_query_id:
                        timestamp = query_created_at_by_id.get(candidate_query_id)
                        if timestamp is not None:
                            break
            timestamp = timestamp or previous_timestamp or session_created_at
            message["created_at"] = timestamp
            previous_timestamp = timestamp
            projected.append(message)
        return projected

    @_session_write_locked
    def save_message(
        self,
        session_id: str,  # 会话 ID
        role: str,  # 角色：user 或 assistant
        content: str,  # 消息内容
        tool_calls: list[dict[str, Any]] | None = None,  # 可选的工具调用记录
        sources: list[dict[str, Any]] | None = None,  # 用户可见的结构化来源
        citations: list[dict[str, Any]] | None = None,  # 正文与来源的引用映射
        reasoning_content: str | None = None,  # 思考链内容（工具调用回合必须回传）
        timeline: list[dict[str, Any]] | None = None,  # 前端时间轴（reasoning/tool 交错顺序）
        segments: list[dict[str, Any]] | None = None,  # UI 分段（每轮模型调用为一个 segment）
        interrupted: bool = False,  # 本轮是否由用户主动停止
        interruption_notice: str | None = None,  # 用户可见的停止提示
        error_notice: str | None = None,  # 用户可见的错误提示
        run_boundary_notice: dict[str, Any] | None = None,  # 跨 Run 续跑/停止说明
        attachments: list[dict[str, Any]] | None = None,  # Session-scoped stable attachment refs
        output_attachments: list[dict[str, Any]] | None = None,  # Assistant-published derived attachments
        created_at: float | None = None,  # Query/message input time as Unix seconds
    ) -> None:
        """追加一条消息到会话历史"""
        data = self._read_file(session_id)  # 读取现有数据
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        compaction = data.get("agent_context_compaction")
        if role == "user" and self._agent_context_compaction_is_active(compaction):
            raise RuntimeError(
                f"Session {session_id} is compacting Agent context; retry after maintenance completes"
            )
        if role == "user" and isinstance(compaction, dict) and compaction.get("status") == "running":
            compaction["status"] = "expired"
            compaction["completed_at"] = time.time()
            compaction["error"] = "maintenance claim expired before user message"
        msg = self._build_message_payload(
            role,
            content,
            tool_calls=tool_calls,
            sources=sources,
            citations=citations,
            reasoning_content=reasoning_content,
            timeline=timeline,
            segments=segments,
            interrupted=interrupted,
            interruption_notice=interruption_notice,
            error_notice=error_notice,
            run_boundary_notice=run_boundary_notice,
            attachments=attachments,
            output_attachments=output_attachments,
            created_at=created_at,
        )
        data["messages"].append(msg)  # 追加到消息列表末尾
        if isinstance(data.get("display_messages"), list):
            data["display_messages"].append(dict(msg))
        self._write_file(session_id, data)  # 写回磁盘

    @staticmethod
    def _build_message_payload(
        role: str,
        content: str,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
        sources: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        timeline: list[dict[str, Any]] | None = None,
        segments: list[dict[str, Any]] | None = None,
        interrupted: bool = False,
        interruption_notice: str | None = None,
        error_notice: str | None = None,
        verification_summary: str | None = None,
        run_boundary_notice: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        output_attachments: list[dict[str, Any]] | None = None,
        query_id: str | None = None,
        status: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        """Build the persisted message shape shared by append and upsert paths."""

        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "created_at": float(created_at) if created_at is not None else time.time(),
        }
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        if timeline:
            msg["timeline"] = timeline
        if segments:
            msg["segments"] = segments
        if sources:
            msg["sources"] = sources
        if citations:
            msg["citations"] = citations
        if interrupted:
            msg["interrupted"] = True
        if interruption_notice:
            msg["interruption_notice"] = interruption_notice
        if error_notice:
            msg["error_notice"] = error_notice
        if verification_summary:
            msg["verification_summary"] = verification_summary
        if run_boundary_notice:
            msg["run_boundary_notice"] = run_boundary_notice
        if attachments:
            msg["attachments"] = [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or item.get("id") or "attachment"),
                    "type": str(item.get("type") or "file"),
                    "mime_type": str(item.get("mime_type") or ""),
                    "size": int(item.get("size") or 0),
                    "source": str(item.get("source") or "upload"),
                    "sha256": str(item.get("sha256") or ""),
                    "download_url": str(item.get("download_url") or ""),
                    "preview_url": str(item.get("preview_url") or ""),
                    "preview_mime_type": str(item.get("preview_mime_type") or ""),
                    "width": int(item.get("width") or 0),
                    "height": int(item.get("height") or 0),
                }
                for item in attachments
                if isinstance(item, dict) and item.get("id")
            ]
        if output_attachments:
            msg["output_attachments"] = [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or item.get("id") or "attachment"),
                    "type": str(item.get("type") or "file"),
                    "mime_type": str(item.get("mime_type") or ""),
                    "size": int(item.get("size") or 0),
                    "source": str(item.get("source") or "generated"),
                    "sha256": str(item.get("sha256") or ""),
                    "derived_from": str(item.get("derived_from") or ""),
                    "created_by_run_id": str(item.get("created_by_run_id") or ""),
                    "created_by_query_id": str(item.get("created_by_query_id") or ""),
                    "created_by_tool_call_id": str(item.get("created_by_tool_call_id") or ""),
                    "created_by_goal_id": str(item.get("created_by_goal_id") or ""),
                    "created_by_goal_revision": item.get("created_by_goal_revision"),
                    "created_at": float(item.get("created_at") or 0),
                    "download_url": str(item.get("download_url") or ""),
                    "preview_url": str(item.get("preview_url") or ""),
                    "preview_mime_type": str(item.get("preview_mime_type") or ""),
                    "width": int(item.get("width") or 0),
                    "height": int(item.get("height") or 0),
                }
                for item in output_attachments
                if isinstance(item, dict) and item.get("id")
            ]
        if query_id:
            msg["query_id"] = query_id
        if status:
            msg["status"] = status
        return msg

    @_session_write_locked
    def upsert_assistant_message(
        self,
        session_id: str,
        *,
        query_id: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        sources: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        timeline: list[dict[str, Any]] | None = None,
        segments: list[dict[str, Any]] | None = None,
        interrupted: bool = False,
        interruption_notice: str | None = None,
        error_notice: str | None = None,
        verification_summary: str | None = None,
        run_boundary_notice: dict[str, Any] | None = None,
        output_attachments: list[dict[str, Any]] | None = None,
        status: str = "running",
    ) -> None:
        """Create or replace the assistant draft for a query.

        Agent mode streams partial work over SSE. This method makes the session
        file the durable source of truth while a run is still in progress, so
        refreshes and later "继续" turns can see completed tool results.
        """

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")

        msg = self._build_message_payload(
            "assistant",
            content,
            tool_calls=tool_calls,
            sources=sources,
            citations=citations,
            reasoning_content=reasoning_content,
            timeline=timeline,
            segments=segments,
            interrupted=interrupted,
            interruption_notice=interruption_notice,
            error_notice=error_notice,
            verification_summary=verification_summary,
            run_boundary_notice=run_boundary_notice,
            output_attachments=output_attachments,
            query_id=query_id,
            status=status,
        )

        messages = data.setdefault("messages", [])
        replaced = False
        previous_context_signature: tuple[tuple[str, str, str], ...] = ()

        def context_signature(message: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
            entries: list[tuple[str, str, str]] = []
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict) or not tool_call.get("context_output"):
                    continue
                metadata = tool_call.get("context_compaction")
                if not isinstance(metadata, dict) or metadata.get("status") != "ready":
                    continue
                entries.append(
                    (
                        str(tool_call.get("id") or ""),
                        str(metadata.get("source_hash") or ""),
                        str(tool_call.get("context_output") or ""),
                    )
                )
            return tuple(entries)

        for index, existing in enumerate(messages):
            if (
                isinstance(existing, dict)
                and existing.get("role") == "assistant"
                and existing.get("query_id") == query_id
            ):
                previous_context_signature = context_signature(existing)
                existing_created_at = self._valid_timestamp(existing.get("created_at"))
                if existing_created_at is not None:
                    msg["created_at"] = existing_created_at
                messages[index] = msg
                replaced = True
                break
        if not replaced:
            messages.append(msg)

        next_context_signature = context_signature(msg)
        if next_context_signature and next_context_signature != previous_context_signature:
            data["tool_context_revision"] = int(data.get("tool_context_revision", 0) or 0) + 1

        if isinstance(data.get("display_messages"), list):
            display_messages = data["display_messages"]
            display_replaced = False
            for index, existing in enumerate(display_messages):
                if (
                    isinstance(existing, dict)
                    and existing.get("role") == "assistant"
                    and existing.get("query_id") == query_id
                ):
                    display_messages[index] = dict(msg)
                    display_replaced = True
                    break
            if not display_replaced:
                display_messages.append(dict(msg))

        self._write_file(session_id, data)

    @_session_write_locked
    def commit_accepted_completion(
        self,
        session_id: str,
        *,
        run: dict[str, Any],
        goal: dict[str, Any] | None,
        query_id: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        sources: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        timeline: list[dict[str, Any]] | None = None,
        segments: list[dict[str, Any]] | None = None,
        output_attachments: list[dict[str, Any]] | None = None,
        verification_summary: str | None = None,
    ) -> None:
        """Atomically publish an accepted answer with its Run/Goal authority.

        A caller cannot use this path to publish a draft, a failed verification
        result, or a Goal Run that has not completed the current revision.
        The final Session write contains the assistant message, accepted report,
        RunOutcome and Goal decision together.
        """

        from harness.models import (
            GoalCompletionPolicy,
            GoalCompletionRequest,
            GoalCompletionRequestStatus,
            GoalRecord,
            GoalStatus,
            RunOutcome,
            RunRecord,
            VerificationStatus,
        )

        validated_run = RunRecord.model_validate(run)
        if validated_run.session_id != session_id or validated_run.query_id != query_id:
            raise ValueError("Accepted completion identity does not match the Session query")
        report = validated_run.verification_report
        if validated_run.outcome != RunOutcome.COMPLETED:
            raise ValueError("Only a completed Run may publish a final response")

        validated_goal: GoalRecord | None = None
        if goal is not None:
            validated_goal = GoalRecord.model_validate(goal)
            if (
                validated_goal.session_id != session_id
                or validated_goal.goal_id != validated_run.goal_id
                or validated_goal.status != GoalStatus.COMPLETED
            ):
                raise ValueError("Goal completion is not accepted for the current revision")

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        runs = harness.setdefault("runs", {})
        if validated_run.run_id not in runs:
            raise ValueError(f"Run {validated_run.run_id} does not exist in session {session_id}")
        completion_requests = harness.setdefault("completion_requests", {})
        completion_request: GoalCompletionRequest | None = None
        if validated_goal is not None:
            goals = harness.setdefault("goals", {})
            raw_goal = goals.get(validated_goal.goal_id)
            raw_run = runs.get(validated_run.run_id)
            if not isinstance(raw_goal, dict) or not isinstance(raw_run, dict):
                raise ValueError("Goal completion authority no longer exists")
            if (
                str(raw_goal.get("status") or "") != GoalStatus.ACTIVE.value
                or int(raw_goal.get("objective_revision") or 0) != validated_goal.objective_revision
                or str(raw_goal.get("current_run_id") or "") != validated_run.run_id
                or str(raw_run.get("status") or "")
                != (
                    "evaluating"
                    if validated_goal.completion_policy == GoalCompletionPolicy.RUBRIC
                    else "running"
                )
            ):
                raise ValueError("Goal completion state-safety check failed")
            if raw_goal.get("requested_status"):
                raise ValueError("Goal completion is blocked by a pending control request")
            # A staged external mutation is an unfinished operation, not
            # completion evidence. Refuse before changing any terminal state.
            for lease_collection, draft_statuses in (
                ("external_artifact_leases", {"claiming", "staged"}),
                ("external_directory_leases", {"claiming", "staged", "prepared"}),
            ):
                leases = data.get(lease_collection)
                if any(
                    isinstance(lease, dict)
                    and str(lease.get("status") or "") in draft_statuses
                    and self._lease_matches_execution_scope(lease, validated_run)
                    for lease in (leases or {}).values()
                ) if isinstance(leases, dict) else False:
                    raise ValueError("Goal completion is blocked by an unfinished external mutation")
            request_id = str(validated_run.completion_request_id or "")
            raw_request = completion_requests.get(request_id)
            if not isinstance(raw_request, dict):
                raise ValueError("Goal completion requires a persisted completion request")
            completion_request = GoalCompletionRequest.model_validate(raw_request)
            if (
                completion_request.goal_id != validated_goal.goal_id
                or completion_request.run_id != validated_run.run_id
                or completion_request.objective_revision != validated_goal.objective_revision
                or completion_request.status not in {
                    GoalCompletionRequestStatus.REQUESTED,
                    GoalCompletionRequestStatus.EVALUATING,
                }
            ):
                raise ValueError("Goal completion request is no longer valid")
            if completion_request.policy != validated_goal.completion_policy:
                raise ValueError("Goal completion policy does not match its request")
            if completion_request.policy == GoalCompletionPolicy.RUBRIC and not (
                report is not None
                and report.status == VerificationStatus.SATISFIED
                and report.accepted_for_goal_revision is True
            ):
                raise ValueError("Rubric completion requires an accepted report")
            if completion_request.policy == GoalCompletionPolicy.STANDARD and report is not None:
                raise ValueError("Standard completion must not create a Rubric report")
            completion_request.status = GoalCompletionRequestStatus.ACCEPTED
            completion_request.decided_at = time.time()
            completion_requests[request_id] = completion_request.model_dump(mode="json")
            validated_goal.latest_completion_request_id = request_id
        runs[validated_run.run_id] = validated_run.model_dump(mode="json")
        harness["latest_run_id"] = validated_run.run_id
        self._abandon_terminal_run_search_snapshots(data, validated_run)
        if validated_goal is not None:
            goals[validated_goal.goal_id] = validated_goal.model_dump(mode="json")
            harness["active_goal_id"] = None
            self._abandon_uncommitted_execution_leases(data, validated_run)

        message = self._build_message_payload(
            "assistant",
            content,
            tool_calls=tool_calls,
            sources=sources,
            citations=citations,
            reasoning_content=reasoning_content,
            timeline=timeline,
            segments=segments,
            output_attachments=output_attachments,
            query_id=query_id,
            status="completed",
        )
        if verification_summary:
            message["verification_summary"] = verification_summary
        for collection_name in ("messages", "display_messages"):
            collection = data.get(collection_name)
            if collection_name == "messages" and not isinstance(collection, list):
                collection = data.setdefault("messages", [])
            if not isinstance(collection, list):
                continue
            for index, existing in enumerate(collection):
                if (
                    isinstance(existing, dict)
                    and existing.get("role") == "assistant"
                    and existing.get("query_id") == query_id
                ):
                    existing_created_at = self._valid_timestamp(existing.get("created_at"))
                    if existing_created_at is not None:
                        message["created_at"] = existing_created_at
                    collection[index] = dict(message)
                    break
            else:
                collection.append(dict(message))

        self._write_file(session_id, data)

    @_session_write_locked
    def record_goal_completion_request(
        self,
        session_id: str,
        *,
        goal_id: str,
        objective_revision: int,
        run_id: str,
        tool_call_id: str,
        message: str = "",
    ) -> dict[str, Any]:
        """Persist one explicit completion declaration, keyed by Tool Call id."""

        from harness.models import (
            GoalCompletionRequest,
            GoalRecord,
            GoalStatus,
            RunRecord,
            RunStatus,
        )

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        raw_goal = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(raw_run, dict) or not isinstance(raw_goal, dict):
            raise ValueError("Goal or Run does not exist")
        run = RunRecord.model_validate(raw_run)
        goal = GoalRecord.model_validate(raw_goal)
        if (
            goal.status != GoalStatus.ACTIVE
            or goal.objective_revision != objective_revision
            or goal.current_run_id != run_id
            or run.goal_id != goal_id
            or run.goal_revision != objective_revision
            or run.status != RunStatus.RUNNING
        ):
            raise ValueError("Completion request does not match the active Goal Run")
        call_id = str(tool_call_id or "").strip()
        if not call_id:
            raise ValueError("Completion request requires tool_call_id")
        requests = harness.setdefault("completion_requests", {})
        for raw in requests.values():
            if not isinstance(raw, dict):
                continue
            existing = GoalCompletionRequest.model_validate(raw)
            if existing.tool_call_id == call_id:
                if (
                    existing.goal_id == goal_id
                    and existing.objective_revision == objective_revision
                    and existing.run_id == run_id
                ):
                    return existing.model_dump(mode="json")
                raise ValueError("tool_call_id is already bound to another completion request")
        request = GoalCompletionRequest(
            request_id=f"completion-{uuid.uuid4().hex[:16]}",
            goal_id=goal_id,
            objective_revision=objective_revision,
            run_id=run_id,
            tool_call_id=call_id,
            policy=goal.completion_policy,
            message=str(message or "").strip()[:2000],
        )
        requests[request.request_id] = request.model_dump(mode="json")
        goal.latest_completion_request_id = request.request_id
        run.completion_request_id = request.request_id
        run.completion_requested_at = request.requested_at
        runs[run_id] = run.model_dump(mode="json")
        goals[goal_id] = goal.model_dump(mode="json")
        self._write_file(session_id, data)
        return request.model_dump(mode="json")

    @_session_write_locked
    def invalidate_goal_completion_request(
        self,
        session_id: str,
        *,
        run_id: str,
        reason: str,
    ) -> dict[str, Any] | None:
        """Invalidate the latest live request when work resumes after declaring done."""

        from harness.models import GoalCompletionRequest, GoalCompletionRequestStatus

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        requests = harness.get("completion_requests") if isinstance(harness, dict) else None
        if not isinstance(requests, dict):
            return None
        candidates = [
            GoalCompletionRequest.model_validate(raw)
            for raw in requests.values()
            if isinstance(raw, dict)
            and str(raw.get("run_id") or "") == run_id
            and str(raw.get("status") or "") == GoalCompletionRequestStatus.REQUESTED.value
        ]
        if not candidates:
            return None
        request = max(candidates, key=lambda item: item.requested_at)
        request.status = GoalCompletionRequestStatus.INVALIDATED
        request.invalidated_reason = str(reason or "post_completion_tool_call")[:500]
        request.decided_at = time.time()
        requests[request.request_id] = request.model_dump(mode="json")
        self._write_file(session_id, data)
        return request.model_dump(mode="json")

    @_session_write_locked
    def update_goal_completion_request_status(
        self,
        session_id: str,
        request_id: str,
        status: str,
        *,
        verification_report_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Advance a persisted request without conflating it with Goal state."""

        from harness.models import GoalCompletionRequest, GoalCompletionRequestStatus

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        requests = harness.get("completion_requests") if isinstance(harness, dict) else None
        raw = requests.get(request_id) if isinstance(requests, dict) else None
        if not isinstance(raw, dict):
            raise ValueError(f"Completion request {request_id} does not exist")
        request = GoalCompletionRequest.model_validate(raw)
        next_status = GoalCompletionRequestStatus(status)
        if request.status == next_status:
            return request.model_dump(mode="json")
        if request.status not in {
            GoalCompletionRequestStatus.REQUESTED,
            GoalCompletionRequestStatus.EVALUATING,
        }:
            raise ValueError(f"Completion request {request_id} is already decided")
        request.status = next_status
        request.verification_report_id = verification_report_id or request.verification_report_id
        if reason:
            request.invalidated_reason = reason[:500]
        if next_status in {
            GoalCompletionRequestStatus.ACCEPTED,
            GoalCompletionRequestStatus.REJECTED,
            GoalCompletionRequestStatus.INVALIDATED,
            GoalCompletionRequestStatus.NEEDS_REVISION,
        }:
            request.decided_at = time.time()
        requests[request_id] = request.model_dump(mode="json")
        self._write_file(session_id, data)
        return request.model_dump(mode="json")

    @_session_write_locked
    def set_assistant_run_boundary_notice(
        self,
        session_id: str,
        query_id: str,
        notice: dict[str, Any],
        *,
        clear_verification_summary: bool = False,
    ) -> None:
        """Persist a user-facing Run boundary without rewriting message content.

        When the Goal flow auto-continues, the boundary notice is the single
        call to action: any terminal verification guidance on the same
        message would contradict it, so callers may clear it here.
        """

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        updated = False
        for collection_name in ("messages", "display_messages"):
            collection = data.get(collection_name)
            if not isinstance(collection, list):
                continue
            for message in reversed(collection):
                if (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and message.get("query_id") == query_id
                ):
                    message["run_boundary_notice"] = dict(notice)
                    if clear_verification_summary:
                        message.pop("verification_summary", None)
                    updated = True
                    break
        if not updated:
            raise ValueError(f"Assistant message for query {query_id} does not exist")
        self._write_file(session_id, data)

    @staticmethod
    def _tool_result_context(
        tool_calls: list[dict[str, Any]],
        *,
        max_total_chars: int = 10000,
        max_output_chars: int = 600,
    ) -> str:
        """Build LLM-only context from persisted tool outputs without replaying tool_calls."""

        lines: list[str] = []
        total = 0
        for index, tool_call in enumerate(tool_calls, start=1):
            output = tool_call.get("output") or tool_call.get("raw_output") or ""
            if not output:
                continue
            tool = str(tool_call.get("tool") or tool_call.get("name") or "unknown_tool")
            tool_input = tool_call.get("input") or tool_call.get("args") or ""
            if isinstance(tool_input, (dict, list)):
                tool_input_text = json.dumps(tool_input, ensure_ascii=False)
            else:
                tool_input_text = str(tool_input)
            output_text = str(output)
            if len(output_text) > max_output_chars:
                output_text = f"{output_text[:max_output_chars]}... [truncated]"
            block = f"- 工具 {index}: {tool}\n  Input: {tool_input_text[:500]}\n  Output: {output_text}"
            if total + len(block) > max_total_chars:
                lines.append("- 其余历史工具结果因长度限制已省略。")
                break
            lines.append(block)
            total += len(block)
        if not lines:
            return ""
        return "\n\n[历史工具结果摘要，仅用于理解已完成事实，不代表本轮新工具调用]\n" + "\n".join(lines)

    @_session_write_locked
    def rename_session(self, session_id: str, title: str) -> None:
        """重命名会话标题"""
        data = self._read_file(session_id)  # 读取会话数据
        if not data:  # 会话不存在则报错
            raise FileNotFoundError(f"Session {session_id} not found")
        data["title"] = title  # 更新标题
        self._write_file(session_id, data)  # 写回磁盘

    @staticmethod
    def _todo_scope_key(
        *,
        goal_id: str | None = None,
        goal_revision: int | None = None,
        run_id: str | None = None,
    ) -> str:
        if goal_id:
            return f"goal:{goal_id}:revision:{int(goal_revision or 1)}"
        if run_id:
            return f"run:{run_id}"
        return "session:legacy"

    def get_todos(
        self,
        session_id: str,
        *,
        goal_id: str | None = None,
        goal_revision: int | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the Todo ledger for one Goal revision or standalone Run."""
        data = self._read_file(session_id)
        if not data:
            return []
        if goal_id or run_id:
            ledgers = data.get("todo_ledgers")
            scoped = (
                ledgers.get(
                    self._todo_scope_key(
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                        run_id=run_id,
                    )
                )
                if isinstance(ledgers, dict)
                else None
            )
            return deepcopy(scoped) if isinstance(scoped, list) else []
        todos = data.get("todos")
        return deepcopy(todos) if isinstance(todos, list) else []

    @classmethod
    def _todo_ledger_revision(cls, data: dict[str, Any], scope_key: str) -> int:
        metadata = data.get("todo_ledger_meta")
        raw = metadata.get(scope_key) if isinstance(metadata, dict) else None
        return int(raw.get("revision") or 0) if isinstance(raw, dict) else 0

    @classmethod
    def _latest_todo_operation(cls, data: dict[str, Any], scope_key: str) -> dict[str, Any]:
        metadata = data.get("todo_ledger_meta")
        raw = metadata.get(scope_key) if isinstance(metadata, dict) else None
        operations = raw.get("applied_operations") if isinstance(raw, dict) else None
        if not isinstance(operations, dict) or not operations:
            return {}
        operation_id = next(reversed(operations))
        receipt = operations.get(operation_id)
        return (
            {
                "operation_id": operation_id,
                "persisted_at": receipt.get("persisted_at"),
            }
            if isinstance(receipt, dict)
            else {}
        )

    def get_todo_snapshot(
        self,
        session_id: str,
        *,
        goal_id: str | None = None,
        goal_revision: int | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        data = self._read_file(session_id)
        if not data:
            return {"todos": [], "authority": {"kind": "none"}, "ledger_revision": 0}
        if goal_id or run_id:
            scope_key = self._todo_scope_key(
                goal_id=goal_id,
                goal_revision=goal_revision,
                run_id=run_id,
            )
            ledgers = data.get("todo_ledgers")
            scoped = ledgers.get(scope_key) if isinstance(ledgers, dict) else None
            authority = (
                {
                    "kind": "goal",
                    "goal_id": goal_id,
                    "goal_revision": int(goal_revision or 1),
                }
                if goal_id
                else {"kind": "run", "run_id": run_id}
            )
            return {
                "todos": deepcopy(scoped) if isinstance(scoped, list) else [],
                "authority": authority,
                "ledger_revision": self._todo_ledger_revision(data, scope_key),
                **self._latest_todo_operation(data, scope_key),
            }
        todos, authority = self._current_todo_projection(data)
        scope_key = self._todo_scope_key(
            goal_id=str(authority.get("goal_id") or "") or None,
            goal_revision=authority.get("goal_revision"),
            run_id=str(authority.get("run_id") or "") or None,
        )
        return {
            "todos": todos,
            "authority": authority,
            "ledger_revision": self._todo_ledger_revision(data, scope_key),
            **self._latest_todo_operation(data, scope_key),
        }

    @classmethod
    def _current_todo_projection(
        cls,
        data: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Project the authoritative current Todo ledger for the chat UI.

        The top-level ``todos`` field is retained as a legacy write-through
        cache, but it is not a lifecycle owner.  A completed Goal/Run must not
        remain visible as current work merely because it wrote that cache last.
        """

        harness = data.get("harness")
        if not isinstance(harness, dict):
            legacy = data.get("todos")
            return (
                deepcopy(legacy) if isinstance(legacy, list) else [],
                {"kind": "legacy"},
            )
        ledgers = data.get("todo_ledgers")
        ledgers = ledgers if isinstance(ledgers, dict) else {}
        goals = harness.get("goals")
        goals = goals if isinstance(goals, dict) else {}
        runs = harness.get("runs")
        runs = runs if isinstance(runs, dict) else {}
        latest_run_id = harness.get("latest_run_id")
        if not isinstance(latest_run_id, str):
            run_order = harness.get("run_order")
            if isinstance(run_order, list):
                latest_run_id = next(
                    (item for item in reversed(run_order) if isinstance(item, str)),
                    None,
                )
        latest_run = runs.get(latest_run_id) if isinstance(latest_run_id, str) else None
        terminal_statuses = {
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }
        if (
            isinstance(latest_run, dict)
            and str(latest_run.get("status") or "") not in terminal_statuses
            and str(latest_run.get("run_kind") or "") == "goal_inspection"
            and str(latest_run.get("context_goal_id") or "")
        ):
            context_goal_id = str(latest_run["context_goal_id"])
            context_revision = int(latest_run.get("context_goal_revision") or 1)
            scoped = ledgers.get(
                cls._todo_scope_key(
                    goal_id=context_goal_id,
                    goal_revision=context_revision,
                )
            )
            return (
                deepcopy(scoped) if isinstance(scoped, list) else [],
                {
                    "kind": "goal",
                    "goal_id": context_goal_id,
                    "goal_revision": context_revision,
                },
            )
        if (
            isinstance(latest_run, dict)
            and str(latest_run.get("status") or "") not in terminal_statuses
            and not str(latest_run.get("goal_id") or "")
        ):
            scoped = ledgers.get(cls._todo_scope_key(run_id=latest_run_id))
            return (
                deepcopy(scoped) if isinstance(scoped, list) else [],
                {"kind": "run", "run_id": latest_run_id},
            )

        active_goal_id = harness.get("active_goal_id")
        if isinstance(active_goal_id, str):
            goal = goals.get(active_goal_id)
            if isinstance(goal, dict) and goal.get("status") == "active":
                revision = int(goal.get("objective_revision") or 1)
                scoped = ledgers.get(
                    cls._todo_scope_key(
                        goal_id=active_goal_id,
                        goal_revision=revision,
                    )
                )
                return (
                    deepcopy(scoped) if isinstance(scoped, list) else [],
                    {
                        "kind": "goal",
                        "goal_id": active_goal_id,
                        "goal_revision": revision,
                    },
                )

        if isinstance(latest_run, dict) and str(latest_run.get("status") or "") not in terminal_statuses:
            scoped = ledgers.get(cls._todo_scope_key(run_id=latest_run_id))
            return (
                deepcopy(scoped) if isinstance(scoped, list) else [],
                {"kind": "run", "run_id": latest_run_id},
            )
        return [], {"kind": "none"}

    @_session_write_locked
    def update_todos(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
        *,
        goal_id: str | None = None,
        goal_revision: int | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Persist the current Todo ledger with explicit lifecycle ownership."""
        data = self._read_file(session_id)
        if not data:
            return []
        saved = deepcopy(todos)
        data["todos"] = saved
        if goal_id or run_id:
            ledgers = data.setdefault("todo_ledgers", {})
            scope_key = self._todo_scope_key(
                goal_id=goal_id,
                goal_revision=goal_revision,
                run_id=run_id,
            )
            prior = ledgers.get(scope_key)
            metadata = data.setdefault("todo_ledger_meta", {})
            meta = metadata.setdefault(scope_key, {"revision": 0, "applied_operations": {}})
            if (
                isinstance(prior, list)
                and prior != saved
                and isinstance(meta.get("applied_operations"), dict)
                and bool(meta["applied_operations"])
            ):
                raise ValueError("Todo ledger is transactional; list replacement cannot overwrite committed patches")
            ledgers[scope_key] = saved
            if not isinstance(prior, list) or prior != saved:
                meta["revision"] = int(meta.get("revision") or 0) + 1
                meta["updated_at"] = time.time()
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def apply_todo_patch(
        self,
        session_id: str,
        *,
        goal_id: str | None = None,
        goal_revision: int | None = None,
        run_id: str | None = None,
        operation_id: str,
        expected_revision: int | None = None,
        mutator: Callable[
            [list[dict[str, Any]]],
            tuple[list[dict[str, Any]], list[dict[str, Any]]],
        ],
    ) -> dict[str, Any]:
        """Atomically apply one idempotent Todo operation batch."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        if goal_id:
            harness = data.get("harness")
            goals = harness.get("goals") if isinstance(harness, dict) else None
            goal = goals.get(goal_id) if isinstance(goals, dict) else None
            if isinstance(goal, dict):
                current_goal_revision = int(goal.get("objective_revision") or 1)
                requested_goal_revision = int(goal_revision or 1)
                if requested_goal_revision != current_goal_revision:
                    raise ValueError(
                        "Todo Goal revision conflict: "
                        f"requested {requested_goal_revision}, current {current_goal_revision}"
                    )
        scope_key = self._todo_scope_key(
            goal_id=goal_id,
            goal_revision=goal_revision,
            run_id=run_id,
        )
        ledgers = data.setdefault("todo_ledgers", {})
        current = ledgers.get(scope_key)
        current = deepcopy(current) if isinstance(current, list) else []
        metadata = data.setdefault("todo_ledger_meta", {})
        meta = metadata.setdefault(scope_key, {"revision": 0, "applied_operations": {}})
        revision = int(meta.get("revision") or 0)
        applied_operations = meta.setdefault("applied_operations", {})
        existing_receipt = applied_operations.get(operation_id) if isinstance(applied_operations, dict) else None
        authority = (
            {
                "kind": "goal",
                "goal_id": goal_id,
                "goal_revision": int(goal_revision or 1),
            }
            if goal_id
            else {"kind": "run", "run_id": run_id}
        )
        if isinstance(existing_receipt, dict):
            emit_harness_metric(
                logger,
                "todo_patch_idempotent_replay_total",
                session_id=session_id,
                scope_key=scope_key,
                operation_id=operation_id,
                ledger_revision=revision,
            )
            return {
                "todos": current,
                "applied": deepcopy(existing_receipt.get("applied") or []),
                "authority": authority,
                "ledger_revision": revision,
                "operation_id": operation_id,
                "replayed": True,
                "persisted_at": existing_receipt.get("persisted_at"),
            }
        if expected_revision is not None and expected_revision != revision:
            emit_harness_metric(
                logger,
                "todo_ledger_revision_conflict_total",
                session_id=session_id,
                scope_key=scope_key,
                operation_id=operation_id,
                expected_revision=expected_revision,
                ledger_revision=revision,
            )
            raise ValueError(f"Todo ledger revision conflict: expected {expected_revision}, current {revision}")
        saved, applied = mutator(current)
        saved = deepcopy(saved)
        next_revision = revision + 1
        persisted_at = time.time()
        ledgers[scope_key] = saved
        data["todos"] = deepcopy(saved)
        receipt = {
            "revision": next_revision,
            "persisted_at": persisted_at,
            "applied": deepcopy(applied),
        }
        if not isinstance(applied_operations, dict):
            applied_operations = {}
            meta["applied_operations"] = applied_operations
        applied_operations[operation_id] = receipt
        # Bound durable idempotency metadata while retaining recent retries.
        if len(applied_operations) > 500:
            for stale_id in list(applied_operations)[: len(applied_operations) - 500]:
                applied_operations.pop(stale_id, None)
        meta["revision"] = next_revision
        meta["updated_at"] = persisted_at
        self._write_file(session_id, data)
        emit_harness_metric(
            logger,
            "todo_patch_committed_total",
            session_id=session_id,
            scope_key=scope_key,
            operation_id=operation_id,
            ledger_revision=next_revision,
            operation_count=len(applied),
        )
        return {
            "todos": deepcopy(saved),
            "applied": deepcopy(applied),
            "authority": authority,
            "ledger_revision": next_revision,
            "operation_id": operation_id,
            "replayed": False,
            "persisted_at": persisted_at,
        }

    @_session_write_locked
    def inherit_unfinished_todos_for_run(
        self,
        session_id: str,
        run_id: str,
        *,
        continuation_requested: bool = False,
    ) -> list[dict[str, Any]]:
        """Carry the latest unfinished standalone ledger into a new Run."""

        data = self._read_file(session_id)
        if not data:
            return []
        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        current = runs.get(run_id) if isinstance(runs, dict) else None
        if not continuation_requested or not isinstance(current, dict) or current.get("goal_id"):
            return []
        ledgers = data.setdefault("todo_ledgers", {})
        current_key = self._todo_scope_key(run_id=run_id)
        existing = ledgers.get(current_key)
        if isinstance(existing, list) and existing:
            return deepcopy(existing)
        run_order = harness.get("run_order") if isinstance(harness, dict) else None
        prior_ids = list(run_order or [])
        if run_id in prior_ids:
            prior_ids = prior_ids[: prior_ids.index(run_id)]
        for prior_run_id in reversed(prior_ids):
            prior_run = runs.get(prior_run_id) if isinstance(runs, dict) else None
            if not isinstance(prior_run, dict) or prior_run.get("goal_id"):
                continue
            prior = ledgers.get(self._todo_scope_key(run_id=str(prior_run_id)))
            if not isinstance(prior, list) or not any(
                isinstance(item, dict) and item.get("status") in {"pending", "in_progress"} for item in prior
            ):
                continue
            inherited = deepcopy(prior)
            ledgers[current_key] = inherited
            data["todos"] = deepcopy(inherited)
            self._write_file(session_id, data)
            return inherited
        return []

    def get_harness_state(self, session_id: str) -> dict[str, Any]:
        """Return the product-level Run/Goal state stored in Session JSON."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        if not isinstance(harness, dict):
            return {"runs": {}, "run_order": [], "goals": {}, "goal_order": []}
        result = deepcopy(harness)
        result.setdefault("runs", {})
        result.setdefault("run_order", [])
        result.setdefault("goals", {})
        result.setdefault("goal_order", [])
        return result

    def get_run_state(
        self,
        session_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return one persisted Run, defaulting to the latest Run."""

        harness = self.get_harness_state(session_id)
        effective_id = run_id or harness.get("latest_run_id")
        runs = harness.get("runs")
        if not isinstance(effective_id, str) or not isinstance(runs, dict):
            return None
        run = runs.get(effective_id)
        return deepcopy(run) if isinstance(run, dict) else None

    @_session_write_locked
    def reserve_delta_repair_tool_call(
        self,
        session_id: str,
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any]:
        """Atomically reserve one Tool call against a bounded delta-repair Run."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(run, dict):
            return {"applies": False, "allowed": False, "reason": "run_not_found"}
        if str(run.get("execution_mode") or "native") != "delta_repair":
            return {"applies": False, "allowed": True}
        call_id = str(tool_call_id or "").strip()
        if not call_id:
            return {"applies": True, "allowed": False, "reason": "missing_tool_call_id"}
        reserved = [str(item) for item in run.get("delta_repair_tool_call_ids") or []]
        limit = int(run.get("delta_repair_tool_budget") or 12)
        if call_id in reserved:
            return {
                "applies": True,
                "allowed": True,
                "count": len(reserved),
                "limit": limit,
            }
        if len(reserved) >= limit:
            emit_harness_metric(
                logger,
                "delta_repair_tool_calls",
                session_id=session_id,
                value=len(reserved),
                status="budget_exhausted",
            )
            return {
                "applies": True,
                "allowed": False,
                "reason": "delta_repair_tool_budget_exhausted",
                "count": len(reserved),
                "limit": limit,
            }
        reserved.append(call_id)
        run["delta_repair_tool_call_ids"] = reserved
        self._write_file(session_id, data)
        emit_harness_metric(
            logger,
            "delta_repair_tool_calls",
            session_id=session_id,
            value=len(reserved),
            status="reserved",
        )
        return {
            "applies": True,
            "allowed": True,
            "count": len(reserved),
            "limit": limit,
        }

    @_session_write_locked
    def record_sql_generation(
        self,
        session_id: str,
        generation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a server-authored SQL generation for Run/Goal recovery."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        ledger = harness.setdefault("sql_generation_ledger", {})
        existing = ledger.get(generation_id)
        if isinstance(existing, dict) and existing != payload:
            raise ValueError(f"SQL generation {generation_id} is immutable")
        ledger[generation_id] = deepcopy(payload)
        self._write_file(session_id, data)
        return deepcopy(ledger[generation_id])

    def get_sql_generation(
        self,
        session_id: str,
        generation_id: str,
    ) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        ledger = harness.get("sql_generation_ledger") if isinstance(harness, dict) else None
        item = ledger.get(generation_id) if isinstance(ledger, dict) else None
        return deepcopy(item) if isinstance(item, dict) else None

    def list_sql_generations(self, session_id: str) -> list[dict[str, Any]]:
        """List immutable generation ledger records for server-side derivation."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        ledger = harness.get("sql_generation_ledger") if isinstance(harness, dict) else None
        if not isinstance(ledger, dict):
            return []
        return [deepcopy(item) for item in ledger.values() if isinstance(item, dict)]

    @_session_write_locked
    def record_sql_submission(
        self,
        session_id: str,
        submission_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an Agent-authored SQL submission as an immutable ledger entry."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        ledger = harness.setdefault("sql_submission_ledger", {})
        existing = ledger.get(submission_id)
        if isinstance(existing, dict) and existing != payload:
            raise ValueError(f"SQL submission {submission_id} is immutable")
        ledger[submission_id] = deepcopy(payload)
        self._write_file(session_id, data)
        return deepcopy(ledger[submission_id])

    def get_sql_submission(self, session_id: str, submission_id: str) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        ledger = harness.get("sql_submission_ledger") if isinstance(harness, dict) else None
        item = ledger.get(submission_id) if isinstance(ledger, dict) else None
        return deepcopy(item) if isinstance(item, dict) else None

    @_session_write_locked
    def record_database_evidence(
        self,
        session_id: str,
        evidence_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an immutable Agent database-evidence envelope."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        ledger = harness.setdefault("database_evidence_ledger", {})
        existing = ledger.get(evidence_id)
        if isinstance(existing, dict) and existing != payload:
            raise ValueError(f"Database evidence {evidence_id} is immutable")
        ledger[evidence_id] = deepcopy(payload)
        self._write_file(session_id, data)
        return deepcopy(ledger[evidence_id])

    def get_database_evidence(self, session_id: str, evidence_id: str) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        ledger = harness.get("database_evidence_ledger") if isinstance(harness, dict) else None
        item = ledger.get(evidence_id) if isinstance(ledger, dict) else None
        return deepcopy(item) if isinstance(item, dict) else None

    @_session_write_locked
    def record_database_schema_evidence(
        self,
        session_id: str,
        receipt_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a schema/profile Receipt for process-restart recovery."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        ledger = harness.setdefault("database_schema_evidence_ledger", {})
        existing = ledger.get(receipt_id)
        if isinstance(existing, dict) and existing != payload:
            raise ValueError(f"Database schema evidence {receipt_id} is immutable")
        ledger[receipt_id] = deepcopy(payload)
        self._write_file(session_id, data)
        return deepcopy(ledger[receipt_id])

    def get_database_schema_evidence(self, session_id: str, receipt_id: str) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        ledger = harness.get("database_schema_evidence_ledger") if isinstance(harness, dict) else None
        item = ledger.get(receipt_id) if isinstance(ledger, dict) else None
        return deepcopy(item) if isinstance(item, dict) else None

    @_session_write_locked
    def record_database_path_event(
        self,
        session_id: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one immutable Agent/legacy database path transition."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        ledger = harness.setdefault("database_path_events", {})
        existing = ledger.get(event_id)
        if isinstance(existing, dict) and existing != payload:
            raise ValueError(f"Database path event {event_id} is immutable")
        ledger[event_id] = deepcopy(payload)
        self._write_file(session_id, data)
        return deepcopy(ledger[event_id])

    def list_database_path_events(
        self,
        session_id: str,
        *,
        query_id: str = "",
        run_id: str = "",
        goal_id: str = "",
        goal_revision: int | None = None,
    ) -> list[dict[str, Any]]:
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        ledger = harness.get("database_path_events") if isinstance(harness, dict) else None
        if not isinstance(ledger, dict):
            return []
        return sorted(
            (
                deepcopy(item)
                for item in ledger.values()
                if isinstance(item, dict)
                and (not query_id or str(item.get("query_id") or "") == str(query_id))
                and (not run_id or str(item.get("run_id") or "") == str(run_id))
                and (not goal_id or str(item.get("goal_id") or "") == str(goal_id))
                and (goal_revision is None or item.get("goal_revision") == goal_revision)
            ),
            key=lambda item: float(item.get("created_at") or 0),
        )

    @_session_write_locked
    def record_sql_validation_receipt(
        self,
        session_id: str,
        receipt_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an immutable SQL validator receipt bound to one SQL hash."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        receipts = harness.setdefault("sql_validation_receipts", {})
        existing = receipts.get(receipt_id)
        if isinstance(existing, dict) and existing != payload:
            raise ValueError(f"SQL validation receipt {receipt_id} is immutable")
        receipts[receipt_id] = deepcopy(payload)
        self._write_file(session_id, data)
        return deepcopy(receipts[receipt_id])

    def get_sql_validation_receipt(
        self,
        session_id: str,
        receipt_id: str,
    ) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        receipts = harness.get("sql_validation_receipts") if isinstance(harness, dict) else None
        item = receipts.get(receipt_id) if isinstance(receipts, dict) else None
        return deepcopy(item) if isinstance(item, dict) else None

    def list_sql_validation_receipts(self, session_id: str) -> list[dict[str, Any]]:
        """List immutable validation receipts for server-side plan reuse."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        receipts = harness.get("sql_validation_receipts") if isinstance(harness, dict) else None
        if not isinstance(receipts, dict):
            return []
        return [deepcopy(item) for item in receipts.values() if isinstance(item, dict)]

    @_session_write_locked
    def record_sql_execution_attestation(
        self,
        session_id: str,
        validation_receipt_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist successful execution of one immutable validation receipt."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        attestations = harness.setdefault("sql_execution_attestations", {})
        existing = attestations.get(validation_receipt_id)
        if isinstance(existing, dict) and (
            existing.get("generation_id") != payload.get("generation_id")
            or existing.get("sql_sha256") != payload.get("sql_sha256")
        ):
            raise ValueError(f"SQL execution attestation {validation_receipt_id} is immutable")
        if isinstance(existing, dict):
            return deepcopy(existing)
        attestations[validation_receipt_id] = deepcopy(payload)
        self._write_file(session_id, data)
        return deepcopy(payload)

    def list_sql_execution_attestations(self, session_id: str) -> list[dict[str, Any]]:
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        attestations = harness.get("sql_execution_attestations") if isinstance(harness, dict) else None
        if not isinstance(attestations, dict):
            return []
        return [deepcopy(item) for item in attestations.values() if isinstance(item, dict)]

    @_session_write_locked
    def upsert_run_state(
        self,
        session_id: str,
        run: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one Run while preserving the first committed terminal state."""

        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        persisted_session_id = str(run.get("session_id") or "").strip()
        if persisted_session_id != session_id:
            raise ValueError("Run session_id does not match persistence session")

        from harness.models import RunRecord, RunStatus

        incoming = RunRecord.model_validate(run)
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        runs = harness.setdefault("runs", {})
        existing = runs.get(run_id)
        if not isinstance(existing, dict) and incoming.status != RunStatus.PREPARING:
            raise ValueError("A new Run must start in preparing state")
        terminal_statuses = {
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }
        if isinstance(existing, dict) and existing.get("status") in terminal_statuses:
            if existing != run:
                raise ValueError(
                    f"Run {run_id} already has terminal outcome {existing.get('outcome') or existing.get('status')}"
                )
        if isinstance(existing, dict):
            current = RunRecord.model_validate(existing)
            if current.status != incoming.status:
                current.transition(incoming.status)

        saved = deepcopy(run)
        if isinstance(existing, dict):
            for immutable_field in (
                "run_id",
                "query_id",
                "session_id",
                "objective",
                "goal_id",
                "project_id",
                "analytics_model_id",
                "verification_enabled",
                "task_profile",
                "declared_verification_contract",
                "config_snapshot",
                "created_at",
            ):
                if immutable_field in existing:
                    saved[immutable_field] = deepcopy(existing[immutable_field])
            existing_activations = existing.get("verification_activations")
            incoming_activations = saved.get("verification_activations")
            merged_activations: list[dict[str, Any]] = []
            seen_activation_ids: set[str] = set()
            for raw in (existing_activations if isinstance(existing_activations, list) else []) + (
                incoming_activations if isinstance(incoming_activations, list) else []
            ):
                if not isinstance(raw, dict):
                    continue
                activation_id = str(raw.get("activation_id") or "")
                if activation_id and activation_id in seen_activation_ids:
                    continue
                if activation_id:
                    seen_activation_ids.add(activation_id)
                merged_activations.append(deepcopy(raw))
            for activation in merged_activations:
                if activation.get("status") != "succeeded":
                    continue
                activation["stable_evidence_refs"] = register_activation_evidence(
                    data,
                    run=saved,
                    activation=activation,
                )
            saved["verification_activations"] = merged_activations

            for list_field, identity_field in (
                ("delegation_contracts", "subagent_run_id"),
                ("delegation_results", "subagent_run_id"),
                ("delegation_events", "event_id"),
            ):
                merged: dict[str, dict[str, Any]] = {}
                existing_items = existing.get(list_field)
                incoming_items = saved.get(list_field)
                for raw in [
                    *(existing_items if isinstance(existing_items, list) else []),
                    *(incoming_items if isinstance(incoming_items, list) else []),
                ]:
                    if isinstance(raw, dict) and raw.get(identity_field):
                        merged[str(raw[identity_field])] = deepcopy(raw)
                saved[list_field] = list(merged.values())

            existing_contract = existing.get("verification_contract")
            if isinstance(existing_contract, dict):
                saved["verification_contract"] = deepcopy(existing_contract)
        runs[run_id] = saved
        run_order = harness.setdefault("run_order", [])
        if run_id not in run_order:
            run_order.append(run_id)
        harness["latest_run_id"] = run_id
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def transition_run_status(
        self,
        session_id: str,
        run_id: str,
        status: str,
        *,
        expected_statuses: set[str] | None = None,
    ) -> dict[str, Any]:
        """Atomically apply one legal, idempotent Run status transition."""

        from harness.models import RunRecord, RunStatus

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        if expected_statuses is not None and run.status.value not in expected_statuses:
            raise ValueError(f"Run {run_id} is {run.status}; expected one of {sorted(expected_statuses)}")
        run.transition(RunStatus(status))
        saved = run.model_dump(mode="json")
        runs[run_id] = saved
        harness["latest_run_id"] = run_id
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def resume_run_from_hitl(
        self,
        session_id: str,
        run_id: str,
        *,
        goal_id: str = "",
        goal_revision: int | None = None,
    ) -> dict[str, Any]:
        """Atomically validate Goal control state and resume one waiting Run."""

        from harness.models import RunRecord, RunStatus

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        if run.status != RunStatus.WAITING_HITL:
            raise ValueError(f"Run {run_id} is not waiting for HITL")
        if goal_id:
            goals = harness.get("goals") if isinstance(harness, dict) else None
            goal = goals.get(goal_id) if isinstance(goals, dict) else None
            if (
                not isinstance(goal, dict)
                or str(goal.get("status") or "") != "active"
                or bool(goal.get("requested_status"))
                or str(goal.get("current_run_id") or "") != run_id
                or int(goal.get("objective_revision") or 1)
                != int(goal_revision or 1)
            ):
                raise ValueError("Goal control changed while waiting for user input")
        run.transition(RunStatus.RUNNING)
        saved = run.model_dump(mode="json")
        runs[run_id] = saved
        harness["latest_run_id"] = run_id
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def append_run_verification_activation(
        self,
        session_id: str,
        run_id: str,
        activation: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Append one idempotent current-Run verification activation."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        if str(activation.get("run_id") or "") != run_id:
            raise ValueError("Verification activation run_id mismatch")
        if str(activation.get("query_id") or "") != str(run.get("query_id") or ""):
            raise ValueError("Verification activation query_id mismatch")
        if str(activation.get("status") or "") != "succeeded":
            raise ValueError("Only successful Tool results may activate verification packs")
        if str(run.get("status") or "") in {
            "evaluating",
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }:
            raise ValueError("Evaluating or terminal Runs cannot accept verification activations")
        activations = run.get("verification_activations")
        if not isinstance(activations, list):
            activations = []
        activation_id = str(activation.get("activation_id") or "")
        if not activation_id:
            raise ValueError("Verification activation_id is required")
        for existing in activations:
            if isinstance(existing, dict) and existing.get("activation_id") == activation_id:
                return deepcopy(existing), False
        saved = deepcopy(activation)
        saved["stable_evidence_refs"] = register_activation_evidence(
            data,
            run=run,
            activation=saved,
        )
        activations.append(saved)
        run["verification_activations"] = activations
        run["updated_at"] = time.time()
        self._write_file(session_id, data)
        return deepcopy(saved), True

    def get_evidence_record(
        self,
        session_id: str,
        evidence_type: str,
        evidence_id: str,
    ) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        ledger = data.get("evidence_ledger") if data else None
        item = (
            ledger.get(ref_key(EvidenceRef(type=evidence_type, id=evidence_id))) if isinstance(ledger, dict) else None
        )
        return deepcopy(item) if isinstance(item, dict) else None

    def list_evidence_records(self, session_id: str) -> list[dict[str, Any]]:
        data = self._read_file(session_id)
        ledger = data.get("evidence_ledger") if data else None
        return [
            deepcopy(item) for item in (ledger.values() if isinstance(ledger, dict) else []) if isinstance(item, dict)
        ]

    def resolve_evidence_ref(
        self,
        session_id: str,
        evidence_ref: dict[str, Any],
        *,
        goal_id: str | None = None,
        goal_revision: int | None = None,
        require_inheritable: bool = True,
        allow_artifact_revision_inheritance: bool = False,
    ) -> dict[str, Any] | None:
        self.repair_legacy_evidence_ledger(session_id)
        data = self._read_file(session_id)
        resolved = resolve_evidence_ref(
            data,
            evidence_ref,
            goal_id=goal_id,
            goal_revision=goal_revision,
            require_inheritable=require_inheritable,
            allow_artifact_revision_inheritance=allow_artifact_revision_inheritance,
        )
        return resolved.model_dump(mode="json") if resolved is not None else None

    @_session_write_locked
    def repair_legacy_evidence_ledger(self, session_id: str) -> bool:
        """Run deterministic evidence migrations under the Session write lock."""

        data = self._read_file(session_id)
        changed = repair_legacy_validation_wrapper_records(data)
        changed = repair_legacy_tool_execution_records(data) or changed
        if changed:
            self._write_file(session_id, data)
        return changed

    @_session_write_locked
    def migrate_run_declared_artifact_targets(
        self,
        session_id: str,
        run_id: str,
        targets: list[str],
    ) -> list[str]:
        """Persist one v2 target-resolution migration; never re-read templates."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(run, dict):
            return []
        if int(run.get("declared_artifact_targets_version") or 1) >= 2:
            return list(run.get("declared_artifact_targets") or [])
        run["declared_artifact_targets"] = list(dict.fromkeys(targets))
        run["declared_artifact_targets_version"] = 2
        run["updated_at"] = time.time()
        self._write_file(session_id, data)
        return list(run["declared_artifact_targets"])

    @staticmethod
    def _artifact_bound_goal_evidence_refs(
        data: dict[str, Any],
        goal_id: str,
    ) -> list[dict[str, str]]:
        """Select immutable artifact-bound evidence; never carry analytics conclusions."""

        ledger = data.get("evidence_ledger")
        records = ledger.values() if isinstance(ledger, dict) else []
        refs: dict[str, dict[str, str]] = {}
        for raw in records:
            if not isinstance(raw, dict):
                continue
            if (
                raw.get("goal_id") != goal_id
                or raw.get("status") != "active"
                or raw.get("inheritable") is not True
                or raw.get("kind") not in {"artifact", "validation_receipt", "external_mutation"}
            ):
                continue
            ref = {"type": str(raw["kind"]), "id": str(raw["id"])}
            refs[ref_key(ref)] = ref
        return list(refs.values())

    @_session_write_locked
    def restore_goal_artifact_evidence(
        self,
        session_id: str,
        goal_id: str,
    ) -> list[dict[str, str]]:
        """Restore hash-bound artifact refs after a same-Goal objective revision."""

        data = self._read_file(session_id)
        repair_legacy_validation_wrapper_records(data)
        harness = data.get("harness") if data else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        goal = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(goal, dict):
            return []
        restored = self._artifact_bound_goal_evidence_refs(data, goal_id)
        existing = [dict(item) for item in goal.get("evidence_refs") or [] if is_evidence_ref(item)]
        merged = {ref_key(item): item for item in [*existing, *restored]}
        goal["evidence_refs"] = list(merged.values())
        goal["updated_at"] = time.time()
        self._write_file(session_id, data)
        return deepcopy(restored)

    def resolve_goal_evidence_records(
        self,
        session_id: str,
        goal_id: str,
        goal_revision: int,
    ) -> list[dict[str, Any]]:
        self.repair_legacy_evidence_ledger(session_id)
        goal = self.get_goal_state(session_id, goal_id)
        refs = goal.get("evidence_refs") if isinstance(goal, dict) else []
        resolved: list[dict[str, Any]] = []
        for evidence_ref in refs if isinstance(refs, list) else []:
            if not is_evidence_ref(evidence_ref):
                continue
            record = self.resolve_evidence_ref(
                session_id,
                evidence_ref,
                goal_id=goal_id,
                goal_revision=goal_revision,
                allow_artifact_revision_inheritance=True,
            )
            if record is not None:
                resolved.append(record)
        return resolved

    @_session_write_locked
    def backfill_goal_declared_artifact_writes(
        self,
        session_id: str,
        goal_id: str,
        goal_revision: int,
    ) -> list[dict[str, Any]]:
        """Recover hash-bound Artifact evidence from legacy successful writes.

        Old ``write_file`` executions could durably store the successful Tool
        call while missing the later HostFileBroker/VerificationActivation
        flush. Backfill is allowed only when the original Run declared the
        exact target and the current bytes equal the exact UTF-8 ``content``
        passed to that successful Tool call.
        """

        from harness.models import (
            ArtifactReference,
            ArtifactRole,
            ArtifactScope,
        )

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        goal = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(goal, dict) or not isinstance(runs, dict):
            return []
        if int(goal.get("objective_revision") or 1) != int(goal_revision):
            return []

        run_by_query = {
            str(run.get("query_id") or ""): run
            for run in runs.values()
            if isinstance(run, dict) and run.get("query_id")
        }
        ledger = data.setdefault("evidence_ledger", {})
        goal_refs = [dict(item) for item in goal.get("evidence_refs") or [] if is_evidence_ref(item)]
        known_goal_ref_keys = {ref_key(item) for item in goal_refs}
        backfilled: list[dict[str, Any]] = []
        data_changed = False

        for _message_index, _tool_index, message, tool_call in self._iter_persisted_tool_calls(data):
            tool_name = str(tool_call.get("tool") or tool_call.get("name") or "")
            if tool_name != "write_file":
                continue
            status, output_complete = self._evidence_status(tool_call, message)
            if status != "success" or not output_complete:
                continue
            query_id = str(message.get("query_id") or "")
            run_id = str(tool_call.get("source_run_id") or "")
            run = runs.get(run_id) if run_id else run_by_query.get(query_id)
            if not isinstance(run, dict):
                continue
            run_id = str(run.get("run_id") or "")
            if str(run.get("goal_id") or "") != goal_id or int(run.get("goal_revision") or 1) != int(goal_revision):
                continue
            raw_input = tool_call.get("input", tool_call.get("args"))
            if isinstance(raw_input, str):
                try:
                    args = json.loads(raw_input)
                except json.JSONDecodeError:
                    continue
            elif isinstance(raw_input, dict):
                args = raw_input
            else:
                continue
            raw_path = str(args.get("file_path") or args.get("path") or "").strip()
            content = args.get("content")
            if not raw_path or not isinstance(content, str):
                continue
            raw_target = Path(raw_path).expanduser()
            if raw_target.is_symlink():
                # A retroactive migration has no trustworthy record of the
                # symlink target at original write time. Fail closed instead
                # of authorizing whichever inode it happens to reference now.
                continue
            try:
                target = raw_target.resolve(strict=True)
            except OSError:
                continue
            if not target.is_file():
                continue
            declared_targets = [str(item) for item in run.get("declared_artifact_targets") or [] if str(item)]
            if not any(Path(item).expanduser().resolve() == target for item in declared_targets):
                continue
            expected_bytes = content.encode("utf-8")
            expected_sha256 = "sha256:" + hashlib.sha256(expected_bytes).hexdigest()
            try:
                if target.read_bytes() != expected_bytes:
                    continue
            except OSError:
                continue

            execution = (
                run.get("config_snapshot", {}).get("execution", {})
                if isinstance(run.get("config_snapshot"), dict)
                else {}
            )
            workspace_id = str(execution.get("workspace_id") or "")
            artifact_id = (
                "artifact-"
                + hashlib.sha256((f"external\0{workspace_id}\0{target}\0{expected_sha256}").encode()).hexdigest()[:20]
            )
            ledger_key = f"artifact:{artifact_id}"
            existing = ledger.get(ledger_key)
            if isinstance(existing, dict):
                existing_payload = existing.get("payload")
                if (
                    isinstance(existing_payload, dict)
                    and str(existing_payload.get("content_sha256") or "") == expected_sha256
                ):
                    stable = {"type": "artifact", "id": artifact_id}
                    if ref_key(stable) not in known_goal_ref_keys:
                        goal_refs.append(stable)
                        known_goal_ref_keys.add(ref_key(stable))
                        data_changed = True
                    continue
                # Evidence identities are immutable. Never overwrite an older
                # version under the same artifact id during migration.
                continue

            tool_call_id = str(tool_call.get("id") or "")
            if not tool_call_id:
                continue
            output = str(tool_call.get("raw_output", tool_call.get("output", "")) or "")
            output_digest = str(tool_call.get("source_hash") or "")
            if not output_digest.startswith("sha256:"):
                output_digest = "sha256:" + hashlib.sha256(output.encode()).hexdigest()
            stat = target.stat()
            artifact = ArtifactReference(
                artifact_id=artifact_id,
                scope=ArtifactScope.EXTERNAL,
                role=ArtifactRole.TARGET,
                path=str(target),
                host_path=str(target),
                authorized=True,
                permission_grant_id=f"declared-artifact:{run_id}",
                mutation_receipt_id=(f"legacy-write-backfill:{tool_call_id}"),
                authority_kind="legacy_declared_artifact_backfill",
                run_id=run_id,
                query_id=str(run.get("query_id") or query_id) or None,
                goal_id=goal_id,
                goal_revision=goal_revision,
                backend_id=str(execution.get("backend_id") or "") or None,
                workspace_id=workspace_id or None,
                tool_call_id=tool_call_id,
                output_digest=output_digest,
                content_sha256=expected_sha256,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                written_at=float(tool_call.get("completed_at") or run.get("updated_at") or time.time()),
            )
            activation_id = (
                "verification-activation-legacy-write-"
                + hashlib.sha256(f"{run_id}:{tool_call_id}:{expected_sha256}".encode()).hexdigest()[:20]
            )
            activation = {
                "activation_id": activation_id,
                "run_id": run_id,
                "query_id": str(run.get("query_id") or query_id),
                "tool_call_id": tool_call_id,
                "tool_name": "write_file",
                "pack": "artifact",
                "source": "legacy_declared_artifact_backfill",
                "status": "succeeded",
                "evidence_refs": [
                    {
                        "kind": "artifact_write",
                        **artifact.model_dump(mode="json"),
                        "material": True,
                    }
                ],
                "created_at": float(tool_call.get("completed_at") or run.get("updated_at") or time.time()),
            }
            stable_refs = register_activation_evidence(
                data,
                run=run,
                activation=activation,
            )
            activation["stable_evidence_refs"] = stable_refs
            activations = run.setdefault("verification_activations", [])
            if not any(isinstance(item, dict) and item.get("activation_id") == activation_id for item in activations):
                activations.append(activation)
            for stable in stable_refs:
                key = ref_key(stable)
                if key not in known_goal_ref_keys:
                    goal_refs.append(dict(stable))
                    known_goal_ref_keys.add(key)
                    data_changed = True
            backfilled.append(artifact.model_dump(mode="json"))
            data_changed = True

        if data_changed:
            goal["evidence_refs"] = goal_refs
            goal["updated_at"] = time.time()
            self._write_file(session_id, data)
        return deepcopy(backfilled)

    @_session_write_locked
    def migrate_goal_evidence_refs(
        self,
        session_id: str,
        goal_id: str,
    ) -> list[dict[str, str]]:
        """One-way migrate legacy Goal evidence payloads into stable refs."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        goal = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(goal, dict):
            raise ValueError(f"Goal {goal_id} does not exist in session {session_id}")
        refs = migrate_legacy_refs(
            data,
            list(goal.get("evidence_refs") or []),
            goal_id=goal_id,
            goal_revision=int(goal.get("objective_revision") or 1),
        )
        goal["evidence_refs"] = refs
        goal["updated_at"] = time.time()
        self._write_file(session_id, data)
        return deepcopy(refs)

    @_session_write_locked
    def prepare_run_evaluation(
        self,
        session_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Freeze the current Tool ledger and atomically enter evaluation."""

        from harness.models import RunRecord, RunStatus
        from harness.rubric_compiler import RunRubricCompiler

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        if run.status == RunStatus.EVALUATING:
            return deepcopy(raw_run)
        if run.terminal:
            raise ValueError(f"Terminal Run {run_id} cannot enter evaluation")
        if run.verification_enabled:
            run.verification_contract = RunRubricCompiler.expand_for_activations(
                contract=run.declared_verification_contract,
                profile=run.task_profile,
                message=run.objective,
                activations=list(run.verification_activations),
            )
        run.transition(RunStatus.EVALUATING)
        saved = run.model_dump(mode="json")
        runs[run_id] = saved
        harness["latest_run_id"] = run_id
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def terminalize_run_state(
        self,
        session_id: str,
        run_id: str,
        terminal_run: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit a terminal Run without allowing stale callers to replace authority."""

        from harness.models import RunRecord
        from harness.rubric_compiler import RunRubricCompiler

        data = self._read_file(session_id)
        repair_legacy_validation_wrapper_records(data)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_current = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_current, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        incoming = RunRecord.model_validate(terminal_run)
        if incoming.run_id != run_id or incoming.session_id != session_id:
            raise ValueError("Terminal Run identity does not match persistence session")
        if incoming.outcome is None:
            raise ValueError("Terminal Run outcome is required")

        current = RunRecord.model_validate(raw_current)
        if current.terminal:
            if current.outcome != incoming.outcome:
                raise ValueError(f"Run {run_id} already has terminal outcome {current.outcome}")
            goal_is_terminal = False
            if current.goal_id:
                goals = harness.get("goals") if isinstance(harness, dict) else None
                raw_goal = goals.get(current.goal_id) if isinstance(goals, dict) else None
                goal_is_terminal = isinstance(raw_goal, dict) and str(raw_goal.get("status") or "") in {
                    "completed",
                    "cancelled",
                    "budget_exceeded",
                }
            leases_changed = False
            search_leases_changed = self._abandon_terminal_run_search_snapshots(
                data,
                current,
            )
            if not current.goal_id or goal_is_terminal:
                leases_changed = self._abandon_uncommitted_execution_leases(data, current)
            existing_handoff = current.handoff_summary
            existing_refs = (
                [
                    *existing_handoff.evidence_refs,
                    *existing_handoff.artifact_refs,
                ]
                if existing_handoff is not None
                else []
            )
            handoff_is_safe = existing_handoff is not None and all(
                self._is_safe_handoff_evidence(data, item) for item in existing_refs if isinstance(item, dict)
            )
            if leases_changed or search_leases_changed or not handoff_is_safe:
                current.handoff_summary = self._build_run_handoff(data, current)
            self._migrate_missing_tool_call_ids(session_id, data)
            self._ensure_evidence_metadata(session_id, data)
            saved_terminal = current.model_dump(mode="json")
            runs[run_id] = saved_terminal
            self._write_file(session_id, data)
            return deepcopy(saved_terminal)

        if current.verification_enabled:
            current.verification_contract = RunRubricCompiler.expand_for_activations(
                contract=current.declared_verification_contract,
                profile=current.task_profile,
                message=current.objective,
                activations=list(current.verification_activations),
            )
        current.verification_report = incoming.verification_report
        current.model_call_count = incoming.model_call_count
        current.budget_exhaustion_reason = incoming.budget_exhaustion_reason
        current.finish(incoming.outcome, error=incoming.error)
        if current.execution_mode == "delta_repair" and current.completed_at is not None:
            emit_harness_metric(
                logger,
                "delta_repair_elapsed_ms",
                session_id=session_id,
                value=round(
                    max(0.0, current.completed_at - current.created_at) * 1000,
                    2,
                ),
                outcome=current.outcome.value if current.outcome is not None else "",
            )
        self._abandon_terminal_run_search_snapshots(data, current)
        # A standalone Run has no later execution scope that may legally reuse
        # its draft. Goal-revision drafts remain live until the Goal itself is
        # accepted or otherwise terminal, so bounded continuation is preserved.
        if not current.goal_id:
            self._abandon_uncommitted_execution_leases(data, current)
        current.handoff_summary = self._build_run_handoff(data, current)
        # Terminalization is the idempotent projection boundary. Interrupted,
        # failed and budget-exhausted Runs receive the same Evidence contract
        # as successful Runs before any continuation can start.
        self._migrate_missing_tool_call_ids(session_id, data)
        self._ensure_evidence_metadata(session_id, data)
        saved = current.model_dump(mode="json")
        runs[run_id] = saved
        harness["latest_run_id"] = run_id
        self._write_file(session_id, data)
        return deepcopy(saved)

    @classmethod
    def _build_run_handoff(cls, data: dict[str, Any], run: Any) -> Any:
        from harness.models import RunHandoffSummary

        scope_key = cls._todo_scope_key(
            goal_id=run.goal_id,
            goal_revision=run.goal_revision,
            run_id=None if run.goal_id else run.run_id,
        )
        ledgers = data.get("todo_ledgers")
        todos = ledgers.get(scope_key) if isinstance(ledgers, dict) else []
        completed_todos = [
            {key: item.get(key) for key in ("id", "content", "status", "parent_id") if key in item}
            for item in (todos if isinstance(todos, list) else [])
            if isinstance(item, dict) and item.get("status") in {"completed", "cancelled"}
        ][-40:]
        refs: list[dict[str, Any]] = []
        for activation in run.verification_activations:
            for raw in activation.stable_evidence_refs:
                stable = raw.model_dump(mode="json")
                resolved = resolve_evidence_ref(
                    data,
                    stable,
                    goal_id=run.goal_id,
                    goal_revision=run.goal_revision,
                    require_inheritable=True,
                )
                if resolved is not None:
                    refs.append(stable)
        refs = [item for item in refs if cls._is_safe_handoff_evidence(data, item)]
        refs = list({ref_key(item): item for item in refs}.values())
        artifact_refs = [item for item in refs if item.get("type") in {"artifact", "external_mutation"}]
        sql_refs = [
            item for item in refs if item.get("type") in {"analytics_result", "sql_generation", "sql_validation"}
        ]
        report = run.verification_report
        durable_facts = []
        if report is not None and report.status.value in {"satisfied", "not_required"}:
            explanation = str(report.explanation or "").strip()
            if explanation:
                durable_facts.append(explanation[:500])
        return RunHandoffSummary(
            source_run_id=run.run_id,
            goal_id=run.goal_id,
            goal_revision=run.goal_revision,
            terminal_status=run.outcome.value if run.outcome is not None else run.status.value,
            objective=run.objective,
            completed_todos=completed_todos,
            durable_facts=durable_facts,
            evidence_refs=refs[-100:],
            artifact_refs=artifact_refs[-40:],
            sql_generation_refs=sql_refs[-40:],
            unresolved_gaps=(list(report.gaps) if report is not None else []),
        )

    @staticmethod
    def _lease_matches_execution_scope(lease: dict[str, Any], run: Any) -> bool:
        def field(name: str) -> Any:
            return run.get(name) if isinstance(run, dict) else getattr(run, name, None)

        goal_id = str(field("goal_id") or "")
        if goal_id:
            return str(lease.get("goal_id") or "") == goal_id and int(lease.get("goal_revision") or 1) == int(
                field("goal_revision") or field("objective_revision") or 1
            )
        return (
            not str(lease.get("goal_id") or "")
            and str(lease.get("run_id") or "") == str(field("run_id") or "")
            and str(lease.get("query_id") or "") == str(field("query_id") or "")
        )

    @classmethod
    def _abandon_uncommitted_execution_leases(
        cls,
        data: dict[str, Any],
        run: Any,
        *,
        reason: str = "execution_scope_terminal",
    ) -> bool:
        """Close draft leases owned by a terminal Run or Goal revision.

        The mutation is deliberately idempotent: committed and already
        abandoned receipts are immutable audit records.
        """

        now = time.time()
        changed = False
        for collection_name, draft_statuses in (
            ("external_artifact_leases", {"claiming", "staged"}),
            ("external_directory_leases", {"claiming", "staged", "prepared"}),
        ):
            leases = data.get(collection_name)
            if not isinstance(leases, dict):
                continue
            for lease in leases.values():
                if (
                    not isinstance(lease, dict)
                    or str(lease.get("status") or "") not in draft_statuses
                    or not cls._lease_matches_execution_scope(lease, run)
                ):
                    continue
                lease["status"] = "abandoned"
                lease.setdefault("abandoned_at", now)
                lease.setdefault("abandoned_reason", reason)
                changed = True
        return changed

    @staticmethod
    def _abandon_terminal_run_search_snapshots(
        data: dict[str, Any],
        run: Any,
    ) -> bool:
        """Expire read-only search snapshots at every Run boundary.

        Writable Goal-revision drafts intentionally survive a non-terminal Goal
        Run. Search snapshots do not: they are derived from the permission state
        of one concrete Run and must never turn a Run grant into Goal-scoped
        access merely because the Goal reuses its scratch directory.
        """

        def field(name: str) -> Any:
            return run.get(name) if isinstance(run, dict) else getattr(run, name, None)

        run_id = str(field("run_id") or "")
        query_id = str(field("query_id") or "")
        if not run_id:
            return False
        now = time.time()
        changed = False
        for collection_name in (
            "external_artifact_leases",
            "external_directory_leases",
        ):
            leases = data.get(collection_name)
            if not isinstance(leases, dict):
                continue
            for lease in leases.values():
                if (
                    not isinstance(lease, dict)
                    or str(lease.get("status") or "") != "search_snapshot"
                    or str(lease.get("run_id") or "") != run_id
                    or (query_id and str(lease.get("query_id") or "") != query_id)
                ):
                    continue
                lease["status"] = "abandoned"
                lease.setdefault("abandoned_at", now)
                lease.setdefault("abandoned_reason", "run_search_snapshot_terminal")
                changed = True
        return changed

    @staticmethod
    def _external_draft_paths_overlap(
        *,
        first_kind: str,
        first_path: str,
        second_kind: str,
        second_path: str,
    ) -> bool:
        """Return whether two writable draft claims cover any same formal file."""

        first = Path(first_path).expanduser().resolve()
        second = Path(second_path).expanduser().resolve()
        if first_kind == "exact_file" and second_kind == "exact_file":
            return first == second
        if first_kind == "exact_directory" and second_kind == "exact_directory":
            try:
                first.relative_to(second)
                return True
            except ValueError:
                try:
                    second.relative_to(first)
                    return True
                except ValueError:
                    return False
        file_path, directory_path = (first, second) if first_kind == "exact_file" else (second, first)
        try:
            file_path.relative_to(directory_path)
            return True
        except ValueError:
            return False

    @_session_write_locked
    def claim_external_draft(
        self,
        session_id: str,
        *,
        lease_kind: str,
        lease: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically reserve one authoritative writable draft surface.

        Exact-file and directory leases share this conflict check, so the same
        formal file cannot silently acquire two writable scratch authorities.
        Read-only search snapshots never enter the active status set.
        """

        if lease_kind not in {"exact_file", "exact_directory"}:
            raise ValueError(f"unsupported external draft kind: {lease_kind}")
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        lease_id = str(lease.get("lease_id") or "")
        target_path = str(lease.get("target_path") if lease_kind == "exact_file" else lease.get("directory_path") or "")
        if not lease_id or not target_path:
            raise ValueError("external draft claim requires lease_id and target path")

        now = time.time()
        collections = (
            ("external_artifact_leases", "exact_file", {"claiming", "staged"}),
            (
                "external_directory_leases",
                "exact_directory",
                {"claiming", "staged", "prepared"},
            ),
        )
        for collection_name, existing_kind, active_statuses in collections:
            leases = data.get(collection_name)
            if not isinstance(leases, dict):
                continue
            for existing in leases.values():
                if not isinstance(existing, dict):
                    continue
                status = str(existing.get("status") or "")
                if status not in active_statuses:
                    continue
                if float(existing.get("expires_at") or 0) < now:
                    existing["status"] = "abandoned"
                    existing.setdefault("abandoned_at", now)
                    existing.setdefault("abandoned_reason", "draft_claim_expired")
                    continue
                if str(existing.get("lease_id") or "") == lease_id:
                    continue
                existing_path = str(
                    existing.get("target_path")
                    if existing_kind == "exact_file"
                    else existing.get("directory_path") or ""
                )
                if existing_path and self._external_draft_paths_overlap(
                    first_kind=lease_kind,
                    first_path=target_path,
                    second_kind=existing_kind,
                    second_path=existing_path,
                ):
                    raise RuntimeError(
                        "authoritative writable draft conflict: "
                        f"requested {lease_kind} {target_path}, but active "
                        f"{existing_kind} lease {existing.get('lease_id')} covers "
                        f"{existing_path}. Continue from that lease or abandon it first."
                    )

        collection_name = "external_artifact_leases" if lease_kind == "exact_file" else "external_directory_leases"
        leases = data.setdefault(collection_name, {})
        saved = deepcopy(lease)
        saved["status"] = "claiming"
        leases[lease_id] = saved
        self._write_file(session_id, data)
        return deepcopy(saved)

    @staticmethod
    def _is_durable_handoff_artifact(data: dict[str, Any], artifact: dict[str, Any]) -> bool:
        """Return whether an artifact is a formal, durable delivery reference."""

        paths = [str(artifact.get(key) or "").replace("\\", "/") for key in ("path", "host_path", "virtual_path")]
        if (
            str(artifact.get("scope") or "") == "scratch"
            or str(artifact.get("role") or "") == "temporary"
            or any(path.startswith("/scratch/") or "/scratch/validation/" in path for path in paths)
        ):
            return False

        scope = str(artifact.get("scope") or "")
        role = str(artifact.get("role") or "")
        if scope in {"workspace", "attachment"}:
            return role == "target"
        if scope not in {"", "external"}:
            return False

        target = str(artifact.get("host_path") or artifact.get("path") or "")
        content_sha256 = str(artifact.get("content_sha256") or "")
        leases = data.get("external_artifact_leases")
        if isinstance(leases, dict):
            for lease in leases.values():
                if not isinstance(lease, dict) or lease.get("status") != "committed":
                    continue
                if str(lease.get("target_path") or "") != target:
                    continue
                committed_sha256 = str(lease.get("committed_sha256") or "")
                if not content_sha256 or not committed_sha256 or content_sha256 == committed_sha256:
                    return True

        directory_leases = data.get("external_directory_leases")
        if isinstance(directory_leases, dict):
            try:
                target_path = Path(target).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                return False
            for lease in directory_leases.values():
                if not isinstance(lease, dict) or lease.get("status") != "committed":
                    continue
                try:
                    target_path.relative_to(Path(str(lease.get("directory_path") or "")).expanduser().resolve())
                except (OSError, RuntimeError, ValueError):
                    continue
                return True
        return False

    @classmethod
    def _is_safe_handoff_evidence(
        cls,
        data: dict[str, Any],
        evidence: dict[str, Any],
    ) -> bool:
        if is_evidence_ref(evidence):
            return (
                resolve_evidence_ref(
                    data,
                    evidence,
                    require_inheritable=False,
                )
                is not None
            )
        paths = [str(evidence.get(key) or "").replace("\\", "/") for key in ("path", "host_path", "virtual_path")]
        if (
            str(evidence.get("scope") or "") == "scratch"
            or str(evidence.get("role") or "") == "temporary"
            or any(path.startswith("/scratch/") or "/scratch/validation/" in path for path in paths)
        ):
            return False
        if evidence.get("kind") == "artifact_write":
            return cls._is_durable_handoff_artifact(data, evidence)
        return True

    @_session_write_locked
    def update_terminal_run_verification_report(
        self,
        session_id: str,
        run_id: str,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach the post-Goal acceptance projection to an existing terminal Run."""

        from harness.models import RubricEvaluationReport, RunRecord

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        current = RunRecord.model_validate(raw_run)
        if not current.terminal:
            raise ValueError(f"Run {run_id} is not terminal")
        validated = RubricEvaluationReport.model_validate(report)
        if validated.run_id != run_id:
            raise ValueError("Verification report run_id does not match terminal Run")
        current.verification_report = validated
        saved = current.model_dump(mode="json")
        runs[run_id] = saved
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def update_run_verification_contract(
        self,
        session_id: str,
        run_id: str,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically update only the effective contract, preserving Tool ledger."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        if str(run.get("status") or "") in {
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }:
            raise ValueError(f"Terminal Run {run_id} cannot update its verification contract")
        saved = deepcopy(contract)
        run["verification_contract"] = saved
        run["updated_at"] = time.time()
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def upgrade_run_verification_mode(
        self,
        session_id: str,
        run_id: str,
        mode: str,
    ) -> dict[str, Any]:
        """Monotonically upgrade ordinary Runs without enabling Goal repair.

        ``agent -> proportional`` is the only runtime upgrade performed by
        successful mutation tools. Goal mode is established only by the Run
        coordinator from explicit product state and can never be inferred from
        a tool call or task classifier.
        """

        from harness.models import RunRecord, RunStatus, VerificationMode

        requested = VerificationMode(mode)
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.BUDGET_EXCEEDED,
            RunStatus.VERIFICATION_FAILED,
        }:
            raise ValueError(f"Terminal Run {run_id} cannot change verification mode")
        if run.verification_mode == VerificationMode.RUBRIC:
            return deepcopy(raw_run)
        if requested == VerificationMode.RUBRIC:
            raise ValueError("Goal verification mode requires an explicit Goal")
        if run.verification_mode == VerificationMode.AGENT and requested == VerificationMode.PROPORTIONAL:
            run.verification_mode = requested
            run.updated_at = time.time()
            saved = run.model_dump(mode="json")
            runs[run_id] = saved
            harness["latest_run_id"] = run_id
            self._write_file(session_id, data)
            return deepcopy(saved)
        return deepcopy(raw_run)

    @_session_write_locked
    def record_run_skill_selection(
        self,
        session_id: str,
        run_id: str,
        skill_id: str,
    ) -> dict[str, Any]:
        """Persist one main-Agent Skill choice after SKILL.md was read.

        This is the semantic routing authority for a live Run.  Preflight may
        record explicit user requests, but only a successful authoritative
        Skill read turns a candidate into an Agent-selected execution route.
        """

        from harness.models import RunRecord, RunStatus, SkillCandidate

        normalized = str(skill_id or "").strip()
        if not normalized:
            raise ValueError("skill_id is required")
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.BUDGET_EXCEEDED,
            RunStatus.VERIFICATION_FAILED,
        }:
            raise ValueError(f"Terminal Run {run_id} cannot change Skill routing")

        candidates = {item.skill_id: item for item in run.task_profile.skill_candidates}
        existing = candidates.get(normalized)
        candidates[normalized] = SkillCandidate(
            skill_id=normalized,
            confidence=1.0,
            evidence=(
                existing.evidence
                if existing is not None and existing.explicit
                else "主 Agent 已读取该 Skill 的权威 SKILL.md"
            ),
            explicit=bool(existing.explicit) if existing is not None else False,
        )
        run.task_profile.skill_candidates = list(candidates.values())
        run.task_profile.missing_explicit_skill_ids = [
            item for item in run.task_profile.missing_explicit_skill_ids if item.lower() != normalized.lower()
        ]
        run.task_profile.execution_route = "skill_first"
        run.task_profile.native_fallback = True
        run.task_profile.classifier = "agent_runtime"
        reason = f"agent_loaded_skill:{normalized}"
        if reason not in run.task_profile.reasons:
            run.task_profile.reasons.append(reason)
        run.updated_at = time.time()
        saved = run.model_dump(mode="json")
        runs[run_id] = saved
        self._write_file(session_id, data)
        return deepcopy(saved["task_profile"])

    @_session_write_locked
    def record_run_skill_activation(
        self,
        session_id: str,
        run_id: str,
        activation: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a verified Skill activation without granting Session scope."""

        from harness.models import RunRecord, RunStatus, SkillActivation

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.BUDGET_EXCEEDED,
            RunStatus.VERIFICATION_FAILED,
        }:
            raise ValueError(f"Terminal Run {run_id} cannot activate a Skill")
        candidate = SkillActivation.model_validate(
            {
                **activation,
                "run_id": run_id,
                "goal_id": run.goal_id,
                "goal_revision": run.goal_revision,
                "scope": "run",
            }
        )
        by_id = {item.activation_id: item for item in run.skill_activations}
        by_id[candidate.activation_id] = candidate
        run.skill_activations = list(by_id.values())
        run.updated_at = time.time()
        runs[run_id] = run.model_dump(mode="json")

        if run.goal_id:
            goals = harness.get("goals") if isinstance(harness, dict) else None
            raw_goal = goals.get(run.goal_id) if isinstance(goals, dict) else None
            if isinstance(raw_goal, dict) and int(raw_goal.get("objective_revision") or 1) == int(
                run.goal_revision or 1
            ):
                inherited = SkillActivation.model_validate(candidate.model_copy(update={"scope": "goal"}))
                existing = [
                    item
                    for item in raw_goal.get("skill_activations") or []
                    if isinstance(item, dict) and str(item.get("activation_id") or "") != inherited.activation_id
                ]
                raw_goal["skill_activations"] = [
                    *existing,
                    inherited.model_dump(mode="json"),
                ]
                raw_goal["updated_at"] = time.time()
        self._write_file(session_id, data)
        return candidate.model_dump(mode="json")

    def get_effective_run_skill_activations(
        self,
        session_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Return current-Run activations plus relevant same-Goal inheritance."""

        from harness.models import RunRecord, SkillActivation

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            return []
        run = RunRecord.model_validate(raw_run)
        activations = list(run.skill_activations)
        if run.goal_id:
            goals = harness.get("goals") if isinstance(harness, dict) else None
            raw_goal = goals.get(run.goal_id) if isinstance(goals, dict) else None
            if isinstance(raw_goal, dict) and int(raw_goal.get("objective_revision") or 1) == int(
                run.goal_revision or 1
            ):
                for raw in raw_goal.get("skill_activations") or []:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        activation = SkillActivation.model_validate(raw)
                    except ValueError:
                        continue
                    if int(activation.goal_revision or 1) == int(run.goal_revision or 1):
                        activations.append(activation)
        unique = {item.activation_id: item for item in activations}
        return [item.model_dump(mode="json") for item in unique.values()]

    @_session_write_locked
    def record_run_capability_manifest(
        self,
        session_id: str,
        run_id: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the exact prompt/schema authority used by one model call."""

        from harness.models import CapabilityManifest, RunRecord, RunStatus

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.BUDGET_EXCEEDED,
            RunStatus.VERIFICATION_FAILED,
        }:
            raise ValueError(f"Terminal Run {run_id} cannot change capabilities")
        parsed = CapabilityManifest.model_validate({**manifest, "run_id": run_id})
        run.capability_manifest = parsed
        run.updated_at = time.time()
        runs[run_id] = run.model_dump(mode="json")
        self._write_file(session_id, data)
        return parsed.model_dump(mode="json")

    @_session_write_locked
    def record_run_permission_manifest(
        self,
        session_id: str,
        run_id: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist the permission projection used by one model call."""

        from harness.models import PermissionManifest, RunRecord, RunStatus

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.BUDGET_EXCEEDED,
            RunStatus.VERIFICATION_FAILED,
        }:
            raise ValueError(f"Terminal Run {run_id} cannot change permissions")
        parsed = PermissionManifest.model_validate({**manifest, "run_id": run_id})
        run.permission_manifest = parsed
        run.updated_at = time.time()
        runs[run_id] = run.model_dump(mode="json")
        self._write_file(session_id, data)
        return parsed.model_dump(mode="json")

    @_session_write_locked
    def record_delegation_contract(
        self,
        session_id: str,
        run_id: str,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one immutable server-authored subagent contract."""

        from harness.models import DelegationContract, RunRecord

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        parsed = DelegationContract.model_validate({**contract, "session_id": session_id, "parent_run_id": run_id})
        run = RunRecord.model_validate(raw_run)
        existing = {item.subagent_run_id: item for item in run.delegation_contracts}
        if parsed.subagent_run_id in existing and existing[parsed.subagent_run_id] != parsed:
            raise ValueError(f"Delegation {parsed.subagent_run_id} is immutable")
        existing[parsed.subagent_run_id] = parsed
        run.delegation_contracts = list(existing.values())
        run.updated_at = time.time()
        runs[run_id] = run.model_dump(mode="json")
        self._write_file(session_id, data)
        return parsed.model_dump(mode="json")

    @_session_write_locked
    def record_delegation_event(
        self,
        session_id: str,
        run_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """Append an idempotent nested subagent lifecycle event."""

        from harness.models import RunRecord

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        payload = deepcopy(event)
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            raise ValueError("delegation event_id is required")
        by_id = {
            str(item.get("event_id") or ""): item
            for item in run.delegation_events
            if isinstance(item, dict) and item.get("event_id")
        }
        by_id[event_id] = payload
        run.delegation_events = list(by_id.values())
        run.updated_at = time.time()
        runs[run_id] = run.model_dump(mode="json")
        self._write_file(session_id, data)
        return deepcopy(payload)

    @_session_write_locked
    def record_delegation_result(
        self,
        session_id: str,
        run_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one terminal structured handoff from a subagent."""

        from harness.models import DelegationResultEnvelope, RunRecord

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        parsed = DelegationResultEnvelope.model_validate(result)
        run = RunRecord.model_validate(raw_run)
        by_id = {item.subagent_run_id: item for item in run.delegation_results}
        by_id[parsed.subagent_run_id] = parsed
        run.delegation_results = list(by_id.values())
        run.updated_at = time.time()
        runs[run_id] = run.model_dump(mode="json")
        self._write_file(session_id, data)
        return parsed.model_dump(mode="json")

    @_session_write_locked
    def start_harness_run(
        self,
        session_id: str,
        run: dict[str, Any],
        goal: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Atomically persist a new Run and its optional attached Goal."""

        run_id = str(run.get("run_id") or "").strip()
        if not run_id or run.get("session_id") != session_id:
            raise ValueError("Run identity does not match persistence session")
        goal_id = str(goal.get("goal_id") or "").strip() if goal else ""
        if goal is not None and (not goal_id or goal.get("session_id") != session_id):
            raise ValueError("Goal identity does not match persistence session")
        if goal is not None and run.get("goal_id") != goal_id:
            raise ValueError("Run goal_id does not match attached Goal")

        from harness.models import RunRecord, RunStatus

        incoming_run = RunRecord.model_validate(run)
        if incoming_run.status != RunStatus.PREPARING or incoming_run.outcome is not None:
            raise ValueError("A new Run must start in preparing state without an outcome")

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        compaction = data.get("agent_context_compaction")
        if self._agent_context_compaction_is_active(compaction):
            raise ValueError(
                f"Session {session_id} is compacting Agent context; retry after maintenance completes"
            )
        if isinstance(compaction, dict) and compaction.get("status") == "running":
            compaction["status"] = "expired"
            compaction["completed_at"] = time.time()
            compaction["error"] = "maintenance claim expired before Run start"
        harness = data.setdefault("harness", {})
        runs = harness.setdefault("runs", {})
        if run_id in runs:
            raise ValueError(f"Run {run_id} already exists")
        terminal_run_statuses = {
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }
        active_run = next(
            (
                item
                for item in runs.values()
                if isinstance(item, dict) and item.get("status") not in terminal_run_statuses
            ),
            None,
        )
        if active_run is not None:
            raise ValueError(f"Session {session_id} already has active Run {active_run.get('run_id')}")

        saved_goal: dict[str, Any] | None = None
        if goal is not None:
            goals = harness.setdefault("goals", {})
            active_goal_id = harness.get("active_goal_id")
            if (
                isinstance(active_goal_id, str)
                and active_goal_id != goal_id
                and isinstance(goals.get(active_goal_id), dict)
                and goals[active_goal_id].get("status") == "active"
            ):
                raise ValueError(f"Session {session_id} already has active Goal {active_goal_id}")
            existing_goal = goals.get(goal_id)
            if isinstance(existing_goal, dict) and existing_goal.get("status") in {
                "completed",
                "cancelled",
                "budget_exceeded",
            }:
                raise ValueError(f"Goal {goal_id} is already terminal")
            if isinstance(existing_goal, dict):
                if existing_goal.get("status") != "active":
                    raise ValueError(f"Goal {goal_id} is not active ({existing_goal.get('status')})")
                if existing_goal.get("requested_status"):
                    raise ValueError(
                        f"Goal {goal_id} has pending control request {existing_goal.get('requested_status')}"
                    )
                if existing_goal.get("current_run_id"):
                    raise ValueError(f"Goal {goal_id} already has running Run {existing_goal.get('current_run_id')}")
                if int(existing_goal.get("objective_revision") or 1) != int(goal.get("objective_revision") or 1):
                    raise ValueError(f"Goal {goal_id} revision changed before Run start")
                saved_goal = deepcopy(existing_goal)
                run_ids = saved_goal.setdefault("run_ids", [])
                if run_id not in run_ids:
                    if int(saved_goal.get("round") or 0) >= int(saved_goal.get("max_rounds") or 0):
                        raise ValueError(f"Goal {goal_id} has no remaining Runs")
                    run_ids.append(run_id)
                    saved_goal["round"] = int(saved_goal.get("round") or 0) + 1
                saved_goal["current_run_id"] = run_id
                saved_goal["pending_revision"] = False
                saved_goal["updated_at"] = time.time()
            else:
                saved_goal = deepcopy(goal)
            goals[goal_id] = saved_goal
            goal_order = harness.setdefault("goal_order", [])
            if goal_id not in goal_order:
                goal_order.append(goal_id)
            if saved_goal.get("status") == "active":
                harness["active_goal_id"] = goal_id

        saved_run = deepcopy(run)
        config_snapshot = saved_run.get("config_snapshot")
        if not isinstance(config_snapshot, dict):
            config_snapshot = {}
        # The Session lock makes this the linearization point between a mode
        # change and a new Run. Caller-supplied values can never override it.
        config_snapshot["permissions"] = permission_policy_snapshot(data.get("permissions"))
        saved_run["config_snapshot"] = config_snapshot
        runs[run_id] = saved_run
        run_order = harness.setdefault("run_order", [])
        run_order.append(run_id)
        harness["latest_run_id"] = run_id
        self._write_file(session_id, data)
        return deepcopy(saved_run), deepcopy(saved_goal)

    @_session_write_locked
    def request_goal_control(
        self,
        session_id: str,
        goal_id: str,
        requested_status: str,
    ) -> dict[str, Any]:
        """Linearize pause/cancel intent against Run start and completion."""

        if requested_status not in {"paused", "cancelled"}:
            raise ValueError(f"Unsupported Goal control status: {requested_status}")
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        raw_goal = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(raw_goal, dict):
            raise ValueError(f"Goal {goal_id} does not exist in session {session_id}")
        status = str(raw_goal.get("status") or "")
        if status in {"completed", "cancelled"}:
            raise ValueError(f"Goal {goal_id} is already terminal ({status})")
        if status == "budget_exceeded" and requested_status != "cancelled":
            raise ValueError(f"Goal {goal_id} exhausted its budget and can only be cancelled or explicitly extended")
        now = time.time()
        current_run_id = str(raw_goal.get("current_run_id") or "").strip()
        if current_run_id:
            raw_goal["requested_status"] = requested_status
            notice = (
                "已请求暂停 Goal，正在停止当前 Run。"
                if requested_status == "paused"
                else "已请求取消 Goal，正在停止当前 Run。"
            )
            notices = raw_goal.setdefault("control_notices", [])
            if notice not in notices:
                notices.append(notice)
        else:
            raw_goal["status"] = requested_status
            raw_goal["requested_status"] = None
            raw_goal["current_run_id"] = None
            if requested_status == "cancelled":
                raw_goal["completed_at"] = now
            if harness.get("active_goal_id") == goal_id:
                harness.pop("active_goal_id", None)
        raw_goal["updated_at"] = now
        if raw_goal.get("status") in {"cancelled", "budget_exceeded", "completed"}:
            self._abandon_uncommitted_execution_leases(data, raw_goal)
        self._write_file(session_id, data)
        return deepcopy(raw_goal)

    @_session_write_locked
    def extend_goal_budget(
        self,
        session_id: str,
        goal_id: str,
        *,
        additional_rounds: int,
    ) -> dict[str, Any]:
        """Atomically add user-approved Runs and reopen an exhausted Goal paused."""

        if isinstance(additional_rounds, bool) or not isinstance(additional_rounds, int):
            raise ValueError("additional_rounds must be an integer")
        if additional_rounds < 1 or additional_rounds > 100:
            raise ValueError("additional_rounds must be between 1 and 100")
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        raw_goal = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(raw_goal, dict):
            raise ValueError(f"Goal {goal_id} does not exist in session {session_id}")
        status = str(raw_goal.get("status") or "")
        if status != "budget_exceeded":
            raise ValueError(f"Goal {goal_id} is not budget_exceeded (current status: {status})")
        if raw_goal.get("current_run_id") or raw_goal.get("requested_status"):
            raise ValueError("Goal still has an active control transition")

        previous_max = int(raw_goal.get("max_rounds") or 0)
        raw_goal["max_rounds"] = previous_max + additional_rounds
        raw_goal["status"] = "paused"
        raw_goal["completed_at"] = None
        raw_goal["budget_exhaustion_reason"] = None
        raw_goal["updated_at"] = time.time()
        notice = f"用户已追加 {additional_rounds} 轮执行预算（{previous_max} → {raw_goal['max_rounds']}），等待继续。"
        notices = raw_goal.setdefault("control_notices", [])
        if notice not in notices:
            notices.append(notice)
        if harness.get("active_goal_id") == goal_id:
            harness.pop("active_goal_id", None)
        self._write_file(session_id, data)
        return deepcopy(raw_goal)

    @_session_write_locked
    def bind_run_execution_snapshot(
        self,
        session_id: str,
        run_id: str,
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind the prepared Run to one effective workspace backend exactly once."""

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        if raw_run.get("status") != "preparing":
            raise ValueError("Execution snapshot may only be bound while Run is preparing")
        snapshot = deepcopy(execution)
        config_snapshot = raw_run.setdefault("config_snapshot", {})
        existing = config_snapshot.get("execution")
        if existing is not None:
            if existing != snapshot:
                raise ValueError("Run execution snapshot is already bound")
            return deepcopy(raw_run)
        config_snapshot["execution"] = snapshot
        raw_run["updated_at"] = time.time()
        self._write_file(session_id, data)
        return deepcopy(raw_run)

    def get_goal_state(
        self,
        session_id: str,
        goal_id: str,
    ) -> dict[str, Any] | None:
        """Return one persisted Goal from Session JSON."""

        goals = self.get_harness_state(session_id).get("goals")
        if not isinstance(goals, dict):
            return None
        goal = goals.get(goal_id)
        return deepcopy(goal) if isinstance(goal, dict) else None

    def get_active_goal_state(self, session_id: str) -> dict[str, Any] | None:
        """Return the single active Goal for a Session, if present."""

        harness = self.get_harness_state(session_id)
        active_goal_id = harness.get("active_goal_id")
        goals = harness.get("goals")
        if not isinstance(active_goal_id, str) or not isinstance(goals, dict):
            return None
        goal = goals.get(active_goal_id)
        if not isinstance(goal, dict) or goal.get("status") != "active":
            return None
        return deepcopy(goal)

    @_session_write_locked
    def update_goal_objective(
        self,
        session_id: str,
        goal_id: str,
        *,
        objective: str,
        expected_revision: int,
        contract: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Atomically revise a Goal objective and its frozen acceptance contract."""

        data = self._read_file(session_id)
        repair_legacy_validation_wrapper_records(data)
        harness = data.get("harness") if data else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        raw_goal = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(raw_goal, dict):
            raise ValueError(f"Goal {goal_id} does not exist in session {session_id}")
        status = str(raw_goal.get("status") or "")
        if status in {"completed", "cancelled", "budget_exceeded"}:
            raise ValueError(f"Goal {goal_id} is already terminal ({status})")
        current_revision = int(raw_goal.get("objective_revision") or 1)
        if current_revision != expected_revision:
            raise ValueError(f"Goal revision conflict: expected {expected_revision}, current {current_revision}.")
        current_objective = str(raw_goal.get("objective") or "").strip()
        if objective == current_objective:
            return deepcopy(raw_goal)

        revisions = raw_goal.get("revisions")
        if not isinstance(revisions, list):
            revisions = []
        if not revisions:
            existing_contract = raw_goal.get("goal_contract")
            revisions.append(
                {
                    "revision": current_revision,
                    "objective": current_objective,
                    "contract_id": (
                        existing_contract.get("contract_id") if isinstance(existing_contract, dict) else None
                    ),
                    "created_at": float(raw_goal.get("created_at") or time.time()),
                }
            )
        next_revision = current_revision + 1
        revisions.append(
            {
                "revision": next_revision,
                "objective": objective,
                "contract_id": contract.get("contract_id") if isinstance(contract, dict) else None,
                "created_at": time.time(),
            }
        )
        raw_goal["objective"] = objective
        raw_goal["objective_revision"] = next_revision
        raw_goal["revisions"] = revisions
        raw_goal["goal_contract"] = deepcopy(contract)
        raw_goal["pending_revision"] = True
        raw_goal["gaps"] = []
        # Artifact receipts are acceptance evidence for one immutable Goal
        # revision. Runs remain in history for audit, but old receipts cannot
        # satisfy a materially revised objective.
        # A wording/constraint revision invalidates semantic conclusions, but
        # it does not erase immutable file writes and validations. Downstream
        # checks still require the new declared targets to match and the bytes
        # to equal their recorded hashes before these refs can satisfy anything.
        raw_goal["evidence_refs"] = self._artifact_bound_goal_evidence_refs(
            data,
            goal_id,
        )
        raw_goal["skill_activations"] = []
        raw_goal["latest_verification_report_id"] = None
        raw_goal["latest_goal_decision"] = None
        requests = harness.get("completion_requests") if isinstance(harness, dict) else None
        if isinstance(requests, dict):
            for request in requests.values():
                if (
                    isinstance(request, dict)
                    and str(request.get("goal_id") or "") == goal_id
                    and int(request.get("objective_revision") or 0) == current_revision
                    and str(request.get("status") or "") in {"requested", "evaluating"}
                ):
                    request["status"] = "invalidated"
                    request["invalidated_reason"] = "goal_revision_superseded"
                    request["decided_at"] = time.time()
        raw_goal["latest_completion_request_id"] = None
        raw_goal["budget_exhaustion_reason"] = None
        raw_goal["updated_at"] = time.time()
        # A revised objective establishes a new immutable execution authority.
        # Old-revision drafts cannot remain writable or they will both block the
        # replacement revision and risk being committed under the wrong
        # acceptance contract. This mutation shares the Goal revision CAS/write.
        self._abandon_uncommitted_execution_leases(
            data,
            {
                "goal_id": goal_id,
                "goal_revision": current_revision,
            },
            reason="goal_revision_superseded",
        )
        self._write_file(session_id, data)
        return deepcopy(raw_goal)

    @_session_write_locked
    def upsert_goal_state(
        self,
        session_id: str,
        goal: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one Goal and enforce at most one active Goal per Session."""

        goal_id = str(goal.get("goal_id") or "").strip()
        if not goal_id:
            raise ValueError("goal_id is required")
        persisted_session_id = str(goal.get("session_id") or "").strip()
        if persisted_session_id != session_id:
            raise ValueError("Goal session_id does not match persistence session")

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        goals = harness.setdefault("goals", {})
        status = str(goal.get("status") or "")
        active_goal_id = harness.get("active_goal_id")
        existing = goals.get(goal_id)
        if (
            isinstance(existing, dict)
            and existing.get("status")
            in {
                "completed",
                "cancelled",
                "budget_exceeded",
            }
            and existing != goal
        ):
            raise ValueError(f"Goal {goal_id} is already terminal ({existing.get('status')})")
        if status == "active" and isinstance(active_goal_id, str) and active_goal_id != goal_id:
            active = goals.get(active_goal_id)
            if isinstance(active, dict) and active.get("status") == "active":
                raise ValueError(f"Session {session_id} already has active Goal {active_goal_id}")

        saved = deepcopy(goal)
        if isinstance(existing, dict):
            existing_revision = int(existing.get("objective_revision") or 1)
            incoming_revision = int(saved.get("objective_revision") or 1)
            if existing_revision > incoming_revision:
                # A Run that started under an older objective may finish after
                # the user edits the Goal. It may detach itself and contribute
                # usage, but it must never overwrite the revised objective,
                # contract, status, or pending-revision marker.
                authoritative = deepcopy(existing)
                authoritative["current_run_id"] = saved.get("current_run_id")
                authoritative["model_call_count"] = max(
                    int(authoritative.get("model_call_count") or 0),
                    int(saved.get("model_call_count") or 0),
                )
                authoritative["updated_at"] = max(
                    float(authoritative.get("updated_at") or 0),
                    float(saved.get("updated_at") or 0),
                )
                saved = authoritative
            elif existing_revision == incoming_revision:
                activations = {
                    str(item.get("activation_id")): deepcopy(item)
                    for item in [
                        *(existing.get("skill_activations") or []),
                        *(saved.get("skill_activations") or []),
                    ]
                    if isinstance(item, dict) and item.get("activation_id")
                }
                saved["skill_activations"] = list(activations.values())
        goals[goal_id] = saved
        goal_order = harness.setdefault("goal_order", [])
        if goal_id not in goal_order:
            goal_order.append(goal_id)
        if status == "active":
            harness["active_goal_id"] = goal_id
        elif active_goal_id == goal_id:
            harness.pop("active_goal_id", None)
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def finalize_goal_run_state(
        self,
        session_id: str,
        goal: dict[str, Any],
        *,
        run_id: str,
    ) -> dict[str, Any]:
        """Atomically detach one Run without losing concurrent Goal control.

        Run completion is a compare-and-set operation over the authoritative
        Session JSON. A pause/cancel request or objective revision that lands
        after the coordinator loaded its snapshot must win over the stale
        completion proposal.
        """

        goal_id = str(goal.get("goal_id") or "").strip()
        if not goal_id or not run_id:
            raise ValueError("goal_id and run_id are required")
        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        existing = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(existing, dict):
            raise ValueError(f"Goal {goal_id} does not exist in session {session_id}")
        current_run_id = str(existing.get("current_run_id") or "").strip()
        if current_run_id != run_id:
            raise ValueError(f"Goal {goal_id} current Run changed: expected {run_id}, got {current_run_id or 'none'}")

        incoming = deepcopy(goal)
        existing_revision = int(existing.get("objective_revision") or 1)
        incoming_revision = int(incoming.get("objective_revision") or 1)
        if existing_revision > incoming_revision:
            # Preserve the edited objective/contract and only close the old
            # Run. Its evidence and acceptance result belong to the old
            # revision and cannot satisfy the new one.
            saved = deepcopy(existing)
            saved["current_run_id"] = None
            saved["model_call_count"] = max(
                int(existing.get("model_call_count") or 0),
                int(incoming.get("model_call_count") or 0),
            )
            saved["pending_revision"] = True
            saved["gaps"] = []
            revision_notice = "目标描述已更新，将按最新版本进入下一 Run。"
            notices = saved.setdefault("control_notices", [])
            if revision_notice not in notices:
                notices.append(revision_notice)
        else:
            saved = incoming
            saved["current_run_id"] = None
            activations = {
                str(item.get("activation_id")): deepcopy(item)
                for item in [
                    *(existing.get("skill_activations") or []),
                    *(saved.get("skill_activations") or []),
                ]
                if isinstance(item, dict) and item.get("activation_id")
            }
            saved["skill_activations"] = list(activations.values())

        # Merge notices written by the control endpoint after the coordinator
        # loaded its snapshot, then consume the latest requested transition.
        notices = saved.setdefault("control_notices", [])
        for notice in existing.get("control_notices") or []:
            if notice and notice not in notices:
                notices.append(notice)
        requested = str(existing.get("requested_status") or "").strip()
        if requested in {"paused", "cancelled"}:
            saved["status"] = requested
            saved["requested_status"] = None
            if requested == "cancelled":
                saved["completed_at"] = time.time()
        else:
            saved["requested_status"] = None
        saved["updated_at"] = time.time()

        goals[goal_id] = saved
        if saved.get("status") in {"completed", "cancelled", "budget_exceeded"}:
            self._abandon_uncommitted_execution_leases(data, saved)
        if saved.get("status") == "active":
            harness["active_goal_id"] = goal_id
        elif harness.get("active_goal_id") == goal_id:
            harness.pop("active_goal_id", None)
        self._write_file(session_id, data)
        return deepcopy(saved)

    def get_trace(self, session_id: str) -> dict[str, Any] | None:
        """Return the latest persisted execution trace for a session."""
        data = self._read_trace_file(session_id)
        latest_query_id = data.get("latest_query_id")
        traces = data.get("traces")
        if isinstance(latest_query_id, str) and isinstance(traces, dict):
            trace = traces.get(latest_query_id)
            if isinstance(trace, dict):
                return dict(trace)
        return None

    def get_traces(self, session_id: str) -> dict[str, dict[str, Any]]:
        """Return all query-scoped traces for a session."""
        data = self._read_trace_file(session_id)
        traces = data.get("traces") if data else None
        if not isinstance(traces, dict):
            return {}
        return {str(query_id): dict(trace) for query_id, trace in traces.items() if isinstance(trace, dict)}

    @staticmethod
    def _trace_model_input_spans(trace: dict[str, Any]) -> list[dict[str, Any]]:
        spans = [
            item for item in trace.get("spans") or [] if isinstance(item, dict) and item.get("type") == "model_input"
        ]
        return sorted(
            spans,
            key=lambda item: (
                int((item.get("metadata") or {}).get("model_call_index") or 0),
                float(item.get("started_at") or 0),
            ),
        )

    @staticmethod
    def _model_input_fingerprints(span: dict[str, Any]) -> dict[str, str]:
        output = span.get("output")
        contract = (
            output.get("model_call_contract")
            if isinstance(output, dict) and isinstance(output.get("model_call_contract"), dict)
            else {}
        )
        fingerprints = contract.get("fingerprints")
        if not isinstance(fingerprints, dict):
            metadata = span.get("metadata")
            fingerprints = (
                metadata.get("fingerprints")
                if isinstance(metadata, dict) and isinstance(metadata.get("fingerprints"), dict)
                else {}
            )
        return {
            key: str(fingerprints.get(key) or "")
            for key in fingerprints
            if key
            in {
                "system_prompt_hash",
                "system_stable_hash",
                "system_project_hash",
                "system_versioned_hash",
                "system_memory_hash",
                "system_active_runtime_hash",
                "system_volatile_tail_hash",
                "tool_schema_hash",
                "tool_stable_prefix_hash",
                "tool_full_schema_hash",
                "messages_hash",
                "messages_history_hash",
                "messages_volatile_tail_hash",
                "cache_cohort_id",
            }
        }

    @staticmethod
    def _model_input_message_hashes(span: dict[str, Any]) -> list[str]:
        output = span.get("output")
        previews = (
            output.get("messages_preview")
            if isinstance(output, dict) and isinstance(output.get("messages_preview"), list)
            else []
        )
        return [
            hashlib.sha256(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            for item in previews
            if isinstance(item, dict) and str(item.get("role") or "").lower() not in {"system", "systemmessage"}
        ]

    @classmethod
    def _trace_cache_continuity(
        cls,
        previous: dict[str, Any],
        current: dict[str, Any],
        *,
        previous_query_id: str,
    ) -> dict[str, Any] | None:
        previous_spans = cls._trace_model_input_spans(previous)
        current_spans = cls._trace_model_input_spans(current)
        if not previous_spans or not current_spans:
            return None
        previous_span = previous_spans[-1]
        current_span = current_spans[0]
        previous_fingerprints = cls._model_input_fingerprints(previous_span)
        current_fingerprints = cls._model_input_fingerprints(current_span)
        previous_messages = cls._model_input_message_hashes(previous_span)
        current_messages = cls._model_input_message_hashes(current_span)
        prefix_count = 0
        for before, after in zip(previous_messages, current_messages, strict=False):
            if before != after:
                break
            prefix_count += 1
        prefix_ratio = round(prefix_count / len(previous_messages), 6) if previous_messages else 1.0
        first_diff_part = "none"
        first_diff_path = ""
        for part, paths in (
            (
                "system",
                (
                    "system_stable_hash",
                    "system_project_hash",
                    "system_versioned_hash",
                    "system_memory_hash",
                    "system_active_runtime_hash",
                    "system_volatile_tail_hash",
                    "system_prompt_hash",
                ),
            ),
            ("tools", ("tool_stable_prefix_hash", "tool_full_schema_hash", "tool_schema_hash")),
            ("messages", ("messages_history_hash", "messages_volatile_tail_hash", "messages_hash")),
        ):
            for path in paths:
                if previous_fingerprints.get(path) != current_fingerprints.get(path):
                    first_diff_part = part
                    first_diff_path = path
                    break
            if first_diff_part != "none":
                break
        system_match = bool(
            previous_fingerprints.get("system_prompt_hash")
            and previous_fingerprints.get("system_prompt_hash") == current_fingerprints.get("system_prompt_hash")
        )
        tool_schema_match = bool(
            previous_fingerprints.get("tool_schema_hash")
            and previous_fingerprints.get("tool_schema_hash") == current_fingerprints.get("tool_schema_hash")
        )
        continuity = {
            "previous_query_id": previous_query_id,
            "system_prompt_hash_match": system_match,
            "tool_schema_hash_match": tool_schema_match,
            "messages_hash_match": bool(
                previous_fingerprints.get("messages_hash")
                and previous_fingerprints.get("messages_hash") == current_fingerprints.get("messages_hash")
            ),
            "previous_message_count": len(previous_messages),
            "current_message_count": len(current_messages),
            "message_prefix_match_count": prefix_count,
            "message_prefix_ratio": prefix_ratio,
            "stable_boundary_match": system_match and tool_schema_match,
            "full_previous_request_prefix_match": (
                system_match and tool_schema_match and prefix_count == len(previous_messages)
            ),
        }
        partition_keys = {
            "system_stable_hash",
            "system_versioned_hash",
            "system_project_hash",
            "system_memory_hash",
            "system_active_runtime_hash",
            "system_volatile_tail_hash",
            "tool_stable_prefix_hash",
            "tool_full_schema_hash",
            "messages_history_hash",
            "messages_volatile_tail_hash",
            "cache_cohort_id",
        }
        if partition_keys & (set(previous_fingerprints) | set(current_fingerprints)):
            continuity.update(
                {
                    "cache_cohort_id_match": bool(
                        previous_fingerprints.get("cache_cohort_id")
                        and previous_fingerprints.get("cache_cohort_id")
                        == current_fingerprints.get("cache_cohort_id")
                    ),
                    "first_diff_part": first_diff_part,
                    "first_diff_path": first_diff_path,
                    "system_stable_hash_match": bool(
                        previous_fingerprints.get("system_stable_hash")
                        and previous_fingerprints.get("system_stable_hash")
                        == current_fingerprints.get("system_stable_hash")
                    ),
                    "tool_stable_prefix_hash_match": bool(
                        previous_fingerprints.get("tool_stable_prefix_hash")
                        and previous_fingerprints.get("tool_stable_prefix_hash")
                        == current_fingerprints.get("tool_stable_prefix_hash")
                    ),
                    "messages_history_hash_match": bool(
                        previous_fingerprints.get("messages_history_hash")
                        and previous_fingerprints.get("messages_history_hash")
                        == current_fingerprints.get("messages_history_hash")
                    ),
                }
            )
        return continuity

    @_session_write_locked
    def update_trace(
        self,
        session_id: str,
        trace: dict[str, Any],
        query_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a query trace in its sidecar without duplicating the latest trace."""
        if not self._session_path(session_id).exists():
            return trace
        data = self._read_trace_file(session_id)
        saved = dict(trace)
        effective_query_id = query_id or saved.get("query_id")
        previous_query_id = data.get("latest_query_id")
        traces = data.get("traces")
        previous = (
            traces.get(previous_query_id)
            if isinstance(previous_query_id, str)
            and isinstance(traces, dict)
            and previous_query_id != effective_query_id
            else None
        )
        if isinstance(previous, dict):
            continuity = self._trace_cache_continuity(
                previous,
                saved,
                previous_query_id=previous_query_id,
            )
            if continuity is not None:
                saved["cache_continuity"] = continuity
                for metric_name, metric_value in (
                    (
                        "cache_continuity_system_prompt_match",
                        int(continuity["system_prompt_hash_match"]),
                    ),
                    (
                        "cache_continuity_tool_schema_match",
                        int(continuity["tool_schema_hash_match"]),
                    ),
                    (
                        "cache_continuity_message_prefix_ratio",
                        float(continuity["message_prefix_ratio"]),
                    ),
                ):
                    emit_harness_metric(
                        logger,
                        metric_name,
                        session_id=session_id,
                        value=metric_value,
                        query_id=effective_query_id,
                        previous_query_id=previous_query_id,
                    )
        if isinstance(effective_query_id, str) and effective_query_id:
            saved["query_id"] = effective_query_id
            if not isinstance(traces, dict):
                traces = {}
            traces[effective_query_id] = saved
            data["traces"] = traces
            data["latest_query_id"] = effective_query_id
        if saved.get("trace_id"):
            data["latest_trace_id"] = saved.get("trace_id")
        self._write_trace_file(session_id, data)
        return dict(saved)

    def get_trace_state(self, session_id: str) -> dict[str, Any]:
        """Return trace history and its selection metadata for the trace UI."""
        data = self._read_trace_file(session_id)
        session = self._read_file(session_id)
        traces = data.get("traces")
        result = {
            "traces": {str(query_id): dict(trace) for query_id, trace in traces.items() if isinstance(trace, dict)}
            if isinstance(traces, dict)
            else {},
            "latest_query_id": data.get("latest_query_id"),
            "latest_trace_id": data.get("latest_trace_id"),
        }
        todos, authority = self._current_todo_projection(session)
        result["todos"] = todos
        result["todos_authority"] = authority
        result["todo_ledger_revision"] = self.get_todo_snapshot(session_id).get("ledger_revision", 0)
        if isinstance(session.get("graph"), dict):
            result["graph"] = dict(session["graph"])
        return result

    @_session_write_locked
    def update_graph(self, session_id: str, graph: dict[str, Any]) -> dict[str, Any]:
        """Persist the compiled LangGraph structure for trace inspection."""

        data = self._read_file(session_id)
        if not data:
            return graph
        saved = dict(graph)
        data["graph"] = saved
        self._write_file(session_id, data)
        return dict(saved)

    @_session_write_locked
    def update_title(self, session_id: str, title: str) -> None:
        """更新标题（rename_session 的别名，供 API 层调用）"""
        self.rename_session(session_id, title)

    @_session_write_locked
    def delete_session_if_idle_headless_before(
        self,
        session_id: str,
        *,
        cutoff: float,
        terminal_run_statuses: set[str],
    ) -> bool:
        """Atomically delete one expired Headless Session if no Run is active."""

        data = self._read_file(session_id)
        if (
            not data
            or data.get("headless_enabled") is not True
        ):
            return False
        try:
            updated_at = float(data.get("updated_at"))
        except (TypeError, ValueError):
            return False
        if updated_at > cutoff:
            return False
        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else {}
        if any(
            isinstance(run, dict)
            and str(run.get("status") or "") not in terminal_run_statuses
            for run in (runs.values() if isinstance(runs, dict) else ())
        ):
            return False
        self.delete_session(session_id)
        return True

    @_session_write_locked
    def delete_session(self, session_id: str) -> None:
        """删除会话文件"""
        data = self._read_file(session_id)
        source_workspaces: set[str] = set()
        if data:
            logical = deepcopy(data)
            logical["messages"] = deepcopy(self.load_session(session_id))
            self._ensure_evidence_metadata(session_id, logical)
            for item in (logical.get("evidence_index") or {}).values():
                raw_ref = item.get("raw_output_ref") if isinstance(item, dict) else None
                if isinstance(raw_ref, dict) and raw_ref.get("kind") == "deepagents_large_tool_result":
                    workspace = str(raw_ref.get("workspace_path") or "")
                    if workspace:
                        source_workspaces.add(workspace)
            legacy_workspace = str(data.get("workspace_path") or "")
            if legacy_workspace:
                source_workspaces.add(legacy_workspace)
        path = self._session_path(session_id)  # 获取文件路径
        if path.exists():  # 存在则删除
            path.unlink()
        self._trace_path(session_id).unlink(missing_ok=True)
        archive_dir = self._sessions_dir / "archive"
        if archive_dir.exists():
            safe_session = "".join(c for c in session_id if c.isalnum() or c in "-_")
            for archive in archive_dir.glob(f"{safe_session}_*.json"):
                archive.unlink(missing_ok=True)
        # Keep attachment lifetime aligned with the authoritative Session.
        # Import lazily to avoid a module cycle at startup.
        from graph.attachment_store import attachment_store

        if attachment_store.root_dir is not None:
            attachment_store.delete_session(session_id)
        if self._base_dir is not None:
            safe_session = re.sub(r"[^A-Za-z0-9_-]+", "_", session_id)
            projects_root = self._base_dir / "data" / "harness-scratch" / "projects"
            if projects_root.exists():
                for project_root in projects_root.iterdir():
                    shutil.rmtree(project_root / safe_session, ignore_errors=True)
            for source_workspace in source_workspaces:
                workspace = Path(source_workspace).expanduser().resolve()
                large_results_root = (workspace / ".puddingclaw" / "large_tool_results").resolve()
                target = (large_results_root / safe_session).resolve()
                try:
                    target.relative_to(large_results_root)
                except ValueError:
                    continue
                shutil.rmtree(target, ignore_errors=True)

    def get_raw_messages(self, session_id: str) -> dict[str, Any]:
        """Return session data without loading heavyweight trace sidecars."""
        data = self._read_file(session_id)  # 读取会话文件
        if not data:  # 不存在返回空结构
            return {"title": "", "messages": []}
        data = dict(data)
        data["messages"] = self.load_session(session_id)
        deliveries = data.get("attachment_deliveries")
        if isinstance(deliveries, dict):
            by_query = {
                str(query_id): [dict(item) for item in items if isinstance(item, dict)]
                for query_id, items in deliveries.items()
                if isinstance(items, list)
            }
            seen_queries: set[str] = set()
            for message in data["messages"]:
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                query_id = str(message.get("query_id") or "")
                if query_id not in by_query:
                    continue
                persisted_by_id = {
                    str(item.get("id") or ""): item
                    for item in message.get("output_attachments") or []
                    if isinstance(item, dict) and item.get("id")
                }
                message["output_attachments"] = [
                    {
                        **deepcopy(persisted_by_id.get(str(item.get("id") or ""), {})),
                        **deepcopy(item),
                    }
                    for item in by_query[query_id]
                ]
                seen_queries.add(query_id)
            # A crash can happen after durable publish but before the stream
            # consumes its ToolMessage. Keep the generated file discoverable.
            for query_id, items in by_query.items():
                if query_id not in seen_queries and items:
                    data["messages"].append(
                        {
                            "role": "assistant",
                            "content": "",
                            "query_id": query_id,
                            "status": "interrupted",
                            "output_attachments": deepcopy(items),
                        }
                    )
        # Lightweight runtime state remains available, but trace data has a
        # dedicated lazy endpoint and is never read by the conversation view.
        todos, authority = self._current_todo_projection(data)
        legacy_todos = data.get("todos")
        if isinstance(legacy_todos, list) and legacy_todos != todos:
            emit_harness_metric(
                logger,
                "goal_todo_projection_mismatch_count",
                session_id=session_id,
                authority=authority.get("kind"),
            )
        data["todos"] = todos
        data["todos_authority"] = authority
        scope_key = self._todo_scope_key(
            goal_id=str(authority.get("goal_id") or "") or None,
            goal_revision=authority.get("goal_revision"),
            run_id=str(authority.get("run_id") or "") or None,
        )
        data["todo_ledger_revision"] = self._todo_ledger_revision(data, scope_key)
        if isinstance(data.get("graph"), dict):
            data["graph"] = dict(data["graph"])
        else:
            data.pop("graph", None)
        if isinstance(data.get("harness"), dict):
            data["harness"] = deepcopy(data["harness"])
        else:
            data.pop("harness", None)
        return data  # 返回完整数据

    def get_active_messages(self, session_id: str) -> list[dict[str, Any]]:
        """返回当前 session.json 中尚未归档的活跃消息。仅供 Agent 上下文优化使用。"""
        data = self._read_file(session_id)
        if not data:
            return []
        return list(data.get("messages", []))

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有会话的元数据（id/title/updated_at），按修改时间倒序"""
        assert self._sessions_dir is not None  # 确保已初始化
        sessions: list[dict[str, Any]] = []  # 结果列表
        for f in sorted(
            self._sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):  # 遍历所有 JSON 文件，按修改时间倒序
            # Background jobs may reuse the Agent harness for one isolated run,
            # but they are task-center records rather than user conversations.
            if f.stem.startswith("background-job-"):
                continue
            raw: Any = None
            try:
                # Reuse the canonical reader so legacy embedded traces are
                # migrated once instead of slowing every sidebar refresh.
                raw = self._read_file(f.stem)
                if isinstance(raw, dict):  # v2 格式
                    title = raw.get("title", f.stem)  # 取标题，缺省用文件名
                    updated_at = raw.get("updated_at", f.stat().st_mtime)  # 取更新时间
                else:  # v1 格式（纯列表）
                    title = f.stem  # 用文件名作标题
                    updated_at = f.stat().st_mtime  # 用文件修改时间
            except Exception:  # 解析失败兜底
                title = f.stem  # 用文件名
                updated_at = f.stat().st_mtime  # 用文件修改时间

            meta = {
                "id": f.stem,  # 会话 ID = 文件名（不含 .json）
                "title": title,  # 会话标题
                "updated_at": updated_at,  # 最后更新时间
                # Files without runtime_mode predate Agent mode and remain
                # classified as legacy Chat instead of being silently migrated.
                "runtime_mode": raw.get("runtime_mode", "chat") if isinstance(raw, dict) else "chat",
            }
            if isinstance(raw, dict):
                for key in (
                    "project_id",
                    "project_path",
                    "workspace_type",
                    "workspace_path",
                    "analytics_model_id",
                    "llm_model_id",
                    "thinking_level",
                    "credential_name",
                ):
                    if key in raw:
                        meta[key] = raw.get(key)
            sessions.append(meta)  # 追加到结果
        return sessions  # 返回所有会话列表

    @staticmethod
    def _searchable_message_content(message: dict[str, Any]) -> str:
        """Return only user-visible message text, excluding tool payloads."""

        def collect(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                parts: list[str] = []
                for item in value:
                    parts.extend(collect(item))
                return parts
            if not isinstance(value, dict):
                return []

            # Multimodal message blocks commonly store visible copy in
            # ``text`` or nest it under ``content``. Deliberately do not walk
            # arbitrary keys: tool inputs/outputs are execution records, not
            # conversation content.
            text = value.get("text")
            if isinstance(text, str):
                return [text]
            if "content" in value:
                return collect(value.get("content"))
            return []

        content = " ".join(collect(message.get("content")))
        return re.sub(r"\s+", " ", content).strip()

    @staticmethod
    def _search_snippet(text: str, query: str, *, max_length: int = 180) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return ""

        match = re.search(re.escape(query), normalized, flags=re.IGNORECASE)
        if match is None:
            return normalized if len(normalized) <= max_length else f"{normalized[:max_length].rstrip()}…"

        start = max(0, match.start() - 58)
        end = min(len(normalized), start + max_length)
        if end - start < max_length:
            start = max(0, end - max_length)
        snippet = normalized[start:end].strip()
        return f"{'…' if start > 0 else ''}{snippet}{'…' if end < len(normalized) else ''}"

    def search_sessions(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Search session titles and visible conversation content."""

        normalized_query = query.strip()
        if not normalized_query:
            return []

        query_key = normalized_query.casefold()
        matches: list[dict[str, Any]] = []
        for meta in self.list_sessions():
            title = str(meta.get("title") or meta["id"])
            title_matches = query_key in title.casefold()
            content_match = ""
            preview = ""

            for message in self.load_session(str(meta["id"])):
                if not isinstance(message, dict):
                    continue
                if str(message.get("role") or "") not in {"user", "assistant"}:
                    continue
                text = self._searchable_message_content(message)
                if not text:
                    continue
                if not preview:
                    preview = text
                if title_matches:
                    break
                if query_key in text.casefold():
                    content_match = text
                    break

            if not title_matches and not content_match:
                continue

            result = dict(meta)
            result["matched_in"] = "title" if title_matches else "content"
            result["snippet"] = self._search_snippet(
                content_match or preview,
                normalized_query if content_match else "",
            )
            matches.append(result)

        matches.sort(
            key=lambda item: (
                item.get("matched_in") == "title",
                float(item.get("updated_at") or 0),
            ),
            reverse=True,
        )
        return matches[: max(1, min(limit, 100))]

    # ── 短期记忆压缩（核心机制）────────────────────────────────────────────────

    @_session_write_locked
    def compress_history(self, session_id: str, summary: str, num_to_remove: int) -> None:
        """压缩短期记忆：归档旧消息 + 保存 LLM 生成的摘要"""
        assert self._sessions_dir is not None  # 确保已初始化
        data = self._read_file(session_id)  # 读取当前会话
        if not data:  # 会话不存在则跳过
            return

        messages = data.get("messages", [])  # 获取消息列表
        archived_messages = messages[:num_to_remove]  # 取出要归档的前 N 条消息

        # 将被压缩的消息归档到 sessions/archive/ 目录（备份，不丢失原始数据）
        archive_dir = self._sessions_dir / "archive"  # 归档目录路径
        archive_dir.mkdir(exist_ok=True)  # 不存在则创建
        archive_data = {  # 归档数据结构
            "session_id": session_id,  # 所属会话
            "archived_at": time.time(),  # 归档时间戳
            "messages": archived_messages,  # 被归档的消息
        }
        archive_path = archive_dir / f"{session_id}_{int(time.time())}.json"  # 归档文件名含时间戳防重复
        archive_path.write_text(  # 写入归档文件
            json.dumps(archive_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        data["messages"] = messages[num_to_remove:]  # 从会话中删除已归档的消息

        # 将摘要追加到 compressed_context 字段（支持多次压缩，用 --- 分隔）
        existing_context = data.get("compressed_context", "")  # 读取已有摘要
        if existing_context:  # 已有摘要则拼接
            data["compressed_context"] = existing_context + "\n---\n" + summary
        else:  # 首次压缩直接写入
            data["compressed_context"] = summary

        self._write_file(session_id, data)  # 写回磁盘

    def get_compressed_context(self, session_id: str) -> str | None:
        """获取压缩摘要（如果存在）"""
        data = self._read_file(session_id)  # 读取会话数据
        if not data:  # 不存在返回 None
            return None
        return data.get("compressed_context")  # 返回摘要字段

    @_session_write_locked
    def middle_trim_history(
        self,
        session_id: str,
        summary: str,
        start_idx: int,
        end_idx: int,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """归档 active messages 的中段，并把摘要追加到 middle_trim_context。

        start_idx/end_idx 是当前 session.json 中 data["messages"] 的半开区间。
        前端仍可通过 load_session() 看到 archive + current 的完整历史；LLM 只读取
        middle_trim_context + current active messages。
        """
        assert self._sessions_dir is not None
        data = self._read_file(session_id)
        if not data:
            return None

        messages = data.get("messages", [])
        start_idx = max(0, start_idx)
        end_idx = min(len(messages), end_idx)
        if start_idx >= end_idx:
            return None

        archived_messages = messages[start_idx:end_idx]
        if not isinstance(data.get("display_messages"), list):
            data["display_messages"] = self.load_session(session_id)

        archive_dir = self._sessions_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
        now = time.time()
        archive_name = f"{session_id}_middle_{int(now * 1000)}.json"
        archive_path = archive_dir / archive_name
        archive_data = {
            "session_id": session_id,
            "archive_type": "middle_trim",
            "archived_at": now,
            "range": {"start_idx": start_idx, "end_idx": end_idx},
            "messages": archived_messages,
            "summary": summary,
        }
        if metadata:
            archive_data["metadata"] = metadata
        archive_path.write_text(
            json.dumps(archive_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        data["messages"] = messages[:start_idx] + messages[end_idx:]

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        block = (
            f"[中段裁剪摘要 {timestamp}]\n"
            f"archive: {archive_name}\n"
            f"messages: {len(archived_messages)}\n"
            f"range: active messages[{start_idx}:{end_idx}]\n"
            f"摘要：\n{summary.strip()}"
        )
        existing_context = data.get("middle_trim_context", "")
        data["middle_trim_context"] = existing_context + "\n---\n" + block if existing_context else block

        self._write_file(session_id, data)
        return archive_name

    def get_middle_trim_context(self, session_id: str) -> str | None:
        """获取中段裁剪摘要（如果存在）。"""
        data = self._read_file(session_id)
        if not data:
            return None
        return data.get("middle_trim_context")

    @_session_write_locked
    def update_tool_call_output(
        self,
        session_id: str,
        tool_call_id: str,
        output: str,
        summary_source: str | None = None,
    ) -> bool:
        """按 tool_call_id 更新 session.json 中对应 tool_call 的 output。

        由 ToolResultClearMiddleware 触发 tool_result_clear 事件后，chat.py 调用本函数
        把摘要写回历史 tool_call，并标记 summary_source。
        """
        data = self._read_file(session_id)
        if not data:
            return False

        for msg in data.get("messages", []):
            for tc in msg.get("tool_calls", []):
                if tc.get("id") == tool_call_id:
                    tc.setdefault("raw_output", tc.get("output", ""))
                    tc["output"] = output
                    if summary_source:
                        tc["summary_source"] = summary_source
                    self._write_file(session_id, data)
                    return True
        return False

    # ── DeepAgents Tool Context compaction ──────────────────────────────────

    @staticmethod
    def _tool_context_source(tool_call: dict[str, Any]) -> str:
        value = tool_call.get("raw_output")
        if value is None or str(value).strip() == "":
            value = tool_call.get("output", "")
        return str(value or "")

    @staticmethod
    def _tool_context_source_hash(output: str) -> str:
        normalized = output.replace("\r\n", "\n").replace("\r", "\n")
        return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _tool_context_tokens(output: str) -> int:
        # Keep selection deterministic and cheap. Runtime/model accounting uses
        # the normal tokenizer approximation at the actual model boundary.
        return max(1, (len(output) + 3) // 4) if output else 0

    @staticmethod
    def _tool_context_result_id(output: str) -> str | None:
        patterns = (
            r'"result_id"\s*:\s*"([^"\\]+)"',
            r"\bresult[_ -]?id\s*[：:=]\s*([A-Za-z0-9_.:-]+)",
            r"\b(result-[A-Za-z0-9_-]+)\b",
        )
        for pattern in patterns:
            match = re.search(pattern, output, flags=re.IGNORECASE)
            if match:
                return str(match.group(1))
        return None

    @classmethod
    def _tool_context_raw_ref(
        cls,
        session_id: str,
        tool_call_id: str,
        output: str,
        source_hash: str,
        *,
        tool_name: str = "",
        source_query_id: str = "",
        source_hash_scope: str = "raw_result",
        workspace_path: str = "",
    ) -> dict[str, Any]:
        result_id = cls._tool_context_result_id(output)
        session_ref = {
            "kind": "session_tool_call",
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "source_hash": source_hash,
            "output_complete": True,
        }
        if source_query_id:
            session_ref["source_query_id"] = source_query_id
        normalized_tool = str(tool_name or "").lower().replace("-", "_")
        if result_id and (
            not normalized_tool
            or normalized_tool
            in {
                "database_sql_execute",
                "database_knowledge_query",
            }
        ):
            # The Session record only contains the preview/profile emitted by
            # database_sql_execute. It remains useful after expiry, but is not
            # a truthful fallback for the complete materialized JSONL result.
            sql_ref: dict[str, Any] = {
                "kind": "sql_query_result",
                "result_id": result_id,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
                "source_query_id": source_query_id,
                "artifact_format": "jsonl",
                "source_hash": source_hash,
                "source_hash_scope": source_hash_scope,
                "fallback": {**session_ref, "output_complete": False},
            }
            field_patterns = {
                "generation_id": r"\bgeneration_id\s*[：:=]\s*([A-Za-z0-9_.:-]+)",
                "validation_receipt_id": r"\bvalidation_receipt_id\s*[：:=]\s*([A-Za-z0-9_.:-]+)",
                "sql_sha256": r"\bsql_sha256\s*[：:=]\s*([A-Za-z0-9_.:-]+)",
                "expires_at": r"(?:过期时间|expires_at)\s*[：:=]\s*([^）)\s]+)",
                "artifact_sha256": r"\bartifact_sha256\s*[：:=]\s*(sha256:[A-Fa-f0-9]{64})",
            }
            for key, pattern in field_patterns.items():
                match = re.search(pattern, output, flags=re.IGNORECASE)
                if match:
                    sql_ref[key] = str(match.group(1))
            return sql_ref
        if re.search(r"(?:^|\s)/large_tool_results/[^\s`]+", output):
            return {
                "kind": "deepagents_large_tool_result",
                "session_id": session_id,
                "source_query_id": source_query_id,
                "tool_call_id": tool_call_id,
                "artifact_name": tool_call_id.replace(".", "_").replace("/", "_").replace("\\", "_"),
                "workspace_path": workspace_path,
                "source_hash": source_hash,
                "source_hash_scope": source_hash_scope,
            }
        return session_ref

    @staticmethod
    def _evidence_status(tool_call: dict[str, Any], message: dict[str, Any]) -> tuple[str, bool]:
        output = str(tool_call.get("raw_output", tool_call.get("output", "")) or "")
        call_status = str(tool_call.get("status") or "")
        if (
            not output
            or "工具结果缺失" in output
            or "未收到工具返回" in output
            or call_status == "interrupted"
            or str(tool_call.get("summary_source") or "") in {"stream_cancelled", "missing_tool_output"}
        ):
            return "interrupted", False
        if tool_call.get("is_error") or call_status in {"error", "failed"}:
            return "failed", True
        if call_status == "running" and not tool_call.get("completed_at"):
            return "interrupted", False
        return "success", True

    @classmethod
    def _ensure_evidence_metadata(cls, session_id: str, data: dict[str, Any]) -> bool:
        """Attach stable, deterministic Evidence metadata to persisted calls."""

        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        run_by_query = {
            str(run.get("query_id") or ""): str(run.get("run_id") or "")
            for run in (runs.values() if isinstance(runs, dict) else [])
            if isinstance(run, dict) and run.get("query_id")
        }
        changed = False
        evidence_index = data.setdefault("evidence_index", {})
        if not isinstance(evidence_index, dict):
            evidence_index = {}
            data["evidence_index"] = evidence_index
            changed = True
        for _, _, message, tool_call in cls._iter_persisted_tool_calls(data):
            tool_call_id = str(tool_call.get("id") or "")
            if not tool_call_id:
                continue
            source_query_id = str(message.get("query_id") or "")
            source_run_id = str(tool_call.get("source_run_id") or run_by_query.get(source_query_id) or "")
            raw_source = cls._tool_context_source(tool_call)
            context_metadata = tool_call.get("context_compaction")
            tagged_hash = str(
                tool_call.get("source_hash")
                or (context_metadata.get("source_hash") if isinstance(context_metadata, dict) else "")
                or ""
            )
            source_hash = tagged_hash or cls._tool_context_source_hash(raw_source)
            provenance_id = source_run_id or (f"query:{source_query_id}" if source_query_id else "")
            digest = hashlib.sha256(
                "\0".join((session_id, provenance_id, tool_call_id, source_hash)).encode("utf-8")
            ).hexdigest()[:32]
            evidence_id = f"evidence-{digest}"
            status, output_complete = cls._evidence_status(tool_call, message)
            raw_ref = tool_call.get("raw_output_ref")
            if not isinstance(raw_ref, dict):
                raw_ref = cls._tool_context_raw_ref(
                    session_id,
                    tool_call_id,
                    raw_source,
                    source_hash,
                    tool_name=str(tool_call.get("tool") or tool_call.get("name") or ""),
                    source_query_id=source_query_id,
                    source_hash_scope=("raw_result" if tagged_hash else "pointer"),
                )
            elif raw_ref.get("kind") == "deepagents_large_tool_result" and not raw_ref.get("source_query_id"):
                raw_ref = {**raw_ref, "source_query_id": source_query_id}
            metadata = {
                "evidence_id": evidence_id,
                "tool_call_id": tool_call_id,
                "tool": str(tool_call.get("tool") or tool_call.get("name") or "unknown_tool"),
                "source_session_id": session_id,
                "source_run_id": source_run_id,
                "source_query_id": source_query_id,
                "source_hash": source_hash,
                "status": status,
                "output_complete": output_complete,
                "raw_output_ref": deepcopy(raw_ref),
                "projection": {
                    "profile": "detailed",
                    "version": "evidence-projection-v1",
                },
            }
            for key in (
                "evidence_id",
                "source_run_id",
                "source_query_id",
                "source_hash",
                "status",
                "output_complete",
                "raw_output_ref",
            ):
                value = metadata[key]
                if tool_call.get(key) != value:
                    tool_call[key] = deepcopy(value)
                    changed = True
            if evidence_index.get(evidence_id) != metadata:
                evidence_index[evidence_id] = metadata
                changed = True
        return changed

    @staticmethod
    def _iter_persisted_tool_calls(data: dict[str, Any]):
        for message_index, message in enumerate(data.get("messages") or []):
            if not isinstance(message, dict):
                continue
            for tool_index, tool_call in enumerate(message.get("tool_calls") or []):
                if isinstance(tool_call, dict):
                    yield message_index, tool_index, message, tool_call

    @classmethod
    def _tool_call_ids(cls, data: dict[str, Any]) -> list[str]:
        return [str(tool_call.get("id") or "") for _, _, _, tool_call in cls._iter_persisted_tool_calls(data)]

    def _migrate_missing_tool_call_ids(self, session_id: str, data: dict[str, Any]) -> bool:
        """Persist deterministic IDs for legacy Tool Results that lacked one."""

        changed = False
        used = {item for item in self._tool_call_ids(data) if item}
        for message_index, tool_index, _message, tool_call in self._iter_persisted_tool_calls(data):
            if str(tool_call.get("id") or ""):
                continue
            seed = json.dumps(
                {
                    "session_id": session_id,
                    "message_index": message_index,
                    "tool_index": tool_index,
                    "tool": tool_call.get("tool") or tool_call.get("name"),
                    "input": tool_call.get("input") or tool_call.get("args"),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            base = f"historical_tool_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"
            stable_id = base
            suffix = 1
            while stable_id in used:
                suffix += 1
                stable_id = f"{base}_{suffix}"
            tool_call["id"] = stable_id
            display_messages = data.get("display_messages")
            if isinstance(display_messages, list) and message_index < len(display_messages):
                display_message = display_messages[message_index]
                display_tool_calls = display_message.get("tool_calls") if isinstance(display_message, dict) else None
                if isinstance(display_tool_calls, list) and tool_index < len(display_tool_calls):
                    display_tool_call = display_tool_calls[tool_index]
                    if isinstance(display_tool_call, dict) and not display_tool_call.get("id"):
                        display_tool_call["id"] = stable_id
            used.add(stable_id)
            changed = True
        return changed

    @staticmethod
    def _merge_replayed_tool_call(target: dict[str, Any], replay: dict[str, Any]) -> None:
        """Merge useful fields from one cumulative-stream replay into its first record."""

        empty_values = (None, "", [], {})
        for key, value in replay.items():
            if key == "completed_at":
                # The first completion is authoritative; a later timestamp is
                # evidence of the replay, not a second execution.
                continue
            if target.get(key) in empty_values and value not in empty_values:
                target[key] = value

    @classmethod
    def _coalesce_replayed_tool_calls(cls, data: dict[str, Any]) -> bool:
        """Remove duplicate persisted representations of the same Tool Call ID.

        A Tool Call ID denotes one execution. LangGraph may repeat its completed
        ToolMessage in cumulative parallel-node updates, but the Session/model
        transcript must remain idempotent. The first record keeps its ID and
        position; sparse fields from later replays are merged into it.
        """

        changed = False

        def coalesce_records(
            records: list[Any],
            seen: dict[tuple[str, str], dict[str, Any]],
            scope: str,
        ) -> list[Any]:
            nonlocal changed
            result: list[Any] = []
            for record in records:
                if not isinstance(record, dict):
                    result.append(record)
                    continue
                tool_call_id = str(record.get("id") or "")
                if not tool_call_id:
                    result.append(record)
                    continue
                identity = (scope, tool_call_id)
                first = seen.get(identity)
                if first is None:
                    seen[identity] = record
                    result.append(record)
                    continue
                cls._merge_replayed_tool_call(first, record)
                changed = True
            return result

        def coalesce_messages(messages: Any) -> None:
            nonlocal changed
            if not isinstance(messages, list):
                return
            seen_top_level: dict[tuple[str, str], dict[str, Any]] = {}
            seen_segment: dict[tuple[str, str], dict[str, Any]] = {}
            seen_timeline: dict[tuple[str, str], dict[str, Any]] = {}
            for message_index, message in enumerate(messages):
                if not isinstance(message, dict):
                    continue
                scope = str(message.get("query_id") or f"legacy-message-{message_index}")
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    deduped = coalesce_records(tool_calls, seen_top_level, scope)
                    if len(deduped) != len(tool_calls):
                        message["tool_calls"] = deduped

                segments = message.get("segments")
                if isinstance(segments, list):
                    for segment in segments:
                        if not isinstance(segment, dict):
                            continue
                        segment_calls = segment.get("tool_calls")
                        if isinstance(segment_calls, list):
                            deduped = coalesce_records(segment_calls, seen_segment, scope)
                            if len(deduped) != len(segment_calls):
                                segment["tool_calls"] = deduped

                timeline = message.get("timeline")
                if isinstance(timeline, list):
                    next_timeline: list[Any] = []
                    for item in timeline:
                        if not isinstance(item, dict) or item.get("type") != "tool":
                            next_timeline.append(item)
                            continue
                        tool_call = item.get("tool_call")
                        if not isinstance(tool_call, dict):
                            next_timeline.append(item)
                            continue
                        tool_call_id = str(tool_call.get("id") or "")
                        identity = (scope, tool_call_id)
                        if not tool_call_id or identity not in seen_timeline:
                            if tool_call_id:
                                seen_timeline[identity] = tool_call
                            next_timeline.append(item)
                            continue
                        cls._merge_replayed_tool_call(seen_timeline[identity], tool_call)
                        changed = True
                    if len(next_timeline) != len(timeline):
                        message["timeline"] = next_timeline

        coalesce_messages(data.get("messages"))
        # display_messages is an independent UI projection, so dedupe it with
        # fresh identity maps rather than comparing it to the model transcript.
        coalesce_messages(data.get("display_messages"))
        return changed

    def select_tool_context_candidates(
        self,
        session_id: str,
        *,
        min_result_tokens: int,
        keep_recent: int,
        policy_version: str,
        context_profile: str = "detailed",
    ) -> list[dict[str, Any]]:
        """Return immutable background candidates without changing visible output."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return []
            migrated = self._migrate_missing_tool_call_ids(session_id, data)
            ids = self._tool_call_ids(data)
            nonempty_ids = [item for item in ids if item]
            if len(nonempty_ids) != len(set(nonempty_ids)):
                data["tool_context_job"] = {
                    "status": "failed",
                    "error": "duplicate_tool_call_id",
                    "updated_at": time.time(),
                }
                self._write_file(session_id, data)
                return []

            completed: list[tuple[dict[str, Any], str, str, int, float, str]] = []
            for _, _, message, tool_call in self._iter_persisted_tool_calls(data):
                tool_call_id = str(tool_call.get("id") or "")
                output = self._tool_context_source(tool_call)
                if not tool_call_id or not output:
                    continue
                source_hash = str(tool_call.get("source_hash") or "") or self._tool_context_source_hash(output)
                completed_at = tool_call.get("completed_at")
                try:
                    completion_order = float(completed_at)
                except (TypeError, ValueError):
                    # Legacy calls have no timestamp. Their persisted protocol
                    # order is stable and necessarily predates new timestamped calls.
                    completion_order = float(len(completed))
                completed.append(
                    (
                        tool_call,
                        tool_call_id,
                        source_hash,
                        self._tool_context_tokens(output),
                        completion_order,
                        str(message.get("query_id") or ""),
                    )
                )

            protected_ids = (
                {item[1] for item in sorted(completed, key=lambda item: item[4])[-max(0, int(keep_recent)) :]}
                if keep_recent > 0
                else set()
            )
            candidates: list[dict[str, Any]] = []
            cache_hit_count = 0
            for (
                tool_call,
                tool_call_id,
                source_hash,
                estimated_tokens,
                completion_order,
                source_query_id,
            ) in completed:
                if tool_call_id in protected_ids or estimated_tokens < max(1, int(min_result_tokens)):
                    continue
                existing_raw_ref = tool_call.get("raw_output_ref")
                if (
                    isinstance(existing_raw_ref, dict)
                    and existing_raw_ref.get("kind") == "deepagents_large_tool_result"
                ):
                    # DeepAgents has already replaced this result with a stable
                    # external pointer. Compacting the pointer adds no value and
                    # risks hiding the only recovery instruction.
                    continue
                metadata = tool_call.get("context_compaction")
                if (
                    isinstance(metadata, dict)
                    and metadata.get("status") == "ready"
                    and metadata.get("source_hash") == source_hash
                    and metadata.get("policy_version") == policy_version
                    and metadata.get("context_profile", "detailed") == context_profile
                    and tool_call.get("context_output")
                ):
                    cache_hit_count += 1
                    continue
                candidates.append(
                    {
                        "tool_call_id": tool_call_id,
                        "tool": str(tool_call.get("tool") or tool_call.get("name") or "unknown_tool"),
                        "input": tool_call.get("input") or tool_call.get("args") or "",
                        "output": self._tool_context_source(tool_call),
                        "source_hash": source_hash,
                        "estimated_tokens": estimated_tokens,
                        "completed_at": completion_order,
                        "is_error": bool(tool_call.get("is_error")),
                        "user_referenced": bool(
                            tool_call.get("user_referenced") or tool_call.get("referenced_by_user")
                        ),
                        "raw_output_ref": self._tool_context_raw_ref(
                            session_id,
                            tool_call_id,
                            self._tool_context_source(tool_call),
                            source_hash,
                            tool_name=str(tool_call.get("tool") or tool_call.get("name") or ""),
                            source_query_id=source_query_id,
                        ),
                        "context_profile": context_profile,
                    }
                )
            candidates.sort(
                key=lambda item: (
                    int(bool(item.get("is_error") or item.get("user_referenced"))),
                    -int(item.get("estimated_tokens") or 0),
                    float(item.get("completed_at") or 0),
                )
            )
            if candidates:
                candidates[0]["scan_cache_hit_count"] = cache_hit_count
            if migrated:
                self._write_file(session_id, data)
            return candidates

    def ensure_tool_call_ids(self, session_id: str) -> bool:
        """Persist stable, unique Tool Call IDs before model reconstruction."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return False
            logical = deepcopy(data)
            logical["messages"] = deepcopy(self.load_session(session_id))
            changed = self._migrate_missing_tool_call_ids(session_id, logical)
            changed = self._coalesce_replayed_tool_calls(logical) or changed
            changed = self._ensure_evidence_metadata(session_id, logical) or changed
            if data.get("display_messages") != logical.get("messages"):
                data["display_messages"] = deepcopy(logical.get("messages") or [])
                changed = True
            if data.get("evidence_index") != logical.get("evidence_index"):
                data["evidence_index"] = deepcopy(logical.get("evidence_index") or {})
                changed = True
            if not changed:
                return False
            self._write_file(session_id, data)
            return True

    def get_evidence(self, session_id: str, evidence_id: str) -> dict[str, Any] | None:
        """Return one stable Evidence descriptor, backfilling legacy calls."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return None
            changed = self._migrate_missing_tool_call_ids(session_id, data)
            changed = self._coalesce_replayed_tool_calls(data) or changed
            logical = deepcopy(data)
            logical["messages"] = deepcopy(self.load_session(session_id))
            self._ensure_evidence_metadata(session_id, logical)
            if data.get("evidence_index") != logical.get("evidence_index"):
                data["evidence_index"] = deepcopy(logical.get("evidence_index") or {})
                changed = True
            if changed:
                self._write_file(session_id, data)
            index = logical.get("evidence_index")
            item = index.get(evidence_id) if isinstance(index, dict) else None
            if not isinstance(item, dict):
                ledger = data.get("evidence_ledger")
                ledger_item = ledger.get(evidence_id) if isinstance(ledger, dict) else None
                if isinstance(ledger_item, dict):
                    item = {
                        "evidence_id": evidence_id,
                        "tool_call_id": str(ledger_item.get("origin_tool_call_id") or ""),
                        "tool": str(ledger_item.get("origin_tool_name") or ""),
                        "source_session_id": session_id,
                        "source_run_id": str(ledger_item.get("source_run_id") or ""),
                        "source_query_id": str(ledger_item.get("source_query_id") or ""),
                        "source_hash": str(ledger_item.get("output_digest") or ""),
                        "status": str(ledger_item.get("status") or "active"),
                        "output_complete": True,
                        "raw_output_ref": {
                            "kind": "evidence_ledger",
                            "ledger_kind": str(ledger_item.get("kind") or ""),
                            "ledger_id": evidence_id,
                        },
                        "payload": deepcopy(ledger_item),
                        "projection": {
                            "profile": "verification",
                            "version": "evidence-projection-v1",
                        },
                    }
            return deepcopy(item) if isinstance(item, dict) else None

    @staticmethod
    def _safe_evidence_component(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or ""))
        if not safe or safe in {".", ".."}:
            raise ValueError("Evidence locator contains an invalid identifier")
        return safe

    def read_evidence(
        self,
        session_id: str,
        evidence_id: str,
        *,
        workspace_path: str | Path | None = None,
        offset: int = 0,
        limit: int = 20_000,
        page: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Read immutable historical evidence without re-executing its tool."""

        descriptor = self.get_evidence(session_id, evidence_id)
        if descriptor is None:
            return {"evidence_id": evidence_id, "status": "not_found", "content": ""}
        raw_ref = descriptor.get("raw_output_ref")
        raw_ref = raw_ref if isinstance(raw_ref, dict) else {}
        kind = str(raw_ref.get("kind") or "session_tool_call")
        safe_offset = max(0, int(offset or 0))
        safe_limit = max(1, min(int(limit or 20_000), 100_000))
        content = ""
        available = False
        artifact_sha256 = ""
        hash_matches: bool | None = None
        unavailable_status = "missing"
        result: dict[str, Any] = {
            **deepcopy(descriptor),
            "historical": True,
            "raw_result_available": False,
        }

        if kind == "evidence_ledger":
            result["content"] = json.dumps(
                descriptor.get("payload") or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            result["raw_result_available"] = True
            result["hash_matches"] = None
            return result
        if kind == "deepagents_large_tool_result":
            origin_workspace = str(raw_ref.get("workspace_path") or workspace_path or "")
            if not origin_workspace:
                return {**result, "status": "workspace_unavailable", "content": ""}
            workspace = Path(origin_workspace).expanduser().resolve()
            safe_session = self._safe_evidence_component(str(raw_ref.get("session_id") or session_id))
            safe_query = self._safe_evidence_component(str(raw_ref.get("source_query_id") or ""))
            artifact_name = str(raw_ref.get("artifact_name") or "")
            if not artifact_name:
                artifact_name = (
                    str(raw_ref.get("tool_call_id") or "").replace(".", "_").replace("/", "_").replace("\\", "_")
                )
            if not artifact_name or artifact_name in {".", ".."} or "/" in artifact_name or "\\" in artifact_name:
                return {**result, "status": "invalid_locator", "content": ""}
            root = (workspace / ".puddingclaw" / "large_tool_results" / safe_session / safe_query).resolve()
            artifact = (root / artifact_name).resolve()
            try:
                artifact.relative_to(root)
            except ValueError:
                return {**result, "status": "invalid_locator", "content": ""}
            if artifact.is_file():
                raw_bytes = artifact.read_bytes()
                artifact_sha256 = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
                text = raw_bytes.decode("utf-8", errors="replace")
                source_text_hash = self._tool_context_source_hash(text)
                content = text[safe_offset : safe_offset + safe_limit]
                available = True
                large_hash_scope = str(raw_ref.get("source_hash_scope") or "raw_result")
                if large_hash_scope == "raw_result":
                    hash_matches = source_text_hash == str(descriptor.get("source_hash") or "")
                elif large_hash_scope == "raw_bytes":
                    hash_matches = artifact_sha256 == str(descriptor.get("source_hash") or "")
                result.update(
                    {
                        "offset": safe_offset,
                        "limit": safe_limit,
                        "total_chars": len(text),
                        "has_more": safe_offset + safe_limit < len(text),
                    }
                )
        elif kind == "sql_query_result":
            result_id = self._safe_evidence_component(str(raw_ref.get("result_id") or ""))
            if self._base_dir is None:
                return {**result, "status": "store_unavailable", "content": ""}
            root = (self._base_dir / "data" / "database-query-results").resolve()
            catalog_path = (root / ".catalog" / f"{result_id}.json").resolve()
            catalog: dict[str, Any] = {}
            try:
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                unavailable_status = "catalog_missing"
            strict_owner = str(catalog.get("owner_binding_version") or "") == "strict-v1"
            catalog_query_id = str(catalog.get("source_query_id") or "")
            catalog_run_id = str(catalog.get("source_run_id") or "")
            descriptor_query_id = str(descriptor.get("source_query_id") or "")
            descriptor_run_id = str(descriptor.get("source_run_id") or "")
            owner_matches = bool(catalog) and (
                str(catalog.get("result_id") or "") == result_id
                and str(catalog.get("session_id") or "") == session_id
                and str(catalog.get("tool_call_id") or "")
                == str(raw_ref.get("tool_call_id") or descriptor.get("tool_call_id") or "")
                and (
                    catalog_query_id == descriptor_query_id
                    if strict_owner
                    else catalog_query_id in {"", descriptor_query_id}
                )
                and (
                    catalog_run_id == descriptor_run_id
                    if strict_owner and catalog_run_id
                    else catalog_run_id in {"", descriptor_run_id}
                )
            )
            if catalog and not owner_matches:
                unavailable_status = "unauthorized"
            catalog_artifact = str(catalog.get("artifact_path") or "")
            artifact = (
                (self._base_dir / catalog_artifact).resolve()
                if catalog_artifact
                else (root / f"{result_id}.jsonl").resolve()
            )
            try:
                artifact.relative_to(root)
            except ValueError:
                return {**result, "status": "invalid_locator", "content": ""}
            effective_page = max(1, int(page or 1))
            effective_page_size = max(1, min(int(page_size or 100), 500))
            rows: list[Any] = []
            row_count = 0
            expired_at = str(catalog.get("expires_at") or "")
            is_expired = False
            if expired_at:
                try:
                    parsed_expiry = datetime.fromisoformat(expired_at.replace("Z", "+00:00"))
                    if parsed_expiry.tzinfo is None:
                        parsed_expiry = parsed_expiry.replace(tzinfo=timezone.utc)
                    is_expired = parsed_expiry <= datetime.now(timezone.utc)
                except ValueError:
                    is_expired = False
            if is_expired:
                unavailable_status = "expired"
            elif not artifact.is_file() and owner_matches:
                unavailable_status = "missing"
            if artifact.is_file() and not is_expired and owner_matches and str(catalog.get("status") or "") == "ready":
                digest = hashlib.sha256()
                start = (effective_page - 1) * effective_page_size
                end = start + effective_page_size
                corrupt = False
                with artifact.open("rb") as handle:
                    for index, raw_line in enumerate(handle):
                        digest.update(raw_line)
                        row_count += 1
                        if start <= index < end:
                            try:
                                line = raw_line.decode("utf-8")
                            except UnicodeDecodeError:
                                corrupt = True
                                break
                            try:
                                rows.append(json.loads(line))
                            except json.JSONDecodeError:
                                corrupt = True
                                break
                artifact_sha256 = f"sha256:{digest.hexdigest()}"
                expected_artifact_hash = str(catalog.get("artifact_sha256") or "")
                hash_matches = bool(expected_artifact_hash) and artifact_sha256 == expected_artifact_hash
                if corrupt or not hash_matches:
                    rows = []
                    unavailable_status = "corrupt"
                else:
                    available = True
            result.update(
                {
                    "result_id": result_id,
                    "page": effective_page,
                    "page_size": effective_page_size,
                    "row_count": row_count,
                    "has_next": effective_page * effective_page_size < row_count,
                    "rows": rows,
                }
            )
        else:
            data = {"messages": self.load_session(session_id)}
            wanted_id = str(raw_ref.get("tool_call_id") or descriptor.get("tool_call_id") or "")
            wanted_query_id = str(raw_ref.get("source_query_id") or descriptor.get("source_query_id") or "")
            wanted_run_id = str(descriptor.get("source_run_id") or "")
            wanted_hash = str(descriptor.get("source_hash") or "")
            for _, _, message, tool_call in self._iter_persisted_tool_calls(data):
                if str(tool_call.get("id") or "") != wanted_id:
                    continue
                if wanted_query_id and str(message.get("query_id") or "") != wanted_query_id:
                    continue
                if wanted_run_id and str(tool_call.get("source_run_id") or "") not in {"", wanted_run_id}:
                    continue
                text = self._tool_context_source(tool_call)
                text_hash = self._tool_context_source_hash(text) if text else ""
                if wanted_hash and text_hash != wanted_hash:
                    continue
                content = text[safe_offset : safe_offset + safe_limit]
                available = bool(text)
                artifact_sha256 = text_hash
                result.update(
                    {
                        "offset": safe_offset,
                        "limit": safe_limit,
                        "total_chars": len(text),
                        "has_more": safe_offset + safe_limit < len(text),
                    }
                )
                break

        if kind != "sql_query_result":
            result["content"] = content
        result["raw_result_available"] = available
        result["artifact_sha256"] = artifact_sha256 or None
        expected_hash = str(descriptor.get("source_hash") or "")
        hash_scope = str(raw_ref.get("source_hash_scope") or "raw_result")
        if (
            available
            and kind != "sql_query_result"
            and hash_scope in {"raw_result", "raw_bytes"}
            and expected_hash
            and (hash_matches is False or (hash_matches is None and artifact_sha256 != expected_hash))
        ):
            result["status"] = "hash_mismatch"
            result["hash_matches"] = False
            logger.warning(
                "Evidence hash mismatch session=%s evidence=%s expected=%s actual=%s",
                session_id,
                evidence_id,
                expected_hash,
                artifact_sha256,
            )
            emit_harness_metric(
                logger,
                "evidence_hash_mismatch_count",
                session_id=session_id,
                evidence_id=evidence_id,
            )
        elif available:
            result["hash_matches"] = (
                hash_matches
                if hash_matches is not None
                else (True if kind != "sql_query_result" and hash_scope in {"raw_result", "raw_bytes"} else None)
            )
        if not available:
            result["status"] = unavailable_status
            result["output_complete"] = False
            emit_harness_metric(
                logger,
                "evidence_missing_count",
                session_id=session_id,
                evidence_id=evidence_id,
                kind=kind,
            )
        return result

    def session_references_result_id(self, session_id: str, result_id: str) -> bool:
        """Whether a live Session Evidence ledger still owns a SQL artifact."""

        data = self._read_file(session_id)
        if not data:
            return False
        logical = deepcopy(data)
        logical["messages"] = deepcopy(self.load_session(session_id))
        self._ensure_evidence_metadata(session_id, logical)
        index = logical.get("evidence_index")
        if not isinstance(index, dict):
            return False
        return any(
            isinstance(item, dict)
            and isinstance(item.get("raw_output_ref"), dict)
            and item["raw_output_ref"].get("kind") == "sql_query_result"
            and str(item["raw_output_ref"].get("result_id") or "") == result_id
            for item in index.values()
        )

    def result_owner_tool_call(self, session_id: str, result_id: str) -> dict[str, str] | None:
        """Resolve one legacy SQL result to exactly one persisted ToolCall occurrence."""

        data = {"messages": self.load_session(session_id)}
        matches: list[dict[str, str]] = []
        for _, _, message, tool_call in self._iter_persisted_tool_calls(data):
            tool_name = str(tool_call.get("tool") or tool_call.get("name") or "")
            if tool_name not in {"database_sql_execute", "database_knowledge_query"}:
                continue
            tool_call_id = str(tool_call.get("id") or "")
            if tool_call_id and self._tool_context_result_id(self._tool_context_source(tool_call)) == result_id:
                matches.append(
                    {
                        "tool_call_id": tool_call_id,
                        "source_query_id": str(message.get("query_id") or ""),
                        "source_run_id": str(tool_call.get("source_run_id") or ""),
                        "source_hash": str(tool_call.get("source_hash") or ""),
                    }
                )
        return matches[0] if len(matches) == 1 else None

    def begin_tool_context_job(
        self,
        session_id: str,
        *,
        job_id: str,
        candidates: list[dict[str, Any]],
        policy_version: str,
        lease_timeout_seconds: int = 300,
    ) -> bool:
        """Mark a candidate snapshot pending and persist one session-scoped job."""

        if not candidates:
            return False
        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return False
            existing = data.get("tool_context_job")
            now = time.time()
            if isinstance(existing, dict) and existing.get("status") in {"pending", "running"}:
                updated_at = float(existing.get("updated_at") or existing.get("created_at") or 0)
                if now - updated_at < max(1, int(lease_timeout_seconds)):
                    return False
                existing["status"] = "expired"
                existing["error"] = "job_lease_expired"
                existing["updated_at"] = now
            before_ids = self._tool_call_ids(data)
            by_id = {str(item["tool_call_id"]): item for item in candidates}
            marked = 0
            for _, _, _, tool_call in self._iter_persisted_tool_calls(data):
                tool_call_id = str(tool_call.get("id") or "")
                candidate = by_id.get(tool_call_id)
                if not candidate:
                    continue
                if self._tool_context_source_hash(self._tool_context_source(tool_call)) != candidate["source_hash"]:
                    continue
                tool_call["raw_output_ref"] = dict(candidate["raw_output_ref"])
                tool_call["context_compaction"] = {
                    "status": "pending",
                    "source_hash": candidate["source_hash"],
                    "policy_version": policy_version,
                    "job_id": job_id,
                    "updated_at": now,
                }
                marked += 1
            after_ids = self._tool_call_ids(data)
            if before_ids != after_ids or marked == 0:
                return False
            data["tool_context_job"] = {
                "id": job_id,
                "status": "pending",
                "policy_version": policy_version,
                "candidate_count": marked,
                "completed_count": 0,
                "failed_count": 0,
                "created_at": now,
                "updated_at": now,
                "base_revision": int(data.get("tool_context_revision", 0) or 0),
            }
            self._write_file(session_id, data)
            return True

    def update_tool_context_job(
        self,
        session_id: str,
        job_id: str,
        *,
        status: str,
        completed_count: int | None = None,
        failed_count: int | None = None,
        metrics: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            job = data.get("tool_context_job") if data else None
            if not isinstance(job, dict) or job.get("id") != job_id:
                return False
            job["status"] = status
            job["updated_at"] = time.time()
            if completed_count is not None:
                job["completed_count"] = int(completed_count)
            if failed_count is not None:
                job["failed_count"] = int(failed_count)
            if metrics is not None:
                job["metrics"] = dict(metrics)
            if error:
                job["error"] = error
            self._write_file(session_id, data)
            return True

    def fail_unresolved_tool_context_candidates(
        self,
        session_id: str,
        job_id: str,
        *,
        reason: str,
    ) -> int:
        """Release pending compaction leases while retaining raw Tool output."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return 0
            failed = 0
            for _, _, _, tool_call in self._iter_persisted_tool_calls(data):
                metadata = tool_call.get("context_compaction")
                if not isinstance(metadata, dict) or metadata.get("job_id") != job_id:
                    continue
                if metadata.get("status") not in {"pending", "running"}:
                    continue
                metadata["status"] = "failed"
                metadata["error"] = reason
                metadata["updated_at"] = time.time()
                failed += 1
            if failed:
                self._write_file(session_id, data)
            return failed

    def complete_tool_context_compaction(
        self,
        session_id: str,
        *,
        job_id: str,
        tool_call_id: str,
        source_hash: str,
        policy_version: str,
        context_output: str,
        method: str,
        context_profile: str = "detailed",
    ) -> bool:
        """CAS one result to ready without changing output or tool_call_id."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return False
            before_ids = self._tool_call_ids(data)
            nonempty_ids = [item for item in before_ids if item]
            if len(nonempty_ids) != len(before_ids) or len(nonempty_ids) != len(set(nonempty_ids)):
                return False
            updated = False
            stale = False
            for _, _, _, tool_call in self._iter_persisted_tool_calls(data):
                if str(tool_call.get("id") or "") != tool_call_id:
                    continue
                current_hash = str(tool_call.get("source_hash") or "") or self._tool_context_source_hash(
                    self._tool_context_source(tool_call)
                )
                metadata = tool_call.get("context_compaction")
                if current_hash != source_hash or not isinstance(metadata, dict) or metadata.get("job_id") != job_id:
                    if isinstance(metadata, dict):
                        metadata["status"] = "stale"
                        metadata["updated_at"] = time.time()
                        stale = True
                    break
                tool_call["context_output"] = context_output
                tool_call["context_compaction"] = {
                    "status": "ready",
                    "source_hash": source_hash,
                    "policy_version": policy_version,
                    "method": method,
                    "context_profile": context_profile,
                    "projection_version": "evidence-projection-v1",
                    "job_id": job_id,
                    "compacted_at": time.time(),
                }
                updated = True
                break
            after_ids = self._tool_call_ids(data)
            if before_ids != after_ids:
                return False
            if updated:
                data["tool_context_revision"] = int(data.get("tool_context_revision", 0) or 0) + 1
                self._write_file(session_id, data)
            elif stale:
                self._write_file(session_id, data)
            return updated

    def persist_immediate_tool_context(
        self,
        session_id: str,
        *,
        tool_call_id: str,
        source_output: str,
        context_output: str,
        method: str,
        policy_version: str,
        context_profile: str = "detailed",
    ) -> bool:
        """Attach current-turn immediate compaction after the visible result is persisted."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return False
            source_hash = self._tool_context_source_hash(source_output)
            before_ids = self._tool_call_ids(data)
            nonempty_ids = [item for item in before_ids if item]
            if len(nonempty_ids) != len(before_ids) or len(nonempty_ids) != len(set(nonempty_ids)):
                return False
            for _, _, _, tool_call in self._iter_persisted_tool_calls(data):
                if str(tool_call.get("id") or "") != tool_call_id:
                    continue
                if self._tool_context_source_hash(self._tool_context_source(tool_call)) != source_hash:
                    return False
                tool_call["context_output"] = context_output
                tool_call["raw_output_ref"] = self._tool_context_raw_ref(
                    session_id, tool_call_id, source_output, source_hash
                )
                tool_call["context_compaction"] = {
                    "status": "ready",
                    "source_hash": source_hash,
                    "policy_version": policy_version,
                    "method": method,
                    "context_profile": context_profile,
                    "projection_version": "evidence-projection-v1",
                    "compacted_at": time.time(),
                }
                if before_ids != self._tool_call_ids(data):
                    return False
                data["tool_context_revision"] = int(data.get("tool_context_revision", 0) or 0) + 1
                self._write_file(session_id, data)
                return True
            return False

    def get_ready_tool_context_outputs(self, session_id: str) -> dict[str, str]:
        """Return only valid ready outputs; pending/running entries stay raw."""

        entries = self.get_ready_tool_context_entries(session_id)
        return {
            tool_call_id: str(candidates[0]["context_output"])
            for tool_call_id, candidates in entries.items()
            if len(candidates) == 1
        }

    def get_ready_tool_context_entries(self, session_id: str) -> dict[str, list[dict[str, str]]]:
        """Return ready outputs with Run and source identity for safe matching."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return {}
            ready: dict[str, list[dict[str, str]]] = {}
            for _, _, message, tool_call in self._iter_persisted_tool_calls(data):
                tool_call_id = str(tool_call.get("id") or "")
                context_output = tool_call.get("context_output")
                metadata = tool_call.get("context_compaction")
                if not tool_call_id or not context_output or not isinstance(metadata, dict):
                    continue
                if metadata.get("status") != "ready":
                    continue
                current_hash = self._tool_context_source_hash(self._tool_context_source(tool_call))
                if metadata.get("source_hash") != current_hash:
                    continue
                ready.setdefault(tool_call_id, []).append(
                    {
                        "context_output": str(context_output),
                        "source_hash": current_hash,
                        "query_id": str(message.get("query_id") or ""),
                    }
                )
            return ready

    def get_tool_context_status(self, session_id: str) -> dict[str, Any]:
        data = self._read_file(session_id)
        job = data.get("tool_context_job") if data else None
        if not isinstance(job, dict):
            return {"status": "idle", "revision": int(data.get("tool_context_revision", 0) or 0) if data else 0}
        return {**job, "revision": int(data.get("tool_context_revision", 0) or 0)}

    @_session_write_locked
    def update_context_usage_peak(self, session_id: str, used_tokens: int) -> None:
        """更新 session 的 context_usage_peak（运行时 token 用量峰值）。"""
        data = self._read_file(session_id)
        if not data:
            return
        current_peak = data.get("context_usage_peak", 0)
        if used_tokens > current_peak:
            data["context_usage_peak"] = used_tokens
            self._write_file(session_id, data)

    def get_context_usage_peak(self, session_id: str) -> int:
        """获取 session 的 context_usage_peak；不存在返回 0。"""
        data = self._read_file(session_id)
        if not data:
            return 0
        return data.get("context_usage_peak", 0) or 0

    @_session_write_locked
    def update_agent_context_usage(self, session_id: str, used_tokens: int) -> None:
        """Persist the current effective Agent context (not its historical peak)."""
        data = self._read_file(session_id)
        if not data:
            return
        data["agent_context_usage"] = max(0, int(used_tokens))
        self._write_file(session_id, data)

    def get_agent_context_usage(self, session_id: str) -> int:
        """Return the latest effective Agent context size."""
        data = self._read_file(session_id)
        if not data:
            return 0
        return int(data.get("agent_context_usage", 0) or 0)

    @staticmethod
    def _run_agent_context_record(data: dict[str, Any]) -> dict[str, Any] | None:
        """Read the current Run snapshot, including the legacy flat shape."""

        record = data.get("run_agent_context")
        if isinstance(record, dict) and isinstance(record.get("messages"), list):
            return record
        legacy_messages = data.get("agent_context_messages")
        if not isinstance(legacy_messages, list):
            return None
        return {
            "run_id": data.get("agent_context_run_id"),
            "messages": legacy_messages,
            "updated_at": data.get("updated_at"),
        }

    def get_effective_agent_context_usage(
        self,
        session_id: str,
        *,
        use_tool_context: bool,
    ) -> int:
        """Estimate the current model context after ready Tool Context replacements.

        The saved Agent context remains lossless so disabling the middleware can
        immediately restore raw ToolMessage content. For the context meter only,
        apply the same ready-only replacement delta that the middleware applies
        before the model call.
        """

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return 0
            usage = int(data.get("agent_context_usage", 0) or 0)
            if not use_tool_context or usage <= 0:
                return usage

            ready: dict[str, str] = {}
            for _, _, _, tool_call in self._iter_persisted_tool_calls(data):
                tool_call_id = str(tool_call.get("id") or "")
                context_output = tool_call.get("context_output")
                metadata = tool_call.get("context_compaction")
                if not tool_call_id or not context_output or not isinstance(metadata, dict):
                    continue
                if metadata.get("status") != "ready":
                    continue
                source_hash = self._tool_context_source_hash(self._tool_context_source(tool_call))
                if metadata.get("source_hash") == source_hash:
                    ready[tool_call_id] = str(context_output)
            if not ready:
                return usage

            delta = 0
            run_context = self._run_agent_context_record(data)
            messages = run_context.get("messages") if run_context else None
            if not isinstance(messages, list):
                return usage
            for message in messages:
                if not isinstance(message, dict) or message.get("type") != "tool":
                    continue
                payload = message.get("data")
                if not isinstance(payload, dict):
                    continue
                tool_call_id = str(payload.get("tool_call_id") or "")
                replacement = ready.get(tool_call_id)
                if replacement is None:
                    continue
                content = payload.get("content", "")
                if isinstance(content, str):
                    original = content
                else:
                    original = json.dumps(content, ensure_ascii=False, default=str)
                delta += self._tool_context_tokens(replacement) - self._tool_context_tokens(original)
            return max(0, usage + delta)

    @_session_write_locked
    def update_agent_context_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        run_id: str | None = None,
    ) -> None:
        """Persist the current Run's model execution snapshot."""
        data = self._read_file(session_id)
        if not data:
            return
        data["run_agent_context"] = {
            "run_id": run_id,
            "messages": messages,
            "updated_at": time.time(),
        }
        data.pop("agent_context_messages", None)
        data.pop("agent_context_run_id", None)
        self._write_file(session_id, data)

    def get_agent_context_messages(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load a model execution snapshot only for its source Run."""
        data = self._read_file(session_id)
        if not data:
            return []
        record = self._run_agent_context_record(data)
        if record is None:
            return []
        if run_id is not None and record.get("run_id") != run_id:
            return []
        messages = record.get("messages")
        if not isinstance(messages, list):
            return []
        return [item for item in messages if isinstance(item, dict)]

    @_session_write_locked
    def update_session_summary_projection(
        self,
        session_id: str,
        *,
        summary_text: str,
        recent_messages: list[dict[str, Any]],
        transcript_boundary: dict[str, Any],
        source_run_id: str,
        history_ref: str = "",
        tokens_after: int = 0,
    ) -> None:
        """Persist the completed DeepAgents Summary projection across Runs."""

        normalized_summary = str(summary_text or "").strip()
        source_query_id = str(transcript_boundary.get("source_query_id") or "")
        if not normalized_summary or not source_query_id:
            raise ValueError("Session summary projection requires summary_text and source_query_id")
        data = self._read_file(session_id)
        if not data:
            return
        data["session_summary_projection"] = {
            "schema_version": 1,
            "status": "completed",
            "summary_text": normalized_summary,
            "recent_messages": [item for item in recent_messages if isinstance(item, dict)],
            "transcript_boundary": {
                "source_query_id": source_query_id,
                "message_count": max(0, int(transcript_boundary.get("message_count") or 0)),
            },
            "source_run_id": str(source_run_id or ""),
            "history_ref": str(history_ref or ""),
            "tokens_after": max(0, int(tokens_after or 0)),
            "created_at": time.time(),
        }
        self._write_file(session_id, data)

    @_session_write_locked
    def begin_agent_context_compaction(
        self,
        session_id: str,
        *,
        operation_id: str,
        focus: str = "",
    ) -> dict[str, Any]:
        """Claim one idle Agent Session for manual context compaction."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        if str(data.get("runtime_mode") or "") != "agent":
            raise ValueError("Manual /compact is available only for Agent sessions")

        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        terminal_statuses = {
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }
        active_run = next(
            (
                run
                for run in (runs.values() if isinstance(runs, dict) else ())
                if isinstance(run, dict) and str(run.get("status") or "") not in terminal_statuses
            ),
            None,
        )
        if active_run is not None:
            raise RuntimeError(
                f"Session {session_id} has active Run {active_run.get('run_id')}; compact at a safe boundary"
            )

        tool_context_job = data.get("tool_context_job")
        if isinstance(tool_context_job, dict) and str(tool_context_job.get("status") or "") in {
            "queued",
            "running",
        }:
            raise RuntimeError("Tool context maintenance is still running; retry /compact when it finishes")

        now = time.time()
        previous = data.get("agent_context_compaction")
        if self._agent_context_compaction_is_active(previous, now=now):
            raise RuntimeError("Agent context compaction is already running for this Session")
        if isinstance(previous, dict) and previous.get("status") == "running":
            previous["status"] = "expired"
            previous["completed_at"] = now
            previous["error"] = "maintenance claim expired"

        messages = data.get("messages")
        transcript = messages if isinstance(messages, list) else []
        last_message = next(
            (item for item in reversed(transcript) if isinstance(item, dict) and item.get("role") != "system"),
            None,
        )
        if (
            not isinstance(last_message, dict)
            or last_message.get("role") != "assistant"
            or last_message.get("status") != "completed"
        ):
            raise ValueError("Manual /compact requires a completed Assistant turn")
        source_query_id = str(last_message.get("query_id") or "")
        if not source_query_id:
            raise ValueError("The latest Assistant turn has no stable query boundary")
        latest_run_id = str(harness.get("latest_run_id") or "") if isinstance(harness, dict) else ""

        claim = {
            "operation_id": str(operation_id),
            "status": "running",
            "trigger": "manual",
            "focus": str(focus or ""),
            "source_query_id": source_query_id,
            "source_run_id": latest_run_id,
            "message_count": len(transcript),
            "transcript_sha256": self._agent_context_transcript_fingerprint(transcript),
            "started_at": now,
        }
        data["agent_context_compaction"] = claim
        self._write_file(session_id, data)
        return deepcopy(claim)

    @_session_write_locked
    def complete_agent_context_compaction(
        self,
        session_id: str,
        *,
        operation_id: str,
        summary_text: str,
        recent_messages: list[dict[str, Any]],
        effective_messages: list[dict[str, Any]],
        tokens_before: int,
        tokens_after: int,
        summarized_message_count: int,
        kept_recent_message_count: int,
        summary_model: str = "",
    ) -> dict[str, Any]:
        """Commit a manual compact projection only if its transcript claim is unchanged."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        claim = data.get("agent_context_compaction")
        if (
            not isinstance(claim, dict)
            or claim.get("status") != "running"
            or str(claim.get("operation_id") or "") != str(operation_id)
        ):
            raise RuntimeError("Agent context compaction claim is no longer active")

        messages = data.get("messages")
        transcript = messages if isinstance(messages, list) else []
        last_message = next(
            (item for item in reversed(transcript) if isinstance(item, dict) and item.get("role") != "system"),
            None,
        )
        if (
            len(transcript) != int(claim.get("message_count") or 0)
            or not isinstance(last_message, dict)
            or last_message.get("role") != "assistant"
            or last_message.get("status") != "completed"
            or str(last_message.get("query_id") or "") != str(claim.get("source_query_id") or "")
            or self._agent_context_transcript_fingerprint(transcript)
            != str(claim.get("transcript_sha256") or "")
        ):
            raise RuntimeError("Session transcript changed while /compact was running; projection was not committed")

        normalized_summary = str(summary_text or "").strip()
        if not normalized_summary:
            raise ValueError("Agent context summary is empty")
        now = time.time()
        projection = {
            "schema_version": 2,
            "status": "completed",
            "summary_text": normalized_summary,
            "recent_messages": [item for item in recent_messages if isinstance(item, dict)],
            "transcript_boundary": {
                "source_query_id": str(claim.get("source_query_id") or ""),
                "message_count": len(transcript),
            },
            "source_run_id": str(claim.get("source_run_id") or ""),
            "history_ref": "",
            "trigger": "manual",
            "focus": str(claim.get("focus") or ""),
            "tokens_before": max(0, int(tokens_before)),
            "tokens_after": max(0, int(tokens_after)),
            "created_at": now,
        }
        data["session_summary_projection"] = projection
        data["run_agent_context"] = {
            "run_id": str(claim.get("source_run_id") or ""),
            "messages": [item for item in effective_messages if isinstance(item, dict)],
            "updated_at": now,
        }
        data["agent_context_usage"] = max(0, int(tokens_after))
        completed = {
            **claim,
            "status": "completed",
            "tokens_before": max(0, int(tokens_before)),
            "tokens_after": max(0, int(tokens_after)),
            "summarized_message_count": max(0, int(summarized_message_count)),
            "kept_recent_message_count": max(0, int(kept_recent_message_count)),
            "summary_model": str(summary_model or ""),
            "completed_at": now,
        }
        data["agent_context_compaction"] = completed
        history = data.setdefault("agent_context_compactions", [])
        if isinstance(history, list):
            history.append(deepcopy(completed))
            del history[:-20]
        self._write_file(session_id, data)
        return deepcopy(completed)

    @_session_write_locked
    def fail_agent_context_compaction(
        self,
        session_id: str,
        *,
        operation_id: str,
        error: str,
    ) -> None:
        """Release a matching compaction claim without changing the last good projection."""

        data = self._read_file(session_id)
        claim = data.get("agent_context_compaction") if data else None
        if (
            not isinstance(claim, dict)
            or claim.get("status") != "running"
            or str(claim.get("operation_id") or "") != str(operation_id)
        ):
            return
        claim["status"] = "failed"
        claim["error"] = str(error or "Agent context compaction failed")[:1000]
        claim["completed_at"] = time.time()
        self._write_file(session_id, data)

    @_session_write_locked
    def get_agent_context_compaction_status(
        self,
        session_id: str,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return one persisted manual compaction operation, expiring stale claims."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        record = data.get("agent_context_compaction")
        if not isinstance(record, dict):
            return None
        if operation_id and str(record.get("operation_id") or "") != str(operation_id):
            history = data.get("agent_context_compactions")
            record = next(
                (
                    item
                    for item in reversed(history if isinstance(history, list) else [])
                    if isinstance(item, dict)
                    and str(item.get("operation_id") or "") == str(operation_id)
                ),
                None,
            )
            if not isinstance(record, dict):
                return None
        if self._agent_context_compaction_is_active(record):
            return deepcopy(record)
        if record.get("status") == "running":
            record["status"] = "expired"
            record["error"] = "Agent context compaction exceeded its maintenance lease"
            record["completed_at"] = time.time()
            if data.get("agent_context_compaction") is record:
                self._write_file(session_id, data)
        return deepcopy(record)

    def get_session_summary_projection(self, session_id: str) -> dict[str, Any] | None:
        """Return the latest completed cross-Run Summary projection."""

        data = self._read_file(session_id)
        projection = data.get("session_summary_projection") if data else None
        if not isinstance(projection, dict) or projection.get("status") != "completed":
            return None
        if not str(projection.get("summary_text") or "").strip():
            return None
        if not isinstance(projection.get("recent_messages"), list):
            return None
        boundary = projection.get("transcript_boundary")
        if not isinstance(boundary, dict) or not str(boundary.get("source_query_id") or ""):
            return None
        return deepcopy(projection)

    @_session_write_locked
    def register_delivered_artifact(
        self,
        session_id: str,
        *,
        target_path: str,
        content_sha256: str,
        source_run_id: str,
        source_query_id: str,
        source_goal_id: str | None = None,
        source_goal_revision: int | None = None,
        related_artifact_ids: list[str] | None = None,
        contract_ids: list[str] | None = None,
        validation_receipt_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist the formal target/hash produced by a successful commit.

        The artifact id is stable for the formal target while each content
        version gets an immutable delivery receipt in the history ledger.
        Scratch paths are execution state and are deliberately rejected.
        """

        from harness.models import DeliveredArtifact

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        normalized_target = str(Path(target_path).expanduser().resolve())
        if normalized_target.startswith("/scratch/"):
            raise ValueError("delivered artifacts require a formal non-scratch target")
        if not str(content_sha256).startswith("sha256:"):
            raise ValueError("delivered artifacts require content_sha256")
        artifact_id = "artifact-" + hashlib.sha256(f"external\0{normalized_target}".encode()).hexdigest()[:20]
        delivery_receipt_id = "delivery-" + hashlib.sha256(f"{artifact_id}\0{content_sha256}".encode()).hexdigest()[:20]
        now = time.time()
        registry = data.setdefault("delivered_artifacts", {})
        previous = registry.get(artifact_id)
        created_at = float(previous.get("created_at") or now) if isinstance(previous, dict) else now
        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        source_run = runs.get(source_run_id) if isinstance(runs, dict) else None
        source_skill_ids = sorted(
            {
                str(item.get("skill_id") or "")
                for item in (source_run.get("skill_activations") if isinstance(source_run, dict) else [])
                if isinstance(item, dict) and str(item.get("skill_id") or "")
            }
        )
        requested_validation_ids = {str(item) for item in (validation_receipt_ids or []) if str(item)}
        receipt_refs: list[dict[str, Any]] = []
        for activation in (source_run.get("verification_activations") if isinstance(source_run, dict) else []) or []:
            if isinstance(activation, dict):
                receipt_refs.extend(
                    ref
                    for ref in activation.get("evidence_refs") or []
                    if isinstance(ref, dict) and ref.get("kind") == "validation_receipt"
                )
        goals = harness.get("goals") if isinstance(harness, dict) else None
        source_goal = goals.get(source_goal_id) if isinstance(goals, dict) and source_goal_id else None
        if isinstance(source_goal, dict) and source_goal.get("objective_revision") == source_goal_revision:
            receipt_refs.extend(
                ref
                for ref in source_goal.get("evidence_refs") or []
                if isinstance(ref, dict) and ref.get("kind") == "validation_receipt"
            )

        def receipt_matches_delivery(ref: dict[str, Any]) -> bool:
            if (
                str(ref.get("validation_receipt_id") or "") not in requested_validation_ids
                or not bool(ref.get("commit_authority"))
                or str(ref.get("status") or "passed") != "passed"
                or int(ref.get("exit_code", -1)) != 0
                or int(ref.get("checks_failed") or 0) != 0
            ):
                return False
            return any(
                isinstance(item, dict)
                and str(Path(str(item.get("path") or "")).expanduser().resolve()) == normalized_target
                and str(item.get("content_sha256") or "") == content_sha256
                for item in ref.get("artifact_refs") or []
            )

        accepted_receipts = [ref for ref in receipt_refs if receipt_matches_delivery(ref)]
        selected_validation_ids = {
            str(ref.get("validation_receipt_id") or "")
            for ref in accepted_receipts
            if str(ref.get("validation_receipt_id") or "")
        }
        inferred_contract_ids = {
            str(ref.get("validator_version") or "")
            for ref in accepted_receipts
            if str(ref.get("validator_kind") or "") == "artifact_ui_contract"
            and str(ref.get("validator_version") or "")
        }
        inferred_related_ids = {
            "artifact-"
            + hashlib.sha256(
                f"external\0{str(Path(str(item.get('path') or '')).expanduser().resolve())}".encode()
            ).hexdigest()[:20]
            for ref in accepted_receipts
            if str(ref.get("validator_kind") or "") == "artifact_ui_contract"
            for item in ref.get("artifact_refs") or []
            if isinstance(item, dict)
            and str(item.get("path") or "")
            and str(Path(str(item.get("path") or "")).expanduser().resolve()) != normalized_target
        }
        selected_related_ids = inferred_related_ids | {
            str(item) for item in (related_artifact_ids or []) if str(item) and str(item) != artifact_id
        }
        payload = DeliveredArtifact(
            artifact_id=artifact_id,
            target_path=normalized_target,
            content_sha256=content_sha256,
            delivery_receipt_id=delivery_receipt_id,
            status="active",
            related_artifact_ids=sorted(selected_related_ids),
            contract_ids=sorted(
                {
                    *inferred_contract_ids,
                    *(str(item) for item in (contract_ids or []) if str(item)),
                }
            ),
            validation_receipt_ids=sorted(selected_validation_ids),
            source_skill_ids=source_skill_ids,
            source_run_id=source_run_id,
            source_query_id=source_query_id,
            source_goal_id=source_goal_id,
            source_goal_revision=source_goal_revision,
            created_at=created_at,
            updated_at=now,
        ).model_dump(mode="json")
        registry[artifact_id] = payload
        history = data.setdefault("artifact_delivery_history", [])
        if not any(
            isinstance(item, dict) and item.get("delivery_receipt_id") == delivery_receipt_id for item in history
        ):
            history.append(deepcopy(payload))
            if len(history) > 500:
                del history[:-500]
        self._write_file(session_id, data)
        return deepcopy(payload)

    @_session_write_locked
    def mark_delivered_artifact_deleted(
        self,
        session_id: str,
        *,
        target_path: str,
        source_run_id: str,
        source_query_id: str,
    ) -> dict[str, Any] | None:
        """Tombstone a formally delivered target removed by a committed plan."""

        data = self._read_file(session_id)
        registry = data.get("delivered_artifacts") if data else None
        if not isinstance(registry, dict):
            return None
        normalized_target = str(Path(target_path).expanduser().resolve())
        artifact_id = "artifact-" + hashlib.sha256(f"external\0{normalized_target}".encode()).hexdigest()[:20]
        current = registry.get(artifact_id)
        if not isinstance(current, dict):
            return None
        tombstone = dict(current)
        now = time.time()
        tombstone.update(
            {
                "status": "deleted",
                "deleted_at": now,
                "stale_reason": "deleted_by_committed_directory_plan",
                "source_run_id": source_run_id,
                "source_query_id": source_query_id,
                "updated_at": now,
            }
        )
        registry[artifact_id] = tombstone
        self._write_file(session_id, data)
        return deepcopy(tombstone)

    @_session_write_locked
    def restore_delivered_artifact_registry_entries(
        self,
        session_id: str,
        entries: dict[str, dict[str, Any] | None],
    ) -> None:
        """Restore registry heads after a failed cross-store directory commit."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        registry = data.setdefault("delivered_artifacts", {})
        for artifact_id, previous in entries.items():
            if isinstance(previous, dict):
                registry[artifact_id] = deepcopy(previous)
            else:
                registry.pop(artifact_id, None)
        self._write_file(session_id, data)

    @staticmethod
    def _fresh_artifact_view(item: dict[str, Any]) -> dict[str, Any]:
        """Return current target freshness without mutating registry history."""

        view = deepcopy(item)
        if str(view.get("status") or "active") != "active":
            return view
        target = Path(str(view.get("target_path") or "")).expanduser()
        try:
            if not target.is_file():
                view.update(status="stale", stale_reason="target_missing")
                return view
            digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            view.update(status="stale", stale_reason=f"target_unreadable:{type(exc).__name__}")
            return view
        if digest != str(view.get("content_sha256") or ""):
            view.update(
                status="stale",
                stale_reason="target_hash_mismatch",
                observed_content_sha256=digest,
            )
        return view

    def list_delivered_artifacts(
        self,
        session_id: str,
        *,
        verify_freshness: bool = False,
        include_inactive: bool = True,
    ) -> list[dict[str, Any]]:
        data = self._read_file(session_id)
        registry = data.get("delivered_artifacts") if data else None
        if not isinstance(registry, dict):
            return []
        artifacts = [deepcopy(item) for item in registry.values() if isinstance(item, dict)]
        if verify_freshness:
            artifacts = [self._fresh_artifact_view(item) for item in artifacts]
        if not include_inactive:
            artifacts = [item for item in artifacts if str(item.get("status") or "active") == "active"]
        artifacts.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
        return artifacts

    def resolve_follow_up_artifacts(
        self,
        session_id: str,
        objective: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Conservatively relate a standalone prompt to recent formal artifacts.

        This is continuity resolution, not task/Skill classification. An
        explicit path/name match wins; otherwise only clear follow-up language
        may select the latest delivered group.
        """

        registered_artifacts = self.list_delivered_artifacts(session_id)
        artifacts = self.list_delivered_artifacts(
            session_id,
            verify_freshness=True,
            include_inactive=False,
        )
        if not artifacts:
            if registered_artifacts:
                emit_harness_metric(
                    logger,
                    "artifact_handoff_stale_ref_count",
                    session_id=session_id,
                    value=len(registered_artifacts),
                )
            return []
        text = str(objective or "").strip().lower()
        explicit: list[dict[str, Any]] = []
        for item in artifacts:
            target = str(item.get("target_path") or "").strip().lower()
            name = Path(target).name if target else ""
            if target and (target in text or (name and name in text)):
                explicit.append(item)
        if explicit:
            selected_ids = {str(item.get("artifact_id") or "") for item in explicit}
            for item in explicit:
                selected_ids.update(str(value) for value in item.get("related_artifact_ids") or [])
            resolved = [item for item in artifacts if str(item.get("artifact_id") or "") in selected_ids][
                : max(1, limit)
            ]
            emit_harness_metric(
                logger,
                "artifact_handoff_hit_count",
                session_id=session_id,
                value=len(resolved),
                route="explicit",
            )
            return resolved
        repair_follow_up = re.search(
            r"(?:继续(?:修改|修复|更新|补充)|再试|再来|还是(?:没有|没|不对)|仍然(?:没有|没|不对)|还没|没有更新|没更新|补上|修复(?:这个|该)|(?:这个|刚才|上一轮).*(?:产物|文件|报告|图表|页面|代码).*(?:不对|有误|没更新|修复))",
            text,
        )
        read_only_follow_up = re.search(
            r"(?:(?:html|页面|报告|图表|javascript|js).*(?:哪个|哪一个|用的|引用|来自|数据|配置率|是多少)|(?:哪个|哪一个|用的|引用|来自|数据|配置率|是多少).*(?:html|页面|报告|图表|javascript|js))",
            text,
        )
        if not repair_follow_up and not read_only_follow_up:
            return []
        latest = artifacts[0]
        data = self._read_file(session_id)
        latest_assistant_query_id = next(
            (
                str(message.get("query_id") or "")
                for message in reversed(data.get("messages") or [])
                if isinstance(message, dict)
                and message.get("role") == "assistant"
                and str(message.get("query_id") or "")
            ),
            "",
        )
        if latest_assistant_query_id != str(latest.get("source_query_id") or ""):
            return []
        source_run_id = str(latest.get("source_run_id") or "")
        source_goal_id = str(latest.get("source_goal_id") or "")
        resolved = [
            item
            for item in artifacts
            if (source_run_id and str(item.get("source_run_id") or "") == source_run_id)
            or (
                source_goal_id
                and str(item.get("source_goal_id") or "") == source_goal_id
                and item.get("source_goal_revision") == latest.get("source_goal_revision")
            )
        ][: max(1, limit)]
        if resolved:
            emit_harness_metric(
                logger,
                "artifact_handoff_hit_count",
                session_id=session_id,
                value=len(resolved),
                route="deictic",
            )
        return resolved

    @_session_write_locked
    def upsert_external_artifact_lease(
        self,
        session_id: str,
        lease: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one external-artifact staging lease in Session JSON."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        lease_id = str(lease.get("lease_id") or "")
        if not lease_id:
            raise ValueError("external artifact lease requires lease_id")
        leases = data.setdefault("external_artifact_leases", {})
        leases[lease_id] = deepcopy(lease)
        self._write_file(session_id, data)
        return deepcopy(leases[lease_id])

    def get_external_artifact_lease(
        self,
        session_id: str,
        lease_id: str,
    ) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        leases = data.get("external_artifact_leases") if data else None
        lease = leases.get(lease_id) if isinstance(leases, dict) else None
        return deepcopy(lease) if isinstance(lease, dict) else None

    def list_external_artifact_leases(self, session_id: str) -> list[dict[str, Any]]:
        """Return exact-file lease control state without hydrating message history."""

        data = self._read_file(session_id)
        leases = data.get("external_artifact_leases") if data else None
        if not isinstance(leases, dict):
            return []
        return [deepcopy(lease) for lease in leases.values() if isinstance(lease, dict)]

    def find_staged_external_artifact_lease(
        self,
        session_id: str,
        *,
        run_id: str,
        query_id: str,
        target_path: str,
        goal_id: str = "",
        goal_revision: Any = None,
    ) -> dict[str, Any] | None:
        """Return the active exact-file lease already owned by this Run.

        A model may repeat the staging call after compaction or a tool-routing
        correction. Reusing the active lease preserves staged edits and avoids
        creating multiple scratch paths for the same authoritative target.
        """

        data = self._read_file(session_id)
        leases = data.get("external_artifact_leases") if data else None
        if not isinstance(leases, dict):
            return None

        def same_owner(lease: dict[str, Any]) -> bool:
            return (
                bool(goal_id)
                and str(lease.get("goal_id") or "") == goal_id
                and lease.get("goal_revision") == goal_revision
            ) or (
                not goal_id
                and not str(lease.get("goal_id") or "")
                and str(lease.get("run_id") or "") == run_id
                and str(lease.get("query_id") or "") == query_id
            )

        owned_target_leases = [
            lease
            for lease in leases.values()
            if isinstance(lease, dict) and str(lease.get("target_path") or "") == target_path and same_owner(lease)
        ]
        # A successful commit supersedes every older draft for the same Goal
        # revision and target.  Without this boundary, a stale pre-commit lease
        # can block a later validation/restage with an obsolete source hash.
        latest_commit_at = max(
            (
                float(lease.get("committed_at") or lease.get("created_at") or 0)
                for lease in owned_target_leases
                if lease.get("status") == "committed"
            ),
            default=0.0,
        )
        matches = [
            lease
            for lease in owned_target_leases
            if lease.get("status") == "staged" and float(lease.get("created_at") or 0) > latest_commit_at
        ]
        if not matches:
            return None
        matches.sort(key=lambda lease: float(lease.get("created_at") or 0), reverse=True)
        return deepcopy(matches[0])

    @_session_write_locked
    def upsert_external_directory_lease(
        self,
        session_id: str,
        lease: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one Run-scoped external-directory snapshot lease."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        lease_id = str(lease.get("lease_id") or "")
        if not lease_id:
            raise ValueError("external directory lease requires lease_id")
        leases = data.setdefault("external_directory_leases", {})
        leases[lease_id] = deepcopy(lease)
        self._write_file(session_id, data)
        return deepcopy(leases[lease_id])

    def get_external_directory_lease(
        self,
        session_id: str,
        lease_id: str,
    ) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        leases = data.get("external_directory_leases") if data else None
        lease = leases.get(lease_id) if isinstance(leases, dict) else None
        return deepcopy(lease) if isinstance(lease, dict) else None

    def list_external_directory_leases(self, session_id: str) -> list[dict[str, Any]]:
        """Return directory lease control state without hydrating message history."""

        data = self._read_file(session_id)
        leases = data.get("external_directory_leases") if data else None
        if not isinstance(leases, dict):
            return []
        return [deepcopy(lease) for lease in leases.values() if isinstance(lease, dict)]

    @_session_write_locked
    def record_legacy_external_lease_tool_use(
        self,
        session_id: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a bounded compatibility-use journal for lease retirement.

        Metrics and Trace spans are useful operationally but are not a durable
        migration authority. This counter lets release audits prove that no
        new Agent-facing lease calls occurred before schemas are removed.
        """

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        control = data.setdefault("legacy_external_lease_compatibility", {})
        usage = control.setdefault("usage", [])
        saved = {
            **deepcopy(record),
            "used_at": float(record.get("used_at") or time.time()),
        }
        usage.append(saved)
        if len(usage) > 500:
            del usage[:-500]
        control["total_call_count"] = int(control.get("total_call_count") or 0) + 1
        control["last_used_at"] = saved["used_at"]
        control["source_code_retained"] = True
        control["minimum_zero_call_release_cycles"] = 2
        self._write_file(session_id, data)
        return deepcopy(saved)

    @_session_write_locked
    def audit_legacy_external_leases(
        self,
        session_id: str,
        *,
        migrate: bool = True,
        release_id: str | None = None,
    ) -> dict[str, Any]:
        """Audit and safely close orphaned pre-Broker draft leases.

        Known terminal owners and expired drafts cannot be resumed, so they
        are marked ``abandoned``. Unknown owners are preserved: an old Session
        may still need checkpoint migration and must never lose recoverable
        state merely because its historical schema lacked Run metadata.
        """

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        now = time.time()
        harness = data.get("harness") if isinstance(data.get("harness"), dict) else {}
        runs = harness.get("runs") if isinstance(harness.get("runs"), dict) else {}
        goals = harness.get("goals") if isinstance(harness.get("goals"), dict) else {}
        terminal = {
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }
        active_statuses = {"claiming", "staged", "prepared", "committing", "publishing"}
        migrated: list[str] = []
        active: list[dict[str, Any]] = []
        for collection_name, kind in (
            ("external_artifact_leases", "exact_file"),
            ("external_directory_leases", "exact_directory"),
        ):
            collection = data.get(collection_name)
            if not isinstance(collection, dict):
                continue
            for lease in collection.values():
                if not isinstance(lease, dict) or str(lease.get("status") or "") not in active_statuses:
                    continue
                owner_terminal = False
                owner_known = False
                goal_id = str(lease.get("goal_id") or "")
                run_id = str(lease.get("run_id") or "")
                if goal_id and isinstance(goals.get(goal_id), dict):
                    owner_known = True
                    owner_terminal = str(goals[goal_id].get("status") or "") in terminal
                elif run_id and isinstance(runs.get(run_id), dict):
                    owner_known = True
                    owner_terminal = str(runs[run_id].get("status") or "") in terminal
                expired = bool(lease.get("expires_at")) and float(lease.get("expires_at") or 0) <= now
                if migrate and (expired or (owner_known and owner_terminal)):
                    lease["status"] = "abandoned"
                    lease.setdefault("abandoned_at", now)
                    lease.setdefault(
                        "abandoned_reason",
                        "legacy_migration_expired" if expired else "legacy_migration_owner_terminal",
                    )
                    lease["legacy_migration_version"] = 1
                    migrated.append(str(lease.get("lease_id") or ""))
                    continue
                active.append(
                    {
                        "lease_id": str(lease.get("lease_id") or ""),
                        "kind": kind,
                        "status": str(lease.get("status") or ""),
                        "run_id": run_id or None,
                        "goal_id": goal_id or None,
                        "goal_revision": lease.get("goal_revision"),
                        "owner_known": owner_known,
                    }
                )

        control = data.setdefault("legacy_external_lease_compatibility", {})
        total_calls = int(control.get("total_call_count") or 0)
        observations = control.setdefault("release_observations", [])
        release_observation_added = False
        if release_id and not any(
            isinstance(item, dict) and item.get("release_id") == release_id for item in observations
        ):
            previous_calls = int(
                observations[-1].get("total_call_count") or 0
                if observations and isinstance(observations[-1], dict)
                else 0
            )
            observations.append(
                {
                    "release_id": release_id,
                    "observed_at": now,
                    "total_call_count": total_calls,
                    "calls_since_previous": max(0, total_calls - previous_calls),
                    "active_lease_count": len(active),
                }
            )
            release_observation_added = True
            if len(observations) > 20:
                del observations[:-20]
        zero_call_cycles = 0
        for observation in reversed(observations):
            if not isinstance(observation, dict) or int(observation.get("calls_since_previous") or 0) != 0:
                break
            zero_call_cycles += 1
        retirement_eligible = not active and zero_call_cycles >= 2
        audit = {
            "migration_version": 1,
            "audited_at": now,
            "active_lease_count": len(active),
            "active_leases": active,
            "migrated_lease_ids": [item for item in migrated if item],
            "legacy_tool_call_count": total_calls,
            "zero_call_release_cycles": zero_call_cycles,
            "minimum_zero_call_release_cycles": 2,
            "retirement_eligible": retirement_eligible,
            "source_code_retained": True,
        }
        # Model-schema filtering calls this audit on every model turn. Persist
        # only an actual migration or one release observation; otherwise a
        # read-only visibility check would rewrite the Session JSON and update
        # timestamps on every turn without changing authoritative state.
        if migrated or release_observation_added:
            control["latest_audit"] = audit
            self._write_file(session_id, data)
        return deepcopy(audit)

    def resolve_terminal_scratch_reference(
        self,
        session_id: str,
        scratch_path: str,
    ) -> dict[str, Any] | None:
        """Resolve an old lease path to formal delivery state, never to sibling guesses."""

        def observed(payload: dict[str, Any]) -> dict[str, Any]:
            emit_harness_metric(
                logger,
                "terminal_scratch_ref_recovery_count",
                session_id=session_id,
                status=payload.get("status"),
            )
            return payload

        normalized = str(PurePosixPath(scratch_path.replace("\\", "/")))
        for lease in self.list_external_artifact_leases(session_id):
            staged_path = str(lease.get("staged_path") or "").replace("\\", "/")
            if not staged_path or normalized != staged_path:
                continue
            if lease.get("status") == "committed":
                formal_target = str(lease.get("target_path") or "")
                artifact_id = str(lease.get("delivered_artifact_id") or "")
                latest = next(
                    (
                        item
                        for item in self.list_delivered_artifacts(
                            session_id,
                            verify_freshness=True,
                            include_inactive=True,
                        )
                        if (artifact_id and str(item.get("artifact_id") or "") == artifact_id)
                        or (formal_target and str(item.get("target_path") or "") == formal_target)
                    ),
                    None,
                )
                if not isinstance(latest, dict) or str(latest.get("status") or "active") != "active":
                    return observed(
                        {
                            "status": "artifact_stale",
                            "formal_target_path": formal_target or None,
                            "stale_reason": (
                                latest.get("stale_reason") if isinstance(latest, dict) else "delivery_registry_missing"
                            ),
                        }
                    )
                return observed(
                    {
                        "status": "durable",
                        "formal_target_path": latest.get("target_path"),
                        "content_sha256": latest.get("content_sha256"),
                        "delivered_artifact_id": latest.get("artifact_id"),
                    }
                )
            if lease.get("status") in {"abandoned", "superseded", "expired"}:
                return observed(
                    {
                        "status": "artifact_not_durable",
                        "lease_id": lease.get("lease_id"),
                        "lease_status": lease.get("status"),
                    }
                )
            return None
        for lease in self.list_external_directory_leases(session_id):
            staged_dir = str(lease.get("staged_dir") or "").replace("\\", "/").rstrip("/")
            if not staged_dir or not (normalized == staged_dir or normalized.startswith(f"{staged_dir}/")):
                continue
            if lease.get("status") == "committed":
                relative = posixpath.relpath(normalized, staged_dir)
                target = str((Path(str(lease.get("directory_path") or "")).expanduser().resolve() / relative).resolve())
                artifact = next(
                    (
                        item
                        for item in self.list_delivered_artifacts(
                            session_id,
                            verify_freshness=True,
                            include_inactive=True,
                        )
                        if str(item.get("target_path") or "") == target
                    ),
                    None,
                )
                if not isinstance(artifact, dict) or str(artifact.get("status") or "active") != "active":
                    return observed(
                        {
                            "status": "artifact_stale",
                            "formal_target_path": target,
                            "stale_reason": (
                                artifact.get("stale_reason")
                                if isinstance(artifact, dict)
                                else "delivery_registry_missing"
                            ),
                        }
                    )
                return observed(
                    {
                        "status": "durable",
                        "formal_target_path": target,
                        "content_sha256": (artifact.get("content_sha256") if isinstance(artifact, dict) else None),
                        "delivered_artifact_id": (artifact.get("artifact_id") if isinstance(artifact, dict) else None),
                    }
                )
            if lease.get("status") in {"abandoned", "superseded", "expired"}:
                return observed(
                    {
                        "status": "artifact_not_durable",
                        "lease_id": lease.get("lease_id"),
                        "lease_status": lease.get("status"),
                    }
                )
            return None
        return None

    def find_staged_external_directory_lease(
        self,
        session_id: str,
        *,
        run_id: str,
        query_id: str,
        directory_path: str,
        goal_id: str = "",
        goal_revision: Any = None,
    ) -> dict[str, Any] | None:
        """Return a reusable directory draft for the same execution scope."""

        data = self._read_file(session_id)
        leases = data.get("external_directory_leases") if data else None
        if not isinstance(leases, dict):
            return None

        def same_owner(lease: dict[str, Any]) -> bool:
            if goal_id:
                return str(lease.get("goal_id") or "") == goal_id and lease.get("goal_revision") == goal_revision
            return (
                not str(lease.get("goal_id") or "")
                and str(lease.get("run_id") or "") == run_id
                and str(lease.get("query_id") or "") == query_id
            )

        matches = [
            lease
            for lease in leases.values()
            if isinstance(lease, dict)
            and lease.get("status") in {"staged", "prepared"}
            and str(lease.get("directory_path") or "") == directory_path
            and same_owner(lease)
        ]
        if not matches:
            return None
        matches.sort(key=lambda lease: float(lease.get("created_at") or 0), reverse=True)
        return deepcopy(matches[0])

    @_session_write_locked
    def upsert_attachment_edit_lease(
        self,
        session_id: str,
        lease: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one immutable-source attachment edit lease in Session JSON."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        lease_id = str(lease.get("lease_id") or "")
        if not lease_id:
            raise ValueError("attachment edit lease requires lease_id")
        leases = data.setdefault("attachment_edit_leases", {})
        leases[lease_id] = deepcopy(lease)
        self._write_file(session_id, data)
        return deepcopy(leases[lease_id])

    def get_attachment_edit_lease(
        self,
        session_id: str,
        lease_id: str,
    ) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        leases = data.get("attachment_edit_leases") if data else None
        lease = leases.get(lease_id) if isinstance(leases, dict) else None
        return deepcopy(lease) if isinstance(lease, dict) else None

    @_session_write_locked
    def claim_attachment_publish(
        self,
        session_id: str,
        *,
        lease_id: str,
        tool_call_id: str,
        output_path: str,
        output_name: str,
    ) -> dict[str, Any]:
        """Atomically claim the one allowed publish branch for a lease."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        leases = data.get("attachment_edit_leases")
        lease = leases.get(lease_id) if isinstance(leases, dict) else None
        if not isinstance(lease, dict):
            raise FileNotFoundError(f"AttachmentEditLease {lease_id} not found")
        status = str(lease.get("status") or "")
        if status == "published":
            return deepcopy(lease)
        if status == "publishing":
            same_claim = (
                str(lease.get("publish_tool_call_id") or "") == tool_call_id
                and str(lease.get("publish_output_path") or "") == output_path
                and str(lease.get("publish_name") or "") == output_name
            )
            if not same_claim:
                raise RuntimeError("AttachmentEditLease already has an in-flight publish branch")
            return deepcopy(lease)
        if status != "staged":
            raise RuntimeError(f"AttachmentEditLease is not publishable ({status})")
        lease.update(
            {
                "status": "publishing",
                "publish_started_at": time.time(),
                "publish_tool_call_id": tool_call_id,
                "publish_output_path": output_path,
                "publish_name": output_name,
            }
        )
        self._write_file(session_id, data)
        return deepcopy(lease)

    @_session_write_locked
    def commit_attachment_publish(
        self,
        session_id: str,
        *,
        lease_id: str,
        tool_call_id: str,
        published_fields: dict[str, Any],
        delivery: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically commit lease completion and its durable UI outbox."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        leases = data.get("attachment_edit_leases")
        lease = leases.get(lease_id) if isinstance(leases, dict) else None
        if not isinstance(lease, dict):
            raise FileNotFoundError(f"AttachmentEditLease {lease_id} not found")
        if lease.get("status") == "published":
            return deepcopy(lease)
        if lease.get("status") != "publishing" or str(lease.get("publish_tool_call_id") or "") != tool_call_id:
            raise RuntimeError("AttachmentEditLease publish claim no longer belongs to this Tool call")
        lease.update(deepcopy(published_fields))
        lease["status"] = "published"

        query_id = str(delivery.get("created_by_query_id") or lease.get("query_id") or "")
        attachment_id = str(delivery.get("id") or "")
        if not query_id or not attachment_id:
            raise ValueError("attachment delivery requires query and attachment ids")
        outbox = data.setdefault("attachment_deliveries", {})
        entries = outbox.setdefault(query_id, [])
        if not any(isinstance(item, dict) and str(item.get("id") or "") == attachment_id for item in entries):
            entries.append(deepcopy(delivery))
        self._write_file(session_id, data)
        return deepcopy(lease)

    @_session_write_locked
    def release_attachment_publish_claim(
        self,
        session_id: str,
        *,
        lease_id: str,
        tool_call_id: str,
    ) -> None:
        """Release a claim after a handled pre-commit failure."""

        data = self._read_file(session_id)
        leases = data.get("attachment_edit_leases") if data else None
        lease = leases.get(lease_id) if isinstance(leases, dict) else None
        if not isinstance(lease, dict):
            return
        if lease.get("status") == "publishing" and str(lease.get("publish_tool_call_id") or "") == tool_call_id:
            lease["status"] = "staged"
            for key in (
                "publish_started_at",
                "publish_tool_call_id",
                "publish_output_path",
                "publish_name",
            ):
                lease.pop(key, None)
            self._write_file(session_id, data)

    def list_attachment_deliveries(
        self,
        session_id: str,
        query_id: str,
    ) -> list[dict[str, Any]]:
        data = self._read_file(session_id)
        outbox = data.get("attachment_deliveries") if data else None
        entries = outbox.get(query_id) if isinstance(outbox, dict) else None
        return [deepcopy(item) for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []

    @_session_write_locked
    def update_agent_context_state(
        self,
        session_id: str,
        *,
        used_tokens: int,
        messages: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
    ) -> None:
        """Atomically persist Agent usage and an optional same-Run snapshot."""
        data = self._read_file(session_id)
        if not data:
            return
        data["agent_context_usage"] = max(0, int(used_tokens))
        if messages is not None:
            data["run_agent_context"] = {
                "run_id": run_id,
                "messages": messages,
                "updated_at": time.time(),
            }
            data.pop("agent_context_messages", None)
            data.pop("agent_context_run_id", None)
        self._write_file(session_id, data)

    # ── Host-file mutation receipts ──────────────────────────────────────────

    def _external_rewind_backup_path(
        self,
        session_id: str,
        receipt_id: str,
    ) -> Path:
        assert self._base_dir is not None
        safe_session = "".join(character for character in session_id if character.isalnum() or character in "-_")
        safe_receipt = "".join(character for character in receipt_id if character.isalnum() or character in "-_")
        return self._base_dir / "data" / "harness-rewind" / safe_session / f"{safe_receipt}.bin"

    @_session_write_locked
    def append_external_mutation_receipt(
        self,
        session_id: str,
        receipt: dict[str, Any],
        *,
        before_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """Persist one immutable HostFileBroker mutation receipt."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        receipt_id = str(receipt.get("receipt_id") or "")
        if not receipt_id:
            raise ValueError("external mutation receipt requires receipt_id")
        persisted = deepcopy(receipt)
        if before_bytes is not None:
            backup_path = self._external_rewind_backup_path(session_id, receipt_id)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if backup_path.exists():
                existing_digest = f"sha256:{hashlib.sha256(backup_path.read_bytes()).hexdigest()}"
                incoming_digest = f"sha256:{hashlib.sha256(before_bytes).hexdigest()}"
                if existing_digest != incoming_digest:
                    raise ValueError(f"external mutation rewind backup {receipt_id} is immutable")
            else:
                temporary = backup_path.with_name(f".{backup_path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(before_bytes)
                    temporary.replace(backup_path)
                finally:
                    temporary.unlink(missing_ok=True)
            persisted["rewind_backup_ref"] = f"rewind-backup:{receipt_id}"
            persisted["rewindable"] = True
        elif str(receipt.get("operation") or "") in {
            "create",
            "copy",
            "materialize_create",
        }:
            # Rewinding a create deletes the exact file after a hash check; no
            # before-bytes backup is needed.
            persisted["rewindable"] = True
        receipts = data.setdefault("external_mutation_receipts", {})
        existing = receipts.get(receipt_id)
        if isinstance(existing, dict):
            if existing != persisted:
                raise ValueError(f"external mutation receipt {receipt_id} is immutable")
            return deepcopy(existing)
        receipts[receipt_id] = persisted
        self._write_file(session_id, data)
        return deepcopy(receipts[receipt_id])

    def load_external_mutation_backup(
        self,
        session_id: str,
        receipt_id: str,
    ) -> bytes | None:
        """Return server-side rewind bytes without exposing their path."""

        if self._base_dir is None:
            return None
        path = self._external_rewind_backup_path(session_id, receipt_id)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    @_session_write_locked
    def append_external_mutation_receipts_atomic(
        self,
        session_id: str,
        entries: list[tuple[dict[str, Any], bytes | None]],
    ) -> list[dict[str, Any]]:
        """Persist one Broker transaction's receipts in a single Session write."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        receipts = data.setdefault("external_mutation_receipts", {})
        persisted_entries: list[dict[str, Any]] = []
        pending_backups: list[tuple[Path, bytes]] = []
        for receipt, before_bytes in entries:
            persisted = deepcopy(receipt)
            receipt_id = str(persisted.get("receipt_id") or "")
            if not receipt_id:
                raise ValueError("external mutation receipt requires receipt_id")
            existing = receipts.get(receipt_id)
            if isinstance(existing, dict):
                if existing != persisted:
                    raise ValueError(f"external mutation receipt {receipt_id} is immutable")
                persisted_entries.append(deepcopy(existing))
                continue
            if before_bytes is not None:
                backup_path = self._external_rewind_backup_path(
                    session_id,
                    receipt_id,
                )
                if backup_path.exists() and backup_path.read_bytes() != before_bytes:
                    raise ValueError(f"external mutation rewind backup {receipt_id} is immutable")
                persisted["rewind_backup_ref"] = f"rewind-backup:{receipt_id}"
                persisted["rewindable"] = True
                pending_backups.append((backup_path, before_bytes))
            elif str(persisted.get("operation") or "") in {
                "create",
                "copy",
                "materialize_create",
            }:
                persisted["rewindable"] = True
            persisted_entries.append(persisted)

        created_backups: list[Path] = []
        try:
            for backup_path, before_bytes in pending_backups:
                if backup_path.exists():
                    continue
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = backup_path.with_name(f".{backup_path.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(before_bytes)
                    temporary.replace(backup_path)
                    created_backups.append(backup_path)
                finally:
                    temporary.unlink(missing_ok=True)
            for persisted in persisted_entries:
                receipts[str(persisted["receipt_id"])] = deepcopy(persisted)
            self._write_file(session_id, data)
        except Exception:
            for backup_path in created_backups:
                backup_path.unlink(missing_ok=True)
            raise
        return [deepcopy(item) for item in persisted_entries]

    def list_external_mutation_receipts(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        data = self._read_file(session_id)
        receipts = data.get("external_mutation_receipts") if data else None
        if not isinstance(receipts, dict):
            return []
        values = [
            deepcopy(item)
            for item in receipts.values()
            if isinstance(item, dict) and (run_id is None or str(item.get("run_id") or "") == run_id)
        ]
        return sorted(values, key=lambda item: float(item.get("created_at") or 0))

    def find_external_mutation_receipt(
        self,
        session_id: str,
        *,
        run_id: str,
        canonical_path: str,
        after_sha256: str | None = None,
    ) -> dict[str, Any] | None:
        matches = [
            item
            for item in self.list_external_mutation_receipts(
                session_id,
                run_id=run_id,
            )
            if str(item.get("canonical_path") or "") == canonical_path
            and str(item.get("status") or "") == "completed"
            and (not after_sha256 or str(item.get("after_sha256") or "") == after_sha256)
        ]
        return deepcopy(matches[-1]) if matches else None

    def find_stale_validation_receipts(
        self,
        session_id: str,
        *,
        canonical_path: str,
        current_sha256: str | None = None,
    ) -> list[dict[str, Any]]:
        """Passing validation receipts bound to ``canonical_path`` whose recorded
        content hash no longer matches the file's current bytes.

        A mutation landing after a successful validation invalidates the
        receipt, because evidence is bound to the pre-mutation content hash.
        Write tools use this to warn the agent at mutation time instead of
        letting the staleness surface one verification round later.
        """

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        if not isinstance(runs, dict):
            return []
        stale: list[dict[str, Any]] = []
        seen: set[str] = set()
        for run in runs.values():
            if not isinstance(run, dict):
                continue
            for activation in run.get("verification_activations") or []:
                if not isinstance(activation, dict):
                    continue
                for ref in activation.get("evidence_refs") or []:
                    if not isinstance(ref, dict) or ref.get("kind") != "validation_receipt":
                        continue
                    if (
                        str(ref.get("status") or "") != "passed"
                        or int(ref.get("exit_code", -1)) != 0
                        or int(ref.get("checks_failed") or 0) != 0
                    ):
                        continue
                    receipt_id = str(ref.get("validation_receipt_id") or "")
                    if not receipt_id or receipt_id in seen:
                        continue
                    for artifact in ref.get("artifact_refs") or []:
                        if not isinstance(artifact, dict):
                            continue
                        path = str(artifact.get("path") or "")
                        if not path:
                            continue
                        try:
                            resolved = str(Path(path).expanduser().resolve())
                        except OSError:
                            resolved = path
                        if resolved != canonical_path:
                            continue
                        content_sha256 = str(artifact.get("content_sha256") or "")
                        if current_sha256 and content_sha256 == current_sha256:
                            continue
                        seen.add(receipt_id)
                        stale.append(
                            {
                                "validation_receipt_id": receipt_id,
                                "validator_kind": str(ref.get("validator_kind") or ""),
                                "content_sha256": content_sha256,
                            }
                        )
                        break
        return stale

    # ── Immutable source references and materialization receipts ─────────────

    @_session_write_locked
    def register_source_reference(
        self,
        session_id: str,
        source_reference: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        source_ref = str(source_reference.get("source_ref") or "")
        if not source_ref:
            raise ValueError("source reference requires source_ref")
        persisted = deepcopy(source_reference)
        references = data.setdefault("source_references", {})
        existing = references.get(source_ref)
        if isinstance(existing, dict):
            if existing != persisted:
                raise ValueError(f"source reference {source_ref} is immutable")
            return deepcopy(existing)
        references[source_ref] = persisted
        self._write_file(session_id, data)
        return deepcopy(persisted)

    def get_source_reference(
        self,
        session_id: str,
        source_ref: str,
    ) -> dict[str, Any] | None:
        data = self._read_file(session_id)
        references = data.get("source_references") if data else None
        if not isinstance(references, dict):
            return None
        value = references.get(source_ref)
        return deepcopy(value) if isinstance(value, dict) else None

    def list_source_references(self, session_id: str) -> list[dict[str, Any]]:
        data = self._read_file(session_id)
        references = data.get("source_references") if data else None
        if not isinstance(references, dict):
            return []
        return sorted(
            (deepcopy(item) for item in references.values() if isinstance(item, dict)),
            key=lambda item: float(item.get("created_at") or 0),
        )

    @_session_write_locked
    def append_materialization_receipt(
        self,
        session_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        receipt_id = str(receipt.get("materialization_receipt_id") or "")
        if not receipt_id:
            raise ValueError("materialization receipt requires materialization_receipt_id")
        persisted = deepcopy(receipt)
        receipts = data.setdefault("materialization_receipts", {})
        existing = receipts.get(receipt_id)
        if isinstance(existing, dict):
            existing_stable = {key: value for key, value in existing.items() if key != "created_at"}
            incoming_stable = {key: value for key, value in persisted.items() if key != "created_at"}
            if existing_stable != incoming_stable:
                raise ValueError(f"materialization receipt {receipt_id} is immutable")
            return deepcopy(existing)
        receipts[receipt_id] = persisted
        self._write_file(session_id, data)
        return deepcopy(persisted)

    def list_materialization_receipts(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        data = self._read_file(session_id)
        receipts = data.get("materialization_receipts") if data else None
        if not isinstance(receipts, dict):
            return []
        return sorted(
            (
                deepcopy(item)
                for item in receipts.values()
                if isinstance(item, dict) and (run_id is None or str(item.get("run_id") or "") == run_id)
            ),
            key=lambda item: float(item.get("created_at") or 0),
        )

    # ── Permission grants ─────────────────────────────────────────────────────

    def list_permission_grants(self, session_id: str) -> list[dict[str, Any]]:
        """Return active session permission grants."""
        data = self._read_file(session_id)
        if not data:
            return []
        permissions = data.get("permissions")
        grants = permissions.get("grants") if isinstance(permissions, dict) else None
        if not isinstance(grants, list):
            return []
        return [
            dict(grant)
            for grant in grants
            if isinstance(grant, dict) and not grant.get("revoked_at") and not grant.get("superseded_at")
        ]

    def permission_grants_snapshot(
        self,
        session_id: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return active grants and their monotonic revision from one read."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return [], 0
            permissions = data.get("permissions")
            grants = permissions.get("grants") if isinstance(permissions, dict) else None
            revision = int(permissions.get("grants_revision") or 0) if isinstance(permissions, dict) else 0
            active = [
                dict(grant)
                for grant in (grants if isinstance(grants, list) else [])
                if isinstance(grant, dict) and not grant.get("revoked_at") and not grant.get("superseded_at")
            ]
            return active, revision

    def list_permission_grant_history(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recently consumed or revoked grants, newest first.

        One-shot grants remain useful audit evidence after consumption even
        though they must no longer be treated as active permissions.
        """
        data = self._read_file(session_id)
        if not data:
            return []
        permissions = data.get("permissions")
        grants = permissions.get("grants") if isinstance(permissions, dict) else None
        if not isinstance(grants, list):
            return []
        inactive = [
            dict(grant)
            for grant in grants
            if isinstance(grant, dict) and (grant.get("revoked_at") or grant.get("superseded_at"))
        ]
        inactive.sort(
            key=lambda grant: float(
                grant.get("consumed_at") or grant.get("revoked_at") or grant.get("created_at") or 0
            ),
            reverse=True,
        )
        return inactive[: max(0, int(limit))]

    @_session_write_locked
    def migrate_permission_grants(self, session_id: str) -> int:
        """Persist current semantic bindings and supersede active duplicates."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        permissions = data.get("permissions")
        grants = permissions.get("grants") if isinstance(permissions, dict) else None
        if not isinstance(grants, list):
            return 0
        changed = self._migrate_permission_grants(session_id, grants)
        if changed:
            permissions["grants_revision"] = int(permissions.get("grants_revision") or 0) + 1
            self._write_file(session_id, data)
        return sum(
            1
            for item in grants
            if isinstance(item, dict) and item.get("supersede_reason") == "semantic_duplicate_v2_migration"
        )

    @_session_write_locked
    def add_permission_grant(
        self,
        session_id: str,
        *,
        grant_type: str,
        target_kind: str,
        target: str,
        capabilities: list[str],
        scope: str = "session",
        source: str = "user",
        metadata: dict[str, Any] | None = None,
        bindings: dict[str, Any] | None = None,
        consume_immediately: bool = False,
    ) -> dict[str, Any]:
        """Persist a permission grant and return it.

        ``consume_immediately`` is for synchronous structured actions where the
        approval click and execution attempt share one request. It records the
        one-shot authorization directly as consumed, so a process failure can
        never leave reusable authority behind.
        """
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")

        if (
            grant_type.startswith(("external_file_", "external_directory_"))
            and target_kind in {"exact_file", "exact_directory"}
            and target != "*"
        ):
            classified_target = classify_path_authority(
                target,
                workspace_root=str(data.get("workspace_path") or "") or None,
            )
            if classified_target.authority in {
                PathAuthority.WORKSPACE,
                PathAuthority.SCRATCH,
                PathAuthority.MANAGED,
                PathAuthority.ESCAPE,
            }:
                raise ValueError(
                    "External permission grants cannot target internal virtual paths "
                    f"or the current workspace: {target}"
                )

        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        grants = permissions.get("grants")
        if not isinstance(grants, list):
            grants = []

        normalized_bindings: dict[str, Any] | None = None
        if bindings:
            effective = permission_policy_snapshot(permissions)
            if (
                bindings.get("policy_epoch") != effective["policy_epoch"]
                or bindings.get("policy_version") != effective["policy_version"]
            ):
                raise ValueError("Permission request belongs to a stale policy epoch")
            run_id = str((metadata or {}).get("run_id") or "")
            harness = data.get("harness")
            runs = harness.get("runs") if isinstance(harness, dict) else None
            run = runs.get(run_id) if run_id and isinstance(runs, dict) else None
            if not isinstance(run, dict) or run.get("status") in {
                "completed",
                "cancelled",
                "failed",
                "blocked",
                "budget_exceeded",
                "verification_failed",
            }:
                raise ValueError("Permission request no longer belongs to an active Run")
            expected_bindings = RunPermissionContext.from_config_snapshot(run.get("config_snapshot")).grant_bindings()
            if bindings != expected_bindings:
                raise ValueError("Permission request does not match the active Run")
            normalized_bindings = deepcopy(bindings)

        if consume_immediately and scope != "once":
            raise ValueError("Only one-time permissions can be consumed immediately")
        now = time.time()
        normalized_capabilities = list(dict.fromkeys(capabilities))
        self._migrate_permission_grants(session_id, grants)
        semantic_runtime_bindings = self._permission_semantic_runtime_bindings(
            scope=scope,
            metadata=metadata,
            bindings=normalized_bindings,
        )
        semantic_key, stable_bindings = PermissionBindingPolicy.semantic_key(
            session_id=session_id,
            grant_type=grant_type,
            scope=scope,
            target_kind=target_kind,
            target=target,
            capabilities=normalized_capabilities,
            runtime_bindings=semantic_runtime_bindings,
        )
        # Session approvals are semantic capabilities, not a log of button
        # clicks. Re-approving the same bound scope must reuse one authoritative
        # grant so subagents and later Goal Runs do not create duplicate cards.
        if scope == "session":
            for existing in grants:
                if not isinstance(existing, dict) or existing.get("revoked_at") or existing.get("superseded_at"):
                    continue
                if (
                    existing.get("type") == grant_type
                    and existing.get("scope") == scope
                    and existing.get("target_kind") == target_kind
                    and existing.get("target") == target
                    and set(existing.get("capabilities") or []) == set(normalized_capabilities)
                    and existing.get("semantic_key") == semantic_key
                ):
                    existing["last_approved_at"] = now
                    if metadata:
                        prior_metadata = existing.get("metadata")
                        prior_metadata = dict(prior_metadata) if isinstance(prior_metadata, dict) else {}
                        prior_metadata.update(metadata)
                        existing["metadata"] = prior_metadata
                    permissions["grants"] = grants
                    data["permissions"] = permissions
                    self._write_file(session_id, data)
                    return dict(existing)
        grant = {
            "id": f"grant-{uuid.uuid4().hex[:12]}",
            "type": grant_type,
            "scope": scope,
            "target_kind": target_kind,
            "target": target,
            "capabilities": normalized_capabilities,
            "source": source,
            "created_at": now,
            "binding_schema_version": PERMISSION_BINDING_SCHEMA_VERSION,
            "semantic_key": semantic_key,
            "stable_bindings": stable_bindings,
            "runtime_observations": {
                "backend_id_at_approval": str((normalized_bindings or {}).get("backend_id") or ""),
            },
        }
        if metadata:
            grant["metadata"] = dict(metadata)
        if normalized_bindings is not None:
            grant["bindings"] = normalized_bindings
        if consume_immediately:
            grant["revoked_at"] = now
            grant["consumed_at"] = now
        grants.append(grant)
        permissions["grants"] = grants
        permissions["grants_revision"] = int(permissions.get("grants_revision") or 0) + 1
        data["permissions"] = permissions
        self._write_file(session_id, data)
        return dict(grant)

    @_session_write_locked
    def add_shell_directory_grants_atomic(
        self,
        session_id: str,
        *,
        grant_specs: list[ShellDirectoryGrantSpec],
        scope: str,
        run_id: str,
        bindings: dict[str, Any],
        source: str = "user",
    ) -> list[dict[str, Any]]:
        """Validate and persist one native shell authority set atomically."""

        if not grant_specs or len(grant_specs) > 16:
            raise ValueError("Shell directory grant set must contain 1-16 entries")
        if scope not in {"run", "session"} or not run_id:
            raise ValueError("Shell directory grants require an active Run scope")
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(run, dict) or run.get("status") in {
            "completed",
            "cancelled",
            "failed",
            "blocked",
            "budget_exceeded",
            "verification_failed",
        }:
            raise ValueError(
                "Shell directory grant request no longer belongs to an active Run"
            )
        expected = RunPermissionContext.from_config_snapshot(
            run.get("config_snapshot")
        ).shell_grant_bindings()
        if not PermissionBindingPolicy.shell_v3_equivalent(bindings, expected):
            raise ValueError("Shell directory grant bindings do not match the active Run")

        normalized: list[tuple[ShellDirectoryGrantSpec, str]] = []
        seen: set[tuple[str, str]] = set()
        workspace_root = str(data.get("workspace_path") or "") or None
        for spec in grant_specs:
            if spec.access not in {"read", "write"} or (
                spec.delete and spec.access != "write"
            ):
                raise ValueError("Invalid shell directory grant capability")
            path = Path(spec.target).expanduser()
            if (
                not path.is_absolute()
                or path.is_symlink()
                or not path.is_dir()
                or str(path.resolve()) != str(path)
            ):
                raise ValueError(
                    "Shell directory grant target must be a canonical real directory"
                )
            classified = classify_path_authority(
                str(path),
                workspace_root=workspace_root,
            )
            if classified.authority is not PathAuthority.EXTERNAL:
                raise ValueError(
                    "Shell directory grants may target only external directories"
                )
            key = (str(path), spec.access)
            if key in seen:
                raise ValueError("Shell directory grant set contains duplicate entries")
            seen.add(key)
            normalized.append((spec, str(path)))
        targets_with_read = {
            target for spec, target in normalized if spec.access == "read"
        }
        for spec, target in normalized:
            if spec.access == "write" and target not in targets_with_read:
                raise ValueError(
                    "Shell write authority requires an atomic matching read grant"
                )

        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        grants = permissions.get("grants")
        if not isinstance(grants, list):
            grants = []
        migrated = self._migrate_permission_grants(session_id, grants)
        now = time.time()
        results: list[dict[str, Any]] = []
        appended = migrated
        for spec, target in normalized:
            grant_type = f"external_directory_{spec.access}"
            capabilities = spec.capabilities
            semantic_key, stable_bindings = (
                PermissionBindingPolicy.shell_v3_semantic_key(
                    session_id=session_id,
                    scope=scope,
                    run_id=run_id,
                    grant_type=grant_type,
                    target=target,
                    capabilities=capabilities,
                    bindings=bindings,
                )
            )
            existing = next(
                (
                    item
                    for item in grants
                    if isinstance(item, dict)
                    and not item.get("revoked_at")
                    and not item.get("superseded_at")
                    and item.get("binding_schema_version")
                    == SHELL_PERMISSION_BINDING_SCHEMA_VERSION
                    and item.get("semantic_key") == semantic_key
                ),
                None,
            )
            if existing is not None:
                results.append(dict(existing))
                continue
            grant = {
                "id": f"grant-{uuid.uuid4().hex[:12]}",
                "type": grant_type,
                "scope": scope,
                "target_kind": "exact_directory",
                "target": target,
                "capabilities": capabilities,
                "source": source,
                "created_at": now,
                "binding_schema_version": SHELL_PERMISSION_BINDING_SCHEMA_VERSION,
                "semantic_key": semantic_key,
                "stable_bindings": stable_bindings,
                "bindings": dict(bindings),
                "metadata": {"run_id": run_id, "authority_plane": "shell"},
                "runtime_observations": {},
            }
            grants.append(grant)
            results.append(dict(grant))
            appended = True
        if appended:
            permissions["grants"] = grants
            permissions["grants_revision"] = int(
                permissions.get("grants_revision") or 0
            ) + 1
            data["permissions"] = permissions
            self._write_file(session_id, data)
        return results

    @staticmethod
    def _permission_semantic_runtime_bindings(
        *,
        scope: str,
        metadata: dict[str, Any] | None,
        bindings: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Project authority identity without conflating concurrent Run grants.

        Runtime bindings describe the stable execution boundary. Run scope has
        one additional authority dimension: the exact Run that the user
        approved. It lives in metadata for matching, but must also participate
        in the semantic key so concurrent Runs never supersede each other.
        """

        projected = dict(bindings or {})
        if scope == "run":
            projected["run_id"] = str((metadata or {}).get("run_id") or "")
        return projected or None

    @staticmethod
    def _migrate_permission_grants(
        session_id: str,
        grants: list[Any],
    ) -> bool:
        """Upgrade legacy grants and preserve duplicate records as history."""

        changed = False
        active_by_key: dict[str, list[dict[str, Any]]] = {}
        for raw in grants:
            if not isinstance(raw, dict):
                continue
            bindings = raw.get("bindings") if isinstance(raw.get("bindings"), dict) else None
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else None
            grant_type = str(raw.get("type") or "")
            scope = str(raw.get("scope") or "session")
            target_kind = str(raw.get("target_kind") or "")
            target = str(raw.get("target") or "")
            capabilities = [str(item) for item in raw.get("capabilities") or []]
            native_shell = (
                grant_type in {"external_directory_read", "external_directory_write"}
                and target_kind == "exact_directory"
                and "shell_access" in capabilities
                and PermissionBindingPolicy.shell_v3_equivalent(bindings, bindings)
            )
            if native_shell:
                key, stable = PermissionBindingPolicy.shell_v3_semantic_key(
                    session_id=session_id,
                    scope=scope,
                    run_id=str((metadata or {}).get("run_id") or ""),
                    grant_type=grant_type,
                    target=target,
                    capabilities=capabilities,
                    bindings=bindings or {},
                )
                schema_version = SHELL_PERMISSION_BINDING_SCHEMA_VERSION
            else:
                semantic_bindings = SessionManager._permission_semantic_runtime_bindings(
                    scope=scope,
                    metadata=metadata,
                    bindings=bindings,
                )
                key, stable = PermissionBindingPolicy.semantic_key(
                    session_id=session_id,
                    grant_type=grant_type,
                    scope=scope,
                    target_kind=target_kind,
                    target=target,
                    capabilities=capabilities,
                    runtime_bindings=semantic_bindings,
                )
                schema_version = PERMISSION_BINDING_SCHEMA_VERSION
            desired = {
                "binding_schema_version": schema_version,
                "semantic_key": key,
                "stable_bindings": stable,
            }
            for field, value in desired.items():
                if raw.get(field) != value:
                    raw[field] = value
                    changed = True
            if "runtime_observations" not in raw:
                raw["runtime_observations"] = {"backend_id_at_approval": str((bindings or {}).get("backend_id") or "")}
                changed = True
            if not raw.get("revoked_at") and not raw.get("superseded_at"):
                active_by_key.setdefault(key, []).append(raw)

        now = time.time()
        for group in active_by_key.values():
            if len(group) < 2:
                continue
            authoritative = max(
                group,
                key=lambda item: float(item.get("last_approved_at") or item.get("created_at") or 0),
            )
            for duplicate in group:
                if duplicate is authoritative:
                    continue
                duplicate["superseded_at"] = now
                duplicate["superseded_by"] = authoritative.get("id")
                duplicate["supersede_reason"] = "semantic_duplicate_v2_migration"
                changed = True
        return changed

    @staticmethod
    def _permission_bindings_match(
        existing: Any,
        required: Any,
        *,
        target_kind: str,
        target: str,
    ) -> bool:
        """Compare authority bindings without coupling them to an instance.

        Session-wide network authority belongs to the stable workspace and
        policy boundary. A Docker container id is an execution observation,
        not part of that authority; rebuilding the container must not create a
        second grant. Other capabilities remain exact-bound until they define
        their own stable projection.
        """

        return PermissionBindingPolicy.equivalent(
            grant_type="tool_action",
            scope=(
                "session"
                if target_kind in {"network_origin", "network_profile"}
                or target == "session_network_access"
                else "once"
            ),
            target_kind=target_kind,
            target=target,
            left=existing if isinstance(existing, dict) else None,
            right=required if isinstance(required, dict) else None,
        )

    @_session_write_locked
    def revoke_permission_grant(self, session_id: str, grant_id: str) -> bool:
        """Mark a session permission grant as revoked."""
        data = self._read_file(session_id)
        permissions = data.get("permissions") if data else None
        grants = permissions.get("grants") if isinstance(permissions, dict) else None
        if not isinstance(grants, list):
            return False
        now = time.time()
        changed = False
        for grant in grants:
            if isinstance(grant, dict) and grant.get("id") == grant_id and not grant.get("revoked_at"):
                grant["revoked_at"] = now
                changed = True
                break
        if changed:
            assert isinstance(permissions, dict)
            permissions["grants_revision"] = int(permissions.get("grants_revision") or 0) + 1
            self._write_file(session_id, data)
        return changed

    def has_external_file_read_permission(self, session_id: str, path: Path) -> bool:
        """Return whether the session may read the given external file."""
        resolved = str(path.expanduser().resolve())
        for grant in self.list_permission_grants(session_id):
            if grant.get("type") != "external_file_read":
                continue
            if "read" not in (grant.get("capabilities") or []):
                continue
            target_kind = grant.get("target_kind")
            if target_kind == "all_external_files":
                return True
            if target_kind == "exact_file" and grant.get("target") == resolved:
                return True
        return False

    def has_external_file_write_permission(self, session_id: str, path: Path) -> bool:
        """Return whether the session may write the given exact external file."""
        resolved = str(path.expanduser().resolve())
        for grant in self.list_permission_grants(session_id):
            if grant.get("type") != "external_file_write":
                continue
            if "write" not in (grant.get("capabilities") or []):
                continue
            if grant.get("target_kind") == "exact_file" and grant.get("target") == resolved:
                return True
        return False

    def has_external_file_delete_permission(self, session_id: str, path: Path) -> bool:
        """Return whether the session may delete the given exact external file.

        Delete is deliberately not implied by an exact-file write grant. An
        exact-directory write grant may include the separate ``delete``
        capability, which is checked by HostFileBroker at the descendant
        boundary.
        """

        resolved = str(path.expanduser().resolve())
        for grant in self.list_permission_grants(session_id):
            if grant.get("type") != "external_file_delete":
                continue
            if "delete" not in (grant.get("capabilities") or []):
                continue
            if grant.get("target_kind") == "exact_file" and grant.get("target") == resolved:
                return True
        return False

    def has_external_directory_permission(
        self,
        session_id: str,
        path: Path,
        *,
        access: str,
        run_id: str,
    ) -> bool:
        """Return whether this Run may recursively read or write one directory."""

        if access not in {"read", "write"} or not run_id:
            return False
        resolved = str(path.expanduser().resolve())
        for grant in self.list_permission_grants(session_id):
            if grant.get("type") != f"external_directory_{access}":
                continue
            if access not in (grant.get("capabilities") or []):
                continue
            if grant.get("target_kind") != "exact_directory" or grant.get("target") != resolved:
                continue
            metadata = grant.get("metadata")
            if grant.get("scope") == "run" and isinstance(metadata, dict) and metadata.get("run_id") == run_id:
                return True
            if grant.get("scope") != "session":
                continue
            bindings = grant.get("bindings")
            if not isinstance(bindings, dict):
                # Legacy unbound Session directory grants cannot be safely
                # reused across a policy/workspace boundary.
                continue
            run = self.get_run_state(session_id, run_id)
            if not isinstance(run, dict):
                continue
            required = RunPermissionContext.from_config_snapshot(run.get("config_snapshot")).grant_bindings()
            if PermissionBindingPolicy.equivalent(
                grant_type=str(grant.get("type") or ""),
                scope="session",
                target_kind="exact_directory",
                target=resolved,
                left=bindings,
                right=required,
            ):
                return True
        return False

    def has_external_directory_delete_permission(
        self,
        session_id: str,
        path: Path,
        *,
        run_id: str,
    ) -> bool:
        """Return whether a write Grant separately includes directory delete."""

        resolved = str(path.expanduser().resolve())
        for grant in self.list_permission_grants(session_id):
            if (
                grant.get("type") != "external_directory_write"
                or "write" not in (grant.get("capabilities") or [])
                or "delete" not in (grant.get("capabilities") or [])
                or grant.get("target_kind") != "exact_directory"
                or grant.get("target") != resolved
            ):
                continue
            metadata = grant.get("metadata")
            if grant.get("scope") == "run" and isinstance(metadata, dict) and metadata.get("run_id") == run_id:
                return True
            if grant.get("scope") != "session":
                continue
            bindings = grant.get("bindings")
            run = self.get_run_state(session_id, run_id)
            if not isinstance(bindings, dict) or not isinstance(run, dict):
                continue
            required = RunPermissionContext.from_config_snapshot(run.get("config_snapshot")).grant_bindings()
            if PermissionBindingPolicy.equivalent(
                grant_type="external_directory_write",
                scope="session",
                target_kind="exact_directory",
                target=resolved,
                left=bindings,
                right=required,
            ):
                return True
        return False

    @_session_write_locked
    def consume_tool_action_permission(
        self,
        session_id: str,
        fingerprint: str,
        *,
        session_target_kind: str | None = None,
        session_target: str | None = None,
        required_bindings: dict[str, Any] | None = None,
        required_capabilities: list[str] | None = None,
        current_run_id: str | None = None,
    ) -> bool:
        """Consume a matching once/session grant for one managed Tool action."""

        data = self._read_file(session_id)
        permissions = data.get("permissions") if data else None
        grants = permissions.get("grants") if isinstance(permissions, dict) else None
        if not isinstance(grants, list):
            return False
        for grant in grants:
            if (
                not isinstance(grant, dict)
                or grant.get("revoked_at")
                or grant.get("type") != "tool_action"
                or "execute" not in (grant.get("capabilities") or [])
            ):
                continue
            binding_target = str(session_target or grant.get("target") or "")
            if required_bindings is not None and not self._permission_bindings_match(
                grant.get("bindings"),
                required_bindings,
                target_kind=str(session_target_kind or grant.get("target_kind") or "fingerprint"),
                target=binding_target,
            ):
                continue
            if required_capabilities is not None and not set(required_capabilities).issubset(
                set(grant.get("capabilities") or [])
            ):
                continue
            if grant.get("scope") == "once" and current_run_id:
                metadata = grant.get("metadata")
                grant_run_id = str(metadata.get("run_id") or "") if isinstance(metadata, dict) else ""
                if grant_run_id != current_run_id:
                    continue
            exact_match = grant.get("target_kind") == "fingerprint" and grant.get("target") == fingerprint
            reusable_session_match = (
                grant.get("scope") == "session"
                and session_target_kind
                and session_target
                and grant.get("target_kind") == session_target_kind
                and grant.get("target") == session_target
            )
            if not exact_match and not reusable_session_match:
                continue
            if grant.get("scope") == "once":
                grant["revoked_at"] = time.time()
                grant["consumed_at"] = grant["revoked_at"]
                self._write_file(session_id, data)
            return True
        return False

    # ── 为 Agent（LLM）准备消息 ─────────────────────────────────────────────────

    @_session_write_locked
    def load_session_for_agent(
        self,
        session_id: str,
        *,
        current_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """加载会话历史并格式化为 LLM 可用的消息列表

        两个关键处理：
        1. 合并连续的普通 assistant 文本消息（保持 user/assistant 严格交替）
        2. 如有压缩摘要，在头部注入一条摘要消息让 LLM 保留历史上下文
        """
        self.ensure_tool_call_ids(session_id)
        data = self._read_file(session_id)  # 读取会话数据
        logical = deepcopy(data) if data else {}
        logical["messages"] = deepcopy(self.load_session(session_id)) if data else []
        if data and self._ensure_evidence_metadata(session_id, logical):
            if data.get("evidence_index") != logical.get("evidence_index"):
                data["evidence_index"] = deepcopy(logical.get("evidence_index") or {})
                self._write_file(session_id, data)
        messages = logical.get("messages", []) if logical else []  # active + archive

        # ``complete_tool_context_compaction`` commits ready context outputs to
        # the raw ``messages`` store only. A middle-trimmed Session returns a
        # ``display_messages`` snapshot from ``load_session`` that predates the
        # commit, so merge the authoritative compaction fields back by tool_call
        # id or the deterministic projection can never select them.
        context_compaction_by_call_id: dict[str, dict[str, Any]] = {}
        for _, _, _, stored_call in self._iter_persisted_tool_calls(data or {}):
            stored_call_id = str(stored_call.get("id") or "")
            stored_context_output = stored_call.get("context_output")
            stored_context_metadata = stored_call.get("context_compaction")
            if stored_call_id and stored_context_output and isinstance(stored_context_metadata, dict):
                context_compaction_by_call_id[stored_call_id] = {
                    "context_output": str(stored_context_output),
                    "context_compaction": deepcopy(stored_context_metadata),
                }

        if current_run_id is None and data:
            harness = data.get("harness")
            runs = harness.get("runs") if isinstance(harness, dict) else None
            latest_run_id = harness.get("latest_run_id") if isinstance(harness, dict) else None
            latest_run = runs.get(latest_run_id) if isinstance(runs, dict) and latest_run_id else None
            if isinstance(latest_run, dict) and str(latest_run.get("status") or "") not in {
                "completed",
                "cancelled",
                "failed",
                "blocked",
                "budget_exceeded",
                "verification_failed",
            }:
                current_run_id = str(latest_run_id)

        import config

        deterministic_projection = bool(
            config.load_config().get("harness", {}).get("prompt_cache", {}).get(
                "deterministic_session_projection", True
            )
        )
        merged: list[dict[str, Any]] = []  # 合并后的结果列表

        # A display/archive projection already contains the original logical
        # history. Inject summaries only for legacy sessions whose originals
        # are genuinely unavailable; otherwise the model sees summary + source
        # twice and trimming increases context instead of reducing it.
        has_full_projection = (
            bool(isinstance(data.get("display_messages"), list) or len(messages) > len(data.get("messages") or []))
            if data
            else False
        )
        compressed = data.get("compressed_context", "") if data else ""  # 读取摘要
        if compressed and not has_full_projection:  # 摘要存在则注入
            merged.append(
                {
                    "role": "assistant",  # 伪装为 assistant 消息
                    "content": f"{COMPRESSED_CONTEXT_PREFIX}\n{compressed}",  # 前缀标识 + 摘要内容
                }
            )

        middle_trim_context = data.get("middle_trim_context", "") if data else ""
        if middle_trim_context and not has_full_projection:
            merged.append(
                {
                    "role": "assistant",
                    "content": (
                        f"{MIDDLE_TRIM_CONTEXT_PREFIX}\n"
                        "以下内容是因上下文裁剪移出活跃消息的历史任务状态摘要。"
                        "它只用于理解历史完成情况，不代表当前任务结果，也不要在新任务中续写。\n"
                        f"{middle_trim_context}"
                    ),
                }
            )

        for msg in messages:  # 遍历所有消息
            entry: dict[str, Any] = {"role": msg["role"], "content": msg["content"]}
            if msg.get("query_id"):
                entry["query_id"] = str(msg["query_id"])
            # Attachments are durable session-scoped references, not transient
            # request payloads. Keep their structured metadata in the agent
            # history so a later Run (for example after a reload or "continue")
            # can rebuild att_xxx references and let read_resource/image_analyzer
            # materialize the original file on demand.
            if isinstance(msg.get("attachments"), list):
                entry["attachments"] = deepcopy(msg["attachments"])
            msg_has_tool_calls = bool(msg.get("tool_calls"))
            if msg_has_tool_calls:
                calls: list[dict[str, Any]] = []
                for raw_call in msg.get("tool_calls") or []:
                    if not isinstance(raw_call, dict):
                        continue
                    call = deepcopy(raw_call)
                    stored_context = context_compaction_by_call_id.get(str(call.get("id") or ""))
                    if stored_context:
                        call["context_output"] = stored_context["context_output"]
                        call["context_compaction"] = stored_context["context_compaction"]
                    source_run_id = str(call.get("source_run_id") or "")
                    call["historical"] = bool(not current_run_id or source_run_id != current_run_id)
                    calls.append(call)
                if calls:
                    entry["tool_calls"] = calls
            prev_has_tool_calls = bool(merged[-1].get("_had_tool_calls")) if merged else False
            if (
                not deterministic_projection
                and
                merged  # 列表非空
                and merged[-1]["role"] == "assistant"  # 上一条是 assistant
                and msg["role"] == "assistant"  # 当前也是 assistant
                and not prev_has_tool_calls  # 上一条也不能是 tool_call 消息
                and not msg_has_tool_calls  # 当前消息无 tool_calls 才合并
            ):
                merged[-1]["content"] += "\n" + msg["content"]  # 合并为一条（避免连续 assistant）
                if msg.get("query_id"):
                    merged[-1]["query_id"] = str(msg["query_id"])
            else:
                entry["_had_tool_calls"] = msg_has_tool_calls  # 内部标记，用于下一轮合并判断
                merged.append(entry)
        # 移除内部标记后再返回
        for entry in merged:
            entry.pop("_had_tool_calls", None)
        return merged  # 返回格式化后的消息列表

    def get_message_count(self, session_id: str) -> int:
        """返回会话中的消息总数（用于判断是否触发自动压缩）"""
        data = self._read_file(session_id)  # 读取会话数据
        if not data:  # 不存在返回 0
            return 0
        return len(data.get("messages", []))  # 返回消息数量

    @_session_write_locked
    def clear_messages(self, session_id: str) -> None:
        """清空会话消息，但保留标题等元数据"""
        data = self._read_file(session_id)  # 读取会话数据
        if not data:  # 不存在则跳过
            return
        data["messages"] = []  # 清空消息列表
        if "display_messages" in data:
            del data["display_messages"]
        if "compressed_context" in data:  # 同时清除压缩摘要
            del data["compressed_context"]
        if "middle_trim_context" in data:
            del data["middle_trim_context"]
        data.pop("run_agent_context", None)
        data.pop("agent_context_messages", None)
        data.pop("agent_context_run_id", None)
        data.pop("session_summary_projection", None)
        data.pop("agent_context_compaction", None)
        data.pop("agent_context_compactions", None)
        data.pop("agent_context_usage", None)
        if "harness" in data:
            del data["harness"]
        self._write_file(session_id, data)  # 写回磁盘


# 全局单例，整个后端进程共用一个 SessionManager 实例
session_manager = SessionManager()
