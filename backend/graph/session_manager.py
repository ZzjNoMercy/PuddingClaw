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
from copy import deepcopy
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any

from graph.permission_policy import (
    DEFAULT_APPROVAL_MODE,
    PERMISSION_BINDING_SCHEMA_VERSION,
    PermissionBindingPolicy,
    RunPermissionContext,
    normalize_approval_mode,
    permission_policy_snapshot,
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
            "runtime_mode": "chat",  # 默认会话运行时；Agent 路由会覆盖为 agent
            "messages": [],  # 空消息列表
            "permissions": {
                "approval_mode": normalize_approval_mode(approval_mode or DEFAULT_APPROVAL_MODE).value,
                "policy_epoch": 1,
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
            for grant in grants:
                grant_type = str(grant.get("type") or "") if isinstance(grant, dict) else ""
                invalidated_session_capability = (
                    grant_type == "tool_action"
                    or (
                        grant.get("scope") == "session"
                        and grant_type.startswith("external_directory_")
                    )
                ) if isinstance(grant, dict) else False
                if invalidated_session_capability and not grant.get("revoked_at"):
                    grant["revoked_at"] = now
                    grant["revocation_reason"] = "permission_policy_changed"
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
        """Return session-scoped Skill activations, migrating legacy traces once."""

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
        """Persist newly loaded Skills for subsequent turns in the same session."""

        data = self._read_file(session_id)
        if not data:
            return []
        current = {str(item) for item in data.get("loaded_skill_ids") or [] if str(item)}
        current.update(str(item) for item in skill_ids if str(item))
        loaded = sorted(current)
        data["loaded_skill_ids"] = loaded
        self._write_file(session_id, data)
        return loaded

    def load_session(self, session_id: str) -> list[dict[str, Any]]:
        """加载指定会话的消息列表，自动合并 archive/ 中的归档消息。

        前端通过 /history 调用本方法时，始终看到完整历史（archive + 当前 messages）。
        """
        data = self._read_file(session_id)
        if not data:
            return []

        if isinstance(data.get("display_messages"), list):
            return list(data.get("display_messages", []))

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

        return messages

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
    ) -> None:
        """追加一条消息到会话历史"""
        data = self._read_file(session_id)  # 读取现有数据
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
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
        run_boundary_notice: dict[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        output_attachments: list[dict[str, Any]] | None = None,
        query_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Build the persisted message shape shared by append and upsert paths."""

        msg: dict[str, Any] = {"role": role, "content": content}
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
        if run_boundary_notice:
            msg["run_boundary_notice"] = run_boundary_notice
        if attachments:
            msg["attachments"] = [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or item.get("id") or "attachment"),
                    "type": str(item.get("type") or "file"),
                    "mime_type": str(item.get("mime_type") or ""),
                    "source": str(item.get("source") or "upload"),
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
                    "created_by_goal_id": str(item.get("created_by_goal_id") or ""),
                    "created_by_goal_revision": item.get("created_by_goal_revision"),
                    "download_url": str(item.get("download_url") or ""),
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
        result, or a Goal Run that has not achieved the current revision.
        The final Session write contains the assistant message, accepted report,
        RunOutcome and Goal decision together.
        """

        from harness.models import (
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
        if (
            validated_run.outcome != RunOutcome.COMPLETED
            or report is None
            or report.status
            not in {VerificationStatus.NOT_REQUIRED, VerificationStatus.SATISFIED}
        ):
            raise ValueError("Only an accepted completed Run may publish a final response")

        validated_goal: GoalRecord | None = None
        if goal is not None:
            validated_goal = GoalRecord.model_validate(goal)
            decision = validated_goal.latest_goal_decision
            if (
                validated_goal.session_id != session_id
                or validated_goal.goal_id != validated_run.goal_id
                or validated_goal.status != GoalStatus.ACHIEVED
                or decision is None
                or not decision.accepted
                or decision.accepted_run_id != validated_run.run_id
                or report.accepted_for_goal_revision is not True
            ):
                raise ValueError("Goal completion is not accepted for the current revision")

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        harness = data.setdefault("harness", {})
        runs = harness.setdefault("runs", {})
        if validated_run.run_id not in runs:
            raise ValueError(f"Run {validated_run.run_id} does not exist in session {session_id}")
        runs[validated_run.run_id] = validated_run.model_dump(mode="json")
        harness["latest_run_id"] = validated_run.run_id
        self._abandon_terminal_run_search_snapshots(data, validated_run)
        if validated_goal is not None:
            goals = harness.setdefault("goals", {})
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
                    collection[index] = dict(message)
                    break
            else:
                collection.append(dict(message))

        self._write_file(session_id, data)

    @_session_write_locked
    def set_assistant_run_boundary_notice(
        self,
        session_id: str,
        query_id: str,
        notice: dict[str, Any],
    ) -> None:
        """Persist a user-facing Run boundary without rewriting message content."""

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
        latest_run = (
            runs.get(latest_run_id) if isinstance(latest_run_id, str) else None
        )
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

        if (
            isinstance(latest_run, dict)
            and str(latest_run.get("status") or "") not in terminal_statuses
        ):
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
            ledgers[
                self._todo_scope_key(
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                    run_id=run_id,
                )
            ] = saved
        self._write_file(session_id, data)
        return deepcopy(saved)

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
            raise ValueError(
                f"Run {run_id} is {run.status}; expected one of {sorted(expected_statuses)}"
            )
        run.transition(RunStatus(status))
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
        activations.append(saved)
        run["verification_activations"] = activations
        run["updated_at"] = time.time()
        self._write_file(session_id, data)
        return deepcopy(saved), True

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
                goal_is_terminal = isinstance(raw_goal, dict) and str(
                    raw_goal.get("status") or ""
                ) in {"achieved", "cancelled", "budget_exceeded"}
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
                self._is_safe_handoff_evidence(data, item)
                for item in existing_refs
                if isinstance(item, dict)
            )
            if leases_changed or search_leases_changed or not handoff_is_safe:
                current.handoff_summary = self._build_run_handoff(data, current)
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
            {
                key: item.get(key)
                for key in ("id", "content", "status", "parent_id")
                if key in item
            }
            for item in (todos if isinstance(todos, list) else [])
            if isinstance(item, dict) and item.get("status") in {"completed", "cancelled"}
        ][-40:]
        refs: list[dict[str, Any]] = []
        for activation in run.verification_activations:
            for raw in activation.evidence_refs:
                if not isinstance(raw, dict):
                    continue
                projected = {
                    key: raw.get(key)
                    for key in (
                        "kind",
                        "scope",
                        "role",
                        "activation_id",
                        "tool_call_id",
                        "tool_name",
                        "source_id",
                        "result_id",
                        "generation_id",
                        "trace_id",
                        "artifact_id",
                        "path",
                        "host_path",
                        "virtual_path",
                        "content_sha256",
                        "size_bytes",
                        "uri",
                        "output_digest",
                        "source_hash",
                        "raw_output_ref",
                        "run_id",
                        "goal_id",
                        "goal_revision",
                    )
                    if raw.get(key) is not None
                }
                if projected:
                    refs.append(projected)
        refs = [item for item in refs if cls._is_safe_handoff_evidence(data, item)]
        workspace_artifact_refs = [
            item
            for item in refs
            if item.get("kind") == "artifact_write"
            and item.get("scope") in {"workspace", "attachment"}
        ]
        registry = data.get("delivered_artifacts")
        delivery_refs = [
            deepcopy(item)
            for item in registry.values()
            if isinstance(registry, dict)
            and isinstance(item, dict)
            and str(item.get("source_run_id") or "") == run.run_id
        ] if isinstance(registry, dict) else []
        artifact_refs = [*workspace_artifact_refs, *delivery_refs]
        sql_refs = [
            item
            for item in refs
            if str(item.get("tool_name") or "").startswith("database_sql_")
            or item.get("generation_id")
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
            return (
                str(lease.get("goal_id") or "") == goal_id
                and int(lease.get("goal_revision") or 1)
                == int(field("goal_revision") or field("objective_revision") or 1)
            )
        return (
            not str(lease.get("goal_id") or "")
            and str(lease.get("run_id") or "") == str(field("run_id") or "")
            and str(lease.get("query_id") or "")
            == str(field("query_id") or "")
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
                    or (
                        query_id
                        and str(lease.get("query_id") or "") != query_id
                    )
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
        file_path, directory_path = (
            (first, second)
            if first_kind == "exact_file"
            else (second, first)
        )
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
        target_path = str(
            lease.get("target_path")
            if lease_kind == "exact_file"
            else lease.get("directory_path")
            or ""
        )
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
                    else existing.get("directory_path")
                    or ""
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

        collection_name = (
            "external_artifact_leases"
            if lease_kind == "exact_file"
            else "external_directory_leases"
        )
        leases = data.setdefault(collection_name, {})
        saved = deepcopy(lease)
        saved["status"] = "claiming"
        leases[lease_id] = saved
        self._write_file(session_id, data)
        return deepcopy(saved)

    @staticmethod
    def _is_durable_handoff_artifact(data: dict[str, Any], artifact: dict[str, Any]) -> bool:
        """Return whether an artifact is a formal, durable delivery reference."""

        paths = [
            str(artifact.get(key) or "").replace("\\", "/")
            for key in ("path", "host_path", "virtual_path")
        ]
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
        paths = [
            str(evidence.get(key) or "").replace("\\", "/")
            for key in ("path", "host_path", "virtual_path")
        ]
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
        if run.verification_mode == VerificationMode.GOAL:
            return deepcopy(raw_run)
        if requested == VerificationMode.GOAL:
            raise ValueError("Goal verification mode requires an explicit Goal")
        if (
            run.verification_mode == VerificationMode.AGENT
            and requested == VerificationMode.PROPORTIONAL
        ):
            run.verification_mode = requested
            run.updated_at = time.time()
            saved = run.model_dump(mode="json")
            runs[run_id] = saved
            harness["latest_run_id"] = run_id
            self._write_file(session_id, data)
            return deepcopy(saved)
        return deepcopy(raw_run)

    @_session_write_locked
    def enhance_run_task_profile(
        self,
        session_id: str,
        run_id: str,
        enhancement: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Monotonically merge a late semantic Router result into a live Run.

        The Run already owns a deterministic baseline before this method can
        be called.  A slow or failed Router therefore cannot prevent Agent
        startup, remove acceptance requirements, or detach selected model
        context.
        """

        from harness.models import RunRecord, RunStatus, RunTaskProfile
        from harness.rubric_compiler import RubricBuildContext, RunRubricCompiler
        from harness.task_profiles import TaskProfileClassifier

        data = self._read_file(session_id)
        harness = data.get("harness") if data else None
        runs = harness.get("runs") if isinstance(harness, dict) else None
        raw_run = runs.get(run_id) if isinstance(runs, dict) else None
        if not isinstance(raw_run, dict):
            raise ValueError(f"Run {run_id} does not exist in session {session_id}")
        run = RunRecord.model_validate(raw_run)
        # The verification rubric and graph state freeze when the main Agent
        # enters RUNNING. A later semantic result remains advisory and must not
        # widen the persisted contract behind an already-running grader.
        if run.status != RunStatus.PREPARING:
            return deepcopy(raw_run), False

        semantic = RunTaskProfile.model_validate(enhancement)
        merged = TaskProfileClassifier.merge_semantic_enhancement(
            run.task_profile,
            semantic,
            analytics_model_id=run.analytics_model_id,
        )
        if merged == run.task_profile:
            return deepcopy(raw_run), False
        run.task_profile = merged

        if run.verification_enabled:
            completion = run.config_snapshot.get("completion")
            rubric = completion.get("rubric") if isinstance(completion, dict) else {}
            custom_rules = (
                list(rubric.get("custom_rules") or [])
                if isinstance(rubric, dict)
                and rubric.get("custom_rules_enabled", False)
                else []
            )
            declared = RunRubricCompiler.compile(
                RubricBuildContext(
                    user_message=run.objective,
                    analytics_model_id=run.analytics_model_id,
                    project_id=run.project_id,
                    custom_rules=tuple(custom_rules),
                    force_required=bool(run.goal_id),
                    task_profile=merged,
                )
            )
            run.declared_verification_contract = declared
            run.verification_contract = RunRubricCompiler.expand_for_activations(
                contract=declared,
                profile=merged,
                message=run.objective,
                activations=list(run.verification_activations),
            )

        run.updated_at = time.time()
        saved = run.model_dump(mode="json")
        runs[run_id] = saved
        harness["latest_run_id"] = run_id
        self._write_file(session_id, data)
        return deepcopy(saved), True

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
            item
            for item in run.task_profile.missing_explicit_skill_ids
            if item.lower() != normalized.lower()
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
            if isinstance(raw_goal, dict) and int(
                raw_goal.get("objective_revision") or 1
            ) == int(run.goal_revision or 1):
                inherited = SkillActivation.model_validate(
                    candidate.model_copy(update={"scope": "goal"})
                )
                existing = [
                    item
                    for item in raw_goal.get("skill_activations") or []
                    if isinstance(item, dict)
                    and str(item.get("activation_id") or "")
                    != inherited.activation_id
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
            if isinstance(raw_goal, dict) and int(
                raw_goal.get("objective_revision") or 1
            ) == int(run.goal_revision or 1):
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
        parsed = DelegationContract.model_validate(
            {**contract, "session_id": session_id, "parent_run_id": run_id}
        )
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
                "achieved",
                "cancelled",
                "budget_exceeded",
            }:
                raise ValueError(f"Goal {goal_id} is already terminal")
            if isinstance(existing_goal, dict):
                if existing_goal.get("status") != "active":
                    raise ValueError(
                        f"Goal {goal_id} is not active ({existing_goal.get('status')})"
                    )
                if existing_goal.get("requested_status"):
                    raise ValueError(
                        f"Goal {goal_id} has pending control request "
                        f"{existing_goal.get('requested_status')}"
                    )
                if existing_goal.get("current_run_id"):
                    raise ValueError(
                        f"Goal {goal_id} already has running Run "
                        f"{existing_goal.get('current_run_id')}"
                    )
                if int(existing_goal.get("objective_revision") or 1) != int(
                    goal.get("objective_revision") or 1
                ):
                    raise ValueError(f"Goal {goal_id} revision changed before Run start")
                saved_goal = deepcopy(existing_goal)
                run_ids = saved_goal.setdefault("run_ids", [])
                if run_id not in run_ids:
                    if int(saved_goal.get("round") or 0) >= int(
                        saved_goal.get("max_rounds") or 0
                    ):
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
        if status in {"achieved", "cancelled", "budget_exceeded"}:
            raise ValueError(f"Goal {goal_id} is already terminal ({status})")
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
        if raw_goal.get("status") in {"cancelled", "budget_exceeded", "achieved"}:
            self._abandon_uncommitted_execution_leases(data, raw_goal)
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
        harness = data.get("harness") if data else None
        goals = harness.get("goals") if isinstance(harness, dict) else None
        raw_goal = goals.get(goal_id) if isinstance(goals, dict) else None
        if not isinstance(raw_goal, dict):
            raise ValueError(f"Goal {goal_id} does not exist in session {session_id}")
        status = str(raw_goal.get("status") or "")
        if status in {"achieved", "cancelled", "budget_exceeded"}:
            raise ValueError(f"Goal {goal_id} is already terminal ({status})")
        current_revision = int(raw_goal.get("objective_revision") or 1)
        if current_revision != expected_revision:
            raise ValueError(
                f"Goal revision conflict: expected {expected_revision}, current {current_revision}."
            )
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
                        existing_contract.get("contract_id")
                        if isinstance(existing_contract, dict)
                        else None
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
        raw_goal["evidence_refs"] = []
        raw_goal["skill_activations"] = []
        raw_goal["latest_verification_report_id"] = None
        raw_goal["latest_goal_decision"] = None
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
                "achieved",
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
            raise ValueError(
                f"Goal {goal_id} current Run changed: expected {run_id}, got {current_run_id or 'none'}"
            )

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
        if saved.get("status") in {"achieved", "cancelled", "budget_exceeded"}:
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
        if isinstance(effective_query_id, str) and effective_query_id:
            saved["query_id"] = effective_query_id
            traces = data.get("traces")
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
    def delete_session(self, session_id: str) -> None:
        """删除会话文件"""
        path = self._session_path(session_id)  # 获取文件路径
        if path.exists():  # 存在则删除
            path.unlink()
        self._trace_path(session_id).unlink(missing_ok=True)
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
                message["output_attachments"] = deepcopy(by_query[query_id])
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
                "runtime_mode": raw.get("runtime_mode", "chat") if isinstance(raw, dict) else "chat",
            }
            if isinstance(raw, dict):
                for key in (
                    "project_id",
                    "project_path",
                    "workspace_type",
                    "workspace_path",
                    "analytics_model_id",
                ):
                    if key in raw:
                        meta[key] = raw.get(key)
            sessions.append(meta)  # 追加到结果
        return sessions  # 返回所有会话列表

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
            r"\bresult[_ -]?id\s*[:=]\s*([A-Za-z0-9_.:-]+)",
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
    ) -> dict[str, Any]:
        result_id = cls._tool_context_result_id(output)
        session_ref = {
            "kind": "session_tool_call",
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "source_hash": source_hash,
        }
        if result_id:
            # Query result artifacts currently have a TTL. Keep a Session-local
            # evidence fallback so an expired result_id never makes the UI's
            # “完整结果” reference irrecoverable.
            return {"kind": "result_id", "value": result_id, "fallback": session_ref}
        return session_ref

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

            completed: list[tuple[dict[str, Any], str, str, int, float]] = []
            for _, _, _, tool_call in self._iter_persisted_tool_calls(data):
                tool_call_id = str(tool_call.get("id") or "")
                output = self._tool_context_source(tool_call)
                if not tool_call_id or not output:
                    continue
                source_hash = self._tool_context_source_hash(output)
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
                    )
                )

            protected_ids = (
                {item[1] for item in sorted(completed, key=lambda item: item[4])[-max(0, int(keep_recent)) :]}
                if keep_recent > 0
                else set()
            )
            candidates: list[dict[str, Any]] = []
            cache_hit_count = 0
            for tool_call, tool_call_id, source_hash, estimated_tokens, completion_order in completed:
                if tool_call_id in protected_ids or estimated_tokens < max(1, int(min_result_tokens)):
                    continue
                metadata = tool_call.get("context_compaction")
                if (
                    isinstance(metadata, dict)
                    and metadata.get("status") == "ready"
                    and metadata.get("source_hash") == source_hash
                    and metadata.get("policy_version") == policy_version
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
                        ),
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
            changed = self._migrate_missing_tool_call_ids(session_id, data)
            changed = self._coalesce_replayed_tool_calls(data) or changed
            if not changed:
                return False
            self._write_file(session_id, data)
            return True

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
                current_hash = self._tool_context_source_hash(self._tool_context_source(tool_call))
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
            messages = data.get("agent_context_messages")
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
        """Persist DeepAgents' compact model context separately from UI history."""
        data = self._read_file(session_id)
        if not data:
            return
        data["agent_context_messages"] = messages
        data["agent_context_run_id"] = run_id
        self._write_file(session_id, data)

    def get_agent_context_messages(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load the compact model context saved by a previous Agent turn."""
        data = self._read_file(session_id)
        if not data:
            return []
        if run_id is not None and data.get("agent_context_run_id") != run_id:
            return []
        messages = data.get("agent_context_messages")
        if not isinstance(messages, list):
            return []
        return [item for item in messages if isinstance(item, dict)]

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
        artifact_id = "artifact-" + hashlib.sha256(
            f"external\0{normalized_target}".encode()
        ).hexdigest()[:20]
        delivery_receipt_id = "delivery-" + hashlib.sha256(
            f"{artifact_id}\0{content_sha256}".encode()
        ).hexdigest()[:20]
        now = time.time()
        registry = data.setdefault("delivered_artifacts", {})
        previous = registry.get(artifact_id)
        created_at = (
            float(previous.get("created_at") or now)
            if isinstance(previous, dict)
            else now
        )
        harness = data.get("harness")
        runs = harness.get("runs") if isinstance(harness, dict) else None
        source_run = runs.get(source_run_id) if isinstance(runs, dict) else None
        source_skill_ids = sorted(
            {
                str(item.get("skill_id") or "")
                for item in (
                    source_run.get("skill_activations")
                    if isinstance(source_run, dict)
                    else []
                )
                if isinstance(item, dict) and str(item.get("skill_id") or "")
            }
        )
        requested_validation_ids = {
            str(item) for item in (validation_receipt_ids or []) if str(item)
        }
        receipt_refs: list[dict[str, Any]] = []
        for activation in (
            source_run.get("verification_activations")
            if isinstance(source_run, dict)
            else []
        ) or []:
            if isinstance(activation, dict):
                receipt_refs.extend(
                    ref
                    for ref in activation.get("evidence_refs") or []
                    if isinstance(ref, dict) and ref.get("kind") == "validation_receipt"
                )
        goals = harness.get("goals") if isinstance(harness, dict) else None
        source_goal = (
            goals.get(source_goal_id)
            if isinstance(goals, dict) and source_goal_id
            else None
        )
        if (
            isinstance(source_goal, dict)
            and source_goal.get("objective_revision") == source_goal_revision
        ):
            receipt_refs.extend(
                ref
                for ref in source_goal.get("evidence_refs") or []
                if isinstance(ref, dict) and ref.get("kind") == "validation_receipt"
            )

        def receipt_matches_delivery(ref: dict[str, Any]) -> bool:
            if (
                str(ref.get("validation_receipt_id") or "")
                not in requested_validation_ids
                or not bool(ref.get("commit_authority"))
                or str(ref.get("status") or "passed") != "passed"
                or int(ref.get("exit_code", -1)) != 0
                or int(ref.get("checks_failed") or 0) != 0
            ):
                return False
            return any(
                isinstance(item, dict)
                and str(Path(str(item.get("path") or "")).expanduser().resolve())
                == normalized_target
                and str(item.get("content_sha256") or "") == content_sha256
                for item in ref.get("artifact_refs") or []
            )

        accepted_receipts = [
            ref for ref in receipt_refs if receipt_matches_delivery(ref)
        ]
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
            and str(Path(str(item.get("path") or "")).expanduser().resolve())
            != normalized_target
        }
        selected_related_ids = inferred_related_ids | {
            str(item)
            for item in (related_artifact_ids or [])
            if str(item) and str(item) != artifact_id
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
            isinstance(item, dict)
            and item.get("delivery_receipt_id") == delivery_receipt_id
            for item in history
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
        artifact_id = "artifact-" + hashlib.sha256(
            f"external\0{normalized_target}".encode()
        ).hexdigest()[:20]
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
            artifacts = [
                item for item in artifacts if str(item.get("status") or "active") == "active"
            ]
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
            selected_ids = {
                str(item.get("artifact_id") or "") for item in explicit
            }
            for item in explicit:
                selected_ids.update(
                    str(value) for value in item.get("related_artifact_ids") or []
                )
            resolved = [
                item
                for item in artifacts
                if str(item.get("artifact_id") or "") in selected_ids
            ][: max(1, limit)]
            emit_harness_metric(
                logger,
                "artifact_handoff_hit_count",
                session_id=session_id,
                value=len(resolved),
                route="explicit",
            )
            return resolved
        if not re.search(
            r"(?:继续(?:修改|修复|更新|补充)|再试|再来|还是(?:没有|没|不对)|仍然(?:没有|没|不对)|还没|没有更新|没更新|补上|修复(?:这个|该)|(?:这个|刚才|上一轮).*(?:产物|文件|报告|图表|页面|代码).*(?:不对|有误|没更新|修复))",
            text,
        ):
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
            if (
                source_run_id
                and str(item.get("source_run_id") or "") == source_run_id
            )
            or (
                source_goal_id
                and str(item.get("source_goal_id") or "") == source_goal_id
                and item.get("source_goal_revision")
                == latest.get("source_goal_revision")
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
            if isinstance(lease, dict)
            and str(lease.get("target_path") or "") == target_path
            and same_owner(lease)
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
            if lease.get("status") == "staged"
            and float(lease.get("created_at") or 0) > latest_commit_at
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
                        if (
                            artifact_id
                            and str(item.get("artifact_id") or "") == artifact_id
                        )
                        or (
                            formal_target
                            and str(item.get("target_path") or "") == formal_target
                        )
                    ),
                    None,
                )
                if not isinstance(latest, dict) or str(latest.get("status") or "active") != "active":
                    return observed({
                        "status": "artifact_stale",
                        "formal_target_path": formal_target or None,
                        "stale_reason": (
                            latest.get("stale_reason")
                            if isinstance(latest, dict)
                            else "delivery_registry_missing"
                        ),
                    })
                return observed({
                    "status": "durable",
                    "formal_target_path": latest.get("target_path"),
                    "content_sha256": latest.get("content_sha256"),
                    "delivered_artifact_id": latest.get("artifact_id"),
                })
            if lease.get("status") in {"abandoned", "superseded", "expired"}:
                return observed({
                    "status": "artifact_not_durable",
                    "lease_id": lease.get("lease_id"),
                    "lease_status": lease.get("status"),
                })
            return None
        for lease in self.list_external_directory_leases(session_id):
            staged_dir = str(lease.get("staged_dir") or "").replace("\\", "/").rstrip("/")
            if not staged_dir or not (
                normalized == staged_dir or normalized.startswith(f"{staged_dir}/")
            ):
                continue
            if lease.get("status") == "committed":
                relative = posixpath.relpath(normalized, staged_dir)
                target = str(
                    (
                        Path(str(lease.get("directory_path") or "")).expanduser().resolve()
                        / relative
                    ).resolve()
                )
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
                    return observed({
                        "status": "artifact_stale",
                        "formal_target_path": target,
                        "stale_reason": (
                            artifact.get("stale_reason")
                            if isinstance(artifact, dict)
                            else "delivery_registry_missing"
                        ),
                    })
                return observed({
                    "status": "durable",
                    "formal_target_path": target,
                    "content_sha256": (
                        artifact.get("content_sha256") if isinstance(artifact, dict) else None
                    ),
                    "delivered_artifact_id": (
                        artifact.get("artifact_id") if isinstance(artifact, dict) else None
                    ),
                })
            if lease.get("status") in {"abandoned", "superseded", "expired"}:
                return observed({
                    "status": "artifact_not_durable",
                    "lease_id": lease.get("lease_id"),
                    "lease_status": lease.get("status"),
                })
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
                return (
                    str(lease.get("goal_id") or "") == goal_id
                    and lease.get("goal_revision") == goal_revision
                )
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
        if (
            lease.get("status") != "publishing"
            or str(lease.get("publish_tool_call_id") or "") != tool_call_id
        ):
            raise RuntimeError("AttachmentEditLease publish claim no longer belongs to this Tool call")
        lease.update(deepcopy(published_fields))
        lease["status"] = "published"

        query_id = str(delivery.get("created_by_query_id") or lease.get("query_id") or "")
        attachment_id = str(delivery.get("id") or "")
        if not query_id or not attachment_id:
            raise ValueError("attachment delivery requires query and attachment ids")
        outbox = data.setdefault("attachment_deliveries", {})
        entries = outbox.setdefault(query_id, [])
        if not any(
            isinstance(item, dict) and str(item.get("id") or "") == attachment_id
            for item in entries
        ):
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
        if (
            lease.get("status") == "publishing"
            and str(lease.get("publish_tool_call_id") or "") == tool_call_id
        ):
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
        """Atomically persist Agent usage and, when compacted, its model context."""
        data = self._read_file(session_id)
        if not data:
            return
        data["agent_context_usage"] = max(0, int(used_tokens))
        if messages is not None:
            data["agent_context_messages"] = messages
            data["agent_context_run_id"] = run_id
        self._write_file(session_id, data)

    # ── Host-file mutation receipts ──────────────────────────────────────────

    @_session_write_locked
    def append_external_mutation_receipt(
        self,
        session_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one immutable HostFileBroker mutation receipt."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        receipt_id = str(receipt.get("receipt_id") or "")
        if not receipt_id:
            raise ValueError("external mutation receipt requires receipt_id")
        receipts = data.setdefault("external_mutation_receipts", {})
        existing = receipts.get(receipt_id)
        if isinstance(existing, dict):
            if existing != receipt:
                raise ValueError(f"external mutation receipt {receipt_id} is immutable")
            return deepcopy(existing)
        receipts[receipt_id] = deepcopy(receipt)
        self._write_file(session_id, data)
        return deepcopy(receipts[receipt_id])

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
            if isinstance(item, dict)
            and (run_id is None or str(item.get("run_id") or "") == run_id)
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
            and (
                not after_sha256
                or str(item.get("after_sha256") or "") == after_sha256
            )
        ]
        return deepcopy(matches[-1]) if matches else None

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
            if isinstance(grant, dict)
            and not grant.get("revoked_at")
            and not grant.get("superseded_at")
        ]

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
            if isinstance(grant, dict)
            and (grant.get("revoked_at") or grant.get("superseded_at"))
        ]
        inactive.sort(
            key=lambda grant: float(
                grant.get("consumed_at")
                or grant.get("revoked_at")
                or grant.get("created_at")
                or 0
            ),
            reverse=True,
        )
        return inactive[: max(0, int(limit))]

    @_session_write_locked
    def migrate_permission_grants(self, session_id: str) -> int:
        """Persist v2 semantic bindings and supersede active duplicates."""

        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")
        permissions = data.get("permissions")
        grants = permissions.get("grants") if isinstance(permissions, dict) else None
        if not isinstance(grants, list):
            return 0
        changed = self._migrate_permission_grants(session_id, grants)
        if changed:
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
    ) -> dict[str, Any]:
        """Persist a session permission grant and return it."""
        data = self._read_file(session_id)
        if not data:
            raise FileNotFoundError(f"Session {session_id} not found")

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
            expected_bindings = RunPermissionContext.from_config_snapshot(
                run.get("config_snapshot")
            ).grant_bindings()
            if bindings != expected_bindings:
                raise ValueError("Permission request does not match the active Run")
            normalized_bindings = deepcopy(bindings)

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
                if (
                    not isinstance(existing, dict)
                    or existing.get("revoked_at")
                    or existing.get("superseded_at")
                ):
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
                "backend_id_at_approval": str(
                    (normalized_bindings or {}).get("backend_id") or ""
                ),
            },
        }
        if metadata:
            grant["metadata"] = dict(metadata)
        if normalized_bindings is not None:
            grant["bindings"] = normalized_bindings
        grants.append(grant)
        permissions["grants"] = grants
        data["permissions"] = permissions
        self._write_file(session_id, data)
        return dict(grant)

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
            semantic_bindings = SessionManager._permission_semantic_runtime_bindings(
                scope=str(raw.get("scope") or "session"),
                metadata=metadata,
                bindings=bindings,
            )
            key, stable = PermissionBindingPolicy.semantic_key(
                session_id=session_id,
                grant_type=str(raw.get("type") or ""),
                scope=str(raw.get("scope") or "session"),
                target_kind=str(raw.get("target_kind") or ""),
                target=str(raw.get("target") or ""),
                capabilities=[str(item) for item in raw.get("capabilities") or []],
                runtime_bindings=semantic_bindings,
            )
            desired = {
                "binding_schema_version": PERMISSION_BINDING_SCHEMA_VERSION,
                "semantic_key": key,
                "stable_bindings": stable,
            }
            for field, value in desired.items():
                if raw.get(field) != value:
                    raw[field] = value
                    changed = True
            if "runtime_observations" not in raw:
                raw["runtime_observations"] = {
                    "backend_id_at_approval": str((bindings or {}).get("backend_id") or "")
                }
                changed = True
            if not raw.get("revoked_at") and not raw.get("superseded_at"):
                active_by_key.setdefault(key, []).append(raw)

        now = time.time()
        for group in active_by_key.values():
            if len(group) < 2:
                continue
            authoritative = max(
                group,
                key=lambda item: float(
                    item.get("last_approved_at") or item.get("created_at") or 0
                ),
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
            scope="session" if target == "session_network_access" else "once",
            target_kind="capability" if target == "session_network_access" else "fingerprint",
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
            required = RunPermissionContext.from_config_snapshot(
                run.get("config_snapshot")
            ).grant_bindings()
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

    def load_session_for_agent(self, session_id: str) -> list[dict[str, Any]]:
        """加载会话历史并格式化为 LLM 可用的消息列表

        两个关键处理：
        1. 合并连续的普通 assistant 文本消息（保持 user/assistant 严格交替）
        2. 如有压缩摘要，在头部注入一条摘要消息让 LLM 保留历史上下文
        """
        data = self._read_file(session_id)  # 读取会话数据
        messages = data.get("messages", []) if data else []  # 取消息列表

        merged: list[dict[str, Any]] = []  # 合并后的结果列表

        # 如有压缩摘要，作为第一条 assistant 消息注入（让 LLM 知道之前聊了什么）
        compressed = data.get("compressed_context", "") if data else ""  # 读取摘要
        if compressed:  # 摘要存在则注入
            merged.append(
                {
                    "role": "assistant",  # 伪装为 assistant 消息
                    "content": f"{COMPRESSED_CONTEXT_PREFIX}\n{compressed}",  # 前缀标识 + 摘要内容
                }
            )

        middle_trim_context = data.get("middle_trim_context", "") if data else ""
        if middle_trim_context:
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
            # New Runs receive user-visible conversation only. Raw Tool
            # inputs/outputs and reasoning belong to their source Run; legal
            # continuity is carried by RunHandoffSummary and evidence refs.
            # 合并判断仍依赖原始消息是否携带 tool_calls，但不要把 tool_calls 放进 entry。
            msg_has_tool_calls = bool(msg.get("tool_calls"))
            prev_has_tool_calls = bool(merged[-1].get("_had_tool_calls")) if merged else False
            if (
                merged  # 列表非空
                and merged[-1]["role"] == "assistant"  # 上一条是 assistant
                and msg["role"] == "assistant"  # 当前也是 assistant
                and not prev_has_tool_calls  # 上一条也不能是 tool_call 消息
                and not msg_has_tool_calls  # 当前消息无 tool_calls 才合并
            ):
                merged[-1]["content"] += "\n" + msg["content"]  # 合并为一条（避免连续 assistant）
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
        if "harness" in data:
            del data["harness"]
        self._write_file(session_id, data)  # 写回磁盘


# 全局单例，整个后端进程共用一个 SessionManager 实例
session_manager = SessionManager()
