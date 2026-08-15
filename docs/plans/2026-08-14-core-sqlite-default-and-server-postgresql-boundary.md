# PuddingClaw Core SQLite 默认化与服务端 PostgreSQL 边界方案

> 状态：Draft（代码与测试现状已核对，待实施）
>
> 日期：2026-08-14
>
> 决策范围：Core Catalog、任务中心、后台任务队列、CLI 初始化和部署边界

## 1. 决策摘要

PuddingClaw Core 应采用以下数据库策略：

- 桌面端、本地单用户和单 Backend 实例默认使用 SQLite，数据库位于
  `$PUDDINGCLAW_HOME/databases/catalog.sqlite3`。
- Core 不再把 PostgreSQL 作为所有初始化 Profile 的优先或必选基础设施。
- PostgreSQL 保留为服务端能力：用于多 Backend 副本、多独立 Worker、共享服务、高可用和多租户部署。
- 任务中心的展示、通知、任务状态和事件不依赖 PostgreSQL；队列领取改为同时支持 SQLite 和 PostgreSQL
  的原子 lease/CAS 协议。
- gbrain 的 PostgreSQL + pgvector 是独立可选运行时，不决定 Core Catalog 的数据库类型。
- 用户接入的业务 PostgreSQL 数据源属于 Analytics/知识数据源，不属于 Core 数据库；Core 使用 SQLite
  不影响继续查询外部 PostgreSQL。
- PostgreSQL 是多租户服务的基础设施条件之一，但不是多租户能力本身。租户身份、授权、Credential、
  数据过滤、审计和资源配额仍需独立设计和验收。

目标模式如下：

| 运行模式 | Core 数据库 | 支持边界 |
|---|---|---|
| Desktop / Local | SQLite（默认） | 单 Backend 实例、进程内 Worker、本机可靠文件系统 |
| Personal Server（单实例） | SQLite 可用，PostgreSQL 可选 | 只能有一个写入实例；SQLite 必须位于可靠单写块存储 |
| Server / K8s | PostgreSQL（推荐，默认模板要求） | 多副本、多 Worker、滚动升级、数据库独立备份与高可用 |
| Multi-tenant SaaS | PostgreSQL（必需但不充分） | 另行实现租户隔离、授权、审计、配额与运维控制面 |

## 2. 背景与现状

Core Catalog 当前保存的是规模较小、结构化的应用元数据和任务状态，主要包括：

- 知识库、文档、稍后读条目；
- 外部数据库源的 Catalog 记录；
- 表格资产、字段 Profile 和查询结果索引；
- 知识导入任务、语义维度构建任务及其事件；
- 任务通知和 Worker 访问审计。

原始知识文件、Wiki、Session、查询结果 JSONL、附件和可重建向量索引均不以 Core 数据库作为唯一事实源。
因此 Core 当前的容量、查询复杂度和单机并发需求不构成 PostgreSQL 前置条件。

代码已经具备 SQLite 主链路：

- [`backend/db.py`](../../backend/db.py) 在没有 PostgreSQL URL 时解析
  `$PUDDINGCLAW_HOME/databases/catalog.sqlite3`。
- [`backend/knowledge/models.py`](../../backend/knowledge/models.py) 使用 SQLAlchemy 通用
  `String`、`Text`、`JSON`、`DateTime`、索引和唯一约束，没有把 Core 模型绑定到 JSONB、ARRAY、
  PostgreSQL UUID 或 pgvector 类型。
- GUI 已提供 SQLite 存储选项。
- 现有 SQLite 测试覆盖知识 Catalog、稍后读、任务、通知、表格资产和问数结果等主要链路。

2026-08-14 评估时运行以下测试集：

```text
backend/tests/test_gateway_settings.py
backend/tests/test_knowledge_service.py
backend/tests/test_read_later.py
backend/tests/test_semantic_dimension_jobs.py
backend/tests/test_table_catalog_concat.py
backend/tests/test_database_query_result_contract.py
```

