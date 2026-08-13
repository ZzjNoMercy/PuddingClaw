import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";

export async function readJson(file, fallback = null) {
  try {
    const raw = await fs.readFile(file, "utf8");
    return JSON.parse(raw);
  } catch (error) {
    if (error?.code === "ENOENT") return fallback;
    throw error;
  }
}

export async function writeJsonAtomic(file, value, { mode = 0o600 } = {}) {
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = path.join(path.dirname(file), `.${path.basename(file)}.${randomUUID()}.tmp`);
  try {
    await fs.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode });
    await fs.rename(temporary, file);
    if (process.platform !== "win32") await fs.chmod(file, mode);
  } finally {
    await fs.rm(temporary, { force: true }).catch(() => {});
  }
}
