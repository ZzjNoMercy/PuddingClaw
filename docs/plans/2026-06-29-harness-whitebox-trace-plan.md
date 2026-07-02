# Harness 白盒化 Trace 方案

> 状态：Draft for review  
> 范围：基于两节 Harness Engineering notebook，结合 PuddingClaw 当前 LangChain / LangGraph / DeepAgents 链路，整理“可观察、可干预、可控”的本地 Trace 看板方案。  
> 核心判断：Trace 的主语不是 LangGraph compiled graph，而是 **Harness 机制如何驱动一次 Agent 运行**。LangGraph Raw Graph 只用于检查编译后的底层图结构；用户默认应该看到 Agent Loop、Context、Progress、Tool、Memory、Permission、Budget、Verification 等 Harness 机制的动态。

---

## 1. Notebook 中的 Harness 机制清单

第一节课把 Agent 拆成：

```text
Agent = Model + Harness
```

其中 Harness 不是 prompt 本身，而是围绕模型建立的一组工程约束、运行时机制和垃圾回收能力。第一课的产品主线可以抽成 **三大类 + 八大机制**；第二课用 Mini Harness 继续补出 Permission、Hooks、Token Budget 三个扩展模块，让控制面更完整。

### 1.1 产品信息架构：八大机制 × 三支柱矩阵

Trace 看板和后续 Harness 配置页应共用这套信息架构：

三支柱不是树形父节点，而是矩阵维度。一个机制可以同时落在多个支柱里，并且有主次关系：

- `主`：这个机制的主要价值归属。
- `辅`：这个机制会明显服务这个支柱，但不是主目标。
- `-`：当前不作为该支柱呈现。

| 机制 | Context Engineering | Architectural Constraints | Garbage Collection | 归属判断 |
|---|---|---|---|---|
| Agent Loop | 辅 | 主 | - | 每轮都要整理 context；四相循环本身是架构约束 |
| Tool Use | - | 主 | - | 工具 schema / dispatch 本身约束 action space |
| Progress Tracking | 主 | 辅 | - | 跨 session 状态持久化属于 context 延展，也约束任务推进 |
| Context Management | 主 | - | 辅 | 本柱核心；compression / trim 也有 GC 属性 |
| Feature List | 主 | 辅 | 辅 | JSON/todo 清单是 context 规划层，也约束任务顺序，并能清理短期注意力 |
| Verification Loop | - | 主 | - | 事件驱动的错误显性化属于架构约束 |
| Subagents | 主 | 辅 | 辅 | 子上下文隔离是 context 手段；分治边界是架构约束；返回摘要有 GC 属性 |
| Generator-Evaluator | - | 主 | - | 角色分离是架构约束的典型实现 |
| Permission Gate extension | - | 主 | - | 危险操作拦截属于架构约束；第二课补充 |
| Token Budget extension | - | - | 主 | 成本 / token 上限属于 GC；第二课补充 |

也就是说：

- **展示上**：Trace 看板可以按机制聚合，也可以按三支柱过滤；同一个机制可以出现在多个支柱视图中，但标注主/辅。
- **配置上**：Harness 配置页建议默认按机制组织，每个机制内部展示它对三支柱的作用和配置项；也可以提供“三支柱视角”切换。
- **实现上**：Trace span metadata 不能只带一个 `pillar`，应该带 `pillars` 数组，里面记录 `pillar + role(primary/supporting)`。

### 1.2 Mini Harness 11 个模块

第二节课把机制落成 11 个模块：

