# V5 → 魔镜Claw 后端增量迁移指南（完整版）

> **目标**：在 V5 后端基础上，移植魔镜Claw 的**上下文工程优化**、**提示词工程**、**MCP 持久会话**三大核心能力。  
> **前提**：前端保留 V5 React 版本，不引入 JWT/Django 认证，不引入 OSS 持久化。

---

## 一、交付文件清单

将 `migration/` 目录中的以下文件复制到 V5 项目的 `backend/` 目录中：

| # | 迁移文件 | 覆盖目标 | 说明 |
|---|---------|---------|------|
| 1 | `graph_agent_with_mcp.py` | `backend/graph/agent.py` | Agent 核心：上下文预算、MCP 持久会话 |
| 2 | `graph_prompt_builder.py` | `backend/graph/prompt_builder.py` | 提示词工程：日期/环境注入、工具指南 |
| 3 | `api_chat.py` | `backend/api/chat.py` | API 层：口头写入检测、错误分类、SSE 事件增强 |
| 4 | `mcp_clients/__init__.py` | `backend/mcp_clients/__init__.py`（新建） | MCP Client 工厂 |
| 5 | `mcp_clients/servers.py` | `backend/mcp_clients/servers.py`（新建） | MCP 服务器注册表 |
| 6 | `middleware_compression_patch.py` | `backend/graph/middlewares/compression.py`（手动替换） | Token 计数升级：计入 tool_calls |
| 7 | `middleware_cache_patch.py` | `backend/graph/middlewares/cache.py`（新建/覆盖） | Cache-friendly 中段裁剪：TailTrim + CacheBoundary |

---

## 二、依赖安装

```bash
pip install langchain-mcp-adapters
```

> 其他依赖（`langchain-deepseek`、`sse-starlette` 等）沿用 V5 已有环境即可。

---

## 三、配置修改

### 3.1 config.json（新增字段）

在 V5 的 `backend/config.json` 中补充以下字段：

```json
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
    "enabled": []
  }
}
```

| 新增字段 | 含义 | 建议值 |
|---------|------|--------|
| `llm.context_window` | 模型上下文窗口大小，用于 Context Rot 计算 | DeepSeek V3: `200000` |
| `compression.max_history_messages` | 按条数硬截断的上限 | `100` |
| `compression.cache.tail_trim.max_tokens` | TailTrim 触发阈值（token 数超过此值才裁剪中段） | `50000`（200K 窗口的 25%） |
| `compression.cache.tail_trim.head_keep` | 保护区：前几条消息不删，稳定 prefix cache | `2` |
| `compression.cache.tail_trim.keep_recent` | 保护区：末尾保留最近多少条消息 | `30` |
| `mcp.enabled` | 启用的 MCP 服务器名称列表 | `[]`（默认关闭） |

> **TailTrim 阈值说明**：`max_tokens=50000` 配合 `keep_recent=30` 是 200K 窗口模型的推荐值。`head_keep=2` 保护首轮 user+assistant，确保 DeepSeek prefix cache 锚点不被破坏。Agent 代码中已内联这些默认值，如想调整，直接修改 `backend/graph/agent.py` 中 `build_cache_middlewares({...})` 的 `tail_trim` 字段即可。

### 3.2 config.py（追加函数）

在 `backend/config.py` 末尾追加：

```python
def get_max_history_messages() -> int:
    return load_config().get("compression", {}).get("max_history_messages", 100)


def get_context_window() -> int:
    return load_config().get("llm", {}).get("context_window", 200000)
```

### 3.3 MCP 服务器注册（按需）

编辑 `backend/mcp_clients/servers.py`，在 `_REGISTRY` 中注册你的 MCP Server：

```python
_REGISTRY: dict[str, Any] = {
    "my_server": {
        "transport": "streamable-http",  # 或 "sse"
        "url": "https://your-mcp-server.com/mcp",
        "headers": {
            "Authorization": f"Bearer {_get_env('MCP_API_KEY')}"
        },
        "timeout": 60,
    },
}

_SERVER_DISPLAY_NAMES = {
    "my_server": "我的MCP服务",
}
```

然后在 `config.json` 中启用：

```json
{
  "mcp": {
    "enabled": ["my_server"]
  }
}
```