结果为 `154 passed`。这证明主要业务模型和查询已可运行于 SQLite，但不等于 SQLite 的并发、迁移、
备份和异常恢复已经达到默认生产质量；这些缺口属于本方案的实施范围。

## 3. 为什么 Core 不应继续 PostgreSQL 优先

当前 PostgreSQL 优先策略给桌面和首次体验引入了与实际需求不相称的成本：

- 需要发现、安装、启动、认证和升级独立数据库服务；
- Knowledge Profile 还可能把 Core 数据库与 pgvector/gbrain 的需求错误耦合；
- Docker、本机包管理器和外部数据库形成多条初始化分支；
- 数据库密码、端口、服务生命周期和备份增加用户认知与故障面；
- Harness-only 和普通本地知识管理并不需要网络数据库。

SQLite 默认化带来的收益包括：

- 安装后零服务即可启动；
- Core 数据跟随 PuddingClaw Home，便于本地备份和迁移；
- 与现有 `evaluation.sqlite3`、Token Usage 等本地持久化方向一致；
- 减少 CLI 探测、Docker 依赖、端口冲突和 Credential 配置；
- 保留 SQLAlchemy 数据访问层，未来仍可切换 PostgreSQL。

该决策不是因为 SQLite 在所有场景都优于 PostgreSQL，而是因为数据库能力应与部署形态匹配：本地单写
客户端优先 SQLite，共享多写服务优先 PostgreSQL。

## 4. 任务中心与队列边界

### 4.1 任务中心不依赖 PostgreSQL

`GET /analytics/task-center` 当前只是展示适配器：分别读取语义维度任务和知识导入任务，在内存中合并、
排序并返回。`task_notifications` 也是普通的通知表。它们只需要普通查询、插入、更新和索引，SQLite 足以
支持。

任务中心当前聚合两类 Core 队列：

| 队列 | 表 | 当前消费者 |
|---|---|---|
| 知识导入、稍后读、LLM Wiki Ingest、向量发布等 | `knowledge_import_jobs` / `knowledge_import_events` | `KnowledgeImportWorkerManager` |
| 语义维度构建 | `semantic_dimension_build_jobs` / `semantic_dimension_build_events` | `SemanticDimensionBuildWorkerManager` |

Evaluation 使用独立的 `evaluation.sqlite3` 和 `EvaluationWorkerManager`，不由上述任务中心接口统一持有队列。
未来可以在展示层聚合，但不能为了 UI 聚合而合并不同队列的所有权和事务边界。

### 4.2 当前 PostgreSQL 专属点

知识导入队列领取任务时调用 `with_for_update(skip_locked=True)`。它解决的是多个数据库消费者同时领取
任务时的竞争，不是任务中心自身的存储需求。

当前桌面运行时每个 Backend 只创建一个进程内知识 Worker 和一个进程内语义维度 Worker；语义维度队列
目前甚至没有行锁。因此 PostgreSQL 不能作为当前所有队列并发正确性的统一证明。

### 4.3 目标队列协议

Core 队列应采用可跨 SQLite/PostgreSQL 实现的 at-least-once lease 协议，而不是让业务服务直接依赖某个
数据库的锁语法。

建议为后台任务统一增加或规范以下字段：

```text
status              queued | running | succeeded | failed | cancelled
lease_owner         当前消费者实例 ID，可空
lease_expires_at    lease 到期时间，可空
heartbeat_at        最后一次续租时间，可空
attempt             实际执行次数
started_at
finished_at
error_message
```

领取任务必须是一次原子状态转换。下面的 SQL 表达跨数据库的 CAS 语义，同时作为 SQLite 的参考实现；
`:db_now` 和 `:db_lease_expires_at` 表示由同一数据库连接取得或通过方言表达式计算的数据库权威时间，
不能直接使用某台 Worker 主机生成的时间：

