# ModelClient × DeepAgents latest 测试记录

日期：2026-06-25  
状态：已完成一轮隔离环境烟测  
目标：验证当前 `ModelClientChatModel` 能否在不修改项目依赖的前提下，作为最新 DeepAgents 的 `model=` 参数运行。

## 1. 隔离环境

使用 `uv run --no-project --with ...` 创建临时 Python 依赖环境，不修改：

- `backend/requirements.txt`
- `backend/pyproject.toml`
- `backend/uv.lock`

本轮解析到的版本：

- `deepagents==0.6.11`
- `langchain==1.3.11`
- `langchain-core==1.4.8`
- `langgraph==1.2.6`

说明：这组依赖高于当前后端固定依赖中的 LangChain / LangGraph 版本，因此目前适合先作为兼容性探针和独立集成测试环境，不建议直接合入生产依赖锁定。

## 2. 本地现有测试

命令：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest tests/test_model_client.py -q
```

结果：

```text
7 passed in 2.03s
```

结论：当前既有 `ModelClient` 单元测试通过。

## 3. DeepAgents latest API 入口确认

`deepagents.create_deep_agent` 在 `0.6.11` 中仍支持：

```text
model: str | BaseChatModel | None
```

同时文档提示 DeepAgents 需要支持 tool calling 的 LLM。

结论：`ModelClientChatModel(BaseChatModel)` 是正确的适配方向；关键在于它是否足够保持 LangChain ChatModel 合约。

## 4. 无真实 Provider 的协议探针

方法：在 latest DeepAgents 隔离环境中，用 fake `BaseChatModel` 替代实际 provider，验证 DeepAgents 是否能接受当前 `ModelClientChatModel` 并完成工具调用循环。

结果：

```text
RESULT_MESSAGES 4
FAKE_CALLS 2
BOUND_TOOLS_COUNT 9
BOUND_KWARGS [{}, {}]
LAST FINAL_OK
```

结论：

- DeepAgents latest 能接受当前 `ModelClientChatModel`。
- DeepAgents 工具调用循环可以跑通。
- 底层模型确实收到工具定义。
- 当前 `bind_tools` 传到底层的 kwargs 为空；如果上层使用 `tool_choice` / `strict` / `parallel_tool_calls` 等高级参数，当前 wrapper 仍可能与原生 `ChatOpenAI` / `ChatDeepSeek` 不完全一致。

## 5. Provider 直连烟测

路径：

```text
DeepAgents latest -> ModelClientChatModel(force_direct=True) -> fallback provider
```

结果：

```text
MESSAGES 2
LAST MODELCLIENT_DEEPAGENTS_OK
```

结论：在 latest DeepAgents 隔离环境中，当前 `ModelClientChatModel` 可以通过 provider 直连完成真实模型调用。

## 6. Higress 路由烟测

路径：

```text
DeepAgents latest -> ModelClientChatModel -> Higress localhost:8080/v1 -> provider
```

说明：主机侧不能解析 Docker 网络名 `higress`，测试中将 gateway URL 指向宿主机暴露的 `http://localhost:8080/v1`。

结果：

```text
MESSAGES 2
LAST HIGRESS_DEEPAGENTS_OK
```

结论：latest DeepAgents + 当前 `ModelClientChatModel` + Higress 路由可以完成真实模型调用。

## 7. Higress + 真实工具调用烟测

路径：

```text
DeepAgents latest
  -> ModelClientChatModel
  -> Higress localhost:8080/v1
  -> provider tool calling
  -> DeepAgents tool execution
  -> final answer
```

自定义工具：

```python
@tool
def pudding_probe(text: str) -> str:
    return "TOOL_MARKER:" + text
```

结果：

```text
MESSAGES 4
0 HumanMessage Call the required tool and return only its result. None
1 AIMessage  [{'name': 'pudding_probe', 'args': {'text': 'deepagents'}, ...}]
2 ToolMessage TOOL_MARKER:deepagents None
3 AIMessage TOOL_MARKER:deepagents []
LAST TOOL_MARKER:deepagents
```

结论：真实工具调用链路通过。模型通过 Higress 收到工具定义，发起 tool call，DeepAgents 执行工具，模型回收工具结果并返回最终答案。

## 8. 当前判断

可以开展基于最新 DeepAgents 的测试，并且当前实现已经通过最小可用链路：

- latest DeepAgents 可加载当前 `ModelClientChatModel`
- provider 直连可用
- Higress 路由可用
- Higress + tool calling 可用

