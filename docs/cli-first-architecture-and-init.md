# PuddingClaw CLI-first 部署工具与初始化方案

> 状态：0.1.2 实现基线；无参数交互 init、Provider bootstrap、条件依赖探测与 Runtime 裁剪已落地
> 日期：2026-08-12

当前实现位于 `packages/puddingclaw-deploy-cli`，唯一命令为 `puddingclaw`，默认 Home 为
`~/.puddingclaw`。已完成独立配置、交互式 Profile/Provider 初始化、Python/uv/端口探测、Knowledge
依赖图探测、受校验的 Runtime Bundle 安装，以及带实例挑战应答的
`start/restart/status/open/stop/logs`。生产 Runtime 下载与签名、Analytics 数据源完整向导和安装后增量
扩展向导仍属于后续阶段。现有 Headless
`run/respond/cancel/models/capabilities` 已合并到产品 CLI 开发包，并保持 JSON、JSONL、Session、HITL
和 artifact export 协议兼容。
`init --plan --json` 会输出按 Profile 裁剪、含 `depends_on` 和 `execution_order` 的 Settings/Probe 计划。
0.1.2 已接入首次可用所需的 Provider、Catalog、PostgreSQL/pgvector、Milvus、Embedding 与 MinerU
分支；Harness 高级调优项仍使用产品默认值，后续由共享 Settings Schema 驱动高级向导，避免 Node CLI
复制一套会漂移的字段校验。

### 0.1.2 已落地的初始化顺序

```text
Profile
  └─ Initial Agent Provider + API Key 验证
      └─ Python / uv / Home / Ports
          ├─ Harness-only
          │   └─ 安装 requirements-harness.lock
          └─ Knowledge
              ├─ 本机 PostgreSQL 发现
              │   ├─ SQLite 轻量分支
              │   └─ PostgreSQL URL → 认证 → pgvector
              ├─ 本机/远程 Milvus → Collection API
              │   └─ 启用时要求 Embedding Provider
              └─ MinerU /health（可选，不阻断）
```

扩展选择同时作用于五个边界：Next 菜单与直达路由、FastAPI Router、Agent Tool 注册、后台 Worker、
Python requirements。源码开发启动没有 CLI 扩展契约时继续默认全量，因而不会改变当前开发实例；只有
CLI 管理的 Runtime 使用显式裁剪状态。

CLI 控制面写入 `deploy.json`；Backend/GUI 共享的产品设置继续写入 Home 下的 `config.json`。两者不能
混用：前者描述安装、端口、Profile 和 Runtime，后者必须严格遵守 Backend Settings Schema。

发行方式采用接近 OpenClaw 的单 npm 包体验：npm 包内携带 Node CLI、Backend wheel、锁定依赖描述和
Next standalone Web；`init` 再在用户 Home 内用 uv 准备版本隔离的 Python 环境。平台相关的大型可选
组件才使用外部 Release 资产。仓库内 `scripts/build-embedded-runtime.mjs` 负责生成嵌入式 Bundle，
`verify:publish` 在包名、License、依赖哈希或 Runtime 完整性未满足时阻止发布。
> 关联文档：[Headless Worker / CLI](./headless-worker-cli.md)、[开源项目结构与可选基础设施方案](./开源项目结构与可选基础设施方案.md)

> 范围说明：本文只描述通过 npm 安装的 PuddingClaw CLI 及其独立管理的 Runtime。当前通过源码脚本、开发服务器或 Electron 运行的 PuddingClaw 不在本次开发范围内；其进程、端口、配置和数据都不应被 CLI 开发版本读取、迁移、停止或改写。

## 1. 决策摘要

PuddingClaw 新增一种面向其他电脑的 **CLI-first 本地部署方式**：

```text
npm install -g @puddingai/puddingclaw
          │
          ▼
  puddingclaw CLI（Node.js）
          │
          ├─ 安装和升级版本化 Runtime
          ├─ 执行 init、探测与配置
          ├─ 管理 Backend / Web / 可选基础设施进程
          └─ 打开 GUI、执行 Headless Agent 命令
                    │
                    ▼
          Python FastAPI Backend
                    │ HTTP / SSE
                    ▼
              Next.js Web GUI
                    │
                    └─ 浏览器，或可选 Electron 薄壳
```

其中：

- npm 安装的是 Node.js CLI，不要求业务后端改写成 Node.js。
- Python Backend 继续承载 Agent、知识库、智能问数、配置、权限和任务运行时。
- Next.js GUI 继续通过 HTTP/SSE 访问 Backend，不依赖 Electron IPC 才能工作。
- Electron 降级为可选窗口壳，只连接已经运行的本地服务，不再拥有配置、Python、Docker 或数据生命周期。
- `puddingclaw init` 先选择产品能力组合，再完整走一遍已选择模块的用户可见设置面，并在相关步骤执行能力探测。
- Agent Harness 是最小 Core；知识库和智能问数是可独立启用的扩展。
- 探测本身只读；安装依赖、创建数据库、启动容器、初始化 gbrain、终止端口占用进程等动作必须单独确认。

## 2. 目标与非目标

### 2.1 目标

1. 用户只需记住一个入口：`puddingclaw`。
2. 首次安装后，通过一次 `puddingclaw init` 完成 Harness Core，以及用户主动选择的扩展配置。
3. Backend、Web、CLI 和可选 Electron 使用同一个用户配置与运行状态目录。
4. 端口冲突、依赖缺失和可选能力不可用时给出可操作的选择，不隐式破坏其他进程或数据。
5. 无 PostgreSQL、Milvus、MinerU、gbrain 或 Docker 时，Harness Core 仍可启动，并明确展示未启用的扩展能力。
6. 保留现有 Headless Worker CLI 能力，并逐步归入统一命令空间。

### 2.2 非目标

- 本轮不重写 Python Backend 或 Next.js GUI。
- 本轮不要求 Electron 立即删除。
- npm 包不内嵌完整 Python 解释器；缺失时由 `init` 一键准备用户级 Python 环境。
- `init` 不应擅自安装 Docker、PostgreSQL、pgvector、MinerU 或终止未知进程。
- 不把所有内部兼容参数都暴露成新手问题；`init` 覆盖当前用户可见设置，隐藏的 legacy/internal 参数继续使用默认值。

