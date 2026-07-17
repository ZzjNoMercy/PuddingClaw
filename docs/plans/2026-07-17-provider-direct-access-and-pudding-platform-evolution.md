# Provider 直连与 Pudding 产品演进方案

> 日期：2026-07-17
>
> 状态：方案草案
>
> 适用仓库：当前 PuddingClaw，后续拟更名/演进为 PuddingData

## 1. 结论

1. **Higress 退出默认运行链路。** PuddingData 直接访问模型 Provider，不再把网关作为本地单用户产品的必经层。
2. **保留客户端抽象，替换网关抽象。** `ModelClient`、Embedding Client 和多模态 Embedding Client 继续存在，它们统一从 `ProviderRegistry` 解析模型、凭证、端点和协议。
3. **Provider、Endpoint、Model 分开建模。** 协议属于端点或模型，而不是简单地属于 Provider；这能覆盖同一个 Provider 同时提供 OpenAI-compatible LLM、原生多模态 Embedding 等情况。
4. **当前产品聚焦 PuddingData。** 继续把 Data Agent 场景做深，不立即为了“通用”而拆库或重构成框架。
5. **等第二个真实产品出现后抽象 PuddingAgent。** PuddingAgent 是可开源、可二开的 Agent Harness/Runtime，不包含问数业务。
6. **PuddingEvaluate 是建立在 PuddingAgent 上的独立评测 Agent。** 当前个人研究评测先复用 PuddingData 的正常运行链路；未来迁移到 PuddingEvaluate，不能在 PuddingData 内形成第二套执行引擎。
7. **Yuxi 当前最值得借鉴的是 Child Run 思路。** 现阶段只为 Subagent 调用记录基本身份、状态和用量；持久化 Run、SSE/Trace 和统一调用路径沿用 Pudding 已有能力，不照搬其 Postgres + Redis 部署形态。

本文只确定架构与迁移顺序，不执行仓库、包名和产品名的批量重命名。

## 2. 产品边界

### 2.1 PuddingData

当前阶段的产品与主仓库，定位为面向数据分析/问数的垂直 Agent。

它负责：

- 数据源、语义层、SQL、安全策略与数据分析工作流；
- Data Agent 专属提示词、工具、验证规则和交互界面；
- 问数质量、完成时间、Token 消耗等真实场景研究；
- 先验证 Harness 是否真的能支撑长期任务，而不是提前追求通用性。

它不负责：

- 把所有 Agent 类型都塞进同一个产品；
- 把评测平台能力耦合进问数产品；
- 对外承诺稳定的通用 Agent SDK（在 PuddingAgent 抽象完成之前）。

### 2.2 PuddingAgent

未来从至少两个真实 Agent 产品的共同需求中提取出的开源 Harness/Runtime。

建议只承载这些稳定能力：

- Agent 定义与有效配置快照；
- Run 生命周期、幂等、暂停、恢复、取消；
- 模型、工具、知识、文件和工作区的运行时作用域；
- LangGraph 图执行与 Checkpoint 适配；
- Tool Pipeline、HITL、权限策略和验证；
- 可重放事件协议、Trace、Artifact；
- Child Run/Subagent 生命周期；
- Provider Registry 及模型调用接口；
- 可插拔的观测、评测和导出接口。

PuddingAgent 不应包含：

- Data Agent 的 SQL/语义层业务；
- 评测数据集、评分 Rubric 和实验看板；
- 特定云厂商或 Higress 的部署假设；
- 某个可观测平台作为必选依赖。

### 2.3 PuddingEvaluate

独立的评测 Agent/研究产品。它是 PuddingAgent 的消费者，与 PuddingData 平级，而不是 PuddingData 的内部模块。

它负责：

- Test Case、Dataset、Rubric、Judge 和人工评分；
- 并发、重复运行、对照实验和回归；
- 完成时间、首 Token、Token 消耗、工具轨迹、错误和质量评分；
- 对目标 Agent 的统一调用适配；
- 结果对比、统计分析和可选的 Langfuse 等外部导出。

推荐依赖关系：

```text
PuddingData ───────┐
                   ├──> PuddingAgent
PuddingEvaluate ──┘

PuddingEvaluate ──RunRequest/RunEvents/RunResult──> PuddingData 或其他目标 Agent
```

不允许 PuddingAgent 反向依赖 PuddingData 或 PuddingEvaluate。

## 3. 为什么移除 Higress

