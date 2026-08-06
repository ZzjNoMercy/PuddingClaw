import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const cli = path.join(root, "dist", "cli.js");

test("doctor renders a human-readable diagnostic by default", async () => {
  const child = spawn(process.execPath, ["--import", path.join(root, "test", "mock-fetch.js"), cli, "doctor"], {
    env: { ...process.env, PUDDINGCLAW_URL: "http://127.0.0.1:8888", PUDDINGCLAW_TOKEN: "test-token", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 0, stderr);
  assert.match(stdout, /PuddingClaw Doctor v0\.2\.0 · (macos-aarch64|macos-x86_64|windows-|linux-)/);
  assert.match(stdout, /Worker API/);
  assert.match(stdout, /authenticated · reachable/);
  assert.match(stdout, /worker key.*wak_test/);
  assert.match(stdout, /Environment/);
  assert.match(stdout, /node.*26\.5\.0/);
});

test("doctor keeps local environment healthy when the token is missing", async () => {
  const child = spawn(process.execPath, [cli, "doctor"], {
    env: { ...process.env, PUDDINGCLAW_TOKEN: "", PUDDINGCLAW_HEADLESS_TOKEN: "", PUDDINGCLAW_PROJECTS_ROOT: "/tmp/puddingclaw-cli-test" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  const [result] = await once(child, "close");
  assert.equal(result, 2);
  assert.match(stdout, /PUDDINGCLAW_TOKEN is not configured/);
  assert.match(stdout, /✓ puddingclaw\s+installed/);
  assert.match(stdout, /node.*\d+\.\d+\.\d+/);
});
