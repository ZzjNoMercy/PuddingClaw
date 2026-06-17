# PuddingClaw 项目架构分析文档

## 📋 项目概述

PuddingClaw 是一个**轻量级 AI Agent 对话系统**，采用前后端分离架构，支持工具调用、技能扩展、长期记忆管理和 RAG 检索。

**核心特点**：
- 基于 LangChain + DeepSeek 的智能对话
- 可视化工具调用过程
- 文件即配置的设计理念
- 支持传统记忆注入和 RAG 向量检索
- 流式输出 + 多段响应机制

---

## 🏗️ 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    前端 (Next.js 14)                      │
│  ┌──────────┬─────────────────────┬──────────────────┐  │
│  │ Sidebar  │    ChatPanel        │ InspectorPanel   │  │
│  │ 会话列表  │    对话区域          │ 文件编辑器        │  │
│  └──────────┴─────────────────────┴──────────────────┘  │
│                         ↓ SSE                            │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│                  后端 (FastAPI)                          │
│  ┌─────────────────────────────────────────────────┐   │
│  │  API 层 (chat, sessions, files, config)        │   │
│  └─────────────────────────────────────────────────┘   │
│                         ↓                               │
│  ┌─────────────────────────────────────────────────┐   │
│  │  Agent 核心 (LangChain + DeepSeek)              │   │
│  │  - System Prompt Builder (6层配置文件)          │   │
│  │  - Session Manager (会话持久化)                 │   │
│  │  - Memory Indexer (RAG 向量检索)                │   │
│  └─────────────────────────────────────────────────┘   │
│                         ↓                               │
│  ┌─────────────────────────────────────────────────┐   │
│  │  工具层 (5个核心工具 + 技能扩展)                 │   │
│  │  terminal | python_repl | fetch_url | read_file│   │
│  │  search_knowledge | skills_scanner              │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

---

## 🔧 后端核心实现

### 1. Agent 引擎 (`graph/agent.py`)

**核心流程**：

```python
# 初始化（启动时执行一次）
agent_manager.initialize(base_dir)
  ├─ 加载 5 个核心工具
  ├─ 初始化 DeepSeek LLM（流式输出）
  └─ 初始化会话管理器

# 每次对话动态构建 Agent
_build_agent()
  ├─ 读取 6 个 Markdown 配置文件
  ├─ 根据 RAG 模式决定记忆注入方式
  └─ 使用 LangChain create_agent 创建实例

# 流式响应
astream(message, history)
  ├─ [可选] RAG 检索相关记忆片段
  ├─ 构建消息列表（历史 + 新消息 + RAG 上下文）
  ├─ 流式输出事件：
  │   ├─ retrieval: RAG 检索结果
  │   ├─ token: LLM 生成的文本片段
  │   ├─ tool_start: 工具调用开始
  │   ├─ tool_end: 工具执行结果
  │   ├─ new_response: 新回复段落开始
  │   └─ done: 完整响应结束
  └─ 返回完整内容
```

**关键设计**：
- **动态 System Prompt**：每次对话重新读取配置，支持热更新
- **多段响应**：工具调用后可生成多个独立回复气泡
- **RAG 模式切换**：可动态开启/关闭记忆检索

---

### 2. System Prompt 构建器 (`graph/prompt_builder.py`)

**6 层配置文件组合**：

```python
build_system_prompt(base_dir, rag_mode=False)
  ├─ SKILLS_SNAPSHOT.md      # 可用技能列表（自动生成）
  ├─ workspace/SOUL.md       # 核心人格设定
  ├─ workspace/IDENTITY.md   # 身份标识
  ├─ workspace/USER.md       # 用户画像
  ├─ workspace/AGENTS.md     # 操作规范
  └─ memory/MEMORY.md        # 长期记忆（RAG 模式下跳过）
```

**设计亮点**：
- **文件即配置**：所有配置都是可读可编辑的 Markdown
- **分层设计**：人格、身份、操作规范分离
- **RAG 兼容**：支持全量注入和向量检索两种模式

---

### 3. 会话管理器 (`graph/session_manager.py`)

**存储格式（JSON）**：

```json
{
  "title": "会话标题",
  "created_at": 1706000000,
  "updated_at": 1706000100,
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]}
  ],
  "compressed_context": "历史对话摘要"
}
```

