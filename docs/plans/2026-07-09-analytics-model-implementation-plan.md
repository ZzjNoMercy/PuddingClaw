# Analytics Model Implementation Plan

创建时间：2026-07-09 22:59:41

状态：第一版闭环已接入并通过静态验证，待浏览器运行态复核

## 第一性原理

分析模型不是传统编排器，也不是底层 LLM model。它是一个可迁移的业务上下文包：

- 用文件目录承载，方便导入、导出、审查和手工修复。
- 用 YAML frontmatter 提供机器可读元数据。
- 用 Markdown 正文提供 Agent 可读 Playbook。
- 用户在对话入口选择模型后，后端必须把它注入本轮 Agent 上下文。
- Trace 必须能证明模型真的加载、注入，并列出缺失引用。

## 对抗式审查清单

- [ ] 不能只做前端选择，不传给后端。
- [ ] 不能只保存模型文件，不进入 Agent 上下文。
- [ ] 不能让模型包目录绕过 FS backend 边界。
- [ ] 不能让导入 ZIP 写出 `analytics-models/` 根目录。
- [ ] 不能让模型引用的语义资产、SQL 守卫缺失时静默失败。
- [ ] 不能把“分析模型”文案写成“LLM 模型”。
- [ ] 不能破坏已有数据资产、语义资产、SQL 守卫页面。

## 实施范围

第一版做闭环：

- 后端 `backend/analytics-models/` registry。
- API：列表、刷新、详情、创建、导入。
- FS backend：`/analytics-models/` 挂载、`/api/files`、`write_file` 放行。
- Agent 请求：支持 `analytics_model_id`。
- Agent 上下文：注入 `model.md` metadata + 正文 Playbook。
- Trace/runtime：展示已选模型、路径和缺失引用。
- 前端智能问数页：模型列表、创建、导入、刷新。
- 新建模型时结构化选择数据表、语义资产、SQL 守卫，避免用户手写 frontmatter。
- 输入框：底部选择分析模型，显示 chip，发送时携带 `analytics_model_id`。

不做：

- 固定 DAG workflow。
- HTML/Markdown 模板渲染引擎。
- 多模型组合。
- 模型权限隔离。
- 复杂文件树编辑器。第一版可通过 `/api/files` 编辑 `model.md`。

## 任务清单

### 1. 后端模型 registry

- [x] 新增 `backend/analytics/models/registry.py`。
- [x] 定义 `analytics-model` frontmatter 解析。
- [x] 扫描 `backend/analytics-models/**/model.md`。
- [x] 支持创建模型模板。
- [x] 支持 ZIP / 文件夹导入。
- [x] 校验路径穿越和安全后缀。

### 2. 后端 API

- [x] `GET /api/analytics/models`
- [x] `POST /api/analytics/models/refresh`
- [x] `GET /api/analytics/models/{model_id}`
- [x] `POST /api/analytics/models`
- [x] `POST /api/analytics/models/import`

### 3. FS backend

- [x] DeepAgents 挂载 `/analytics-models/`。
- [x] terminal path alias 增加 `/analytics-models`。
- [x] runtime inventory 增加 `/analytics-models/`。
- [x] `/api/files` 允许 `analytics-models/`。
- [x] `write_file` 允许 `analytics-models/`。

### 4. Agent 上下文注入

- [x] `AgentRequest` 增加 `analytics_model_id`。
- [x] `streamAgent` 发送 `analytics_model_id`。
- [x] `DeepAgentsAgentManager.astream` 接收模型 ID。
- [x] 构建系统提示时注入模型上下文。
- [x] 模型注入信息写入 runtime inventory 或 trace metadata。

### 5. 前端管理页

- [x] `api.ts` 增加模型类型和 API client。
- [x] Analytics 页面加载模型列表。
- [x] 模型 section 展示真实模型卡片。
- [x] 新建模型弹窗。
- [x] 新建模型弹窗支持选择数据表、语义资产、SQL 守卫和默认模板。
- [x] 导入 ZIP / 文件夹。
- [x] 刷新模型 registry。

### 6. 输入框选择模型

- [x] ChatInput 加载模型列表。
- [x] 输入框底部增加“分析模型”选择入口。
- [x] 模型选择弹层。
- [x] 已选模型 chip。
- [x] 发送后请求携带 `analytics_model_id`。

### 7. 验证

- [x] 后端 py_compile。
- [x] 前端 `npx tsc --noEmit`。
- [x] 至少验证模型列表 API。
- [x] 至少验证创建模型 API。
- [x] 静态检查上下文注入函数输出。

## 验收标准

- 智能问数页能看到模型列表。
- 能新建一个模型并在 `backend/analytics-models/{slug}/model.md` 看到文件。
- 新建模型时选择的数据表、语义资产、SQL 守卫会写入 `model.md` frontmatter。
- Agent 输入框能选择模型并显示 chip。
- 发送消息时 payload 包含 `analytics_model_id`。
- 后端 Agent system prompt 包含当前模型名称、路径、metadata 和正文。
- Trace/runtime 能看到 `/analytics-models/` 挂载。
