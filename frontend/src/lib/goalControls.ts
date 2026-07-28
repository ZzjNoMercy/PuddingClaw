export type GoalLifecycleStatus =
  | "active"
  | "paused"
  | "blocked"
  | "completed"
  | "cancelled"
  | "budget_exceeded";

export type GoalPrimaryAction = "pause" | "start" | "resume_and_start" | null;

export interface GoalControlPresentation {
  metric: string;
  primaryAction: GoalPrimaryAction;
  primaryLabel: string;
}

export type GoalExecutionStep = "pause" | "resume" | "start";

export type GoalTodoStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "cancelled"
  | "error";

export interface GoalTodoProgress {
  completed: number;
  total: number;
}

export function goalTodoProgress(statuses: GoalTodoStatus[]): GoalTodoProgress {
  const activeStatuses = statuses.filter((status) => status !== "cancelled");
  return {
    completed: activeStatuses.filter((status) => status === "completed").length,
    total: activeStatuses.length,
  };
}

export function goalRemainsVisible(status: GoalLifecycleStatus): boolean {
  // Completion closes execution authority, not inspection authority. Keep the
  // finished Goal in the drawer so its Runs, Todos, evidence, and verification
  // remain reviewable; only an explicitly cancelled Goal leaves the surface.
  return status !== "cancelled";
}

export function shouldShowInlineBudgetRequest(
  status: GoalLifecycleStatus,
  requestedStatus?: GoalLifecycleStatus | null,
): boolean {
  return status === "budget_exceeded" && !requestedStatus;
}

export function parseGoalBudgetRounds(value: string | number): number | null {
  const normalized = typeof value === "string" ? value.trim() : value;
  if (normalized === "") return null;
  const rounds = Number(normalized);
  if (!Number.isInteger(rounds) || rounds < 1 || rounds > 100) return null;
  return rounds;
}

export function goalRevisionApplyPlan(
  status: GoalLifecycleStatus,
  executionActive: boolean,
): GoalExecutionStep[] {
  if (executionActive) return ["pause", "resume", "start"];
  if (status === "paused" || status === "blocked") return ["resume", "start"];
  if (status === "active") return ["start"];
  return [];
}

export function goalControlPresentation(
  status: GoalLifecycleStatus,
  requestedStatus: GoalLifecycleStatus | null | undefined,
  executionActive: boolean,
  pendingRevision = false,
): GoalControlPresentation {
  if (requestedStatus === "paused") {
    return { metric: "正在暂停", primaryAction: null, primaryLabel: "正在暂停" };
  }
  if (requestedStatus === "cancelled") {
    return { metric: "正在取消", primaryAction: null, primaryLabel: "正在取消" };
  }
  if (status === "active" && executionActive) {
    return { metric: "进行中", primaryAction: "pause", primaryLabel: "暂停目标" };
  }
  if (status === "active") {
    return {
      metric: pendingRevision ? "待按新版本启动" : "待启动",
      primaryAction: "start",
      primaryLabel: pendingRevision ? "按新版本启动目标" : "启动目标",
    };
  }
  if (status === "paused") {
    return { metric: "已暂停", primaryAction: "resume_and_start", primaryLabel: "继续目标" };
  }
  if (status === "blocked") {
    return {
      metric: "受阻",
      primaryAction: "resume_and_start",
      primaryLabel: "重新启动目标",
    };
  }
  const terminalLabels: Record<GoalLifecycleStatus, string> = {
    active: "待启动",
    paused: "已暂停",
    blocked: "受阻",
    completed: "已完成",
    cancelled: "已取消",
    budget_exceeded: "预算已耗尽",
  };
  return { metric: terminalLabels[status], primaryAction: null, primaryLabel: "" };
}
