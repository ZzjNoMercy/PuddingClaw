import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import net from "node:net";
import { createRequire } from "node:module";
import { chmod, mkdir, mkdtemp, readFile, rm, stat, utimes, writeFile } from "node:fs/promises";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { selectPorts } from "../src/init.js";
import {
  discoverExtensionInfrastructure,
  discoverInitialMultimodalProvider,
  multimodalProviderPreset,
  providerPreset,
} from "../src/init-discovery.js";
import { defaultConfig, loadConfig } from "../src/config.js";
import { buildInitPlan } from "../src/init-schema.js";
import { resolveRuntimeProcess } from "../src/runtime-bundle.js";
import { bootstrapUv, MANAGED_UV_VERSION, managedUvInstaller } from "../src/uv-runtime.js";
import { pythonHeadersAvailable } from "../src/runtime-python.js";
import { probePort } from "../src/probes.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const cli = path.join(root, "src", "cli.js");
const legacyControlProcess = path.join(root, "test", "legacy-control-process.js");
const packageVersion = createRequire(import.meta.url)("../package.json").version;

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
    assert.equal(result.code, 0, `${result.stderr}\n${result.stdout}`);
    assert.deepEqual(JSON.parse(result.stdout), {
      schema_version: "1",
      cli: "puddingclaw",
      cli_version: packageVersion,
      agent_id: "puddingclaw",
      protocol_version: "1",
    });
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("DeepSeek init defaults to the product Flash model", () => {
  assert.equal(providerPreset("1").model, "deepseek-v4-flash");
});

test("image analyzer init defaults to the DashScope multimodal model", () => {
  assert.deepEqual(multimodalProviderPreset("1"), {
    id: "dashscope",
    name: "阿里云百炼",
    protocol: "openai_compatible",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen3.7-plus",
  });
});

