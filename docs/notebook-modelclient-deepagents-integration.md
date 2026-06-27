# Notebook 集成：在 DeepAgents 课件中使用 PuddingClaw ModelClient

本文档说明如何在外部 Jupyter Notebook 中直接使用 PuddingClaw backend 的 `ModelClientChatModel` 测试 DeepAgents。

适用场景：

- 在课程 Notebook 中验证 PuddingClaw 的模型接入层。
- 手动测试 `ModelClientChatModel` 是否兼容 latest DeepAgents。
- 以 Higress 为主验证 tool calling / structured output / graph streaming 等路径。

不适用场景：

- 生产部署。
- 修改 PuddingClaw runtime 依赖。
- 把 `ModelClient` 源码复制进 Notebook。

## 1. 目标 Notebook

示例 Notebook：

```text
/Users/pet/Code/AI/Agent/2026全年班_大模型Agent智能体开发实战/【专题课】Harness Engineering驾驭工程实战/Part 3. Harness Engineering 驾驭工程 · DeepAgents 框架实战/HarnessEngineering_第三节_deepAgents实战.ipynb
```

Notebook 中建议直接 import backend 的实现：

```python
from llm.model_client import ModelClientChatModel
```

不要把 `ModelClient` / `ModelClientChatModel` 源码复制进 Notebook，否则测到的不是 PuddingClaw 当前真实实现。

## 2. 注册 Notebook Kernel

在终端执行：

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw/backend

UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache \
uv run --group deepagents-test \
python -m ipykernel install --user \
  --name puddingclaw-deepagents \
  --display-name "PuddingClaw DeepAgents"
```

这一步的含义：

1. 使用 PuddingClaw backend 的 `uv` 项目环境。
2. 启用 `deepagents-test` 测试依赖组。
3. 使用 `deepagents-test` 依赖组中的 `ipykernel`。
4. 把这个 backend Python 环境注册成 Jupyter 可选 Kernel。

注册完成后，在 Notebook 里选择：

```text
PuddingClaw DeepAgents
```

注意：这不是把 `deepagents` 装进全局 Python，也不是生产依赖接入；它只是把 backend 测试环境暴露给 Jupyter Notebook 使用。不要在注册命令里使用 `--with ipykernel`，否则 kernelspec 可能指向 `uv` 的临时环境路径，而不是稳定的 `backend/.venv/bin/python`。

## 3. Notebook 初始化 Cell

在 Notebook 开头增加：

```python
import os
import sys
from pathlib import Path

BACKEND_DIR = Path("/Users/pet/Code/AI/Agent/PuddingClaw/backend")
os.chdir(BACKEND_DIR)

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm.model_client import ModelClientChatModel, ModelClient
```

## 4. Higress 初始化

Notebook 通常运行在宿主机，不在 Docker 网络内，因此不要使用 Docker 内部服务名形式的 Higress 地址。

在 Notebook 中测试 Higress 时，使用宿主机暴露地址：

```text
http://localhost:8080/v1
```

推荐在 Notebook 手动验证中使用临时 monkeypatch，避免改动 backend 持久化配置：

```python
import capabilities
import llm.model_client as mc

capabilities._EFFECTIVE_GATEWAY_URL = "http://localhost:8080/v1"
mc.ModelClient._should_use_gateway = lambda self: True
```

之后创建模型时使用：

```python
model = ModelClientChatModel(
    force_direct=False,
    streaming=False,
)
```

## 5. Higress 基础调用测试

```python
from deepagents import create_deep_agent

model = ModelClientChatModel(
    force_direct=False,
    streaming=False,
)

agent = create_deep_agent(
    model=model,
    tools=[],
    system_prompt="你是一个测试助手。请只回复 MODELCLIENT_OK。",
)

result = agent.invoke({
    "messages": [
        {"role": "user", "content": "请回复 MODELCLIENT_OK"}
    ]
})

result["messages"][-1].content
```

预期：

```text
MODELCLIENT_OK
```

## 6. Higress Tool Calling 测试

```python
from deepagents import create_deep_agent
from langchain_core.tools import tool

