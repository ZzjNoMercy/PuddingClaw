# PuddingClaw 用户目录与用户状态分层迁移方案

> 状态：Draft（用户配置与持久化路径审计完成，待实现）
>
> 日期：2026-08-11
>
> 核心范围：将用户配置、Credential、Session、扩展、画像、记忆和其他用户事实状态从源码目录或分散状态目录迁移到 `PUDDINGCLAW_HOME`，并建立跨平台、Docker/Kubernetes 可复用的路径契约
>
> 默认用户目录：macOS/Linux 为 `~/.puddingclaw`，原生 Windows 为 `C:\Users\<用户名>\.puddingclaw`

## 1. 结论

PuddingClaw 应把“随应用版本发布的只读资产”和“属于当前用户的可变运行状态”分开：

- `backend/skills/` 保留为内置 Skill 发布源，运行时只读，不再承载导入、编辑、重命名、版本快照或评测结果。
- `$PUDDINGCLAW_HOME/skills/` 保存用户安装或创建的 Skill。
- Registry 预留 `origin=project`；项目 Skill 在信任门完成后按 `project > user > bundled` 参与普通 Skill 选择，受保护控制 Skill 始终禁止覆盖。
- `$PUDDINGCLAW_HOME/sessions/` 保存 Session、Trace 和 Archive。
- 前端上传、创建、编辑或 Fork 的 Skill 一律通过 Backend 管理 API 写入 `$PUDDINGCLAW_HOME/skills/`；前端不选择物理目录，也不能修改 bundled Skill。
- 模型 Provider、MCP、Web Search、Evaluation、Harness 等非敏感用户覆盖归入 `$PUDDINGCLAW_HOME/config/`；程序默认值仍在发布包中，用户文件只保存覆盖项。
- API Key、数据库密码和 Connector Token 不得明文混入配置、Catalog、任务快照、Session、Skill 或根目录，而应写入用户级加密 Credential Vault。
- 用户画像、长期记忆、项目注册表和用户创建的语义定义均属于用户状态；它们应进入 Home 或项目自身的受控目录，不能继续把源码树当事实源。
- 项目首次打开必须经过用户 Registry 中的信任门；批准前不得读取或注入项目 Context、偏好、Skill 或 MCP，项目注册/列表操作也不得写项目目录。
- `PUDDINGCLAW_HOME` 默认由操作系统用户 Home 推导，不硬编码 `/Users`、`/home`、盘符或用户名。
- 原生 Windows 默认落在 `%USERPROFILE%\.puddingclaw`，通常即 `C:\Users\Alice\.puddingclaw`。
- Docker 内使用固定 Linux 路径 `/app/.puddingclaw`，宿主路径通过 bind mount 注入；容器不能自行把 `~` 解释成宿主用户目录。
- Docker Desktop 的 SQLite `db/` 使用 nested named volume；K8s 生产首选 PostgreSQL，保留 SQLite 时只允许经验证的单写块存储。
- Session 与 Skill 迁移采用“复制、校验、切换、保留旧目录”的可恢复流程，不在启动时自动删除用户内容；Credential 在加密读回验证后必须清理旧明文，只保留加密回滚材料和非敏感迁移记录。

### 1.1 本轮复核决策

| 复核项 | 决策 | 依据与落点 |
|---|---|---|
| 1. 项目首次信任门 | 采纳，P0 安全前置 | 当前注册/列表会写项目且 Prompt Builder 无条件读取 Context；第 6.2 节改为用户 Registry 信任门 |
| 2. Project Skill 层 | 调整后采纳 | 本次预留 `origin=project`，信任门完成后再启用；受保护 Skill 禁止覆盖，普通 Skill 显式 `project > user > bundled` |
| 3. SQLite 与 bind mount | 采纳，并修正证据表述 | OpenClaw `/mnt/c` 测试证明路径隔离，不直接证明 SQLite；Docker `db/` 改 named volume，K8s 首选 PostgreSQL |
| 5. 顶层 `workspace/` | 采纳删除 | 当前已注册项目、未绑定 Agent Workspace、Headless 项目根已有不同所有者，不能再造无主目录 |
| 6. Fork 上游更新 | 采纳 | Registry 保存 Fork 基线 digest/version，升级后显示非阻断上游更新状态 |
| 7. Session 根混排 | 不改布局，固化约束 | 当前实现只扫描 `*.json`，不会误认子目录；补普通文件/Schema 过滤与回归测试，避免无收益迁移 |
| 8. Credential reveal | 采纳删除 | 不保留受限 reveal 路由，只允许替换、测试、删除，写入后永不返回完整 Secret |

目标布局：

```text
PuddingClaw source/package
└── backend/
    ├── skills/                 # bundled，只读、随版本发布
    ├── prompts/                # bundled，只读
    └── ...

PUDDINGCLAW_HOME/
├── config/
│   ├── settings.json          # 非敏感用户 overrides，不是 defaults 完整快照
│   ├── providers.json         # Provider 元数据与 credential_ref，不含明文密钥
│   ├── mcp.json               # MCP server 元数据与 Secret 引用
│   ├── web-search.json        # Provider 开关、路由与选项
│   └── evaluation.json        # Evaluation/LangSmith 非敏感设置
├── profile/
│   ├── SOUL.md                # 用户覆盖；缺失时回退 bundled prompt
│   ├── IDENTITY.md
│   ├── USER.md
│   └── AGENTS.md
├── memory/
│   ├── global/
│   └── projects/
├── projects/
│   └── registry.json          # 本机项目书签、可信权限策略和执行偏好
├── sessions/
│   ├── <session-id>.json
│   ├── traces/
│   └── archive/
├── skills/                     # user-managed，可写
│   └── <skill-id>/
├── definitions/                # user-managed 声明式业务资产
│   ├── semantic-assets/
│   ├── analytics-models/
│   └── sql-guardrails/
├── knowledge/                  # 默认用户知识根；也可显式绑定外部目录
├── db/                         # 原生桌面；容器可由独立 Volume 覆盖
│   ├── catalog.sqlite3
│   └── evaluation.sqlite3
├── data/
│   ├── skill-management/
│   ├── skill-evals/
│   ├── attachments/
│   ├── query-results/
│   ├── usage/
│   └── agent-workspaces/
├── cache/                      # 可重建索引、模型发现与下载缓存
├── runtime/                    # 已有受管 Node/Python Runtime
├── users/
│   └── <owner>/
│       ├── credentials/       # 加密后的模型/数据库/Connector Credential
│       └── skill-secrets/
│           └── registry.enc   # 已有 Skill Secret Vault
├── state/
│   ├── backend.lease           # 用户根单写者 Lease
│   ├── headless-idempotency.json
│   └── jobs/
├── tmp/                        # 可安全回收的临时文件
├── logs/
└── migrations/
```

首个交付仍优先迁移 Session、Skill、普通设置和 Credential；本次复核同时为用户画像、项目注册表、知识根、Catalog、`semantic-assets/`、`analytics-models/`、`sql-guardrails/`、日志和缓存确定归属与目标路径。它们可以分阶段实施，但不再允许新增写入继续依赖源码目录。

## 2. 当前问题

Backend 目前以源码目录 `BASE_DIR = backend/` 同时承担应用资产和运行状态：

- `scan_skills(BASE_DIR)` 扫描 `backend/skills/`。
- `SessionManager.initialize(BASE_DIR)` 写入 `backend/sessions/`。
- Skill 管理 API、评测 API 和文件 API 直接读写 `BASE_DIR / "skills"`。
- `SkillManagementService` 将安装计划、注册表和快照写入 `backend/data/skill-management/`。
- Docker Compose 将 `./backend/sessions`、`./backend/skills`、`./backend/workspace`、`./backend/data` 等目录挂载到容器。
- 前端 Skill 导入最终调用 `get_skill_management_service(BASE_DIR)`，Skill 读取、保存和重命名 API 也直接使用 `BASE_DIR / "skills"`，所以当前上传内容会混入 bundled Skill 目录。
- 模型 Provider Registry 当前使用另一套 `PUDDINGDATA_USER_DATA_DIR` / `PUDDINGCLAW_USER_DATA_DIR` 和平台特定的 `PuddingData` 目录；`LocalCredentialStore` 将 API Key 明文写入 `credentials.json`。这既没有统一到 `PUDDINGCLAW_HOME`，也不满足静态加密要求。
- 旧设置更新链路仍接收并可能短暂写入 `backend/config.json` 中的 `api_key`，即使后续加载会迁出和清理，也不应让明文密钥经过源码目录配置文件。
- `backend/config.json` 同时混合程序默认值、用户覆盖、部署参数、MCP 定义和数据库密码；保存完整合并结果会把旧版本默认值固化为用户配置，阻碍后续升级默认策略。
- `backend/data/evaluation-settings.json`、Web Search Registry 和 LangSmith Credential 使用各自的设置文件，但仍复用明文 `LocalCredentialStore`；生命周期与主设置不一致。
- `KnowledgeDatabaseSource.password` 目前明文存入 Catalog 数据库，Vanna 实体导入任务还会把包含密码的数据库源快照复制到 `job_metadata`，形成不必要的 Secret 副本。
- Provider 配置 API 存在显式 reveal 完整 Credential 的路由；这与“设置页只显示已配置状态和掩码”的目标边界冲突。
- `backend/prompts/*.md` 是发布资产，但设置页编辑的是 `backend/workspace/{SOUL,IDENTITY,USER,AGENTS}.md`；读取与编辑来源不统一，用户画像没有稳定事实源。
- `ProjectRegistry.register()`/`list_projects()` 会调用 `ensure_project_context()` 写项目目录，DeepAgents Prompt Builder 又会无条件读取 `PROJECT_CONTEXT.md`；未受信仓库可在首次打开时静默注入 Prompt。
- 项目注册表、项目可信权限、Worker Access Key 哈希、Evaluation 数据、Token 用量、附件、查询结果、长期记忆和 SQLite Catalog 都写在 `backend/data`、`backend/memory` 或 `backend/storage`。
- 用户可通过前端创建或修改 `semantic-assets`、`analytics-models` 和 `sql-guardrails`，但它们与版本控制中的 bundled 定义共享目录，存在与 Skill 相同的来源混淆。

这会带来以下问题：

1. 拉取代码、切换分支、覆盖安装或删除源码目录时，用户 Session 和自定义 Skill 容易丢失。
2. 内置 Skill 与用户 Skill 共享可写目录，无法可靠判断来源、升级策略和信任等级。
3. 源码树持续产生 Session JSON、Trace、临时文件、评测结果和运行缓存。
4. 打包后的应用目录可能只读，现有路径契约无法用于正式桌面安装。
5. Windows 安装目录可能位于 `Program Files`，普通用户不能把 Session 或 Skill 写回应用目录。
6. Docker 当前把仓库路径当持久化路径，容器部署与桌面原生部署形成两套路由。
7. Provider 配置、Credential Vault 和 Skill Secret 已出现多套根目录与存储策略，备份、迁移、Windows 行为和 K8s Secret 注入难以形成统一契约。
8. 项目权限虽然能留在用户 Registry，但项目上下文、偏好和未来 Project Skill 仍缺少首次信任门，Prompt Injection 与权限自提升必须分别防御。

仓库已经存在 `backend/runtime_identity/paths.py`，并通过 `PUDDINGCLAW_HOME` 管理 Runtime 和 Credential。Session 与用户 Skill 应复用这套用户所有权边界，而不是再建立第二套 Home 解析逻辑。

