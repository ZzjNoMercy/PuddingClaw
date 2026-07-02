# State 与 Model Input 双事实 Trace 方案与实施计划

## 目标

Trace 的第一性原理是先记录运行时事实，再做归因解释：

- State 与 Model input 是并列的一手事实：前者记录 LangGraph runtime 中每一步读写的状态，后者记录真实进入 LLM 的最终上下文。
- State 负责说明“运行过程中发生了什么字段变化”，Model input 负责说明“模型这一轮到底看到了什么”。
- Proxy 是统一埋点入口：中间件前后、wrap handler 边界都通过代理层采集，避免散落在业务代码和第三方源码里。
- UI 渐进：后端先提供稳定事实结构，前端先复用现有视图逐步增强，不一次性大拆。

## 核心数据模型

### 1. State Snapshot

每个 state hook 前后都记录 state 摘要：

- `state_keys`：顶层 key 列表。
- `state_fields`：按字段记录类型、hash、数量、轻量摘要。
- `messages` 特化指标：message count、roles、tool call count、hash。
- `skills_metadata` 特化指标：skill count、skill names、hash。
- `todos` 特化指标：todo count、status counts、hash。
- `files` 特化指标：file count / keys / hash。

State snapshot 只说明“state 里有什么”，不说“已经进入 system prompt”。

### 2. State Diff

每个 middleware proxy invocation 对比 before / after：

- `state_keys_added`
- `state_keys_removed`
- `state_fields_changed`
- 特化 delta：`message_count_delta`、`todo_count_delta`、`skills_count_delta`
- hash 变化：`messages_hash_changed`、`state_hash_changed`

这回答“state 里哪个字段发生了变化”。

### 3. Model Input Contract

每次真实模型调用保存最终输入：

- `system_prompt`
- `messages`
- `tool_schemas`
- `model_params`
- `tool_choice`
- 指纹：`messages_hash`、`system_prompt_hash`、`tool_schema_hash`

这回答“LLM 到底看到了什么”，与 State Diff 并列展示，不作为 State 的替代品。

### 4. Attribution

归因嵌入事实节点，不单独作为主流程：

- `direct`：proxy 明确记录某个 middleware 的 before / after。
- `boundary`：只知道模型输入边界变化，不能归因到单个 middleware。
- `inferred`：通过内容特征推断。

UI 上归因是事实的注释，不替代 State / Model input 事实本身。

## UI 原则

默认仍是一条连续 Timeline：

```text
before_agent
  SkillsMiddleware
    State: skills_metadata +17
    Impact: will be appended to model system_message later

graph.model / Model Call #1
  Model Input
    System Prompt
    Messages
    Tools
```

辅助视图可以过滤 state、model calls、runtime inventory、compiled graph，但默认不把用户切碎到多个 tab 里。中间件视图正常展示它实际记录到的 state 变化和 model input 变化，不预设某一类变化更重要。

## 实施阶段

### Phase 1：后端 State 字段级摘要

- [x] `TraceCollector._state_summary()` 增加 `state_fields`。
- [x] `hook_summary_diff()` 增加字段级 diff。
- [x] 测试覆盖 `skills_metadata`、`todos`、普通字段变化。
- [x] 保持现有 UI 可读，不要求前端一次性大改。

### Phase 2：Proxy 统一事件语义

- [ ] state hook proxy 记录 `payload_kind=state` 的 before / after。
- [ ] wrap hook proxy 记录 `payload_kind=model_request` 的 request before / request sent。
- [x] 所有 proxy invocation metadata 标明 `observability_layer=middleware_proxy`。

### Phase 3：Model Input 与 State 辅助关联

- [ ] model input contract 可选增加 `linked_state_event_order` 或最近 state snapshot 引用，作为辅助导航，不强行归因。
- [ ] system prompt section 尝试标注来源：skills / memory / subagent / summarization。
- [ ] 只把可证明的来源标为 `direct`，其余为 `boundary` / `inferred`。

### Phase 4：UI 渐进增强

- [ ] Timeline 节点详情展示事实变化摘要：State Diff 和 Model Input Diff 并列。
- [ ] graph.model 节点内展示 model input contract。
- [ ] 再做 State / Model Calls 的筛选视图。

## 当前不做

- 不一次性重写 Trace UI。
- 不把 model input 变化强行归因给某个 middleware。
- 不修改 LangChain / DeepAgents 源码。
- 不在 before_agent 阶段声称 system prompt 已经注入。
