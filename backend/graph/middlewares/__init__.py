"""Shared cache, compression, Skill intent, Toolset and write middleware exports."""
from graph.middlewares.cache import (
    DeepSeekCacheBoundaryMiddleware,
    TailTrimMiddleware,
    build_cache_middlewares,
)
from graph.middlewares.compression import (
    ToolResultClearMiddleware,
    MessageTrimMiddleware,
    CompactionMiddleware,
    build_compression_middlewares,
    count_tokens_tiktoken,
    TOOL_SUMMARY_PROMPTS,
    DEFAULT_TOOL_SUMMARY_PROMPT,
    COMPACTION_SUMMARY_PROMPT,
    SUMMARIZATION_PROMPT_ZH,
    SUMMARY_PREFIX,
    COMPRESSED_CONTEXT_PREFIX,
)
from graph.middlewares.skill_intent_router import SkillIntentRouterMiddleware
from graph.middlewares.toolset import ToolsetMiddleware, discover_skill_toolsets
from graph.middlewares.task_state import (
    TaskStateMiddleware,
    build_write_middlewares,
)

__all__ = [
    # Cache (Ch5)
    "DeepSeekCacheBoundaryMiddleware",
    "TailTrimMiddleware",
    "build_cache_middlewares",
    # Compression (Ch1)
    "ToolResultClearMiddleware",
    "MessageTrimMiddleware",  # 类保留供外部/测试使用；默认装配已移除（由 TailTrim 接管）
    "CompactionMiddleware",
    "build_compression_middlewares",
    "count_tokens_tiktoken",
    "TOOL_SUMMARY_PROMPTS",
    "DEFAULT_TOOL_SUMMARY_PROMPT",
    "COMPACTION_SUMMARY_PROMPT",
    "SUMMARIZATION_PROMPT_ZH",
    "SUMMARY_PREFIX",
    "COMPRESSED_CONTEXT_PREFIX",
    # Skill intent / toolset boundary
    "SkillIntentRouterMiddleware",
    "ToolsetMiddleware",
    "discover_skill_toolsets",
    # Write (Ch2)
    "TaskStateMiddleware",
    "build_write_middlewares",
]
