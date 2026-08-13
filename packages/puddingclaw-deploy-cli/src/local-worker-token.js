import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { CliError } from "./errors.js";

function validateToken(value) {
  const token = String(value || "").trim();
  if (!/^pck_[A-Za-z0-9_-]{32,}$/.test(token)) {
    throw new CliError("local Worker token is invalid", {
      code: "local_worker_token_invalid",
      exitCode: 1,
    });
  }
  return token;
}

async function assertPrivateRegularFile(file) {
  const stat = await fs.lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new CliError("local Worker token is not a regular file", {
      code: "local_worker_token_invalid",
      exitCode: 1,
    });
  }
  if (process.platform !== "win32" && (stat.mode & 0o077) !== 0) {
    throw new CliError("local Worker token permissions must be 0600", {
      code: "local_worker_token_permissions",
      exitCode: 1,
    });
  }
}

export async function readLocalWorkerToken(paths) {
  try {
    await assertPrivateRegularFile(paths.workerToken);
    return validateToken(await fs.readFile(paths.workerToken, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

export async function ensureLocalWorkerToken(paths) {
  const existing = await readLocalWorkerToken(paths);
  if (existing) return existing;
  const directory = path.dirname(paths.workerToken);
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  if (process.platform !== "win32") await fs.chmod(directory, 0o700);
  const temporary = path.join(directory, `.headless-token.${randomUUID()}.tmp`);
  const token = `pck_${crypto.randomBytes(32).toString("base64url")}`;
  try {
    await fs.writeFile(temporary, `${token}\n`, { mode: 0o600, flag: "wx" });
    await fs.rename(temporary, paths.workerToken);
    if (process.platform !== "win32") await fs.chmod(paths.workerToken, 0o600);
  } finally {
    await fs.rm(temporary, { force: true }).catch(() => {});
  }
  return token;
}
