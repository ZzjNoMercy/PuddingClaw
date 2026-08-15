# AI Native 语义资产与分析模型对话共创方案

> 状态：**P0 五类单文件纵切已落地并通过组合回归**
>
> 日期：2026-08-12
>
> 范围：度量值、维度、颗粒度、资产关联、分析模型的发现、创建、修改、验证、发布、重构与废弃
>
> 前置条件：[用户 Home、Session 与 Skill 迁移方案](./2026-08-11-user-home-sessions-and-skills-migration.md) 已落地
>
> 方案关系：收敛此前的语义资产统一建模、分析模型边界与业务回归设计；与历史方案冲突时以本文及用户 Home 迁移契约为准

## 1. 决策摘要

PuddingClaw 下一阶段不能继续以“用户在表单或 Markdown 编辑器中逐项编写定义”为主要维护方式。核心产品形态调整为：

> 用户通过自然语言与 Agent 共创全部语义资产和分析模型；Agent 主动检查数据、解释业务决策、逐步引导用户补齐必要信息，并通过结构化草稿、ChangeSet、验证和显式确认安全发布。

这不是只增加一个生成 Markdown 的 Prompt，而是增加一套以 Skill + Toolset 为核心、带证据读取和安全发布的 Agent 创作流程：

```text
用户目标 / 历史问题 / 数据 Profile / 现有资产 / Query Trace
                             │
                             ▼
                    Semantic Steward Agent
               发现事实、识别歧义、逐步引导决策
                             │
                             ▼
        Registry Discovery：列举/检索候选、读取正文、比较复用/修改/新增
                             │
                             ▼
             LLM-readable Authoring Brief（必需的临时控制提纲）
                 整理事实、决策、证据与未决问题
                             │
                             ▼
                  Agent 编写 staging Markdown 草稿
                             │
                             ▼
             Backend 校验并修缮 frontmatter 机器投影
                             │
                             ▼
              digest-bound Prepared Plan + 影响说明
                             │
                             ▼
              静态校验 / 数据探测 / 案例回放 / Diff
                             │
                             ▼
                       用户显式确认发布
                             │
                             ▼
     $PUDDINGCLAW_HOME/definitions/{semantic-assets,analytics-models}
```

本文固定采用“**Schema/Skill 引导 + 必需 Authoring Brief + 完整 Markdown 候选 + digest-bound Prepared Plan + Home Markdown 唯一事实源**”。Authoring Brief 是给 LLM 看的临时控制提纲，用来证明必要主题已经检查、未决问题已清零；它可以整理事实、决策和证据，但不参与运行时、不编译成定义，也不能脱离最终 Markdown 独立发布。P0 冻结单文件候选、基线、校验、预览和发布摘要；只有在单对象纵切证明需要后，才增加跨 Session ChangeSet，不把控制面当作第一阶段前置。

用户的主要创作面是自然语言对话和 Markdown 正文，不是 YAML、Schema 或领域表单。Agent 负责把用户的业务表达整理为清晰、完整、可维护的正文；frontmatter、稳定 ID、引用语法、版本和文件位置由 Agent/Toolset 自动处理。技术用户可以查看原始 Markdown，但普通用户默认只审核渲染后的正文、业务变化和风险。任何会改变运行时业务行为的 frontmatter 都必须在正文中有等价表达；后端只能自动修缮机器表示，不能在正文之外创造新的业务口径。

## 2. 为什么提高到 P0

现有智能问数已经具备数据 Profile、语义资产、分析模型、关系、Guardrail、跨源维度构建和统一语义运行时，但资产维护仍高度依赖人工填写和直接编辑：

- 前端 REST API 可以创建语义资产和分析模型，但创建模板仍容易留下占位说明或不完整业务规则。
- 当前专用 Agent 工作流主要覆盖复杂维度构建，没有覆盖度量值、颗粒度、关系和分析模型的完整对话式创作。
- 主 `/api/agent` 文件系统已经把 `/semantic-assets` 和 `/analytics-models` 挂载为只读；遗留 `/api/chat` 仍曾暴露旧写工具，需要移除其定义目录白名单并增加回归测试。
- 自然语言正文、frontmatter、模型依赖和运行时实际行为可能发生漂移。
- 重复资产、缺失关系、无引用资产和版本影响需要人逐个排查，无法从 Query Trace 和失败案例持续演化。
- Measure 和 Grain 的计算、分子分母、单位、唯一键、去重和上卷规则主要存在于 Markdown 正文。Agent 缺少统一的 Markdown Schema、类型写作指南和逐步决策流程，因此同类定义的完整度和表达方式不稳定。
- 现有专用发布流程尚未覆盖 Measure 等对象，无法形成“完整候选 → 检查 → 预览 → digest 绑定确认 → 发布”的统一 Agent 创作边界。

如果这一层不先补齐，继续增加语义类型、模板和自动分析能力，只会扩大人工维护成本和口径漂移面。因此应先补齐面向 Agent 的写作上下文、Skill 和 Toolset，而不是先建设新的语义存储或编译体系。

因此实施从 **Markdown Authoring Contract** 开始：为五类对象定义 frontmatter 元数据、必需章节、决策清单、引用方式和验证要求，供 Agent 在写作前读取。Schema 约束 Markdown 的形态，不取代 Markdown，也不要求把全部业务语义搬入结构化 frontmatter。

### 2.1 本轮评审结论的处理

| 评审问题 | 本方案决策 | 主要落点 |
| --- | --- | --- |
| Measure/Grain 缺少统一机器上下文 | 增加 Markdown Authoring Schema；允许 LLM-readable Authoring Brief，不引入权威计算 IR | §8、P0-0 |
| P0-1 范围过大 | 先做 Measure 单对象纵切，只提供 prepare/publish 两个工具；验证后再横向扩展 | §10、§14 |
| Dimension Publisher 能力边界 | 只复用 staging、atomic write/copy 和快照 primitive，不建设通用 Registry 事务框架 | §9.4 |
| Monaco 与其他写入通道 | 本 P0 只禁止 Agent 绕过 Toolset；人工 Markdown 编辑继续存在，并以 digest 使 Agent 草稿 stale | §10.4、§13 |
| Markdown 往返可能破坏正文 | Brief 只指导写作，不编译正文；Agent 在 staging 中直接编辑完整 Markdown | §8.6 |
| 跨 Session 未定义 | P0 Measure 计划与创建 Session 绑定且 24 小时过期；owner-scoped ChangeSet 后置 | §5.4、§9.2 |

## 3. 产品目标

### 3.1 用户最终能做什么

用户不需要先理解 frontmatter、Registry 或文件目录，只需在对话中表达业务目标，例如：

- “帮我定义成交均价，销量为 0 时不要返回 0。”
- “我们需要一个渠道维度，直营网点和加盟店要归成两类。”
- “配置率按款型计算和按车系计算分别是什么意思？帮我建正确的颗粒度。”
- “把订单和退款表关联起来，但退款可能一单多条。”
- “创建一个汽车市场与产品配置联合分析模型。”
- “这个销量资产是不是重复了？合并会影响哪些模型？”
- “根据最近失败的查询，把模型缺少的语义资产补齐。”

