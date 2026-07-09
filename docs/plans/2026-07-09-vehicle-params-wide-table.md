# vehicle_params_wide 建表与初始物化

## 目标

- [x] 在 `insight_data.public` 建立 `vehicle_params_wide`。
- [x] 一行代表一个款型，业务主键为 `brand + serial_name + car_name`。
- [x] 从 `vehicle_params` 固化常用维度：上市时间、能源类型、级别、价格、价格段、品牌、车系。
- [x] 从 `vehicle_serial_info` 冗余销售状态：`sale_status`。
- [x] 先做一次初始物化，暂不开发定时/手动刷新任务。
- [x] 增加适合问数过滤和关联的索引。

## 命名

采用 `vehicle_params_wide`，不采用 `vehicle_pramas_wide`，避免拼写错误。

## 数据来源

- `vehicle_params`
  - `brand`
  - `serial_name`
  - `car_name`
  - `type_name`
  - `type_value`
- `vehicle_serial_info`
  - `serial_name`
  - `car_name`
  - `sale_status`

## 第一版边界

- 不修改原始表。
- 不开发刷新 API。
- 不开发定时任务。
- 不迁移语义资产到宽表优先，等验证效果后再改语义资产和 guardrail。

## 实施记录

- 表名：`public.vehicle_params_wide`
- 主键：`PRIMARY KEY (brand, serial_name, car_name)`
- 初始物化行数：`29123`
- 数据基准：只从 `vehicle_params` 去重款型出发；`vehicle_serial_info` 只左连接补充销售状态，不会把额外车型带入宽表。
- 车名对齐：`vehicle_params.car_name` 是 `20款/26款`，`vehicle_serial_info.car_name` 是 `2020款/2026款`，宽表增加 `car_name_full_year` 用于状态表匹配。
- 销售状态匹配：`23298` 行匹配，`5825` 行未匹配。
- 字段完整率：
  - `launch_year` 非空：`29120`
  - `energy_type` 非空：`29120`
  - `vehicle_level` 非空：`29120`
  - `price` 非空：`29109`

## 索引

- `idx_vehicle_params_wide_launch_year`
- `idx_vehicle_params_wide_energy_type`
- `idx_vehicle_params_wide_vehicle_level`
- `idx_vehicle_params_wide_price_band`
- `idx_vehicle_params_wide_brand`
- `idx_vehicle_params_wide_serial_name`
- `idx_vehicle_params_wide_sale_status`
- `idx_vehicle_params_wide_common_filters (launch_year, energy_type, vehicle_level, sale_status)`

## 初步性能验证

```sql
EXPLAIN ANALYZE
SELECT count(*)
FROM vehicle_params_wide
WHERE launch_year = 2026
  AND energy_type = '纯电'
  AND vehicle_level <> '皮卡';
```

结果：使用 `idx_vehicle_params_wide_common_filters`，执行时间约 `1.996 ms`。

## 数据资产与 Vanna 训练

- [x] 已将 `vehicle_params_wide` 加入 `insight_data` 数据源的 `selected_tables`。
  - 当前表：`["vehicle_params", "vehicle_params_wide"]`
- [x] 已导入 `vehicle_params_wide` DDL 到 Vanna。
  - training id: `3749a9e6ed0de5b4f1ed8b7526e8cb54-hash`
- [x] 已导入 `vehicle_params_wide` 业务说明到 Vanna。
  - training id: `2c8caf4839b4338c14412b3046473129-hash`
- [x] 已验证 Vanna training data 中 `vehicle_params_wide` 统计：
  - DDL: `1`
  - documentation: `1`
  - SQL examples: `0`

## 后续

- [x] 补充 2-3 条 `vehicle_params_wide + vehicle_params` 的正确 SQL 示例训练。
- [x] 验证 Agent 是否会优先选择 `vehicle_params_wide` 计算分母。
- [x] 验证稳定后，把语义资产改成宽表优先、EAV fallback。
- [x] 增加 SQL guardrail：配置率问题如果路由包含 `vehicle_params_wide`，但生成 SQL 只用 `vehicle_params` 计算分母，则拦截并重写。
- [x] 保留 EAV flags 作为 fallback；fallback 也必须按 `brand + serial_name + car_name` 分组，不允许只按 `car_name` 合并同名款型。

## 最新验证

问题：

```text
2026年上市的纯电新车中，空气悬架的配备率是多少？
```

生成 SQL 已采用：

- `vehicle_params_wide` 作为分母，筛选 `launch_year = 2026`、`energy_type = '纯电'`、排除 `vehicle_level = '皮卡'`。
- `vehicle_params` 作为配置明细表，按 `brand + serial_name + car_name` 回连判断 `可调悬架种类` 是否包含 `空气悬架`。

验证结果：

- 分母：`618`
- 分子：`151`
- 配置率：`24.43%`
- SQL 执行耗时：约 `72 ms`

## 当前优化方案

### 核心原则

`vehicle_params` 是 EAV 明细表，适合保存完整配置明细，但不适合每次从中反复计算上市时间、能源类型、级别、价格、品牌、车系、销售状态等常用维度。

问数链路采用两层结构：

1. `vehicle_params_wide`：款型基础维度宽表，一行代表一个款型。
2. `vehicle_params`：配置明细 EAV 表，用于判断某个配置是否搭载。

