"""MemoryIndexer — 长期记忆管理器，对 MEMORY.md 建立向量索引实现 RAG 检索"""

import hashlib       # MD5 哈希计算，用于文件变更检测
import os            # 读取环境变量（API Key、模型名等）
from pathlib import Path    # 路径操作
from typing import Any      # 类型注解

from config import get_embedding_config  # 从 config.json 读取 Embedding 配置


class MemoryIndexer:
    """长期记忆核心类：读取 memory/MEMORY.md → 向量化 → 持久化索引 → 语义检索"""

    def __init__(self, base_dir: Path) -> None:
        """初始化各路径，但不立即构建索引（懒加载）"""
        self._base_dir = base_dir                                    # 项目根目录
        self._memory_path = base_dir / "memory" / "MEMORY.md"       # 长期记忆源文件（LLM 通过 write_file 工具写入）
        self._storage_dir = base_dir / "storage" / "memory_index"   # 向量索引持久化目录
        self._hash_path = self._storage_dir / ".memory_hash"        # 存储上次构建时的 MD5 哈希值
        self._index: Any = None                                     # 内存中的索引对象（懒加载）

    # ── 哈希工具方法（用于变更检测）──────────────────────────────────────────────

    def _get_file_hash(self) -> str:
        """计算 MEMORY.md 当前内容的 MD5 哈希，文件不存在返回空串"""
        if not self._memory_path.exists():       # 文件不存在
            return ""                            # 返回空串表示无内容
        content = self._memory_path.read_bytes() # 读取文件原始字节
        return hashlib.md5(content).hexdigest()  # 计算并返回 MD5 十六进制字符串

    def _get_stored_hash(self) -> str:
        """读取上次构建索引时保存的哈希值，首次构建前返回空串"""
        if not self._hash_path.exists():                         # 哈希文件不存在（首次运行）
            return ""                                            # 返回空串
        return self._hash_path.read_text(encoding="utf-8").strip()  # 读取并去除空白

    def _save_hash(self, hash_value: str) -> None:
        """将哈希值写入 .memory_hash 文件，供下次比对"""
        self._hash_path.parent.mkdir(parents=True, exist_ok=True)  # 确保目录存在
        self._hash_path.write_text(hash_value, encoding="utf-8")   # 写入哈希值

    # ── 变更检测 ─────────────────────────────────────────────────────────────────

    def _maybe_rebuild(self) -> None:
        """比对当前哈希与存储哈希，不一致则触发索引重建（每次 retrieve 前调用）"""
        current_hash = self._get_file_hash()      # 计算 MEMORY.md 当前哈希
        stored_hash = self._get_stored_hash()      # 读取上次构建时的哈希
        if current_hash and current_hash != stored_hash:  # 文件存在且内容有变化
            self.rebuild_index()                   # 触发索引重建

    # ── 索引构建 ─────────────────────────────────────────────────────────────────

    def rebuild_index(self) -> None:
        """核心方法：读取 MEMORY.md → 文本切分 → Embedding 向量化 → 构建索引 → 持久化"""
        if not self._memory_path.exists():                  # MEMORY.md 不存在
            print("⚠️ memory/MEMORY.md not found, skipping index build")
            self._index = None                              # 清空索引
            return                                          # 直接返回

        try:
            # 延迟导入 LlamaIndex 组件（仅 RAG 模式需要，减少启动开销）
            from llama_index.core import (
                Document,               # 文档封装类
                StorageContext,          # 存储上下文（持久化用）
                VectorStoreIndex,        # 向量索引类
            )
            from llama_index.core.node_parser import SentenceSplitter  # 按句子边界切分文本
            from llama_index.core.settings import Settings             # 全局配置
            from llama_index.embeddings.openai import OpenAIEmbedding  # OpenAI Embedding 模型

            # 配置 Embedding 模型（从 config.json 读取，fallback 到环境变量）
            emb_cfg = get_embedding_config()
            Settings.embed_model = OpenAIEmbedding(
                model=emb_cfg["model"],
                api_key=emb_cfg["api_key"],
                api_base=emb_cfg["api_base"],
            )

            content = self._memory_path.read_text(encoding="utf-8")  # 读取 MEMORY.md 全文
            if not content.strip():                                  # 文件为空则跳过
                self._index = None                                   # 无内容可索引
                return

            # 将 MEMORY.md 全文包装为 LlamaIndex Document 对象
            doc = Document(text=content, metadata={"source": "MEMORY.md"})  # 附带来源元数据

            # 按句子边界切分为多个 chunk（每个 chunk 是一个独立检索单元）
            splitter = SentenceSplitter(chunk_size=256, chunk_overlap=32)  # 256 token/chunk，32 token 重叠
            nodes = splitter.get_nodes_from_documents([doc])               # 执行切分，返回 Node 列表

            # 构建向量索引（调用 Embedding API 计算每个 chunk 的向量）
            self._storage_dir.mkdir(parents=True, exist_ok=True)    # 确保存储目录存在
            index = VectorStoreIndex(nodes)                         # 构建内存中的向量索引

            # 持久化到磁盘（下次启动直接加载，不用重新调 Embedding API）
            index.storage_context.persist(persist_dir=str(self._storage_dir))  # 写入 storage/memory_index/
            self._index = index                                     # 缓存到内存

            self._save_hash(self._get_file_hash())                  # 记录当前哈希，标记索引已同步
            print(f"🔄 Memory index rebuilt ({len(nodes)} chunks)")  # 打印构建结果

        except ImportError as e:                                     # LlamaIndex 未安装
            print(f"⚠️ LlamaIndex not fully installed: {e}")         # 降级提示
            self._index = None                                       # RAG 不可用但不崩溃
        except Exception as e:                                       # 其他异常
            print(f"⚠️ Memory index build error: {e}")               # 打印错误
            self._index = None                                       # 索引置空

    # ── 索引加载 ─────────────────────────────────────────────────────────────────

    def _load_index(self) -> Any:
        """从磁盘加载已持久化的向量索引（优先用内存缓存）"""
        if self._index is not None:               # 内存中已有索引
            return self._index                     # 直接返回（最快路径）

        if not self._storage_dir.exists() or not any(self._storage_dir.iterdir()):  # 磁盘无持久化文件
            return None                            # 返回 None，后续触发 rebuild

        try:
            # 延迟导入（与 rebuild_index 相同的依赖）
            from llama_index.core import StorageContext, load_index_from_storage  # 存储加载工具
            from llama_index.core.settings import Settings                       # 全局配置
            from llama_index.embeddings.openai import OpenAIEmbedding            # Embedding 模型

            # 加载时也要配置 Embedding（检索时需要对 query 做向量化，从 config.json 读取）
            emb_cfg = get_embedding_config()
            Settings.embed_model = OpenAIEmbedding(
                model=emb_cfg["model"],
                api_key=emb_cfg["api_key"],
                api_base=emb_cfg["api_base"],
            )

            # 从磁盘还原索引
            storage_context = StorageContext.from_defaults(
                persist_dir=str(self._storage_dir)       # 指定持久化目录
            )
            self._index = load_index_from_storage(storage_context)  # 加载索引到内存
            return self._index                                      # 返回索引对象
        except Exception as e:                                      # 加载失败
            print(f"⚠️ Failed to load memory index: {e}")           # 打印错误
            return None                                             # 返回 None

    # ── 检索入口 ─────────────────────────────────────────────────────────────────

    def retrieve(
        self, query: str, top_k: int = 3
    ) -> list[dict[str, Any]]:
        """检索与 query 最相关的 top_k 条记忆片段

        调用链：agent.py astream() → retrieve(用户消息) → 返回相关片段注入对话
        """
        self._maybe_rebuild()                      # 先检查 MEMORY.md 是否有变更，有则重建索引

        index = self._load_index()                 # 加载索引（内存缓存 → 磁盘 → None）
        if index is None:                          # 索引不可用（文件空或构建失败）
            return []                              # 返回空列表，调用方回退到 Direct 模式

        try:
            retriever = index.as_retriever(similarity_top_k=top_k)  # 创建检索器，限制返回数量
            nodes = retriever.retrieve(query)                       # 执行语义检索：query 向量化 → 余弦相似度匹配

            results: list[dict[str, Any]] = []                      # 结果列表
            for node in nodes:                                      # 遍历检索结果
                results.append({
                    "text": node.get_text(),                        # chunk 文本内容
                    "score": f"{node.get_score():.4f}" if node.get_score() else "N/A",  # 相似度分数（保留4位小数）
                    "source": node.metadata.get("source", "MEMORY.md"),  # 来源标记
                })
            return results                                          # 返回检索结果列表
        except Exception as e:                                      # 检索异常
            print(f"⚠️ Memory retrieval error: {e}")                # 打印错误
            return []                                               # 返回空列表


# ── 全局单例 ──────────────────────────────────────────────────────────────────────
_instance: MemoryIndexer | None = None  # 模块级单例变量，整个进程只有一个 MemoryIndexer


def get_memory_indexer(base_dir: Path) -> MemoryIndexer:
    """获取全局唯一的 MemoryIndexer 实例（首次调用时创建）"""
    global _instance                     # 声明使用模块级变量
    if _instance is None:                # 首次调用
        _instance = MemoryIndexer(base_dir)  # 创建实例
    return _instance                     # 返回单例
