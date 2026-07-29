# gbrain 融合：统一知识库与 Agent 记忆体系方案

> 状态：设计提案（未实施）
> 范围：个人知识库管理、Agent 跨 session 记忆、知识库可移植打包、MCP 双向接入
> 非目标：替换现有 MinerU 解析管线与 Milvus 多模态 RAG（文档问答继续走现状）；改动 analytics/Vanna 域
> 架构决策（2026-07-29 与用户确认）：**fork gbrain 源码进项目（参照 `backend/vanna` 先例），管理面在 PuddingClaw（fork/构建/配置/生命周期/前端），使用面统一走 MCP——我们自己的 agent 也以 MCP client 身份调用 gbrain，与外部 Agent 同一条路径。**

## 第一性原理推导

1. **知识库 = 内容 + 元数据/schema + 索引 + 访问接口**。内容是源数据（人类可读、可 diff）；索引是派生数据（可重建）；接口必须是与运行时无关的标准协议。
2. **可移植性的底线**：能打包带走的是"内容 + schema + 接口声明"。绑定外部服务的架构天然不可移植——gbrain 的 PGLite 内嵌形态（零外部服务，数据=一个目录）正好满足，这也是 fork 它而非重写的核心理由：**它的存储引擎和 schema 已经被生产验证（146K pages 实例），重写在 Python 上只是重复劳动且追不上它的迭代**。
3. **个人知识库与 Agent 记忆同构**：条目化内容 + 语义召回 + 生命周期管理。gbrain 的 pages（知识）+ facts（热记忆）一套模型同时覆盖两个诉求；我们现状是三处割裂的记忆 + 一个隐式 default 知识库。
4. **管理与使用分离**：谁管进程生命周期、配置、数据目录（管理面），和谁怎么读写知识（使用面），是两个正交问题。使用面统一成 MCP 的好处：我们的 agent、外部 Agent（Claude Code/Codex/另一个 PuddingClaw）、打包带走后的任何宿主，走**完全同一条访问路径**，可移植性自动成立，也避免我们在 Python 侧再造一套客户端语义。
5. **模型绑定间接化**："模型打包带走"= 带走能力引用（embedding/chat/rerank 的 provider:model 声明），到目标环境重绑定——gbrain 的 ai gateway recipes（dashscope/zhipu/ollama…23 家）与我们的 provider_registry 能力模型可以直接对接。

## gbrain 是什么（fork 对象的事实清单）

- Bun/TypeScript，MIT 许可；CLI + 库 + MCP server 三形态，全部收敛到同一个 operations 注册表（154 个 op）。
- 存储：PGLite（WASM 内嵌 Postgres 17 + pgvector）= 默认零服务形态，数据落 `brain.pglite/` 目录；Postgres 外部引擎可选。
- schema：`pages`（slug/type/frontmatter/软删/版本）、`content_chunks`（embedding HNSW + tsvector GIN）、`links`（typed edge，写入时零 LLM 正则抽取）、`facts`（置信度衰减 + supersession 审计的热记忆）、`sources`（库内多租户）、schema pack（类型系统声明式配置，默认 15 类）。
- MCP：stdio（`gbrain serve`，本地无鉴权）+ HTTP（`gbrain serve --http`，OAuth 2.1 + Bearer token，30+ tools 与 CLI 同源）。
- 关键约束：**单进程写锁**（同一数据目录同时只允许一个进程打开，必须由管理面保证单例）；运行时必须随应用分发预编译二进制（`bun build --compile`，官方只有 darwin-arm64/linux-x64，Windows 需我们自己构建）；默认模型指向云 API（init 时显式配置）。
- fork 先例：`backend/vanna`（vendored 源码 + `backend/knowledge/vanna-ai_vanna_tracker.md` 记录 upstream 版本与本地补丁）。gbrain 沿用同一约定。

## 目标架构

