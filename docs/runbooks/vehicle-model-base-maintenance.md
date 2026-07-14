# `vehicle_model_base` 字段新增与维护手册

## 1. 文档目的

本文固化 `vehicle_model_base` 的完整维护流程，适用于：

- 从 `vehicle_params` EAV 明细中挑选稳定的款型基础属性并物化；
- 新增或修改基础字段、索引和字段注释；
- 为物化字段建立语义维度及固定分段；
- 把维度注册到分析模型，并同步报告规则、表路由和 Vanna 训练数据；
- 验证数据库、语义资产、Agent 路由和 NL2SQL 是否使用一致口径。

维护时不要只修改数据库表。一个新字段至少涉及数据库、语义资产、分析模型、路由、Vanna 和测试六层；漏掉任意一层，都可能导致 Agent 看得到模型却仍然选错表或错误计算。

## 2. 表定位与历史决策

### 2.1 为什么叫 `vehicle_model_base`

旧表名是 `vehicle_params_wide`，容易让人误以为要把 `vehicle_params` 的所有配置项横向展开。当前设计不是通用宽表，而是款型基础属性当前快照，因此改名为：

```text
vehicle_model_base
```

表的业务主键为：

```text
brand + serial_name + car_name
```

一行代表一个款型。它只保存高频、稳定、适合直接筛选和分组的基础属性，不保存所有可扩展配置，也不保存同一款型属性变化的历史版本。

### 2.2 哪些字段适合进入基础表

适合物化：

- 上市日期、能源类型、车型级别；
- 厂商指导价、轴距、电动机总功率；
- 销售状态及匹配状态；
- 由上述字段直接派生、查询频繁且口径稳定的时间字段或价格段。

通常不适合物化：

- 空气悬架、激光雷达、座舱芯片等大量可扩展配置；
- 需要保留多值、供应商、硬件型号或配置变化历史的属性；
- 口径尚未稳定、枚举仍频繁变化的分析字段；
- 只为单次报告临时使用的计算结果。

判断原则：字段必须是款型属性，能稳定映射到完整款型键，并且物化后能显著减少 EAV 自关联。

### 2.3 物化字段与语义分段的边界

数据库只保存标准化数值，例如：

```text
wheelbase_mm
motor_power_kw
```

轴距段、电机功率段等业务分箱不物化进基础表，而是放在对应维度 Markdown 中。这样调整分段时不需要重建表，也避免数据库和语义资产出现两套区间。

价格段目前是历史上已经物化的字段；新增维度默认遵循“原始数值物化、业务分段语义化”的原则。

## 3. 当前权威文件

| 层级 | 权威文件 | 用途 |
| --- | --- | --- |
| 完整刷新 | `backend/scripts/refresh_vehicle_model_base.sql` | 创建/扩列、清空、重算、索引、注释 |
| 一次性改名 | `backend/scripts/migrate_vehicle_model_base.sql` | `vehicle_params_wide` 改名、索引改名、兼容视图 |
| Vanna 更新 | `backend/scripts/migrate_vanna_vehicle_model_base.py` | 训练最新 DDL、文档并迁移旧 SQL 样例 |
| 语义维度 | `backend/semantic-assets/dimensions/*/dimension.md` | 字段口径、颗粒度、分段、SQL Hint、禁止规则 |
| 产品配置模型 | `backend/analytics-models/产品配置分析/model.md` | 注册表和语义资产 |
| 报告规范 | `backend/analytics-models/产品配置分析/references/report-generation.md` | 报告字段映射和图表口径 |
| 表路由 | `backend/analytics/nl2sql/table_router.py` | 让问题优先命中基础表 |
| 路由测试 | `backend/tests/test_table_router.py` | 防止新增字段后路由退化 |

不要在多个临时 SQL 中分别维护建表逻辑。`refresh_vehicle_model_base.sql` 是完整重建的唯一权威 SQL。

## 4. 新增字段的完整流程

下面每一步都必须执行。

### 第一步：确认原始参数的真实枚举

不要根据报告标题或自然语言猜 `type_name`。先在数据库中查看实际枚举：

```sql
SELECT
  type_name,
  COUNT(*) AS row_count
FROM public.vehicle_params
WHERE type_name ILIKE '%关键词%'
GROUP BY type_name
ORDER BY row_count DESC, type_name;
```

以电机功率为例，实际字段是：

```text
电动机总功率[kW]
```

不能写成“电机总功率（kW）”，也不能误用以下字段：

- `前电动机最大功率[kW]`
- `后电动机最大功率[kW]`
- `系统综合功率[kW]`
- `最大功率[kW]`
- `最大净功率[kW]`

本次核验时，`电动机总功率[kW]` 有 10,669 条记录，全部为完整数字字符串；该检查结果只用于确定清洗规则，未来数据刷新后仍应重新核验。

