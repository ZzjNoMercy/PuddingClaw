# 语义资产、分析模型边界与业务回归方案

> 状态：待审核，**本文件不代表已进入开发**。  
> 范围：逻辑数据集完善、分析模型执行边界、版本治理与业务人员维护的测试集。  
> 原则：白盒化、AI Native、第一性原理、对抗式审查。

## 1. 背景与当前状态

工作区已经具备以下能力：

- 语义资产以 Markdown 管理度量值、维度、颗粒度、资产关联和 SQL 守卫。
- `entity_lookup` 维度具备构建任务、人工覆盖、发布和版本快照能力。
- 分析模型可选择数据资产、语义资产、资产关联、守卫和模板，并在对话中注入 DeepAgents 上下文。
- 逻辑数据集可把多个字段口径相近的表定义为虚拟纵向合并；实际读取时才展开来源数据。
- 模型注册表已校验多资产模型的语义图连通性，并注入已选关联和共同维度路径。

但当前大部分约束仍是 Prompt 级“应该遵守”。Agent 理论上仍可越过模型边界调用其他工具、读取未选资产，度量值和维度也尚未编译为可确定执行的查询计划。因此下一阶段目标不是堆叠固定工作流，而是把已声明的业务边界下沉为可检查、可追溯的运行时策略。

## 2. 明确待开发项

### 2.1 逻辑数据集完善

| 项目 | 当前状态 | 待开发结果 |
| --- | --- | --- |
| 覆盖范围 | `dataset.json.coverage` 目前为空 | 后端基于来源 Profile 提供时间、区域、品牌等可用覆盖摘要；未知时明确标记，不由 LLM 编造。 |
| 新鲜度 | 仅记录定义刷新时间 | 分别记录定义刷新、来源 Profile 更新时间和来源内容指纹/版本。 |
| 统计信息 | `rows_estimate` 复用来源元数据 | 明确“估算”或“已验证”；可由异步 Profile 任务更新。 |
| 定义编辑 | 可创建、追加、刷新、查看 | 可编辑名称、描述、标签、路由意图、单期直查策略；创建、HITL 与编辑使用同一份字段契约。 |
| 路由说明 | 已注入模型摘要 | 将可用场景、原始表直查条件、字段契约和覆盖范围稳定暴露给 Agent 与 Trace。 |
| 性能策略 | 虚拟读取时仍需加载来源 | 保持默认虚拟；后续可选异步物化/缓存，不改变逻辑资产 ID 和语义。 |

### 2.2 语义资产执行与质量

| 项目 | 当前状态 | 待开发结果 |
| --- | --- | --- |
| 可执行语义 | 度量值/维度主要是文档提示 | 逐步为高频资产增加可验证的结构化约束或查询片段，先覆盖配置率、上市时间、价格段、车系。 |
| 运行时引用 | 工具和模型会注入相关正文 | 建立按问题、模型和资产选择的精确加载策略，避免全量注入，也避免只读预览行误判。 |
| 大型实体维度 | active crosswalk 可用 | 为大规模 lookup 提供查询索引/缓存与覆盖率、候选、停用实体的可观测性。 |
| 资产关系执行 | 模型可声明并注入关系 | 在执行前校验实际联合是否使用已声明 `dimension_binding` 或 `direct_join`，禁止仅凭同名字段猜测 Join。 |

### 2.3 分析模型治理

| 项目 | 当前状态 | 待开发结果 |
| --- | --- | --- |
| 模型边界 | Prompt 中要求优先遵守 | 将模型选择转换为工具调用前的有效访问策略，成为可执行约束。 |
| 模型版本 | 编辑后立即刷新 registry | 提供 draft / publish / active / snapshot / rollback，模型变更可复现。 |
| 模板运行 | 模板是模型内文件和提示 | 后续提供模板选择、参数补齐、产物记录和重跑能力；不把模型变成固定后端流水线。 |
| 模型验证 | 仅有开发级测试 | 每个模型可绑定业务测试集和发布前回归结果。 |

## 3. 分析模型边界控制策略

### 3.1 责任边界

| 层级 | 责任 | 不负责 |
| --- | --- | --- |
| Prompt / 模型 Playbook | 解释业务目的、口径、推荐路径、输出结构 | 不能单独作为安全或边界控制。 |
| Analytics Policy Middleware | 汇总当前模型、用户、项目和会话授权，决定本轮能调用什么资产与关系 | 不生成业务结论。 |
| 工具内校验 | 对 SQL、Pandas、文件读取做最终引用校验 | 不推测用户意图。 |
| Trace | 记录模型、策略、临时授权、实际资产和关系路径 | 不替代策略执行。 |

