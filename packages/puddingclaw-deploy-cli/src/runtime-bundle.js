import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { CliError } from "./errors.js";
import { readJson, writeJsonAtomic } from "./store.js";

const SAFE_VERSION = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;

function resolveInside(root, relative, label) {
  if (!relative || typeof relative !== "string" || path.isAbsolute(relative)) {
    throw new CliError(`${label} must be a relative path`, { code: "invalid_runtime_manifest" });
  }
  const resolvedRoot = path.resolve(root);
  const resolved = path.resolve(resolvedRoot, relative);
  if (resolved !== resolvedRoot && !resolved.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw new CliError(`${label} escapes the runtime bundle`, { code: "invalid_runtime_manifest" });
  }
  return resolved;
}

export function validateRuntimeManifest(manifest, bundleRoot) {
  if (!manifest || manifest.schema_version !== 1) {
    throw new CliError("runtime manifest schema_version must be 1", { code: "invalid_runtime_manifest" });
  }
  if (!SAFE_VERSION.test(String(manifest.release_version || ""))) {
    throw new CliError("runtime manifest release_version is invalid", { code: "invalid_runtime_manifest" });
  }
  for (const contract of ["puddingclaw_home", "dynamic_ports", "extensions"]) {
    if (manifest.contracts?.[contract] !== 1) {
      throw new CliError(`runtime contract ${contract}=1 is required`, {
        code: "incompatible_runtime_contract",
      });
    }
  }
  if (!manifest.files || typeof manifest.files !== "object" || Array.isArray(manifest.files)) {
    throw new CliError("runtime manifest files map is required", { code: "invalid_runtime_manifest" });
  }
  if (!Object.keys(manifest.files).length) {
    throw new CliError("runtime manifest files map cannot be empty", { code: "invalid_runtime_manifest" });
  }
  for (const [relative, digest] of Object.entries(manifest.files)) {
    if (relative === "manifest.json") {
      throw new CliError("manifest.json cannot checksum itself", { code: "invalid_runtime_manifest" });
    }
    resolveInside(bundleRoot, relative, `files.${relative}`);
    if (!/^[a-f0-9]{64}$/i.test(String(digest))) {
      throw new CliError(`files.${relative} must contain a SHA-256 digest`, { code: "invalid_runtime_manifest" });
    }
  }
  for (const name of ["backend", "frontend"]) {
    const processSpec = manifest.processes?.[name];
    if (!processSpec || !Array.isArray(processSpec.args || [])) {
      throw new CliError(`processes.${name} is required`, { code: "invalid_runtime_manifest" });
    }
    const kind = processSpec.kind || "command";
    if (kind === "command") {
      resolveInside(bundleRoot, processSpec.command, `processes.${name}.command`);
      if (!Object.hasOwn(manifest.files, processSpec.command)) {
        throw new CliError(`processes.${name}.command must be covered by the files checksum map`, {
          code: "invalid_runtime_manifest",
        });
      }
    } else if (kind === "node_script") {
      resolveInside(bundleRoot, processSpec.script, `processes.${name}.script`);
      if (!Object.hasOwn(manifest.files, processSpec.script)) {
        throw new CliError(`processes.${name}.script must be covered by the files checksum map`, {
          code: "invalid_runtime_manifest",
        });
      }
    } else if (kind === "python_module") {
      if (!/^[A-Za-z_][A-Za-z0-9_.]*$/.test(String(processSpec.module || ""))) {
        throw new CliError(`processes.${name}.module is invalid`, { code: "invalid_runtime_manifest" });
      }
    } else {
      throw new CliError(`processes.${name}.kind is unsupported`, { code: "invalid_runtime_manifest" });
    }
    resolveInside(bundleRoot, processSpec.cwd || ".", `processes.${name}.cwd`);
    if (processSpec.env && (typeof processSpec.env !== "object" || Array.isArray(processSpec.env))) {
      throw new CliError(`processes.${name}.env must be an object`, { code: "invalid_runtime_manifest" });
    }
  }
  const pythonInstall = manifest.install?.python;
  if (pythonInstall) {
    if (typeof pythonInstall !== "object" || Array.isArray(pythonInstall)) {
      throw new CliError("install.python must be an object", { code: "invalid_runtime_manifest" });
    }
    for (const key of ["wheel", "requirements"]) {
      if (!pythonInstall[key] || typeof pythonInstall[key] !== "string") {
        throw new CliError(`install.python.${key} must be a relative path`, {
          code: "invalid_runtime_manifest",
        });
      }
      resolveInside(bundleRoot, pythonInstall[key], `install.python.${key}`);
      if (!Object.hasOwn(manifest.files, pythonInstall[key])) {
        throw new CliError(`install.python.${key} must be covered by the files checksum map`, {
          code: "invalid_runtime_manifest",
        });
      }
    }
    const requirementProfiles = pythonInstall.requirements_by_profile;
    if (requirementProfiles !== undefined) {
      if (!requirementProfiles || typeof requirementProfiles !== "object" || Array.isArray(requirementProfiles)) {
        throw new CliError("install.python.requirements_by_profile must be an object", {
          code: "invalid_runtime_manifest",
        });
      }
      for (const profile of ["harness", "knowledge", "analytics", "full"]) {
        const relative = requirementProfiles[profile];
        if (!relative || typeof relative !== "string") {
          throw new CliError(`install.python.requirements_by_profile.${profile} is required`, {
            code: "invalid_runtime_manifest",
          });
        }
        resolveInside(bundleRoot, relative, `install.python.requirements_by_profile.${profile}`);
        if (!Object.hasOwn(manifest.files, relative)) {
          throw new CliError(`requirements profile ${profile} must be covered by the files checksum map`, {
            code: "invalid_runtime_manifest",
          });
        }
      }
    }
    if (typeof pythonInstall.require_hashes !== "boolean") {
      throw new CliError("install.python.require_hashes must be a boolean", {
        code: "invalid_runtime_manifest",
      });
    }
  }
  return manifest;
}

