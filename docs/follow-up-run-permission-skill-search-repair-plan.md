# Goal 完成后追问：权限、Skill 连续性与外部搜索修复方案

> 状态：§10.5–§10.11 已实施并完成 24 项代码级验收；Stage/lease 源码按双发布周期退出门禁保留兼容
> 日期：2026-07-22
> 范围：Goal/Run 生命周期、外部目录授权、Skill Capability Manifest、外部路径 `grep/glob/ls`、临时产物与验收契约
> 原则：事故证据与设计决策保留为审计基线；实施结果以本节的代码、测试和最终 hash 约束为准。

## 0. 实施与复盘映射

本轮实现直接采用了用户提供的“36 次工具调用 / 12 分钟”复盘，不把它当作背景材料。复盘中的四类额外开销和一项安全失败，分别落到了下列控制面：

| 复盘事实 | 第一性原理约束 | 已实施机制 |
| --- | --- | --- |
| 为找文件反复读取旧 lease、`glob/grep` 14 次 | 正式位置只能有一个权威来源 | Durable Artifact Registry 持久化 target path/hash；追问只解析新鲜 active artifact；旧 scratch 结构化恢复到最新正式 hash |
| 新 Run 重新争取数据库能力，子代理绕行约 5 分钟 | 能力激活与能力推荐分离；连续性不能靠分类器运气 | active Goal 同 revision 继承 activation；独立追问只产生 `recommended_inactive_skills`；读取具体 `SKILL.md` 后才开放工具 |
| workspace 影子副本 → 目录 stage → copy → commit | 一个目标只能有一个可写 draft | exact-file/directory draft claim 互斥；禁止影子副本写回；外部 target 规范化；目录提交使用 no-follow dirfd 与 commit intent |
| 目录授权跨 Run 重复弹卡 | 授权事实必须与用户选择、后端匹配、UI scope 一致 | exact-directory 支持 Session scope、稳定 bindings、Grant 语义去重、兼容 pending request 自动恢复 |
| `node --check` 已失败仍提交坏 JS | 验证结论必须绑定最终 target + bytes hash | ValidationReceipt 区分 evidence 与 commit authority；代码类提交只接受受控 validator 对同一 target/hash 的成功 receipt；失败 obligation 不可被其他检查洗掉 |

实施期间的三路对抗审查又补出了原复盘未完全覆盖的边界：

1. Router 被主 Agent取消后仍会发“路由不可用”完成事件；现已保证取消只进 Trace，不污染聊天时间线。
2. Artifact Registry 原先没有删除 tombstone 和新鲜度校验；现已区分 `active/deleted/stale`，外部改写、缺失和目录删除都不会继续注入旧事实。
3. 跨文件 UI 契约的 exact-file 提交原先没有自动建立 related artifact；现从受控 contract receipt 推导双向稳定 artifact id。
4. 文件系统写回与 Registry 更新之间原先存在失败窗口；exact-file 提交失败会回滚 target，目录提交使用 `committing` journal、可重放恢复和 Registry head rollback。
5. 同一 Run/query 内重复 `grep/glob/ls` 原先仍会重复上传 snapshot；现复用不可变 search snapshot，新 Run 再刷新，兼顾一致性与性能。

关键回归包括：Goal 结束后 Todo 清空、active Goal 与独立 Run 并发投影、Session 目录授权复用、搜索 snapshot 隔离、Skill 推荐/激活、终态 scratch 恢复、产物 stale/tombstone、跨文件 contract、验证失败阻断、delta repair 预算、子代理超时回退和流式中断。

最终验证基线（2026-07-22）：

- 后端产品测试集：`1000 passed`；
- 本轮状态/路径/产物核心回归：`95 passed`；
- 前端 TypeScript：`npx tsc --noEmit` 通过；
- 子代理活动状态测试：`3 passed`；
- 所有本轮修改/新增 Python 文件 Ruff：通过；
- `git diff --check`：通过。

## 1. 摘要

本轮事故不是单一工具失败，而是六条状态链在 Goal 结束后的独立 Run 中失去一致性：

1. **展示状态未切换作用域**：旧 Goal 的 11 条已完成 Todo 仍作为 Session 当前 Todo 返回，新 Run 看起来仍在执行旧计划。
2. **授权能力没有表达用户真实选择**：目录授权被后端写死为 Run 级，每个追问 Run 都必须重新 HITL；权限侧栏又把非 `once` 的 Run Grant 标成“本 Session”。
3. **Skill 连续性只有“激活”没有“推荐”层**：确定性分类稳定漏判数据库 Skill；已完成 Goal 后的独立追问不应继承旧能力，但 Capability Manifest 也没有告诉主 Agent应读取哪个 Skill。
4. **外部搜索没有利用已有授权**：`grep/glob/ls` 遇到外部绝对路径一律拒绝，既不能在已授权精确文件上搜索，也不会把已授权目录自动映射到安全 snapshot。
5. **正式产物的位置事实没有连续**：上一 Run 已提交的 target path/hash 没有成为下一 Run 的权威入口，Agent 先读已删除 lease，再在多个影子副本间搜索和猜测。
6. **验证没有约束最终交付 hash**：失败的 `node --check` 没有阻止提交；另一轮又在提交后临时编写验证脚本，造成大量往返和一次假阳性。

此外，旧 Goal 的 scratch 已按设计删除，但 lease 状态、Handoff artifact 引用和最终回答仍暴露临时验证路径，导致新 Run 首先读取一个必然不存在的文件。随后 Agent 又错误改走宿主机 `execute` 和子代理，放大为数分钟绕行。

本方案的目标不是放宽沙箱，而是让四个事实来源对齐：

```text
用户授权范围
  = Permission Grant 的真实 scope
  = 工具执行时的匹配范围
  = UI 显示范围

当前工作状态
  = 当前 active Goal 或当前 Run 的 Todo ledger
  ≠ Session 最近一次 Todo 快照

可用能力
  = 已激活 Skill 产生的 Capability Manifest
推荐能力
  = 与当前任务相关但尚未激活的 Skill

可交付产物
  = 正式 target_path + delivery receipt + content hash
  ≠ /scratch/validation 下的临时验证文件

最终交付状态
  = 通过验证的 draft hash 原子提交后的同一 hash
  ≠ Agent 对“错误不是本次引入”的主观判断
```

## 2. 事故证据

### 2.1 Session 状态

证据文件：`backend/sessions/session-9ea2a3e43160.json`。

- 旧 Goal：`goal-de7bb8d4ca0a4fd7`，状态为 `achieved`。
- `active_goal_id = null`。
- 追问 Run：`run-43be523dd58745eb`，`goal_id = null`，属于新的独立 Run。
- 后续追问 Run：`run-9e84b951441b46ca`，同样 `goal_id = null`。
- Session 顶层 `todos` 仍是旧 Goal 的 11 条 completed Todo。
- `todo_ledgers` 只有 `goal:goal-de7bb8d4ca0a4fd7:revision:1`，没有上述独立 Run 的 Todo ledger。

因此：**新 Run 没有继承旧 Todo，错误发生在 Session 级 UI 投影，不在 Run Todo ledger 本身。**

### 2.2 重复目录授权

同一目录：

```text
/Users/pet/Code/AI/Agent/PuddingClaw/designs/product-configuration-analysis
```

Session 中先后产生多条 `external_directory_read/write` Grant。语义迁移会把旧记录标记为 `superseded`，所以权限侧栏最终只保留最近一条有效记录；但每个新 Run 仍然会创建新的 pending request 并要求用户点击授权。

这说明当前“语义去重”只收敛了**存储记录**，没有复用**跨 Run 权限能力**。

关键代码：

- `backend/api/permissions.py:147`：目录授权无条件保存为 `scope="run"`。
- `backend/graph/session_manager.py:4042-4065`：目录权限只接受 scope 为 `run` 且 metadata.run_id 等于当前 Run。
- `frontend/src/components/citations/SourcesPanel.tsx:1379-1381`：所有非 `once` Grant 都显示成“本 Session”，导致 Run Grant 被错误标注。
- `frontend/src/components/chat/ChatMessage.tsx:423-439`：pending 授权卡本身正确写着“本次 Run”；错误主要发生在授权侧栏的持久状态展示。

### 2.3 Skill 激活与分类

当前代码的 Goal Skill 继承确实存在正向候选硬门：

- `backend/graph/session_manager.py:1685` 从当前 Run 的 `task_profile.skill_candidates` 生成 `relevant`。
- `backend/graph/session_manager.py:1686-1704` 只有 `run.goal_id` 存在且候选命中时，才继承同 Goal revision 的 activation。

确定性分类复现结果：

```text
classify("纯电轴距 × 电机功率组合密度（款型数）还没有更新")
=> execution_route=native
=> skill_candidates=[]

classify("纯电轴距 × 电机功率组合密度……选择年份：2024")
=> execution_route=native
=> skill_candidates=[]
```

Capability Manifest 也只有：

```text
if a business tool is absent, read the matching /skills/<id>/SKILL.md first
```

见 `backend/graph/middlewares/toolset.py:419-423`。Manifest 没有 `recommended_inactive_skills`，模型不知道这里的 `<id>` 应为 `database-analysis`。

但外部排查结论需要修正两点：

