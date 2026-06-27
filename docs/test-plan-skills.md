# Skill 测试用例清单

> 按「基础工具 → 信息查询 → 代码工程 → 内容生成 → 元技能」整理，逐个验证后打钩。

---

## 1. 基础工具类

### ✅ `get-date`
**输入**：`现在几点了？今天是几号？`

**验证项**：
- [ ] 调用 `execute_skill("get-date")`
- [ ] 回答里包含当前日期时间
- [ ] 时间轴只出现 1 次 tool call

---

### ✅ `get_weather`
**输入**：`北京今天天气怎么样？`

**验证项**：
- [ ] 调用 `execute_skill("get_weather")` 或 `fetch_url("https://wttr.in/Beijing?format=j1&lang=zh")`
- [ ] 返回温度、天气描述、湿度、风速
- [ ] 没有编造数据

---

## 2. 信息查询类

### ✅ `aihot`
**输入**：`最近 AI 圈有什么大新闻？`

**验证项**：
- [ ] 调用 `execute_skill("aihot")`
- [ ] 返回带 `[^source_id]` 引用
- [ ] 右侧来源面板出现 aihot 来源
- [ ] 每条新闻都能点来源追溯

---

### ✅ `tavily-search` / `web-tools-guide`
**输入**：`搜索一下 OpenAI 最新发布的模型`

**验证项**：
- [ ] 调用 `tavily_search`
- [ ] 结果带引用标记
- [ ] 来源面板显示检索结果

---

## 3. 代码工程类（重点）

### ✅ `review`
**输入**：
> 帮我 review 一下最近这次提交

或

> 检查 `backend/graph/deepagents_manager.py` 的改动有没有问题

**验证项**：
- [ ] 触发 `review` skill
- [ ] 输出分类：security / performance / maintainability / testing 等
- [ ] 给出具体代码行引用
- [ ] 不会直接改代码，只输出 review 结论

---

### ✅ `qa`
**输入**：
> 给我们的前端跑一轮 QA，看看有没有明显 bug

**验证项**：
- [ ] 触发 `qa` skill
- [ ] 生成测试计划
- [ ] 调用相关工具检查（如读文件、运行测试）
- [ ] 输出 health score 和 bug 列表

---

### ✅ `investigate`
**输入**：
> 后端日志里看到一个错误：`AssertionError: Please remove duplicate middleware instances`，帮我 investigate

**验证项**：
- [ ] 触发 `investigate` skill
- [ ] 走四阶段：investigate → analyze → hypothesize → implement
- [ ] 先定位根因再给出修复
- [ ] 时间轴显示多轮 tool 调用

---

### ✅ `ship`
**输入**：
> 当前分支测试都过了，帮我 ship 到 main

**验证项**：
- [ ] 触发 `ship` skill
- [ ] 执行：merge base → run tests → review diff → bump VERSION → update CHANGELOG → commit → push
- [ ] **注意**：这个会真改 git，最好在测试分支跑

---

## 4. 内容生成类

### ✅ `dialogue-summarizer`
**输入**：
> 把下面这段会议记录总结一下：[粘贴一段对话]

**验证项**：
- [ ] 调用 `execute_skill("dialogue-summarizer")`
- [ ] 输出按模板：关键点、待办、决策
- [ ] 待办项出现在右侧进度卡片

---

### ✅ `design-html`
**输入**：
> 给我生成一个展示项目状态的 HTML 仪表盘

**验证项**：
- [ ] 调用 `design-html` skill
- [ ] 输出完整 HTML 文件
- [ ] 文件写入 workspace，可打开预览

---

## 5. 元技能

### ✅ `skill-creator` / `skill-creator-pro`
**输入**：
> 帮我创建一个能查询 GitHub trending 的新 skill

**验证项**：
- [ ] 触发 skill creator
- [ ] 在 `backend/skills/` 下生成新 skill 目录
- [ ] 包含 SKILL.md 和脚本
- [ ] 生成后能用 `execute_skill` 调用新 skill

---

### ✅ `skill-benchmark`
**输入**：
> 跑一遍 skill-benchmark，看看最近加的 skill 表现怎么样

**验证项**：
- [ ] 触发 benchmark
- [ ] 输出评分报告
- [ ] 包含 trace 验证

---

## 建议测试顺序

1. **先跑基础**：get-date、get_weather，确认 execute_skill 通路正常
2. **再跑信息查询**：aihot，验证引用和来源面板
3. **跑代码类**：investigate 一个已知错误，验证时间轴和修复流程
4. **跑内容类**：dialogue-summarizer，验证进度卡片自动展开
5. **最后跑 ship**：在副本分支上验证发布流程