```
┌──────────────────────────── PuddingClaw ───────────────────────────────┐
│ 管理面（我们的代码）                                                    │
│   Electron/后端 GBrainManager：init / spawn / 健康检查 / 停止 / 升级迁移 │
│   config.json `gbrain` 节 + /api/gbrain/* + 前端 GBrain 配置卡片       │
│   密钥注入：provider_registry CredentialStore → 子进程 env（不落明文）  │
│ ────────────────────────────────────────────────────────────────────── │
│ 使用面（统一 MCP）                                                      │
│   DeepAgents 主运行时 ─┐                                                │
│   legacy Chat agent   ─┼─ MCP client ──► gbrain serve（托管子进程）    │
│   外部 Agent / 其他宿主 ─┘    stdio 或 streamable-http（localhost+token）│
│ ────────────────────────────────────────────────────────────────────── │
│ 被管理对象（fork + 数据）                                               │
│   third_party/gbrain/     vendored 源码 + tracker（upstream 版本/补丁） │
│   运行时二进制           bun build --compile 产物（随应用分发）          │
│   <userData>/gbrain/      GBRAIN_HOME：config.json + brain.pglite/     │
│                           + raw/ + content/（wiki）+ schema/ + mounts  │
└─────────────────────────────────────────────────────────────────────────┘
```

现有 Milvus 多模态 RAG（MinerU 文档问答）保持不动，与 gbrain 分工：**稳定、需要跨源合成与治理的知识走 gbrain（编译式）；高频变动的文档问答走向量 RAG**。P4 再考虑查询路由器做架构级 RRF 融合（课程验证的混合架构）。

## 编译契约（写入侧方法论，来自 LLM Wiki 课程）

gbrain 解决"怎么存、怎么查"，LLM Wiki（编译式 RAG）解决"知识怎么被 LLM 可信地整理出来"。核心交付物是**一个 schema 文件 + 一份 AGENTS.md 行为契约**：

**三层所有权**：

| 层 | 拥有者 | 可变性 |
|---|---|---|
| `raw/` 原料层（原始文档、MinerU 解析产物、剪藏） | 人 | LLM 只读、绝不回写 |
| `content/` 知识层（编译产出的互链 wiki 页） | LLM 拥有并持续维护 | LLM 按契约读写改 |
| schema 规则层（schema pack + AGENTS.md 编译契约） | 人 | LLM 只读执行 |

**三种操作**：`Ingest`（读 raw + schema → 只写 content/wiki + 更新 index.md + 追加 log.md）、`Query`（只读产物，禁读 raw）、`Lint`（只读巡检断链/孤儿/过期，只出报告）。

**schema 与 gbrain 对齐（融合关键）**：wiki 编译 schema 直接采用 gbrain 约定——平铺目录 + slug 文件名；frontmatter `title/type/sources/updated`，type 取自 schema pack；互链用**带目录前缀的 `[[people/slug]]`**（裸 `[[slug]]` 会静默降级成无类型 mentions 边，课程实证）。`index.md`/`log.md` 保留为人读视图。**gbrain 最终加载的就是 wiki**：`gbrain import content/ --source fs` 建 pages + typed links，brain.pglite 是 wiki 的可重建索引视图。

**brain-first 行为契约**（写进项目 AGENTS.md，课程 Part 3 验证）：Search-First（答前先查 brain）/ Write-Back（答后 put_page 写回）/ Cite（标注来源 slug）。接了 MCP 工具 agent 仍然失忆——记忆的开关是行为契约不是工具。

**聊天快速捕获（chat capture）**：对话中临时丢一段原料（灵感、截图文字、会议纪要）是高频场景，它不违背 raw 原则——**这段话本身就是原料**。处理模型是**单一编译路径**（我们项目独有的一层）：

1. 原文一字不动落 `raw/inbox/<date>-<slug>.md`（带时间戳、来源 session 标记）；
2. **立即"进wiki"**：由 agent 当场编译——归入合适的 type、新建或并入既有 wiki 页、写 frontmatter、建立前缀互链、更新 `index.md`；
3. 机械收尾走 `brain_sync` tool：校验 frontmatter/schema → 追加 `log.md` → 触发 `gbrain import` 同步 → 返回页面数/建边数。