### 2.3 产品能力分层

| 层 | 内容 | 默认 |
| --- | --- | --- |
| Harness Core | Agent 对话、模型调用、Session、工具编排、文件与终端、Skill、SubAgent、权限、上下文、Goal/Rubric、Trace | 必选 |
| Knowledge 扩展 | 知识目录、导入任务、RAG、MinerU、Milvus、LLM Wiki、gbrain | 可选，默认关闭 |
| Analytics 扩展 | 数据源、Profile、语义资产、智能问数、NL2SQL/Vanna、结果存储 | 可选，默认关闭 |
| Headless Worker 扩展 | Worker Access Key、外部调用、运行审计 | 可选，默认关闭 |

知识库和智能问数彼此独立。用户可以选择：

```text
[1] 只使用 Agent Harness（推荐首次体验）
[2] Agent Harness + 知识库
[3] Agent Harness + 智能问数
[4] 完整功能
[5] 自定义选择
```

扩展状态必须显式保存，例如：

```json
{
  "extensions": {
    "knowledge": {"enabled": false},
    "analytics": {"enabled": false},
    "headless_worker": {"enabled": false}
  }
}
```

“未启用”和“已启用但探测失败”是不同状态：前者是 `disabled`，不应在系统状态中显示为红色故障；后者是 `degraded` 或 `unavailable`。

## 3. 架构原则

### 3.1 安装目录只读，用户目录可写

程序包和用户状态必须分离。不得继续把生产配置、虚拟环境、Session 或日志写回 npm 包、应用包或源码目录。

默认用户目录：

```text
~/.puddingclaw/
├── config.json                 # 非敏感、用户可迁移配置
├── providers.json              # Provider、Endpoint、模型与 Binding
├── credentials.json            # 首期本地凭证存储，0600；后续可换 OS Keyring
├── secrets/
│   ├── headless-token          # CLI 与本地 Backend 的随机认证凭证，0600
│   ├── initial-provider-api-key# 首次 Provider Key，仅注入 Backend
│   ├── embedding-provider-api-key
│   └── database-url            # 含数据库密码时也不进入 deploy.json
├── runtime.json                # 当前实例 URL、PID、版本和所有权
├── init-state.json             # 未完成 init 的可恢复草稿，不含明文密钥
├── toolchains/
│   ├── uv/                       # CLI 一键准备的用户级 uv
│   └── python/                   # 本机无兼容 Python 时安装的受管理 Python
├── runtime/
│   ├── releases/<release-version>/
│       ├── manifest.json
│       ├── backend/            # Python wheel/应用资源
│       ├── web/                # Next.js standalone
│   └── venvs/<release-version>/# 版本隔离的 Python 环境
├── logs/
├── sessions/
├── cache/
└── data/
```

平台默认路径可以遵循操作系统习惯，但都必须能由 `PUDDINGCLAW_HOME` 覆盖：

| 平台 | 默认根目录 |
| --- | --- |
| macOS / Linux | `~/.puddingclaw` |
| Windows | `%APPDATA%\PuddingClaw` |

现有 `backend/config.json` 需要迁移到用户目录。Provider Registry 已经采用用户目录，应与普通配置统一到同一个根路径解析器。

### 3.2 一个配置事实源

配置优先级固定为：

```text
命令行本次参数
  > 受支持的部署环境变量
  > 用户目录 config/providers/credentials
  > 程序默认值
```

- GUI 设置页和 CLI 调用同一套配置服务、Schema 和校验规则。
- 环境变量覆盖必须在 `config show` 和 GUI 中明确标记，不能显示为已经写入配置。
- Provider、Endpoint、模型、模型分类和 Binding 以 Provider Registry 为事实源。
- `fallback_llm`、`fallback_embedding` 中的旧凭证只用于迁移，不继续作为新安装的主配置入口。
- 明文密钥不写入 `config.json`、`runtime.json`、命令历史或日志。CLI 与本地 Backend
  之间的随机认证凭证单独存入 `secrets/headless-token`，仅在启动 Backend 时注入；外部 Worker Key
  仍由 Backend 的 Worker Access Key Store 管理。

### 3.3 CLI 拥有生命周期

只有 CLI Runtime Supervisor 可以管理本地服务生命周期。它必须记录每个进程的：

- PID；
- 启动时间；
- 可执行文件和版本；
- Backend / Frontend 实际 URL；
- 由哪个 PuddingClaw 实例启动；
- 日志文件；
- 基础设施是否由本次实例创建或接管。

`stop` 只终止 `runtime.json` 中经过 PID、启动时间和实例标识复核的自有进程。Electron、GUI 或另一份 CLI 不得停止无法证明所有权的进程或 Compose Project。

### 3.4 扩展在运行时组合，而不是只在 UI 中隐藏

Backend 启动时根据 `extensions.*.enabled` 构建 Runtime Composition：

```text
Harness Core
├── knowledge enabled?  ──► Knowledge API / Tools / Workers / Guides
├── analytics enabled?  ──► Analytics API / Tools / Workers / Guides
└── headless enabled?   ──► Worker API / Access / Audit
```

未启用扩展时必须同时满足：

- 不注册对应 Agent Tool，模型完全看不到其名称和 Schema；
- 不注入对应 Tool Guide、System Prompt 或路由提示；
- 不启动导入、索引、Profile、语义构建或清理 Worker；
- 不连接和探测该扩展专属的 PostgreSQL、Milvus、MinerU 或 gbrain；
- 不加载大体积可选 Python 依赖；
- GUI 隐藏对应工作台，或只展示一个明确的“启用扩展”入口；
- 对旧书签/API 请求返回稳定的 `extension_disabled`，不能伪装成基础设施故障。

扩展启用必须由 Toolset/Runtime Composition 层控制，不能只依赖前端隐藏菜单，也不能把不可用 Tool 暴露给模型后再在执行时报错。

