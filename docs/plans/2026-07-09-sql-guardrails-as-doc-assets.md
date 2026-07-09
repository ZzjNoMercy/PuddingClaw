# SQL Guardrails As Document Assets

## 目标

把 SQL 守卫从单纯 JSON 配置升级为类似度量值、维度、颗粒度的文档资产：

- 人可以直接阅读和审核。
- LLM 可以理解约束意图和适用场景。
- 后端可以从 frontmatter 编译成可执行 guardrail。
- 目录可以迁移、打包、版本化，不绑定单个环境的数据库记录。

守卫不再只是“系统配置”，而是“可迁移的问数口径资产”。

## 目录结构

第一版单独放在后端目录，和 `semantic-assets` 同级：

```text
backend/sql-guardrails/
  rules/
    launch_time_no_car_name_year/
      guardrail.md
    config_rate_model_key_group/
      guardrail.md
  drafts/
    config_rate_exclude_pickup/
      guardrail.md
```

虚拟挂载：

```text
/sql-guardrails -> backend/sql-guardrails
```

后续如果希望和语义资产统一打包，也可以整体迁移成：

```text
backend/semantic-assets/
  guardrails/
    launch_time_no_car_name_year/
      guardrail.md
```

但第一版建议单独目录，避免和 `measure.md`、`dimension.md`、`grain.md` 的 registry 混在一起。

## 文档格式

每条守卫是一个 Markdown 文件。YAML frontmatter 是机器可执行部分，正文是人和 LLM 可读说明。

```markdown
---
formatter: sql-guardrail
id: launch_time_no_car_name_year
name: 上市时间不能从款型名推断
enabled: true
version: 0.1.0
type: forbid_sql_pattern
scope:
  table_scope:
    mode: any
    values: []
  semantic_assets:
    - dimension:launch_time
params:
  pattern: "\\bcar_name\\b\\s+(?:LIKE|ILIKE)\\s+['\\\"]\\d{2}款%"
  unless_contains: "type_name = '上市时间'"
action:
  type: rewrite
  message: 命中语义资产“上市时间”，必须改用 type_name = '上市时间' 的 type_value 过滤真实上市日期。
created: 2026-07-09 00:00:00
updated_at: 2026-07-09 00:00:00
---

## 业务约束

当用户问“某年上市”“2026 年上市”等问题时，上市时间必须来自真实上市时间字段。

## 禁止写法

不要从款型名推断年份，例如：

```sql
car_name LIKE '26款%'
```

## 推荐写法

使用上市时间语义资产定义的字段路径。

在 EAV 表中，使用：

```sql
type_name = '上市时间'
```

在宽表中，优先使用：

```sql
vehicle_params_wide.launch_year = 2026
```

## 适用场景

- 用户问题命中 `dimension:launch_time`
- SQL 生成阶段出现从 `car_name` 推断年份的倾向

## 风险说明

当前正则只覆盖 `car_name LIKE '26款%'` / `ILIKE` 形态。
如果后续发现 `substring(car_name, 1, 2)`、`regexp_replace(car_name, ...)` 等写法，需要新增独立守卫。
```

## 字段边界

frontmatter 字段用于后端执行：

- `formatter`: 固定为 `sql-guardrail`。
- `id`: 稳定 ID，建议与目录名一致。
- `name`: 前端展示名。
- `enabled`: 是否参与检测。
- `version`: 文档版本。
- `type`: 后端 detector 类型。
- `scope.table_scope`: 路由表命中条件。
- `scope.semantic_assets`: 语义资产命中条件。
- `params`: detector 参数。
- `action`: 命中后的处理方式。
- `created` / `updated_at`: 可读时间。

正文用于人和 LLM：

- 业务约束
- 禁止写法
- 推荐写法
- 适用场景
- 风险说明
- 关联语义资产建议

后端执行时只信任 frontmatter，不从正文解析执行参数。

## Skill 工作流

计划新增一个守卫生成 skill，例如：

```text
sql-guardrail-designer
```

职责：

