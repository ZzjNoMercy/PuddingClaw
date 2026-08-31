import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const cli = path.join(root, "src", "cli.js");
const mockFetch = path.join(root, "test", "mock-worker-fetch.js");

async function runAgent(args, { input, env = {}, prepareHome } = {}) {
  const home = await mkdtemp(path.join(os.tmpdir(), "puddingclaw-agent-cli-"));
  if (prepareHome) await prepareHome(home);
  const cliArgs = args[0] === "doctor" ? args : ["agent", ...args];
  const child = spawn(process.execPath, ["--import", mockFetch, cli, ...cliArgs], {
    env: {
      ...process.env,
      PUDDINGCLAW_HOME: home,
      PUDDINGCLAW_URL: "http://127.0.0.1:8888",
      ...env,
    },
    stdio: [input === undefined ? "ignore" : "pipe", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  if (input !== undefined) child.stdin.end(JSON.stringify(input));
  const [code] = await once(child, "close");
  await rm(home, { recursive: true, force: true });
  return { code, stdout, stderr };
}

test("merged run preserves JSON, Session, and human output", async () => {
  const json = await runAgent(["run", "hello", "--session", "session-1", "--json"]);
  assert.equal(json.code, 0, json.stderr);
  assert.equal(JSON.parse(json.stdout).session_id, "session-1");
  const human = await runAgent(["run", "hello"]);
  assert.equal(human.code, 0, human.stderr);
  assert.equal(human.stdout, "final ok\n");
});

test("unified doctor preserves Worker probe fields and adds deployment diagnostics", async () => {
  const result = await runAgent(["doctor", "--json"]);
  assert.equal(result.code, 0, result.stderr);
  const diagnostic = JSON.parse(result.stdout);
  assert.equal(diagnostic.status, "ok");
  assert.equal(diagnostic.configured, true);
  assert.equal(diagnostic.reachable, true);
  assert.equal(diagnostic.agent_id, "puddingclaw");
  assert.equal(diagnostic.deployment.initialized, false);
  assert.equal(diagnostic.deployment.status, "needs_action");
});

test("merged run accepts stdin JSON and preserves an explicit workspace", async () => {
  const workspace = await mkdtemp(path.join(os.tmpdir(), "puddingclaw-agent-workspace-"));
  try {
    const result = await runAgent(["run", "--input-json", "-", "--json"], {
      input: { message: "你好", workspace_path: workspace },
    });
    assert.equal(result.code, 0, result.stderr);
    assert.equal(JSON.parse(result.stdout).workspace_path, workspace);
  } finally {
    await rm(workspace, { recursive: true, force: true });
  }
});

test("merged run discovers the managed runtime's dynamic Backend URL", async () => {
  const result = await runAgent(["run", "endpoint", "--json"], {
    env: { PUDDINGCLAW_URL: "", PUDDINGCLAW_BACKEND_URL: "" },
    prepareHome: async (home) => {
      await writeFile(path.join(home, "runtime.json"), JSON.stringify({
        backend_url: "http://127.0.0.1:45678",
      }));
    },
  });
  assert.equal(result.code, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).request_url, "http://127.0.0.1:45678/api/headless/runs?stream=true");
});

test("merged run uses the local Backend without a CLI credential", async () => {
  const result = await runAgent(["run", "local auth", "--json"]);
  assert.equal(result.code, 0, result.stderr);
  const payload = JSON.parse(result.stdout);
  assert.equal(payload.final_response, "final ok");
  assert.equal(payload.authorization_header, null);
});

test("merged run returns external approval without deciding in JSON mode", async () => {
  const result = await runAgent(["run", "needs approval", "--json"]);
  assert.equal(result.code, 1, result.stderr);
  const response = JSON.parse(result.stdout);
  assert.equal(response.status, "needs_input");
  assert.equal(response.continuation_token, "continuation-token-long-enough");
});

test("merged run streams JSONL and emits exactly one result event", async () => {
  const result = await runAgent(["run", "stream", "--jsonl"]);
  assert.equal(result.code, 0, result.stderr);
  const events = result.stdout.trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(events.map((event) => event.event), ["run_started", "progress", "result"]);
  assert.equal(events.at(-1).data.final_response, "stream done");
});

test("merged run remains compatible with a legacy Backend single JSON boundary", async () => {
  const result = await runAgent(["run", "legacy", "--json"], {
    env: { MOCK_LEGACY_STREAM: "1" },
  });
  assert.equal(result.code, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).final_response, "legacy done");
});

test("merged respond and cancel preserve lifecycle protocol", async () => {
  const responded = await runAgent(["respond", "run-respond", "--input-json", "-", "--json"], {
    input: {
      continuation_token: "continuation-token-long-enough",
      decisions: [{ request_id: "permission-1", decision: "approve", scope: "once" }],
    },
  });
  assert.equal(responded.code, 0, responded.stderr);
  assert.equal(JSON.parse(responded.stdout).final_response, "responded");
  const cancelled = await runAgent(["cancel", "run-cancel", "--json"]);
  assert.equal(cancelled.code, 0, cancelled.stderr);
  assert.equal(JSON.parse(cancelled.stdout).outcome, "cancelled");
});

test("merged respond accepts structured user-input answers", async () => {
  const responded = await runAgent(["respond", "run-respond", "--input-json", "-", "--json"], {
    input: {
      continuation_token: "continuation-token-long-enough",
      decisions: [{
        request_id: "user-input-1",
        action: "submit",
        answers: [{ question_id: "choice", option_ids: ["B"], text: "" }],
      }],
    },
  });
  assert.equal(responded.code, 0, responded.stderr);
  assert.equal(JSON.parse(responded.stdout).final_response, "responded");
});

test("merged respond forwards resumed Run progress as JSONL", async () => {
  const responded = await runAgent(["respond", "run-respond", "--input-json", "-", "--jsonl"], {
    input: {
      continuation_token: "continuation-token-long-enough",
      decisions: [{ request_id: "permission-1", decision: "approve", scope: "once" }],
    },
  });
  assert.equal(responded.code, 0, responded.stderr);
  const events = responded.stdout.trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(events.map((event) => event.event), [
    "permission_resolved",
    "tool_start",
    "tool_end",
    "result",
  ]);
  assert.equal(events.at(-1).data.final_response, "responded");
});

test("merged run exports only Backend-declared workspace artifacts", async () => {
  const projectsRoot = await mkdtemp(path.join(os.tmpdir(), "puddingclaw-agent-export-"));
  const workspace = path.join(projectsRoot, "puddingclaw");
  const exportDir = path.join(projectsRoot, "handoff");
  try {
    await mkdir(workspace, { recursive: true });
    await writeFile(path.join(workspace, "report.txt"), "report\n");
    const result = await runAgent(["run", "export", "--export", exportDir, "--json"], {
      env: { PUDDINGCLAW_PROJECTS_ROOT: projectsRoot },
    });
    assert.equal(result.code, 0, result.stderr);
    assert.equal(await readFile(path.join(exportDir, "report.txt"), "utf8"), "report\n");
    assert.equal(JSON.parse(result.stdout).export.exported[0].exported_path, "report.txt");
  } finally {
    await rm(projectsRoot, { recursive: true, force: true });
  }
});

test("merged models and expired Session outcomes remain machine-readable", async () => {
  const models = await runAgent(["models", "list", "--json"]);
  assert.equal(models.code, 0, models.stderr);
  assert.equal(JSON.parse(models.stdout).models[0].id, "auto-analysis");
  const expired = await runAgent(["run", "continue", "--session", "expired-session", "--json"]);
  assert.equal(expired.code, 1, expired.stderr);
  assert.deepEqual(JSON.parse(expired.stdout), {
    schema_version: "1",
    status: "error",
    outcome: "session_expired",
    error_code: "session_expired",
    http_status: 410,
    error: "Headless Session expired",
  });
});

test("merged run rejects caller-selected analytics models", async () => {
  const option = await runAgent(["run", "hello", "--model", "sales", "--json"]);
  assert.equal(option.code, 2);
  assert.equal(JSON.parse(option.stdout).error_code, "argument_error");
  const input = await runAgent(["run", "--input-json", "-", "--json"], {
    input: { message: "hello", analytics_model_id: "sales" },
  });
  assert.equal(input.code, 2);
  assert.match(JSON.parse(input.stdout).error, /model input is not supported/);
});