默认款型颗粒度使用：

```text
brand + serial_name + car_name
```

不要只按 `car_name` 去重或分组，否则会合并不同品牌或车系下的同名款型。

### 推荐查询路径

有 `vehicle_params_wide` 时，配置率、配备率、搭载率等分析应使用：

```text
vehicle_params_wide 先筛分母款型
JOIN vehicle_params 查配置项
```

典型 SQL 结构：

```sql
WITH denominator AS (
  SELECT brand, serial_name, car_name
  FROM vehicle_params_wide
  WHERE launch_year = 2026
    AND energy_type = '纯电'
    AND vehicle_level IS DISTINCT FROM '皮卡'
),
numerator AS (
  SELECT DISTINCT d.brand, d.serial_name, d.car_name
  FROM denominator d
  JOIN vehicle_params vp
    ON vp.brand = d.brand
   AND vp.serial_name = d.serial_name
   AND vp.car_name = d.car_name
  WHERE vp.type_name = '可调悬架种类'
    AND vp.type_value LIKE '%空气悬架%'
    AND vp.type_value IS NOT NULL
    AND vp.type_value NOT IN ('', '-', '无', '未配备', '不配备')
)
SELECT
  COUNT(*) AS total_count,
  (SELECT COUNT(*) FROM numerator) AS equipped_count,
  ROUND((SELECT COUNT(*) FROM numerator) * 100.0 / NULLIF(COUNT(*), 0), 2) AS config_rate_pct
FROM denominator;
```

### EAV Flags Fallback

如果没有 `vehicle_params_wide`，或者某些临时维度还没有固化进宽表，可以 fallback 到 EAV flags。

fallback 仍然必须一次扫描相关 `type_name`，按 `brand + serial_name + car_name` 聚合：

```sql
WITH car_flags AS (
  SELECT
    brand,
    serial_name,
    car_name,
    BOOL_OR(type_name = '上市时间' AND type_value >= '2026-01-01' AND type_value < '2027-01-01') AS is_target_launch,
    BOOL_OR(type_name = '能源类型' AND type_value = '纯电') AS is_target_energy,
    BOOL_OR(type_name = '级别' AND type_value = '皮卡') AS is_pickup,
    BOOL_OR(
      type_name = '可调悬架种类'
      AND type_value IS NOT NULL
      AND type_value NOT IN ('', '-', '无', '未配备', '不配备')
      AND type_value LIKE '%空气悬架%'
    ) AS has_air_suspension
  FROM vehicle_params
  WHERE brand IS NOT NULL
    AND serial_name IS NOT NULL
    AND car_name IS NOT NULL
    AND type_name IN ('上市时间', '能源类型', '级别', '可调悬架种类')
  GROUP BY brand, serial_name, car_name
)
SELECT
  COUNT(*) FILTER (WHERE is_target_launch AND is_target_energy AND NOT is_pickup) AS total_count,
  COUNT(*) FILTER (WHERE is_target_launch AND is_target_energy AND NOT is_pickup AND has_air_suspension) AS equipped_count,
  ROUND(
    COUNT(*) FILTER (WHERE is_target_launch AND is_target_energy AND NOT is_pickup AND has_air_suspension) * 100.0
    / NULLIF(COUNT(*) FILTER (WHERE is_target_launch AND is_target_energy AND NOT is_pickup), 0),
    2
  ) AS config_rate_pct
FROM car_flags;
```

### 禁止路径

以下 SQL 形态容易慢或口径错误，应避免：

- 多层 `EXISTS` / `NOT EXISTS` 自关联 `vehicle_params`。
- `COUNT(DISTINCT car_name)` 作为默认款型口径。
- `GROUP BY car_name` 作为默认款型口径。
- 从 `car_name` 里的 `26款` 推断上市年份。
- 用 `car_name LIKE '%皮卡%'` 判断皮卡。

### 索引策略

宽表常用维度索引：

```sql
CREATE INDEX IF NOT EXISTS idx_vehicle_params_wide_common_filters
ON public.vehicle_params_wide (launch_year, energy_type, vehicle_level, sale_status);
```

EAV 明细回连索引：

```sql
CREATE INDEX IF NOT EXISTS idx_vehicle_params_model_type
ON public.vehicle_params (brand, serial_name, car_name, type_name);
```

这个索引不是给某个配置单独优化，而是支持通用路径：

```text
宽表筛出目标款型集合 -> 回连 EAV 查任意配置项
```

### Agent Guardrail

后端已增加硬规则：

- 配置率问题如果路由包含 `vehicle_params_wide`，但生成 SQL 只使用 `vehicle_params` 计算分母，则拦截并重写。
- 配置率 fallback 如果只按 `car_name` 分组，则拦截并要求使用 `brand + serial_name + car_name`。
- 空气悬架 reference 要求使用 `type_name = '可调悬架种类'` 且 `type_value` 包含 `空气悬架`。

### 后续演进

当前阶段保持数据库工程侧手动维护：

- 手动运行建表/刷新 SQL。
- 手动维护索引。
- 不做前端刷新入口。
- 不做定时任务。

如果这套宽表长期稳定，再考虑：

- migration 化。
- 定时刷新或手动刷新 API。
- 物化视图。
- `vehicle_config_facts`：把 400 多个配置统一成 `(brand, serial_name, car_name, config_name, has_config)`。
