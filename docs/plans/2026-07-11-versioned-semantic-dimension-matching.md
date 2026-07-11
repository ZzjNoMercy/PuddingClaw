# 版本化语义维度匹配实施计划

状态：实施中  
更新时间：2026-07-11（北京时间）

## 目标

把语义维度的跨源匹配从“构建 Job 生成一个难以人工修正的 JSON”变为可持续维护的闭环：

1. Skill 和后台 Job 只生成可复跑的构建基线。
2. 用户在匹配管理界面维护人工覆盖，而不直接编辑构建产物。
3. 发布生成新的生效版本；下一次构建读取已发布的人工覆盖和来源登记。
4. 上险量、订单等是独立来源绑定，不是车系维度的新增字段。

## 文件合同

以 `semantic-assets/dimensions/<dimension_id>/references/` 为根：

| 文件 | 责任 | 是否由用户直接编辑 |
| --- | --- | --- |
| `generated_crosswalk.json` | 最近一次 Job 的未覆盖构建基线 | 否 |
| `active_crosswalk.json` | 当前生效 Crosswalk，供 `semantic_entity_lookup` 读取 | 否 |
| `manual_overrides.json` | 用户在匹配管理中保存的覆盖草稿，以及最近一次已发布的草稿快照 | 通过 UI |
| `source_registry.json` | 已接入来源及其身份字段映射，用于后续文件复用 | 通过 UI/HitL |
| `versions/vX.Y.Z.json` | 每次发布的生效快照 | 否 |

`active_crosswalk.json = apply(generated_crosswalk.json, published_manual_overrides)`，不是文件拼接。编辑中的 `overrides` 只用于页面预览；用户确认发布后才复制为 `published_overrides`、重算 active 并写入一个新的 `versions/vX.Y.Z.json`。覆盖按 `source_ref + source_key` 定位：先移除旧绑定，再按操作写入目标规范实体或标记排除。旧 `full_crosswalk.json` 不再作为运行时输入。

## 交互

语义维度详情增加“匹配管理”页签：

- 统计：规范实体、已绑定、待审核、未匹配、人工覆盖、来源数。
- 总览：一行一个规范实体，已登记来源作为列，查看跨源覆盖；支持搜索规范实体、`entity_key` 和任一来源键，服务端分页；总览不直接改绑。
- 来源编辑：先选择一个来源，再按来源键审核、检索规范实体、保存改绑草稿或标记不关联；支持来源键/规范实体搜索和服务端分页。
- 发布：编辑只保存草稿；点击页面发布或由 Agent 发布时，才物化新的 `active_crosswalk.json` 并生成版本快照。构建 Job 发布仍会先更新基线再叠加草稿覆盖。

### 规范基准治理

规范实体变化有两个入口，但共享同一份版本化生命周期覆盖，均在维度的“匹配管理”中维护：

1. **构建发现变化（被动）**：刷新规范基准时，worker 比较新的基准全集和当前 active 基准。新增、真实移除和键迁移写入 staging 差异；任务中心只提示并跳转到该维度的匹配管理，不能在任务中心直接删除或发布。
2. **主动维护（主动）**：用户可在“全局总览”中搜索实体并执行停用、恢复或移除。移除先弹出确认对话框；操作先保存为草稿，发布后才改变 active Crosswalk。

生命周期操作写入 `manual_overrides.json` 的规范实体覆盖（而不是编辑 `generated_crosswalk.json`）：

- `inactive`：保留规范实体、历史绑定和审计，但默认运行时查找不返回。
- `removed`：从 active Crosswalk 移除，并保留墓碑覆盖；后续构建即使基准表仍出现该实体，也不会悄悄复活。
- `active`：撤销停用/移除覆盖，重新采用生成基线。

每次发布都把生命周期覆盖与来源绑定覆盖一并快照到 `versions/vX.Y.Z.json`。基准缩减不再直接把构建标记失败：若差异是旧 `entity_key` 标准化迁移，按原始规范字段重算后应自动识别为同一实体；若是真实缩减，构建保留 staging 产物并进入“待处理规范基准变更”，等待用户在匹配管理做出决策。

HitL 接入新输入时把两个问题分开：

1. 来源行为：追加已有来源，或注册新来源。
2. 维度行为：仅映射至已有规范实体，或确实扩展规范维度字段。

订单表通常选择“注册新来源 + 映射已有实体”，不是新增车系字段。

追加已有来源时，HITL 会默认选择该来源的 `append` 模式；后端拒绝“已登记 source_id 却标记为 new”的规则。增量构建会保留历史的成功绑定、候选和未匹配诊断；若同一规范基准在构建结果中出现实体缩减，任务会失败而不是进入待发布状态。每个 Agent 创建的 Job 都必须带入当前 `session_id`，供任务中心和原对话追溯。

### 输入探测、建议与确认的职责边界

