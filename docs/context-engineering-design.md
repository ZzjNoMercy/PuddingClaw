# 上下文工程（Context Engineering）设计文档

## 1. 设计目标

为 DeepSeek V4（1M 上下文窗口）设计一套分层、可控、前端无感的上下文压缩体系：

1. **单轮效果优先**：LLM 必须先看完当前轮次的原始 tool output，再进行压缩。
2. **多轮可持续**：压缩结果必须持久化，避免同一段历史反复摘要。
3. **前端无感**：用户在前端始终看到完整消息历史，压缩对前端透明。
4. **分级兜底**：从轻到重设置多层压缩，最后才触发全局 reset。

---

## 2. 整体架构

```text
chat.py::_maybe_middle_trim_session  # 入模前：中段摘要 + 归档 + 前端完整轨维护
    ↓
DeepSeekCacheBoundaryMiddleware (observer)      # backend/graph/middlewares/cache.py
    ↓
TailTrimMiddleware               # 运行时兜底裁剪，不持久化 (cache.py)
    ↓
ToolResultClearMiddleware        # 工具结果摘要（主力）(compression.py)
    ↓
SummarizationMiddleware          # 叙述性摘要（中间兜底）(compression.py)
    ↓
CompactionMiddleware             # 全局 reset（最后兜底）(compression.py)
    ↓
agent.py::_build_messages        # 入模前最后一层 0.85*context_window 硬兜底
    ↓
skills_router → task_state → LLM
```

> **废弃组件**：`MessageTrimMiddleware`（原 `compression.py` 中的硬截断实现）已不在默认装配链中，由 `TailTrimMiddleware` 接管。迁移到好得 app 时无需迁移该类。

---

## 3. 阈值配置（1M 上下文）

| 配置项 | 值 | 说明 |
|---|---|---|
| `context_window` | `1000000` | 模型上下文窗口 |
| `cache.middle_trim.max_tokens` | `200000` | 入模前 active messages 超过 200K 时触发中段摘要归档 |
| `cache.middle_trim.head_keep` | `2` | 中段摘要归档时保护前 2 条 active messages |
| `cache.middle_trim.keep_recent` | `30` | 中段摘要归档时保护最近 30 条 active messages，并向前对齐到 user 边界 |
| `cache.middle_trim.summary_budget_chars` | `60000` | 中段摘要输入字符预算 |
| `TailTrimMiddleware.max_tokens` | `200000` | 总 token 超过 200K 时兜底删除运行时 middle slice |
| `TailTrimMiddleware.head_keep` | `2` | 保护前 2 条消息（prefix cache） |
| `TailTrimMiddleware.keep_recent` | `30` | 保护最近 30 条消息 |
| `ToolResultClearMiddleware.keep_recent` | `10` + 轮次边界 | 只摘要最后一条 HumanMessage 之前的旧 ToolMessage，保留最近 10 条完整；当前轮次工具结果不摘要 |
| `ToolResultClearMiddleware.min_summary_length` | `500` 字符 | 原始输出长度 ≥ 500 字符才做 LLM 摘要，避免短输出被越摘要越长 |
| `_summarize_tool_result` 阈值 | `20000` tokens（字符比例估算） | 单条 tool output 估算超过 20K 时立即按 tool 类型摘要；实际使用 `_estimate_tokens` 做字符比例估算，非 DeepSeek tokenizer 精确计数 |
| `SummarizationMiddleware.trigger_tokens` | `200000` | 总 token 超过 200K 时触发叙述性摘要 |
| `SummarizationMiddleware.keep_messages` | `10` | 保留最近 10 条消息 |
| `CompactionMiddleware.trigger_tokens` | `500000` | 总 token 超过 500K 时触发全局 reset |
| `CompactionMiddleware.keep_recent` | `8` | 保留最近 8 条完整消息 |
| `CompactionMiddleware.compact_budget_tokens` | `120000` | 摘要输入总预算，按消息类型动态截断 |

---

## 4. 各中间件行为

### 4.1 Middle Trim Session Preflight（中段摘要归档）

- **位置**：`api/chat.py::_maybe_middle_trim_session()`，在 `load_session_for_agent()` 之前执行。
- **触发条件**：
  1. `cache.middle_trim.enabled=true`。
  2. 当前 `session.json.messages`（active messages）估算 token > `max_tokens`（默认 200K）。
  3. active messages 数量 > `head_keep + keep_recent`。
