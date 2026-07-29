# PuddingClaw AgentState Schema（Living Reference）

> 文档性质：当前 Agent 图状态的长期业务逻辑参考，不是一次性实施方案。  
> 最后复盘：2026-07-29。  
> 适用范围：`DeepAgentsAgentManager` 创建的主 Agent；子代理差异单独说明。  
> 权威顺序：运行时代码与编译后的图 schema > 本文 > 历史方案与聊天记录。

## 1. 为什么维护这份文档

AgentState 是模型循环、Middleware、业务工具和验收控制之间的共享工作状态。它决定：

- Agent 当前拥有哪些已验证上下文；
- 哪些信息可以渐进写入并被后续工具或子代理消费；
- 哪些字段禁止由 graph input 或 Agent 自行伪造；
- 哪些内容属于本 Run，哪些内容实际存放在 Session/Harness 权威账本；
- 新增 Middleware 时是否无意间扩大、覆盖或泄漏状态。

它不是单个 `TypedDict` 的字段列表。最终有效 schema 是基础 state 与所有已挂载 Middleware state schema 的联合：

```text
Effective Main AgentState
  = PuddingClawAgentState
  + DeepAgents base middleware state
  + PuddingClaw middleware state
  + conditionally mounted Rubric / ModelCallLimit state
```

当前主 Agent 的最大联合 schema 共 **39 个键**。

## 2. 当前完整键集

### 2.1 基础循环与上下文

| 键 | 类型/形态 | 写入方 | 说明 |
|---|---|---|---|
| `messages` | `list[Message]` | Agent/Tool/Middleware | 唯一必需字段；使用消息 reducer 增量合并。 |
| `jump_to` | `"tools" \| "model" \| "end" \| None` | Middleware | 临时图跳转控制；不进入 graph input/output schema。 |
| `structured_response` | `ResponseT` | Structured output | 配置结构化输出时存在。 |
| `files` | `dict[str, FileData]` | DeepAgents Filesystem | StateBackend 使用；当前真实文件 Backend 下通常为空。 |
| `todos` | `list[dict]` | `HarnessTodoMiddleware` | 本轮 Todo 白盒镜像；权威持久化仍由 Session ledger 管理。 |
| `memory_contents` | `dict[str, str]` | Memory Middleware | 已读取 Memory 内容，私有。 |

### 2.2 分析模型、语义资产与模板

| 键 | 类型/形态 | 写入方 | 说明 |
|---|---|---|---|
| `analytics_model_id` | `str \| None` | Run 初始化 | UI 当前选择的分析模型。 |
| `semantic_assets_model_id` | `str` | `SemanticAssetsMiddleware` | 已加载语义资产所属模型。 |
| `semantic_assets_metadata` | `list[dict]` | `SemanticAssetsMiddleware` | 当前模型允许使用的语义资产索引。 |
| `allowed_semantic_asset_ids` | `list[str]` | `SemanticAssetsMiddleware` | SQL 等工具的服务端可信 allowlist。 |
| `_active_analysis_template` | `dict \| None` | `AnalysisTemplateMiddleware` | Agent 成功读取注册模板 guide 后渐进写入的可信模板上下文。 |

`_active_analysis_template` 当前结构：

```python
{
    "model_id": str,
    "template_id": str,
    "template_version": str,
    "guide_content_sha256": str,
    "virtual_path": str,
    "guide_virtual_path": str,
    "asset_virtual_paths": list[str],
    "semantic_scope": dict,
    "source": "authoritative_guide_read",
    "source_tool_call_id": str,
}
```

模板渐进激活链路：

```text
model.md 暴露 path / guide / assets / use_when / do_not_use_when
  -> Agent 根据语义与对话上下文选择模板
  -> Agent read_file(注册的 TEMPLATE.md)
  -> AnalysisTemplateMiddleware 从完整文件重新解析 frontmatter
  -> Registry 校验 semantic_scope 的 member / classification
  -> 写入 _active_analysis_template
  -> SQL generator 只从该私有 state 获取模板授权
```

模板判断归 Agent；manifest 解析、枚举校验与 SQL 授权归服务端。不得恢复基于 `query_match` token 组的服务端关键词路由，也不得把 `semantic_scope` 降级成 prompt 软约束。

### 2.3 Skill、任务理解与能力

| 键 | 类型/形态 | 写入方 | 说明 |
|---|---|---|---|
| `task_profile` | `dict` | Task Router/Run 初始化 | 本轮任务理解、Skill 候选和验证包。 |
| `active_skill_ids` | `list[str]` | Toolset Middleware | 已激活 Skill ID。 |
| `skill_activations` | `list[dict]` | Toolset Middleware | 带内容 hash、Run、来源调用和解锁工具的激活记录。 |
| `capability_manifest` | `dict` | Toolset schema | Schema 已声明；当前主要版本持久化在 RunRecord 并按 ModelRequest 注入，不保证 state 中始终有值。 |
| `skills_metadata` | `list[SkillMetadata]` | DeepAgents Skills Middleware | 可渐进读取的 Skill 元数据，私有。 |
| `skills_load_errors` | `list[str]` | DeepAgents Skills Middleware | Skill 加载异常，私有。 |

