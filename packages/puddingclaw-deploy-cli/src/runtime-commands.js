import fs from "node:fs/promises";
import path from "node:path";
import { CliError, assertArgument } from "./errors.js";
import { installRuntimeBundle, loadActiveRuntime } from "./runtime-bundle.js";
import { prepareRuntimePython } from "./runtime-python.js";
import { fileURLToPath } from "node:url";
import { readJson } from "./store.js";

const embeddedRuntime = fileURLToPath(new URL("../runtime-bundle", import.meta.url));
const SAFE_VERSION = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;

async function directoryEntries(root) {
  try {
    return await fs.readdir(root, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
}

async function pruneRuntime(paths) {
  const active = await readJson(path.join(paths.runtime, "active.json"), null);
  if (!active?.release_version || !SAFE_VERSION.test(active.release_version)) {
    throw new CliError("no active Runtime is installed; nothing can be pruned safely", {
      code: "runtime_not_installed",
      exitCode: 1,
    });
  }
  const runtimeState = await readJson(paths.runtimeState, null);
  const protectedVersions = new Set([
    active.release_version,
    ...(SAFE_VERSION.test(String(runtimeState?.release_version || "")) ? [runtimeState.release_version] : []),
  ]);
  const releasesRoot = path.join(paths.runtime, "releases");
  const venvsRoot = path.join(paths.runtime, "venvs");
  const removedReleases = [];
  const removedVenvs = [];
  const skipped = [];

  for (const entry of await directoryEntries(releasesRoot)) {
    if (entry.name.startsWith(".")) continue;
    if (!entry.isDirectory() || !SAFE_VERSION.test(entry.name)) {
      skipped.push({ kind: "release", name: entry.name, reason: "unrecognized" });
      continue;
    }
    if (protectedVersions.has(entry.name)) continue;
    const target = path.join(releasesRoot, entry.name);
    const manifest = await readJson(path.join(target, "manifest.json"), null);
    if (manifest?.release_version !== entry.name) {
      skipped.push({ kind: "release", name: entry.name, reason: "manifest_mismatch" });
      continue;
    }
    await fs.rm(target, { recursive: true, force: true });
    removedReleases.push(entry.name);
  }

  for (const entry of await directoryEntries(venvsRoot)) {
    if (!entry.isDirectory() || !SAFE_VERSION.test(entry.name)) {
      skipped.push({ kind: "venv", name: entry.name, reason: "unrecognized" });
      continue;
    }
    if (protectedVersions.has(entry.name)) continue;
    const target = path.join(venvsRoot, entry.name);
    try {
      const marker = await fs.lstat(path.join(target, "pyvenv.cfg"));
      if (!marker.isFile() || marker.isSymbolicLink()) throw new Error("invalid venv marker");
    } catch {
      skipped.push({ kind: "venv", name: entry.name, reason: "not_a_managed_venv" });
      continue;
    }
    await fs.rm(target, { recursive: true, force: true });
    removedVenvs.push(entry.name);
  }

  return {
    status: "pruned",
    active_version: active.release_version,
    protected_versions: [...protectedVersions],
    removed_releases: removedReleases,
    removed_venvs: removedVenvs,
    skipped,
  };
}

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
  if (action === "prune") {
    assertArgument(!source, "usage: runtime prune");
    return pruneRuntime(paths);
  }
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
  throw new CliError("usage: runtime install <bundle-directory|bundled> | runtime prepare | runtime inspect | runtime prune", {
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
