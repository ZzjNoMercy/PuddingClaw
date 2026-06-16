"""Compression middleware 最小升级补丁

只需把 graph/middlewares/compression.py 中的 count_tokens_tiktoken 替换为以下版本，
其余逻辑（ToolResultClear / Summarization / Compaction）完全保留 V5 即可。

如果你的 V5 compression.py 中缺少以下类/阈值，请按生产版本补齐：

- ToolResultClearMiddleware:
    keep_recent_tool_results = 50
    _CACHE_MAX_SIZE = 500
- MessageTrimMiddleware（可选，agent.py 中已由 _build_messages 硬截断接管）:
    max_tokens = 12000
    keep_last = 10
- SummarizationMiddleware（通过 build_compression_middlewares 配置）:
    trigger_tokens = 50000（默认）/ 80000（agent.py 内联运行值）
    keep_messages = 10
- CompactionMiddleware:
    trigger_tokens = 100000（默认）/ 150000（agent.py 内联运行值）
    keep_recent = 4
"""

import json

# 替换原有 count_tokens_tiktoken 函数：
def count_tokens_tiktoken(messages) -> int:
    """Count tokens across messages, using tiktoken cl100k_base.

    升级点：额外计入 tool_calls 的 token（修复 V5 遗漏）。
    """
    total = 0
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(content, list):
            content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        if not isinstance(content, str):
            content = str(content)
        total += _encode_text(content)
        # 新增：计入 tool_calls 的 token
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                if isinstance(tc, dict):
                    tc_text = json.dumps(tc, ensure_ascii=False)
                else:
                    tc_text = json.dumps(tc.__dict__, ensure_ascii=False)
                total += _encode_text(tc_text)
    return total


# 在同一文件里新增这个函数：
def count_text_tokens(text: str) -> int:
    """计算单段文本的 token 数（供 system prompt 等使用）。"""
    return _encode_text(text)
