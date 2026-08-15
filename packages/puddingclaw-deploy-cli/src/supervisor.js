import fs from "node:fs/promises";
import fsSync from "node:fs";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { CliError } from "./errors.js";
import { loadConfig } from "./config.js";
import { readJson, writeJsonAtomic } from "./store.js";
import { probeRuntimeState } from "./probes.js";
import { selectPorts } from "./init.js";
import { loadActiveRuntime, resolveRuntimeProcess } from "./runtime-bundle.js";
import { ensureLocalWorkerToken } from "./local-worker-token.js";
import { readSecret } from "./secrets.js";

const launcherPath = fileURLToPath(new URL("./runtime-launcher.js", import.meta.url));

function isAlive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

function controlPath(home, instanceId, role) {
  const key = createHash("sha256").update(`${path.resolve(home)}\0${instanceId}\0${role}`).digest("hex").slice(0, 32);
  return path.join(home, "control", key);
}

function matchesIdentity(identity, item, nonce) {
  return identity?.ok === true
    && identity.nonce === nonce
    && identity.pid === item.pid
    && identity.instance_id === item.instance_id
    && identity.role === item.role;
}

async function runIdentityHandshake(item, root, { legacy = false, timeoutMs = 2000 } = {}) {
  const nonce = randomUUID();
  const requestPath = path.join(root, legacy ? "request.json" : `request-${nonce}.json`);
  const responsePath = path.join(root, legacy ? "response.json" : `response-${nonce}.json`);
  try {
    await fs.rm(responsePath, { force: true });
    await writeJsonAtomic(requestPath, { action: "identify", token: item.control_token, nonce });
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const identity = await readJson(responsePath, null).catch(() => null);
      if (matchesIdentity(identity, item, nonce)) return true;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return false;
  } finally {
    await fs.rm(requestPath, { force: true }).catch(() => {});
    await fs.rm(responsePath, { force: true }).catch(() => {});
  }
}

async function acquireLegacyHandshakeLock(root, timeoutMs = 2500) {
  const lock = path.join(root, ".legacy-handshake.lock");
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      await fs.mkdir(lock, { mode: 0o700 });
      return lock;
    } catch (error) {
      if (error?.code !== "EEXIST") return null;
      const stat = await fs.stat(lock).catch(() => null);
      if (stat && Date.now() - stat.mtimeMs > 5000) {
        await fs.rm(lock, { recursive: true, force: true }).catch(() => {});
      } else {
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
  }
  return null;
}

async function verifyOwnedProcess(item, home) {
  if (!item || !Number.isInteger(item.pid) || !item.launcher || !item.instance_id) return false;
  if (!isAlive(item.pid)) return false;
  if (!item.control_path || !item.control_token || !item.role) return false;
  const expectedRoot = path.resolve(item.control_path);
  const controlRoot = path.resolve(home, "control");
  if (!expectedRoot.startsWith(`${controlRoot}${path.sep}`)) return false;
  if (await runIdentityHandshake(item, expectedRoot)) return true;
  if (!isAlive(item.pid)) return false;
  const legacyLock = await acquireLegacyHandshakeLock(expectedRoot);
  if (!legacyLock) return false;
  try {
    return await runIdentityHandshake(item, expectedRoot, { legacy: true });
  } finally {
    await fs.rm(legacyLock, { recursive: true, force: true }).catch(() => {});
  }
}

export function publicRuntimeState(state) {
  if (!state) return state;
  return {
    ...state,
    processes: Object.fromEntries(Object.entries(state.processes || {}).map(([name, item]) => [name, {
      pid: item.pid,
      command: item.command,
      log: item.log,
    }])),
  };
}

export async function probeManagedRuntimeState(paths, state) {
  const base = await probeRuntimeState(state, paths.home);
  if (base.status !== "running") return { ...base, ownership_verified: false };
  const items = Object.values(state?.processes || {});
  if (!items.length) return { ...base, status: "unverified", ownership_verified: false };
  const verified = await Promise.all(items.map((item) => verifyOwnedProcess(item, paths.home)));
  return verified.every(Boolean)
    ? { ...base, ownership_verified: true }
    : { ...base, status: "unverified", ownership_verified: false };
}

