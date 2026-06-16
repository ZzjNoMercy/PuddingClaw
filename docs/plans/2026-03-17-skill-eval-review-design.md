# Skill 评估审核页面设计文档

> 日期：2026-03-17
> 状态：已确认，待实施

---

## 一、需求概述

在 PuddingClaw 的 `/skills/review` 页面实现完整的 Skill 五维度质量评估功能：

1. 用户选择 Skill → 点击"开始评估" → 系统通过聊天 API 调用 `/skill-benchmark` 执行评估
2. 评估过程中展示实时进度（六阶段时间轴 + 五维度评分条 + Agent 分析日志）
3. 评估完成后展示结构化结果（总分 + 等级 + 雷达图 + 维度详情 + 优劣分析）

## 二、技术方案：约定格式输出 + 前端轻量解析

### 2.1 核心思路

- **零后端改动**：复用现有 `POST /api/chat` SSE 流式接口
- **Prompt 约定格式标记**：发送给 Agent 的评估指令中约定输出格式（如 `[STAGE:1:候选检查:started]`）
- **前端正则解析**：解析标记驱动 UI 状态，非标记文本展示为实时评估日志
- **双重信息**：结构化标记驱动仪表盘 + 自然语言日志展示真实过程

### 2.2 数据流

```
前端评估页 → POST /api/chat
  message: 带格式约定的评估 prompt
  session_id: eval-{skillName}-{timestamp}（专用会话）
  → Agent 加载 skill-benchmark → 执行 6 阶段流水线
  → SSE token 流推送到前端
  → useEvalStream Hook 解析标记 → 驱动 UI
  → 非标记文本 → 追加到实时日志面板
  → 评估完成 → 结果 JSON 保存到 skills/{name}/evals/
```

## 三、格式标记协议

Agent 输出中嵌入以下标记，前端正则匹配提取：

| 标记类型 | 格式 | 示例 |
|----------|------|------|
| 阶段进度 | `[STAGE:序号:阶段名:started/done]` | `[STAGE:1:候选检查:started]` |
| 维度评分 | `[DIM:维度名:分数:理由]` | `[DIM:触发质量:4:边界精确，包含正面触发词]` |
| 检查项 | `[CHECK:维度名:检查项描述:pass/fail]` | `[CHECK:触发质量:明确说明Use when:pass]` |
| 优势 | `[STRENGTH:维度名:描述]` | `[STRENGTH:路由清晰度:决策树覆盖完整]` |
| 问题 | `[WEAKNESS:维度名:描述]` | `[WEAKNESS:验证强度:缺少成功标准]` |
| 最终判定 | `[VERDICT:等级:总分:建议]` | `[VERDICT:基础扎实:18:建议优先加强验证强度]` |

### 正则表达式

```typescript
const PATTERNS = {
  stage:    /\[STAGE:(\d+):(.+?):(started|done)\]/,
  dim:      /\[DIM:(.+?):(\d+):(.+?)\]/,
  check:    /\[CHECK:(.+?):(.+?):(pass|fail)\]/,
  strength: /\[STRENGTH:(.+?):(.+?)\]/,
  weakness: /\[WEAKNESS:(.+?):(.+?)\]/,
  verdict:  /\[VERDICT:(.+?):(\d+):(.+?)\]/,
};
```

## 四、评估 Prompt 模板

```
请使用 /skill-benchmark 对以下 Skill 进行五维度质量评估：
Skill 路径：{skillPath}

评估框架（五维度，每维度 1-5 分）：
1. 触发质量 — 边界精确度，是否能精确识别触发请求
2. 路由清晰度 — agent 是否清楚"接下来该做什么"
3. 上下文效率 — SKILL.md 是否精简，detail 是否正确分层
4. 复用与确定性 — 重复工作是否脚本化，执行结果是否一致
5. 验证强度 — 是否有明确的成功标准

请严格按以下格式输出标记（标记之间可以自由输出分析文字）：
- 每个评估阶段开始/结束：[STAGE:序号:阶段名:started/done]
- 每个维度评分：[DIM:维度名:分数:一句话理由]
- 每个检查项：[CHECK:维度名:检查项描述:pass/fail]
- 优势：[STRENGTH:维度名:描述]
- 问题：[WEAKNESS:维度名:描述]
- 最终判定：[VERDICT:等级:总分:一句话建议]

等级标准：22-25=生产级，17-21=基础扎实，12-16=可用但不稳定，<12=需重做

评估阶段（按顺序执行）：
1. 候选检查 — 验证 SKILL.md 是否存在，文件结构是否合规
2. 结构分析 — 扫描 scripts/、references/、assets/ 目录结构
3. 触发与路由分析 — 分析 description、workflow、decision tree
4. 上下文与复用分析 — 检查 SKILL.md 行数、引用分层、脚本覆盖
5. 验证与质量分析 — 检查 validation 章节、成功标准、测试覆盖
6. 综合评分 — 汇总五维度评分，输出判定和改进建议
```