1. 读取用户描述的约束。
2. 结合内置 guardrail 模板和现有语义资产，生成 `guardrail.md` 草案。
3. 先写入 `/sql-guardrails/drafts/{id}/guardrail.md` 或直接在对话中展示草案。
4. 等用户明确确认后，再写入 `/sql-guardrails/rules/{id}/guardrail.md`。
5. 提醒用户是否需要同步更新相关 measure/dimension/grain 文档。

硬规则：

- 用户第一次描述约束时，不直接创建正式规则。
- 必须先展示草案。
- 只有用户明确说“确认 / 创建 / 保存 / 没问题”后，才写入正式 `rules/`。
- 如果规则会 block 查询，必须在草案里明确写出风险。

## 后端加载方式

第一版实现：

```text
load_guardrail_rules()
  - 扫描 backend/sql-guardrails/rules/**/guardrail.md
  - 解析 YAML frontmatter
  - formatter 必须是 sql-guardrail
  - 用现有 GuardrailRule Pydantic schema 校验
  - 返回 GuardrailRuleSet
```

错误处理：

- 单个文件解析失败时，不拖垮整个问数服务。
- 失败文件进入 diagnostics / trace。
- 前端 SQL 守卫页面展示“无效守卫”状态，方便修复。

缓存策略：

- 参考语义资产 registry。
- 应用启动时加载一次。
- 前端“刷新 SQL 守卫”触发 refresh。
- 保存/导入守卫后触发 refresh。

## 前端交互

SQL 守卫页面展示的仍是规则列表，但底层来源变成文档：

- 卡片展示 `name/type/enabled/scope/action`。
- 点开卡片可以查看文件树。
- 支持编辑 `guardrail.md`。
- 表单编辑器可以继续存在，但保存时生成/更新 Markdown frontmatter。
- JSON 预览改成“编译后的 GuardrailRule 预览”。

导入能力：

- 支持导入单个 `guardrail.md`。
- 支持导入 zip / 文件夹。
- 目录结构和语义资产导入保持一致。

## 文件系统挂载

需要像 `/semantic-assets/` 一样支持：

- DeepAgents `FilesystemBackend` 挂载 `/sql-guardrails/`。
- terminal path aliases 支持 `/sql-guardrails -> backend/sql-guardrails`。
- `/api/files` 允许读写 `sql-guardrails/`。
- `write_file` 工具允许写 `sql-guardrails/`。
- Trace runtime inventory 显示 `/sql-guardrails/` 是否 mounted。

这样 skill 可以直接 `read_file` / `write_file` 管理守卫文档，不需要调用专门 API。

## 与语义资产的关系

守卫不是替代语义资产，而是对语义资产的硬约束补充：

```text
measure/dimension/grain 文档
  - 解释业务口径
  - 引导 SQL 生成

sql-guardrail 文档
  - 解释禁止/必须规则
  - 编译成后端 detector
  - 兜底阻断错误 SQL
```

同一条重要约束通常需要两边同步：

- 语义资产里写“应该怎么做”。
- 守卫里写“绝不能怎么做 / 必须包含什么”。

## 迁移计划

第一步：文档化现有默认规则。

把当前 5 条默认规则迁移为：

```text
backend/sql-guardrails/rules/
  launch_time_no_car_name_year/guardrail.md
  air_suspension_reference_type_value/guardrail.md
  config_rate_use_wide_denominator/guardrail.md
  config_rate_model_key_group/guardrail.md
  config_rate_no_exists_distinct/guardrail.md
```

第二步：后端 loader 支持 Markdown frontmatter。

第三步：前端保存从 JSON 文件改为写 `guardrail.md`。

第四步：增加 skill 草案生成和确认创建流程。

第五步：删除旧 `backend/data/sql-guardrails.json`，SQL 守卫只以 `guardrail.md` 文档资产为主数据源。

## 不做的事

第一版不做：

- 不让 LLM 从正文自由推断 detector 参数。
- 不支持一个 `guardrail.md` 里定义多条规则。
- 不直接把 drafts 自动启用。
- 不把 SQL 守卫混入 database query prompt 全量注入；只有命中 scope 的诊断信息进入 rewrite prompt。