Agent 的工具可见性矩阵至少满足：

| 运行模式 | Core Harness Tools | Knowledge Tools | Analytics Tools |
| --- | --- | --- | --- |
| Harness-only | 可见 | 不注册 | 不注册 |
| Harness + Knowledge | 可见 | 按已配置能力注册 | 不注册 |
| Harness + Analytics | 可见 | 不注册 | 按已配置能力注册 |
| Full | 可见 | 按已配置能力注册 | 按已配置能力注册 |

Knowledge Tools 包括知识检索、导入、索引、Wiki/gbrain 查询等工具族；Analytics Tools 包括数据源、Profile、语义资产、NL2SQL、结果查询等工具族。具体工具仍受 Session、项目、权限和能力状态二次过滤。

测试必须对最终发送给模型的 tool schema 做快照或集合断言，证明 Harness-only 模式下没有 Knowledge/Analytics Tool；只测试菜单隐藏或 API 返回 403 不算完成。

## 4. npm CLI 与 Python Backend 的发行方式

### 4.1 薄 npm CLI + `init` 准备 Python 环境

```bash
npm install -g @puddingai/puddingclaw
puddingclaw init
```

CLI 要求 Node.js 20+，并在 `init` 中探测 Python 3.11/3.12 和 `uv`：

1. npm 只安装轻量 Node CLI。
2. `init` 依次探测系统 PATH、平台 Python Launcher 和已知 `uv` 环境中的兼容 Python。
3. 找到多个兼容解释器时，显示版本、路径、架构和来源，允许用户选择；默认推荐最新的 Python 3.12。
4. 如果没有兼容 Python，询问用户是否“一键准备 Python 环境”。
5. 用户确认后，CLI 先准备用户级 `uv`，再由 `uv` 安装受管理的 Python 3.12；不替换、不升级、不删除系统 Python。
6. 使用选中的 Python 在 `runtime/<version>/venv` 创建 PuddingClaw 独立虚拟环境。
7. 先安装轻量 Harness Core Backend 和匹配版本的 Next.js standalone bundle。
8. 用户选择产品能力组合后，再按需安装 Knowledge/Analytics 的 Python optional dependencies；Harness-only 不下载这些扩展依赖。
9. 不在 npm 全局包目录或 Git 工作区中执行 `uv sync`。

“一键配置”必须是用户明确确认后的可观察流程，而不是静默修改系统：

```text
未找到兼容的 Python 3.11/3.12。

[1] 一键准备 Python 3.12（推荐）
    将安装用户级 uv 和受管理 Python 到 ~/.puddingclaw/toolchains，
    不修改系统 Python。
[2] 手动指定 Python 路径
[3] 查看手动安装说明
[4] 退出
```

如果 `uv` 已存在则复用兼容版本；如果不存在，CLI 从固定版本的官方安装地址下载安装器，由官方安装器校验其发行产物，并在完成后校验实际 `uv` 版本。所有安装都限制在用户目录，不要求 root/admin，也不自动修改 Shell profile。正式发布前还必须在干净的 macOS、Linux 和 Windows 环境验证这条链路。安装失败时保留诊断日志，并允许重试、手动指定解释器或退出。

非交互模式不得自动下载或安装 Python，除非显式传入例如 `--prepare-python`；CI 也可以通过 `--python /absolute/path/to/python` 指定解释器。

本方案明确不把 Python 解释器预先塞进 npm 包。用户可以预装 Python，也可以在 `init` 中让 PuddingClaw 一键完成用户级环境准备。

### 4.2 版本兼容

每个 release 必须包含：

```json
{
  "cli": "1.0.0",
  "backend": "1.0.0",
  "web": "1.0.0",
  "protocol": "1",
  "python": ">=3.11,<3.13",
  "node": ">=20",
  "sha256": {}
}
```

CLI 在启动前校验兼容性；不允许新 Web 静默连接不兼容的旧 Backend。升级先安装新 Runtime，健康检查成功后原子切换 active version，失败则保留旧版本可回滚。

## 5. 命令面

### 5.1 生命周期与配置

```bash
puddingclaw init
puddingclaw init --advanced
puddingclaw init --config ./puddingclaw-init.yaml --non-interactive
puddingclaw config show
puddingclaw config get <key>
puddingclaw config set <key> <value>
puddingclaw config edit
puddingclaw extension list
puddingclaw extension enable knowledge
puddingclaw extension enable analytics
puddingclaw extension disable <name>

puddingclaw start
puddingclaw start --open
puddingclaw start --backend-port 9000 --frontend-port 4000
puddingclaw start --port auto
puddingclaw status
puddingclaw doctor
puddingclaw logs --follow
puddingclaw open
puddingclaw stop
puddingclaw restart
```

### 5.2 可选基础设施

```bash
puddingclaw infra status
puddingclaw infra start postgres
puddingclaw infra start milvus
puddingclaw infra start mineru
puddingclaw infra stop <service>
```

### 5.3 Agent / Worker 命令

目标命令空间：

```bash
puddingclaw agent run "分析上个月销售变化"
puddingclaw agent respond <run-id> --input-json -
puddingclaw agent cancel <run-id>
puddingclaw agent models list
```

只保留 `puddingclaw agent ...` 这一套 Agent 命令空间，不并行保留顶层 `run/respond/cancel/models` 别名。生命周期命令不得改变 Headless 命令的 stdout/JSON、退出码和 HITL 协议。

禁用扩展的 API 保持 `404` 边界，但必须返回可区分、可修复的结构化响应。例如知识库未启用时返回 `code=extension_disabled`、`extension=knowledge`，并提示运行 `puddingclaw init`。直接访问禁用扩展页面时进入统一说明页，不得无提示跳回首页。

## 6. 端口与进程冲突

### 6.1 默认行为

- Backend 和 Web 默认只绑定 `127.0.0.1`。
- 默认端口分别为 `8888` 和 `3000`，但可以持久配置或本次覆盖。
- 端口占用时，CLI 先识别占用者，绝不直接执行 `lsof ... | xargs kill`。

交互终端显示：

