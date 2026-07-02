# 前端 Trace + write_todos 方案

> 目标：让 LangGraph / DeepAgents 的运行流程白盒化，同时实现 write_todos 工具。
> 决策：**不引入 PostgreSQL，复用本地 session 文件做持久化**，保持系统简单、可调试、可离线运行。

---

## 1. 背景与约束

- 当前 `deepagents` 版本（`.venv`）没有 `TodoListMiddleware`，无法直接复用教学示例里的 todo 中间件。
- 已有 `session_manager` 把会话历史保存为本地 JSON，机制简单、白盒、无需额外服务。
- LangGraph checkpoint 默认内存版（`MemorySaver`）后端重启即丢失，不符合本地持久化诉求。
- 为白盒化，trace 数据也应写入本地文件，便于事后复盘。

---

## 2. 持久化决策

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| PostgreSQL | 容量大、并发好 | 引入外部依赖、配置复杂、违背白盒/本地优先 | **不使用** |
| LangGraph `MemorySaver` | 原生支持 | 内存态，重启丢失 | 仅作备选 |
| LangGraph `SqliteSaver`（本地 sqlite） | 持久化、原生 | 和现有 session JSON 双轨，调试时需看两个地方 | 可选 |
| **复用本地 session JSON** | 和现有架构一致、白盒、可手动查看 | 需要手动管理 state 合并 | **主方案** |

**最终决策**：
- `todos`、`trace`、`context peak` 等运行时状态全部保存到现有 `session_manager` 会话 JSON。
- 不新增数据库服务。
- 若后续需要严格跨轮 graph state，可再引入本地 `SqliteSaver` 作为第二层，但优先用 session JSON 满足当前需求。

---

## 3. write_todos 工具

### 3.1 工具实现

文件：`backend/tools/write_todos_tool.py`

```python
@tool
def write_todos(todos: list[dict[str, Any]]) -> str:
    """把任务拆解为待办清单。

    Args:
        todos: 每项包含 content（必填）和可选 status（pending/in_progress/completed）。
    """
    normalized = []
    for item in todos:
        normalized.append({
            "content": str(item.get("content", "")).strip(),
            "status": item.get("status") or "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    return json.dumps({
        "puddingclaw_tool_result": 1,
        "answer_context": f"已创建 {len(normalized)} 项待办",
        "todos": normalized,
    }, ensure_ascii=False)
```

### 3.2 注册

在 `backend/tools/__init__.py`（或现有工具注册入口）加入 `write_todos`。

### 3.3 提示词

在 system prompt 中增加：

> 当你需要把复杂任务拆成可执行的步骤时，请调用 `write_todos` 工具创建待办清单，而不是只在回答里列出步骤。

### 3.4 前端消费

`SourcesPanel` 的 `ProgressCard` 已从 `message.toolCalls` 提取 todos，无需改动。

---

## 4. 前端 Trace（LangGraph 运行流程可视化）

### 4.1 设计目标

- 显示：LangGraph 节点切换 → 模型调用 → 工具调用 → 工具返回 → state 更新 → 最终回答。
- 包含中间件痕迹：在 trace 中标注 `MemoryMiddleware.*`、`SkillsMiddleware.*`、`PatchToolCallsMiddleware.*`、`TodoListMiddleware.*` 等包装层。
- 区分运行类型：`graph`、`middleware`、`skill`、`memory`、`tool`、`llm`、`reasoning`、`todo`。
- 白盒：trace 数据写入本地 session JSON，前端实时消费 `trace_span_*` / `graph_node_active` SSE。

### 4.1.1 当前实现梳理（2026-06-29）

后端当前链路：

