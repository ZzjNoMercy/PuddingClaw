#!/usr/bin/env node

import { createRequire } from "node:module";
import { parseArgs } from "./args.js";
import { CliError } from "./errors.js";
import { resolveHome, homePaths } from "./home.js";
import { runInit } from "./init.js";
import { configCommand, doctorCommand, extensionCommand, formatDoctor, statusCommand } from "./commands.js";
import { logsCommand, requireRuntimeForStart, runtimeCommand } from "./runtime-commands.js";
import { openRuntime, startRuntime, stopRuntime } from "./supervisor.js";
import { writeError, writeHuman, writeJson } from "./output.js";
import { workerCommand, workerDoctorCommand } from "./worker-commands.js";
import { databaseCommand } from "./database-commands.js";

const { version: VERSION } = createRequire(import.meta.url)("../package.json");

const COMMAND_FLAGS = Object.freeze({
  init: [
    "api_key", "backend_port", "base_url", "database_create_if_missing", "database_mode",
    "database_name", "database_port", "database_url", "database_username", "embedding_api_key",
    "force", "frontend_port", "install_runtime", "milvus", "milvus_uri", "model",
    "multimodal_api_key", "multimodal_base_url", "multimodal_model", "multimodal_provider",
    "multimodal_provider_id", "multimodal_provider_name", "non_interactive", "plan", "port",
    "prepare_python", "profile", "provider", "provider_id", "provider_name", "python", "uv", "yes",
  ],
  database: [
    "database_create_if_missing", "database_mode", "database_name", "database_port", "database_url",
    "database_username", "non_interactive",
  ],
  agent: ["export", "input_json", "jsonl", "session"],
  start: ["port"],
  stop: ["force"],
  restart: ["force", "port"],
  config: [],
  extension: [],
  runtime: [],
  logs: [],
  open: [],
  status: [],
  doctor: [],
  version: [],
});

function assertCommandFlags(command, flags) {
  const accepted = new Set(["help", "json", ...(COMMAND_FLAGS[command] || [])]);
  const unknown = Object.keys(flags).find((name) => !accepted.has(name));
  if (unknown) {
    throw new CliError(`unknown option: --${unknown.replaceAll("_", "-")}`, {
      code: "argument_error",
    });
  }
}

function usage() {
  return [
    "PuddingClaw CLI",
    "",
    "Usage:",
    "  puddingclaw init [--profile <harness|knowledge|analytics|full>] [--port auto] [--python /path] [--uv /path] [--prepare-python] [--install-runtime]",
    "  puddingclaw init --profile <profile> --plan --json",
    "  puddingclaw config show|get|set ...",
    "  puddingclaw database show|configure",
    "  puddingclaw extension list|enable|disable ...",
    "  puddingclaw agent run <message> [--session <id>] [--export <dir>] [--json|--jsonl]",
    "  puddingclaw agent respond <run_id> --input-json - [--json]",
    "  puddingclaw agent cancel <run_id> [--json]",
    "  puddingclaw agent models list [--json]",
    "  puddingclaw agent capabilities [--json]",
    "  puddingclaw runtime install <bundle-directory|bundled>",
    "  puddingclaw runtime prepare",
    "  puddingclaw runtime inspect",
    "  puddingclaw runtime prune",
    "  puddingclaw logs [--json]",
    "  puddingclaw start [--port auto] [--json]",
    "  puddingclaw stop [--force] [--json]",
    "  puddingclaw restart [--force] [--port auto] [--json]",
    "  puddingclaw open [--json]",
    "  puddingclaw doctor [--json]",
    "  puddingclaw status [--json]",
    "  puddingclaw version [--json]",
  ].join("\n");
}