Agent 应完成以下工作：

1. 主动读取相关数据资产、Profile、字段样例、已有语义资产、模型和依赖。
2. 区分可以从证据确定的事实和必须由用户决定的业务口径。
3. 给出推荐选项及影响，每轮只推进一个关键决策或一个紧密相关的小决策组。
4. 持续维护 staging Markdown 正文和决策摘要，让用户随时看到“已确认、系统推断、仍待决定”的内容。
5. 自动产生样例问题、反例和发布前验证，不要求用户编写 YAML、SQL 或测试代码。
6. 在最终发布前默认展示正文预览、业务变化、依赖影响、风险和验证结果；原始 Markdown Diff 作为可展开的高级信息。
7. 发布后返回稳定资产 ID、版本、使用方式和审计回执。

### 3.2 非目标

- 不把对话变成长表单逐项盘问。
- 不要求用户理解或填写 frontmatter、Schema、逻辑 ID、引用语法或 ChangeSet 字段。
- 不让 Agent 凭字段名或常识静默决定关键业务口径。
- 不允许 Agent 通过通用 `write_file` 绕过 ChangeSet 直接发布正式定义。
- 不在 P0 将所有 Markdown 一次性编译成可执行 DSL。
- 不在 P0 建设组织级多人审批、RBAC 或复杂治理门户。
- 不让分析模型复制一份私有语义资产；模型仍然引用共享资产。

## 4. 权威边界与目录契约

用户目录迁移后，用户可变定义不得回写源码或安装目录。

```text
<package>/backend/skills/semantic-steward/
  SKILL.md                         # 内置只读工作流，可随产品发布
  references/                     # Markdown Schema、ChangeSet 和引导策略参考

$PUDDINGCLAW_HOME/
  definitions/
    semantic-assets/              # 已发布语义资产的唯一事实源
    analytics-models/             # 已发布分析模型的唯一事实源
    sql-guardrails/
  data/
    semantic-steward/
      change-sets/                # 可导出的提案和应用回执
      evaluations/                # 验证、回放和健康检查报告
      snapshots/                  # 发布前快照和回滚材料
  state/
    semantic-steward/             # Lease、Job、幂等和恢复状态
  tmp/
    semantic-steward/             # 可丢弃 Markdown staging
```

约束：

- Agent 只使用逻辑 ID 和虚拟路径，不接收或持久化宿主绝对 Home 路径。
- Backend 使用 `PuddingClawPaths.user_definitions()` 解析真实目标。
- ChangeSet 中记录 `definition_root: user`，不记录 `/Users/...`、`/home/...` 或 `~/.puddingclaw`。
- Skill 目录只保存工作流和静态参考，不保存用户草稿、评测结果或版本快照。
- 用户 Fork 的 Steward Skill 可以位于 `$PUDDINGCLAW_HOME/skills/`，但仍只能通过相同的受控 Backend 工具发布定义。
- 已迁走的 `backend/semantic-assets` 和 `backend/analytics-models` 不得作为 fallback、样例写入目标或兼容事实源。

## 5. 对话式共创原则

### 5.1 Agent 主动做证据工作

在向用户提问前，Agent 应先完成安全的只读检查：

- 搜索名称、别名、描述和计算结构相似的现有资产。
- 读取候选数据资产的 Profile、字段类型、空值率、distinct 样例和时间覆盖。
- 检查当前分析模型、关系图、Guardrail 和模板是否已有可复用定义。
- 检查历史 Query Trace、用户修正和评测失败是否能解释需求。

能够从系统证据确定的内容不得反问用户。例如字段类型、已有资产 ID、表中是否存在某列、模型当前引用了哪些关系，都应由 Agent 自己检查。

### 5.2 每轮只推进必要决策

Agent 不一次抛出十几个技术问题。每轮应：

1. 简短说明已经确认的事实。
2. 提出当前最关键、会改变后续方案的业务问题。
3. 给出推荐答案和选择后的影响。
4. 接受自然语言修正，而不是要求用户填写 schema。

示例：

> 我查到销售表同时有“成交金额”和“指导价×销量”。建议成交均价使用实际成交金额 ÷ 销量，因为后者是指导价口径。你希望采用实际成交金额，还是业务上必须使用指导价？

### 5.3 始终显示决策归属

草稿中的字段至少分为：

| 状态 | 含义 | 发布要求 |
| --- | --- | --- |
| `observed` | 从 Profile、Catalog 或已发布定义确定 | 可自动带入 |
| `inferred` | Agent 根据证据推荐 | 发布前必须可见 |
| `confirmed` | 用户已明确确认 | 可作为业务权威 |
| `unresolved` | 仍有多个合理口径 | 阻止发布 |

Agent 不能把 `inferred` 在无提示情况下提升为 `confirmed`。

### 5.4 用户可以中断和恢复

对话最终需要支持跨 Session 恢复，但这不是当前单文件发布协议的前置。五类对象都将完整候选、基线 digest、校验结果和预览冻结为短期 Prepared Plan：

```yaml
plan_id: semantic-plan-...
session_id: session_a
logical_path: semantic-assets/measures/average-price/measure.md
baseline_digest: sha256:...
candidate_digest: sha256:...
plan_digest: sha256:...
expires_at: ...
```

约束：

- Prepared Plan 只用于“预览后发布”控制，不是语义定义；过期或丢失不会影响已发布 Markdown。
- 当前计划与创建 Session 绑定，不能被另一个 Session 静默发布。
- 发布必须同时匹配 `plan_digest`、`candidate_digest` 和 active Markdown 的 `baseline_digest`。
- 用户需要跨 Session 继续时，Agent 重新读取已发布 Markdown 和对话摘要，再生成新计划；不会覆盖其间的人工编辑。
- 当真实使用证明长周期草稿恢复是高频需求后，再引入 owner-scoped ChangeSet、revision 和显式接管，不能预先把 lease/RBAC 做成 P0 基础设施。

## 6. 各类资产的引导契约

所有类型共享“理解目标 → 检查复用 → 数据落地 → 业务决策 → 示例/反例 → 草稿 → 验证 → 确认发布”，但具体决策不同。

### 6.1 度量值 Measure

Agent 至少引导确认：

- 指标解决什么问题，和已有指标有什么区别；
- 分子、分母、聚合函数和计算顺序；
- 默认统计颗粒度及允许的切换颗粒度；
- 时间口径、币种或单位；
- 空值、零分母、负数、退款和重复记录如何处理；
- 默认筛选、排除规则和适用数据资产；
- 必须报告的口径说明与结果字段；
- 正例、边界例和禁止行为。

对话示例：

```text
用户：帮我建成交均价。
Agent：我找到订单表的“成交金额”和“销售数量”，也发现已有“指导价”字段。
       建议定义为 SUM(成交金额) / SUM(销售数量)，而不是 AVG(单行价格)，
       这样多数量订单不会被等权。退货数量当前为负数，你希望净额口径包含退货吗？
用户：包含退货，销量为 0 时返回空。
Agent：已确认净成交口径和零分母规则。下一步我会检查币种是否唯一，
       然后生成度量草稿和三个验证案例。
```