```sql
UPDATE knowledge_import_jobs
SET status = 'running',
    lease_owner = :worker_id,
    lease_expires_at = :db_lease_expires_at,
    heartbeat_at = :db_now,
    started_at = COALESCE(started_at, :db_now)
WHERE id = (
    SELECT id
    FROM knowledge_import_jobs
    WHERE status = 'queued'
       OR (status = 'running' AND lease_expires_at < :db_now)
    ORDER BY created_at, id
    LIMIT 1
)
AND (
    status = 'queued'
    OR (status = 'running' AND lease_expires_at < :db_now)
)
RETURNING *;
```

实现要求：

- SQLite 使用短写事务和 `UPDATE ... RETURNING` 完成原子领取；写事务由 SQLite 串行化。
- PostgreSQL 的默认生产实现必须在 Queue Repository 内使用 `SELECT ... FOR UPDATE SKIP LOCKED` 选择候选，
  再在同一事务中更新并返回任务。上面的无锁子查询 CAS 在 PostgreSQL `READ COMMITTED` 下可保证不会重复
  领取，但多个消费者会命中同一队头、阻塞并在谓词重检后返回空，不能作为服务端高并发默认路径。
- CAS/lease 是统一语义契约，SQLite 原子 `UPDATE ... RETURNING` 与 PostgreSQL `SKIP LOCKED` 是两种方言
  实现；方言 SQL 不得泄漏到业务服务层。
- 多机 PostgreSQL 部署使用数据库服务器时间计算 `heartbeat_at`、`lease_expires_at` 和过期判断，例如
  `clock_timestamp()` 加 lease interval；不能依赖各 Worker 主机时钟。SQLite 本地模式同样优先使用数据库
  `CURRENT_TIMESTAMP` 及数据库日期表达式，lease 粒度按秒设计。NTP 仍是运维要求，但不是队列正确性的
  唯一防线。
- Worker 处理长任务时定期 heartbeat；只有持有匹配 `lease_owner` 的 Worker 才能更新进度或结束任务。
- Backend/Worker 崩溃后，到期 lease 可由其他 Worker 回收。
- 任务处理必须尽量幂等；外部副作用使用稳定 job ID、临时目录和原子发布，接受 at-least-once 而不是假设
  exactly-once。
- 取消、重试和完成操作必须带期望状态条件，禁止陈旧 Worker 覆盖新状态。

Desktop 模式还应在 `$PUDDINGCLAW_HOME/state/backend.lease` 上使用本机 OS advisory lock。SQLite/WAL 在受支持
的本地文件系统上可以安全协调同机多进程，队列 CAS 也必须独立防止重复领取；因此 Backend 单实例 lease
不是数据库正确性防线，而是运维护栏，用来避免重复启动后台消费者、重复状态推送、端口分叉和用户困惑。
第二个 Backend 无法取得 lock 时应拒绝启动数据库后台 Worker，并返回明确诊断。队列的 CAS/lease 不能
退化为只依靠该文件锁或进程内 `asyncio.Lock`。

## 5. SQLite 默认化的工程要求

### 5.1 连接配置

Core SQLite 连接建立时至少启用：

```sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 10000;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
```

约束如下：

- 启动时执行 `SELECT sqlite_version()` 并断言 SQLite `>= 3.35.0`；队列协议依赖该版本引入的
  `UPDATE ... RETURNING`。版本不足时 Core 初始化必须 fail closed，并返回可执行的升级提示；
- `foreign_keys=ON` 必须对每条新连接生效；
- `busy_timeout` 避免短暂写竞争直接变成 `database is locked`；
- `journal_mode=WAL` 在初始化阶段设置并验证结果；
- 写事务保持短小，LLM、文件解析、网络调用和子进程执行不得占用数据库事务；
- 定期 checkpoint，不能无限保留 WAL；
- 为锁等待、lease 回收、checkpoint 和 integrity check 增加结构化日志与状态探测。

项目的 `EvaluationRepository` 已经使用 WAL、`busy_timeout`、外键和期望状态更新，可以作为 Core SQLite
实现的参考，但 Core 应通过 SQLAlchemy 连接事件和统一 Queue Repository 落地，而不是复制同步
`sqlite3` 代码。

### 5.2 Schema migration

当前 `Base.metadata.create_all` 只能创建缺失表，不能可靠升级已有表。SQLite 成为默认 Core 前必须建立正式
迁移机制：

