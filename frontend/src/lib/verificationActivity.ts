export type VerificationEventKind =
  | "deterministic_checks_completed"
  | "rubric_evaluation_end";

export interface VerificationFailurePresentation {
  willContinue: boolean;
  label: string;
  detail: string;
  summary?: string;
  displayStatus: "running" | "failed";
}

type VerificationTimelineItem = {
  type: string;
  id: string;
  status?: string;
};

type VerificationCriterion = {
  criterion_id?: unknown;
  name?: unknown;
  passed?: unknown;
  gap?: unknown;
  reason?: unknown;
  explanation?: unknown;
};

const criterionLabels: Record<string, string> = {
  artifact_delivery: "产物交付",
  code_validation: "代码验证",
  todo_reconciliation: "Todo 收口",
  task_fulfillment: "任务完成度",
  report_integrity: "报告完整性",
  metric_consistency: "指标口径一致性",
  time_scope: "数据时间范围",
  analysis_traceability: "分析证据可追溯",
};

function verificationGapDetail(criteria: unknown): string {
  if (!Array.isArray(criteria)) return "";
  const issues = criteria
    .filter((item): item is VerificationCriterion => (
      typeof item === "object" && item !== null && item.passed === false
    ))
    .slice(0, 3)
    .map((item) => {
      const criterionId = String(item.criterion_id || item.name || "未命名验收项");
      const label = criterionLabels[criterionId] || criterionId;
      const rawReason = String(
        item.gap || item.reason || item.explanation || "未提供具体判定依据",
      ).trim();
      const reason = rawReason.length > 180
        ? `${rawReason.slice(0, 177).trimEnd()}…`
        : rawReason;
      return `${label}：${reason}`;
    });
  return issues.length > 0 ? `待处理：${issues.join("；")}` : "";
}

export function settleRunningVerificationActivities<
  T extends VerificationTimelineItem,
>(timeline: T[], status: string): T[] {
  return timeline.map((item) => (
    item.type === "activity"
      && item.id.startsWith("verification-")
      && item.status === "running"
      ? { ...item, status }
      : item
  ));
}

export function verificationFailureActivity(
  eventKind: VerificationEventKind,
  result: string,
  explicitWillContinue?: boolean,
  hasGoal = false,
  criteria?: unknown,
): VerificationFailurePresentation {
  const willContinue = eventKind === "rubric_evaluation_end"
    ? result === "needs_revision"
    : explicitWillContinue === true;
  const scope = eventKind === "deterministic_checks_completed"
    ? "完成条件"
    : "完成质量";
  const gapDetail = verificationGapDetail(criteria);
  if (willContinue) {
    return {
      willContinue: true,
      label: `发现${scope}缺口，正在自动继续修复`,
      detail: gapDetail || "验收事件未附具体缺口；Agent 正在重新核对结构化验收结果。",
      displayStatus: "running",
    };
  }

  if (result === "infrastructure_error") {
    return {
      willContinue: false,
      label: "验收服务异常，自动处理已停止",
      detail: "这不代表业务产物未通过。请发送“重试验收”继续。",
      summary: [
        "**验收流程遇到异常，任务结果尚未被判定失败。**",
        "",
        "当前进度和证据已保留。请发送 **“重试验收”**；如果再次出现，可在右侧“验收”中查看技术明细。",
      ].join("\n"),
      displayStatus: "failed",
    };
  }

  const detail = gapDetail || (hasGoal
    ? "Goal、Todo 和证据已保留。发送“继续完成剩余工作”即可从当前进度继续。"
    : "请查看右侧验收明细，再发送具体修复要求。");
  return {
    willContinue: false,
    label: `${scope}仍有缺口，自动处理已停止`,
    detail,
    summary: hasGoal
      ? [
          "**还有未完成项，但当前进度没有丢失。**",
          "",
          "Goal、Todo、产物和证据均已保留。请发送 **“继续完成剩余工作”**，系统会启动新的 Goal Run 并从当前进度继续。",
        ].join("\n")
      : [
          "**还有未完成项。**",
          "",
          "请打开右侧 **“验收”** 查看具体缺口，然后发送对应的修复要求。",
        ].join("\n"),
    displayStatus: "failed",
  };
}
