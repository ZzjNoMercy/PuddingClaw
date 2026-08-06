#!/usr/bin/env node
import os from "node:os";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { createInterface } from "node:readline/promises";
import { WorkerClient, WorkerClientError } from "./client.js";
import { exitCodeForResponse, writeDiagnostic, writeJson } from "./output.js";

const VERSION = "0.1.0";
const CAPABILITIES = ["data.query", "data.analysis", "data.nl2sql", "knowledge.query"];

async function ensureWorkspace() {
  const root = String(process.env.PUDDINGCLAW_PROJECTS_ROOT || os.homedir());
  const target = path.resolve(root, "puddingclaw");
  const rootResolved = path.resolve(root);
  if (target !== rootResolved && !target.startsWith(`${rootResolved}${path.sep}`)) {
    throw new WorkerClientError("platform workspace escapes projects root", { code: "configuration_error" });
  }
  await fs.mkdir(target, { recursive: true, mode: 0o700 });
  return target;
}

function config() {
  const endpoint = String(process.env.PUDDINGCLAW_URL || process.env.PUDDINGCLAW_BACKEND_URL || "http://127.0.0.1:8888").replace(/\/+$/, "");
  const parsed = new URL(endpoint);
  if (parsed.protocol === "http:" && !["localhost", "127.0.0.1", "::1"].includes(parsed.hostname)) {
    throw new WorkerClientError("remote Worker endpoint must use HTTPS", { code: "configuration_error" });
  }
  const token = String(process.env.PUDDINGCLAW_TOKEN || process.env.PUDDINGCLAW_HEADLESS_TOKEN || "").trim();
  if (!token) throw new WorkerClientError("PUDDINGCLAW_TOKEN is not configured", { code: "configuration_error" });
  return { endpoint, token, timeoutMs: Math.max(1000, Number(process.env.PUDDINGCLAW_TIMEOUT_S || 600) * 1000) };
}

function jsonMode(args) { return args.includes("--json"); }

function emit(value, asJson) {
  if (asJson) writeJson(value);
  else if (value?.status === "needs_input") writeJson(value);
  else if (typeof value?.final_response === "string" && value.final_response) process.stdout.write(`${value.final_response}\n`);
  else if (typeof value?.reply === "string") process.stdout.write(`${value.reply}\n`);
  else writeJson(value);
}

function doctorLine(ok, label, value) {
  const mark = ok === true ? "✓" : ok === false ? "✗" : "!";
  return `  ${mark} ${label.padEnd(18)}${value}`;
}

function doctorDetail(label, value) {
  if (value === undefined || value === null || value === "") return "";
  return `      ${label.padEnd(22)}${value}`;
}