async function waitForProcess(child, spec, url, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (child.exitCode !== null || !isAlive(child.pid)) return false;
    if (!spec.health_path) {
      if (Date.now() - started >= 400) return true;
    } else {
      try {
        const response = await fetch(`${url}${spec.health_path}`, {
          signal: AbortSignal.timeout(1500),
          redirect: "manual",
        });
        if (response.status >= 200 && response.status < 400) return true;
      } catch {
        // Service may still be starting.
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return false;
}

function terminateProcessGroup(pid, signal) {
  if (!Number.isInteger(pid) || !isAlive(pid)) return;
  if (process.platform === "win32") {
    const args = ["/PID", String(pid), "/T"];
    if (signal === "SIGKILL") args.push("/F");
    spawnSync("taskkill.exe", args, { stdio: "ignore", timeout: 5000 });
    return;
  }
  try { process.kill(-pid, signal); } catch { process.kill(pid, signal); }
}

async function waitUntilStopped(pids, timeoutMs) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (pids.every((pid) => !isAlive(pid))) return true;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return pids.every((pid) => !isAlive(pid));
}

async function startupFailure(name, logFile) {
  return new CliError(`${name} did not become ready`, {
    code: `${name}_start_failed`,
    exitCode: 1,
    details: { log: logFile },
  });
}

async function cleanupControlPath(item, home) {
  if (!item?.control_path) return;
  const target = path.resolve(item.control_path);
  const root = path.resolve(home, "control");
  if (!target.startsWith(`${root}${path.sep}`)) return;
  await fs.rm(target, { recursive: true, force: true }).catch(() => {});
}

export async function startRuntime(paths, { automaticPorts = false, timeoutMs = 30_000 } = {}) {
  const config = await loadConfig(paths.config);
  if (!config?.initialized) {
    throw new CliError("deploy CLI is not initialized", { code: "not_initialized", exitCode: 1 });
  }
  const existing = await readJson(paths.runtimeState, null);
  const existingStatus = await probeManagedRuntimeState(paths, existing);
  if (existingStatus.status === "running") {
    return { status: "already_running", runtime: publicRuntimeState(existing) };
  }
  if (
    existing
    && existingStatus.status === "stale"
    && existingStatus.alive_pids?.length === 0
    && path.resolve(String(existing.home || "")) === path.resolve(paths.home)
  ) {
    await Promise.all(Object.values(existing.processes || {}).map((item) => cleanupControlPath(item, paths.home)));
    await fs.rm(paths.runtimeState, { force: true });
  } else if (existing) {
    throw new CliError(
      "runtime ownership cannot be verified; run `puddingclaw status --json` and do not kill unknown PIDs. "
      + `After confirming they are unrelated, delete ${paths.runtimeState} and retry`,
      {
        code: "stale_runtime_state",
        exitCode: 1,
        details: {
          ...existingStatus,
          recovery: {
            inspect: "puddingclaw status --json",
            state_file: paths.runtimeState,
            warning: "Never terminate a PID unless you have independently confirmed that you own it.",
          },
        },
      },
    );
  }
  const active = await loadActiveRuntime(paths);
  if (!active) {
    throw new CliError("no PuddingClaw runtime is installed; install a verified runtime bundle first", {
      code: "runtime_not_installed",
      exitCode: 1,
    });
  }
  const localWorkerToken = await ensureLocalWorkerToken(paths);
  const initialProviderApiKey = config.provider?.status === "unconfigured"
    ? ""
    : await readSecret(paths.providerApiKey);
  const initialMultimodalProviderApiKey = config.multimodal_provider?.status === "unconfigured"
    || config.multimodal_provider?.reuse_primary_credential
    ? ""
    : await readSecret(paths.multimodalProviderApiKey);
  const embeddingApiKey = config.infrastructure?.embedding?.status === "disabled"
    ? ""
    : await readSecret(paths.embeddingApiKey);
  const databaseUrl = config.infrastructure?.catalog?.mode === "postgresql"
    ? await readSecret(paths.databaseUrl)
    : "";
  const ports = await selectPorts({
    backendPort: config.server.backend_port,
    frontendPort: config.server.frontend_port,
    automatic: automaticPorts,
  });
  const instanceId = `pc-${randomUUID()}`;
  const extensions = Object.fromEntries(Object.entries(config.extensions)
    .map(([name, value]) => [name, Boolean(value.enabled)]));
  const variables = {
    PUDDINGCLAW_HOME: paths.home,
    PUDDINGCLAW_INSTANCE_ID: instanceId,
    BACKEND_PORT: ports.backendPort,
    FRONTEND_PORT: ports.frontendPort,
    BACKEND_URL: `http://${config.server.host}:${ports.backendPort}`,
    FRONTEND_URL: `http://${config.server.host}:${ports.frontendPort}`,
    EXTENSION_KNOWLEDGE: extensions.knowledge ? "1" : "0",
    EXTENSION_ANALYTICS: extensions.analytics ? "1" : "0",
    EXTENSION_HEADLESS_WORKER: extensions.headless_worker ? "1" : "0",
    PYTHON_COMMAND: config.runtime?.python?.command || "",
  };
  const backend = resolveRuntimeProcess(active, "backend", variables);
  const frontend = resolveRuntimeProcess(active, "frontend", variables);
  await fs.mkdir(paths.logs, { recursive: true, mode: 0o700 });
  const timestamp = new Date().toISOString().replaceAll(":", "-");
  const logPaths = {
    backend: path.join(paths.logs, `backend-${timestamp}.log`),
    frontend: path.join(paths.logs, `frontend-${timestamp}.log`),
  };
  const children = [];
  const launch = (name, resolved) => {
    const descriptor = fsSync.openSync(logPaths[name], "a", 0o600);
    try {
      const control = controlPath(paths.home, instanceId, name);
      const token = randomUUID();
      const child = spawn(process.execPath, [launcherPath, instanceId, name], {
        cwd: resolved.cwd,
        detached: true,
        env: {
          ...process.env,
          ...resolved.env,
          PUDDINGCLAW_LAUNCH_COMMAND: resolved.command,
          PUDDINGCLAW_LAUNCH_CWD: resolved.cwd,
          PUDDINGCLAW_LAUNCH_ARGS: JSON.stringify(resolved.args),
          PUDDINGCLAW_CONTROL_PATH: control,
          PUDDINGCLAW_CONTROL_TOKEN: token,
          PUDDINGCLAW_HOME: paths.home,
          PUDDINGCLAW_INSTANCE_ID: instanceId,
          PUDDINGCLAW_PROFILE: config.profile,
          PUDDINGCLAW_EXTENSIONS: JSON.stringify(extensions),
          PUDDINGCLAW_EXTENSION_KNOWLEDGE: variables.EXTENSION_KNOWLEDGE,
          PUDDINGCLAW_EXTENSION_ANALYTICS: variables.EXTENSION_ANALYTICS,
          PUDDINGCLAW_EXTENSION_HEADLESS_WORKER: variables.EXTENSION_HEADLESS_WORKER,
          ...(name === "backend" ? {
            PUDDINGCLAW_HEADLESS_TOKEN: localWorkerToken,
            PUDDINGCLAW_INITIAL_PROVIDER: JSON.stringify(config.provider || {}),
            PUDDINGCLAW_INITIAL_PROVIDER_BOOTSTRAP_ID: config.initialized_at || "legacy",
            ...(initialProviderApiKey ? { PUDDINGCLAW_INITIAL_PROVIDER_API_KEY: initialProviderApiKey } : {}),
            PUDDINGCLAW_INITIAL_MULTIMODAL_PROVIDER: JSON.stringify(config.multimodal_provider || {}),
            ...(initialMultimodalProviderApiKey
              ? { PUDDINGCLAW_INITIAL_MULTIMODAL_PROVIDER_API_KEY: initialMultimodalProviderApiKey }
              : {}),
            ...(embeddingApiKey ? { DASHSCOPE_API_KEY: embeddingApiKey } : {}),
            PUDDINGCLAW_DATABASE_MODE: config.infrastructure?.catalog?.mode || "sqlite",
            PUDDINGCLAW_DATABASE_SOURCE: config.infrastructure?.catalog?.source || "fallback",
            ...(databaseUrl ? { PUDDINGCLAW_DATABASE_URL: databaseUrl } : {}),
            ...(config.infrastructure?.milvus?.enabled
              ? {
                PUDDINGCLAW_MILVUS_URI: config.infrastructure.milvus.uri,
                MILVUS_URL: config.infrastructure.milvus.uri,
              }
              : {}),
            PUDDINGCLAW_ENABLE_MULTIMODAL_INDEX: config.infrastructure?.milvus?.enabled
              && config.infrastructure?.embedding?.status === "configured" ? "1" : "0",
            PUDDINGCLAW_MINERU_URL: config.infrastructure?.mineru?.base_url || "http://127.0.0.1:8002",
          } : {}),
          BACKEND_PORT: String(ports.backendPort),
          FRONTEND_PORT: String(ports.frontendPort),
          BACKEND_INTERNAL_URL: variables.BACKEND_URL,
          PORT: name === "frontend" ? String(ports.frontendPort) : String(ports.backendPort),
        },
        stdio: ["ignore", descriptor, descriptor],
      });
      child.unref();
      children.push({ name, child, resolved, control, token });
      return child;
    } finally {
      fsSync.closeSync(descriptor);
    }
  };
  try {
    const backendChild = launch("backend", backend);
    const backendReady = await waitForProcess(
      backendChild,
      active.manifest.processes.backend,
      variables.BACKEND_URL,
      timeoutMs,
    );
    if (!backendReady) throw await startupFailure("backend", logPaths.backend);
    const frontendChild = launch("frontend", frontend);
    const frontendReady = await waitForProcess(
      frontendChild,
      active.manifest.processes.frontend,
      variables.FRONTEND_URL,
      timeoutMs,
    );
    if (!frontendReady) throw await startupFailure("frontend", logPaths.frontend);
    const controls = Object.fromEntries(children.map((item) => [item.name, item]));
    const state = {
      schema_version: 1,
      instance_id: instanceId,
      home: paths.home,
      release_version: active.manifest.release_version,
      extensions,
      started_at: new Date().toISOString(),
      backend_url: variables.BACKEND_URL,
      frontend_url: variables.FRONTEND_URL,
      backend_pid: backendChild.pid,
      frontend_pid: frontendChild.pid,
      processes: {
        backend: {
          pid: backendChild.pid,
          launcher: launcherPath,
          instance_id: instanceId,
          role: "backend",
          control_path: controls.backend.control,
          control_token: controls.backend.token,
          command: backend.command,
          log: logPaths.backend,
        },
        frontend: {
          pid: frontendChild.pid,
          launcher: launcherPath,
          instance_id: instanceId,
          role: "frontend",
          control_path: controls.frontend.control,
          control_token: controls.frontend.token,
          command: frontend.command,
          log: logPaths.frontend,
        },
      },
    };
    await writeJsonAtomic(paths.runtimeState, state);
    return { status: "running", runtime: publicRuntimeState(state) };
  } catch (error) {
    for (const { child } of children.reverse()) terminateProcessGroup(child.pid, "SIGTERM");
    await waitUntilStopped(children.map(({ child }) => child.pid), 2000);
    await Promise.all(children.map((item) => cleanupControlPath({ control_path: item.control }, paths.home)));
    throw error;
  }
}

export async function stopRuntime(paths, { force = false } = {}) {
  const state = await readJson(paths.runtimeState, null);
  if (!state) return { status: "stopped", message: "no managed runtime is recorded" };
  if (path.resolve(String(state.home || "")) !== path.resolve(paths.home)) {
    throw new CliError("runtime state belongs to another deploy home", { code: "runtime_ownership_mismatch" });
  }
  const items = Object.values(state.processes || {});
  const alive = items.filter((item) => isAlive(item.pid));
  const verification = await Promise.all(alive.map(async (item) => ({
    item,
    verified: await verifyOwnedProcess(item, paths.home),
  })));
  const unverified = verification.filter(({ verified }) => !verified).map(({ item }) => item);
  if (unverified.length) {
    throw new CliError(
      "refusing to stop a process whose ownership cannot be verified; the PID may have been reused. "
      + `Run \`puddingclaw status --json\`; after confirming the PID is unrelated, delete ${paths.runtimeState} `
      + "without terminating that process",
      {
        code: "runtime_ownership_mismatch",
        details: {
          pids: unverified.map((item) => item.pid),
          recovery: {
            inspect: "puddingclaw status --json",
            state_file: paths.runtimeState,
            warning: "Never terminate a PID unless you have independently confirmed that you own it.",
          },
        },
      },
    );
  }
  for (const item of alive) terminateProcessGroup(item.pid, "SIGTERM");
  const stopped = await waitUntilStopped(alive.map((item) => item.pid), 5000);
  if (!stopped && !force) {
    throw new CliError("runtime did not stop after SIGTERM; rerun with --force to send SIGKILL", {
      code: "runtime_stop_timeout",
      exitCode: 1,
    });
  }
  if (!stopped && force) {
    for (const item of alive) terminateProcessGroup(item.pid, "SIGKILL");
    const forced = await waitUntilStopped(alive.map((item) => item.pid), 2000);
    if (!forced) {
      throw new CliError("runtime remained alive after the authenticated force-stop request", {
        code: "runtime_force_stop_failed",
        exitCode: 1,
        details: { pids: alive.map((item) => item.pid) },
      });
    }
  }
  await Promise.all(items.map((item) => cleanupControlPath(item, paths.home)));
  await fs.rm(paths.runtimeState, { force: true });
  return { status: "stopped", instance_id: state.instance_id };
}

export async function openRuntime(paths, { opener = spawn } = {}) {
  const state = await readJson(paths.runtimeState, null);
  const status = await probeManagedRuntimeState(paths, state);
  if (status.status !== "running" || !state.frontend_url) {
    throw new CliError("managed PuddingClaw runtime is not running", { code: "runtime_not_running", exitCode: 1 });
  }
  const command = process.platform === "darwin" ? "open" : process.platform === "win32" ? "cmd" : "xdg-open";
  const args = process.platform === "win32" ? ["/c", "start", "", state.frontend_url] : [state.frontend_url];
  const child = opener(command, args, { detached: true, stdio: "ignore" });
  child.unref();
  return { status: "opened", url: state.frontend_url };
}