1. 本 Session 的旧 Goal `skill_activations` 实际为空，并不存在可供追问继承的 Goal activation。因此“本次事故是 Goal activation 被候选硬门挡住”在代码层可能发生，但不是 `run-43be...` 的直接事实原因。
2. 旧 Goal 已经 `achieved`，普通用户追问创建 `goal_id = null` 的独立 Run 是正确的不可变生命周期语义。系统不应静默复活已完成 Goal。若用户要继续同一个 Goal，应显式 resume/revise；普通追问只能获得上下文和 Skill 推荐，不能直接获得旧 Goal 能力。

本次直接原因是：**独立追问没有可用的推荐 Skill 层，确定性分类又稳定漏判，导致主 Agent 开场只能看到基础工具。**

### 2.4 scratch 与产物引用

旧 Goal 最终 Run 结束后，`backend/graph/deepagents_manager.py:7097-7112` 会删除 Goal scratch；该删除符合预期。

但 Session 中仍存在状态为 `staged` 的旧 artifact lease，上一轮最终回答还把以下临时文件列为“产物”：

```text
/scratch/validation/artifact-lease-4300d65be231d7cb/validate_html.py
/scratch/validation/validate_consistency.py
```

`backend/graph/session_manager.py:1374` 又把所有 `kind == "artifact_write"` 都加入 Handoff `artifact_refs`，没有排除 `/scratch/validation`。

新 Run 因此根据旧验证目录错误推导出：

```text
.../validation/artifact-lease-4300d65be231d7cb/product-config-charts-2024.js
```

该路径从未是正式交付路径。正式文件一直位于：

```text
designs/product-configuration-analysis/product-config-charts-2024.js
```

### 2.5 `grep` 外部路径行为

`backend/graph/middlewares/workspace_path_router.py:150-162` 对外部绝对路径执行以下统一规则：

- `read_file` 可重路由到精确 `read_resource`；
- `grep/glob/ls` 一律返回错误；
- 不检查路径究竟是已授权精确文件还是已授权精确目录；
- 不查询当前 Session/Run 是否已有可复用 directory lease；
- 不把源目录映射到安全的 `/scratch/external-directories/<lease_id>`。

当前策略的安全边界是对的：**精确文件授权不能隐式扩大为父目录授权。** 问题在于执行层没有区分两类合法搜索：

1. 在一个已经精确授权的文件内 grep；
2. 在一个已经明确授权的目录 snapshot 内 grep。

### 2.6 热力图展示契约漏验

正式 JS 已包含 2025、2026 矩阵，并设置 `currentHeatYear = "2026"`：

- `designs/product-configuration-analysis/product-config-charts-2024.js:296-300`

修复前，正式 HTML 的 `<select>` 只有 2021–2024，且 `selected=2024`：

- `designs/product-configuration-analysis/产品配置分析_2026.html:502-506`

结果是页面初始图表使用 2026 数据，但控件显示 2024，且用户无法选择 2025/2026。上一轮验收只证明“JS 包含这些年份”，没有验证跨文件 UI/data contract。

`run-9e84b951441b46ca` 已把 HTML 选项补齐到 2026，并将默认值改为 2026；本节记录的是导致追问的历史漏验，不能因为当前文件已修复而删除该回归用例。

### 2.7 配对 Run 实证：数据刷新被系统摩擦放大

Run：`run-43be523dd58745eb`，Query：`query-702a4e0143e7`。

用户意图是补齐“纯电轴距 × 电机功率组合密度”的年度数据。Trace 显示：

- 总时长约 732 秒；
- 主 Agent 共 36 次工具调用；
- 其中 1 次 `task` 子代理调用耗时约 243 秒；
- 前 14 次调用都在恢复文件位置和定位图表代码；
- 实际数据库结果获取只用了 `database_sql_execute` 加两次分页；
- 从 patch 影子副本到正式目录 commit 又耗时约 164 秒。

实际动作链为：

```text
读取已删除的旧 scratch/lease 路径，失败
  -> glob/grep/read_file 反复搜索多个副本
  -> execute 直连数据库被 container_path_expansion 拦截
  -> 委托子代理恢复数据库能力并生成 SQL
  -> 主 Agent 执行 SQL 和分页
  -> patch /workspace 影子 JS
  -> node --check 明确失败
  -> 仍然 stage 正式外部目录
  -> 把有语法错误的影子 JS cp 到目录 lease
  -> prepare + commit 正式目录
```

交叉验证后的结论：

1. **位置连续性缺失成立。** 第一次读取就是已删除的 Goal scratch 路径，正式 artifact registry 中已有的 target path/hash 没成为当前 Run 的权威文件入口。
2. **能力恢复成本成立，但不能简单归因于“旧 Goal Skill 没继承”。** 该 Run 的 `goal_id = null`，旧 Goal 已完成；正确做法不是静默复活旧 Goal，而是注入 durable artifact facts 和 Skill 推荐，由主 Agent读取 SKILL.md 后恢复数据库工具。
3. **双副本问题成立。** Agent 修改 `/workspace/product-config-charts-2024.js`，再把它复制到 external directory lease；系统没有保证 draft 只有一个权威副本。
4. **验证放行错误成立，而且是 P0。** `node --check /workspace/product-config-charts-2024.js` 返回 FAIL，但 Agent以“不是本次引入”为由继续提交。随后独立 Run `run-a09ec6aace4b4114` 才修复 `priceBox.values` 的残留数组并重新通过 `node --check`。这证明上一 Run 确实交付过不可解析的 JS。

“不是本次引入”只能改变归责，不能改变交付状态。对会被浏览器直接加载的完整 JS 文件，只要最终 hash 不能解析，就必须选择：修复、回滚本次修改或明确停止交付，不能判定成功。

### 2.8 配对 Run 实证：6 行 HTML 修复仍被放大

Run：`run-9e84b951441b46ca`，Query：`query-eec5e2b29374`。

这轮不需要数据库重算。JS 已有 2021–2026 数据，唯一业务改动是 HTML：新增 2025/2026 两个 `<option>`，把 `selected` 从 2024 改为 2026。

Trace 显示：

- 总时长约 262 秒；
- 19 次工具调用；
- 约 24 次模型轮次；
- 无子代理；
- 产生 1 次新的 run-scope 目录读取 Grant；
- 验收迭代 2 轮后通过。

调用可分成三段：

| 阶段 | 调用数 | 实际情况 |
|---|---:|---|
| 定位差异 | 7 | 读 JS、两次外部目录 grep 失败、再分段读 JS/HTML；正式文件路径其实已知 |
| 完成修改 | 4 | `stage_external_artifact -> inspect -> patch -> commit`，这是核心必要链路 |
| 事后验证 | 8 | commit 后被验收打回；重新 stage 整个目录、检查未修改的 JS、临时编写并修正 HTML 校验脚本 |

后半段还暴露三个问题：

1. **验证顺序倒置。** 先 commit，后验证。正确顺序应是验证 draft hash，通过后原子提交同一 hash。
2. **验证范围与风险不成比例。** 只改一个 `<select>`，却检查全部 8 个 section、24 个 chart div 和未修改 JS 的语法。
3. **ValidationReceipt 目标绑定不严。** `node --check` 实际检查的是目录 snapshot 中的 JS，但生成的 receipt artifact refs 包含本轮修改的 HTML。命令成功不等于验证了被引用的 artifact；receipt 必须记录真实输入文件及其 hash。

临时 HTML 校验脚本首次还把 `<header>` 误计为 `<head>`，导致一次假失败。说明这类稳定契约不应由 Agent 每轮现写脚本，而应由 Harness 或项目声明的可复用 validator 执行。

## 3. 设计原则

### 3.1 历史保留不等于当前激活

- 已完成 Goal 的 Todo、Skill activation、Evidence 和报告必须可审计。
- 新独立 Run 的当前进度、当前能力、当前 lease 必须从空作用域开始。
- 历史状态只能作为结构化推荐或证据引用，不能冒充当前状态。

### 3.2 推荐不扩权，激活才扩权

- `recommended_inactive_skills` 只告诉模型“下一步应该读哪个 SKILL.md”。
- 只有成功读取对应 SKILL.md，才产生 Run SkillActivation 和工具集扩展。
- Skill 推荐不得自动通过联网、外部文件、目录写入或危险命令权限。

### 3.3 权限 scope 必须由用户选择决定

- UI 显示的 scope、API 请求的 scope、持久化 Grant scope 和执行匹配必须完全一致。
- Session 级权限绑定稳定 workspace/policy 边界，不绑定 Docker 容器实例或 Run ID。
- exact directory 始终是 exact directory，不向父目录或 sibling 自动扩张。

### 3.4 snapshot 是执行细节，不是用户必须手工编排的步骤

用户授权了 exact directory 后，系统可以为了沙箱隔离自动创建、刷新和映射只读 snapshot。这个动作不扩大权限，应可观测但不应再次 HITL。

### 3.5 临时验证物永远不是交付物

- `/scratch/validation/**` 只能进入 verification evidence。
- 最终 artifact 必须有正式 target path、delivery receipt 和内容 hash。
- Goal 终态必须同步关闭临时 lease 元数据和物理 scratch。

### 3.6 单一权威副本

同一个 target 在一个 Run 内只能有一个 writable draft：

```text
authoritative source target
  -> one staged draft lease
  -> patch draft
  -> validate draft content hash
  -> atomic commit same content hash
```