Higress 在多团队、多租户、统一流量治理、集中审计、复杂路由和企业级密钥托管场景下有价值；但当前 PuddingData 是本地优先的单用户产品，网关带来的成本大于收益：

- 多一个常驻服务及其健康检查、配置同步和故障面；
- “网关模型”和“直连模型”两套配置产生语义漂移；
- LLM、文本 Embedding、多模态 Embedding 并不总能共享同一兼容协议；
- 调试与评测时很难确认实际调用的端点、模型和回退路径；
- 本地安装、Electron 打包、Docker Compose 和文档都被网关细节污染。

因此目标不是去掉抽象后在业务代码里散落 SDK 调用，而是把抽象改成：

```text
Data Agent / Harness
        │
        ├── ModelClient
        ├── TextEmbeddingClient
        └── MultimodalEmbeddingClient
                  │
           ProviderRegistry
                  │
       Provider → Endpoint → Model
                  │
           Direct Provider API
```

## 4. Provider Registry 设计

### 4.1 核心原则

- 使用稳定的 `provider_id:model_id` 标识模型，显示名只用于 UI；
- 凭证只通过 `credential_ref` 引用安全存储，不写入普通配置、API 响应或缓存；
- 明确记录端点协议，不用“是否 OpenAI-compatible”一个布尔值覆盖所有能力；
- 模型必须声明输入/输出模态，能力检查不能依靠模型名称猜测；
- 文本 Embedding 和多模态 Embedding 是两个独立 Binding；
- 评测时禁止静默切换模型、端点或降级路径；
- 检索索引必须绑定 Embedding 模型和向量空间指纹。

### 4.2 建议配置结构

```yaml
providers:
  - id: dashscope
    name: DashScope
    credential_ref: keychain://pudding/provider/dashscope
    endpoints:
      - id: compatible_chat
        base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
        protocol: openai_chat
      - id: compatible_embedding
        base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
        protocol: openai_embeddings
      - id: multimodal_embedding
        base_url: https://dashscope.aliyuncs.com/api/v1
        protocol: dashscope_multimodal_embedding
    models:
      - id: qwen-plus
        kind: llm
        endpoint: compatible_chat
        input_modalities: [text]
        output_modalities: [text]
      - id: qwen-vl-max
        kind: llm
        endpoint: compatible_chat
        input_modalities: [text, image]
        output_modalities: [text]
      - id: text-embedding-v4
        kind: embedding
        endpoint: compatible_embedding
        input_modalities: [text]
        output_modalities: [vector]
        dimension: 1024
      - id: multimodal-embedding-v1
        kind: embedding
        endpoint: multimodal_embedding
        input_modalities: [text, image, video]
        output_modalities: [vector]
        dimension: 1024

bindings:
  llm: dashscope:qwen-plus
  text_embedding: dashscope:text-embedding-v4
  multimodal_embedding: dashscope:multimodal-embedding-v1
```

运行态可以把配置编译成不可变快照：

```text
ResolvedModelSpec
  provider_id
  model_id
  kind
  endpoint_id
  base_url
  protocol
  input_modalities
  output_modalities
  model_parameters
  credential_ref
  registry_revision
```

每次 Run 创建时记录不含明文密钥的 `ResolvedModelSpec`，后续 Resume 继续使用同一快照，避免配置变化使一次任务前后调用不同模型。

### 4.3 Embedding 与索引一致性

每个向量索引必须保存：

- `provider_id`、`model_id`、`endpoint_id`；
- `protocol` 和 `dimension`；
- 输入模态与预处理版本；
- Registry revision；
- 由上述字段计算的 `vector_space_fingerprint`。

绑定的 Embedding 发生变化时：

- 指纹不一致则索引标记为 `stale`；
- 不允许新模型查询旧向量而无提示；
- 需要显式重建或选择兼容索引；
- 评测结果必须记录实际索引指纹。

### 4.4 当前阶段的存储

PuddingData 先使用：

- 本地 JSON/YAML 保存 Provider、Endpoint、Model 和 Binding；
- macOS Keychain/现有 Token Store 保存密钥；
- 进程内不可变 Registry Snapshot；
- 配置 revision 用于 Run 和 Trace 复现。

当前不需要为了对齐 Yuxi 引入 Postgres + Redis。未来 PuddingAgent 只定义 Registry 接口，再提供 Local、Database 等适配器。

### 4.5 对 Yuxi Provider 管理的取舍

