"""FileStateCache — 减少工具层重复文件 IO。

设计原则：
- 基于 mtime 判断缓存有效性
- TTL 兜底防止外部修改绕过 mtime 检测
- 容量限制防止内存膨胀
"""

import time
from pathlib import Path

DEFAULT_TTL = 60  # 秒
MAX_CACHE_SIZE = 50  # 最多缓存文件数


class FileStateCache:
    """内存级文件内容缓存，通过 mtime + TTL 判断有效性。"""

    def __init__(self, ttl: int = DEFAULT_TTL, max_size: int = MAX_CACHE_SIZE):
        self._ttl = ttl
        self._max_size = max_size
        # key: str(absolute_path) → value: (mtime, content, cached_at)
        self._cache: dict[str, tuple[float, str, float]] = {}

    def get(self, path: Path) -> str | None:
        """尝试从缓存读取文件内容。返回 None 表示 cache miss。"""
        key = str(path.resolve())
        entry = self._cache.get(key)
        if entry is None:
            return None

        cached_mtime, cached_content, cached_at = entry

        # TTL 过期
        if time.time() - cached_at > self._ttl:
            del self._cache[key]
            return None

        # mtime 变化
        try:
            current_mtime = path.stat().st_mtime
        except FileNotFoundError:
            del self._cache[key]
            return None

        if current_mtime != cached_mtime:
            del self._cache[key]
            return None

        return cached_content

    def put(self, path: Path, content: str) -> None:
        """写入缓存。如果超出容量限制，淘汰最早的条目。"""
        key = str(path.resolve())
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return

        # 容量淘汰：删除最早缓存的条目
        if len(self._cache) >= self._max_size and key not in self._cache:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][2])
            del self._cache[oldest_key]

        self._cache[key] = (mtime, content, time.time())

    def invalidate(self, path: Path) -> None:
        """手动失效某个路径的缓存。"""
        key = str(path.resolve())
        self._cache.pop(key, None)


# 全局单例
file_cache = FileStateCache()
