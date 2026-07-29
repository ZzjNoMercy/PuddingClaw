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
- 主 Agent、图片分析 SubAgent、文本 Embedding 和多模态 Embedding 使用独立 Binding；
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
  agent: dashscope:qwen-plus
  image_analyzer: dashscope:qwen-vl-max
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

- 用户数据目录下的 `providers.json` 保存 Provider、Endpoint、Model 和 Binding；
- 用户数据目录下独立的 `credentials.json` 保存 Key，不再把 Secret 写进仓库或 `backend/config.json`；
- 从第一版开始通过 `CredentialStore` 接口访问 Key，第一版使用 `LocalCredentialStore`，第二版替换为系统 Keyring；
- 进程内不可变 Registry Snapshot；
- 配置 revision 用于 Run 和 Trace 复现。

当前代码没有可直接复用的模型密钥 Token Store，模型 Key 主要仍在仓库内的 `backend/config.json` 中明文持久化。第一版的安全目标是把它移出仓库、限制文件访问和所有外泄路径；不在这一版引入跨平台 Keyring 的打包与兼容成本。

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

### 4.6 现有模型配置与 Key 的无缝迁移

这是本次改造的发布硬约束：已有用户升级后不需要重新选择模型、重新填写 Base URL 或重新输入任何 API Key。首次启动自动迁移；迁移失败时继续使用旧配置，不能让模型调用失效。

#### 迁移来源

迁移器必须读取实际生效值及其来源，覆盖：

| 旧来源 | 迁移内容 | 新目标 |
| --- | --- | --- |
| `fallback_llm` | Provider、Model、Base URL、Key、temperature、max tokens、thinking 配置 | LLM Provider/Endpoint/Model + `llm` Binding |
| `gateway_llm` + Higress route/provider | 网关模型别名、上游 Provider、模型路由、Token | 对应直连 Provider/Endpoint/Model；保留当前主模型语义 |
| `fallback_embedding` | Provider、Model、Base URL、Key、dimension、batch size | Text Embedding Endpoint/Model + `text_embedding` Binding |
| `multimodal_embedding` | Provider、Model、原生 Base URL/route path、Key、dimension、并发 | Multimodal Embedding Endpoint/Model + `multimodal_embedding` Binding |
| `rag.rerank` | Provider、Model、Base URL、Key | Rerank Provider/Endpoint/Model（若启用或已配置） |
| `vanna.llm` / `vanna.embedding` | 非空的独立覆盖配置和 Key | 独立模型引用；`reuse` 配置继续指向默认 Binding |
| 环境变量 | `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`DASHSCOPE_API_KEY`、`EMBEDDING_API_KEY` 等有效覆盖 | `env://VARIABLE_NAME` Credential Reference，不复制环境变量明文 |
| Higress `ai-proxy` 配置 | Provider、route、model alias、`apiTokens` | Provider Registry + LocalCredentialStore |

迁移必须按旧代码相同的优先级解析 effective value，尤其保留多模态 Embedding 当前的 Key 顺序：显式配置 → `DASHSCOPE_API_KEY` → `EMBEDDING_API_KEY` → Higress DashScope Token。

#### CredentialStore

新增统一接口：

```text
CredentialStore
  put(credential_id, secret, metadata)
  get(credential_id)
  exists(credential_id)
  delete(credential_id)
```

第一版实现选择：**Python Backend 使用 `LocalCredentialStore`，把 Key 移出仓库并存入用户数据目录的独立文件。** Provider Registry 从第一版起只保存 `credential_ref`，第二版切换系统 Keyring 时不修改 Provider、前端或模型客户端结构。

```text
PuddingData userData/
  providers.json
  credentials.json
  provider-migration.json

providers.json:
  credential_ref: local-file://provider/<credential_id>

credentials.json:
  credentials:
    <credential_id>:
      secret: <actual API key>
      created_at: <timestamp>
      updated_at: <timestamp>
```

第一版路径：