| 机制 | Mini Harness 模块 | 解决的问题 | Trace 中应该看到什么 |
|---|---|---|---|
| Agent Loop | `core.py` | 防止一次性调用不可控，形成 Gather Context -> Verify -> Take Action -> Iterate 循环 | run、iteration、phase、stop reason、max step 命中情况 |
| Tool Use | `core.py` | 工具 schema、dispatch、结构化错误 | tool call、input、output preview、error、sources/citations |
| Progress Tracking | `progress.py` | 跨步骤、跨会话恢复 | progress write、todo diff、session state 更新 |
| Context Management | `context.py` | 上下文溢出、cache miss、context rot | token estimate、trim/compress、before/after、保留的 head/tail/summary |
| Feature List | `planner.py` | 任务状态从模型上下文外置 | todo replace/merge/query、当前 in_progress 项 |
| Verification Loop | `verifier.py` | 模型自检不可靠，需要真实验证 | pytest/build/browser/tool verification span、pass/fail |
| Subagents | `subagent.py` | 子任务隔离，避免污染父上下文 | parent task -> child run -> summarized return |
| Generator-Evaluator | `evaluator.py` | 生成者和评审者角色隔离 | generator span、evaluator span、score/verdict/revision |
| Permission Gate | `permission.py` | 高风险工具执行前闸门 | gate decision、allow/deny、rule、reason |
| Hooks | `hooks.py` | 各机制挂到统一事件总线 | session_start、pre_iteration、post_iteration、pre_tool_use、post_tool_use、session_stop |
| Token Budget | `budget.py` | 成本、token、循环失控 | usage delta、remaining budget、budget exceeded |

这 11 个模块不应该简单分到单一柱子，而应按上面的矩阵记录主/辅归属。为了实现方便，Trace metadata 使用多归属：

```json
{
  "mechanism": "feature_list",
  "pillars": [
    {"name": "context_engineering", "role": "primary"},
    {"name": "architectural_constraints", "role": "supporting"},
    {"name": "garbage_collection", "role": "supporting"}
  ]
}
```

当前 Trace 看板要做的不是复刻 notebook 的 Mini Harness，而是把这些机制映射到 PuddingClaw 的真实运行时。

---

## 2. 和 LangChain / LangGraph / LangSmith 的对齐

### 2.1 LangSmith 怎么做

LangSmith 的核心视角是 trace / run tree：一次顶层 agent run 下挂 LLM、tool、retriever、chain 等子 run，每个 run 有 input、output、metadata、error、耗时，并可按层级展开。它擅长回答：

- 本次请求调用了哪些模型、工具、检索器。
- 哪一步失败或变慢。
- 每个子调用的输入输出是什么。
- metadata / tags / project 如何归档和过滤。

对 PuddingClaw 的启发：

- 本地 Trace 也应该是 **span tree**，不是只画一张静态 LangGraph 图。
- `agent.run` 是 root，model/tool/memory/skill/middleware/harness 都是子 span。
- 每个 span 都应有 `type/name/status/duration/input/output/metadata`。
- LangSmith 可以作为可选云端补充，但默认本地 Trace 必须完整可用。

参考：

