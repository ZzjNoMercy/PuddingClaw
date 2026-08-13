import fs from "node:fs/promises";
import { constants as fsConstants } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawnSync } from "node:child_process";

function executableVersion(command, args, parse) {
  try {
    const result = spawnSync(command, args, { encoding: "utf8", timeout: 3000 });
    if (result.status !== 0) return null;
    const output = `${result.stdout || ""}\n${result.stderr || ""}`.trim();
    return parse(output, command);
  } catch {
    return null;
  }
}

export function resolveExecutablePath(command) {
  const candidate = String(command || "").trim();
  if (!candidate) return "";
  if (path.isAbsolute(candidate)) return path.resolve(candidate);
  try {
    const locator = process.platform === "win32" ? "where" : "which";
    const output = execFileSync(locator, [candidate], {
      encoding: "utf8",
      timeout: 2000,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const located = String(output || "").split(/\r?\n/).find(Boolean) || "";
    return path.isAbsolute(located) ? path.resolve(located) : "";
  } catch {
    return "";
  }
}

function parsePython(output, command) {
  const match = output.match(/Python\s+(\d+)\.(\d+)\.(\d+)/i);
  if (!match) return null;
  const major = Number(match[1]);
  const minor = Number(match[2]);
  return {
    command: resolveExecutablePath(command) || command,
    version: `${match[1]}.${match[2]}.${match[3]}`,
    supported: major === 3 && (minor === 11 || minor === 12),
  };
}

export function probePython(explicitCommand = "") {
  const explicit = String(explicitCommand || process.env.PUDDINGCLAW_DEPLOY_PYTHON || "").trim();
  const candidates = explicit
    ? [[explicit, ["--version"]]]
    : process.platform === "win32"
    ? [["py", ["-3.12", "--version"]], ["py", ["-3.11", "--version"]], ["python", ["--version"]]]
    : [["python3.12", ["--version"]], ["python3.11", ["--version"]], ["python3", ["--version"]], ["python", ["--version"]]];
  const seen = new Set();
  const interpreters = [];
  for (const [command, args] of candidates) {
    const result = executableVersion(command, args, parsePython);
    if (!result) continue;
    const key = `${result.command}:${result.version}`;
    if (!seen.has(key)) interpreters.push(result);
    seen.add(key);
  }
  const selected = interpreters.find((item) => item.supported) || null;
  return {
    probe: "runtime.python",
    status: selected ? "available" : "needs_action",
    required: true,
    selected,
    interpreters,
    remediation: selected ? [] : ["一键准备 Python 3.12", "手动指定兼容 Python"],
  };
}

export function probeUv(explicitCommand = "") {
  const command = String(explicitCommand || process.env.PUDDINGCLAW_DEPLOY_UV || "uv").trim();
  const result = executableVersion(command, ["--version"], (output, selectedCommand) => {
    const match = output.match(/uv\s+(\d+\.\d+\.\d+)/i);
    return match ? { command: selectedCommand, version: match[1] } : null;
  });
  return {
    probe: "runtime.uv",
    status: result ? "available" : "needs_action",
    required: false,
    selected: result,
    remediation: result ? [] : ["安装用户级 uv 后重试"],
  };
}

export async function probeHome(home, { create = false } = {}) {
  try {
    if (create) await fs.mkdir(home, { recursive: true, mode: 0o700 });
    const stat = await fs.stat(home);
    if (!stat.isDirectory()) throw new Error("path is not a directory");
    await fs.access(home, fsConstants.R_OK | fsConstants.W_OK);
    return { probe: "runtime.home", status: "available", required: true, path: home };
  } catch (error) {
    return {
      probe: "runtime.home",
      status: "failed",
      required: true,
      path: home,
      code: error?.code || "home_unavailable",
      reason: error?.message || String(error),
    };
  }
}

export function probeNode() {
  const major = Number.parseInt(process.versions.node.split(".")[0], 10);
  return {
    probe: "runtime.node",
    status: major >= 20 ? "available" : "failed",
    required: true,
    version: process.versions.node,
    executable: process.execPath,
  };
}

export async function probePort(port, host = "127.0.0.1") {
  if (process.platform !== "win32") {
    const owner = await portOwner(port);
    return owner
      ? { probe: "runtime.port", status: "occupied", required: true, host, port, owner }
      : { probe: "runtime.port", status: "available", required: true, host, port };
  }
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", async (error) => {
      resolve({
        probe: "runtime.port",
        status: "occupied",
        required: true,
        host,
        port,
        code: error.code || "port_unavailable",
        owner: await portOwner(port),
      });
    });
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve({
        probe: "runtime.port",
        status: "available",
        required: true,
        host,
        port,
      }));
    });
  });
}