```text
端口 8888 已被占用
PID: 12345
程序: python
命令: uvicorn other_app:app

[1] 停止该进程并使用 8888
[2] 自动选择可用端口
[3] 手动指定其他端口
[4] 取消启动
```

规则：

1. 只有用户选择 1 后才能向未知进程发送 `SIGTERM`。
2. 等待超时后需要再次确认，才能升级为强制终止。
3. 如果占用者是已验证的同版本 PuddingClaw Backend，优先提供“复用已有实例”或“重启自有实例”。
4. 如果目标只是另一个返回 200 的 HTTP 服务，不能仅凭端口和健康路径把它认作 PuddingClaw；还需校验实例 ID、协议和版本。
5. 非 TTY 模式不得弹询问或自动杀进程，必须报结构化冲突，除非传入明确参数。

非交互参数：

```bash
puddingclaw start --backend-port 9000 --frontend-port 4000
puddingclaw start --port auto
puddingclaw start --backend-port 8888 --kill-port-owner
```

`--kill-port-owner` 是明确授权，但仍需验证精确 PID，禁止用模糊进程名、未解析环境变量或宽泛端口范围执行终止。

### 6.2 动态端口传播

自动选择的端口只写入 `runtime.json`，不覆盖用户默认配置：

```json
{
  "instance_id": "pc-...",
  "backend_url": "http://127.0.0.1:9000",
  "frontend_url": "http://127.0.0.1:4173",
  "backend_pid": 1234,
  "frontend_pid": 1235,
  "owner": "puddingclaw-cli",
  "started_at": "2026-08-10T10:00:00+08:00"
}
```

Web 启动时注入实际 `BACKEND_INTERNAL_URL`；`open/status/logs/stop` 读取 `runtime.json`，而不是重新假设 3000/8888。

## 7. `puddingclaw init` 总体协议

### 7.1 交互原则

- 默认向导会依次访问 Harness Core，以及用户已选择扩展的全部用户可见设置分组。
- 未选择的扩展只记录为 `disabled`，跳过其配置问题和全部专属探测。
- 每组先显示当前值、推荐值和探测结果；用户可以直接接受默认值。
- 可选能力允许选择“禁用”，但仍会记录显式禁用状态，不留含糊的半配置。
- `--advanced` 展开所有调优字段；普通模式也必须逐组经过，只是可整组接受推荐值。
- `Ctrl+C` 保存不含密钥的草稿，重新运行可选择继续或重新开始。
- 重新运行 `puddingclaw init` 可以启用或关闭扩展；关闭前必须提示仍会保留哪些资产和配置。
- 最后先显示变更摘要，再原子写入正式配置。
- 密钥通过隐藏输入读取；answer file 只允许 `env://NAME`、Keyring 引用或交互补录，不接受明文 secret。

### 7.2 探测与变更的边界

探测操作必须满足：

- 默认只读；
- 有明确超时；
- 返回稳定的 code、status、latency、details 和 remediation；
- 可选能力失败不会阻止 Core 初始化；
- 必需能力失败时允许修复后重试、修改配置或退出；
- 探测结果不能代替用户配置，也不能因为探测成功就静默启用能力。

以下动作不是探测，必须再次确认：

- 下载或安装 Python、uv、Backend 或 Web Runtime；
- 启动 Docker daemon 或 Compose 服务；
- 创建 PostgreSQL database/user/extension；
- 拉取 Docker 镜像；
- 创建或删除 Milvus collection；
- 初始化 gbrain Schema Pack；
- 创建 Worker Access Key；
- 终止端口占用进程；
- 修改 Shell profile、PATH、系统服务或开机启动项。

### 7.3 共享探测实现

Node CLI 不应重新实现 asyncpg、pgvector、Milvus、模型协议和 Docker 沙箱的业务判断。安装 Runtime 后，由 CLI 调用版本匹配的 Python bridge：

```bash
<managed-python> -m puddingclaw_runtime probe database --json
<managed-python> -m puddingclaw_runtime probe capabilities --json
```

Python bridge 与 FastAPI 的 `/api/settings/.../test`、`/api/capabilities` 共用 Probe Service。这样 CLI 和 GUI 得到相同的错误码、超时和修复建议。

## 8. `init` 完整步骤

### 8.1 阶段 0：安装和本机环境

检查并展示：

| 项目 | 探测 | 失败处理 |
| --- | --- | --- |
| OS / Arch | 平台是否有匹配 Runtime | 阻断并给出受支持平台 |
| Node.js | 当前版本是否 >=20 | npm CLI 已运行时通常通过；提示版本不一致 |
| Python | 3.11/3.12、路径、架构、来源 | 确认后一键准备或选择其他解释器 |
| uv | 版本和路径 | 确认后安装用户级 uv |
| 用户目录 | 创建、写入、权限、剩余空间 | 选择其他 `PUDDINGCLAW_HOME` |
| Runtime manifest | 签名、SHA-256、版本兼容 | 阻断当前版本安装 |

本阶段同时询问：

- PuddingClaw Home；
- release channel（首期只提供 stable，也应写入配置）；
- 是否允许检查更新；
- 是否创建 Shell completion；
- 是否在 `start` 后自动打开浏览器。

### 8.2 阶段 1：选择产品能力组合

向导首先确认本次启用的模块：

```text
请选择初始化方案：

[1] 只使用 Agent Harness（推荐首次体验）
    无需 PostgreSQL、Milvus、MinerU 或 gbrain。
[2] Agent Harness + 知识库
[3] Agent Harness + 智能问数
[4] 完整功能
[5] 自定义选择
```

选择结果立即决定后续向导和探测计划：

| 选择 | 继续执行 | 跳过 |
| --- | --- | --- |
| 只使用 Harness | 服务、Agent 模型、Harness、可选 SubAgent | PostgreSQL、知识库、RAG、Milvus、MinerU、gbrain、智能问数 |
| + 知识库 | Catalog DB、知识目录、索引、Wiki/gbrain 可选项 | 智能问数 |
| + 智能问数 | Analytics DB/数据源、Profile、NL2SQL、结果配置 | 知识库导入/RAG；共享基础设施按需配置 |
| 完整功能 | 全部已选择模块 | 无 |

