import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";

export async function writeSecret(file, value) {
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = path.join(path.dirname(file), `.${path.basename(file)}.${randomUUID()}.tmp`);
  try {
    await fs.writeFile(temporary, `${String(value).trim()}\n`, { mode: 0o600 });
    await fs.rename(temporary, file);
    if (process.platform !== "win32") await fs.chmod(file, 0o600);
  } finally {
    await fs.rm(temporary, { force: true }).catch(() => {});
  }
}

export async function readSecret(file) {
  try {
    return (await fs.readFile(file, "utf8")).trim();
  } catch (error) {
    if (error?.code === "ENOENT") return "";
    throw error;
  }
}