- 使用 Alembic 或受版本控制的等价 migration runner；
- 每个 Release 明确支持从哪些 schema version 升级；
- 同一逻辑迁移同时覆盖 SQLite 和 PostgreSQL；
- SQLite 需要改列或约束时使用可恢复的建新表、复制、校验、切换流程；
- 启动时迁移失败必须保持旧数据库可恢复，不得继续以半迁移 schema 提供写服务；
- 增加空库初始化、跨多个历史版本升级和中断恢复测试。

### 5.3 备份与恢复

- SQLite 在线备份使用 SQLite Backup API 或 `VACUUM INTO`，并在操作前后处理 WAL/checkpoint；运行中不得只
  复制主 `.sqlite3` 文件而遗漏 `-wal` / `-shm`。
- 备份产物附带 schema version、应用版本、文件哈希和 integrity check 结果。
- 提供 restore 到临时路径、校验、原子切换流程。
- PostgreSQL 服务端继续使用逻辑备份和数据库级恢复方案，不复制正在运行的数据目录。

### 5.4 文件系统边界

SQLite 只允许以下部署：

- 原生桌面本机磁盘；
- Docker 内独立 named volume；
- 经验证的单写块存储。

以下部署必须拒绝或强告警：

- 多个 Pod 共享同一 SQLite 文件；
- NFS、SMB 或未验证锁/WAL 语义的 RWX 文件系统；
- `replicas > 1` 仍选择 SQLite；
- SQLite 文件位于易被应用升级覆盖的源码或安装目录。

## 6. PostgreSQL 的保留边界

PostgreSQL 仍是以下场景的正确选择：

- K8s 多 Backend Pod；
- 独立部署多个 Queue Worker；
- 多台主机共享 Core；
- 需要数据库独立高可用、只读副本、集中备份和恢复；
- 多租户 SaaS；
- 任务并发和写吞吐超出单写 SQLite 的合理范围。

### 6.1 多副本

多副本指两个或更多 Backend/Worker 进程可能同时访问同一 Core 数据库，包括：

- K8s `Deployment replicas > 1`；
- 多台服务器共同服务；
- 一个部署启动多个会各自创建后台消费者的 Uvicorn/Gunicorn Worker；
- Web API 与后台 Worker 拆成独立部署。

仅仅为了利用多个 Web Worker 而重复启动后台消费者是错误配置。服务端模式应把 Web 与队列 Worker 的
角色显式化，并通过 lease 协议协调。

### 6.2 多租户

选择 PostgreSQL 之后仍需完成以下多租户设计，才能声明支持多租户：

- 所有租户事实表具备不可绕过的 `tenant_id` / owner scope；
- Repository 和授权层默认按租户过滤，跨租户操作使用显式管理权限；
- Credential Vault、知识目录、任务、通知、Session、附件和查询结果均按租户归属；
- 唯一约束和幂等键包含租户边界；
- 审计记录主体、租户、请求、Worker 和副作用；
- 可选使用 PostgreSQL RLS 作为纵深防御，但不能只依赖应用层约定或只依赖 RLS；
- 配额、限流、数据导出、删除和租户级备份恢复有明确契约。

本方案只确定 PostgreSQL 是该场景的 Core 数据库，不宣告上述多租户能力已经实现。

## 7. gbrain、pgvector 与外部数据库

### 7.1 gbrain

gbrain 当前明确要求独立 PostgreSQL database 和 pgvector，用于 Wiki pages、links、chunks、ingest log 和
向量检索。近期不在 Core SQLite 默认化中改写 gbrain 存储引擎。

必须解除的耦合是：

- Core 选择 SQLite 时仍可使用普通 Knowledge、Wiki 文件协议和本地/其他检索能力；
- 用户显式启用 gbrain import 时，才单独引导配置 PostgreSQL + pgvector；
- gbrain 数据库继续与 Core Catalog 分库、分 owner；
- gbrain 不可用时不能把 Core SQLite 标记为数据库故障。