1. `backend/graph/deepagents_manager.py` 构建 DeepAgents agent 后，通过 `agent.get_graph()` 提取 LangGraph `nodes` / `edges`，以 `graph_structure` SSE 发给前端。
2. 每次 `messages` 流事件携带 `metadata.langgraph_node` 时，后端发出 `graph_node_active`，并通过 `TraceCollector.add_graph_node_span()` 写入图节点 trace。
3. 模型输出文本时，`TraceCollector.start_llm_span()` 创建 `llm` span；节点离开 model 或工具开始/结束时关闭该 span。
4. 工具调用开始时，`TraceCollector.start_tool_span()` 创建 span，并根据工具名/输入细分：
   - `execute_skill` 或 skill 相关工具：`skill`
   - `save_*_memory`、`search_*_memories`、读写 `memory` 路径：`memory`
   - 其他工具：`tool`
5. `values.todos` 变化时写入 session JSON，并创建 `todo` span。
6. 结束时 `TraceCollector.finish()` 生成扁平 trace，`session_manager.update_trace()` 持久化，并以 `trace_updated` 发给前端。

前端当前链路：

1. `frontend/src/lib/store.tsx` 接收 `graph_structure`、`graph_node_active`、`trace_span_start`、`trace_span_end`、`trace_updated`。
2. `applyTraceSpanEvent()` 在内存中重建 span 树，右侧面板即时刷新。
3. `frontend/src/components/agent/TraceViewer.tsx` 同时渲染：
   - LangGraph 执行图：按 DAG 分层布局、可滚动、不让长 middleware 名称挤压节点。
   - Trace span 列表：按父子关系显示 model / skill / middleware / tool / memory / todo。
4. 图节点状态由 trace 中的 `metadata.graph_node` 和当前 `activeGraphNode` 共同推导：
   - running：当前活跃节点
   - completed：本轮已进入过的节点
   - error：相关 span 异常

### 4.1.2 前端样式修正（2026-06-29）

- 执行图容器改为固定最大高度 + 横纵向滚动，避免小侧栏里强行压缩。
- 节点宽度根据压缩后的标签动态计算，保留 SVG `<title>` 展示完整节点名。
- `Middleware`、`Memory`、`Skills`、`PatchToolCalls` 等长名称会压缩成短标签，例如 `Skills:before`、`Memory:before`。
- 节点按类型使用不同底色，当前节点和关联边高亮，已运行节点显示完成态。
- Trace 列表增加类型 pill，能直接看到 `skill` / `memory` / `middleware` / `tool` 的区别。

### 4.1.3 Trace 看板形态调整（2026-06-29）

右侧抽屉宽度不足以承载 LangGraph 执行图，尤其是包含 middleware 回边、tools 循环、memory/skill 节点时，图会被迫横纵滚动且阅读成本高。因此 Trace 不再作为抽屉里的第三个 tab 承载主体内容：

- 右侧抽屉保留 `进度` / `来源` 两个轻量 tab。
- 抽屉中提供 `打开 Trace 看板` 入口，只显示 span 数量摘要。
- 主工作区新增 `TraceDashboard`，在聊天区和 Trace 看板之间切换。
- Trace 看板顶部展示运行状态、节点/边数量、span 数量。
- Trace 看板使用更大的图画布（默认 960px 起步、最大高度 760px），用于观察 LangGraph 动态运行、skill 调用、middleware、tools、memory 写入。

### 4.1.4 语义执行图而非原始 compiled graph（2026-06-29）

LangChain Agent middleware 的 node-style hooks 会编译进 LangGraph 节点，例如 `before_agent`、`before_model`、`after_model`、`after_agent`。如果前端按原始图最长路径自动布局，`model -> tools -> model` 这类循环边会把层级无限拉长，导致 `MemoryMiddleware.before_agent` 到 `model` 出现很长的竖线。

前端执行图改为语义层级：

1. `Agent start`
2. `*.before_agent`
3. `*.before_model`
4. `model`
5. `*.after_model`
6. `tools`
7. `*.after_agent`
8. `Agent end`

节点标签显示为两行：

