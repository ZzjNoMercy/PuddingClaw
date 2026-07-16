# Tool Context 压缩设计方案

状态：设计确认稿
适用范围：DeepAgents；Chat 链路仅复用兼容的数据协议，不改变其现有功能
核心原则：UI 历史完整、模型上下文精简、工具调用协议完整、压缩过程不打断用户

## 1. 背景与问题

长任务中，工具结果会持续进入后续每一轮模型上下文。最近一次问题会话的模型输入约为 101,349 tokens，其中 106 条历史 `ToolMessage` 占约 84,247 tokens，即 83.1%。这会导致：

- token 计数增长过快；
- 有效用户意图和系统约束被大量旧工具结果稀释；
- 模型开始重复读取、误判文件路径或输出语言漂移；
- 即使全局上下文尚未达到 200k，也可能提前出现质量退化；
- 全局 summarize 需要承担本应由工具上下文治理解决的问题。

因此需要在不损害可观测性和证据完整性的前提下，单独治理 Tool Context。

## 2. 目标与非目标

### 2.1 目标

1. 当前轮工具结果默认完整交给模型，保证本轮推理质量。
2. Agent 结束后异步压缩历史工具上下文，不阻塞最终回复。
3. UI 仍展示原有工具过程和结果，用户观感连续。
4. Session 保留可追溯的完整证据引用。
5. 下一轮 `_build_messages()` 优先使用压缩后的 `context_output`。
6. 所有压缩操作严格保持 Tool Call 与 Tool Result 的协议关联。
7. 与 DeepAgents 的 200k 全局 summarize 独立工作。
8. 不影响没有内置 summarize middleware 的 Chat 链路。

### 2.2 非目标

- 本阶段不设计长期 Memory、用户偏好学习或跨 Session 知识沉淀。
- 不删除 UI 历史消息。
- 不用压缩结果替换当前轮正在使用的原始结果，除非触发单条超大结果保险。
- 不把工具过程隐藏成一段不可审计的 Agent 摘要。

## 3. 不可破坏的协议原则

### 3.1 `tool_call_id` 必须原样保留

`tool_call_id` 是 Tool Call 与 Tool Result 的协议主键。任何压缩都只能改变 Tool Result 的内容表示，不能改变消息关联。

必须满足：

- 压缩前后的 `tool_call_id` 字符串完全一致；
- Tool Call ID 集合、数量及顺序不因压缩发生变化；
- 一条 Tool Result 仍对应原来那一条 Tool Call；
- 不生成新的 ID，不复用其他调用的 ID；
- 不合并多条 Tool Result 为一条而丢失逐条关联；
- 不删除只有错误结果但仍属于协议链的 Tool Result；
- 批量更新必须以 `(session_id, tool_call_id)` 精确定位。

压缩前：

```json
{
  "type": "tool",
  "tool_call_id": "call_abc123",
  "output": "完整工具结果"
}
```

压缩后供模型使用：

```json
{
  "type": "tool",
  "tool_call_id": "call_abc123",
  "content": "结构化压缩结果"
}
```

其中 `call_abc123` 必须逐字节不变。

### 3.2 历史缺失 ID 的处理

若旧数据本身缺少 `tool_call_id`，不能在每次加载时临时随机生成。应通过一次性迁移生成稳定 ID 并持久化，同时建立 Call/Result 对应关系；无法可靠配对的数据应标记异常并退出自动压缩。

## 4. 数据模型

每条工具结果建议保留以下逻辑字段：

```json
{
  "tool_call_id": "call_abc123",
  "output": "供 UI 展示的完整结果或可展开预览",
  "context_output": "供模型上下文使用的压缩结果",
  "raw_output_ref": {
    "kind": "result_id",
    "value": "result-xxx"
  },
  "context_compaction": {
    "status": "ready",
    "source_hash": "sha256:...",
    "policy_version": "tool-context-v1",
    "method": "database_adapter",
    "compacted_at": "2026-07-15T10:00:00+08:00"
  }
}
```

字段职责：