- **裁剪范围**：
  1. 保留前 `head_keep` 条 active messages。
  2. 保留最近 `keep_recent` 条 active messages。
  3. tail 起点如果落在 assistant/tool 中间，则向前移动到上一条 `role=user`，确保最近上下文从用户请求开始。
  4. 中间 `[head_keep : tail_start)` 作为 middle span。
- **行为**：
  1. 将 middle span 转成摘要输入，包含 message content、tool call input、tool output。
  2. 调用 LLM 生成“任务状态摘要”，重点保留已完成/失败/未完成、关键工具结果、文件路径、版本、错误和旧任务边界。
  3. 摘要成功后才归档；摘要失败则跳过本次 middle trim，不修改 session。
  4. middle 原文写入 `sessions/archive/session-xxx_middle_*.json`。
  5. `session.json.messages` 移除 middle span，只保留 head + tail active messages。
  6. 摘要按时间追加到 `session.json.middle_trim_context`。
  7. 首次 middle trim 时创建 `display_messages`，保存用户可见完整历史；后续 `save_message()` 同步追加到该字段。
- **持久化**：持久化 `middle_trim_context`、`display_messages` 和 archive 文件。
- **影响**：LLM 不再看到残缺中段；前端 `/history` 仍通过 `display_messages` 返回完整原始消息历史。

### 4.2 TailTrimMiddleware

- **位置**：`backend/graph/middlewares/cache.py`（**注意**：不在 `compression.py` 中）。
- **触发条件**：`state["messages"]` 总 token > 200K，且消息数 > `head_keep + keep_recent`（即 > 32）。
- **行为**：作为运行时兜底删除 middle slice。tail 起点同样向前对齐到 `HumanMessage`；middle 中的 `HumanMessage` 也会删除，避免留下“用户请求还在、完成证据没了”的残缺历史。AI+Tool 配对仍保持原子删除，跨保护区边界的配对会被保护。
- **持久化**：不持久化，只改运行时 state。
- **影响**：正常情况下应由 Middle Trim Session Preflight 先完成摘要归档；TailTrim 只作为最后的 cache-friendly 兜底。

### 4.3 ToolResultClearMiddleware

- **触发条件**：
  1. 只考虑**最后一条 HumanMessage 之前**的 ToolMessage。
  2. 这些 ToolMessage 数量 > `keep_recent`（默认 10 条）**才会进入处理循环**。
  3. 在候选 ToolMessage 中，只有原始输出长度 ≥ `min_summary_length`（默认 500 字符）的最旧消息才会被 LLM 摘要替换；若候选全为短输出，则本次不修改。
- **行为**：
  1. 满足长度门槛的最旧 ToolMessage 被 LLM 摘要替换。
  2. 摘要结果加 `[摘要] ` 前缀。
  3. 已带 `[摘要] ` 前缀的 ToolMessage 不再二次摘要。
  4. 短输出（< 500 字符）即使位于历史区域也保留原文，避免无意义摘要。
- **持久化**：触发时 emit `tool_result_clear` 事件，`chat.py` 收到后按 `tool_call_id` 把摘要写回对应 tool_call 的 `output`，并在 `tool_calls` 对象上标记 `summary_source: "tool_result_clear"`。
- **前缀**：固定为 `[摘要] `（含一个空格）。迁移到好得 app 时保持该前缀和 `summary_source` 字段不变。
- **影响**：保证当前轮次所有 ToolMessage 完整，只压缩上一轮及更早的历史；短工具输出不被污染。

### 4.4 _summarize_tool_result（单条超长兜底）

- **位置**：`backend/graph/agent.py`（**注意**：不在 `chat.py` 中）。
- **触发条件**：单条 tool output 估算 token > 20000（使用 `_estimate_tokens` 字符比例估算，非 DeepSeek tokenizer 精确计数）。
- **行为**：按 tool 类型选择 prompt，LLM 生成结构化摘要。
- **持久化**：替换当前 segment 中的 tool output，最终写入 session，并在 `tool_calls` 对象上标记 `summary_source: "single_tool_overflow"`。
- **影响**：防止单条 patent/文档 output 直接撑爆上下文。