### 第二步：检查值格式、覆盖率和重复情况

检查数值格式：

```sql
SELECT
  COUNT(*) AS total_rows,
  COUNT(*) FILTER (
    WHERE type_value ~ '^[0-9]+(\.[0-9]+)?$'
  ) AS numeric_rows,
  COUNT(DISTINCT type_value) AS distinct_values
FROM public.vehicle_params
WHERE type_name = '电动机总功率[kW]';
```

检查常见值：

```sql
SELECT
  type_value,
  COUNT(*) AS row_count
FROM public.vehicle_params
WHERE type_name = '电动机总功率[kW]'
GROUP BY type_value
ORDER BY row_count DESC, type_value
LIMIT 50;
```

检查一个款型是否存在多个不同值：

```sql
SELECT
  brand,
  serial_name,
  car_name,
  COUNT(DISTINCT type_value) AS value_count,
  ARRAY_AGG(DISTINCT type_value ORDER BY type_value) AS values
FROM public.vehicle_params
WHERE type_name = '电动机总功率[kW]'
GROUP BY brand, serial_name, car_name
HAVING COUNT(DISTINCT type_value) > 1
ORDER BY value_count DESC, brand, serial_name, car_name;
```

如果存在一款多值，不能直接用 `MAX(type_value)` 掩盖冲突。应先确认数据版本、重复原因及取值规则，再决定是否物化。

### 第三步：确认字段语义，不自行推导

优先物化数据源直接提供的权威字段。只有语义资产明确规定时才做计算。

电动机总功率是原始整车总功率，因此：

- 不计算前电机功率加后电机功率；
- 不用系统综合功率替代；
- 不用发动机功率替代；
- 缺失时保留 `NULL`，不自行补值。

### 第四步：修改完整刷新 SQL

新增字段时，检查并修改 `refresh_vehicle_model_base.sql` 的所有位置：

1. `CREATE TABLE IF NOT EXISTS` 中增加物理列；
2. `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 兼容已存在的表；
3. `model_dims` 中读取准确的 `type_name`；
4. `WHERE type_name IN (...)` 中加入该枚举；
5. `base_rows` 中完成安全清洗和类型转换；
6. `INSERT INTO` 字段列表加入新列；
7. 最终 `SELECT` 按完全相同顺序加入新列；
8. 为常用筛选/分组字段增加索引；
9. 增加 `COMMENT ON COLUMN`，写清来源、单位和禁止替代规则。

电机总功率的标准实现：

```sql
MAX(type_value) FILTER (
  WHERE type_name = '电动机总功率[kW]'
) AS motor_power_raw
```

```sql
CASE
  WHEN motor_power_raw ~ '^[0-9]+(\.[0-9]+)?$'
  THEN motor_power_raw::numeric(10, 2)
  ELSE NULL
END AS motor_power_kw
```

不要把非法值转为 `0`，因为 `0` 是有效数值，会错误进入最低功率段。

### 第五步：保留现有销售状态关联规则

修改 `model_dims` 时不要破坏销售状态关联。当前规则为：

1. 将 `26款` 等名称通过 `car_name_full_year` 转换为 `2026款`；
2. 对 `vehicle_serial_info.car_name` 执行 `btrim`；
3. 使用 `serial_name + car_name_full_year` 关联；
4. 同一规范化键同时出现“在售”和“停售”时，只要任一记录在售，就记为“在售”；
5. 同步维护 `sale_status_matched` 和 `sale_status_source`。

之前销售状态未关联的主要原因之一就是款型名称空格及年款格式不一致。新增字段时不要退回简单的 `MAX(sale_status)` 和未清洗字符串关联。

### 第六步：创建或更新语义维度

目录格式：

```text
backend/semantic-assets/dimensions/<dimension_id>/dimension.md
```

维度必须包含：

- `name`、`type: dimension`、描述和 aliases；
- `resolution.bindings`，指向 `vehicle_model_base` 的物理字段；
- 原始字段及精确 `type_name`；
- 单位和安全清洗规则；
- 款型颗粒度：`brand + serial_name + car_name`；
- 固定区间及边界定义；
- SQL Hint；
- 占比分子、分母和覆盖率；
- 缺失值处理；
- 禁止替代和禁止推导规则。

所有连续数值区间统一采用左闭右开：

```text
[下界, 上界)
```

最后一档无上界。边界值必须在文档中给出例子，避免 Agent 把恰好等于分界点的记录分错档。

电机功率维度的固定档位为：

```text
0-50kW
50-100kW
100-150kW
150-200kW
200-250kW
250-300kW
300-350kW
350-400kW
400-450kW
450-500kW
500kW以上
未知
```

### 第七步：注册到分析模型

在 `backend/analytics-models/产品配置分析/model.md` 中：

1. 确认 `data_assets.tables` 包含 `vehicle_model_base`；
2. 将新维度加入 `semantic_assets.dimensions`；
3. 递增模型补丁版本；
4. 必要时更新模型正文中的分析原则。

示例：

```yaml
semantic_assets:
  dimensions:
    - dimension:wheelbase
    - dimension:motor_power
