"""Memory tools factory — 4 save + 4 search tools, one per Claude Code memory type."""

import contextvars

from langchain_core.tools import tool
from config import get_memory_backend

# 请求级 user_id：由 chat.py 在 SSE 入口设置，工具函数从此读取
# 解决 LangChain tool 无法直接接收请求参数的问题
current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user_id", default="default_user"
)


def create_memory_tools():
    """Factory: returns 8 memory tools (4 save + 4 search)."""

    # --- Save tools ---
    @tool
    def save_user_memory(content: str) -> str:
        """Save user profile information to long-term memory: name, role, technical background, preferences.

        Use when the user shares personal info like "I'm a Python developer" or "I prefer dark mode".

        Args:
            content: One-sentence summary of the user profile fact to save
        """
        return _save_memory(content, "user")

    @tool
    def save_feedback_memory(content: str) -> str:
        """Save behavioral rules and user corrections to long-term memory.

        Use when the user corrects your behavior like "don't summarize at the end" or "always use Chinese".

        Args:
            content: One-sentence summary of the behavioral rule to save
        """
        return _save_memory(content, "feedback")

    @tool
    def save_project_memory(content: str) -> str:
        """Save project context, decisions, and ongoing work to long-term memory.

        Use when the user mentions project facts like "we're using FastAPI" or "deadline is next Friday".

        Args:
            content: One-sentence summary of the project fact to save
        """
        return _save_memory(content, "project")

    @tool
    def save_reference_memory(content: str) -> str:
        """Save reference pointers: doc URLs, file paths, API endpoints, config locations.

        Use when the user shares references like "the config is at /etc/app.yaml" or "docs at https://...".

        Args:
            content: One-sentence summary of the reference to save
        """
        return _save_memory(content, "reference")

    # --- Search tools ---
    @tool
    def search_user_memories(query: str) -> str:
        """Search long-term memory for user profile and preferences.

        Use when you need to recall user's name, role, technical background, or personal preferences.

        Args:
            query: What to search for in user memories
        """
        return _search_memories(query, "user")

    @tool
    def search_feedback_memories(query: str) -> str:
        """Search long-term memory for behavioral rules and user corrections.

        Use when you need to recall how the user wants you to behave or past corrections.

        Args:
            query: What to search for in feedback memories
        """
        return _search_memories(query, "feedback")

    @tool
    def search_project_memories(query: str) -> str:
        """Search long-term memory for project context and decisions.

        Use when you need to recall project facts, tech stack, deadlines, or decisions.

        Args:
            query: What to search for in project memories
        """
        return _search_memories(query, "project")

    @tool
    def search_reference_memories(query: str) -> str:
        """Search long-term memory for reference pointers: URLs, file paths, configs.

        Use when you need to find a previously mentioned URL, file path, or API endpoint.

        Args:
            query: What to search for in reference memories
        """
        return _search_memories(query, "reference")

    return [
        save_user_memory, save_feedback_memory, save_project_memory, save_reference_memory,
        search_user_memories, search_feedback_memories, search_project_memories, search_reference_memories,
    ]


def _save_memory(content: str, mem_type: str) -> str:
    """Shared save logic for all 4 save tools."""
    backend = get_memory_backend()
    if backend != "mem0":
        return "当前使用 Markdown 记忆模式，记忆由系统自动管理"

    try:
        from graph.mem0_manager import mem0_manager
    except ImportError:
        return "mem0 模块未安装"

    if not mem0_manager.is_available():
        return "mem0 服务不可用，请检查配置"

    try:
        user_id = current_user_id.get()
        result = mem0_manager.add(
            messages=[{"role": "user", "content": content}],
            user_id=user_id,
            metadata={"type": mem_type},
        )
        type_labels = {"user": "用户画像", "feedback": "行为规则", "project": "项目上下文", "reference": "参考信息"}
        label = type_labels.get(mem_type, mem_type)
        preview = content[:50] + ("..." if len(content) > 50 else "")
        if result:
            return f"[{label}] 记忆已保存: {preview}"
        return f"[{label}] 保存完成（mem0 判断为无需新增）"
    except Exception as e:
        return f"记忆保存失败: {e}"


def _search_memories(query: str, mem_type: str) -> str:
    """Shared search logic for all 4 search tools."""
    backend = get_memory_backend()
    if backend != "mem0":
        return "当前使用 Markdown 记忆模式，不支持分类检索"

    try:
        from graph.mem0_manager import mem0_manager
    except ImportError:
        return "mem0 模块未安装"

    if not mem0_manager.is_available():
        return "mem0 服务不可用"

    try:
        user_id = current_user_id.get()
        results = mem0_manager.search(query, user_id=user_id, limit=10)
        # Filter by type metadata
        typed_results = []
        for r in results:
            metadata = r.get("metadata") or {}
            r_type = metadata.get("type", "")
            if r_type == mem_type:
                typed_results.append(r)

        if not typed_results:
            type_labels = {"user": "用户画像", "feedback": "行为规则", "project": "项目上下文", "reference": "参考信息"}
            return f"未找到相关的{type_labels.get(mem_type, mem_type)}记忆"

        lines = []
        for r in typed_results[:5]:
            score = r.get("score", 0)
            lines.append(f"- [{score:.2f}] {r['memory']}")
        return "\n".join(lines)
    except Exception as e:
        return f"记忆检索失败: {e}"
