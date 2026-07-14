# 分析模型语义对 Agent 不可见问题与最小修复

## 问题记录

2026-07-14 的产品配置分析 Session 中，Agent 在计算“更新次数”和“上市周期”时自行调用：

```json
{
  "model_id": "产品配置分析",
  "table_names": ["vehicle_params_wide"]
}
```

生成结果 `sql-gen-19f77a9df52d` 的路由范围因此只有 `vehicle_params_wide`。SQL 生成器随后依据 `measure:launch_cycle` 正文生成了使用 `vehicle_params` 的 SQL，校验阶段按 generation 登记范围将其识别为越界表，最终反复返回“SQL 引用了未授权数据表：vehicle_params”。

`vehicle_params` 实际已经被“产品配置分析”模型授权。错误中的“未授权”只表示它不在该 generation 的表范围中，不代表不在模型或数据源授权范围中。

## 根因

分析模型被选择后，DeepAgents 原先只注入：

- 模型 frontmatter；
- 模型 Playbook 正文；
- 资产关联和逻辑数据集摘要；
- 模型声明的语义资产 ID。

模型引用的度量值、维度和颗粒度没有形成可供外层 Agent 渐进加载的全局索引。外层 Agent 在调用 SQL 生成工具前不能稳定发现并读取具体度量值的表口径，而完整语义正文主要在内层 SQL 生成阶段才加载。

这导致了职责割裂：外层 Agent 先把表范围锁定为 `vehicle_params_wide`，内层 SQL 生成器才读到“上市周期必须使用 `vehicle_params`”。

## 本次最小修复

本轮不增加 `required_tables`、`forbidden_tables` 或 `execution_contract` 等结构化 frontmatter，只验证正文语义是否足以改善 Agent 行为。

改动包括：

1. `AnalyticsModelRegistry.get_model_context()` 解析当前模型声明的全部度量值、维度和颗粒度，并返回其 frontmatter 和完整定义路径。
2. 新增模型专属 `SemanticAssetsMiddleware`，参照 DeepAgents `SkillsMiddleware` 的 metadata-first 模式，将当前模型语义资产 frontmatter 索引和允许 ID 范围写入 State，并在模型调用时注入索引。
3. DeepAgents `AGENTS.md` 明确语义优先级：具体度量值 Reference > 具体度量值 > 模型 Playbook > 通用维度 > 字段匹配或模型推断。
4. 模型问数由 Agent 从 State 中的模型专属索引选择相关资产，通过 `selected_semantic_asset_ids` 传给 `database_sql_generate`；工具按 State 中的允许范围校验后，由 SQL Generator 从 Registry 精确加载完整正文。除非用户明确指定物理表或正在调试表，否则 Agent 不自行缩小 `table_names`。
5. 明确区分“该 generation 表范围不包含”与“模型或数据源未授权”，避免错误诊断。
6. 复测发现，仅靠 Agent 不传 `table_names` 仍不够：旧表路由器没有消费 `model_id`，会按问题字段把范围缩成 `vehicle_params_wide`。现已让表路由器在选择模型且调用方未显式指定表时，默认纳入该模型 `data_assets.tables` 声明的全部数据库表；“产品配置分析”因此同时授权 `vehicle_params` 与 `vehicle_params_wide`，具体物理实现继续由度量值语义和 SQL 生成器决定。
7. Agent 必须原样传递用户的业务问题，不得自行补入物理表名、字段、EAV/宽表偏好或 CTE 实现，也不得仅为强制自己推断的表偏好发起 SQL 修改审批。
8. 选中的 `analytics_model_id`、模型专属语义资产 metadata 和允许 ID 范围已加入 DeepAgent 自定义 State；`database_sql_generate` 通过 LangChain 原生注入的 `ToolRuntime.state` 读取模型和允许范围。`runtime` 对 LLM 隐藏，State 中的模型优先于 LLM 工具参数，因此模型路由不再依赖 Agent 主动填写 `model_id`。
9. 语义资产正文的执行边界属于 SQL Generator，而不是外层 Agent。模型问数不再使用全库 Top 8 模糊资产检索，也不再将选中正文截断为 2400 字符；只按 Agent 传入并经模型范围校验的 ID 加载完整定义。未选择模型的兼容调用仍保留原通用检索。
10. `database_sql_validate` 与 `database_sql_execute` 在 Agent 模式改为只信任 `generation_id`，并在服务端加载登记 SQL。Agent 不再复制 SQL，因此删除注释、调整空白或截断内容不会造成 generation 一致性误报。
11. DeepAgents HITL 恢复器支持一次返回多个 interrupt：逐一展示和收集决策后，使用 LangGraph `interrupt_id -> decision` 映射恢复，避免并行审批触发 `multiple pending interrupts`。

## 暂不实施

以下增强先记录，待最小修复测试结果不稳定时再评估：

- 为度量值增加结构化 `required_tables`；
- 根据度量值依赖自动合并表范围；
- SQL 注册 generation 前增加实际引用表与路由范围的一致性校验；
- 区分 `user_selected_tables`、`agent_suggested_tables` 和 `router_selected_tables`。

## 验收场景

使用“产品配置分析”模型重新执行以下问题：

> 计算新能源行业 2021–2026 年每年的更新次数和平均上市周期。

验收点：

- Agent 能从 `SemanticAssetsMiddleware` 注入的模型专属索引看到 `measure:launch_cycle` 和 `measure:launch_update_count` 的 frontmatter 与资产 ID。
- Agent 只把两个相关 ID 放入 `selected_semantic_asset_ids`，不复制正文、不传入全部模型资产。
- SQL Generator 只加载这两个资产及命中的度量值 Reference 完整正文，不混入 Top 8 其他资产，也不做 2400 字符截断。
- Agent 不自行传入仅包含 `vehicle_params_wide` 的 `table_names`。
- Agent 将两个度量值与 `vehicle_params` 的完整上市事件集合关联起来。
- SQL 先基于完整历史事件计算 `LAG`，再按本次上市年份筛选。
- 不再出现把 generation 局部表范围误述为全局未授权的循环。

如果 Agent 仍然忽略正文或错误锁表，再进入结构化依赖和语义前置路由方案。
