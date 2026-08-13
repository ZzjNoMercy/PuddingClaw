import { CliError, assertArgument } from "./errors.js";
import { readJson, writeJsonAtomic } from "./store.js";

export const PROFILES = Object.freeze({
  harness: { knowledge: false, analytics: false, headless_worker: true },
  knowledge: { knowledge: true, analytics: false, headless_worker: true },
  analytics: { knowledge: false, analytics: true, headless_worker: true },
  full: { knowledge: true, analytics: true, headless_worker: true },
});

export const EXTENSIONS = Object.freeze(["knowledge", "analytics", "headless_worker"]);
export const DEFAULT_BACKEND_PORT = 8888;
export const DEFAULT_FRONTEND_PORT = 3000;

export function defaultConfig({
  profile = "harness",
  backendPort = DEFAULT_BACKEND_PORT,
  frontendPort = DEFAULT_FRONTEND_PORT,
} = {}) {
  assertArgument(Object.hasOwn(PROFILES, profile), `unknown profile: ${profile}`);
  const selected = PROFILES[profile];
  return {
    schema_version: 1,
    initialized: false,
    profile,
    release: { channel: "stable" },
    server: {
      host: "127.0.0.1",
      backend_port: backendPort,
      frontend_port: frontendPort,
      port_conflict: "ask",
      auto_open: false,
    },
    extensions: Object.fromEntries(
      EXTENSIONS.map((name) => [name, { enabled: selected[name] }]),
    ),
    provider: {
      status: "unconfigured",
      id: "",
      name: "",
      protocol: "openai_compatible",
      base_url: "",
      model: "",
    },
    multimodal_provider: {
      status: "unconfigured",
      id: "",
      name: "",
      protocol: "openai_compatible",
      base_url: "",
      model: "",
      reuse_primary_credential: false,
    },
    infrastructure: {
      catalog: {
        mode: "sqlite",
        preferred_mode: "postgresql",
        fallback_mode: "sqlite",
        source: "fallback",
        host: "",
        port: 0,
        database: "",
        probe_status: "skipped",
      },
      milvus: { enabled: false, uri: "http://127.0.0.1:19530", probe_status: "skipped" },
      embedding: { status: "disabled", provider: "", model: "" },
      mineru: { enabled: false, base_url: "http://127.0.0.1:8002", probe_status: "skipped" },
    },
    harness: {
      sandbox_mode: "auto",
      model_call_limit: { enabled: true, run_limit: 50, exit_behavior: "end" },
      goals: { enabled: true, max_rounds: 8 },
    },
  };
}

export async function loadConfig(file) {
  const config = await readJson(file, null);
  if (!config) return null;
  const standardProfile = Object.hasOwn(PROFILES, config.profile);
  const profile = standardProfile ? config.profile : "harness";
  const defaults = defaultConfig({
    profile,
    backendPort: config.server?.backend_port || DEFAULT_BACKEND_PORT,
    frontendPort: config.server?.frontend_port || DEFAULT_FRONTEND_PORT,
  });
  const normalized = {
    ...defaults,
    ...config,
    server: { ...defaults.server, ...config.server },
    extensions: Object.fromEntries(
      EXTENSIONS.map((name) => [
        name,
        { ...defaults.extensions[name], ...config.extensions?.[name] },
      ]),
    ),
    provider: { ...defaults.provider, ...config.provider },
    multimodal_provider: { ...defaults.multimodal_provider, ...config.multimodal_provider },
    infrastructure: {
      ...defaults.infrastructure,
      ...config.infrastructure,
      catalog: { ...defaults.infrastructure.catalog, ...config.infrastructure?.catalog },
      milvus: { ...defaults.infrastructure.milvus, ...config.infrastructure?.milvus },
      embedding: { ...defaults.infrastructure.embedding, ...config.infrastructure?.embedding },
      mineru: { ...defaults.infrastructure.mineru, ...config.infrastructure?.mineru },
    },
    harness: { ...defaults.harness, ...config.harness },
  };
  // 0.1.2-0.1.7 incorrectly modeled Worker/Headless as a Full-only
  // extension. It is part of Harness Core in every standard profile.
  if (standardProfile) normalized.extensions.headless_worker.enabled = true;
  return normalized;
}

export async function saveConfig(file, config) {
  validateConfig(config);
  await writeJsonAtomic(file, config);
}

