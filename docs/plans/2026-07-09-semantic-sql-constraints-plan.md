# 语义资产 SQL 约束方案

创建时间：2026-07-09 00:25:17 CST

状态：待评审，暂不开发

## 背景

智能问数已经支持通过语义资产给 `database_knowledge_query` 注入业务口径，例如：

- 维度：上市时间、价格段、车型级别、能源类型
- 度量值：配置率、充电倍率
- 颗粒度：款型、车系
- 度量值 reference：空气悬架等特定配置口径

当前语义资产主要通过 Markdown 正文进入 LLM/Vanna 上下文。这个机制可以提升 SQL 生成质量，但仍然是 prompt 约束，不能保证模型一定遵守。

已经出现过的真实问题：

- 用户问“2026 年上市的新车”，语义资产要求使用 `type_name = '上市时间'`，但 Agent 失败后退化为 `car_name LIKE '26款%'`，导致分母错误。
- 用户问“空气悬架配置率”，语义资产 reference 要求使用 `type_name = '可调悬架种类'` 且 `type_value` 包含 `空气悬架`，但模型可能使用 `type_name LIKE '%空气悬架%'` 猜测字段。

短期已经用后端 hard guardrail 拦截高风险反模式。长期不应持续在 Python 里手写零散规则，而应把可执行约束沉淀到语义资产中。

## 目标

将语义资产从“给 LLM 看的说明文档”扩展为：

```text
Markdown 正文：给 LLM/Vanna 理解业务口径
YAML frontmatter：给后端机器校验 SQL
Constraint engine：统一执行语义约束
Trace：证明约束已加载、已校验、是否发生冲突
```

核心目标：

- 避免模型生成看似可执行但业务口径错误的 SQL。
- 让数据分析师通过维护语义资产定义业务分析契约，而不是依赖工程师不断补 Python 正则。
- 保持第一版实现简单可落地，优先覆盖最常见、最高风险的错误。

## 非目标

第一版不做：

- 完整 SQL AST 校验。
- 自动修复所有 SQL。
- 图形化约束表单。
- 指标系统或数据模型全量建设。
- 用 guardrail 替代语义资产正文和 Vanna prompt。

## 设计原则

1. 语义资产是规则来源。
2. 后端 guardrail 只执行资产声明的机器约束。
3. 第一版优先 fail-fast，不给错答案。
4. Trace 必须暴露规则加载和冲突情况。
5. 约束 schema 要足够小，先覆盖真实问题。

## 语义资产 YAML 扩展

### 维度约束示例：上市时间

```yaml
formatter: semantic-asset
name: 上市时间
type: dimension

sql_constraints:
  required_when_used:
    any_of:
      - contains:
          - "type_name = '上市时间'"
      - contains:
          - 'type_name = "上市时间"'

  forbidden_patterns:
    - name: forbid_model_year_from_car_name
      pattern: "\\bcar_name\\b\\s+(LIKE|ILIKE)\\s+['\"]\\d{2}款%"
      message: "上市时间必须使用 type_name='上市时间' 的 type_value，不得从 car_name 的 26款 推断。"
```

### 度量值 reference 约束示例：空气悬架

```yaml
formatter: semantic-asset
name: 空气悬架配置率口径
type: measure_reference

sql_constraints:
  required_when_used:
    contains:
      - "type_name = '可调悬架种类'"
      - "空气悬架"

  forbidden_patterns:
    - name: forbid_air_suspension_type_name_guess
      pattern: "\\btype_name\\b\\s+(LIKE|ILIKE)\\s+['\"]%空气悬架%"
      message: "空气悬架配置率必须使用可调悬架种类，不得模糊匹配 type_name。"
```

### 度量值约束示例：配置率

```yaml
formatter: semantic-asset
name: 配置率
type: measure

default_filters:
  exclude:
    - name: exclude_pickup
      unless_user_mentions:
        - 包含皮卡
        - 只看皮卡
        - 皮卡
      required_sql:
        contains:
          - "type_name = '级别'"
          - "type_value = '皮卡'"
          - "NOT EXISTS"
      message: "配置率默认排除车型级别为皮卡的统计对象。"

metric_constraints:
  distinct_subject_required: true
  numerator_denominator_same_scope: true
  default_subject: car_name
```

## 第一版约束能力

第一版只支持三个基础能力：

### contains

SQL 必须包含指定片段。

```yaml
contains:
  - "type_name = '上市时间'"
```

### forbidden_patterns

SQL 不能命中指定正则。

```yaml
forbidden_patterns:
  - name: forbid_model_year_from_car_name
    pattern: "\\bcar_name\\b\\s+(LIKE|ILIKE)\\s+['\"]\\d{2}款%"
    message: "不得从 car_name 推断上市年份。"
```

### required_when_used

当该语义资产被命中并注入 SQL 生成上下文时，SQL 必须满足要求。

```yaml
required_when_used:
  contains:
    - "type_name = '可调悬架种类'"
```

## 后端模块设计

新增模块：

```text
backend/analytics/semantic_assets/constraints.py
```

建议接口：

```python
@dataclass
class SemanticSqlConflict:
    asset_id: str
    asset_name: str
    rule_name: str
    severity: str
    message: str
    sql_fragment: str = ""


def validate_semantic_sql(
    sql: str,
    semantic_resolution: dict,
    question: str,
) -> list[SemanticSqlConflict]:
    ...
```

接入点：