`/workspace` 搜索副本、目录 snapshot 和历史 scratch 均为只读参考，不能被复制后冒充当前 draft。若必须从参考副本导入，应显式创建新 draft，并记录 `derived_from_hash`，不能使用裸 `cp` 绕过 artifact lineage。

### 3.7 验证强度与改动风险相称

- 数据矩阵改变：检查 SQL receipt、矩阵形状、年度 key 和 JS 语法。
- HTML 控件改变：检查目标 selector、跨文件 key contract 和 HTML 基本结构。
- 未修改文件不重复做全量检查，除非它是本次跨文件契约的一部分。
- 任一针对候选交付物的语法/构建检查失败，都阻止该 hash 提交。
- 验证失败若可证明来自 baseline，Agent可修复、缩小改动或停止交付；不得仅靠自然语言声明绕过。
- 所有 fail-closed validator 必须有可执行修复路径和停滞熔断，避免制造新的不可闭合门。

## 4. 目标状态

### 4.1 Todo 投影

```text
GET /history
  -> active_goal_id 存在：返回 active Goal revision Todo
  -> 无 active Goal 且 latest Run 非终态：返回 latest Run Todo
  -> 无当前执行：返回 []

历史 Goal 面板
  -> 按 todo_ledgers[goal:<id>:revision:<n>] 查询归档 Todo
```

前端收到 `run_started` 时，在首个 `todos_updated` 之前先将当前 Run Todo 清空，避免旧缓存闪现。

### 4.2 目录权限

扩展授权请求：

```json
{
  "target_kind": "exact_directory",
  "path": "/absolute/path",
  "scope": "run | session",
  "permission_request_id": "permission-..."
}
```

建议 UI：

- 目录读取：`本 Session 允许读取此目录`（推荐） / `仅本次 Run` / `拒绝`。
- 目录写入：`仅本次 Run 写回`（推荐） / `本 Session 允许写回此目录` / `拒绝`。
- Session 级写入必须醒目标注“未来 Run 可在同一 exact directory 内按已审核 commit plan 写回”；仍保留 plan digest、冲突检查、禁止未声明新增文件等确定性防线。

权限判定：

```python
if grant.scope == "session":
    return exact target + capability + stable bindings match
if grant.scope == "run":
    return exact target + capability + metadata.run_id == current_run_id
```

Session 级 exact directory Grant 复用现有 semantic key 去重；用户再次点击同义授权时更新 `last_approved_at`，不产生新卡。

### 4.3 Skill 连续性

#### 活跃 Goal 内部续跑

同一 active Goal、同一 objective revision 的自动续跑应继承 Goal activation。分类候选不再作为正向白名单；它可以作为推荐排序或显式负向冲突信号。

```text
same active Goal + same revision
  -> inherit Goal activations
  -> if objective revision changes, require reconfirm/read SKILL.md
```

这避免“继续”或短追问被分类器漏判后丢失能力，同时不把能力泄漏给其他 Goal。

#### Goal 完成后的普通追问

不继承旧 activation，不自动复活旧 Goal。根据以下结构化事实生成推荐：

- 当前用户请求；
- selected analytics model；
- 最近正式交付 artifact 的类型与关联 Skill；
- 最近已完成 Goal 的 task profile 和 durable handoff facts；
- 动态 Skill Catalog。

Capability Manifest 新增：

```json
{
  "active_skill_ids": [],
  "recommended_inactive_skills": [
    {
      "skill_id": "database-analysis",
      "confidence": 0.86,
      "evidence": "追问要求补算产品配置热力图，且已选择产品配置分析模型",
      "source": "semantic_router | recent_artifact | selected_model"
    }
  ],
  "allowed_tool_names": []
}
```

系统提示必须给出明确动作：

```text
若要重算该图表，先读取 /skills/database-analysis/SKILL.md。
成功读取后数据库工具才会进入下一轮 Capability Manifest。
```

低置信度推荐不激活、不报错，主 Agent仍可按 native 路线推进。

### 4.4 `grep/glob/ls` 外部路径路由

增加统一 `ExternalSearchPathResolver`，在工具执行边界输出结构化决策：

```text
workspace_or_virtual
exact_external_file
authorized_external_directory
unauthorized_external_directory
invalid_or_escaped_path
```

#### 精确文件搜索

当 `grep.path` 是文件且已有 exact-file read Grant：

- 只读取该文件；
- 在隔离执行层完成正则匹配；
- 不列目录、不推断父目录、不发现 sibling；
- 保留与普通 grep 一致的 `content/files_with_matches/count` 输出模式。

#### 精确目录搜索

当 path 是目录且有 exact-directory read Grant：

1. 查找当前 Run 对该 source directory 的有效 snapshot lease；
2. 有 lease：把源路径和其子路径映射到 `staged_dir + relative_path`；
3. 无 lease或 snapshot 过期：自动执行只读 stage/refresh；
4. 发出 `external_directory_snapshot_started/completed` 事件；
5. 在 staged snapshot 中执行原 grep/glob/ls；
6. 不再二次 HITL。

当没有目录 Grant 时，原始 grep 调用直接生成 exact-directory read HITL；用户批准后重放同一工具调用并自动 stage，不要求模型再编排一次 `stage_external_directory`。

#### 安全不变量

- exact-file Grant 不能搜索 parent；
- exact-directory Grant 只覆盖该目录及其规范化 descendants；
- symlink escape、`.env`、密钥和既有 skip 规则继续由 snapshot 扫描器执行；
- grep/glob/ls 永远是只读能力，不能隐式获得 write Grant；
- snapshot 必须校验 source manifest freshness，不能静默读取旧 Goal 的过期副本。

### 4.5 lease 与 artifact 收口

Goal 达到 `achieved/cancelled/budget_exceeded` 时执行一个幂等终态事务：

1. 删除对应 Goal revision scratch；
2. 未提交 lease 标记为 `abandoned`；
3. 已提交 lease 保留正式 `target_path/committed_sha256/receipt_id`，移除执行时 staged path 投影；
4. Handoff 排除 `/scratch/validation/**`；
5. 最终回复的“产物”只引用 delivery receipt；
6. 验证脚本仅显示在验收详情，不进入用户交付列表。

新 Run 如果收到历史 scratch path：

```text
terminal scratch ref
  -> 有 committed target：解析到正式 target_path
  -> 无 committed target：返回 artifact_not_durable，并要求重新 stage 正式源
  -> 禁止 glob 全机和猜测相邻文件
```

### 4.6 UI/data contract 验收

对热力图这类“数据文件 + HTML 控件”组合，增加结构化契约：

```text
set(HTML select option values) == set(Object.keys(heatmapByYear))
HTML selected option == JS currentHeatYear
每年 heatmap matrix == 8 rows × 10 columns
事件处理器只允许引用存在的数据 key
```

这类检查归入 artifact/UI contract validation，不以“JS 中出现 2025/2026 字符串”代替。

建议实现为稳定的 `heatmap_year_contract/v1`，输入为两个明确 artifact hash，输出结构化 receipt：

```json
{
  "validator_kind": "heatmap_year_contract",
  "inputs": [
    {"path": "产品配置分析_2026.html", "content_sha256": "sha256:..."},
    {"path": "product-config-charts-2024.js", "content_sha256": "sha256:..."}
  ],
  "checks": {
    "year_key_set_equal": true,
    "default_year_equal": true,
    "matrix_shape_valid": true
  }
}
```

Receipt 只能绑定命令或 validator 实际读取的输入，禁止把“检查 JS 成功”登记为“HTML 已验证”。

### 4.7 Durable Artifact Handoff 与增量修复模式

每次正式 commit 后持久化：

```json
{
  "artifact_id": "artifact-...",
  "target_path": "/absolute/formal/path",
  "content_sha256": "sha256:...",
  "role": "delivered",
  "related_artifact_ids": ["artifact-html", "artifact-js"],
  "contract_ids": ["heatmap_year_contract/v1"]
}
```

后续独立 Run 不继承旧 scratch 或写权限，但应在开场获得与当前请求相关的正式 artifact facts。系统可记录：

```text
follow_up_of_goal_id / follow_up_of_artifact_ids
```

它表达语义连续性，不改变已完成 Goal 的不可变状态，也不自动扩展 Skill 或权限。

若当前请求是对已交付产物的局部纠错，进入 `delta_repair`：

1. 先比较用户断言与已交付 artifact contract；
2. 明确受影响文件和最小差异；
3. 若数据已存在，不激活数据库 Skill、不重新查询数据库；
4. 对最多两个已知文件优先 exact-file read/stage，不先递归扫目录；
5. 验证最小契约，通过后提交并立即结束。

### 4.8 验证门控提交

把“修改—验证—提交”从 Agent习惯升级为工具协议：

```text
patch draft(hash=B)
  -> validator receipt(input_hash=B, passed=true)
  -> commit_external_artifact(
       lease_id,
       expected_draft_sha256=B,
       validation_receipt_ids=[...]
     )
  -> target hash 必须等于 B
```

对代码类 artifact，若该 hash 已有失败的 blocking receipt 且没有后续同 target/hash 的成功 receipt，commit 必须拒绝。成功提交后只核对 target hash，无需为了“验证正式文件”重新 stage 整个目录。

## 5. 分阶段实施

### P0-0：立即止血

