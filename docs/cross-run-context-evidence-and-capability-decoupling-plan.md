# 跨 Run 完整上下文、Evidence 投影与 Harness 能力解耦方案

> 状态：Implemented，已完成对抗式复核与 E2E 验收
> 日期：2026-07-22
> 范围：Session 历史恢复、ToolCall/ToolMessage、DeepAgents 大结果外置、SQL `result_id` 分页、Todo/Goal 连续性、Skill 激活、权限与 UI 回放
> 核心原则：**原始事实完整保存；模型上下文按需投影；历史证据不授予当前能力或权限。**

## 0. 审核结论摘要

本方案确认并区分两套已经存在、用途不同的“大结果”机制：

1. **DeepAgents 原生大 Tool Result 外置**
   - 由 DeepAgents `FilesystemMiddleware` 在单条 ToolMessage 超过默认约 20,000 tokens 时触发；
   - 完整文本写入 `/large_tool_results/<tool_call_id>`；
   - 给模型的 ToolMessage 替换为外置路径、头尾预览以及分段读取说明；
   - PuddingClaw 将该虚拟目录映射到工作区下 `.puddingclaw/large_tool_results/<session_id>/<query_id>`。

2. **PuddingClaw 自己的 SQL 完整结果分页**
   - `database_sql_execute` 对未完整内联展示但已 materialize 的结果生成 `result_id`；
   - 完整行写入 `backend/data/database-query-results/<result_id>.jsonl`；
   - 模型通过 `database_query_result_page(result_id, page, page_size)` 读取分页；
   - SQL `generation_id`、validation receipt、`result_id` 和 JSONL 是数据库证据链的一部分。

二者不能互相替代：

- DeepAgents 大结果外置解决“任意 ToolMessage 单条过长”；
- SQL Result Store 解决“结构化查询完整结果、分页、导出和后续追问”。

此外，当前代码还存在两种 PuddingClaw 自定义压缩：

- `ToolResultClearMiddleware`：摘要更早的历史 ToolMessage；
- `ToolContextCompactionService` / `single_tool_overflow`：生成历史上下文投影或单条超限摘要。

这些压缩只能改变给 LLM 的上下文，不得删除或覆盖上述两类权威原始结果。

---

## 1. 问题定义

当前跨 Run 上下文链路把“避免旧工具扩权”错误实现成了“删除旧工具上下文”：

```text
上一个 Run
  ├─ ToolCall 输入
  ├─ ToolMessage 结果
  ├─ Todo 进度
  ├─ Artifact / SQL 证据
  └─ 中断位置

新 Run
  └─ 只看到用户可见普通文本
```

结果是：

- 用户输入“继续”后重新查数据库；
- 重新搜索已经定位的文件；
- 重新创建重复 Todo；
- 无法利用上一轮错误输出和验证结果继续分析；
- 自动续跑因为额外 handoff prompt 偶尔正常，手动续跑却从头开始；
- 模型把“看不到历史工具”误认为“此前没有执行过工具”。

第一性原理上，系统混淆了两个问题：

1. **Evidence Plane**：模型应该知道此前发生了什么；
2. **Authority Plane**：模型现在可以调用什么、是否获得权限。

正确修复不是继续删除历史，而是彻底解耦两者。

---

## 2. 总体架构

| 层 | 职责 | 是否是执行权威 |
| --- | --- | --- |
| Session / Event Store | 保存完整原始消息、工具输入输出、状态和关联 ID | 否 |
| Raw Result Store | 保存 DeepAgents 外置大结果与 SQL JSONL 完整结果 | 否 |
| Evidence Projection | 为不同模型生成可追溯的上下文投影 | 否 |
| Context Assembler | 恢复协议完整历史并按预算选择 full/compacted/pointer | 否 |
| Capability Manifest | 告诉 Agent 当前 Run 已激活和可激活的工具 | 是，能力声明 |
| Permission Manifest | 告诉 Agent 当前授权、需 HITL 和禁止项 | 是，权限声明 |
| Tool Gate | 对每次实际 ToolCall 做最终实时核验 | 是，最终放行 |

目标链路：

```text
完整 Session 历史 + 原始结果引用
              │
              ▼
      Evidence Projection
              │
              ▼
协议完整的历史 AIMessage + ToolMessage
              │
              ├── 仅用于模型理解
              │
Capability Manifest + Permission Manifest
              │
              ▼
        当前 Run 主 Agent
              │
              ▼
         Tool Gate 实时放行
```

