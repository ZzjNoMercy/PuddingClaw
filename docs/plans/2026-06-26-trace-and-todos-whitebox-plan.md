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

- 显示：模型调用 → 工具调用 → 工具返回 → state 更新 → 最终回答。
- 包含中间件痕迹：在 trace 中标注 `memory`、`summarization`、`permissions` 等包装层的进入/退出。
- 白盒：trace 数据写入本地，可下载/查看原始 JSON。

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
- [ ] 后端 `astream` 增强 `values` 分支，emit `trace_*` 事件
- [ ] 前端新增 `TracePanel` 组件
- [ ] 将 trace 接入 assistant message 时间轴或右侧边栏
- [ ] 手动测试：发送多工具调用请求，确认 trace 树正确

### Phase 3：本地持久化
- [ ] `session_manager` 增加 `save_trace` / `load_trace`
- [ ] `write_todos` 产生的 todos 保存到 session JSON
- [ ] 新会话加载时，把历史 todos 注入 system prompt 或作为上下文
- [ ] 验证跨轮 todo 可见

### Phase 4： polish
- [ ] trace 可下载原始 JSON
- [ ] trace 支持过滤（只看 tool / 只看 state）
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