- `output`：UI 使用。保持用户已经看到的过程，不因模型上下文压缩而被替换。
- `context_output`：模型专用。`_build_messages()` 有值时优先读取，无值时回退 `output`。
- `raw_output_ref`：完整证据的持久引用，避免把同一份大文本重复塞进 Session JSON。
- `context_compaction`：压缩状态、幂等信息和审计信息。

当工具适配后的 `output` 与原始结果完全一致时，不额外写一份相同的
`raw_output`；只有两者确实不同时才保留原文副本并建立引用，避免 Session
体积无意义翻倍。

### 4.1 `raw_output_ref` 的生命周期

数据库查询已经有稳定 `result_id` 时，应直接引用而非复制正文。若原结果存储有 TTL，压缩任务必须选择以下一种策略：

- 为 Session 引用续期；
- 将结果固定到长期对象存储；
- 复制到专用的 Session artifact 存储，并更新引用。

不能出现 UI 声称“可展开完整结果”，但引用已过期的情况。

## 5. 三阶段执行模型

### 5.1 阶段 A：当前轮保险

执行过程中，每一条 Tool Result 独立判断，默认保持完整。只有单条工具结果超过 8k tokens 时，才对进入模型的内容做即时保护。多条结果均未超过 8k 时，不因本轮累计 tokens 增长而触发额外压缩。

该行为由 `ToolContextCompactionMiddleware` 提供。工具结果压缩关闭时不注册该中间件，因此不会执行单条 8k 即时保护，当前轮工具结果自然按原文进入模型。

即时保护仍须保留 `tool_call_id` 和 `raw_output_ref`，并优先使用确定性裁剪：保留头尾、错误、schema、行数、结果引用和继续读取方法。它是防止单轮爆窗的保险，不替代事后压缩。

### 5.2 阶段 B：Agent 结束后的后台压缩

Agent 完成、取消或报错后，`after_agent` 只做轻量操作：

1. 扫描历史 Tool Result 候选；
2. 排除最近 N 条、未完成、已压缩和低于单条候选阈值的结果；
3. 存在候选时才写入或合并一个幂等 Job；
4. 没有候选时直接结束，不产生事件、不修改 Session；
5. 立即返回，不等待摘要模型。

后台 Worker 再执行选择、压缩和批量更新。最终回复不应因为该 Job 延迟流式输出；UI 可短暂显示“正在优化上下文”，完成后自动消失，不要求用户点击“继续”。

取消和异常轮次同样允许入队，因为中断前工具结果仍会污染下一轮上下文；但 Job 不得修改仍在写入中的结果。

`after_agent` 的轻量候选扫描每次默认执行，但静默压缩没有历史累计总量门槛。只有单条达到后台最小结果阈值的 Tool Result 才进入 Job；如果所有候选都低于该阈值，则当作无事发生。该单条阈值只服务于事后静默压缩，与执行中单条超过 8k 的即时保护相互独立。

Tool Context 总开关关闭时不注册中间件，因此没有 `after_agent` 候选扫描，也不会创建新 Job。配置关闭前已经排队或运行的 Job 可以正常收尾；其产出的 `context_output` 只作为辅助字段保留，未注册中间件时不会进入模型上下文，重新开启后可复用。

### 5.3 阶段 C：下一轮增量回填

下一轮开始构建模型消息时：

1. 读取 Session 的 `tool_context_revision` 和 Job 状态；
2. 不等待仍在运行的后台 Job；
3. 状态为 `ready` 且 hash 有效的条目使用最新 `context_output`；
4. 尚未压缩或仍为 `pending/running` 的条目继续使用原始 `output`，不跳过、不摘要、不重排；

因此用户在压缩尚未结束时继续追问，也能立即开始下一轮：已完成多少就增量使用多少，未完成部分保持原样，消息链和 Tool Call/Result 关系始终连续。下一轮不设置 Tool Context 总量硬阈值，也不启动同步压缩；整体上下文仅由 DeepAgents 的全局 summarize 阈值兜底。

## 6. 阈值建议

首版建议配置：

| 配置项 | 建议值 | 说明 |
| --- | ---: | --- |
| 单条即时保护 | 8k tokens | 防止一个结果挤爆当前轮 |
| 后台单条最小结果 | 1k tokens | 小于该值的历史 Tool Result 不做静默压缩 |
| 静默压缩最近结果保护 | 最近 12 个已完成 Tool Result（可配置） | 不进入事后后台压缩候选集 |
| 全局 summarize | 200k tokens | DeepAgents 独立阈值 |

