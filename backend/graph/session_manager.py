"""SessionManager — 短期记忆管理器，基于 JSON 文件持久化会话历史"""

import json      # JSON 序列化/反序列化
import time      # 获取时间戳
from pathlib import Path      # 路径操作
from typing import Any        # 类型注解

# 压缩摘要的固定前缀标识，agent.py 和本模块共用，用于识别摘要消息
COMPRESSED_CONTEXT_PREFIX = "[历史对话摘要]"
MIDDLE_TRIM_CONTEXT_PREFIX = "[中段历史摘要]"


class SessionManager:
    """短期记忆核心类：将每个会话的消息历史存为 sessions/{id}.json 文件"""

    def __init__(self) -> None:
        self._sessions_dir: Path | None = None  # 会话文件存储目录，initialize() 时设置

    def initialize(self, base_dir: Path) -> None:
        """初始化：设置存储目录为 base_dir/sessions/，不存在则创建"""
        self._sessions_dir = base_dir / "sessions"  # 拼接会话目录路径
        self._sessions_dir.mkdir(exist_ok=True)      # 目录不存在时自动创建

    def _session_path(self, session_id: str) -> Path:
        """根据 session_id 生成对应的 JSON 文件路径"""
        assert self._sessions_dir is not None                                    # 确保已初始化
        safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")     # 过滤特殊字符防路径注入
        return self._sessions_dir / f"{safe_id}.json"                            # 返回完整文件路径

    def _read_file(self, session_id: str) -> dict[str, Any]:
        """从磁盘读取会话文件，自动兼容 v1(纯列表) → v2(带元数据的字典) 格式"""
        path = self._session_path(session_id)          # 获取文件路径
        if not path.exists():                          # 文件不存在返回空字典
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))  # 读取并解析 JSON
            if isinstance(data, list):                           # v1 格式：纯消息列表
                now = time.time()                                # 获取当前时间戳
                return {                                         # 转换为 v2 格式
                    "title": session_id,                         # 用 session_id 作为默认标题
                    "created_at": path.stat().st_ctime,          # 用文件创建时间作为会话创建时间
                    "updated_at": now,                           # 更新时间设为当前
                    "messages": data,                            # 原始消息列表保留
                }
            return data                                          # v2 格式直接返回
        except (json.JSONDecodeError, Exception):                # JSON 解析失败返回空
            return {}

    def _write_file(self, session_id: str, data: dict[str, Any]) -> None:
        """将会话数据写入磁盘，自动更新 updated_at 时间戳"""
        data["updated_at"] = time.time()                                   # 每次写入都刷新更新时间
        path = self._session_path(session_id)                              # 获取文件路径
        path.write_text(                                                   # 写入 JSON 文件
            json.dumps(data, ensure_ascii=False, indent=2),                # 中文不转义，缩进 2 格
            encoding="utf-8",                                              # UTF-8 编码
        )

    def create_session(self, session_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """创建空会话，返回元数据（id/title/时间戳）"""
        now = time.time()                    # 当前时间戳
        data: dict[str, Any] = {             # 初始会话结构
            "title": "New Chat",             # 默认标题
            "created_at": now,               # 创建时间
            "updated_at": now,               # 更新时间
            "runtime_mode": "chat",          # 默认会话运行时；Agent 路由会覆盖为 agent
            "messages": [],                  # 空消息列表
        }
        if metadata:
            data.update(metadata)
        self._write_file(session_id, data)   # 写入磁盘
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
        ):
            if key in data:
                meta[key] = data.get(key)
        return meta

    def update_metadata(self, session_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """Merge metadata into a session, creating the session if needed."""
        data = self._read_file(session_id)
        if not data:
            return self.create_session(session_id, metadata=metadata)
        data.update(metadata)
        self._write_file(session_id, data)
        return self._metadata_from_data(session_id, data)

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

    def save_message(
        self,
        session_id: str,                                      # 会话 ID
        role: str,                                            # 角色：user 或 assistant
        content: str,                                         # 消息内容
        tool_calls: list[dict[str, Any]] | None = None,       # 可选的工具调用记录
        sources: list[dict[str, Any]] | None = None,          # 用户可见的结构化来源
        citations: list[dict[str, Any]] | None = None,        # 正文与来源的引用映射
        reasoning_content: str | None = None,                  # 思考链内容（工具调用回合必须回传）
    ) -> None:
        """追加一条消息到会话历史"""
        data = self._read_file(session_id)        # 读取现有数据
        if not data:                              # 会话不存在则创建新的
            now = time.time()                     # 当前时间戳
            data = {                              # 初始化会话结构
                "title": "New Chat",              # 默认标题
                "created_at": now,                # 创建时间
                "updated_at": now,                # 更新时间
                "messages": [],                   # 空消息列表
            }
        msg: dict[str, Any] = {"role": role, "content": content}  # 构造消息字典
        if tool_calls:                                            # 有工具调用则附加
            msg["tool_calls"] = tool_calls
        if reasoning_content:                                     # 思考链内容持久化，支持历史回看与 API 回传
            msg["reasoning_content"] = reasoning_content
        if sources:
            msg["sources"] = sources
        if citations:
            msg["citations"] = citations
        data["messages"].append(msg)              # 追加到消息列表末尾
        if isinstance(data.get("display_messages"), list):
            data["display_messages"].append(dict(msg))
        self._write_file(session_id, data)        # 写回磁盘

    def rename_session(self, session_id: str, title: str) -> None:
        """重命名会话标题"""
        data = self._read_file(session_id)                             # 读取会话数据
        if not data:                                                   # 会话不存在则报错
            raise FileNotFoundError(f"Session {session_id} not found")
        data["title"] = title                                          # 更新标题
        self._write_file(session_id, data)                             # 写回磁盘

    def update_title(self, session_id: str, title: str) -> None:
        """更新标题（rename_session 的别名，供 API 层调用）"""
        self.rename_session(session_id, title)

    def delete_session(self, session_id: str) -> None:
        """删除会话文件"""
        path = self._session_path(session_id)    # 获取文件路径
        if path.exists():                        # 存在则删除
            path.unlink()

    def get_raw_messages(self, session_id: str) -> dict[str, Any]:
        """返回完整会话数据（含标题、时间戳、所有消息），供前端展示"""
        data = self._read_file(session_id)                     # 读取会话文件
        if not data:                                           # 不存在返回空结构
            return {"title": "", "messages": []}
        data = dict(data)
        data["messages"] = self.load_session(session_id)
        return data                                            # 返回完整数据

    def get_active_messages(self, session_id: str) -> list[dict[str, Any]]:
        """返回当前 session.json 中尚未归档的活跃消息。仅供 Agent 上下文优化使用。"""
        data = self._read_file(session_id)
        if not data:
            return []
        return list(data.get("messages", []))

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有会话的元数据（id/title/updated_at），按修改时间倒序"""
        assert self._sessions_dir is not None                  # 确保已初始化
        sessions: list[dict[str, Any]] = []                    # 结果列表
        for f in sorted(self._sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):  # 遍历所有 JSON 文件，按修改时间倒序
            raw: Any = None
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))         # 解析 JSON
                if isinstance(raw, dict):                               # v2 格式
                    title = raw.get("title", f.stem)                    # 取标题，缺省用文件名
                    updated_at = raw.get("updated_at", f.stat().st_mtime)  # 取更新时间
                else:                                                   # v1 格式（纯列表）
                    title = f.stem                                      # 用文件名作标题
                    updated_at = f.stat().st_mtime                      # 用文件修改时间
            except Exception:                                           # 解析失败兜底
                title = f.stem                                          # 用文件名
                updated_at = f.stat().st_mtime                          # 用文件修改时间

            meta = {
                "id": f.stem,                    # 会话 ID = 文件名（不含 .json）
                "title": title,                  # 会话标题
                "updated_at": updated_at,        # 最后更新时间
                "runtime_mode": raw.get("runtime_mode", "chat") if isinstance(raw, dict) else "chat",
            }
            if isinstance(raw, dict):
                for key in (
                    "project_id",
                    "project_path",
                    "workspace_type",
                    "workspace_path",
                ):
                    if key in raw:
                        meta[key] = raw.get(key)
            sessions.append(meta)                 # 追加到结果
        return sessions                          # 返回所有会话列表

    # ── 短期记忆压缩（核心机制）────────────────────────────────────────────────

    def compress_history(
        self, session_id: str, summary: str, num_to_remove: int
    ) -> None:
        """压缩短期记忆：归档旧消息 + 保存 LLM 生成的摘要"""
        assert self._sessions_dir is not None                  # 确保已初始化
        data = self._read_file(session_id)                     # 读取当前会话
        if not data:                                           # 会话不存在则跳过
            return

        messages = data.get("messages", [])                    # 获取消息列表
        archived_messages = messages[:num_to_remove]           # 取出要归档的前 N 条消息

        # 将被压缩的消息归档到 sessions/archive/ 目录（备份，不丢失原始数据）
        archive_dir = self._sessions_dir / "archive"           # 归档目录路径
        archive_dir.mkdir(exist_ok=True)                       # 不存在则创建
        archive_data = {                                       # 归档数据结构
            "session_id": session_id,                          # 所属会话
            "archived_at": time.time(),                        # 归档时间戳
            "messages": archived_messages,                     # 被归档的消息
        }
        archive_path = archive_dir / f"{session_id}_{int(time.time())}.json"  # 归档文件名含时间戳防重复
        archive_path.write_text(                               # 写入归档文件
            json.dumps(archive_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        data["messages"] = messages[num_to_remove:]            # 从会话中删除已归档的消息

        # 将摘要追加到 compressed_context 字段（支持多次压缩，用 --- 分隔）
        existing_context = data.get("compressed_context", "")  # 读取已有摘要
        if existing_context:                                   # 已有摘要则拼接
            data["compressed_context"] = existing_context + "\n---\n" + summary
        else:                                                  # 首次压缩直接写入
            data["compressed_context"] = summary

        self._write_file(session_id, data)                     # 写回磁盘

    def get_compressed_context(self, session_id: str) -> str | None:
        """获取压缩摘要（如果存在）"""
        data = self._read_file(session_id)              # 读取会话数据
        if not data:                                    # 不存在返回 None
            return None
        return data.get("compressed_context")           # 返回摘要字段

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
        data["middle_trim_context"] = (
            existing_context + "\n---\n" + block if existing_context else block
        )

        self._write_file(session_id, data)
        return archive_name

    def get_middle_trim_context(self, session_id: str) -> str | None:
        """获取中段裁剪摘要（如果存在）。"""
        data = self._read_file(session_id)
        if not data:
            return None
        return data.get("middle_trim_context")

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

    # ── 为 Agent（LLM）准备消息 ─────────────────────────────────────────────────

    def load_session_for_agent(self, session_id: str) -> list[dict[str, Any]]:
        """加载会话历史并格式化为 LLM 可用的消息列表

        两个关键处理：
        1. 合并连续的普通 assistant 文本消息（保持 user/assistant 严格交替）
        2. 如有压缩摘要，在头部注入一条摘要消息让 LLM 保留历史上下文
        """
        data = self._read_file(session_id)                              # 读取会话数据
        messages = data.get("messages", []) if data else []             # 取消息列表

        merged: list[dict[str, Any]] = []                               # 合并后的结果列表

        # 如有压缩摘要，作为第一条 assistant 消息注入（让 LLM 知道之前聊了什么）
        compressed = data.get("compressed_context", "") if data else ""  # 读取摘要
        if compressed:                                                   # 摘要存在则注入
            merged.append({
                "role": "assistant",                                     # 伪装为 assistant 消息
                "content": f"{COMPRESSED_CONTEXT_PREFIX}\n{compressed}", # 前缀标识 + 摘要内容
            })

        middle_trim_context = data.get("middle_trim_context", "") if data else ""
        if middle_trim_context:
            merged.append({
                "role": "assistant",
                "content": (
                    f"{MIDDLE_TRIM_CONTEXT_PREFIX}\n"
                    "以下内容是因上下文裁剪移出活跃消息的历史任务状态摘要。"
                    "它只用于理解历史完成情况，不代表当前任务结果，也不要在新任务中续写。\n"
                    f"{middle_trim_context}"
                ),
            })

        for msg in messages:                                             # 遍历所有消息
            entry: dict[str, Any] = {"role": msg["role"], "content": msg["content"]}
            if msg.get("tool_calls"):
                entry["tool_calls"] = msg["tool_calls"]
            # 思考模式下，assistant 消息的 reasoning_content 需要回传给 API（含工具调用时尤其关键）
            if msg.get("reasoning_content"):
                entry["reasoning_content"] = msg["reasoning_content"]
            prev_has_tool_calls = bool(merged[-1].get("tool_calls")) if merged else False
            current_has_tool_calls = bool(entry.get("tool_calls"))
            if (
                merged                                                   # 列表非空
                and merged[-1]["role"] == "assistant"                     # 上一条是 assistant
                and msg["role"] == "assistant"                            # 当前也是 assistant
                and not prev_has_tool_calls                                # 上一条也不能是 tool_call 消息
                and not current_has_tool_calls                             # 当前消息无 tool_calls 才合并
            ):
                merged[-1]["content"] += "\n" + msg["content"]           # 合并为一条（避免连续 assistant）
            else:
                merged.append(entry)
        return merged                                                    # 返回格式化后的消息列表

    def get_message_count(self, session_id: str) -> int:
        """返回会话中的消息总数（用于判断是否触发自动压缩）"""
        data = self._read_file(session_id)          # 读取会话数据
        if not data:                                # 不存在返回 0
            return 0
        return len(data.get("messages", []))        # 返回消息数量

    def clear_messages(self, session_id: str) -> None:
        """清空会话消息，但保留标题等元数据"""
        data = self._read_file(session_id)          # 读取会话数据
        if not data:                                # 不存在则跳过
            return
        data["messages"] = []                       # 清空消息列表
        if "display_messages" in data:
            del data["display_messages"]
        if "compressed_context" in data:            # 同时清除压缩摘要
            del data["compressed_context"]
        if "middle_trim_context" in data:
            del data["middle_trim_context"]
        self._write_file(session_id, data)          # 写回磁盘


# 全局单例，整个后端进程共用一个 SessionManager 实例
session_manager = SessionManager()
