#!/usr/bin/env node
import os from "node:os";
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { createInterface } from "node:readline/promises";
import { WorkerClient, WorkerClientError } from "./client.js";
import { exitCodeForResponse, writeDiagnostic, writeJson } from "./output.js";

const VERSION = "0.2.0";
const CAPABILITIES = ["data.query", "data.analysis", "data.nl2sql", "knowledge.query"];

async function ensureWorkspace(workspacePath) {
  if (workspacePath !== undefined) {
    if (typeof workspacePath !== "string" || !workspacePath.trim() || !path.isAbsolute(workspacePath)) {
      throw new WorkerClientError("workspace_path must be an absolute host path", { code: "argument_error" });
    }
    const resolved = path.resolve(workspacePath);
    try {
      const stat = await fs.stat(resolved);
      if (!stat.isDirectory()) throw new Error("not a directory");
    } catch (error) {
      throw new WorkerClientError(`workspace_path is unavailable: ${resolved}`, { code: error?.code === "ENOENT" ? "argument_error" : "configuration_error" });
    }
    return resolved;
  }
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
    if (arg === "--export") { flags.exportDir = args[++i]; continue; }
    if (arg === "--jsonl") { flags.jsonl = true; continue; }
    if (arg.startsWith("--")) throw new WorkerClientError(`unknown option: ${arg}`, { code: "argument_error" });
    positionals.push(arg);
  }
  return { positionals, flags };
}

function validateExportDir(value) {
  if (value === undefined) return undefined;
  const clean = String(value || "").trim();
  if (!clean || clean === "." || clean === "..") {
    throw new WorkerClientError("--export requires a directory", { code: "argument_error" });
  }
  return path.resolve(clean);
}

async function exportArtifacts(response, exportDir, workspaceRoot) {
  const target = validateExportDir(exportDir);
  if (!target) return response;
  const artifacts = Array.isArray(response?.artifacts) ? response.artifacts : [];
  const projectRoot = path.resolve(workspaceRoot || path.resolve(String(process.env.PUDDINGCLAW_PROJECTS_ROOT || os.homedir()), "puddingclaw"));
  await fs.mkdir(target, { recursive: true, mode: 0o700 });
  const exported = [];
  const skipped = [];
  for (const item of artifacts) {
    const relative = String(item?.path || "").replaceAll("\\", "/").replace(/^\/+/, "");
    if (!relative || relative.split("/").includes("..")) {
      skipped.push({ name: item?.name || "artifact", reason: "invalid_relative_path" });
      continue;
    }
    const source = path.resolve(projectRoot, relative);
    if (source !== projectRoot && !source.startsWith(`${projectRoot}${path.sep}`)) {
      skipped.push({ name: item?.name || relative, reason: "outside_worker_workspace" });
      continue;
    }
    try {
      const stat = await fs.stat(source);
      if (!stat.isFile()) throw new Error("not a file");
      const destination = path.resolve(target, relative);
      if (destination !== target && !destination.startsWith(`${target}${path.sep}`)) {
        skipped.push({ name: item?.name || relative, reason: "invalid_destination" });
        continue;
      }
      await fs.mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
      await fs.copyFile(source, destination);
      exported.push({ ...item, exported_path: path.relative(target, destination) });
    } catch (error) {
      skipped.push({ name: item?.name || relative, reason: error?.code === "ENOENT" ? "source_missing" : "copy_failed" });
    }
  }
  return { ...response, export: { directory: target, exported, skipped } };
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
      body: {
        continuation_token: token,
        decisions,
        request_id: `puddingclaw-cli-response-${randomUUID()}`,
      },
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
  if (input?.workspace_path !== undefined && typeof input.workspace_path !== "string") {
    throw new WorkerClientError("workspace_path must be an absolute host path", { code: "argument_error" });
  }
  if (flags.session !== undefined && inputSession !== undefined && String(flags.session) !== String(inputSession)) {
    throw new WorkerClientError("--session conflicts with stdin session_id", { code: "argument_error" });
  }
  const sessionId = flags.session ?? inputSession;
  const workspacePath = await ensureWorkspace(input?.workspace_path);
  const client = new WorkerClient(config());
  const controller = new AbortController();
  const onSignal = () => controller.abort();
  process.once("SIGINT", onSignal);
  try {
    const body = {
      message: String(message),
      ...(sessionId ? { session_id: String(sessionId) } : {}),
      ...(input?.workspace_path !== undefined ? { workspace_path: workspacePath } : {}),
      ...(input?.metadata && typeof input.metadata === "object" ? { metadata: input.metadata } : {}),
      ...(input?.request_id ? { request_id: String(input.request_id) } : {}),
    };
    let response;
    if (flags.jsonl) {
      response = await client.streamJsonl("/api/headless/runs?stream=true", {
        method: "POST",
        body,
        signal: controller.signal,
        onEvent: async (event) => {
          if (event?.event !== "result") writeJson(event);
        },
      });
      response = await exportArtifacts(response, flags.exportDir, workspacePath);
      writeJson({ event: "result", data: response });
      return exitCodeForResponse(response);
    }
    response = await client.request("/api/headless/runs", {
      method: "POST", body, signal: controller.signal,
    });
    response = await resumeWithCliApproval(client, response, {
      asJson: Boolean(flags.json || flags.jsonl),
      signal: controller.signal,
    });
    response = await exportArtifacts(response, flags.exportDir, workspacePath);
    emit(response, Boolean(flags.json || flags.jsonl));
    return exitCodeForResponse(response);
  } finally { process.removeListener("SIGINT", onSignal); }
}

