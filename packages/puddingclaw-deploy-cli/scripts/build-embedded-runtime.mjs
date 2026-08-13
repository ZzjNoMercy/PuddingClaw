#!/usr/bin/env node

import crypto from "node:crypto";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const packageRoot = path.resolve(scriptDirectory, "..");
const repositoryRoot = path.resolve(packageRoot, "../..");
const backendRoot = path.join(repositoryRoot, "backend");
const frontendRoot = path.join(repositoryRoot, "frontend");

function parseArguments(argv) {
  const options = {
    output: path.join(packageRoot, "runtime-bundle"),
    skipBuild: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--skip-build") options.skipBuild = true;
    else if (value === "--output") options.output = path.resolve(argv[++index] || "");
    else throw new Error(`unknown argument: ${value}`);
  }
  if (!path.basename(options.output).startsWith("runtime-bundle")) {
    throw new Error("runtime output directory name must start with runtime-bundle");
  }
  return options;
}

async function run(command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: options.cwd,
    env: { ...process.env, ...options.env },
    stdio: "inherit",
  });
  const code = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("close", resolve);
  });
  if (code !== 0) throw new Error(`${command} exited with code ${code}`);
}

async function findWheel(directory) {
  const wheels = (await fs.readdir(directory))
    .filter((name) => /^puddingclaw_backend-.*\.whl$/.test(name))
    .sort();
  if (!wheels.length) throw new Error(`PuddingClaw Backend wheel not found in ${directory}`);
  return path.join(directory, wheels.at(-1));
}

async function removeEnvironmentFiles(root) {
  for (const entry of await fs.readdir(root, { withFileTypes: true })) {
    const absolute = path.join(root, entry.name);
    if (entry.isDirectory()) await removeEnvironmentFiles(absolute);
    else if (entry.name === ".env" || entry.name.startsWith(".env.")) await fs.rm(absolute, { force: true });
  }
}

async function assertNoSensitiveFiles(root) {
  const rejected = [];
  async function visit(directory) {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      const relative = path.relative(root, absolute).split(path.sep).join("/");
      if (entry.isSymbolicLink()) rejected.push(`${relative} (symbolic link)`);
      else if (entry.isDirectory()) await visit(absolute);
      else if (
        entry.name === ".npmrc"
        || entry.name === ".pypirc"
        || entry.name.endsWith(".pem")
        || entry.name.endsWith(".key")
        || entry.name === ".env"
        || entry.name.startsWith(".env.")
      ) rejected.push(relative);
    }
  }
  await visit(root);
  if (rejected.length) throw new Error(`runtime contains forbidden files: ${rejected.join(", ")}`);
}

async function sanitizeBuildPaths(root) {
  const replacements = [
    [frontendRoot, "/__puddingclaw_build__/frontend"],
    [repositoryRoot, "/__puddingclaw_build__"],
    [frontendRoot.replaceAll("\\", "\\\\"), "/__puddingclaw_build__/frontend"],
    [repositoryRoot.replaceAll("\\", "\\\\"), "/__puddingclaw_build__"],
  ].sort((left, right) => right[0].length - left[0].length);
  for (const relative of await listFiles(root)) {
    const file = path.join(root, relative);
    let content = await fs.readFile(file);
    let encoded = content.toString("latin1");
    let changed = false;
    for (const [search, replacement] of replacements) {
      if (!search || !encoded.includes(search)) continue;
      encoded = encoded.replaceAll(search, replacement);
      changed = true;
    }
    if (changed) await fs.writeFile(file, Buffer.from(encoded, "latin1"));
  }
  const leaks = [];
  for (const relative of await listFiles(root)) {
    const content = (await fs.readFile(path.join(root, relative))).toString("latin1");
    if (content.includes(repositoryRoot) || content.includes(frontendRoot)) leaks.push(relative);
  }
  if (leaks.length) throw new Error(`runtime contains local build paths: ${leaks.join(", ")}`);
}

async function patchStandaloneServer(serverFile) {
  const marker = "process.env.__NEXT_PRIVATE_STANDALONE_CONFIG = JSON.stringify(nextConfig)";
  const source = await fs.readFile(serverFile, "utf8");
  if (!source.includes(marker)) throw new Error("Next standalone server marker was not found");
  const runtimeRewrite = [
    "const runtimeBackendUrl = (process.env.BACKEND_INTERNAL_URL || 'http://127.0.0.1:8888').replace(/\\/$/, '')",
    "for (const rule of nextConfig?._originalRewrites?.afterFiles || []) {",
    "  if (typeof rule.destination === 'string' && rule.source.startsWith('/api/')) {",
    "    rule.destination = rule.destination.replace(/^https?:\\/\\/[^/]+/, runtimeBackendUrl)",
    "  }",
    "}",
    marker,
  ].join("\n");
  await fs.writeFile(serverFile, source.replace(marker, runtimeRewrite));
}

async function listFiles(root, directory = root) {
  const files = [];
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await listFiles(root, absolute));
    else if (entry.isFile()) files.push(path.relative(root, absolute).split(path.sep).join("/"));
  }
  return files.sort();
}

async function sha256(file) {
  const hash = crypto.createHash("sha256");
  const content = await fs.readFile(file);
  hash.update(content);
  return hash.digest("hex");
}