### 3.2 有效访问策略

模型不是权限系统，但模型选择应参与本轮允许范围的计算：

```text
effective_policy
  = 用户角色允许范围
  ∩ 项目允许范围
  ∩ 当前分析模型选择范围
  ∩ 本会话临时授权范围
```

第一版如尚未引入角色系统，可将前两项视为“项目默认全部允许”，但模型范围与会话授权必须生效。

示例：

```json
{
  "model_id": "auto_industry_analysis",
  "allowed_table_assets": ["table_asset:tbl_sales_2023"],
  "allowed_database_tables": [
    "insight_data.vehicle_params_wide",
    "insight_data.vehicle_params"
  ],
  "allowed_semantic_assets": [
    "dimension:vehicle_series",
    "dimension:launch_time",
    "measure:config_rate"
  ],
  "allowed_relation_ids": [
    "relation:insurance_sales_to_vehicle_series"
  ],
  "allowed_guardrail_ids": ["config_rate_model_key_group"],
  "allowed_paths": ["/knowledge/imported/..."],
  "allow_unlisted_assets": false
}
```

### 3.3 中间件拦截点

`AnalyticsPolicyMiddleware` 位于 DeepAgents 的工具调用链中，在工具开始前解析当前会话的 `effective_policy`：

| 工具类别 | 中间件和工具必须检查 |
| --- | --- |
| `database_schema_inspect` | 只返回允许的数据源和表。 |
| SQL 生成 | 只提供允许表的 DDL、模型选择的语义资产、关联和守卫。 |
| SQL 校验/执行 | 解析实际引用的 schema、表、函数和 Join 路径；拒绝未授权资产。 |
| `pandas_knowledge_query` | 只加载模型选择的表资产、逻辑数据集及其允许来源。 |
| `read_file` / `ls` | 限制为模型声明路径、会话附件、必要 Skill 路径和临时授权路径。 |
| `semantic_entity_lookup` | 只检索模型已选择或会话明确授权的维度。 |

对于跨资产分析，只允许：

1. 单资产查询；
2. 模型已选择的 `direct_join`；
3. 两个已选择资产经共同、已发布维度的路径。

若发现用户问题确实需要模型外资产，Agent 不得静默绕过，应发起 HITL 临时扩展授权。

### 3.4 临时扩展授权

对话中展示授权卡：

> 当前模型未包含“2023 年上险量”。是否仅在本轮分析中临时加入？

用户确认后写入 `session_grant`，仅作用于当前会话/有效期；Trace 记录：

- 选定模型版本；
- 原模型边界；
- 请求扩展的资产和原因；
- 用户确认时间；
- 实际调用的工具、表、关系路径。

模型本身不会被临时授权自动修改。

### 3.5 Trace 最小记录

每个受策略影响的工具调用增加：

```json
{
  "policy_source": ["model:auto_industry_analysis", "session_grant:sg_xxx"],
  "resolved_assets": ["table_asset:tbl_sales_2023", "insight_data.vehicle_params_wide"],
  "relation_path": ["sales -> vehicle_series <- vehicle_config"],
  "decision": "allow",
  "denied_assets": []
}
```

拒绝时应返回可理解原因，例如“该模型未选择订单表”，而不是泛化为工具错误。

## 4. 业务测试集

### 4.1 定位

测试集由数据分析业务人员维护，不要求写 SQL，也不要求所有问题维护精确数值。它是发布前的业务验收资产，用来防止修改语义、模型或数据集后已有分析口径悄然漂移。

测试可挂在：

- 度量值、维度、颗粒度；
- 逻辑数据集；
- 资产关联；
- 分析模型；
- 跨上述对象的端到端场景。

### 4.2 最小测试用例格式

```yaml
id: config_rate_ev_air_suspension_2026
name: 2026 年纯电车型空气悬架配置率
scope:
  model: auto_industry_analysis
  semantic_assets: [measure:config_rate, dimension:launch_time]
question: 2026 年纯电车型空气悬架配置率是多少？
expectations:
  required_assets: [insight_data.vehicle_params_wide, insight_data.vehicle_params]
  required_terms: [总款型数, 搭载款型数, 配置率]
  required_rules: [config_rate_model_key_group]
  forbidden_behaviors: ["按 car_name 单列去重", "从款型名称推断上市时间"]
  result_contract:
    columns: [total_count, equipped_count, config_rate_pct]
```