但它还不是“完全等价于原生 ChatOpenAI / ChatDeepSeek”的状态。下一步建议优先补齐：

1. `ModelClientChatModel.bind_tools(tools, **kwargs)` 保留并传递 kwargs。
2. fallback 到 direct provider 时保留已绑定 tools 和 bind kwargs。
3. `invoke/ainvoke/stream/astream` 透传 `config`、`stop`、callbacks、以及 LangChain runtime kwargs。
4. 将本轮 smoke probe 固化为可跳过的 integration tests，例如通过 `PUDDINGCLAW_RUN_DEEPAGENTS_LATEST=1` 启用。

## 10. 自动化测试落地

新增测试文件：

- `backend/tests/integration/test_model_client_deepagents_latest.py`

设计原则：

- API 以 latest DeepAgents 为准。
- Notebook 只作为功能覆盖参考，不复刻旧版 API。
- 项目默认环境未安装 `deepagents` 时自动 skip。
- latest 隔离环境中实际执行测试。
- 使用 fake `BaseChatModel`，不消耗真实 provider token，重点验证 `ModelClientChatModel` 的协议兼容性。

已覆盖：

- `MC-DA-006a` latest DeepAgents 默认 middleware + 自定义工具能通过 `ModelClientChatModel` 完成工具循环。
- `MC-DA-019a` latest DeepAgents 默认 `general-purpose` 子代理可经由 `task` 工具回调同一个 `ModelClientChatModel` wrapper。
- `MC-DA-025a` / `MC-DA-026a` latest DeepAgents HITL interrupt + approve resume 后，历史 tool call / ToolMessage 流程可继续被模型消费。

默认项目环境验证：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest tests/test_model_client.py tests/integration/test_model_client_deepagents_latest.py -q
```

结果：

```text
7 passed, 1 skipped in 1.57s
```

latest DeepAgents 隔离环境验证：

```bash
env PYTHONPATH=/Users/pet/Code/AI/Agent/PuddingClaw/backend \
  UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache \
  uv run --no-project \
    --with deepagents==0.6.11 \
    --with langchain-openai \
    --with langchain-deepseek \
    --with pytest \
    pytest /Users/pet/Code/AI/Agent/PuddingClaw/backend/tests/integration/test_model_client_deepagents_latest.py -q
```

结果：

```text
3 passed in 1.93s
```

## 12. 第二批契约补强与测试

本轮补强：

- `ModelClient` 新增 `bind_tools_kwargs`，保持 `tool_choice` / `strict` / `parallel_tool_calls` 等参数。
- `ModelClient` 的 `invoke` / `ainvoke` / `stream` / `astream` 透传 `config`、`stop`、`**kwargs`。
- gateway fallback 到 direct provider 时复用 tools 与 bind kwargs。
- `ModelClientChatModel` 移除自定义 `invoke/ainvoke` 覆盖，回到标准 `BaseChatModel` 输入转换与 callback 生命周期。
- `ModelClientChatModel._generate/_agenerate/_stream/_astream` 将 `stop` 与 runtime kwargs 继续传给 `ModelClient`。

新增测试文件：

- `backend/tests/test_model_client_chat_model.py`

新增覆盖：

- `MC-U-013` 字符串输入转换为 `HumanMessage`。
- `MC-U-015` `stop`、provider kwargs、timeout 等调用参数透传。
- `MC-U-023` `bind_tools(..., tool_choice=...)` 不丢失。
- `MC-U-024` `strict` / `parallel_tool_calls` 不丢失。
- `MC-U-028` gateway fallback 后仍保留 tools 与 bind kwargs。

默认项目环境验证：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest \
  tests/test_model_client.py \
  tests/test_model_client_chat_model.py \
  tests/integration/test_model_client_deepagents_latest.py \
  -q
```

结果：

```text
10 passed, 1 skipped in 1.55s
```

latest DeepAgents 隔离环境验证：

```bash
env PYTHONPATH=/Users/pet/Code/AI/Agent/PuddingClaw/backend \
  UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache \
  uv run --no-project \
    --with deepagents==0.6.11 \
    --with langchain-openai \
    --with langchain-deepseek \
    --with pytest \
    pytest /Users/pet/Code/AI/Agent/PuddingClaw/backend/tests/integration/test_model_client_deepagents_latest.py -q
```

结果：

```text
3 passed in 1.68s
```

