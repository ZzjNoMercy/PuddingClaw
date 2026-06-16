"""Task State Middleware — 任务清单 after_model 写入。"""

from __future__ import annotations

import collections
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

# 默认触发关键词（可通过 config 覆盖）
# 注意：避免使用过于宽泛的词（如"任务"），容易误判查询类请求
_DEFAULT_TRIGGERS = ["帮我记得", "提醒我", "需要做"]

# 排除词：包含这些词的请求不会被当作任务
_EXCLUDE_KEYWORDS = ["查看", "列出", "显示", "标记", "完成", "清理", "删除", "归档"]

# 全局 dedup 缓存：跨 middleware 实例共享，避免 mem0 模式下每次重建导致缓存丢失
# key = md5(user_text), value = True
_GLOBAL_DEDUP_CACHE: collections.OrderedDict[str, bool] = collections.OrderedDict()
_GLOBAL_DEDUP_MAX = 200


class TaskStateMiddleware(AgentMiddleware):
    """检测用户消息中的任务关键词，追加到 TODO.md。

    设计要点：
    - 继承 AgentMiddleware，使用 after_model hook 由 LangChain 框架自动触发
    - 纯副作用：返回 None 不修改 state
    - 使用全局 dedup 缓存，避免 mem0 模式下每次重建 middleware 导致缓存丢失
    - 文件写失败不阻断 pipeline（logger.warning + return None）
    """

    def __init__(
        self,
        todo_path: Path,
        triggers: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.todo_path = Path(todo_path)
        # 仅 None 时 fallback；空列表视为"静默模式"（middleware 仍挂载但永不命中）
        self.triggers = list(triggers) if triggers is not None else list(_DEFAULT_TRIGGERS)

    def _extract_last_user_text(self, messages) -> str | None:
        """反向遍历 state messages，找到最后一条 HumanMessage 的文本内容。

        注意：过滤掉 SkillsRouter 注入的路由提示，只保留用户原始输入。
        """
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = msg.content
                # 兼容多模态 list content
                if isinstance(content, list):
                    content = "".join(
                        block.get("text", "") if isinstance(block, dict) else str(block)
                        for block in content
                    )
                if not isinstance(content, str):
                    content = str(content)

                # 过滤 SkillsRouter 路由提示（格式：\n\n[系统路由提示] ...）
                if "\n\n[系统路由提示]" in content:
                    content = content.split("\n\n[系统路由提示]")[0].strip()

                return content
        return None

    def _matches_trigger(self, text: str) -> str | None:
        """返回首个命中的关键词，无命中返回 None。

        注意：如果文本包含排除词（查看、标记、清理等），则不触发。
        """
        # 检查排除词
        for exclude_kw in _EXCLUDE_KEYWORDS:
            if exclude_kw in text:
                return None

        # 检查触发词
        for kw in self.triggers:
            if kw in text:
                return kw
        return None

    def _is_duplicate(self, text: str) -> bool:
        """检查并标记 dedup。命中返回 True；未命中插入新 hash 并返回 False。

        使用全局缓存，避免 mem0 模式下每次重建 middleware 导致缓存丢失。
        """
        h = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()
        if h in _GLOBAL_DEDUP_CACHE:
            _GLOBAL_DEDUP_CACHE.move_to_end(h)
            return True
        _GLOBAL_DEDUP_CACHE[h] = True
        if len(_GLOBAL_DEDUP_CACHE) > _GLOBAL_DEDUP_MAX:
            _GLOBAL_DEDUP_CACHE.popitem(last=False)
        return False

    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        """模型响应后检测任务关键词并追加到 TODO.md。"""
        try:
            messages = state.get("messages", [])
            user_text = self._extract_last_user_text(messages)
            if not user_text:
                return None

            trigger = self._matches_trigger(user_text)
            if not trigger:
                return None

            if self._is_duplicate(user_text):
                logger.debug("[TaskState] dedup hit, skipping: %s", user_text[:60])
                return None

            # 任务描述：clip 200 字符，单行
            task_desc = user_text.strip().replace("\n", " ")[:200]
            timestamp = datetime.now().isoformat(timespec="seconds")
            line = f"- [ ] {task_desc} (created: {timestamp}, trigger: {trigger})\n"

            self.todo_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.todo_path, "a", encoding="utf-8") as f:
                f.write(line)

            logger.info("[TaskState] task appended to %s (trigger=%s)", self.todo_path.name, trigger)
        except Exception as e:
            logger.warning("[TaskState] write failed (non-blocking): %s: %s", type(e).__name__, e)

        return None


def build_write_middlewares(base_dir: Path, config: dict) -> list:
    """构造 write 类 middleware 列表（after_model 副作用类）。

    与 build_compression_middlewares（before_model 类）并列，由 agent.py 拼接后挂到 create_agent。

    config 格式：
        {
            "enabled": True,
            "task_state": {
                "enabled": True,
                "todo_path": "workspace/TODO.md",  # 相对 base_dir
                "triggers": ["帮我", "待办", "记得", "提醒", "任务", "需要做"],
            }
        }
    """
    if not config.get("enabled", True):
        return []

    middlewares: list = []

    task_cfg = config.get("task_state", {})
    if task_cfg.get("enabled", True):
        todo_rel = task_cfg.get("todo_path", "workspace/TODO.md")
        todo_path = base_dir / todo_rel if not Path(todo_rel).is_absolute() else Path(todo_rel)
        # 仅在未配置 (None) 时 fallback；显式传入 [] 视为"静默模式"（中间件挂载但不触发）
        triggers = task_cfg.get("triggers")
        if triggers is None:
            triggers = list(_DEFAULT_TRIGGERS)
        middlewares.append(TaskStateMiddleware(todo_path=todo_path, triggers=triggers))

    return middlewares