这些阈值应配置化，并以真实会话分布校准。后台静默压缩按单条结果判断，不依赖历史总量；200k 管的是整体对话 summarize。下一轮没有额外的 Tool Context 总量压缩阈值。

### 6.1 Harness 前端配置

在“设置 → Harness 配置”下增加“上下文工程”分类。本方案涉及的运行阈值全部从这里读取和修改，不再把关键数值只留在后端常量或手工编辑的 `config.json` 中：

| 前端字段 | 配置键 | 默认值 | 作用阶段 |
| --- | --- | ---: | --- |
| 全局摘要触发阈值 | `compression.deepagents.summarization.trigger_tokens` | 200,000 tokens | DeepAgents 整体对话 summarize |
| 工具结果压缩 | `compression.deepagents.tool_context.enabled` | 开启 | 控制执行中单条保护、事后静默压缩及模型是否使用 `context_output` |
| 执行中单条工具阈值 | `compression.deepagents.tool_context.single_tool_trigger_tokens` | 8,000 tokens | 当前执行中，单条 Tool Result 即时保护 |
| 静默压缩单条下限 | `compression.deepagents.tool_context.background_min_result_tokens` | 1,000 tokens | `after_agent` 候选扫描；低于此值不处理 |
| 保留最近工具结果 | `compression.deepagents.tool_context.keep_recent_tool_results` | 12 条 | 事后静默压缩保护窗口 |

“最近 N 条”只约束 Agent 结束后的静默压缩候选集。执行中每一条 Tool Result 仅在自身超过 8k 时触发即时保护；Agent 结束后，本轮已完成的 Tool Result 也按完成时间进入最近 N 条保护窗口，而不是排除整个用户轮次。用户只配置全局摘要触发阈值；摘要输入上限不作为配置保存或回传，由后端在运行时按当前模型 `context_window` 的 80% 自动派生。1M 上下文对应 800k，因此正常在 200k 触发时，待摘要历史可完整进入摘要调用。这些设置仅作用于 DeepAgents；Chat 的 `compression.middleware` 配置和行为保持不变。

“工具结果压缩”默认开启。关闭后：

- 构建 DeepAgents graph 时不注册 `ToolContextCompactionMiddleware`；
- 不执行当前轮单条 8k 即时保护；
- 不执行 `after_agent` 候选扫描和事后静默压缩；
- 模型消息沿用原始 Tool Result，不执行 `context_output` 替换；
- 不删除已有摘要、`raw_output_ref` 或压缩元数据，重新开启后可继续复用；
- DeepAgents 的 200k 全局 summarize 继续生效；
- Chat 行为保持不变。

前端交互要求：

- 每个字段显示单位、默认值、作用阶段和简短风险提示；
- 总开关关闭时禁用其下属三个 Tool Context 参数输入，并提示“200k 全局摘要仍然生效”；
- 保存时做正整数、合理上下界和阈值关系校验；
- `background_min_result_tokens` 必须小于 `single_tool_trigger_tokens`；
- 修改后明确提示“对下一次 Agent 运行生效”；
- 设置 API 只读写 `compression.deepagents.*`，不得映射或同步到 Chat 的 `compression.middleware.*`；
- 配置缺失时使用上述默认值，旧 Session 继续兼容。

## 7. 压缩选择策略

候选结果按以下顺序处理：

1. 排除尚未完成或仍在写入的 Tool Result；
2. 按 `keep_recent_tool_results` 保护完成时间最新的 N 条 Tool Result；
3. 排除小于 `background_min_result_tokens` 的短结果；
4. 排除 `source_hash + policy_version` 已完成且内容未变化的结果；
5. 优先压缩体积大、年代久、重复度高的结果；
6. 错误、用户明确引用、后续推理依赖的证据降低压缩优先级；
7. 按 Job 的并发、耗时和 token 预算处理候选；预算耗尽时保留剩余条目，下一次 `after_agent` 扫描再继续，不为追求一次清空而阻塞系统。