Yuxi 的 Provider CRUD、远程模型发现、Chat/Embedding Adapter 和缓存分层可以作为实现骨架参考，但不应原样复制其数据模型：

- 可以借鉴 Provider Service 与具体 SDK Adapter 分离；
- 可以借鉴模型发现结果先校验、再写入 Registry；
- 不能只用 `chat / embedding / rerank` 表达模型能力；
- 不能在发现后丢弃输入/输出 modalities；
- 需要补上多模态 Embedding 的协议、模型和客户端；
- API 与缓存不得包含可还原的明文 API Key；
- 协议要落到 Endpoint/Model，不能假设一个 Provider 只有一种协议。

结论是“参考服务分层，不继承其 Provider schema”。

## 5. Higress 迁移步骤

### Phase A：冻结行为并建立 Provider Registry

- 为现有 `gateway_llm`、`fallback_llm`、Embedding 和多模态 Embedding 配置建立一次性迁移器；
- 增加 Provider/Endpoint/Model/Binding 校验与 capability 检查；
- 保持现有 Client 接口，先只替换配置解析；
- 在 Trace 中记录 resolved provider/model/endpoint/protocol/revision；
- 为配置迁移、协议选择和密钥脱敏增加测试。

### Phase B：默认切换为 Provider 直连

- `ModelClient` 不再探测 Higress 健康状态后决定调用路径；
- LLM、文本 Embedding、多模态 Embedding 分别从 Binding 解析；
- 明确区分普通故障重试与跨模型回退；
- 默认不启用跨模型回退；如用户启用，必须产生可见事件并写入 Run 结果；
- 评测模式强制关闭跨模型/跨协议静默回退。

### Phase C：更新设置和能力展示

- 设置页改为 Provider 列表、端点、模型发现/手工录入及三个默认 Binding；
- 能力页从 Registry 读取实际模态、协议和健康状态；
- API 响应只返回 `has_credential`，不返回密钥或 `credential_ref` 的敏感细节；
- 连通性测试按具体 Endpoint + Model 执行。

#### 设置页界面设计

当前设置侧栏中的“AI 网关”改名为**模型服务**。这里管理的是 Provider 直连配置，不再出现 Higress、Gateway Console、网关健康检查或“Fallback 直连配置”等概念。

页面沿用 PuddingData 现有设置页的左侧分类导航、白色卡片、钴蓝主色和紧凑桌面布局。信息层级采用“默认用途在前，基础设施在后”：普通用户先完成模型选择，高级用户再进入 Provider 细节。

```text
设置 / 模型服务                                      [刷新状态]
管理模型来源、凭证和默认用途。凭证仅保存在本机。

┌ 默认模型 ────────────────────────────────────────────┐
│ 主对话模型          文本向量模型       多模态向量模型  │
│ Qwen Plus           text-embedding-v4  mm-embedding-v1 │
│ DashScope · 正常    1024 维 · 正常     图/文 · 正常    │
│ [更换]              [更换]             [更换]          │
└──────────────────────────────────────────────────────┘

模型供应商                         [搜索供应商] [新增供应商]
┌ DashScope ───────────────────────────────────────────┐
│ 已启用 · 凭证已配置     3 个端点 · 8 个模型            │
│ Chat · Text Embedding · Multimodal Embedding          │
│ 默认用途：主对话 / 文本向量 / 多模态向量              │
│                              [测试] [管理模型与端点]    │
└──────────────────────────────────────────────────────┘
```

##### A. 默认模型区

首屏固定展示三个 Binding 卡片：

1. **主对话模型**：显示模型名、Provider、文本/图像输入能力和连接状态；
2. **文本向量模型**：显示模型名、Provider、维度和连接状态；
3. **多模态向量模型**：显示模型名、Provider、输入模态、维度和连接状态。

点击“更换”打开统一模型选择器：

- 只显示满足该 Binding 能力的已启用模型；
- 支持按 Provider、模型名和模态筛选；
- 模型行展示协议、上下文或维度、最近一次测试状态；
- 选择后先显示影响说明，再明确保存；
- 更换 Embedding 时若向量空间指纹变化，必须提示哪些索引会变为 `stale`，不能静默保存。

这里不允许直接输入任意 Model ID；未登记模型必须先进入 Provider 管理添加。

##### B. Provider 总览区

参考 Yuxi 的 Provider 卡片、搜索和统计，但放在设置页内，不建立独立“管理员模型管理”产品页面。

