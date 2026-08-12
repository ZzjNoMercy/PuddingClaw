# PuddingClaw Harness Prompt 标准规范

> 状态：Agent / DeepAgents 模式的权威 Prompt 规范
> 版本：1.0
> 更新日期：2026-08-11
> 适用对象：PuddingClaw 使用者、项目维护者、Skill/Tool 开发者与 Harness 维护者

## 1. 适用范围

本文规定 PuddingClaw Harness Prompt 的来源、唯一装配顺序、文件所有权、扩展入口、Memory 边界和验收要求。

本文只描述当前 Agent / DeepAgents 模式，不包含已废弃的 Chat 模式。以下历史机制不属于本规范：

- `backend/prompts/deepagents/`；
- bundled 或用户层 `USER.md`；
- 用户层 `SOUL.md`、`IDENTITY.md`；
- `mem0`、`memory_backend`、Smart Extractor 等旧记忆开关；
- backend 下的用户 Memory、Skill、语义资产或 Session 写入兜底。

文中的“必须”“不得”表示强制要求；“应”表示默认要求，只有明确理由和验证证据时才可偏离；“可以”表示可选行为。

## 2. 使用者快速入口

PuddingClaw 只提供三种常用 Prompt/Memory 扩展入口：

| 需求 | 写入位置 | 典型内容 |
|---|---|---|
| 所有项目都适用的个人工作规则 | `$PUDDINGCLAW_HOME/profile/AGENTS.md` | 回复语言、代码验证习惯、报告风格 |
| 单个项目的稳定约定 | `<project>/AGENTS.md` | 架构、目录、构建命令、项目术语 |
| 跨 Run 保留的用户事实或偏好 | 当前作用域的 `MEMORY.md` | 称呼、稳定偏好、长期业务口径 |

`PUDDINGCLAW_HOME` 默认是 `~/.puddingclaw`。用户文件不得写入 `backend/`。

### 2.1 用户 Home AGENTS

文件：

```text
$PUDDINGCLAW_HOME/profile/AGENTS.md
```

文件可不存在；首次在设置页保存时创建。它适合低频变化、跨项目复用的行为规则：

```markdown
# My PuddingClaw Instructions

- 默认使用中文回答。
- 修改代码后运行与风险相称的测试。
- 报告结论时区分事实、推断和建议。
```

不得在这里保存 Secret、临时任务状态、一次性日期或大段业务数据。Home AGENTS 不能关闭权限、审批、Tool Gate、安全规则或验收规则。

### 2.2 项目 AGENTS

文件：

```text
<project>/AGENTS.md
```

它只对当前受信项目生效。PuddingClaw 不自动创建该文件，也不从 backend 模板兜底。推荐记录：

- 项目架构和目录职责；
- 构建、测试与格式化命令；
- 命名、代码风格和交付约定；
- 项目特有术语及稳定背景。

PuddingClaw 前端不提供项目 AGENTS 编辑器。用户应使用本地编辑器在项目根目录创建或修改 `AGENTS.md`；文件不存在时等同于空层，运行时跳过注入，不报错也不创建文件。

项目未受信时，其 `AGENTS.md` 不得进入模型输入。项目 AGENTS 不授予文件、网络、Secret 或 Tool 权限。

### 2.3 Memory

当前支持两种作用域：

```text
$PUDDINGCLAW_HOME/memory/global/MEMORY.md
$PUDDINGCLAW_HOME/memory/projects/<project_id>/MEMORY.md
```

- 无项目 Run 使用全局 Memory；
- 项目 Run 使用对应项目 Memory；
- 主 Agent 通过 `MemoryMiddleware` 读取，通过 `update_memory` 写入；
- `update_memory` 的物理路径由 Backend 绑定，模型不能传入或猜测 Home 路径；
- Subagent 不可直接调用 `update_memory`。

只应保存明确要求记住或忘记的内容、稳定偏好、长期有效的纠正，以及未来 Run 仍有价值的决策。不得保存临时任务状态、聊天摘要、工具输出、Secret、未经验证的推断或已有权威文件维护的事实。

## 3. 唯一 Prompt 顺序