```

只创建维度 Markdown 而不注册模型，Agent 选择“产品配置分析”时不会把该维度纳入模型上下文。

### 第八步：同步报告生成规则

更新 `references/report-generation.md`：

- `vehicle_model_base` 的职责说明；
- 物理字段映射；
- 已注册维度清单；
- 报告图表所使用的正式维度；
- 旧的“尚未注册”说明。

不要让报告 reference 继续提示 Agent 从 EAV 临时计算一个已经物化并注册的字段。

### 第九步：更新表路由器

在 `backend/analytics/nl2sql/table_router.py` 中加入：

- 自然语言关键词；
- 新物理字段存在性检查；
- 命中基础表的原因说明。

新增字段应使用独立的列存在性判断，避免在数据库尚未执行迁移时，让所有基础维度路由一起失效。例如电机功率只有在 `motor_power_kw` 已存在时才获得专属路由加分。

同时在 `backend/tests/test_table_router.py` 中：

- 把字段加入模拟基础表列清单；
- 添加自然语言问题；
- 断言 `vehicle_model_base` 得分高于 `vehicle_params`；
- 断言包含“款型基础表”路由理由。

### 第十步：更新 Vanna DDL、文档和 SQL 样例

更新 `backend/scripts/migrate_vanna_vehicle_model_base.py` 中的 `DOCUMENTATION`：

- 新字段名、单位和来源 `type_name`；
- 颗粒度；
- 何时优先使用基础表；
- 禁止替代/推导规则；
- 与 `vehicle_params` 的连接方式。

脚本的 `--apply` 模式会：

1. 从真实数据库重新训练 `vehicle_model_base` DDL；
2. 训练最新业务文档；
3. 如仍存在旧 `vehicle_params_wide` SQL 样例，则训练替换后的 SQL；
4. 新训练全部成功后，删除旧表名训练记录。

当前脚本即使没有遗留旧记录，也会刷新 DDL 和文档。

### 第十一步：数据库执行顺序

推荐在 Navicat 中执行：

1. 备份或记录当前行数及关键字段覆盖率；
2. 运行完整的 `backend/scripts/refresh_vehicle_model_base.sql`；
3. 检查事务是否成功提交；
4. 执行验证 SQL；
5. 再运行 Vanna 更新脚本。

Vanna 必须在数据库字段真实存在后更新，否则训练得到的 DDL 不包含新字段。

```bash
PYTHONPATH=backend backend/.venv/bin/python \
  backend/scripts/migrate_vanna_vehicle_model_base.py --apply
```

### 第十二步：验证

#### 数据库结构

```sql
SELECT
  column_name,
  data_type,
  numeric_precision,
  numeric_scale
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'vehicle_model_base'
ORDER BY ordinal_position;
```

#### 主键唯一性

```sql
SELECT
  COUNT(*) AS rows,
  COUNT(DISTINCT (brand, serial_name, car_name)) AS model_keys
FROM public.vehicle_model_base;
```

两个数必须相同。

#### 新字段覆盖率

```sql
SELECT
  COUNT(*) AS all_models,
  COUNT(motor_power_kw) AS known_models,
  ROUND(COUNT(motor_power_kw)::numeric / NULLIF(COUNT(*), 0), 4) AS coverage,
  MIN(motor_power_kw) AS min_kw,
  MAX(motor_power_kw) AS max_kw
FROM public.vehicle_model_base;
```

还应按能源类型和年份检查覆盖率，避免整体覆盖率掩盖局部分组缺失：

```sql
SELECT
  launch_year,
  energy_type,
  COUNT(*) AS all_models,
  COUNT(motor_power_kw) AS known_models,
  ROUND(COUNT(motor_power_kw)::numeric / NULLIF(COUNT(*), 0), 4) AS coverage
FROM public.vehicle_model_base
GROUP BY launch_year, energy_type
ORDER BY launch_year, energy_type;
```

#### 销售状态匹配

```sql
SELECT
  sale_status,
  sale_status_matched,
  COUNT(*) AS model_count
FROM public.vehicle_model_base
GROUP BY sale_status, sale_status_matched
ORDER BY sale_status_matched DESC, sale_status;
```

#### 维度边界

至少验证 `50`、`100`、`150`、`500` 等边界值是否进入正确的左闭右开区间。

#### 代码和资产

```bash
cd backend
PYTHONPATH=. ./.venv/bin/pytest -q \
  tests/test_table_router.py \
  tests/test_semantic_assets_registry.py

