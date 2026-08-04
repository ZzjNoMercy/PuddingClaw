#!/usr/bin/env node
import os from "node:os";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { WorkerClient, WorkerClientError } from "./client.js";
import { exitCodeForResponse, writeDiagnostic, writeJson } from "./output.js";

const VERSION = "0.1.0";
const CAPABILITIES = ["data.query", "data.analysis", "data.nl2sql", "knowledge.query"];

function platformId() {
  const value = String(process.env.PUDDING_PLATFORM_ID || "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value) || value === "." || value === "..") {
    throw new WorkerClientError("PUDDING_PLATFORM_ID is missing or invalid", { code: "configuration_error" });
  }
  return value;
}

async function ensureWorkspace() {
  const root = String(process.env.PUDDINGCLAW_PROJECTS_ROOT || os.homedir());
  const target = path.resolve(root, platformId());
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
  else if (typeof value?.reply === "string") process.stdout.write(`${value.reply}\n`);
  else writeJson(value);
}

function parseFlags(args) {
  const positionals = [];
  const flags = {};
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--json") { flags.json = true; continue; }
    if (arg === "--input-json") { flags.inputJson = args[++i]; continue; }
    if (arg === "--model") { flags.model = args[++i]; continue; }
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

async function runCommand(args) {
  const { positionals, flags } = parseFlags(args);
  const input = await readInput(flags);
  const message = input?.message ?? (positionals.length ? positionals.join(" ") : "");
  const inputModel = input?.model ?? input?.analytics_model_id;
  if (!message || !String(message).trim()) throw new WorkerClientError("message is required", { code: "argument_error" });
  if (flags.model !== undefined && inputModel !== undefined && String(flags.model) !== String(inputModel)) {
    throw new WorkerClientError("--model conflicts with stdin model", { code: "argument_error" });
  }
  const model = flags.model ?? inputModel;
  await ensureWorkspace();
  const client = new WorkerClient(config());
  const controller = new AbortController();
  const onSignal = () => controller.abort();
  process.once("SIGINT", onSignal);
  try {
    const body = {
      message: String(message),
      analytics_model_id: model === undefined || model === null ? null : String(model),
      ...(input?.session_id ? { session_id: String(input.session_id) } : {}),
      ...(input?.metadata && typeof input.metadata === "object" ? { metadata: input.metadata } : {}),
      ...(input?.request_id ? { request_id: String(input.request_id) } : {}),
    };
    const response = await client.request("/api/headless/runs", {
      method: "POST", body, signal: controller.signal,
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
    emit({ ...base, ...response, configured: true }, asJson);
    return 0;
  } catch (error) {
    const result = { ...base, configured: false, authenticated: error?.code === "auth_error" ? false : null, reachable: error?.code === "connection_error" ? false : null, error: error?.message || String(error) };
    emit(result, asJson);
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
  if (command === "capabilities") { emit({ schema_version: "1", agent_id: "puddingclaw", protocol_version: "1", capabilities: CAPABILITIES, analytics_model_selection: { type: "analytics_model", required: true, discovery_command: ["models", "list", "--json"] } }, jsonMode(rest)); return 0; }
  if (command === "doctor") return doctor(rest);
  if (command === "models" && rest[0] === "list") return models(rest.slice(1));
  if (command === "run") return runCommand(rest);
  throw new WorkerClientError("usage: puddingclaw run <message> [--model <analytics_model_id>] [--json]", { code: "argument_error" });
}

try {
  const code = await main(process.argv.slice(2));
  process.exitCode = code;
} catch (error) {
  if (error?.code === "cancelled") process.exitCode = 130;
  else if (error?.code === "timeout") process.exitCode = 3;
  else process.exitCode = 2;
  if (process.argv.includes("--json")) {
    writeJson({ schema_version: "1", status: "error", error: error?.message || String(error) });
  } else {
    writeDiagnostic(error?.message || String(error));
  }
}