export function validateConfig(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new CliError("config must be a JSON object", { code: "configuration_error" });
  }
  if (config.schema_version !== 1) {
    throw new CliError("unsupported config schema_version", { code: "configuration_error" });
  }
  for (const key of ["backend_port", "frontend_port"]) {
    const value = config.server?.[key];
    if (!Number.isInteger(value) || value < 1 || value > 65535) {
      throw new CliError(`server.${key} must be an integer between 1 and 65535`, {
        code: "configuration_error",
      });
    }
  }
  if (config.server.backend_port === config.server.frontend_port) {
    throw new CliError("server backend and frontend ports must differ", {
      code: "configuration_error",
    });
  }
  if (!["127.0.0.1", "::1", "localhost"].includes(config.server.host)) {
    throw new CliError("server.host must be a loopback address in this release", {
      code: "configuration_error",
    });
  }
  if (!["ask", "error", "auto"].includes(config.server.port_conflict)) {
    throw new CliError("server.port_conflict must be ask, error, or auto", {
      code: "configuration_error",
    });
  }
  if (typeof config.server.auto_open !== "boolean") {
    throw new CliError("server.auto_open must be boolean", { code: "configuration_error" });
  }
  for (const extension of EXTENSIONS) {
    if (typeof config.extensions?.[extension]?.enabled !== "boolean") {
      throw new CliError(`extensions.${extension}.enabled must be boolean`, {
        code: "configuration_error",
      });
    }
  }
  if (!config.provider || !["unconfigured", "configured", "needs_action"].includes(config.provider.status)) {
    throw new CliError("provider.status must be unconfigured, configured, or needs_action", {
      code: "configuration_error",
    });
  }
  if (!config.multimodal_provider
      || !["unconfigured", "configured", "needs_action"].includes(config.multimodal_provider.status)) {
    throw new CliError(
      "multimodal_provider.status must be unconfigured, configured, or needs_action",
      { code: "configuration_error" },
    );
  }
  if (typeof config.multimodal_provider.reuse_primary_credential !== "boolean") {
    throw new CliError("multimodal_provider.reuse_primary_credential must be boolean", {
      code: "configuration_error",
    });
  }
  if (!config.infrastructure || !["sqlite", "postgresql"].includes(config.infrastructure.catalog?.mode)) {
    throw new CliError("infrastructure.catalog.mode must be sqlite or postgresql", {
      code: "configuration_error",
    });
  }
  if (typeof config.infrastructure.milvus?.enabled !== "boolean") {
    throw new CliError("infrastructure.milvus.enabled must be boolean", { code: "configuration_error" });
  }
  if (!config.harness || !["auto", "spawn", "kernel"].includes(config.harness.sandbox_mode)) {
    throw new CliError("harness.sandbox_mode must be auto, spawn, or kernel", {
      code: "configuration_error",
    });
  }
}

export function getConfigValue(config, dottedPath) {
  assertArgument(Boolean(dottedPath), "config key is required");
  let current = config;
  for (const segment of dottedPath.split(".")) {
    if (!current || typeof current !== "object" || !Object.hasOwn(current, segment)) {
      throw new CliError(`unknown config key: ${dottedPath}`, { code: "configuration_error" });
    }
    current = current[segment];
  }
  return current;
}

export function setConfigValue(config, dottedPath, value) {
  assertArgument(Boolean(dottedPath), "config key is required");
  if (/(api[_-]?key|token|password|secret|credential)/i.test(dottedPath)) {
    throw new CliError("secrets cannot be written with config set", { code: "secret_rejected" });
  }
  const segments = dottedPath.split(".");
  let current = config;
  for (const segment of segments.slice(0, -1)) {
    if (!current?.[segment] || typeof current[segment] !== "object") {
      throw new CliError(`unknown config key: ${dottedPath}`, { code: "configuration_error" });
    }
    current = current[segment];
  }
  const leaf = segments.at(-1);
  if (!Object.hasOwn(current, leaf)) {
    throw new CliError(`unknown config key: ${dottedPath}`, { code: "configuration_error" });
  }
  current[leaf] = value;
  validateConfig(config);
  return config;
}

export function applyExtension(config, name, enabled) {
  assertArgument(EXTENSIONS.includes(name), `unknown extension: ${name}`);
  config.extensions[name].enabled = enabled;
  config.profile = Object.entries(PROFILES).find(([, expected]) =>
    EXTENSIONS.every((item) => config.extensions[item].enabled === expected[item]))?.[0] || "custom";
  return config;
}