**tool 与 skill 的分工**：进 wiki 是编译判断（归类、并页、建链、措辞），只能由 LLM 做，抽象为 skill；编译的机械关卡（schema 校验、log 格式、gbrain 同步）必须确定性，抽成 `brain_sync` tool。**LLM 永远不直接写 brain——`brain_sync` 是 brain 的唯一写入入口**，保证 brain.db 每条内容都有对应 wiki 源文件（brain 永远是 wiki 的可重建索引视图，可移植性不被破坏）。

落地为两个 managed skill（操作化编译契约）：

- `brain-capture`：快速通道——存 `raw/inbox/` → 当场精简编译进 wiki → 调 `brain_sync` 收尾 → 确认回复；
- `brain-ingest`：批量通道——遍历 `raw/`（含积压 inbox）→ 完整编译/重组 wiki → `brain_sync` 同步 → 附 Lint 报告（断链/孤儿/过期）。

参考：gbrain 自带 43 个 skills 的写法；我们的 managed skill 体系在 `backend/skills/`。

**查询分档与成本边界**：编译产物全文直读约 21x 于向量 top-k（课程实测 165 万 vs 8 万 token/次）——高频定位走 `search`，高价值综合走 `think`/直读 wiki。answer-jitter 与 stale-index 两种范式都解决不了，靠 Lint 节奏 + 重新编译治理。

## 记忆生命周期（跨 session 闭环，走 MCP）

1. **沉淀**：session/run 结束时后台任务抽取候选 facts，经 MCP `put_page`/`recall` 系 tools 写入 facts（gbrain 原生置信度衰减 + supersession 审计）。
2. **召回**：DeepAgents MemoryMiddleware 改造——不再全文注入 MEMORY.md，改为经 MCP `recall`/`search` 按任务语义取 top-N facts + pages 注入 `<agent_memory>`，带 token budget。
3. **维护**：`forget_fact`（软删）、supersede 审计链、定时 decay。现有三个 MEMORY.md 作为遗留一次性 import 进 gbrain，之后只读导出兼容。
4. 注意课程边界：gbrain 偏稳定知识，**高频对话碎记忆**若日后不够用，Mem0/Zep 是备选（现有 mem0_manager 已是可选后端，不堵路）。

## 可移植打包

**真正的资产是 raw 和 wiki；gbrain 是 wiki 的运行时增强。** 生产只有两步：agent 按 schema/AGENTS.md 把 raw 编译成 wiki（LLM 判断，skill 承载，不碰 gbrain）；`brain_sync` 把 wiki 同步进 brain.db（机械，tool 承载）。可移植性因此分三层：

1. **raw + content(wiki) + schema** —— 任何环境可带走，甚至不需要 gbrain：文件系统型 Agent（Claude Code/Codex）直接读 wiki 即可使用（index.md 导航）；
2. **brain.pglite** —— 可选携带（省重建，但绑定当时的 embedding 维度）；不带就在目标环境 `gbrain import` 重建，派生数据；
3. **MCP** —— 有 gbrain 运行时的标准访问接口，不是数据本身。

推论：`brain_sync` 允许延迟或批量执行——wiki 落盘即知识安全，同步只是索引问题。

一个 Brain = GBRAIN_HOME 目录：

```
my-brain/
├── config.json       # gbrain 配置：引擎、模型能力引用（provider:model，不含密钥）
├── brain.pglite/     # 内嵌数据库（pages/chunks/links/facts + 向量索引）
├── raw/              # 原料层（只读源文档）
├── content/          # 知识层：互链 wiki markdown + index.md + log.md
├── schema/           # schema pack + AGENTS.md 编译契约
└── mcp.json          # MCP 启动声明（任何 MCP 宿主加载即用）
```

