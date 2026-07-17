"""SessionManager — 短期记忆管理器，基于 JSON 文件持久化会话历史"""

import hashlib
import json
import re
import threading
import time
import uuid
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any

from graph.permission_policy import (
    DEFAULT_APPROVAL_MODE,
    RunPermissionContext,
    normalize_approval_mode,
    permission_policy_snapshot,
)

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
        self._sessions_dir = base_dir / "sessions"  # 拼接会话目录路径
        self._sessions_dir.mkdir(exist_ok=True)  # 目录不存在时自动创建
        self._traces_dir = self._sessions_dir / "traces"
        self._traces_dir.mkdir(exist_ok=True)

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
                if isinstance(grant, dict) and grant.get("type") == "tool_action" and not grant.get("revoked_at"):
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

    def get_todos(self, session_id: str) -> list[dict[str, Any]]:
        """Return the persisted todo list for a session."""
        data = self._read_file(session_id)
        if not data:
            return []
        todos = data.get("todos")
        return list(todos) if isinstance(todos, list) else []

    @_session_write_locked
    def update_todos(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist the todo list and return the saved list."""
        data = self._read_file(session_id)
        if not data:
            return []
        data["todos"] = list(todos)
        self._write_file(session_id, data)
        return list(todos)

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
                contract=run.verification_contract,
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
            return deepcopy(raw_current)

        if current.verification_enabled:
            current.verification_contract = RunRubricCompiler.expand_for_activations(
                contract=current.verification_contract,
                profile=current.task_profile,
                message=current.objective,
                activations=list(current.verification_activations),
            )
        current.verification_report = incoming.verification_report
        current.model_call_count = incoming.model_call_count
        current.budget_exhaustion_reason = incoming.budget_exhaustion_reason
        current.finish(incoming.outcome, error=incoming.error)
        saved = current.model_dump(mode="json")
        runs[run_id] = saved
        harness["latest_run_id"] = run_id
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
        if isinstance(session.get("todos"), list):
            result["todos"] = list(session["todos"])
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

    def get_raw_messages(self, session_id: str) -> dict[str, Any]:
        """Return session data without loading heavyweight trace sidecars."""
        data = self._read_file(session_id)  # 读取会话文件
        if not data:  # 不存在返回空结构
            return {"title": "", "messages": []}
        data = dict(data)
        data["messages"] = self.load_session(session_id)
        # Lightweight runtime state remains available, but trace data has a
        # dedicated lazy endpoint and is never read by the conversation view.
        if isinstance(data.get("todos"), list):
            data["todos"] = list(data["todos"])
        else:
            data.pop("todos", None)
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
        """Persist stable IDs for legacy tool calls before model reconstruction."""

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data or not self._migrate_missing_tool_call_ids(session_id, data):
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

        with self._tool_context_lock(session_id):
            data = self._read_file(session_id)
            if not data:
                return {}
            ready: dict[str, str] = {}
            for _, _, _, tool_call in self._iter_persisted_tool_calls(data):
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
                ready[tool_call_id] = str(context_output)
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
    ) -> None:
        """Persist DeepAgents' compact model context separately from UI history."""
        data = self._read_file(session_id)
        if not data:
            return
        data["agent_context_messages"] = messages
        self._write_file(session_id, data)

    def get_agent_context_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Load the compact model context saved by a previous Agent turn."""
        data = self._read_file(session_id)
        if not data:
            return []
        messages = data.get("agent_context_messages")
        if not isinstance(messages, list):
            return []
        return [item for item in messages if isinstance(item, dict)]

    @_session_write_locked
    def update_agent_context_state(
        self,
        session_id: str,
        *,
        used_tokens: int,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Atomically persist Agent usage and, when compacted, its model context."""
        data = self._read_file(session_id)
        if not data:
            return
        data["agent_context_usage"] = max(0, int(used_tokens))
        if messages is not None:
            data["agent_context_messages"] = messages
        self._write_file(session_id, data)

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
        return [dict(grant) for grant in grants if isinstance(grant, dict) and not grant.get("revoked_at")]

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

        now = time.time()
        grant = {
            "id": f"grant-{uuid.uuid4().hex[:12]}",
            "type": grant_type,
            "scope": scope,
            "target_kind": target_kind,
            "target": target,
            "capabilities": list(dict.fromkeys(capabilities)),
            "source": source,
            "created_at": now,
        }
        if metadata:
            grant["metadata"] = dict(metadata)
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
            grant["bindings"] = deepcopy(bindings)
        grants.append(grant)
        permissions["grants"] = grants
        data["permissions"] = permissions
        self._write_file(session_id, data)
        return dict(grant)

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
            if required_bindings is not None and grant.get("bindings") != required_bindings:
                continue
            if required_capabilities is not None and not set(required_capabilities).issubset(
                set(grant.get("capabilities") or [])
            ):
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
            # 历史 tool_calls 不再回传给模型：
            # 1. 我们的存储把 tool 结果合并到 assistant message，缺少独立 tool role 消息，
            #    直接回传会导致 OpenAI API 报 duplicate tool_call_id。
            # 2. 历史工具调用会在 LangGraph 流中重新被 emit，污染当前轮次时间轴。
            # 结构化 tool_calls 不回传，但已完成工具输出必须以普通文本摘要回传，
            # 否则用户说“继续”时模型看不到中断前已经查到的事实。
            if msg.get("tool_calls"):
                entry["content"] += self._tool_result_context(msg.get("tool_calls") or [])
            # 思考模式下，assistant 消息的 reasoning_content 需要回传给 API（含工具调用时尤其关键）
            if msg.get("reasoning_content"):
                entry["reasoning_content"] = msg["reasoning_content"]
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
