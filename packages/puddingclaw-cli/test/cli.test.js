import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const cli = path.join(root, "dist", "cli.js");

test("run sends only the question and keeps stdout JSON-only", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "分析销售", "--json"], {
    env: { ...process.env, PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 0, stderr);
  assert.deepEqual(JSON.parse(stdout), { schema_version: "1", status: "completed", outcome: "completed", reply: "ok", final_response: "final ok", analytics_model_id: "auto-analysis", analytics_model_match: { status: "matched", selected_id: "auto-analysis", strategy: "semantic" } });
});

test("stdin JSON preserves non-ASCII message while backend selects the model", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "--input-json", "-", "--json"], {
    env: { ...process.env, PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.stdin.end(JSON.stringify({ message: "换行\n含引号\"和中文" }));
  const [result] = await once(child, "close");
  assert.equal(result, 0, stderr);
  assert.equal(JSON.parse(stdout).analytics_model_id, "auto-analysis");
});

test("run accepts an explicit session for a continuous task", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "继续分析", "--session", "worker-session-existing", "--json"], {
    env: { ...process.env, PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 0, stderr);
  assert.equal(JSON.parse(stdout).session_id, "worker-session-existing");
});

test("JSON mode returns external approval without making a decision", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "需要授权", "--json"], {
    env: { ...process.env, PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 1, stderr);
  const response = JSON.parse(stdout);
  assert.equal(response.status, "needs_input");
  assert.equal(response.outcome, "waiting_hitl");
  assert.equal(response.needs_input.request_id, "perm-req-test");
  assert.equal(response.continuation_token, "continuation-token-long-enough");
});

test("non-TTY human mode preserves structured approval instead of printing a blank reply", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "需要授权"], {
    env: { ...process.env, PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 1, stderr);
  assert.equal(JSON.parse(stdout).status, "needs_input");
});

test("expired session is a structured recoverable outcome", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "继续分析", "--session", "worker-session-expired", "--json"], {
    env: { ...process.env, PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 1, stderr);
  assert.deepEqual(JSON.parse(stdout), {
    schema_version: "1",
    status: "error",
    outcome: "session_expired",
    error_code: "session_expired",
    http_status: 410,
    error: "Headless Session expired after its configured inactivity TTL",
  });
});

test("human-readable run output prefers final_response over aggregate reply", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "分析销售"], {
    env: { ...process.env, PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 0, stderr);
  assert.equal(stdout, "final ok\n");
});

test("run rejects the removed --model option", async () => {
  const child = spawn(process.execPath, [cli, "run", "分析销售", "--model", "sales", "--json"], {
    env: { ...process.env, PUDDINGCLAW_TOKEN: "test-token" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 2);
  assert.equal(JSON.parse(stdout).error_code, "argument_error");
});

test("stdin JSON rejects model selection fields", async () => {
  const child = spawn(process.execPath, [cli, "run", "--input-json", "-", "--json"], {
    env: { ...process.env, PUDDINGCLAW_TOKEN: "test-token" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stdin.end(JSON.stringify({ message: "分析销售", analytics_model_id: "sales" }));
  const [result] = await once(child, "close");
  assert.equal(result, 2);
  assert.match(JSON.parse(stdout).error, /model input is not supported/);
});
