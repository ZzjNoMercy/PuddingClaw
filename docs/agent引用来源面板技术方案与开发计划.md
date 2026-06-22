# Agent 引用来源面板技术方案与开发计划

> 目标：在 PuddingClaw 的 Agent 对话界面右侧增加“引用来源”面板，运行过程中动态展示工具与 Skill 检索到的文档，并将最终答案中的具体论述与真实来源绑定。引用信息必须可追溯、可校验、可持久化，并能用于后续 Markdown、Word、PDF 等文档导出。

更新时间：2026-06-22

实施进度：2026-06-22 已完成结构化来源链路、工具结束后 `source_found`、会话持久化、右侧来源面板 MVP、最终引用标记校验与正文/来源双向定位。工具执行过程中的逐条 custom stream 和文档导出尚未实施。

## 1. 背景与问题定义

PuddingClaw 当前已经具备以下基础能力：

- Agent 通过 `tool_start`、`tool_end`、`token` 等 SSE 事件向前端流式传递执行状态。
- `search_knowledge_base` 可以从本地知识库检索内容。
- 前端 `RetrievalCard` 可以展示 RAG 检索片段。
- 右侧已有 Inspector 面板和可调整宽度的工作台布局。

但现有链路还不能形成可信引用：

1. `search_knowledge_base` 将 LlamaIndex 响应直接转换为字符串，文件名、页码、chunk、原文位置等元数据会丢失。
2. `tool_end` 主要传递 `output` 文本，没有独立的结构化来源字段。
3. 前端 `RetrievalResult` 只有 `text`、`score`、`source`，不能稳定标识文档和引用位置。
4. 当前检索卡片只能说明“检索过什么”，不能证明“答案中的哪一句由哪个来源支持”。
5. 工具输出可能被截断、预览或压缩，引用数据如果混在 `output` 文本中，会随上下文维护一起受损。

因此，本功能不是简单地把检索结果从消息下方移动到右侧，而是需要建立独立的“来源注册、引用绑定、流式展示、持久化和导出”数据链。

## 2. 设计目标

### 2.1 核心目标

- Agent 运行时，右侧面板实时增加新发现的文档来源。
- 最终答案中的 `[1]`、`[2]` 等引用角标可以定位到右侧来源。
- 点击正文引用时，高亮并滚动到对应来源卡片。
- 点击来源卡片时，高亮正文中引用该来源的内容。
- 明确区分“检索到的资料”和“最终采用的引用”。
- 引用数据独立于工具输出文本，不受工具结果压缩和预览截断影响。
- 会话刷新后可以恢复来源与引用关系。
- 后续导出 Markdown、Word、PDF 时可以生成脚注或参考资料列表。

### 2.2 非目标

第一期不包含：

- 对任意外部网页进行永久归档。
- 自动判断来源在法律、医学等领域是否权威。
- PDF 原文全文预览和复杂坐标级高亮。
- 跨会话建立全局文献管理库。
- 用模型生成的 URL 或文件名替代工具返回的真实来源元数据。

## 3. 行业实现方式

主流 Agent/RAG 产品通常采用“来源注册表 + 生成内容引用映射”，而不是只要求模型在 Markdown 中自行拼接链接。

### 3.1 OpenAI File Search 模式

OpenAI File Search 在生成内容之外返回 `annotations`，引用项包含 `file_citation`、文本位置、`file_id` 和文件名。其特点是引用作为响应协议的一部分存在，而不是依靠解析模型自由生成的 Markdown。

参考：<https://platform.openai.com/docs/guides/tools-file-search>

### 3.2 Anthropic Citations 模式

Anthropic Citations 将引用作为结构化内容返回，并通过文档索引及原文位置把生成答案与输入文档绑定。该模式适合需要精确定位引用文本区间的场景。

参考：<https://platform.claude.com/docs/en/build-with-claude/citations>

### 3.3 Google Grounding 模式

Google Gemini Grounding 将数据拆成两部分：

- `groundingChunks`：检索来源注册表。
- `groundingSupports`：生成内容与来源之间的具体映射。

这种分层最适合作为 PuddingClaw 的整体模型：来源只注册一次，正文引用通过稳定 ID 关联来源。

参考：<https://cloud.google.com/vertex-ai/generative-ai/docs/grounding/grounding-with-google-search>

### 3.4 PuddingClaw 推荐组合