./.venv/bin/python -m py_compile \
  scripts/migrate_vanna_vehicle_model_base.py \
  analytics/nl2sql/table_router.py
```

还要使用 `AnalyticsModelRegistry.get_model_context('产品配置分析')` 验证新维度确实出现在 `semantic_assets` 中，而不是只存在于磁盘。

### 第十三步：Agent 验收

至少测试以下问题：

```text
按电动机总功率区间统计新能源款型占比。
统计2026年新能源车型各电机功率段的款型数和占比。
查看150kW和500kW边界分别属于哪个功率段。
```

检查生成 SQL：

- 是否优先读取 `vehicle_model_base.motor_power_kw`；
- 是否没有回到 `vehicle_params` 做不必要的 EAV 自关联；
- 是否按完整款型键统计；
- 是否排除皮卡，除非用户明确要求包含；
- 是否使用左闭右开区间；
- 是否披露有效样本覆盖率；
- 是否没有把前后电机功率相加。

## 5. 一次性表名迁移流程

只有仍处于旧表 `vehicle_params_wide` 的环境才执行 `migrate_vehicle_model_base.sql`。该脚本负责：

1. 把物理表改名为 `vehicle_model_base`；
2. 改主键约束名；
3. 改各索引名；
4. 补充新增基础字段和索引；
5. 写入表/字段注释；
6. 创建同名兼容视图 `vehicle_params_wide`；
7. 让旧查询在迁移期继续可用。

已经存在 `vehicle_model_base` 的环境不要重复运行一次性改名脚本，日常维护只运行完整刷新 SQL。

## 6. 本次已落地的两个示例

### 6.1 轴距

- 原始参数：`type_name = '轴距[mm]'`
- 物理字段：`vehicle_model_base.wheelbase_mm integer`
- 语义资产：`dimension:wheelbase`
- 分段：2600 以下、每 50mm 一档、3000 以上
- 关键规则：同一车系可能有多个轴距，必须保留款型颗粒度

### 6.2 电动机总功率

- 原始参数：`type_name = '电动机总功率[kW]'`
- 物理字段：`vehicle_model_base.motor_power_kw numeric(10,2)`
- 语义资产：`dimension:motor_power`
- 分段：0–50kW 起每 50kW 一档，500kW 以上封顶
- 关键规则：使用原始整车总功率，不进行前后电机相加，不用系统综合功率替代

## 7. 回滚与故障处理

### 7.1 刷新 SQL 执行失败

完整刷新 SQL 包含 `BEGIN/COMMIT`。中途失败时应执行：

```sql
ROLLBACK;
```

不要在失败后直接继续执行剩余片段；修正原因后重新运行完整脚本。

### 7.2 新字段数据异常

如果字段类型、覆盖率或极值明显异常：

1. 暂停 Vanna 更新；
2. 回查原始 `type_name/type_value`；
3. 检查同一款型是否多值；
4. 修正刷新 SQL 的取值或清洗规则；
5. 完整刷新并重新验证；
6. 最后再训练 Vanna。

### 7.3 Agent 仍使用 EAV 表

依次检查：

1. 数据库是否真实存在新列；
2. 分析模型是否注册新维度；
3. 维度 binding 是否指向正确字段；
4. 表路由器是否有关键词和列存在判断；
5. Vanna DDL/文档是否在数据库刷新后重新训练；
6. Agent 请求是否带有所选分析模型；
7. 最新 trace 中加载的语义资产是否包含新维度。

## 8. 每次维护的最终检查清单

- [ ] 已确认字段适合款型基础表，而不是可扩展配置明细。
- [ ] 已查询数据库真实 `type_name`，没有根据自然语言猜字段。
- [ ] 已检查值格式、覆盖率、异常值和一款多值。
- [ ] 已更新完整刷新 SQL 的建表、扩列、聚合、清洗、插入、索引和注释。
- [ ] 未破坏销售状态的年款转换、`btrim` 和在售优先规则。
- [ ] 已创建/更新语义维度，明确单位、颗粒度、区间、分母、覆盖率和禁止规则。
- [ ] 已把维度注册到产品配置分析模型并递增版本。
- [ ] 已同步报告生成 reference。
- [ ] 已更新表路由和路由测试。
- [ ] 已更新 Vanna 文档；数据库刷新后重新训练 DDL/文档。
- [ ] 已验证主键唯一、字段覆盖率、边界分段和销售状态匹配。
- [ ] 已通过相关单元测试和模型上下文加载检查。
- [ ] 已用 Agent 实际问题检查最终 SQL。

