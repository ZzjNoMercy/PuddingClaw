"""config.py + config.json 补丁（含 MCP 支持）

在 V5 的 backend/config.py 中，找到 load_config() 函数附近，追加以下函数。
"""

# 1) 在 config.py 末尾追加：

def get_max_history_messages() -> int:
    return load_config().get("compression", {}).get("max_history_messages", 100)


def get_context_window() -> int:
    return load_config().get("llm", {}).get("context_window", 200000)


# 2) 在 config.json 中增加/修改以下字段：
"""
{
  "llm": {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "temperature": 0.7,
    "max_tokens": 4096,
    "context_window": 200000
  },
  "compression": {
    "ratio": 0.5,
    "trigger_count": 15,
    "max_history_messages": 100,
    "middleware": { ... }
  },
  "mcp": {
    "enabled": ["technical_qa"]
  }
}
"""

# 3) 依赖安装：
# pip install langchain-mcp-adapters
