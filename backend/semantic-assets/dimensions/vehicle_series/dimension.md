---
formatter: semantic-asset
id: vehicle_series
name: 车系
type: dimension
description: 跨源品牌车系维度，用于将销量、上险量和产品配置等来源中的品牌车系解析到同一个可 Join 的实体键。
aliases:
  - 车系跨源匹配
  - 销量配置关联
  - 品牌车系对齐
  - vehicle series resolution
tags:
  - 汽车销量
  - 汽车产品配置
  - 跨源分析
  - vehicle_series
resolution_mode: entity_lookup
version: 0.2.0
resolution:
  mode: entity_lookup
  canonical:
    key: entity_key
    fields: [canonical_brand, canonical_series]
  bindings:
    - asset_ref: table_asset:tbl_73d53d94a3a29ff425235dfa
      display_name: 2023年1-5月乘用车市场上险量.xlsx · 工作表1 (21)
      fields:
        brand: 品牌
        series: 1-子车型
    - asset_ref: dbs_77982e981bac4a6fa8.vehicle_params_wide
      display_name: insight_data · vehicle_params_wide
      fields:
        brand: brand
        series: serial_name
  reference_path: references/active_crosswalk.json
build_skill:
  name: build-semantic-dimension
  adapter: entity_crosswalk_v1
  generated_resources: [references/active_crosswalk.json, references/generated_crosswalk.json, references/manual_overrides.json, references/source_registry.json]
changelog:
  - version: 0.2.0
    date: 2026-07-10
    changes: 全品牌全量构建 (job sdb_e80e195cbebb)；规范车系 1,831 条，auto_matched 498，canonical_only 1,333；来源侧匹配覆盖率 60.3%。
created: 2026-07-10 00:00:00
updated_at: 2026-07-11 21:14:11
---

# 车系

## 目标

将不同来源中名称不完全一致的品牌、车系解析到同一个 `entity_key`，用于销量、上险量、产品配置等跨源联合分析。产品配置表是规范实体的唯一基准；其他来源只提供可追加的来源绑定与匹配诊断。

这不是物理外键，也不是要求原始表改名。原始字段保持不变；车系维度保存每个来源各自的原始键，以及它们共同指向的规范实体。

## 构建方式

本维度由 `build-semantic-dimension` Skill 的 `vehicle_series_full` adapter 构建。Skill 负责从多个来源清洗并生成 binding 草案；本目录只保存车系语义、来源契约与可迁移的 crosswalk 结果。构建脚本不由 Agent 自动执行；只有手工运行或被显式授权的工作流才可执行。

## 规范实体

```yaml
entity_key: 比亚迪::秦plus
canonical_brand: 比亚迪
canonical_series: 秦PLUS
```

`canonical_series` 单独不是唯一键。所有跨源 Join 必须使用 `entity_key`，或完整使用 `canonical_brand + canonical_series`。

`canonical_brand` 与 `canonical_series` 必须逐字取自产品配置表 `vehicle_params_wide.brand` 和 `vehicle_params_wide.serial_name`。`entity_key` 也只由这对配置表原始值派生：`normalize(brand) + "::" + normalize(serial_name)`。来源表不参与三者的生成，只能提供绑定证据；它们不从销量、上险量或其他来源改写。

## 来源键契约

### 销量 / 上险量表

```yaml
source_kind: table_asset
key_fields: [品牌, 1-子车型]
example:
  品牌: 比亚迪
  1-子车型: 比亚迪秦PLUS
```

### 产品配置 PostgreSQL

```yaml
source_kind: database_table
table: vehicle_params_wide
key_fields: [brand, serial_name]
example:
  brand: 比亚迪
  serial_name: 秦PLUS
```

## AI 使用规则

1. 涉及销量/上险量与产品配置联合分析时，优先调用 `semantic_entity_lookup` 查询 frontmatter `resolution.reference_path` 指向的活跃 crosswalk；不得扫描、读取或使用同目录其他候选 reference。只有用户明确要求版本比较、审核候选或刷新/发布维度时，才允许直接读取非活跃 reference。
2. 配置库 `brand + serial_name` 定义完整的规范车系全集。来源表在销量侧用 `品牌 + 1-子车型` 查找绑定；配置侧用原始 `brand + serial_name` 查找规范实体；两侧只通过同一个 `entity_key` 合并。
3. 只有 `resolution.status` 为 `auto_matched` 或 `accepted` 且 `join_eligible=true` 的记录可以进入正式统计。
4. `candidate` 只能用于提出待审核映射，不得写入最终统计分子、分母或明细结果。
5. `unmatched` 必须保留并在结果中报告覆盖范围，不能因为名称相似而自行合并。
6. 遇到新的未匹配名称，输出包含两侧候选、匹配理由和置信度的草案；审核通过后才新增或更新 crosswalk 记录。
7. 配置库中没有来源绑定的规范实体状态为 `canonical_only`：它仍是有效语义实体，但不能参与当前来源表与配置表的跨源 Join。

## 自动归一化边界

允许：

- Unicode NFKC、大小写、全半角、空格和标点归一。
- 仅在品牌已经唯一解析后，移除销量车系中该品牌的已确认前缀。
- 在同一品牌范围内做候选排序。
- 当来源品牌是集团口径、别名或缺失时，允许车系经同样规范化后在**全配置库唯一精确命中**，并以该唯一配置车系自动绑定；这不使用全局模糊匹配。

禁止：

- 跨品牌模糊匹配。
- 仅因全局同名车系有多个候选而自动选择其中一个。
- 将 `元` 自动合并到 `元PLUS`，将 `瑞虎3` 自动合并到 `瑞虎3x` 等父子/近似车系。
- 仅用 `canonical_series` 跨品牌 Join。

## 可迁移文件格式

`references/*.json` 使用 `entity-resolution-crosswalk` 格式。`records` 只包含配置库规范实体，每一条记录都有 `entity`、`bindings`、`resolution` 三部分；来源侧 `candidate` / `unmatched` 单独放在 `source_diagnostics`，不计入规范实体数量。

```json
{
  "entity": {
    "entity_key": "比亚迪::秦plus",
    "canonical_brand": "比亚迪",
    "canonical_series": "秦PLUS"
  },
  "bindings": [
    {
      "source_kind": "table_asset",
      "key_fields": {"品牌": "比亚迪", "1-子车型": "比亚迪秦PLUS"}
    },
    {
      "source_kind": "database_table",
      "table_or_sheet": "vehicle_params_wide",
      "key_fields": {"brand": "比亚迪", "serial_name": "秦PLUS"}
    }
  ],
  "resolution": {
    "status": "accepted",
    "join_eligible": true
  }
}
```