采用“Google 式来源注册表 + OpenAI/Anthropic 式正文 annotation”的组合：

1. 工具或 Skill 负责返回真实来源。
2. 后端为来源生成稳定的 `source_id` 并去重。
3. 模型只能引用已注册的 `source_id`。
4. 后端校验引用是否合法并生成最终编号。
5. 前端根据结构化引用渲染角标和右侧来源面板。

## 4. 总体架构

```text
知识库 / Web / Skill
        │
        ▼
结构化工具结果
answer_context + sources[]
        │
        ├── source_found SSE ──────► 右侧来源面板实时更新
        │
        ▼
模型基于 source_id 生成回答
markdown + citation_refs[]
        │
        ▼
后端校验、编号、持久化
        │
        ├── token / citation_delta ─► 正文流式渲染
        └── citations_finalized ────► 已引用/仅检索状态定稿
```

### 4.1 数据职责边界

| 层级 | 职责 | 不负责 |
| --- | --- | --- |
| 工具/Skill | 返回真实文档元数据、引用片段和检索分数 | 决定最终答案编号 |
| Agent/模型 | 根据已注册来源生成回答并声明使用的 `source_id` | 创造不存在的来源 |
| 后端协议层 | 去重、校验、编号、持久化、发送 SSE | 仅从 Markdown 猜测引用 |
| 前端 | 动态展示、联动、高亮、过滤 | 修改引用事实 |
| 导出层 | 将同一引用模型渲染成脚注或参考资料 | 重新调用模型生成引用 |

### 4.2 工具结果适配层

实现采用独立的 `ToolResultAdapter`，并在所有 `ToolMessage` 进入 Agent 的统一边界调用，而不是实现为 LangChain `AgentMiddleware`。

原因：

- 适配职责是将工具返回值规范化为 `answer_context + sources[]`，不负责模型调用策略、权限、重试或上下文裁剪。
- ToolMessage 边界可以同时覆盖本地工具、`execute_skill`、Skill 内 terminal/curl、Web Search 和 MCP 工具。
- 不依赖特定 LangChain middleware API，后续框架升级或迁移时更稳定。

适配优先级：

1. PuddingClaw 标准结构化 envelope，可信度最高。
2. 通用 JSON：递归识别 `items/results/sections` 以及 `title/url/snippet/summary/sourceUrl` 等常见字段；覆盖 AI HOT、Tavily 和常见 MCP Web Search。
3. `fetch_url`：以工具输入中的请求 URL 作为唯一页面来源，避免把页面内的所有外链误认为证据来源。
4. Markdown 链接和裸 URL：作为兼容兜底，进入“已检索”；只有最终答案明确引用后才进入“已引用”。
5. 纯文本：不生成来源，不允许猜测 URL。

隐式适配必须通过“来源资格门”：

- `read_file`、`write_file`、`execute_skill` 的说明文本不做 JSON/Markdown URL 自动提取，避免把 SKILL.md、源码、Prompt、配置文件里的示例链接误判为检索结果。
- `terminal`、`python_repl` 只有在输入同时包含 HTTP URL 和 `curl/wget/httpie/requests/urllib/fetch` 等网络调用信号时，才允许隐式提取来源。
- 名称明确包含 `search/fetch/browse/research/retrieve/tavily/news/knowledge/web` 等语义的工具允许隐式适配，覆盖常见 Web Search 和 MCP 搜索工具。
- 标准结构化 envelope 不受资格门限制，因为它是工具主动声明的来源协议。

代码位置：

```text
backend/graph/tool_result_adapter.py
backend/graph/citations.py
backend/graph/agent.py
```

## 5. 核心数据模型

### 5.1 工具结构化结果

工具结果应保留供模型阅读的上下文，同时将来源作为独立字段传递：

```json
{
  "answer_context": "供模型生成答案使用的检索内容",
  "sources": [
    {
      "source_id": "src_01HXYZ",
      "title": "Agent 架构设计文档",
      "uri": "/knowledge/agent-architecture.pdf",
      "document_id": "doc_123",
      "chunk_id": "chunk_17",
      "source_type": "knowledge_base",
      "page": 12,
      "quote": "Agent 在工具执行完成后……",
      "score": 0.87,
      "tool_call_id": "call_abc",
      "metadata": {}
    }
  ]
}
```