主 Agent 最终模型输入必须保持以下语义顺序：

```text
SOUL
→ IDENTITY
→ bundled AGENTS
→ bundled tool_guides/core
→ DeepAgents Agent Core
→ Home AGENTS（存在时）
→ 可信项目 AGENTS（存在时）
→ Versioned Analytics / Semantics
→ Memory
→ Active Skill Instructions / Activated Tool Guides
→ Capability / Permission / Todo / Current Run Delta
```

对应的标准分层为：

1. `Stable Core`：系统人格、身份、通用行为和常驻工具协议；
2. `Agent Core`：DeepAgents 自带基础协议；
3. `User AGENTS Additions`：稳定的用户 Home 指令；
4. `Project AGENTS`：受信项目约定；
5. `Versioned Analytics / Semantics`：版本化业务语义；
6. `<agent_memory>`：当前作用域长期记忆；
7. `Active Skill Instructions` / `Activated Tool Guides`：按需能力协议；
8. Capability、Permission、Todo、`Current Run Delta`：当前调用的权威动态状态。

Home AGENTS 位于 Agent Core 之后、项目和动态层之前。它是稳定 Prompt 前缀的一部分，不是最终后缀。

## 4. 强制不变量

### 4.1 系统层始终领先

`SOUL.md`、`IDENTITY.md`、bundled `AGENTS.md` 和 `tool_guides/core.md` 必须保持在最前面的系统 Stable Core。Home 或项目文件只能补充，不能替换系统层。

### 4.2 Home AGENTS 是唯一用户 Prompt 文件

用户层只读取：

```text
$PUDDINGCLAW_HOME/profile/AGENTS.md
```

不得读取或合并用户版 `SOUL.md`、`IDENTITY.md`、`USER.md`。用户事实和稳定偏好应进入 Memory，而不是重新引入 `USER.md`。

### 4.3 项目层必须受信

只有注册且受信的项目根 `AGENTS.md` 可以注入。项目 ID、工作区和注册路径必须一致；不一致或未受信时项目层为空。

### 4.4 Runtime 状态不能由 Markdown 伪造

Capability、Permission、Tool 可见性、审批、SQL Guardrail、Receipt 和完成状态由 Backend 与 Harness 决定。Prompt、Skill 或项目文件中的声明不能扩大能力或绕过服务端校验。

### 4.5 用户状态只写 Home

Session、Memory、Skill、知识库、语义资产、Analytics Model、SQL Guardrail、配置和运行时数据必须写入 `$PUDDINGCLAW_HOME`。正常运行不得保留写入 backend 的逻辑或 fallback。

评测可以通过 `PUDDINGCLAW_EVALUATION_RUNTIME_ROOT` 使用隔离目录，但不得污染真实 Home。

### 4.6 Memory 必须受作用域约束

Memory 读取与写入必须使用同一个全局或项目目录。写入必须满足：

- Backend 绑定物理文件和 Memory 根；
- 拒绝未绑定作用域；
- 拒绝越出 Home Memory 根的路径和符号链接；
- 使用跨进程文件锁与原子替换，避免并发丢失和半写入；
- 只有工具返回成功后，Agent 才能声称已保存、修改或忘记。

## 5. Prompt 来源与所有权

