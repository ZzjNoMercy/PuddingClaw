# 资产关联实施计划

创建时间：2026-07-11  
状态：实施中

## 核心原则

- **白盒化**：关系必须说明资产、字段、基数、粒度、覆盖率与运行时实际路径。
- **AI Native**：AI 可以发现候选和规划路径；已发布关系才可用于正式分析，不要求用户预建完整传统 BI 关系网。
- **第一性原理**：只保存不可替代的语义边。共同维度路径由模型动态组合，不为每对事实表重复建关系。
- **对抗式审查**：保存和运行时检查端点范围、图连通性、基数、重复计数、粒度和未匹配边界。

## 范围

资产关联是工作区级、可复用的语义资产；分析模型只选择已发布资产关联，不复制关系定义。

第一版仅支持：

1. `dimension_binding`（关联维度）：一个数据资产的字段接入一个已发布维度。
2. `direct_join`（字段关联）：两张数据资产存在稳定业务键时，声明字段映射、基数、粒度、Join 方向和重复计数约束。

不实现 `via_dimension`、Bridge 或自由语义 Join 创建器。模型选择多个资产和共同维度时，系统自动推导共同维度路径。

## 文件结构与格式

```text
backend/semantic-assets/relations/{relation_slug}/relation.md
```

```yaml
formatter: asset-relation
id: insurance_sales_to_vehicle_series
name: 上险量关联车系维度
type: relation
relation_type: dimension_binding
version: 0.1.0
description: 将上险量来源的品牌和子车型映射为规范车系。
status: published
tags: [汽车, 上险量]

asset:
  ref: table_asset:tbl_xxx
  display_name: 乘用车上险量
  key_fields: [品牌, 1-子车型]

dimension:
  ref: dimension:vehicle_series
  output_key: entity_key

cardinality: many_to_one
grain: [月份, 品牌, 1-子车型]
rules:
  - 仅使用已发布且 join_eligible=true 的映射。
```

字段关联使用 `left`、`right`、`field_mapping`、`cardinality`、`grain` 与 `join_type`。

## 任务清单

### 1. 关系 Registry 与 API

- [x] 扩展语义资产 Registry，扫描、创建、导入、详情与文件树支持 `relations/**/relation.md`。
- [x] 定义并校验两种关系 frontmatter；拒绝未知类型、非法资产引用、字段数量不一致和无效基数。
- [x] API 提供资产关联的列表、创建、详情、刷新与结构化更新。
- [x] API 提供可选数据资产及字段候选，Excel 读取 Profile，数据库读取表字段。

### 2. 分析模型图约束

- [x] 模型 frontmatter 增加 `asset_relations`。
- [x] 模型创建时校验关系端点均在已选数据资产内。
- [x] 多资产模型校验资产图连通：边来自字段关联或已选维度的 published binding。
- [x] 移除资产时前端自动取消相关关联；后端保存时再次校验。

### 3. Agent 与 Trace

- [x] 注入模型已选关系、共同维度路径、基数与粒度要求。
- [x] Trace/模型上下文携带关系、推导路径和缺失引用边界。
- [x] Agent 不得使用未选关系或仅凭同名字段跨资产 Join。

### 4. 前端工作台

- [x] 语义资产导航增加“资产关联”分类、搜索和筛选。
- [x] 创建弹窗提供“关联维度 / 字段关联”两种结构化选择。
- [x] 关联维度编辑器选择资产、维度和字段。
- [x] 字段关联编辑器选择左右资产、字段配对和基数。
- [x] 模型创建/编辑页仅展示当前数据资产范围内可用关系，并在移除端点时自动取消无效选择。

### 5. 验证

- [x] Registry、模型图校验、提示词注入单测。
- [x] 前端 `tsc` 检查。
- [x] 车系 + 上险量 + 产品配置案例：共同维度路径可被解析；无关系时后端明确拒绝。

## 进度记录

| 时间 | 进展 | 结果 |
| --- | --- | --- |
| 2026-07-11 | 方案收敛 | 仅保留关联维度和字段关联；共同维度路径由模型动态组合。 |
| 2026-07-11 | Registry 与模型校验 | 已完成关系文件扫描、结构化 API、模型连通图校验与关系上下文注入；进入模型编辑和 Trace 细化阶段。 |
