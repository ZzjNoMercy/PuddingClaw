# 在售车型过滤与关联表方案

创建时间：2026-07-09 CST

状态：待评审，暂不开发

## 背景

当前 `vehicle_params` 是车型配置分析的核心明细表。用户后续希望分析“在售车型”的配置率、价格段、车型级别、上市时间等问题，例如：

- 在售车型空气悬架配置率是多少？
- 在售新能源车型按价格段的激光雷达搭载率是多少？
- 当前在售车系里，哪些配置渗透率最高？

新增一张车型销售状态表后，可以先用销售状态筛选出目标车型，再回到 `vehicle_params` 做配置分析。

截图中的新增表字段包括：

```text
serial_name
serial_url
serial_id
car_id
car_name
sale_status
```

用户建议第一版使用 `serial_name + car_name` 作为关联 key。

## 问题判断

这个问题不应主要依赖 Vanna “学会 JOIN”。Vanna 的 DDL、documentation、question-SQL pair 训练仍然有价值，但它不能保证每次都召回正确关系，也不能保证模型一定遵守业务口径。

更稳的方式是：

```text
业务口径：在售车型
  -> 语义资产定义
  -> table catalog 显式关系
  -> 可选数据库视图/物化视图
  -> Vanna 训练样例
  -> 后端 guardrail 校验
```

核心原则：

- Vanna 负责生成 SQL。
- PuddingClaw 负责把“在售”变成可复用、可验证的业务过滤器。
- 不允许模型通过 `car_name` 年款、文本猜测、模糊条件自行推断是否在售。

## 目标

第一版目标：

- 新增“在售车型”分析范围。
- 用 `serial_name + car_name` 关联销售状态表和 `vehicle_params`。
- 让 Agent 在用户提到“在售、当前在售、售卖中、停售”等口径时，稳定使用销售状态表。
- 在 Trace 中能看到是否命中“在售状态”语义资产，以及实际使用的关联路径。

长期目标：

- 如果后续数据质量允许，优先升级为 `car_id` 关联。
- 对高频分析范围提供视图或物化视图，降低 LLM 自由 JOIN 的失败率。
- 将“在售状态”纳入语义资产约束体系，避免口径只停留在 prompt。

## 非目标

第一版不做：

- 重构 `vehicle_params` 表结构。
- 建完整汽车主数据模型。
- 自动修复所有错误 JOIN。
- 用 Vanna 训练替代后端校验。
- 强制要求所有数据源都有 `car_id`。

## 建议表设计

建议新增表名：

```text
vehicle_sale_status
```

建议字段：

```sql
CREATE TABLE vehicle_sale_status (
    serial_name text NOT NULL,
    serial_url text,
    serial_id integer,
    car_id integer,
    car_name text NOT NULL,
    sale_status varchar(255) NOT NULL,
    source text,
    snapshot_date date,
    updated_at timestamp
);
```

第一版建议唯一键：

```sql
UNIQUE (serial_name, car_name)
```

建议索引：

```sql
CREATE INDEX idx_vehicle_sale_status_sale_status
ON vehicle_sale_status (sale_status);

CREATE INDEX idx_vehicle_sale_status_serial_car
ON vehicle_sale_status (serial_name, car_name);

CREATE INDEX idx_vehicle_sale_status_car_id
ON vehicle_sale_status (car_id)
WHERE car_id IS NOT NULL;
```

说明：

- `serial_name + car_name` 作为第一版主关联 key，符合当前数据可得性。
- `car_id` 保留为未来更稳定的关联键。
- `snapshot_date` 用于表示该销售状态快照来自哪一天，避免“当前在售”的时间口径不清。

## 与 vehicle_params 的关联策略

第一版默认关联：

```sql
vp.serial_name = ss.serial_name
AND vp.car_name = ss.car_name
```

其中：

```text
vp = vehicle_params
ss = vehicle_sale_status
```

如果 `vehicle_params` 当前没有稳定的 `serial_name` 字段，需要先确认：

- 是否已有等价字段，例如 `series_name`、`车系`、`serial_name`。
- 是否需要从 `vehicle_params` 中 `type_name = '车系'` 或实体辅助字段还原。
- 是否应该先生成一个中间映射表，而不是让 SQL 每次从 EAV 明细里临时抽取。

不建议第一版只用：

```sql
vp.car_name = ss.car_name
```

原因：

- 不同车系可能存在同名款型。
- 数据源中款型命名可能有空格、标点、年款前缀差异。
- 单字段文本 JOIN 会放大误匹配风险。

## 推荐查询入口

为了降低 LLM 生成复杂 JOIN 的失败率，建议提供一个视图。

如果 `vehicle_params` 中已有 `serial_name`：

```sql
CREATE VIEW vehicle_params_on_sale AS
SELECT vp.*
FROM vehicle_params vp
JOIN vehicle_sale_status ss
  ON vp.serial_name = ss.serial_name
 AND vp.car_name = ss.car_name
WHERE ss.sale_status = '在售';
```