- 第一行：完整中间件名，例如 `MemoryMiddleware`、`SkillsMiddleware`、`PatchToolCallsMiddleware`、`TodoListMiddleware`
- 第二行：hook 名，例如 `before_agent`、`before_model`、`after_model`

这个视角更接近 LangSmith 的 trace/run tree：关注 Agent、Model、Tool、Retriever/Memory、Middleware Hook 等语义运行单元，而不是把所有 compiled graph implementation detail 等权展示。

### 4.2 后端事件

继续复用 `stream_mode=["messages", "updates", "custom", "values"]`，重点增强 `values` 分支：

| 事件 | 来源 | 内容 |
|---|---|---|
| `trace_start` | agent stream 开始 | run_id, timestamp |
| `trace_message` | `values["messages"]` 新增消息 | role, type(tool_call/tool_return/ai), name, preview |
| `trace_state_update` | `values` 字段变化 | field (todos/memory/...), delta |
| `trace_middleware` | `updates` 中的非 tools 节点 | middleware name, status(start/end), duration_ms |
| `trace_end` | stream 结束 | total_steps, duration_ms |

实现位置：`backend/graph/deepagents_manager.py` 的 `astream` 方法。

### 4.3 trace 持久化

- 每轮对话结束时，把 trace 事件列表保存到 session JSON：`session["traces"][message_id] = [...]`。
- `session_manager.py` 增加 `save_trace(session_id, message_id, trace_events)`。

### 4.4 前端 UI

新增 `frontend/src/components/trace/TracePanel.tsx`：

- 默认折叠，点击展开。
- 树形或时间轴展示。
- 节点类型：
  - 🧠 model
  - 🔧 tool_start / tool_end
  - 📦 state_update (todos 等)
  - 🛡️ middleware
- 显示耗时、输入/输出预览（可点击展开）。

集成位置：右侧边栏「来源」卡片下方，或每个 assistant message 的 ThoughtChain 下方。

---

## 5. 分阶段实施

### Phase 1：write_todos 单轮可用
- [ ] 实现 `write_todos_tool.py`
- [ ] 注册工具
- [ ] system prompt 增加调用提示
- [ ] 手动测试：发送拆解任务，确认 ProgressCard 显示 todo

### Phase 2：Trace 事件流
- [x] 后端 `astream` 增强 `values` / `messages` / `updates` 分支，emit `trace_*` 和 `graph_node_active` 事件
- [x] 前端在 `TraceViewer` 中渲染执行图和 trace 树
- [x] 将 trace 接入右侧边栏
- [ ] 手动测试：发送多工具调用请求，确认 trace 树正确

### Phase 3：本地持久化
- [x] `session_manager` 增加 trace 更新/读取能力
- [x] `write_todos` / `values.todos` 产生的 todos 保存到 session JSON
- [ ] 新会话加载时，把历史 todos 注入 system prompt 或作为上下文
- [ ] 验证跨轮 todo 可见

### Phase 4： polish
- [ ] trace 可下载原始 JSON
- [ ] trace 支持过滤（只看 tool / skill / memory / middleware）
- [x] Trace 从右侧抽屉移到独立看板，抽屉只保留入口摘要
- [ ] 单元测试

---

## 6. 关于 Checkpoint

- **当前不需要**：Phase 1-2 完全不需要 checkpoint。
- **Phase 3 跨轮 state**：优先用 session JSON 手动持久化，而不是 LangGraph checkpoint。
- **何时引入 checkpoint**：如果以后需要让 LangGraph 原生节点（如 `create_react_agent` 的 memory）自动持久化，再考虑本地 `SqliteSaver`。

---

## 7. 风险

- `deepagents` 版本较旧，部分 trace 字段可能和示例不一致，需要边实现边调试。
- 不引入 checkpoint 意味着需要手动维护 state 合并逻辑，跨轮复杂任务可能不如原生 checkpoint 稳定。
- trace 事件量可能很大，需要截断/采样。
