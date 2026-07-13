# Skill 与 Toolset 渐进加载方案

## 状态

第一版已实施（2026-07-13）：后端 Toolset 注册表、Skill frontmatter
声明、`SkillIntentRouterMiddleware` 与 `ToolsetMiddleware` 已接入
DeepAgents。后续仍可补充专用 Loader Tool 与 Trace 可视化。

## 问题

当前 `backend/tools/` 中的 Tool 会由 `get_all_tools()` 统一注册，
`DeepAgentsAgentManager._build_tools()` 再按 `AGENT_MODE_PUDDINGCLAW_TOOLS`
白名单全部传入 Agent。当前 `/skills/` 提供可发现的 Skill 元数据快照；Agent
随后可读取某个 `SKILL.md` 执行该 Skill 的流程。无论 Skill 是否被读取，当前
模型可见的 Tool 集合都不会改变。

随着语义维度、逻辑数据集、SQL 守卫、模型管理等工作流增加，所有专属
写入工具平铺给通用 Agent 会增加工具选择负担与误调用风险。

## 目标

在不预先调用路由 LLM、也不为加载 Skill 重建 Agent 的情况下，按本轮运行时
状态渐进暴露 Tool。

- Skill catalogue：可发现层，向模型提供全部可选 Skill 的名称、描述和路径。
- Active skill：本轮实际被选择/读取的业务流程，保留 `SKILL.md` /
  references / scripts 结构。
- Toolset：平台执行能力分类，决定当前模型可调用哪些 Tool。
- Tool：仍是后端受控入口，承担会话、权限、HITL、事务和 Trace。

Toolset 不是一个 Skill 一套私有工具。它是跨 Skill、跨 Agent 可复用的稳定分类；
Skill 仅在 frontmatter 中声明本流程需要哪些 Toolset。这个层次参考 Hermes 的
`terminal / file / web / skills / memory` 分类方式，而不是将每个业务 Skill 直接
等同为一个执行权限集合。

## Toolset 注册边界

Toolset 必须由后端统一注册，例如新增 `backend/tools/toolsets.py`：

```python
TOOLSETS = {
    "core": {...},
    "knowledge_access": {...},
    "database_analysis": {...},
    "analytics_assets": {...},
    "semantic_assets": {...},
    "artifact_output": {...},
}
```

Skill 的 frontmatter 只能引用已注册的分类：

```yaml
toolsets:
  - analytics_assets
  - knowledge_access
```

Loader 必须校验引用存在；未知 Toolset 视为 Skill 配置错误，不能静默降级或由
前端/Skill 自定义 Tool 名称。这样 Toolset 保持平台能力分类，Skill 保持可迁移的
业务流程说明，执行权限仍由后端掌握。

## 当前与目标

```text
旧行为：所有 PuddingClaw Tool -> 每次模型调用均可见

当前行为：DeepAgents 原生基础 Tool + 默认 PuddingClaw 基础 Tool
`terminal` / `read_resource` / `tavily_search` / `fetch_url` + 已读取 Skill
声明的业务 Toolset 对应 Tool。
```

DeepAgents 原生基础 Toolset 始终可见：`core_workspace`、
`workspace_write`、`local_execution`、`delegation`。Skill 专属业务工具
默认不可见。`terminal`、`read_resource`、`tavily_search` 与 `fetch_url` 是
PuddingClaw 补充的无条件基础能力。`web_research` 分组保留用于运行时清单和
Skill 文档，但声明它不会改变授权面。

示例：

```python
TOOLSETS = {
    "analytics_assets": {
        "ensure_attachment_table_asset",
        "list_logical_dataset_candidates",
        "request_logical_dataset_rule",
        "apply_logical_dataset_rule",
    },
    "semantic_assets": {
        "inspect_dimension_build_input",
        "request_dimension_build_rule",
        "enqueue_semantic_dimension_build",
        "publish_semantic_dimension_build",
    },
    "database_analysis": {
        "database_schema_inspect",
        "database_sql_generate",
        "database_sql_validate",
        "database_sql_execute",
    },
}
```

当前平台注册表位于 `backend/tools/toolsets.py`，采用单一归属避免工具面
悄然膨胀。无条件扩展 Toolset 为 `web_research`；Skill 门控业务 Toolset 为
`knowledge_analysis`、`database_analysis`、`semantic_lookup`、
`semantic_dimension_build`、`logical_dataset`。
Skill 可声明多个 Toolset；未知名称会在后端启动/装配时直接报错。

## 运行时机制