### 6.2 维度 Dimension

Agent 先根据证据推荐 `resolution_mode`：

- `source_field`：直接使用已确认来源字段；
- `derived`：由字段和稳定业务规则推导；
- `entity_lookup`：跨来源解析到规范实体；
- `calendar_lookup`：映射到统一日期和周期成员。

随后引导确认：

- 业务含义、成员范围、显示名和别名；
- 来源字段及数据角色；
- 是否需要规范成员、层级、排序或分类；
- 未知值、其他值、空值和停用成员策略；
- 跨来源时的 canonical universe 和唯一键；
- 枚举成员和分类映射是否需要强一致 Guardrail；
- 刷新、人工审核和覆盖率边界。

`entity_lookup` 不在对话中直接生成全量映射。Steward 负责形成构建意图和字段契约，再调用 `build-semantic-dimension` 完成 staging、审核和发布。

### 6.3 颗粒度 Grain

Agent 至少引导确认：

- 一个统计对象究竟是什么；
- 唯一键或组合键；
- 同一对象多行的产生原因；
- 聚合前和 Join 前需要在哪一层去重；
- 从细粒度上卷到粗粒度时的判定规则；
- 慢变属性、时间快照或多版本记录如何选择；
- 哪些 Measure 默认或禁止使用该颗粒度。

Agent 应通过样例数据探测唯一性，而不是只接受字段名。例如声明 `order_id` 唯一前，应运行非破坏性的 distinct/duplicate 检查。

### 6.4 资产关联 Relation

Agent 先判断应使用：

- `dimension_binding`：数据资产接入已发布维度；
- `direct_join`：两个资产存在稳定、可解释的业务键。

Agent 至少引导确认：

- 两端业务角色与来源资产；
- 字段映射和规范输出键；
- 一对一、一对多、多对一或多对多基数；
- 默认 Join 方向；
- 两端颗粒度和重复计数风险；
- 空键、未匹配、冲突和覆盖率策略；
- Join 前聚合或去重要求；
- 关系失效时应该阻断还是降级。

系统必须用数据探测验证声明的基数。发现右表键不唯一时，不得仍发布 `many_to_one`。

### 6.5 分析模型 Analytics Model

Agent 不从空白模板开始让用户填写字段，而是围绕“用户希望解决哪些问题”组装模型：

1. 收集模型目标、典型问题和默认业务范围。
2. 根据问题检索候选数据资产、Measure、Dimension、Grain、Relation 和 Guardrail。
3. 解释复用与新建建议；缺少语义资产时，在同一 ChangeSet 中创建依赖草稿。
4. 构建并验证资产关系图，确保多资产模型连通。
5. 确认默认过滤、分子分母、跨源覆盖率和输出要求。
6. 根据明确的交付意图选择模板；不为普通问答强制模板。
7. 自动生成业务测试问题、必需资产、禁止行为和结果契约。
8. 展示模型将允许使用的资产范围和缺失依赖。

分析模型共创覆盖 `model.md`、模型专属 `references/`、模板选择与路由规则，以及必要的模板资源。涉及复杂 HTML 视觉模板时，Steward 应调用专门的设计 Skill 生成模板候选，再把确认后的资源纳入同一个模型 ChangeSet；不能让用户为了完成模型而退出对话手工补文件。

当用户确认的业务不变量需要确定性 SQL 拦截时，Steward 应编排 `sql-guardrail-designer` 创建或更新 Guardrail，再由模型引用它。Steward 负责需求、依赖和统一发布影响，不复制 Guardrail 设计器的专业流程。

模型草稿可以引用同一 ChangeSet 中尚未发布的新资产，但发布事务必须按依赖拓扑排序，并保证要么全部成功，要么不切换任何 active 定义。

### 6.6 修改、重构与废弃

编辑已有对象前，Agent 必须先回答：

- 哪些模型、资产、Guardrail、模板和测试引用它；
- 这是补丁、兼容扩展还是破坏性口径变化；
- 是否需要 SemVer major/minor/patch 变化；
- 历史查询和导出项目会不会改变；
- 是否需要替换引用、迁移别名或保留 deprecated redirect；
- 是否有回滚快照。

合并重复资产、修改分母、改变实体唯一键、修改关系基数、删除或废弃资产均属于高影响操作，必须显式确认。

## 7. 对话状态机

```text
discovering
    ↓
grounding             读取 Profile、已有资产和依赖
    ↓
eliciting_decisions   逐步确认关键业务语义
    ↓
draft_ready           Markdown 草稿完整，无 unresolved 决策
    ↓
validating            静态检查、数据探测、回放
    ├─→ needs_revision
    │       └──────────────→ eliciting_decisions / draft_ready
    ↓
preparing_publication 冻结验证结果、审批要求和 prepared plan
    ↓
awaiting_approval     展示并批准绑定 prepared plan 的 Diff、影响、风险、版本
    ↓
publishing            CAS + 快照 + atomic replace + Registry refresh
    ↓
published
```

其他终态：

- `abandoned`：用户主动放弃；保留审计摘要，不发布。
- `stale`：基线 digest 已变化，需要重新分析影响和验证。
- `failed_recoverable`：发布未切换 active，可从 staging 重试。

每次 Tool 调用只推进合法状态迁移。Agent 不能仅通过文字宣称“已发布”。

## 8. Markdown Authoring Contract

Markdown 文件是 Agent、用户、Registry 和运行时共同参考的唯一持久定义。正文是用户真正创作和审核的业务资产；frontmatter 是 Backend 为识别、索引和运行维护的机器投影。Schema 和 Authoring Brief 是 Agent 的内部写作上下文，不是用户需要学习的格式，也不能脱离 Markdown 独立生效。

### 8.1 LLM-readable Authoring Brief

Agent 可以在 ChangeSet 中维护一份临时 Authoring Brief，帮助长对话保持一致并指导正文写作：

```yaml
kind: measure
goal: 定义成交均价
observed:
  - 成交金额和销量来自 monthly_sales
confirmed:
  - 先分别 SUM 再相除
  - 退货按负数计入
  - 零分母返回空
unresolved:
  - 多币种是否直接拒绝
evidence:
  - profile:monthly_sales@sha256:...
body_outline:
  - 业务含义
  - 计算口径
  - 颗粒度与数据来源
  - 业务规则
  - 验收案例
```

它可以是 YAML、JSON 或结构化 Markdown，只要求 LLM 容易阅读和稳定更新。它的作用是防止 Agent 漏掉决定、混淆证据和重复追问，不承担运行时解析或编译职责。发布后可以随 ChangeSet 留作审计，也可以删除；无论如何，运行时只读取最终 Markdown。

### 8.2 Frontmatter 所有权与生效映射

Backend 必须维护一份可查询的 **Frontmatter Effect Registry**，说明每个字段由谁维护、在哪里消费、产生什么效果、是否必须在正文中体现。不能继续存在“字段写在那里，但没人知道是否生效”的隐式契约。

