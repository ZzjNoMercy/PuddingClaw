# 分析模型联合分析 Session 复盘与优化方案

> 复盘对象：`backend/sessions/session-8429f7eee5c5.json`  
> 分析模型：`汽车行业综合分析`  
> 任务：基于 2023 年 1-5 月上险量 Top 20 车系，关联产品配置库判断空气悬架配置，并输出品牌、车系、上险量、是否搭载空气悬架、能源类型、价格段。

## 1. 结论摘要

这次任务最终跑出了结果，但执行路径成本偏高。核心问题不是“Agent 不会做联合分析”，而是模型上下文、表格资产解析和工具职责边界还不够清晰，导致 Agent 需要自己探索数据在哪里、字段怎么对齐、车系名称怎么映射。

本次总耗时约 368 秒，主要耗时集中在：

| 环节 | 次数 | 累计耗时 |
| --- | ---: | ---: |
| LLM model call | 44 | 250.82s |
| `database_sql_generate` | 2 | 116.17s |
| `pandas_knowledge_query` | 1 | 68.29s |
| `database_sql_execute` | 6 | 20.62s |
| `database_schema_inspect` | 4 | 9.20s |
| `ls` | 6 | 6.75s |

最值得优化的不是 SQL 执行，而是：

1. 分析模型里的 `table_asset:*` 没有被解析成文件名、路径、sheet、列、profile。
2. Agent 先尝试用数据库 SQL 生成找“上险量”，但销量事实在 Excel table asset，不在 PostgreSQL。
3. `pandas_knowledge_query` 能分析表格，但没有明确支持 `asset_id`，只能靠 `file_hint` 或自然语言匹配。
4. 数据资产之间缺少跨源 join key 说明，导致车系名称需要 Agent 临时肉眼修正。
5. 查询结果需要跨 Pandas 和 PostgreSQL 合并，但目前没有“跨资产联合分析”的标准工作流。

## 2. 本次执行路径复盘

实际工具调用大致路径：

1. 读取模型元数据，看到数据资产：
   - `table_asset:tbl_...`
   - `dbs_77982e981bac4a6fa8.vehicle_params`
   - `dbs_77982e981bac4a6fa8.vehicle_params_wide`

2. 先调用 `database_sql_generate` 生成“上险量 Top 20” SQL。
   - 返回：无法生成，因为当前数据库只提供产品配置表，缺少上险量事实表。
   - 这是合理失败，但本不应发生。模型已绑定 table asset，系统应告诉 Agent 销量数据在 table asset 里。

3. 调用 `database_schema_inspect` 查看数据库表。
   - 只看到 `vehicle_params` 和 `vehicle_params_wide`。
   - 这说明数据库工具是正常的，但它天然看不到知识库表格资产。

4. 调用多次 `ls /knowledge` 找 Excel 文件。
   - 最终找到 `/knowledge/imported/20260709/2023年1-5月乘用车市场上险量.xlsx`。
   - 这个路径应该由模型上下文直接提供，而不是让 Agent 从文件系统猜。

5. 调用 `pandas_knowledge_query` 计算 Top 20 车系。
   - 成功匹配 2023 年 1-5 月上险量 Excel。
   - 输出 Top 20 车系、品牌、上险量、能源类型、价格段。

6. 调用数据库工具查询产品配置表结构和空气悬架字段。
   - 发现宽表没有空气悬架字段。
   - EAV 表里存在相关 `type_name`：
     - `空气悬架类型`
     - `IAS智能空气悬架包`
     - `高性能空气悬架套装`
     - `可调悬架种类` 等。

7. 生成并执行空气悬架 SQL。
   - 第一版用 Excel 里的车系名直接 join `vehicle_params_wide.serial_name`，出现大量 `未知`。
   - 原因：Excel 车系名带品牌前缀，例如 `特斯拉Model Y`，而配置库中是 `brand=特斯拉, serial_name=Model Y`。

8. Agent 查询配置库里的实际命名并手动修正映射。
   - 如：
     - `特斯拉Model Y` -> `特斯拉` + `Model Y`
     - `传祺AION.S` -> `埃安` + `AION S`
     - `宏光mini` -> `五菱汽车` + `宏光MINIEV`

9. 再次执行空气悬架匹配。
   - Top 20 车系均无空气悬架记录。

10. 最后执行全面验证。
    - 对 Top 20 范围内查询 `type_name/type_value` 包含悬架/空气的记录，返回 0 行。
    - 结论可信度提高。

## 3. 第一性原理分析

### 3.1 分析模型的本质

分析模型不是“提示词片段”，而是一个可执行分析上下文包。它至少要回答四个问题：

1. **有什么数据可以用**：数据资产在哪里，类型是什么，怎么访问。
2. **这些数据表达什么业务含义**：度量值、维度、颗粒度、口径。
3. **不同数据之间怎么关联**：join key、映射规则、粒度转换。
4. **怎样产出结果**：推荐工具链、模板、验证规则、输出格式。

