# PuddingClaw

**面向本地知识与业务数据的白盒 AI 工作台。**

PuddingClaw 目前主打两条产品主线：

- **知识库**：把 PDF、Markdown、图片、Excel / CSV / TSV 和数据库源沉淀为用户自己拥有的知识与数据资产；当前支持精确检索、混合检索和图文多模态 RAG，并已规划以贯穿 raw、Wiki 和 gbrain 的统一 Schema 增加稳定知识补充层。
- **智能问数**：用 Profile、语义资产、分析模型和 SQL Guardrails 约束 Agent，让自然语言问题能够落到可解释、可复算的 SQL / Pandas 分析和报告产物。

贯穿两条主线的是两个原则：

- **白盒化**：把 Agent 的状态、工具、来源、SQL、权限、任务进度、验收与产物证据结构化呈现。白盒化指可审计的运行事实，不是暴露模型私有推理文本。
- **资产可迁移**：原始文件、Markdown 语义定义、分析模型、守卫、模板和 Profile 都以开放文件保留；向量索引只是可重建的加速层。分析项目可以导出为独立目录或 ZIP，交给 Codex、Claude Code 等文件系统型 Agent 继续使用。

> 当前产品边界：优先服务知识管理和智能问数场景；通用 Agent、Skill、MCP、Goal 和沙箱能力作为底层 Harness 提供支撑。

## 为什么是 PuddingClaw

传统 RAG 往往只返回一段答案，传统 ChatBI 又容易让模型直接根据字段名猜 SQL。PuddingClaw 在两者之间增加了一层可维护、可检查、可迁移的资产体系：

```text
原始文件 / 数据库
        │
        ├─ 知识资产：原件、Markdown、图片、引用元数据
        │              ├─ 当前：本地精确检索 + 文本/图片向量检索
        │              └─ 规划：LLM Wiki 编译 → gbrain 图谱/记忆 → MCP
        │
        └─ 数据资产：表格、数据库表、逻辑数据集、Profile
                       └─ 度量值 / 维度 / 颗粒度 / 资产关联
                                      └─ 分析模型 + SQL Guardrails + 模板
                                                     │
                                                     ▼
                                      Agent 执行、Trace、Evidence、可迁移产物
```

模型负责理解问题和编排工具；业务口径、数据边界、执行结果和完成证据不只存在于模型的临时上下文中。

## 核心能力

### 1. 用户自有的知识库

- 统一管理 PDF、Markdown、图片、表格文件和 PostgreSQL 数据源。
- PDF 通过 MinerU 解析为 Markdown 与独立图片资产，原件和解析产物都落在用户选择的知识库目录。
- 文本与图片采用独立索引管道，可进行全文、BM25、文本向量、图片向量和 Rerank 融合检索。
- Milvus 或多模态索引不可用时，原始文件与 Markdown 仍然保留，可继续使用 glob / grep 等本地精确检索。
- 导入、解析、向量发布采用任务队列，支持查看进度、失败原因、重试和索引重建。
- 检索结果携带来源信息，可进入对话右侧的来源与证据面板。

知识库目录是用户资产目录，不是缓存目录。PostgreSQL 保存 Catalog、任务和引用元数据；Milvus 保存可重建的检索索引，两者都不替代本地原始资产。

#### P0 开发中：LLM Wiki + gbrain 稳定知识补充层

> **状态：首个纵向切片已实现。** 已包含统一 Schema Bundle、LLM Wiki 文件协议、Agent 工具、gbrain 编译校验、现有 PostgreSQL 上隔离的 pgvector database、MCP allowlist 和 Schema Studio；Schema generation 级崩溃原子切换仍在后续阶段。该能力不会替换现有 MinerU + Milvus 多模态 RAG，也不改动智能问数的 Analytics / Vanna 域。

现有 RAG 更适合原始文档问答和证据定位；规划中的 LLM Wiki + gbrain 面向稳定知识、跨来源合成和关系查询。它们补充现有知识库，不替代现有 MinerU / Milvus 链路。目标分工是：