test("image analyzer init keeps a dedicated Provider credential", async () => {
  const discovery = await discoverInitialMultimodalProvider({
    flags: {
      multimodal_provider: "custom",
      multimodal_provider_id: "vision-provider",
      multimodal_provider_name: "Vision Provider",
      multimodal_base_url: "https://vision.example.com/v1",
      multimodal_model: "vision-model",
      multimodal_api_key: "image-secret",
    },
    nonInteractive: true,
    primaryDiscovery: {
      provider: {
        status: "configured",
        id: "agent-provider",
        base_url: "https://agent.example.com/v1",
        model: "agent-model",
      },
      apiKey: "agent-secret",
    },
    probeProvider: async () => ({ probe: "provider.endpoint", status: "available", required: true }),
  });
  assert.equal(discovery.provider.status, "configured");
  assert.equal(discovery.provider.model, "vision-model");
  assert.equal(discovery.provider.reuse_primary_credential, false);
  assert.equal(discovery.apiKey, "image-secret");
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
  assert.match(managedUvInstaller("darwin").sha256, /^[a-f0-9]{64}$/);
  assert.match(managedUvInstaller("win32").sha256, /^[a-f0-9]{64}$/);
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
      installerSha256: crypto.createHash("sha256").update(body).digest("hex"),
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

test("managed uv bootstrap rejects an installer with the wrong checksum", async () => {
  const home = await tempHome();
  try {
    const body = "x".repeat(200);
    await assert.rejects(
      bootstrapUv(home, {
        fetchImpl: async () => new Response(body, { status: 200 }),
        installerSha256: "0".repeat(64),
        stderr: { write() {} },
      }),
      (error) => error.code === "uv_integrity_failed",
    );
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("init plan covers core settings and excludes disabled extension probes", async () => {
  const harness = buildInitPlan("harness");
  assert.ok(harness.steps.some((step) => step.id === "provider.agent" && step.status === "selected"));
  assert.ok(harness.steps.some((step) => step.id === "provider.multimodal" && step.status === "selected"));
  assert.deepEqual(
    harness.steps.find((step) => step.id === "harness.subagents").depends_on,
    ["provider.agent", "provider.multimodal"],
  );
  assert.ok(harness.steps
    .filter((step) => ["knowledge", "analytics"].includes(step.extension))
    .every((step) => step.status === "disabled"));
  assert.equal(harness.steps.find((step) => step.id === "headless.worker").status, "selected");
  assert.equal(harness.steps.find((step) => step.id === "database.shared").status, "selected");
  assert.deepEqual(harness.branches.database, [
    "sqlite_local_default", "postgresql_if_explicit", "sqlite_fallback_on_unreachable",
  ]);

  const full = buildInitPlan("full");
  assert.ok(full.execution_order.length > harness.execution_order.length);
  assert.ok(full.steps.every((step) => step.status === "selected"));
  assert.ok(full.execution_order.indexOf("database.shared") < full.execution_order.indexOf("knowledge.storage"));
  assert.ok(full.execution_order.indexOf("knowledge.storage") < full.execution_order.indexOf("knowledge.index"));
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

test("non-interactive Harness init enables its Worker and disables business extensions", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const result = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(result.code, 0, `${result.stderr}\n${result.stdout}`);
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
      headless_worker: { enabled: true },
    });
    assert.equal(config.server.host, "127.0.0.1");
    assert.equal(config.provider.status, "unconfigured");
    assert.equal(config.multimodal_provider.status, "unconfigured");
    assert.equal(config.infrastructure.catalog.mode, "sqlite");
    assert.equal(config.infrastructure.catalog.provider, "sqlite");
    assert.equal(config.infrastructure.catalog.source, "local_file");
    assert.equal(config.infrastructure.catalog.probe_status, "skipped");
    assert.equal(config.infrastructure.milvus.enabled, false);
    const tokenFile = path.join(home, "secrets", "headless-token");
    assert.match(await readFile(tokenFile, "utf8"), /^pck_[A-Za-z0-9_-]{32,}\n$/);
    if (process.platform !== "win32") assert.equal((await stat(tokenFile)).mode & 0o777, 0o600);
    assert.equal(JSON.stringify(config).includes("pck_"), false);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("init infrastructure defaults directly to SQLite without database discovery", async () => {
  const discovered = await discoverExtensionInfrastructure({
    profile: "harness",
    flags: {},
    nonInteractive: false,
    home: "/unused",
  });

  assert.deepEqual(discovered.catalog, {
    mode: "sqlite",
    provider: "sqlite",
    source: "local_file",
    host: "",
    port: 0,
    database: "",
    probe_status: "skipped",
  });
  assert.equal(discovered.databaseUrl, "");
  assert.deepEqual(discovered.probes, []);
});

test("forced init preserves an existing PostgreSQL catalog without rediscovery", async () => {
  const existingCatalog = {
    mode: "postgresql",
    provider: "postgresql",
    source: "external",
    host: "db.example.com",
    port: 5432,
    database: "puddingclaw",
    username: "puddingclaw",
    probe_status: "available",
  };
  const databaseUrl = "postgresql+asyncpg://puddingclaw:secret@db.example.com/puddingclaw";
  const discovered = await discoverExtensionInfrastructure({
    profile: "harness",
    flags: {},
    nonInteractive: false,
    home: "/unused",
    existingCatalog,
    existingDatabaseUrl: databaseUrl,
  });

  assert.deepEqual(discovered.catalog, existingCatalog);
  assert.equal(discovered.databaseUrl, databaseUrl);
  assert.deepEqual(discovered.probes, []);
});

test("database configure updates only the database after init", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const initialized = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(initialized.code, 0, `${initialized.stderr}\n${initialized.stdout}`);
    const before = JSON.parse(await readFile(path.join(home, "deploy.json"), "utf8"));

    const configured = await runCli([
      "database", "configure", "--database-mode", "sqlite", "--non-interactive", "--json",
    ], { home, env });
    assert.equal(configured.code, 0, configured.stderr);
    const response = JSON.parse(configured.stdout);
    assert.equal(response.status, "updated");
    assert.equal(response.database.mode, "sqlite");

    const after = JSON.parse(await readFile(path.join(home, "deploy.json"), "utf8"));
    assert.equal(after.provider.status, before.provider.status);
    assert.deepEqual(after.extensions, before.extensions);
    assert.deepEqual(after.server, before.server);
    assert.equal(after.infrastructure.catalog.mode, "sqlite");

    const shown = await runCli(["database", "show", "--json"], { home, env });
    assert.equal(shown.code, 0, shown.stderr);
    assert.equal(JSON.parse(shown.stdout).database.mode, "sqlite");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("database configure blocks a silent provider switch when the source catalog has data", async () => {
  const home = await tempHome();
  const listener = net.createServer();
  await new Promise((resolve) => listener.listen(0, "127.0.0.1", resolve));
  const pgPort = listener.address().port;
  try {
    const env = await fakePython(home);
    const initialized = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(initialized.code, 0, `${initialized.stderr}\n${initialized.stdout}`);

    // Simulate an existing non-empty SQLite catalog at the real runtime path
    // ($PUDDINGCLAW_HOME/db/catalog.sqlite3, see backend/runtime_identity/paths.py).
    await mkdir(path.join(home, "db"), { recursive: true });
    await writeFile(path.join(home, "db", "catalog.sqlite3"), "not-empty");

    const databaseUrl = `postgresql+asyncpg://u:p@127.0.0.1:${pgPort}/puddingclaw`;
    const blocked = await runCli([
      "database", "configure", "--database-mode", "postgresql",
      "--database-url", databaseUrl, "--non-interactive", "--json",
    ], { home, env });
    assert.equal(blocked.code, 1, blocked.stderr);
    assert.equal(JSON.parse(blocked.stdout).error_code, "database_switch_requires_confirmation");

    const confirmed = await runCli([
      "database", "configure", "--database-mode", "postgresql",
      "--database-url", databaseUrl, "--non-interactive", "--confirm-empty-switch", "--json",
    ], { home, env });
    // The guard passes; the run then fails later because no runtime Python is prepared.
    assert.equal(confirmed.code, 1);
    assert.equal(JSON.parse(confirmed.stdout).error_code, "runtime_python_not_prepared");

    // Neither attempt modified the stored catalog.
    const config = JSON.parse(await readFile(path.join(home, "deploy.json"), "utf8"));
    assert.equal(config.infrastructure.catalog.provider, "sqlite");
    assert.equal(config.infrastructure.catalog.mode, "sqlite");
  } finally {
    listener.close();
    await rm(home, { recursive: true, force: true });
  }
});

test("0.1.1 deploy config is normalized for the current runtime contract", async () => {
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
    assert.equal(config.extensions.headless_worker.enabled, true);
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
    assert.equal(result.code, 0, `${result.stderr}\n${result.stdout}`);
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
    assert.equal(result.code, 0, `${result.stderr}\n${result.stdout}`);
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
      profile: "knowledge",
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

test("port probing detects a real listener without relying on lsof", async () => {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0, exclusive: true }, resolve);
  });
  try {
    const port = server.address().port;
    const result = await probePort(port);
    assert.equal(result.status, "occupied");
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
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
    assert.equal(diagnostic.authenticated, false);
    assert.equal(typeof diagnostic.reachable, "boolean");
    assert.equal(diagnostic.deployment.initialized, true);
    assert.equal(diagnostic.deployment.status, "ok");
    assert.deepEqual(diagnostic.deployment.extensions.map((item) => item.status), ["disabled", "disabled", "enabled"]);
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
    const unknown = await runCli(["version", "--typo", "value", "--json"], { home });
    assert.equal(unknown.code, 2);
    assert.equal(JSON.parse(unknown.stdout).error_code, "argument_error");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime install verifies checksums and activates an immutable release", async () => {
  const home = await tempHome();
  try {
    const abandoned = path.join(home, "runtime", "releases", ".install-abandoned");
    await mkdir(abandoned, { recursive: true });
    const old = new Date(Date.now() - 2 * 60 * 60 * 1000);
    await utimes(abandoned, old, old);
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
    await assert.rejects(stat(abandoned), { code: "ENOENT" });
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime prepare reports not initialized instead of an internal error", async () => {
  const home = await tempHome();
  try {
    const bundle = await runtimeBundle(home);
    const installed = await runCli(["runtime", "install", bundle, "--json"], { home });
    assert.equal(installed.code, 0, installed.stderr);
    const prepared = await runCli(["runtime", "prepare", "--json"], { home });
    assert.equal(prepared.code, 1);
    assert.equal(JSON.parse(prepared.stdout).error_code, "not_initialized");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("runtime prune removes only recognized inactive releases and managed venvs", async () => {
  const home = await tempHome();
  try {
    const bundle = await runtimeBundle(home);
    const installed = await runCli(["runtime", "install", bundle, "--json"], { home });
    assert.equal(installed.code, 0, installed.stderr);
    const releases = path.join(home, "runtime", "releases");
    const venvs = path.join(home, "runtime", "venvs");
    for (const version of ["0.8.0", "0.9.0"]) {
      await mkdir(path.join(releases, version), { recursive: true });
      await writeFile(path.join(releases, version, "manifest.json"), `${JSON.stringify({ release_version: version })}\n`);
      await mkdir(path.join(venvs, version), { recursive: true });
      await writeFile(path.join(venvs, version, "pyvenv.cfg"), "home = managed\n");
    }
    await mkdir(path.join(releases, "unknown"), { recursive: true });
    await writeFile(path.join(releases, "unknown", "manifest.json"), '{"release_version":"other"}\n');
    await mkdir(path.join(venvs, "unknown"), { recursive: true });
    await writeFile(path.join(home, "runtime.json"), `${JSON.stringify({
      schema_version: 1,
      home,
      release_version: "0.8.0",
    })}\n`);

    const pruned = await runCli(["runtime", "prune", "--json"], { home });
    assert.equal(pruned.code, 0, `${pruned.stderr}\n${pruned.stdout}`);
    const response = JSON.parse(pruned.stdout);
    assert.deepEqual(response.removed_releases, ["0.9.0"]);
    assert.deepEqual(response.removed_venvs, ["0.9.0"]);
    assert.deepEqual(response.protected_versions, ["1.0.0-test", "0.8.0"]);
    await stat(path.join(releases, "1.0.0-test"));
    await stat(path.join(releases, "0.8.0"));
    await stat(path.join(venvs, "0.8.0"));
    await stat(path.join(releases, "unknown"));
    await stat(path.join(venvs, "unknown"));
    await assert.rejects(stat(path.join(releases, "0.9.0")), { code: "ENOENT" });
    await assert.rejects(stat(path.join(venvs, "0.9.0")), { code: "ENOENT" });
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

    const staleControl = path.join(home, "control", "stale-test");
    await mkdir(staleControl, { recursive: true });
    await writeFile(path.join(home, "runtime.json"), `${JSON.stringify({
      schema_version: 1,
      instance_id: "stale-after-reboot",
      home,
      backend_pid: 2147483647,
      frontend_pid: 2147483646,
      processes: {
        backend: { pid: 2147483647, control_path: staleControl },
        frontend: { pid: 2147483646, control_path: staleControl },
      },
    })}\n`);
    const recovered = await runCli(["start", "--port", "auto", "--json"], { home, env });
    assert.equal(recovered.code, 0, `${recovered.stderr}\n${recovered.stdout}`);
    assert.equal(JSON.parse(recovered.stdout).status, "running");
    const recoveredStop = await runCli(["stop", "--json"], { home, env });
    assert.equal(recoveredStop.code, 0, `${recoveredStop.stderr}\n${recoveredStop.stdout}`);
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

test("new CLI safely stops a Runtime launched with the legacy control protocol", async () => {
  const home = await tempHome();
  const instanceId = "legacy-upgrade-instance";
  const role = "backend";
  const token = "legacy-control-token";
  const control = path.join(home, "control", "legacy-upgrade-test");
  const legacy = spawn(process.execPath, [legacyControlProcess, control, token, instanceId, role], {
    detached: process.platform !== "win32",
    stdio: "ignore",
  });
  await once(legacy, "spawn");
  try {
    const started = Date.now();
    while (Date.now() - started < 2000) {
      try {
        await stat(path.join(control, "ready"));
        break;
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
    }
    await stat(path.join(control, "ready"));
    await writeFile(path.join(home, "runtime.json"), `${JSON.stringify({
      schema_version: 1,
      instance_id: instanceId,
      home,
      release_version: "0.1.16",
      backend_pid: legacy.pid,
      frontend_pid: legacy.pid,
      processes: {
        backend: {
          pid: legacy.pid,
          launcher: "legacy-runtime-launcher",
          instance_id: instanceId,
          role,
          control_path: control,
          control_token: token,
        },
      },
    })}\n`, { mode: 0o600 });

    const stopped = await runCli(["stop", "--json"], { home });
    assert.equal(stopped.code, 0, `${stopped.stderr}\n${stopped.stdout}`);
    assert.equal(JSON.parse(stopped.stdout).status, "stopped");
    await assert.rejects(readFile(path.join(home, "runtime.json")), { code: "ENOENT" });
  } finally {
    try { process.kill(legacy.pid, "SIGKILL"); } catch {}
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
    const config = defaultConfig();
    config.initialized = true;
    await writeFile(path.join(home, "deploy.json"), `${JSON.stringify(config)}\n`, { mode: 0o600 });
    const bundle = await runtimeBundle(home, { longRunning: true });
    const installed = await runCli(["runtime", "install", bundle, "--json"], { home });
    assert.equal(installed.code, 0, installed.stderr);
    await writeFile(path.join(home, "runtime.json"), `${JSON.stringify({
      schema_version: 1,
      instance_id: "forged-instance",
      home,
      release_version: "1.0.0-test",
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
    const stopError = JSON.parse(stopped.stdout);
    assert.equal(stopError.error_code, "runtime_ownership_mismatch");
    assert.match(stopError.error, /delete .*runtime\.json/);
    assert.equal(stopError.details.recovery.inspect, "puddingclaw status --json");
    const started = await runCli(["start", "--port", "auto", "--json"], { home });
    assert.equal(started.code, 1);
    const startError = JSON.parse(started.stdout);
    assert.equal(startError.error_code, "stale_runtime_state");
    assert.match(startError.error, /do not kill unknown PIDs/);
    assert.equal(startError.details.recovery.state_file, path.join(home, "runtime.json"));
    assert.doesNotThrow(() => process.kill(unknown.pid, 0));
  } finally {
    unknown.kill("SIGKILL");
    await rm(home, { recursive: true, force: true });
  }
});

test("database migrate runs catalog_migration through the runtime Python", {
  skip: process.platform === "win32",
}, async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const initialized = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(initialized.code, 0, `${initialized.stderr}\n${initialized.stdout}`);
    const bundle = await runtimeBundle(home);
    await mkdir(path.join(bundle, "backend"), { recursive: true });
    const installed = await runCli(["runtime", "install", bundle, "--json"], { home });
    assert.equal(installed.code, 0, installed.stderr);

    // Recording fake Python replaces the init-selected interpreter.
    const recorder = path.join(home, "python-args.txt");
    const python = path.join(home, "recording-python");
    await writeFile(python, `#!/bin/sh\nprintf '%s\\n' "$@" > "${recorder}"\n`, { mode: 0o700 });
    await chmod(python, 0o700);
    const configPath = path.join(home, "deploy.json");
    const config = JSON.parse(await readFile(configPath, "utf8"));
    config.runtime.python.command = python;
    await writeFile(configPath, `${JSON.stringify(config)}\n`);

    const migrated = await runCli([
      "database", "migrate", "sqlite-to-postgres",
      "--url", "postgresql://u:p@db.internal:5432/puddingclaw", "--json",
    ], { home });
    assert.equal(migrated.code, 0, `${migrated.stderr}\n${migrated.stdout}`);
    assert.deepEqual(JSON.parse(migrated.stdout), {
      status: "migrated",
      direction: "sqlite-to-postgres",
      next_command: "puddingclaw start",
    });
    const args = (await readFile(recorder, "utf8")).trim().split("\n");
    assert.deepEqual(args, [
      "-m", "catalog_migration", "sqlite-to-pg",
      "--target-url", "postgresql://u:p@db.internal:5432/puddingclaw",
    ]);

    const down = await runCli(["database", "migrate", "postgres-to-sqlite", "--json"], { home });
    assert.equal(down.code, 0, `${down.stderr}\n${down.stdout}`);
    const downArgs = (await readFile(recorder, "utf8")).trim().split("\n");
    assert.deepEqual(downArgs, ["-m", "catalog_migration", "pg-to-sqlite"]);
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("database migrate validates direction and the required --url", async () => {
  const home = await tempHome();
  try {
    const env = await fakePython(home);
    const initialized = await runCli([
      "init", "--profile", "harness", "--non-interactive", "--port", "auto", "--json",
    ], { home, env });
    assert.equal(initialized.code, 0, `${initialized.stderr}\n${initialized.stdout}`);

    const badDirection = await runCli(["database", "migrate", "sideways", "--json"], { home });
    assert.equal(badDirection.code, 2);
    assert.equal(JSON.parse(badDirection.stdout).error_code, "argument_error");

    const missingUrl = await runCli(["database", "migrate", "sqlite-to-postgres", "--json"], { home });
    assert.equal(missingUrl.code, 2);
    assert.equal(JSON.parse(missingUrl.stdout).error_code, "argument_error");

    const unknownFlag = await runCli([
      "database", "migrate", "postgres-to-sqlite", "--typo", "value", "--json",
    ], { home });
    assert.equal(unknownFlag.code, 2);
    assert.equal(JSON.parse(unknownFlag.stdout).error_code, "argument_error");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});