### 2.4 验收与完成控制

| 键 | 类型/形态 | 使用方 | 说明 |
|---|---|---|---|
| `rubric` | `str` | Rubric Middleware | 本轮 Rubric 文本；只在 Rubric 验收下需要。 |
| `verification_contract` | `dict` | Harness | 当前 Run 的验收合同。 |
| `verification_activations` | `list[dict]` | Verification activation | 工具/产物触发的验收激活，私有。 |
| `_verification_attempts` | `int` | Completion gate | 验收尝试次数。 |
| `_completion_gate_iterations` | `int` | Completion gate | 确定性完成门循环次数。 |
| `_completion_gate_status` | `str \| None` | Completion gate | 当前完成门状态。 |
| `_completion_gate_failure_signature` | `str` | Completion gate | 当前失败集合指纹。 |
| `_completion_gate_stagnation_count` | `int` | Completion gate | 无进展修复次数。 |
| `_deterministic_evaluations` | `list[dict]` | Deterministic checks | 确定性检查结果。 |
| `_goal_verification_context` | `dict` | Goal verification | 当前 Goal revision 的验收上下文。 |
| `_goal_completion_reminder_count` | `int` | Goal completion | 标准 Goal 完成声明提醒次数。 |

Rubric Middleware 挂载时还会贡献：

| 键 | 说明 |
|---|---|
| `_rubric_status` | `satisfied/needs_revision/failed/...`。 |
| `_rubric_iterations` | Rubric grader 迭代次数。 |
| `_rubric_evaluations` | Rubric grader 原始结构化评估。 |
| `_current_grading_run_id` | 当前 grader 执行标识。 |
| `_active_rubric` | 当前实际采用的 Rubric。 |

标准验收不会依赖这些 Rubric 私有字段。

### 2.5 Run 边界、模型调用限制与工具上下文

| 键 | 类型/形态 | 写入方 | 说明 |
|---|---|---|---|
| `_run_query_id` | `str` | `RunScopeMiddleware` | 当前 Run 的可信 Query 边界。 |
| `_run_objective` | `str` | `RunScopeMiddleware` | 当前 Run 的可信原始目标；SQL 授权不得信任 delegated HumanMessage 替代它。 |
| `run_model_call_count` | `int` | ModelCallLimit | 本 Run 主 Agent 模型调用计数。 |
| `thread_model_call_count` | `int` | ModelCallLimit | 当前 checkpoint thread 调用计数。 |
| `_model_call_limit_exceeded` | `dict` | Observable ModelCallLimit | 达限原因和计数快照。 |
| `tool_context_enqueue` | `bool` | Tool context compaction | 是否需要后台处理大工具上下文。 |

## 3. 39 个键的机械清单

```text
messages
jump_to
structured_response
analytics_model_id
semantic_assets_model_id
semantic_assets_metadata
allowed_semantic_asset_ids
tool_context_enqueue
rubric
task_profile
verification_contract
verification_activations
_verification_attempts
_completion_gate_iterations
_completion_gate_status
_completion_gate_failure_signature
_completion_gate_stagnation_count
_deterministic_evaluations
_run_query_id
_run_objective
_active_analysis_template
_goal_verification_context
_goal_completion_reminder_count
run_model_call_count
thread_model_call_count
_model_call_limit_exceeded
todos
active_skill_ids
skill_activations
capability_manifest
skills_metadata
skills_load_errors
files
memory_contents
_rubric_status
_rubric_iterations
_rubric_evaluations
_current_grading_run_id
_active_rubric
```

## 4. State、Runtime Context、Session Ledger 和 Model Input 的边界

### AgentState

- 当前活动图的可变工作状态；
- 由 reducer 和 Middleware 渐进更新；
- terminal Run 后 checkpoint thread 会删除；
- `PrivateStateAttr` 字段不允许从 graph input/output 注入或导出。

### Runtime Context

以下是不可变 Run 调用上下文，不属于 AgentState：

```text
session_id
query_id
run_id
goal_id
goal_revision
user_id
project_id
workspace_path
permission_policy
run_objective
```

### Session/Harness 权威账本

以下内容不应塞入 AgentState 作为唯一事实来源：

- 完整 `RunRecord`、`GoalRecord` 和状态机；
- CompletionRequest、VerificationReport、Evidence ledger；
- PermissionGrant、PermissionManifest、CapabilityManifest 审计记录；
- Artifact registry、外部文件 lease、Todo ledger；
- Skill cache 和跨 Run 激活记录。

AgentState 可以持有这些事实的本轮镜像，但不得反向覆盖 Session 权威状态。

### Model Input

LLM 不会自动看到整个 AgentState。模型实际看到的是：

