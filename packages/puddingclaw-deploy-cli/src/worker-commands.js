import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { createInterface } from "node:readline/promises";
import { CliError } from "./errors.js";
import { DEFAULT_BACKEND_PORT, loadConfig } from "./config.js";
import { writeJson } from "./output.js";
import { readJson } from "./store.js";
import { WorkerClient, WorkerClientError } from "./worker-client.js";

const WORKER_MANIFEST = createRequire(import.meta.url)("../worker.manifest.json");

function assertFlags(flags, allowed) {
  const accepted = new Set(["json", ...allowed]);
  const unknown = Object.keys(flags).filter((name) => !accepted.has(name));
  if (unknown.length) {
    throw new WorkerClientError(`unknown option: --${unknown[0].replaceAll("_", "-")}`, {
      code: "argument_error",
    });
  }
}

export async function agentClientConfig(paths) {
  const runtime = await readJson(paths.runtimeState, null);
  const deploy = await loadConfig(paths.config);
  const configuredEndpoint = process.env.PUDDINGCLAW_URL || process.env.PUDDINGCLAW_BACKEND_URL;
  const deployHost = deploy?.server?.host === "::1" ? "[::1]" : deploy?.server?.host;
  const fallbackEndpoint = runtime?.backend_url
    || (deploy?.server
      ? `http://${deployHost}:${deploy.server.backend_port}`
      : `http://127.0.0.1:${DEFAULT_BACKEND_PORT}`);
  const endpoint = String(configuredEndpoint || fallbackEndpoint).replace(/\/+$/, "");
  let parsed;
  try { parsed = new URL(endpoint); } catch {
    throw new WorkerClientError("PUDDINGCLAW_URL is invalid", { code: "configuration_error" });
  }
  if (!["localhost", "127.0.0.1", "::1", "[::1]"].includes(parsed.hostname)) {
    throw new WorkerClientError("PuddingClaw CLI only connects to a local loopback Backend", {
      code: "configuration_error",
    });
  }
  const requestedTimeout = Number(process.env.PUDDINGCLAW_TIMEOUT_S || 600);
  if (!Number.isFinite(requestedTimeout) || requestedTimeout <= 0) {
    throw new WorkerClientError("PUDDINGCLAW_TIMEOUT_S must be a positive number", {
      code: "configuration_error",
    });
  }
  return {
    endpoint,
    timeoutMs: Math.max(1000, requestedTimeout * 1000),
  };
}

export async function workerDoctorCommand(paths) {
  let clientConfig;
  try {
    clientConfig = await agentClientConfig(paths);
  } catch (error) {
    const failure = error instanceof WorkerClientError
      ? error
      : new WorkerClientError(error?.message || String(error), { code: "configuration_error" });
    return {
      code: failure.exitCode,
      value: {
        schema_version: "1",
        agent_id: "puddingclaw",
        protocol_version: "1",
        configured: false,
        reachable: false,
        error_code: failure.code,
        error: failure.message,
      },
    };
  }
  try {
    const value = await new WorkerClient(clientConfig).request("/api/headless/health");
    return { code: 0, value };
  } catch (error) {
    const failure = error instanceof WorkerClientError
      ? error
      : new WorkerClientError(error?.message || String(error));
    return {
      code: failure.exitCode,
      value: {
        schema_version: "1",
        agent_id: "puddingclaw",
        protocol_version: "1",
        configured: true,
        reachable: false,
        error_code: failure.code,
        error: failure.message,
      },
    };
  }
}

async function ensureWorkspace(paths, workspacePath) {
  if (workspacePath !== undefined) {
    if (typeof workspacePath !== "string" || !workspacePath.trim() || !path.isAbsolute(workspacePath)) {
      throw new WorkerClientError("workspace_path must be an absolute host path", { code: "argument_error" });
    }
    const resolved = path.resolve(workspacePath);
    try {
      const stat = await fs.stat(resolved);
      if (!stat.isDirectory()) throw new Error("not a directory");
    } catch (error) {
      throw new WorkerClientError(`workspace_path is unavailable: ${resolved}`, {
        code: error?.code === "ENOENT" ? "argument_error" : "configuration_error",
      });
    }
    return resolved;
  }
  const projectsRoot = String(process.env.PUDDINGCLAW_PROJECTS_ROOT || "").trim();
  const target = projectsRoot
    ? path.resolve(projectsRoot, "puddingclaw")
    : path.resolve(paths.home, "workspace");
  const root = path.resolve(projectsRoot || paths.home);
  if (target !== root && !target.startsWith(`${root}${path.sep}`)) {
    throw new WorkerClientError("platform workspace escapes projects root", { code: "configuration_error" });
  }
  await fs.mkdir(target, { recursive: true, mode: 0o700 });
  return target;
}