扩展可以保存默认配置模板，但未启用时不得进行连接、创建资源或激活 Tool。用户以后可以通过 `puddingclaw extension enable knowledge`、`puddingclaw extension enable analytics` 或重新运行 `init` 补充配置。

### 8.3 阶段 2：本地服务与端口

配置：

- Backend host/port；
- Frontend host/port；
- 固定端口或自动端口策略；
- 端口冲突默认行为：询问、报错或自动换端口；
- 日志级别和日志保留期；
- 是否允许局域网访问。

探测：

- host 是否为合法本地绑定；
- 端口可用性；
- 占用进程详情；
- 若启用非回环地址，检查访问 Token、CORS 和风险确认。

推荐默认值是 `127.0.0.1`、冲突时询问、非 TTY 时报错。

### 8.4 阶段 3：扩展数据库与 pgvector

仅在知识库或智能问数扩展需要数据库时进入本阶段。Harness-only 跳过，不探测 PostgreSQL。

配置：

- `bundled` 或 `external`；
- host、port、database、username、password；
- 是否允许创建缺失 database；
- bundled 模式的 Compose Project 和数据目录。

探测顺序：

1. TCP 可达性；
2. PostgreSQL 鉴权；
3. 目标 database 是否存在；
4. server version；
5. pgvector extension 是否安装及版本；
6. 当前用户是否具备必要 schema/migration 权限；
7. Backend 预期 migration 版本是否兼容。

database 不存在时只能询问是否创建；pgvector 缺失时显示精确安装指令，不能仅显示“数据库不可用”。bundled 模式若 Docker 不可用，应允许切换 external 或退出，不能假装启动成功。

### 8.5 阶段 4：模型 Provider、Endpoint 与 Binding

对每个启用的 Provider 依次配置：

- Provider 启用状态和显示名；
- credential scope；
- 一个或多个 API Key 名称；
- Endpoint URL、protocol、route path；
- Endpoint 支持能力：`llm`、`text_embedding`、`multimodal_embedding`、`rerank`；
- 模型发现和手动登记；
- 模型分类、dimension、batch size、concurrency；
- thinking profile。

每个 Endpoint 分开执行：

1. URL 和 TLS 基础校验；
2. 鉴权/连通性测试；
3. 模型列表发现；
4. 用户从发现结果中登记模型；
5. 对选中模型执行最小真实请求。

随后完成全局 Binding：

| Binding | 用途 | 必需性 |
| --- | --- | --- |
| `agent` | 对话、规划与工具调用 | Core 必需 |
| `image_analyzer` | 图片理解 | 可选 |
| `text_embedding` | 文本检索、部分分析能力 | 仅所选扩展按需必需 |
| `multimodal_embedding` | 图文向量索引 | 可选 |
| `rerank` | 候选重排 | 可选 |

如果启用 AI Gateway，还要配置和探测：

- base URL；
- health path；
- gateway 模型；
- `fallback_to_direct`；
- 环境变量覆盖状态。

模型发现成功不代表模型可调用；Binding 前至少做一次能力匹配和最小请求。视觉、Embedding 和 Rerank 不应只依赖模型名称猜能力。

### 8.6 阶段 5：知识库与解析

仅在 Knowledge 扩展启用时进入本阶段。

配置和探测：

| 配置 | 探测 |
| --- | --- |
| `knowledge.root_dir` | 绝对路径、创建确认、读写权限、剩余空间 |
| MinerU base URL | `GET /health`、版本、响应时间 |
| MinerU runtime output | 目录权限；是否保留临时产物 |
| 搜索目录规则 | 路径合法性、是否越过知识库根目录 |
| 多模态 Embedding batch size | 与选中模型声明的能力和限制一致 |

用户必须理解知识库目录是长期资产目录，而不是缓存。若 MinerU 不可用，向导允许禁用富解析并保留 Markdown、表格和基础解析能力。

### 8.7 阶段 6：Milvus 与知识索引

仅在 Knowledge 扩展启用且用户选择向量索引时进入本阶段。

配置：

- 是否启用 `knowledge.multimodal_index`；
- vector store；
- Milvus URI；
- text/image collection；
- BM25；
- overwrite 策略；
- Embedding 和 Rerank Binding。

探测：

1. Milvus 连通性和版本；
2. 鉴权；
3. 列出 collection 的权限；
4. 已有 collection 的维度、metric 和 schema 是否兼容选中模型；
5. text/image collection 是否发生命名冲突；
6. Rerank Endpoint 最小请求。

`init` 不创建、覆盖或清空 collection，除非用户明确确认。Milvus 失败时可显式禁用向量索引，保留本地精确检索。

### 8.8 阶段 7：LLM Wiki 与 gbrain

仅在 Knowledge 扩展启用且用户选择 LLM Wiki/gbrain 时进入本阶段。

配置：

- Wiki Compiler Agent 模型；
- Hybrid Retrieval 是否启用；
- gbrain Embedding 模型；
- gbrain Think 模型；
- gbrain 独立 PostgreSQL database 和 owner。

探测：

- 模型 Binding 是否满足能力；
- gbrain CLI/Runtime 版本；
- 独立数据库连接与 pgvector；
- Schema Pack 是否已安装、版本和 hash 是否一致；
- Wiki workspace 是否完整；
- MCP allowlist 和只读工具是否可发现。

创建独立 database、安装 Schema Pack、首次同步都属于写操作，必须分别确认。用户选择禁用时，不影响普通知识库。

### 8.9 阶段 8：RAG 检索参数

仅在 Knowledge 扩展启用时进入本阶段。

普通模式显示一组推荐配置并要求确认；`--advanced` 逐项编辑：

- RAG enabled；
- `top_k`；
- similarity threshold；
- hybrid enabled/mode；
- text vector、image vector、BM25 权重；
- hybrid candidate top-k；
- rerank enabled/model/top-n/candidate top-k。

