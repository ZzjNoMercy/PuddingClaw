import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { CliError } from "./errors.js";
import { probeUv } from "./probes.js";

export const MANAGED_UV_VERSION = "0.11.32";

export function managedUvInstaller(platform = process.platform) {
  const windows = platform === "win32";
  return {
    url: `https://astral.sh/uv/${MANAGED_UV_VERSION}/install.${windows ? "ps1" : "sh"}`,
    filename: windows ? "install.ps1" : "install.sh",
  };
}

function managedUvExecutable(installDir, platform = process.platform) {
  return path.join(installDir, platform === "win32" ? "uv.exe" : "uv");
}

async function runInstaller(script, installDir, { stderr = process.stderr } = {}) {
  const command = process.platform === "win32" ? "powershell.exe" : "sh";
  const args = process.platform === "win32"
    ? ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script]
    : [script];
  let child;
  try {
    child = spawn(command, args, {
      env: {
        ...process.env,
        UV_UNMANAGED_INSTALL: installDir,
        UV_NO_MODIFY_PATH: "1",
      },
      stdio: ["ignore", "ignore", "pipe"],
    });
    child.stderr.on("data", (chunk) => stderr.write(chunk));
  } catch (error) {
    throw new CliError(`could not start the official uv installer: ${error.message}`, {
      code: "uv_install_failed",
      exitCode: 1,
    });
  }
  const result = await Promise.race([
    once(child, "close").then(([code]) => ({ code })),
    once(child, "error").then(([error]) => ({ error })),
  ]);
  if (result.error) {
    throw new CliError(`could not start the official uv installer: ${result.error.message}`, {
      code: "uv_install_failed",
      exitCode: 1,
    });
  }
  const { code } = result;
  if (code !== 0) {
    throw new CliError(`official uv installer exited with code ${code}`, {
      code: "uv_install_failed",
      exitCode: 1,
    });
  }
}

export async function bootstrapUv(home, {
  fetchImpl = globalThis.fetch,
  stderr = process.stderr,
} = {}) {
  const installDir = path.join(home, "toolchains", "uv");
  const executable = managedUvExecutable(installDir);
  const existing = probeUv(executable);
  if (existing.status === "available") {
    return { status: "already_prepared", install_dir: installDir, selected: existing.selected };
  }
  const installer = managedUvInstaller();
  const downloadDir = path.join(home, "runtime", "downloads");
  const script = path.join(downloadDir, `uv-${MANAGED_UV_VERSION}-${installer.filename}`);
  await fs.mkdir(downloadDir, { recursive: true, mode: 0o700 });
  await fs.mkdir(installDir, { recursive: true, mode: 0o700 });
  try {
    let response;
    try {
      response = await fetchImpl(installer.url, {
        redirect: "follow",
        signal: AbortSignal.timeout(30_000),
      });
    } catch (error) {
      throw new CliError(`failed to download the official uv ${MANAGED_UV_VERSION} installer`, {
        code: "uv_download_failed",
        exitCode: 1,
        details: { url: installer.url, reason: error?.message || String(error) },
      });
    }
    if (!response.ok) {
      throw new CliError(`official uv installer download returned HTTP ${response.status}`, {
        code: "uv_download_failed",
        exitCode: 1,
        details: { url: installer.url, status: response.status },
      });
    }
    const body = Buffer.from(await response.arrayBuffer());
    if (body.length < 100 || body.length > 2 * 1024 * 1024) {
      throw new CliError("official uv installer has an unexpected size", {
        code: "uv_download_failed",
        exitCode: 1,
        details: { url: installer.url, bytes: body.length },
      });
    }
    await fs.writeFile(script, body, { mode: 0o600 });
    await runInstaller(script, installDir, { stderr });
  } finally {
    await fs.rm(script, { force: true }).catch(() => {});
  }
  const installed = probeUv(executable);
  if (installed.status !== "available" || installed.selected.version !== MANAGED_UV_VERSION) {
    throw new CliError(`uv ${MANAGED_UV_VERSION} was not installed at the expected location`, {
      code: "uv_install_failed",
      exitCode: 1,
      details: { executable },
    });
  }
  return {
    status: "prepared",
    install_dir: installDir,
    source: installer.url,
    selected: { ...installed.selected, managed: true },
  };
}