async function readInput(flags) {
  if (flags.input_json !== undefined && flags.input_json !== "-") {
    throw new WorkerClientError("--input-json only accepts -", { code: "argument_error" });
  }
  if (flags.input_json === undefined) return null;
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 1024 * 1024) {
      throw new WorkerClientError("stdin JSON exceeds 1 MiB", { code: "protocol_error" });
    }
    chunks.push(chunk);
  }
  let value;
  try { value = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch {
    throw new WorkerClientError("stdin is not valid JSON", { code: "protocol_error" });
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new WorkerClientError("stdin JSON must be an object", { code: "protocol_error" });
  }
  return value;
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
  const projectRoot = path.resolve(workspaceRoot);
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
  if (!requests.length || !process.stdin.isTTY || !process.stderr.isTTY) return null;
  const rl = createInterface({ input: process.stdin, output: process.stderr });
  const decisions = [];
  try {
    for (const request of requests) {
      const options = new Set(Array.isArray(request?.options) ? request.options.map(String) : []);
      const supportsOnce = options.size === 0 || options.has("once") || [...options].some((item) => item.endsWith("_run"));
      const supportsSession = options.has("session") || [...options].some((item) => item.endsWith("_session"));
      process.stderr.write(`\nAgent 请求授权\n  ${permissionSummary(request)}\n`);
      if (request?.reason) process.stderr.write(`  原因：${request.reason}\n`);
      const choices = [
        ...(supportsOnce ? ["[1] 仅允许本次"] : []),
        ...(supportsSession ? ["[2] 本 Session 允许"] : []),
        "[3] 拒绝",
      ].join("  ");
      const valid = new Set([...(supportsOnce ? ["1"] : []), ...(supportsSession ? ["2"] : []), "3"]);
      let selected = "";
      while (!valid.has(selected)) selected = String(await rl.question(`${choices}\n请选择：`)).trim();
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

async function resumeWithCliApproval(client, response, { jsonMode, signal, onEvent }) {
  let current = response;
  while (current?.status === "needs_input" && current?.outcome === "waiting_hitl") {
    if (jsonMode) return current;
    const decisions = await collectApprovalDecisions(current);
    if (!decisions) return current;
    const runId = String(current.run_id || "");
    const continuationToken = String(current.continuation_token || "");
    if (!runId || !continuationToken) {
      throw new WorkerClientError("Headless approval response is missing continuation data", {
        code: "protocol_error",
      });
    }
    current = await client.streamJsonl(`/api/headless/runs/${encodeURIComponent(runId)}/resume?stream=true`, {
      method: "POST",
      body: {
        continuation_token: continuationToken,
        decisions,
        request_id: `puddingclaw-cli-response-${randomUUID()}`,
      },
      signal,
      onEvent,
    });
  }
  return current;
}

export function exitCodeForResponse(response) {
  return response?.outcome === "completed" || response?.status === "completed"
    || response?.outcome === "cancelled" || response?.status === "cancelled" ? 0 : 1;
}

function resultPresentation(response, flags) {
  if (flags.json) return { value: response, code: exitCodeForResponse(response) };
  if (response?.status === "needs_input") {
    return { value: response, forceJson: true, code: exitCodeForResponse(response) };
  }
  if (typeof response?.final_response === "string" && response.final_response) {
    return { value: response, human: response.final_response, code: exitCodeForResponse(response) };
  }
  if (typeof response?.reply === "string") {
    return { value: response, human: response.reply, code: exitCodeForResponse(response) };
  }
  return { value: response, forceJson: true, code: exitCodeForResponse(response) };
}

function progressMessage(event) {
  const data = event?.data && typeof event.data === "object" ? event.data : {};
  if (event?.event === "run_starting") return "Worker 已接收任务";
  if (event?.event === "task_preflight_started") return "正在准备任务上下文";
  if (event?.event === "task_preflight_completed") return "任务上下文已准备";
  if (event?.event === "run_started") return "Agent 已开始执行";
  if (event?.event === "model_transport_interrupted") {
    return data.next_action === "retry_same_model_node" ? "模型连接中断，正在重试" : "模型连接中断";
  }
  if (event?.event === "model_response_recovery_started") return "模型回答不完整，正在自动恢复";
  if (event?.event === "model_response_incomplete") return "模型未形成完整回答";
  if (event?.event === "permission_required") return "等待人工审批";
  if (event?.event === "tool_start") {
    const tool = data.tool || data.tool_name;
    return `正在调用工具${tool ? `：${tool}` : ""}`;
  }
  if (event?.event === "tool_end") return "工具调用完成";
  if (event?.event === "final_response") return "正在整理最终结果";
  if (event?.event === "done" || event?.event === "result") {
    const completed = data.outcome === "completed" || data.status === "completed"
      || data.run_outcome === "completed";
    const cancelled = data.outcome === "cancelled" || data.status === "cancelled"
      || data.run_outcome === "cancelled";
    return completed ? "任务完成" : cancelled ? "任务已取消" : "任务未完成";
  }
  return "Agent 正在执行";
}

const HUMAN_PROGRESS_EVENTS = new Set([
  "run_starting",
  "task_preflight_started",
  "task_preflight_completed",
  "run_started",
  "model_transport_interrupted",
  "model_response_recovery_started",
  "model_response_incomplete",
  "permission_required",
  "tool_start",
  "tool_end",
  "final_response",
  "done",
]);

async function runCommand(args, flags, paths) {
  assertFlags(flags, ["input_json", "session", "export", "jsonl"]);
  const input = await readInput(flags);
  const message = input?.message ?? (args.length ? args.join(" ") : "");
  if (!message || !String(message).trim()) {
    throw new WorkerClientError("message is required", { code: "argument_error" });
  }
  if (input?.model !== undefined || input?.analytics_model_id !== undefined) {
    throw new WorkerClientError("model input is not supported; PuddingClaw routes the question on the backend", {
      code: "argument_error",
    });
  }
  if (input?.workspace_path !== undefined && typeof input.workspace_path !== "string") {
    throw new WorkerClientError("workspace_path must be an absolute host path", { code: "argument_error" });
  }
  if (flags.session !== undefined && input?.session_id !== undefined && String(flags.session) !== String(input.session_id)) {
    throw new WorkerClientError("--session conflicts with stdin session_id", { code: "argument_error" });
  }
  const sessionId = flags.session ?? input?.session_id;
  const workspacePath = await ensureWorkspace(paths, input?.workspace_path);
  const client = new WorkerClient(await agentClientConfig(paths));
  const controller = new AbortController();
  let activeCancelHandle = "";
  let remoteCancellation = null;
  const onSignal = () => {
    if (activeCancelHandle && !remoteCancellation) {
      remoteCancellation = client.request(
        `/api/headless/runs/${encodeURIComponent(activeCancelHandle)}/cancel`,
        { method: "POST" },
      ).catch(() => null);
    }
    controller.abort();
  };
  process.once("SIGINT", onSignal);
  // Process supervisors (including PuddingTeams) terminate a child with
  // SIGTERM. Treat it exactly like Ctrl-C so the in-flight HTTP stream is
  // aborted and the Backend can observe the disconnect/cancel path.
  process.once("SIGTERM", onSignal);
  try {
    const body = {
      message: String(message),
      ...(sessionId ? { session_id: String(sessionId) } : {}),
      ...(input?.workspace_path !== undefined ? { workspace_path: workspacePath } : {}),
      ...(input?.metadata && typeof input.metadata === "object" ? { metadata: input.metadata } : {}),
      ...(input?.request_id ? { request_id: String(input.request_id) } : {}),
    };
    let response;
    const streamProgress = async (event) => {
      const data = event?.data && typeof event.data === "object" ? event.data : {};
      const run = data.run && typeof data.run === "object" ? data.run : {};
      activeCancelHandle = String(run.run_id || data.run_id || activeCancelHandle || data.session_id || "");
      if (event?.event === "result") return;
      if (flags.jsonl) writeJson(event);
      else if (!flags.json && HUMAN_PROGRESS_EVENTS.has(event?.event)) {
        process.stderr.write(`${progressMessage(event)}\n`);
      }
    };
    response = await client.streamJsonl("/api/headless/runs?stream=true", {
      method: "POST",
      body,
      signal: controller.signal,
      onEvent: streamProgress,
    });
    if (flags.jsonl) {
      response = await exportArtifacts(response, flags.export, workspacePath);
      writeJson({ event: "result", data: response });
      return { value: null, suppressOutput: true, code: exitCodeForResponse(response) };
    }
    response = await resumeWithCliApproval(client, response, {
      jsonMode: Boolean(flags.json || flags.jsonl),
      signal: controller.signal,
      onEvent: streamProgress,
    });
    response = await exportArtifacts(response, flags.export, workspacePath);
    return resultPresentation(response, flags);
  } finally {
    if (remoteCancellation) await remoteCancellation;
    process.removeListener("SIGINT", onSignal);
    process.removeListener("SIGTERM", onSignal);
  }
}

async function respondCommand(args, flags, paths) {
  assertFlags(flags, ["input_json", "export", "jsonl"]);
  if (args.length !== 1) throw new WorkerClientError("run_id is required", { code: "argument_error" });
  const input = await readInput(flags);
  const runId = String(args[0] || "").trim();
  if (!runId) throw new WorkerClientError("run_id is required", { code: "argument_error" });
  if (!input || typeof input.continuation_token !== "string" || input.continuation_token.length < 20) {
    throw new WorkerClientError("continuation_token is required", { code: "protocol_error" });
  }
  if (!Array.isArray(input.decisions) || input.decisions.length === 0) {
    throw new WorkerClientError("decisions must be a non-empty array", { code: "protocol_error" });
  }
  for (const decision of input.decisions) {
    const permission = decision && typeof decision === "object"
      && ["approve", "reject"].includes(String(decision.decision))
      && ["once", "session"].includes(String(decision.scope || "once"));
    const userInput = decision && typeof decision === "object"
      && ["submit", "cancel"].includes(String(decision.action))
      && Array.isArray(decision.answers || []);
    if (typeof decision?.request_id !== "string" || permission === userInput) {
      throw new WorkerClientError("each decision must be either a permission decision or a user-input answer", { code: "protocol_error" });
    }
  }
  const workspacePath = await ensureWorkspace(paths, input.workspace_path);
  const client = new WorkerClient(await agentClientConfig(paths));
  const controller = new AbortController();
  let remoteCancellation = null;
  const onSignal = () => {
    if (!remoteCancellation) {
      remoteCancellation = client.request(
        `/api/headless/runs/${encodeURIComponent(runId)}/cancel`,
        { method: "POST" },
      ).catch(() => null);
    }
    controller.abort();
  };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  let response;
  try {
    response = await client.streamJsonl(
      `/api/headless/runs/${encodeURIComponent(runId)}/resume?stream=true`,
      {
        method: "POST",
        body: {
          continuation_token: input.continuation_token,
          decisions: input.decisions,
          ...(input.workspace_path !== undefined ? { workspace_path: workspacePath } : {}),
          ...(input.request_id ? { request_id: String(input.request_id) } : {}),
        },
        signal: controller.signal,
        onEvent: async (event) => {
          if (event?.event === "result") return;
          if (flags.jsonl) writeJson(event);
          else if (!flags.json && HUMAN_PROGRESS_EVENTS.has(event?.event)) {
            process.stderr.write(`${progressMessage(event)}\n`);
          }
        }
      },
    );
  } finally {
    if (remoteCancellation) await remoteCancellation;
    process.removeListener("SIGINT", onSignal);
    process.removeListener("SIGTERM", onSignal);
  }
  response = await exportArtifacts(response, flags.export, workspacePath);
  if (flags.jsonl) {
    writeJson({ event: "result", data: response });
    return { value: null, suppressOutput: true, code: exitCodeForResponse(response) };
  }
  return resultPresentation(response, flags);
}

async function cancelCommand(args, flags, paths) {
  assertFlags(flags, []);
  if (args.length !== 1) throw new WorkerClientError("run_id is required", { code: "argument_error" });
  const runId = String(args[0] || "").trim();
  if (!runId) throw new WorkerClientError("run_id is required", { code: "argument_error" });
  const response = await new WorkerClient(await agentClientConfig(paths)).request(
    `/api/headless/runs/${encodeURIComponent(runId)}/cancel`,
    { method: "POST" },
  );
  return resultPresentation(response, flags);
}

export async function workerCommand(command, args, flags, paths) {
  if (command === "run") return runCommand(args, flags, paths);
  if (command === "respond") return respondCommand(args, flags, paths);
  if (command === "cancel") return cancelCommand(args, flags, paths);
  if (command === "models") {
    assertFlags(flags, []);
    if (args.length !== 1 || args[0] !== "list") {
      throw new CliError("usage: agent models list [--json]", { code: "argument_error" });
    }
    const value = await new WorkerClient(await agentClientConfig(paths)).request("/api/headless/models");
    return { value, forceJson: !flags.json, code: 0 };
  }
  if (command === "capabilities") {
    assertFlags(flags, []);
    if (args.length) throw new CliError("usage: agent capabilities [--json]", { code: "argument_error" });
    return {
      value: {
        schema_version: "1",
        agent_id: "puddingclaw",
        protocol_version: "1",
        capabilities: WORKER_MANIFEST.capabilities,
        operations: WORKER_MANIFEST.operations,
        interaction_kinds: WORKER_MANIFEST.interactionKinds,
        progress: WORKER_MANIFEST.progress,
        transport: WORKER_MANIFEST.transport.type,
        analytics_model_routing: {
          strategy: WORKER_MANIFEST.modelRouting.strategy,
          input: WORKER_MANIFEST.modelRouting.input,
          discovery_command: WORKER_MANIFEST.modelRouting.discoveryCommand,
          ambiguity_outcome: WORKER_MANIFEST.modelRouting.ambiguityOutcome,
        },
      },
      forceJson: !flags.json,
      code: 0,
    };
  }
  throw new CliError(`unsupported Agent command: ${command}`, { code: "argument_error" });
}
