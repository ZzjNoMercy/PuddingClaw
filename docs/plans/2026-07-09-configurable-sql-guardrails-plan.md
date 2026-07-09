# Configurable SQL Guardrails

## 目标

- [x] 将当前写在 `backend/analytics/nl2sql/service.py` 的业务 SQL guardrail 抽象成可配置规则。
- [x] 规则在后端按结构化 schema 解析，不作为 prompt 文本处理。
- [x] 前端智能问数页面渲染规则列表和编辑表单，字段由 rule type schema 驱动。
- [x] 第一版保留当前已验证规则能力，并支持后续扩展。

> 当前实现已升级为 Markdown 文档资产，详见
> `docs/plans/2026-07-09-sql-guardrails-as-doc-assets.md`。本文件保留第一版可配置 SQL 守卫的规则类型和执行链路背景。

## 第一版范围

### 存储

- 使用 Markdown 文档资产：`backend/sql-guardrails/rules/**/guardrail.md`。
- YAML frontmatter 编译成后端 `GuardrailRule`。
- 不再保留 `backend/data/sql-guardrails.json`。

### 规则结构

```yaml
guardrails:
  - id: config_rate_model_key_group
    name: 配置率款型颗粒度分组
    enabled: true
    type: require_group_by
    scope:
      table_scope:
        mode: any
        values: [vehicle_params]
      semantic_assets: [measure:config_rate]
    params:
      forbidden_columns_only: [car_name]
    action:
      type: rewrite
      message: 默认款型颗粒度必须按 brand + serial_name + car_name 分组。
```

### 后端规则类型

- `forbid_sql_pattern`
  - 使用正则匹配 SQL。
  - 支持 `unless_contains` 例外。
- `require_sql_contains`
  - 命中 scope 后，要求 SQL 包含指定片段。
  - 支持 `when_contains_any`，用于只在 SQL/问题相关词出现时触发。
- `require_table_when_available`
  - 路由包含某表时，SQL 必须使用该表。
- `require_group_by`
  - 可要求 SQL 的 `GROUP BY` 包含指定分组字段。
  - 可禁止仅按某些字段分组。
  - 当前 `config_rate_model_key_group` 只禁止 `GROUP BY car_name`，不强制所有聚合都包含 `brand + serial_name + car_name`，避免误拦截“先确定款型粒度，再按年份/品牌二次汇总”的趋势分析。
- `forbid_exists_distinct_pattern`
  - 用于拦截 EAV 表上的多层 `EXISTS` + `COUNT(DISTINCT ...)` 慢查询模式。

### 执行链路

1. Vanna 生成 SQL。
2. 后端加载 enabled guardrail rules。
3. 判断 scope 是否命中：
   - table scope：统一表示路由表命中条件，`mode=any` 表示任意表命中即触发，`mode=all` 表示必须同时包含全部表。
   - semantic assets
4. 按 `type` 调 detector。
5. 命中后按 action 处理：
   - `rewrite`：拼入冲突原因并让 Vanna 重写一次。
   - `block`：直接报错。
   - `warn`：写入 trace，不阻断执行。

## 当前迁移规则

- [x] 上市时间不能从 `car_name LIKE '26款%'` 推断。
- [x] 空气悬架必须使用 `type_name = '可调悬架种类'`。
- [x] 配置率命中宽表路由时，分母必须使用 `vehicle_params_wide`。
- [x] EAV flags fallback 必须按 `brand + serial_name + car_name` 分组。
- [x] 配置率禁止多层 `EXISTS` + `COUNT(DISTINCT car_name)` 慢查询写法。

## 前端

- [x] 智能问数页面增加 Guardrail 区块。
- [x] 显示规则列表：启用状态、名称、类型、scope、action。
- [x] 支持新增、编辑、删除、启用/禁用。
- [x] 编辑表单按 rule type schema 动态渲染 `params` 字段。
- [x] Scope 中的表范围和语义资产使用多选项渲染；数据源只作为前端表选项筛选器，不写入规则、不参与后端命中。
- [x] 支持 JSON 预览。

## 验证

- [x] 单元测试覆盖 rule loading / scope matching / detector。
- [x] 现有 semantic guardrail tests 通过。
- [x] 真实问数仍能把配置率问题重写到 `vehicle_params_wide + vehicle_params` 路径。

## 实施结果

- 后端模块：`backend/analytics/nl2sql/guardrails.py`
- 默认规则目录：`backend/sql-guardrails/rules/**/guardrail.md`
- API：
  - `GET /api/analytics/sql-guardrail-types`
  - `GET /api/analytics/sql-guardrails`
  - `POST /api/analytics/sql-guardrails`
  - `PUT /api/analytics/sql-guardrails`
  - `DELETE /api/analytics/sql-guardrails/{rule_id}`
  - `POST /api/analytics/sql-guardrails/reset`
- 前端入口：智能问数 -> SQL 守卫
- Scope 选择器：
  - 数据源筛选：`KnowledgeDatabaseSource.name`，只过滤可选表，不进入保存后的 guardrail JSON。
  - 表范围：`scope.table_scope.mode + scope.table_scope.values`；`any` 表示路由包含任一表即命中，`all` 表示路由必须同时包含全部表。
  - 语义资产：semantic asset registry 的 `asset.id`
- 验证：
  - `backend/.venv/bin/pytest tests/test_database_semantic_guardrails.py tests/test_sql_guardrails.py -q`
  - `backend/.venv/bin/python -m py_compile analytics/nl2sql/service.py analytics/nl2sql/guardrails.py api/analytics.py`
  - `frontend/npx tsc --noEmit`