```python
sql = extract_sql(raw_sql)
conflicts = validate_semantic_sql(sql, semantic_resolution, request.question)
if conflicts:
    raise DatabaseKnowledgeQueryError(...)
execution = await run_readonly_sql(...)
```

## Trace 设计

在 database trace 中新增：

```json
{
  "semantic_constraints": {
    "loaded": [
      {
        "asset_id": "dimension:launch_time",
        "rule": "forbid_model_year_from_car_name"
      }
    ],
    "conflicts": [],
    "retry_count": 0,
    "action": "passed"
  }
}
```

如果拦截：

```json
{
  "semantic_constraints": {
    "loaded": [
      {
        "asset_id": "dimension:launch_time",
        "rule": "forbid_model_year_from_car_name"
      }
    ],
    "conflicts": [
      {
        "asset_id": "dimension:launch_time",
        "rule": "forbid_model_year_from_car_name",
        "message": "上市时间必须使用 type_name='上市时间' 的 type_value，不得从 car_name 的 26款 推断。",
        "sql_fragment": "car_name LIKE '26款%'"
      }
    ],
    "retry_count": 0,
    "action": "blocked"
  }
}
```

## 冲突处理策略

第一版采用 fail-fast：

```text
生成 SQL 与语义资产约束冲突，已拦截执行：...
```

原因：

- 先保证不返回错误答案。
- 便于在 Trace 中观察问题。
- 避免自动重试引入更多不可控行为。

第二版可增加 auto-retry：

```text
SQL 冲突
→ 将冲突消息追加给 Vanna
→ 要求只重写 SQL
→ 最多 retry 1 次
→ 仍冲突则失败
```

重试提示示例：

```text
你刚才生成的 SQL 被语义约束拦截：
- 上市时间必须使用 type_name='上市时间'
- 不得使用 car_name LIKE '26款%'

请只重写 SQL。
```

## 前端影响

第一版不做复杂表单，只需要：

- 语义资产编辑器继续允许编辑 Markdown/YAML。
- 新建模板增加可选字段：

```yaml
sql_constraints:
  forbidden_patterns: []
  required_when_used: {}
default_filters: []
metric_constraints: {}
```

后续再考虑结构化 UI：

- 禁止规则列表
- 必需 SQL 片段
- 默认筛选条件
- 规则测试按钮

## 落地步骤

1. 更新文档，冻结第一版 schema。
2. 扩展 semantic asset registry，保留并返回 frontmatter 中的：
   - `sql_constraints`
   - `default_filters`
   - `metric_constraints`
3. 新增 `constraints.py`，实现：
   - contains
   - forbidden_patterns
   - required_when_used
4. 替换当前 Python 硬编码 `_detect_semantic_sql_conflicts`。
5. 将现有两条硬规则迁移到 YAML：
   - 上市时间禁止 `car_name LIKE '26款%'`
   - 空气悬架禁止 `type_name LIKE '%空气悬架%'`
6. 将配置率默认排除皮卡迁移到 `default_filters.exclude`。
7. Trace 增加 `semantic_constraints` 节点。
8. 补测试：
   - 约束加载
   - 禁止规则命中
   - required_when_used 缺失
   - 无冲突 SQL 放行
9. 根据实际效果决定是否开发 auto-retry。
10. 稳定后再评估 sqlglot AST 校验。

## 测试用例建议

### 上市时间

应拦截：

```sql
SELECT COUNT(DISTINCT car_name)
FROM vehicle_params
WHERE car_name LIKE '26款%'
```

应放行：

```sql
SELECT COUNT(DISTINCT car_name)
FROM vehicle_params
WHERE type_name = '上市时间'
  AND type_value LIKE '2026-%'
```

### 空气悬架

应拦截：

```sql
SELECT COUNT(DISTINCT car_name)
FROM vehicle_params
WHERE type_name LIKE '%空气悬架%'
```

应放行：

```sql
SELECT COUNT(DISTINCT car_name)
FROM vehicle_params
WHERE type_name = '可调悬架种类'
  AND type_value LIKE '%空气悬架%'
```

### 配置率默认排除皮卡

应提示或拦截：

```sql
SELECT COUNT(DISTINCT car_name)
FROM vehicle_params
WHERE type_name = '可调悬架种类'
  AND type_value LIKE '%空气悬架%'
```

如果该问题命中配置率且用户没有要求包含皮卡，后续应要求 SQL 带上：

```sql
NOT EXISTS (
  SELECT 1
  FROM vehicle_params level_filter
  WHERE level_filter.car_name = <统计对象>.car_name
    AND level_filter.type_name = '级别'
    AND level_filter.type_value = '皮卡'
)
```

## 风险与边界

- 正则/contains 不是完整 SQL 理解，可能误判复杂 SQL。
- 第一版不要过度抽象，优先覆盖已出现的高风险错误。
- `default_filters` 比 `forbidden_patterns` 更难校验，因为需要理解分子分母 scope。第一版可以先作为 prompt + trace 展示，第二版再用 AST 强校验。
- 如果过早引入 sqlglot，开发成本会明显增加，建议等真实规则稳定后再做。

## 当前结论

短期后端 hard guardrail 是止血手段。

长期应改造为：

```text
语义资产声明机器可执行约束
后端统一校验 SQL
Trace 暴露约束加载和冲突
必要时自动重试生成 SQL
```

这样语义资产不只是说明文档，而是智能问数 Agent 的可执行业务契约。