仅在基准数据稳定的小样本中额外增加：

```yaml
expected_value_range:
  config_rate_pct: [20, 30]
```

### 4.3 运行与判定

测试执行必须同时检查：

1. 模型是否加载了预期版本；
2. 是否只使用允许资产；
3. 是否沿声明关系联合；
4. SQL/Pandas 是否命中必需守卫与禁止规则；
5. 结果是否满足结构、解释和可选的值范围；
6. Trace 是否能解释失败原因。

测试失败不自动篡改语义资产或模型，只给出差异和建议，由人审核。

## 5. 已确认的实施优先级

以下顺序是后续开发的默认路线。除非出现阻断性缺陷，不跳过前一阶段直接建设后一阶段。

### 阶段 1：补齐逻辑数据集

- Profile、时间覆盖、新鲜度、定义编辑、路由摘要。
- 先让数据资产本身可理解、可验证。
- 虚拟数据集保持按需读取；物化只是后续可选的性能实现，不改变语义。

### 阶段 2：补齐模型边界与权限控制

- 模型选了哪些数据资产、语义资产、关联和守卫，工具层就只能在这个范围内工作。
- 使用 Policy Middleware、工具最终校验、关系路径校验、会话临时授权和 Trace 落实该约束。
- 这是避免 Agent “看了模型但仍自己乱找表”的关键。

### 阶段 3：补齐模型发布与版本

- 语义资产、关联、逻辑数据集变化影响模型前，要有版本快照、依赖检查和回滚能力。
- 模型至少具备 `draft`、`publish`、`active` 和 `snapshot` 生命周期。
- 这一步应在测试体系之前或同步做，否则测试结果没有稳定的版本对象可对应。

### 阶段 4：开发业务人员维护的测试集

- 用户维护自然语言问题、适用模型、预期口径/关键约束，以及可选的期望结果或结果范围。
- 系统执行时检查：是否选对资产、是否走已声明关系、SQL 是否符合守卫、结果结构/关键数值是否符合预期。
- 测试集可以分别挂在语义资产、逻辑数据集、资产关系和分析模型下，也可以有跨对象的端到端测试。
- 第一版优先支持结构和口径断言，再逐步加入稳定基准数据的数值范围断言。

### 后续增强：可执行语义与性能层

- 把高频度量值/维度逐步结构化为可验证的执行约束，而不是一次性试图编译所有 Markdown。
- 对高频逻辑数据集增加异步物化或缓存，保持逻辑资产定义不变。

## 6. 非目标

- 不把分析模型改造成传统 BI 的固定 ETL 或预建星型模型。
- 不让 Agent 凭字段名相同或相似自行跨未关联资产 Join。
- 不要求业务人员维护 SQL、代码或全量精确预期结果。
- 不在第一版引入复杂 RBAC；先落实模型边界和会话临时授权。

## 7. 审核确认项

在开发前需要确认：

1. 模型边界是否从第一版就对工具调用做强制拦截，而非仅告警？
2. 临时扩展授权的有效期是否限定为当前会话，还是允许配置时长？
3. 逻辑数据集 Profile 是否允许异步扫描大型来源，还是仅从已有 Profile 汇总？
4. 模型版本发布是否与语义资产发布解耦，允许模型引用最新已发布语义资产？
5. 业务测试集是否优先挂在分析模型下，再支持语义资产级复用？

## 附录 A：未来企业级数据权限方案（备案，不进入当前开发）

### A.1 当前范围与设计约束

当前 PuddingClaw 是本地、个人 Agent。近期开发只落实“分析模型边界”：用户选定模型后，工具只能在该模型声明的数据资产、语义资产、关联和守卫范围内工作。

本附录描述未来多人/企业部署所需的完整权限方案。它不应改变现有模型文件的可迁移性，也不应把权限判断分散进 Prompt。当前实现只需为它保留稳定的策略输入、工具校验点和 Trace 字段，不提前建设角色、账号或权限后台。

### A.2 第一性原理