> **摘要来源标记**：两种摘要机制都会在 `session.json` 的 `tool_calls[i]` 中保留 `summary_source` 字段，便于排查是"单条超长"还是"历史清理"产生的摘要。
> **禁止硬截断持久化**：`tool_end` 事件中的 `output` 必须保留完整工具输出或 `single_tool_overflow` 摘要；前端 SSE 展示可使用 `output_preview`（例如前 2000 字符），但不能把 preview 写入 `session.json`。

### 4.5 SummarizationMiddleware

- **触发条件**：`state["messages"]` 总 token > 200K。
- **行为**：把超窗口的历史压缩成一段叙述性中文摘要，前缀 `[历史对话摘要]`。
- **持久化**：不持久化，只改运行时 state。
- **影响**：比 Compaction 温和，保留更多上下文结构。

### 4.6 CompactionMiddleware

- **触发条件**：`state["messages"]` 总 token > 500K。
- **行为**：
  1. 保留首条 SystemMessage。
  2. 保留最近 8 条完整消息。
  3. 其余消息按 `compact_budget_tokens=120000` 预算动态截断后生成全局摘要：
     - `HumanMessage` / `AIMessage`：完整保留
     - `ToolMessage` ≤ 2K tokens：完整保留
     - `ToolMessage` 2K-20K tokens：保留约 5K 字符
     - `ToolMessage` > 20K tokens：保留约 10K 字符
     - 超出预算时截断并追加 `[更多历史已省略]`
  4. 摘要前缀 `[历史对话摘要]`（代码外层强制包装）。
     - **注意**：`COMPACTION_SUMMARY_PROMPT` 内部模板也要求 LLM 以 `[对话摘要]` 为前缀，因此实际摘要中可能出现 `[历史对话摘要][对话摘要]...` 的双前缀现象。迁移时建议统一 prompt，只保留一个前缀。
  5. 清空旧 messages，重置为 `[System, 摘要, 最近 8 条]`。
- **持久化**：触发时 emit `compaction` 事件，`chat.py` 调用 `compress_history` 归档旧消息并写入 `compressed_context`。
- **影响**：彻底的 reset，但前端通过 archive 合并仍能看到完整历史。

### 4.7 `_build_messages` 入模前最后一层兜底

- **位置**：`backend/graph/agent.py::_build_messages()`。
- **触发条件**：按字符比例估算 `messages` 总 token > `context_window * 0.85`（即 850K）。
- **行为**：直接丢弃前半段消息，保留后半段，确保不超出模型上下文硬上限。
- **持久化**：不持久化，只改运行时 state。
- **影响**：这是进入 LLM 之前的最后一道保险丝，正常情况下不应触发；若触发说明前面所有压缩层都未生效。

---

## 5. 持久化策略

### 5.1 session.json 结构

```json
{
  "title": "...",
  "created_at": 1234567890,
  "updated_at": 1234567999,
  "messages": [
    // LLM active messages：未被 middle trim / compaction 移出活跃上下文的消息
    {
      "role": "assistant",
      "content": "...",
      "tool_calls": [
        {
          "tool": "patsnap_search",
          "input": "...",
          "id": "call_xxx",
          "output": "[摘要] ...",
          "summary_source": "tool_result_clear"  // 或 "single_tool_overflow"，原始输出无此字段
        }
      ]
    }
  ],
  "display_messages": [
    // 用户可见完整消息历史；首次 middle trim 时创建，后续 save_message 同步追加
  ],
  "compressed_context": "[历史对话摘要] ...",
  "middle_trim_context": "[中段裁剪摘要 2026-06-18 14:32:10]\narchive: session-xxx_middle_...\nmessages: 16\nrange: active messages[2:18]\n摘要：\n- ...",
  "context_usage_peak": 523456
}
```

- `compressed_context` **只由 `CompactionMiddleware` 触发后写入**，是全局历史摘要；`ToolResultClearMiddleware` 的摘要仅写入对应 `tool_call.output`，不会追加到 `compressed_context`。
- `middle_trim_context` **只由 Middle Trim Session Preflight 写入**，是被移出 active messages 的中段任务状态摘要。多次触发时按时间追加，用 `---` 分隔。
- `display_messages` 是用户可见轨。它不是 LLM active context；存在时 `/history` 直接返回它，确保前端完整历史无感。
- `tool_calls` 中的 `summary_source` 标记摘要来源：
  - `"single_tool_overflow"`：单条 output > 20K tokens，由 `_summarize_tool_result` 实时摘要；
  - `"tool_result_clear"`：历史 ToolMessage 过多，由 `ToolResultClearMiddleware` 清理；
  - 无该字段：原始输出，未被摘要。