未来若 gbrain 或替代运行时支持 SQLite FTS/vector 扩展，可以另行评估完全本地化；这不是本方案的前置
条件。

### 7.2 Analytics 与知识数据库源

`KnowledgeDatabaseSource`/Analytics 当前可以保存用户业务 PostgreSQL 的连接元数据，并通过独立连接执行
Schema 探测和只读 SQL。该数据库是被分析的数据源，不是 PuddingClaw Core。

因此：

- Core SQLite 不阻止连接 PostgreSQL 数据源；
- `asyncpg` 仍可由 Analytics 或 gbrain 扩展安装；
- Core 的基础依赖不应仅因为外部数据源能力而强制携带并配置 PostgreSQL 服务。

## 8. 配置、CLI 与依赖调整

### 8.1 配置模型

当前 `database.mode=bundled|external|sqlite` 混合了数据库产品和部署来源。目标配置应区分：

```json
{
  "database": {
    "provider": "sqlite",
    "source": "local_file",
    "path": "databases/catalog.sqlite3"
  }
}
```

PostgreSQL 示例：

```json
{
  "database": {
    "provider": "postgresql",
    "source": "external",
    "credential_ref": "...",
    "host": "db.internal",
    "port": 5432,
    "database": "puddingclaw"
  }
}
```

兼容期继续读取旧 `mode`，但新写入只使用明确的 provider/source 语义。环境变量保持可覆盖，CLI 和 GUI
必须展示最终生效值及来源。

### 8.2 CLI 初始化

- Desktop/Harness/Knowledge/Analytics/Full 本地初始化默认 SQLite，不先探测或安装 PostgreSQL。
- 只有用户显式选择 Server、PostgreSQL Core 或 gbrain 时才进入 PostgreSQL 探测与配置。
- `puddingclaw database configure` 支持 SQLite 与 PostgreSQL 之间的受校验迁移，不允许只改连接配置造成
  用户看到空 Catalog。
- K8s/server 模板默认 PostgreSQL；SQLite 需要显式 override，并通过副本数和存储类型校验。
- CLI 文案从“PostgreSQL 优先 / SQLite 保底”改为“SQLite 本地默认 / PostgreSQL 服务端可选”。

### 8.3 Python 依赖

- Core 基础依赖保留 `sqlalchemy[asyncio]` 和 `aiosqlite`。
- `asyncpg` 移到 PostgreSQL Core、Analytics PostgreSQL connector 或 gbrain 对应的可选依赖集合；最终打包
  可以按 Runtime Profile 合并安装。
- 能力探测分别报告 `core_database`、`gbrain_postgres/pgvector` 和外部数据源能力，禁止继续用一个
  `postgres` 状态代表所有数据库能力。

## 9. 数据迁移与兼容策略

### 9.1 新安装

- 默认创建 SQLite Catalog；
- 执行正式 schema migration 到当前版本；
- 设置 WAL/外键/timeout 并执行健康检查；
- 不启动或探测 PostgreSQL；
- gbrain 保持未配置的可选状态。

### 9.2 已有 SQLite 用户

- 仅执行 schema migration；
- 首次升级设置并验证 WAL、外键和 busy timeout；
- 迁移前创建可恢复备份；
- 不改变数据库路径或用户知识目录。

### 9.3 PostgreSQL Core 迁移到 SQLite

不能通过切换设置直接完成。迁移前应先实现数据库级 drain/maintenance 协议，而不是由 CLI 临时猜测
哪些 API 和 Worker 需要停止。建议增加单例 `core_runtime_control` 记录：

```text
write_mode          normal | draining | maintenance
maintenance_owner   当前迁移实例 ID
lease_expires_at    维护 lease 到期时间
generation          每次状态切换递增
reason              非敏感维护说明
```

CLI 通过受认证的本地控制 API 获取并续租 maintenance lease。所有 Backend/Worker 从同一 Core 数据库读取
该状态并遵循以下协议：

