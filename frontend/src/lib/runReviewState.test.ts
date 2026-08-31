import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import { projectRunReviewState, visibleRunReviewStatus } from "./runReviewState.ts";

const completedRun = {
  run_id: "run-1",
  query_id: "query-1",
  session_id: "session-1",
  objective: "完成任务",
  run_kind: "standalone" as const,
  goal_id: null,
  status: "completed" as const,
  outcome: "completed",
  model_call_count: 1,
  created_at: 1,
  updated_at: 2,
};

test("a cancelled Run never projects a review spinner from policy alone", () => {
  assert.deepEqual(projectRunReviewState({
    ...completedRun,
    status: "cancelled",
    outcome: "cancelled",
    run_review_policy: "shadow",
  }, undefined, undefined), { eligible: false });
});

test("a configured policy without a durable operation is not pending", () => {
  assert.deepEqual(projectRunReviewState({
    ...completedRun,
    run_review_policy: "shadow",
  }, undefined, {}), { eligible: true });
});

test("a real semantic operation projects pending and running states", () => {
  const run = { ...completedRun, evaluation_snapshot_id: "snapshot-1" };
  assert.deepEqual(projectRunReviewState(run, undefined, {
    "operation-1": {
      operation_id: "operation-1",
      snapshot_id: "snapshot-1",
      method: "semantic_rubric",
      status: "pending",
      attempt_no: 0,
    },
  }), { eligible: true, status: "pending" });
  assert.deepEqual(projectRunReviewState(run, undefined, {
    "operation-1": {
      operation_id: "operation-1",
      snapshot_id: "snapshot-1",
      method: "semantic_rubric",
      status: "running",
      attempt_no: 0,
    },
  }), { eligible: true, status: "running" });
});

test("an immutable report owns the final visible status", () => {
  assert.deepEqual(projectRunReviewState(completedRun, {
    report_id: "report-1",
    run_id: "run-1",
    snapshot_id: "snapshot-1",
    policy: "shadow",
    status: "satisfied",
  }, undefined), { eligible: true, status: "satisfied" });
});

test("not_requested clears an optimistic review status", () => {
  assert.equal(visibleRunReviewStatus("not_requested"), undefined);
  assert.equal(visibleRunReviewStatus("pending"), "pending");
});