**核心功能**：
- **会话持久化**：每个会话一个 JSON 文件
- **历史压缩**：将旧消息归档并生成摘要
- **多段消息合并**：`load_session_for_agent` 合并连续 assistant 消息
- **自动标题生成**：首次对话后使用 LLM 生成标题

---

### 4. RAG 记忆索引器 (`graph/memory_indexer.py`)

**技术栈**：
- LlamaIndex 向量索引
- OpenAI Embedding (`text-embedding-3-small`)
- MD5 哈希检测文件变化

**工作流程**：

```python
# 启动时构建索引
rebuild_index()
  ├─ 读取 memory/MEMORY.md
  ├─ SentenceSplitter 切分（chunk_size=256, overlap=32）
  ├─ 生成向量索引并持久化
  └─ 保存文件 MD5 哈希

# 对话时检索
retrieve(query, top_k=3)
  ├─ 检查文件是否变化（MD5 对比）
  ├─ 如有变化则重建索引
  ├─ 向量相似度检索 top_k 个片段
  └─ 返回 [{text, score, source}, ...]
```

---

### 5. 工具系统 (`tools/`)

**5 个核心工具**：

| 工具 | 功能 | 安全机制 |
|------|------|----------|
| `terminal` | 执行 Shell 命令 | 沙箱限制在 base_dir 内 |
| `python_repl` | 执行 Python 代码 | 使用 exec() 执行 |
| `fetch_url` | 获取网页内容 | 返回 Markdown 格式 |
| `read_file` | 读取本地文件 | 路径沙箱保护 |
| `search_knowledge` | 知识库检索 | 向量搜索 |

**技能扩展机制**：
- 技能定义在 `skills/*/SKILL.md`
- 启动时扫描生成 `SKILLS_SNAPSHOT.md`
- Agent 通过 `read_file` 读取技能定义后执行

---

### 6. API 路由 (`api/`)

**核心接口**：

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/api/chat` | 流式聊天接口（SSE） |
| GET | `/api/sessions` | 列出所有会话 |
| POST | `/api/sessions` | 创建新会话 |
| PUT | `/api/sessions/{id}` | 重命名会话 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| GET | `/api/sessions/{id}/history` | 获取会话历史 |
| POST | `/api/sessions/{id}/compress` | 压缩会话历史 |
| GET | `/api/files?path=...` | 读取文件 |
| POST | `/api/files` | 保存文件 |
| GET | `/api/config/rag-mode` | 获取 RAG 模式状态 |
| PUT | `/api/config/rag-mode` | 切换 RAG 模式 |

---

## 🎨 前端核心实现

### 1. 技术栈

- **Next.js 14** (App Router)
- **React 18** (Hooks + Context)
- **TypeScript**
- **Tailwind CSS** (毛玻璃风格)
- **Monaco Editor** (代码编辑器)
- **React Markdown** (消息渲染)

---

### 2. 状态管理 (`lib/store.tsx`)

**全局状态（Context API）**：

```typescript
AppState {
  // 聊天状态
  messages: ChatMessage[]
  isStreaming: boolean
  sendMessage: (text) => Promise

  // 会话管理
  sessionId: string
  sessions: SessionMeta[]
  createSession, renameSession, deleteSession

  // UI 状态
  sidebarOpen, inspectorOpen
  sidebarWidth, inspectorWidth
  rightTab: "memory" | "skills"

  // 功能状态
  ragMode: boolean
  isCompressing: boolean
  rawMessages: RawMessage[]
}
```

**核心逻辑**：

```typescript
sendMessage(text)
  ├─ 创建 user 消息气泡
  ├─ 创建空的 assistant 消息气泡
  ├─ 调用 streamChat API（SSE）
  ├─ 处理事件流：
  │   ├─ retrieval → 显示 RAG 检索卡片
  │   ├─ token → 逐字追加到当前气泡
  │   ├─ tool_start/tool_end → 更新工具调用状态
  │   ├─ new_response → 创建新的 assistant 气泡
  │   ├─ title → 更新会话标题
  │   └─ done → 结束流式输出
  └─ 刷新会话列表