1. `draining`：新建任务以及除“已持有有效 lease 的任务 heartbeat、进度和完成写入”之外的 Core mutation
   返回带 `Retry-After` 的维护响应；Worker 停止领取新任务，已在运行的任务可以继续并完成。
2. running 任务归零后切换为 `maintenance`：所有非迁移 owner 的 Core 写入被拒绝，迁移 owner 持续续租。
3. 迁移成功并完成配置原子切换后恢复 `normal`；迁移失败则保持源数据库不变，并显式释放或等待维护
   lease 到期后恢复。
4. 多机 PostgreSQL 下维护 lease 的获得、续租和过期判断同样使用数据库服务器时间和 CAS generation，
   保证所有副本观察同一状态。

在此基础上，CLI 应提供显式迁移：

1. 验证源 PostgreSQL 可读、目标 SQLite 路径可写且没有正在运行的迁移。
2. 获取维护 lease，进入 `draining`，等待或由用户明确取消当前 running 任务，再进入 `maintenance`。
3. 在一致性读事务中按依赖顺序导出 Core 表，不导出 gbrain database 或外部业务数据库。
4. 将 JSON、时间、空值和 Credential 引用规范化后批量写入临时 SQLite。
5. 校验每表行数、主键集合、外键、关键摘要和 `PRAGMA integrity_check`。
6. 原子切换 Core 配置并重启 Backend。
7. 保留源 PostgreSQL 和非敏感迁移报告，在用户确认稳定前不自动删除源数据。

迁移必须明确排除：

- gbrain 独立 database；
- Milvus collection；
- 用户业务 PostgreSQL 表；
- 已经以文件为事实源的知识、Session 和附件。

### 9.4 SQLite 迁移到 PostgreSQL

服务端扩容时提供对称流程：停写、导出一致快照、导入临时 PostgreSQL schema、校验、切换配置、重启，
并保留 SQLite 回滚文件。禁止两个数据库长期双写作为默认方案；双写会扩大一致性和故障恢复复杂度。

## 10. 分阶段实施

### P0：让 SQLite 成为可靠 Core

- [ ] 启动时校验 SQLite `>= 3.35.0`，版本不足时 fail closed 并给出升级提示。
- [ ] 增加 SQLite PRAGMA、WAL、busy timeout 和外键连接配置。
- [ ] 建立 Core schema migration 基线，替换仅依赖 `create_all` 的升级路径。
- [ ] 抽取 Queue Repository。
- [ ] 将知识导入和语义维度任务改为原子领取、lease、heartbeat、过期回收和期望状态更新。
- [ ] 增加 Backend 单实例 lease。
- [ ] 增加 SQLite 并发领取、崩溃恢复、取消竞争和重试测试。
- [ ] 增加在线备份、完整性检查和恢复命令。

### P1：默认值和产品入口切换

- [ ] CLI 本地 Profile 默认 SQLite，不再先探测 PostgreSQL。
- [ ] GUI 将 SQLite 标记为本地推荐选项，PostgreSQL 标记为服务端/共享部署选项。
- [ ] 拆分 database provider 与 deployment source 配置。
- [ ] 拆分 Core、gbrain/pgvector 和外部数据源的能力状态。
- [ ] 更新 README、Compose、初始化文档和故障排查文案。
- [ ] 将 `asyncpg` 从所有 Profile 的 Core 必选依赖中移出。

### P2：迁移与服务端模式

- [ ] 增加数据库级 `core_runtime_control`、drain/maintenance lease 和受认证控制 API。
- [ ] 提供 PostgreSQL Core → SQLite 迁移、校验和回滚。
- [ ] 提供 SQLite → PostgreSQL 升级迁移、校验和回滚。
- [ ] 提供显式 Server/K8s Profile，默认 PostgreSQL。
- [ ] Web 与 Worker 角色拆分，验证多副本 Queue lease。
- [ ] K8s 校验拒绝多副本 SQLite 和未验证 RWX 文件系统。
- [ ] 若产品进入多租户阶段，另立租户隔离 ADR 和安全验收计划。

## 11. 验收标准

### 11.1 SQLite Desktop