---

## 3. 原始消息与 Evidence 契约

### 3.1 原始 Session 消息不可变

Session / Trace 必须保存：

- 用户消息；
- Agent 普通回复；
- 已持久化且允许回放的用户可见过程内容；
- ToolCall ID、名称、Input；
- ToolMessage 完整结果或权威 Raw Result 引用；
- 成功、失败、中断、超时状态；
- source Run / Query / Goal；
- Todo、Artifact、SQL generation、validation receipt；
- Permission Request / Grant 的审计事实。

压缩任务不得覆盖或删除原始审计记录。

### 3.2 稳定 Evidence ID

每个已完成或已中断的 ToolCall 获得稳定 Evidence ID：

```text
evidence_id = hash(session_id, source_run_id, tool_call_id, source_hash)
```

建议契约：

```json
{
  "evidence_id": "evidence_xxx",
  "tool_call_id": "call_xxx",
  "tool": "database_sql_execute",
  "source_session_id": "session_xxx",
  "source_run_id": "run_xxx",
  "source_query_id": "query_xxx",
  "source_hash": "sha256:...",
  "status": "success",
  "output_complete": true,
  "raw_output_ref": {},
  "projection": {},
  "metadata": {
    "historical": true
  }
}
```

`historical` 是相对当前 Run 的属性。在原 Run 中它仍是当前调用；组装给后续 Run 时才标记为历史。

加载时必须有兜底：

```python
historical = metadata.historical or source_run_id != current_run_id
```

### 3.3 Run 终态覆盖

以下终态都必须生成或补齐 Evidence 投影：

- `completed`
- `failed`
- `interrupted`
- `budget_exceeded`
- `network_error`

中断工具必须明确：

```json
{
  "status": "interrupted",
  "output_complete": false,
  "raw_result_available": false
}
```

Run 结束任务必须幂等；若后台投影未完成，新 Run 加载时允许即时生成确定性投影，但不得阻塞恢复完整历史。

---

## 4. DeepAgents 原生大 Tool Result

### 4.1 保留原生机制

继续使用 DeepAgents `FilesystemMiddleware` 的大结果外置：

```text
ToolMessage 超过约 20K tokens
  → backend.write(/large_tool_results/<tool_call_id>)
  → ToolMessage 变为路径 + 头尾预览
  → Agent 使用 read_file(offset, limit) 分段读取
```

不能再用一套自定义“长结果摘要”抢在它之前删除原文。现有 `ToolContextCompactionMiddleware` 已对超过 `20_000 * 4` 字符的结果放行给外层 FilesystemMiddleware，这一边界继续保留并加测试锁定。

### 4.2 修复跨 Run 路径歧义

当前宿主目录为：

```text
.puddingclaw/large_tool_results/<session_id>/<query_id>/<tool_call_id>
```

但模型看到的虚拟路径只有：

```text
/large_tool_results/<tool_call_id>
```

新 Run 的 `/large_tool_results/` 会映射到新的 Query 目录，因此旧路径不能直接用 `read_file` 恢复。

修复要求：

1. Evidence 中记录 `source_query_id` 和稳定 Raw Result locator；
2. 历史投影不再提示模型直接 `read_file` 当前虚拟路径；
3. 统一通过 `read_evidence(evidence_id, offset, limit)` 解析源 Query 目录；
4. 保留现有物理目录，第一版不做破坏性迁移；
5. 后续可评估改成 Session 级物理根目录，但必须先处理 legacy 路径与 tool_call_id 冲突。

Raw locator：

```json
{
  "kind": "deepagents_large_tool_result",
  "session_id": "session_xxx",
  "source_query_id": "query_xxx",
  "tool_call_id": "call_xxx",
  "source_hash": "sha256:..."
}
```

### 4.3 生命周期

DeepAgents 大结果不能在 Run 结束时删除。至少保留到：

- Session 删除；或
- 明确的数据保留策略到期，且没有 active Goal、Todo、Artifact 或 Evidence 引用。

清理前必须扫描 Evidence 引用，禁止仅按 Query 终态删除。

---

## 5. PuddingClaw SQL `result_id` 分页