当前模型只部分满足第 2 点，且第 1 点只给了 opaque ID，没有给可执行信息。

### 3.2 数据资产不是一个字符串 ID

`table_asset:tbl_xxx` 对人和 Agent 都不可用。它必须被解析成：

```json
{
  "asset_id": "tbl_xxx",
  "asset_type": "table_asset",
  "file_name": "2023年1-5月乘用车市场上险量.xlsx",
  "virtual_path": "/knowledge/imported/20260709/2023年1-5月乘用车市场上险量.xlsx",
  "sheet_name": "工作表1",
  "rows": 839093,
  "columns": ["年份", "月份", "品牌", "1-子车型", "销量", "燃料种类_细分", "价格段"],
  "profile_available": true
}
```

PostgreSQL 里的 `KnowledgeTableAsset` 负责资产索引，profile 文件负责字段画像。模型上下文应组合两者。

### 3.3 工具选择应该由资产类型驱动

当前 Agent 先尝试数据库 SQL 生成，是因为模型没有告诉它：

- `dbs_...` 表走数据库工具。
- `table_asset:...` 表走 Pandas 表格工具。
- 混合分析先用 Pandas 取小结果集，再用数据库查配置。

正确工具选择不应该靠 Agent 猜，而应该由模型上下文明确声明。

### 3.4 跨源分析必须有 join contract

这次最大的数据一致性问题来自车系命名：

- 销量表：`1-子车型 = 特斯拉Model Y`
- 配置库：`brand = 特斯拉`, `serial_name = Model Y`

这类映射不能每次让 Agent 临时推理。需要在模型或语义资产里定义 join contract：

```yaml
join_contracts:
  - id: sales_series_to_vehicle_config_series
    left:
      asset: table_asset:sales
      fields: [品牌, 1-子车型]
    right:
      asset: dbs_77982e981bac4a6fa8.vehicle_params_wide
      fields: [brand, serial_name]
    strategy:
      - exact: 品牌 -> brand
      - normalize_series_name:
          left: 1-子车型
          right: serial_name
          rules:
            - remove_brand_prefix
            - normalize_dot_space
            - alias_dictionary
```

## 4. 可优化点

### P0：解析模型绑定的数据资产

目标：Agent 一开始就知道每个资产是什么。

建议在 `_analytics_model_context` 里展开：

- `table_asset:*`
  - 查询 `KnowledgeTableAsset`
  - 读取 profile 摘要
  - 注入文件名、路径、sheet、shape、列名、关键字段样例
- `dbs_xxx.table`
  - 注入数据库源、表名、字段列表、表说明

上下文示例：

```text
模型数据资产：
1. 上险量表
   - ref: table_asset:tbl_xxx
   - 工具: pandas_knowledge_query
   - 文件: 2023年1-5月乘用车市场上险量.xlsx
   - sheet: 工作表1
   - 行列: 839093 x 30
   - 关键列: 年份, 月份, 品牌, 1-子车型, 销量, 燃料种类_细分, 价格段

2. 产品配置宽表
   - ref: dbs_77982e981bac4a6fa8.vehicle_params_wide
   - 工具: database_schema_inspect / database_sql_execute
   - join key: brand + serial_name + car_name
```

### P0：让 `pandas_knowledge_query` 支持 `asset_id`

当前只能通过 `file_hint` 命中文件。应增加：

```json
{
  "query": "...",
  "asset_id": "tbl_73d53d94a3a29ff425235dfa",
  "sheet_name": "工作表1"
}
```

这样模型上下文可以直接指挥 Agent 使用指定资产，避免 fuzzy match 和 `ls /knowledge`。

### P0：增加模型资产使用规则

在分析模型上下文中明确写入：

- 不要用 `database_schema_inspect` 查 `table_asset`。
- 不要用 `ls /knowledge` 定位已绑定资产。
- 如果任务涉及销量、上险量、Excel 表格，优先使用绑定的 `table_asset`。
- 如果任务涉及配置、空气悬架、能源类型、价格段，使用配置库 DB 表。
- 跨源分析先把一侧聚合成小表，再到另一侧查补充字段。

### P1：增加 table asset inspect/list 工具或复用现有 API

如果不希望把完整 profile 注入 prompt，可以给 Agent 一个轻量工具：

```text
analytics_model_asset_inspect(model_id)
table_asset_inspect(asset_id)
```

这样 Agent 可以先拿资产列表和字段摘要，再决定执行哪个查询。

但对于已选择模型的场景，建议仍然在上下文中注入摘要，减少一次工具调用。

### P1：定义跨源 join contract

当前跨源 join 依赖 Agent 临时修正命名，风险较高。

建议在模型文件中增加：

```yaml
join_contracts:
  - id: sales_series_to_config_series
    left_asset: table_asset:tbl_...
    right_asset: dbs_77982e981bac4a6fa8.vehicle_params_wide
    grain: series
    left_fields: [品牌, 1-子车型]
    right_fields: [brand, serial_name]
    normalization:
      brand_aliases:
        传祺: 埃安
        五菱: 五菱汽车
      series_rules:
        - remove_brand_prefix
        - normalize_case
        - normalize_dot_space
        - alias_map
```