1. `run_started` 清空当前 Todo 投影。
2. `/history` 改为按 active Goal/latest active Run 返回 Todo。
3. 权限侧栏正确显示 `本 Run` 与 `本 Session`。
4. Handoff/最终产物过滤 `/scratch/validation/**`。
5. Goal 终态将遗留 staged lease 标记为 abandoned。
6. 修复热力图 HTML 选项 2025/2026 与默认 2026，并加入一次性契约校验。
7. 任一候选交付 hash 的 `node --check`/语法检查失败时，禁止 commit 同一 hash。
8. ValidationReceipt 只绑定 validator 实际读取的 path/hash。

### P0-A：目录 Session Grant

1. `ExternalFileGrantRequest` 增加 `scope=run|session`。
2. API 按用户选择持久化 scope，不再硬编码。
3. `has_external_directory_permission` 同时支持 run/session。
4. Session Grant 使用稳定 binding 和现有 semantic dedupe。
5. 并发 pending request 在 Session Grant 生效后自动 resolve 同义请求。

### P0-B：搜索工具自动路由

1. 支持已授权 exact file grep。
2. 支持已授权 exact directory 自动 stage/refresh/mapping。
3. 无权限的外部目录 grep 直接触发目录 read HITL。
4. 为自动 snapshot 增加可观测事件和大小/耗时指标。

### P0-C：单一 draft 与验证门控提交

1. external artifact 修改只允许发生在该 target 的唯一 writable lease。
2. 禁止把 `/workspace` 影子副本用裸 `cp` 覆盖到 external directory draft。
3. code/artifact commit 接受并校验 artifact-bound ValidationReceipt。
4. 验证失败记录按 target path + content hash 成为 blocker。
5. 验证 draft 通过后提交同一 hash；提交后仅做 hash 确认，不重新 stage 整目录。
6. `/scratch/validation` 允许覆盖临时脚本，但稳定项目契约优先使用内建 validator，不让 Agent每轮重写。

### P1-A：Skill 推荐与 Goal 继承

1. 同 active Goal revision activation 默认继承。
2. Capability Manifest 增加 `recommended_inactive_skills`。
3. semantic Router、selected model 和 recent durable artifact 共同产生推荐。
4. 推荐只引导读取 SKILL.md，不直接扩权。
5. 移除字面量 `<id>` 提示，改为具体 Skill 路径。

### P1-B：恢复协议与验收

1. terminal scratch 引用结构化恢复。
2. artifact/UI 跨文件契约。
3. Trace 增加 permission reuse、snapshot route、skill recommend/activate 时间线。

### P1-C：Durable Artifact Handoff 与增量修复

1. commit receipt 持久化正式 target path/hash、related artifacts 和 contract ids。
2. 为独立追问建立 `follow_up_of_artifact_ids`，不复活已完成 Goal。
3. 对局部纠错启用 `delta_repair`，先判断数据是否已存在，再决定是否激活数据库能力。
4. 已知的双文件修复优先 exact-file 并行读取，禁止先读旧 lease 或全盘 glob。
5. 将“发现差异后继续搜索”的停止条件写入主 Agent提示：最小 patch 已唯一确定时结束 discovery。

## 6. 测试矩阵

### 6.1 Todo 与 Goal

- Goal achieved 后新独立 Run：当前 Todo 为 `[]`，Goal 历史仍有 11/11。
- 页面刷新：不恢复旧 Goal Todo 为当前进度。
- active Goal 自动续跑：同 revision Todo 正常继承。
- Goal revise：新 revision 不继承旧 revision 当前 Todo。

### 6.2 目录权限

- Session read Grant 在下一 Run 命中，不弹卡。
- Run read Grant 在下一 Run 不命中。
- Session write Grant 只匹配 exact directory，不匹配 sibling/parent。
- policy epoch 变化后旧 Session Grant 失效。
- 同义 Session Grant 重复批准只保留一个有效 Grant。
- UI 分别显示“本 Run”“本 Session”。

### 6.3 Skill

- active Goal 同 revision、候选为空：仍继承已激活 Skill。
- Goal revision 改变：不自动继承。
- achieved Goal 后独立追问：不继承 active tools，但 Manifest 推荐 database-analysis。
- 推荐 Skill 未读取：数据库工具不可见。
- 成功读取 SKILL.md：下一模型轮 Manifest 和 Tool Schema 同时出现数据库工具。
- 低置信度无匹配：native 正常放行。

### 6.4 grep/glob/ls

- exact external file + file Grant：grep 成功，不能发现 sibling。
- exact external directory + Session Grant：首 Run 自动 stage，后续 Run 自动 refresh，无新 HITL。
- 无目录 Grant：grep 触发一次 exact-directory HITL，批准后原调用自动完成。
- symlink escape、secret skip、`..` traversal 均 fail closed。
- source manifest 变化：snapshot 刷新后搜索新内容。
- 有未提交 draft 且 source 并发变化：不静默覆盖 draft。

### 6.5 artifact 与热力图

- Goal 终态后 scratch 不存在且 lease 为 abandoned/committed，不再显示 staged。
- validation script 不进入 `artifact_refs` 和最终产物链接。
- 新 Run 追问正式产物时解析到 target path，而不是旧 scratch。
- HTML 年份选项与 JS heatmap key 完全一致。
- selected year 与 currentHeatYear 一致。
- 2021–2026 每个矩阵均为 8×10。
- 数据已含 2025/2026、HTML 选项缺失：只修改 HTML，不触发数据库 Skill 或 SQL。
- HTML/JS contract validator 的 receipt 同时绑定两个实际输入 hash。
- `node --check` 检查 JS 时，receipt 不得错误绑定 HTML 作为被验证目标。

### 6.6 单一副本与失败验证

- `/workspace` 参考副本与 external draft 同时存在：只有 external draft 可写、可提交。
- draft hash B 的 `node --check` 失败：commit B 被拒绝。
- 修复为 hash C 并通过检查：只允许 commit C。
- baseline 原本失败：Agent选择修复、回滚或停止；不能用自然语言绕过 blocker。
- draft 验证通过并 commit：target hash 等于 receipt input hash，且不需要再次目录 HITL。

### 6.7 配对 Run 回归预算

- “热力图数据还没有更新”：在文件位置和权限已持久化时，不超过 12 次主 Agent工具调用；不因数据库工具不可见而自动委托黑盒子代理。
- “下拉只到 2024”：不超过 6 次工具调用；无数据库调用、无子代理、无目录递归写授权。
- 两个用例都不得读取 terminal scratch 路径。
- 两个用例都不得提交任何已知语法检查失败的 artifact hash。

## 7. 观测指标

- `permission_prompt_count{type,target,session}`：同 Session exact directory 的重复弹卡数。
- `permission_reuse_count{scope}`：Session Grant 复用次数。
- `external_snapshot_stage_ms`、`external_snapshot_refresh_count`。
- `external_search_route{workspace,exact_file,staged_directory,denied}`。
- `skill_recommended_count`、`skill_activated_from_recommendation_count`。
- `goal_todo_projection_mismatch_count`。
- `terminal_scratch_ref_recovery_count`。
- `artifact_ui_contract_failure_count`。
- `artifact_handoff_hit_count`、`artifact_handoff_stale_ref_count`。
- `authoritative_draft_conflict_count`。
- `validation_receipt_target_mismatch_count`。
- `commit_blocked_by_failed_validation_count`。
- `delta_repair_tool_calls`、`delta_repair_elapsed_ms`。

## 8. 审核决策点

1. **目录读取默认按钮**：是否确认以“本 Session exact-directory read”为推荐选项？本方案建议确认。
2. **目录写入 Session scope**：是否允许用户显式选择？本方案建议允许，但默认仍为“仅本次 Run”，并保留 commit plan、冲突检测和未声明文件防护。
3. **active Goal Skill 继承**：是否接受“同 revision 默认继承，分类候选只作推荐而非硬门”？本方案建议接受。
4. **已完成 Goal 的追问**：是否维持独立 Run、不自动复活 Goal？本方案建议维持；通过推荐 Skill 和 durable artifact handoff解决连续性。
5. **外部 grep 自动 stage**：是否接受在已有 exact-directory Grant 时透明创建只读 snapshot？本方案建议接受，并以 Trace 事件保持可观测。
6. **验证门控 commit**：是否接受代码类 external artifact 必须携带同 draft hash 的成功 ValidationReceipt？本方案建议接受；这是阻断坏 JS 交付的硬边界。
7. **独立追问连续性**：是否接受使用 `follow_up_of_artifact_ids` 继承位置与契约事实，但不自动复活 achieved Goal、不自动继承工具能力？本方案建议接受。

## 9. 最终判断

外部排查的权限根因、候选硬门和 Manifest 缺少推荐字段均有源码依据；但“本次旧 Goal activation 被硬门挡住”和“追问 Run 未挂已完成 Goal 是 bug”不符合当前 Session 事实或正确生命周期语义，需要按本文修正。

优先顺序应为：

```text
先阻断失败验证 hash 的提交，并修正 ValidationReceipt 目标绑定
  -> 再修当前状态投影与目录 Session Grant
  -> 再建立正式 artifact handoff 与唯一 writable draft
  -> 再让 grep 自动消费已有权限
  -> 再补推荐 Skill 与 active Goal 继承
  -> 最后用风险相称的跨文件契约阻断“数据更新、控件未更新”
```

这样既消除重复 HITL、文件搜索和事后验收绕行，也不会通过隐式父目录授权、跨 Goal 工具继承、自动复活 Goal 或 Agent主观忽略失败检查来换取表面上的顺畅。