### 5.1 保持 SQL Result Store 为权威

继续使用现有契约：

```text
database_sql_generate
  → generation_id
database_sql_validate
  → validation_receipt_id
database_sql_execute
  → preview/profile + result_id
database_query_result_page
  → result_id + page + page_size
```

完整行继续由：

```text
backend/data/database-query-results/<result_id>.jsonl
```

保存，不复制到普通 ToolMessage 或通用大结果文件中。

### 5.2 SQL Evidence 必须保留的字段

```json
{
  "kind": "sql_query_result",
  "generation_id": "sql-gen-xxx",
  "validation_receipt_id": "receipt-xxx",
  "sql_sha256": "sha256:...",
  "result_id": "qr-xxx",
  "columns": [],
  "row_count": 18542,
  "preview_count": 100,
  "is_complete": false,
  "artifact_format": "jsonl",
  "artifact_path": "backend/data/database-query-results/qr-xxx.jsonl",
  "expires_at": "...",
  "source_hash": "sha256:..."
}
```

分页 ToolCall 自身仍作为完整历史消息保存，包括：

- `result_id`
- `page`
- `page_size`
- 返回的行范围
- `has_next`
- 页内容 hash
- source Run / Query / ToolCall

### 5.3 不伪造“分页进度”

SQL Result Store 当前 materialize 的是完整 JSONL，分页工具是对已保存完整结果的窗口读取。因此不能把它错误建模成“前 7 页已下载、第 8 页待下载”。

正确语义是：

```text
完整结果是否已 materialize：由 result_id / JSONL 状态决定
模型已经阅读哪些页：由历史 database_query_result_page ToolCall 决定
```

新 Run 可以看到已经读过的页；如果问题需要其他页，再调用分页工具。不能因为上下文压缩而重新执行原 SQL。

### 5.4 TTL 与可恢复性

当前 Result Store 有 TTL。要满足跨 Run 连续性：

1. active Session / Goal 引用的 `result_id` 不得提前清理；
2. `raw_result_available=true` 只能在 JSONL 确实存在且未过期时设置；
3. 过期后必须返回结构化 `expired`，不能把 preview 冒充完整结果；
4. 若产品要求 Session 生命周期内始终可恢复，应把 retention 与 Session/Evidence 生命周期对齐，而不是依赖默认 168 小时；
5. `session_tool_call` fallback 只能恢复当时内联的 preview，不能声称恢复完整 JSONL。

---

## 6. Tool Context 压缩与投影

### 6.1 复用现有实现，不新建平行系统

现有 `ToolContextCompactionService` 已具备：

- 稳定 ToolCall ID 迁移；
- replay 去重；
- `raw_output` / `context_output` 分离；
- `source_hash`；
- `raw_output_ref`；
- 对 `result_id` 的识别；
- 后台候选选择和策略版本。

本方案在其上补充：

- `evidence_id`
- `historical`
- `source_run_id/source_query_id`
- DeepAgents 原生大结果 locator
- SQL Result Store 精确 locator
- 模型投影 profile

### 6.2 禁止覆盖 Raw Result

`ToolResultClearMiddleware` 或 `single_tool_overflow` 可以更新模型使用的 `context_output/output`，但必须先保证：

```text
raw_output 或 raw_output_ref 已持久化
且 source_hash 可验证
```

规则：

- 原始数据写入失败：不允许清理 ToolMessage 原文；
- 摘要失败：保留原文或确定性头尾投影；
- `tool_result_clear` 不再把摘要当作唯一持久化结果；
- UI preview 不能写回 Raw Result；
- `summary_source` 继续保留，用于审计投影来源。

### 6.3 按模型选择投影

同一个 Evidence 可以有多个缓存投影：

```text
主 Agent       → detailed
任务 Router    → minimal
验收模型       → verification
上下文压缩模型 → compaction
```

缓存键：

```text
source_hash + projection_version + context_profile
```

投影不得包含 `time.time()` 等每次调用变化的字段，避免破坏 Prompt Cache。

---

## 7. 跨 Run 协议完整恢复

### 7.1 Context Assembler

统一构建：

```text
System Prompt
+ 历史 UserMessage
+ 历史 AIMessage(tool_calls)
+ 历史 ToolMessage
+ 历史普通 Assistant 文本
+ Todo / Goal / Artifact / Evidence 状态
+ 当前用户消息
+ 当前 Capability Manifest
+ 当前 Permission Manifest
```