## 五、五维度评分标准（来源：五维度评分表模板）

### 维度 1：触发质量（1-5 分）

| 分数 | 描述 |
|------|------|
| 1 | 触发模糊或缺失调用上下文 |
| 2 | 能力已命名，但路由质量弱 |
| 3 | 主路径可用，但与相邻任务有重叠 |
| 4 | 触发精确且包含上下文 |
| 5 | 触发精确、有界、高度可发现 |

### 维度 2：路由清晰度（1-5 分）

| 分数 | 描述 |
|------|------|
| 1 | 无清晰流程 |
| 2 | 有步骤，但下一步行动模糊 |
| 3 | 主路径可用，分支弱 |
| 4 | 主路径和分支都明确 |
| 5 | 路由明显、紧凑、鲁棒 |

### 维度 3：上下文效率（1-5 分）

| 分数 | 描述 |
|------|------|
| 1 | 主文件臃肿（> 420 行） |
| 2 | 部分 detail 应该外部化（260-420 行） |
| 3 | 混合纪律 |
| 4 | 大部分 detail 正确分层（< 260 行） |
| 5 | 主文件精简，引用清晰路由（< 180 行） |

### 维度 4：复用与确定性（1-5 分）

| 分数 | 描述 |
|------|------|
| 1 | 重复工作完全手动 |
| 2 | 复用机会大多被忽略 |
| 3 | 部分可复用 |
| 4 | 重复工作大多稳定化 |
| 5 | 脚本/引用/资产意图明确 |

### 维度 5：验证强度（1-5 分）

| 分数 | 描述 |
|------|------|
| 1 | 无明确验证 |
| 2 | 验证隐含，未定义 |
| 3 | 基本检查存在 |
| 4 | 清晰的检查和成功标准 |
| 5 | 验证具体、可测试、难伪造 |

### 质量等级判定

| 总分范围 | 质量等级 | 徽章 |
|---------|---------|------|
| 22-25 | 生产级 | 🟢 |
| 17-21 | 基础扎实 | 🟡 |
| 12-16 | 可用但不稳定 | 🟠 |
| < 12 | 需重做 | 🔴 |

## 六、UI 设计

### 6.1 页面状态机

```
idle（空闲）→ evaluating（评估中）→ completed（完成）
     ↑                                    │
     └────────── 重新评估 ─────────────────┘
```

### 6.2 评估进度视图（evaluating 状态）

双栏布局：

**左栏（60%）**：
- 阶段时间轴（6 阶段，timeline 样式，三态：待处理/进行中/已完成）
- 五维度评分条（实时填充，分数到达时有短暂入场动画）

**右栏（40%）**：
- 实时评估日志（Agent 的非标记文本输出，终端风格，自动滚动）

**顶部**：Skill 名称 + 评估状态徽章 + 耗时计时器
**底部**：停止评估按钮

### 6.3 评估结果视图（completed 状态）

**顶部概览区**（三列）：
- 左：总分大数字 `18/25` + 环形进度条
- 中：质量等级徽章（🟢🟡🟠🔴）+ 一句话建议
- 右：CSS 五边形雷达图

**五维度详情卡片**（可折叠）：
- 概览行：维度名 + 图标 + 分数进度条 + 分数
- 展开内容：评分理由 + 检查项列表（✅/❌）
- 颜色：4-5 分绿色 / 3 分黄色 / 1-2 分红色

**优势与问题区**（左右两栏）：
- 左：绿色边框，列出 STRENGTH 项
- 右：橙色边框，列出 WEAKNESS 项

**底部操作区**：
- 重新评估 / 导出报告 / 查看评估日志