## 10. 二次架构审核：对照 Codex 与 Grok Build 收缩权限和验收边界

### 10.1 审核结论

第一阶段修复了既有 lease、Grant、搜索和验证链中的具体错误，但仍保留了两个过重的前提：

1. 把用户明确选中的非 workspace 目录长期当作“外部 Artifact”，让 Agent 显式编排 `stage -> patch -> prepare -> commit`；
2. 默认给每个普通 Run 打开完整验收，再依靠任务分类和 Rubric 裁剪复杂度。

这两个前提都应调整。更通用的边界是：

```text
用户选中的目录
  -> 成为本 Session 的附加工作根
  -> 普通文件工具直接操作
  -> 权限和沙箱在工具下方执行

普通 Run
  -> 主 Agent 自己判断需要读取、修改和验证什么
  -> Harness 只记录事实并执行少量硬不变量

显式 Goal / 用户明确要求严格验收
  -> 才启用独立完成度评审、修订循环和跨 Run 聚合
```

重点不是复制 Codex 或 Grok Build 的实现，而是恢复正确分工：**基础设施负责真实权限和真实执行结果，AI 负责理解任务、制定修改与验证方案；重验收只服务明确的长任务，不覆盖所有聊天。**

### 10.2 Grok Build 本地源码结论

本次核对源码版本：`/Users/pet/Code/AI/Agent/源码合集/grok-build`，commit `b189869b7755d2b482969acf6c92da3ecfeffd36`。

#### 文件系统与权限

Grok Build 的 `workspace` sandbox profile：

- 默认可读整个文件系统；
- 可写 workspace、`~/.grok` 和临时目录；
- OS sandbox 在进程启动时一次性应用，不为每次文件修改创建 lease；
- 普通 `read/grep` 是权限快路径；
- Ask 模式允许用户选择“本 Session 允许所有编辑”；
- Auto 模式把文件编辑视为普通本机开发工作，但仍受底层 OS sandbox 的真实可写根限制；权限放行不会凭空扩大 kernel capability。

对应源码：

- `crates/codegen/xai-grok-sandbox/src/profiles.rs`：`workspace` profile 为 `default_read=true`，写根来自 `essential_writable_paths(workspace)`；
- `crates/codegen/xai-grok-sandbox/src/paths.rs`：workspace、Grok state 与 temp 是默认写根；
- `crates/codegen/xai-grok-workspace/src/permission/auto_mode.rs`：Read/Grep/WebSearch 快路径；Auto 模式允许 Edit，但明确晚于底层 policy/sandbox；
- `crates/codegen/xai-grok-workspace/src/permission/manager.rs`：`allow_edits_for_session` 为 Session 内存状态，安全命令、持久化命令 Grant 和显式 deny 分层处理；
- `crates/codegen/xai-grok-workspace/src/permission/prompter.rs`：编辑权限提供 `AllowEditsForSession`，并为更高风险能力保留 AllowOnce/Always/Reject。

因此 Grok Build 并没有解决“在固定 workspace sandbox 中动态写任意外部目录”。它的顺畅来自两个事实：

1. 正常工作目录在启动时就是 workspace；
2. 权限提示不会把底层文件事务协议暴露给模型。

如果要处理另一个目录，正确动作是把它作为工作区/额外可写根重新启动或使用更宽 profile，而不是让模型手工搬运文件。

#### 验收

Grok Build 的复杂完成判定严格依附显式 Goal：

- TodoGate 的代码注释明确写着只在 `/goal` active 时运行；
- Goal classifier、continuation、skeptic 与 backoff 由 active Goal 状态机触发；
- 普通聊天不会因为出现“配置率”“HTML”“数据库”等关键词自动进入 Goal 验收循环。

对应源码：

- `crates/codegen/xai-grok-shell/src/agent/config.rs`：TodoGate 只在 `/goal` active 时运行；
- `crates/codegen/xai-grok-shell/src/session/acp_session_impl/goal.rs`：只有 active Goal 才在轮次结束运行 continuation/verification；
- `crates/codegen/xai-grok-shell/src/session/goal_classifier.rs`：复杂 reviewer 是 Goal 子系统，而不是每轮聊天的默认尾处理器。

Grok Build 的 reviewer 本身并不轻，但它没有把这份成本施加给简单追问。这是 PuddingClaw 当前最应该借鉴的边界。

### 10.3 Codex 当前运行模型的对照

当前 Codex 桌面运行时采用同样的两层思路：

- filesystem sandbox 先定义 workspace roots 与额外 writable roots；
- 根内修改由普通编辑工具直接完成；
- 根外写入通过一次明确的权限升级或把目录加入 workspace root；
- 权限批准的是能力/命令范围，不要求模型操作外部 Artifact lease；
- 验证强度由任务和改动风险决定，普通问答、只读检查不会自动进入独立 Rubric 修订循环。

Codex 与 Grok Build 的共同点不是“权限更松”，而是：

```text
工作根是稳定能力
工具直接表达用户动作
权限协议隐藏在工具下方
重验收是显式任务模式，不是聊天默认模式
```

### 10.4 PuddingClaw 当前结构为何仍显得不流畅

PuddingClaw 的 Docker Backend 无法像本地进程一样，在进程启动后直接增加任意 host bind mount。现有 lease 机制是合理的隔离实现，但它把容器约束泄漏给了 Agent：

```text
用户：改这个 HTML
Agent：stage_external_artifact
       inspect
       patch
       prepare
       commit
       重新 stage 验证正式文件
```

用户授权的是“处理这个目录”，Agent 却被迫理解 snapshot、lease、源 hash、draft hash 和 commit plan。该复杂度属于 Harness，不属于任务推理。

验收侧的对应问题是：

- `HarnessRunCoordinator.start_run(... verification_enabled=True)` 默认给所有 Run 打开验收；
- TaskProfile 在主 Agent 前通过关键词/分类器决定 packs；
- 成功工具又动态扩展 contract；
- 中间件在普通 Run 结束时也可能 `needs_revision -> jump_to model`。

因此一次只读追问也可能被先验分类成数据库分析，然后继承不相称的证据标准。当前 HUD 查询就是这种机制性误伤。

### 10.5 新权限模型：Session Workspace Root

> 本节是后续实施的权威方案，取代第 5 节 P0-B 中“外部目录搜索自动 stage/refresh/mapping”作为长期默认路径的设计。第 5 节保留为第一阶段事故修复与实施审计记录，不再作为普通外部文件操作的目标架构。

#### 核心对象

当用户在 UI 中选择目录，或首次为 exact directory 授权时，系统创建：

```json
{
  "root_id": "session-root-...",
  "session_id": "session-...",
  "canonical_host_path": "/absolute/selected/directory",
  "capabilities": ["read", "search", "create", "update", "delete"],
  "scope": "session",
  "policy_epoch": "...",
  "backend_mode": "local|docker",
  "status": "active"
}
```

这不是新的项目 workspace，也不自动获得 Terminal/网络/业务工具权限；它只是把用户明确选择的目录提升为 Session 文件工作根。

#### Agent 看到的接口

主 Agent继续只看到通用文件工具：

```text
read_file
grep / glob / ls
apply_patch
write_file
delete_file
```

`WorkspacePathRouter` 根据路径透明路由：

```text
项目 workspace       -> 原 FilesystemBackend
Session Workspace Root -> HostFileBroker
未授权外部路径         -> 一次 HITL，批准后重放原工具调用
```

Agent 不再调用 `stage_external_artifact`、`stage_external_directory`、`prepare_*` 或 `commit_*` 完成普通文件编辑。

当用户最初只提供一个 exact file 时，系统继续遵守最小授权原则，不自动扩大到父目录。若 Agent 读取文件后确认需要发现 sibling dependency，原始 `ls/glob/grep/read_file` 调用直接产生该文件**直接父目录**的 exact-directory HITL：

```text
exact file grant
  -> Agent 需要 sibling discovery
  -> 请求直接父目录，本 Session，Read only 或 Read/Write
  -> 用户批准
  -> 自动重放原始文件工具
  -> 后续原生文件工具直接命中 HostFileBroker
```

禁止自动申请 `/`、`/Users`、用户 Home、`.ssh` 等过宽或敏感根；上传附件的临时缓存目录也不能从单文件授权自动升级为目录授权。需要更高层祖先目录时必须再次说明具体原因并显式申请。

#### HostFileBroker

Docker 场景新增一个宿主侧最小文件代理，只接受签名后的 Grant/Root：

- canonical path + descendant 校验；
- symlink escape 防护；
- `expected_sha256` 并发冲突检查；
- 原子写入/rename；
- create/update/delete 操作日志；
- 可选短期备份或 Git 可恢复性；
- 每个响应返回最终 path、bytes hash 与操作 receipt。

普通单文件 patch 直接写入真实目标，不再复制到 `/workspace` 影子文件。若一次修改需要多文件原子性，Broker 内部开启事务，仍不把事务步骤暴露给模型。

Hash 继续作为并发控制和审计事实存在，但不再由 Agent 传递或区分 `source/draft/current/expected_source_sha256`。Broker 在读取时建立内部 version token，写入时自动检查目标是否被宿主机并发修改；冲突只返回“重新读取并重新应用 patch”的可执行错误。写入使用临时文件加原子 rename，并对不存在的新文件从最近存在的父目录重新执行 canonical descendant 与 symlink escape 校验。

