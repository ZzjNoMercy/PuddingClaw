import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import { isSessionSubmitting, mergeRunningSessionIds } from "./sessionConcurrency.ts";

test("a submission only blocks its own session", () => {
  const submitting = new Set(["session-a"]);

  assert.equal(isSessionSubmitting(submitting, "session-a"), true);
  assert.equal(isSessionSubmitting(submitting, "session-b"), false);
  assert.equal(isSessionSubmitting(submitting, "default"), false);
});

test("sidebar running state includes local and other-window sessions", () => {
  assert.deepEqual(
    Array.from(mergeRunningSessionIds(
      new Set(["session-a", "session-b"]),
      new Set(["session-b", "session-c"]),
    )).sort(),
    ["session-a", "session-b", "session-c"],
  );
});