字段要求：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `source_id` | 是 | 会话内稳定、不可由模型自由生成 |
| `title` | 是 | 前端展示名称 |
| `uri` | 否 | 本地路径或外部 URL |
| `document_id` | 建议 | 文档级去重 |
| `chunk_id` | 建议 | 精确定位检索片段 |
| `source_type` | 是 | `knowledge_base`、`web`、`file`、`skill` 等 |
| `page` | 否 | PDF/文档页码 |
| `quote` | 建议 | 最小必要原文证据 |
| `score` | 否 | 检索相关度，只用于辅助展示 |
| `tool_call_id` | 是 | 追踪来源来自哪次工具调用 |

### 5.2 最终引用映射

```json
{
  "citation_id": "cite_01HXYZ",
  "source_id": "src_01HXYZ",
  "display_index": 1,
  "start": 18,
  "end": 34,
  "quoted_text": "应保留工具调用记录",
  "status": "verified"
}
```

推荐最终存储字符区间 `start/end`。第一期如果流式字符位置实现成本较高，可以先采用正文标记：

```markdown
Agent 应保留工具调用记录。[^src_01HXYZ]
```

生成完成后由后端转换为稳定的 `citation_id`、`display_index` 和文本位置。

### 5.3 前端状态

```ts
interface SourceRecord {
  sourceId: string;
  title: string;
  uri?: string;
  documentId?: string;
  chunkId?: string;
  sourceType: "knowledge_base" | "web" | "file" | "skill";
  page?: number;
  quote?: string;
  score?: number;
  toolCallId: string;
  status: "retrieved" | "cited" | "unused" | "error";
}

interface CitationRef {
  citationId: string;
  sourceId: string;
  displayIndex: number;
  start?: number;
  end?: number;
  quotedText?: string;
  status: "pending" | "verified" | "invalid";
}
```

## 6. SSE 事件协议

### 6.1 建议事件序列

```text
tool_start
→ source_found（可重复）
→ tool_end
→ token / citation_delta
→ citations_finalized
→ done
```

### 6.2 `source_found`

当工具发现来源时发送。同一个 `source_id` 重复出现时，前端执行 upsert 而不是追加重复卡片。

```json
{
  "event": "source_found",
  "data": {
    "message_id": "assistant_123",
    "tool_call_id": "call_abc",
    "source": {
      "source_id": "src_01HXYZ",
      "title": "Agent 架构设计文档",
      "page": 12,
      "source_type": "knowledge_base",
      "quote": "Agent 在工具执行完成后……",
      "score": 0.87,
      "status": "retrieved"
    }
  }
}
```

### 6.3 `citation_delta`

可选事件。用于流式正文已经出现引用标记、但回答尚未结束的场景：

```json
{
  "event": "citation_delta",
  "data": {
    "source_id": "src_01HXYZ",
    "marker": "[^src_01HXYZ]",
    "status": "pending"
  }
}
```

第一期可以不实现该事件，只在 Markdown 中临时显示引用标记，并在结束后统一定稿。

### 6.4 `citations_finalized`

```json
{
  "event": "citations_finalized",
  "data": {
    "message_id": "assistant_123",
    "citations": [
      {
        "citation_id": "cite_01HXYZ",
        "source_id": "src_01HXYZ",
        "display_index": 1,
        "start": 18,
        "end": 34,
        "status": "verified"
      }
    ],
    "cited_source_ids": ["src_01HXYZ"]
  }
}
```

### 6.5 与 `tool_end` 的兼容关系

为了渐进式落地，`tool_end` 可以先增加可选的 `sources` 字段。这样不支持工具内部流式上报的同步工具，也能在工具结束时批量显示来源：

```json
{
  "event": "tool_end",
  "data": {
    "tool": "search_knowledge_base",
    "output": "供模型使用的文本",
    "sources": [],
    "id": "call_abc"
  }
}
```

第二期再让检索工具通过 LangGraph custom stream 在每个结果可用时发送 `source_found`，获得真正逐条出现的体验。

## 7. 引用生成与校验策略

### 7.1 推荐流程

1. 工具返回 `answer_context` 和 `sources`。
2. 后端把来源写入当前会话的来源注册表。
3. 注入模型的上下文为每个片段标记不可变 `source_id`。
4. 系统提示要求模型只使用已提供的 `source_id`。
5. 模型在答案中输出 `[^source_id]` 标记或结构化 citation refs。
6. 后端检查 `source_id` 是否存在、引用标记是否落在有效文本之后。
7. 合法引用按首次出现顺序编号；非法引用标记为 `invalid`，不伪装成真实引用。
8. 前端收到 `citations_finalized` 后更新正文角标和来源状态。