@tool
def pudding_probe(text: str) -> str:
    """Return a deterministic marker."""
    return "TOOL_MARKER:" + text

model = ModelClientChatModel(
    force_direct=False,
    streaming=False,
)

agent = create_deep_agent(
    model=model,
    tools=[pudding_probe],
    system_prompt=(
        "你必须调用 pudding_probe，参数 text='deepagents'，"
        "然后只返回工具结果。"
    ),
)

result = agent.invoke({
    "messages": [
        {"role": "user", "content": "调用工具并返回结果"}
    ]
})

result["messages"][-1].content
```

预期：

```text
TOOL_MARKER:deepagents
```

### 可选：修改 backend 配置

如果希望 Notebook 按正常配置走 Higress，可临时把 `backend/config.json` 中的 gateway 地址设为：

```json
{
  "ai_gateway": {
    "base_url": "http://localhost:8080/v1",
    "health_path": "/health",
    "fallback_to_direct": true
  }
}
```

然后使用：

```python
model = ModelClientChatModel(
    force_direct=False,
    streaming=False,
)
```

## 7. Higress Structured Output 测试

```python
from deepagents import create_deep_agent
from pydantic import BaseModel, Field

class ProbeAnswer(BaseModel):
    answer: str = Field(description="short answer")
    score: int = Field(description="confidence score")

model = ModelClientChatModel(force_direct=False, streaming=False)

agent = create_deep_agent(
    model=model,
    tools=[],
    response_format=ProbeAnswer,
)

result = agent.invoke({
    "messages": [
        {"role": "user", "content": "返回 answer='ok', score=9"}
    ]
})

result["structured_response"]
```

预期返回：

```python
ProbeAnswer(answer="ok", score=9)
```

## 8. Higress Graph Streaming 测试

```python
model = ModelClientChatModel(force_direct=False, streaming=False)

agent = create_deep_agent(
    model=model,
    tools=[pudding_probe],
    system_prompt="调用 pudding_probe，参数 text='stream'，然后返回工具结果。",
)

chunks = list(agent.stream(
    {"messages": [{"role": "user", "content": "stream graph"}]},
    stream_mode=["updates", "values"],
))

chunks[:3]
```

你应该能看到 `updates` / `values` 两类 graph event，其中包含 model 与 tools 更新。

## 9. 可选：Direct Provider 排障测试

Notebook 主流程以 Higress 为准。只有当你怀疑问题来自 Higress 配置、网关路由或上游 provider 映射时，再用 direct provider 做对照：

```python
model = ModelClientChatModel(
    force_direct=True,
    streaming=False,
)
```

然后复用上面的基础调用或 tool calling 测试。

## 10. 常见问题

### 10.1 为什么不是直接新建 venv？

`uv run --group deepagents-test ...` 使用的是 backend 项目的 `uv` 环境和锁文件，避免 Notebook 使用一套与 backend 不一致的依赖。

### 10.2 为什么需要 `--group deepagents-test`？

`deepagents` 已进入 backend runtime 依赖。`deepagents-test` 组只用于 Notebook 和集成测试辅助依赖（例如 `ipykernel` / `langgraph-cli`）：

```bash
uv run --group deepagents-test ...
```

### 10.3 为什么 Notebook 中 Higress 要用 localhost？

Docker Compose 内部服务名 `higress` 只在 Docker 网络内可解析。Notebook 运行在宿主机时，应使用 Higress 暴露到宿主机的端口：

```text
http://localhost:8080/v1
```

### 10.4 是否需要 LangSmith key？

普通 Notebook 验证不需要 LangSmith key。

只有当你要验证 LangSmith 云端 trace 页面中的主 Agent / model / tool / subagent span 时，才需要：

```bash
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=...
```

## 11. 对应自动化测试

Notebook 手动验证对应的自动化测试在：

```text
backend/tests/integration/test_model_client_deepagents_latest.py
backend/tests/test_model_client_chat_model.py
```

可在 backend 中运行：

```bash
cd /Users/pet/Code/AI/Agent/PuddingClaw/backend

UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache \
uv run --group deepagents-test \
pytest tests/integration/test_model_client_deepagents_latest.py -q
```

全量 backend 测试：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest tests -q
```