| Frontmatter 字段 | 维护者 | 典型消费者/效果 | 正文审核要求 |
| --- | --- | --- | --- |
| `formatter` | Backend | Registry formatter 与文档 loader | 由目标路径确定，可自动补齐 |
| `type` | Agent 提案、Backend 校验 | Registry 与 Resolver 路由；Relation 不参与自由检索，Grain 只显式命中 | 在发布摘要中说明；只能由显式目标类型补齐，冲突必须拒绝 |
| `name`、`aliases` | Agent/Backend | 搜索、展示和匹配 | 名称变化应在正文标题或发布摘要可见 |
| `description`、`tags` | Agent/Backend | Catalog 摘要、过滤和检索 | 描述变化在正文或发布摘要可见 |
| `version` | Agent/Backend | 展示、导出与审计元数据 | 不是并发控制；变化在发布摘要可见 |
| 文件 digest | Backend 控制面 | 基线 CAS、候选冻结与批准绑定 | 不写入 frontmatter，不要求用户编辑 |
| Dimension `resolution` | Agent 提案、Backend 校验 | 实体解析和 Join | 必须在正文说明解析方式和未知值策略 |
| Relation `relation`/`cardinality` | Agent 提案、Backend 校验 | Join 路径和重复计数行为 | 必须在正文说明字段映射、基数和风险 |
| Model 资产、关系、Guardrail 引用 | Agent 提案、Backend 校验 | 模型选数与运行时约束 | 必须在正文列出依赖、范围和限制 |

修缮规则：

- Backend 可以不经用户确认修复格式、缺省 formatter、排序和由明确目标路径唯一确定的技术字段；不能从正文或 Brief 猜业务字段。
- 会改变选数、计算、Join、颗粒度、过滤或 Guardrail 行为的字段属于业务有效字段，只能根据用户已确认的正文/Brief 生成，且必须在正文中有等价表达。
- Frontmatter 修缮必须发生在用户预览之前；预览绑定正文与 frontmatter 的联合 digest。批准后任何一侧变化都使批准失效。
- 正文与业务有效 frontmatter 冲突时阻止发布，由 Agent 修正文或重新生成 frontmatter；不能静默选择一侧。
- Toolset 应提供 `inspect_frontmatter_contract`，让 Agent 能查到字段消费者和效果；普通用户不需要看到这份技术映射。

### 8.3 身份、Schema 与业务版本

定义文件继续使用现有 frontmatter 表达 Registry 路由所需的稳定元数据；这些字段由 Agent/Toolset 自动生成和维护，普通用户无需直接编辑：

```yaml
formatter: semantic-asset
type: measure
name: 成交均价
description: 指定范围内每个售出单位对应的净成交金额
version: 1.2.0
```

规则：

- Canonical 资源 ID 仍由 `<type>:<relative-directory>` 得出；显示名和别名不参与身份判断。
- P0 不向已发布文件加入 `authoring_schema`。Skill 根据目标类型读取对应 reference，避免未知字段改变语义 hash 或制造迁移负担。
- `version` 当前只是业务展示与审计元数据，不能充当 CAS。P0 不自动推断 SemVer；改变分母、唯一键、关系基数或默认范围时由 Agent 明确提示版本影响。
- Dimension 的 `resolution`、Relation 的 `relation` 等运行时已有结构字段继续保留；不为追求统一而把 Measure、Grain 正文强制改写成新的 YAML DSL。

### 8.4 五类 Markdown Schema

每个 Schema 包含“允许的 frontmatter、正文应覆盖的主题、必须回答的业务决策、引用格式、验证规则和完整示例”。它用于提示 Agent 和检查最终 Markdown，而不是要求用户按模板填表，也不是把正文再解析成权威结构。

| 类型 | Markdown 必须讲清楚的内容 |
| --- | --- |
| Measure | 业务含义、输入、计算顺序、默认 Grain、空值/零值/单位/时间策略、正反例 |
| Dimension | 成员含义、来源或解析方式、规范成员、未知值/分类/排序策略、正反例 |
| Grain | 业务对象、唯一键、重复来源、去重、上卷、快照策略、适用 Measure |
| Relation | 两端角色、字段映射、基数、Join 方向、空键/未匹配/覆盖率、重复计数风险 |
| Analytics Model | 目标问题、数据与语义依赖、关系路径、默认范围、输出要求、验收案例 |

Schema 应允许 Agent 根据业务表达选择自然的标题和叙述方式，只要求关键信息可读且不矛盾，不把章节顺序或标题文字变成僵硬表单。需要确定性执行的 SQL Hint、Guardrail、关系映射或 Dimension resolution 可以继续使用现有结构化区块或 Reference 文件，并由 Agent 自动维护。

### 8.5 Agent 最终编写的 Markdown 示例

```markdown
---
formatter: semantic-asset
type: measure
name: 成交均价
version: 1.0.0
description: 指定范围内每个售出单位对应的净成交金额
aliases: [平均成交价, ASP]
---

# 成交均价

## 业务含义

指定范围内每个售出单位对应的净成交金额。

## 计算口径

先分别汇总成交金额与销售数量，再计算 `SUM(成交金额) / SUM(销售数量)`；
不得使用行级价格的简单平均。

## 颗粒度与数据来源

- 默认颗粒度：`grain:vehicle_series`
- 成交金额：`table_asset:monthly_sales.成交金额`
- 销售数量：`table_asset:monthly_sales.销量`

## 业务规则

- 退货按负数计入净额和净销量。
- 分母为 0 时返回空值。
- 同一次计算只允许一种币种。

## 验收案例

- 正常：两个订单分别销售 1 台和 2 台，应按总金额除以 3。
- 边界：净销量为 0 时结果为空。
- 反例：不得对订单行单价执行 `AVG`。
```

Agent 可以重写或补充 Markdown，但必须在 ChangeSet Diff 中完整展示。编辑已有文件时优先做局部 Patch，保留与用户意图无关的正文、注释和 Reference；如果需要大范围重写，必须在预览中明确标记。

普通用户默认看到上述正文的渲染结果，不需要审阅 `formatter` 等元数据。发布确认以“业务含义发生了什么变化”为主，原始 frontmatter 和文件 Diff 仅在展开技术详情时显示。

### 8.6 固定写作数据流

```text
已发布 Markdown + Authoring Schema + Skill Guide
        + Catalog/Profile/依赖/Trace + 用户决策
                              │
                              ▼
             Authoring Brief（事实、决定、证据、未决项）
                              │
                              ▼
                    Agent 编写 staging Markdown
                              │
                              ▼
             Backend 修缮并校验 frontmatter 机器投影
                              │
                              ▼
       Prepared Plan（完整候选、Diff、基线、验证、摘要）
                              │
                              ▼
                用户确认后发布为新的 Home Markdown
```

约束：

- Schema、Skill Guide、Authoring Brief、Catalog index 和 ChangeSet 摘要都是辅助上下文，不能脱离 Markdown 独立生效。
- ChangeSet 丢失时最多丢失未发布草稿，不能影响已发布定义。
- Search index、依赖图和 UI 摘要均可从 Markdown 派生并重建；派生结果与 Markdown 冲突时以 Markdown 为准并重建索引。
- P0 和后续版本都不计划把 `definition.yaml` 或持久 IR 提升为与 Markdown 并列或高于 Markdown 的事实源。