1. **模型不是权限**：模型选择的是业务分析范围；不能因模型选中某资产就获得读取权限。
2. **权限不写入可迁移资产**：`model.md`、`dimension.md`、`relation.md` 和 `dataset.json` 不包含用户、角色或组织 ID。
3. **执行点强制，而非 Prompt 自觉**：Prompt 用于解释，数据读取、SQL 执行、导出和结果分页必须由策略层强制。
4. **权限沿数据流继承**：查询结果、导出文件、逻辑数据集和派生产物不能成为绕过来源权限的旁路。
5. **拒绝优先且可解释**：缺少权限或关系时，拒绝原因必须明确展示为“缺少哪个资产/关系/字段权限”，而不是伪装成查询失败。

### A.3 统一资源模型

未来将所有可操作对象统一为资源，而不是只给数据库表做权限：

| 资源类型 | 示例 | 常见操作 |
| --- | --- | --- |
| `table_asset` | Excel、CSV、逻辑数据集 | discover、read、manage、share、export |
| `database_table` | `insight_data.vehicle_params_wide` | discover、read、query、manage |
| `semantic_asset` | 维度、度量值、颗粒度、关系、守卫 | discover、read、manage、publish |
| `analytics_model` | 汽车行业综合分析 | discover、run、manage、publish、share |
| `query_result` | `result_id` 结果集 | read、export、delete |
| `artifact` | HTML 报告、CSV 导出 | read、export、delete、share |

资源标识保持项目内稳定，例如 `table_asset:tbl_xxx`、`db_table:insight_data.vehicle_params_wide`、`dimension:vehicle_series`；展示名称可变化，不参与授权判断。

### A.4 权限主体与权限模型

权限主体可分为用户、用户组、服务账号和项目角色：

```text
subject = user | group | service_account | project_role
```

采用“RBAC 提供基础能力 + ABAC 表达数据条件 + 资源 ACL 精确授权”的组合：

- **RBAC**：Owner、Editor、Analyst、Viewer 等角色定义默认操作能力。
- **资源 ACL**：为某用户/组额外授予具体资产的 `discover/read/manage/share/export`。
- **ABAC/策略条件**：行级、列级、环境、时间或用途限制，例如仅可读 `region in [华东, 华南]`。

最小授权动作集合：

```yaml
permissions:
  - discover  # 可在选择器、目录和模型配置中看见
  - read      # 可读取数据或语义正文
  - query     # 可用于 SQL/Pandas 分析
  - manage    # 可编辑、删除、刷新定义
  - publish   # 可将草稿变为 active 版本
  - export    # 可生成或下载导出产物
  - share     # 可向其他主体授权
```

### A.5 有效策略与模型兼容方式

模型声明的是“希望使用什么”，权限层计算的是“本轮实际允许什么”：

```text
effective_policy
  = project_policy
  ∩ subject_permissions
  ∩ selected_model_scope
  ∩ session_grants
```

其中：

- `project_policy`：租户/项目级总限制；本地个人模式下默认全允许。
- `subject_permissions`：用户、组、角色和资产 ACL 的最终结果。
- `selected_model_scope`：模型 `model.md` 选择的资产、语义资产、关系和守卫。
- `session_grants`：用户明确确认的短期“模型外扩展”；它**只能缩小或扩展模型范围，不能突破用户本来没有的权限**。

模型加载后应产生运行时专用对象，不回写模型文件：

```json
{
  "model_id": "auto_industry_analysis",
  "requested_assets": ["table_asset:insurance_sales", "db_table:insight_data.vehicle_params_wide"],
  "granted_assets": ["db_table:insight_data.vehicle_params_wide"],
  "denied_assets": [
    {"ref": "table_asset:insurance_sales", "reason": "missing_read_permission"}
  ],
  "blocked_relations": ["relation:insurance_sales_to_vehicle_series"],
  "model_readiness": "blocked"
}
```

若某个模型的关键资产或关系不可用，必须标记模型为 `blocked` 或 `degraded`，不能让 Agent 以不完整数据悄然回答完整跨源问题。

### A.6 策略执行架构

```text
身份 / 项目 / 角色 / ACL / 行列策略
                ↓
      Policy Resolver（生成 effective_policy）
                ↓
    Analytics Policy Middleware（工具调用前）
                ↓
 SQL / Pandas / 文件 / 导出工具内最终校验
                ↓
       结果、导出和 Artifact 的继承策略
```

`AnalyticsPolicyMiddleware` 应是未来唯一的策略编排入口。它负责将模型、用户和会话条件转为工具可消费的约束；具体工具不得重新实现不同版本的授权逻辑。

