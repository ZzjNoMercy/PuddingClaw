import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { chmod, copyFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(packageRoot, "../..");
const launcherSource = path.join(repoRoot, "skills", "puddingclaw", "scripts", "run.mjs");

test("Skill launcher loads its sibling .env and preserves unified CLI argv", async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), "puddingclaw-skill-env-"));
  try {
    const skillDir = path.join(temp, "puddingclaw");
    const scriptsDir = path.join(skillDir, "scripts");
    await mkdir(scriptsDir, { recursive: true });
    const launcher = path.join(scriptsDir, "run.mjs");
    await copyFile(launcherSource, launcher);
    await writeFile(path.join(skillDir, ".env"), "PUDDINGCLAW_TOKEN=test-token\n", { mode: 0o600 });

    const fakeCli = path.join(temp, "fake-puddingclaw.mjs");
    await writeFile(fakeCli, `#!/usr/bin/env node
if (process.env.PUDDINGCLAW_TOKEN !== "test-token") process.exit(9);
process.stdout.write(JSON.stringify({ token_loaded: true, argv: process.argv.slice(2) }));
`);
    await chmod(fakeCli, 0o700);

    const child = spawn(process.execPath, [launcher, "agent", "run", "--input-json", "-", "--json"], {
      env: {
        ...process.env,
        PUDDINGCLAW_TOKEN: "",
        PUDDINGCLAW_HEADLESS_TOKEN: "",
        PUDDINGCLAW_CLI_BIN: fakeCli,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    const [result] = await once(child, "close");
    assert.equal(result, 0, stderr);
    assert.deepEqual(JSON.parse(stdout), {
      token_loaded: true,
      argv: ["agent", "run", "--input-json", "-", "--json"],
    });
    assert.doesNotMatch(stdout + stderr, /test-token/);
  } finally {
    await rm(temp, { recursive: true, force: true });
  }
});

test("Skill launcher runs the unified CLI with its sibling .env", async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), "puddingclaw-skill-e2e-"));
  try {
    const skillDir = path.join(temp, "puddingclaw");
    const scriptsDir = path.join(skillDir, "scripts");
    await mkdir(scriptsDir, { recursive: true });
    const launcher = path.join(scriptsDir, "run.mjs");
    await copyFile(launcherSource, launcher);
    await writeFile(path.join(skillDir, ".env"), "PUDDINGCLAW_TOKEN=test-token\n", { mode: 0o600 });

    const cliShim = path.join(temp, "puddingclaw-cli-shim.mjs");
    const mockFetch = path.join(packageRoot, "test", "mock-worker-fetch.js");
    const realCli = path.join(packageRoot, "src", "cli.js");
    await writeFile(cliShim, `#!/usr/bin/env node
await import(${JSON.stringify(pathToFileURL(mockFetch).href)});
await import(${JSON.stringify(pathToFileURL(realCli).href)});
`);
    await chmod(cliShim, 0o700);

    const child = spawn(process.execPath, [launcher, "agent", "run", "验证 Skill", "--json"], {
      env: {
        ...process.env,
        PUDDINGCLAW_TOKEN: "",
        PUDDINGCLAW_HEADLESS_TOKEN: "",
        PUDDINGCLAW_CLI_BIN: cliShim,
        PUDDINGCLAW_HOME: path.join(temp, "home"),
        PUDDINGCLAW_PROJECTS_ROOT: temp,
        PUDDINGCLAW_URL: "http://127.0.0.1:8888",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    const [result] = await once(child, "close");
    assert.equal(result, 0, stderr);
    const payload = JSON.parse(stdout);
    assert.equal(payload.status, "completed");
    assert.equal(payload.final_response, "final ok");
    assert.doesNotMatch(stdout + stderr, /test-token/);
  } finally {
    await rm(temp, { recursive: true, force: true });
  }
});