首版内部工作预算默认每个 Job 最多处理 48 条候选，LLM 兜底的每条输入约束为
24,000 字符、输出约束为 4,000 字符；这些 worker 安全参数暂不作为 Harness
业务设置暴露。

## 8. 各类工具的确定性压缩

确定性适配器优先于 LLM，因为它更快、可验证、不会改变关键字段。

### 8.1 数据库查询

保留：

- `result_id` / `raw_output_ref`；
- 查询行数、列名和类型；
- 聚合值、空值率或查询返回的 profile；
- 与当前任务相关的代表行；
- 截断标记和继续读取方式；
- SQL 错误的完整错误类型与关键位置。

不要把几百行表格全文重复放入 `context_output`。

### 8.2 SQL 生成与验证

保留：

- SQL generation ID；
- 最终 SQL，或在极长时保留完整引用和关键 CTE；
- 使用的表、字段、过滤条件、分组口径；
- validation 状态；
- 数据库错误原文的关键部分。

### 8.3 文件读取

保留：

- 绝对路径；
- 读取的行号或 offset/limit；
- 文件 hash 或 revision；
- 与目标匹配的精确片段；
- 是否截断及下一段读取位置。

对于重复读取同一文件，可用 revision + 范围去重，但不能删除各自的 `tool_call_id`。

### 8.4 终端命令

保留：

- command、cwd、exit code；
- 关键 stdout/stderr；
- 失败原因；
- 生成或修改的 artifact 路径；
- 输出截断信息。

### 8.5 搜索与 grep

保留 query、搜索范围、命中总数、主要命中位置和文件 revision。大量同质命中可折叠，但需留下继续展开完整证据的引用。

### 8.6 错误结果

错误结果采用高保真策略：工具名、参数摘要、异常类型、关键堆栈、重试状态和诊断线索必须保留。错误不能只压成“工具失败”。

## 9. LLM 摘要兜底

只有无稳定结构的长文本才进入摘要模型。约束如下：

- 使用专用内部角色，例如 `role="summary"`；
- 非流式调用，不能混入用户可见输出；
- 每批 4–8 条，最大并发建议 4；
- 每条结果有输入、输出和超时预算；
- 失败时回退确定性头尾裁剪；
- 摘要 Prompt 明确要求保留 ID、路径、数字、错误、决策和后续动作；
- 摘要结果不能编造原结果不存在的信息。

现有 Chat `ToolResultClearMiddleware` 若逐条串行调用摘要模型，会在大型 Session 上产生几十次调用，不能直接照搬到 DeepAgents 后台 Job。应复用其协议思想，而不是复用其执行方式。

## 10. 幂等、并发与不重复压缩

### 10.1 幂等键

每条结果的逻辑幂等键为：

```text
(session_id, tool_call_id, source_hash, policy_version)
```

- `source_hash`：原始结果的规范化内容 hash；
- `policy_version`：压缩规则版本；
- 两者都相同且状态为 `ready`，直接复用已有 `context_output`；
- 原结果变化或规则升级时才允许重算。

### 10.2 状态机

```text
pending -> running -> ready
                  -> failed
                  -> stale
```

Worker 获取有时限的 lease。崩溃后的 `running` 可在 lease 过期后重试。更新前必须再次比较 `source_hash`，避免用旧摘要覆盖新结果。

### 10.3 Session revision

Session 维护单调递增的 `tool_context_revision`：

- Job 创建时记录 `base_revision`；
- Worker 只更新仍与快照一致的目标记录；
- 提交成功后一次性增加 revision；
- `_build_messages()` 以 revision 为缓存键，防止读到新旧混合状态。

批量提交前后应校验：

```text
tool_call_id_set(before) == tool_call_id_set(after)
```

不相等则整批拒绝提交并报警。

## 11. 模型消息构建

中间件注册时，在送入模型前对每条 Tool Result 使用：

```python
if result.context_compaction.status == "ready" and source_hash_is_valid(result):
    content = result.context_output
else:
    content = result.output
```

同时必须：