### 7.2 为什么不能只靠 Markdown 链接

- 模型可能生成不存在的 URL、文件名或页码。
- 文本流式更新后，纯字符串替换容易造成编号漂移。
- 工具输出被压缩时，解析出来的引用可能消失。
- 同一来源的不同 chunk 难以去重。
- 无法稳定支持刷新恢复、双向定位和多格式导出。

### 7.3 可信度边界

`verified` 只表示引用指向真实存在且由工具返回的来源，不表示来源内容一定正确，也不表示引用必然充分支持结论。后续如需更严格的“论断—证据一致性”检查，应增加独立的 citation entailment 评估步骤。

## 8. 右侧引用来源面板设计

### 8.1 信息架构

```text
引用来源  3                       [收起]
────────────────────────────────────
已引用  2

[1] Agent 架构设计文档
    第 12 页 · 本地知识库
    “Agent 在工具执行完成后……”
    [打开原文]

[2] Context Engineering
    第 7 页 · Skill 检索

其他检索结果  1

    LangGraph Streaming Guide
    已检索 · 未被最终答案采用
```

### 8.2 交互规则

- 右侧面板只展示当前轮次：从会话中最后一条 user message 开始，聚合其后的全部 assistant/tool 分段；上一轮来源不进入当前面板。
- Agent 开始检索时自动打开面板，可在设置中关闭自动打开。
- `source_found` 到达后立即新增卡片，并显示“已检索”状态。
- `citations_finalized` 到达后，将真实采用的来源移动到“已引用”。
- 未采用来源保留在折叠的“其他检索结果”中。
- 点击正文 `[1]`：打开面板、滚动到来源、高亮卡片。
- 点击来源卡片：滚动并高亮正文中的所有对应引用。
- 同一文档不同 chunk 默认合并成一张文档卡片，展开后展示多个引用片段。
- 外部 URL 使用新标签页打开；本地文件只允许通过受控 API 打开或预览。
- 面板支持收起和拖动宽度；窄屏降级为底部抽屉。

### 8.3 与现有 Inspector 的关系

引用来源不是 coding agent 的 git/environment 面板，可以先作为现有右侧 Inspector 的一个新模式：

```text
右侧面板
├── 引用来源（聊天时默认）
├── 文件预览
├── Memory
├── Skills
└── MCP
```

避免同时渲染两个互相争夺宽度的右侧面板。文件来源被打开后，可以从“引用来源”切换到“文件预览”，并保留返回引用列表的入口。

## 9. 持久化方案

建议在 assistant display message 中增加：

```json
{
  "role": "assistant",
  "content": "最终 Markdown",
  "sources": [],
  "citations": [],
  "tool_calls": []
}
```

要求：

- `sources`、`citations` 属于用户可见记录，应跟随 `display_messages` 持久化。
- `tool_calls.raw_output` 保存工具最初返回值，作为可审计的事实源；适配器不得覆盖它。
- `tool_calls.output` 保存当前供模型使用的适配/摘要结果，以兼容现有上下文压缩逻辑。
- 历史会话重新进入 LangChain 时，以 `raw_output` 为输入重新执行确定性适配；已经由 `single_tool_overflow` 或 `tool_result_clear` 摘要的记录继续使用其 `output`，避免重新注入超长结果。
- 模型上下文中的工具输出可以压缩，但不得反向修改已存储的来源与引用。
- `output_preview` 仅用于工具卡预览，不作为引用恢复的数据源。
- 会话历史 API 必须原样返回 `sources` 和 `citations`。
- 删除消息或清空会话时同步删除对应引用数据。

## 10. 最终文档引用

同一份结构化引用数据可以渲染成不同格式：

### 10.1 Markdown

```markdown
Agent 应保留结构化工具结果。[^1]

[^1]: 《Agent 架构设计文档》，第 12 页，/knowledge/agent-architecture.pdf
```

### 10.2 Word/PDF

- 正文使用上标编号。
- 页脚或文末生成脚注/参考资料。
- 有页码时保留页码。
- 外部来源保留标题、URL 和访问时间。
- 本地来源可显示逻辑文档名，避免暴露不必要的宿主机绝对路径。