## 9. ChangeSet 协议

本节描述 P1 目标协议，不是首个 Measure 纵切的实现前置。P0 使用最小 Prepared Plan：冻结一个完整 Markdown 候选、基线 digest、候选 digest、验证、预览和机器效果摘要，并绑定创建 Session 与 24 小时 TTL。只有出现多对象事务或跨 Session 长草稿的真实需求后，才升级为下述可持久 ChangeSet。

```yaml
schema: semantic-change-set/v1
id: sc_20260812_market_config
status: awaiting_approval
owner_user_id: local
created_by_session_id: session_a
last_touched_by_session_id: session_b
revision: 7
editor_lease:
  session_id: session_b
  expires_at: 2026-08-12T12:00:00Z

scope:
  definition_root: user

intent:
  source: user
  text: 把销量和产品配置联合起来，按车系分析配置率和销量

authoring_brief:
  content_digest: sha256:...
  confirmed: [按款型计算配置率, 按车系展示]
  unresolved: []

base_revision:
  catalog_digest: sha256:...
  objects:
    model:汽车行业综合分析:
      version: 0.2.1
      digest: sha256:...
    measure:sales:
      version: 0.1.0
      digest: sha256:...

draft_files:
  - logical_path: semantic-assets/measure/config_rate/measure.md
    action: create
    content_digest: sha256:...
    schema: semantic-measure/v1
  - logical_path: analytics-models/automotive/model.md
    action: update
    base_digest: sha256:...
    content_digest: sha256:...

decisions:
  - subject: measure:config_rate.denominator_grain
    value: grain:car_model
    status: confirmed
    source: user
  - subject: model:automotive.replace_measure_reference
    value: measure:config_rate
    status: confirmed
    source: user
    risk: high

impact:
  affected_models: [汽车行业综合分析]
  affected_assets: [measure:销量, measure:sales]
  runtime_paths: [sql, pandas, analysis-project-export]

validation:
  status: passed
  result_digest: sha256:...
  checks:
    - markdown_schema_complete
    - all_references_exist
    - dependency_closure_complete
    - model_graph_connected
  replay_cases:
    - 按车系统计销量与空气悬架配置率

publication:
  prepared_plan_digest: sha256:...
  expected_base_digests:
    model:汽车行业综合分析: sha256:...
    measure:sales: sha256:...
  create_snapshot: true
  rollback_on_failure: true
```

### 9.1 ChangeSet 是 Markdown 操作信封

ChangeSet 不保存 Measure、Grain 或 Model 的另一份结构化定义。它持久化的是：

- staging 中的完整 Markdown 文件或受限 Patch；
- 给 Agent 使用的 Authoring Brief、确认状态和证据；
- 每个目标的逻辑路径、create/update/move/delete 动作和基线 digest；
- 用户决策、证据、影响、风险、验证结果和批准；
- 发布前后 digest、快照与回滚回执。

UI 可以从 Markdown 草稿派生卡片摘要，Agent 也可以在决策记录中使用 `measure.calculation` 之类的字段路径，但这些摘要和路径不构成可独立发布的语义对象。最终审核对象始终是 Markdown Diff。

### 9.2 草稿所有权、接管与不可变发布计划（P1）

ChangeSet 的业务所有权属于 `owner_user_id`，Session 只是创建者或当前编辑者。Backend 必须按 `owner_user_id + id` 授权和索引，不能把草稿藏在 Session 私有状态中。

- 创建、修改 staging Markdown、更新决策和恢复编辑均增加 `revision`，并要求调用方提交 `expected_revision`。
- editor lease 到期后其他 Session 可以显式接管；接管不改变已确认决策，且写入审计事件。
- 验证结果绑定 `change_set_id + revision + base_revision + draft_files_digest`。草稿再发生任何修改，原验证结果立即失效。
- `prepare-publication` 将已验证的 Markdown 内容、文件动作、风险与审批要求、目标 digest 和文件清单冻结为不可变 `prepared_plan_digest`。
- 用户批准必须绑定该摘要；`publish` 只接受摘要和与之绑定的批准，不能在发布请求中临时增删 operation 或更换目标。摘要与当前 revision、批准或基线不符时返回 `stale`。
- 同一 owner 可以从任何 Session 列出、查看和恢复未完成草稿；默认只允许一个 Session 编辑，其他 Session 只读观察。

### 9.3 Agent 发布安全

- 开始草稿时记录每个目标的 digest 和版本。
- 验证前重新读取最新 Markdown/Registry；基线变化则标记 `stale`。
- 发布使用 compare-and-swap，不能覆盖用户在另一会话中的修改。
- Agent 只能写独立 staging；完成全部校验和确认后，Toolset 才替换 active Markdown。
- 多文件 ChangeSet 在写入前建立完整目标快照，按依赖顺序使用单文件 atomic replace；中途失败立即从快照恢复已替换文件。
- 发布回执记录新 digest、版本、文件列表、快照和 Registry refresh 结果。
- 失败时不得留下部分新模型引用未发布资产的状态。

P0 的目标是给 Agent Toolset 提供单 Backend、单定义文件的安全发布和失败恢复，不在本阶段建设多文件事务、owner-scoped 草稿、通用 Catalog 事务、immutable Registry generation 或覆盖所有人工/API 写入的全局 Mutation Gateway。人工编辑导致 digest 变化时，Prepared Plan 发布返回 `baseline_changed`，Agent 必须重新读取并预览。

### 9.4 复用现有 Dimension Publisher 的边界

`semantic_dimension_publisher.py` 已有 staging、显式发布和单文件 `_atomic_write` / `_atomic_copy`；crosswalk 流程也已有版本快照经验。这些应抽取为公共 primitive，并保留现有 Dimension Builder 的调用兼容。

现有实现不是通用多文件事务框架，因此不能直接宣称“真正原子”。五类单文件发布复用其思路实现临时写入 + `os.replace`、基线 CAS、对应 Registry refresh 和失败恢复；不抽取目录 lease 或多文件事务。此次审计同时修复 Dimension Publisher 对预存 `id`/`build_skill.adapter` 的脆弱正则前置要求，改为结构化修缮 frontmatter 并保留正文。

## 10. Backend 工具边界

新增 `semantic_steward` Toolset，仅在 `semantic-steward` Skill 激活后暴露。工具名为建议契约，实施时可按现有命名规范调整。

### 10.1 Registry Discovery 是创作前置门禁

文件遍历可以临时回答清单问题，但不能稳定证明 Agent 在新增前检查过已有定义。因此增加一个统一的 `discover_semantic_definitions`：直接刷新 Semantic Asset 与 Analytics Model Registry，按 kind 列举或按业务概念检索，返回稳定 ID、逻辑路径、匹配依据、definition digest 和 Analytics Model 反向引用。空查询用于分页清单；非空查询生成 Session-bound、Catalog-digest-bound discovery receipt。