async function main({ positionals, flags }) {
  const [command, ...rest] = positionals;
  const paths = homePaths(resolveHome());
  if (!command || command === "help" || flags.help) return { value: usage(), humanOnly: true, code: 0 };
  assertCommandFlags(command, flags);
  if (command === "version") {
    return {
      value: {
        schema_version: "1",
        cli: "puddingclaw",
        cli_version: VERSION,
        agent_id: "puddingclaw",
        protocol_version: "1",
      },
      code: 0,
    };
  }
  if (command === "agent") {
    const [agentCommand, ...agentArgs] = rest;
    if (!agentCommand || agentCommand === "help") {
      return { value: usage(), humanOnly: true, code: 0 };
    }
    if (["run", "respond", "cancel", "models", "capabilities"].includes(agentCommand)) {
      return workerCommand(agentCommand, agentArgs, flags, paths);
    }
    throw new CliError(`unknown agent command: ${agentCommand}`, { code: "argument_error" });
  }
  if (command === "init") return { value: await runInit({ flags, paths }), code: 0 };
  if (command === "config") return { value: await configCommand(rest, paths), code: 0 };
  if (command === "database") return { value: await databaseCommand(rest, flags, paths), code: 0 };
  if (command === "extension") return { value: await extensionCommand(rest, paths), code: 0 };
  if (command === "runtime") return { value: await runtimeCommand(rest, paths), code: 0 };
  if (command === "logs") return { value: await logsCommand(paths), code: 0 };
  if (command === "start") {
    await requireRuntimeForStart(paths);
    return { value: await startRuntime(paths, { automaticPorts: flags.port === "auto" }), code: 0 };
  }
  if (command === "stop") return { value: await stopRuntime(paths, { force: Boolean(flags.force) }), code: 0 };
  if (command === "restart") {
    const previous = await stopRuntime(paths, { force: Boolean(flags.force) });
    await requireRuntimeForStart(paths);
    const started = await startRuntime(paths, { automaticPorts: flags.port === "auto" });
    return { value: { status: "restarted", previous, runtime: started.runtime }, code: 0 };
  }
  if (command === "open") return { value: await openRuntime(paths), code: 0 };
  if (command === "status") return { value: await statusCommand(paths), code: 0 };
  if (command === "doctor") {
    const deployment = await doctorCommand(paths);
    const worker = await workerDoctorCommand(paths);
    const workerReady = worker.value.configured === true
      && worker.value.authenticated === true
      && worker.value.reachable === true;
    const value = {
      ...worker.value,
      schema_version: "1",
      cli_version: VERSION,
      agent_id: "puddingclaw",
      protocol_version: "1",
      status: workerReady ? "ok" : "needs_action",
      deployment,
    };
    return {
      value,
      human: formatDoctor(value),
      code: workerReady ? 0 : (worker.code || (deployment.status === "ok" ? 2 : 1)),
    };
  }
  throw new CliError(`unknown command: ${command}`, { code: "argument_error" });
}

try {
  const parsed = parseArgs(process.argv.slice(2));
  const result = await main(parsed);
  process.exitCode = result.code;
  if (result.suppressOutput) {
    // Streaming commands already emitted their protocol events.
  } else if (parsed.flags.json || result.forceJson) writeJson(result.value);
  else if (result.humanOnly) writeHuman(result.value);
  else if (result.human) writeHuman(result.human);
  else writeHuman(JSON.stringify(result.value, null, 2));
} catch (error) {
  const cliError = error instanceof CliError
    ? error
    : new CliError(error?.message || String(error), { code: "internal_error" });
  process.exitCode = cliError.exitCode;
  if (process.argv.includes("--json")) {
    const outcomeCodes = new Set(["session_expired", "interaction_expired", "interaction_conflict", "run_expired"]);
    writeJson({
      schema_version: "1",
      status: "error",
      ...(outcomeCodes.has(cliError.code) ? { outcome: cliError.code } : {}),
      error_code: cliError.code,
      ...(Number(cliError.status) > 0 ? { http_status: Number(cliError.status) } : {}),
      error: cliError.message,
      ...(cliError.details && !Number(cliError.status) ? { details: cliError.details } : {}),
    });
  } else {
    writeError(cliError.message);
  }
}
