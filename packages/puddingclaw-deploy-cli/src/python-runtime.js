import fs from "node:fs/promises";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { CliError } from "./errors.js";
import { probeUv } from "./probes.js";

export async function prepareManagedPython(home, {
  version = "3.12",
  uvCommand = "",
  stderr = process.stderr,
} = {}) {
  const uv = probeUv(uvCommand);
  if (uv.status !== "available") {
    throw new CliError(
      "uv is required for one-click Python preparation",
      { code: "uv_required", exitCode: 1 },
    );
  }
  const installDir = path.join(home, "toolchains", "python");
  await fs.mkdir(installDir, { recursive: true, mode: 0o700 });
  const child = spawn(uv.selected.command, [
    "python", "install", version,
    "--install-dir", installDir,
    "--no-bin",
    "--no-registry",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  child.stderr.on("data", (chunk) => stderr.write(chunk));
  const [code] = await once(child, "close");
  if (code !== 0) {
    throw new CliError(`uv failed to prepare Python ${version}`, {
      code: "python_prepare_failed",
      exitCode: 1,
    });
  }
  const found = spawnSync(uv.selected.command, ["python", "find", version, "--managed-python"], {
    encoding: "utf8",
    timeout: 5000,
    env: { ...process.env, UV_PYTHON_INSTALL_DIR: installDir },
  });
  const executable = found.status === 0 ? String(found.stdout || "").trim() : "";
  if (!executable || !path.isAbsolute(executable)) {
    throw new CliError(`Python ${version} was installed but its executable could not be resolved`, {
      code: "python_resolve_failed",
      exitCode: 1,
    });
  }
  return {
    status: "prepared",
    version,
    install_dir: installDir,
    selected: { command: executable, version, supported: true, managed: true },
  };
}
