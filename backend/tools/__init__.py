"""Core Tools factory — auto-discovers all *_tool.py files in this package."""

import importlib
import inspect
from pathlib import Path
from typing import List

from langchain_core.tools import BaseTool

# 模块级工具实例缓存：避免动态加载路径每次请求重建工具对象
# key = (module_name, base_dir)，value = List[BaseTool]
# 保证 SearchKnowledgeBaseTool._index 等有状态缓存不丢失
_tool_instance_cache: dict[tuple[str, str], List[BaseTool]] = {}

# 动态工具加载注册表：按意图类别分组，用于按需加载工具子集
# core 类别始终加载；其他类别根据用户消息意图检测按需激活
TOOL_CATEGORIES: dict[str, list[str]] = {
    "core": ["read_file_tool", "write_file_tool", "terminal_tool", "task_manager_tool"],
    "knowledge": ["search_knowledge_tool", "fetch_url_tool"],
    # research 独立于 knowledge：deep_research 是 subagent 隔离工具，不是简单检索
    # cf. graph/middlewares/skills_router.py _DEFAULT_SKILL_REGISTRY["research"]
    "research": ["deep_research_tool"],
    "skill": ["execute_skill_tool", "create_skill_version_tool"],
    "code_exec": ["python_repl_tool"],
    "memory": ["memory_tools"],
}


def _load_tool_module(module_name: str, base_dir: Path) -> List[BaseTool]:
    """加载单个工具模块并返回其创建的工具列表（带实例缓存）。

    内部辅助函数，被 get_all_tools() 和 get_tools_by_categories() 共用。
    缓存工具实例，避免动态加载路径每次请求重建（保护 SearchKnowledgeBaseTool._index 等有状态缓存）。
    """
    cache_key = (module_name, str(base_dir))
    if cache_key in _tool_instance_cache:
        return _tool_instance_cache[cache_key]

    try:
        module = importlib.import_module(f".{module_name}", package=__package__)
        factory = next(
            (
                obj
                for name, obj in inspect.getmembers(module, inspect.isfunction)
                if name.startswith("create_")
            ),
            None,
        )
        if factory is None:
            print(f"[tools] Warning: no create_* function found in {module_name}")
            return []

        params = list(inspect.signature(factory).parameters.values())
        result = factory(base_dir) if (params and ("dir" in params[0].name or "path" in params[0].name)) else factory()

        tools = result if isinstance(result, list) else [result]
        _tool_instance_cache[cache_key] = tools
        return tools
    except Exception as exc:
        print(f"[tools] Warning: failed to load {module_name}: {exc}")
        return []


def get_all_tools(base_dir: Path) -> List[BaseTool]:
    """Create and return all tools by auto-scanning *_tool.py files in tools/."""
    tools_dir = Path(__file__).parent
    tools: List[BaseTool] = []

    tool_files = sorted(set(tools_dir.glob("*_tool.py")) | set(tools_dir.glob("*_tools.py")))
    for tool_file in tool_files:
        module_name = tool_file.stem
        if module_name in ("__init__", "skills_scanner"):
            continue
        tools.extend(_load_tool_module(module_name, base_dir))

    safe = sum(1 for t in tools if getattr(t, 'risk_level', 'safe') == 'safe')
    moderate = sum(1 for t in tools if getattr(t, 'risk_level', '') == 'moderate')
    dangerous = sum(1 for t in tools if getattr(t, 'risk_level', '') == 'dangerous')
    print(f"[tools] Loaded {len(tools)} tools (safe={safe}, moderate={moderate}, dangerous={dangerous})")

    return tools


def get_tools_by_categories(base_dir: Path, categories: set[str]) -> List[BaseTool]:
    """按类别按需加载工具，始终包含 core 类别。

    用于动态工具加载：根据用户意图检测结果只加载相关工具子集，
    减少无关工具对 LLM 的干扰，降低 token 消耗。
    """
    # core 类别始终加载
    active_categories = categories | {"core"}
    seen_modules: set[str] = set()
    tools: List[BaseTool] = []

    for category in active_categories:
        module_names = TOOL_CATEGORIES.get(category, [])
        for module_name in module_names:
            if module_name in seen_modules:
                continue
            seen_modules.add(module_name)
            tools.extend(_load_tool_module(module_name, base_dir))

    safe = sum(1 for t in tools if getattr(t, 'risk_level', 'safe') == 'safe')
    moderate = sum(1 for t in tools if getattr(t, 'risk_level', '') == 'moderate')
    dangerous = sum(1 for t in tools if getattr(t, 'risk_level', '') == 'dangerous')
    print(f"[tools] Dynamic load: {len(tools)} tools from categories={sorted(active_categories)} "
          f"(safe={safe}, moderate={moderate}, dangerous={dangerous})")

    return tools
