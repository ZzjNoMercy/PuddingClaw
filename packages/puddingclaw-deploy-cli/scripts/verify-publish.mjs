#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { verifyRuntimeBundle } from "../src/runtime-bundle.js";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(packageRoot, "../..");

async function main() {
  const packageDocument = JSON.parse(await fs.readFile(path.join(packageRoot, "package.json"), "utf8"));
  const failures = [];
  if (packageDocument.private) failures.push("package.json is still private");
  if (packageDocument.name.includes("-dev")) failures.push("development package name is not publishable");
  if (Object.keys(packageDocument.bin || {}).some((name) => name.endsWith("-dev"))) {
    failures.push("development CLI bin name is not publishable");
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