如果 `vehicle_params` 是 EAV 结构且车系存在于 `type_name = '车系'`，建议不要让 Agent 每次临时写复杂 JOIN，而是先建设中间事实视图：

```text
vehicle_model_identity
  - serial_name
  - car_name
  - car_id 可选
```

再基于它构造：

```text
vehicle_params_on_sale
```

这样用户问“在售车型配置率”时，Agent 可以优先查视图，而不是自由拼接多层 EAV JOIN。

## 语义资产设计

建议新增维度或过滤器语义资产：

```text
backend/semantic-assets/dimensions/sale_status/dimension.md
```

建议 YAML：

```yaml
---
formatter: semantic-asset
name: 在售状态
type: dimension
description: 定义车型是否处于在售、停售等销售状态，用于筛选 vehicle_params 的分析范围。
aliases:
  - 在售
  - 当前在售
  - 售卖中
  - 停售
tags:
  - 汽车
  - vehicle_params
  - 销售状态
version: 0.1.0
created: 2026-07-09
updated_at: 2026-07-09
---
```

正文建议：

```md
# 在售状态

当用户提到“在售车型”“当前在售”“售卖中车型”时，必须使用销售状态表或在售视图筛选车型范围。

默认表：

- `vehicle_sale_status`

默认关联：

- `vehicle_params.serial_name = vehicle_sale_status.serial_name`
- `vehicle_params.car_name = vehicle_sale_status.car_name`

默认条件：

- 在售：`vehicle_sale_status.sale_status = '在售'`
- 停售：`vehicle_sale_status.sale_status = '停售'`

优先查询入口：

- 如果存在 `vehicle_params_on_sale`，用户问在售车型时优先使用该视图。

禁止：

- 禁止根据 `car_name` 中的年款推断是否在售。
- 禁止根据上市年份推断是否在售。
- 禁止只用 `car_name` 单字段关联销售状态，除非用户明确接受近似匹配。
```

## Table Catalog 关系

建议在 table catalog 中显式维护关系，而不是只靠 DDL 外键：

```yaml
relationships:
  - name: vehicle_params_to_sale_status
    from_table: vehicle_params
    to_table: vehicle_sale_status
    join_type: inner
    keys:
      - from: serial_name
        to: serial_name
      - from: car_name
        to: car_name
    business_usage:
      - 在售车型筛选
      - 停售车型筛选
      - 销售状态分组
    preferred_view: vehicle_params_on_sale
```

如果后续升级 `car_id`：

```yaml
relationships:
  - name: vehicle_params_to_sale_status_by_car_id
    from_table: vehicle_params
    to_table: vehicle_sale_status
    join_type: inner
    keys:
      - from: car_id
        to: car_id
    priority: high
```

## Vanna 训练建议

Vanna 训练仍然需要做，但定位是提升 SQL 生成质量，不是唯一保障。

### DDL

训练：

- `vehicle_params`
- `vehicle_sale_status`
- 可选 `vehicle_params_on_sale`

### Documentation

训练表关系说明：

```text
vehicle_sale_status 记录车型销售状态。
当用户提到在售车型、当前在售、售卖中时，需要使用 vehicle_sale_status.sale_status = '在售'。
第一版 vehicle_params 与 vehicle_sale_status 使用 serial_name + car_name 关联。
如果存在 vehicle_params_on_sale 视图，优先使用该视图。
```

### Question-SQL Pair

至少补充以下样例：

```sql
-- 在售车型数量
SELECT COUNT(DISTINCT vp.car_name) AS on_sale_car_count
FROM vehicle_params vp
JOIN vehicle_sale_status ss
  ON vp.serial_name = ss.serial_name
 AND vp.car_name = ss.car_name
WHERE ss.sale_status = '在售';
```

```sql
-- 在售车型按车系统计
SELECT ss.serial_name, COUNT(DISTINCT ss.car_name) AS car_count
FROM vehicle_sale_status ss
WHERE ss.sale_status = '在售'
GROUP BY ss.serial_name
ORDER BY car_count DESC;
```

```sql
-- 在售车型某配置配置率
WITH scope AS (
  SELECT DISTINCT vp.car_name, vp.serial_name
  FROM vehicle_params vp
  JOIN vehicle_sale_status ss
    ON vp.serial_name = ss.serial_name
   AND vp.car_name = ss.car_name
  WHERE ss.sale_status = '在售'
),
hit AS (
  SELECT DISTINCT vp.car_name, vp.serial_name
  FROM vehicle_params vp
  JOIN scope s
    ON vp.serial_name = s.serial_name
   AND vp.car_name = s.car_name
  WHERE vp.type_name = '可调悬架种类'
    AND vp.type_value LIKE '%空气悬架%'
)
SELECT
  COUNT(*) AS total_count,
  COUNT(h.car_name) AS hit_count,
  ROUND(COUNT(h.car_name)::numeric / NULLIF(COUNT(*), 0), 4) AS config_rate
FROM scope s
LEFT JOIN hit h
  ON s.serial_name = h.serial_name
 AND s.car_name = h.car_name;
```

## Guardrail 设计

