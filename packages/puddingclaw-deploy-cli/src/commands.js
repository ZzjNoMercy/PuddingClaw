import { CliError, assertArgument } from "./errors.js";
import { applyExtension, EXTENSIONS, getConfigValue, loadConfig, saveConfig, setConfigValue } from "./config.js";
import { parseConfigValue } from "./args.js";
import { probeHome, probeNode, probePlatform, probePort, probePython, probeUv } from "./probes.js";
import { readJson } from "./store.js";
import { mark } from "./output.js";
import { probeManagedRuntimeState, publicRuntimeState } from "./supervisor.js";

async function requireConfig(paths) {
  const config = await loadConfig(paths.config);
  if (!config) {
    throw new CliError("PuddingClaw is not initialized; run puddingclaw init", {
      code: "not_initialized",
      exitCode: 1,
    });
  }
  return config;
}

function urlPort(value) {
  try {
    const parsed = new URL(value);
    if (parsed.port) return Number(parsed.port);
    return parsed.protocol === "https:" ? 443 : parsed.protocol === "http:" ? 80 : null;
  } catch {
    return null;
  }
}

export async function configCommand(args, paths) {
  const [action, key, rawValue] = args;
  const config = await requireConfig(paths);
  if (action === "show") return config;
  if (action === "get") return { key, value: getConfigValue(config, key) };
  if (action === "set") {
    assertArgument(key && rawValue !== undefined, "usage: config set <key> <value>");
    setConfigValue(config, key, parseConfigValue(rawValue));
    await saveConfig(paths.config, config);
    return { status: "updated", key, value: getConfigValue(config, key) };
  }
  throw new CliError("usage: config show | config get <key> | config set <key> <value>", {
    code: "argument_error",
  });
}

export async function extensionCommand(args, paths) {
  const [action, name] = args;
  const config = await requireConfig(paths);
  if (action === "list") {
    return {
      profile: config.profile,
      extensions: EXTENSIONS.map((item) => ({ name: item, enabled: config.extensions[item].enabled })),
    };
  }
  if (action === "enable" || action === "disable") {
    assertArgument(Boolean(name), `usage: extension ${action} <name>`);
    applyExtension(config, name, action === "enable");
    await saveConfig(paths.config, config);
    return { status: "updated", profile: config.profile, extension: name, enabled: action === "enable" };
  }
  throw new CliError("usage: extension list | extension enable <name> | extension disable <name>", {
    code: "argument_error",
  });
}

export async function statusCommand(paths) {
  const config = await loadConfig(paths.config);
  const runtime = await readJson(paths.runtimeState, null);
  const instance = await probeManagedRuntimeState(paths, runtime);
  return {
    schema_version: 1,
    initialized: Boolean(config?.initialized),
    home: paths.home,
    profile: config?.profile || null,
    extensions: config?.extensions || null,
    instance,
    runtime: publicRuntimeState(runtime),
  };
}

export async function doctorCommand(paths) {
  const config = await loadConfig(paths.config);
  const runtime = await readJson(paths.runtimeState, null);
  const instance = await probeManagedRuntimeState(paths, runtime);
  const probes = [
    await probePlatform(),
    probeNode(),
    probePython(config?.runtime?.python?.command || ""),
    probeUv(),
    await probeHome(paths.home, { create: false }),
  ];
  if (config?.server) {
    for (const [name, port] of [
      ["backend", config.server.backend_port],
      ["frontend", config.server.frontend_port],
    ]) {
      const portProbe = await probePort(port, config.server.host);
      const runtimePort = urlPort(runtime?.[`${name}_url`]);
      probes.push(portProbe.status === "occupied" && instance.status === "running" && runtimePort === port
        ? { ...portProbe, status: "managed", required: false, instance_id: runtime.instance_id }
        : portProbe);
    }
  }
  probes.push(instance);
  const extensions = EXTENSIONS.map((name) => ({
    probe: `extension.${name}`,
    status: config?.extensions?.[name]?.enabled ? "enabled" : "disabled",
    required: false,
  }));
  const blocking = probes.filter((probe) => probe.required && ["failed", "needs_action", "occupied"].includes(probe.status));
  return {
    schema_version: 1,
    status: blocking.length ? "needs_action" : "ok",
    initialized: Boolean(config?.initialized),
    home: paths.home,
    probes,
    extensions,
  };
}

export function formatDoctor(result) {
  const deployment = result.deployment || result;
  const lines = ["PuddingClaw Doctor", `Home: ${deployment.home}`, "", "Deployment"];
  for (const probe of deployment.probes) {
    const detail = probe.version || probe.path || probe.selected?.version || probe.status;
    lines.push(`${mark(probe.status)} ${probe.probe.padEnd(24)} ${detail}`);
  }
  lines.push("");
  for (const extension of deployment.extensions) {
    lines.push(`${mark(extension.status)} ${extension.probe.padEnd(24)} ${extension.status}`);
  }
  if (result.deployment) {
    lines.push("", "Worker API");
    const ready = result.configured === true && result.reachable === true;
    const detail = ready ? "local · reachable" : (result.error || "not ready");
    lines.push(`${ready ? "✓" : "!"} ${"connection".padEnd(24)} ${detail}`);
    if (result.server_version) lines.push(`  ${"server version".padEnd(24)} ${result.server_version}`);
    if (result.project_id) lines.push(`  ${"project".padEnd(24)} ${result.project_id}`);
    if (result.workspace_ready !== undefined) {
      lines.push(`  ${"workspace".padEnd(24)} ${result.workspace_ready ? "ready" : "not ready"}`);
    }
  }
  return lines.join("\n");
}