#### Shell/构建

文件授权不改变命令执行边界。`execute`、build、test、install 和 network 继续使用现有项目 Docker Backend、现有网络/安装/危险命令 HITL 和现有 `/workspace`、`/scratch` 挂载，不因为 Session Workspace Root 授权而重建项目容器，也不创建长期 Session 容器。

普通外部绝对路径继续受 `container_path_expansion` 阻断；获得文件读写权限不等于获得在任意 shell 中访问该宿主路径的能力。外部文件需要语法或结构验证时，由 Harness 在 Agent 看不到的内部步骤中完成：

```text
HostFileBroker 读取最终 bytes/hash
  -> 物化到 /scratch/validation/<content-hash>/
  -> 现有项目容器运行 node/pytest/validator
  -> ValidationReceipt 绑定原始 canonical path + content hash
  -> 自动清理临时副本
```

只有命令确实需要完整外部目录语义时，才采用两个显式出口：优先提示用户将该目录选择为项目 Workspace；或者在额外命令级授权后，使用现有镜像和依赖卷启动一次性 `docker run --rm`，只挂载该 exact directory，并在命令结束后销毁。不得把全部 Session root 挂入共享项目容器，也不得恢复成 Agent 手工 `cp + lease`。

#### 权限体验

建议默认选项：

- 读取/搜索：`本 Session 使用此文件夹`；
- 编辑：`本 Session 允许在此文件夹内创建和修改文件`；
- 删除普通文件包含在显式目录写权限中，但递归删除、批量删除、覆盖并发修改仍单独 ask；
- `仅本次` 保留为次选；
- 相同 Root/能力的并发 pending request 归并为一个，Grant 生效后自动恢复所有同义请求。

父目录 Grant 生效后，应将已被覆盖的 exact-file Grant 在 UI 中折叠为“已由目录授权覆盖”或标记 superseded，避免权限侧栏继续堆积多张语义重复卡片。Read/Write 不隐含任意 shell 权限；递归删除、批量删除和覆盖并发修改仍使用现有高风险确认。

#### Stage/lease 兼容与退役记录

`stage_external_artifact`、`commit_external_artifact`、`stage_external_directory`、`prepare_external_directory_commit`、`commit_external_directory` 从新 Run 的默认 Toolset、Capability Manifest 和 Prompt 中退出。第一版不直接删除源码，原因是旧 Session、进行中的 lease、历史 checkpoint 和回放测试仍可能依赖这些工具。

兼容期规则：

1. 仅旧 Run 恢复或显式多文件事务兼容层可以调用 Stage/lease 工具；新普通文件任务不得选择该路由；
2. Broker 可以内部复用既有冲突检测、回滚和 commit journal，但不得把 lease id、staged path 或 hash 参数暴露给 Agent；
3. Trace 为兼容调用记录 `legacy_external_lease_tool_used`，包含 tool、Run 来源和是否来自旧 checkpoint；
4. 源码增加统一 deprecated 注释和删除追踪项，禁止继续向兼容层增加新产品能力；
5. 连续两个发布周期没有新 Run 调用、没有 active lease、旧 checkpoint 恢复测试完成迁移后，删除工具 Schema、Prompt、Middleware 分支及孤立测试；
6. 删除前保留一次状态迁移，将未完成 lease 标记为 `abandoned` 或转换为 Broker transaction receipt，不能静默遗失可恢复任务。

外部交付验收也必须从“是否调用 `commit_external_*`”迁移为消费 Broker 的 `external_mutation_completed` receipt。Receipt 至少记录 grant id、canonical path、before/after hash、changed files 与原子写入结果；否则 Stage 工具退出后会再次出现“文件已经写好但 Harness 永远不认”的完成死路。

### 10.6 新验收模型：从“所有 Run 重验收”改为三级模式

不再通过不断增加任务关键词来选择验收标准。Run 从显式状态和实际工具行为升级验收强度：

#### Level 0：Agent-owned

适用：普通问答、解释、状态检查、只读 Artifact/代码检查、简单追问。

- 不创建独立 Rubric reviewer；
- 不运行 completion repair loop；
- 主 Agent根据问题按需读取证据并直接回答；
- Harness 只确保工具结果真实、失败不能伪装成成功、引用路径存在。

HUD 查询应落在此级：读 HTML 当前 script 引用、读 JS HUD 数组、回答。无数据库、无 Goal、无“发现完成条件缺口”。

#### Level 1：Proportional verification

适用：普通 Run 中实际发生文件修改、代码修改、数据库写入或用户明确要求“检查一下”。

- 不启动第二个 LLM reviewer；
- 主 Agent制定与变更风险相称的验证计划；
- Harness 根据实际成功工具行为记录 receipts；
- 只执行少量硬不变量：写入/提交确实成功、最终 bytes 与 receipt 一致、已有失败检查未被自然语言绕过；
- 没有预先命中的“pytest/ruff/node”命令清单，也不因缺少某种固定测试自动打回；
- 若 Agent没有足够验证，应在最终回复中如实说明，而不是自动开启多轮修订。

例如只把下拉框从 2024 补到 2026：Agent可读取相关 HTML/JS、做最小 patch、运行一个针对该 selector 的检查并结束，不重新验整个报告数据库。

#### Level 2：Goal verification

仅在以下条件启用：

- 用户显式创建/继续 Goal；
- 用户明确选择“严格验收”；
- 产品层明确规定必须独立审批的高风险流程。

此时才启用：

- 持久化 acceptance contract；
- 独立 reviewer；
- deterministic obligations；
- `needs_revision` 修订循环；
- 跨 Run evidence 聚合；
- 停滞熔断和人工接管。

这与 Grok Build 的 `/goal` 边界一致，但保留 PuddingClaw 已有的 Rubric、Receipt 与业务数据验收能力。

#### 模式升级，不做前置关键词分类

```text
Run 开始：Level 0

active Goal / strict verification toggle
  -> 直接 Level 2

实际成功执行 mutation tool
  -> Level 0 动态升级 Level 1

实际使用 DB/Web/Skill
  -> 记录对应 evidence receipt
  -> 不自动升级 Level 2
```

TaskProfile 可以继续帮助主 Agent理解 Skill 候选，但不能再决定普通 Run 是否进入 completion loop。Skill 激活、权限、证据记录和完成验收是四个独立维度。

#### Level 2 跨 Run 稳定证据引用

跨 Run 继承不得复制一份可能缺少 `tool_call_id`、`result_id` 或来源字段的 evidence JSON。Run/Goal/Handoff/compact 只传递稳定引用，验收时回查权威 Ledger：

```json
{
  "evidence_ref": {
    "type": "analytics_result",
    "id": "result-456"
  }
}
```

优先复用已有的权威主键，不为每层包装重复造 ID：

- Analytics 查询结果使用全局唯一、可持久化的 `result_id`；
- SQL/语义校验使用 `sql_validation_receipt_id`；
- Artifact 使用 `artifact_id + content_sha256`；
- Code/结构验证使用 `validation_receipt_id`；
- Tool result 在没有更高层 Receipt 时才使用 `origin_tool_call_id`；
- 只有旧结果没有任何可复用权威主键时，Backend 才创建一次 `evidence_record_id`，模型不能自行生成。

Ledger 中的权威记录至少保留：

```json
{
  "id": "result-456",
  "kind": "analytics_result",
  "source_run_id": "run-A",
  "source_query_id": "query-A",
  "origin_tool_call_id": "call-123",
  "goal_id": "goal-X",
  "goal_revision": 1,
  "output_digest": "sha256:...",
  "result_id": "result-456",
  "status": "active"
}
```

继承时只追加 `{type, id}`，Resolver 必须确认：记录真实存在、来源工具或 Receipt 成功、Goal/revision 继承策略允许、内容 digest 未失配、状态不是 `stale/revoked/deleted`。确定性检查不再要求浅层继承对象必须直接携带 `tool_call_id`；它先解析 `evidence_ref`，再检查权威记录中的 `origin_tool_call_id`，或检查等价的 `result_id + query trace + source_run_id` 完整 lineage。

Compact 摘要必须原样保留 `evidence_ref`，不能把权威记录重新摘要成自由文本或裁剪后的 JSON。旧 evidence JSON 只有在来源字段完整时才能一次性迁移进 Ledger；来源不完整的旧记录可用于当前 Run 展示，但不得成为跨 Run Goal 验收证据。

该机制只服务 Level 2 Goal 的合法证据连续性。Level 0 简单追问不建立证据继承合同，Level 1 普通修改只消费当前 Run 的 mutation/validation receipt，不能因存在历史 analytics evidence 自动升级到 Level 2。

### 10.7 AI-native 的职责边界

保留的确定性规则只处理不可争议事实：

1. 工具成功或失败；
2. 用户究竟授权了哪个 canonical root 和哪些 capability；
3. 最终写入 path/hash 是否与 receipt 一致；
4. 已经运行且失败的阻断检查不能被口头忽略；
5. Goal 是否 active，是否已到预算/停滞上限。

AI 负责：

1. 用户究竟想查、改还是重算；
2. 该读取哪些文件和上下文；
3. 修改方案是否要改变文件名或交付结构；
4. 普通修改需要什么比例的验证；
5. 验证失败后修复、回滚还是说明阻塞；
6. Goal 的语义完成度。

这避免两种极端：既不让模型绕过真实权限，也不让 Harness 用大量业务词法规则替模型理解任务。