async function respondCommand(args) {
  const { positionals, flags } = parseFlags(args);
  if (positionals.length !== 1) throw new WorkerClientError("run_id is required", { code: "argument_error" });
  const input = await readInput(flags);
  const runId = String(positionals[0] || "").trim();
  if (!runId) throw new WorkerClientError("run_id is required", { code: "argument_error" });
  if (!input || typeof input.continuation_token !== "string" || input.continuation_token.length < 20) {
    throw new WorkerClientError("continuation_token is required", { code: "protocol_error" });
  }
  if (!Array.isArray(input.decisions) || input.decisions.length === 0) {
    throw new WorkerClientError("decisions must be a non-empty array", { code: "protocol_error" });
  }
  if (input.workspace_path !== undefined && typeof input.workspace_path !== "string") {
    throw new WorkerClientError("workspace_path must be an absolute host path", { code: "argument_error" });
  }
  for (const decision of input.decisions) {
    if (!decision || typeof decision !== "object" || typeof decision.request_id !== "string"
      || !["approve", "reject"].includes(String(decision.decision))
      || !["once", "session"].includes(String(decision.scope || "once"))) {
      throw new WorkerClientError("decisions must contain request_id, decision and scope", { code: "protocol_error" });
    }
  }
  const workspacePath = await ensureWorkspace(input.workspace_path);
  const client = new WorkerClient(config());
  const body = {
    continuation_token: input.continuation_token,
    decisions: input.decisions,
    ...(input.workspace_path !== undefined ? { workspace_path: workspacePath } : {}),
    ...(input.request_id ? { request_id: String(input.request_id) } : {}),
  };
  let response = await client.request(`/api/headless/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST", body,
  });
  response = await exportArtifacts(response, flags.exportDir, workspacePath);
  emit(response, Boolean(flags.json || flags.jsonl));
  return exitCodeForResponse(response);
}

async function cancelCommand(args) {
  const { positionals, flags } = parseFlags(args);
  if (positionals.length !== 1) throw new WorkerClientError("run_id is required", { code: "argument_error" });
  const runId = String(positionals[0] || "").trim();
  if (!runId) throw new WorkerClientError("run_id is required", { code: "argument_error" });
  const response = await new WorkerClient(config()).request(`/api/headless/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  emit(response, Boolean(flags.json || flags.jsonl));
  return exitCodeForResponse(response);
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
  if (command === "capabilities") { emit({ schema_version: "1", agent_id: "puddingclaw", protocol_version: "1", capabilities: CAPABILITIES, operations: { run: true, continue: true, respond: true, cancel: true }, interaction_kinds: ["permission_request"], progress: "jsonl", transport: "cli", analytics_model_routing: { strategy: "backend", input: "message", discovery_command: ["models", "list", "--json"], ambiguity_outcome: "analytics_model_clarification_required" } }, jsonMode(rest)); return 0; }
  if (command === "doctor") return doctor(rest);
  if (command === "models" && rest[0] === "list") return models(rest.slice(1));
  if (command === "run") return runCommand(rest);
  if (command === "respond") return respondCommand(rest);
  if (command === "cancel") return cancelCommand(rest);
  throw new WorkerClientError("usage: puddingclaw run <message> [--session <session_id>] [--export <dir>] [--json] | respond <run_id> --input-json - --json | cancel <run_id> --json", { code: "argument_error" });
}

try {
  const code = await main(process.argv.slice(2));
  process.exitCode = code;
} catch (error) {
  if (error?.code === "cancelled") process.exitCode = 130;
  else if (error?.code === "timeout") process.exitCode = 3;
  else if (["session_expired", "interaction_expired", "interaction_conflict", "run_expired"].includes(error?.code)) process.exitCode = 1;
  else process.exitCode = 2;
  if (process.argv.includes("--json")) {
    writeJson({
      schema_version: "1",
      status: "error",
      ...(error?.code === "session_expired" ? { outcome: "session_expired" } : {}),
      ...(error?.code === "interaction_expired" ? { outcome: "interaction_expired" } : {}),
      ...(error?.code === "interaction_conflict" ? { outcome: "interaction_conflict" } : {}),
      ...(error?.code === "run_expired" ? { outcome: "run_expired" } : {}),
      error_code: error?.code || "unknown_error",
      ...(Number(error?.status) > 0 ? { http_status: Number(error.status) } : {}),
      error: error?.message || String(error),
    });
  } else {
    writeDiagnostic(error?.message || String(error));
  }
}