async function writeManifest(staging, version, { wheelName, requirementsNames, requireHashes }) {
  const files = {};
  for (const relative of await listFiles(staging)) {
    if (relative === "manifest.json") continue;
    files[relative] = await sha256(path.join(staging, relative));
  }
  const manifest = {
    schema_version: 1,
    release_version: version,
    protocol_version: "1",
    contracts: { puddingclaw_home: 1, dynamic_ports: 1, extensions: 1 },
    install: {
      python: {
        wheel: `backend/${wheelName}`,
        requirements: `backend/${requirementsNames.harness}`,
        requirements_by_profile: Object.fromEntries(
          Object.entries(requirementsNames).map(([profile, name]) => [profile, `backend/${name}`]),
        ),
        require_hashes: requireHashes,
      },
    },
    files,
    processes: {
      backend: {
        kind: "python_module",
        module: "uvicorn",
        args: ["app:app", "--host", "127.0.0.1", "--port", "${BACKEND_PORT}"],
        cwd: "backend",
        health_path: "/",
      },
      frontend: {
        kind: "node_script",
        script: "web/server.js",
        args: [],
        cwd: "web",
        health_path: "/",
      },
    },
  };
  await fs.writeFile(path.join(staging, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o600 });
  return manifest;
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  const packageDocument = JSON.parse(await fs.readFile(path.join(packageRoot, "package.json"), "utf8"));
  const temporaryRoot = await fs.mkdtemp(path.join(os.tmpdir(), "puddingclaw-runtime-build-"));
  const staging = path.join(path.dirname(options.output), `.runtime-bundle-${crypto.randomUUID()}`);
  let frontendBuild = path.join(frontendRoot, ".next-build");
  let wheelDirectory = path.join(backendRoot, "dist");
  let requirementsSources = Object.fromEntries(
    ["harness", "knowledge", "analytics", "full"].map((profile) => [
      profile,
      path.join(backendRoot, "requirements.txt"),
    ]),
  );
  let requireHashes = false;
  const frontendTsconfig = path.join(frontendRoot, "tsconfig.json");
  let originalFrontendTsconfig;
  try {
    if (!options.skipBuild) {
      wheelDirectory = path.join(temporaryRoot, "wheel");
      await fs.mkdir(wheelDirectory, { recursive: true });
      await run("uv", ["build", "--wheel", "--out-dir", wheelDirectory], { cwd: backendRoot });
      const exportExtras = {
        harness: [],
        knowledge: ["--extra", "knowledge"],
        analytics: ["--extra", "analytics"],
        full: ["--extra", "knowledge", "--extra", "analytics"],
      };
      requirementsSources = {};
      for (const [profile, extras] of Object.entries(exportExtras)) {
        const destination = path.join(temporaryRoot, `requirements-${profile}.lock`);
        await run("uv", [
          "export", "--quiet", "--frozen", "--format", "requirements.txt", "--no-dev", "--no-emit-project",
          ...extras,
          "--output-file", destination,
        ], { cwd: backendRoot });
        const exportedRequirements = await fs.readFile(destination, "utf8");
        if (!exportedRequirements.includes("--hash=sha256:")) {
          throw new Error(`uv export did not produce a hash-locked ${profile} requirements file`);
        }
        requirementsSources[profile] = destination;
      }
      requireHashes = true;
      frontendBuild = path.join(frontendRoot, ".next-runtime-build");
      originalFrontendTsconfig = await fs.readFile(frontendTsconfig);
      await run("npm", ["run", "build"], {
        cwd: frontendRoot,
        env: { NEXT_DIST_DIR: ".next-runtime-build", BACKEND_INTERNAL_URL: "http://127.0.0.1:8888" },
      });
    }

    const wheel = await findWheel(wheelDirectory);
    const standalone = path.join(frontendBuild, "standalone");
    await fs.access(path.join(standalone, "server.js"));
    await fs.mkdir(path.join(staging, "backend"), { recursive: true, mode: 0o700 });
    await fs.cp(wheel, path.join(staging, "backend", path.basename(wheel)));
    const requirementsNames = {};
    for (const [profile, source] of Object.entries(requirementsSources)) {
      const name = options.skipBuild ? `requirements-${profile}.txt` : `requirements-${profile}.lock`;
      await fs.cp(source, path.join(staging, "backend", name));
      requirementsNames[profile] = name;
    }

    const web = path.join(staging, "web");
    await fs.cp(standalone, web, { recursive: true });
    const staticSource = path.join(frontendBuild, "static");
    try {
      await fs.cp(staticSource, path.join(web, path.basename(frontendBuild), "static"), { recursive: true });
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    try {
      await fs.cp(path.join(frontendRoot, "public"), path.join(web, "public"), { recursive: true });
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    await removeEnvironmentFiles(web);
    await patchStandaloneServer(path.join(web, "server.js"));
    await sanitizeBuildPaths(web);
    await assertNoSensitiveFiles(staging);
    const manifest = await writeManifest(staging, packageDocument.version, {
      wheelName: path.basename(wheel),
      requirementsNames,
      requireHashes,
    });

    const previous = `${options.output}.previous`;
    await fs.rm(previous, { recursive: true, force: true });
    try { await fs.rename(options.output, previous); } catch (error) { if (error?.code !== "ENOENT") throw error; }
    await fs.rename(staging, options.output);
    await fs.rm(previous, { recursive: true, force: true });
    process.stdout.write(`${JSON.stringify({
      status: "built",
      output: options.output,
      release_version: manifest.release_version,
      files: Object.keys(manifest.files).length,
      require_hashes: requireHashes,
    })}\n`);
  } finally {
    await fs.rm(staging, { recursive: true, force: true }).catch(() => {});
    await fs.rm(temporaryRoot, { recursive: true, force: true }).catch(() => {});
    if (!options.skipBuild) {
      await fs.rm(path.join(frontendRoot, ".next-runtime-build"), { recursive: true, force: true }).catch(() => {});
      if (originalFrontendTsconfig) {
        await fs.writeFile(frontendTsconfig, originalFrontendTsconfig);
      }
    }
  }
}

main().catch((error) => {
  process.stderr.write(`${error?.stack || error}\n`);
  process.exitCode = 1;
});