真实 Higress + latest DeepAgents + tool calling 烟测：

```text
MESSAGES 4
LAST TOOL_MARKER:deepagents
```

全量 backend 测试（第一次）：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest tests -q
```

结果：

```text
79 passed, 1 skipped, 1 failed
```

失败项：

- `tests/test_context_optimizations.py::TestToolResultClearMiddleware::test_only_summarizes_before_last_human`

失败原因：测试期望 `runtime.stream_writer` 只调用 1 次；当前实现会发送 `context_maintenance start`、`context_maintenance done`、`tool_result_clear summary` 共 3 次。该失败属于现有 context middleware 事件预期漂移，和本轮 `ModelClient` 契约补强无直接关系。

修复：已更新 `tests/test_context_optimizations.py::TestToolResultClearMiddleware::test_only_summarizes_before_last_human`，不再只断言调用次数，而是精确断言 3 个事件的语义：

1. `context_maintenance start`
2. `context_maintenance done`
3. `tool_result_clear summary`

全量 backend 测试（修复后）：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest tests -q
```

结果：

```text
80 passed, 1 skipped in 1.96s
```

latest DeepAgents 隔离环境复测：

```text
3 passed in 1.92s
```

真实 Higress + latest DeepAgents + tool calling 复测：

```text
MESSAGES 4
LAST TOOL_MARKER:deepagents
```

## 11. 执行记录

- 2026-06-25：完成 latest DeepAgents 隔离环境版本确认、现有单测、fake model 协议探针、provider 直连烟测、Higress 烟测、Higress 真实工具调用烟测。
- 2026-06-25：新增 latest DeepAgents 自动化 contract tests，并验证默认环境 skip、latest 隔离环境通过。
- 2026-06-25：完成第二批 `ModelClientChatModel` 契约补强与测试；真实 Higress tool calling 复测通过；全量 backend 测试剩余 1 个既有 context middleware 事件预期失败。
- 2026-06-25：修复 context middleware 测试预期，按当前事件协议断言 start/done/summary；全量 backend 测试通过。

## 13. 第三批：结构化输出、流式、callbacks

本轮新增覆盖：

- 结构化输出：`ModelClientChatModel.with_structured_output(PydanticModel)` 可经由 LangChain bind-tools parser 返回 Pydantic 实例。
- 同步流式：`stream` 保留 chunk 顺序，并透传 `stop` / provider kwargs。
- 异步流式：`astream` 保留 chunk 顺序，并透传 `stop` / provider kwargs。
- 流式 fallback：gateway 首 chunk 前失败可回退 direct，且保留 tools / bind kwargs。
- 流式失败边界：gateway 已输出 chunk 后失败不回退，避免重复内容。
- callbacks：标准 `BaseChatModel` callback 生命周期可收到 start/end/error。

新增/更新测试：

- `backend/tests/test_model_client_chat_model.py`

ModelClient 相关测试：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest \
  tests/test_model_client.py \
  tests/test_model_client_chat_model.py \
  tests/integration/test_model_client_deepagents_latest.py \
  -q
```

结果：

```text
17 passed, 1 skipped in 1.43s
```

latest DeepAgents 隔离测试：

```bash
env PYTHONPATH=/Users/pet/Code/AI/Agent/PuddingClaw/backend \
  UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache \
  uv run --no-project \
    --with deepagents==0.6.11 \
    --with langchain-openai \
    --with langchain-deepseek \
    --with pytest \
    pytest /Users/pet/Code/AI/Agent/PuddingClaw/backend/tests/integration/test_model_client_deepagents_latest.py -q
```

结果：

```text
3 passed in 1.74s
```

全量 backend 测试：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest tests -q
```

结果：

```text
87 passed, 1 skipped in 1.82s
```

真实 Higress + latest DeepAgents + tool calling 复测：

```text
MESSAGES 4
LAST TOOL_MARKER:deepagents
```

剩余注意：

- callbacks 已覆盖 start/end/error 生命周期；流式 token-level callback 还需要真实 provider streaming 或更细 fake callback manager 单独验证。
- structured output 已覆盖 LangChain Pydantic parser；DeepAgents `response_format=` 的真实 graph 集成仍可作为下一批 E2E。

## 14. 常规 tracing 与 backend 测试环境迁移

本轮补充：

- callbacks 常规测试不依赖 LangSmith key，已断言：
  - start/end/error 生命周期
  - `tags`
  - `metadata`
  - `run_name`