- 全新安装无需 PostgreSQL/Docker 即可完成 Core 初始化和正常启动。
- 知识导入、稍后读、LLM Wiki 文件编译、语义维度任务、通知和查询结果 Catalog 均可运行。
- 两个并发领取者只能领取同一任务一次；失败/崩溃后的过期 lease 能被安全回收。
- 数据库短暂写竞争不会频繁暴露 `database is locked`。
- 强杀 Backend 后重启，数据库通过 integrity check，任务状态按 lease 规则恢复。
- 在线备份恢复后表数、行数、主键、关键摘要和文件引用一致。

### 11.2 PostgreSQL Server

- 多个 Web/Worker 副本不会重复完成同一任务或由陈旧 Worker 覆盖最终状态。
- 滚动升级期间 schema migration 具有明确的版本兼容窗口。
- Core PostgreSQL 不要求与 gbrain 共 database 或共享 owner。
- 数据库备份、恢复和故障切换不依赖应用 Pod 本地文件。

### 11.3 配置与降级

- Core SQLite 健康时，未配置 gbrain PostgreSQL 只显示可选能力不可用，不影响 Core 健康状态。
- 外部 PostgreSQL 数据源不可用时，只降级对应数据源，不影响 Core Catalog。
- 旧 `database.mode` 配置能兼容读取，并可以迁移到新的 provider/source 模型。
- 用户不能通过简单切换下拉框把已有 PostgreSQL Catalog 静默替换为空 SQLite。

### 11.4 Schema 与数据库迁移

- SQLite 和 PostgreSQL 均支持空库初始化，以及从发布策略声明的最近 `N` 个 schema version 升级；初始
  支持窗口设为最近两个 Release，后续只能显式扩大或在 Release Notes 中声明收窄。
- 在建临时表、复制数据、校验和原子切换前后注入中断，旧数据库均保持可恢复，重复执行迁移不会产生
  重复行或半迁移 schema。
- PostgreSQL Core 与 SQLite 互迁前，所有副本都能观察同一 `draining/maintenance` generation；维护期间
  新 mutation 和新任务领取被确定性拒绝，running 任务按协议排空。
- 迁移完成后按表校验行数、主键集合、外键、关键摘要、schema version 和 integrity check；任何校验失败
  都不得切换生效配置。

## 12. 风险与明确不做

### 风险

- SQLite 成为默认后，若没有先补齐 migration、WAL 和原子队列，会把原本被低并发掩盖的问题带入正式
  用户数据路径。
- 一个 Uvicorn/Gunicorn 配置启动多个应用进程时，每个进程都可能启动后台 Worker；必须有角色配置和
  Backend lease 防护。
- SQLite 与 PostgreSQL 对时间、JSON、约束和并发错误的细节不同，双数据库测试必须持续存在。
- 把 gbrain 状态和 Core 数据库状态拆分后，需要同步修改设置页、系统状态、CLI 和文档，避免出现新的
  能力含义漂移。

### 本方案不做

- 不在本轮把 gbrain 从 PostgreSQL/pgvector 改写为 SQLite vector store。
- 不合并 Milvus、Evaluation、Session 和 Core Catalog 的事实所有权。
- 不宣告已经支持多租户。
- 不采用长期 Core 双写。
- 不为了兼容多副本而继续让所有桌面用户安装 PostgreSQL。

## 13. 与既有文档的关系

本方案形成以下新边界：

- 取代 `2026-07-02-knowledge-import-job-queue-plan.md` 中“PostgreSQL 是正式任务状态存储，SQLite 仅用于
  本地开发”的旧结论；任务状态存储改为按运行模式选择，队列正确性由 lease/CAS 协议保证。
- 细化 `2026-08-11-user-home-sessions-and-skills-migration.md` 中 SQLite/K8s 的部署要求：桌面默认 SQLite，
  K8s 默认 PostgreSQL；K8s SQLite 只允许显式单副本和经验证单写块存储。
- 不改变 LLM Wiki/gbrain 现有独立 PostgreSQL + pgvector 决策，只解除它与 Core 数据库选择的耦合。