历史工具必须恢复为 Provider 合法的配对：

```text
AIMessage(tool_calls=[...])
ToolMessage(tool_call_id=...)
```

不能只把工具结果拼成 Assistant 普通文本。

### 7.2 “完整可见”的定义

完整可见不是把所有 Raw Result 字节无限塞入一次模型调用，而是：

- 所有历史消息在逻辑上存在；
- 所有 ToolCall 与 ToolMessage 协议关系存在；
- 小结果原文进入上下文；
- 大结果以原生外置引用或 SQL `result_id` 进入上下文；
- 模型可以按 `evidence_id` 获取原始细节；
- 不允许静默删除、只留无来源摘要。

Middle Trim / Archive 不能让归档工具消息永久退出模型可恢复范围。Context Assembler 应从 active messages、archive 和 Evidence 索引统一构建预算化视图。

### 7.3 所有续跑路径一致

以下情况都使用同一个 Context Assembler：

- 自动 Goal continuation；
- 用户输入“继续”；
- 中断后再次发送；
- 非 Goal 模式但已有 Todo；
- 用户换一种说法继续同一任务。

`_goal_continuation_prompt` 只能提供额外目标引导，不能成为唯一的历史交接入口。

---

## 8. 历史 Evidence 与当前能力解耦

### 8.1 Skill 激活

历史里出现过：

```text
read_file(/skills/database-analysis/SKILL.md)
database_sql_execute(...)
```

只说明以前读过 Skill 和查过数据库，不代表当前 Run 自动拥有数据库工具。

`ToolsetMiddleware` 必须忽略 `historical=true` 的 AIMessage 和 ToolMessage，不得从历史消息推导 Skill Activation。

当前能力只能来自：

1. 本 Run 成功读取 `SKILL.md`；
2. 同一 Goal revision 下仍有效、Skill hash 未变化的 Goal Activation；
3. Harness 明确配置的无条件工具。

### 8.2 Capability Manifest

给 Agent 明确注入：

```json
{
  "active_tools": ["read_file", "grep", "update_todos"],
  "recommended_inactive_skills": [
    {
      "skill_id": "database-analysis",
      "activation_instruction": "read /skills/database-analysis/SKILL.md"
    }
  ],
  "unavailable_tools": [
    {
      "tool": "database_sql_execute",
      "reason": "skill_not_activated"
    }
  ]
}
```

Manifest 只在能力真实变化时改变，模型可见 JSON 不包含 per-call 时间戳。

### 8.3 Permission Manifest

权限单独表达：

```json
{
  "allowed": [],
  "hitl_required": [],
  "blocked": []
}
```

Agent 必须区分：

- 工具未激活；
- 工具已激活但需要 HITL；
- 已授权可执行；
- 被策略禁止。

最终仍由 Tool Gate 对每次调用实时核验。历史 Permission 消息和历史 Evidence 都不能扩权。

---

## 9. Todo、Goal 与 Artifact 连续性

新 Run 除历史工具外还要恢复：

- 当前未完成 Todo；
- 已完成 Todo 及其 Evidence 引用；
- Goal revision、验收缺口；
- 已交付 Artifact 的正式路径和 hash；
- 中断工具与下一步；
- SQL generation/result_id；
- 用户明确要求继续的任务对象。

Todo 创建需要按规范化内容和作用域去重，避免中断后 12 条变 24 条。

Todo 完成必须引用稳定 Evidence ID，不能复制一份脱离来源的结果 JSON。

---

## 10. UI 与 Trace

历史 ToolCall 恢复给模型，但不能被前端当成本轮执行重放：

- 不重新发送 `tool_start`；
- 不重新发送 `tool_end`；
- 不计入本轮“使用了 N 个工具”；
- 不重复追加 final response；
- Trace 可显示“引用历史 Evidence”；
- UI 必要时提供“上一轮工具记录”入口。

本轮新增 ToolCall 仍按当前 UI 逻辑展示。

---

## 11. 实施修改点

### 11.1 `backend/graph/session_manager.py`

