# Middleware Hook Invocation UI 实施方案

> 状态：Phase 1 Done  
> 创建时间：2026-06-30  
> 关联设计稿：`designs/trace-middleware-hooks/index.html`  
> 关联代码：`frontend/src/components/agent/TraceViewer.tsx`、`backend/graph/trace_collector.py`、`backend/graph/deepagents_manager.py`

## 1. 背景

当前 Trace 看板已经能展示 LangGraph 图、span 流、工具调用、todo、runtime inventory 和 middleware effects。但中间件视图仍偏“实现清单”：

- 用户看到的是 middleware stack / effect 分类，而不是 LangChain / DeepAgents 真实生命周期。
- 左侧数字如果只表达 middleware 数量，会误导用户：一次 query 内同一个 hook 可能多次触发。
- 用户需要理解 `before_model`、`wrap_model_call`、`after_model` 等 hook 在一次 Agent Loop 中如何反复出现，但不应被迫跳到复杂流程图中查找。

新的 UI 目标是：以 **六大 Hook** 为主轴，同时在每个 hook 下展示 **本次 query 的触发次数 invocation**、**当前 invocation 所处流程位置**、**参与的 middleware** 和 **可验证证据**。

## 2. 设计原则

### 2.1 左侧数字含义

左侧 hook badge 不表示 middleware 数量，而表示本次 query 内该 hook 被触发的次数：

```text
before_model      2×
wrap_model_call   3×
wrap_tool_call    3×
```

middleware 数量降级为辅助 chip：

```text
2 middleware · 洋葱式包裹
```

### 2.2 Hook 视图不替代流程视图

Hook 视图不展示完整 LangGraph DAG，也不要求用户跳流程视图。它在当前页面内提供 mini flow：

```text
Tool result → before_model → LLM → after_model
```

这样用户能知道当前 invocation 来自哪一轮 Agent Loop。

### 2.3 第一版不伪造 diff

真实 before / after diff 只有在后端 trace 已捕获快照时才展示。没有埋点时，前端只显示：

- middleware 注册清单；
- hook 触发次数；
- span / effect / tool / todo 证据；
- 当前 trace 暂无 before/after 快照提示。

不要为了 UI 好看伪造 MemoryMiddleware、SummarizationMiddleware 等 diff。

## 3. UI 内容与实现难度

| UI 内容 | 难度 | 第一版策略 |
|---|---:|---|
| 三栏布局：Hook / Diff / Evidence | 低 | 直接在现有 `MiddlewareTracePanel` 内实现 |
| 六大 hook 列表 | 低 | 固定 hook 列表，按 runtime inventory 聚合 |
| hook badge 显示 `N×` | 中 | 从 trace spans / middleware effects 推导 invocation 数 |
| middleware 数量 chip | 低 | 从 runtime inventory 的 `middleware.stack[].hooks` 聚合 |
| mini flow | 中 | 从当前 invocation 附近 spans 推导，第一版可用简化流 |
| invocation tabs | 中 | 每个 hook 下按触发顺序生成 tabs |
| middleware 卡片 | 中 | 使用 inventory entries + effects 证据 |
| `changed/read/noop` 状态 | 中-高 | 第一版使用 effect presence 推断，后续由后端提供 |
| before / after diff | 高 | 第一版只展示已有 `TraceMiddlewareEffect`，不伪造 |
| 右侧“实际执行证据” | 中 | 从 invocation spans/effects 拼出证据列表 |
| Raw / Timeline 切换 | 中-高 | 暂不做，保留到后续 |

## 4. 数据模型

### 4.1 前端派生模型

第一阶段不要求后端立即新增协议，先从现有 `AgentTrace` 派生：

```ts
type HookName =
  | "before_agent"
  | "before_model"
  | "wrap_model_call"
  | "after_model"
  | "wrap_tool_call"
  | "after_agent";

type HookInvocation = {
  id: string;
  hook: HookName;
  index: number;
  sequence: number;
  title: string;
  note: string;
  previous: string;
  next: string;
  flow: string[];
  evidence: string[];
  spans: TraceSpan[];
  effects: TraceMiddlewareEffect[];
};

type HookGroup = {
  hook: HookName;
  description: string;
  rule: "正序执行" | "反序执行" | "洋葱式包裹";
  middleware: TraceRuntimeMiddlewareEntry[];
  invocations: HookInvocation[];
};
```