Receipt 只证明“指定 Catalog 快照已经被检索”，不替 Agent 或用户决定业务语义。Agent 必须读取所有合理候选的完整 Markdown，向用户解释复用、修改或新增的判断。Receipt 显式区分 `inventory` / `targeted` 并由服务端 HMAC 签名；`prepare_semantic_markdown` 确定性拒绝缺失、空查询或纯标点、结果未完整、目标存量定义未返回、字段篡改、跨 Session、错误 kind、过期或 Catalog 已变化的 receipt。

### 10.2 五类对象共用两个写工具

| 工具 | 作用 |
| --- | --- |
| `discover_semantic_definitions` | 分页列举或检索五类 Registry；返回候选、匹配原因、模型反向引用和 discovery receipt；不修改 active definition |
| `prepare_semantic_markdown` | 接收完整 Measure、Grain、普通 Dimension、Relation 或 Analytics Model Markdown 候选；补齐目标路径唯一确定的字段，校验正文/frontmatter/Brief/引用，冻结候选和基线，返回正文预览、机器效果摘要、技术 Diff 与 `plan_digest`；不写 active definition |
| `publish_semantic_markdown` | 只接受已准备的 `plan_id + plan_digest`；校验 Session、TTL、候选 digest 和 active 基线，执行单文件 atomic replace、Registry refresh 与失败恢复 |

三个工具形成“发现 → 准备 → 发布”的稳定 Agent 协议；五类对象使用同一参数契约和 kind-specific validator，不为每类对象新增一套工具。

### 10.3 后置能力

跨 Session 草稿、多个文件的拓扑发布、显式历史回滚、通用依赖图和 Trace 健康建议进入后续阶段。出现需求时可以在 prepare/publish 协议内部引入 ChangeSet 服务，但不为每类对象复制工具。

复杂 `entity_lookup` 维度继续使用现有 `semantic_dimension_build` Toolset。Steward 只负责正确编排，不复制构建脚本。

### 10.4 禁止旁路

- Agent 的通用 `write_file` 对 active `semantic-assets/`、`analytics-models/` 的 create/update/delete/rename 必须拒绝，并提示激活 Steward Toolset。
- Steward Toolset 的 prepare 只能写内部 plan；只有 `publish_semantic_markdown` 可以在 CAS、验证和用户确认后写 active Markdown。
- 这一限制约束 Agent 能力边界，不改变 Markdown 的事实源地位，也不强制取消用户通过 Monaco、REST 或 Import 直接维护 Markdown 的现有能力。
- 人工或其他 API 编辑 active Markdown 后，所有未发布 Agent ChangeSet 依靠 base digest 检测 `stale`，不得覆盖新内容。
- 未来如果需要统一人工审批，再另立 Mutation Gateway 项目；不作为本次 Semantic Steward Skill/Toolset 的前置。

### 10.5 最小 Backend 支持

本 P0 不新增完整 Control Plane REST 产品。Toolset 只需要复用或补充以下内部能力：

- 按逻辑 ID 读取 Markdown、Schema、Profile、依赖和使用证据；
- 五类 Registry 的统一发现、分页、匹配依据、模型反向引用和 Session-bound receipt；
- 查询 Frontmatter Effect Registry，并在预览前修缮/验证 frontmatter 机器投影；
- session-bound Prepared Plan 存储、TTL、候选 digest 与 definition CAS；
- 完整候选 Markdown 的 Schema/引用验证、Diff 和风险摘要；
- 单文件 atomic replace、失败恢复、Registry refresh 和发布回执；
- Agent `write_file` 对 active definitions 的路径拦截。

若后续前端需要 ChangeSet 详情页，再把相同服务暴露为 REST API；P0 不为了未来 UI 先设计一套大而全的资源协议。

## 11. Semantic Steward Skill

Skill 名称建议为 `semantic-steward`，定位是对话式语义建模伙伴，而不是后台文件管理员。

### 11.1 触发范围

描述应覆盖：

- 创建、修改、合并、废弃度量值、维度、颗粒度和资产关联；
- 创建或维护分析模型；
- 从业务问题、数据 Profile 或 Trace 发现缺失语义；
- 检查重复、冲突、断链、未引用和正文/结构漂移；
- 预览、验证、发布或回滚语义 ChangeSet。

### 11.2 Skill 内只保留流程知识

```text
semantic-steward/
├── SKILL.md
└── references/
    ├── dialogue-policy.md
    ├── markdown-schemas.md
    ├── change-set-contract.md
    ├── asset-authoring-guides.md
    └── validation-and-risk.md
```

`SKILL.md` 保持精简，说明核心流程、禁止旁路、何时读取哪个 reference，以及如何与 `build-semantic-dimension` 配合。Schema、长示例和各资产决策清单放进 references，避免每次激活占满上下文。

## 12. 验证体系

### 12.1 静态验证

- ID、类型、版本和 formatter 合法；
- Markdown 符合对应 Authoring Schema 的必需章节和引用格式；
- Authoring Brief 中所有 `confirmed` 业务决定已体现在正文，`unresolved` 为空；
- 所有引用存在或属于同一 ChangeSet 的待发布对象；
- frontmatter、正文和 Reference 中没有明显冲突；发现冲突时返回诊断，由 Agent 与用户修订 Markdown；
- 模型多资产关系图连通；
- 关系两端和模型选择范围一致；
- 没有同一模型角色下无法解释的重复资产；
- 模板、Reference 和 Guardrail 路径存在；
- 没有占位文本、空业务规则或未决决策。

### 12.2 数据验证

- 字段存在且类型可用；
- Grain 声明的唯一键符合样本或全量探测；
- Relation 声明的基数、空键率和覆盖率符合阈值；
- 枚举成员、分类和来源值差异可解释；
- Measure 的分子、分母、单位、零值和空值案例可执行；
- Entity lookup 只使用已发布且 join-eligible 的映射。

### 12.3 行为回放

每个对象至少自动生成：

- 一个正常业务问题；
- 一个容易误算的边界问题；
- 一个禁止行为或反例；
- 一个结构化结果断言。

发布前运行 SQL/Pandas 计划级回放，优先验证资产选择、关系路径、颗粒度、Guardrail 和结果契约；只有稳定基准数据才要求精确数值或范围。

Agent 根据 Markdown、Schema 和已确认决策生成验证计划。只有 Markdown 中存在可确定执行的 SQL Hint、Guardrail、数据绑定或验证案例时才运行对应 SQL/Pandas 回放；不能用 LLM 临时猜出的 SQL 冒充定义本身。无法自动执行的业务规则保留为人工可读验收案例，并在预览中明确标为“未自动回放”。

### 12.4 发布风险分级

| 风险 | 示例 | 要求 |
| --- | --- | --- |
| 低 | 补充别名、说明、缺失但已明确的模型关系 | 展示 Diff，用户确认发布 |
| 中 | 增加数据绑定、默认过滤、模板路由 | 影响分析 + 回放 + 用户确认 |
| 高 | 修改分母、唯一键、关系基数、实体合并、资产替换或废弃 | 完整回放 + 明确风险确认 + 快照 |