每张 Provider 卡片展示：

- 图标、展示名和稳定 `provider_id`；
- `已启用 / 已停用 / 凭证缺失 / 部分异常` 状态；
- Endpoint 数、已启用模型数；
- 能力摘要：Chat、Vision、Text Embedding、Multimodal Embedding；
- 当前承载的默认 Binding；
- 最近一次连接测试的时间与结果；
- “测试”和“管理模型与端点”两个主要操作。

页面顶部只保留“搜索供应商”“新增供应商”和“刷新状态”。不在首屏展示 Base URL、协议 JSON 或 API Key 输入框。

##### C. 新增 Provider 流程

使用分步抽屉，而不是把所有技术字段塞进一个大表单：

1. **选择模板**：OpenAI、DeepSeek、DashScope、自定义 OpenAI-compatible；
2. **配置凭证**：输入 API Key，保存后只显示“已配置”和末四位；
3. **确认端点**：模板自动生成 Endpoint；自定义 Provider 才需要填写 Base URL 和协议；
4. **测试并发现模型**：逐 Endpoint 测试，成功后获取远端候选模型；
5. **启用模型**：勾选需要的模型，必要时填写无法发现的维度、模态等信息。

创建成功后返回 Provider 总览。默认 Binding 不在向导中自动替换；用户需要在顶部默认模型区显式选择，避免新增 Provider 意外改变当前运行配置。

##### D. Provider 管理抽屉

点击“管理模型与端点”打开右侧宽抽屉，分为三个页签：

**概览**

- 展示名、Provider ID、启用状态；
- 凭证状态，以及“替换凭证”“删除凭证”；
- 该 Provider 承载的默认 Binding；
- 删除 Provider 的危险操作。

**端点**

采用表格而不是多个散落的 Base URL 字段：

| 用途 | 协议 | Base URL | 状态 | 操作 |
| --- | --- | --- | --- | --- |
| Chat | `openai_chat` | `…/compatible-mode/v1` | 正常 | 测试 / 编辑 |
| Text Embedding | `openai_embeddings` | `…/compatible-mode/v1` | 正常 | 测试 / 编辑 |
| Multimodal Embedding | `dashscope_multimodal_embedding` | `…/api/v1` | 正常 | 测试 / 编辑 |

“新增端点”只在高级操作中出现。Headers、超时和扩展参数放入折叠的“高级配置”，普通模板无需用户处理。

**模型**

参考 Yuxi 的“已启用模型 + 远端候选模型”，但不再用上下堆叠的长列表：

- 默认显示已启用模型表；
- 工具栏提供搜索、类型筛选、“发现远端模型”和“手动添加”；
- 远端发现结果使用独立选择弹窗，支持多选后一次添加；
- 模型表列为：模型、类型、输入模态、Endpoint、上下文/维度、Binding、状态、操作；
- 每个模型可测试、编辑、停用或移除；
- 手动添加必须选择 Endpoint，并明确声明 `kind`、输入/输出 modalities；Embedding 必须提供或探测 dimension。

##### E. 状态与保护规则

- API Key 永不回显完整值，前端只能看到 `has_credential` 和可选末四位；
- Provider 被默认 Binding 使用时，不能停用或删除，必须先更换 Binding；
- 模型被默认 Binding 使用时，不能移除；
- Endpoint 被启用模型引用时，不能删除；
- “测试 Provider”实际逐个测试其启用 Endpoint，并分别返回结果，不用一个绿色状态掩盖局部故障；
- “测试模型”必须调用确切的 Endpoint + Model，结果显示耗时、协议和错误摘要；
- 环境变量覆盖存在时只读展示来源，不允许页面保存造成“看似成功、运行时未变化”；
- 模型发现只生成候选项，必须经用户确认才能写入 Registry；
- 任何默认模型切换、Embedding 指纹变化和跨模型 fallback 都需要明确提示。

##### F. 从 Yuxi 借鉴与调整

直接借鉴：

- Provider 卡片总览、搜索、启停状态和模型数量；
- 远端模型发现与手动添加两条路径；
- 已启用模型的类型、上下文/维度和连接测试；
- 默认模型使用中禁止删除的保护逻辑。

针对 PuddingData 调整：

