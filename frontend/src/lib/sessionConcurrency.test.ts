import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import {
  isSessionSubmitting,
  mergeRunningSessionIds,
  releaseOrphanedPlaceholderLock,
  rebindSessionScopedLock,
} from "./sessionConcurrency.ts";

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

test("new-chat submission lock follows the durable session id", () => {
  const original = new Set(["default", "session-existing"]);
  const rebound = rebindSessionScopedLock(
    original,
    "default",
    "session-created",
  );

  assert.deepEqual(
    Array.from(rebound).sort(),
    ["session-created", "session-existing"],
  );
  assert.equal(rebound.has("default"), false);
  assert.equal(original.has("default"), true);
});

test("session lock rebinding does not invent a missing reservation", () => {
  const rebound = rebindSessionScopedLock(
    new Set(["session-existing"]),
    "default",
    "session-created",
  );

  assert.deepEqual(Array.from(rebound), ["session-existing"]);
});

test("an orphaned default lock is released without touching durable sessions", () => {
  const recovered = releaseOrphanedPlaceholderLock(
    new Set(["default", "session-running"]),
    "default",
    { creationPending: false, streaming: false },
  );

  assert.deepEqual(Array.from(recovered), ["session-running"]);
});

test("a live default creation keeps its duplicate-submit lock", () => {
  const creating = releaseOrphanedPlaceholderLock(
    new Set(["default"]),
    "default",
    { creationPending: true, streaming: false },
  );
  const streaming = releaseOrphanedPlaceholderLock(
    new Set(["default"]),
    "default",
    { creationPending: false, streaming: true },
  );

  assert.equal(creating.has("default"), true);
  assert.equal(streaming.has("default"), true);
});