工具侧最终校验要求：

| 执行路径 | 强制项 |
| --- | --- |
| Schema 探测 | 只返回具有 `discover`/`read` 的表和字段。 |
| SQL 生成 | 只提供允许表、字段、关系、维度和守卫。 |
| SQL 验证/执行 | 解析实际表、列、函数和 Join；注入行策略并拒绝越权引用。 |
| Pandas / 逻辑数据集 | 加载前校验来源；加载后执行列投影、行过滤；虚拟数据集要求每个实际来源均可读。 |
| 文件工具 | 只允许模型路径、会话附件、显式授权路径与系统必需路径。 |
| 查询结果/导出 | 继承生成它的 `effective_policy`，不可凭 `result_id` 或文件路径绕过。 |

### A.7 行级、列级与派生资产

行级和列级策略必须在执行引擎处落地，而不交给 LLM：

```yaml
resource: db_table:crm.orders
subject: group:east_china_analysts
permissions: [discover, read, query]
row_policy:
  expression: "region IN ('华东', '华南')"
column_policy:
  allow: [order_id, order_date, region, amount, product_id]
  deny: [customer_phone, identity_number]
```

- PostgreSQL：通过 SQL AST 校验、投影限制和强制 WHERE 谓词实现。
- Pandas/文件：先筛列，再筛行，之后才进入生成代码或执行表达式。
- Profile、样例值、Embedding、语义检索和 Trace 中同样不得泄露无权字段或行的内容。
- 逻辑数据集、结果集、导出和报告使用“来源策略交集”；若任一来源不允许，应拒绝或显式生成脱敏聚合结果，不能默认完整暴露。

### A.8 临时授权与 HITL

当问题需要模型外资产时：

1. 先检查主体是否对该资产有 `discover/read/query`；没有则只提示联系管理员。
2. 有基础权限但模型未选择时，展示 HITL 卡片请求“仅本轮加入”。
3. 确认后创建有过期时间的 `session_grant`，默认仅当前会话有效。
4. 临时授权不修改模型、语义资产或关系文件，也不能自动升级为长期权限。

### A.9 审计与 Trace

每次调用记录策略决策，但不记录被屏蔽的敏感值：

```json
{
  "subject": "user:example",
  "policy_sources": ["project:default", "role:analyst", "model:auto_industry_analysis"],
  "resolved_assets": ["table_asset:insurance_sales"],
  "relation_path": ["sales -> vehicle_series <- vehicle_config"],
  "decision": "allow",
  "applied_row_policies": ["region_scope_v1"],
  "redacted_columns": ["customer_phone"],
  "denied_refs": []
}
```

审计日志需要可按会话、主体、资产、模型版本、拒绝原因和导出动作检索。

### A.10 对当前开发的兼容要求

当前不实现企业权限后台，但后续代码应遵守以下约束：

1. **模型 registry 输出可接收运行时 `effective_policy`**，不要把“可用资产”硬编码为 model frontmatter。
2. **每个数据工具接受统一的 `policy_context`**；个人模式传入全允许策略即可。
3. **资源统一用稳定 ref**，禁止用展示名称做关系、授权或结果访问判断。
4. **查询结果和 Artifact 保存 origin metadata**：模型版本、来源资产 ref、关联路径和 policy fingerprint。
5. **逻辑数据集在展开来源时逐一校验**，不要只校验逻辑数据集自身。
6. **所有拒绝都有机器可读 reason code**，便于 UI、HITL 和 Trace 统一呈现。
7. **静态模型/语义资产保持无用户身份字段**，保证可导入、导出和跨项目迁移。

### A.11 未来实施阶段

| 阶段 | 内容 | 前置条件 |
| --- | --- | --- |
| P0 | 当前模型边界、工具范围校验、会话临时扩展 | 不需要多用户系统。 |
| P1 | 统一资源 ref、`policy_context`、结果来源元数据 | P0。 |
| P2 | 用户/角色/资产 ACL，`discover/read/manage/export` | 用户体系和项目空间。 |
| P3 | 列级与行级策略，SQL/Pandas 双执行器一致实现 | P2。 |
| P4 | 审计、审批流、服务账号、外部身份源集成 | P2/P3。 |

当前开发仅按 P0 推进，并按 A.10 保留 P1 以上的扩展接口。