- 管理入口放入设置页，并将名称从“AI 网关”改为“模型服务”；
- 默认 Binding 提升到页面首屏，而不是藏在 Agent 或 Provider 配置中；
- Provider、Endpoint、Model 三层分开，不使用 Provider 级单一 Base URL/Protocol；
- 增加 Vision 和 Multimodal Embedding 的完整模态展示；
- 凭证进入本机安全存储，不像 Yuxi 当前表单那样回填 API Key；
- 模型管理采用宽抽屉和独立远端选择弹窗，避免一个 Modal 内同时堆两套长列表。

### Phase D：清理 Higress 运行依赖

重点影响面包括：

- `backend/llm/model_client.py`
- `backend/llm/embed_client.py`
- `backend/llm/multimodal_embedding.py`
- `backend/config.py`
- `backend/api/config_api.py`
- `backend/api/capabilities.py`
- `backend/capabilities.py`
- `backend/graph/agent.py`
- `backend/graph/deepagents_manager.py`
- `backend/analytics/nl2sql/service.py`
- `backend/higress_config_reader.py`
- `backend/.env.example`
- Docker Compose、Electron 启动/安装脚本、前端设置页和相关文档/测试。

完成直连验证后：

- 从默认 Compose 和 Electron 生命周期中删除 Higress；
- 删除 Higress 配置读取器及专属健康检查；
- 清理 `gateway_llm`/`fallback_llm` 旧字段；
- 更新 README、安装文档和 ADR。

若需要照顾已有本地配置，可保留一个版本的只读迁移兼容，但不能继续维护两套运行逻辑。

### Phase E：验收

- 全新安装无需启动 Higress 即可完成 LLM、文本 Embedding、多模态 Embedding 调用；
- 老配置能够迁移且密钥不进入普通配置文件；
- 设置页能够精确展示当前 Binding 和实际协议；
- Run/Trace 可回答“本次到底调用了哪个端点和模型”；
- Embedding 切换会使不兼容索引显式失效；
- 评测模式下不会发生未记录的 fallback；
- 仓库运行时代码、Compose、Electron 和文档中不再要求 Higress。

## 6. 从 Yuxi 借鉴什么

Yuxi 同样建立在 LangGraph 之上。对当前 PuddingData 而言，大部分 Harness 能力已经有对应实现，真正值得新增的是把 Subagent 调用从匿名工具调用变成可识别的 Child Run。这里先借鉴其数据关系，不建设完整调度基础设施。

### 6.1 借鉴优先级

| 能力 | Yuxi 做法 | Pudding 当前基础 | 决策 |
| --- | --- | --- | --- |
| Typed Runtime Context | 一个 Context schema 同时驱动 UI 配置、权限过滤和运行态解析 | 已有 effective manifest、工具 schema 和 Session 状态 | 借鉴 schema 驱动，但 effective manifest 继续作为最终权威 |
| Durable Agent Run | `AgentRun` 拥有稳定 ID、状态、request id、输入输出、模型快照和父子关系 | 已有 `HarnessRunCoordinator`、Goal、暂停/恢复/取消 | 沿用现有实现，暂不新增数据库式 Run 层 |
| 单一执行入口 | 对话、外部调用、评测、Worker 都进入正常 AgentRun | 当前主要是产品内调用 | 评测必须复用同一路径，不直接调用 LangGraph 内部图 |
| Replayable Event Stream | 版本化事件 envelope、单调 seq、cursor replay、heartbeat、终态补偿 | 已有 SSE、Trace 和状态优先展示 | 当前没有断线重放需求，不新增 Event Store |
| Subagent as Child Run | 子 Agent 有独立 run/thread/checkpoint，支持 start/status/await/cancel | 已有配置化 subagent 和 DeepAgents task | **当前唯一新增项：先记录 Child Run 基本信息，不实现完整异步生命周期** |
| Context/Filesystem Scope | workspace、artifact、attachment、skills 作用域显式化 | 已有 Docker/Restricted Host Workspace 与工具策略 | 把 scope 纳入 `RunRequest`，沿用当前安全边界 |
| Checkpoint | Postgres 优先，SQLite/Memory fallback | 已有 LangGraph checkpoint 与 Session 权威边界 | 借鉴适配器接口；生产态禁止静默降级到内存 |
| Token/State Observability | Middleware 写入 token snapshot、上下文占用、summary 状态 | 已有 Trace、Token usage 和上下文压缩 | 统一到 ModelCall 事件和 Run 聚合指标 |
| Evaluation | 数据集逐条通过正常 conversation-backed Run，再把结果发给 Langfuse | 正计划用问数 Agent 做研究评测 | 借鉴统一入口；PuddingEvaluate 自己持有评测真相，Langfuse 只做可选 adapter |
| Optional Tracing | Langfuse 不进入关键执行链路 | 已有本地 Trace | 保持本地可用，外部观测平台通过 exporter 接入 |