| 层 | 物理来源 | 注入机制 | 可变性 | 作用域 |
|---|---|---|---|---|
| 系统人格 | `backend/prompts/SOUL.md` | Prompt Builder | bundled、稳定 | 主 Agent |
| 系统身份 | `backend/prompts/IDENTITY.md` | Prompt Builder | bundled、稳定 | 主 Agent |
| 系统操作规则 | `backend/prompts/AGENTS.md` | Prompt Builder | bundled、稳定 | 主 Agent |
| 常驻工具协议 | `backend/prompts/tool_guides/core.md` | Prompt Builder | bundled、稳定 | 主 Agent |
| DeepAgents 基础协议 | `deepagents.graph.BASE_AGENT_PROMPT` | `create_deep_agent()` | 依赖版本 | 各 Agent Core |
| 用户稳定指令 | `$PUDDINGCLAW_HOME/profile/AGENTS.md` | `UserAgentsPromptMiddleware` + 确定性重排 | 用户拥有、低频 | 主 Agent 与本地 Subagent |
| 项目约定 | `<project>/AGENTS.md` | Trusted Project Prompt Builder | 项目拥有、低频 | 主 Agent 当前项目 |
| Analytics Model | `$PUDDINGCLAW_HOME/definitions/analytics-models/` | Manager + Middleware | 用户拥有、版本化 | 主 Agent与相关 Subagent |
| Memory | `$PUDDINGCLAW_HOME/memory/` | `MemoryMiddleware` | 用户拥有、动态 | 主 Agent当前作用域 |
| Skill | `/skills/` Home 运行时视图 | `SkillsMiddleware` + `ToolsetMiddleware` | 安装与激活时变化 | 主 Agent与相关 Subagent |
| 按需 Tool Guide | `backend/prompts/tool_guides/*.md` | `ToolGuideMiddleware` | 按能力激活 | 当前模型调用 |
| 能力与权限 | Harness Runtime | `ToolsetMiddleware` 等 | 高频变化 | 当前模型调用 |
| Todo 与 Run Delta | Harness Runtime | Middleware / Manager | 高频变化 | 当前 Run/调用 |

## 6. 扩展内容放在哪里

| 要增加的内容 | 标准位置 |
|---|---|
| 用户跨项目工作习惯 | Home `profile/AGENTS.md` |
| 用户事实、称呼、稳定偏好 | 当前作用域 `MEMORY.md` |
| 单项目架构、命令和约定 | `<project>/AGENTS.md` |
| 所有主 Agent 必须遵守的产品规则 | `backend/prompts/AGENTS.md` |
| 系统人格或身份 | `backend/prompts/SOUL.md` / `IDENTITY.md` |
| 所有 Tool 都需要的通用协议 | `backend/prompts/tool_guides/core.md` |
| 某 Skill/Tool 才需要的使用协议 | 按需 Tool Guide + `manifest.yaml` |
| 可按需执行的专业方法和工作流 | `SKILL.md` |
| 权限、安全或强制校验 | Backend 策略、Tool Gate 或验证器 |

### 6.1 新增按需 Tool Guide

1. 新建 `backend/prompts/tool_guides/<guide-id>.md`；
2. 在 `backend/prompts/tool_guides/manifest.yaml` 登记唯一 ID、文件和 Skill/Tool 激活条件；
3. 增加“命中时注入、未命中时不注入”的测试；
4. 重建 Agent，使 Middleware 重新读取内容和 SHA256。

未登记的 Guide 文件必须被孤儿文件校验拒绝。Tool Guide 只描述模型使用协议，不能替代 Tool schema、权限和服务端验证。

### 6.2 新增 Skill

专业工作流应写成 `SKILL.md`，通过渐进披露按需读取。不要为了让模型“总能看到”而把完整 Skill、参考资料或脚本说明放入 Stable Core。

Skill 文档本身不授予 Tool 权限；实际可用能力以 Toolset、Capability Manifest 和 Tool Gate 为准。

## 7. Prompt Cache 标准

开启 `harness.prompt_cache.ordered_system_sections` 时，`reorder_system_prompt_sections()` 必须按第 3 节的顺序重排所有已知区块。

设计原则：

- 系统层、Agent Core、Home AGENTS 和项目 AGENTS 属于稳定前缀；
- Analytics/Semantics 是版本化区；
- Memory、Skill、Guide、能力、权限、Todo 和 Run Delta 属于动态区；
- 高频动态内容不得插入 Home AGENTS 之前；
- Home AGENTS 使用独立 fingerprint，Runtime 变化不得改变其 hash；
- 同类重复区块必须全部收集并确定性排序，不能只处理第一次出现。

Prompt 顺序用于可读性、模型行为和缓存稳定性，但不代表授权优先级。安全与权限始终以 Backend 为准。

## 8. 主 Agent 与 Subagent 边界

Subagent 是独立构建的 Agent，不复制主 Agent 的完整 SystemMessage。