| 层 | 职责 | 资产边界 |
| --- | --- | --- |
| Canonical Schema Bundle | 统一约束 raw、Wiki 页面/关系、index/log、gbrain pack 和查询结果 | official `pack.yaml` 保存 gbrain 扩展，`brain.schema.yaml` 保存 Wiki/Agent 规则；通过锁、CAS、SemVer 和 hash 门禁生成 `AGENTS.md`、validator 和 resolved pack |
| LLM Wiki / 编译式 RAG | 按同一 Schema 将 raw 快照整理为互链 Wiki，执行 Ingest / Query / Lint | `raw/` 对 Compiler 只读；`wiki/` 由编译发布流程维护；`index.md` 是查询入口；`log.md` 只追加 |
| gbrain | 按同一 Schema 将 Wiki 编译为 pages、typed links 和全文/向量索引 | 默认复用现有 PostgreSQL server 中的独立 database/owner；它是可重建运行时索引，不是唯一知识源 |
| MCP | 向 PuddingClaw Agent 暴露筛选后的 gbrain 查询工具 | 只加载 allowlist；首期不暴露 mutation/admin operations |

目标链路：

```text
        brain.schema.yaml + official custom pack.yaml
                  │      │       │
                  │      │       └─► selected parent + custom resolved pack
                  │      └─────────► Wiki/raw validator
                  └────────────────► AGENTS.md
                                      │ PuddingClaw LLM Agent
                                      │ Ingest / Query / Lint
原始文档 / MinerU 产物 ─► raw/ ── Ingest ─► wiki/
                                              ├─ index.md
                                              ├─ log.md
                                              └─ <slug>.md
                                                   │ brain_sync
                                                   ▼
                                        gbrain / PostgreSQL
                                                   │
                                    筛选后的 MCP 查询 tools
                                                   │
                                                   ▼
                                         PuddingClaw Agent
```

核心约束：

- P0 同时落最小 Schema、`raw/ + wiki/ + wiki/index.md + wiki/log.md + AGENTS.md` 文件协议和 Ingest / Query / Lint；PostgreSQL 索引运行时后接。
- Schema Bundle 贯穿 raw、Wiki 与 gbrain：官方 `pack.yaml` 只保存 gbrain 原生字段，`brain.schema.yaml` 只保存 Wiki/Agent/MCP 补充规则，两者统一 version/hash 和发布事务。
- `AGENTS.md` 专门约束我们的 LLM Agent 如何执行 Ingest / Query / Lint；gbrain 不读取它，只读取 custom/resolved schema pack。
- gbrain Schema 默认采用 `gbrain-base-v2 + puddingclaw-wiki custom pack`；用户可选择其他兼容内置 parent，再增量补充业务类型和关系，系统锁定 parent/custom/resolved hash。
- 前端展示全部 gbrain 内置 packs 及真实 YAML；用户可选择兼容 parent 或 `extends: null`，按官方 `SchemaPackManifest v1` 结构化编辑 page/link type、frontmatter link、ExtractableSpec、subtype、borrow、filing、calibration、migration 和三类 mapping rule，并预览 custom/parent/resolved 原始 YAML。数组逐项编辑并保留顺序和含逗号文本。
- LLM 不直接写 raw 或 gbrain 数据库；确定性的 Wiki 发布和 `brain_sync` 分别是文件与索引写入入口。
- 真正可迁移的知识资产是 `schema + AGENTS.md + raw + wiki`；gbrain PostgreSQL 数据可按需重建。
- gbrain 首期复用用户现有 PostgreSQL 服务，但使用独立的 `puddingclaw_gbrain` database 与 owner，避免和 Catalog 共表、共享迁移权限；不要求第二个 PostgreSQL 进程或容器。PGLite 仅作为后续可选实验，不是零依赖目标。
- PuddingClaw 负责 gbrain 的构建、配置、进程生命周期、MCP allowlist 和前端管理；首期只供内部 Agent 查询。
- 模型配置只携带 `provider:model` 能力引用，不携带 API Key；导入新环境后通过 Provider Registry 重新绑定。
- 后续再评估稳定知识 gbrain 与实时文档 Milvus RAG 的查询路由和结果融合，不在首期实现中提前耦合。

### 2. AI Native 智能问数

智能问数工作台把结构化数据组织为五类对象：

| 对象 | 作用 | 主要载体 |
| --- | --- | --- |
| 数据资产 | Excel / CSV / TSV、数据库表、逻辑数据集及其来源 | Catalog + `profile.json` |
| 语义资产 | 度量值、维度、颗粒度和资产关联 | `measure.md` / `dimension.md` / `grain.md` / `relation.md` |
| SQL 守卫 | 约束业务口径、枚举、Join 与性能风险 | `guardrail.md` |
| 分析模型 | 绑定可用数据、语义、守卫、Reference 和输出模板 | `model.md` + `references/` + `templates/` |
| 查询结果 | 保存大结果、摘要、Profile、分页和可选导出 | `result_id` + 本地结果存储 |

