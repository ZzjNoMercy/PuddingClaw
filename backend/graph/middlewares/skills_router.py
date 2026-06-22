"""SkillsRouterMiddleware — 动态工具路由中间件（before_model 软路由）。

设计要点：
- 替代 agent.py 中的 _detect_tool_categories + agent 重建模式
- 通过 before_model 注入路由提示 SystemMessage，引导 LLM 优先使用特定工具子集
- Agent 始终以全量工具构建，可被缓存（不因工具变化而重建）
- 路由提示为临时注入：仅在 LLM 调用期间存在，不持久化到会话历史
- 关键词匹配作为初始分类器，后续可替换为 LLM 语义分类

与 compression middleware 的叠加顺序：
    compression（修改 messages，外层）→ skills_router（注入路由，中层）→ write（after_model 副作用，内层）
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

# 路由提示标识前缀：before_model 用它识别和清理上一轮注入的路由消息
_ROUTER_PREFIX = "[SKILLS_ROUTER]"

# 范围线索提取正则：从用户消息中检测文件路径、URL、glob 模式
# 供 scope_aware 技能（如 research）构造 deep_research 的 scope 参数提示
_SCOPE_HINT_PATTERNS: dict[str, re.Pattern] = {
    "file_path": re.compile(r'(?:^|[\s,])([.~/][\w./\-]+\.\w{1,10})\b'),
    "directory": re.compile(r'(?:^|[\s,])([.~/][\w./\-]+/)'),
    "url": re.compile(r'(https?://\S+)'),
    "glob": re.compile(r'(?:^|[\s,])(\*\*?/[\w*.\-]+)'),
}

# 默认技能注册表：合并自原 agent.py._CATEGORY_KEYWORDS + tools/__init__.py.TOOL_CATEGORIES
# research 从 knowledge 中拆出，独立为高优先级技能（Ch4 subagent 隔离场景）
# tool_categories 字段与 tools/__init__.py.TOOL_CATEGORIES 交叉引用，保持两套注册表同步
_DEFAULT_SKILL_REGISTRY: dict[str, dict[str, Any]] = {
    "research": {
        # 仅保留明确的研究意图关键词，避免 "帮我看一下"/"分析" 等泛意图误触发 subagent
        "keywords": ["研究", "综述", "调研", "深入分析", "deep research"],
        "preferred_tools": ["deep_research"],
        "tool_categories": ["research"],  # cf. tools/__init__.py TOOL_CATEGORIES["research"]
        "scope_aware": True,  # 启用 scope 自动提取：从用户消息中检测文件路径/URL/glob
        "routing_prompt": (
            "用户意图为深度研究类任务（综述/分析/调研）。"
            "请优先使用 deep_research 工具，将研究任务委派给独立子 agent 处理，"
            "主对话只接收摘要结果。不要尝试自己逐步读取大量文件。"
            "deep_research 需要 query（研究问题）和 scope（研究范围）两个参数。"
        ),
    },
    "knowledge": {
        # "分析"/"看一下"/"帮我看" 等泛意图归入 knowledge 而非 research，避免触发重量级 subagent
        "keywords": ["搜索", "查找", "查询", "知识", "知识库", "网页", "url", "http", "链接",
                     "新闻", "资讯", "最近有什么", "最新消息", "近况",
                     "search", "find", "knowledge",
                     "分析", "梳理", "看一下", "帮我看", "深入"],
        "preferred_tools": ["tavily_search", "search_knowledge_base", "fetch_url"],
        "tool_categories": ["knowledge"],  # cf. tools/__init__.py TOOL_CATEGORIES["knowledge"]
        "routing_prompt": (
            "用户意图为知识检索类。"
            "通用新闻、近期动态和公开网页检索优先使用 tavily_search，一次获得结构化搜索结果；"
            "已有明确文章 URL 时才使用 fetch_url 抓正文；本地资料使用 search_knowledge_base。"
            "不要通过连续猜测多个搜索页 URL 来替代 web search。"
        ),
    },
    "skill": {
        "keywords": ["技能", "skill", "执行技能", "运行技能", "create_skill", "execute_skill"],
        "preferred_tools": ["execute_skill", "create_skill_version"],
        "tool_categories": ["skill"],
        "routing_prompt": (
            "用户意图为技能操作类。"
            "请优先使用 execute_skill 或 create_skill_version 工具。"
        ),
    },
    "code_exec": {
        "keywords": ["代码", "python", "运行代码", "执行代码", "计算", "脚本",
                     "code", "script", "run code", "run python"],
        "preferred_tools": ["python_repl"],
        "tool_categories": ["code_exec"],
        "routing_prompt": (
            "用户意图为代码执行类。"
            "请使用 python_repl 工具运行代码。"
        ),
    },
    "memory": {
        "keywords": ["记忆", "记住", "保存记忆", "memory", "remember", "记录"],
        "preferred_tools": [
            "save_user_memory", "save_feedback_memory", "save_project_memory", "save_reference_memory",
            "search_user_memories", "search_feedback_memories", "search_project_memories", "search_reference_memories"
        ],
        "tool_categories": ["memory"],
        "routing_prompt": (
            "用户意图为记忆操作类。"
            "请优先使用记忆相关工具（save_user_memory / save_feedback_memory / save_project_memory / save_reference_memory）。"
            "若系统注入的「用户长期记忆」中已有相关条目，直接告知用户该信息已保存，不要重复调用 save_*_memory；"
            "也不要反复 search 已经在上下文中呈现的记忆。"
        ),
    },
}


class SkillsRouterMiddleware(AgentMiddleware):
    """基于意图分类的动态工具路由中间件。

    工作原理：
    - Agent 始终以全量工具构建并缓存（不因工具变化重建）
    - 每次 LLM 调用前，分析用户消息意图（关键词匹配）
    - 匹配到特定技能时，注入 SystemMessage 引导 LLM 优先使用相关工具
    - 未匹配时不注入路由提示，LLM 自由选择工具（纯聊天场景）

    相比原方案的收益：
    - 消除动态工具模式下的 agent 重建 → 保留 DeepSeek prefix cache
    - 保留 middleware 状态（ToolResultClear LRU / Compaction 计数器等）
    - 每个技能可携带专属路由提示，比单纯的工具裁剪更有指导性
    """

    def __init__(
        self,
        skill_registry: dict[str, dict[str, Any]] | None = None,
        history_window: int = 2,
    ) -> None:
        super().__init__()
        self.skill_registry = dict(skill_registry) if skill_registry is not None else dict(_DEFAULT_SKILL_REGISTRY)
        self.history_window = history_window
        # 可观测性：记录最近一次路由决策，供调试和日志使用
        self._last_decision: dict[str, Any] = {}

    def _extract_context_text(self, messages: list) -> str:
        """提取最近 N 条用户消息文本（N=history_window+1），用于意图分类。

        包含当前消息 + 最近 history_window 条历史，与原 _detect_tool_categories 逻辑一致。
        """
        user_texts: list[str] = []
        count = 0
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, list):
                    content = "".join(
                        block.get("text", "") if isinstance(block, dict) else str(block)
                        for block in content
                    )
                user_texts.append(str(content))
                count += 1
                if count > self.history_window:
                    break
        return " ".join(user_texts).lower()

    @staticmethod
    def _extract_raw_user_text(messages: list) -> str:
        """提取最后一条用户消息的原始文本（保留大小写）。

        与 _extract_context_text 不同：不做 .lower()，用于 scope hint 提取，
        因为文件路径和 URL 大小写敏感。
        """
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, list):
                    content = "".join(
                        block.get("text", "") if isinstance(block, dict) else str(block)
                        for block in content
                    )
                return str(content)
        return ""

    @staticmethod
    def _extract_scope_hints(text: str) -> dict[str, list[str]]:
        """从用户消息中提取研究范围线索（文件路径、URL、glob 模式）。

        仅在 scope_aware=True 的技能匹配时调用，为 routing_prompt 补充
        deep_research 的 scope 参数提示，帮助 LLM 构造更精确的工具调用。
        """
        hints: dict[str, list[str]] = {}
        for hint_type, pattern in _SCOPE_HINT_PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                seen: set[str] = set()
                unique: list[str] = []
                for m in matches:
                    m = m.strip()
                    if m and m not in seen:
                        seen.add(m)
                        unique.append(m)
                if unique:
                    hints[hint_type] = unique
        return hints

    def validate_preferred_tools(self, available_tool_names: set[str]) -> list[str]:
        """校验 preferred_tools 名称在实际工具集中存在。

        返回不存在的工具名列表（空列表表示全部匹配）。
        在 agent 初始化时调用，不匹配时仅 warning 不阻断。
        """
        missing: list[str] = []
        for skill_id, skill_def in self.skill_registry.items():
            for tool_name in skill_def.get("preferred_tools", []):
                if tool_name not in available_tool_names:
                    missing.append(f"{skill_id}.{tool_name}")
        return missing

    def _classify_intent(self, text: str) -> dict[str, Any]:
        """关键词匹配分类器。返回匹配到的技能列表和路由提示。

        匹配策略：
        - 遍历所有技能的关键词，收集全部命中
        - research 优先级最高：若同时匹配 research 和 knowledge，去掉 knowledge
          （综述/分析场景应走 subagent 隔离，而非简单检索）
        - 多技能可同时匹配（如 knowledge + code_exec），路由提示合并
        - 无匹配时返回 matched=False
        """
        matched_skills: list[str] = []
        routing_prompts: list[str] = []
        preferred_tools: list[str] = []

        for skill_id, skill_def in self.skill_registry.items():
            keywords = skill_def.get("keywords", [])
            if any(kw.lower() in text for kw in keywords):
                matched_skills.append(skill_id)
                routing_prompts.append(skill_def["routing_prompt"])
                preferred_tools.extend(skill_def.get("preferred_tools", []))

        if not matched_skills:
            return {"matched": False, "skills": [], "preferred_tools": [], "routing_prompt": ""}

        # research 优先级提升：与 knowledge 关键词重叠时，research 胜出
        if "research" in matched_skills and "knowledge" in matched_skills:
            matched_skills.remove("knowledge")
            routing_prompts = [self.skill_registry[s]["routing_prompt"] for s in matched_skills]
            preferred_tools = []
            for s in matched_skills:
                preferred_tools.extend(self.skill_registry[s].get("preferred_tools", []))

        return {
            "matched": True,
            "skills": matched_skills,
            "preferred_tools": list(dict.fromkeys(preferred_tools)),  # 去重保序
            "routing_prompt": "\n".join(routing_prompts),
        }

    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        """模型调用前：分类意图并注入路由提示 SystemMessage。

        执行流程：
        1. 清理上一轮残留的路由消息（multi-turn agent loop 防累积）
        2. 提取用户消息文本做意图分类
        3. 命中时追加路由 SystemMessage；未命中时不注入
        """
        messages = state.get("messages", [])
        if not messages:
            return None

        # 1. 清理上一轮的路由提示（multi-turn agent loop 中 before_model 会被多次调用）
        # 同时清理 HumanMessage 中的路由提示（防止多轮对话累积）
        cleaned = []
        for m in messages:
            if isinstance(m, SystemMessage) and _ROUTER_PREFIX in str(m.content):
                # 跳过旧的 SystemMessage 路由提示
                continue
            elif isinstance(m, HumanMessage):
                # 清理 HumanMessage 中的路由提示
                content = m.content
                if isinstance(content, str) and "\n\n[系统路由提示]" in content:
                    # 移除路由提示部分
                    content = content.split("\n\n[系统路由提示]")[0]
                    cleaned.append(HumanMessage(content=content))
                else:
                    cleaned.append(m)
            else:
                cleaned.append(m)
        had_old_routing = len(cleaned) != len(messages)

        # 2. 提取用户文本做意图分类
        context_text = self._extract_context_text(cleaned)
        if not context_text:
            self._last_decision = {"matched": False, "skills": [], "preferred_tools": []}
            return {"messages": cleaned} if had_old_routing else None

        decision = self._classify_intent(context_text)
        self._last_decision = decision

        if not decision["matched"]:
            logger.debug("[SkillsRouter] no skill matched, using full tools")
            return {"messages": cleaned} if had_old_routing else None

        # 3. 构造路由提示（对 scope_aware 技能追加范围线索）
        prompt_parts = [decision["routing_prompt"]]
        if any(self.skill_registry.get(s, {}).get("scope_aware") for s in decision["skills"]):
            # 从原始用户消息提取 scope（保留大小写），不用 lowercased 的 context_text
            # 文件路径和 URL 大小写敏感，lowercased 会导致 subagent 打开失败
            raw_user_text = self._extract_raw_user_text(cleaned)
            scope_hints = self._extract_scope_hints(raw_user_text)
            if scope_hints:
                hint_segments = [f"{k}={v}" for k, v in scope_hints.items()]
                prompt_parts.append(
                    f"检测到研究范围线索：{'，'.join(hint_segments)}。"
                    "请在调用 deep_research 时，将上述线索作为 scope 参数的参考。"
                )

        # 注入路由提示到最后一条 HumanMessage（避免修改 system_message 破坏 DeepSeek prefix cache）
        last_human_idx = None
        for i in range(len(cleaned) - 1, -1, -1):
            if isinstance(cleaned[i], HumanMessage):
                last_human_idx = i
                break

        if last_human_idx is not None:
            original_content = cleaned[last_human_idx].content
            # 将路由提示追加到用户消息末尾（内部传递，用户不可见）
            routing_hint = f"\n\n[系统路由提示] {' '.join(prompt_parts)}"
            cleaned[last_human_idx] = HumanMessage(
                content=original_content + routing_hint
            )

        logger.info(
            "[SkillsRouter] matched skills=%s, preferred_tools=%s",
            decision["skills"], decision["preferred_tools"],
        )
        return {"messages": cleaned}


def build_skills_router_middlewares(config: dict) -> list:
    """构造 SkillsRouter 类中间件列表。

    与 build_compression_middlewares / build_write_middlewares 对称的工厂函数。

    config 格式：
        {
            "enabled": True,
            "history_window": 2,
            "skills": { ... }  # 可选覆盖，默认使用 _DEFAULT_SKILL_REGISTRY
        }
    """
    if not config.get("enabled", True):
        return []

    skill_registry = config.get("skills")  # None = use default
    history_window = config.get("history_window", 2)

    return [SkillsRouterMiddleware(
        skill_registry=skill_registry,
        history_window=history_window,
    )]