当语义资产命中“在售状态”时，后端应校验 SQL。

第一版规则：

```text
如果用户问题包含“在售、当前在售、售卖中”
则 SQL 必须满足以下任一条件：

1. 使用 vehicle_params_on_sale
2. 使用 vehicle_sale_status 且包含 sale_status = '在售'
```

同时禁止：

```text
car_name LIKE '26款%'
上市时间 LIKE '2026-%'
```

这些条件不能替代“在售状态”。

Trace 建议展示：

```json
{
  "semantic_resolution": {
    "matched_assets": ["dimension:sale_status"]
  },
  "semantic_constraints": {
    "loaded": [
      {
        "asset_id": "dimension:sale_status",
        "rule": "require_sale_status_filter"
      }
    ],
    "conflicts": [],
    "action": "passed"
  },
  "join_path": {
    "from": "vehicle_params",
    "to": "vehicle_sale_status",
    "keys": ["serial_name", "car_name"]
  }
}
```

## 与配置率、上市时间等语义资产的组合

用户可能同时提到多个口径：

```text
2026 年上市的在售车型空气悬架配置率是多少？
```

此时应同时满足：

- 上市时间维度：必须使用 `type_name = '上市时间'`。
- 在售状态维度：必须使用 `vehicle_sale_status.sale_status = '在售'` 或 `vehicle_params_on_sale`。
- 配置率度量值：默认排除车型级别为皮卡。
- 空气悬架 reference：必须使用 `type_name = '可调悬架种类'`。
- 默认颗粒度：款型。

不能用任何一个条件替代另一个条件。

错误示例：

```sql
WHERE car_name LIKE '26款%'
```

原因：

- 这既不能代表 2026 年上市，也不能代表在售。

## 数据质量要求

上线前需要检查：

- `vehicle_sale_status.serial_name` 是否能和 `vehicle_params` 的车系字段稳定对齐。
- `vehicle_sale_status.car_name` 是否和 `vehicle_params.car_name` 命名一致。
- 同一个 `(serial_name, car_name)` 是否存在多个销售状态。
- `sale_status` 是否只有稳定枚举，例如：`在售`、`停售`、`未上市`。
- 销售状态是否有快照日期，避免历史状态覆盖当前状态。

建议增加数据质量 SQL：

```sql
-- 重复 key 检查
SELECT serial_name, car_name, COUNT(*)
FROM vehicle_sale_status
GROUP BY serial_name, car_name
HAVING COUNT(*) > 1;
```

```sql
-- 状态枚举检查
SELECT sale_status, COUNT(*)
FROM vehicle_sale_status
GROUP BY sale_status
ORDER BY COUNT(*) DESC;
```

```sql
-- 无法匹配 vehicle_params 的销售状态记录
SELECT ss.serial_name, ss.car_name
FROM vehicle_sale_status ss
LEFT JOIN vehicle_params vp
  ON vp.serial_name = ss.serial_name
 AND vp.car_name = ss.car_name
WHERE vp.car_name IS NULL
LIMIT 100;
```

## 落地阶段

### 阶段 1：数据接入与口径固化

- 新增 `vehicle_sale_status` 表。
- 导入销售状态数据。
- 检查 `(serial_name, car_name)` 唯一性和匹配率。
- 新增 `在售状态` 语义资产。
- 将关系写入 table catalog。

### 阶段 2：查询入口稳定化

- 建 `vehicle_params_on_sale` 视图。
- Vanna 训练 DDL、documentation、question-SQL pair。
- 在 `database_knowledge_query` Trace 中展示命中的语义资产和 join path。

### 阶段 3：后端约束

- 增加“在售状态” SQL guardrail。
- 当问题命中在售语义资产但 SQL 未使用销售状态表或视图时，阻断执行。
- 记录冲突原因到 Trace。

### 阶段 4：优化与升级

- 评估 `car_id` 可用性。
- 如果 `car_id` 覆盖率高，升级默认 JOIN key。
- 对高频范围构建物化视图。
- 将在售状态纳入前端语义资产编辑与预览。

## 待决问题

- `vehicle_params` 是否已有稳定 `serial_name` 字段。
- `sale_status` 的枚举值是否只有 `在售/停售`，还是还包括 `未上市/即将上市/进口停售` 等。
- 销售状态是否需要保留历史快照，还是只保留最新状态。
- “在售车型”是否默认排除皮卡，需要和配置率默认排除皮卡规则叠加确认。
- 用户问“停售车型”时，是否也允许复用同一张表和同一个维度。

## 当前结论

第一版建议采用：

```text
vehicle_sale_status
  key: serial_name + car_name
  status: sale_status

vehicle_params_on_sale
  作为在售车型配置分析的优先查询入口

semantic asset: 在售状态
  负责把用户语言映射到销售状态筛选口径

guardrail
  负责阻止模型用年款、上市时间或文本猜测替代在售状态
```

这套方案比单纯训练 Vanna JOIN 示例更稳定，也能和已有的上市时间、配置率、车型级别、颗粒度等语义资产组合。