维度构建不能把“读取字段”“提出映射建议”“复用既有来源”和“用户确认”混成一个自动动作：

| 环节 | 责任方 | 做什么 | 不做什么 |
| --- | --- | --- | --- |
| 输入探测 | `inspect_dimension_build_input` | 附件仅读取表头；数据资产读取已登记的 Profile/字段；返回精确字段名和可复用 input object | 不推断业务键、不自动建立来源 |
| 规则建议 | Agent + `build-semantic-dimension` Skill | 依据用户意图、`dimension.md` 和已检查字段，建议规范基准、同位置键映射及来源行为 | 不越过 HITL 创建 Job、不把建议当最终规则 |
| 来源契约 | `source_registry.json` | 保存已发布来源的稳定 `source_id`、显示名和 identity fields，例如 `insurance_sales = 品牌 + 1-子车型` | 不记录某个附件的临时路径或文件实例 |
| 用户确认 | HITL 卡片 | 选择规范基准、字段映射，以及“追加已有来源”或“注册新来源” | 不直接修改 active Crosswalk |
| 规则校验 | 后端 `build_rule_from_decision` | 校验字段都来自候选输入、键位数一致、已登记来源只能 append、未知来源不能 append | 不凭空替用户更换字段 |

附件字段是后端确定性探测的结果；映射是 Agent 的可解释建议；`source_registry.json` 是已发布的复用契约；HITL 选择才是最终输入。请求接口必须完整保留 `source_id`、`source_name`、`source_mode`，否则 Pydantic 会把它们丢弃并把 append 错判为 new。该字段透传已有回归测试。

## 实施步骤

- [x] 定义文件职责、合并语义和前端交互边界。
- [x] 实现 Crosswalk 覆盖、来源登记和版本快照服务。
- [x] 改造 Job 发布：保存构建基线、物化生效 Crosswalk、写版本。
- [x] 提供匹配管理查询、覆盖更新和来源登记 API。
- [x] 在语义维度详情接入匹配管理界面。
- [x] 将匹配管理拆为规范实体总览与按来源编辑，并加入规范实体检索改绑。
- [x] 将人工覆盖改为草稿，前端/Agent 发布共用 active 版本和快照。
- [x] 覆盖发布、手工改绑、撤销和重建保留覆盖的回归测试。
- [x] 支持规范实体生命周期草稿（停用、恢复、移除需确认）并纳入版本快照。
- [x] 基准缩减进入 staging 变更状态，任务中心仅通知和跳转。
- [x] 在匹配管理的全局总览提供“规范基准变更”和“主动维护规范实体”入口；不再单独设置规范维护页签。

## 验收

- 手动改绑一条上险量来源记录后，页面只显示草稿预览；发布后 `semantic_entity_lookup` 才读取修改后的生效版本。
- 后续 Job 发布新的构建基线时，已有手工改绑仍存在。
- 首次接入订单表时，可登记订单字段映射，且不改变车系规范字段。
- 所有发布版本可在文件系统中追溯；UI 不要求用户寻找或编辑 JSON 文件。

## 实施记录