- backend Python 要求提升为 `>=3.11,<3.13`，与 latest DeepAgents `Python>=3.11` 对齐。
- 新增 backend dependency group：

```toml
[dependency-groups]
deepagents-test = [
    "deepagents==0.6.11",
]
```

迁移后，latest DeepAgents 测试不再需要手写：

```bash
uv run --no-project --with deepagents==0.6.11 ...
```

改为 backend 内命令：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache \
  uv run --group deepagents-test \
  pytest tests/integration/test_model_client_deepagents_latest.py -q
```

结果：

```text
3 passed in 2.06s
```

全量 backend 测试：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest tests -q
```

结果：

```text
90 passed in 3.02s
```

当前已无 skipped。

backend 内真实 Higress + latest DeepAgents + tool calling 复测：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache \
  uv run --group deepagents-test python <smoke>
```

结果：

```text
MESSAGES 4
LAST TOOL_MARKER:deepagents
```

LangSmith key 说明：

- 当前常规 tracing/callback 合约测试不需要 LangSmith key。
- 如果要验证 LangSmith 云端 trace 页面里的主 Agent / tool / subagent span，需要后续提供 `LANGSMITH_API_KEY`、`LANGSMITH_TRACING=true`、`LANGSMITH_PROJECT` 后再跑真实 E2E。

## 15. 第四批：DeepAgents E2E 扩展

按优先级继续补充 latest DeepAgents 功能路径：

1. `response_format=` graph E2E
2. `agent.stream(...)` graph streaming
3. HITL `edit` / `reject` / `respond`
4. `FilesystemBackend` / `CompositeBackend` 文件落盘
5. 自定义 `SubAgent` / `CompiledSubAgent`

新增/扩展测试文件：

- `backend/tests/integration/test_model_client_deepagents_latest.py`

新增覆盖：

- DeepAgents `create_deep_agent(..., response_format=PydanticModel)` 返回 `structured_response`。
- DeepAgents 为结构化输出通过 `bind_tools(..., tool_choice="any")` 注入 schema。
- `agent.stream(..., stream_mode=["updates", "values"])` 能输出 model/tools 更新和最终 state。
- HITL `edit` 决策修改工具参数后执行工具，模型继续收敛到最终答案。
- HITL `reject` 决策生成 error ToolMessage，模型能消费拒绝结果并继续。
- HITL `respond` 决策由人工直接提供 ToolMessage，模型能消费并继续。
- `FilesystemBackend(root_dir=tmp_path, virtual_mode=True)` 可通过 `write_file` 真实落盘。
- `CompositeBackend(default=StateBackend(), routes={"/artifacts/": FilesystemBackend(...)})` 可将 `/artifacts/...` 路由到文件系统并真实落盘。
- 自定义 `SubAgent` 经 `task` 工具执行后，主 Agent 能消费返回结果。
- `CompiledSubAgent` 返回 `messages` 后，主 Agent 能消费返回结果。

latest DeepAgents backend 内测试：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache \
  uv run --group deepagents-test \
  pytest tests/integration/test_model_client_deepagents_latest.py -q
```

结果：

```text
12 passed in 2.25s
```

全量 backend 测试：

```bash
UV_CACHE_DIR=/private/tmp/puddingclaw-uv-cache uv run pytest tests -q
```

结果：

```text
99 passed in 2.81s
```

真实 Higress + latest DeepAgents + tool calling 复测：

```text
MESSAGES 4
LAST TOOL_MARKER:deepagents
```

仍未覆盖的更深项：

- `write_todos`、`read_file`、`edit_file`、`grep/glob` 全工具矩阵。
- Skills metadata / 完整 `SKILL.md` 按需读取。
- Memory / `AGENTS.md` 注入。
- Permissions denied 后不死循环。
- LangSmith 云端 trace UI 验收。
- Provider 路由矩阵与并发稳定性。

## 16. Notebook 集成说明

Notebook 手动验证流程已单独整理：

- `docs/notebook-modelclient-deepagents-integration.md`

该文档以 Higress 为主路径，说明如何把 backend 的 `deepagents-test` 环境注册为 Jupyter Kernel，并在课程 Notebook 中直接使用：

```python
from llm.model_client import ModelClientChatModel
```

主流程覆盖：

- Higress 基础调用
- Higress tool calling
- Higress structured output
- Higress graph streaming

Direct provider 只作为排障对照路径。