- `messages`；
- ModelRequest 的 system message；
- Middleware 显式追加的模板、Skill、Capability、Permission 或 Harness 上下文；
- 当前可见工具 schema。

“不是 `PrivateStateAttr`”不等于“模型自动可见”；“注入 system prompt”也不等于“进入 AgentState”。

## 5. 主 Agent 与子代理差异

声明式子代理继承 `PuddingClawAgentState`，因此可以继承 `_active_analysis_template`、Run 边界和语义资产等固定字段。子代理自己的 Middleware 还挂载了 `ToolCallLimitMiddleware`，可能额外出现：

```text
run_tool_call_count: dict[str, int]
thread_tool_call_count: dict[str, int]
```

主 Agent 当前不挂载该 Middleware，因此这两个键不计入主 Agent 的 39 键。

已编译子代理和远程子代理不自动继承主 Agent schema，必须在其自身图中显式声明兼容 state。

## 6. 条件字段与常见误判

- `_active_analysis_template`：只有成功读取当前分析模型注册的 guide 后存在；新 Run 需要重新读取。
- Rubric 私有字段：只在 Rubric Middleware 挂载并进入评估后有值。
- 模型调用计数字段：只有 ModelCallLimit 启用并配置有效阈值时有意义。
- `files`：使用真实 FilesystemBackend 时通常不是文件事实来源。
- `structured_response`：未配置 response format 时为空。
- `capability_manifest`：schema 中存在不代表每轮都写入 state；当前权威审计在 RunRecord。
- `permission_manifest`：不属于 AgentState；它在每次 ModelRequest 生成并持久化到 Run。
- `goal_id/run_id/session_id`：不属于 AgentState；它们位于 runtime context 和 Session ledger。

## 7. 定期复盘机制

至少在以下事件发生时复盘本文：

1. 新增、删除或调整 `PuddingClawAgentState` 字段；
2. 新增带 `state_schema` 的 Middleware；
3. DeepAgents/LangChain/LangGraph 升级；
4. 修改 Skill、Template、Semantic Asset 的渐进激活方式；
5. 修改 Rubric、标准验收、Goal/Run 边界或 checkpoint 生命周期；
6. 每个正式版本发布前；长期无版本发布时至少每季度一次。

复盘检查项：

- [ ] 重新机械生成有效键集，不凭记忆维护数量；
- [ ] 核对字段写入者、读取者和 reducer；
- [ ] 核对 `PrivateStateAttr` 是否正确；
- [ ] 核对 graph input/output 是否能伪造权限或授权字段；
- [ ] 核对主 Agent、声明式子代理、已编译子代理和远程子代理差异；
- [ ] 核对 state、runtime context、Session ledger、Model Input 边界；
- [ ] 为新增的渐进状态同时补“成功写入”和“未命中不写入”测试；
- [ ] 更新本页的数量、清单、最后复盘日期和变更记录。

用于机械核对主 Agent 最大联合键集的参考命令：

```bash
backend/.venv/bin/python - <<'PY'
from typing import get_type_hints

from deepagents.middleware.filesystem import FilesystemState
from deepagents.middleware.memory import MemoryState
from deepagents.middleware.rubric import RubricState
from deepagents.middleware.skills import SkillsState
from langchain.agents.middleware.model_call_limit import ModelCallLimitState

from graph.deepagents_manager import PuddingClawAgentState
from graph.middlewares.harness_todos import HarnessTodoState
from graph.middlewares.toolset import ToolsetState

schemas = [
    PuddingClawAgentState,
    HarnessTodoState,
    ToolsetState,
    SkillsState,
    FilesystemState,
    MemoryState,
    RubricState,
    ModelCallLimitState,
]
keys = []
for schema in schemas:
    for key in get_type_hints(schema, include_extras=True):
        if key not in keys:
            keys.append(key)
print(len(keys))
print("\n".join(keys))
PY
```

此命令是核对辅助，不替代对实际 Middleware 装配条件的检查。

## 8. 主要代码入口

| 领域 | 文件 |
|---|---|
| 主 AgentState 与 Middleware 装配 | `backend/graph/deepagents_manager.py` |
| 模板渐进激活 | `backend/graph/middlewares/analysis_templates.py` |
| 语义资产 state | `backend/graph/middlewares/semantic_assets.py` |
| Todo state | `backend/graph/middlewares/harness_todos.py` |
| Skill/Capability state | `backend/graph/middlewares/toolset.py` |
| Session/Run/Goal 权威持久化 | `backend/graph/session_manager.py`、`backend/harness/models.py` |
| SQL 模板授权消费 | `backend/tools/database/sql_generate_tool.py` |

## 9. 变更记录

| 日期 | 变化 | 复盘结论 |
|---|---|---|
| 2026-07-29 | 建立首版；记录 39 键；加入 `_active_analysis_template` 渐进激活 | 模板选择归 Agent，服务端读取并校验 manifest 后写入私有 state；SQL 强守卫保持不变。 |