如果未来要稳定做销量与配置联动分析，这个是必要能力。

### P1：对 Pandas 大表查询做结构化输出和落盘

本次 `pandas_knowledge_query` 读取 839093 行，用时 68 秒。后续优化方向：

- 对大 Excel 预转 Parquet。
- 生成 table profile 时顺便生成列类型和常用索引。
- Pandas 查询返回结构化结果表，而不只是文本和 `raw_result`。
- TopN 结果落盘为 `result_id`，供后续 DB 查询或导出使用。

### P1：将“销量”语义资产绑定到具体表格列

当前模型里有 `measure:销量`，但上下文没有说明：

- 销量来自哪个 table asset。
- 字段名是 `销量`。
- 上险量与销量字段的关系是什么。
- 常用时间字段是 `年份`、`月份`。
- 车系字段是 `1-子车型`。

建议 `measure:销量` 或模型中增加：

```yaml
measure_bindings:
  销量:
    preferred_assets:
      - table_asset:tbl_...
    expression: sum(销量)
    time_fields: [年份, 月份]
    grain_fields:
      series: [品牌, 1-子车型]
```

### P2：减少不必要的 SQL 生成

这次 `database_sql_generate` 第二次耗时 96 秒，最后生成的 SQL 仍需要人工修正映射。

对小范围验证类查询，可以让 Agent 直接写 SQL 并用 `database_sql_execute` 执行，而不是再走 SQL 生成器。

建议规则：

- 如果 Agent 已经掌握表结构和字段，且查询范围是明确 VALUES 列表，允许直接执行 SQL。
- `database_sql_generate` 更适合开放式 NL2SQL，不适合已经结构化后的二阶段补数。

### P2：模型卡片和 Trace 显示“资产解析状态”

为了调试，需要在 Trace 中显示：

- 选择了哪个模型。
- 模型绑定了几个数据资产。
- 每个 table asset 是否成功解析。
- 解析后的文件名、sheet、列数。
- 哪些资产被实际使用。

否则只能从 session JSON 反推。

## 5. 建议落地顺序

### 第一阶段：让模型上下文可执行

1. `_analytics_model_context` 解析 `table_asset:*`。
2. 注入资产摘要和工具使用建议。
3. `pandas_knowledge_query` 支持 `asset_id`。
4. Trace 记录模型资产解析结果。

验收标准：

- 用户选择“汽车行业综合分析”后问“列出模型绑定的数据资产”，Agent 能直接输出文件名、sheet、行列和关键列。
- 对上险量问题，Agent 不再先调用 `database_sql_generate` 查 DB。
- 不再调用 `ls /knowledge` 定位已绑定 table asset。

### 第二阶段：跨源 join contract

1. 模型支持 `join_contracts`。
2. 前端支持选择左/右资产和字段。
3. 支持品牌/车系 alias 字典。
4. Agent 上下文注入 join contract。

验收标准：

- `特斯拉Model Y` 能自动映射到 `特斯拉 + Model Y`。
- `传祺AION.S` 能自动映射到 `埃安 + AION S`。
- `宏光mini` 能自动映射到 `五菱汽车 + 宏光MINIEV`。

### 第三阶段：表格计算性能优化

1. Excel 导入后生成 Parquet 缓存。
2. `pandas_knowledge_query` 优先读 Parquet。
3. TopN/聚合结果支持落盘和分页。
4. 支持把 Pandas 结果作为临时表或 result set 交给 DB 二阶段查询。

验收标准：

- 80 万行 Excel TopN 聚合从 68 秒下降到 10 秒以内。
- 后续关联配置库不需要手动复制 Top20 列表。

## 6. 当前结果可信度评估

本次最终结果可信度中等偏高：

- Top20 上险量来自明确 Excel 表，Pandas 聚合逻辑清楚。
- 产品配置侧做了字段探测和最终验证。
- Top20 车系中空气悬架结果为全无，且最终验证 SQL 在 Top20 范围内没有找到悬架/空气匹配记录。

但仍有两个残余风险：

1. 车系映射是 Agent 手工修正，不是系统性 join contract。
2. 空气悬架口径主要用了 `可调悬架种类` 和悬架/空气关键字，若业务口径未来变化，需要由语义资产或 guardrail 固化。

## 7. 推荐最终方向

这个场景不应该走“万能 Agent 自己找文件”的路线，而应该走“模型选择后，系统给 Agent 一个可执行的数据地图”。

最小闭环是：

```text
分析模型
  -> 解析数据资产
  -> 注入表格/数据库访问方式
  -> 注入语义资产口径
  -> 注入跨源 join contract
  -> Agent 只负责规划、执行、验证和表达
```

这样既保留 AI-native 的灵活性，又避免每次分析都从“数据在哪里”开始重新探索。