- 使用原始 `tool_call_id` 构造 `ToolMessage`；
- 保持消息顺序；
- 不因 `context_output` 为空而跳过 Tool Result；
- 对引用失效、hash 不匹配或压缩状态异常的结果回退 `output`；
- 后台 Job 未完成时只回填已经 `ready` 的条目，其余条目不做同步压缩；
- 混合使用已压缩与原始结果时仍保持原消息顺序和完整 Tool Call/Result 配对；
- 尚未完成的 Tool Result 不进入后台压缩；已完成结果按最近 N 条规则决定是否保护，不扩大为整轮保护。

这段替换属于 `ToolContextCompactionMiddleware` 的模型输入处理，不应固化为 DeepAgents `_build_messages()` 的无条件逻辑。中间件未注册时，模型构建链路完全沿用原始 `output`。

## 12. UI 与用户体验

UI 继续读取 `output`，因此历史工具调用、过程观察和结果仍按原样显示。`context_output` 默认不展示，可在诊断模式中显示“模型使用了压缩上下文”。

推荐状态条：

- 触发时：`上下文较长，正在后台优化…`
- 完成后：短暂显示 `上下文已优化，任务将继续`，随后自动收起；
- 不新增一条聊天消息；
- 不清空流式内容；
- 不要求用户再次输入“继续”；
- 后台 Job 失败时不打断对话，只记录诊断；未压缩条目继续回退原始 `output`。

后台 Job 完成后前端重新拉取一次 token 用量。该用量按当前开关动态应用已经
`ready` 的 `context_output` 差值，但不改写保存的原始 Agent 消息；因此关闭
中间件时，计数和模型输入都会一致地回到原始工具上下文口径。

工具过程中的可见 AI 文本仍应按用户语言输出。Tool Context 压缩只能缓解模型污染，不能替代 AGENTS.md 的中文可见输出约束。

## 13. Chat 与 DeepAgents 的兼容边界

### 13.1 DeepAgents

新增 `context_output` 消费、后台 Job、确定性适配器和下一轮增量回填。内置 summarize 默认改为 200k，并可在 Harness 的“上下文工程”中配置，负责整体对话压缩。

### 13.2 Chat

Chat 是待退出的兼容链路，不属于本方案的配置或改造范围。实施时只允许：

- 数据结构向后兼容；
- 未设置 `context_output` 时完全沿用 `output`；
- 继续运行现有 `ToolResultClearMiddleware`；
- 不把 DeepAgents 的 200k 阈值或后台 Job 强行接入 Chat。

现有 Chat `ToolResultClearMiddleware` 与本方案的候选选择语义相近：它在 `abefore_model` 中只考虑最后一条 `HumanMessage` 之前的历史 Tool Result，并保留最近 N 条（当前默认 10 条），因此执行中的当前轮不会被 Tool Result Clear，到了下一轮才成为历史候选；重建 `ToolMessage` 时也保留原 `tool_call_id`。

两者的执行机制仍须保持隔离：Chat 当前在下一轮 `before_model` 内串行等待摘要，且以字符长度阈值筛选；DeepAgents 新方案在 `after_agent` 后台静默压缩，执行中只对单条超过 8k tokens 的结果做即时保护，下一轮对未完成的后台结果不等待。首版不借重构 DeepAgents 的机会改变 Chat 行为。

不为 Chat 新增上下文工程配置，不同步 DeepAgents 的阈值，不借本方案重构 Chat 中间件。现有 Chat 行为仅作为兼容回归基线，直至该链路下线。

## 14. 任务取消与失败

- Agent 正常结束：创建后台 Job。
- 用户停止：完成持久化后可创建 Job，但跳过未闭合的 Tool Call。
- 模型超时：已完成的 Tool Result 可压缩，未完成结果保持原状。
- 工具异常：保留高保真错误内容并允许压缩其他旧结果。
- 后台摘要失败：记录 `failed`，不影响 Session 可继续使用；下一轮继续使用原始 `output`。

## 15. 可观测性

至少记录以下指标：

- `tool_context_tokens_before`
- `tool_context_tokens_after`
- `selected_tool_count`
- `deterministic_compaction_count`
- `llm_summary_count`
- `compaction_job_duration_ms`
- `compaction_job_queue_delay_ms`
- `compaction_cache_hit_count`
- `compaction_failure_count`
- `raw_output_ref_missing_count`
- `tool_call_id_integrity_failure_count`