当前链路包括：

- 为文件表格和数据库表生成字段类型、样例、行数、实体候选等 Profile。
- 用自然语言 Markdown 维护业务口径；前置 metadata 负责索引，正文作为 Agent 的完整语义来源。
- 支持直接字段、推导规则、实体匹配和日历映射四类维度定义。
- 支持跨来源实体解析、版本化 Crosswalk、人工覆盖、发布与任务中心。
- 支持虚拟纵向合并逻辑数据集，保留来源、字段契约、Profile、新鲜度与时间覆盖，不覆盖原始表。
- SQL 路径通过 Vanna / NL2SQL 生成与执行，文件数据路径通过 Pandas 分析；二者共享语义上下文，减少同一指标在不同执行器中口径漂移。
- SQL Guardrails 在执行前检查确定性规则；命中后可告警、改写或阻断，并在 Trace 中留下依据。
- 大结果不会全部塞进模型上下文：系统按预算返回完整明细或预览，将结果持久化后提供分页、Profile 和可选 CSV 导出。
- 分析模型可以绑定 HTML / Markdown 等模板，用于可复跑的业务分析与报告刷新。

### 3. 白盒 Agent Harness

PuddingClaw 不把“模型说完成了”当作唯一完成证据。运行时围绕三个控制面组织：

- **Action Control**：工具可见性、权限、Shell 分析、外部目录授权、Docker / Host Backend 和 HITL。
- **Context Control**：项目上下文、Skill 激活、会话历史、摘要、工具结果压缩和跨 Run Evidence 投影。
- **Completion Control**：Todo、Goal / Run、确定性检查、可选 Rubric 验收、预算和继续执行。

前端可查看的结构化事实包括：

- Agent / Chat 运行模式与当前项目上下文；
- Graph、Middleware Hook、Tool 调用和耗时；
- State snapshot / diff 与实际模型输入归因；
- 知识库召回、融合、Rerank 和引用来源；
- 生成 SQL、语义资产命中、Guardrail 结果和查询结果 ID；
- Todo、Goal、Run、预算、验收标准、缺口和修正记录；
- Permission request / grant、Backend、工作区与产物 Evidence。

### 4. 资产可迁移

PuddingClaw 内部可以继续使用稳定的原生资产 ID；迁移由独立导出编译层完成，不要求目标工具实现 PuddingClaw 的导入协议。

分析模型详情页可以导出一个自包含或本地绑定的分析项目：

```text
analysis-project/
├── AGENTS.md
├── README.md
├── analysis-project.yaml
├── model/
│   └── model.md
├── semantic/
│   ├── measures/
│   ├── dimensions/
│   ├── grains/
│   └── relations/
├── guardrails/
│   ├── compiled/
│   └── runtime/
├── profiles/
├── templates/
├── data/                   # 可选复制原始数据
├── bindings.example.yaml
└── bindings.local.yaml     # 本机路径/连接绑定，不应提交
```

导出时可以：

- 把文件数据复制到 `data/`，生成相对路径、Profile 与 SHA-256，得到更完整的跨机器项目；
- 保留当前机器的绝对路径，只导出校验信息和本地绑定，适合大文件或敏感数据；
- 对数据库只保存环境变量名、表映射和 Profile，不把密码写入项目；
- 同步导出可执行的 SQL Guardrail 校验器，避免外部 Agent 使用另一套近似规则。

向量库、临时结果和平台运行状态不是业务资产的唯一事实源。即使离开 PuddingClaw，模型说明、语义口径、守卫、模板和数据绑定仍然可读、可版本化、可继续执行。

知识库未来的目标迁移单元是一个 Brain 目录：`schema/`、`AGENTS.md`、`raw/` 和 `wiki/` 可以整体打包；gbrain PostgreSQL 数据不作为必需资产，可从 Schema + Wiki 重建。该导出/导入链路属于 LLM Wiki + gbrain 方案，当前尚未实现。

## 典型使用流程