```

---

### 3. SSE 客户端 (`lib/api.ts`)

**自定义 SSE 解析器**（原生 EventSource 只支持 GET）：

```typescript
async function* streamChat(message, sessionId) {
  const response = await fetch('/api/chat', {
    method: 'POST',
    body: JSON.stringify({message, session_id: sessionId})
  })

  const reader = response.body.getReader()
  let buffer = ""

  while (true) {
    const {done, value} = await reader.read()
    if (done) break

    buffer += decoder.decode(value)
    const lines = buffer.split('\n')
    buffer = lines.pop() || ""

    // 解析 SSE 格式
    for (const line of lines) {
      if (line.startsWith('event:')) currentEvent = line.slice(6)
      if (line.startsWith('data:')) {
        yield {event: currentEvent, data: JSON.parse(line.slice(5))}
      }
    }
  }
}
```

---

### 4. UI 组件架构

**三栏布局**：

```
┌─────────────────────────────────────────────────┐
│              Navbar (顶部导航栏)                  │
├──────────┬─────────────────────┬─────────────────┤
│          │                     │                 │
│ Sidebar  │    ChatPanel        │ InspectorPanel  │
│ (会话列表)│    (对话区域)        │ (文件编辑器)     │
│          │                     │                 │
│ 可调整宽度 │    自适应宽度        │  可调整宽度      │
└──────────┴─────────────────────┴─────────────────┘
```

**关键组件**：

1. **ChatPanel** - 消息列表 + 自动滚动 + 空状态提示
2. **ChatMessage** - 用户/AI 消息气泡 + Markdown 渲染
3. **ThoughtChain** - 工具调用过程展示（可折叠）
4. **RetrievalCard** - RAG 检索结果卡片
5. **InspectorPanel** - Monaco Editor 文件编辑
6. **Sidebar** - 会话列表管理

---

### 5. 样式设计

**毛玻璃风格（Glassmorphism）**：

```css
.glass-panel {
  background: rgba(255, 255, 255, 0.6);
  backdrop-filter: blur(12px);
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.app-bg {
  background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
}
```

**动画效果**：
- 消息淡入：`animate-fade-in`
- 打字指示器：三个点的波浪动画
- 面板过渡：`panel-transition`

---

## 🌟 核心特性实现

### 1. 多段响应机制

**问题**：Agent 在工具调用后可能生成多段回复，如何在 UI 上正确展示？

**解决方案**：

```python
# 后端：检测工具执行完成后的新文本
if tools_just_finished:
    yield {"type": "new_response"}
    tools_just_finished = False
```

```typescript
// 前端：收到 new_response 事件时创建新气泡
if (event.event === "new_response") {
  const newId = `assistant-${Date.now()}-${Math.random()}`
  currentAssistantIdRef.current = newId
  setMessages(prev => [...prev, {
    id: newId,
    role: "assistant",
    content: "",
    toolCalls: [],
    timestamp: Date.now()
  }])
}
```

---

### 2. RAG 记忆检索

**传统模式**：
- 将整个 `MEMORY.md` 注入到 System Prompt
- 优点：简单直接
- 缺点：Token 消耗大，超长记忆会超限

**RAG 模式**：
- 根据用户问题检索相关记忆片段
- 只注入 top_k 个最相关的片段
- 优点：Token 高效，支持超长记忆
- 缺点：需要 Embedding API

**切换逻辑**：

```python
# 后端
if rag_mode:
    indexer = get_memory_indexer(base_dir)
    results = indexer.retrieve(message, top_k=3)
    yield {"type": "retrieval", "query": message, "results": results}
    rag_context = format_results(results)
    augmented_history.append({"role": "assistant", "content": rag_context})
```

```typescript
// 前端
if (event.event === "retrieval") {
  setMessages(prev => {
    const updated = [...prev]
    const idx = updated.findIndex(m => m.id === targetId)
    updated[idx] = {...updated[idx], retrievals: event.data.results}
    return updated
  })
}
```

---

### 3. 会话历史压缩

**问题**：长对话会导致 Token 超限

**解决方案**：

```python
# 后端
compress_history(session_id, summary, num_to_remove)
  ├─ 将前 N 条消息归档到 sessions/archive/
  ├─ 使用 LLM 生成摘要
  ├─ 将摘要存储为 compressed_context
  └─ 从会话中移除已归档消息

# 加载会话时自动注入摘要
load_session_for_agent(session_id)
  ├─ 读取 compressed_context
  ├─ 将摘要作为第一条 assistant 消息注入
  └─ 返回合并后的历史
```

---

### 4. 技能扩展系统

**定义技能**：

```markdown
---
name: get_weather
description: 查询城市天气
---

# 使用说明

使用 `fetch_url` 工具调用天气 API...
```

**Agent 调用流程**：

```python
# 1. 启动时扫描技能
scan_skills(base_dir)
  ├─ 遍历 skills/*/SKILL.md
  ├─ 解析 YAML frontmatter
  └─ 生成 SKILLS_SNAPSHOT.md

# 2. Agent 看到技能列表
system_prompt = build_system_prompt(base_dir)
  # 包含 SKILLS_SNAPSHOT.md

# 3. Agent 决定使用技能
Agent: "我需要查询天气，先读取技能定义"
read_file("./backend/skills/get_weather/SKILL.md")

# 4. Agent 根据定义执行
Agent: "根据定义，我需要调用 fetch_url..."
fetch_url("https://api.weather.com/...")
```

---

## 🚀 启动流程

### macOS / Linux

```bash
./scripts/start-macos-linux.sh
  ├─ 检查 Python3 和 Node.js
  ├─ 创建虚拟环境 backend/.venv
  ├─ 安装后端依赖 pip install -r requirements.txt
  ├─ 检查 backend/.env（不存在则复制 .env.example）
  ├─ 安装前端依赖 npm install
  ├─ 启动后端 uvicorn app:app --host 0.0.0.0 --port 8002
  ├─ 等待 3 秒
  └─ 启动前端 npm run dev --host 0.0.0.0 --port 3000
```

### Windows

```bat
scripts\start-windows.bat
  ├─ 检查 Python 和 Node.js
  ├─ 创建虚拟环境 backend\.venv
  ├─ 安装后端依赖
  ├─ 检查 backend\.env
  ├─ 安装前端依赖
  ├─ 后台启动后端
  ├─ 等待 3 秒
  └─ 前台启动前端（Ctrl+C 停止所有服务）
```

---

## 📦 依赖管理

### 后端依赖 (`requirements.txt`)

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
sse-starlette>=2.0.0
python-dotenv>=1.0.0
pydantic>=2.0.0

# LangChain
langchain>=1.0.0
langchain-openai>=0.3.0
langchain-deepseek>=0.1.0
langchain-community>=0.3.0
langgraph>=1.0.0

# LlamaIndex
llama-index-core>=0.12.0
llama-index-embeddings-openai>=0.3.0

# 工具
tiktoken>=0.7.0
html2text>=2024.2.26
beautifulsoup4>=4.12.0
requests>=2.31.0
pyyaml>=6.0.0
```

### 前端依赖 (`package.json`)

```json
{
  "dependencies": {
    "next": "^14.2.0",
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "react-markdown": "^9.0.0",
    "remark-gfm": "^4.0.0",
    "@monaco-editor/react": "^4.6.0",
    "lucide-react": "^0.400.0",
    "clsx": "^2.1.0"
  }
}
```

---

## 🔐 安全机制

1. **路径沙箱**：所有文件操作限制在 `base_dir` 内
2. **命令白名单**：`terminal` 工具可配置允许的命令
3. **API Key 保护**：敏感信息存储在 `.env` 文件
4. **CORS 配置**：后端配置允许的前端域名

---

## 🎯 最佳实践

1. **配置管理**：使用 `.env` 文件管理敏感信息
2. **会话管理**：定期压缩长对话历史
3. **RAG 模式**：超长记忆场景建议开启 RAG
4. **技能开发**：遵循 SKILL.md 格式规范
5. **错误处理**：前端展示友好的错误提示

---

## 📝 总结

PuddingClaw 是一个设计精良的 AI Agent 系统，核心亮点包括：

✅ **文件即配置**：所有配置都是可读可编辑的 Markdown
✅ **流式输出**：实时展示 Agent 思考过程
✅ **技能扩展**：通过文件定义新能力，无需修改代码
✅ **记忆管理**：支持传统注入和 RAG 检索两种模式
✅ **多段响应**：工具调用后可生成多个独立回复
✅ **会话压缩**：支持长对话历史的智能压缩
✅ **一键启动**：跨平台启动脚本，开箱即用

这个项目非常适合作为学习 AI Agent 开发的参考案例！