### 6.2 未来抽取边界，而非当前建设项

以下对象仍可作为未来 PuddingAgent 的抽取参考，但当前不需要为了形式完整而统一重写。现有 Session、Run、SSE 和 Trace 保持不变：

```text
AgentDefinition
  identity + version
  graph factory
  model/tool/knowledge/skill policy
  state schema

RunRequest
  request_id
  agent_id/version
  thread_id
  input
  resolved_model_spec
  effective_manifest
  workspace/artifact/attachment/skill scopes
  parent_run_id (optional)
  metadata

RunRecord
  run_id + status
  request snapshot
  checkpoint reference
  started/finished timestamps
  error/interrupt/cancel state

RunEvent
  schema_version
  run_id + thread_id
  seq + timestamp
  type
  namespace/child_run_id
  payload

RunResult
  terminal status
  output
  usage/timing summary
  artifacts
  verification result
  trace/event cursor
```

当 PuddingEvaluate 真正独立、出现外部调用需求后，再冻结这些公共协议。LangGraph 的内部 State、节点名称和 Middleware 顺序不应成为未来外部 API。

### 6.3 事件协议暂缓

当前继续使用现有 SSE 和 Trace，不增加 JSONL/SQLite Event Store，也不为尚未出现的断线重放场景引入 cursor 协议。未来若 PuddingEvaluate 或远程 Worker 确实需要稳定事件接口，可以从以下小集合开始：

- `run.created`、`run.started`、`run.interrupted`、`run.completed`、`run.failed`、`run.cancelled`；
- `model.started`、`model.delta`、`model.completed`、`model.failed`；
- `tool.started`、`tool.completed`、`tool.failed`；
- `artifact.created`；
- `verification.completed`；
- `child_run.created`、`child_run.updated`；
- `usage.updated`；
- `fallback.applied`。

这是一份未来兼容性备忘，不属于本轮 Higress/Provider 改造范围。

### 6.4 Subagent 对评测的特殊价值

PuddingEvaluate 中 Judge、Reviewer、对照 Agent 更适合成为独立 Child Run，而不是父进程里一个不透明的模型调用：

- 每个 Judge 有独立模型快照、上下文和 Token 统计；
- 可以重试、取消、并发和审计；
- 能区分被测 Agent 成本与评审 Agent 成本；
- 人工评分和模型评分可以挂在同一个 Evaluation Result 上；
- Judge 失败不会伪装成被测 Agent 失败。

当前只记录以下基本信息：

```text
ChildRunRecord
  child_run_id
  parent_run_id
  subagent_id/name
  model/provider snapshot
  status
  started_at/finished_at/duration
  input/output token usage
  trace reference
  error summary (optional)
```

第一阶段保持 `task()` 同步等待，不单独提供 `start/status/await/cancel`，不要求独立 Thread、Checkpoint 或 Worker，也不改变现有 Subagent 的执行语义。等后台并行、独立恢复或 PuddingEvaluate Judge 出现真实需求后，再升级为完整托管式 Child Run。

### 6.5 不建议照搬的部分

- 不为本地单用户产品直接复制 Postgres、Redis、ARQ Worker 全家桶；
- 不把 API Key 明文放入数据库响应或 Redis Model Cache；
- 不把协议只定义在 Provider 级别；
- 不丢弃模型的输入/输出模态；
- 不在评测时用 OCR、默认模型或协议转换进行静默兼容；
- 不在持久化 Checkpoint 失败时静默退化为内存；
- 不让 Langfuse 成为评测数据和结果的唯一真相来源。

## 7. 评测路径

### 7.1 当前个人研究阶段

继续使用 PuddingData 的问数 Agent 执行测试，但评测数据应记录在独立目录/存储中，避免把“评测产品逻辑”混入 Agent 业务：

```text
EvaluationCase
  input + expected constraints + dataset snapshot

TargetRun
  exact RunRequest + RunResult + RunEvents

EvaluationScore
  latency + first-token latency + token usage
  deterministic checks
  human score + notes
  optional judge run references
```