### 2.1 第一性原理判定框架

每项数据先回答五个问题，再决定路径，不能以“当前位于 `backend/`”作为迁移依据：

1. **谁拥有**：应用发行方、部署管理员、操作系统用户、某个项目，还是某次 Session/Run？
2. **是否可变**：升级时应被替换、合并、保留，还是随任务结束清理？
3. **是否敏感**：是否包含 Secret、个人内容、文件路径、Prompt 或业务数据？
4. **是否权威**：它是不可丢失的事实源，还是可以从事实源重建的索引、缓存或快照？
5. **是否可移植**：能否跨机器恢复，还是绑定本机路径、端口、OS Credential 或集群资源？

由此得到六条不变量：

- 发布包必须可删除和替换；任何升级后仍应存在的用户内容都不能以 package/source root 为唯一事实源。
- 程序默认值属于发布包，用户配置只保存显式覆盖；有效配置是运行时合并结果，不能把整个合并结果反写成用户配置。
- Secret 与引用分离；业务记录只能保存 `credential_ref`，不能为了异步任务方便复制密码快照。
- 事实源、派生数据、缓存和日志分目录并采用不同备份/保留策略。
- 项目内容跟随项目，用户对项目的信任、授权和本机路径绑定跟随用户；仓库内容不能自行授予更高权限，也不能在信任前自动进入 Prompt 或工具发现面。
- 部署环境变量、K8s ConfigMap/Secret 和启动参数属于 Operator 层，不应被设置页自动持久化为用户值。

### 2.2 用户层级配置盘点结果

| 当前对象 | 第一性归属 | 目标 | 决策 |
|---|---|---|---|
| `backend/config.json` 普通设置 | 用户覆盖 | `$PUDDINGCLAW_HOME/config/settings.json` | 迁移；只保存相对 bundled defaults 的差异并带 `schema_version` |
| `backend/.env` | Desktop bootstrap 或 Operator 注入 | 已知用户值导入 Settings/Vault；部署值仍留环境层 | 不整文件搬迁，也不把进程环境反写到 Home |
| Provider、Endpoint、Model、Binding | 用户配置 | `$PUDDINGCLAW_HOME/config/providers.json` | 迁移；只含元数据和 `credential_ref` |
| `config.mcp` Server、启用状态、参数 | 用户配置 | `$PUDDINGCLAW_HOME/config/mcp.json` | 迁移；Secret 仅允许引用，命令和路径按平台校验 |
| Web Search 路由、Provider 选项 | 用户配置 | `$PUDDINGCLAW_HOME/config/web-search.json` | 从旧 `PuddingData/web_search.json` 迁移 |
| Evaluation/LangSmith 非敏感设置 | 用户配置 | `$PUDDINGCLAW_HOME/config/evaluation.json` | 从 `backend/data/evaluation-settings.json` 迁移，API Key 入 Vault |
| `SOUL.md`、`IDENTITY.md`、`USER.md`、用户 `AGENTS.md` | 用户画像/行为覆盖 | `$PUDDINGCLAW_HOME/profile/` | 新增覆盖层；`backend/prompts` 只保留只读默认模板 |
| `projects.json`、Pinned、执行模式、可信权限规则 | 用户对本机项目的登记和信任 | `$PUDDINGCLAW_HOME/projects/registry.json` | 迁移；增加 `pending/trusted/denied` 信任门，绝对路径标记 `machine_local`，不随通用配置盲目导入 |
| 全局/项目长期记忆 | 用户事实状态 | `$PUDDINGCLAW_HOME/memory/{global,projects}/` | 迁移；项目 `PROJECT_CONTEXT.md` 仍留在项目自身 `.puddingclaw/` |
| Knowledge Root 和搜索范围 | 用户配置+内容事实源 | 默认 `$PUDDINGCLAW_HOME/knowledge/`，也可引用外部根 | 配置迁移；外部内容不复制，Registry 保存经用户批准的机器本地绑定 |
| Semantic Asset、Analytics Model、SQL Guardrail | 用户创建的声明式定义或 bundled 定义 | 用户版本进 `$PUDDINGCLAW_HOME/definitions/` | 像 Skill 一样双根扫描、来源标记、重名冲突和 Fork；不能整目录搬迁 |
| SQLite Catalog、Evaluation DB | 用户事实状态 | 原生桌面 `$PUDDINGCLAW_HOME/db/`；容器使用独立 Volume 或 PostgreSQL | 迁移；Docker Desktop 不放宿主 bind mount，K8s 不放 RWX/NFS/SMB |
| 数据库源、MCP、LangSmith、Web Search Credential | 用户 Secret | owner 级加密 Vault | 元数据留原 Catalog/Registry，密码字段替换为 `credential_ref` |
| Worker Access Key 哈希和授权元数据 | 用户安全状态 | `$PUDDINGCLAW_HOME/users/<owner>/access/` | 迁移；Token 仅创建/轮换时显示一次，哈希不当作普通配置导出 |
| Skill 启用、信任、版本和来源 | 用户扩展配置 | `$PUDDINGCLAW_HOME/data/skill-management/` | 已纳入 Skill Registry；内容与控制面状态分离 |

`settings.json` 主要承载 RAG/Memory 模式、Thinking、Knowledge、Vanna、Analytics、Compression、Harness、Subagent 和扩展开关等普通覆盖。旧 `fallback_llm`、Embedding、Rerank 和数据库块中的连接元数据分别归一到 Provider/Source Registry，Secret 进入 Vault，避免同一事实在多个配置块重复存在。

这次扫描还发现一批“不是配置，但必须退出源码树”的相邻用户状态：附件、Headless Idempotency、Query Result、Evaluation 记录、Token Usage、DeepAgents Memory、Rewind Receipt、Agent Workspace、Semantic Build Job 和 WebBridge Artifact。它们分别进入 `$PUDDINGCLAW_HOME/data/` 或 `$PUDDINGCLAW_HOME/state/`，按是否可恢复和保留期限管理。

### 2.3 明确不迁入用户配置的对象

- `_DEFAULT_CONFIG`、Prompt 默认模板、Tool Guide、Schema、Evaluation 示例和 bundled manifest 属于发布资产，继续留在 package 中并保持只读。
- `backend/.venv`、Node/Python Runtime、Tokenizer/模型发现缓存和向量索引属于可重建 Runtime/Cache，进入 `runtime/` 或 `cache/`，不作为用户配置备份。
- `PUDDINGCLAW_HOME`、监听地址、容器端口、K8s StorageClass、管理员 Token 等部署参数继续由 CLI/环境变量/ConfigMap/Secret 管理；GUI 只显示来源，不覆盖 Operator 锁定值。
- Frontend `localStorage/sessionStorage` 中的当前 Tab、Inspector 状态、窗口 ID、临时 Session ID 和活动 Run UI 投影属于浏览器/窗口状态，默认不迁入 Backend Home。
- 项目内 `.puddingclaw/PROJECT_CONTEXT.md` 和知识根内 `.puddingclaw/table_profiles` 分别属于项目内容和知识根派生 Sidecar；前者留在项目，后者可重建且随知识根放置。
- 外部 PostgreSQL、Milvus、对象存储和 Docker/K8s Volume 中的数据不复制到 Home；Home 只保存非敏感连接元数据和 Secret 引用。

### 2.4 本次源码扫描证据

| 证据位置 | 当前行为 |
|---|---|
| `backend/config.py` | `CONFIG_FILE` 指向 `backend/config.json`；默认配置混合 LLM、数据库、Knowledge、Vanna、Analytics、Harness、Subagent 和 MCP |
| `backend/provider_registry.py` | 使用独立 `PuddingData` 用户目录；`LocalCredentialStore` 明文写 `credentials.json` |
| `backend/api/config_api.py` | 设置 API 写旧配置，并存在返回完整 Provider Credential 的 reveal 路由 |
| `backend/evaluation/settings.py`、`backend/web_search/registry.py` | 非敏感配置分散存储，但 Credential 仍复用 `LocalCredentialStore` |
| `backend/knowledge/models.py`、`database_sources.py`、`import_jobs.py` | 数据库源密码明文入库，异步实体导入 Job Metadata 复制含密码 Source Snapshot |
| `backend/projects/registry.py`、`worker_access.py` | 项目路径/权限和 Worker Access Key 哈希写入 `backend/data` |
| `backend/db.py`、`evaluation/repository.py`、`graph/token_usage_store.py` | Catalog、Evaluation 和 Token Usage SQLite 默认位于 `backend/data` |
| `backend/api/files.py`、`graph/prompt_builder.py` | Profile/Memory 编辑路径与 bundled Prompt 读取路径不一致，通用文件 API 可写多类 package-relative 目录 |
| `backend/analytics/*/registry.py`、`analytics/nl2sql/guardrails.py` | 用户创建的声明式资产直接写 bundled 目录 |
| `frontend/src/lib/store.tsx` | Tab、窗口、当前项目、活动 Run 等 UI 状态在浏览器 Storage 中，属于设备/窗口状态而非 Backend 用户配置 |

## 3. 本地开源项目对照

本方案参考了本机开源源码快照中的实际实现，而不是只参考产品文档。

| 项目 | 本地源码证据 | 可借鉴做法 |
|---|---|---|
| OpenAI Codex | `codex-rs/utils/home-dir/src/lib.rs`、`sdk/typescript/src/codex.ts`、`scripts/install/install.ps1` | `CODEX_HOME` 可覆盖，默认 `~/.codex`；Session 位于 `~/.codex/sessions`；Windows 安装脚本使用 `%USERPROFILE%\.codex` |
| OpenClaw | `src/config/paths.ts`、`docs/openclaw-agent-runtime.md`、`src/docker-setup.e2e.test.ts`、`src/infra/sqlite-*` | 用一个可覆盖的 State Root 统一 Session、Workspace、Credential 和数据库；保留旧目录发现；`/mnt/c/Users/...` E2E 证明路径隔离，SQLite 另有 WAL、`busy_timeout` 和损坏维护逻辑，二者不可混为同一证据 |
| Pi | `packages/coding-agent/src/config.ts`、`core/trust-manager.ts`、`core/package-manager.ts`、`docs/sessions.md`、`docs/skills.md` | 使用 `os.homedir()`；Session 位于 `~/.pi/agent/sessions`；项目资源受目录信任控制；明确区分项目、用户和包内 Skill 的优先级 |
| Claude Code | `rust/crates/runtime/src/trust_resolver.rs` | 文件夹信任区分自动信任、要求审批和拒绝，并把 allowlist/denylist 与运行时事件分开 |
| DeepAgents | `libs/code/deepagents_code/skills/trust.py`、`config.py` | 项目与用户 Skill 多层发现；规范化 Skill 目录持久信任，并在使用时重新校验以防符号链接换目标 |
| Hermes Agent | `hermes_constants.py`、`docker-compose.windows.yml` | 单一 `HERMES_HOME` 路径函数；Windows 与 POSIX 分支明确；Windows Compose 使用 `${USERPROFILE}` 挂载用户状态 |
| OpenCode | `packages/core/src/global.ts`、`packages/opencode/src/config/paths.ts` | 统一的 Path 对象；区分 data/cache/config/state/tmp；发现用户级与项目级配置时不把路径散落在业务模块 |

由这些项目可以提炼出五条共同原则：