1. 在“设置 → 模型服务”登记 Provider、接口和凭证，并为对话、视觉、文本 Embedding、多模态 Embedding、Rerank 绑定默认模型。
2. 在“设置 → 知识库”选择长期知识目录，配置 PostgreSQL、MinerU 和可选 Milvus。
3. 在“知识库”上传文档或表格，或登记数据库源；查看导入、解析与索引任务。
4. 在“智能问数”生成 Profile，维护语义资产、逻辑数据集、SQL 守卫和分析模型。
5. 回到 Agent 对话，选择分析模型后用自然语言提问；在右侧面板核对来源、SQL、任务进度、权限与验收证据。
6. 需要跨工具协作时，从分析模型详情导出 Analysis Project ZIP。

## 设置项与运行边界

设置页是桌面运行时的主要配置入口，当前信息架构如下：

| 设置分类 | 主要内容 |
| --- | --- |
| 模型服务 | Provider、Endpoint、模型发现、默认模型绑定、直连 / AI Gateway、思考模式 |
| 项目上下文 | 当前项目的业务背景、架构约束、目录约定与稳定决策 |
| 智能问数设置 | 上下文 Token 预算、明细行列保护、SQL 超时、结果持久化、分页、Profile 与导出 |
| RAG 设置 | Top-K、相似度阈值、文本 / 图片 / BM25 权重、候选池与 Rerank |
| 知识库 | Catalog PostgreSQL、用户知识目录、多模态 Embedding 并发、Milvus / 本地索引与索引重建 |
| 记忆管理 | Markdown / mem0 长期记忆及相关后端配置 |
| Harness 配置 | SubAgent、上下文压缩、Goal 与验收、终端 / Docker 沙箱、模型调用保护 |
| 高级设置 | 兼容性压缩参数等低频运行选项 |
| 系统状态 | PostgreSQL、Milvus、MinerU、模型接入等能力探测与降级状态 |

正常桌面使用以设置页和 `backend/config.json` 为事实源；环境变量主要用于部署覆盖。不要把包含真实 API Key、数据库密码或本机路径的配置提交到版本库。

知识库现已提供 Schema Studio：可查看 gbrain 内置 Schema、选择父 pack、结构化编辑完整官方 manifest，并预览 custom/parent/resolved YAML。当前还缺少可视化 diff、官方 YAML CST 导入/注释保留、Ingest / Query / Lint 运维页、raw/Wiki drift 看板、Embedding 绑定与托管式重建入口。

## 快速开始

### 环境要求

- Python 3.11 或 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 18+
- Docker（启动内置 PostgreSQL / Milvus 时需要）
- gbrain CLI（Schema 官方校验、Wiki 编译和筛选 MCP 查询需要）

MinerU、Milvus 和 Docker Agent Sandbox 都是按能力启用的增强组件。知识库 Catalog 和任务管理依赖 PostgreSQL；不使用图文向量检索时可以关闭 Milvus 索引，继续使用本地文件检索。

### macOS / Linux（推荐）

先启动本地基础设施。脚本会检测 5432 端口：已有本机 PostgreSQL 时保留它，否则启动 PuddingClaw 内置 PostgreSQL；同时启动 Milvus。

```bash
chmod +x scripts/start-local-infra.sh scripts/start-macos-linux.sh
./scripts/start-local-infra.sh
```

再启动后端与前端：

```bash
./scripts/start-macos-linux.sh
```

默认地址：

- 前端：http://127.0.0.1:3000
- 后端 API：http://127.0.0.1:8888
- OpenAPI：http://127.0.0.1:8888/docs
- PostgreSQL：`127.0.0.1:5432`
- Milvus：`localhost:19530`
- MinerU API：`localhost:8002`

首次进入应用后，在“设置 → 模型服务”登记 Provider 和模型。模型配置不再要求以 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` 作为唯一入口。

### 可选：启动 MinerU

复杂 PDF、扫描件、中文 OCR 和图文分离建议使用 MinerU：

```bash
python scripts/setup-mineru.py --foreground
```

如果只处理 Markdown、表格或数据库，可以不启动 MinerU。

### 手动开发

后端：

```bash
cd backend
uv sync --group dev
uv run python -m uvicorn app:app --reload --host 0.0.0.0 --port 8888
```

前端：

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 3000
```

自定义端口：

```bash
BACKEND_PORT=9000 FRONTEND_PORT=4000 ./scripts/start-macos-linux.sh
```

### Docker 全栈

