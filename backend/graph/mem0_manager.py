"""Mem0Manager — mem0 长期记忆管理器，懒加载单例模式。

仅当 config.json 中 memory_backend == "mem0" 时才初始化 mem0 实例。
mem0 未安装时不崩溃，调用方检查 is_available() 后再使用。

融合 Claude Code 记忆类型体系（user/feedback/project/reference），
在检索阶段对 mem0 结果进行分类，支持结构化上下文注入。
"""

from typing import Any

# Claude Code 记忆类型体系的规则分类关键词
# 优先使用 mem0 metadata 中的 type 字段；若无则按此规则兜底
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "feedback": [
        "不要", "禁止", "规则", "避免", "风格", "方式", "偏好操作",
        "总结模式", "不需要", "请不要", "停止", "不再",
    ],
    "project": [
        "项目", "代码库", "功能需求", "截止", "版本", "部署",
        "任务", "进度", "正在做", "开发中", "需求",
    ],
    "reference": [
        "文档", "链接", "地址", "文件路径", "目录路径", "api端点", "配置文件",
        "仓库", "url", "endpoint",
    ],
}


class Mem0Manager:
    """mem0 长期记忆单例管理器。"""

    def __init__(self) -> None:
        self._memory = None          # mem0 Memory 实例（懒加载）
        self._initialized = False    # 是否已尝试初始化
        self._available = False      # 初始化是否成功

    def _ensure_initialized(self) -> None:
        """懒加载：首次调用时初始化 mem0，连接失败时允许下次重试。

        ImportError（未安装 mem0 包本身）标记为永久不可用，不重试。
        其他异常（Milvus 未就绪、grpc 内部 ImportError 等）允许下次重试。
        """
        if self._initialized:
            return

        # 第一步：单独检测 mem0 是否安装
        try:
            from mem0 import Memory
        except ImportError:
            self._initialized = True
            self._available = False
            print("⚠️ mem0 未安装，请运行: pip install mem0ai")
            return

        # 第二步：初始化实例（Milvus 连接等），失败则保留重试机会
        try:
            from config import get_mem0_config

            config = get_mem0_config()

            # Milvus TCP 预探：vector_store 不可达时 fail-fast，避免 pymilvus
            # 在 gRPC 握手无超时地阻塞整个 event loop（chat SSE 因此永久不返回）。
            vs = config.get("vector_store", {}) if isinstance(config, dict) else {}
            if vs.get("provider") == "milvus":
                import socket
                from urllib.parse import urlparse

                url = (vs.get("config") or {}).get("url", "")
                parsed = urlparse(url if "://" in url else f"//{url}", scheme="")
                host = parsed.hostname or "localhost"
                port = parsed.port or 19530
                try:
                    with socket.create_connection((host, port), timeout=2):
                        pass
                except OSError:
                    self._available = False
                    self._initialized = True  # 永久关闭，避免每次 chat 再等 2 秒
                    print(f"⚠️ mem0 向量存储 {url or f'{host}:{port}'} 不可达，已降级为空记忆（不再重试）")
                    return

            self._memory = Memory.from_config(config)
            self._available = True
            self._initialized = True
            print("✅ mem0 Memory 初始化完成")
        except Exception as e:
            self._available = False
            print(f"⚠️ mem0 初始化失败（将在下次调用时重试）: {e}")

    def is_available(self) -> bool:
        """检查 mem0 是否可用（已安装且初始化成功）。"""
        self._ensure_initialized()
        return self._available

    def search(
        self, query: str, user_id: str, limit: int = 5, score_threshold: float = 0.0
    ) -> list[dict[str, Any]]:
        """检索与 query 最相关的记忆条目。

        返回格式：[{"memory": "...", "score": 0.85, "id": "..."}, ...]
        score_threshold > 0 时过滤低于阈值的结果，减少噪声注入。
        不可用时返回空列表。
        """
        if not self.is_available():
            return []
        try:
            results = self._memory.search(query, user_id=user_id, limit=limit)
            items = results.get("results", [])
            if score_threshold > 0:
                items = [r for r in items if r.get("score", 0) >= score_threshold]
            return items
        except Exception as e:
            print(f"⚠️ mem0 search 失败: {e}")
            return []

    def add(
        self, messages: list[dict[str, str]], user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """将对话消息提交给 mem0，由 LLM 裁判自动提取关键事实（同步版本）。

        messages 格式：[{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        metadata: 可选的元数据（如 {"type": "preference"}），会附加到提取的记忆条目上。
        返回 mem0 的提取结果，不可用时返回 None。
        """
        if not self.is_available():
            return None
        try:
            kwargs: dict[str, Any] = {"user_id": user_id}
            if metadata:
                kwargs["metadata"] = metadata
            result = self._memory.add(messages, **kwargs)
            return result
        except Exception as e:
            print(f"⚠️ mem0 add 失败: {e}")
            return None

    async def async_add(
        self, messages: list[dict[str, str]], user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """将对话消息提交给 mem0（异步版本，不阻塞事件循环）。

        在 async 上下文（如 FastAPI event_generator）中应使用此方法，
        避免同步 LLM 调用阻塞整个事件循环。
        注意：chat.py 已改用 SmartExtractor，此方法保留供其他调用方使用。
        """
        import asyncio
        import functools
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(self.add, messages, user_id, metadata=metadata)
        )

    def _classify_type(self, memory_text: str) -> str:
        """规则分类：将记忆文本映射到 Claude Code 四类型之一。

        优先级：feedback > project > reference > user（兜底）。
        """
        text_lower = memory_text.lower()
        for mem_type in ("feedback", "project", "reference"):
            if any(kw in text_lower for kw in _TYPE_KEYWORDS[mem_type]):
                return mem_type
        return "user"

    @staticmethod
    def _freshness_label(result: dict[str, Any], stale_days: int = 30) -> str:
        """根据记忆时间戳生成自然语言新鲜度标注（认知工程版）。

        用 LLM 容易理解的「N天前」替代 ISO 时间戳，
        超过 stale_days 的记忆标注为「可能过时」引导 LLM 主动确认。
        """
        from datetime import datetime, timezone

        # 优先取 updated_at（有更新的记忆），兜底 created_at
        ts_str = result.get("updated_at") or result.get("created_at")
        if not ts_str:
            return ""
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            # naive datetime（无时区信息）补 UTC，防止与 aware datetime 相减抛 TypeError
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - ts).days
        except (ValueError, TypeError):
            return ""

        if age_days <= stale_days:
            return f"（{age_days}天前）"
        return f"（{age_days}天前，可能过时，建议与用户确认）"

    def get_typed_context(
        self, query: str, user_id: str, limit: int = 8,
        score_threshold: float = 0.1, stale_days: int = 30,
    ) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
        """检索记忆并按 Claude Code 类型分组，同时返回带 score 的原始结果。

        分组逻辑：
        1. 优先使用 mem0 metadata 中存储的 type 字段（主动标注时）
        2. 无 type 字段时使用规则分类（向后兼容历史无标注数据）
        3. 每条记忆附带新鲜度标注（N天前 / 可能过时）

        limit=8：确保跨 4 个类型有足够候选（平均每类 2 条）
        score_threshold：过滤低相关性结果，减少噪声注入
        stale_days：超过此天数的记忆标注为「可能过时」

        返回：
            grouped: {"user": [...], "feedback": [...], ...}（空类型不包含）
            raw_results: 原始 search 结果列表，保留 score 供 retrieval event 使用
        """
        if not self.is_available():
            return {}, []
        try:
            raw_results = self.search(
                query, user_id=user_id, limit=limit,
                score_threshold=score_threshold,
            )
        except Exception as e:
            print(f"⚠️ mem0 get_typed_context 检索失败: {e}")
            return {}, []

        grouped: dict[str, list[str]] = {}
        for r in raw_results:
            mem_text = r.get("memory", "")
            if not mem_text:
                continue
            # metadata.type 优先；无则规则分类
            metadata = r.get("metadata") or {}
            mem_type = metadata.get("type") or self._classify_type(mem_text)
            if mem_type not in ("user", "feedback", "project", "reference"):
                mem_type = "user"
            # 附加新鲜度标注
            freshness = self._freshness_label(r, stale_days=stale_days)
            grouped.setdefault(mem_type, []).append(f"{mem_text}{freshness}")
        return grouped, raw_results

    def update(self, memory_id: str, data: str) -> dict[str, Any] | None:
        """更新指定 ID 的记忆内容。

        Args:
            memory_id: 记忆条目 ID（从 search/get_all 结果中获取）
            data: 新的记忆文本内容
        返回更新结果，不可用时返回 None。
        """
        if not self.is_available():
            return None
        try:
            result = self._memory.update(memory_id, data)
            return result
        except Exception as e:
            print(f"⚠️ mem0 update 失败: {e}")
            return None

    def delete(self, memory_id: str) -> bool:
        """删除指定 ID 的记忆条目。

        Args:
            memory_id: 记忆条目 ID（从 search/get_all 结果中获取）
        返回是否删除成功。
        """
        if not self.is_available():
            return False
        try:
            self._memory.delete(memory_id)
            return True
        except Exception as e:
            print(f"⚠️ mem0 delete 失败: {e}")
            return False

    def delete_all(self, user_id: str) -> bool:
        """删除指定用户的全部记忆。谨慎使用。

        Args:
            user_id: 用户 ID
        返回是否删除成功。
        """
        if not self.is_available():
            return False
        try:
            self._memory.delete_all(user_id=user_id)
            return True
        except Exception as e:
            print(f"⚠️ mem0 delete_all 失败: {e}")
            return False

    def history(self, memory_id: str) -> list[dict[str, Any]]:
        """获取指定记忆条目的变更历史。

        Args:
            memory_id: 记忆条目 ID
        返回历史记录列表，不可用时返回空列表。
        """
        if not self.is_available():
            return []
        try:
            return self._memory.history(memory_id)
        except Exception as e:
            print(f"⚠️ mem0 history 失败: {e}")
            return []

    def get_all(self, user_id: str) -> list[dict[str, Any]]:
        """获取指定用户的全部记忆条目。

        返回格式：[{"memory": "...", "id": "...", "created_at": "..."}, ...]
        不可用时返回空列表。
        """
        if not self.is_available():
            return []
        try:
            results = self._memory.get_all(user_id=user_id)
            return results.get("results", [])
        except Exception as e:
            print(f"⚠️ mem0 get_all 失败: {e}")
            return []


# 全局单例
mem0_manager = Mem0Manager()
