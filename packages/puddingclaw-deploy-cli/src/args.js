import { CliError } from "./errors.js";

export function parseArgs(argv) {
  const positionals = [];
  const flags = {};
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) {
      positionals.push(token);
      continue;
    }
    const [rawName, inline] = token.slice(2).split("=", 2);
    const name = rawName.replaceAll("-", "_");
    if ([
      "json", "jsonl", "help", "non_interactive", "force", "prepare_python", "install_runtime", "yes", "plan",
      "database_create_if_missing",
    ].includes(name)) {
      flags[name] = inline === undefined ? true : inline !== "false";
      continue;
    }
    const value = inline === undefined ? argv[++index] : inline;
    if (value === undefined || value.startsWith("--")) {
      throw new CliError(`--${rawName} requires a value`, { code: "argument_error" });
    }
    flags[name] = value;
  }
  return { positionals, flags };
}

export function integerFlag(value, label) {
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new CliError(`${label} must be an integer between 1 and 65535`, {
      code: "argument_error",
    });
  }
  return parsed;
}

export function parseConfigValue(raw) {
  if (raw === undefined) throw new CliError("config value is required", { code: "argument_error" });
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}