校验包括权重范围及总和、`top_n <= candidate_top_k`、所需 Binding 和基础设施是否可用。能力未配置时必须把相关功能保存为 disabled，而不是启用后运行时才静默失败。

### 8.10 阶段 9：智能问数

仅在 Analytics 扩展启用时进入本阶段。

配置：

- Vanna enabled；
- default database source ID；
- default dialect；
- entity top-k 默认值和按类型覆盖；
- LLM/Embedding 复用 Binding；
- Vanna Milvus collections 和 metric；
- training 开关。

结果预览：

- full rows token budget；
- preview rows token budget；
- profile token budget；
- hard row/column cap；
- max cell chars；
- query timeout；
- SQL generation timeout。

持久化与路径开关：

- result materialization row cap；
- result store enabled / TTL；
- default/max page size；
- export enabled；
- profile enabled；
- Agent SQL path enabled、rollout percentage、fallback、shadow compare。

探测至少覆盖默认数据源、SQL 只读执行、查询超时能力、Vanna collection 兼容性。涉及执行测试 SQL 时，应显示语句并确认；默认只执行 `SELECT 1` 和只读元数据查询。

### 8.11 阶段 10：上下文、缓存与压缩

配置：

- compression ratio；
- summarization model；
- summarization trigger tokens；
- tool context enabled；
- immediate compaction；
- single tool trigger tokens；
- background minimum result tokens；
- keep recent tool results；
- Prompt Cache 的 trace diagnostics、ordered system sections、tail routing、deterministic session projection、stable tool schema。

向导执行范围校验，并检查摘要模型 Binding。此阶段不需要发起昂贵模型调用；模型连通性复用阶段 4 的结果。

### 8.12 阶段 11：Goal、Rubric 与运行保护

配置：

- Goal enabled、显式激活策略、max rounds；
- Rubric enabled、模型、max iterations、max stagnant repairs；
- Custom Rubric Rules enabled 和逐条规则；
- Model Call Limit enabled、run/thread limit、exit behavior。

向导需要解释预算之间的关系，并校验 Rubric 模型存在。自定义规则应逐条展示 ID、statement、required 和 verifier，不把空规则写入配置。

### 8.13 阶段 12：终端与 Docker 沙箱

配置：

- sandbox mode：`auto`、`kernel`、`docker`；
- unavailable policy；
- default timeout；
- Docker connection/context；
- image；
- CPU、memory、PID limit；
- persistent network；
- dependency setup；
- project lifecycle 和 idle stop。

探测顺序：

1. Kernel sandbox 是否支持当前平台；
2. Docker daemon、connection 和 context；
3. 镜像是否存在；
4. 如镜像缺失，询问是否 pull/build；
5. 运行一个无网络、只读、短时容器检查 Python/Node 版本；
6. 检查资源限制是否被当前 Docker Runtime 支持。

普通“探测 Docker”不得创建容器；第 5 步属于验证运行，必须单独确认。`auto` 模式应明确最终优先级和 fail-closed 行为。

### 8.14 阶段 13：SubAgent

逐个配置：

- enabled、name；
- model；
- description、route trigger；
- tools inherit/none；
- skills inherit/custom/none 和 paths；
- system prompt。

探测和校验：模型存在、Skill 路径存在、名称唯一、route trigger 合法。图片分析 SubAgent 应与 `image_analyzer` Binding 保持一致。

### 8.15 阶段 14：记忆、项目上下文、MCP 与 Worker Access

配置：

- memory backend（Markdown/mem0）及对应模型/Embedding；
- 默认项目目录和项目上下文文档；
- MCP server enabled list、超时、allowlist；
- Web Search / Connector 的显式启用状态；
- 是否启用 Headless Worker Access；
- Worker Key 名称、允许的能力和项目根目录。

探测：

- Memory 文件/索引目录权限；
- mem0 所需模型 Binding；
- 项目路径存在且可访问；
- MCP server handshake 和工具 allowlist；
- Connector CLI/凭证状态；
- Worker Access Key 管理存储可写。

创建或轮换 Worker Key 时，Token 只显示一次；默认不在 `init` 中自动创建远程访问凭证。

### 8.16 阶段 15：汇总、提交和启动验收

提交前输出：

```text
Harness Core
  ✓ Runtime 1.0.0
  ✓ Agent model: deepseek:...
  ✓ Session / Permission / Goal / Trace
  ✓ Kernel sandbox

Extensions
  ○ Knowledge disabled
  ○ Analytics disabled
  ○ Headless Worker disabled

Ports
  Backend: 127.0.0.1:8888
  Web:     127.0.0.1:3000
```

用户确认后：

1. 原子写入 `config.json` 和 `providers.json`；
2. 单独写入凭证存储；
3. 删除 `init-state.json` 中已提交的草稿；
4. 可选执行 `puddingclaw start`；
5. 等待 Backend health、协议版本和 Web 页面；
6. 执行一次强制 capability probe；
7. 输出可用、降级和不可用能力；
8. 用户选择后打开浏览器。

### 8.17 现有设置页到 `init` 的映射

| 当前设置分组 | `init` 阶段 | 主要探测 |
| --- | --- | --- |
| 模型服务 | 阶段 4 | Endpoint 鉴权、模型发现、最小模型请求、Binding 能力匹配 |
| 项目上下文 | 阶段 14 | 默认项目路径、上下文文档读写 |
| 智能问数 | 阶段 9，仅 Analytics | 默认数据源、只读 SQL、Vanna/Milvus collection |
| RAG 设置 | 阶段 8，仅 Knowledge | 模型 Binding、权重和 top-k 约束、所需索引能力 |
| 知识库 | 阶段 3、5、6、7，仅 Knowledge | PostgreSQL/pgvector、目录、MinerU、Milvus、gbrain |
| 记忆管理 | 阶段 14 | 文件/索引目录、mem0 模型依赖 |
| Harness 配置 | 阶段 10、11、12、13 | 模型预算、Docker/Kernel Sandbox、SubAgent/Skill |
| Worker Access | 阶段 14，仅 Headless Worker | Key Store、项目根目录；创建 Key 需单独确认 |
| 高级设置 | 已选模块各阶段的 advanced 问题 | 范围、依赖和交叉字段校验 |
| 系统状态 | 阶段 15 与 `doctor` | 聚合 Core 和已启用扩展探测，区分 available/degraded/disabled |

