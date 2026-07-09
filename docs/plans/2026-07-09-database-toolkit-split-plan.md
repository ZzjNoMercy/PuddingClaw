# 通用数据库工具拆分方案

创建时间：2026-07-09

状态：已完成

## 背景

`database_knowledge_query` 当前封装了完整 NL2SQL 链路：

```text
表路由 -> 语义资产召回 -> Vanna 资料召回 -> SQL 生成 -> SQL 守卫 -> SQL 执行 -> Profile -> 持久化 -> Trace
```

这个工具适合普通自然语言问数，但不适合 Agent 排查问题。排查时 Agent 需要控制中间步骤，例如复用上一次 SQL、只验证 SQL、安全执行修正版 SQL、查 schema 和枚举值，而不是每次重新让 Vanna 生成一版 SQL。

最新激光雷达配置率不一致案例暴露的问题：

- Agent 没有直接审计第一次 SQL。
- 反复自然语言调用 `database_knowledge_query`，导致 SQL 每次变化。
- 元数据排查也会命中配置率语义资产和 SQL 守卫。
- 缺少“直接执行明确 SQL”的工具。

## 目标

- [x] 保留 `database_knowledge_query` 作为高级业务问数工具，但不再注册到 Agent allowlist。
- [x] 新增通用低层数据库工具，供 Agent 排查、对账、验证。
- [x] 新增 `database_sql_generate`，只做 SQL 生成，不执行。
- [x] 低层执行/校验/结构检查工具不走 Vanna SQL 生成。
- [x] 低层工具默认只允许 SELECT / WITH。
- [x] 低层工具沿用数据源 selected_tables 授权。
- [x] SQL 执行写入 Trace 自定义 span。

## 第一版工具

### database_sql_generate

只生成 SQL，不执行。

链路：

```text
表路由 -> 语义资产召回 -> Vanna 资料召回 -> SQL 生成 -> SQL 守卫 rewrite/block -> 返回 SQL
```

用途：

- 让 Agent 先看到 SQL。
- 让 Agent 审核 SQL 口径。
- 让 Agent 决定是否调用 `database_sql_validate` / `database_sql_execute`。
- 保留 Vanna、语义资产和 SQL guardrail 的价值，但切断“生成后立即执行”的黑盒路径。

### database_sql_execute

执行 Agent 已经明确给出的 SQL。

边界：

- 只允许 `SELECT` / `WITH`。
- 禁止多语句。
- 禁止 DDL/DML。
- 默认按当前数据源 selected_tables 校验授权表。
- 支持 `limit`、`timeout_ms`、`profile`。
- 不经过 Vanna。

用途：

- 对账。
- 验证 SQL 假设。
- 跑修正版 SQL。
- 验证 PostgreSQL 语义。

### database_sql_validate

只校验 SQL，不执行。

第一版校验：

- 只读校验。
- 授权表校验。
- 暂不做语义 SQL guardrail 校验；guardrail 仍在 `database_sql_generate` 阶段生效。

用途：

- Agent 生成 SQL 后先检查风险。
- 排查为什么 SQL 被拦截。

### database_schema_inspect

查数据源结构和枚举。

第一版能力：

- `tables`：列出已选表。
- `columns`：列出表字段。
- `type_names`：针对 EAV 表列出 `type_name` 枚举及计数。
- `sample`：查看表样例。

用途：

- 元数据排查。
- 查 EAV 字段。
- 不触发配置率语义资产。

### database_query_trace_inspect

从已有 session trace 中抽取数据库工具调用信息。

第一版能力：

- 按 `session_id` 读取 session 文件。
- 默认读最新 trace。
- 返回 database tool calls、SQL、路由表、语义资产、执行结果摘要。

用途：

- Agent 复盘上一轮 SQL。
- 避免靠上下文猜。

## 职责边界

```text
database_knowledge_query
  面向兼容旧路径，输入自然语言，输出业务答案和 SQL。
  当前保留代码和 API，但不注册到 Agent allowlist。

database_sql_generate
  面向 Agent 问数第一步，输入自然语言，输出 SQL 和生成上下文，不执行。

database_sql_execute / validate / schema_inspect / trace_inspect / result_page
  面向 Agent 调试、对账和执行，输入明确 SQL 或结构化查询参数。
```

## 实施记录

- [x] 新增工具类：`database_sql_generate`、`database_sql_validate`、`database_sql_execute`、`database_schema_inspect`、`database_query_trace_inspect`。
- [x] 加入 `create_database_knowledge_tool()` 返回列表。
- [x] 加入 Agent allowlist。
- [x] 从 Agent allowlist 移除 `database_knowledge_query`，保留旧工具代码和非 Agent 路径兼容。
- [x] 更新 ToolIntentRouter：数据库业务问数引导为 `database_sql_generate -> database_sql_validate/database_sql_execute`。
- [x] 补单元测试。
- [x] 验证工具工厂可加载新工具，Agent allowlist 不再暴露 `database_knowledge_query`。
- [x] 验证 `database_sql_validate` / `database_sql_execute` 可对 `insight_data` 执行简单只读 SQL。
- [ ] 用激光雷达不一致案例做完整 Agent e2e 验证。

## 代码拆分计划

状态：实施中

`backend/tools/database_knowledge_tool.py` 已经超过 1000 行，包含 legacy 问数工具、SQL 生成、SQL 校验、SQL 执行、结构检查、Trace 检查和分页读取。下一步按工具职责拆到 `backend/tools/database/` 包中，保留原文件作为兼容入口。

目标结构：

```text
backend/tools/database/
  __init__.py
  models.py              # Tool input schema
  formatting.py          # Markdown 输出、错误输出
  scope.py               # 数据源、授权表、表名处理
  spans.py               # database trace span
  result_store.py        # 工具层分页/持久化结果读取封装
  legacy_query_tool.py   # database_knowledge_query
  sql_generate_tool.py   # database_sql_generate
  sql_validate_tool.py   # database_sql_validate
  sql_execute_tool.py    # database_sql_execute
  schema_inspect_tool.py # database_schema_inspect
  trace_inspect_tool.py  # database_query_trace_inspect
  result_page_tool.py    # database_query_result_page
```

拆分原则：

- [x] 第一轮只移动代码，不改工具名、输入参数、输出文案和 Trace 字段。
- [x] `database_knowledge_tool.py` 只保留 re-export / factory，降低外部 import 风险。
- [x] 分页读取不放进 `sql_execute_tool.py`；由 `result_page_tool.py` 调 `result_store.py`。
- [x] `database_sql_execute` 当前只负责执行明确 SQL 和返回结果；未来如果要让它生成 `result_id`，只新增调用 `result_store.py`，不把分页读取逻辑塞回执行工具。
- [x] 拆完运行现有单测和基础导入校验。

验证记录：

- `cd backend && .venv/bin/python -m py_compile tools/database_knowledge_tool.py tools/database/*.py`
- `cd backend && .venv/bin/pytest tests/test_tool_intent_router.py tests/test_database_query_result_contract.py tests/test_deepagents_manager.py -q`：35 passed。
- `cd backend && .venv/bin/pytest tests/test_sql_guardrails.py tests/test_database_semantic_guardrails.py tests/test_database_query_result_contract.py -q`：21 passed。
