import assert from "node:assert/strict";
import test from "node:test";

// @ts-expect-error Node's native TypeScript runner requires the source suffix.
import { getSubagentActivityIdentity, getSubagentToolLabel } from "./subagentActivity.ts";

test("task tool exposes user-facing subagent execution states", () => {
  assert.equal(getSubagentToolLabel("running"), "子代理执行中");
  assert.equal(getSubagentToolLabel("done"), "子代理已返回结果");
  assert.equal(getSubagentToolLabel("done", true), "子代理执行未完成");
  assert.equal(getSubagentToolLabel("done").includes("完成"), false);
});

test("subagent lifecycle terminal events overwrite the start activity", () => {
  const started = getSubagentActivityIdentity("subagent_started", {
    subagent_run_id: "subrun-1",
  });
  const completed = getSubagentActivityIdentity("subagent_completed", {
    subagent_run_id: "subrun-1",
  });

  assert.equal(started.activityId, completed.activityId);
  assert.equal(completed.terminal, true);
  assert.equal(completed.settlePrefix, "subagent-subrun-1-");
});

test("permission wait reuses lifecycle row without pretending completion", () => {
  const started = getSubagentActivityIdentity("subagent_started", {
    subagent_run_id: "subrun-1",
  });
  const waiting = getSubagentActivityIdentity(
    "subagent_waiting_for_permission",
    { subagent_run_id: "subrun-1" },
  );

  assert.equal(waiting.activityId, started.activityId);
  assert.equal(waiting.terminal, false);
  assert.equal(waiting.statusOverride, "waiting_for_permission");
});

test("subagent tool completion overwrites its matching tool start", () => {
  const started = getSubagentActivityIdentity("subagent_tool_started", {
    subagent_run_id: "subrun-1",
    tool_call_id: "call-7",
    tool: "database_sql_execute",
  });
  const completed = getSubagentActivityIdentity("subagent_tool_completed", {
    subagent_run_id: "subrun-1",
    tool_call_id: "call-7",
    tool: "database_sql_execute",
  });

  assert.equal(started.activityId, completed.activityId);
});

test("parent fallback is a completed handoff rather than a permanent spinner", () => {
  const fallback = getSubagentActivityIdentity("subagent_fallback_to_parent", {
    subagent_run_id: "subrun-1",
  });

  assert.equal(fallback.statusOverride, "completed");
  assert.equal(fallback.activityId, "subagent-subrun-1-fallback");
  assert.equal(fallback.terminal, true);
});