### 4.2 第一版推导规则

优先级从高到低：

1. `trace.middleware_effects[].hook` / `metadata.hook` / `metadata.langchain_hook`。
2. `TraceSpan.name` / `TraceSpan.label` / `TraceSpan.metadata` 中包含 hook 名。
3. `TraceSpan.type` 映射：
   - `model_input` / `llm` 前后可推导 `before_model`、`wrap_model_call`、`after_model`。
   - `tool` / `skill` / `memory` 可推导 `wrap_tool_call`。
   - root span 开始/结束可推导 `before_agent`、`after_agent`。

如果找不到真实触发，仍展示 runtime inventory 中该 hook 的 middleware，但 invocation 显示为 `0×` 或“本轮暂无记录”。

## 5. 后端后续协议

为了让 UI 从“推导”升级为“准确”，后端后续应在 trace event 中增加：

```json
{
  "type": "middleware_invocation",
  "hook": "wrap_model_call",
  "middleware": "MemoryMiddleware",
  "invocation_index": 2,
  "sequence": 9,
  "status": "changed",
  "input_preview": "...",
  "output_preview": "...",
  "diff": {
    "kind": "prompt_patch",
    "before": "...",
    "after": "..."
  },
  "flow_ref": {
    "previous": "before_model #2",
    "next": "LLM stream #2",
    "phase": "follow_up_model_request"
  }
}
```

注意：

- prompt、tool input、memory 内容必须脱敏与截断。
- diff 默认只保存 preview，不保存完整敏感内容。
- Raw JSON 下载需要单独的隐私开关。

## 6. 分阶段计划

### Phase 1：前端 Hook Invocation UI（已完成，2026-06-30）

- [x] 完成 HTML 设计稿：`designs/trace-middleware-hooks/index.html`
- [x] 评估 UI 内容实现难度
- [x] 编写本实施文档
- [x] 在 `TraceViewer.tsx` 中把 middleware tab 改成六大 hook 视图
- [x] 从现有 `runtime_inventory` + `trace.spans` + `middleware_effects` 派生 hook groups
- [x] 左侧 badge 显示 invocation 次数，middleware 数量作为 chip
- [x] 中间支持 invocation tabs + mini flow + middleware/effect 卡片
- [x] 右侧展示当前 invocation 摘要和证据
- [x] 右侧摘要节点可点击弹出关联流程视图
- [x] 关联流程视图中插入独立的 middleware hook 高亮节点
- [x] 执行证据去重，避免 invocation 和 effect 证据重复展示
- [x] invocation 序号从 `01/03` 改为 `第 1 次 / 共 3 次`
- [x] 中间详情区改为 invocation-scoped：选中某一步时只展示该 invocation 的 effect/diff/evidence，不汇总整个 hook 下所有 changed
- [x] 区分 entered/checked 与 triggered/changed：例如 SummarizationMiddleware 进入 `before_model` 链但未达到摘要阈值时显示 observed，不显示 changed diff
- [x] 前端构建验证：`npm run build`

Phase 1 说明：

- 这一版没有修改后端协议。
- invocation 数量优先从现有 hook/effect trace 中识别；如果没有 hook 字段，则按 span 类型做保守推导。
- 没有真实 before/after 快照时，前端明确展示“不伪造 diff”的提示。
- 点击“当前触发 / 上一节点 / 下一节点”会打开轻量弹窗。弹窗左侧复用本次实际流程卡片视图，但会按 `event_order` 插入一个独立的紫色 `middleware hook` 虚拟节点，默认高亮该节点，而不是只复述相邻的 `graph.model` / `graph.tools` 节点。右侧同步显示该 middleware invocation 的 before/after/diff/evidence；如果没有关联 span，则回退展示 effect 和执行证据。
- `middleware hook` 节点是 UI 层的定位标记：它不伪造 LangGraph 原生节点，只表达“这个中间件触发点发生在实际流程的这个位置”。
- 执行证据会合并 invocation evidence 和 effect evidence 后去重，避免同一条证据在面板里出现两次。
- Hook 下的 middleware 卡片仍展示该 hook 的挂载清单，但 changed/read/noop 状态必须基于当前选中的 invocation 计算。不能把同一 hook 下其他 invocation 的 changed 状态混入当前步骤。
- `Model input boundary` 表示最终模型输入快照被捕获，不等于每个 `before_model` middleware 都实际改写了上下文。SummarizationMiddleware 这类阈值型 middleware，只有出现真实 summary 产物、压缩 diff 或后端明确 triggered 事件时才算 changed；单纯进入检查链只能显示 observed/checked。
- 后续若要提高准确性，需要进入 Phase 2，新增后端 `middleware_invocation` 事件。

