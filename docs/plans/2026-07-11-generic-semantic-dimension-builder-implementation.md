# 通用语义维度构建 Skill 实施账本

状态：已完成  
目标：把“跨表构建维度”从车系专用脚本提升为固定 JSON 契约驱动的 Skill。LLM 发现候选与生成草案，用户通过 HITL 明确输入角色、基准表、键字段与冲突策略；Job 按固定模板产出 staging Crosswalk，显式发布后替换活跃引用。

## 不变量

1. 维度 `dimension.md` 是语义和活跃产物说明，不强制绑定长期数据资产。
2. 临时附件可作为本次构建输入；原始附件不自动进入知识库或智能问数资产目录。
3. 构建规则、staging 产物和最终 Crosswalk 均有固定 JSON schema；LLM 不能自行发明落盘格式。
4. 基准表由 HITL 选择，绝不全局写死为产品配置表。
5. Job 只写 staging；发布仍需用户显式确认，并原子替换 `dimension.md` 声明的引用文件。
6. 规则支持一个 canonical 输入和多个 source 输入；增量构建默认追加既有活跃 Crosswalk 的来源绑定，不因新附件覆盖旧来源。

## 实施清单

1. [x] 梳理现有车系 Skill、Job、Worker、发布器与附件存储边界。
2. [x] 新增通用构建规则/产物契约与输入检查工具。
3. [x] 新增维度规则 HITL 请求、恢复 API、SSE 和对话选择卡。
4. [x] 实现规则驱动的 `entity_crosswalk_v1` Worker 适配器，支持附件、表格资产与数据库表。
5. [x] 更新 Skill/Tool Guide，使 Agent 先检查输入、请求 HITL、再入队。
6. [x] 编写单元和端到端测试：两个临时 Excel，HITL 选择基准输入，构建、校验、发布。
7. [x] 运行后端测试、前端类型检查和运行时工具/API 验证。

## 验证记录

- `pytest backend/tests/test_generic_semantic_dimension_builder.py backend/tests/test_semantic_dimension_jobs.py backend/tests/test_vehicle_series_full.py backend/tests/test_deepagents_manager.py -q`：33 passed。
- `npx tsc --noEmit`：通过。
- `py_compile` 与 `git diff --check`：通过。
- 运行中的 backend OpenAPI 已暴露 `GET/POST /api/analytics/dimension-build-requests/{request_id}`；Agent 运行时工具集已包含 `inspect_dimension_build_input`、`request_dimension_build_rule` 与 `enqueue_semantic_dimension_build`。
