import { CliError } from "./errors.js";
import { loadConfig, saveConfig } from "./config.js";
import { discoverCoreDatabase, validatePreparedInfrastructure } from "./init-discovery.js";
import { readSecret, writeSecret } from "./secrets.js";
import { readJson } from "./store.js";
import { probeManagedRuntimeState } from "./supervisor.js";

function publicCatalog(config) {
  const catalog = config.infrastructure?.catalog || {};
  return {
    mode: catalog.mode || "sqlite",
    source: catalog.source || "fallback",
    host: catalog.host || "",
    port: Number(catalog.port || 0),
    database: catalog.database || "",
    username: catalog.username || "",
    create_if_missing: Boolean(catalog.create_if_missing),
  };
}

async function requireConfig(paths) {
  const config = await loadConfig(paths.config);
  if (!config?.initialized) {
    throw new CliError("PuddingClaw is not initialized; run puddingclaw init", {
      code: "not_initialized",
      exitCode: 1,
    });
  }
  return config;
}

export async function databaseCommand(args, flags, paths) {
  const [action = "show"] = args;
  const config = await requireConfig(paths);
  if (action === "show") {
    return { status: "configured", database: publicCatalog(config) };
  }
  if (action !== "configure") {
    throw new CliError("usage: database show | database configure", {
      code: "argument_error",
    });
  }

  const existingDatabaseUrl = await readSecret(paths.databaseUrl);
  const previousCatalog = structuredClone(config.infrastructure.catalog);
  const discovered = await discoverCoreDatabase({
    profile: config.profile,
    flags,
    nonInteractive: Boolean(flags.non_interactive),
    home: paths.home,
    existingDatabaseUrl,
    existingCatalog: config.infrastructure?.catalog,
    reuseExistingDatabaseUrl: false,
  });
  if (discovered.catalog.mode === "sqlite" && !discovered.catalog.selection_explicit) {
    throw new CliError(
      discovered.catalog.fallback_reason || "数据库重配未完成；原配置未修改",
      {
        code: "database_reconfiguration_cancelled",
        exitCode: 1,
        details: { previous: publicCatalog(config) },
      },
    );
  }

  let validation = [];
  if (discovered.databaseUrl) {
    const prepared = await readJson(`${paths.runtime}/prepared.json`, null);
    if (!prepared?.python) {
      throw new CliError("Runtime Python is not prepared; run puddingclaw runtime prepare first", {
        code: "runtime_python_not_prepared",
        exitCode: 1,
      });
    }
    validation = validatePreparedInfrastructure({
      python: prepared.python,
      databaseUrl: discovered.databaseUrl,
      createDatabaseIfMissing: discovered.createDatabaseIfMissing,
      milvus: { enabled: false },
      requirePgvector: Boolean(config.extensions?.knowledge?.enabled),
    });
    const databaseProbe = validation.find((probe) => probe.probe === "database.connection");
    if (databaseProbe?.status !== "available") {
      throw new CliError(databaseProbe?.reason || "数据库连接验证失败；原配置未修改", {
        code: databaseProbe?.code || "database_validation_failed",
        exitCode: 1,
        details: { validation, previous: publicCatalog(config) },
      });
    }
    if (config.extensions?.knowledge?.enabled && databaseProbe.pgvector?.status !== "available") {
      throw new CliError(databaseProbe.pgvector?.reason || "知识库已启用，但目标数据库缺少 pgvector", {
        code: "pgvector_required",
        exitCode: 1,
        details: { validation, previous: publicCatalog(config) },
      });
    }
  }

  config.infrastructure.catalog = discovered.catalog;
  await saveConfig(paths.config, config);
  if (discovered.databaseUrl) {
    try {
      await writeSecret(paths.databaseUrl, discovered.databaseUrl);
    } catch (error) {
      config.infrastructure.catalog = previousCatalog;
      await saveConfig(paths.config, config);
      throw new CliError(`无法保存数据库凭据；已恢复原配置：${error?.message || String(error)}`, {
        code: "database_secret_write_failed",
        exitCode: 1,
      });
    }
  }

  const runtimeState = await readJson(paths.runtimeState, null);
  const instance = await probeManagedRuntimeState(paths, runtimeState);
  return {
    status: "updated",
    database: publicCatalog(config),
    validation,
    restart_required: instance.status === "running",
    next_command: instance.status === "running" ? "puddingclaw restart" : "puddingclaw start",
  };
}