`context_usage_peak` 记录该 session 运行时真实的 token 用量峰值。由于 session 中的 tool output 可能被摘要或截断，`context_usage_peak` 比静态统计更能反映 LLM 实际消耗的上下文。

### 5.2 archive 目录

```
sessions/
├── session-xxx.json
└── archive/
    ├── session-xxx_1778000000.json
    └── session-xxx_1778000100.json
```

归档文件分两类：

1. **Compaction 归档**：`archive/session-xxx_时间戳.json`，保存被全局压缩移出的前缀消息。
2. **Middle Trim 归档**：`archive/session-xxx_middle_时间戳.json`，保存被中段摘要移出 active messages 的原始 middle span，并带有：

```json
{
  "archive_type": "middle_trim",
  "range": {"start_idx": 2, "end_idx": 18},
  "messages": [...],
  "summary": "...",
  "metadata": {"reason": "middle_trim"}
}
```

### 5.3 前端加载

- `/api/sessions/{id}/history` 调用 `load_session()`。
- 如果 `session.json.display_messages` 存在，`load_session()` 直接返回它。
- 如果没有 `display_messages`，`load_session()` 自动合并 `archive/` 中的所有归档消息 + `session.json` 中的当前消息，用于兼容旧 session 和 compaction-only session。
- 前端始终看到完整历史。

### 5.4 LLM 加载

- `load_session_for_agent()` 注入：
  1. `compressed_context`（如果存在）：`[历史对话摘要]`
  2. `middle_trim_context`（如果存在）：代码会额外包裹一段引导语（如"以下是被移出活跃上下文的中段历史摘要，只用于理解历史完成情况，不代表当前任务结果..."），再附上 `[中段历史摘要]` 内容
  3. `session.json.messages` active messages
- LLM 看到压缩视图，控制 token。
- `display_messages` 不进入 LLM context，只用于前端展示。

---

## 6. 按 Tool 类型定制摘要

| Tool | 摘要策略 |
|---|---|
| `patsnap_search` | 提取专利号、标题、申请人、申请日、法律状态、核心摘要（每条 100 字以内） |
| `patsnap_fetch` | 结构化提取：专利号、标题、申请人、发明人、申请日、公开日、法律状态、摘要、关键权利要求、附图说明 |
| `terminal` | 保留关键结论、数字、来源 |
| `read_file` | 保留文件核心内容 |
| `execute_skill` | 保留技能指引的核心步骤 |
| 其他 | 通用一句话摘要 |

---

## 7. 数据流示例

### 7.1 单轮专利查询

```text
user: 查问界折叠方向盘专利
  ↓
LLM 调用 patsnap_search → 返回 20 条专利（可能 5K-20K tokens）
  ↓
LLM 调用 patsnap_fetch → 返回 3 条专利详情（可能 50K+ tokens，含图片 base64）
  ↓
单条 output > 20K → _summarize_tool_result 摘要（结构化提取专利字段）
  ↓
LLM 基于完整/摘要后的 tool output 生成回复
  ↓
chat.py 保存：user 消息 + assistant 消息 + 摘要后的 tool_calls
```

### 7.2 多轮累积后 ToolResultClear 触发

```text
第 N 轮结束时：已有 12 条历史 ToolMessage
  ↓
第 N+1 轮 LLM 调用前
  ↓
ToolResultClearMiddleware 检查
  ↓
最后一条 HumanMessage 之前有 12 条 ToolMessage > 10
  ↓
只对其原始输出 ≥ 500 字符的最旧 ToolMessage 做 LLM 摘要，加 [摘要] 前缀
  ↓
emit tool_result_clear 事件（携带 tool_call_id、summary、summary_source）
  ↓
chat.py 调用 session_manager.update_tool_call_output 直接更新 session.json 中对应 tool_call 的 output
  ↓
下次加载时，该 tool_call.output 已是 [摘要] 版本，summary_source="tool_result_clear"，不再二次摘要
```

### 7.3 Compaction 触发

