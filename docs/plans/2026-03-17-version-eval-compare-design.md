# Skill 版本评估与对比功能设计文档

> 日期：2026-03-17
> 状态：已确认，待实施
> 前置：基于 2026-03-17-skill-eval-review-design.md 已实现的评估审核功能

---

## 一、需求概述

在已完成的 Skill 五维度评估审核页面基础上，新增：

1. **版本选择评估** — 评估页面和 compare 页面都支持选择 skill 的不同版本进行评估
2. **版本间评估对比** — 评估页面有完整对比视图（雷达图叠加 + 维度逐项对比），compare 页面有轻量对比摘要
3. **左侧栏多选** — 版本列表支持多选（最多 2 个），1 个 = 单版本模式，2 个 = 对比模式

## 二、存储与 API 层

### 2.1 评估结果存储路径

| 版本 | 存储路径 |
|------|----------|
| current（当前工作目录） | `skills/{name}/evals/five-dim-result.json` |
| 历史版本（如 v1.0） | `skills/{name}/versions/{label}/five-dim-result.json` |

### 2.2 后端 API 改动（eval_api.py）

修改现有端点，增加 `version` 查询参数：

```
POST /skills/{name}/eval-result?version=v1.0   → 保存到 versions/v1.0/five-dim-result.json
POST /skills/{name}/eval-result                → 保存到 evals/five-dim-result.json（current）
GET  /skills/{name}/eval-result?version=v1.0   → 读取 versions/v1.0/five-dim-result.json
GET  /skills/{name}/eval-result                → 读取 evals/five-dim-result.json（current）
```

新增端点，列出所有已评估版本的摘要：

```
GET /skills/{name}/eval-results-list
→ 扫描 evals/ 和 versions/*/five-dim-result.json
→ 返回 [
    {version: "current", total_score: 18, grade: "基础扎实", timestamp: 1710000000},
    {version: "v1.0", total_score: 15, grade: "可用但不稳定", timestamp: 1709000000}
  ]
```

### 2.3 前端 API 改动（evalApi.ts）

- `saveEvalResult(skillName, result, version?)` — 增加可选 version 参数
- `getEvalResult(skillName, version?)` — 增加可选 version 参数
- 新增 `listEvalResults(skillName)` — 获取所有已评估版本的摘要列表

新增类型：

```typescript
export interface EvalResultSummary {
  version: string;
  total_score: number;
  grade: string;
  timestamp: number;
}
```

## 三、评估页面左侧栏重构

### 3.1 两层选择结构

从"只选 skill"改为"skill → 版本列表"：

- 点击 skill 名称 → 展开/折叠版本列表
- 版本列表第一项固定为 `current`，其余为 `versions/` 下的历史版本
- 每个版本前有勾选框，最多选 2 个

### 3.2 交互逻辑

- 选 1 个 → 单版本评估/查看模式
- 选 2 个 → 自动进入对比模式
- 尝试选第 3 个 → 禁止（toast 提示"最多选择 2 个版本"）
- 跨 skill 选择时自动清除前一个 skill 的选择
- 底部显示已选计数 + "清除"按钮

### 3.3 版本项显示信息

每个版本行：
- 勾选框
- 版本标签（`current` / `v1.0`）
- 有评估结果时：显示分数徽章（如 `18分 🟡`）
- 无评估结果时：显示 `未评估` 灰色文字

### 3.4 数据流

选中 skill 后：
1. 调用 `listVersions(skillName)` 获取版本列表
2. 调用 `listEvalResults(skillName)` 获取已评估版本的摘要
3. 合并为统一列表

## 四、主内容区状态机

### 4.1 视图推导（组合判断，非新 enum）

```typescript
const selectedVersions: Array<{skill: string, version: string}>;  // 长度 0/1/2

selectedVersions.length === 0              → 空状态
selectedVersions.length === 1 + idle       → 单版本详情（开始评估/历史结果）
selectedVersions.length === 1 + evaluating → 单版本评估进度
selectedVersions.length === 2              → 对比视图
```

### 4.2 单版本视图

复用现有组件，不变。唯一区别：评估时 prompt 中的路径根据版本不同：
- current → `skills/{name}/`
- 历史版本 → `skills/{name}/versions/{label}/`

### 4.3 对比视图（EvalCompare.tsx）

布局从上到下：

1. **对比标题栏** — `v1.0 vs current`
2. **雷达图叠加** — 两个版本的数据多边形叠在同一张雷达图上，用不同颜色区分（蓝色 vs 绿色）
3. **总分对比卡片** — 三列：版本 A 分数/等级、版本 B 分数/等级、差值（+N ↑ 绿色 / -N ↓ 红色）
4. **五维度逐项对比** — 每行：维度名 + 两个版本分数 + 差值箭头
5. **未评估处理** — 某版本无评估结果时显示灰色占位 + "评估此版本"按钮
6. **底部操作** — 导出对比报告

## 五、Compare 页面轻量扩展

在代码 diff 区域下方新增"评估对比摘要"卡片：

- 仅在两个版本都有评估结果时显示
- 展示：两个版本的总分/等级 + 五维度分数变化（紧凑单行）
- "查看完整评估对比 →" 链接跳转到评估页面并自动选中这两个版本
- 无评估结果时显示"暂无评估数据"

## 六、RadarChart 双数据集支持

修改 `RadarChart.tsx`，增加可选的第二组数据：

```typescript
interface RadarChartProps {
  dimensions: Array<{ name: string; score: number | null }>;
  dimensionsB?: Array<{ name: string; score: number | null }>;  // 新增
  size?: number;
}
```

- `dimensions` 绘制绿色多边形（保持现有行为）
- `dimensionsB` 绘制蓝色多边形（半透明填充 + 实线描边）
- 两组数据共享同一个背景网格和标签
- 标签区域同时显示两个分数（如 `3 → 4`）

## 七、useEvalStream Hook 改动

`startEval` 增加 `version` 参数：

```typescript
startEval: (skillName: string, skillPath: string, version?: string) => void;
```

- version 传入后保存到 ref，用于 done 事件时调用 `saveEvalResult(name, result, version)`
- prompt 中的 Skill 路径根据 version 拼接

## 八、改动文件清单

### 新增 2 个文件

| 文件 | 说明 |
|------|------|
| `frontend/src/components/eval/EvalCompare.tsx` | 完整对比视图 |
| `frontend/src/components/eval/EvalCompareSummary.tsx` | 轻量摘要卡片 |

### 修改 6 个文件

| 文件 | 改动 |
|------|------|
| `frontend/src/components/eval/RadarChart.tsx` | 支持双数据集 |
| `frontend/src/hooks/useEvalStream.ts` | startEval 增加 version 参数 |
| `frontend/src/lib/evalApi.ts` | API 增加 version + listEvalResults |
| `frontend/src/app/skills/review/page.tsx` | 左侧栏重构 + 对比模式 |
| `frontend/src/app/skills/compare/page.tsx` | 底部加摘要 |
| `backend/api/eval_api.py` | 端点支持 version + 新增列表端点 |

### 不改文件

所有其他 eval 组件（EvalProgress、EvalResult、ScoreOverview、DimensionCard、DimensionBars、EvalLog、StageTimeline、StrengthWeakness）、api.ts、store.tsx