### 3.4 所有中间件阈值速查表（200K 窗口模型版）

以下阈值已**全部硬编码在 `agent.py`** 的 `_build_agent_core` / `_make_agent_with_mw` 中，开箱即用。如需调整，直接改代码中的字面量即可。

| 中间件 | 参数 | 运行值 | 说明 |
|--------|------|--------|------|
| **TailTrim** | `max_tokens` | `50000` | 超过此 token 数触发中段裁剪（200K 窗口的 25%） |
| | `head_keep` | `2` | 保护前 2 条消息，稳定 DeepSeek prefix cache |
| | `keep_recent` | `30` | 保留末尾最近 30 条；裁剪点对齐 HumanMessage |
| **ToolResultClear** | `keep_recent` | `50` | 保留最近 50 条 ToolMessage，超出用 LLM 摘要替换 |
| **Summarization** | `trigger_tokens` | `80000` | Token 超 80K 时把历史压缩为叙述性摘要 |
| | `keep_messages` | `10` | 摘要时保留最近 10 条消息不压缩 |
| **Compaction** | `trigger_tokens` | `150000` | Token 超 150K 时执行全局压缩重启（最后保险丝） |
| | `keep_recent` | `4` | 压缩后保留最近 4 条消息 + System + 摘要 |
| **Context Rot（_build_messages）** | `warning_ratio` | `0.40` | 上下文窗口 40% 时 logger.warning |
| | `critical_ratio` | `0.85` | 上下文窗口 85% 时强制截断，只保留最近一半 |
| **tool_reminder** | 触发条件 | `len(history) >= 12` | 历史超过 12 轮时在 system prompt 注入工具调用提醒 |
| **TOOL_RESULT_CLEARING** | 阈值 | `999999999` | 工具返回值超过此长度才走 LLM 摘要（实际靠 middleware 主力摘要） |

> **Middleware 叠加顺序（必须严格遵守）**：
> ```
> ToolResultClear → CacheBoundary → TailTrim → Summarization → Compaction → SkillsRouter → TaskState
> ```
> 特别说明：`ToolResultClear` 必须排在 `TailTrim` 之前。如果 TailTrim 先执行，会把中段 ToolMessage 删掉，导致 ToolResultClear 永远看不到超过 50 条的 ToolMessage，永远触发不了。

---

## 四、核心改动详解

### 4.1 Agent 核心（`graph/agent.py`）

#### A. Token 预算感知（Context Rot 防护）

```
上下文窗口利用率检测：
- 40%  → logger.warning 提示
- 85%  → 强制截断历史，保留最近一半消息
```

截断时**保护 AIMessage↔ToolMessage 配对**，避免 LLM 看到半截 tool call。

#### A+. Cache-friendly 中段裁剪（TailTrimMiddleware）

Token 预算感知（85% 强制截断）是最后一道保险，但触发时已经处于高压力区。
TailTrim 作为**日常主力裁剪**，在 token 超过 `max_tokens`（默认 50K）时提前成对删除中段历史，特点：

1. **不破坏 prefix cache**：只删中段，保留前缀（head_keep=2）和末尾（keep_recent=30）不动
2. **AI↔Tool 原子配对**：有 tool_calls 的 AIMessage 必须与对应 ToolMessage 一起删，否则下轮 LLM 报 400
3. **保存完整的一轮对话再裁剪（关键修复）**：`tail_start` 必须对齐到 `HumanMessage` 边界。如果保留区起点落在某个任务中间（如只剩 tool 输出、丢了最终结论），LLM 会因看到半截上下文而**反复开展同一任务**；对齐到 HumanMessage 后，tail 区永远是完整的最近任务，杜绝此问题。

推荐阈值（200K 窗口）：
- `max_tokens=50000`（25% 窗口）
- `head_keep=2`（保护首轮对话）
- `keep_recent=30`（保留最近 15 轮）

> **Middleware 顺序必须严格遵守**：`ToolResultClear → CacheBoundary → TailTrim → Summarization → Compaction → SkillsRouter → TaskState`。特别地，`ToolResultClear` 必须排在 `TailTrim` 之前。如果顺序颠倒，TailTrim 会先把中段 ToolMessage 删掉，导致 ToolResultClear 永远看不到超过 50 条的 ToolMessage，永远触发不了。