```text
总 token > 500K
  ↓
CompactionMiddleware 触发
  ↓
保留 System + 最近 8 条消息
  ↓
其余消息生成全局摘要
  ↓
emit compaction 事件
  ↓
chat.py 调用 compress_history
  ↓
旧消息归档到 archive/session-xxx_时间戳.json
  ↓
摘要写入 compressed_context
  ↓
session.json 中 messages 只保留最近 8 条
  ↓
前端 /history 合并 archive 后仍看到完整历史
  ↓
LLM load_session_for_agent 看到 [摘要] + 最近 8 条
```

### 7.4 Middle Trim 触发

```text
第 N+1 轮请求进入 chat.py
  ↓
_maybe_middle_trim_session(session_id)
  ↓
读取当前 active messages（不含 display_messages）
  ↓
估算 token > cache.middle_trim.max_tokens
  ↓
保留 head_keep=2；保留最近 keep_recent=30，并把 tail_start 向前对齐到 role=user
  ↓
middle span 生成任务状态摘要
  ↓
session_manager.middle_trim_history()
  ↓
middle 原文归档到 archive/session-xxx_middle_时间戳.json
  ↓
摘要追加到 middle_trim_context
  ↓
session.json.messages 移除 middle，仅保留 head + tail active messages
  ↓
display_messages 保存并持续追加完整用户可见历史
  ↓
load_session_for_agent 注入 [中段历史摘要] + active messages
```

**关键约束**：

- 摘要失败时不裁剪、不归档，避免破坏历史。
- 摘要只影响 LLM context，不能作为普通 assistant 消息展示。
- 归档是审计备份；用户历史以 `display_messages` 为准保持完整顺序。

---

## 8. 前端 Token 统计修复

### 8.1 运行时峰值记录

每次 `context_usage` 事件触发时，`chat.py` 把 `used_tokens` 峰值写入 session.json 的 `context_usage_peak` 字段：

```python
ws.session_manager.update_context_usage_peak(session_id, used_tokens)
```

### 8.2 `/api/tokens/session/{id}` 优先返回峰值

`/api/tokens/session/{id}` 先静态统计 content + tool_calls，如果存在 `context_usage_peak` 且更大，则使用峰值：

```python
context_usage_peak = ws.session_manager.get_context_usage_peak(session_id)
if context_usage_peak > system_tokens + message_tokens:
    message_tokens = context_usage_peak - system_tokens

used_tokens = system_tokens + message_tokens
compaction_trigger = get_compaction_trigger_tokens()
```

### 8.3 前端显示

前端进度条的分母使用 `compaction_trigger`（默认 500K），而不是模型上下文窗口（1M）：

```vue
{{ (store.contextUsage.used / 1000).toFixed(1) }}k / {{ (store.contextUsage.total / 1000).toFixed(0) }}k
```

这样用户可以直观看到：
- 当前 session 已用多少 token
- 接近 500K 时会触发 Compaction
- 不需要关心 1M 的模型上下文上限

`context_usage` 事件返回 `total_tokens = compaction_trigger`；`/api/tokens/session/{id}` 返回真实的 `total_tokens = system_tokens + message_tokens`，并额外返回 `compaction_trigger` 字段供前端进度条使用。前端应使用 `compaction_trigger` 作为进度条分母，而不是 `total_tokens`。

---

## 9. 关键设计原则重申

1. **当前轮次完整**：通过轮次边界判断，`ToolResultClearMiddleware` 不摘要当前轮次 tool output。
2. **摘要结果持久化**：避免多轮重复摘要同一内容。
3. **前端无感**：完整历史通过 `display_messages` 或 archive 合并展示，压缩只影响 LLM 视图。
4. **中段不残缺**：中段不能只保留用户请求而删除完成证据；正式路径用 middle summary，TailTrim 兜底也删除完整 middle slice。
5. **分级兜底**：MiddleTrimSession → ToolResultClear → TailTrim → Summarization → Compaction → `_build_messages` 0.85 兜底，从轻到重。`MessageTrimMiddleware` 已废弃，不再使用。
6. **类型化摘要**：专利、文档、技能等不同 tool 使用不同摘要策略。

---

## 10. 测试验证集

### 10.1 测试集 1：基线（不触发压缩）

**Query**: `今天几号了？`

**预期**:
- 只调用 `get_date` 一个 tool
- 不触发任何压缩
- 验证基本对话流程正常

---

### 10.2 测试集 2：单条超长 tool output（触发 `_summarize_tool_result`）

**Query**:
```
查一下专利 CN120942418A 的完整信息，包括权利要求、附图和所有法律状态。
```

