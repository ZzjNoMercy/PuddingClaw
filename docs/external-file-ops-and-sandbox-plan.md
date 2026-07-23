# 目录优先的外部文件操作与自适应 Sandbox 优化方案

> 状态：待审核(v3,已确立“目录授权 + 标准 shell”主路径、HostFileBroker 内部化、Kernel 默认 + Docker 可选/按需/强制)
> 范围：外部目录写路径(cp/mv/mkdir/覆盖/批量生成)、精确文件兼容路径、数据→文件通道、统一 Grant Profile、执行分层(Kernel sandbox 默认 + Docker 按需或强制)
> 依据:session-7b019aad45e1 / session-e851ed9c80c9 / session-9ea2a3e43160 的完整执行证据链
> 关联文档:`docs/agent-control-plane-repair-plan.md`(本方案可作为其 §17 后续章节并入)

## 0. V3 审核结论

本方案采用以下产品与架构原则:

1. **目录授权是主要工作模式**:用户授权一个目录后,agent 直接使用标准 `cp`/`mv`/`mkdir`/`sed`/构建/测试命令,不再为每种文件动作暴露一个模型工具。
2. **标准 shell 是默认文件工程界面**:workspace、scratch 和已授权外部目录由同一份 Sandbox Grant Profile 约束;安全性由 Tool Gate + OS sandbox 共同保证。
3. **HostFileBroker 内部化**:保留 exact-file grant、原子 replace、SHA 冲突、receipt、回滚和 symlink/TOCTOU 防护;模型只保留一个高层 `patch_file` 精确编辑入口,不再暴露 `copy_file`/`replace_file`/`delete_file` 等底层文件动作。
4. **Kernel sandbox 是默认执行层**:普通离线计算、文件操作、构建和测试不启动 Docker。
5. **Docker 是可配置升级层**:用户可以禁用、允许按需升级,或强制所有命令使用 Docker;联网、装包、浏览器、重任务和不可信代码优先升级 Docker。
6. **Grant Profile 只有一套**:授权语义不绑定具体 sandbox 后端;Kernel 与 Docker 只是同一份 profile 的不同执行投影,切换 runner 不应使授权失效。

核心取舍:**用更粗但更自然的目录授权换取标准 shell 与更小工具面;需要精确文件权限或事务保证时,由服务端内部回落 HostFileBroker。**

## 1. 背景与问题清单

V2 报告刷新任务(创建 V2 = 复制模板 + 重灌数据)暴露的核心矛盾:**Harness 把"写"拆成大量模型原语 + 只读 shell,常规文件工程操作(cp、覆盖、批量生成)缺少一条自然、统一的合法路径,agent 只能在工具选择和禁令缝隙里反复编排甚至发明危险绕行。**

实测证据:

| # | 现场 | 证据 |
|---|---|---|
| E1 | `cp 产品配置分析_2026.html 产品配置分析_2026_v2.html` 被拦 | `execute_external_directory` 只读挂载:`cp: cannot create regular file ...: Read-only file system`(session-7b019aad45e1 msg11 #41) |
| E2 | `write_file` 对已有文件拒绝 → agent 用 **delete_file + write_file** 绕过(删用户真实文件,delete 后 write 失败即数据丢失) | 同 session msg11 #29→#31 |
| E3 | 337 行查询结果只能分页进入模型上下文再转抄 → agent 写出 `bevHeatmapRaw: __BEV_HEATMAP_ARRAY__` 占位符,被 commit 校验拦截;第二次内联 337 行字面量再被拦 | 同 session msg11 #9/#15 |
| E4 | 子代理死法一:委托合同模型调用上限 `run limit (12/12)`,4 分钟白跑,0 todo 完成 | 同 session msg11 #2 |
| E5 | 子代理死法二:子代理内触发 HITL,`GraphInterrupt: permission_request` 直接打死——子代理没有服务用户授权的能力 | 同 session msg11 #43 |
| E6 | 版本/sha 舞蹈:`patch_file` / `upsert_scratch_file` 冲突 → inspect → 重试,循环多次 | 同 session msg9/msg11 多处 |
| E7 | 工作区影子副本:agent patch `/Users/pet/puddingclaw` 下带旧语法错误的过期副本再 cp 回 lease,带病进入交付 | session-9ea2a3e43160 msg17;工作区现存 6 个 `product-config-charts*.js` 变体 |

## 2. 外部参照:Grok Build 与 Codex

源码:本地 `/Users/pet/Code/AI/Agent/源码合集/grok-build`。

- **编辑用专用工具,通用文件操作走 Bash**:`grok_build` 工具命名空间只有 bash/grep/list_dir/read_file/search_replace 等,**没有 copy_file,连通用 write 都没有**。编辑需要精确锚定所以是工具;`cp`/`mv` 就是普通 shell 命令。
- **敢放开 shell 的两层底座**:
  1. 内核级 sandbox(`xai-grok-sandbox`,nono 0.53.0,Linux Landlock / macOS Seatbelt):进程启动时 apply 一次,"全盘可读、仅 workspace + 少量路径可写",子进程继承;子进程网络用 seccomp 单独掐。
  2. 权限快路径(`permission/auto_mode.rs` + `prompter.rs`):确定性规则覆盖高频命令,灰区给 LLM 分类器,授权分 once / session(仅内存)/ always(按 cwd 持久化)三档。
- **Codex**:workspace 边界 + approval reviewer;workspace 内写默认放行,越界审批后 `apply_patch` 直接落盘。无 run 级验收。
- **两者都不依赖 Docker**——基本命令(不装包、不联网的计算/构建/测试/文件操作)直接本地跑,内核 sandbox 兜底。
- **V3 结论**:自由度必须用边界确定性换。默认采用"目录 Grant + 标准 shell + 内核钉边界";精确文件原语降为服务端内部兼容层,不再作为主要模型交互面。

## 3. 优化方案 A:目录授权 + 标准 shell 主路径

### A1 用户交互:目录授权优先

用户侧默认只展示一个自然权限动作:

> 允许读取和修改 `/path/reports`

内部仍拆成独立 Grant,保证最小权限与审计:

| 命令 | 最小目录 Grant |
|---|---|
| 同目录 `cp a b` | 目录 read + write |
| 跨目录 `cp A/a B/b` | A read + B write |
| `mkdir A/new` | A write |
| 同目录 `mv a b` | 目录 read + write + delete |
| 跨目录 `mv A/a B/b` | A read + delete + B write |
| `cp -r A B` | A recursive read + B recursive write;受数量/大小上限约束 |

规则:

- `external_directory_write` 不隐式扩大为 read;UI 的“允许编辑目录”一次批准后由服务端原子创建配对的 read/write Grant。
- shell 只消费目录 Grant;exact-file Grant 不投影给任意 shell。
- 目录授权卡一次展示 source/destination、读写范围、是否含 delete,避免 `cp` 先问 source、再问 target 的两次 HITL。
- `/skills`、`/knowledge`、`/semantic-assets` 等系统资源按激活状态只读投影,永不进入 writable。

### A2 模型工具面收敛

默认模型文件工具面收敛为:

```
execute / read_file / patch_file
```

- `execute`:复制、移动、建目录、批量生成、构建、测试等普通文件工程操作。
- `read_file`:高效读取并返回内容版本(SHA),供精确编辑使用。
- `patch_file`:唯一保留的专用编辑原语,负责锚点替换、expected SHA、一次机械 rebase、原子提交和 receipt;作用类似 Grok 的 `search_replace`。

默认不再暴露:

```
copy_file / replace_file / patch_files / delete_file
inspect_file_version
stage_external_artifact / commit_external_artifact
```

`patch_file` 内部读取/核对当前版本;`read_file` 返回的版本可作为乐观锁,不再要求模型执行独立的 `inspect_file_version → patch_file` 编排。其余原语保留为服务端内部 capability,供精确文件授权、附件发布、事务提交、恢复和兼容旧 Run 使用。

迁移期提供:

```yaml
harness:
  file_operations:
    model_tool_exposure: internal   # internal | legacy
```

- `internal` 为新默认:仅 `patch_file` 作为高层精确编辑入口,底层文件动作不进入模型 manifest/prompt。
- `legacy` 仅用于回滚和兼容测试,后续移除。

### A3 精确文件兼容路径:HostFileBroker 内部化

当只有 exact-file Grant、没有目录 Grant 时,通用 shell 不能安全获得父目录创建权限。服务端按以下顺序处理:

1. 默认请求升级为最窄的直接父目录授权;
2. 用户不愿扩大授权、但已有精确 source-read + target-write 时,由内部 `FileOperationRouter → HostFileBroker` 完成精确复制/覆盖;
3. 两者都不满足则 fail-closed。

HostFileBroker 继续负责:

- exact-file read/write/delete;
- 原子 create/replace、expected SHA、并发冲突;
- symlink/TOCTOU 防护;
- mutation/validation receipt、事务和 rewind。

它与 sandbox runner 解耦:Grant 是共同事实来源;HostFileBroker 是精确文件 I/O 实现,Kernel/Docker 是通用命令执行实现。

### A4 直接目录写的风险分级

- 新建文件、普通 `cp`、`mkdir`:目录 Grant + Kernel sandbox 后直接执行,记录 command receipt 与触达目录。
- 覆盖、delete、`mv`、递归/批量写:Tool Gate 至少 ASK;批准卡展示命令、目录、delete 能力和影响预览。
- 正式 artifact、需要 SHA/验证/回滚的交付:服务端可透明选择“目录草稿 → shell 执行 → diff → 原子 commit”;模型仍只看到标准命令,不承担 lease/hash 编排。
- 工作区影子副本不作为权威交付源;交付验收只认可正式 target 或服务端事务 receipt。历史影子文件清理另立迁移任务,不在本方案中自动删除用户文件。

## 4. 优化方案 B:数据 → 文件内部通道

根治 E3(337 行穿上下文、占位符)。

- **B1 SourceReference → materialize capability**:查询结果直接落 scratch/workspace/已授权目录,不进模型上下文。
- **B2 模板填充合法化**:服务端通过 typed slot 注入数据,与 commit 时语法校验兼容;裸占位符(无对应 fill 意图)继续拦截。
- 数据 materialize 是“数据不穿模型上下文”的语义能力,不等同于普通文件操作工具;可以保留一个高层入口,底层写入仍由 HostFileBroker 或目录事务完成。

## 5. 优化方案 C:Skill 模板走标准目录模型

- Skill frontmatter 增加 `templates:` 声明;Skill 激活后其 `templates/` 自动进入只读 profile。
- 纯复制时 agent 直接 `cp /skills/... /authorized-target/...`。
- 需要灌数据时走 B 的 materialize/typed-slot 内部 capability。
- 不再新增 `instantiate_skill_template` 等模型工具;模板声明只负责可发现性、只读授权和 slot schema。

## 6. 优化方案 D:子代理修两种死法

- **D1 HITL 上抛**(治 E5):子代理内 permission_request 不得 `GraphInterrupt` 打死;挂起并路由到父 Run 的用户授权队列,授权后恢复。备选:委托时继承父 Run 的 permission context(DelegationContract 扩展)。
- **D2 限制按任务类型配置**(治 E4):数据嵌入/批量查询类委托的模型调用上限放宽(12 → 按类型 20-30);超时/失败后主 agent 接管,禁止原样重派。

## 7. 优化方案 E:自适应执行——Kernel 默认,Docker 可选/按需/强制

### 7.1 产品配置与路由语义

前端入口放在现有 **设置 → Harness → 终端与沙箱**。当前“启用 Docker 项目沙箱”开关替换为三选一:

| 前端选项 | 配置值 | 用户语义 |
|---|---|---|
| 自动选择(推荐) | `auto` | 优先 Kernel;命令确实需要 Docker 时才懒启动 Docker |
| 仅内核沙箱 | `kernel` | 永不启动 Docker;超出 Kernel 能力时拒绝并说明原因 |
| 强制 Docker 沙箱 | `docker` | 所有 shell 命令都在 Docker 中执行 |

前端说明文案:

- **自动选择(推荐)**:“普通文件操作、构建和测试使用轻量内核沙箱;联网、装包、浏览器、重任务或内核沙箱不可用时按需使用 Docker。”
- **仅内核沙箱**:“完全不依赖 Docker。需要联网、装包或更强隔离的命令将被拒绝。”
- **强制 Docker 沙箱**:“所有命令使用 Docker 项目沙箱,启动更重但隔离和运行环境最稳定。”

`auto` 是 **Kernel-first adaptive mode**,不是无沙箱的 host fallback:

```
Kernel 自检通过 + 普通离线命令       → Kernel
Kernel 自检通过 + Docker-only 能力   → 经过既有权限判定后懒启动 Docker
Kernel 自检失败 + Docker 可用         → Docker
Kernel 与 Docker 都不可用             → fail-closed
```

切换 Docker 不单独制造第二张授权卡。命令本身若涉及 raw network、装包、delete 等风险,继续使用现有 HITL/Session Grant;授权通过后 runner 才执行。已经存在兼容 Grant 时可直接路由。

```yaml
harness:
  terminal:
    sandbox_mode: auto       # auto | kernel | docker；由前端三选一写入
    docker:
      # connection/context/image/resources 等现有 Docker 高级配置继续保留
```

- `auto`(默认):普通离线命令走 Kernel;需要 Docker capability 时按已有权限策略升级。
- `kernel`:强制 Kernel runner;命令需要装包、原始联网、浏览器或强隔离时 ASK/DENY,不静默启动 Docker。
- `docker`:用户强制所有 shell 命令走 Docker,保持当前高隔离模式。
- Docker 必须懒启动:`auto` Run 的普通命令不能在 Run 创建阶段 probe/build/start container。

前端行为:

- 选择 `auto` 或 `docker` 时显示折叠的 Docker 高级设置与“探测 Docker”按钮。
- 选择 `kernel` 时隐藏/禁用 Docker 高级设置,但不删除用户已有 Docker 配置。
- `auto` 显示 Kernel/Docker 自检状态和最近一次实际 runner;`docker` 在 Docker 不可用时保存前告警、执行时 fail-closed。
- Run 创建后冻结 `sandbox_mode`;设置变更只影响新 Run,避免执行中途改变安全边界。

默认路由:

| Capability | `auto` runner |
|---|---|
| workspace/scratch/授权目录内 `cp`/`mv`/`mkdir` | Kernel |
| 离线计算、构建、lint、测试 | Kernel |
| 受控 `fetch_url`/搜索工具 | 主进程类型化网络工具,不进入 shell runner |
| shell 原始联网 | Docker;ASK 或已有 raw-network Grant |
| pip/npm/apt 等装包 | Docker |
| 浏览器、重任务、不可信代码 | Docker |
| Docker 不可用且 Kernel 不足 | DENY,说明所缺 capability |

路由依据是显式 capability + 确定性分类;分类器漏判只允许导致 Kernel 中执行失败,不能导致越权。LLM reviewer 可解释灰区,但不是安全边界。

### 7.2 一套 Runner-neutral Grant Profile

`SandboxGrantProfile` 是现有 Grant 体系的单一执行投影,不属于 Kernel 或 Docker:

```
RunPermissionContext
  + 当前有效 Permission Grants
  + 系统受管资源声明
  → SandboxGrantProfile
      read_roots:
        workspace、scratch
        激活的 /skills
        必要的只读运行时/系统依赖
        external_directory_read targets
      write_roots:
        workspace、scratch
        external_directory_write targets
      delete_roots:
        显式带 delete capability 的 directory targets
      exact_file_grants:
        不进入 shell profile;仅供 HostFileBroker
      network:
        deny by default;typed network 与 raw network 分离
      limits:
        timeout、max_output、pids、memory、cpu、file-size
      env/fd:
        最小环境、关闭非标准继承 FD
```

关键约束:

- **default deny read + deny write**,不是“宿主其余路径全部只读”;未授权敏感文件不能通过 shell 进入模型输出。
- exact-file target 尤其是不存在的新文件,不能安全投影给 Landlock 通用 shell,因为创建权限落在父目录;继续由 HostFileBroker 处理。
- `external_directory_write` 不自动授予 read/delete;用户侧组合授权由服务端拆成多条明确 Grant。
- profile 按每次 execute 即时生成,HITL 新 Grant 下一次执行自然生效。
- Tool Gate、prompt manifest、HostFileBroker、Kernel 和 Docker 共用同一个 `EffectiveGrantResolver`;禁止各自筛选。
- runner 切换不改变授权语义。现有 grant binding 中的具体 `backend_mode/backend_id` 应迁移为稳定的 `execution_boundary_id`(workspace + policy epoch/version + profile schema),具体 runner/version仅进入 execution receipt。

### 7.3 KernelSandboxRunner 不是单一 Landlock/Seatbelt 包装

Kernel 默认层由以下组合构成:

| 控制面 | Linux | macOS |
|---|---|---|
| 文件边界 | Landlock ABI 自检 + ruleset | Seatbelt profile |
| syscall 缺口 | seccomp deny chmod/chown/xattr/utime/mount/ptrace 等 | Seatbelt规则 + 命令策略 |
| 网络 | 默认 deny socket/connect;raw network 不在首期 Kernel 放行 | profile 默认 deny network |
| 资源 | cgroup v2 优先;不可用时 prlimit + supervisor | setrlimit + supervisor |
| 进程生命周期 | 新进程组,超时杀全组,关闭继承 FD | 同左 |
| 环境 | scrub env,独立 HOME/TMPDIR | 同左 |

原因:

- Landlock 能约束文件层级,但不能单独覆盖所有元数据 syscall,必须与 seccomp/命令策略组合。
- seccomp 是补充而不是完整 sandbox。
- 资源限制不能只写“ulimit 即可”;cgroup 不可用时必须明确降级保证和残余风险。
- helper 在 exec 前应用规则并 fail-closed;检测不到所需 ABI/策略能力时不得裸跑。

生命周期采用**按命令创建、按进程继承、命令结束即销毁**,不创建“每项目一个 Kernel 沙箱实例”:

```
每次 execute
  → 读取当前项目 workspace + Run Grants
  → 生成/复用 SandboxGrantProfile
  → 启动一个受限 helper/sandbox-exec 进程
  → 子进程继承限制
  → 超时或命令结束后清理进程组/cgroup
```

项目级只保留轻量上下文,不是沙箱实例:

- `workspace_id/execution_boundary_id`;
- workspace、Goal-scoped scratch、独立 HOME/TMPDIR;
- 静态 profile 模板和运行时依赖探测缓存;
- 项目命令锁/并发策略。

动态目录 Grant 必须在每次 execute 时重新投影;不得因为缓存 profile 而漏掉撤销、policy epoch 或新 HITL。可以缓存静态系统规则,最终 profile digest 必须包含当前有效 Grants。

Docker 生命周期保持不同:只有 Docker runner 可以按项目懒创建/复用容器;`auto` 模式下未触发 Docker capability 的项目不会产生容器。

实现建议:

```
AdaptiveExecutionBackend
  ├─ KernelSandboxRunner
  │    ├─ LinuxLandlockHelper (小型静态 helper binary)
  │    └─ MacOSSandboxExecAdapter
  └─ LazyDockerRunner
```

DeepAgents 的 `execute(command, timeout)` 协议可以保持,但 backend 构造必须注入 Run-scoped `profile_provider`;不能只在现有 `workspace_backends.py` 的出口包一层而不改 Tool Gate 和 Run 绑定。

### 7.4 DockerRunner 消费同一份 Profile

Docker 不是另一套授权系统:

- `read_roots` → readonly bind mounts;
- `write_roots/delete_roots` → writable bind mounts;
- 未声明路径不 mount;
- network 默认 `none`,raw-network Grant 下使用临时联网容器;
- 保留 read-only rootfs、cap-drop、no-new-privileges、PID/memory/CPU limit。

exact-file Grant 仍不直接变成父目录 mount;由 HostFileBroker 在容器外执行精确事务。

### 7.5 外部目录命令的执行方式

目录已授权后,模型统一发标准命令;不因 runner 不同改变工具调用:

```
execute("cp /authorized/reports/report.html /authorized/reports/report-v2.html")
```

执行链:

1. Tool Gate 解析命令能力和 cwd/source/target;
2. `EffectiveGrantResolver` 验证目录 read/write/delete;
3. 生成同一份 `SandboxGrantProfile`;
4. Adaptive backend 按配置选择 Kernel 或 Docker;
5. runner 在 OS 层执行 profile;
6. 记录 command receipt(命令指纹、runner、profile digest、退出码、触达目录、输出摘要);
7. 正式 artifact 或高风险批量写可由服务端透明进入草稿/事务提交。

当前必须同步解除:

- restricted-host 对所有 workspace 外绝对路径的一刀切 DENY;
- `execute_external_directory` 的 `requires_docker`;
- smart 普通写快路径的 `backend_mode == docker`;
- Run grant binding 对具体 backend runner 的耦合。

### 7.6 平台矩阵与降级

| 宿主 | Kernel 默认层 | Docker 升级层 |
|---|---|---|
| macOS | Seatbelt adapter;启动自检;deprecated 风险显式记录 | Docker Desktop |
| Linux | Landlock + seccomp + cgroup/prlimit | Docker |
| Windows | 首期无等价宿主路径 sandbox;`auto` 实际选择 Docker | Docker Desktop/WSL2 |

- macOS `sandbox-exec` 当前仍可用但已标记 deprecated,必须有启动自检、版本化 profile、CI 覆盖和 Docker fallback。
- Windows 不能宣称“一次 Kernel 开发三平台生效”;没有 Docker/WSL 时应 fail-closed,不可退回裸宿主 shell。
- 多租户/服务端部署可由部署策略强制 `sandbox_mode:docker`,忽略用户 `auto/kernel` 降级请求。

## 8. 已在轨/已完成的配套(备查)

- 目录授权 session 化(`api/permissions.py` effective_scope + `has_external_directory_permission` session 分支)——已修;
- `all_external_files` 通配读在 HostFileBroker 生效(仅精确文件,不放开目录发现)——已修;
- HostFileBroker 已具备 copy/replace/transaction/receipt/TOCTOU 防护——保留并内部化,不重复实现;
- `patch_file` 一次机械 rebase 已有实现和测试——保留为唯一模型可见的专用编辑原语;SourceReference materialize、目录 writable draft 转为内部语义/事务能力;
- 当前 `RestrictedHostWorkspaceBackend` 已 scrub env、独立 HOME/TMPDIR、timeout——Kernel runner 在此基础上补 OS profile、资源与进程组保证;
- 缓存优化方向已定:manifest 剥 run_id、semantic assets 白名单投影(~1.5K tokens/调用)——待实施;
- 追问验收分级(informational 轻合同、goal_inheritable 证据继承)——见 `agent-control-plane-repair-plan.md` §16。

## 9. 实施顺序

| 批次 | 内容 | 解决 |
|---|---|---|
| P0-1 | 抽取 `EffectiveGrantResolver` + runner-neutral `SandboxGrantProfile`;引入 `execution_boundary_id` | Grant 单一事实来源、runner 切换不失效 |
| P0-2 | 模型文件 Toolset 收敛为 `execute/read_file/patch_file`;其余切 `internal`;保留 `legacy` 回滚开关;目录读写组合授权卡 | 工具面收敛、一次 HITL |
| P1-1 | macOS Kernel runner(Seatbelt + env/fd/process/limit + 启动自检) | 当前主平台普通命令不依赖 Docker |
| P1-2 | Linux Kernel runner(Landlock helper + seccomp + cgroup/prlimit) | Linux 离线执行本地化 |
| P1-3 | 解除 external-directory/smart-write 的 Docker 硬编码;目录 Grant 投影到 Kernel profile | `cp`/`mv`/`mkdir` 标准命令主路径 |
| P2-1 | `AdaptiveExecutionBackend` + LazyDockerRunner + 前端 Harness `auto/kernel/docker` 三选一 | Docker 可选、按需或强制 |
| P2-2 | HostFileBroker 内部 `FileOperationRouter`;正式 artifact 透明草稿/事务 | exact-file 与高风险交付保留强保证 |
| P2-3 | B/C 数据与模板通道收口;删除旧模型工具文案和过渡入口 | 不穿上下文、最终工具面稳定 |
| 并行 | D1 子代理 HITL 恢复 + D2 任务型限制 | E4/E5,不阻塞 Kernel 主线 |

## 10. 迁移与兼容

1. 新 Run 默认 `sandbox_mode:auto` + `model_tool_exposure:internal`;旧 Run 按冻结 snapshot 继续使用 legacy Toolset。
2. Permission binding schema 升级;旧 grant 可在同 workspace/policy 下投影为新的 `execution_boundary_id`,无法证明等价时重新授权,不静默扩大。
3. `backend_mode=restricted_host/docker` 的历史字段保留只读审计;新 Run 使用 `adaptive` + receipt 中的实际 runner。
4. 旧前端/配置迁移:
   - `docker_enabled:true` → `sandbox_mode:docker`,保持用户明确选择 Docker 的意图;
   - `docker_enabled:false` → `sandbox_mode:auto`,由 Kernel 接替旧 restricted-host 默认层;
   - connection/context/image/resource limit 等 Docker 配置原样保留;
   - `on_unavailable` 废弃;`auto` 按 runner 能力降级,`docker` 不可用时始终 fail-closed。
5. Kernel 启动自检失败时:
   - `auto` 且 Docker 可用 → 降级 Docker并记录原因;
   - `kernel` 或 Docker 不可用 → fail-closed;
   - 永不退回无 OS sandbox 的裸 `subprocess.run`。
6. 模型工具隐藏前先确保提示、Skill、测试和旧 session 不再依赖显式 `copy_file/replace_file/inspect_file_version/...` 编排;`patch_file` 改为自行核对当前版本。

## 11. 验收标准

### 11.1 权限与命令矩阵

- 同一目录 read+write Grant 下 `cp a b` 在 Kernel 与 Docker 均成功。
- A read + B write 下跨目录 `cp` 成功;缺任一 Grant 均在执行前 ASK/DENY。
- `mkdir` 仅在父目录 write 下成功。
- `mv` 缺 source delete 或 destination write 时失败。
- exact-file Grant 不进入 shell profile;无目录 Grant 的 `cp` 走目录升级授权或内部 Broker。
- `/skills` 可读不可写;未授权 Home/credential 路径不可读、不可写。
- Linux 下 sandbox 外 `chmod/chown/xattr/utime` 被补充策略阻断。
- Kernel 默认无法建立原始网络连接,即使 capability classifier 漏判。

### 11.2 后端等价性

- 同一 `SandboxGrantProfile` 在 Kernel/Docker 上生成相同 allow/deny 矩阵。
- `auto` 普通命令不 probe/build/start Docker。
- 需要 Docker 的 capability 在 `auto/kernel/docker` 下路由确定,风险授权仍遵循既有 approval mode 与 Session Grant。
- `sandbox_mode:docker` 强制使用 Docker;`sandbox_mode:kernel` 永不静默升级。
- runner 切换后 Session Grant 继续有效,receipt 记录 runner/version/profile digest。
- 前端三选一与后端配置双向一致;切换模式不清除 Docker 高级配置,且只影响新 Run。
- `auto` 状态区能区分“Kernel 正常”“按需 Docker”“Kernel 失败后 Docker 降级”“无可用安全 runner”。

### 11.3 工具面与交付

- 默认 manifest/prompt 只保留 `execute/read_file/patch_file`,不出现 `copy_file/replace_file/patch_files/delete_file/inspect_file_version/commit_external_artifact`。
- legacy 模式仍可跑现有兼容测试。
- 标准 shell 创建的普通文件产生 command receipt。
- 正式 artifact/透明事务产生 mutation + validation receipt,并保留并发冲突与 rollback。
- 数据 materialize 全量结果不穿模型上下文。

### 11.4 性能

- Kernel runner 首次/热执行开销分别设预算并纳入基准;热执行不承担 Docker round-trip。
- `auto` 普通 Run 的 Docker 调用次数为 0。
- profile 生成与权限筛选有稳定上限,目录 Grant 数量异常时 fail-closed 或截断请求,不静默丢规则。

## 12. 已决策与剩余决策

### 已决策

1. 目录授权是主要模式,标准 shell 是主要文件工程界面。
2. 模型文件工具收敛为 `execute/read_file/patch_file`;`copy_file/replace_file/delete_file/...` 不再默认暴露,HostFileBroker 保留为内部 exact-file/事务底座。
3. Kernel sandbox 默认,Docker 可选、按需或用户/部署强制。
4. Kernel 与 Docker 使用同一套 Grant Profile。
5. shell profile 只接受目录 Grant;exact-file Grant 不提升为父目录 shell 权限。
6. default deny read/write;不采用“全盘可读、只限制写”的 profile。

### 剩余决策

1. macOS Seatbelt 首期支持的系统只读路径最小集合及兼容测试矩阵。
2. Linux helper 采用 Rust 静态 binary 还是 C helper;不建议 ctypes 直接承载安全边界。
3. raw-network 是否首期一律 Docker,还是后续允许 Kernel 特化;本方案默认一律 Docker。
4. 普通目录覆盖是否只记录 command receipt,还是统一透明草稿;建议普通写直达、高风险/正式 artifact 事务化。
5. 本方案保持独立文档还是并入 `agent-control-plane-repair-plan.md`;实施前保持独立,稳定后并入架构主文档。