#### B. 历史 tool_calls 正确还原（修复 V5 Bug）

V5 加载历史会话时，assistant 消息的 `tool_calls` 以原始 dict 存储，但未正确还原为 LangChain `AIMessage.tool_calls`，导致 LLM 看不到之前的工具输出。

迁移后 `_build_messages()` 会自动还原：
- `tool_calls` → `AIMessage.tool_calls`
- `tool_calls[i].output` → `ToolMessage(content=output)`

#### C. MCP 持久会话模式

```
用户提问 → 检查 MCP Client 是否已创建
         → 若已启用 MCP Server：
             AsyncExitStack 建立持久 Session
             load_mcp_tools() 加载远程工具
             与本地工具合并后构建 Agent
             流式对话
             Session 自动关闭
         → 若 MCP 失败：fallback 到本地工具模式
```

#### D. SSE 事件增强

| 事件类型 | 触发时机 | 前端处理建议 |
|---------|---------|------------|
| `retrieval` | RAG/mem0 检索到记忆 | 已有，显示引用来源 |
| `context_usage` | 每次 tool 后 + 对话结束时 | **可选**：显示用量百分比 |
| `token` | LLM 输出 token | 已有，实时渲染 |
| `new_response` | 工具执行后重新生成 | **可选**：分割气泡 |
| `tool_start` / `tool_end` | 工具调用开始/结束 | 已有，显示工具卡片 |
| `done` | 对话完成 | 已有，结束渲染 |
| `error` | API 异常 | **建议接入**：显示中文错误 |

---

### 4.2 提示词工程（`graph/prompt_builder.py`）

#### 新增内容

1. **日期/环境注入**
   ```
   【当前时间】今天是 2026年6月9日 星期二。
   【运行环境】当前系统为 Windows。使用 terminal 工具时请注意：...
   ```
   → 解决 LLM "不知道今天星期几" 的低级错误。

2. **Tool 使用指南**
   - 明确 `execute_skill` / `read_file` / `write_file` / `terminal` 的适用场景
   - **同一话题不重复探测 skill**（减少 token 浪费）
   - 查询失败优先修复重试，而非重新开始 workflow

3. **system.md 支持**
   - 若 `workspace/system.md` 存在，自动注入到 prompt 中
   - 用于存放业务规则（不污染 SOUL.md 的人格设定）

#### 保留的 V5 优势

- **Prefix Caching 优化**：静态前缀（SKILLS/SOUL/IDENTITY/USER/AGENTS）与动态内容（记忆/RAG）分离，最大化 DeepSeek 缓存命中率。

---

### 4.3 API 层（`api/chat.py`）

#### A. 口头写入检测与补偿

场景：LLM 回复中说"已保存到 MEMORY.md"，但实际没有发起 `write_file` tool call。

检测逻辑：
1. 扫描回复文本中的关键词（"已保存"、"已写入"、"已记住"等）
2. 检查本轮对话是否真正调用了 `write_file` 到 memory 路径
3. 若检测到"假写入"，用独立 LLM 调用提取关键信息并补偿写入

#### B. 错误分类（用户友好提示）

| 原始异常 | 用户看到的提示 |
|---------|--------------|
| 429 / ratelimit / quota | "模型 API 调用额度已用完（429），请稍后重试或联系管理员。" |
| 401 / unauthorized | "API 密钥无效或已过期（401），请联系管理员检查配置。" |
| 503 / 502 / 500 | "模型服务暂时不可用（5xx），请稍后重试。" |
| timeout | "请求超时，请稍后重试。" |
| 其他 | "生成回复时出错: {原始错误}" |

---

## 五、前端适配（可选，5 分钟）

### 5.1 必做：error 事件处理

在 V5 前端处理 SSE 的地方增加：

```typescript
if (event.event === 'error') {
  const data = JSON.parse(event.data);
  appendMessage({
    role: 'assistant',
    content: `⚠️ ${data.error}`,
    isError: true,
  });
}
```

### 5.2 选做：context_usage 显示

```typescript
if (event.event === 'context_usage') {
  const data = JSON.parse(event.data);
  // data.used_tokens, data.total_tokens, data.percentage
  // 可在输入框上方展示：上下文 31.2% (81700/262144)
}
```