1. 用户根目录必须只有一个解析入口，并允许环境变量覆盖。
2. Session 等可变状态不写入源码或安装目录。
3. 内置扩展、用户扩展和项目扩展具有不同来源与优先级。
4. 容器内部路径和宿主路径是两个命名空间，必须显式映射。
5. Windows 路径由平台 API 或 `%USERPROFILE%` 推导，不能拼接 Unix Home。

## 4. 路径契约

### 4.1 唯一解析顺序

宿主 Backend 使用以下顺序解析用户数据根：

1. 非空的 `PUDDINGCLAW_HOME`。
2. `Path.home() / ".puddingclaw"`。

规则：

- `PUDDINGCLAW_HOME` 必须是绝对路径。
- 解析后使用 `resolve(strict=False)` 得到规范化绝对路径。
- 不允许业务模块再次读取 `HOME`、`USERPROFILE`、`APPDATA` 或自行拼接 `.puddingclaw`。
- 启动时验证根目录可创建、可读写且不是普通文件。
- 同一用户根默认采用单写者模型；Backend 启动时持有根级实例 Lease，第二个写进程必须明确失败，不能依赖进程内线程锁避免跨进程损坏。
- 测试通过依赖注入或临时 `PUDDINGCLAW_HOME` 隔离，不能写开发者真实 Home。

首期身份模型明确为“一个 `PUDDINGCLAW_HOME` 对应一个可信桌面 owner”；请求体中的 `user_id` 不能选择物理根或 Credential principal。未来若提供真正多租户服务，应为每个租户使用独立 Home/存储命名空间，或把 Session、Skill、Profile、Memory、Project 和 Config 全部一致地 owner-scope，不能只给 Credential 加 `users/<owner>` 前缀后宣称已支持多租户。

平台默认值：

| 运行环境 | 默认值 | 示例 |
|---|---|---|
| macOS | `Path.home() / ".puddingclaw"` | `/Users/alice/.puddingclaw` |
| Linux | `Path.home() / ".puddingclaw"` | `/home/alice/.puddingclaw` |
| 原生 Windows | `Path.home() / ".puddingclaw"` | `C:\Users\Alice\.puddingclaw` |
| Windows 自定义盘 | `PUDDINGCLAW_HOME` | `D:\PuddingClawData` |
| Docker 容器内 | 显式环境变量 | `/app/.puddingclaw` |
| WSL 独立运行 | Linux Home | `/home/alice/.puddingclaw` |

Windows PowerShell 覆盖示例：

```powershell
$env:PUDDINGCLAW_HOME = "D:\PuddingClawData"
```

如果 Windows 和 WSL 需要共享同一份状态，可在 WSL 中显式设置：

```bash
export PUDDINGCLAW_HOME=/mnt/c/Users/Alice/.puddingclaw
```

不建议原生 Windows Backend 与 WSL Backend 同时写同一个目录；文件锁、大小写和原子替换语义可能不同。首期也不承诺 SMB/UNC 网络共享上的并发一致性。

### 4.2 路径对象

继续扩展 `PuddingClawPaths`，让它成为用户状态的唯一投影：

```python
@dataclass(frozen=True)
class PuddingClawPaths:
    root: Path

    def sessions(self) -> Path: ...
    def session_traces(self) -> Path: ...
    def session_archive(self) -> Path: ...
    def user_skills(self) -> Path: ...
    def config(self) -> Path: ...
    def settings(self) -> Path: ...
    def provider_registry(self) -> Path: ...
    def mcp_registry(self) -> Path: ...
    def web_search_registry(self) -> Path: ...
    def evaluation_settings(self) -> Path: ...
    def profile(self) -> Path: ...
    def memory(self) -> Path: ...
    def projects(self) -> Path: ...
    def user_definitions(self) -> Path: ...
    def knowledge(self) -> Path: ...
    def databases(self) -> Path: ...
    def credentials_root(self, owner: str) -> Path: ...
    def skill_secret_registry(self, owner: str) -> Path: ...
    def data(self) -> Path: ...
    def agent_workspaces(self) -> Path: ...
    def state(self) -> Path: ...
    def skill_management(self) -> Path: ...
    def skill_evals(self) -> Path: ...
    def cache(self) -> Path: ...
    def logs(self) -> Path: ...
```

应用发布资产使用另一个明确对象，不得塞进 `PuddingClawPaths`：

```python
@dataclass(frozen=True)
class PuddingClawPackagePaths:
    root: Path                 # 当前 backend/package root
    bundled_skills: Path
    prompts: Path
```

这样可避免把 `base_dir` 同时解释成源码根、用户状态根和 Sandbox 投影根。新接口应接受精确路径对象；`initialize(base_dir)` 只作为测试兼容层逐步淘汰。

### 4.3 Windows 文件系统约束

- 使用 `pathlib.Path` 和 `os.path`，不手写 `/` 或 `\` 连接路径。
- Skill ID、Session ID 继续限制长度和字符集，并额外拒绝 Windows 保留名：`CON`、`PRN`、`AUX`、`NUL`、`COM1`–`COM9`、`LPT1`–`LPT9`。
- 安全边界比较使用规范化路径和平台大小写规则；Windows 上加入 `os.path.normcase()`，不能依赖字符串前缀。
- 测试包含带空格和 Unicode 的用户名，如 `C:\Users\Alice Zhang\.puddingclaw` 和 `C:\Users\测试用户\.puddingclaw`。
- 控制目录深度和文件名长度，避免 Session、Eval 与 Skill 版本目录叠加后接近传统 Windows `MAX_PATH`。
- POSIX 的 `0700/0600` 权限在 Windows 上只能作为 best effort；敏感数据继续依赖 Windows Credential Manager/Vault 和用户 ACL，不能把 `chmod` 成功当作安全证明。

### 4.4 配置与 Credential 边界

`PUDDINGCLAW_HOME` 是用户状态的统一所有权边界，但不表示所有文件具有相同安全等级：

| 数据 | 目标位置 | 存储要求 |
|---|---|---|
| Provider、Endpoint、Model ID、默认模型、采样参数 | `$PUDDINGCLAW_HOME/config/providers.json` | 可读 JSON，但只保存 `credential_ref`，不能保存 API Key |
| 模型 API Key、数据库密码、OAuth/Connector Token | `$PUDDINGCLAW_HOME/users/<owner>/credentials/` | AES-256-GCM 等认证加密；日志、API 响应和诊断输出必须脱敏 |
| Skill 所需环境 Secret | `$PUDDINGCLAW_HOME/users/<owner>/skill-secrets/registry.enc` | 继续使用现有 Skill 内容绑定与加密机制 |
| Session 与 Skill | 各自目录 | 不得包含可用于恢复明文密钥的副本 |

普通配置的读取顺序与安全策略要分开处理：

```text
本次调用显式参数
  > 项目级非安全偏好
  > 用户覆盖
  > Operator 提供的部署默认值
  > package defaults
```

Operator 锁定项和安全约束不采用简单的“后写覆盖”：监听边界、可访问目录、网络策略、Secret 来源和权限上限由部署策略封顶，用户或项目只能收紧，不能放宽。`config show` 和设置页必须同时显示有效值、来源、是否锁定以及用户持久值，避免把环境覆盖误认为已保存配置。

`settings.json` 只保存用户明确修改过的字段，不复制 `_DEFAULT_CONFIG` 的完整展开结果。升级时先用新版本 defaults 合并旧用户 overrides，再执行按 `schema_version` 注册的纯迁移函数；未知字段要么由对应扩展 Schema 接管，要么进入诊断，不能静默丢失。

Credential 读取优先级：

1. 部署环境提供的 `env://<NAME>`、K8s Secret 或外部 Secret Manager 引用；默认不复制到本地文件。
2. 桌面平台安全存储中的 Secret 或 Vault 主密钥：macOS Keychain、Windows Credential Manager/DPAPI，Linux 可接 Secret Service。
3. 平台安全存储不可用时，使用 `$PUDDINGCLAW_HOME/users/<owner>/credentials/` 下的加密 Vault；主密钥必须与密文分离，文件权限只作为补充保护。

Provider Registry 只保存类似以下的非敏感记录：

```json
{
  "provider": "deepseek",
  "profile": "default",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "credential_ref": "vault://users/local/credentials/model-providers/deepseek/default"
}
```

原生 Windows 需要补齐 Credential Manager 或 DPAPI Provider。现有 `MasterKeyProvider` 在 macOS 优先使用 Keychain，但 Windows 会退回本地 key 文件；在完成 Windows 安全存储接入前，这只能作为受限兼容模式并在 Doctor 中明确警告，不能宣称等同于系统 Credential Manager。

### 4.5 模型 Credential 迁移

模型密钥迁移必须是事务性的，不能只把当前明文 `credentials.json` 移到新目录：

1. 在根级实例 Lease 下读取旧 Provider Registry、`credentials.json`、Evaluation/Web Search Credential、数据库源密码，以及仍可能存在于 `backend/config.json` 的 Provider/数据库 Credential。
2. 规范化为 `<owner, credential_kind, provider_or_source, profile>`，在 staging Vault 中完成认证加密。
3. 从新 Vault 读回并验证 Provider/Profile 映射；校验过程不记录明文、长度或可逆片段。
4. 原子发布加密 Vault，再写入只含 `credential_ref` 的新 Provider Registry。
5. 原子改写旧配置和 Catalog，把 `api_key`、`password` 等字段替换为 `credential_ref`；异步任务只持久化 Source ID 和不可变非敏感快照，不复制 Credential。
6. 验证成功后删除旧明文 Credential 文件，不保留明文回滚副本；如需回滚，迁移前应生成由用户控制密钥加密的备份。
7. 任何一步失败都不能切换 Registry；Doctor 必须报告“仍存在旧明文 Secret”，而不是静默继续。

设置页提交新 API Key 时只允许单向写入：Browser 经 TLS 把 Secret 发送给 Backend，Backend 立即写入 Vault；后续查询只返回 `configured=true`、末尾掩码或更新时间，绝不回传完整 Key。Provider 测试连接也应通过 `credential_ref` 在服务端解析，不能把密钥拼进任务参数、Session、Trace 或前端状态。

现有 Provider Credential reveal 路由必须删除，不保留“本机受信管理面”例外。普通设置页、Agent Tool、Headless Worker、项目代码和管理 API 都不得拥有取回完整 Secret 的能力；写入后的合法操作只保留“替换、测试、删除”，测试过程只返回连通性和错误分类，不返回 Secret、长度或可逆片段。

迁移器按事实源拆分版本，不做一个覆盖所有目录的巨型事务，例如 `config-home-v1`、`credential-v2`、`session-home-v1`、`skill-roots-v1`、`profile-home-v1`、`definitions-roots-v1` 和 `data-roots-v1`。每个迁移拥有独立 marker、校验和回滚边界；启动协调器只负责依赖顺序和 Readiness，避免一个可重建 Cache 失败阻断已验证的 Credential 或 Session 切换。

## 5. Skill 分层与发现

### 5.1 三类 Skill 来源

| 类型 | 物理目录 | 可写性 | 更新方式 | 当前范围 |
|---|---|---|---|---|
| Bundled Skill | `<package>/backend/skills/<id>` | 只读 | 跟随 PuddingClaw 版本升级 | 本次实现 |
| User Skill | `$PUDDINGCLAW_HOME/skills/<id>` | 仅由 Skill 管理控制面可写 | 导入、创建、更新、回滚 | 本次实现 |
| Project Skill | `<project>/.puddingclaw/skills/<id>` 或兼容发现目录 | 对 Skill 管理控制面只读；由项目文件工作流维护 | 随仓库共享和评审 | Schema 本次预留，发现与加载在项目信任门完成后启用 |