- `active_crosswalk.json` 已成为唯一运行时 Crosswalk；旧 `full_crosswalk.json` 已移除。
- 为现有 `vehicle_series` 做了一次性状态迁移，生成 `generated_crosswalk.json`、空 `manual_overrides.json`、`source_registry.json` 和版本快照；既有上险量来源已规范为 `insurance_sales / 乘用车上险量`，不向业务用户暴露 table asset id。
- HitL 卡现可区分“新建来源”和“追加已登记来源”；稳定来源身份使用 `source_id`，具体附件或表实例继续使用 `source_ref`。
- 匹配管理的规范实体与改绑选项按 Crosswalk 中全部规范字段动态组合展示，例如 `一汽 / 森雅鸿雁`；不再假设字段固定为 `canonical_serial_name`。
- 人工改绑的发布边界已与构建 Job 对齐：`manual_overrides.json` 同时保存草稿与最近一次发布快照，`active_crosswalk.json` 不再被单次编辑直接覆盖。
- 匹配总览与来源编辑均只拉取当前页；关键词在后端先过滤再分页，避免将完整 Crosswalk 加载到浏览器。
- Crosswalk 物化会以 `source_ref + source_key` 替换原有诊断记录；人工改绑未匹配来源键后，不得同时遗留一条 `unmatched` 诊断记录。
- 2026-07-11：增量来源构建补齐了已有来源强制 `append`、历史 diagnostics 合并、规范实体缩减阻断和 Agent Job 会话关联，避免把“追加同字段来源”错误记录为新来源或静默丢失审核队列。
- 2026-07-11：补齐 HITL 规则提交 DTO 的 `source_id`、`source_name`、`source_mode`。此前前端已选择“追加”，但 API 模型会静默丢弃这些字段，后端遂按默认 `new` 错误拒绝；现在提交、契约校验和构建规则保持同一来源模式。
- 规范实体键的标准化保留有业务含义的 `+` 后缀，例如“小鹏 P7”与“小鹏 P7+”必须是不同实体；空格、大小写等格式差异仍会归一。历史 Crosswalk 中 7 条重复 record 实际对应 6 组格式重复和 1 组 `P7/P7+` 碰撞，后续重建会以唯一规范键收敛格式重复并保留 `P7+`。
- 2026-07-11：规范基准缩减保护和历史绑定合并不能直接比较历史 `entity_key`。标准化规则升级后，原始规范值未变但键可能迁移（例如旧版把“双擎E+”写为 `...e`，新版正确写为 `...e+`）。两处逻辑统一改为按记录内的原始规范字段使用当前规则重算身份；这样仍会阻断真实删除，也不会把键格式迁移误报为实体缩减或丢失旧来源绑定。
- 2026-07-11：实现规范基准治理。主动维护通过 `entity_overrides` 保存 `inactive/remove/active` 草稿，发布时同步写入 active 和版本快照；被动构建发现真实缩减时保留 staging 并进入 `waiting_for_baseline_change_confirmation`，任务中心仅作提醒。用户在全局总览处理“保留为停用 / 移除 / 取消”后才进入正常发布确认；所有移除动作均先经确认对话框。
- 2026-07-11：区分“重建规范基准”和“追加来源列”。对已经发布的维度，`append_source` 会自动把 `active_crosswalk.json` 注入为锁定 canonical；HITL 只能选择追加表的字段与来源归属。构建器从 active 读取原有规范实体，因此订单、客户、销量等新增来源只能形成新的匹配列、候选或未匹配诊断，不能增删改左侧规范实体。只有显式 `refresh` 才允许重新选择 canonical 并触发基准差异审查。
- 2026-07-11：人工改绑从“单个文件实例”升级为“来源字段契约规则”。`manual_overrides.json` 记录 `source_id + source_key + scope`；默认 `scope=source_id`，只要追加表沿用同一来源键契约，就会自动重放已确认的映射，例如 `insurance_sales / 极氪001`。只有第一次出现的新键才进入人工队列。需要仅处理一个文件实例时可显式使用 `scope=source_ref`。历史文件级覆盖会从已有 binding 回填 `source_id`，不需要人工重录；订单、客户等不同 `source_id` 不会被误套用。
- 2026-07-11：构建 Job 同时保留三个口径：adapter 写入的原始 staging Crosswalk 用于审计所有原始候选与未匹配；worker 以当前草稿人工规则物化 `published-preview-crosswalk.json`；发布后该预览成为 active Crosswalk 的结果。任务中心、通知和 Agent 的主摘要一律使用发布后预览口径，`raw_summary` 仅作为诊断补充，并显示本次将被人工规则自动消解的来源键数。
- 2026-07-11：发布回执新增 `published_summary`，唯一描述活跃 Crosswalk 的规范实体数、逻辑来源契约数、活跃来源绑定数和剩余诊断；Agent 不得用 Job 的 `result_summary`（staging 原始附件统计）说明发布后的状态。匹配管理卡片的“已接入来源契约”同样使用逻辑 `source_id` 计数，文件实例数只在构建/诊断上下文展示。历史覆盖在补齐 `source_id/scope` 元数据时，草稿与已发布快照同步迁移，不能误显示为待发布修改。
- 2026-07-11：补充基准缩减的隔离 E2E 回归。测试从已发布 Crosswalk 构造一条真实减少的规范实体，经 baseline-change API 分别选择“保留为停用”和“移除”；验证两种决策前 active 文件均不变，停用会在 staging 补回 `inactive` 记录，发布后才更新 active 与版本快照；移除则只会在发布后从 active 消失。同时保留规范化迁移回归，防止 `+`、空格或大小写变化被误报为缩减。
- 验证：Crosswalk、构建、Job 队列回归 `19 passed`；前端 `tsc --noEmit`、后端模块导入和 `git diff --check` 通过。
- 验证：追加来源锁定 active canonical 的 E2E 加入后，Crosswalk、通用构建与 Job 队列回归 `22 passed`；前端 `tsc --noEmit` 与 `git diff --check` 通过。
- 验证：来源字段契约规则复用回归覆盖“一个表人工改绑，另一追加表以同一 `source_id` 自动命中”；Crosswalk、通用构建与 Job 队列 `23 passed`，前端 `tsc --noEmit` 与 `git diff --check` 通过。
- 验证：构建 Job 主摘要改为发布后预览口径后，Job 队列、Crosswalk 和通用构建回归 `23 passed`。
- 验证：`16 passed`（Crosswalk、发布、通用构建、运行时查找、语义资产 registry）；前端 `tsc --noEmit` 和 `git diff --check` 通过。
