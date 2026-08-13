import fs from "node:fs/promises";
import { CliError, assertArgument } from "./errors.js";
import { installRuntimeBundle, loadActiveRuntime } from "./runtime-bundle.js";
import { prepareRuntimePython } from "./runtime-python.js";
import { fileURLToPath } from "node:url";

const embeddedRuntime = fileURLToPath(new URL("../runtime-bundle", import.meta.url));

export async function embeddedRuntimeStatus() {
  try {
    const manifest = JSON.parse(await fs.readFile(`${embeddedRuntime}/manifest.json`, "utf8"));
    return { available: true, path: embeddedRuntime, release_version: manifest.release_version };
  } catch (error) {
    if (error?.code === "ENOENT") return { available: false, path: embeddedRuntime, release_version: null };
    throw error;
  }
}

export async function runtimeCommand(args, paths) {
  const [action, source] = args;
  if (action === "install") {
    assertArgument(Boolean(source), "usage: runtime install <bundle-directory>");
    return installRuntimeBundle(source === "bundled" ? embeddedRuntime : source, paths);
  }
  if (action === "prepare") return prepareRuntimePython(paths, { allowUvBootstrap: true });
  if (action === "inspect") {
    const active = await loadActiveRuntime(paths);
    if (!active) return { status: "not_installed", active: null };
    const { files, ...manifest } = active.manifest;
    return {
      status: "installed",
      active: active.active,
      manifest: {
        ...manifest,
        file_count: Object.keys(files).length,
      },
    };
  }
  throw new CliError("usage: runtime install <bundle-directory|bundled> | runtime prepare | runtime inspect", {
    code: "argument_error",
  });
}

export async function logsCommand(paths) {
  const files = [];
  try {
    for (const name of await fs.readdir(paths.logs)) {
      if (!name.endsWith(".log")) continue;
      const file = `${paths.logs}/${name}`;
      const stat = await fs.stat(file);
      files.push({ name, path: file, size: stat.size, modified_at: stat.mtime.toISOString() });
    }
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  files.sort((left, right) => right.modified_at.localeCompare(left.modified_at));
  return { status: "ok", log_directory: paths.logs, files };
}

export async function requireRuntimeForStart(paths) {
  const active = await loadActiveRuntime(paths);
  if (!active) {
    throw new CliError("no PuddingClaw runtime is installed; install a verified runtime bundle first", {
      code: "runtime_not_installed",
      exitCode: 1,
    });
  }
  return active;
}