内置 Skill 不在首次启动时整包复制到用户目录。复制会导致应用升级后内置 Skill 无法自然更新，也无法区分用户修改与发行版本。

Project Skill 是已知后续演进，不应通过本次双根迁移偷偷启用。它来自可能不可信的仓库，必须先满足第 6.2 节的项目级信任门；项目未受信时，Scanner 不读取 Skill 内容、不执行入口，也不把其说明注入 Prompt。为避免以后再次迁移 Registry，本次先预留 `origin=project`、`project_id` 和来源 digest。

### 5.2 注册表与稳定虚拟路径

Skill Scanner 不再只接收一个 `base_dir`，而是接收来源列表并生成统一注册表：

```text
skill_id
origin: bundled | user | project
physical_root
content_digest
mutable
version/provenance
project_id: null | <stable-project-id>
override_policy: deny | allow
effective
shadowed_sources
forked_from: null | { origin, skill_id, base_digest, base_version }
upstream_status: current | updated | missing
upstream_digest
```

Agent、Prompt 和 Trace 中使用稳定虚拟路径：

```text
/skills/<skill-id>/SKILL.md
```

不要把 `/Users/alice/...` 或 `C:\Users\Alice\...` 写入模型提示、Session 或可移植 Trace。Workspace Backend 根据注册表把虚拟路径映射到实际物理目录，Docker/Kernel 仍只读挂载当前已加载 Skill。

### 5.3 覆盖与重名规则

“控制面 Skill 不可覆盖”和“普通 Skill 可按层定制”应分开处理：

1. bundled manifest 为 `skill-management`、`skill-creator`、`skill-creator-pro`、`review` 等受保护或控制面 Skill 声明 `override_policy=deny`。任何 User/Project 同名项都标为 `protected_name_conflict`，Agent 不加载冲突来源。
2. 普通 Skill 可声明 `override_policy=allow`。项目已受信时采用 `project > user > bundled`；项目未受信时 Project 来源完全不进入候选集，采用 `user > bundled`。
3. Registry 必须返回唯一 `effective` 来源，并记录 `shadowed_sources`、各来源 digest 和选择理由；设置页、Prompt 来源标记与 Trace 都应可见，不能发生无记录的静默替换。
4. Project Skill 只能影响当前已绑定且已受信项目，不能成为全局 User Skill，也不能改变用户 Registry 中的权限、Secret 或网络授权。
5. 可执行、可加载代码、声明工具或 MCP 能力的 Project Skill，在目录信任之外还要绑定内容 digest；内容变化后进入 `approval_required`，不得沿用旧批准自动执行。

这样既保留团队通过仓库共享普通 Skill 的能力，也避免供应链内容替换控制面 Skill。项目层的实际启用依赖第 6.2 节，不属于本次目录迁移的前置条件。

### 5.4 管理 API

- 导入、保存、重命名、删除和更新只能操作 User Skill。
- Bundled 和 Project Skill API 返回 `mutable=false`；直接修改请求分别返回稳定错误 `bundled_skill_read_only` 或 `project_skill_managed_by_repository`。
- “编辑内置 Skill”实际执行 Fork：复制到 User Skill 新名称，更新 frontmatter，并记录 `forked_from` 与原始 digest。
- 安装计划、注册表和回滚快照写入 `$PUDDINGCLAW_HOME/data/skill-management/`。
- Eval、Benchmark、Viewer 输出写入 `$PUDDINGCLAW_HOME/data/skill-evals/<skill-id>/`，不再写入 Skill 包本身。
- 运行依赖继续写入 `$PUDDINGCLAW_HOME/runtime/`，Secret 继续写入用户 Vault；两者都不能进入 Skill 目录。

### 5.5 Fork 的上游更新信号

Fork 时记录 bundled 基线的 `base_digest` 和 `base_version`。每次应用升级或 Registry 刷新后，将其与当前 bundled manifest 比较：相同为 `current`，不同为 `updated`，上游已移除为 `missing`。设置页显示非阻断的“上游已更新”标记，并提供比较、重新 Fork 或人工合并入口；系统不得自动覆盖或自动合并用户 Fork。

### 5.6 内置 Skill 首轮梳理

迁移前先在构建/发布阶段生成 bundled manifest，至少记录 `skill_id`、目录、入口 digest、完整内容 digest 和发布版本。Manifest 必须来自版本控制中的发布资产或显式 allowlist，不能在用户机器首次启动时对可能已经被修改的 `backend/skills/` 现扫现认，否则会把用户改动误判成内置基线。优先确认：

1. 控制面：`skill-management`、`skill-creator`、`skill-creator-pro`、`skill-benchmark`。
2. 会写文件或引用旧路径：`qa`、`review`、`ship`、`investigate`、`hv-analysis`、`design-html`、`sql-guardrail-designer`。
3. 业务 Skill：`knowledge-search`、`database-analysis`、`table-analysis`、`build-logical-dataset`、`build-semantic-dimension`、`llm-wiki`。
4. Connector 与通用 Skill 批量标记为 bundled，后续逐个校验依赖。

`skill-creator` 与 `skill-creator-pro` 的职责收敛属于独立内容治理工作，不阻塞目录迁移；迁移阶段只需要明确它们都是 bundled、默认只读。

### 5.7 前端上传、创建与编辑

前端不应知道宿主机上的 `~/.puddingclaw`、`C:\Users\...` 或容器内路径。它继续上传文件或提交变更意图，由 Backend 进行鉴权、校验、规划和原子提交：

```text
Browser
  -> POST /api/skills/import
  -> Backend 校验并生成 install plan
  -> 用户确认
  -> SkillManagementService 写入 PuddingClawPaths.user_skills()
  -> Registry 返回 origin=user, mutable=true
```

- 上传或新建 Skill 的唯一落点是 `$PUDDINGCLAW_HOME/skills/<skill-id>/`。
- 保存、重命名、更新和删除接口先从 Registry 校验 `origin=user && mutable=true`，不得根据前端传来的路径决定目标；Project Skill 只能通过明确的项目文件编辑流程修改。
- bundled Skill 在列表中显示 `origin=bundled, mutable=false`；编辑按钮应转换为“Fork 为用户 Skill”。Project Skill 显示 `origin=project, mutable=false`、项目和信任状态。
- API 响应优先返回 `skill_id`、`origin`、`mutable` 和虚拟路径，不向浏览器暴露本机绝对路径。
- 导入包中的 `.env`、Credential 文件和疑似 API Key 默认拒绝或隔离，不能随着 Skill 内容写入用户 Skill 目录；需要的 Secret 由单独的 Secret 表单写入 Vault。

## 6. 用户画像、项目与声明式定义

### 6.1 用户画像与长期记忆

`backend/prompts/` 只保存版本化的默认人格、身份、用户模板和系统操作规则。运行时按以下顺序组合：

```text
bundled prompt defaults
  + $PUDDINGCLAW_HOME/profile/ 中存在的用户覆盖
  + 已通过信任门的项目 .puddingclaw/PROJECT_CONTEXT.md
  + Session/Run 动态上下文
```

设置页的 Memory Editor 不再通过通用 `/api/files` 写 `backend/workspace` 或 `backend/memory`，而应调用 typed Profile/Memory API。用户 Profile 是配置，长期 Memory 是用户事实状态；两者不能继续借用 Workspace 目录。全局和项目记忆也要明确 owner/project ID，避免多个用户或项目共享一个隐式 `MEMORY.md`。

`backend/prompts/AGENTS.md` 中的系统安全规则不可被用户 Profile 整体替换。实现应把可个性化内容与不可覆盖的系统约束拆成不同 Prompt 层，并对最终 Prompt 标记来源。

### 6.2 项目级与用户级边界

- `$PUDDINGCLAW_HOME/projects/registry.json` 保存项目 ID、本机绝对路径、Pinned、最近使用、执行模式、信任状态和用户授予的权限规则。
- `<project>/.puddingclaw/PROJECT_CONTEXT.md` 属于项目内容，继续跟随项目目录；它可以提供上下文，但不能授予自身文件系统、网络或 Secret 权限，也不能在项目受信前进入 Prompt。
- 可移植的项目偏好可以进入 `<project>/.puddingclaw/project.json`；本机路径、信任决策和授权记录必须留在用户 Registry。
- 跨机器恢复 Registry 时，只导入名称和可移植偏好；绝对路径先标记 `unbound`，由用户重新选择目录并确认权限。

当前实现的风险不只是“项目不能自行提权”：`ProjectRegistry.register()` 和 `list_projects()` 会调用 `ensure_project_context()` 修改项目目录，Prompt Builder 又会无条件读取 `PROJECT_CONTEXT.md`。这意味着 clone 下来的仓库可以在用户尚未表达信任前影响模型上下文，项目列表这样的读操作也会产生写入副作用。迁移时必须先拆掉这条隐式链路。

#### 项目信任门

1. 全局策略提供 `security.project_trust.default = ask | always | never`，默认 `ask`。`always` 只能由用户或管理员显式设置，不能由项目文件声明。
2. Registry 的项目记录至少包含 `trust_state: pending | trusted | denied`、规范化绝对路径、项目身份指纹、`trusted_at`、策略版本和已批准资源摘要。首期只信任精确目录，不自动信任父目录或同名新路径。
3. 首次打开包含 `.puddingclaw/`、项目级 Skill/MCP 或其他可注入资源的项目时，显示信任卡：规范化路径、可安全取得的仓库来源、发现到的资源类型和将获得的能力。未决定前进入受限模式。
4. 受限模式不读取或注入 `PROJECT_CONTEXT.md`、`project.json`、Project Skill 和项目级 MCP；明确由用户发起的普通文件读取仍遵循既有 Sandbox/权限审批，不把“项目未受信”误解为项目完全不可查看。
5. 信任决定只写 `$PUDDINGCLAW_HOME/projects/registry.json`。项目仓库不能携带或恢复 `trusted` 状态；路径重绑定、项目身份不匹配或信任策略升级时回到 `pending`。
6. `register()`、`list_projects()` 和项目探测必须保持只读，不调用 `ensure_project_context()`。创建模板只能由已受信项目中的显式用户操作触发，且不能因为“缺文件”就在列表查询时写仓库。
7. 信任变化后必须使 Prompt Cache、Agent 实例和相关 Session 的权限修订失效；下一次运行重新构建来源清单，Trace 记录哪些项目资源被纳入。
8. 文件夹信任只允许读取普通声明内容。Project Skill/MCP 中的代码、命令、工具和外部连接还要绑定组件 digest/指纹；变化后单独重新批准，不让一次文件夹批准永久授权未来内容。

本地开源实现支持这一拆分：Pi 的 `trust-manager.ts` 将项目级 Skill、设置和 Prompt 纳入目录信任；Claude Code 的 `trust_resolver.rs` 明确区分自动信任、审批和拒绝；DeepAgents 的 `skills/trust.py` 将规范化目录持久化并防范符号链接换目标。PuddingClaw 应复用相同原则，但把信任事实统一放进 Project Registry，而不是另造可漂移的项目内状态。

### 6.3 Semantic Asset、Analytics Model 与 SQL Guardrail

