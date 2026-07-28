import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import { goalControlPresentation, goalRemainsVisible, goalRevisionApplyPlan, goalTodoProgress, parseGoalBudgetRounds, shouldShowInlineBudgetRequest } from "./goalControls.ts";

test("cancelled Todo tombstones do not dilute visible progress", () => {
  assert.deepEqual(
    goalTodoProgress([
      "cancelled",
      "cancelled",
      "completed",
      "completed",
      "pending",
    ]),
    { completed: 2, total: 3 },
  );
});

test("completed goals remain visible for review while cancelled goals disappear", () => {
  assert.equal(goalRemainsVisible("budget_exceeded"), true);
  assert.equal(goalRemainsVisible("paused"), true);
  assert.equal(goalRemainsVisible("completed"), true);
  assert.equal(goalRemainsVisible("cancelled"), false);
});

test("an unresolved budget request remains visible in the conversation", () => {
  assert.equal(shouldShowInlineBudgetRequest("budget_exceeded"), true);
  assert.equal(
    shouldShowInlineBudgetRequest("budget_exceeded", "cancelled"),
    false,
  );
  assert.equal(shouldShowInlineBudgetRequest("paused"), false);
});

test("budget extension accepts only whole Runs within the API boundary", () => {
  assert.equal(parseGoalBudgetRounds("2"), 2);
  assert.equal(parseGoalBudgetRounds(100), 100);
  assert.equal(parseGoalBudgetRounds(""), null);
  assert.equal(parseGoalBudgetRounds("1.5"), null);
  assert.equal(parseGoalBudgetRounds(0), null);
  assert.equal(parseGoalBudgetRounds(101), null);
});

test("an active Goal without a Run is actionable, not falsely running", () => {
  assert.deepEqual(goalControlPresentation("active", null, false), {
    metric: "待启动",
    primaryAction: "start",
    primaryLabel: "启动目标",
  });
});

test("only a Goal with a live Run exposes pause as the primary action", () => {
  assert.deepEqual(goalControlPresentation("active", null, true), {
    metric: "进行中",
    primaryAction: "pause",
    primaryLabel: "暂停目标",
  });
});

test("paused and revised Goals expose a meaningful start action", () => {
  assert.equal(
    goalControlPresentation("paused", null, false).primaryAction,
    "resume_and_start",
  );
  assert.deepEqual(goalControlPresentation("active", null, false, true), {
    metric: "待按新版本启动",
    primaryAction: "start",
    primaryLabel: "按新版本启动目标",
  });
});

test("pending control requests disable the primary action", () => {
  assert.deepEqual(goalControlPresentation("active", "paused", true), {
    metric: "正在暂停",
    primaryAction: null,
    primaryLabel: "正在暂停",
  });
});

test("applying an edit to a running Goal restarts from the new revision", () => {
  assert.deepEqual(
    goalRevisionApplyPlan("active", true),
    ["pause", "resume", "start"],
  );
  assert.deepEqual(
    goalRevisionApplyPlan("paused", false),
    ["resume", "start"],
  );
  assert.deepEqual(goalRevisionApplyPlan("active", false), ["start"]);
});