- **导出/导入**：目录整体打包 ZIP。pglite 目录带走即索引也带走（embedding 维度已固化）；若目标环境要换 embedding 模型，重跑 `gbrain import` 重建（内容优先原则）。模型引用只写能力名，密钥由目标环境 provider_registry 重新注入。
- **跨 Agent 携带**：任何支持 MCP 的 Agent 按 `mcp.json` 启动 gbrain serve 指向该目录即可读写——使用面统一 MCP 的回报就在这里。
- 与现有"分析项目导出编译层"（`backend/analytics/project_export/`）同一哲学，共用导出框架约定。

## 前端 GBrain 配置（设置页新分区）

配置卡片（管理面入口）：

- **启用开关 + 运行状态**：running/stopped/error、版本、数据目录大小、pages/facts 统计（走 gbrain `get_stats`/`get_health`）。
- **数据目录**：GBRAIN_HOME 路径（默认 `<userData>/gbrain`），可改；多 brain 挂载（mounts）后续暴露。
- **引擎**：PGLite（默认，可移植）/ 外部 Postgres（填连接串，重型场景）。
- **模型**：embedding / chat / reranker 三栏，选项来自 provider_registry 已有模型（dashscope/zhipu/ollama recipes 与我们的 provider 对齐）；保存时后端把对应 api_key 从 CredentialStore 注入子进程 env，**不落明文进 gbrain config.json**。
- **MCP 暴露**：stdio（内部 agent 默认开）+ HTTP 端口开关（外部接入，token 生成/吊销，复用 CredentialStore）。
- **schema pack**：选择/校验（`gbrain schema validate`），AGENTS.md 编译契约模板编辑入口。
- **运维操作**：初始化（init）、升级迁移（upgrade）、Lint 报告查看、导出/导入 brain 包（复用 import job 任务中心展示进度）。

后端 API：`GET/PUT /api/gbrain/config`、`POST /api/gbrain/init`、`GET /api/gbrain/status`、`POST /api/gbrain/export|import`；MCP server 注册表自动登记 gbrain（管理面写入，用户不可误删）。

## MCP 接入（使用面）

- **我们对 gbrain**：`backend/mcp_clients/servers.py` 注册表配置化（名称/transport/command+env 或 url/enabled），gbrain 作为第一个"托管 server"自动登记。PGLite 单进程锁决定**全进程共用一个托管 serve**：内部用 streamable-http（localhost + token，现有 MultiServerMCPClient 已支持该传输，改动最小）；stdio 传输支持补齐（给外部桌面 Agent 场景）。
- **DeepAgents 主运行时接 MCP 工具装载**（目前只有 legacy Chat 接了），统一走权限管线；gbrain tools 按 brain-first 契约注入使用说明。
- **gbrain 本体与我们的二次开发关系**：fork 后我们的 patch 集中在管理面需要的地方（GBRAIN_HOME 默认路径、env 注入约定、admin API 补充），数据模型与检索不动，减少上游合并冲突。

## 实施分期

> 优先级：schema wiring + 个人知识库管理先行；embedding 语义检索由 gbrain 自带能力覆盖，不再需要自研索引。