P0 不允许 Agent 自主发布业务语义变更。用户可以一次性批准 ChangeSet 中所有已展示的低风险操作，但高风险操作必须单独列出。

## 13. 前端体验

对话是主入口，资产页是观察和精细维护入口，两者使用同一 ChangeSet 服务。

普通用户的确认界面以渲染后的 Markdown 正文和业务摘要为主，不展示 Schema、IR、YAML 字段或文件路径。Agent 可以把必要决策呈现为自然语言选项，但不能把 Authoring Schema 伪装成长表单让用户填写。

对话中应提供紧凑结构化卡片：

```text
语义草稿：成交均价
  已确认  退货按负数计入；零分母返回空
  系统建议  SUM(成交金额) / SUM(销量)
  待决定  多币种数据是否拒绝计算

  [查看正文预览] [修改决定] [继续]
```

发布前卡片：

```text
发布 ChangeSet sc_...
  新建：measure:average_transaction_price
  修改：model:销售分析
  影响：1 个模型，3 个测试案例
  验证：12 passed，0 failed
  风险：中

  [查看正文变化] [展开技术 Diff] [确认发布] [返回修改]
```

产品要求：

- P0 的主入口是现有对话；Tool 结果先使用可渲染的结构化摘要，不以前端专用 ChangeSet 工作台为前置。
- 用户可以从对话打开正文变化、验证报告和已发布资产详情；原始 Markdown Diff 按需展开。
- 用户从资产页点击“让 Agent 帮我完善”时，可以启动带当前对象 ID 和 base digest 的对话。
- Monaco 继续作为专家用户直接维护唯一 Markdown 事实源的入口，不强制改为 ChangeSet 编辑器。
- Monaco、REST 或 Import 修改 active Markdown 后，Agent 草稿必须因 base digest 变化进入 `stale`，重新读取并生成 Diff。
- 后续可以增加原生草稿卡片和 ChangeSet 详情页，但它们只是同一 Markdown 草稿的视图，不创建结构化语义事实源。
- 不把宿主物理路径展示为主要信息；展示逻辑 ID、来源、版本和虚拟路径。
- 前端当前仍出现 `backend/semantic-assets`、`backend/analytics-models` 的旧提示文案，实施本 P0 时一并改为用户定义目录的逻辑表述。

## 14. P0 实施顺序

P0 的主体就是 **Semantic Steward Skill + Toolset**。Backend 只补 Toolset 无法绕开的草稿、校验和安全发布 primitive，不扩展成统一语义控制平台。

### P0-0：Effect Audit 与真实契约（已完成首轮）

- 从 Registry、Resolver、Runtime Compiler 和现有 Publisher 反查 frontmatter 字段的实际消费者。
- 明确 `type`、`name`、`description`、`aliases`、`tags` 都可能改变检索或路由，不得称为纯格式字段。
- 明确文件 digest 才是 CAS；`version` 不是并发控制。
- P0 不写入 `authoring_schema`，不从正文/Brief 推断业务 frontmatter。
- 修复 Dimension Publisher 依赖预存 `id` 与 `adapter` 文本行的既有契约裂缝，改为结构化更新并保留正文。

验收：字段效果有代码证据和回归测试；Backend 只补齐由显式目标路径或 Publisher 操作唯一确定的字段，遇到冲突拒绝发布。

### P0-1：Measure 两工具纵切（已实现）

- 创建 `semantic-steward` Skill、Measure 写作指南、frontmatter 效果说明和对话/发布指南。
- Agent 使用现有只读能力读取 Markdown/Profile；写出完整候选，不要求用户填写 YAML。
- `prepare_semantic_markdown` 校验并冻结候选，返回正文预览、机器效果、技术 Diff 与 digest-bound plan，不触碰 active definition。
- `publish_semantic_markdown` 执行 plan/session/TTL/candidate/baseline 校验、单文件 atomic replace、Registry refresh 和失败恢复。
- Agent 展示 prepare 结果后在同一轮发起 publish；Harness 立即弹出唯一一次用户审批，批准指纹包含准确的 `plan_id + plan_digest`。不再先要求用户用聊天消息回复“批准”；Skill 文本不是唯一审批防线。
- 主 `/api/agent` 保持 managed read-only；遗留写工具移除定义目录白名单并增加回归测试。

验收：创建与修改 Measure 都必须经过“prepare → 展示审核内容并同轮请求 publish → Harness HITL 单次批准”；人工并发编辑返回 `baseline_changed`；发布后 Registry 能读取该 Measure；失败不留下半发布文件。

当前验证记录（2026-08-13）：首个 Measure 纵切的审批、Brief、legacy chat 和路径泄漏阻断已关闭；横向扩展与 Discovery 门禁加入后，39 个 Semantic Authoring 定向测试及 563 个 Steward、Tool Execution、Registry、Analytics Model、Dimension Builder、Runtime、Project Export、Toolset、DeepAgents 组合回归通过，Ruff、compileall 与 diff check 通过。Luna 对抗复核提出的正文嵌套值审计、Relation key mapping 一致性、缺失 `table_asset:`、语义空查询和 receipt 篡改阻断均已关闭。

交互修正（2026-08-14）：删除“prepare 后等待用户在聊天中回复批准”的重复确认。Agent 展示冻结候选后必须在同一轮请求 publish，由 Harness 的 digest-bound HITL 卡片完成唯一审批；批准后恢复原调用，拒绝则不写入。Semantic Authoring 与 Toolset 定向回归 98 项通过。

### P0-2：在同一协议上扩展五类对象（已实现）

已按 Grain → Dimension → Relation → Analytics Model 扩展。每个类型只增加：

- 对应 Skill reference 与完整 Markdown 示例；
- kind-specific frontmatter validator 和正文审核主题；
- 引用/依赖/数据探测适配器；
- 该类型的 Registry refresh/发布后断言。

实现边界：普通 Dimension 支持 `source_field`、`derived`、`calendar_lookup`；`entity_lookup` 被 prepare 确定性拒绝并路由到专用 Builder。Analytics Model 校验已选语义资产、Relation 连通图、Guardrail ID、模型包 references/templates，并在发布后加载完整 Model Context；Grain 继续以正文承载唯一键、去重和上卷规则，不新增伪结构化 DSL。

复杂 `entity_lookup` Dimension 继续编排 `build-semantic-dimension`，SQL Guardrail 继续编排专业 Skill。不得复制构建链路，也不得增加每类型专属 prepare/publish 工具。

验收：每类至少一条“自然语言 → 引导决策 → 完整正文 → prepare → 显式批准 → publish”路径；普通用户只审核正文和自然语言机器效果。

### P0-3：Discovery 决策闭环（已实现）

- `discover_semantic_definitions` 支持五类清单、目标检索、分页、匹配原因和模型反向引用；
- Semantic Steward 强制“发现 → 读取候选正文 → 解释复用/修改/新增 → 写作”；
- prepare 必须验证服务端签名的完整 targeted discovery receipt；修改存量定义时目标必须在候选中；Catalog 或 Session 改变时重新发现；
- Discovery 只产生可过期控制凭证，不成为语义事实源，运行时仍只消费发布后的 Markdown。