| 平台 | 用户数据目录 |
| --- | --- | --- |
| macOS | `~/Library/Application Support/PuddingData/` |
| Windows | `%APPDATA%\PuddingData\` |
| Linux Desktop | `~/.config/PuddingData/` |
| Docker/Headless | 显式挂载的用户数据目录，或使用环境变量/Secret Mount |

Electron 将系统 `userData` 目录通过专用环境变量传给 Backend；非 Electron 开发模式使用 `platformdirs` 解析同一位置，不允许默认回退到仓库目录。

第一版要求：

- 环境变量不写入 CredentialStore，只保存 `env://...` 引用；
- API、日志、Trace、异常和迁移报告永不包含完整 Secret；
- Provider 配置只保存 `credential_ref`、`has_credential` 和可选末四位；
- Credential ID 使用随机 ID 或稳定非敏感 ID，不包含 Key 片段；
- macOS/Linux 用户数据目录权限为 `700`，`credentials.json` 权限为 `600`；
- Windows 文件放在当前用户 `%APPDATA%`，继承并校验当前用户 ACL，不放入共享目录；
- `credentials.json` 使用临时文件、flush/fsync、原子 rename 写入；
- 普通备份、导出、诊断包和云同步清单排除 `credentials.json`；
- 文件损坏或不可读时不能覆盖为空文件，必须保留现场并报告错误；
- `providers.json` 和 `credentials.json` 均加入仓库级忽略与 Secret 扫描规则。

第二版新增 `OSKeyringCredentialStore`：

- macOS 映射到 Keychain；
- Windows 映射到 Credential Locker/Credential Manager；
- Linux Desktop 映射到 Secret Service/KWallet；
- Docker/Headless 继续使用 `env://NAME` 或 `secret-file:///run/secrets/name`；
- Backend 直接访问 OS Keyring，不经 Electron 传递明文；
- 自动把 `local-file://` Secret 写入 Keyring、读回验证，再把引用切换为 `os-keyring://`；
- 所有引用切换成功后才删除 `credentials.json` 中对应明文；
- Keyring 不可用时继续使用第一版 LocalCredentialStore，不影响现有用户。

不采用“密文文件旁边保存解密 Key”的自建加密方案；这不能有效改善本地同用户威胁模型。Electron `safeStorage` 也不作为当前方案，因为 Backend 消费 Secret 还需要额外建设鉴权 IPC/Credential Broker。

#### Provider 合并规则

不能仅凭 `provider=qwen` 把旧配置合并。迁移身份至少考虑：

```text
provider family + normalized endpoint origin + credential fingerprint
```

- 相同 Provider、相同 Key、不同用途 Endpoint 可以合并为一个 Provider；
- LLM、文本 Embedding、多模态 Embedding 共用同一 DashScope Key 时，只写入一次 CredentialStore，多个 Endpoint 引用同一 Credential；
- 同一厂商配置了不同 Key 时必须保留为不同 Provider 实例或不同 Credential Profile，不能擅自覆盖；
- Secret fingerprint 只在内存中用于去重，不能持久化可用于猜测 Key 的原始 Hash；
- 模型别名与真实模型映射不明确时保留原别名，并在迁移报告中标记 `needs_verification`，不能静默换成猜测模型。

#### 事务与崩溃恢复

迁移必须幂等，并使用版本化 Journal：

```text
legacy_detected → credentials_prepared → registry_committed
                → legacy_secrets_scrubbed → completed
```

执行顺序：

1. 对旧配置加迁移锁，读取原始配置并计算不含 Secret 的校验摘要；
2. 在内存中生成 Provider Registry 草稿和迁移映射；
3. 把旧明文 Key 写入 CredentialStore，并立即读回比对；
4. 原子写入新 Registry（临时文件、flush/fsync、rename）；
5. 用新 Registry 解析 LLM、文本 Embedding、多模态 Embedding，确认 resolved config 与旧 effective config 等价；
6. 原子回写旧配置，移除已成功迁移的明文 Key 和 Higress Token；
7. 写入 `migration_version`、完成状态和不含 Secret 的迁移摘要。