- **P0 fork 落地 + 管理面 + 配置 UI**：fork 源码到 `third_party/gbrain/`（含 tracker）；`scripts/build-gbrain.sh` 产出预编译二进制；后端 GBrainManager（init/spawn/health/stop、GBRAIN_HOME、env 密钥注入、单进程锁守护）；config.json `gbrain` 节 + `/api/gbrain/*`；前端 GBrain 配置卡片；MCP 注册表配置化 + gbrain 自动登记（streamable-http）。验证：前端点"启用"→ serve 起来 → status 显示 pages/facts 统计；重启应用配置不丢。
- **P1 编译契约 + 个人知识库闭环**：定制 schema pack（个人知识类型）+ AGENTS.md 编译契约模板；raw/content 分层（含 `raw/inbox/` 快速捕获区）；`brain_sync` tool（schema 校验 + log 追加 + gbrain import 触发，brain 唯一写入入口）+ `brain-capture` / `brain-ingest` 两个 managed skill；现有文档导入管线产物进 `raw/`；agent 按契约编译进 `content/`；前端 wiki 管理页（pages 列表/详情/编辑/软删/links 展示）。验证：聊天丢一段话 → 当场编译进 wiki → brain_sync 同步后可检索并进入图谱；记录 → 编译成 wiki → 图谱建边 → search 找回 → 软删恢复，全流程 e2e。
- **P2 记忆统一**：DeepAgents MemoryMiddleware 改 MCP recall 检索注入；session 结束 facts 沉淀任务；MEMORY.md 遗留导入；brain-first 契约写进项目 AGENTS.md。验证：session A 产出事实，全新 session B 命中召回。
- **P3 打包分发**：brain 目录导出/导入 ZIP + mcp.json + 导入时模型重绑定与索引重建；DeepAgents MCP stdio 传输补齐（外部宿主场景）。验证：包在另一个 PuddingClaw 实例导入可用；Claude Code 按 mcp.json 直连完成 search/recall。
- **P4（可选）**：links 图谱可视化、dream cycle 夜间维护（去重/矛盾检测）、稳定知识 gbrain + 实时文档 Milvus RAG 的查询路由器（架构级 RRF）、Windows 预编译目标。

## 关键风险与对策

| 风险 | 对策 |
|---|---|
| Bun 运行时/构建链进仓库 | 只 vendor 源码 + CI 构建单文件二进制，开发机不要求装 Bun；构建脚本固定 Bun 版本 |
| PGLite 单进程写锁 | GBrainManager 单例守护 + 父进程看门狗（沿用 gbrain serve 自带的 60s boot 超时与看门狗设计），禁止绕过管理面裸 spawn |
| Windows 无官方预编译目标 | P0 只支持 macOS/Linux（现状一致），Windows 构建列入 P4 |
| fork 与上游高速迭代分叉（19 个迁移模块） | tracker 文件记录 upstream commit + 本地 patch 清单；patch 只碰管理面；每季度 `gbrain upgrade` 流程跟进 |
| 默认配置指向云 API | init 流程强制显式选择 embedding/chat 模型（来自 provider_registry），不做隐式默认 |
| Memory 注入改 MCP 检索式后召回质量波动 | P2 保留全文注入 fallback 配置；召回面板可观测 |
| 高频对话碎记忆不适合 gbrain | 课程边界已明示；mem0 后端保留为备选，不堵路 |

## 参考

- gbrain 源码（本地 `/Users/pet/Code/AI/Agent/源码合集/gbrain`，v0.42.67.0，MIT）：`src/schema.sql`、`src/core/operations.ts`、`src/mcp/server.ts`、`src/commands/init.ts`（init 流程与 GBRAIN_HOME 约定）、`src/core/pglite-engine.ts`（单进程锁）。
- LLM Wiki / 编译式 RAG 课程（本地 `2026全年班_大模型Agent智能体开发实战/【Part 15】编译式 RAG`）：三层所有权与 Ingest/Query/Lint（Part 1 `llm-wiki-demo/CLAUDE.md`）；wiki → gbrain import 融合与 `[[dir/slug]]` 前缀坑（Part 2 第 4 章）；brain-first 契约与跨会话验证（Part 3 第 5 章）；21x 成本与混合架构（Part 1 S013 / Part 3 7.2 节）。
- fork 先例：`backend/vanna/` + `backend/knowledge/vanna-ai_vanna_tracker.md`。
- 现状：`backend/mcp_clients/servers.py`（MCP client 注册表）、`backend/graph/deepagents_manager.py`（MemoryMiddleware）、`backend/knowledge/`（Milvus 文档 RAG，保持不动）。
- 既有设计：`docs/portable-analysis-project-and-shared-semantic-runtime-plan.md`（导出编译层哲学同源）、`docs/deepagents-project-memory.md`（项目记忆方案 C，本方案是其向统一记忆层的演进）。