**预期**:
- 调用 `patsnap_fetch`
- 返回内容含图片 base64，单条可能 >20K tokens
- 触发 `_summarize_tool_result`
- 最终 session 中该 tool_call.output 应该是 `[摘要] ...`
- LLM 仍能基于摘要给出有效回复

**验证命令**:
```bash
kubectl exec <pod> -- grep -i "summarize_tool_result\|工具结果摘要" /var/log/supervisor/agent_backend_error.log
```

---

### 10.3 测试集 3：多轮 tool 调用（触发 `ToolResultClearMiddleware`）

在同一 session 中连续发送：

**Round 1**:
```
查一下比亚迪折叠方向盘相关专利。
```

**Round 2**:
```
再查一下华为、小米、蔚来的折叠方向盘专利。
```

**Round 3**:
```
再查一下供应商毅赫、均胜的折叠方向盘专利。
```

**Round 4**:
```
再查一下特斯拉、理想、小鹏的隐藏式方向盘专利。
```

**Round 5**:
```
对比一下这些专利的技术路线。
```

**预期**:
- 每轮产生多个 `patsnap_search` + `patsnap_fetch`
- 累计 ToolMessage > 10 条
- 触发 `ToolResultClearMiddleware`
- 原始输出 ≥ 500 字符的最旧 tool output 变成 `[摘要] ...`，并带有 `summary_source="tool_result_clear"`
- 原始输出 < 500 字符的 tool output 保留原文，不带 `[摘要]` 前缀
- 当前轮次（Round 5）的工具结果保持完整

**验证命令**:
```bash
kubectl exec <pod> -- grep -i "ToolResultClear\|compressed.*tool messages" /var/log/supervisor/agent_backend_error.log
kubectl exec <pod> -- python3 -c "
import json
f='/app/agent-backend/workspace/<user_id>/aisight/sessions/session-xxx.json'
d=json.load(open(f))
for m in d['messages']:
    for tc in m.get('tool_calls', []):
        out=tc.get('output','')
        src=tc.get('summary_source')
        if out.startswith('[摘要]'):
            assert src in ('tool_result_clear','single_tool_overflow'), f'missing summary_source: {tc}'
        print(f\"{tc['tool']:20s} src={src or 'None':22s} len={len(out):5d} prefix={out[:20]!r}\")
"
```

---

### 10.4 测试集 4：中段历史累积（触发 Middle Trim）

在同一 session 中持续进行多个中等长度任务，直到 active messages 超过 `cache.middle_trim.max_tokens`。测试环境可临时把 `cache.middle_trim.max_tokens` 调低以快速触发。

**预期**:
- 触发 `_maybe_middle_trim_session`
- `sessions/archive/` 出现 `session-xxx_middle_*.json`
- `session.json.middle_trim_context` 追加一块 `[中段裁剪摘要 ...]`
- `session.json.messages` 只保留 head + tail active messages
- `session.json.display_messages` 仍包含完整原始历史，且后续新消息继续追加
- 前端 `/history` 返回完整历史，不展示 middle summary 为聊天消息
- LLM context 中出现 `[中段历史摘要]`，但不包含被裁剪 middle 的原始消息

**验证命令**:
```bash
python3 -c "
import json, glob
f='/app/agent-backend/workspace/<user_id>/aisight/sessions/session-xxx.json'
d=json.load(open(f))
print('active messages:', len(d.get('messages', [])))
print('display messages:', len(d.get('display_messages', [])))
print('middle_trim_context len:', len(d.get('middle_trim_context','')))
print('middle archives:', glob.glob('/app/agent-backend/workspace/<user_id>/aisight/sessions/archive/session-xxx_middle_*.json')[:5])
"
```

---

### 10.5 测试集 5：超长上下文累积（触发 `CompactionMiddleware`）

先跑测试集 3 累积上下文，然后继续：

**Round 6**:
```
把上面所有专利的详情都列出来，包括申请人、申请日、法律状态、技术摘要。
```

**Round 7**:
```
基于这些专利，分析一下问界折叠方向盘的技术路线和供应链布局。
```

**Round 8**:
```
再查一下问界 M9、M7、M5 的整车参数配置，包括方向盘调节方式。
```

**Round 9**:
```
结合商情和专利，写一份问界折叠方向盘的竞争分析报告。
```