- `load_session_for_agent()` 恢复历史 `tool_calls`，不再只保留普通文本；
- 保留原始 Input/Output 或 Raw Result locator；
- 为后续 Run 生成/读取 `evidence_id`；
- 扩展现有 `context_compaction` metadata；
- 修正 `result_id` 过期后的 fallback 语义；
- Todo 和 Artifact 以结构化状态注入。

### 11.2 `backend/graph/deepagents_manager.py`

- `_build_messages()` 重建历史 `AIMessage + ToolMessage`；
- 添加 `historical/source_run_id/source_query_id/evidence_id`；
- 根据 Raw Result 类型选择 full、compacted、DeepAgents locator 或 SQL `result_id`；
- 历史消息不产生本轮 SSE 工具事件；
- 所有续跑路径使用统一 Context Assembler。

### 11.3 `backend/graph/middlewares/toolset.py`

- 忽略 `historical=true` 的 Skill read 和 ToolMessage；
- 仅认可当前 Run 或有效 Goal Activation；
- Capability Manifest 保持缓存稳定。

### 11.4 `backend/graph/middlewares/tool_context_compaction.py`

- 复用现有 `raw_output_ref/source_hash/context_output`；
- 补充 Evidence metadata 和 projection profile；
- DeepAgents 外置结果不做破坏性摘要；
- SQL 结果只摘要 preview/profile，不复制完整 JSONL；
- 后台 Job 失败可恢复、幂等、不长期停留 `running`。

### 11.5 DeepAgents `/large_tool_results`

- 保留原生外置；
- 增加从 Evidence locator 到源 Query 物理文件的安全解析；
- 不允许历史模型误用当前 Query 的同名虚拟路径；
- 补充 Session 生命周期保留策略。

### 11.6 SQL Result Store

- 保留 `result_id`、JSONL、分页工具；
- Evidence 记录 generation/receipt/result_id/hash；
- retention 与 Session/Goal 引用对齐；
- `raw_result_available` 以真实 artifact 存在性为准。

### 11.7 Evidence Reader

新增安全只读能力：

```text
read_evidence(evidence_id, offset?, limit?, page?, page_size?)
```

内部按 evidence kind 路由：

- 普通 Session ToolCall；
- DeepAgents large tool result；
- SQL `result_id`；
- Artifact / validation receipt。

读取 Evidence 不激活原始 Toolset，不重新联网、不重新查库。

---

## 12. 实施顺序

### P0：恢复 Agent 连续性

1. 跨 Run 恢复协议完整 ToolCall/ToolMessage；
2. 标记 `historical` 和 source Run/Query；
3. Toolset 忽略历史能力；
4. 手动继续和自动续跑共用 Context Assembler；
5. Todo 去重；
6. UI 不重放历史工具事件。

### P1：两类 Raw Result 正确接入

1. 建立稳定 Evidence ID；
2. 接入 DeepAgents `/large_tool_results` locator；
3. 接入 SQL `result_id` / JSONL locator；
4. 修正 TTL 和 fallback 语义；
5. 实现 `read_evidence`；
6. Run 终态投影幂等化。

### P2：Harness 引导和可观测性

1. Capability Manifest；
2. Permission Manifest；
3. Projection profile 与缓存；
4. Tool Context Job 恢复机制；
5. 同 Run System Prompt Hash 变化告警；
6. Evidence 缺失、过期和 hash 不一致 Trace 告警。

---

## 13. 验收矩阵

### 13.1 普通历史工具

- 上轮工具结果在新 Run 中以协议完整消息可见；
- 历史 ToolCall 不产生新 SSE；
- 工具结果失败和错误码不会被摘要丢失；
- 模型不重新执行已完成动作。

### 13.2 DeepAgents 大结果

- 超过 20K tokens 后原文写入 `/large_tool_results`；
- ToolMessage 只保留路径和头尾预览；
- 新 Run 可通过 Evidence ID 读取源 Query 的原文；
- 新 Query 的虚拟路径不会错误指向空文件或同名新文件；
- Run 结束不会删除仍被 Session 引用的大结果。

### 13.3 SQL 分页结果

- `database_sql_execute` 返回 preview + `result_id`；
- JSONL 保存完整 materialized rows；
- 历史中保留已经读取的页和分页参数；
- 新 Run 不重跑 SQL即可继续读其他页；
- `result_id` 过期后明确报告 expired，不把 preview 冒充完整结果；
- SQL Evidence 保留 generation、receipt、hash 和数据源。

