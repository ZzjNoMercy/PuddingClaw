import fs from "node:fs/promises";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { CliError } from "./errors.js";
import { loadConfig, saveConfig } from "./config.js";
import { probePython, probeUv, resolveExecutablePath } from "./probes.js";
import { loadActiveRuntime } from "./runtime-bundle.js";
import { readJson, writeJsonAtomic } from "./store.js";
import { bootstrapUv } from "./uv-runtime.js";
import { prepareManagedPython } from "./python-runtime.js";

async function run(command, args, { cwd, stderr = process.stderr, env = process.env } = {}) {
  const child = spawn(command, args, {
    cwd,
    env,
    stdio: ["ignore", "ignore", "pipe"],
  });
  child.stderr.on("data", (chunk) => stderr.write(chunk));
  const [code] = await once(child, "close");
  if (code !== 0) {
    throw new CliError(`${command} exited with code ${code}`, {
      code: "runtime_python_prepare_failed",
      exitCode: 1,
    });
  }
}

function venvPython(venv) {
  return process.platform === "win32"
    ? path.join(venv, "Scripts", "python.exe")
    : path.join(venv, "bin", "python");
}

export function pythonHeadersAvailable(command) {
  if (!command) return false;
  const result = spawnSync(command, [
    "-c",
    "import os,sysconfig; print(os.path.isfile(os.path.join(sysconfig.get_path('include'), 'Python.h')))",
  ], {
    encoding: "utf8",
    timeout: 5000,
    stdio: ["ignore", "pipe", "ignore"],
  });
  return result.status === 0 && String(result.stdout || "").trim() === "True";
}

export async function prepareRuntimePython(paths, {
  allowUvBootstrap = false,
  stderr = process.stderr,
} = {}) {
  const config = await loadConfig(paths.config);
  if (!config) {
    throw new CliError("PuddingClaw is not initialized; run `puddingclaw init` first", {
      code: "not_initialized",
      exitCode: 1,
    });
  }
  const active = await loadActiveRuntime(paths);
  if (!active) {
    throw new CliError("no runtime is installed", { code: "runtime_not_installed", exitCode: 1 });
  }
  const install = active.manifest.install?.python;
  if (!install) return { status: "not_required", release_version: active.manifest.release_version };
  let uv = probeUv(config.runtime?.uv?.command);
  if (uv.status !== "available" && allowUvBootstrap) {
    const bootstrapped = await bootstrapUv(paths.home, { stderr });
    uv = { ...probeUv(bootstrapped.selected.command), selected: bootstrapped.selected };
    config.runtime.uv = bootstrapped.selected;
  }
  if (uv.status !== "available") {
    throw new CliError("uv is required to prepare the embedded Backend runtime", {
      code: "uv_required",
      exitCode: 1,
    });
  }
  const configuredBasePython = config?.runtime?.python?.base_command || config?.runtime?.python?.command;
  const basePythonProbe = configuredBasePython ? probePython(configuredBasePython) : null;
  let basePython = basePythonProbe?.selected?.command || resolveExecutablePath(configuredBasePython);
  if ((!basePython || !path.isAbsolute(basePython) || !pythonHeadersAvailable(basePython)) && allowUvBootstrap) {
    const managed = await prepareManagedPython(paths.home, {
      uvCommand: uv.selected.command,
      stderr,
    });
    basePython = managed.selected.command;
    config.runtime.python = managed.selected;
  }
  if (!basePython || !path.isAbsolute(basePython)) {
    throw new CliError("a compatible base Python must be selected by init first", {
      code: "python_required",
      exitCode: 1,
    });
  }
  if (!pythonHeadersAvailable(basePython)) {
    throw new CliError("the selected Python does not provide Python.h development headers", {
      code: "python_headers_required",
      exitCode: 1,
    });
  }
  const knowledgeEnabled = Boolean(config.extensions?.knowledge?.enabled);
  const analyticsEnabled = Boolean(config.extensions?.analytics?.enabled);
  const dependencyProfile = knowledgeEnabled && analyticsEnabled
    ? "full"
    : knowledgeEnabled
      ? "knowledge"
      : analyticsEnabled
        ? "analytics"
        : "harness";
  const preparedFile = path.join(paths.runtime, "prepared.json");
  const existing = await readJson(preparedFile, null);
  if (
    existing?.release_version === active.manifest.release_version
    && (existing.dependency_profile || "full") === dependencyProfile
  ) {
    try {
      await fs.access(existing.python);
      return { status: "already_prepared", ...existing };
    } catch {}
  }
  const venv = path.join(paths.runtime, "venvs", active.manifest.release_version);
  await fs.mkdir(path.dirname(venv), { recursive: true, mode: 0o700 });
  await run(uv.selected.command, ["venv", venv, "--python", basePython, "--clear"], { stderr });
  const python = venvPython(venv);
  const requirementsRelative = install.requirements_by_profile?.[dependencyProfile] || install.requirements;
  const requirements = path.join(active.root, requirementsRelative);
  const wheel = path.join(active.root, install.wheel);
  const requirementsArgs = ["pip", "install", "--python", python];
  if (install.require_hashes) requirementsArgs.push("--require-hashes");
  requirementsArgs.push("--no-deps", "-r", requirements);
  await run(uv.selected.command, requirementsArgs, { stderr });
  await run(uv.selected.command, ["pip", "install", "--python", python, "--no-deps", wheel], { stderr });
  await run(python, ["-c", "import app, uvicorn"], {
    cwd: path.join(active.root, "backend"),
    stderr,
    env: {
      ...process.env,
      PUDDINGCLAW_EXTENSION_KNOWLEDGE: knowledgeEnabled ? "1" : "0",
      PUDDINGCLAW_EXTENSION_ANALYTICS: analyticsEnabled ? "1" : "0",
      PUDDINGCLAW_EXTENSION_HEADLESS_WORKER: config.extensions?.headless_worker?.enabled ? "1" : "0",
    },
  });
  config.runtime.python = {
    ...config.runtime.python,
    base_command: basePython,
    command: python,
    managed_runtime: true,
    release_version: active.manifest.release_version,
  };
  await saveConfig(paths.config, config);
  const prepared = {
    schema_version: 1,
    release_version: active.manifest.release_version,
    python,
    venv,
    dependency_profile: dependencyProfile,
    prepared_at: new Date().toISOString(),
  };
  await writeJsonAtomic(preparedFile, prepared);
  return { status: "prepared", ...prepared };
}