任一步失败时：

- 不修改旧配置中的 Key；
- 不切换到新 Registry；
- 继续由旧配置路径提供模型能力；
- 下次启动从 Journal 安全重试；
- 设置页显示“模型配置迁移待完成”，但不要求用户重新输入 Key。

不得创建包含明文 Key 的普通 `.bak` 文件。需要回滚的信息保存为脱敏配置快照；Secret 已由 CredentialStore 管理。

#### 迁移后的前端体验

- 成功迁移不弹阻断向导，设置页直接展示原模型和“凭证已迁移”；
- Provider、模型和三个默认 Binding 的结果必须与升级前有效配置一致；
- Key 输入框显示“已配置”和末四位，只提供“替换”操作；
- 环境变量来源显示“由环境变量提供”，保持只读覆盖语义；
- `needs_verification` 项显示非阻断警告，并允许用户测试确认；
- 只有无法解析到任何有效 Key 的旧配置才显示“凭证缺失”，不能把迁移器自身失败伪装成凭证缺失。

#### 迁移测试矩阵

- LLM、文本 Embedding、多模态 Embedding 分别使用三个不同 Key；
- 三种能力共用一个 DashScope Key，验证只生成一个 Credential；
- config.json 无 Key、完全依赖环境变量；
- 多模态 Embedding 只依赖 Higress DashScope Token；
- 同一 Provider 使用两个不同 Key，验证不会错误合并；
- 自定义 Base URL/OpenAI-compatible Provider；
- Thinking Model、Gateway Model Alias 和 Vanna reuse/override；
- 在写 Credential、提交 Registry、清除旧 Key 三个阶段模拟崩溃并验证恢复；
- 迁移后 API 响应、日志、Trace、Registry 和脱敏备份中均搜索不到原始 Key；
- 迁移前后的 `ResolvedModelSpec`、模型参数、Embedding dimension/batch size 完全一致。

## 5. Higress 迁移步骤

### Phase A：冻结行为并建立 Provider Registry

- 先实现 `CredentialStore` 接口与第一版 `LocalCredentialStore`，再为现有 `gateway_llm`、`fallback_llm`、Embedding、多模态 Embedding、环境变量和 Higress Token 建立幂等迁移器；
- 升级后模型、参数、Base URL、默认用途和 Key 必须无缝保留，不要求用户重新配置；
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

当前设置侧栏中的“AI 网关”改为两个相邻入口：**模型服务**和**默认模型**。这里管理的是 Provider 直连配置，不再出现 Higress、Gateway Console、网关健康检查或“Fallback 直连配置”等概念。

页面参考用户提供界面的三栏信息架构，同时沿用 PuddingData 现有浅色、白色卡片、钴蓝主色和紧凑桌面密度，不照搬参考图的深色视觉。

##### A. 设置内信息架构

```text
设置导航              Provider 列表              Provider 详情
────────────          ────────────────           ─────────────────────
模型服务              搜索 Provider              DashScope       [启用]
默认模型              ┌ DashScope · 正常 ┐       凭证 / Endpoint / 模型
────────────          │ 8 个模型         │       API Key        [测试]
常规设置              └──────────────────┘       Chat Endpoint
数据设置              ┌ DeepSeek · 正常  ┐       Embedding Endpoint
知识库                └──────────────────┘       模型 (8) [获取列表] [+]
Harness               [+ 添加 Provider]
```

- **模型服务**：配置 Provider、凭证、Endpoint、模型发现和已启用模型；
- **默认模型**：只配置运行时 Binding，不暴露 API Key 和 Base URL；
- 两者共享同一 Provider Registry，不能再维护两套模型配置。

桌面宽度充足时，“模型服务”采用三栏布局；窗口较窄时 Provider 列表变为页面，详情使用右侧抽屉。设置主导航仍是 PuddingData 现有导航，不额外复制一层完整侧栏。

##### B. 模型服务页

中间 Provider 列表参考截图：