### 10.3 导出约束

- 导出使用已持久化的 `citations`，不重新让模型生成引用。
- 引用编号按最终正文首次出现顺序重新计算。
- 删除某段正文时同步清理不再使用的引用。
- 无合法引用的来源只能出现在“检索资料附录”，不能进入“参考资料”。

## 11. 代码改动范围

### 11.1 后端

```text
backend/tools/search_knowledge_tool.py
backend/tools/execute_skill_tool.py
backend/graph/agent.py
backend/graph/session_manager.py
backend/api/chat.py
backend/tests/test_context_optimizations.py
```

建议新增：

```text
backend/graph/citations.py
backend/models/citations.py          # 如果后续整理统一 Pydantic models
backend/tests/test_citations.py
```

### 11.2 前端

```text
frontend/src/lib/store.tsx
frontend/src/components/chat/ChatMessage.tsx
frontend/src/components/chat/RetrievalCard.tsx
frontend/src/components/editor/InspectorPanel.tsx
frontend/src/app/page.tsx
frontend/src/app/globals.css
```

建议新增：

```text
frontend/src/components/citations/SourcesPanel.tsx
frontend/src/components/citations/SourceCard.tsx
frontend/src/components/citations/CitationMarker.tsx
frontend/src/components/citations/citationUtils.ts
```

## 12. 分阶段开发计划

### 阶段 0：协议定稿与测试基线

| 状态 | 任务 | 产出 |
| --- | --- | --- |
| [x] | 持久化技术方案与开发计划 | 本文档 |
| [x] | 定稿 `SourceRecord`、`CitationRef` 字段 | 后端模型与前端类型保持一致 |
| [x] | 定义来源去重规则 | 基于文档、chunk、URI、标题与 quote 生成确定性 `source_id` |
| [x] | 补充 SSE 协议样例和兼容规则 | `tool_end.sources`、`source_found`、`citations_finalized` |
| [x] | 建立引用单元测试基线 | 合法、重复、未知来源、结构化结果与会话持久化 |

### 阶段 1：结构化来源链路

目标：先保证来源真实、完整、可持久化；此阶段允许来源在工具结束后批量出现。

| 状态 | 任务 | 文件范围 |
| --- | --- | --- |
| [x] | `search_knowledge_base` 返回 source nodes 元数据 | `backend/tools/search_knowledge_tool.py` |
| [x] | 为 Skill 工具定义可选结构化来源结果协议 | `backend/tools/execute_skill_tool.py` 支持脚本返回结构化 envelope |
| [x] | `tool_end` 增加独立 `sources` 字段 | `backend/graph/agent.py`, `backend/api/chat.py` |
| [x] | 来源数据绕过 output preview/压缩 | 在摘要前解析来源，`sources` 独立透传和持久化 |
| [x] | assistant display message 持久化来源 | `backend/graph/session_manager.py`, `backend/api/chat.py` |
| [x] | 前端 store 接收、去重和恢复来源 | `frontend/src/lib/store.tsx` |
| [x] | 为历史会话兼容缺少 sources 的旧数据 | 可选字段兼容旧消息 |

阶段验收：工具完成后，前端能够拿到真实文件名、页码、chunk 和 quote；刷新会话后来源仍存在；超长工具输出压缩不影响来源。

### 阶段 2：右侧来源面板 MVP

目标：实现可用的右侧来源列表和基本交互，先展示“已检索”，暂不做精确正文区间绑定。

| 状态 | 任务 | 文件范围 |
| --- | --- | --- |
| [x] | 新增 `SourcesPanel` 和 `SourceCard` | `frontend/src/components/citations/*` |
| [x] | 聊天首页挂载右侧来源面板并复用 ResizeHandle | `frontend/src/app/page.tsx` |
| [x] | 支持已引用/其他检索结果分组 | `SourcesPanel.tsx` |
| [-] | 支持折叠、拖动宽度和窄屏抽屉 | 已完成顶部开关与拖动宽度；窄屏抽屉待实现 |
| [-] | 支持 URL 打开和本地来源受控预览 | 已完成外部 URL；本地受控预览待 workspace API |
| [-] | 空状态、加载态、错误态 | 已完成空状态和流式检索状态；独立来源错误态待完善 |

