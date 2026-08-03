import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import {
  datasetActions,
  estimateCaseRuns,
  experimentIsTerminal,
  safeRemoteUrl,
} from "./evaluationState.ts";

test("published datasets are immutable but exportable and syncable", () => {
  const actions = datasetActions({
    status: "published",
    current_version: 2,
    cases: [{}],
  } as never);
  assert.equal(actions.editable, false);
  assert.equal(actions.publishable, false);
  assert.equal(actions.reopenable, true);
  assert.equal(actions.syncable, true);
});

test("experiment polling stops only for terminal states", () => {
  assert.equal(experimentIsTerminal("running"), false);
  assert.equal(experimentIsTerminal("cancel_requested"), false);
  assert.equal(experimentIsTerminal("completed"), true);
  assert.equal(experimentIsTerminal("failed"), true);
  assert.equal(experimentIsTerminal("cancelled"), true);
});

test("remote links accept only http and https", () => {
  assert.equal(safeRemoteUrl("javascript:alert(1)"), null);
  assert.equal(safeRemoteUrl("https://smith.langchain.com/o/example"), "https://smith.langchain.com/o/example");
});

test("run estimate multiplies cases and repetitions", () => {
  assert.equal(estimateCaseRuns(4, 3), 12);
});