- 主 Agent 拥有 bundled Stable Core、可信 Project AGENTS、当前作用域 Memory 和主 Run 控制块；
- Subagent 使用自己的 Agent Core、任务说明和运行时能力边界；
- Home AGENTS 同时进入主 Agent 和本地 Subagent，并位于各自 Agent Core 后、动态层前；
- Analytics Model、Skill、Tool Guide、能力和权限按委派上下文注入；
- Subagent 不可调用 `update_memory`、`update_goal` 或直接向用户提问。

不得假设主 Agent 新增的任意 Prompt 块会自动继承给 Subagent；需要继承的上下文必须在 Subagent 装配处显式声明并测试。

## 9. 禁止的扩展方式

- 不要把用户文件写进 backend；
- 不要重新创建 bundled 或用户层 `USER.md`；
- 不要创建用户层 `SOUL.md`、`IDENTITY.md` 并假设会被合并；
- 不要新增未被 Builder 或 Middleware 注册的 Markdown 并假设会自动注入；
- 不要把完整 Skill、全部语义资产或每轮变化的数据常驻到 Stable Core；
- 不要用 Prompt 代替权限、输入校验、SQL Guardrail、Receipt 或完成状态机；
- 不要通过 `write_file`、`execute` 或物理 Home 路径绕过 `update_memory`；
- 不要为兼容旧目录保留 backend 写入 fallback。

## 10. 实现映射

| 目标 | 权威实现 |
|---|---|
| bundled/project 基础构建 | `backend/graph/deepagents_prompt_builder.py` |
| Run 与 Subagent 装配 | `backend/graph/deepagents_manager.py` |
| Home AGENTS 读取 | `backend/graph/user_agents.py` |
| Home AGENTS 注入与稳定重排 | `backend/graph/middlewares/user_agents_prompt.py` |
| Prompt 分桶、排序和 fingerprint | `backend/graph/prompt_cache.py` |
| Memory 注入 | DeepAgents `MemoryMiddleware` + Home-backed `FilesystemBackend` |
| Memory 写入 | `backend/tools/update_memory_tool.py` |
| Skill/能力/权限清单 | `backend/graph/middlewares/toolset.py` |
| 按需 Tool Guide | `backend/graph/middlewares/tool_guides.py` |
| Todo 协议 | `backend/graph/middlewares/harness_todos.py` |
| 最终模型输入 Trace | `backend/graph/trace_collector.py` |

运行时排查必须查看最终 `ModelRequest.system_message` 的 Trace，不能只检查 Prompt Builder 返回的初始字符串。Memory、Skill、Guide、Manifest、Todo 和 Home AGENTS 都可能在 Middleware 阶段加入或重排。

## 11. 修改与发布验收

任何 Prompt、Middleware、Memory 或 Tool Guide 变更都必须检查：

- [ ] `SOUL → IDENTITY → AGENTS → Agent Core → Home AGENTS → Project AGENTS → Runtime` 顺序不变；
- [ ] Home AGENTS 不存在时不产生空占位层；
- [ ] 用户版 SOUL/IDENTITY/USER 不会被读取；
- [ ] 未受信项目 AGENTS 不会注入；
- [ ] Runtime 变化不改变 Home AGENTS fingerprint；
- [ ] Memory 全局/项目作用域隔离；
- [ ] Memory 无 backend fallback、symlink escape 或跨进程丢更新；
- [ ] Subagent 不可写 Memory 或 Goal；
- [ ] Tool Guide 命中和未命中路径都有测试；
- [ ] 干净检出包含 `backend/prompts/tool_guides/` 全部文件；
- [ ] 后端可以正常导入并构建 Agent。

最低回归命令：

```bash
PUDDINGCLAW_HOME=/tmp/puddingclaw-prompt-test \
  backend/.venv/bin/python -B -m pytest -q \
  backend/tests/test_user_agents_profile.py \
  backend/tests/test_prompt_cache_stability.py \
  backend/tests/test_deepagents_project_agents.py \
  backend/tests/test_tool_guides.py \
  backend/tests/test_update_memory_tool.py
```

后端导入检查：

```bash
PUDDINGCLAW_HOME=/tmp/puddingclaw-prompt-test \
  backend/.venv/bin/python -c \
  'import sys; sys.path.insert(0, "backend"); import app'
```