- 顶部提供搜索、状态/能力筛选；
- 内置模板优先显示 OpenAI、DeepSeek、DashScope，自定义 Provider 排在其后；
- 列表项显示图标、名称、启用状态、凭证状态和模型数量；
- 底部或工具栏提供“添加 Provider”；
- 点击 Provider 后在右侧原位显示详情，避免反复打开/关闭多层 Modal。

右侧详情区从上到下排列：

1. Provider 名称、文档链接、启用开关；
2. **API Key**：只显示是否配置和末四位，提供替换、删除、测试；OAuth 登录仅在 Provider 确实支持时显示，普通模型厂商不伪造登录流程；
3. **Endpoint**：按用途展示 Chat、Text Embedding、Multimodal Embedding 的 Base URL 和协议；
4. **模型**：显示已启用数量、“获取模型列表”和“手动添加”；
5. 折叠的高级设置：Headers、timeout、额外参数和模型发现路径；
6. 页面底部的停用/删除危险操作。

同一个 Base URL 可以对应多个逻辑 Endpoint。例如 DashScope 的 OpenAI-compatible Chat 与 Text Embedding 地址可能相同，但协议分别是 `openai_chat` 和 `openai_embeddings`；多模态 Embedding 可以使用另一个原生 Endpoint。

##### C. 默认模型页

参考截图的卡片式默认模型选择，但换成 PuddingData 的真实运行用途：

1. **主对话模型**：Data Agent 规划、工具调用和最终回答；
2. **图片理解模型**：`image_analyzer` SubAgent 的图片内容理解，必须选择支持视觉输入的对话模型；
3. **文本向量模型**：文本知识、语义资产与 NL2SQL 召回；
4. **多模态向量模型**：图片、图文 PDF 等跨模态索引与召回；
5. **Rerank 模型**：召回候选的相关性重排。

每张卡片展示模型、Provider、模态/维度和最近连接状态。点击选择器只显示满足该 Binding 能力的已启用模型；旁边的设置按钮直接定位到对应 Provider 的模型详情。

- 未登记模型不能在这里手输 Model ID，必须先去“模型服务”添加；
- 模型选择后明确保存，不因为新增 Provider 自动改变默认值；
- 图片理解模型也可以在 Harness 的 `image_analyzer` 编辑页选择；两个入口读写同一个 `image_analyzer` Binding，运行时不再读取旧的 SubAgent 模型字符串；
- 更换 Embedding 时若向量空间指纹变化，必须提示受影响索引将变为 `stale`；
- “思考模式”是运行参数，不作为第四个 Provider Binding 混在这个页面。

##### D. 新增 Provider 流程

使用分步抽屉：

1. **选择模板**：OpenAI、DeepSeek、DashScope、自定义 OpenAI-compatible；
2. **配置凭证**：输入 API Key，第一版保存到用户数据目录的 `LocalCredentialStore`；
3. **确认端点**：模板自动生成 Endpoint，自定义 Provider 才要求填写 Base URL 和协议；
4. **测试连接**：逐 Endpoint 验证鉴权和协议；
5. **获取模型列表**：从远端发现候选模型；
6. **启用模型**：先按模型名预选一次分类，再由用户确认、增加或移除分类后写入 Registry；模型名推断不能直接成为最终能力事实。

一个 LLM 可以同时属于「对话模型」和「多模态模型」，例如用户可把 `qwen3.7` 同时登记到两个分类。分类保存为模型的显式 `categories` 数组；跨调用协议的分类不能混用，例如 LLM 不能同时登记为文本 Embedding。

创建成功后返回 Provider 详情。默认 Binding 不在向导中自动替换。

##### E. 模型列表发现机制

通常优先调用 Provider 的模型枚举接口，但不能假设所有 Provider 都完整支持 `GET /v1/models`。

调用链固定为：

```text
Settings UI
  → PuddingData Backend
    → ProviderAdapter.discover_models(endpoint, credential)
      → Provider API / built-in catalog / unsupported
```