### 13.4 能力和权限

- 历史数据库结果可见，但数据库工具不会自动激活；
- 当前 Run 读取 Skill 后工具立即开放；
- 历史网络授权不因消息回放而扩权；
- Capability Manifest 与 Permission Manifest 表达一致；
- Tool Gate 仍是最终权威。

### 13.5 继续与中断

- 自动继续、手动“继续”和换一种说法继续都能恢复相同历史；
- 非 Goal Todo 也能继续；
- `budget_exceeded/network_error/interrupted` 后不会重建 Todo、重查数据库或重新找文件；
- 中断 ToolCall 明确 `output_complete=false`。

### 13.6 Cache 与性能

- 能力不变时 System Prompt Hash 稳定；
- Evidence Projection 命中缓存，不重复调用摘要模型；
- 小结果不被无意义压缩；
- 大结果不会因为历史恢复直接撑爆模型上下文。

---

## 14. 非目标与约束

- 第一版不统一迁移 DeepAgents 大结果文件和 SQL JSONL 到同一物理数据库；
- 不通过恢复历史消息恢复历史权限；
- 不把 Skill Router 分类结果当成工具授权；
- 不要求所有 Raw Result 同时完整内联到单次 LLM 请求；
- 不允许压缩摘要替代原始 Evidence；
- 不静默复活已完成 Goal；连续性由历史、Todo、Artifact 和 Evidence 提供。

---

## 15. 实施与验证记录

本方案已于 2026-07-22 完成 P0、P1、P2 开发，关键落点如下：

- Session active/archive/display 历史统一迁移稳定 ToolCall ID，并生成 Evidence 索引；
- Context Assembler 恢复协议闭合的历史 `AIMessage + ToolMessage`，使用总量预算选择 full、projection 或 pointer；
- 中断调用持久化为 `interrupted`、`output_complete=false`，历史协议回放为 error；
- `read_evidence` 覆盖普通 Session 结果、DeepAgents 大结果、SQL JSONL 和 Evidence Ledger；
- DeepAgents locator 固化源 workspace、Query、实际文件名和原始字节 hash；
- SQL catalog 绑定 Session、ToolCall、Query/Run、TTL、路径和 artifact hash；启动迁移只补缺失 catalog，既有 catalog 不可重签；
- SQL 清理先将 DB owner 状态原子置为 `deleting`，再删除物理文件，成功后删除元数据；失败保留 tombstone 供周期任务重试。应用启动时执行 backfill/cleanup/orphan scavenge；
- Capability Manifest 的 schema hash 覆盖工具名、描述和参数 schema；Permission Manifest 将路径权限表达为参数依赖；
- Toolset Gate 先于 Permission Pipeline，历史 Skill、授权、隐藏 legacy lease 工具均不能扩权；
- 非 Goal Todo 仅在明确继续语义下继承，创建 Todo 按规范化内容幂等；
- Session 删除同步清理 archive、trace、attachment、scratch 和已登记的 DeepAgents 大结果。

对抗式审查分三条独立路径执行：Authority/Capability、Evidence/Raw Result、Continuity/E2E。修复审查发现后再次复核，最终未发现仍可复现的 P0/P1。

验证结果：

- 全量后端正式测试域：1138 passed（启用 Docker Chromium 浏览器闸门）；
- Evidence 聚焦回归：136 passed；审查发现并修复 orphan 命名/发布竞态、code-like
  candidate 校验 fail-open，以及 Materialization Receipt 幂等冲突；
- SQL Result Store 聚焦测试覆盖两阶段发布、Session owner、catalog/path/hash、TTL tombstone、删除失败重试和 orphan grace；
- 最终 E2E：真实 `create_deep_agent` graph 由 manager 在后续 Run 调用 `read_evidence` 读取 SQL 第 2 页，验证历史协议闭合、旧 SQL 不重跑、历史 cumulative updates 不产生 SSE、新 Evidence 调用被持久化。

---

## 16. 最终决策句

> Session 保存完整事实；DeepAgents 原生大结果外置和 PuddingClaw SQL `result_id` 分页分别保持各自权威；Context Assembler 只把适合当前模型预算的投影交给 LLM；Harness 独立声明当前能力和权限；历史 Evidence 永远不能成为扩权依据。