async function portOwner(port) {
  if (process.platform === "win32") return null;
  try {
    const output = execFileSync("lsof", ["-nP", `-iTCP:${port}`, "-sTCP:LISTEN", "-Fpc"], {
      encoding: "utf8",
      timeout: 2000,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const pid = output.match(/^p(\d+)$/m)?.[1];
    const command = output.match(/^c(.+)$/m)?.[1];
    return pid ? { pid: Number(pid), command: command || "unknown" } : null;
  } catch {
    return null;
  }
}

export async function findFreePort(start, host = "127.0.0.1", { limit = 100 } = {}) {
  for (let port = start; port < Math.min(65536, start + limit); port += 1) {
    const result = await probePort(port, host);
    if (result.status === "available") return port;
  }
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen({ host, port: 0, exclusive: true }, () => {
      const address = server.address();
      const selected = typeof address === "object" && address ? address.port : null;
      server.close(() => resolve(selected));
    });
  });
}

export async function probePlatform() {
  return {
    probe: "runtime.platform",
    status: "available",
    required: true,
    platform: process.platform,
    arch: process.arch,
    hostname: os.hostname(),
  };
}

export async function probeTcpEndpoint({ probe, host, port, timeoutMs = 2500, required = true }) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port });
    const finish = (status, reason = "") => {
      socket.destroy();
      resolve({
        probe,
        status,
        required,
        host,
        port,
        scope: "network_reachability",
        ...(reason ? { reason } : {}),
      });
    };
    socket.setTimeout(timeoutMs);
    socket.once("connect", () => finish("available"));
    socket.once("timeout", () => finish("needs_action", "connection timed out"));
    socket.once("error", (error) => finish("needs_action", error?.code || error?.message || String(error)));
  });
}

export async function probeProviderEndpoint({ baseUrl, apiKey, timeoutMs = 8000, fetchImpl = globalThis.fetch }) {
  const endpoint = `${String(baseUrl).replace(/\/$/, "")}/models`;
  try {
    const response = await fetchImpl(endpoint, {
      headers: { Authorization: `Bearer ${apiKey}` },
      signal: AbortSignal.timeout(timeoutMs),
    });
    return {
      probe: "provider.endpoint",
      status: response.ok ? "available" : "needs_action",
      required: true,
      endpoint,
      http_status: response.status,
      reason: response.ok ? "" : response.status === 401 || response.status === 403
        ? "API Key 未通过验证"
        : `Provider 返回 HTTP ${response.status}`,
    };
  } catch (error) {
    return {
      probe: "provider.endpoint",
      status: "needs_action",
      required: true,
      endpoint,
      reason: error?.message || String(error),
    };
  }
}

export async function probeHttpHealth({ probe, baseUrl, path: healthPath = "/health", timeoutMs = 3000 }) {
  const endpoint = `${String(baseUrl).replace(/\/$/, "")}${healthPath}`;
  try {
    const response = await fetch(endpoint, { signal: AbortSignal.timeout(timeoutMs) });
    return {
      probe,
      status: response.ok ? "available" : "needs_action",
      required: false,
      endpoint,
      http_status: response.status,
      ...(response.ok ? {} : { reason: `health endpoint returned HTTP ${response.status}` }),
    };
  } catch (error) {
    return { probe, status: "needs_action", required: false, endpoint, reason: error?.message || String(error) };
  }
}

export async function probeRuntimeState(state, home) {
  if (!state) return { probe: "runtime.instance", status: "stopped", required: false };
  const pids = [state.backend_pid, state.frontend_pid].filter(Number.isInteger);
  const alive = pids.filter((pid) => {
    try { process.kill(pid, 0); return true; } catch { return false; }
  });
  const sameHome = !state.home || path.resolve(state.home) === path.resolve(home);
  return {
    probe: "runtime.instance",
    status: alive.length === pids.length && pids.length > 0 && sameHome ? "running" : "stale",
    required: false,
    instance_id: state.instance_id || null,
    alive_pids: alive,
    expected_pids: pids,
    same_home: sameHome,
  };
}