### 10.8 分阶段落地

#### P0（同批发布）：普通需求验收降级 + 外部文件直通

P0-A 与 P0-B 必须作为同一批优化上线：前者停止简单任务被重验收打回，后者停止简单文件任务被 lease 协议放大。只做其中一个都会保留另一条空转链。

**P0-A：普通需求验收降级**

1. `RunRecord` 增加 `verification_mode=agent|proportional|goal`；
2. 无 active Goal 的普通 Run 默认 `agent`，不挂载 `PuddingClawRubricMiddleware` 修订循环；
3. 成功 mutation tool 后动态升级 `proportional`；
4. 只有 `goal` 模式允许 `needs_revision -> jump_to model`；
5. TaskProfile/Skill candidates 不再改变 verification mode；
6. 保留 Trace 中的 evidence/receipt，但聊天区不显示验收状态机；
7. 用 HUD 原始追问作为固定回归：最多 3 次文件工具、0 DB、0 HITL、0 revision。

**P0-B：Session Workspace Root 与 HostFileBroker 直通**

1. 扩展现有 `PermissionedCompositeBackend`，从 exact-file 路由升级为 exact-directory descendant 路由；
2. `WorkspacePathRouter` 将已授权外部路径的 `read_file/ls/glob/grep/write/edit/patch` 直接交给 HostFileBroker，不再自动 snapshot/stage；
3. 单文件任务发现 sibling dependency 时，由原始工具调用触发直接父目录 HITL，批准后重放，不要求模型改调权限工具；
4. 相同 Session + canonical root + capability 的 Grant 和 pending request 语义去重；
5. 普通新 Run 的 Toolset/Prompt 隐藏全部 Stage/lease 工具；旧 Run 兼容路径保留；
6. Broker 写入产生 `external_mutation_completed` receipt，Level 1 验收直接消费该 receipt；
7. `execute` 与项目 Docker 权限、网络、安装和危险命令逻辑保持不变。

**P0-C：Goal Analytics 稳定证据引用**

1. 为 Analytics/SQL/Validation/Artifact Ledger 定义统一的 `{type, id}` `evidence_ref`；
2. 优先复用 `result_id/receipt_id/artifact_id`，不强制新增冗余 `evidence_id`；
3. Goal/Handoff/compact 只持久化引用，不复制 evidence payload；
4. Resolver 回查来源 Run、原始 Tool Call、query trace、digest 和 Goal revision；
5. 修复 `analytics_result` 浅层必须含 `tool_call_id` 的死条件，但不接受没有完整 lineage 的任意结果；
6. 同 Goal revision 已通过的同一引用去重继承，不重新索取相同查询证据；
7. 为旧 evidence JSON 提供一次迁移，无法恢复来源的记录标记 `non_inheritable`。

#### P1：Broker 一致性与验证桥接

1. canonical descendant、no-follow/symlink escape、原子 rename 和并发 version token 收敛为单一实现；
2. 外部文件验证自动物化到 `/scratch/validation/<hash>`，用现有项目容器执行并绑定原始 target/hash；
3. 多文件原子修改由 Broker 内部事务完成，Agent仍只看到一次通用文件工具结果；
4. 错误统一为 `permission_required/conflict/validation_failed/io_error`；
5. Read/Write 与 delete/bulk delete 的风险级别和 HITL 明确分离；
6. 基于 Broker mutation receipt 建立 per-Run rewind journal，记录 before/after hash 和 diff，支持“撤销本轮修改”；多文件任务后续可增加逐文件/逐 hunk accept/reject，但不替代并发版本检查。

#### P2：兼容层退役与少数外部命令

1. 上线 legacy Stage/lease 调用指标和 active lease 迁移审计；
2. 满足两个发布周期零新调用等退出条件后删除旧 Tool Schema、Prompt 和 Middleware 分支；
3. 完整外部目录命令优先切换项目 Workspace；确需临时执行时使用命令级授权的 `docker run --rm` 精确挂载；
4. 不实现长期 Session Docker worker，不把 Session roots 批量挂入共享项目容器。

### 10.9 新增验收用例

1. 已完成 Goal 后问“HTML 中 HUD 数据是多少，用哪个 JS”：Level 0，直接读取当前文件，不恢复旧 contract；
2. 历史回答说 2024.js、当前 HTML 引用 2026.js：以当前文件为准；
3. 普通 Run 修改一处 HTML：动态 Level 1，无 reviewer 修订循环；
4. 显式 Goal 刷新完整报告：Level 2，保留现有严格验收；
5. Session Root 授权后跨 Run read/grep/patch/create/delete 不重复 HITL；
6. 项目 Docker 重建后 Root Grant 仍命中，container ID 变化不产生重复授权，且无需创建 Session 容器；
7. 并发两个同目录请求只展示一张权限卡；
8. 外部路径发生并发修改时 expected hash 冲突，不静默覆盖；
9. 普通文件工具响应不包含 staged path、lease id、source hash/draft hash 区分；Trace 仍保留内部事务细节；
10. Goal 外普通 Run 永远不会出现“发现完成条件缺口，继续处理”。
11. exact file 读取后确需 sibling discovery：只申请直接父目录，批准后自动重放原始 `grep/ls`；禁止升级附件缓存目录或敏感祖先目录；
12. 外部目录 Read only Grant 可以 read/ls/glob/grep，但不能 write/patch/delete；
13. 获得外部文件写权限后，`execute` 仍不能直接访问宿主绝对路径，现有 `container_path_expansion` 与命令 HITL 不变；
14. 外部文件验证经 `/scratch/validation/<hash>` 完成时，Receipt 绑定正式 canonical path/hash，临时副本不成为交付物；
15. 新 Run 不暴露 Stage/lease 工具；旧 checkpoint 仍能恢复并完成或被安全迁移；
16. Broker mutation receipt 能独立闭合 Level 1 artifact delivery，不依赖 `commit_external_*` 工具名；
17. Goal Run A 产生 `result_id`，Run B 只携带 `{type: analytics_result, id: result_id}` 仍能解析到原始 Tool Call 与 query trace；
18. Compact 前后 `evidence_ref` 字节级稳定，不退化成自由文本摘要或残缺 evidence JSON；
19. 模型伪造不存在的 `result_id/evidence_record_id` 时验收拒绝，且不触发无关数据库返工；
20. 旧 analytics evidence 有完整 lineage 时迁移成功，缺少来源时标记 `non_inheritable`；
21. 同 Goal revision 重复引用同一 `result_id` 只计一次，不重新要求同一查询；
22. Level 0/1 不因历史 Goal 的 `evidence_ref` 自动激活 analytics contract；
23. 同一语义 gap 连续出现时，停滞签名不受 evidence 列表增长影响并立即早退；
24. Broker rewind 只撤销当前 Run 且目标 hash 仍匹配的修改；发生宿主机并发更新时拒绝覆盖。

### 10.10 修订后的优先级

```text
同批上线：普通验收降级 + Session Root/HostFileBroker 直通
  -> 同时切断 completion loop 与 lease 编排放大

随后补齐 Broker 并发安全和容器验证桥接
  -> 文件直接工作，任意代码仍留在现有 Docker 边界

最后依据遥测删除 Stage/lease Agent 工具兼容层
  -> 精简代码但不破坏旧 Session/checkpoint 恢复
```

第一阶段文档中关于 Artifact Registry、ValidationReceipt、外部目录安全和 Goal 严格验收的机制仍然有价值；需要删除的是它们对普通追问和普通文件编辑的默认侵入，而不是删除底层事实与审计能力。`execute` 的 Docker、网络、安装和危险命令权限逻辑不在本批次放宽范围内。

### 10.11 附件方案交叉审核决策记录

对《优化方案：外部文件权限流畅化 + 追问验收分级》的实测结论按当前源码重新核对后，形成以下取舍；本节用于防止后续实现重新采用已过时或已否决的分支。

#### 直接吸收

1. HUD 只读追问被 `database_analysis + analytics_evidence_traceability` 误伤的事故证据，作为 Level 0 固定回归；
2. Goal analytics evidence 的 lineage 缺口，按本章稳定 `evidence_ref` 方案修复；
3. 禁止 `/workspace` 或 `/scratch` 影子副本成为外部 Artifact 的写回权威；
4. Hash/Receipt 保留在 Backend 与 Trace，对 Agent 隐藏；
5. Analytics pack 以当前 Run 的实际成功工具行为记录 evidence，不以消息关键词激活普通 Run completion contract；
6. Level 2 使用语义停滞签名，相同 gap 连续出现立即早退；
7. 借鉴 rewind/hunk tracker 的可恢复性，在 P1 先实现 Broker per-Run rewind journal。

#### 已由当前代码修复，只保留回归

附件所述“目录 scope 被 API 写死为 Run、Session 匹配分支不存在、相同 Grant 不去重”属于修复前状态。当前 API 已按用户选择存储 `run|session`，SessionManager 已按稳定 bindings 匹配 Session directory Grant，并对相同 Session Grant 做语义去重。后续只保留前后端 scope 一致、跨 Run 复用、容器重建不重复授权的测试，不重复实现第二套权限存储。

#### 改写后吸收

