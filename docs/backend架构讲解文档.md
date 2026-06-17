# PuddingClaw 后端架构讲解文档

> 本文档按当前 backend 代码重新梳理，重点解释 PuddingClaw 后端如何把 FastAPI、LangChain `create_agent`、DeepSeek、工具系统、上下文工程 middleware、短期会话记忆、markdown/RAG/mem0 长期记忆串成一条可运行链路。
>
> 当前重点不是“有哪些文件”，而是“请求进来以后，数据如何流动，哪些模块在什么时候介入，以及为什么这样设计”。

---

## 目录

1. [一句话总览](#1-一句话总览)
2. [目录结构与模块职责](#2-目录结构与模块职责)
3. [启动流程](#3-启动流程)
4. [一次对话的完整链路](#4-一次对话的完整链路)
5. [AgentManager 核心架构](#5-agentmanager-核心架构)
6. [System Prompt 构建逻辑](#6-system-prompt-构建逻辑)
7. [Middleware 上下文工程栈](#7-middleware-上下文工程栈)
8. [记忆系统：短期、markdown/RAG、mem0、任务状态](#8-记忆系统短期markdownragmem0任务状态)
9. [工具系统与 Skills 路由](#9-工具系统与-skills-路由)
10. [API 层职责](#10-api-层职责)
11. [配置系统](#11-配置系统)
12. [关键设计取舍](#12-关键设计取舍)
13. [调试与验证入口](#13-调试与验证入口)

---

## 1. 一句话总览

PuddingClaw backend 是一个 **FastAPI SSE 流式接口 + LangChain `create_agent` Agent + DeepSeek LLM + 本地沙箱工具 + 多层上下文工程 middleware + 多模式长期记忆** 的后端。

核心链路可以压缩为：

```text
前端 /api/chat
  -> api/chat.py SSE event_generator
  -> SessionManager 读取短期历史
  -> AgentManager 检索长期记忆
  -> build_system_prompt 构建 cache-aware system prompt
  -> create_agent 挂载全量工具和 middleware 栈
  -> DeepSeek 流式输出 / 工具调用 / 子 agent
  -> 保存会话、触发记忆写入补偿、mem0 智能萃取、会话压缩
```

当前架构的核心变化是：**Agent 不再靠频繁重建或硬截断解决上下文问题，而是通过 middleware 栈分层管理上下文**。

从外到内：

```text
cache_boundary
  -> tail_trim
  -> tool_clear / summarization / compaction
  -> skills_router
  -> task_state
```

这套顺序的目标是：先保护 DeepSeek prefix cache，再做 cache-friendly 裁剪，最后才启用会破坏 cache 的摘要和压缩兜底。

---

## 2. 目录结构与模块职责

### 2.1 后端主目录

```text
backend/
├── app.py
├── config.py
├── config.json
├── api/
├── graph/
├── graph/middlewares/
├── tools/
├── utils/
├── workspace/
├── memory/
├── sessions/
├── storage/
├── skills/
└── tests/
```

### 2.2 核心职责表

| 层级 | 关键文件 | 职责 |
|---|---|---|
| 应用入口 | `app.py` | FastAPI 应用创建、CORS、生命周期启动、路由注册 |
| API 层 | `api/chat.py` | SSE 流式对话主入口；保存会话；调度长期记忆写入和自动压缩 |
| API 层 | `api/sessions.py` | 会话 CRUD、历史查询、标题生成、清空会话 |
| API 层 | `api/files.py` | 文件读写、skills 文件管理、版本和 diff 管理 |
| API 层 | `api/config_api.py` | RAG、LLM、Embedding、压缩、记忆后端配置接口 |
| API 层 | `api/compress.py` | 会话级 LLM 摘要压缩 |
| API 层 | `api/tokens.py` | token 计数接口 |
| API 层 | `api/skills_api.py` | skill 导入、加载、卸载、watch、文件树等管理 |
| Agent 核心 | `graph/agent.py` | LLM 初始化、工具加载、Agent 缓存、记忆检索、流式事件转换 |
| Prompt 构建 | `graph/prompt_builder.py` | 拼接 system prompt，拆分静态 cache 前缀和动态记忆区 |
| 中间件 | `graph/middlewares/cache.py` | DeepSeek cache boundary 观测、TailTrim cache-friendly 裁剪 |
| 中间件 | `graph/middlewares/compression.py` | ToolResultClear、Summarization、Compaction 高阈值兜底 |
| 中间件 | `graph/middlewares/skills_router.py` | 根据用户意图注入工具路由提示 |
| 中间件 | `graph/middlewares/task_state.py` | after_model 任务关键词检测，写入 `workspace/TODO.md` |
| 短期记忆 | `graph/session_manager.py` | JSON 会话持久化、压缩摘要归档、对话历史整理 |
| 长期记忆 | `graph/memory_indexer.py` | markdown `MEMORY.md` 的向量索引和 RAG 检索 |
| 长期记忆 | `graph/mem0_manager.py` | mem0 懒加载、add/search/update/delete、类型分组 |
| 长期记忆 | `graph/smart_extractor.py` | mem0 对话后智能萃取节流器 |
| 工具系统 | `tools/*.py` | 文件、终端、Python、网页、RAG、skills、mem0、deep_research 工具 |

---

## 3. 启动流程

### 3.1 FastAPI 生命周期

入口在 `app.py` 的 `lifespan()`。

启动顺序：

```text
1. scan_skills(BASE_DIR)
   -> 扫描 backend/skills/
   -> 生成或刷新 SKILLS_SNAPSHOT.md

2. agent_manager.initialize(BASE_DIR)
   -> 加载 config.json
   -> 初始化 ChatDeepSeek
   -> get_all_tools() 自动发现所有工具
   -> 校验 SkillsRouter preferred_tools 是否存在
   -> 初始化 SessionManager

3. 如果 rag_mode=true
   -> get_memory_indexer(BASE_DIR)
   -> rebuild_index()
   -> 读取 memory/MEMORY.md
   -> 切分、Embedding、持久化到 storage/memory_index/

4. 注册 /api 路由
```

### 3.2 为什么 Skills 扫描在 Agent 前面

`SKILLS_SNAPSHOT.md` 是 system prompt 静态前缀的一部分。它必须在 Agent 构建前存在，否则 Agent 使用的 prompt 会缺少当前可用 skills 信息。

### 3.3 为什么 RAG 索引是条件初始化

`memory_indexer.rebuild_index()` 会调用 Embedding API，成本和耗时都高于普通启动。因此只有 `config.json` 中 `rag_mode=true` 时才在启动阶段构建。

---

## 4. 一次对话的完整链路

### 4.1 SSE 流式请求链

主入口是 `POST /api/chat`，核心 generator 是 `api/chat.py:event_generator()`。

```text
前端发送 message/session_id/user_id
  -> 设置 current_user_id contextvar，供 memory_tools 使用
  -> session_manager.load_session_for_agent(session_id)
  -> agent_manager.astream(message, history, user_id)
  -> 将 Agent 事件转成 SSE:
       retrieval
       token
       tool_start
       tool_end
       new_response
       done
  -> 保存 user 消息和 assistant 分段
  -> 首轮对话生成 title
  -> 根据 memory_backend 执行长期记忆写入逻辑
  -> 消息数达到阈值后触发 auto_compress_session
```

### 4.2 Agent 事件如何映射到前端

`AgentManager.astream()` 使用 LangChain 的：

```python
agent.astream(
    {"messages": messages},
    stream_mode=["messages", "updates"],
)
```

事件映射：

| LangChain 事件 | 后端 SSE 事件 | 用途 |
|---|---|---|
| `messages` 中的 AI chunk | `token` | 前端逐 token 显示 |
| `updates.model.messages[].tool_calls` | `tool_start` | 前端 ThoughtChain 显示工具开始 |
| `updates.tools.messages[]` | `tool_end` | 前端 ThoughtChain 显示工具结果 |
| 工具结束后下一段模型回复 | `new_response` | 前端区分多段 assistant 输出 |
| Agent 完成 | `done` | 保存会话并通知前端完成 |

### 4.3 为什么 assistant 会被保存成多个 segment

工具调用会把一次回答切成多段：

```text
assistant 思考/说明
tool_start
tool_end
assistant 根据工具结果继续回答
```

`api/chat.py` 会用 `new_response` 分段保存 assistant 内容，保留每段对应的 tool_calls，便于前端回放 ThoughtChain。

### 4.4 异常和断连如何处理

`event_generator()` 的 finally 会在异常、浏览器断连、cancel scope 等情况下保存已经生成的部分对话，避免用户看到回复但服务端丢历史。

---

## 5. AgentManager 核心架构

### 5.1 AgentManager 的状态

`graph/agent.py:AgentManager` 维护：

| 字段 | 作用 |
|---|---|
| `_base_dir` | backend 根目录 |
| `_tools` | 启动时自动加载的全量工具 |
| `_llm` | `ChatDeepSeek` 实例 |
| `_config_sig` | LLM 配置签名，用于热更新 |
| `_cached_agent` | markdown 模式下复用的 LangChain agent |
| `_cached_agent_key` | Agent 缓存键 |

### 5.2 LLM 初始化与热更新

初始化时读取：

```text
config.json llm
  -> model
  -> api_key
  -> base_url
  -> temperature
```

优先级：

```text
config.json > 环境变量 > 默认值
```

默认模型是 `deepseek-chat`，通过 `langchain_deepseek.ChatDeepSeek` 创建，`streaming=True`。

每次构建 Agent 前会调用 `_refresh_llm_if_needed()`，如果 LLM 配置签名变化，就重建 `ChatDeepSeek`。

### 5.3 Agent 构建策略

`_build_agent()` 会做三件事：

```text
1. build_system_prompt(...)
2. 构造 middleware 栈
3. create_agent(model, tools, system_prompt, middleware)
```

当前关键策略：

```text
Agent 始终使用全量工具构建
  -> 不再按意图裁剪工具列表
  -> 由 SkillsRouterMiddleware 在 before_model 注入路由提示
  -> 这样可以复用 agent，保护 DeepSeek prefix cache 和 middleware 内部状态
```

### 5.4 Agent 缓存策略

缓存键包含：

```text
LLM 配置签名
prompt 文件 mtime 签名
rag_mode
memory_backend
tool_reminder
compression middleware 配置
write middleware 配置
skills router 配置
cache middleware 配置
```

缓存规则：

| 场景 | 是否缓存 Agent | 原因 |
|---|---|---|
| markdown 模式 + 非 RAG 动态检索 | 缓存 | system prompt 静态前缀稳定，适合 DeepSeek prefix cache |
| mem0 模式 | 不缓存 | `mem0_context` 每轮动态变化 |
| RAG 模式且本轮有 `rag_context` | 不缓存 | 检索结果每轮动态变化 |

注意：即使 mem0/RAG 不缓存 Agent，`prompt_builder.py` 仍把动态内容放在 system prompt 末尾，最大化 DeepSeek 可命中的静态前缀。

### 5.5 历史消息保护线

`AgentManager.MAX_HISTORY_MESSAGES = 50` 是最外层保险丝，不是日常裁剪主力。

日常上下文控制由 middleware 完成：

```text
TailTrim -> ToolResultClear -> Summarization -> Compaction
```

50 条限制只是防止 middleware 关闭或异常时历史无限膨胀。

---

## 6. System Prompt 构建逻辑

### 6.1 构建入口

入口是 `graph/prompt_builder.py:build_system_prompt()`。

参数：

```python
build_system_prompt(
    base_dir,
    rag_mode=False,
    memory_backend="markdown",
    mem0_context="",
    rag_context="",
    tool_reminder=False,
)
```

### 6.2 当前 Prompt 分层

当前 prompt 明确分成“静态前缀”和“动态区块”。

```text
静态前缀，DeepSeek prefix cache 锚点区：
1. SKILLS_SNAPSHOT.md
2. workspace/SOUL.md
3. workspace/IDENTITY.md
4. workspace/USER.md
5. workspace/AGENTS.md
6. MEMORY_WRITE_PROTOCOL_STATIC，markdown 模式才注入

动态区块，放在 prompt 末尾：
7. 长期记忆
   - markdown direct: MEMORY.md 全文 + 章节结构快照
   - markdown RAG: RAG guidance + 检索结果 + 章节结构快照
   - mem0: mem0 类型化检索结果
8. Tool Reminder，长对话时条件追加
```

### 6.3 为什么要拆静态和动态

DeepSeek prefix cache 依赖请求开头的稳定字节。旧设计把 `MEMORY.md` 结构快照混在记忆写入规则附近，导致每次 `MEMORY.md` 更新都可能破坏前缀。

当前设计：

```text
写入规则固定不变 -> 放进静态前缀
记忆内容和结构快照会变 -> 放到 Long-term Memory 动态区
```

这样即使长期记忆内容变化，DeepSeek 仍能命中前面的大段静态 system prompt。

### 6.4 mem0 模式下的 Prompt 变化

当 `memory_backend == "mem0"`：

```text
SOUL.md 中“文件即记忆”会被替换为“向量即记忆”
AGENTS.md 中原记忆协议会被替换为 mem0 记忆协议
不会注入 markdown 的 MEMORY_WRITE_PROTOCOL_STATIC
Long-term Memory 区块注入 mem0 检索结果或占位说明
```

### 6.5 Tool Reminder

当历史消息数量达到一定规模时，`AgentManager` 会传入：

```python
tool_reminder=len(history) >= 12
```

`prompt_builder.py` 会把工具提醒追加到 system prompt 末尾。它属于动态区，不应该被视为静态前缀漂移。

---

## 7. Middleware 上下文工程栈

### 7.1 装配顺序

装配位置在 `graph/agent.py:_build_agent()`：

```python
middleware=[*cache_mws, *compression_mws, *skills_mws, *write_mws]
```

实际顺序：

```text
1. cache_boundary
2. tail_trim
3. tool_clear
4. summarization
5. compaction
6. skills_router
7. task_state
```

### 7.2 为什么这个顺序重要

```text
越靠外，越 cache-friendly
越靠内，越偏业务引导或副作用
```

排序原则：

| 顺序 | 中间件 | 目的 |
|---|---|---|
| 1 | `DeepSeekCacheBoundaryMiddleware` | 观测真实 `ModelRequest.system_message` 静态前缀是否漂移 |
| 2 | `TailTrimMiddleware` | 在不破坏头部和 AI/Tool 配对的前提下删除中段消息 |
| 3 | `ToolResultClearMiddleware` | 压缩旧 ToolMessage 输出，降低工具结果污染 |
| 4 | `SummarizationMiddleware` | 16K token 后高阈值摘要兜底 |
| 5 | `CompactionMiddleware` | 32K token 后最后保险丝 |
| 6 | `SkillsRouterMiddleware` | 注入路由提示，引导模型优先使用匹配工具 |
| 7 | `TaskStateMiddleware` | after_model 副作用写 TODO |

### 7.3 CacheBoundary：判断 DeepSeek 前缀缓存是否受影响

`DeepSeekCacheBoundaryMiddleware` 使用 `wrap_model_call`，因为 LangChain `create_agent(system_prompt=...)` 的 system prompt 不在 `state["messages"]`，只在模型调用时出现在 `request.system_message`。

当前日志语义：

```text
INFO    [cache-boundary] 已锁定静态 system 前缀 N 字节（完整 system M 字节）
INFO    [cache-boundary] 静态前缀稳定 N 字节；动态区字节变化 (累计 K 次): full M -> P bytes
WARNING [cache-boundary] 静态前缀字节漂移 (累计 K 次): N -> P bytes, DeepSeek prefix cache 可能受影响
```

解释：

| 日志 | 含义 |
|---|---|
| 已锁定静态 system 前缀 | 第一次看到 system prompt，记录 `Long-term Memory` / `Tool Reminder` 之前的字节 |
| 动态区字节变化 | mem0/RAG/MEMORY snapshot/tool reminder 变化，正常，不代表 prefix cache 被破坏 |
| 静态前缀字节漂移 | 静态区被改了，DeepSeek prefix cache 可能受影响 |

### 7.4 TailTrim：cache-friendly 日常裁剪

`TailTrimMiddleware` 的默认配置：

```text
max_tokens=12000
head_keep=2
keep_recent=10
```

它只删除中段消息：

```text
保留开头 head_keep
删除中间可删消息
保留最近 keep_recent
```

关键生产保护：

```text
AIMessage(tool_calls=[...]) + ToolMessage(tool_call_id=...)
必须作为原子组处理
```

否则 DeepSeek/OpenAI-compatible API 会因为孤儿 ToolMessage 报 400。

### 7.5 Compression：高阈值兜底

`graph/middlewares/compression.py` 当前包含：

| 中间件 | 作用 | 当前定位 |
|---|---|---|
| `ToolResultClearMiddleware` | 旧工具结果摘要替换，保留 `[摘要]` 幂等前缀 | 工具输出压缩 |
| `SummarizationMiddleware` | LangChain 摘要中间件，使用中文 prompt | 16K 后兜底 |
| `CompactionMiddleware` | 把大量历史压成 `[历史对话摘要]` SystemMessage | 32K 后最后保险 |
| `MessageTrimMiddleware` | 类仍保留 | 默认生产装配已由 TailTrim 接管 |

### 7.6 SkillsRouter：软路由，不重建工具集

`SkillsRouterMiddleware` 不改变实际工具列表，而是在 `before_model` 中注入路由提示。

它的价值是：

```text
Agent 保持全量工具和稳定结构
路由信息以短提示方式进入当前轮
避免频繁重建 Agent
保留 DeepSeek prefix cache 和 middleware 状态
```

支持的路由类别：

```text
research
knowledge
skill
code_exec
memory
```

其中 research 优先级高于 knowledge，避免“深入分析”类任务误走简单检索。

### 7.7 TaskState：任务级结构化写入

`TaskStateMiddleware` 使用 `after_model`，检测用户消息中的任务关键词：

```text
帮我
待办
记得
提醒
任务
需要做
```

命中后追加到：

```text
workspace/TODO.md
```

它是纯副作用 middleware：写失败只记录 warning，不阻断主对话。

---

## 8. 记忆系统：短期、markdown/RAG、mem0、任务状态

### 8.1 短期记忆：SessionManager

文件：`graph/session_manager.py`

每个 session 对应一个 JSON：

```text
sessions/{session_id}.json
```

保存内容：

```json
{
  "title": "...",
  "created_at": 0,
  "updated_at": 0,
  "messages": [],
  "compressed_context": "..."
}
```

关键能力：

| 方法 | 作用 |
|---|---|
| `load_session()` | 返回原始 messages |
| `load_session_for_agent()` | 为 Agent 整理历史，注入压缩摘要，合并连续 assistant |
| `save_message()` | 追加 user/assistant 消息 |
| `compress_history()` | 归档旧消息，并把摘要写入 `compressed_context` |
| `clear_messages()` | 清空消息和压缩摘要 |

### 8.2 markdown 直读长期记忆

默认 `memory_backend="markdown"`。

长期记忆文件：

```text
memory/MEMORY.md
```

非 RAG 模式下，`prompt_builder.py` 会把 `MEMORY.md` 全文放入 `Long-term Memory` 动态区。

### 8.3 markdown + RAG 长期记忆

当 `rag_mode=true`：

```text
memory_indexer.retrieve(message)
  -> 检查 MEMORY.md hash 是否变化
  -> 必要时 rebuild_index()
  -> 加载 storage/memory_index/
  -> 检索 top_k 片段
  -> 作为 rag_context 注入 Long-term Memory 动态区
  -> 同时向前端发送 retrieval SSE event
```

索引持久化位置：

```text
storage/memory_index/
```

hash 文件：

```text
storage/memory_index/.memory_hash
```

### 8.4 mem0 长期记忆

当 `memory_backend="mem0"`：

```text
AgentManager._retrieve_memory_context()
  -> mem0_manager.get_typed_context(message, user_id)
  -> search(query, user_id, limit, score_threshold)
  -> 按 user / feedback / project / reference 分组
  -> stale_days 标注可能过时记忆
  -> 格式化后注入 system prompt 动态区
```

`Mem0Manager` 是懒加载：

```text
首次使用时 import mem0.Memory
  -> Memory.from_config(get_mem0_config())
  -> 成功后复用
  -> 初始化失败允许下次重试
  -> mem0 包未安装则标记不可用
```

mem0 的写入入口有两类：

| 入口 | 触发方式 | 说明 |
|---|---|---|
| 主 Agent 工具 | `save_user_memory` 等 4 个 save 工具 | 用户明确要求保存时使用 |
| SmartExtractor | 每 N 轮后台萃取 | 用户不显式保存时，自动提取重要信息 |

### 8.5 SmartExtractor：mem0 后台萃取

文件：`graph/smart_extractor.py`

默认节流：

```text
throttle_every=3
```

逻辑：

```text
每轮 done 后收集 user + assistant
  -> 追加到 session buffer
  -> 如果本轮主 Agent 已调用 save_*_memory，跳过，避免双写
  -> 未到 throttle_every，继续缓冲
  -> 到阈值，调用 mem0_manager.add(buffer_snapshot, user_id)
  -> 成功清空 buffer
  -> 失败恢复 buffer，下轮立即重试
```

生产异步路径使用 `run_in_executor`，避免 mem0 I/O 阻塞事件循环。

### 8.6 口头写入补偿

`api/chat.py` 有两套补偿：

| 模式 | 函数 | 目的 |
|---|---|---|
| markdown | `_detect_and_retry_memory_write()` | LLM 声称“已记住”但没有调用 `write_file(memory/MEMORY.md)` 时，触发补偿写入 |
| mem0 | `_detect_and_retry_mem0_write()` | LLM 声称“已保存长期记忆”但没有调用 `save_*_memory` 时，触发补偿写入 |

这是为了解决 Agent 只口头承诺、不实际调用工具的问题。

### 8.7 TaskState 与长期记忆的区别

`TaskStateMiddleware` 写的是任务清单，不是长期事实记忆。

```text
workspace/TODO.md
```

适合：

```text
提醒
待办
需要做
任务安排
```

不适合：

```text
用户画像
项目事实
行为偏好
资料引用
```

这些应该进入 markdown MEMORY 或 mem0。

---

## 9. 工具系统与 Skills 路由

### 9.1 工具自动发现

入口：`tools/__init__.py:get_all_tools(base_dir)`。

扫描规则：

```text
tools/*_tool.py
tools/*_tools.py
```

每个模块通过 `create_*` factory 返回一个或多个 LangChain `BaseTool`。

工具实例会缓存：

```text
_tool_instance_cache[(module_name, base_dir)] = tools
```

这样可以保留工具内部状态，例如 knowledge search 的 index 缓存。

### 9.2 当前工具清单

当前静态扫描到的工具名：

| 类别 | 工具 |
|---|---|
| core | `read_file`, `write_file`, `terminal`, `task_manager` |
| knowledge | `search_knowledge_base`, `fetch_url` |
| research | `deep_research` |
| skill | `execute_skill`, `create_skill_version` |
| code_exec | `python_repl` |
| memory | `save_user_memory`, `save_feedback_memory`, `save_project_memory`, `save_reference_memory`, `search_user_memories`, `search_feedback_memories`, `search_project_memories`, `search_reference_memories` |

合计 18 个工具。

### 9.3 工具类别和 SkillsRouter 的关系

`tools/__init__.py` 里定义了 `TOOL_CATEGORIES`，但当前主 Agent 构建时不按类别裁剪工具，而是始终挂全量工具。

工具类别主要用于：

```text
1. SkillsRouterMiddleware 生成 preferred_tools 提示
2. deep_research 子 agent 挑选受限工具集
3. 未来按需加载工具时保留接口
```

### 9.4 deep_research 子 agent

`tools/deep_research_tool.py` 实现轻量 isolate。

它会创建一个子 agent，允许的工具限制为：

```text
read_file
terminal
fetch_url
search_knowledge_base
```

明确排除：

```text
write_file
deep_research 自身
```

设计目的：

```text
重度阅读、分析、多文件综述交给子 agent
主 agent 只接收摘要 ToolMessage
避免主对话上下文被大量原始材料污染
```

### 9.5 Skills 文件体系

`backend/skills/{skill_name}/` 通常包含：

```text
SKILL.md
README.md
demo.py / test_skill.py / 其他资源
```

`tools/skills_scanner.py` 会扫描 skills 并更新 `SKILLS_SNAPSHOT.md`，该快照进入 system prompt 静态前缀。

相关 API：

```text
GET/POST /api/skills...
POST /api/skills/import
POST /api/skills/load
POST /api/skills/unload
GET /api/skills/watch
```

---

## 10. API 层职责

### 10.1 路由注册

`app.py` 注册：

```text
/api/chat
/api/files
/api/sessions
/api/tokens
/api/sessions/{id}/compress
/api/config...
/api/skills...
/api/evals...
```

### 10.2 chat API

主路由：

```text
POST /api/chat
```

职责：

```text
SSE 流式输出
多段 assistant 保存
工具调用事件转发
首轮标题生成
markdown/mem0 记忆写入补偿
SmartExtractor 后台萃取调度
会话自动压缩调度
```

### 10.3 files API

职责：

```text
读取和写入 workspace/memory/skills 等文件
当 memory/MEMORY.md 被更新时，必要时触发 memory_indexer.rebuild_index()
管理 skill 文件、版本、diff
```

### 10.4 sessions API

职责：

```text
列出会话
创建会话
重命名会话
删除会话
查询 messages/history
生成标题
清空消息
```

### 10.5 config API

职责：

```text
读取设置
更新 LLM / Embedding / RAG / memory_backend / compression / middleware 配置
测试连接
切换 rag_mode
```

### 10.6 compress API

职责：

```text
手动或自动压缩 session 历史
调用 LLM 生成摘要
通过 SessionManager.compress_history() 归档旧消息
```

---

## 11. 配置系统

### 11.1 默认配置

默认配置在 `config.py:_DEFAULT_CONFIG`。

关键项：

```text
rag_mode: false
memory_backend: markdown
llm: model/api_key/base_url/temperature
embedding: model/api_key/base_url
compression: trigger_count + middleware
cache: cache_boundary + tail_trim
mem0: llm/embedder/vector_store/version
smart_extractor: throttle_every/score_threshold/stale_days
skills_router: enabled/history_window
write_middleware: task_state
```

### 11.2 配置读取策略

`load_config()` 会：

```text
读取 config.json
与 _DEFAULT_CONFIG 深合并
读取失败时回退默认配置
```

### 11.3 LLM 和 Embedding 凭证复用

`get_mem0_config()` 会复用已有配置：

```text
llm.api_key -> mem0.llm.config.api_key
embedding.api_key -> mem0.embedder.config.api_key
embedding.base_url -> mem0 embedder openai_base_url
```

这样用户不用为 mem0 单独维护两套凭证。

### 11.4 Ch5 相关阈值

当前默认：

```text
cache.tail_trim.max_tokens = 12000
cache.tail_trim.head_keep = 2
cache.tail_trim.keep_recent = 10
tool_clear.keep_recent = 6
summarization.trigger_tokens = 16000
compaction.trigger_tokens = 32000
compression.trigger_count = 15
```

含义：

```text
TailTrim 是日常裁剪主力
Summarization 和 Compaction 是高阈值兜底
会话级 compress 是 session 文件层面的历史归档
```

---

## 12. 关键设计取舍

### 12.1 为什么 Agent 始终挂全量工具

旧思路可能是按用户意图动态裁剪工具列表。但这样会导致：

```text
工具 schema 经常变化
Agent 频繁重建
system/tool 前缀不稳定
DeepSeek prefix cache 命中变差
middleware 内部状态难以复用
```

当前设计选择：

```text
全量工具稳定挂载
SkillsRouter 只注入路由提示
```

这是为了让结构稳定优先。

### 12.2 为什么 cache middleware 在最外层

CacheBoundary 必须看到最终送入模型的 `request.system_message`。

TailTrim 必须在摘要和 compaction 前先尝试 cache-friendly 裁剪。

所以顺序是：

```text
cache_boundary -> tail_trim -> compression fallback
```

### 12.3 为什么动态记忆放在 system prompt 末尾

长期记忆、RAG 结果、mem0 检索结果、tool reminder 都可能每轮变化。

如果放在 prompt 前部，就会破坏 DeepSeek prefix cache。

当前设计把它们放在末尾，使请求开头的大段静态 prompt 保持字节级稳定。

### 12.4 为什么 mem0 模式不缓存 Agent

mem0_context 每轮由用户问题检索生成，属于动态内容。

如果缓存 Agent，可能复用上一轮的 mem0_context，造成记忆污染。因此 mem0 模式每轮重建 Agent。

### 12.5 为什么还保留 markdown MEMORY

markdown 记忆适合教学和可解释：

```text
用户能直接打开 MEMORY.md 看到记忆
Agent 可通过 read_file/write_file 修改
RAG 可基于 MEMORY.md 构建向量索引
```

mem0 更适合自动化长期记忆管理，但可观察性和调试成本更高。

### 12.6 为什么有口头写入补偿

LLM 有时会说“我已经记住了”，但没有实际调用工具。

补偿逻辑的目的不是替代工具调用，而是兜底：

```text
检测“声称已记住”
检查是否真的调用 write_file 或 save_*_memory
未调用则补偿写入
```

---

## 13. 调试与验证入口

### 13.1 关键日志

| 日志 | 说明 |
|---|---|
| `[cache-boundary] 已锁定静态 system 前缀...` | CacheBoundary 首次锁定静态前缀 |
| `[cache-boundary] 静态前缀稳定...动态区字节变化...` | 动态记忆区变化，正常 |
| `[cache-boundary] 静态前缀字节漂移...` | 静态前缀变化，DeepSeek cache 可能受影响 |
| `[tail-trim] 中段裁剪...` | TailTrim 删除中段消息 |
| `[ToolResultClear] compressed...` | 工具结果被摘要替换 |
| `[CompactionMiddleware] compacted...` | 触发全局 compaction |
| `[SkillsRouter] matched skills=...` | 路由提示命中 |
| `[TaskState] task appended...` | TODO 写入成功 |
| `[SmartExtractor] ...触发提取` | mem0 后台萃取触发 |
| `[WARN] Fake memory write detected...` | markdown 口头写入补偿 |
| `[WARN] Fake mem0 write detected...` | mem0 口头写入补偿 |

### 13.2 推荐验证命令

在 backend 目录下：

```bash
python -m py_compile app.py config.py graph/agent.py graph/prompt_builder.py graph/middlewares/cache.py
```

pytest 验证：

```bash
python -m pytest tests/test_context_optimizations.py -k DeepSeekCacheBoundary -q
```

DeepSeek 真实 cache 验证：

```bash
python -m tests.smoke_cache_hit
```

注意：`smoke_cache_hit.py` 会调用真实 DeepSeek API，需要有效 API key，并且 token 数、hit/miss 结果会受服务端缓存 TTL 和是否预热影响。

### 13.3 常见排查路径

#### 看到静态前缀漂移 warning

优先检查：

```text
prompt_builder.py 组件顺序是否变化
SKILLS_SNAPSHOT.md / SOUL.md / IDENTITY.md / USER.md / AGENTS.md 是否被修改
memory_backend 是否在请求间切换
是否有 middleware 改写了 system prompt 静态区
```

#### 看到动态区字节变化 INFO

通常正常，来源可能是：

```text
mem0 检索结果变化
RAG 检索结果变化
MEMORY.md 结构快照变化
tool_reminder=True/False 切换
```

这不等于 DeepSeek prefix cache 失效。

#### DeepSeek 400 tool_call_id 错误

优先检查：

```text
TailTrim 是否被关闭
是否有其他逻辑删除了 AIMessage 或 ToolMessage
AIMessage.tool_calls 和 ToolMessage.tool_call_id 是否成对保留
```

#### Agent 声称记住但没有写入

看日志：

```text
[WARN] Fake memory write detected...
[WARN] Fake mem0 write detected...
```

再检查：

```text
markdown 模式：memory/MEMORY.md
mem0 模式：search_*_memories 工具或 mem0 get_all/search
```

---

## 总结

当前 PuddingClaw backend 的核心不是单纯“调用一个 LLM”，而是围绕上下文工程做了完整分层：

```text
FastAPI 负责稳定流式接口
AgentManager 负责 Agent 生命周期、记忆检索、事件转换
PromptBuilder 负责静态前缀和动态记忆区分层
Cache middleware 负责保护 DeepSeek prefix cache
Compression middleware 负责高阈值兜底
SkillsRouter 负责软路由工具选择
TaskState 和 SmartExtractor 负责非阻塞写入副作用
SessionManager、MemoryIndexer、Mem0Manager 共同组成短期和长期记忆系统
```

一句话：**它是一个以 DeepSeek prefix cache 为约束、以 middleware 为上下文工程主轴、以多模式记忆系统为持久化能力的 Agent 后端。**