中间件使用同一份 Toolset 授权同时保护两个入口：`wrap_model_call` 在每次模型
调用前从已成功读取的 `SKILL.md` 推导可见工具；`wrap_tool_call` 在执行前再次
校验工具是否属于已激活 Toolset。模型即使输出未暴露的工具名，也只能收到拒绝
结果，不能进入真实工具实现。模型可见与实际可执行必须保持同一个不变量：

```python
visible_tools = [
    *CORE_TOOLS,
    *tools_for_toolsets(request.state.get("enabled_toolsets", [])),
]
return handler(request.override(tools=visible_tools))

# tool execution wrapper
if request.tool_call["name"] not in allowed_tool_names(request.state):
    return ToolMessage(status="error", content="Toolset 未激活")
return handler(request)
```

Agent state 只持久化真正已调用的 Skill：

```python
active_skill_ids: Annotated[list[str], skill_accumulator] = []
```

`active_skill_ids` 表示真正已读取且成功返回的 Skill。当前第一版通过消息中
成功的 `read_file(/skills/<id>/SKILL.md)` 调用推导该列表，并同步写入 state，
因此 HITL 恢复时可从 checkpoint 与消息重新推导。它不同于系统提示中的可选
Skill catalogue。

`enabled_toolsets` 是中间件的**派生结果**，而非需要重复保存的 state：

```python
enabled_toolsets = CORE_TOOLSETS | union(
    SKILL_TOOLSET_MAP[skill_id]
    for skill_id in request.state.get("active_skill_ids", [])
)
```

这样 Toolset 保持独立、可复用的分类，Skill 是启用它的业务入口；一个 Skill 可
声明多个 Toolset，多个 Skill 也可复用同一 Toolset。只有模型/系统明确需要额外
授权某一 Toolset 时，才另加一个受控的 `granted_toolsets` state。

不使用 `tools_loaded`：状态实际存的是 Toolset，而非逐个 Tool；核心 Tool
也无需“加载”。也不以 `skills_loaded` 同时表达两件事，避免混淆“已读 Skill”与
“已允许 Tool”。

`SkillIntentRouterMiddleware` 只用关键词推荐“先读哪个 Skill”，不再推荐具体
工具，更不裁剪工具。`ToolsetMiddleware` 才是唯一的硬边界。当前由 DeepAgents
原生 `read_file` 充当 Loader；后续若需要显式 `/skill` UI，可再封装通用 Loader Tool。

未来 Loader Tool 可通过：

```python
Command(update={
    "active_skill_ids": ["build-logical-dataset"],
})
```

更新 state。下一次模型调用时，中间件按 Skill -> Toolset 映射计算并暴露对应 Tool。
前端显式 `/skill` 可直接激活 Skill；模型选择器可向本轮初始 state 注入已验证的
Skill，无需额外 LLM 路由。

## 生命周期

- 普通新用户消息：当前架构本来就会新建 Agent，初始 Toolset 为空，只保留核心 Tool。
- 同一轮内：已启用 Toolset 累积，支持一个复杂任务组合多个 Skill。
- HITL 暂停与恢复：不会重建 Agent；`active_skill_ids` 由 checkpoint 保留，
  中间件会重新推导 Toolset，因此确认后对应 `apply_*` / `publish_*` Tool 仍然可见。
- 本期不实现 `after_model` 自动卸载或复杂终态状态机；一轮结束天然清理。

## scripts 边界

`skills/{skill_name}/scripts/` 适合放无状态、可测试的纯逻辑，例如字段兼容性
计算、规范键标准化、规则校验。不得把知识库导入、数据库写入、发布、HITL 等
副作用操作搬到脚本中；它们必须继续经由后端 Tool/API，确保权限、Trace、事务
与审计边界。

## 实施与验证

1. 建立全局 Toolset registry；为 Skill frontmatter 增加 `toolsets` 声明。
2. 已新增 `SkillIntentRouterMiddleware`、`ToolsetMiddleware` 与
   checkpoint-visible `active_skill_ids`。
3. 已迁移 `build-logical-dataset`、`build-semantic-dimension`，并补齐
   `database-analysis`、`table-analysis`、`knowledge-search` 系统 Skill。
4. 待补：Trace 记录每次模型调用的 `active_skill_ids`、推导出的
   `enabled_toolsets`、原始 Tool 数与过滤后 Tool 名称。
5. 已覆盖：Skill frontmatter 合法性、成功读取后激活 Toolset、意图路由只推荐
   Skill。待补 E2E：HITL 恢复保留 Toolset、下一条消息回到初始集合。