仓库仍保留全栈 Compose，可用于容器化启动核心服务：

```bash
cp backend/.env.example backend/.env  # 首次启动时执行；Provider 仍可进入应用后配置
docker compose up --build -d
```

当前本地开发更推荐“`docker-compose.infra.yml` 启动基础设施 + 启动脚本运行 frontend/backend”，便于热更新和使用本机知识目录。

## 项目结构

```text
PuddingClaw/
├── backend/
│   ├── api/                 # Chat、Agent、Knowledge、Analytics、Config 等 API
│   ├── graph/               # DeepAgents 编排、Middleware、Session 与 Trace
│   ├── harness/             # 权限、Goal/Run、验收、Backend 与执行控制
│   ├── knowledge/           # 知识库解析、检索、Catalog 与任务管线
│   ├── analytics/           # 数据 Catalog、NL2SQL、语义运行时与项目导出
│   ├── semantic-assets/     # 度量值、维度、颗粒度、资产关联
│   ├── analytics-models/    # 分析模型、Reference 与报告模板
│   ├── sql-guardrails/      # 可迁移 SQL 守卫文档
│   ├── skills/              # Skill 包与 Reference
│   ├── projects/            # 项目级上下文
│   ├── sessions/            # 会话、Todo、Goal、Trace 与 Evidence 持久化
│   └── config.json          # 桌面设置持久化（注意凭证）
├── frontend/                # Next.js 工作台
├── electron/                # 桌面壳与本机服务管理
├── scripts/                 # 应用、基础设施与 MinerU 启动脚本
├── designs/                 # UI 设计验证稿
├── docker-compose.yml       # 容器化核心服务
└── docker-compose.infra.yml # 本地 PostgreSQL / Milvus 基础设施
```

## 架构与数据边界

| 层 | 当前职责 |
| --- | --- |
| Next.js / Electron | 对话、知识库、智能问数、设置、Trace 与桌面集成 |
| FastAPI | API、SSE、配置、任务、Catalog 和运行时编排 |
| DeepAgents / LangGraph | 模型—工具循环、Middleware、状态与 Checkpoint |
| PostgreSQL | 知识文档、导入任务、数据源与 Catalog 等业务事实 |
| 本地文件系统 | 原始知识、解析 Artifact、语义资产、模型、模板、会话与导出项目 |
| Milvus | 知识库文本 / 图片向量与 Vanna 训练索引；均应可重建 |
| MinerU | 可选的高质量 PDF 解析服务，不拥有最终知识资产 |
| LLM Wiki + gbrain（P0 开发中） | 已落统一 Schema、raw/Wiki/index/log/AGENTS 协议、gbrain 校验、Agent 工具、筛选 MCP 与 Schema Studio；PostgreSQL 托管和 generation 迁移待续 |
| Provider / AI Gateway | 对话、视觉、Embedding 与 Rerank；支持直连和可选网关模式 |

增强服务失败时按能力降级，但不会伪装成“能力仍然可用”：状态页和 Trace 会展示实际 Backend、缺失能力和降级路径。

## 验证与开发

```bash
# 后端测试
cd backend
uv run pytest

# 后端静态检查
uv run ruff check .

# 前端生产构建
cd ../frontend
npm run build
```

后端 OpenAPI 以运行中的 `/docs` 为准；README 不再手工维护容易过期的完整端点列表。

## 当前状态与边界

已经形成可用闭环的能力包括知识导入与检索、表格 / 数据库资产管理、Profile、语义资产、分析模型、SQL Guardrails、跨源维度任务、查询结果存储、结构化 Trace，以及分析项目导出。

仍在持续演进或尚未实施的方向包括：

- LLM Wiki 文件协议与编译契约、贯穿式 Schema、gbrain/PostgreSQL 托管运行时、筛选后的 MCP 查询与 Brain 可移植打包；
- 多用户 / 组织级 RBAC 与完整企业权限后台；
- 更通用的外部 HTTP / MCP 副作用 receipt 与幂等执行层；
- 语义资产的更强确定性编译、自动评估与版本治理；
- 开源发行所需的根目录 License、CONTRIBUTING、SECURITY、CI 与完整脱敏检查。

以上方向仍处于规划或持续演进阶段，不应被理解为当前稳定功能。

## License

后端包元数据当前声明为 MIT；仓库在正式对外发行前仍需补齐根目录 `LICENSE` 和第三方许可证说明。