**预期**:
- 多轮累积后总 token > 500K
- 触发 `CompactionMiddleware`
- 旧消息归档到 `sessions/archive/session-xxx_*.json`
- `session.json` 中 `compressed_context` 被写入摘要
- 前端 `/history` 仍返回完整历史

**验证命令**:
```bash
kubectl exec <pod> -- grep -i "CompactionMiddleware\|compacted.*messages" /var/log/supervisor/agent_backend_error.log
kubectl exec <pod> -- ls /app/agent-backend/workspace/<user_id>/aisight/sessions/archive/
kubectl exec <pod> -- python3 -c "
import json
f='/app/agent-backend/workspace/<user_id>/aisight/sessions/session-xxx.json'
d=json.load(open(f))
print('messages:', len(d['messages']))
print('compressed_context len:', len(d.get('compressed_context','')))
"
```

---

### 10.6 测试集 6：当前轮次工具完整性（验证轮次边界）

**Query**:
```
请帮我完成以下任务：
1. 查问界折叠方向盘的商情信息（供应商、量产时间、技术方案）
2. 查相关专利 TOP 20
3. 对 TOP 5 专利逐条查详情
4. 总结技术趋势和供应链格局
```

**预期**:
- 单轮内可能产生 10+ 条 tool 调用
- 但**当前轮次**内所有 tool output 保持完整
- 不会因为 ToolResultClear 而摘要当前轮次工具
- 只有历史轮次的 tool 才会被摘要

**验证点**:
- 查看最终回复是否完整引用了当前轮次所有专利详情
- 检查日志中 ToolResultClear 摘要的是否都是历史 tool

---

### 10.7 通用验证命令

```bash
# 进入 pod
WEB_POD=$(kubectl get pod -n magic-mirror-prod -l app.zeekrlife.com/service=magic-mirror-web -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it $WEB_POD -n magic-mirror-prod -- bash

# 实时看日志
tail -f /var/log/supervisor/agent_backend_error.log | grep -E "ToolResultClear|CompactionMiddleware|summarize_tool_result|context_usage"

# 查看某 session 结构
python3 -c "
import json
f='/app/agent-backend/workspace/<user_id>/aisight/sessions/session-xxx.json'
d=json.load(open(f))
print('messages:', len(d['messages']))
print('display_messages:', len(d.get('display_messages', [])))
print('compressed_context len:', len(d.get('compressed_context','')))
print('middle_trim_context len:', len(d.get('middle_trim_context','')))
for i,m in enumerate(d['messages']):
    tcs=m.get('tool_calls',[])
    if tcs:
        print(f'[{i}] {m[\"role\"]} tool={tcs[0][\"tool\"]} output_prefix={tcs[0][\"output\"][:20]!r}')
"

# 查看 archive
ls -la /app/agent-backend/workspace/<user_id>/aisight/sessions/archive/
```

---

### 10.8 推荐测试顺序

1. **测试集 1**：确认基本流程正常
2. **测试集 2**：确认单条超长摘要生效
3. **测试集 3**：确认 ToolResultClear 跨轮次摘要
4. **测试集 6**：确认当前轮次完整性
5. **测试集 4**：确认 Middle Trim 中段摘要 + 前端完整历史
6. **测试集 5**：确认 Compaction reset + 归档

---

## 11. 已知问题修复

### 11.1 python_repl 工具中 matplotlib 在异步线程崩溃

**现象**：
```text
UserWarning: Starting a Matplotlib GUI outside of the main thread will likely fail.
RuntimeError: main thread is not in main loop
Tcl_AsyncDelete: async handler deleted by the wrong thread
```

**原因**：`python_repl` 工具在执行 matplotlib 绘图时使用了默认的 GUI backend（TkAgg），而 agent 运行在异步线程中，导致 GUI 事件循环崩溃。

**修复**：在 `tools/python_repl_tool.py` 中强制 matplotlib 使用非交互式 backend：

```python
# 模块级别：在 matplotlib 首次导入前设置环境变量
os.environ.setdefault("MPLBACKEND", "Agg")

# _run 中：prepend 代码确保 backend 设置生效
safe_query = (
    "import matplotlib\n"
    "try:\n"
    "    matplotlib.use('Agg', force=True)\n"
    "except Exception:\n"
    "    pass\n\n" + query
)
```

这样 matplotlib 不会尝试启动 GUI，避免 agent 崩溃。
