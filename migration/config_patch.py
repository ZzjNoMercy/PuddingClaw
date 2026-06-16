"""config.py 最小补丁

在 V5 的 backend/config.py 中，找到 load_config() 函数附近，追加以下两个函数。

同时建议在 config.json 的 llm 配置块中增加 "context_window" 字段。
"""

# 1) 在 config.py 末尾追加这两个函数：

def get_max_history_messages() -> int:
    """获取最大历史消息条数。"""
    return load_config().get("compression", {}).get("max_history_messages", 100)


def get_context_window() -> int:
    """获取当前模型的上下文窗口大小。"""
    return load_config().get("llm", {}).get("context_window", 200000)


# 2) 在 config.json 的 llm 块中增加字段：
"""
"llm": {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "temperature": 0.7,
    "max_tokens": 4096,
    "context_window": 200000   // <-- 新增：用于 Context Rot 计算
}
"""

# 3) 可选：在 compression 块中增加 max_history_messages：
"""
"compression": {
    ...
    "max_history_messages": 100   // <-- 新增：硬截断的条数上限（默认 100）
}
"""