function localCliStatus() {
  let npm = { available: false, path: null, version: null };
  try {
    const version = execFileSync("npm", ["--version"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    const command = process.platform === "win32" ? "where" : "which";
    const npmPath = execFileSync(command, ["npm"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim().split(/\r?\n/)[0];
    npm = { available: Boolean(version), path: npmPath || null, version: version || null };
  } catch { /* npm is optional for an already installed CLI */ }
  const nodeMajor = Number.parseInt(process.versions.node.split(".")[0], 10);
  return {
    command: "puddingclaw",
    installed: true,
    version: VERSION,
    required_version: VERSION,
    version_mismatch: false,
    node: { available: true, path: process.execPath, version: process.versions.node, supported: nodeMajor >= 20 },
    npm,
    install_policy: process.env.PUDDINGCLAW_CLI_INSTALL_POLICY || "not reported",
  };
}

function hostLabel() {
  const platform = { darwin: "macos", win32: "windows", linux: "linux" }[process.platform] || process.platform;
  const arch = { arm64: "aarch64", x64: "x86_64", arm: "arm", ia32: "x86" }[process.arch] || process.arch;
  return `${platform}-${arch}`;
}

function formatDoctor(result) {
  const cli = result.cli || {};
  const node = cli.node || {};
  const npm = cli.npm || {};
  const backendReady = result.configured === true && result.authenticated === true && result.reachable === true;
  const cliReady = cli.installed === true && cli.version_mismatch !== true;
  const lines = [
    `PuddingClaw Doctor v${result.cli_version || VERSION} · ${hostLabel()}`,
    "",
    "Worker API",
    doctorLine(backendReady, "connection", backendReady ? "authenticated · reachable" : (result.error || "not ready")),
    doctorDetail("server version", result.server_version),
    doctorDetail("project", result.project_id),
    doctorDetail("workspace", result.workspace_ready === true ? "ready" : result.workspace_ready === false ? "not ready" : undefined),
    doctorDetail("worker key", result.worker_key_name ? `${result.worker_key_name} (${result.key_id || "unknown"})` : result.key_id),
    doctorDetail("capabilities", Array.isArray(result.capabilities) ? result.capabilities.join(", ") : undefined),
    "",
    "Environment",
    doctorLine(cliReady, "puddingclaw", cliReady ? `installed · ${cli.version || "unknown"}` : (cli.install_message || "not ready")),
    doctorDetail("required version", cli.required_version),
    doctorDetail("command", cli.command),
    doctorDetail("node", node.available ? `${node.version || "available"} · ${node.path || "path unknown"}` : "not available"),
    doctorDetail("npm", npm.available ? `${npm.version || "available"} · ${npm.path || "path unknown"}` : "not available"),
    doctorDetail("install policy", cli.install_policy),
  ];
  if (cli.version_mismatch === true) lines.push(doctorDetail("version check", "mismatch"));
  return `${lines.filter(Boolean).join("\n")}\n`;
}

function emitDoctor(value, asJson) {
  if (asJson) writeJson(value);
  else process.stdout.write(formatDoctor(value));
}

function parseFlags(args) {
  const positionals = [];
  const flags = {};
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--json") { flags.json = true; continue; }
    if (arg === "--input-json") { flags.inputJson = args[++i]; continue; }
    if (arg === "--session") { flags.session = args[++i]; continue; }
    if (arg.startsWith("--")) throw new WorkerClientError(`unknown option: ${arg}`, { code: "argument_error" });
    positionals.push(arg);
  }
  return { positionals, flags };
}

async function readInput(flags) {
  if (flags.inputJson !== undefined && flags.inputJson !== "-") throw new WorkerClientError("--input-json only accepts -", { code: "argument_error" });
  if (flags.inputJson === undefined) return null;
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new WorkerClientError("stdin JSON exceeds 1 MiB", { code: "protocol_error" });
    chunks.push(chunk);
  }
  let value;
  try { value = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { throw new WorkerClientError("stdin is not valid JSON", { code: "protocol_error" }); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new WorkerClientError("stdin JSON must be an object", { code: "protocol_error" });
  return value;
}

function pendingRequests(response) {
  const needs = response?.needs_input;
  if (!needs || needs.type !== "permission_request") return [];
  return Array.isArray(needs.requests) && needs.requests.length ? needs.requests : [needs];
}

function permissionSummary(request) {
  const command = String(request?.command || request?.request?.command || "").trim();
  const pathValue = String(request?.path || request?.request?.path || "").trim();
  const tool = String(request?.tool_name || request?.request?.tool_name || "").trim();
  return command || pathValue || tool || String(request?.permission_type || "受保护操作");
}

async function collectApprovalDecisions(response) {
  const requests = pendingRequests(response);
  if (!requests.length) return null;
  if (!process.stdin.isTTY || !process.stderr.isTTY) return null;
  const rl = createInterface({ input: process.stdin, output: process.stderr });
  const decisions = [];
  try {
    for (const request of requests) {
      const options = new Set(Array.isArray(request?.options) ? request.options.map(String) : []);
      const supportsOnce = options.size === 0 || options.has("once")
        || options.some((option) => option.endsWith("_run"));
      const supportsSession = options.has("session")
        || options.some((option) => option.endsWith("_session"));
      process.stderr.write(`\nAgent 请求授权\n  ${permissionSummary(request)}\n`);
      if (request?.reason) process.stderr.write(`  原因：${request.reason}\n`);
      const choices = [
        ...(supportsOnce ? ["[1] 仅允许本次"] : []),
        ...(supportsSession ? ["[2] 本 Session 允许"] : []),
        "[3] 拒绝",
      ].join("  ");
      const validChoices = new Set([
        ...(supportsOnce ? ["1"] : []),
        ...(supportsSession ? ["2"] : []),
        "3",
      ]);
      let selected = "";
      while (!validChoices.has(selected)) {
        selected = String(await rl.question(`${choices}\n请选择：`)).trim();
      }
      decisions.push({
        request_id: String(request.request_id),
        decision: selected === "3" ? "reject" : "approve",
        scope: selected === "2" ? "session" : "once",
      });
    }
  } finally {
    rl.close();
  }
  return decisions;
}

async function resumeWithCliApproval(client, response, { asJson, signal }) {
  let current = response;
  while (current?.status === "needs_input" && current?.outcome === "waiting_hitl") {
    if (asJson) return current;
    const decisions = await collectApprovalDecisions(current);
    if (!decisions) return current;
    const runId = String(current.run_id || "");
    const token = String(current.continuation_token || "");
    if (!runId || !token) {
      throw new WorkerClientError("Headless approval response is missing continuation data", { code: "protocol_error" });
    }
    current = await client.request(`/api/headless/runs/${encodeURIComponent(runId)}/resume`, {
      method: "POST",
      body: { continuation_token: token, decisions },
      signal,
    });
  }
  return current;
}

async function runCommand(args) {
  const { positionals, flags } = parseFlags(args);
  const input = await readInput(flags);
  const message = input?.message ?? (positionals.length ? positionals.join(" ") : "");
  const inputSession = input?.session_id;
  if (!message || !String(message).trim()) throw new WorkerClientError("message is required", { code: "argument_error" });
  if (input?.model !== undefined || input?.analytics_model_id !== undefined) {
    throw new WorkerClientError("model input is not supported; PuddingClaw routes the question on the backend", { code: "argument_error" });
  }
  if (flags.session !== undefined && inputSession !== undefined && String(flags.session) !== String(inputSession)) {
    throw new WorkerClientError("--session conflicts with stdin session_id", { code: "argument_error" });
  }
  const sessionId = flags.session ?? inputSession;
  await ensureWorkspace();
  const client = new WorkerClient(config());
  const controller = new AbortController();
  const onSignal = () => controller.abort();
  process.once("SIGINT", onSignal);
  try {
    const body = {
      message: String(message),
      ...(sessionId ? { session_id: String(sessionId) } : {}),
      ...(input?.metadata && typeof input.metadata === "object" ? { metadata: input.metadata } : {}),
      ...(input?.request_id ? { request_id: String(input.request_id) } : {}),
    };
    let response = await client.request("/api/headless/runs", {
      method: "POST", body, signal: controller.signal,
    });
    response = await resumeWithCliApproval(client, response, {
      asJson: Boolean(flags.json),
      signal: controller.signal,
    });
    emit(response, Boolean(flags.json));
    return exitCodeForResponse(response);
  } finally { process.removeListener("SIGINT", onSignal); }
}

async function doctor(args) {
  const asJson = jsonMode(args);
  const base = { schema_version: "1", agent_id: "puddingclaw", cli_version: VERSION, protocol_version: "1" };
  try {
    await ensureWorkspace();
    const client = new WorkerClient(config());
    const response = await client.request("/api/headless/health");
    emitDoctor({ ...base, ...response, configured: true }, asJson);
    return 0;
  } catch (error) {
    const result = { ...base, configured: false, authenticated: error?.code === "auth_error" ? false : null, reachable: error?.code === "connection_error" ? false : null, error: error?.message || String(error), cli: localCliStatus() };
    emitDoctor(result, asJson);
    return error?.code === "timeout" ? 3 : 2;
  }
}

async function models(args) {
  const asJson = jsonMode(args);
  try {
    const response = await new WorkerClient(config()).request("/api/headless/models");
    emit(response, asJson);
    return 0;
  } catch (error) { emit({ schema_version: "1", error: error?.message || String(error) }, asJson); return error?.code === "timeout" ? 3 : 2; }
}

async function main(argv) {
  const [command, ...rest] = argv;
  if (command === "version") { emit({ schema_version: "1", cli_version: VERSION, protocol_version: "1", agent_id: "puddingclaw" }, jsonMode(rest)); return 0; }
  if (command === "capabilities") { emit({ schema_version: "1", agent_id: "puddingclaw", protocol_version: "1", capabilities: CAPABILITIES, analytics_model_routing: { strategy: "backend", input: "message", discovery_command: ["models", "list", "--json"], ambiguity_outcome: "analytics_model_clarification_required" } }, jsonMode(rest)); return 0; }
  if (command === "doctor") return doctor(rest);
  if (command === "models" && rest[0] === "list") return models(rest.slice(1));
  if (command === "run") return runCommand(rest);
  throw new WorkerClientError("usage: puddingclaw run <message> [--session <session_id>] [--json]", { code: "argument_error" });
}

try {
  const code = await main(process.argv.slice(2));
  process.exitCode = code;
} catch (error) {
  if (error?.code === "cancelled") process.exitCode = 130;
  else if (error?.code === "timeout") process.exitCode = 3;
  else if (error?.code === "session_expired") process.exitCode = 1;
  else process.exitCode = 2;
  if (process.argv.includes("--json")) {
    writeJson({
      schema_version: "1",
      status: "error",
      ...(error?.code === "session_expired" ? { outcome: "session_expired" } : {}),
      error_code: error?.code || "unknown_error",
      ...(Number(error?.status) > 0 ? { http_status: Number(error.status) } : {}),
      error: error?.message || String(error),
    });
  } else {
    writeDiagnostic(error?.message || String(error));
  }
}