这三类对象和 Skill 具有同一生命周期问题，应采用双根注册表：

| 类型 | Bundled Root | User Root |
|---|---|---|
| Semantic Asset | `<package>/backend/semantic-assets` | `$PUDDINGCLAW_HOME/definitions/semantic-assets` |
| Analytics Model | `<package>/backend/analytics-models` | `$PUDDINGCLAW_HOME/definitions/analytics-models` |
| SQL Guardrail | `<package>/backend/sql-guardrails` | `$PUDDINGCLAW_HOME/definitions/sql-guardrails` |

前端创建、导入和保存只能写 User Root；bundled 对象只读，修改时 Fork。Registry 记录 `origin`、`mutable`、digest、版本和依赖，虚拟路径 `/semantic-assets`、`/analytics-models`、`/sql-guardrails` 保持稳定。现有目录包含 tracked 基线与运行时生成的 crosswalk/override/version 文件，迁移时必须通过发布 manifest 分类，不能整目录复制或移动。

### 6.4 用户事实、派生数据、缓存与日志

| 等级 | 示例 | 目标 | 备份策略 |
|---|---|---|---|
| 用户事实源 | Catalog DB、Evaluation Dataset、长期记忆、Knowledge、Project Registry | `db/`、`memory/`、`knowledge/`、`projects/` | 必须备份、迁移校验 |
| Session 关联产物 | Attachment、Query Result、Rewind Receipt、Run Evidence | `sessions/` 或 `data/`，保留 Session/Run 关联 | 按会话保留和级联清理 |
| 维护状态 | Headless Idempotency、Job Queue、Worker Access Key 哈希、迁移 marker | `state/` 或 owner 安全目录 | 按一致性需求备份，设置 TTL/压缩 |
| 可重建派生物 | Knowledge/Memory Index、Table Profile、模型发现结果 | `cache/` 或知识根 Sidecar | 可清理，不作为恢复成功条件 |
| Runtime/Scratch | Python/Node 环境、未绑定项目的 Agent Workspace、Harness Scratch、MinerU 临时输出 | `runtime/`、`data/agent-workspaces/`、`tmp/` | 默认不备份，设置回收策略 |
| 诊断日志 | LLM Input、Build Job、Token JSONL | `logs/` | 默认关闭敏感正文或脱敏，按天轮转和限期保留 |

`llm-input` 日志当前会保存发送给模型的完整内容与 Tool 参数，风险高于普通运行日志。迁移目录不是充分保护；默认应改为关闭正文、仅记录 digest/长度/结构，显式 Debug 才短期开启，并确保 Credential、数据库 URL 和个人内容经过字段级脱敏。

### 6.5 Workspace 归属

目标布局不再保留无主的 `$PUDDINGCLAW_HOME/workspace/`：

- 已注册项目的 Agent Workspace 就是经过 Registry 绑定和信任判断后的项目目录，内容所有权属于项目，不属于 PuddingClaw 用户状态。
- 未绑定项目的临时 Agent Workspace 使用 `$PUDDINGCLAW_HOME/data/agent-workspaces/unscoped/default`，由 Backend 管理生命周期。
- Headless Worker 的默认项目根由 `PUDDINGCLAW_PROJECTS_ROOT` 显式指定；它保存用户项目内容，不应隐式变成 `$PUDDINGCLAW_HOME/workspace`。
- 旧 `workspace/TODO.md` 等兼容写入按 Session/未绑定 Workspace 的真实归属迁移，不能为了兼容继续创建顶层无主目录。

## 7. Session 迁移

### 7.1 新写入位置

`SessionManager` 改为接收明确的 `sessions_dir`：

```text
$PUDDINGCLAW_HOME/sessions/
├── <session-id>.json
├── traces/<session-id>.json
└── archive/<session-id>_<timestamp>.json
```

Session、Trace、Archive、Todo、Goal、Run、Evidence 和权限状态必须一起迁移，不能只复制聊天消息 JSON。

根目录继续采用扁平的 `<session-id>.json`，本次不增加 `sessions/main/`：当前 `SessionManager` 使用 `sessions_dir.glob("*.json")`，不会把 `traces/` 或 `archive/` 子目录当作 Session。实现期要把这一点固化为存储契约和回归测试：列表器只接受满足 Session ID/Schema 的普通 `.json` 文件，忽略目录、临时文件、符号链接和未知 JSON；未来若改成遍历所有 entry，再以独立 Schema 版本迁移到 `main/`。

### 7.2 迁移算法

首次启动执行 `runtime-home-v1` 迁移：

1. 获取用户根实例 Lease 和 `$PUDDINGCLAW_HOME/migrations/runtime-home-v1.lock`。
2. 检查旧目录 `<package>/backend/sessions/` 和新目录。
3. 将有效 Session、`traces/`、`archive/` 复制到新目录的 staging 区。
4. 对每个文件计算 SHA-256，并重新解析 JSON；不迁移 `.tmp` 半成品，只记录诊断。
5. 通过原子 rename/replace 发布到新目录。
6. 写入 `$PUDDINGCLAW_HOME/migrations/runtime-home-v1.json`，记录来源、目标、文件数、摘要、冲突和时间。
7. 新写入立即只写用户目录；旧目录保留为兼容只读来源一个发布周期。

冲突规则：

- 目标不存在：复制。
- 源和目标 digest 相同：跳过。
- 同名但内容不同：不覆盖目标，把旧文件复制到 `migrations/runtime-home-v1-conflicts/sessions/`，并在 Doctor/设置页提示。
- 迁移中断：下次根据 marker 和 digest 幂等重试。

任何情况下都不在启动时删除 `backend/sessions/`。稳定版本验证完成后，由 `puddingclaw doctor` 给出可恢复的备份与清理建议。

### 7.3 并发和原子性

- 迁移前必须确认没有另一个 Backend 持有迁移锁。
- 单 Session 锁继续按最终目标文件的规范化绝对路径建立。
- 临时文件必须与目标位于同一卷，避免跨卷 rename 在 Windows 失败。
- 文件句柄必须在 `Path.replace()` 前关闭，满足 Windows 的文件占用语义。
- UI 与 Headless Worker 必须共享同一个 `PUDDINGCLAW_HOME`，否则会看到不同 Session 集合。

## 8. 旧 Skill 目录迁移

`backend/skills/` 当前混有内置内容、用户导入内容、版本快照和评测输出，不能整目录移动。迁移先读取旧 Skill Management Registry 判断已登记的用户安装，再使用发布时 bundled manifest 对其余目录分类：

1. 与 bundled manifest 同名且 digest 相同：保持在包目录，不复制。
2. 不在 bundled manifest 中：作为 User Skill 复制到 `$PUDDINGCLAW_HOME/skills/<id>`。
3. 与 bundled Skill 同名但 digest 不同：视为“修改过的内置 Skill”，完整备份到冲突目录，不自动覆盖 bundled 或 user 版本。
4. `.skills_store_lock.json`、安装计划、注册表、版本快照和 Eval 输出迁移到对应的 `data/` 子目录。
5. 每个迁移后的 User Skill 重新执行路径、链接、体积、危险文件和 `SKILL.md` 校验。

冲突目录示例：

```text
$PUDDINGCLAW_HOME/migrations/runtime-home-v1-conflicts/skills/<skill-id>/
```

设置页应提供“检查冲突”和“Fork 为用户 Skill”，但不能自动改写未知 Skill 的名称、脚本引用或资源路径。

## 9. Docker 与 Windows Docker Desktop

### 9.1 容器契约

宿主目录与容器目录分离：

```text
host:      /Users/alice/.puddingclaw
host:      C:/Users/Alice/.puddingclaw
container: /app/.puddingclaw
```

容器环境固定为：

```text
PUDDINGCLAW_HOME=/app/.puddingclaw
```

Compose 使用 long syntax，避免 Windows 盘符中的冒号与短挂载语法冲突：

```yaml
services:
  backend:
    environment:
      PUDDINGCLAW_HOME: /app/.puddingclaw
    volumes:
      - type: bind
        source: ${PUDDINGCLAW_HOST_HOME}
        target: /app/.puddingclaw
      - type: volume
        source: puddingclaw-db
        target: /app/.puddingclaw/db

volumes:
  puddingclaw-db:
```

宿主启动脚本负责设置 `PUDDINGCLAW_HOST_HOME`：

- macOS/Linux：`${HOME}/.puddingclaw`
- Windows PowerShell：`${env:USERPROFILE}\.puddingclaw`
- Windows Compose `.env` 可写成 `PUDDINGCLAW_HOST_HOME=C:/Users/Alice/.puddingclaw`

不把宿主 `PUDDINGCLAW_HOME` 原样传进容器；`C:\Users\...` 对 Linux 容器没有意义。

`db/` 使用嵌套 named volume 是有意的：Docker Desktop 的宿主 bind mount 经过 VirtioFS、osxfs 或 9P 等共享文件系统层，不能只凭“路径映射成功”推断 SQLite 的锁、`fsync`、WAL checkpoint 和崩溃恢复语义可靠。其余需要用户直接备份和查看的 Home 内容仍走 bind mount，SQLite 文件留在 Linux VM 管理的 volume 中。备份必须使用 SQLite Backup API、受控停机副本或应用导出，不允许运行中直接复制 `.sqlite3`、`-wal`、`-shm` 文件。

代价是“直接复制宿主 `.puddingclaw`”不再构成完整容器备份。Compose/Doctor 必须提供一个一致性导出命令，把 named volume 中的数据库通过 Backup API 导出到用户选择的备份目录，并在恢复时先导入数据库再启动 Backend；文档和 UI 要明确显示数据库位于独立 Volume，避免用户误以为 Home bind mount 已覆盖全部事实源。

本地 OpenClaw 的 `/mnt/c/Users/...` E2E 只能证明 Windows/WSL 路径不会泄漏到容器命令，不能作为 SQLite bind mount 可靠性的直接证据；其代码中单独设置 WAL、`busy_timeout` 并处理损坏/维护，反而说明 SQLite 需要独立验证。PuddingClaw 当前 Evaluation DB 已使用 WAL/`busy_timeout`，Catalog 与 Token Usage 路径并不一致，迁移时必须统一数据库初始化策略和恢复约束。

### 9.2 Bundled Skill 挂载

- 镜像内 bundled Skill 来自构建产物，例如 `/app/bundled-skills`，只读。
- 用户 Skill 位于 `/app/.puddingclaw/skills`，只有 Backend Skill 管理控制面可写。
- Agent Sandbox 不挂载整个用户 Home；只把当前已批准 Skill 映射到稳定的 `/skills/<id>:ro`。

### 9.3 Windows 验证矩阵

至少覆盖：

- 原生 Windows Backend：`C:\Users\Alice\.puddingclaw`。
- 用户名包含空格和中文。
- Docker Desktop 从 `${USERPROFILE}/.puddingclaw` bind mount。
- Docker Desktop 的 `db/` 由 nested named volume 覆盖；验证宿主 bind mount 中不会生成 SQLite/WAL/SHM 文件。
- WSL 默认 Linux Home。
- WSL 显式 `/mnt/c/Users/Alice/.puddingclaw`。
- 自定义 `D:\PuddingClawData`。
- 目标目录不存在、只读、被文件占用和磁盘空间不足。
- SQLite 并发读写、容器 `SIGKILL`/重启、WAL checkpoint、`PRAGMA quick_check`、备份恢复和 named volume 重建。