阶段验收：Agent 每完成一次检索，右侧面板无需刷新即可显示来源；切换会话后显示对应会话来源；主聊天区域不会被面板遮挡。

### 阶段 3：最终答案引用绑定

目标：建立正文引用角标与来源之间的可信映射。

| 状态 | 任务 | 文件范围 |
| --- | --- | --- |
| [x] | Prompt 注入来源 ID 和引用输出约束 | `backend/graph/prompt_builder.py`、结构化工具结果 |
| [x] | 解析 `[^source_id]` citation refs | `backend/graph/citations.py` |
| [x] | 校验未知、重复、失效引用 | 未注册来源不会成为合法引用，重复来源复用编号 |
| [x] | 生成 `citations_finalized` 事件 | `backend/api/chat.py` |
| [x] | 持久化最终 citation mappings | `backend/graph/session_manager.py` |
| [x] | Markdown 渲染引用角标 | `frontend/src/components/chat/ChatMessage.tsx` |
| [x] | 正文与来源卡片双向定位 | 正文角标锚点 + 来源卡片反向滚动定位 |

阶段验收：每个引用角标都能解析到真实来源；模型输出未知 `source_id` 时不会生成可点击的伪引用；相同来源多次引用保持同一编号。

### 阶段 4：真正的逐条动态来源

目标：在长耗时搜索或多文档 Skill 中，来源无需等待整个工具结束即可逐条出现。

| 状态 | 任务 | 文件范围 |
| --- | --- | --- |
| [ ] | 工具执行中发送 LangGraph custom `source_found` | 检索工具/Skill 执行适配器 |
| [ ] | API 透传并保证事件顺序 | `backend/graph/agent.py`, `backend/api/chat.py` |
| [x] | 前端按 `source_id` 幂等 upsert | `frontend/src/lib/store.tsx` |
| [ ] | 处理中、完成、失败状态联动 | `SourcesPanel.tsx` |
| [x] | 断流后通过会话历史恢复最终来源 | partial save、session history、store 均保留来源 |

阶段验收：多文档检索过程中，来源卡片逐条出现；重复事件不会生成重复卡片；停止生成后已发现来源仍可查看。

### 阶段 5：文档导出与质量评估

| 状态 | 任务 | 产出 |
| --- | --- | --- |
| [ ] | Markdown 脚注导出 | 可复用导出函数 |
| [ ] | Word/PDF 引用映射接口预留 | 统一 export citation model |
| [ ] | 外部 URL 记录访问时间 | 来源元数据 |
| [ ] | 引用覆盖率统计 | 有引用论断比例、无效引用数 |
| [ ] | 引用一致性评估 | 可选 entailment evaluator |
| [ ] | 端到端回归测试 | 检索、生成、刷新、导出完整路径 |

## 13. 测试计划

### 13.1 后端单元测试

- 同一文档和 chunk 重复出现时正确去重。
- 同一文档不同 chunk 可以合并展示但保持片段级引用。
- 模型引用未知 `source_id` 时标记为 invalid。
- 工具结果超过压缩阈值后，`sources` 仍完整。
- `output_preview` 截断不会截断 `sources`。
- 工具失败时已发现来源保留，并标记工具状态。
- 历史旧消息缺少 citations 字段时可以正常加载。

### 13.2 前端测试

- `source_found` 重复事件执行 upsert。
- `citations_finalized` 后来源正确分组。
- 点击角标定位来源，点击来源定位正文。
- 切换会话不会串用来源。
- 面板关闭后引用角标仍可点击并重新打开面板。
- 移动端/窄屏使用抽屉，不挤压正文到不可读宽度。

### 13.3 端到端场景

1. 提问需要检索三份本地文档的问题。
2. 观察工具运行和来源出现过程。
3. 验证最终答案只引用其中两份。
4. 验证第三份进入“其他检索结果”。
5. 刷新页面并重新打开会话。
6. 点击引用角标和来源卡片验证双向定位。
7. 导出 Markdown，验证脚注和编号。