日志必须包含 `session_id`、Job ID、policy version 和 revision；不要记录敏感原始输出正文。

## 16. 测试门禁

### 16.1 协议测试

- 压缩前后 `tool_call_id` 集合、顺序、数量完全一致；
- 每个 Tool Call 均能找到原 Tool Result；
- error Tool Result 也不丢失；
- 批量提交出现 ID 差异时原子回滚。

### 16.2 幂等与并发测试

- 相同 hash + policy 重跑不再次调用摘要模型；
- 原输出变化后旧结果被判定为 stale；
- 两个 Worker 抢同一 Job 时只有一个获得 lease；
- Job 执行期间新工具结果写入不会被旧快照覆盖。
- 用户在 Job 运行中继续追问时，只使用已 `ready` 的 `context_output`，其余结果原样回退 `output`。

### 16.3 上下文测试

- 当前轮短结果完整进入模型；
- 单条 >8k 触发即时保护；
- 每次 Agent 结束都扫描候选，但没有合格候选时不创建 Job、不发状态事件；
- 存在超过后台单条下限且不在最近 N 条保护区的结果时创建后台 Job；
- 后台 Job 未完成时，下一轮不等待且不触发同步压缩；
- DeepAgents 总上下文 200k summarize 正常触发，且前端修改后对新运行生效。

### 16.4 UI 与回归测试

- UI 历史工具结果不因 `context_output` 改变；
- 流式回复不等待后台摘要；
- 状态条完成后自动收起；
- Harness“上下文工程”可保存并回显全局阈值、单条阈值和最近 N 条 Tool Result；
- Chat 原有 Tool Result Clear 和 compact 流程行为不变；
- Session reload 后完整证据仍可展开。

## 17. 实施阶段与代码量预估

### Phase 1：双字段协议与读取路由

- Session schema 增加 `context_output`、`raw_output_ref`、compaction metadata；
- DeepAgents `_build_messages()` 优先读取 `context_output`；
- `tool_call_id` 完整性断言；
- 向后兼容旧 Session。

预估：后端 150–250 行，测试 150–250 行。

### Phase 2：后台 Job 与幂等机制

- `after_agent` 入队；
- Job 状态、lease、revision、CAS 更新；
- Session 级别批量提交；
- 指标和错误回退。

预估：后端 300–500 行，测试 250–400 行。

### Phase 3：确定性工具适配器

- DB、SQL、文件、终端、搜索、错误适配器；
- token 估算与选择算法；
- `raw_output_ref` 生命周期处理。

预估：后端 450–750 行，测试 350–600 行。

### Phase 4：LLM 摘要兜底和 UI 状态

- 批处理、并发、预算、超时和 fallback；
- context maintenance 状态事件；
- 前端短状态条和诊断信息。

预估：后端 180–300 行，前端 80–150 行，测试 180–300 行。

总计约 2,100–3,500 行（含测试）。如果首版只做 DB、文件、终端三类适配器，可压缩到约 1,400–2,200 行。

## 18. 验收标准

1. 每次 Agent 结束后默认扫描历史 Tool Result；有合格候选才静默压缩，全部候选低于单条阈值时无任何用户可见副作用。
2. 最终回复流式不等待后台 Job，用户无需点击或输入“继续”。
3. UI 历史、工具调用和完整证据引用保持可见、可追溯。
4. 任意压缩前后 `tool_call_id` 集合与关联完全一致。
5. 同一结果不会因重复打开 Session 或重复入队而再次摘要。
6. 原始结果变化或 policy 升级时能安全重算，不覆盖新数据。
7. DeepAgents 200k 全局 summarize 可配置，Chat 现有压缩链路不受破坏。
8. 出错或取消时对话仍可继续，未压缩结果在下一轮保持原文。

## 19. 推荐落地顺序

先实现双字段和 ID 协议测试，再实现后台 Job；随后优先覆盖数据库、文件读取和终端输出，因为它们通常贡献最多 tokens；最后增加 LLM 兜底和 UI 状态。任何阶段只要 `tool_call_id` 完整性测试未通过，就不得上线。