### 6.4 视觉风格

- 延续项目现有的 Tailwind CSS 风格：白色/半透明卡片 + 细边框 + backdrop-blur
- 不引入图表库，雷达图用 CSS/SVG 实现
- 清晰实用，不花哨
- 动画仅用于状态变化（评分到达、阶段切换），不加装饰性动画

## 七、组件拆分

```
skills/review/page.tsx          — 主页面（状态机 + 左侧栏）
  ├─ EvalProgress.tsx           — 评估进度视图
  │    ├─ StageTimeline.tsx     — 六阶段时间轴
  │    ├─ DimensionBars.tsx     — 五维度评分条
  │    └─ EvalLog.tsx           — 实时日志面板
  ├─ EvalResult.tsx             — 评估结果视图
  │    ├─ ScoreOverview.tsx     — 总分 + 等级 + 雷达图
  │    ├─ RadarChart.tsx        — CSS/SVG 五边形雷达图
  │    ├─ DimensionCard.tsx     — 维度详情卡片（×5 复用）
  │    └─ StrengthWeakness.tsx  — 优势/问题双栏
  └─ useEvalStream.ts           — 自定义 Hook
```

## 八、useEvalStream Hook 接口

```typescript
interface UseEvalStreamReturn {
  // 状态
  phase: 'idle' | 'evaluating' | 'completed';

  // 进度数据
  stages: Array<{
    id: number;
    name: string;
    status: 'pending' | 'running' | 'done';
  }>;
  dimensions: Array<{
    name: string;
    score: number | null;
    reason: string;
    checks: Array<{item: string; passed: boolean}>;
  }>;

  // 结果数据
  verdict: {
    grade: string;
    totalScore: number;
    note: string;
  } | null;
  strengths: Array<{dimension: string; text: string}>;
  weaknesses: Array<{dimension: string; text: string}>;

  // 日志
  logLines: string[];

  // 操作
  startEval: (skillName: string, skillPath: string) => void;
  stopEval: () => void;
  resetEval: () => void;

  // 元信息
  elapsed: number;
  error: string | null;
}
```

## 九、数据持久化

评估完成后保存结果 JSON 到 `skills/{skillName}/evals/`：

```json
{
  "skill_name": "switch-model",
  "timestamp": 1710000000,
  "total_score": 18,
  "grade": "基础扎实",
  "verdict_note": "建议优先加强验证强度",
  "dimensions": [
    {
      "name": "触发质量",
      "score": 4,
      "reason": "边界精确，包含正面触发词和负面边界词",
      "checks": [
        {"item": "明确说明Use when", "passed": true},
        {"item": "包含典型请求模式", "passed": true},
        {"item": "包含负面边界词", "passed": false}
      ]
    }
  ],
  "strengths": [
    {"dimension": "路由清晰度", "text": "决策树覆盖所有查询类型"}
  ],
  "weaknesses": [
    {"dimension": "验证强度", "text": "缺少明确的成功标准"}
  ],
  "session_id": "eval-switch-model-1710000000"
}
```

## 十、改动范围

### 新增文件（7 个）

| 文件 | 说明 |
|------|------|
| `frontend/src/components/eval/EvalProgress.tsx` | 评估进度视图 |
| `frontend/src/components/eval/EvalResult.tsx` | 评估结果视图 |
| `frontend/src/components/eval/StageTimeline.tsx` | 六阶段时间轴 |
| `frontend/src/components/eval/DimensionBars.tsx` | 五维度评分条 |
| `frontend/src/components/eval/DimensionCard.tsx` | 维度详情卡片 |
| `frontend/src/components/eval/StrengthWeakness.tsx` | 优势/问题双栏 |
| `frontend/src/hooks/useEvalStream.ts` | SSE 流解析 Hook |

### 修改文件（3 个）

| 文件 | 改动 |
|------|------|
| `frontend/src/app/skills/review/page.tsx` | 重写为三态页面 |
| `frontend/src/lib/evalApi.ts` | 新增 saveEvalResult / getEvalResult |
| `backend/api/skills.py` | 新增评估结果读写端点 |

### 不改文件

- `frontend/src/lib/api.ts` — 复用 streamChat
- `frontend/src/lib/store.tsx` — 评估用独立 Hook
- `backend/api/chat.py` — 原样复用
- 所有其他文件