1. 不采用“Agent 无感但 Middleware 内部每次自动 stage/commit”作为普通路径；普通文件直接走 HostFileBroker，Stage/lease 仅作为旧 Run 兼容或 Broker 内部多文件事务原语；
2. informational 档进一步收缩为 Level 0：无 grader、无 deterministic completion loop；
3. modification 档使用 Level 1 proportional receipt，不采用固定 `code_validation + 2 次 revision`；已有失败检查仍是 blocker，但不会自动启动第二个 LLM reviewer；
4. 验收基础设施异常在 Level 0/1 可作为明确标注的未验证项结束；Level 2 返回可恢复 blocker/HITL，不能全局 fail-open 或宣称通过；
5. Analytics 继承不通过简单删除 `tool_call_id` 条件放宽，而通过解析稳定引用恢复完整 lineage。

#### 明确不采用

1. 本批不增加 `persisted-per-project/always` 外部目录授权；先稳定 `once/session`，长期授权另行评审；
2. 不因目录文件 Grant 自动允许 `execute` 读取宿主绝对路径；文件权限不等于 shell mount 权限；
3. 不把全部 Session roots 挂入共享项目容器，也不建立长期 Session worker；
4. 不用全局 fail-open 掩盖 Goal/高风险验收基础设施故障。

### 10.12 落地状态与验收证据

截至本轮实现，§10.5–§10.8 的目标架构已落地。P2 的“删除旧 Schema/源码”仍严格受“连续两个发布周期零新调用、无 active lease、旧 checkpoint 迁移完成”的退出条件约束；当前完成的是新 Run 退出旧路由、兼容调用遥测、active lease 审计/迁移和删除门禁，而不是提前破坏旧 Session 恢复。

阶段证据：

| 阶段 | 当前实现 | 权威入口 |
|---|---|---|
| P0-A 三级验收 | 普通 Run 为 `agent`；实际 mutation 升级 `proportional`；只有显式 Goal 为 `goal` 并允许 completion loop | `harness/models.py`、`harness/coordinators.py`、`graph/deepagents_manager.py`、`harness/verification_activations.py` |
| P0-B Session Root/Broker | exact-file/exact-directory Grant 经 HostFileBroker 直读写；未授权原调用触发最窄 HITL；普通路径不再 stage | `graph/host_file_broker.py`、`graph/permissioned_filesystem_backend.py`、`graph/middlewares/workspace_path_router.py`、`graph/permission_middleware.py` |
| P0-C 稳定证据引用 | Goal/Handoff/compact 只携带 `{type,id}`；Resolver 校验来源 Run、Tool Call、digest、revision 与状态 | `harness/evidence_ledger.py`、`graph/session_manager.py`、`graph/deepagents_manager.py` |
| P1 Broker 一致性 | 单一 canonical/no-follow 权限边界、原子写、version token、统一错误、内部多文件事务、验证桥、per-Run rewind | `graph/host_file_broker.py`、`graph/permissioned_filesystem_backend.py`、`graph/middlewares/versioned_patch.py` |
| P2 兼容/外部命令 | 新 Run 隐藏 Stage/lease；旧 owner 有界恢复并记指标；一次性 offline read-only `docker run --rm` 精确挂载；项目容器按项目路径稳定命名 | `graph/middlewares/toolset.py`、`scripts/audit_legacy_external_leases.py`、`harness/workspace_backends.py`、`harness/tool_execution.py` |

24 条验收矩阵的代码级证据如下。测试名是长期回归入口，不能用“相邻测试通过”替代：

| # | 验收结论 | 直接证据 |
|---:|---|---|
| 1 | 已完成 Goal 后的 HUD 追问保持 Level 0；无 completion activity；最近交付组可解析到当前 HTML/JS | `test_run_verification_mode_is_owned_by_explicit_goal_state`、`test_non_goal_rubric_middleware_emits_no_completion_activity`、`test_delivered_artifact_registry_resolves_standalone_follow_up_without_scratch` |
| 2 | 历史 `2024.js` 不覆盖当前 `2026.js`；当前文件 hash/存在性是权威 | `test_delivered_artifact_registry_resolves_standalone_follow_up_without_scratch`、`test_follow_up_registry_rejects_deleted_or_externally_modified_targets`、`test_load_session_for_agent_excludes_cross_run_tool_output` |
| 3 | 普通 HTML mutation 动态升 Level 1，仍无 Goal/reviewer 修订循环 | `test_successful_workspace_mutation_upgrades_to_proportional_without_goal_loop`、`test_non_goal_run_does_not_enter_completion_repair_loop` |
| 4 | 显式 Goal 使用 Level 2 并冻结严格 contract | `test_run_verification_mode_is_owned_by_explicit_goal_state`、`test_explicit_goal_always_freezes_a_verification_contract` |
| 5 | Session Root 跨 Run 支持 read/ls/glob/grep/edit/create/delete，不重复申请 | `test_session_directory_grant_survives_container_rebuild_but_stays_workspace_bound` |
| 6 | 容器实例变化不使 Grant 失效；一个项目路径只有稳定项目容器，不创建 Session worker | `test_session_directory_grant_survives_container_rebuild_but_stays_workspace_bound`、`test_project_container_name_is_path_stable_and_not_session_scoped` |
| 7 | 同义并发目录请求共享 UI semantic key，批准一张恢复同义 pending | `test_concurrent_directory_requests_share_ui_semantic_key_across_runs` |
| 8 | 宿主并发变化产生 conflict，多文件事务不部分写入 | `test_multi_file_transaction_is_all_or_nothing_and_journaled`、`test_rewind_refuses_to_overwrite_concurrent_host_change` |
| 9 | 普通文件结果无 lease/staged/source-vs-draft 参数；内部 receipt 仍保留版本与事务事实 | `test_authorized_directory_uses_direct_host_file_tools_and_receipts`、`test_authorized_external_directory_glob_keeps_canonical_host_path` |
| 10 | Goal 外 Run 不执行 completion gate，也不发“发现完成条件缺口”事件 | `test_non_goal_rubric_middleware_emits_no_completion_activity`、`test_non_goal_run_does_not_enter_completion_repair_loop` |
| 11 | exact-file sibling discovery 只请求直接父目录，敏感/过宽根不自动弹卡 | `test_exact_file_sibling_discovery_requests_only_direct_parent`、`test_exact_file_sibling_discovery_never_prompts_for_broad_ancestor` |
| 12 | Read only Root 只允许 read/ls/glob/grep，write/edit/delete fail-closed；无 Grant 的绝对路径不能回落默认 Backend | `test_read_only_directory_grant_allows_search_but_not_mutation`、`test_ungranted_external_absolute_path_never_falls_through_default_backend` |
| 13 | 文件 Grant 不改变项目容器 mount 或 shell 权限；完整目录命令另走一次性 exact-root 授权 | `test_project_container_spec_has_no_docker_socket_or_host_home`、`test_external_directory_command_mounts_only_exact_root_read_only`、`test_external_directory_command_is_exact_one_time_docker_approval` |
| 14 | 验证副本位于 `/scratch/validation/<hash>`，Receipt 绑定正式 path/hash，结束即清理 | `test_broker_validation_bridge_binds_formal_hash_and_blocks_bad_bytes` |
| 15 | 新 Run 不见 Stage/lease；只有 active legacy owner 可见并被审计 | `test_legacy_lease_tools_are_visible_only_to_active_owner_and_audited` |
| 16 | `external_mutation_completed` 可直接形成 Artifact activation 并闭合交付，不依赖 `commit_external_*` 名称 | `test_broker_validation_bridge_binds_formal_hash_and_blocks_bad_bytes` |
| 17 | Run B 的稳定 analytics ref 可解析回 Run A Tool Call/query trace | `test_analytics_ref_resolves_to_authoritative_cross_run_lineage` |
| 18 | compact envelope 重复生成字节相同，`evidence_ref` 保持 `{type,id}` | `test_harness_summary_envelope_is_deterministic_and_keeps_authoritative_pending_work` |
| 19 | 伪造或 revision 错误的 result/evidence id 被拒绝 | `test_forged_or_wrong_revision_ref_is_rejected` |
| 20 | 完整旧 lineage 可迁移；缺来源记录审计为 non-inheritable | `test_legacy_complete_evidence_migrates_and_incomplete_is_audited` |
| 21 | 同一 Goal revision 的同一权威结果引用去重，不复制 payload | `test_duplicate_identity_is_deduplicated_without_copying_payload` |
| 22 | 历史 Goal analytics ref 不激活后续 standalone Level 0/1 contract | `test_historical_goal_evidence_does_not_activate_standalone_run` |
| 23 | 同一语义 gap 不受 evidence 数量增长影响，下一轮即停滞早退 | `test_completion_gate_ignores_growing_receipt_evidence_for_stagnation` |
| 24 | rewind 只处理当前 Run receipt；目标 hash 已变化时拒绝覆盖 | `test_rewind_restores_only_current_run_when_hashes_still_match`、`test_rewind_refuses_to_overwrite_concurrent_host_change` |

补充安全审计结论：HostFileBroker 现在是外部 host path 的最终 fail-closed 边界。即使上游 Router 或权限 Middleware 漏拦，未授权的绝对路径也不能再回落到默认 `FilesystemBackend`；这是 `test_read_only_directory_grant_allows_search_but_not_mutation` 在本轮验收中发现并修复的真实越权缺口。Docker 内的普通绝对路径仍表示容器路径；宿主隔离由 mount spec 保证，不能错误地把“拒绝所有容器绝对路径”当作文件权限不扩 shell 权限的证明。
