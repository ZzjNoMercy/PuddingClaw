import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { chmod, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { selectPorts } from "../src/init.js";
import { loadConfig } from "../src/config.js";
import { buildInitPlan } from "../src/init-schema.js";
import { resolveRuntimeProcess } from "../src/runtime-bundle.js";
import { bootstrapUv, MANAGED_UV_VERSION, managedUvInstaller } from "../src/uv-runtime.js";
import { pythonHeadersAvailable } from "../src/runtime-python.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const cli = path.join(root, "src", "cli.js");

async function runCli(args, { home, env = {} } = {}) {
  const child = spawn(process.execPath, [cli, ...args], {
    env: {
      ...process.env,
      PUDDINGCLAW_HOME: home,
      ...env,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [code] = await once(child, "close");
  return { code, stdout, stderr };
}

async function tempHome() {
  return mkdtemp(path.join(os.tmpdir(), "puddingclaw-deploy-cli-"));
}

async function fakePython(home) {
  const executable = path.join(home, "fake-python-3.12");
  await writeFile(executable, "#!/bin/sh\necho 'Python 3.12.8'\n", { mode: 0o700 });
  await chmod(executable, 0o700);
  return { PUDDINGCLAW_DEPLOY_PYTHON: executable };
}

async function runtimeBundle(parent, { validChecksum = true, longRunning = false } = {}) {
  const bundle = path.join(parent, `bundle-${validChecksum ? "valid" : "invalid"}-${longRunning ? "running" : "short"}`);
  const bin = path.join(bundle, "bin");
  await mkdir(bin, { recursive: true });
  const executable = path.join(bin, "service");
  const content = longRunning
    ? "#!/bin/sh\nwhile true; do sleep 1; done\n"
    : "#!/bin/sh\nexit 0\n";
  await writeFile(executable, content, { mode: 0o700 });
  await chmod(executable, 0o700);
  const digest = crypto.createHash("sha256").update(content).digest("hex");
  await writeFile(path.join(bundle, "manifest.json"), `${JSON.stringify({
    schema_version: 1,
    release_version: validChecksum ? "1.0.0-test" : "1.0.0-bad",
    protocol_version: "1",
    contracts: { puddingclaw_home: 1, dynamic_ports: 1, extensions: 1 },
    files: { "bin/service": validChecksum ? digest : "0".repeat(64) },
    processes: {
      backend: { command: "bin/service", args: [], cwd: "." },
      frontend: { command: "bin/service", args: [], cwd: "." },
    },
  }, null, 2)}\n`);
  return bundle;
}

test("unified CLI exposes the stable Worker and deployment identity", async () => {
  const home = await tempHome();
  try {
    const result = await runCli(["version", "--json"], { home });
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), {
      schema_version: "1",
      cli: "puddingclaw",
      cli_version: "0.1.2",
      agent_id: "puddingclaw",
      protocol_version: "1",
    });
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("managed uv bootstrap uses a pinned official installer per platform", () => {
  assert.match(MANAGED_UV_VERSION, /^\d+\.\d+\.\d+$/);
  assert.equal(
    managedUvInstaller("darwin").url,
    `https://astral.sh/uv/${MANAGED_UV_VERSION}/install.sh`,
  );
  assert.equal(
    managedUvInstaller("win32").url,
    `https://astral.sh/uv/${MANAGED_UV_VERSION}/install.ps1`,
  );
});

test("Python runtime detects whether a selected interpreter provides Python.h", async () => {
  const home = await tempHome();
  try {
    const withHeaders = path.join(home, "python-with-headers");
    const withoutHeaders = path.join(home, "python-without-headers");
    await writeFile(withHeaders, "#!/bin/sh\necho True\n", { mode: 0o700 });
    await writeFile(withoutHeaders, "#!/bin/sh\necho False\n", { mode: 0o700 });
    await chmod(withHeaders, 0o700);
    await chmod(withoutHeaders, 0o700);
    assert.equal(pythonHeadersAvailable(withHeaders), true);
    assert.equal(pythonHeadersAvailable(withoutHeaders), false);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("managed uv bootstrap installs only under the isolated Home", {
  skip: process.platform === "win32",
}, async () => {
  const home = await tempHome();
  try {
    const body = `#!/bin/sh\nset -eu\nmkdir -p "$UV_UNMANAGED_INSTALL"\nprintf '#!/bin/sh\\necho uv ${MANAGED_UV_VERSION}\\n' > "$UV_UNMANAGED_INSTALL/uv"\nchmod 700 "$UV_UNMANAGED_INSTALL/uv"\n# padding-padding-padding-padding-padding-padding-padding-padding\n`;
    const result = await bootstrapUv(home, {
      fetchImpl: async (url) => {
        assert.equal(url, managedUvInstaller(process.platform).url);
        return new Response(body, { status: 200 });
      },
      stderr: { write() {} },
    });
    assert.equal(result.status, "prepared");
    assert.equal(result.selected.version, MANAGED_UV_VERSION);
    assert.ok(result.selected.command.startsWith(`${home}${path.sep}`));
    await assert.rejects(
      readFile(path.join(home, "runtime", "downloads", `uv-${MANAGED_UV_VERSION}-install.sh`)),
      { code: "ENOENT" },
    );
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("init plan covers core settings and excludes disabled extension probes", async () => {
  const harness = buildInitPlan("harness");
  assert.ok(harness.field_count > 30);
  assert.ok(harness.steps.some((step) => step.id === "provider.agent" && step.status === "selected"));
  assert.ok(harness.steps
    .filter((step) => ["knowledge", "analytics", "headless_worker"].includes(step.extension))
    .every((step) => step.status === "disabled"));
  assert.equal(harness.steps.find((step) => step.id === "database.shared").status, "disabled");

  const full = buildInitPlan("full");
  assert.ok(full.field_count > harness.field_count);
  assert.ok(full.steps.every((step) => step.status === "selected"));
  assert.ok(full.execution_order.indexOf("knowledge.storage") < full.execution_order.indexOf("database.shared"));
  assert.ok(full.execution_order.indexOf("database.shared") < full.execution_order.indexOf("knowledge.index"));
  assert.deepEqual(
    full.steps.find((step) => step.id === "knowledge.rag").depends_on,
    ["knowledge.index", "provider.agent"],
  );
});

test("init --plan is read-only and machine-readable", async () => {
  const home = await tempHome();
  try {
    const result = await runCli(["init", "--profile", "knowledge", "--plan", "--json"], { home });
    assert.equal(result.code, 0, result.stderr);
    const plan = JSON.parse(result.stdout);
    assert.equal(plan.status, "plan");
    assert.equal(plan.steps.find((step) => step.id === "knowledge.rag").status, "selected");
    assert.equal(plan.steps.find((step) => step.id === "analytics.vanna").status, "disabled");
    await assert.rejects(readFile(path.join(home, "deploy.json"), "utf8"), { code: "ENOENT" });
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("non-interactive Harness init writes isolated config and disables extensions", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const result = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(result.code, 0, result.stderr);
    const response = JSON.parse(result.stdout);
    assert.equal(response.status, "initialized");
    assert.equal(response.profile, "harness");
    assert.ok(response.settings_plan.selected_steps.includes("harness.context"));
    assert.ok(response.settings_plan.disabled_steps.includes("knowledge.rag"));
    assert.notEqual(response.backend_port, response.frontend_port);
    const config = JSON.parse(await readFile(path.join(home, "deploy.json"), "utf8"));
    assert.equal(config.initialized, true);
    assert.deepEqual(config.extensions, {
      knowledge: { enabled: false },
      analytics: { enabled: false },
      headless_worker: { enabled: false },
    });
    assert.equal(config.server.host, "127.0.0.1");
    assert.equal(config.provider.status, "unconfigured");
    assert.equal(config.infrastructure.catalog.mode, "sqlite");
    assert.equal(config.infrastructure.milvus.enabled, false);
    const tokenFile = path.join(home, "secrets", "headless-token");
    assert.match(await readFile(tokenFile, "utf8"), /^pck_[A-Za-z0-9_-]{32,}\n$/);
    if (process.platform !== "win32") assert.equal((await stat(tokenFile)).mode & 0o777, 0o600);
    assert.equal(JSON.stringify(config).includes("pck_"), false);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("0.1.1 deploy config is normalized for the 0.1.2 runtime contract", async () => {
  const home = await tempHome();
  try {
    const file = path.join(home, "deploy.json");
    await writeFile(file, `${JSON.stringify({
      schema_version: 1,
      initialized: true,
      profile: "harness",
      server: {
        host: "127.0.0.1",
        backend_port: 8888,
        frontend_port: 3000,
        port_conflict: "ask",
        auto_open: false,
      },
      extensions: {
        knowledge: { enabled: false },
        analytics: { enabled: false },
        headless_worker: { enabled: false },
      },
      harness: { sandbox_mode: "auto" },
    })}\n`);
    const config = await loadConfig(file);
    assert.equal(config.provider.status, "unconfigured");
    assert.equal(config.infrastructure.catalog.mode, "sqlite");
    assert.equal(config.infrastructure.embedding.status, "disabled");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("non-interactive init accepts an explicit Python executable", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const python = env.PUDDINGCLAW_DEPLOY_PYTHON;
    const result = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto",
      "--python", python, "--json",
    ], { home, env: { PUDDINGCLAW_DEPLOY_PYTHON: "" } });
    assert.equal(result.code, 0, result.stderr);
    const config = JSON.parse(await readFile(path.join(home, "deploy.json"), "utf8"));
    assert.equal(config.runtime.python.command, python);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("init resolves a PATH Python command to an absolute executable", async () => {
  const home = await tempHome();
  const bin = path.join(home, "bin");
  try {
    await mkdir(bin, { recursive: true });
    const python = path.join(bin, "python3.12");
    await writeFile(python, "#!/bin/sh\necho 'Python 3.12.8'\n", { mode: 0o700 });
    await chmod(python, 0o700);
    const result = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], {
      home,
      env: {
        PATH: `${bin}${path.delimiter}${process.env.PATH}`,
        PUDDINGCLAW_DEPLOY_PYTHON: "",
      },
    });
    assert.equal(result.code, 0, result.stderr);
    const config = JSON.parse(await readFile(path.join(home, "deploy.json"), "utf8"));
    assert.equal(config.runtime.python.command, python);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("extension enable updates only deploy CLI config", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const initialized = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(initialized.code, 0, initialized.stderr);
    const enabled = await runCli(["extension", "enable", "knowledge", "--json"], { home });
    assert.equal(enabled.code, 0, enabled.stderr);
    assert.deepEqual(JSON.parse(enabled.stdout), {
      status: "updated",
      profile: "custom",
      extension: "knowledge",
      enabled: true,
    });
    const listed = await runCli(["extension", "list", "--json"], { home });
    assert.equal(JSON.parse(listed.stdout).extensions[0].enabled, true);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("config set rejects secrets and validates known fields", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const initialized = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(initialized.code, 0, initialized.stderr);
    const updated = await runCli(["config", "set", "server.auto_open", "true", "--json"], { home });
    assert.equal(updated.code, 0, updated.stderr);
    assert.equal(JSON.parse(updated.stdout).value, true);
    const rejected = await runCli(["config", "set", "provider.api_key", "secret", "--json"], { home });
    assert.equal(rejected.code, 2);
    assert.equal(JSON.parse(rejected.stdout).error_code, "secret_rejected");
    const config = JSON.parse(await readFile(path.join(home, "deploy.json"), "utf8"));
    const samePort = await runCli([
      "config", "set", "server.frontend_port", String(config.server.backend_port), "--json",
    ], { home });
    assert.equal(samePort.code, 2);
    assert.equal(JSON.parse(samePort.stdout).error_code, "configuration_error");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("occupied port fails closed and auto mode selects another port", async () => {
  const occupied = async (port) => ({
    probe: "runtime.port",
    status: port === 8888 ? "occupied" : "available",
    required: true,
    port,
    owner: port === 8888 ? { pid: 42, command: "other-service" } : null,
  });
  await assert.rejects(
    selectPorts({ backendPort: 8888, frontendPort: 3000, automatic: false, probe: occupied }),
    (error) => error.code === "port_occupied" && error.details.owner.pid === 42,
  );
  const selected = await selectPorts({
    backendPort: 8888,
    frontendPort: 3000,
    automatic: true,
    probe: occupied,
    findFree: async (start) => start,
  });
  assert.equal(selected.backendPort, 8889);
  assert.equal(selected.frontendPort, 3000);
});

test("doctor and status expose initialized state without probing disabled extensions", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const initialized = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(initialized.code, 0, initialized.stderr);
    const doctor = await runCli(["doctor", "--json"], { home, env });
    assert.equal(doctor.code, 2, doctor.stderr);
    const diagnostic = JSON.parse(doctor.stdout);
    assert.equal(diagnostic.status, "needs_action");
    assert.equal(diagnostic.configured, true);
    assert.equal(diagnostic.reachable, false);
    assert.equal(diagnostic.deployment.initialized, true);
    assert.equal(diagnostic.deployment.status, "ok");
    assert.deepEqual(diagnostic.deployment.extensions.map((item) => item.status), ["disabled", "disabled", "disabled"]);
    const status = await runCli(["status", "--json"], { home });
    assert.equal(status.code, 0, status.stderr);
    assert.equal(JSON.parse(status.stdout).instance.status, "stopped");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("Agent lifecycle capabilities use the single agent command namespace", async () => {
  const home = await tempHome();
  try {
    const result = await runCli(["agent", "capabilities", "--json"], { home });
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout).operations, {
      run: true,
      continue: true,
      respond: true,
      cancel: true,
    });
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("CLI help is a boolean flag and legacy top-level run is not kept in parallel", async () => {
  const home = await tempHome();
  try {
    const help = await runCli(["--help"], { home });
    assert.equal(help.code, 0, help.stderr);
    assert.match(help.stdout, /puddingclaw agent run/);
    const legacy = await runCli(["run", "hello", "--json"], { home });
    assert.equal(legacy.code, 2);
    assert.equal(JSON.parse(legacy.stdout).error_code, "argument_error");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime install verifies checksums and activates an immutable release", async () => {
  const home = await tempHome();
  try {
    const bundle = await runtimeBundle(home);
    const installed = await runCli(["runtime", "install", bundle, "--json"], { home });
    assert.equal(installed.code, 0, installed.stderr);
    assert.equal(JSON.parse(installed.stdout).release_version, "1.0.0-test");
    const inspected = await runCli(["runtime", "inspect", "--json"], { home });
    assert.equal(inspected.code, 0, inspected.stderr);
    const response = JSON.parse(inspected.stdout);
    assert.equal(response.status, "installed");
    assert.equal(response.manifest.release_version, "1.0.0-test");
    assert.equal(response.manifest.file_count, 1);
    assert.equal(response.manifest.files, undefined);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime install rejects a checksum mismatch before activation", async () => {
  const home = await tempHome();
  try {
    const bundle = await runtimeBundle(home, { validChecksum: false });
    const result = await runCli(["runtime", "install", bundle, "--json"], { home });
    assert.equal(result.code, 2);
    assert.equal(JSON.parse(result.stdout).error_code, "runtime_checksum_mismatch");
    const inspected = await runCli(["runtime", "inspect", "--json"], { home });
    assert.equal(JSON.parse(inspected.stdout).status, "not_installed");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime install rejects files omitted from the checksum manifest", async () => {
  const home = await tempHome();
  try {
    const bundle = await runtimeBundle(home);
    await writeFile(path.join(bundle, "unlisted.txt"), "not covered\n");
    const result = await runCli(["runtime", "install", bundle, "--json"], { home });
    assert.equal(result.code, 2);
    assert.equal(JSON.parse(result.stdout).error_code, "runtime_unlisted_file");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime install rejects a bundle that cannot enforce extension gating", async () => {
  const home = await tempHome();
  try {
    const bundle = await runtimeBundle(home);
    const manifestPath = path.join(bundle, "manifest.json");
    const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
    delete manifest.contracts.extensions;
    await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
    const result = await runCli(["runtime", "install", bundle, "--json"], { home });
    assert.equal(result.code, 2);
    assert.equal(JSON.parse(result.stdout).error_code, "incompatible_runtime_contract");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime process kinds use managed Python and the npm Node executable", () => {
  const active = {
    root: "/runtime",
    manifest: {
      processes: {
        backend: {
          kind: "python_module",
          module: "uvicorn",
          cwd: "backend",
          args: ["app:app", "--port", "${BACKEND_PORT}"],
        },
        frontend: {
          kind: "node_script",
          script: "web/server.js",
          cwd: "web",
          args: [],
        },
      },
    },
  };
  const variables = { PYTHON_COMMAND: "/runtime/venv/bin/python", BACKEND_PORT: 9000 };
  const backend = resolveRuntimeProcess(active, "backend", variables);
  assert.equal(backend.command, "/runtime/venv/bin/python");
  assert.deepEqual(backend.args, ["-m", "uvicorn", "app:app", "--port", "9000"]);
  const frontend = resolveRuntimeProcess(active, "frontend", variables);
  assert.equal(frontend.command, process.execPath);
  assert.deepEqual(frontend.args, [path.resolve("/runtime/web/server.js")]);
});

test("start fails honestly when no runtime bundle is installed", async () => {
  const home = await tempHome();
  try {
    const result = await runCli(["start", "--json"], { home });
    assert.equal(result.code, 1);
    assert.equal(JSON.parse(result.stdout).error_code, "runtime_not_installed");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("start, restart, and stop manage only authenticated processes under the isolated deploy home", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const initialized = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(initialized.code, 0, initialized.stderr);
    const bundle = await runtimeBundle(home, { longRunning: true });
    const installed = await runCli(["runtime", "install", bundle, "--json"], { home, env });
    assert.equal(installed.code, 0, installed.stderr);
    const started = await runCli(["start", "--port", "auto", "--json"], { home, env });
    assert.equal(started.code, 0, `${started.stderr}\n${started.stdout}`);
    const runtime = JSON.parse(started.stdout).runtime;
    assert.equal(runtime.home, home);
    assert.ok(runtime.backend_pid > 0);
    assert.ok(runtime.frontend_pid > 0);
    assert.equal(runtime.processes.backend.control_token, undefined);
    const runningStatus = await runCli(["status", "--json"], { home, env });
    assert.equal(JSON.parse(runningStatus.stdout).instance.ownership_verified, true);
    const restarted = await runCli(["restart", "--port", "auto", "--json"], { home, env });
    assert.equal(restarted.code, 0, `${restarted.stderr}\n${restarted.stdout}`);
    const restartedRuntime = JSON.parse(restarted.stdout).runtime;
    assert.notEqual(restartedRuntime.instance_id, runtime.instance_id);
    const stopped = await runCli(["stop", "--json"], { home, env });
    assert.equal(stopped.code, 0, `${stopped.stderr}\n${stopped.stdout}`);
    assert.equal(JSON.parse(stopped.stdout).status, "stopped");
  } finally {
    const statePath = path.join(home, "runtime.json");
    try {
      const state = JSON.parse(await readFile(statePath, "utf8"));
      for (const pid of [state.backend_pid, state.frontend_pid]) {
        try { process.kill(-pid, "SIGKILL"); } catch {}
      }
    } catch {}
    await rm(home, { recursive: true, force: true });
  }
});

test("stop refuses an unverified PID and leaves the unknown process alive", async () => {
  const home = await tempHome();
  const unknown = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    stdio: "ignore",
  });
  await once(unknown, "spawn");
  try {
    await writeFile(path.join(home, "runtime.json"), `${JSON.stringify({
      schema_version: 1,
      instance_id: "forged-instance",
      home,
      backend_pid: unknown.pid,
      frontend_pid: unknown.pid,
      processes: {
        backend: { pid: unknown.pid, command: process.execPath },
      },
    })}\n`, { mode: 0o600 });
    const status = await runCli(["status", "--json"], { home });
    assert.equal(JSON.parse(status.stdout).instance.status, "unverified");
    const stopped = await runCli(["stop", "--json"], { home });
    assert.equal(stopped.code, 2);
    assert.equal(JSON.parse(stopped.stdout).error_code, "runtime_ownership_mismatch");
    assert.doesNotThrow(() => process.kill(unknown.pid, 0));
  } finally {
    unknown.kill("SIGKILL");
    await rm(home, { recursive: true, force: true });
  }
});