验收：用户询问“我有哪些度量值”可直接得到 Registry 清单；用户提出新增概念时，没有有效 targeted receipt 无法 prepare。

### P1：真实需求驱动的治理增强

- owner-scoped 跨 Session ChangeSet、revision 与显式接管；
- 多文件依赖拓扑发布和事务级恢复；
- 反向依赖索引、Trace 健康建议和显式历史回滚；
- 对话草稿/验证/发布卡片及资产页“让 Agent 帮我完善”入口；
- Grain 唯一性、Relation 基数/覆盖率、模型图和可执行案例回放。

进入条件：单对象纵切已经稳定，且出现无法通过重新 prepare 解决的真实跨 Session 或多对象原子需求。

### 后续：Markdown 派生能力

后续可以增强依赖索引、语义搜索、健康评分、多人审批和前端 ChangeSet 工作台，但它们都从 Markdown 派生并可重建。Markdown 始终是唯一事实源，不规划将持久 IR 或 `definition.yaml` 提升为权威定义。

## 15. 代码落点建议

```text
backend/analytics/semantic_authoring/
  contracts.py               # Authoring Brief 与字段 Effect Registry
  documents.py               # Markdown/frontmatter 安全解析与稳定渲染
  validation.py              # kind-specific 正文与 frontmatter 校验
  service.py                 # Prepared Plan、digest CAS、atomic publish

backend/tools/
  semantic_steward_tool.py

backend/skills/semantic-steward/
  SKILL.md
  references/

frontend/src/components/analytics/
  SemanticDraftCard.tsx       # P1，可后置
  SemanticValidationCard.tsx  # P1，可后置
  SemanticPublishCard.tsx     # P1，可后置
```

存储实现应通过 `PuddingClawPaths` 或注入的 typed path 获取 Home 位置，不重新引入含义不明的 `base_dir`。

## 16. 测试计划

### 16.1 单元测试

- 五类对象各自必需业务主题、H1、frontmatter 类型和占位文本检查；
- 缺失 `formatter`/`type` 可以按显式目标路径补齐；Model `id` 可以按目录补齐；冲突值必须拒绝；
- Frontmatter Effect Registry 不把 `type`、`aliases`、`description` 误标为安全格式字段；
- 未决 Authoring Brief 阻止 prepare；Brief 不参与 Runtime；
- 空查询可分页列出五类定义；目标检索返回匹配依据和 Analytics Model 反向引用；
- 缺失、inventory/语义空查询、结果未完整、目标未返回、签名篡改、跨 Session、错误 kind、过期或 stale discovery receipt 阻止 prepare；
- prepare 不写 active definition，候选大小与公开预览有界；
- plan/session/TTL/candidate digest/definition baseline 任一不匹配均阻止发布；
- 单文件发布幂等，Registry refresh 失败时恢复旧文件；
- Dimension resolution、Relation endpoints/keys/cardinality、Model 依赖图/Guardrail/包资源使用真实 Registry 契约校验；
- `entity_lookup` Dimension 必须路由到专用 Builder；
- Dimension Publisher 能为存量/新建包结构化补齐 `id`、reference 和 adapter，同时保留正文；
- 所有路径写入临时 `PUDDINGCLAW_HOME`，Tool 输出不返回宿主绝对路径。

### 16.2 Agent 场景测试

- 用户一句话创建任一普通语义对象，Agent 先搜索并比较已有定义，再读取证据并只追问业务歧义。
- 用户全程不接触 Schema/frontmatter，Agent 生成完整正文和提案字段。
- Agent 在 prepare 后先展示正文和机器效果，再在同一轮请求 publish；Harness HITL 卡片是唯一批准动作，用户无需再发送“批准”消息。
- 并发修改导致 baseline digest 变化，发布返回 `baseline_changed` 而不是覆盖。
- 人工通过 Monaco 修改同一 Markdown 后，Agent 重新读取并生成新 Diff。
- Agent 修改存量定义时保留无关正文，只对本次已确认口径做可见 Patch。
- Grain、普通 Dimension、Relation 和 Model 各有 prepare/publish 场景；复杂 Dimension 使用专用 Builder。

### 16.3 安全回归

- 主 DeepAgents 定义目录保持 read-only；遗留 `write_file` 不能发布定义。
- Monaco、REST 或 Import 的人工变更仍能被 Registry 读取，并会使旧 Prepared Plan stale。
- Prepared Plan 和 Tool 输出不泄露宿主 Home 绝对路径。
- Registry refresh 注入失败时恢复旧文件；重复 publish 返回同一发布状态。
- 只读发现和 Schema 检查不能写入或规范化 Markdown。
- 未明确确认的 `inferred` 决策不能发布。

## 17. 分阶段完成定义

### Measure 纵切完成

1. `$PUDDINGCLAW_HOME/definitions/` 中的 Markdown 是唯一持久语义定义；不存在权威 IR 或 `definition.yaml`。
2. 用户通过自然语言完成 Measure；Agent 读取指南并写完整正文，用户无需编辑 frontmatter。
3. prepare 不写 active definition，并返回正文、机器效果、校验和准确 plan digest。
4. publish 只应用用户审核过的冻结候选，具备 session/TTL/digest CAS、atomic replace、Registry refresh 和失败恢复。
5. 通用 Agent 写工具不能绕过 Steward，人工 Monaco/REST/Import 仍可维护同一 Markdown，并会使旧 plan stale。
6. Effect Registry 与测试覆盖 Measure 已使用字段；Backend 不从正文/Brief 猜业务行为。
7. Skill 黑盒演练能保持“prepare 后展示结果、同轮触发 digest-bound HITL、批准后恢复 publish”的单次审批边界，不暴露宿主路径。

### 五类对象 P0 完成

1. Measure、Grain、Dimension、Relation、Analytics Model 都复用相同 discovery/prepare/publish 协议和各自 Skill reference/validator。
2. Agent 会先通过 Registry Discovery 查找并读取真实 Markdown、Profile、现有资产和依赖，解释复用/修改/新增后再逐步引导，而不是输出待填模板。
3. 普通用户审核渲染正文、业务摘要和风险；原始 Markdown/frontmatter Diff 只是高级详情。
4. 复杂维度和 Guardrail 由 Steward 编排现有专业 Skill，不复制实现。
5. SQL、Pandas 和 Analysis Project 导出继续只消费发布后的 Markdown 定义。

## 18. 后续演进

P0 完成后，才继续扩展：

- 基于 Trace 的定期漂移监测和自动提案；
- 基于使用频率、相似结构和结果差异的资产合并建议；
- 领域包、团队审批和多人评论；
- 从 Markdown 派生且可重建的依赖索引、搜索投影和跨格式导出；
- 模型自动优化和 A/B 评测；
- 将已确认语义反馈给 LLM Wiki / gbrain，但保持知识与分析定义的权威边界。

长期目标不是让 AI 替用户偷偷决定业务语义，而是让 AI 承担发现、整理、验证和维护成本，让用户把注意力集中在真正需要业务判断的少数决策上。