## 14. 风险与应对

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 模型伪造来源 ID | 产生错误引用 | 后端只接受来源注册表中已存在的 ID |
| 工具只返回纯文本 | 无法提取可信元数据 | 为检索类工具定义统一结构化结果协议 |
| 工具输出压缩/截断 | 引用丢失 | sources/citations 独立于 output 持久化 |
| 流式文本位置变化 | `start/end` 漂移 | 生成期间使用稳定 marker，结束后再计算区间 |
| 相同来源重复展示 | 面板噪声 | 会话级 source registry + 确定性去重键 |
| 本地路径泄露 | 暴露宿主环境 | 前端展示逻辑名称，打开动作走受控 API |
| 检索到但未采用也显示为引用 | 误导用户 | 明确区分 retrieved 与 cited |
| 来源真实但不支持结论 | 引用质量不足 | 后续增加论断—证据一致性评估 |

## 15. 推荐实施顺序

推荐严格按以下顺序推进：

1. 先完成阶段 1，确保真实来源元数据能够端到端存活。
2. 再完成阶段 2，将可信来源放进右侧面板。
3. 完成阶段 3，建立最终正文与引用的绑定。
4. 根据工具耗时决定是否实施阶段 4 的逐条动态事件。
5. 最后接入导出与质量评估。

不建议先做右侧静态 UI，再从工具输出字符串中临时解析文件名。那样虽然能快速看到卡片，但后续无法稳定支持引用校验、会话恢复和最终文档导出。

## 16. 完成定义

本功能完成需要同时满足：

- 来源来自工具或 Skill 的结构化真实结果。
- 检索来源与最终引用在数据和 UI 上明确区分。
- 正文角标可以稳定定位到右侧来源。
- 来源和引用可以随会话恢复。
- 工具压缩、预览截断不会破坏来源。
- 未知或伪造来源不能渲染成合法引用。
- Markdown 导出可以生成正确脚注。
- 后端测试、前端构建和端到端场景验证通过。

## 17. 实施记录

### 2026-06-22：结构化来源与右侧面板 MVP

已完成：

- 新增 `backend/graph/citations.py`，统一来源规范化、确定性 ID、结构化工具结果、引用校验与编号。
- 新增 `backend/graph/tool_result_adapter.py`，在 ToolMessage 边界统一适配标准 envelope、AI HOT/Tavily 类 JSON、`fetch_url`、Markdown 链接和裸 URL。
- 本地知识库检索保留 LlamaIndex `source_nodes` 的文档、chunk、页码、quote 和 score。
- `execute_skill` 支持透传 Skill 脚本返回的结构化来源 envelope。
- Agent 在超长工具结果摘要之前提取来源，来源不依赖 `output` 或 `output_preview`。
- Session 同时保存原始 `raw_output` 与模型侧 `output`；适配只发生在工具结果交回 LangChain 的过渡边界，历史重放也会重新适配原始值。
- SSE 增加 `source_found` 和 `citations_finalized`，`tool_end` 同时保留兼容的可选 `sources`。
- assistant message 独立持久化 `sources` 与 `citations`，历史会话和断流保存路径可恢复。
- 聊天首页接入可调整宽度、可折叠的右侧来源面板；检索到来源时自动打开。
- 面板区分“已引用”和“其他检索结果”，支持外部原文打开及正文/来源双向滚动定位。
- Prompt 要求模型使用真实 `source_id` 输出 `[^source_id]`；后端拒绝未注册来源。

验证结果：

- `PYTHONPYCACHEPREFIX=/private/tmp/puddingclaw_pycache python -m compileall -q backend/graph backend/tools backend/api`：通过。
- `PYTHONPYCACHEPREFIX=/private/tmp/puddingclaw_pycache pytest -q backend/tests/test_citations.py`：`13 passed`，覆盖结构化协议、AI HOT、Tavily、fetch_url、Markdown、SSE、原始结果持久化、历史重新适配、会话恢复，以及 read_file 读取 SKILL.md/JSON 不得产生来源的回归场景。
- `npm run build`：通过，Next.js 静态页面与 TypeScript 类型检查成功。
- `backend/tests/test_context_optimizations.py`：同步测试 `29 passed`；本机缺少 `pytest-asyncio`，原有 7 个 `@pytest.mark.asyncio` 用例无法由当前 host pytest 执行。该环境缺口与本次引用功能无关，未将其记录为通过。

后续未完成：

- 检索工具执行中的逐条 LangGraph custom stream。目前 `source_found` 在每次工具完成后、最终回答生成前批量发出。
- 窄屏底部抽屉、本地文件受控预览和独立来源错误态。
- Markdown/Word/PDF 导出与 citation entailment 质量评估。

### 2026-06-22：修复 SKILL.md 示例链接误判