## 10. Kubernetes 部署

### 10.1 兼容结论

本方案兼容 Kubernetes，但当前文件型 Session 和 User Skill 存储只支持**单写 Pod**；SQLite 还要求底层卷提供正确的本地文件锁、同步写和原子替换语义。推荐基线是：

这里描述的是迁移完成后的目标架构。当前代码仍通过 `BASE_DIR` 初始化 `backend/sessions` 和 `backend/skills`，因此现在仅在 K8s Deployment 中设置 `PUDDINGCLAW_HOME` 并不会自动改变这两个目录；必须先完成 Phase 0–3 的代码改造，或暂时把旧目录分别挂载到 PVC。

```text
replicas = 1
PUDDINGCLAW_HOME = /var/lib/puddingclaw
/var/lib/puddingclaw 由 PVC 持久化
bundled skills 位于只读镜像层
日志写 stdout/stderr
```

Kubernetes 官方说明容器文件系统具有临时性，需要 Volume/PersistentVolume 才能在 Pod 被替换后保留数据；`ReadWriteOncePod` 用于把一个 PVC 的读写访问限制到集群中的单个 Pod。参考：

- [Kubernetes Volumes](https://kubernetes.io/docs/concepts/storage/volumes/)
- [Persistent Volumes 与访问模式](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [ReadWriteOncePod](https://kubernetes.io/docs/tasks/administer-cluster/change-pv-access-mode-readwriteoncepod/)

部署形态兼容性：

| 形态 | 结果 | 结论 |
|---|---|---|
| 单 Pod + PVC + `ReadWriteOncePod` | Pod 重建后重新挂载同一用户目录 | 推荐 |
| 单 Pod + PVC + `ReadWriteOnce` | 通常可用，但同一节点可能允许多个 Pod 同时挂载 | 需要 `Recreate` 和实例 Lease |
| 单 Pod + `emptyDir` 或镜像可写层 | Pod 删除后 Session/User Skill 丢失 | 不支持 |
| 多 Pod + 同一个 RWX PVC | 多进程竞争 JSON、Trace、Registry 和 Skill 更新 | 当前不支持 |
| 多 Pod + 每 Pod 独立 PVC | 每个 Pod 看到不同 Session 和 User Skill | 不是同一个逻辑实例 |
| 多 Pod + sticky session | 单次聊天可能稳定，但列表、Skill 更新、后台任务仍分裂 | 不能解决 |
| 多 Pod + 外部 Session/Skill Store | 可实现水平扩展 | 后续架构 |

`ReadWriteOnce` 只限制单节点挂载，不保证只有一个 Pod 写入；严格单写优先使用 CSI 支持的 `ReadWriteOncePod`。Kubernetes 官方已将该模式列为稳定能力，但具体 StorageClass/CSI Driver 仍需支持。

`ReadWriteOncePod` 解决的是“同一 PVC 不被多个 Pod 同时读写”，不验证 PVC 后端是否适合 SQLite。生产 K8s 的首选是把 Catalog、Evaluation 和 Usage 等持久数据库外置到 PostgreSQL；如果迁移阶段仍保留 SQLite，则必须同时满足：

- 使用单 Pod、`Recreate`、实例 Lease 和块存储上接近本地 ext4/xfs 语义的 CSI Volume；
- 明确拒绝把 SQLite 放在 RWX/NFS/SMB/CIFS 等共享文件系统上，即使该卷能够成功挂载；
- 对每个 SQLite Store 统一设置合理的 WAL、`busy_timeout`、checkpoint 和连接生命周期策略，并说明不适用 WAL 的例外；
- Readiness 在迁移后执行快速完整性检查，恢复演练覆盖进程 `SIGKILL`、Pod 重建、节点漂移、卷重新挂载、WAL 恢复和备份还原；
- 运行中不得通过文件级快照只复制主 `.sqlite3` 文件；使用数据库 Backup API、应用停写窗口或经验证的 CSI 快照流程。

因此，“单 Pod + `ReadWriteOncePod`”对 JSON/Skill 文件是推荐基线，对 SQLite 只是必要条件之一；StorageClass 不满足文件系统语义时，部署校验应拒绝 SQLite 模式并要求 PostgreSQL。

### 10.2 推荐的单副本形态

可以使用一个副本的 Deployment，也可以使用 StatefulSet。当前 Backend 不需要每个副本拥有不同身份，因此最小改造是 `Deployment + replicas: 1 + Recreate + PVC`：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: puddingclaw-backend
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: puddingclaw-backend
  template:
    metadata:
      labels:
        app: puddingclaw-backend
    spec:
      containers:
        - name: backend
          image: puddingclaw-backend:<version>
          env:
            - name: PUDDINGCLAW_HOME
              value: /var/lib/puddingclaw
          volumeMounts:
            - name: puddingclaw-state
              mountPath: /var/lib/puddingclaw
      volumes:
        - name: puddingclaw-state
          persistentVolumeClaim:
            claimName: puddingclaw-state
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: puddingclaw-state
spec:
  accessModes:
    - ReadWriteOncePod
  resources:
    requests:
      storage: 20Gi
```

`Recreate` 会在升级时先终止旧 Pod，再创建新 Pod，避免默认 RollingUpdate 短暂产生新旧两个 Backend。代价是升级期间存在短暂停机。Kubernetes 对两种 Deployment 策略的定义见 [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#strategy)。

如果 StorageClass 不支持 `ReadWriteOncePod`，可以退回 `ReadWriteOnce`，但必须同时保留：

- `replicas: 1`；
- `strategy.type: Recreate`；
- PuddingClaw 用户根实例 Lease；K8s 下优先使用 `ReadWriteOncePod` 或 Kubernetes Lease，不能把一个可能残留的普通 marker 文件当成唯一互斥机制；
- 禁止 HPA 把 Backend 扩到两个副本。

需要稳定 Pod 身份、稳定 PVC 绑定或以后拆分有状态组件时可改用单副本 StatefulSet。StatefulSet 能为替换 Pod 保留稳定存储，但把副本数提高到 2 并不会自动让文件型 Session 具备分布式一致性。参考 [StatefulSets](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)。

### 10.3 Pod 生命周期中会发生什么

#### 首次启动

1. Kubelet 将 PVC 挂载到 `/var/lib/puddingclaw`。
2. Backend 通过 `PUDDINGCLAW_HOME` 解析该目录，而不是使用容器用户的 `~`。
3. Backend 获取 `state/backend.lease`。
4. 如果存在迁移 marker，直接加载 Session 和 User Skill。
5. 如果 PVC 是全新的空卷，创建目录结构；不会从另一个已删除 Pod 的可写层恢复数据。
6. 启动和迁移完成后 Readiness Probe 才返回 Ready。

#### Pod 重启或节点漂移

- 只要 PVC 能在目标节点重新挂载，Session、User Skill、Runtime 和用户状态会恢复。
- 镜像中的 bundled Skill 来自新版本，不需要从 PVC 恢复。
- `emptyDir`、容器临时目录和未收集的 stdout 日志按 Kubernetes 生命周期处理，不属于 PuddingClaw 持久事实源。

#### 应用升级

1. `Recreate` 先停止旧 Pod，释放用户根 Lease 和 PVC。
2. 新 Pod 使用新镜像挂载原 PVC。
3. 新版本 bundled manifest 与 PVC 中的 User Skill 分开加载。
4. 必要的数据迁移在新 Pod Ready 前完成。
5. 如果迁移失败，Readiness 保持失败，旧数据和迁移记录保留，不能用空状态继续对外服务。

### 10.4 从旧 K8s 布局迁移

旧数据处于哪种位置，决定升级时会发生什么：

| 旧位置 | 升级结果 | 处理方式 |
|---|---|---|
| 已有 PVC 挂载到 `/app/sessions`、`/app/skills` | 数据仍在 PVC，但目录布局不同 | 停止 Backend 后运行一次迁移 Job |
| `emptyDir` | Pod 删除即丢失 | 升级前从运行中的 Pod 导出 |
| 容器镜像可写层 | 新 Pod 看不到旧层 | 升级前导出，不能指望自动迁移 |
| `hostPath` | 数据绑定特定 Node | 迁到 PVC 后再升级 |

K8s 迁移不应让每个新 Pod 在启动时同时扫描旧共享目录。推荐流程：

1. 将 Backend 缩容到 0，确认旧 Pod 已停止。
2. 对旧 PVC 创建 VolumeSnapshot 或存储侧备份。
3. 启动 `parallelism: 1` 的迁移 Job，同时挂载旧目录和新 `PUDDINGCLAW_HOME`。
4. 通过显式参数或 `PUDDINGCLAW_LEGACY_STATE_ROOT` 指定旧根，不能假设旧目录仍位于新镜像的 `backend/`。
5. 校验 Session JSON、Trace、Archive、Skill digest、文件数和冲突报告。
6. 停止迁移 Job，启动单副本 Backend。
7. 验收通过前保留旧 PVC/快照。

因此，迁移实现除桌面默认的 package-relative 旧目录探测外，还必须支持显式 `legacy_state_root`。如果旧状态只存在于已删除 Pod 的临时文件系统，Kubernetes 无法在事后恢复它。

### 10.5 权限、Secret 与备份

- 镜像使用非 root 用户；通过 Pod `securityContext`、`runAsUser`、`runAsGroup` 和 `fsGroup` 让进程可写 PVC。
- 不使用 `hostPath: C:\Users\...` 或节点 Home 作为生产存储，否则 Pod 会绑定节点且难以漂移。
- K8s Secret、KMS/Vault 主密钥与加密后的用户状态分离；不能把解密主密钥和加密数据只放在同一个 PVC 里。
- 推荐在 Provider Registry 中保存 `env://DEEPSEEK_API_KEY` 等引用，并用 `secretKeyRef`、Secrets Store CSI Driver 或外部 Secret Manager 向 Pod 注入；集群 Secret 不应在启动时自动复制进 PVC 上的 Credential 文件。
- 如果必须使用 PVC 上的加密 Vault，Vault 主密钥应由 K8s Secret/KMS 在 Pod 启动时提供并支持轮换；PVC 快照只能包含密文。
- 应用日志优先写 stdout/stderr，由集群日志系统收集；`$PUDDINGCLAW_HOME/logs` 只保存确有文件语义的诊断产物。
- PVC 使用 CSI VolumeSnapshot 或存储侧快照；PostgreSQL、Milvus/对象存储等仍按各自一致性要求备份，不能只备份 PuddingClaw PVC。
- SQLite 模式只允许已验证的块存储 StorageClass；备份清单必须同时覆盖主库与 WAL 状态，并通过恢复后的 `PRAGMA quick_check`/业务级行数与 digest 校验。
- Runtime 是可重建缓存，但 Session、User Skill、迁移记录、Credential Registry 和尚未外置的数据目录需要纳入恢复演练。

### 10.6 多副本演进条件

要把 Backend 扩展到多个可互换 Pod，需要先完成以下外置：

1. Session、Todo、Goal、Run、权限和索引迁入 PostgreSQL 等事务存储。
2. Trace、Attachment、Artifact 和 Skill 包迁入对象存储或内容寻址 Artifact Store。
3. Skill Registry、版本、来源和启用状态进入数据库；Pod 本地只保留只读缓存。
4. Skill 安装/更新和迁移使用分布式锁或 Kubernetes Lease，并通过一个控制面 Leader 提交。
5. Headless Worker 通过队列领取任务，不能依赖本地 Session JSON。
6. Readiness、版本兼容和 Schema Migration 支持新旧版本短暂共存后，才恢复 RollingUpdate。

在这些条件满足前，增加副本只会造成状态分裂或写冲突，Ingress sticky session 不能替代共享事实源。

### 10.7 Windows 与 K8s 的边界

`C:\Users\<用户名>\.puddingclaw` 只适用于原生 Windows Backend，或本机 Windows Docker Desktop 的宿主 bind mount。生产 K8s 通常运行 Linux 容器，Pod 内只看到 `/var/lib/puddingclaw` 和 PVC，不应知道管理员电脑的 C 盘。

如果未来支持 Windows Node/Windows Container，需要单独提供 Windows 镜像、Windows CSI StorageClass 和容器内 Windows 路径测试；不能把当前 Linux 镜像的 `/var/lib/puddingclaw` 契约直接部署到 Windows Container。

## 11. 实施阶段

### Phase 0：Manifest 与路径基础

- 扩展 `PuddingClawPaths`。
- 新增 `PuddingClawPackagePaths`。
- 为 Settings、Provider、MCP、Web Search、Evaluation 和 Project Registry 建立版本化 Schema；Project Registry 预留 `trust_state`、资源 digest 和 `origin=project`，用户配置只保存 overrides。
- 生成 bundled Skill、Semantic Asset、Analytics Model 和 SQL Guardrail manifest。
- 增加 `puddingclaw doctor paths` 或等价诊断 API，显示解析后的用户根、包根、配置来源、Session 根、双资产根、Provider Registry 和 Vault 状态；不得输出 Secret。
- CI 枚举 package root 写入点；新增写入必须声明 `package|user|project|session|cache|temp` 所有权。

### Phase 1：用户配置中心与 Credential Vault

- 将 `backend/config.json` 的非敏感用户覆盖迁入 `$PUDDINGCLAW_HOME/config/settings.json`，不反写完整 defaults。
- 将 Provider Registry 统一到 `$PUDDINGCLAW_HOME/config/providers.json`，只保留非敏感元数据和 `credential_ref`。
- 将 MCP、Web Search 和 Evaluation 非敏感配置迁入各自的 Home Registry。
- 提供一次性 `.env` 导入器：只识别 allowlist 字段，普通值进入 Settings、Secret 进入 Vault；不复制未知环境变量或整份文件。
- 用现有 `CredentialVault`/`MasterKeyProvider` 取代明文 `LocalCredentialStore`，模型 API Key 进入 owner 级加密 Vault。
- 禁止 `update_settings`、配置 API 和 `save_config` 把明文 `api_key`、`password` 或 Token 写入普通配置。
- 将数据库源和异步任务中的明文密码改为 `credential_ref`；清理已存在的 Job Metadata Secret 副本。
- 迁移旧 `PuddingData` Provider/Web Search、Evaluation 设置和旧配置中的密钥，并在加密读回校验后清理旧明文文件。
- 删除所有 Provider Credential reveal 路由，不保留本机管理面例外；只提供单向替换、测试和删除。
- Windows 接入 Credential Manager 或 DPAPI；K8s 支持 `env://`/Secret Manager 引用而不落盘。
- 前端设置页只显示已配置状态和掩码，禁止读取完整 Credential。

### Phase 2：Session 双读单写

- `SessionManager` 接收 `sessions_dir`。
- 执行 `runtime-home-v1` Session 复制迁移。
- 新写只进入 `$PUDDINGCLAW_HOME/sessions`。
- 旧目录保留只读 fallback 一个发布周期。
- 更新 README 备份说明与 Session 架构文档。

### Phase 3：Skill 分层注册表

- Scanner 支持 bundled/user 两个来源。
- Registry Schema 同时预留 `origin=project`、`project_id`、`effective`、`shadowed_sources`、Fork 基线和上游状态；Project Scanner 暂不启用。
- 引入 origin、mutable、digest、`override_policy` 和冲突状态；受保护 Skill 禁止覆盖，普通 Skill 选择过程可追踪。
- Skill 管理 API 只写 User Skill。
- Workspace/Sandbox 通过虚拟 `/skills/<id>` 路由。
- Eval 和版本输出退出 Skill 包目录。

### Phase 4：旧 Skill 分类迁移

- 根据 bundled manifest 迁移非内置 Skill。
- 隔离修改过的内置 Skill。
- 迁移 Skill 管理注册表、快照和 Eval。
- 设置页显示来源、物理目录、只读状态和冲突处理入口。

### Phase 5：用户画像、项目与声明式定义

- 先实现项目首次打开信任门，停止 `register()`/`list_projects()` 隐式创建项目上下文；未受信项目资源不得进入 Prompt。
- 建立 bundled Prompt defaults + User Profile + trusted Project Context 的显式 Prompt 分层，并让信任变化使 Prompt/Agent Cache 失效。
- 在信任门稳定后启用 Project Skill 发现；代码、命令、工具和项目 MCP 绑定组件 digest，变化后重新审批。
- 将 `backend/memory` 和 DeepAgents global/project memory 迁入 Home；Memory Editor 改用 typed API。
- 将 Project Registry、用户可信权限和本机路径绑定迁入 `$PUDDINGCLAW_HOME/projects/`。
- Semantic Asset、Analytics Model 和 SQL Guardrail 支持 bundled/user 双根、来源冲突和 Fork。
- 用户创建、保存、发布和版本操作不再写 package 中的声明式资产目录。

### Phase 6：用户事实、缓存与日志分离

- SQLite Catalog、Evaluation DB、Attachment、Query Result、Headless Idempotency、Token Usage、Rewind 和 Build Job 状态退出 `backend/data`。
- 统一各 SQLite Store 的 WAL/`busy_timeout`/checkpoint/完整性检查与 Backup API 策略；原生桌面可使用 Home `db/`，Docker/K8s 按部署存储边界单独处理。
- 默认 Knowledge Root 改为 `$PUDDINGCLAW_HOME/knowledge`；外部知识根保留引用式绑定。
- Knowledge/Memory Index、Table Profile 和发现缓存进入 `cache/` 或事实源 Sidecar，并支持安全重建。
- Runtime、Agent Workspace、Harness Scratch 和临时输出设置容量、TTL 和启动清理策略。
- LLM Input 日志默认不记录正文，所有日志进入统一脱敏、轮转和保留策略。

### Phase 7：Docker、Kubernetes 与 Windows

- Compose 将用户状态根 bind mount 到固定容器路径，并用 nested named volume 覆盖 `db/`，不让 SQLite 落在 Docker Desktop 宿主共享文件系统上。
- 增加 Windows override/启动脚本。
- 增加单副本 K8s workload、PVC、Probe、SecurityContext 和迁移 Job 模板。
- K8s 默认使用 `ReadWriteOncePod`；不支持时显式降级到 `ReadWriteOnce + Recreate + Lease`。
- 在 Windows、Docker Desktop 和 WSL 跑完整迁移与恢复测试。
- 在 K8s 验证 Pod 重建、节点漂移、升级失败、VolumeSnapshot 恢复、SQLite WAL/完整性和第二写者拒绝；生产首选 PostgreSQL，SQLite 模式拒绝 RWX/NFS/SMB。
- 验证打包安装目录只读时 Backend 仍能正常运行。

### Phase 8：兼容层下线

- 发布至少一个稳定版本后停止读取 `backend/sessions`。
- Doctor 只报告旧目录，不自动删除。
- 移除业务模块中的 `BASE_DIR / "skills"`、`base_dir / "sessions"`、`BASE_DIR / "data"` 和 package-relative 用户配置写入。
- CI 增加检查，禁止新的运行状态写入 package/source root。

## 12. 主要改动面

Session：

- `backend/app.py`
- `backend/graph/session_manager.py`
- `backend/api/sessions.py`
- `backend/api/agent.py`
- `backend/api/compress.py`
- `backend/scripts/audit_legacy_external_leases.py`

Skill：

- `backend/tools/skills_scanner.py`
- `backend/services/skill_management.py`
- `backend/api/skills_api.py`
- `backend/api/files.py`
- `backend/api/eval_api.py`
- `backend/graph/deepagents_manager.py`
- `backend/graph/permissioned_filesystem_backend.py`
- `backend/harness/host_skill_runtime.py`
- `backend/harness/workspace_backends.py`
- `backend/tools/execute_skill_tool.py`
- `backend/tools/skill_inspection_tool.py`

配置与 Credential：

- `backend/config.py`
- `backend/provider_registry.py`
- `backend/api/config_api.py`
- `backend/runtime_identity/paths.py`
- `backend/runtime_identity/profiles.py`
- `backend/runtime_identity/skill_secrets.py`
- `backend/web_search/registry.py`
- `backend/evaluation/settings.py`
- `backend/knowledge/database_sources.py`
- `backend/knowledge/import_jobs.py`
- `backend/knowledge/models.py`
- 前端模型设置与 Provider 管理页面

用户画像、项目与声明式定义：

- `backend/graph/prompt_builder.py`
- `backend/graph/deepagents_prompt_builder.py`
- `backend/graph/deepagents_manager.py`
- `backend/graph/memory_indexer.py`
- `backend/api/files.py`
- `backend/projects/registry.py`
- `backend/projects/project_context.py`
- 新增项目 Trust Manager/审批 API 与前端信任卡
- `backend/analytics/semantic_assets/registry.py`
- `backend/analytics/models/registry.py`
- `backend/analytics/nl2sql/guardrails.py`

数据、缓存与日志：

- `backend/db.py`
- `backend/evaluation/repository.py`
- `backend/graph/attachment_store.py`
- `backend/analytics/nl2sql/result_store.py`
- `backend/graph/token_usage_store.py`
- `backend/api/headless.py`
- `backend/worker_access.py`
- `backend/knowledge/paths.py`
- `backend/knowledge/indexer.py`
- `backend/knowledge/portal_search.py`
- `backend/graph/llm_input_logger.py`

部署与文档：

- `docker-compose.yml`
- Windows Compose override/launcher
- K8s manifests/Helm chart 与一次性迁移 Job
- `README.md`
- `docs/session-and-context-architecture.md`
- `docs/plans/2026-07-18-managed-skill-install-update.md`

## 13. 测试计划

### 13.1 单元测试

- 默认 Home 与 `PUDDINGCLAW_HOME` 覆盖。
- 相对覆盖路径、普通文件、不可写目录拒绝。
- POSIX、Windows drive、空格、Unicode 和大小写规范化。
- Windows 保留名拒绝。
- Skill bundled/user 扫描、Project origin Schema 兼容、受保护名称冲突、普通来源优先级、只读内置/项目 Skill、Fork 上游 digest 状态。
- Session 同 digest、不同 digest、损坏 JSON、临时文件和幂等重试；根列表器忽略 `traces/`、`archive/`、目录、链接和未知 JSON。
- 新版 defaults + 旧版 user overrides 合并、Schema 迁移、未知字段诊断和 Operator 锁定项。
- Provider Registry 序列化不含 API Key；Vault 加密、解密、篡改拒绝、主密钥缺失和 Credential 轮换。
- MCP、Web Search、Evaluation 和数据库源 Registry 均只能持久化 Credential 引用。
- 异步 Job Snapshot 不包含数据库密码、完整 URL、API Key 或 Token。
- Windows Credential Manager/DPAPI 可用与不可用分支；fallback 必须产生明确安全告警。
- Prompt 分层顺序、bundled 安全规则不可覆盖、项目内容不能扩大权限；`pending`/`denied` 项目资源不被读取或注入，信任变化使缓存失效。
- 项目注册和列表操作不创建 `.puddingclaw/` 或 `PROJECT_CONTEXT.md`；信任记录只能来自用户 Registry，路径/身份变化回到 `pending`。
- SQLite 初始化参数、并发写、WAL checkpoint、`SIGKILL` 恢复、`PRAGMA quick_check`、Backup API 与损坏诊断。
- Semantic Asset、Analytics Model 和 SQL Guardrail 双根扫描、冲突与 Fork。

### 13.2 集成测试

- 从真实旧布局复制 Session、Trace、Archive 后可继续对话。
- Headless Session TTL 和普通 Session 搜索使用新目录。
- 用户 Skill 安装、更新、回滚、重命名和 Eval 全部不修改 bundled root。
- 前端上传、创建、保存和 Fork 的 Skill 全部写入 User Skill 根，且 API 不能通过路径参数越权写 bundled root。
- bundled Skill 升级后，基线 digest 变化的用户 Fork 显示非阻断 `upstream_status=updated`，不会被自动覆盖或合并。
- 设置页保存模型 API Key 后，配置文件、Provider Registry、Session、Trace、日志和 API 查询响应中均找不到明文 Secret。
- 从旧 `PuddingData/credentials.json` 与 `backend/config.json` 迁移后，模型连接仍可用，Registry 仅保存 `credential_ref`，旧明文来源已清理。
- 从旧 Catalog 迁移数据库源后连接和后台任务仍可用，Catalog、Job Metadata 和日志中不存在密码副本。
- MCP、Web Search、LangSmith、数据库源和 Worker 管理 API 均不存在读取完整 Credential 的路由；创建/替换 Secret 只允许单向写入。
- 首次打开含 `.puddingclaw/` 的项目只显示信任卡；批准前 Project Context/Skill/MCP 不进入 Prompt 或工具面，批准后按资源类型与 digest 加载。
- 用户 Profile、全局/项目 Memory 和 Project Registry 在替换 package 后保持可用；项目 Registry 恢复到另一台机器时路径先进入 `unbound`。
- 用户创建的 Semantic Asset、Analytics Model 和 SQL Guardrail 不修改 bundled root，并可在升级后继续加载。
- 清空 `cache/` 后知识检索、Memory Index 和模型发现可按需重建，不损伤事实源。
- Sandbox 在 macOS/Linux/Windows Docker 中都看到相同 `/skills/<id>`。
- package root 设为只读后，启动、聊天、导入 Skill 和写 Trace 正常。
- package root 设为只读后，修改设置、Profile、项目、知识、评测和声明式定义仍正常。
- 两个 Backend 指向同一 Home 时，第二个写进程被根级实例 Lease 明确拒绝。
- Docker Desktop 的 Home bind mount 中不出现 SQLite/WAL/SHM 文件；`db/` named volume 经强杀、重启、备份和恢复后完整性检查通过。
- K8s 单副本 Pod 替换后挂载同一 PVC，并恢复 Session、User Skill 和迁移 marker。
- K8s SQLite 模式只接受经验证的块存储，RWX/NFS/SMB 配置被拒绝；PostgreSQL 模式不依赖 PVC 上的 SQLite。
- K8s 默认 RollingUpdate/HPA 配置被部署校验拒绝或被明确覆盖为单写安全策略。
- K8s 使用 `secretKeyRef`/外部 Secret 引用时，API Key 不写入 PVC；使用加密 Vault 时，PVC 不包含解密主密钥。

### 13.3 回滚测试

- 新版本启动失败时，旧 `backend/sessions` 仍完整保留。
- 迁移 marker 缺失或部分写入时可安全重试。
- 将 `PUDDINGCLAW_HOME` 指回备份目录可恢复 Session。
- Skill 冲突不会覆盖 bundled 或 user 目标。
- Credential 迁移失败时旧 Registry 仍指向可用来源；成功切换后可从用户持有密钥的加密备份恢复，系统不生成明文回滚副本。

## 14. 验收标准

- 新 Session、Trace、Archive 不再写入仓库 `backend/sessions/`。
- Session 根目录只枚举合法普通 `.json` 文件，`traces/`、`archive/` 和未知 entry 不会被识别为 Session。
- 用户导入或创建的 Skill 不再写入 `backend/skills/`。
- 前端对 bundled Skill 只能查看或 Fork，不能直接保存、重命名或删除。
- Registry 可表示 `bundled | user | project`；受保护 Skill 不可覆盖，普通 Skill 的有效来源和被遮蔽来源可审计，Project 来源只在项目受信后启用。
- 用户 Fork 的 bundled 基线发生变化时显示上游更新状态，不自动覆盖用户内容。
- 模型 API Key 不再明文写入 `backend/config.json`、`PuddingData/credentials.json` 或 `$PUDDINGCLAW_HOME/config/`；所有本地持久 Credential 均通过 owner 级加密 Vault 保存。
- 数据库密码、MCP Token、Web Search Key 和 LangSmith Key 同样不出现在普通配置、Catalog、Job Metadata、Session、Trace 或日志中。
- `backend/config.json` 不再是运行时事实源；用户设置文件只保存 overrides，升级默认值不会被旧配置完整快照冻结。
- `backend/prompts` 是只读默认模板；用户 Profile 和 Memory 有独立、稳定、可备份的 Home 路径。
- Project Registry 和可信权限退出 `backend/data`；项目仓库内容无法自行授予更高权限，也不能在首次信任前把 Project Context、Skill、MCP 或偏好静默注入 Prompt/工具面。
- 注册或列出项目不会修改项目目录；创建 `PROJECT_CONTEXT.md` 必须发生在项目受信后的显式用户操作中。
- 用户创建的 Semantic Asset、Analytics Model 和 SQL Guardrail 与 bundled 版本分层，来源和可写性可追踪。
- SQLite Catalog、Evaluation DB、Attachment、Query Result、Usage 和维护状态不再写入 package/source root。
- Docker Desktop 的 SQLite 不位于宿主 bind mount；K8s SQLite 只运行在经验证的单写块存储上，生产部署可切换到 PostgreSQL。
- Cache 可整体删除并重建；Logs 有脱敏、轮转、容量和保留期，默认不记录完整 LLM 正文。
- 原生 Windows 优先使用 Credential Manager/DPAPI 保护 Secret 或 Vault 主密钥；K8s 优先引用 K8s Secret/KMS/外部 Secret Manager。
- PuddingClaw 升级、切换 Git 分支或替换应用包后，Session 与 User Skill 保持可用。
- 原生 Windows 默认路径实际为 `C:\Users\<用户名>\.puddingclaw`，无需用户手工配置。
- Windows Docker Desktop 能从 `%USERPROFILE%\.puddingclaw` 恢复同一批 Session 和 User Skill。
- K8s 使用 PVC 后 Pod 删除或节点漂移不丢失 Session/User Skill；官方 manifests/Helm values 要求提供 PVC，不能默认回退到容器可写层。
- 当前发布明确限制 Backend 为单副本；多副本部署不会以“看似可用、实际状态分裂”的方式启动。
- bundled Skill 始终只读，并能随应用升级；User Skill 的来源、版本和修改状态可追踪。
- 目标 Home 没有无所有者的顶层 `workspace/`；项目 Workspace、未绑定 Agent Workspace 和 Headless 项目根各有唯一归属。
- Prompt、Trace 和 Session 不持久化平台相关的 Home 绝对路径。
- 迁移可重复执行、可诊断，不自动删除 Session、Skill、Profile、Memory 或业务资产，并有明确冲突报告；旧明文 Secret 仅在加密读回验证和备份策略满足后按安全流程清理。

## 15. 不采用的方案

### 把所有内容塞进一个 Home `config.json`

不采用。单文件会重新混合用户偏好、独立 Registry、Secret、项目路径和部署参数，导致并发写放大、迁移耦合和最小权限失效。`settings.json` 只承载普通用户 overrides；拥有独立 Schema 或生命周期的 Provider、MCP、Web Search、Evaluation 和 Credential 分开管理。

### 把整个 `backend/data` 原样移动到 Home

不采用。`backend/data` 同时包含事实源、Session 产物、授权状态、可重建缓存和临时 Scratch；整体移动只改变路径，不解决备份、TTL、安全和恢复语义。必须先分类，再迁入 `db/`、`data/`、`state/`、`cache/`、`runtime/` 或 `tmp/`。

### 把项目权限写进项目仓库

不采用。仓库内容可能来自不受信来源，如果项目文件能声明自身拥有网络、外部目录或 Secret 权限，就会形成权限自提升。项目可携带上下文和普通偏好，可信授权必须保存在用户 Registry 或部署策略中。

### 未经信任自动加载项目上下文、Skill 或 MCP

不采用。即使项目文件不能直接提权，其内容仍可通过 Prompt Injection 影响模型并诱导调用已有工具。项目探测保持只读，只有用户 Registry 中的信任决定可以激活项目资源；代码、命令和外部连接还需绑定组件 digest。

### 整体移动 `backend/skills` 到用户目录

不采用。这样会让 bundled 与 user 内容继续混杂，并阻断内置 Skill 随应用升级。

### 首次启动复制所有 bundled Skill 到用户目录

不采用。复制后无法判断用户修改和上游更新，也会扩大磁盘占用。

### 受保护 Skill 被用户或项目来源覆盖

不采用。控制 Skill 被同名包替换属于供应链和路由风险。普通 Skill 可以采用 `project > user > bundled` 的显式优先级，但 Registry、设置页和 Trace 必须记录有效来源与被遮蔽来源，因此不属于静默覆盖。

### 在 Windows 使用 `%APPDATA%` 或 `%LOCALAPPDATA%` 作为默认根

本次不采用。PuddingClaw 当前已经公开 `~/.puddingclaw` / `%USERPROFILE%\.puddingclaw` 契约，且该路径与 Codex、OpenClaw、Pi 的用户可见状态目录模式一致。未来如果桌面壳需要把纯缓存迁到平台原生 Cache 目录，可以只拆分可重建缓存，不改变 Session、User Skill 和 Vault 的主根。

### 让容器自行解析 `~/.puddingclaw`

不采用。容器的 Home 属于容器用户，不是宿主用户；必须由宿主解析后 bind mount 到固定容器路径。

### 把 SQLite 放在 Docker Desktop Home bind mount 或 K8s RWX 文件系统

不采用。路径可见、PVC 可挂载或 `ReadWriteOncePod` 都不足以证明 SQLite 的锁与持久化语义。Docker Desktop 使用 nested named volume；K8s 使用经验证的单写块存储，生产首选 PostgreSQL，并通过强杀恢复、WAL、完整性和备份还原测试验收。

### 把 API Key 直接放在 `$PUDDINGCLAW_HOME/config.json` 或 `.env`

不采用。统一用户根解决的是所有权和可迁移性，不会让明文文件自动变安全。普通配置只保存 `credential_ref`；Secret 应来自平台安全存储、部署 Secret 引用或加密 Vault。

### 把 Vault 密文和主密钥一起放入同一个用户目录/PVC

不采用。这样备份、PVC 快照或目录泄露会同时取得解密材料，静态加密失去主要意义。桌面端主密钥优先由系统安全存储保护；K8s 主密钥由 Secret/KMS 独立注入。