最低限度需要冻结：

- Case 与数据快照版本；
- Agent/提示词/工具/模型/Provider/索引指纹；
- 总耗时、首 Token、输入/输出/缓存 Token；
- SQL/工具轨迹、重试、fallback 和错误；
- 产物与验证结果；
- 人工评分 Rubric、评分人、评分时间和备注。

### 7.2 PuddingEvaluate 阶段

当个人脚本出现以下任意两个信号时，再正式建立 PuddingEvaluate：

- 同一套 Case 需要评估 PuddingData 之外的 Agent；
- 需要批量回归、并发调度或多次重复采样；
- 需要多个 Judge/Reviewer 或盲评；
- 需要独立结果浏览、对比和实验管理；
- 评测代码开始绕过 PuddingData 的正常 Run 路径。

届时把当前研究数据迁移为 PuddingEvaluate 的 `Case / Experiment / Trial / Score`，执行层只依赖 PuddingAgent 的 Run 契约。

## 8. 建议实施顺序

### 近期：PuddingData 内完成

1. 引入 Provider Registry，迁移 LLM、文本 Embedding、多模态 Embedding；
2. 默认直连并移除 Higress 运行依赖；
3. 在现有 Run/Trace 中冻结 `ResolvedModelSpec`、effective manifest 和索引指纹；
4. 为现有 Subagent 调用记录最小 `ChildRunRecord`，不改变同步执行方式；
5. 让个人评测只通过正常 Run API 执行，并保存可复现快照。

### 中期：为 PuddingAgent 准备

1. 用第二个真实 Agent 验证抽象，而不是先拆包再寻找场景；
2. 出现独立恢复/并行需求后，再为 Child Run 增加 Thread、Checkpoint 和异步生命周期；
3. 出现外部调用需求后，再固化 `RunRequest / RunRecord / RunEvent / RunResult`；
4. 出现断线重放或远程 Worker 后，再引入 Event Store 和语义事件协议；
5. 最后为 Run Store、Checkpoint、Provider Registry、Trace Exporter 定义通用适配器。

### 后期：正式拆分

1. 把稳定 Harness 抽出为 PuddingAgent；
2. PuddingData 作为首个垂直发行版依赖 PuddingAgent；
3. 建立 PuddingEvaluate，并只通过稳定 Run 接口调用目标 Agent；
4. 将 Langfuse/OpenTelemetry 等作为可选导出器；
5. 再决定是否提供 Server/Worker/Redis 等团队部署 Profile。

## 9. 架构决策底线

- 去掉 Higress 不等于去掉 `ModelClient` 和 Provider 抽象；
- PuddingData 先保持垂直，不在当前阶段做无需求支撑的通用框架重写；
- PuddingAgent 的抽取由第二个真实产品需求驱动；
- PuddingEvaluate 不得建立绕过正常 Agent Run 的私有执行路径；
- 模型、提示词、工具、数据、索引和配置都必须可快照、可追溯；
- Fallback、降级、OCR 和模型替换必须显式进入事件与结果；
- 本地简单实现与未来分布式实现共享协议，而不是共享基础设施假设。

## 10. Yuxi 调研依据

本方案参考了本地 Yuxi 仓库的以下实现：

- `../Yuxi/backend/package/yuxi/agents/context.py`
- `../Yuxi/backend/package/yuxi/agents/base.py`
- `../Yuxi/backend/package/yuxi/agents/buildin/chatbot/graph.py`
- `../Yuxi/backend/package/yuxi/services/agent_run_service.py`
- `../Yuxi/backend/package/yuxi/services/run_queue_service.py`
- `../Yuxi/backend/package/yuxi/services/subagent_run_service.py`
- `../Yuxi/backend/package/yuxi/agents/middlewares/subagent_task.py`
- `../Yuxi/backend/package/yuxi/services/agent_invocation_service.py`
- `../Yuxi/backend/package/yuxi/services/langfuse_service.py`
- `../Yuxi/web/src/views/ModelManageView.vue`
- `../Yuxi/web/src/components/model-management/ModelProviderManagePanel.vue`
- `../Yuxi/docs/agents/subagents-management.md`
- `../Yuxi/docs/agents/sandbox-architecture.md`
- `../Yuxi/docs/agents/agent-evaluation.md`
- `../Yuxi/docs/intro/model-config.md`

其中值得保留的是协议和生命周期设计，不是其具体基础设施组合。
