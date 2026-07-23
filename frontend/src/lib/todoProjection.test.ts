import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import { shouldApplyTodoSnapshot } from "./todoProjection.ts";

const goal = { kind: "goal" as const, goal_id: "goal-1", goal_revision: 1 };

test("rejects a stale Todo snapshot for the same Goal revision", () => {
  assert.equal(shouldApplyTodoSnapshot(goal, 8, goal, 7), false);
});

test("accepts an equal or newer Todo snapshot", () => {
  assert.equal(shouldApplyTodoSnapshot(goal, 8, goal, 8), true);
  assert.equal(shouldApplyTodoSnapshot(goal, 8, goal, 9), true);
});

test("accepts a new authority even when its local revision is lower", () => {
  const nextGoal = { kind: "goal" as const, goal_id: "goal-1", goal_revision: 2 };
  assert.equal(shouldApplyTodoSnapshot(goal, 8, nextGoal, 1), true);
});