- 现象：读取 `skills/aihot/SKILL.md` 后，说明文档中的官网、Base URL、OpenAPI 和示例 API 地址出现在“其他检索结果”。
- 根因：Markdown/裸 URL 兜底适配对所有 ToolMessage 生效，没有区分“读取说明文件”和“真正执行外部检索”。
- 修复：为隐式 JSON/Markdown 来源提取增加工具资格判断；`read_file` 等文件操作不再产生隐式来源，terminal 仅在真实网络命令下启用。
- 历史兼容：前端根据 `tool_call_id + metadata.adapter` 隐藏旧会话中由 `read_file/write_file/execute_skill` 产生的遗留误判，不修改原始 Session 审计记录。
- 验证：后端引用测试 `13 passed`，前端 `npm run build` 通过。

### 2026-06-22：来源面板限定当前轮次

- 原行为：面板聚合整个 Session 的 `sources/citations`，历史轮次来源会持续累积。
- 新行为：以前端最后一条 user message 为当前轮起点，只聚合其后的 assistant/tool 分段。
- 多段工具调用、`new_response` 和最终回答仍属于同一轮，不会因 assistant 分段而丢失来源。

### 2026-06-22：恢复用户可见的流式渲染

- 诊断：backend SSE 逐 token 正常，Next `/api` 代理也实时透传事件和 ping；AI HOT 的 terminal/curl 阶段因 `subprocess.run(capture_output=True)` 仍会等待工具完成。
- 前端问题：旧 SSE parser 按单次 `reader.read()` 临时维护 event 名称，网络块恰好切在 `event:` 与 `data:` 之间时可能丢失事件类型；同一网络块内的大量 token 更新也可能被 React 合并成一次绘制。
- 修复：改为按 SSE 空行边界解析完整 frame，跨网络块保留未完成 frame；对同批 token 每 4 个小批次让出浏览器渲染时间，大 token payload 再切成可见小段。
- 边界：这保证最终回答渐进显示；terminal 工具 stdout 和引用来源真正逐条流式仍需要工具执行层 custom stream。

### 2026-06-22：AI HOT Skill 确定性来源输出

- 现象：AI HOT Skill 只有 API/curl 使用说明，Agent 会临时生成 `curl | python` 命令；当格式化代码只打印标题、摘要、来源名称而遗漏 `item.url` 时，回答内容正常但右侧引用来源为 0。
- 原则：Skill 说明文件不是检索结果；只有 API 调用完成后返回的条目才具备来源资格。来源 URL 必须进入机器可读的 `sources[]`，不能依赖模型最终是否把链接写进 Markdown。
- 新增 `backend/skills/aihot/scripts/aihot_query.py` 作为唯一生产查询入口：按本轮中文问题确定性选择精选/全部/日报/存档、类别、关键词和时间窗，统一处理 User-Agent、超时与重试。
- 脚本直接输出 PuddingClaw 标准 `puddingclaw_tool_result: 1` envelope；items 的 `url`、日报的 `sourceUrl` 映射为 `source_type=web` 的独立来源对象。
- AI HOT 的 `summary` 明确标记为 `metadata.evidence_kind=derived_summary`，用于回答上下文但不冒充原文逐字引文；`uri` 始终指向可追溯原文。
- `execute_skill` 通过 `SKILL_USER_QUERY` 环境变量把当前用户问题传给脚本，兼容其他不接收 CLI 参数的 Skill。
- 修复 `execute_skill` 先截断 stdout、后解析协议的问题：现在先解析完整结构化结果，仅限制 `answer_context` 长度，`sources[]` 不受预览截断影响。
- `SKILL.md` 顶部增加强制执行路径，禁止生产查询继续临时拼 `curl | jq/python`；保留原 API 示例作为调试参考。

验证结果：

- `pytest -q backend/tests/test_aihot_skill.py backend/tests/test_citations.py`：`19 passed`，覆盖语义路由、items/daily URL 保真、本轮问题透传和超长上下文下来源保留。
- `quick_validate.py backend/skills/aihot`：`Skill is valid!`。
- 容器内真实请求“今天 AI 圈有什么”：标准 envelope 返回 `source_count=1`，首条 `uri` 为真实原文链接。
- 重建 `puddingclaw-backend` 后通过 `execute_skill` 完整链路复测；容器健康状态为 `healthy`。