前端不能直接请求 Provider：API Key 只由 Backend 从 CredentialStore 解析，Backend 负责处理 CORS、鉴权、超时和响应格式归一化。

Backend 接口建议：

```http
POST /api/model-providers/{provider_id}/discover-models
Content-Type: application/json

{
  "endpoint_id": "compatible_chat",
  "kind": "llm"
}
```

返回统一候选模型：

```json
{
  "models": [
    {
      "id": "qwen-plus",
      "display_name": "Qwen Plus",
      "kind": "llm",
      "input_modalities": ["text"],
      "output_modalities": ["text"],
      "context_length": null,
      "dimension": null,
      "source": "remote"
    }
  ],
  "discovery": {
    "supported": true,
    "partial": true,
    "endpoint_id": "compatible_chat"
  }
}
```

ProviderAdapter 采用三级策略：

1. **远端枚举**：OpenAI-compatible Endpoint 默认请求规范化后的 `GET {base_url}/models`；原生 Provider 使用自己的列表 API；
2. **内置 Catalog 补全**：用 Provider 模板补充远端只返回 ID、缺失的 kind/modality/context/dimension，但不得覆盖远端明确值；
3. **手动添加**：Provider 不支持发现、鉴权限制或接口失败时，用户仍可明确填写 Model ID 和能力。

模型枚举结果只是候选，不直接成为运行配置：

- 远端常常只返回模型 ID/owner，未必包含 Chat、Embedding、模态、上下文和维度；
- 模型类型命名规则只能作为 UI 建议，不能未经确认写入能力事实；
- 用户勾选“启用”后才写入 Registry；
- 刷新列表不会自动删除本地模型，远端消失的模型只标记为 `stale`；
- 候选列表可短期缓存，点击“获取模型列表”执行显式刷新；
- “连接测试”和“获取模型列表”是两个操作，能调用模型但不能枚举时不能标记 Provider 故障。