### 5.3 可忽略：new_response

工具执行后 LLM 重新开始生成时触发。不改的话所有内容合并在一个气泡，和 V5 行为一致，无影响。

---

## 六、验证步骤

### 6.1 启动验证

```bash
cd backend
python app.py
```

预期输出：
```
🤖 Agent initialized with X tools (model: deepseek-chat)
✅ PuddingClaw backend ready
```

若启用了 MCP：
```
MCP client created, servers=['my_server']
```

### 6.2 功能验证

| 测试项 | 操作 | 预期结果 |
|--------|------|---------|
| 日期感知 | 问"今天星期几" | 正确回答当前日期和星期 |
| 上下文用量 | 连续对话 20+ 轮，观察后端日志 | 出现 `Context usage: xx.x%` |
| TailTrim 中段裁剪 | 连续长对话使 token 超过 50K | 日志出现 `[tail-trim] 中段裁剪 X 条`，且保留区起点为 HumanMessage |
| Context Rot | 继续对话到用量超过 85% | 日志出现 `truncated to X messages`，对话不崩溃 |
| 口头写入 | 诱导 LLM 说"已保存到 MEMORY.md"但不实际调用 | 后端出现 `[WARN] Fake memory write detected`，MEMORY.md 被补偿写入 |
| MCP 工具 | 启用 MCP Server 后提问相关业务 | LLM 调用 MCP 工具，工具卡片显示来源为 MCP Server |
| 错误处理 | 故意填错 API Key | 前端收到中文错误："API 密钥无效或已过期（401）..." |

---

## 七、常见问题

### Q1: MCP 配置启用了但工具没出现？

检查：
1. `config.json` 中 `mcp.enabled` 列表里的名称是否与 `servers.py` 中 `_REGISTRY` 的 key 一致
2. MCP Server URL 是否可访问
3. 环境变量（如 `MCP_API_KEY`）是否已设置

### Q2: 上下文用量显示为 0%？

`_estimate_tokens()` 是粗略估算，仅供参考。生产环境可接入 `tiktoken` 精确计算。

### Q3: 想关掉 MCP 怎么办？

`config.json`：
```json
{ "mcp": { "enabled": [] } }
```

或整个删除 `mcp` 字段，Agent 会完全退化为本地工具模式。

### Q4: 不改前端能用吗？

能。`context_usage` / `new_response` / `error` 等新事件会被前端忽略，`token` / `tool_start` / `tool_end` / `done` 等已有事件正常工作。

---

## 八、附录：一键覆盖脚本

```bash
#!/bin/bash
# 在 V5 项目根目录执行

BACKEND="backend"
MIGRATION="migration"

# 核心文件
cp "$MIGRATION/graph_agent_with_mcp.py"   "$BACKEND/graph/agent.py"
cp "$MIGRATION/graph_prompt_builder.py"   "$BACKEND/graph/prompt_builder.py"
cp "$MIGRATION/api_chat.py"               "$BACKEND/api/chat.py"

# MCP
mkdir -p "$BACKEND/mcp_clients"
cp "$MIGRATION/mcp_clients/__init__.py"   "$BACKEND/mcp_clients/__init__.py"
cp "$MIGRATION/mcp_clients/servers.py"    "$BACKEND/mcp_clients/servers.py"

# Cache middleware（新建）
cp "$MIGRATION/middleware_cache_patch.py" "$BACKEND/graph/middlewares/cache.py"

echo "文件覆盖完成。请手动执行以下操作："
echo "1. 在 config.py 末尾追加 get_max_history_messages() 和 get_context_window()"
echo "2. 在 config.json 中增加 llm.context_window 和 compression.max_history_messages"
echo "3. 按需配置 mcp_clients/servers.py 和 config.json 的 mcp.enabled"
echo "4. 手动替换 compression.py 中的 count_tokens_tiktoken 和 count_text_tokens"
echo "5. 在 graph/middlewares/__init__.py 中导出 build_cache_middlewares / TailTrimMiddleware / DeepSeekCacheBoundaryMiddleware（若不存在）"
```

---

*文档结束。如有问题，直接贴日志或报错信息即可。*
