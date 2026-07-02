# Model Call Contract 与归因层实现计划

## 当前目标

- [x] 先保存每次最终进入模型边界的 `Model Call Contract`。
- [x] 在 Trace 面板的模型输入详情中显示 contract 指纹。
- [x] 记录关键运行包版本，便于排查 LangChain / LangGraph / DeepAgents 升级造成的 prompt 或 tool schema 变化。
- [x] Trace 流程视图按 hook 语义展示：`before_model` 保持为 `graph.model` 前置处理，`wrap_model_call` 作为 `graph.model` 内部 wrapper，不再画成线性前置步骤。
- [x] 后续逐步实现 middleware / hook 边界归因的第一版 proxy。

## 第一性原则

Trace 的核心事实不是“源码里哪个中间件看起来会执行”，而是：

1. 模型最终吃到了什么 messages。
2. system prompt 最终是什么。
3. tool schema 最终暴露了什么。
4. 工具最终收到了什么参数并返回了什么结果。

`Model Call Contract` 是事实边界；归因层只用于解释这些事实为什么改变。

## 已落地的数据结构

每次模型调用在 `model_input` span 中保存：

```json
{
  "messages_preview": [],
  "model_call_contract": {
    "message_count": 0,
    "system_prompt_chars": 0,
    "estimated_tokens": 0,
    "tool_schema_count": 0,
    "tool_schemas": [
      {
        "name": "tool_name",
        "description": "...",
        "schema_hash": "..."
      }
    ],
    "params": {
      "model": "...",
      "temperature": 0.7,
      "streaming": true
    },
    "fingerprints": {
      "messages_hash": "...",
      "system_prompt_hash": "...",
      "tool_schema_hash": "..."
    }
  }
}
```

`runtime_inventory.package_versions` 保存：

- `deepagents`
- `langchain`
- `langchain_core`
- `langgraph`

## 后续归因层方案

### Phase A：Hook Boundary Snapshot

目标：不深入第三方包内部，先在 LangChain middleware hook 边界记录 before / after。

当前进展：

- [x] UI 语义先校正：`before_model` 是模型节点前的上下文整理；`wrap_model_call` 是包裹真实 LLM 调用的 wrapper。
- [x] 流程树不再把 `wrap_model_call` 插到 `before_model` 前面，避免误导为线性前置步骤。
- [x] 后端 trace 增加 `hook_boundary_snapshots`，在模型输入边界记录 `before_model.after` 与 `wrap_model_call.before` 的 observed snapshot。
- [x] 前端中间件视图展示当前 hook 的 boundary snapshot，显示 messages / system prompt / tool schema fingerprints。
- [x] 后端补充真实 hook boundary before / after snapshot：对 PuddingClaw 传入 `create_deep_agent` 的 middleware 使用 `TracingMiddlewareProxy` 记录直接归因。

当前边界：

- PuddingClaw 传入的 middleware，例如 `MemoryMiddleware`、`ExternalFilePermissionMiddleware`、`ModelCallLimitMiddleware`，可以通过 proxy 记录 `coverage=direct`。
- DeepAgents 自动注入的 base stack，例如 `TodoListMiddleware`、`SkillsMiddleware`、`SubAgentMiddleware`、`PatchToolCallsMiddleware`，当前仍是 observed / inferred，不伪装成逐个 middleware 归因。

建议快照点：

- `before_agent.before`
- `before_agent.after`
- `before_model.before`
- `before_model.after`
- `wrap_model_call.before`
- `wrap_model_call.after`
- `after_model.before`
- `after_model.after`

每个快照只记录可比较摘要：

- `messages_hash`
- `system_prompt_hash`
- `tool_schema_hash`
- `message_count`
- `system_prompt_chars`
- `tool_schema_count`
- `recent_messages`

### Phase B：DeepAgents Base Stack Attribution

目标：在不修改第三方源码的前提下，对 DeepAgents 自动注入的 base middleware 也做逐个 before / after diff。

已完成的基础设施：

- `backend/graph/middleware_trace_proxy.py` 提供动态代理。
- 代理类同时继承原 middleware 类，保留 `isinstance(m, MemoryMiddleware)` 这类兼容判断。
- 代理只暴露原 middleware 类真实实现的 hook，避免把没有实现的 hook 伪造成运行节点。
- 代理通过 `TraceCollector.record_middleware_hook_attribution()` 写入：
  - `middleware_invocation`
  - `hook_boundary_snapshot`
  - `metadata.coverage=direct`
- `wrap_model_call` / `wrap_tool_call` 采用 handler 代理，不把 request 和 response 互相 diff：
  - `request_before_wrapper`：进入该 wrap middleware 前的 request。
  - `request_sent_to_handler`：middleware 实际调用 `handler(request)` 时送出的 request。
  - `response_observed`：handler 返回的 response，只作为结果快照，不默认作为 middleware 归因 diff。
  - diff 只比较 `request_before_wrapper` 与 `request_sent_to_handler`。
- UI/数据去重规则：
  - `coverage=direct` 的 proxy invocation 是真实 hook 调用。
  - 后验 `coverage=inferred` effect 只作为证据补充；如果同一个 hook/middleware 已有 direct invocation，不再额外制造一次 invocation。
  - 历史 trace 里如同时存在 direct 与 inferred invocation，前端只展示 direct invocation。
  - Hook Boundary Snapshot 面板只显示当前选中 invocation 关联的 snapshot，并把 count/char/hash 分开展示，避免 state snapshot 没有 tool hash 时看起来像空白。
  - `Model input boundary` 是模型调用契约边界事实，不是某个 middleware 的直接归因；它可以作为 `middleware_effect` / `hook_boundary_snapshot` 展示在模型输入详情里，但不生成 `middleware_invocation`，也不让单个 middleware 卡片显示为 changed。

下一步可选方案：

- 接管 DeepAgents base stack 装配，把自动注入 middleware 替换成代理版本。
- 或等待/利用 DeepAgents 暴露 middleware factory / profile hooks 后再包裹。

效果记录示例：

效果记录示例：

```json
{
  "middleware": "MemoryMiddleware",
  "hook": "before_agent",
  "category": "model_input",
  "changed": true,
  "diff": {
    "system_prompt_chars_delta": 842,
    "message_count_delta": 0,
    "tool_schema_count_delta": 0
  },
  "before_hash": "...",
  "after_hash": "...",
  "evidence": [
    "project memory injected",
    "system prompt changed"
  ]
}
```

### Phase C：UI 归因视图

在当前 Trace 面板中补充：

- 每次 model call 的最终 contract。
- 对比上一轮 model call 的 hash 变化。
- 点击变化项后显示候选归因链：
  - 哪个 hook boundary 发生变化。
  - 哪个 middleware wrapper 发生变化。
  - 如果无法归因到单个 middleware，则显示为 hook-level changed。

## 不做的事

- 不依赖第三方包内部源码结构作为事实来源。
- 不把所有 middleware 内部私有字段塞进 trace。
- 不为了归因牺牲模型调用边界的可读性。
