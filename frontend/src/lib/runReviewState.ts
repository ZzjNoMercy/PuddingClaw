import type {
  HarnessRun,
  RunReviewReport,
  RunReviewStatus,
  RunReviewVerificationOperation,
} from "./api";

export interface RunReviewProjection {
  eligible: boolean;
  status?: RunReviewStatus;
}

export function visibleRunReviewStatus(
  status: RunReviewStatus,
): Exclude<RunReviewStatus, "not_requested"> | undefined {
  return status === "not_requested" ? undefined : status;
}

/** Derive visible review state from durable control-plane facts. */
export function projectRunReviewState(
  run: HarnessRun | undefined,
  report: RunReviewReport | undefined,
  operations: Record<string, RunReviewVerificationOperation> | undefined,
): RunReviewProjection {
  if (!run) {
    return report
      ? { eligible: true, status: report.status }
      : { eligible: false };
  }

  // A configured policy is only intent. Cancelled/failed Runs have no final
  // answer to accept, so they must never acquire review UI or review authority.
  const eligible = Boolean(
    run.run_kind === "standalone"
    && !run.goal_id
    && run.status === "completed"
    && run.outcome === "completed"
  );
  if (!eligible) return { eligible: false };
  if (report) return { eligible: true, status: report.status };

  const snapshotId = String(run.evaluation_snapshot_id || "");
  if (!snapshotId) return { eligible: true };
  const matching = Object.values(operations || {}).filter((operation) =>
    operation.snapshot_id === snapshotId
    && operation.method === "semantic_rubric"
  );
  if (matching.length === 0) return { eligible: true };
  const latest = matching.sort((left, right) =>
    Number(right.attempt_no || 0) - Number(left.attempt_no || 0)
  )[0];
  return {
    eligible: true,
    status: latest.status === "pending" ? "pending" : "running",
  };
}