设置页中未来新增任何用户可见配置时，必须同时声明：

1. 所属 `init` 分组；
2. 默认值和推荐值；
3. 是否需要 probe；
4. probe 是否只读；
5. 是否属于 Core 阻断项；
6. 非交互 answer-file 字段；
7. 脱敏和迁移规则。

CI 应校验用户可见 Settings Schema 的字段都被 `init` schema 覆盖，防止 GUI 新增设置后 CLI 向导长期遗漏。

## 9. 探测矩阵

CLI 先根据扩展状态生成 probe plan。下表中的扩展探测只有在对应扩展或子能力启用时才执行；`disabled` 项不发起网络连接，也不计入失败数。

| 能力 | 探测入口 | 默认超时 | 必需 | 失败结果 |
| --- | --- | ---: | --- | --- |
| Runtime manifest | CLI 内置校验 | 10s | 是 | 阻断安装 |
| Python / uv | CLI 本机探测 | 3s | 是 | 一键准备、换解释器或退出 |
| Backend port | CLI socket/process probe | 2s | 是 | 已认证的本 CLI 实例可安全停止；未知进程只能换端口或退出 |
| PostgreSQL | Python Probe Service | 5s | 启用相关扩展时 | 修复、切 external/bundled 或关闭扩展 |
| pgvector | Python Probe Service | 5s | 所选扩展要求时 | 显示版本和安装命令，或关闭相关能力 |
| Provider Endpoint | Provider Registry Probe | 10s | 至少 Agent 是 | 修改 Endpoint/Key 或退出 |
| 模型最小请求 | Model Client Probe | 30s | Agent 是 | 换模型或退出 |
| Knowledge root | Python filesystem probe | 3s | Knowledge 启用时 | 换目录、修复权限或关闭扩展 |
| Docker | Sandbox Manager probe | 5s | 否 | 禁用 Docker 相关能力 |
| Milvus | Milvus adapter probe | 3s | 否 | 禁用向量索引 |
| MinerU | `GET /health` | 3s | 否 | 禁用富解析 |
| gbrain | Runtime/database/schema probe | 10s | 否 | 禁用 Wiki/gbrain 增强 |
| MCP | Server handshake | 每服务 10s | 否 | 禁用对应 Server |
| Web | HTTP health/page | 30s | 是 | 保留 Backend，报告 Web 启动失败 |

所有结果统一输出机器可读结构：

```json
{
  "probe": "postgres.pgvector",
  "status": "failed",
  "required": true,
  "code": "extension_missing",
  "latency_ms": 82,
  "details": {"server_version": "17.5"},
  "remediation": ["运行 CREATE EXTENSION vector", "查看平台安装命令"]
}
```

`doctor --json`、GUI 系统状态和 `init` 必须复用这套结构，避免三个入口对同一能力给出不同结论。

## 10. GUI 和 Electron 的调整

### 10.1 Web GUI

- GUI 设置页继续保留，作为 `init` 后的日常修改入口。
- GUI 使用 Backend 配置 API，不直接写本地配置文件。
- 无 Electron 时，目录选择使用路径输入、受限后端文件浏览器或浏览器上传。
- 系统状态页展示最近探测时间、来源、缓存状态和“重新探测”按钮。
- GUI 修改关键设置后只使相关 probe cache 失效，不在每次页面加载时并发探测所有外部服务。

### 10.2 Electron 薄壳

Electron 只做：

- 读取 `runtime.json` 或调用 `puddingclaw status --json`；
- 在窗口内加载 `frontend_url`；
- 提供系统目录选择器、通知、托盘等可选桌面能力；
- Backend 未运行时提示用户执行/授权 `puddingclaw start`。

Electron 不再：

- 创建 Python venv；
- 启动或关闭未知 Backend；
- 无条件停止 Docker Compose；
- 固定占用或清理 3000/8888；
- 持有独立配置目录。

## 11. 安全与权限

1. 本地服务默认只监听回环地址。
2. GUI 与 Backend 之间使用本地实例 Token；非回环绑定必须强制配置鉴权和 CORS。
3. 凭证输入不回显，不出现在 argv、Shell history、日志或诊断 JSON。
4. answer file 使用环境变量或 Keyring 引用，不保存明文 Secret。
5. PID 所有权不能只靠 PID：必须同时校验启动时间、可执行文件和实例 ID，防止 PID 重用。
6. `stop`、端口清理、数据库创建、collection 重建和基础设施停止遵循最小目标原则。
7. `doctor` 默认脱敏；`--json` 同样不得泄露 Token、密码和 API Key。
8. CLI 不代表用户自动批准 Agent HITL；现有 Headless 权限协议保持不变。

## 12. 部署 CLI 开发计划

### 12.1 开发顺序原则

目标场景是在其他电脑上，通过 npm CLI 完成 PuddingClaw 的安装和部署。当前开发机上正在运行的 PuddingClaw 不参与这条链路。

推荐顺序是：

```text
CLI 骨架、独立 Home 与 Runtime Supervisor
        │
        ▼
通用 Init Step / Probe / Extension 框架
        │
        ▼
Harness-only 最小部署切片
        │
        ├─► Knowledge 扩展步骤
        ├─► Analytics 扩展步骤
        └─► Headless Worker 扩展步骤
                    │
                    ▼
              Full Profile 部署验收
```

具体策略：

1. 先实现 CLI 命令框架、独立 Home、Python 一键准备、Runtime 下载/安装和进程管理。
2. Init Engine 从第一版就支持 Step Registry、条件执行和 `extensions.*.enabled`，避免后面重写一套线性全量向导。
3. 首个可部署 Profile 采用 Harness-only：新电脑只配置 Agent 模型即可启动 GUI 并完成 Agent Run。
4. 再增加 Knowledge、Analytics、Headless Worker 的配置步骤、探测、依赖和 Runtime Composition。
5. 最后在干净机器上验收 Full Profile，确认所有已选步骤可以组合部署。