async function sha256(file) {
  const hash = crypto.createHash("sha256");
  const handle = await fs.open(file, "r");
  try {
    for await (const chunk of handle.createReadStream()) hash.update(chunk);
  } finally {
    await handle.close().catch(() => {});
  }
  return hash.digest("hex");
}

async function listBundleFiles(root, directory = root) {
  const files = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    if (entry.isSymbolicLink()) {
      throw new CliError(`runtime bundle cannot contain symbolic links: ${relative}`, {
        code: "invalid_runtime_bundle",
      });
    }
    if (entry.isDirectory()) files.push(...await listBundleFiles(root, absolute));
    else if (entry.isFile()) files.push(relative);
    else {
      throw new CliError(`runtime bundle contains an unsupported file type: ${relative}`, {
        code: "invalid_runtime_bundle",
      });
    }
  }
  return files;
}

async function mapConcurrent(items, concurrency, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  const runners = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      results[index] = await worker(items[index], index);
    }
  });
  await Promise.all(runners);
  return results;
}

async function cleanupAbandonedInstalls(releases, { minimumAgeMs = 60 * 60 * 1000 } = {}) {
  const now = Date.now();
  for (const entry of await fs.readdir(releases, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.startsWith(".install-")) continue;
    const target = path.join(releases, entry.name);
    const stat = await fs.stat(target).catch(() => null);
    if (stat && now - stat.mtimeMs >= minimumAgeMs) {
      await fs.rm(target, { recursive: true, force: true }).catch(() => {});
    }
  }
}

export async function verifyRuntimeBundle(bundleRoot, manifest) {
  validateRuntimeManifest(manifest, bundleRoot);
  const actualFiles = await listBundleFiles(bundleRoot);
  const unlisted = actualFiles.filter((relative) => relative !== "manifest.json"
    && !Object.hasOwn(manifest.files, relative));
  if (unlisted.length) {
    throw new CliError("runtime bundle contains files missing from the checksum manifest", {
      code: "runtime_unlisted_file",
      details: { files: unlisted },
    });
  }
  const entries = Object.entries(manifest.files);
  const checked = await mapConcurrent(entries, 8, async ([relative, expected]) => {
    const file = resolveInside(bundleRoot, relative, `files.${relative}`);
    let actual;
    try {
      const stat = await fs.lstat(file);
      if (!stat.isFile() || stat.isSymbolicLink()) throw new Error("not a regular file");
      actual = await sha256(file);
    } catch (error) {
      throw new CliError(`runtime file is unavailable: ${relative}`, {
        code: "runtime_file_missing",
        details: { relative, reason: error?.message || String(error) },
      });
    }
    if (actual !== expected.toLowerCase()) {
      throw new CliError(`runtime checksum mismatch: ${relative}`, {
        code: "runtime_checksum_mismatch",
        details: { relative, expected: expected.toLowerCase(), actual },
      });
    }
    return relative;
  });
  return { status: "verified", release_version: manifest.release_version, files: checked };
}