- [LangSmith Observability](https://docs.smith.langchain.com/observability)
- [LangSmith tracing concepts](https://docs.smith.langchain.com/observability/concepts)

### 2.2 LangChain middleware 怎么做

LangChain agent middleware 不是一个抽象概念，它有明确 hook 面：

```text
before_agent
before_model
wrap_model_call
after_model
wrap_tool_call
after_agent
```

其中 `before_*` / `after_*` 更像节点式生命周期 hook，`wrap_*` 更像包裹真实 model/tool 调用的拦截器。PuddingClaw 当前 Chat 模式已有 cache、compression、skills_router、task_state 等 middleware；Agent 模式使用 DeepAgents 默认 middleware 加项目 MemoryMiddleware。

对 Trace 的启发：

- UI 节点不要显示成挤在一起的 `MemoryMiddleware.before_agent` 长条，而应显示为：

```text
MemoryMiddleware
before_agent
```

- `wrap_model_call` / `wrap_tool_call` 不一定在 compiled graph 中可见，但应该在 Trace span 中可见。
- middleware 的观察粒度应从“节点名字”提升为：

```json
{
  "type": "middleware",
  "name": "MemoryMiddleware.before_agent",
  "metadata": {
    "langchain_hook": "before_agent",
    "middleware": "MemoryMiddleware",
    "harness_mechanism": "context_management"
  }
}
```

参考：

- [LangChain custom middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)
- [LangChain middleware overview](https://docs.langchain.com/oss/python/langchain/middleware)

### 2.3 LangGraph streaming 怎么做

LangGraph streaming 提供多种 stream mode：`messages` 用于 token / message chunk，`updates` 用于节点更新，`values` 用于完整 state 快照，`custom` 用于运行时自定义事件。PuddingClaw 当前 Agent 模式已使用：

```python
stream_mode=["messages", "updates", "custom", "values"]
```

对 Trace 的启发：

- `messages`：生成 `model` / reasoning span。
- `updates`：生成 tool start/end、graph node active、middleware 节点变化。
- `values`：生成 todos、memory、context、budget 等状态更新 span。
- `custom`：生成 context maintenance、tool result clear、skill progress 等自定义 Harness span。

参考：

- [LangGraph streaming](https://langchain-ai.github.io/langgraph/concepts/streaming/)

---

## 3. PuddingClaw 当前映射

| Harness 机制 | 当前已有位置 | 当前 Trace 状态 | 需要补强 |
|---|---|---|---|
| Agent Loop | `backend/graph/deepagents_manager.py::astream`；Chat 模式 `backend/graph/agent.py` | root `agent.run`、model/tool span 已有 | 增加 iteration/phase span，记录 stop reason |
| Tool Use | DeepAgents tools、`tool_result_adapter`、`execute_skill` | tool/skill/memory 分类已有 | 记录 schema name、structured error、citations 数量 |
| Progress Tracking | `session_manager.update_todos/update_trace` | `todos_updated` 和 `todo` span 已有 | 增加 progress/session write span，展示持久化是否成功 |
| Context Management | Chat middleware：cache/compression/tail trim/tool clear；Agent MemoryMiddleware | custom/middleware 粗粒度已有 | 补 token before/after、压缩原因、cache prefix 是否保持 |
| Feature List | DeepAgents `write_todos` / graph state `todos` | todo span 已有 | todo diff：added/updated/completed/deleted |
| Verification Loop | 目前主要靠测试命令和工具结果 | 无统一 `verification` type | 增加 verification span，容纳 pytest/build/browser 验证 |
| Subagents | DeepAgents `task` / skill delegation | 目前多半表现为普通 tool/skill | 增加 child run parent_id，展示隔离边界 |
| Generator-Evaluator | 尚未形成通用 evaluator runtime | 无 | 预留 `evaluator` span 类型 |
| Permission Gate | 文件系统作用域、未来外部文件授权流 | 暂无统一 gate span | 增加 `permission_gate` span，先观测后执行 |
| Hooks | LangGraph stream + LangChain middleware + custom events | 分散在代码里 | 抽象成本地 `HarnessTraceEmitter` 或扩展 `TraceCollector` |
| Token Budget | `backend/llm/token_usage_store.py`、模型 usage | 未进入 Trace | 增加 budget delta、remaining、exceeded span |

---

## 4. 推荐 Trace 事件 / 节点类型规范

这里的“类型规范”只是一层实现约定，不是给最终用户看的产品术语。它的作用是把运行时杂乱事件归类成用户能理解的 Harness 机制，例如：

- `MemoryMiddleware.before_agent` 归到 Context / Memory。
- `write_todos` 归到 Progress / Feature List。
- 工具执行前的 allow/deny 归到 Permission Gate。
- token/cost 变化归到 Token Budget。
- pytest/build/browser 检查归到 Verification。

也就是说，前端最终显示的是“哪个 Harness 机制在起作用”，而不是让用户理解实现层分类。

### 4.1 隔离维度

Trace 应该按 **每次用户请求** 隔离，而不是一个 session 只保留一份全局 Trace。

建议三个 ID 分工：

| 字段 | 含义 | 生命周期 | 用途 |
|---|---|---|---|
| `session_id` | 会话 ID | 一个聊天会话长期存在 | 归档消息、todos、最近 trace 列表 |
| `query_id` | 用户请求 ID | 每次用户发送消息创建一个 | 隔离本次请求的 trace、tool calls、model inputs |
| `trace_id` | Trace run ID | 每次运行创建一个 | span id 前缀、前端增量事件关联 |

关系：

```text
session_id
└── query_id / user_message_id
    └── trace_id
        ├── root span: agent.run
        ├── model_input span
        ├── llm span
        ├── tool / skill / memory span
        └── todo / subagent / budget span
```

当前实现更接近：

```json
{
  "session_id": "...",
  "trace": { "trace_id": "...", "spans": [] }
}
```

这只能表达“最近一次 run”，会覆盖同一个 session 中上一轮用户请求的 trace。目标结构应升级为：

```json
{
  "session_id": "...",
  "traces": {
    "query-abc": {
      "query_id": "query-abc",
      "trace_id": "trace-123",
      "user_message_id": "msg-user-1",
      "assistant_message_id": "msg-assistant-1",
      "started_at": 1710000000.0,
      "completed_at": 1710000004.2,
      "status": "completed",
      "spans": []
    }
  },
  "latest_trace_id": "trace-123",
  "latest_query_id": "query-abc"
}
```

前端展示策略：

- 正在 streaming 时，`trace_span_start/end` 必须携带 `session_id + query_id + trace_id`。
- Trace 看板默认显示当前正在运行或最近一次 `query_id`。
- 历史消息旁边可以提供“查看本轮 Trace”，切到对应 `query_id`。
- `todos` 仍是 session 级状态，因为它跨请求延续；但 todo 变化事件需要记录到本次 `query_id` 的 trace 中。

### 4.2 Todos 的数据归属

Todos 不应该完全归属于某一次 `query_id`。它更像 Harness 的长期进度板，应该跟随 `session_id` 延续：

```json
{
  "session_id": "session-abc",
  "todos": [
    {
      "id": "todo-1",
      "content": "整理 trace 方案",
      "status": "completed",
      "created_at": 1710000000.0,
      "updated_at": 1710000400.0,
      "last_changed_query_id": "query-2"
    }
  ]
}
```

但每一次 todo 变化都必须写入当前请求的 trace：

```json
{
  "query_id": "query-2",
  "type": "todo",
  "name": "todos.updated",
  "metadata": {
    "harness": {
      "mechanism": "feature_list",
      "pillars": [
        {"name": "context_engineering", "role": "primary"},
        {"name": "architectural_constraints", "role": "supporting"},
        {"name": "garbage_collection", "role": "supporting"}
      ]
    },
    "todo_diff": {
      "added": [],
      "updated": [
        {
          "id": "todo-1",
          "before": {"status": "in_progress"},
          "after": {"status": "completed"}
        }
      ],
      "removed": []
    }
  }
}
```

这样前端有两个视角：

- **当前进度视角**：从 session 级 `todos` 读取，展示最新任务板。
- **本轮行为视角**：从 query 级 trace 读取，展示“这一轮请求新增/更新/完成了哪些 todo”。

实现建议：

- `session_manager.update_todos(session_id, todos)` 继续维护最新 session todo state。
- 新增 todo diff 计算：`previous_todos -> current_todos`。
- `TraceCollector.add_todo_span(...)` 增加 `query_id` 和 `diff` metadata。
- todo item 增加 `last_changed_query_id`，方便从任务板反查是哪轮请求改动。
- 前端 ProgressCard 仍显示 session 最新 todos；TraceDashboard 显示当前 query 的 todo diff。

现有 `TraceSpan` 已具备基础字段：

```json
{
  "id": "trace-xxx-tool-yyy",
  "parent_id": "trace-xxx-llm-yyy",
  "type": "tool",
  "name": "read_file",
  "started_at": 1710000000.0,
  "completed_at": 1710000001.2,
  "status": "completed",
  "input": "...",
  "output": "...",
  "metadata": {}
}
```

建议扩展为 Harness 语义 metadata，不破坏现有前端协议：

```json
{
  "type": "middleware",
  "name": "MemoryMiddleware.before_agent",
  "metadata": {
    "session_id": "session-abc",
    "query_id": "query-abc",
    "trace_id": "trace-123",
    "harness": {
      "mechanism": "context_management",
      "pillars": [
        {"name": "context_engineering", "role": "primary"},
        {"name": "garbage_collection", "role": "supporting"}
      ],
      "phase": "gather_context"
    },
    "langchain": {
      "middleware": "MemoryMiddleware",
      "hook": "before_agent",
      "graph_node": "MemoryMiddleware.before_agent"
    },
    "metrics": {
      "tokens_before": 32840,
      "tokens_after": 21950,
      "messages_before": 92,
      "messages_after": 44
    }
  }
}
```

实现层新增 / 规范化节点类型：

| type | 含义 |
|---|---|
| `root` | 一次 agent run |
| `iteration` | Agent Loop 的一轮循环 |
| `phase` | gather_context / verify / take_action / iterate |
| `llm` | 模型调用 |
| `reasoning` | reasoning_content / thought |
| `tool` | 普通工具 |
| `skill` | skill 调用 |
| `memory` | memory 读写、MemoryMiddleware |
| `middleware` | LangChain / DeepAgents middleware hook |
| `todo` | Feature List / write_todos |
| `context` | trim、summarize、compact、cache boundary |
| `verification` | pytest、build、browser、检查器 |
| `subagent` | 子代理 run |
| `evaluator` | evaluator / reviewer |
| `permission` | gate allow/deny |
| `budget` | token/cost/time budget |
| `custom` | 未归一化事件 |

---

## 5. 前端看板形态

### 5.1 Raw Graph 只是编译结果检查器

当前图太长的根因是：LangGraph compiled graph 把循环边、middleware hook、model/tools 回环都作为图结构的一部分。它适合开发者检查“LangGraph 最终编译成了什么”，但不适合承担 Trace 主视图。

因此 Raw Graph 的定位应该是：

- 用来排查 DeepAgents / LangChain 编译结果。
- 用来确认某个 middleware hook 是否进入图。
- 用来解释底层 model/tools 回环为什么存在。
- 不作为用户理解 Harness 运行机制的默认入口。

推荐主视图改成三层：

1. **Harness Timeline**：按真实时间显示 Agent Loop、Context、Progress、Tool、Memory、Permission、Budget、Verification 等机制。
2. **Harness Mechanism Graph**：按机制关系显示“这轮运行为什么走到这里”，例如 Gather Context -> Model -> Tool Gate -> Tool -> Progress Update -> Verify -> Iterate。
3. **Run Tree**：类似 LangSmith，root 下展开 model / tool / memory / skill / verification 等实际调用。

Raw compiled graph 保留为调试 / 检查模式，默认折叠。

### 5.2 推荐布局

```text
Trace Dashboard
├── Header
│   ├── run status / duration / spans / tools / token usage
│   └── filters: Agent Loop | Middleware | Tool | Memory | Skill | Budget | Permission
├── Main
│   ├── left: Harness Mechanism Graph
│   ├── center: Harness Timeline lanes
│   │   ├── Agent Loop lane
│   │   ├── LangChain Middleware lane
│   │   ├── Model / Tool lane
│   │   ├── Memory / Skill lane
│   │   └── Budget / Permission / Verification lane
│   └── right: selected span detail
└── Bottom
    └── Run Tree / Raw JSON / Raw Graph tabs
```

### 5.3 节点标签规则

不要显示 `Memory:before` 这种短到看不懂的标签，也不要显示完整长类名挤爆画布。建议：

```text
MemoryMiddleware
before_agent
```

```text
SkillsRouterMiddleware
before_model
```

```text
ToolResultClearMiddleware
custom: summary
```

每个节点详情里展示完整字段：

- middleware class
- hook name
- harness mechanism
- input/output preview
- metrics diff
- source event：messages / updates / values / custom

---

## 6. 事件归一化方案

建议新增一个轻量归一化层，而不是把判断散落在 `deepagents_manager.py` 里：

```text
backend/graph/trace_collector.py
  TraceCollector
  add_harness_span(...)
  add_middleware_span(...)
  add_context_span(...)
  add_budget_span(...)
  add_permission_span(...)
```

或新增：

```text
backend/graph/harness_trace.py
  HarnessTraceEmitter
  normalize_langgraph_node(...)
  normalize_custom_event(...)
  classify_tool(...)
```

归一化规则示例：

| 原始事件 | 归一化 span |
|---|---|
| `metadata.langgraph_node = "model"` | `llm` / `phase=take_action` |
| `*.before_agent` | `middleware` + `mechanism=context_management` |
| `*.before_model` | `middleware` + `mechanism=context_management` 或 `tool_use` |
| `tools` update + tool message | `tool` / `skill` / `memory` |
| `values.todos` changed | `todo` + diff |
| `custom.type=context_maintenance` | `context` |
| `custom.type=tool_result_clear` | `context` |
| token usage callback | `budget` |
| filesystem / external read gate | `permission` |

---

## 7. 分阶段实施计划

### 7.1 推荐批次

不要一次性把 11 个 Harness 机制全部做完。第一批应该先围绕 `DeepAgentsManager` 里已经存在、用户也最容易感知的能力：

```text
model input
tools
todo / progress
memory
subagent
skill
```

这四类有两个好处：

- 后端已经有真实入口，不需要先发明新 runtime。
- 前端展示价值很直接：用户能看见 Agent 做计划、读写记忆、委托子任务、调用 skill。

#### Batch 1：DeepAgents 已有能力白盒化

目标：先让 Trace 看板清楚回答“这轮 Agent 给模型喂了什么、调用了哪些 tools、有没有用 todo/memory/subagent/skill”。

| 能力 | 当前入口 | Trace 呈现 | 优先级 |
|---|---|---|---|
| Model Input | `ModelClientChatModel._generate/_agenerate/_stream/_astream`；Chat 模式 `wrap_model_call` | 每次 LLM 交互的 messages 快照、system prompt 摘要、工具 schema 数量、token estimate | P0 |
| Tools | `updates.tools`、tool calls、`tool_result_adapter` | tool name、args preview、output preview、status/error、duration、source/citation count | P0 |
| Todo / Feature List | `values.todos`、`write_todos` | `todo` 节点 + todo diff + 当前 in_progress | P0 |
| Memory | `MemoryMiddleware`、`save_*_memory`、`search_*_memories`、memory 文件读写 | `memory` 节点 + read/write/search + source | P0 |
| Skill | `execute_skill`、`/skills/` backend、skill 相关工具 | `skill` 节点 + skill name + input/output + sources | P0 |
| Subagent | DeepAgents `task` / delegated run | `subagent` 节点 + child objective + summarized return | P1 |

第一批前端不追求完整的 Harness 宏观理论，而是先做四个清晰的机制卡片：

```text
本轮 Harness 机制
├── Model Input：调用 3 次，最近一次 18 条 messages，约 12.4k tokens
├── Tools：调用 4 次，失败 0 次，产生来源 3 个
├── Todo：创建 5 项，完成 2 项，当前 1 项进行中
├── Memory：读取项目记忆 1 次，写入 0 次
├── Skill：调用 design-html，耗时 8.2s
└── Subagent：委托 1 个子任务，返回摘要 320 字
```

配套 Run Tree 中再展开具体调用。

##### Model Input 监控边界

“进入 model 的内容”必须在最终调用 LLM 前采集，否则看不到 middleware 改写后的真实上下文。建议两层采集：

1. **LangChain middleware 层：`wrap_model_call`**
   - 适合 Chat 模式和自定义 middleware。
   - 能读到 `ModelRequest.system_message` 和最终 `request.messages`。
   - 用来解释“哪个 middleware 改了什么”。

2. **模型适配器兜底层：`ModelClientChatModel._generate/_agenerate/_stream/_astream`**
   - 适合 DeepAgents Manager，因为所有 DeepAgents 主模型、子代理模型最终都会走这里。
   - 用来保证每一次真实 LLM 调用都有 input snapshot。
   - 这里不负责解释 middleware 来源，只负责保证“不漏记”。

Trace 中建议新增 `model_input` 节点，挂在对应 `llm` 节点之前：

```json
{
  "type": "model_input",
  "name": "model.input",
  "metadata": {
    "message_count": 18,
    "estimated_tokens": 12400,
    "system_prompt_chars": 3920,
    "tool_schema_count": 12,
    "capture_boundary": "ModelClientChatModel._astream"
  },
  "output": {
    "messages_preview": [
      {"role": "system", "chars": 3920, "preview": "..."},
      {"role": "human", "chars": 120, "preview": "..."},
      {"role": "tool", "name": "read_file", "chars": 1800, "preview": "..."}
    ]
  }
}
```

默认 UI 展示 preview 和统计信息；完整内容需要显式展开，且应做长度截断和敏感字段脱敏，避免 Trace 自己变成新的上下文/隐私风险。

#### Batch 2：Agent Loop 和 Context 白盒化

目标：让用户看到 Agent 为什么继续循环、为什么压缩/裁剪上下文。

| 能力 | Trace 呈现 |
|---|---|
| Agent Loop | iteration 1/2/3、phase、stop reason |
| Context Management | context tokens before/after、summary/trim/compaction 原因 |
| Middleware Hooks | 哪个 middleware 的哪个 hook 生效 |

#### Batch 3：Permission、Budget、Verification

目标：把 Harness 的“可控性”补齐。

| 能力 | Trace 呈现 |
|---|---|
| Permission Gate | allow/deny、规则、原因、用户授权状态 |
| Token Budget | token/cost delta、remaining、是否触发硬停 |
| Verification Loop | pytest/build/browser/自定义检查的 pass/fail |

#### Batch 4：Chat 模式统一

目标：让 Chat 模式和 Agent 模式使用同一套 Harness Trace 展示语言。

| 能力 | Trace 呈现 |
|---|---|
| cache/compression middleware | `context` / `middleware` |
| skills_router | `skill_routing` |
| task_state | `todo` / `progress` |

### Phase A：Trace 事件类型规范固化

- [x] 在 `frontend/src/lib/api.ts` 扩展 `TraceSpan.type` 联合类型：`model_input`、`subagent`。
- [x] 在 `TraceCollector` 增加 `metadata.harness` 基础约定，先覆盖 `model_input` / `todo` / tool semantic metadata。
- [x] `TraceCollector` 初始化时接收 `query_id`，所有 SSE trace 事件携带 `session_id/query_id/trace_id`。
- [x] `session_manager` 从单个 `trace` 字段升级为 `traces[query_id]`，保留 `trace/latest_query_id/latest_trace_id` 兼容当前 UI。
- [x] 增加测试：span 可序列化、query trace 双写、model_input、todo diff。

### Phase B：后端 Harness 事件补齐

- [ ] DeepAgents Agent 模式：增加 `iteration` / `phase` span。
- [ ] Tool Use：工具 span metadata 增加 `schema/tool_call_id/source_count/is_structured_error`。
- [x] Todo：从全量 todo span 改为同时保存 `diff`，todo item 写入 `last_changed_query_id`。
- [ ] Context：把 `context_maintenance`、`tool_result_clear`、`compaction` 归一成 `context` span。
- [ ] Budget：把 token/cost usage 写入 `budget` span。
- [ ] Permission：先记录观测型 `permission` span，后续再接入真正 allow/deny gate。

### Phase C：前端 Trace 看板升级

- [ ] 增加 Harness Timeline lanes。
- [ ] Harness Mechanism Graph 默认展示，Raw Graph 放到调试 tab。
- [x] Trace 看板增加 Phase 1 Harness 摘要：Model Input、Tools、Todo、Memory、Skill、Subagent。
- [ ] 右侧详情面板展示 Harness metadata 和 metrics diff。
- [ ] 增加 type / mechanism / pillar role 过滤。
- [ ] 支持下载原始 Trace JSON。

### Phase D：LangChain Chat 模式纳入同一 Trace

- [ ] Chat 模式 `backend/graph/agent.py` 也接 `TraceCollector`。
- [ ] cache/compression/skills_router/task_state middleware 统一发 `middleware/context/todo` span。
- [ ] 前端同一 TraceDashboard 兼容 Agent 模式和 Chat 模式。

### Phase E：测试和评测

- [ ] `tests/test_trace_collector.py` 增加 Harness metadata 和 `query_id` fixture。
- [ ] `tests/test_deepagents_manager_graph.py` 增加 fake stream：model -> todo -> tool -> context custom -> final。
- [ ] 增加一个小型 agent 评测集 fixture，验证 trace 必须包含指定机制：
  - 写 todo 的任务必须出现 `todo` span。
  - 检索/记忆任务必须出现 `memory` span。
  - skill 任务必须出现 `skill` span。
  - 长上下文任务必须出现 `context` span。
  - 高风险文件任务必须出现 `permission` span。

---

## 8. 和现有实现的关系

当前已经做对的部分：

- Trace 已经从抽屉移到独立看板，方向正确。
- `TraceCollector` 本地持久化路线正确，不依赖 LangSmith。
- `graph_node_active` 能显示动态执行。
- tool / skill / memory / todo 已有基础分类。
- LangSmith 作为可选 callback 保留，符合“本地默认可用，云端可补充”的路线。

当前不够清晰的部分：

- `Memory:before` 这类标签缺少“哪个 middleware、哪个 hook、属于哪个 Harness 机制”的解释。
- 执行图仍容易被 compiled graph 的循环边影响。
- Context / Budget / Permission / Verification 还没有成为一等 span。
- Agent Loop 四相没有显式显示，用户只能从 model/tool 反推运行阶段。
- 当前 `session.json` 只保存最近一次 `trace`，还没有按 `query_id` 保存每次用户请求的 trace。
- Chat 模式和 Agent 模式的 white-box trace 尚未统一。

---

## 9. 审核问题

需要你确认的产品取舍：

1. Trace 看板默认主视图是否采用 **Harness Timeline + Harness Mechanism Graph**，Raw LangGraph 只作为编译结果检查 tab？
2. Permission Gate 第一阶段是否只做观测，不实际阻断工具？
3. Budget 是否只显示 token/cost，还是要加入硬停止策略？
4. Chat 模式是否也纳入同一套 Harness Trace，还是先只做 Agent 模式？
5. `query_id` 是否直接复用 user message id，还是在 SSE 开始时单独生成 `query-*`？

---

## 10. 建议结论

可以按 Harness Engineering notebook 的机制来做 Trace，但不要把 notebook 的 Mini Harness 逐文件照搬。PuddingClaw 更合适的路线是：

```text
Notebook Harness 机制
  -> 映射成 Trace 事件 / 节点类型规范
  -> 绑定 LangChain middleware hook / LangGraph stream mode
  -> 本地 TraceCollector 持久化
  -> 前端用语义图、时间线、span tree 三种视角展示
```

这样 Trace 会同时回答四个问题：

1. LangGraph 现在跑到哪一个节点？
2. LangChain / DeepAgents 哪个 middleware hook 正在影响上下文或工具？
3. Harness 哪个机制在起作用：context、progress、permission、budget、verification？
4. 真实 model/tool/skill/memory 调用了什么、结果是什么、是否失败？