因此不建议先写死“全量部署向导”再补 Harness-only。不是因为需要兼容当前运行实例，而是为了避免 CLI 自身的向导、依赖和探测逻辑返工。

### 12.2 与开发机当前实例隔离

CLI 开发和验收使用独立目录与自动端口，例如：

```bash
PUDDINGCLAW_HOME="$HOME/.puddingclaw-test" puddingclaw init
PUDDINGCLAW_HOME="$HOME/.puddingclaw-test" puddingclaw start --port auto
```

开发版必须满足：

- 不读取或迁移当前源码实例使用的 `backend/config.json`；
- 不复用当前实例的 PID、Session、数据库、知识目录或 Docker Compose Project；
- 不停止当前实例及其基础设施；
- 默认使用独立 Home 和不冲突的开发端口；
- 只消费构建产物和 release manifest，不把当前 Git 工作区当成用户安装目录。

### Phase 0：CLI 独立路径与配置 Schema

- 在 CLI-managed Runtime 中引入统一 `PUDDINGCLAW_HOME` 路径模块；
- 增加 `extensions.knowledge/analytics/headless_worker.enabled` 作为运行时组合事实源；
- 新部署直接把配置写入 CLI Home，不自动迁移开发机的 `backend/config.json`；
- Provider、Credential、Session、日志都使用 CLI Home；
- 为 GUI 和 CLI 提供共享配置 Schema、迁移器和脱敏展示。

### Phase 1：Runtime Supervisor

- 新增 `start/status/logs/open/stop/restart`；
- 实现 PID/实例所有权和 `runtime.json`；
- 实现安全端口冲突流程及动态端口传播；
- Backend/Web 默认绑定回环地址。

### Phase 2：完整 `init` 与 Probe Service

- 抽取现有 config API、capabilities、Provider 和 Docker 探测逻辑；
- 建立统一 probe result Schema；
- 实现可恢复、原子提交的交互向导；
- 实现 answer file 和非交互模式；
- 根据扩展选择构建向导和 probe plan，禁用扩展不产生专属探测；
- 覆盖本文第 8 节的全部用户设置分组。

### Phase 3：npm Release Runtime

- 将 npm CLI 包从 Worker 专用入口升级为产品 CLI；
- 发布 Backend wheel/release bundle 和 Web standalone；
- 加入 manifest、签名、校验、原子升级和回滚；
- 保留现有 Headless 命令兼容别名。

### Phase 4：Web-first 与 Electron 薄壳

- 普通浏览器路径补齐目录选择降级；
- CLI 首期直接打开系统浏览器，不修改当前 Electron 客户端；
- Electron 连接 CLI Runtime 作为未来可选工作，单独立项；
- 在干净的 macOS/Linux/Windows 机器上验证无 Electron 部署流程。

## 13. 验收标准

1. 全新机器执行 `npm install -g @puddingai/puddingclaw && puddingclaw init`；若缺少 Python，用户确认一次即可准备用户级 Python/uv 环境、完成 Core 配置并启动 GUI。
2. `init` 先选择能力组合；Harness-only 不询问数据库、知识库、RAG 和智能问数配置，选择扩展后才逐组覆盖对应设置。
3. Core 和用户已选扩展所涉及的 PostgreSQL、pgvector、Provider、模型、目录、Docker、Milvus、MinerU、gbrain 和 MCP 都有明确探测结果与修复建议；未选扩展不探测。
4. 可选能力不可用不会阻止 Core；系统状态明确显示 degraded，而不是伪装 available。
5. 端口被未知进程占用时绝不杀进程；交互用户可以自动换端口、手动指定或取消。只有通过 Home、实例令牌和 launcher 挑战应答认证的本 CLI 实例，才能由 `stop/restart` 终止。
6. 非交互模式在没有明确参数时绝不终止进程、创建资源或自动批准权限。
7. 自动端口可被 Backend、Web 和 `open/status/logs/stop` 正确发现。
8. npm 包目录、源码目录和应用包在生产运行时保持只读；所有状态进入用户目录。
9. CLI、Backend、Web 版本不兼容时启动失败并给出升级/回滚路径。
10. GUI 设置、`init` 和 `doctor` 对同一配置和探测结果保持一致。
11. 密钥不进入 `config.json`、`runtime.json`、init 草稿、argv、日志和诊断输出。
12. 如果统一 CLI 复用现有 Headless Worker 命令，其 JSON、HITL、退出码和 Session 行为保持兼容。
13. Harness-only 可以在没有 PostgreSQL、Milvus、MinerU、gbrain 和 Vanna 的环境中启动并完成一次 Agent Run。
14. Harness-only 最终发送给模型的 Tool Schema 不包含任何 Knowledge 或 Analytics Tool，相关后台 Worker 也没有启动。
15. 禁用扩展在系统状态中显示 `disabled`，不会被误报为 `degraded`；启用但探测失败时才显示降级或不可用。

## 14. 待落实的 CLI 与发行适配

部署 CLI 优先需要实现：

- 独立的 npm 产品 CLI 包和命令框架；
- CLI Home、Runtime manifest、版本目录和进程所有权；
- Python/uv 检测与一键用户级环境准备；
- Backend wheel/release bundle 和 Next.js standalone 发行产物；
- `start/status/logs/open/stop/restart` Runtime Supervisor；
- Init Step Registry、Extension Profile、answer file 和原子提交；
- Backend Runtime 对 CLI 注入的 `PUDDINGCLAW_HOME`、动态端口和 `extensions.*` 的支持；
- capability、设置单项 test、Provider、Docker、gbrain/MCP 共用的 Probe Service Schema；
- 干净 macOS/Linux/Windows 机器上的安装、升级、回滚和卸载验证。

当前源码/Electron 启动方式保持不变。部署 CLI 通过独立 Home、环境变量和发行产物启动自己的 Runtime，不包裹、不接管当前 Electron Manager，也不操作开发机正在运行的实例。