export async function installRuntimeBundle(bundleRoot, paths) {
  const source = path.resolve(bundleRoot);
  const manifest = await readJson(path.join(source, "manifest.json"), null);
  if (!manifest) throw new CliError("runtime bundle manifest.json is missing", { code: "runtime_manifest_missing" });
  validateRuntimeManifest(manifest, source);
  const releases = path.join(paths.runtime, "releases");
  const target = path.join(releases, manifest.release_version);
  try {
    await fs.access(target);
    throw new CliError(`runtime ${manifest.release_version} is already installed`, {
      code: "runtime_already_installed",
      exitCode: 1,
    });
  } catch (error) {
    if (error instanceof CliError) throw error;
    if (error?.code !== "ENOENT") throw error;
  }
  await fs.mkdir(releases, { recursive: true, mode: 0o700 });
  await cleanupAbandonedInstalls(releases);
  const staging = path.join(releases, `.install-${manifest.release_version}-${randomUUID()}`);
  try {
    await fs.cp(source, staging, { recursive: true, errorOnExist: true, force: false });
    await verifyRuntimeBundle(staging, manifest);
    await fs.rename(staging, target);
  } finally {
    await fs.rm(staging, { recursive: true, force: true }).catch(() => {});
  }
  const active = {
    schema_version: 1,
    release_version: manifest.release_version,
    protocol_version: String(manifest.protocol_version || "1"),
    path: target,
    activated_at: new Date().toISOString(),
  };
  await writeJsonAtomic(path.join(paths.runtime, "active.json"), active);
  return { status: "installed", ...active };
}

export async function loadActiveRuntime(paths) {
  const active = await readJson(path.join(paths.runtime, "active.json"), null);
  if (!active) return null;
  const root = path.resolve(String(active.path || ""));
  const expectedRoot = path.resolve(path.join(paths.runtime, "releases"));
  if (!root.startsWith(`${expectedRoot}${path.sep}`)) {
    throw new CliError("active runtime path is outside the deploy home", { code: "invalid_runtime_state" });
  }
  const manifest = await readJson(path.join(root, "manifest.json"), null);
  if (!manifest || manifest.release_version !== active.release_version) {
    throw new CliError("active runtime manifest does not match active.json", { code: "invalid_runtime_state" });
  }
  await verifyRuntimeBundle(root, manifest);
  return { active, manifest, root };
}

export function resolveRuntimeProcess(activeRuntime, name, variables) {
  const spec = activeRuntime.manifest.processes[name];
  const interpolate = (value) => String(value).replace(/\$\{([A-Z0-9_]+)\}/g, (_, key) => {
    if (!Object.hasOwn(variables, key)) {
      throw new CliError(`runtime variable is not available: ${key}`, { code: "invalid_runtime_manifest" });
    }
    return String(variables[key]);
  });
  const cwd = resolveInside(activeRuntime.root, spec.cwd || ".", `processes.${name}.cwd`);
  const kind = spec.kind || "command";
  let command;
  let prefixArgs = [];
  if (kind === "command") {
    command = resolveInside(activeRuntime.root, spec.command, `processes.${name}.command`);
  } else if (kind === "node_script") {
    command = process.execPath;
    prefixArgs = [resolveInside(activeRuntime.root, spec.script, `processes.${name}.script`)];
  } else if (kind === "python_module") {
    command = String(variables.PYTHON_COMMAND || "");
    if (!path.isAbsolute(command)) {
      throw new CliError("managed runtime Python is not prepared", {
        code: "runtime_python_not_prepared",
        exitCode: 1,
      });
    }
    prefixArgs = ["-m", spec.module];
  }
  return {
    command,
    cwd,
    args: [...prefixArgs, ...(spec.args || []).map(interpolate)],
    env: Object.fromEntries(Object.entries(spec.env || {}).map(([key, value]) => [key, interpolate(value)])),
  };
}