### Phase 2：后端精准 middleware invocation 事件（部分完成，2026-06-30）

- [x] 扩展 `TraceCollector` 支持 `middleware_invocation`
- [x] `TraceCollector.finish()` 输出 `middleware_invocations`
- [x] `TraceCollector.add_middleware_effect()` 自动生成对应 `middleware_invocation`
- [x] SSE 增加 `middleware_invocation` 事件，前端可实时合并到当前 trace
- [x] 前端 `AgentTrace` 类型增加 `middleware_invocations`
- [x] 中间件 Hook UI 优先使用后端明确的 `middleware_invocations`，旧 trace fallback 到 span/effect 推导
- [x] MemoryMiddleware 在最终 system prompt 中出现 `<agent_memory>` 时，记录 `before_agent / Memory loaded into agent state`
- [x] before_agent prompt 证据按 middleware stack 顺序记录：SkillsMiddleware 在 MemoryMiddleware 前，避免把检测顺序误当执行顺序
- [x] 测试覆盖：collector 生成和 emit `middleware_invocation`
- [ ] 在 DeepAgents manager 的 stream 分支里记录更细的 hook sequence
- [ ] 对 MemoryMiddleware / SummarizationMiddleware / TodoListMiddleware / FilesystemMiddleware 做更精准的专属事件
- [ ] 增加测试覆盖：同一个 hook 多次触发时 invocation index 正确

当前 Phase 2 说明：

- 第一批精准事件来自现有可验证边界：`model_input`、`skills`、`todo state` 等 `middleware_effects`。
- 这一步不伪造 DeepAgents 内部不可见 hook，只把已经被 `TraceCollector` 捕获的 effect 升级成 `middleware_invocation`。
- 后续要进一步细化，需要在 DeepAgents stream 中对模型调用、工具调用、todo 写入等边界显式传入 `flow_ref` 与专属 middleware 名称。

### Phase 3：真实 before / after diff

- [ ] 定义 diff 脱敏和截断规则
- [ ] 先支持 `wrap_model_call` 的 prompt diff
- [ ] 再支持 `before_model` 的 messages/token diff
- [ ] 最后支持 `wrap_tool_call` 的 tool input/output diff

### Phase 4：Raw / Timeline / 导出

- [ ] Raw trace JSON 查看和下载
- [ ] Timeline 视图和 Hook 视图联动
- [ ] 过滤器：只看 model / tool / memory / middleware / todo

## 7. 风险与边界

1. **现有 trace 可能没有 hook 字段**  
   第一版必须允许“推导不完整”，不能阻塞整体 UI。

2. **middleware hooks 与 DeepAgents 内部实现可能变化**  
   runtime inventory 必须来自运行时实际挂载，不要写死 middleware stack。

3. **真实 diff 有隐私风险**  
   尤其是 prompt、memory、文件内容、terminal output。必须先 preview + 截断，再考虑 Raw。

4. **一次 query 的事件量可能很大**  
   Hook 视图默认展示摘要，长文本和 Raw 信息折叠。

## 8. 验收标准

Phase 1 完成后，用户应能在 Trace 看板的“中间件视图”里看到：

- 六大 Hook；
- 每个 Hook 本次 query 触发了几次；
- 每个 Hook 本次挂载了几个 middleware；
- 选中某次 invocation 后，能看到它在局部流程中的位置；
- 能看到对应 middleware/effect 证据；
- 没有真实 diff 时，UI 明确提示“不伪造结果”。
