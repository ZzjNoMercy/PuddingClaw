import fs from "node:fs/promises";
import path from "node:path";

export async function writeSecret(file, value) {
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  await fs.writeFile(file, `${String(value).trim()}\n`, { mode: 0o600 });
  if (process.platform !== "win32") await fs.chmod(file, 0o600);
}

export async function readSecret(file) {
  try {
    return (await fs.readFile(file, "utf8")).trim();
  } catch (error) {
    if (error?.code === "ENOENT") return "";
    throw error;
  }
}