OpenAI 官方 Models API 也只保证返回模型的基础信息，因此 capability 补全和人工确认仍然必要：[OpenAI Models API](https://platform.openai.com/docs/api-reference/models/list)。

##### F. Provider 详情中的模型表

- 默认显示已启用模型；
- 工具栏提供搜索、kind/模态筛选、“获取模型列表”和“手动添加”；
- 远端候选使用独立选择弹窗，支持多选后一次添加；
- 表格列为：模型、kind、输入模态、Endpoint、上下文/维度、Binding、状态、操作；
- 每个模型可测试、编辑、停用或移除；
- 手动添加必须选择 Endpoint 并声明 `kind` 和 modalities；Embedding 必须填写或探测 dimension。

##### G. 状态与保护规则

- API Key 永不回显完整值，前端只能看到 `has_credential` 和可选末四位；
- Provider 被默认 Binding 使用时不能停用或删除，必须先更换 Binding；
- 模型被默认 Binding 使用时不能移除；
- Endpoint 被启用模型引用时不能删除；
- “测试 Provider”逐个测试启用 Endpoint，不用一个绿色状态掩盖局部故障；
- “测试模型”必须调用确切的 Endpoint + Model，显示耗时、协议和错误摘要；
- 环境变量覆盖存在时只读展示来源，不能出现“页面保存成功但运行值未变”；
- 任何默认模型切换、Embedding 指纹变化和跨模型 fallback 都需要明确提示。

##### H. 参考界面的取舍

直接借鉴：

- “模型服务 / 默认模型”两个相邻入口；
- Provider 搜索列表与右侧原位详情；
- Key、API 地址、测试、获取模型列表和手动添加的操作顺序；
- 默认模型使用大卡片选择，并能快速跳回具体模型配置。

结合 Yuxi 与 PuddingData 调整：

- Provider、Endpoint、Model 三层分开，不使用 Provider 级单一 Base URL/Protocol；
- 默认项不是“助手/快速/翻译”，而是主 LLM、文本 Embedding、多模态 Embedding；
- 增加 Vision 和 Multimodal Embedding 的完整模态展示；
- Key 第一版移出仓库进入 LocalCredentialStore，页面不会回填明文；
- 模型发现经过 Backend ProviderAdapter，前端不持有 Key；
- 保留远端发现与手动添加双路径，不把 `/v1/models` 当成所有 Provider 的强制能力。

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

## 11. 2026-07-29 第一版实施记录

已实施第一版直连 Provider Control Plane：

- `backend/provider_registry.py` 将 Provider、Endpoint、Model、Binding 和 CredentialRef 分离；预置 DeepSeek、阿里云百炼、Kimi、硅基流动；
- 首次读取旧配置时导入 DeepSeek LLM、DashScope 文本 Embedding、DashScope 原生多模态 Embedding，以及 Higress 持久化 YAML 中的 DashScope token；Higress 原始数据只读保留、不删除；
- API Key 从 `backend/config.json` 迁移到客户端用户目录的 `credentials.json`，以 `local-file://` 引用；环境变量保持 `env://NAME` 引用，不复制明文；文件采用原子写入与 owner-only 权限。第二版可将同一引用实现替换为系统 Keyring；
- `ModelClient`、图片分析 SubAgent、文本 Embedding、多模态 Embedding、rerank 改为按 Binding 直连；取消“网关失败后静默换 Provider”的行为。多模态与 rerank 使用请求级 HTTP client，避免 DashScope SDK 全局 Key/Base URL 在并发下串用；
- 设置页以「模型服务」展示 Provider/Endpoint，并以「默认模型」管理 Agent、图片理解、文本 Embedding、多模态 Embedding、Rerank Binding；`image_analyzer` 编辑页与图片理解默认项同步；OpenAI-compatible Endpoint 的模型发现使用 Provider `/models`；DashScope-native Endpoint 使用百炼可部署模型目录并合并官方原生多模态 Embedding / Rerank 基础目录，之后仍由用户确认分类再登记；
- 远端候选模型列表保留「推理 / 视觉 / 联网 / 免费 / 嵌入 / 重排 / 工具」名称预筛选；写入 Registry 的运行时分类只允许五类：对话模型、视觉模型、文本 Embedding、多模态 Embedding、Rerank。添加时以名称预分类映射出的运行时分类作为默认勾选，用户不修改则直接采用，修改后以用户选择为准；默认模型选择器使用固定向下展开的应用内菜单，不再依赖操作系统可能向上弹出的原生 `select`；
- Provider 详情的 Endpoint 配置仍按接口切换，但模型区跨该 Provider 的全部 Endpoint 聚合展示，并在模型行标注所属接口；“获取模型列表 / 添加模型”只作用于当前 Endpoint，避免原生多模态模型因接口切换而看似丢失；
- 百炼凭证按 Provider 共享，切换 OpenAI 兼容接口与百炼原生多模态接口不会切换密钥；已有双凭证配置以 OpenAI 兼容接口当前保存的密钥为准完成一次归并，所有百炼运行时 Binding 复用同一 Credential Ref；
- 对话输入区支持按会话选择已登记的对话模型，并在发送时把完整 Model ID 与推理强度冻结到本次 Run；运行时不再通过只替换模型名的方式跨 Provider。推理能力由统一映射表维护：DeepSeek 显示「高 / 最大」，直连 Kimi K3 显示「低 / 高 / 最大」，百炼 Kimi K3 固定「最大」，Qwen 3.7 固定开启思考并将强度控件置灰为「默认」。前端只消费映射表给出的展示能力，后端负责转换为 `reasoning_effort`、`extra_body.thinking` 或 `enable_thinking`；
- Docker/Electron 启动链不再启动或注入 Higress；`ai_gateway` 只保留兼容状态字段，返回 retired，不参与请求路由。

尚未在无用户显式授权的情况下执行真实 Key E2E。真实验收应在迁移后的本地客户端上，分别验证 DeepSeek 流式/工具调用、DashScope 文本向量（1024 维）、DashScope 原生多模态文本+图片向量（1024 维）与 rerank，并确认重启后 Binding 与凭证引用一致。
