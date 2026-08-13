#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { verifyRuntimeBundle } from "../src/runtime-bundle.js";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(packageRoot, "../..");
const execFileAsync = promisify(execFile);

async function main() {
  const packageDocument = JSON.parse(await fs.readFile(path.join(packageRoot, "package.json"), "utf8"));
  const failures = [];
  if (packageDocument.private) failures.push("package.json is still private");
  if (packageDocument.name.includes("-dev")) failures.push("development package name is not publishable");
  if (Object.keys(packageDocument.bin || {}).some((name) => name.endsWith("-dev"))) {
    failures.push("development CLI bin name is not publishable");
  }
  try {
    const cliEntry = path.join(packageRoot, packageDocument.bin.puddingclaw);
    const cliSource = await fs.readFile(cliEntry, "utf8");
    if (/\bVERSION\s*=\s*["']\d+\.\d+\.\d+/.test(cliSource)) {
      failures.push("CLI version must not be hard-coded outside package.json");
    }
    const { stdout } = await execFileAsync(process.execPath, [cliEntry, "version", "--json"]);
    const cliVersion = JSON.parse(stdout).cli_version;
    if (cliVersion !== packageDocument.version) {
      failures.push(`CLI reports ${cliVersion}, package.json reports ${packageDocument.version}`);
    }
    const { stdout: capabilitiesStdout } = await execFileAsync(
      process.execPath,
      [cliEntry, "agent", "capabilities", "--json"],
    );
    const capabilities = JSON.parse(capabilitiesStdout);
    const workerManifest = JSON.parse(await fs.readFile(path.join(packageRoot, "worker.manifest.json"), "utf8"));
    if (JSON.stringify(capabilities.capabilities) !== JSON.stringify(workerManifest.capabilities)) {
      failures.push("CLI capabilities differ from worker.manifest.json");
    }
  } catch (error) {
    failures.push(`CLI version assertion failed: ${error.message}`);
  }
  try { await fs.access(path.join(repositoryRoot, "LICENSE")); } catch { failures.push("root LICENSE is missing"); }
  const runtimeRoot = path.join(packageRoot, "runtime-bundle");
  let manifest;
  try {
    manifest = JSON.parse(await fs.readFile(path.join(runtimeRoot, "manifest.json"), "utf8"));
    await verifyRuntimeBundle(runtimeRoot, manifest);
  } catch (error) {
    failures.push(`embedded runtime is invalid: ${error.message}`);
  }
  if (manifest?.release_version !== packageDocument.version) {
    failures.push("npm package and runtime versions differ");
  }
  if (manifest?.install?.python?.require_hashes !== true) {
    failures.push("production Python dependency lock must require hashes");
  }
  const harnessRequirements = manifest?.install?.python?.requirements_by_profile?.harness;
  if (harnessRequirements) {
    try {
      const content = await fs.readFile(path.join(runtimeRoot, harnessRequirements), "utf8");
      if (!/^asyncpg==/m.test(content)) {
        failures.push("Harness dependency lock must include asyncpg for the core PostgreSQL catalog");
      }
    } catch (error) {
      failures.push(`Harness dependency lock cannot be read: ${error.message}`);
    }
  } else {
    failures.push("Harness dependency lock is missing from the runtime manifest");
  }
  if (failures.length) {
    throw new Error(`publish verification failed:\n- ${failures.join("\n- ")}`);
  }
  process.stdout.write(`${JSON.stringify({
    status: "publish_ready",
    package: packageDocument.name,
    version: packageDocument.version,
    runtime_files: Object.keys(manifest.files).length,
  })}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
