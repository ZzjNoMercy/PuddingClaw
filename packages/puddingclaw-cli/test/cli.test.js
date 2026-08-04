import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const cli = path.join(root, "dist", "cli.js");

test("run maps model to analytics_model_id and keeps stdout JSON-only", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "分析销售", "--model", "sales", "--json"], {
    env: { ...process.env, PUDDING_PLATFORM_ID: "puddingteams", PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 0, stderr);
  assert.deepEqual(JSON.parse(stdout), { schema_version: "1", status: "completed", outcome: "completed", reply: "ok", analytics_model_id: "sales" });
});

test("stdin JSON preserves non-ASCII message and maps model", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "run", "--input-json", "-", "--json"], {
    env: { ...process.env, PUDDING_PLATFORM_ID: "puddingteams", PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  child.stdin.end(JSON.stringify({ message: "换行\n含引号\"和中文", model: "sales" }));
  const [result] = await once(child, "close");
  assert.equal(result, 0, stderr);
  assert.equal(JSON.parse(stdout).analytics_model_id, "sales");
});
