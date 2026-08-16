import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { CliError } from "./errors.js";
import { loadConfig, saveConfig } from "./config.js";
import { discoverCoreDatabase, validatePreparedInfrastructure } from "./init-discovery.js";
import { probeTcpEndpoint } from "./probes.js";
import { loadActiveRuntime } from "./runtime-bundle.js";
import { readSecret, writeSecret } from "./secrets.js";
import { readJson } from "./store.js";
import { probeManagedRuntimeState } from "./supervisor.js";
import { writeError } from "./output.js";

const SWITCH_WARNING = "切换数据库提供方将产生空 Catalog：原有数据不会自动迁移，请使用迁移流程完成数据搬迁。";

function catalogProvider(catalog) {
  // New-schema provider wins; legacy catalogs only carry mode.
  return ["sqlite", "postgresql"].includes(catalog?.provider)
    ? catalog.provider
    : catalog?.mode === "postgresql" ? "postgresql" : "sqlite";
}

function publicCatalog(config) {
  const catalog = config.infrastructure?.catalog || {};
  return {
    mode: catalog.mode || "sqlite",
    provider: catalogProvider(catalog),
    source: catalog.source || "local_file",
    host: catalog.host || "",
    port: Number(catalog.port || 0),
    database: catalog.database || "",
    username: catalog.username || "",
    create_if_missing: Boolean(catalog.create_if_missing),
  };
}

async function sourceCatalogMightHaveData(provider, existingDatabaseUrl, home) {
  // Best-effort guard input: SQLite checks the catalog file, PostgreSQL
  // checks endpoint reachability. An unreachable/unknown source is treated
  // as empty so a fresh install never blocks on a dead endpoint.
  if (provider === "sqlite") {
    try {
      const stat = await fs.stat(path.join(home, "db", "catalog.sqlite3"));
      return stat.size > 0;
    } catch {
      return false;
    }
  }
  if (!existingDatabaseUrl) return false;
  let metadata;
  try {
    metadata = new URL(existingDatabaseUrl.replace("postgresql+asyncpg://", "postgresql://"));
  } catch {
    return false;
  }
  const probe = await probeTcpEndpoint({
    probe: "database.postgresql.switch_guard",
    host: metadata.hostname || "127.0.0.1",
    port: Number(metadata.port || 5432),
    required: false,
  });
  return probe.status === "available";
}

async function confirmProviderSwitch() {
  const rl = createInterface({ input, output });
  try {
    const answer = String(await rl.question("确认切换并放弃自动迁移？[y/N] ")).trim().toLowerCase();
    return answer === "y" || answer === "yes";
  } finally {
    rl.close();
  }
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

const MIGRATE_USAGE = "usage: database migrate sqlite-to-postgres --url <pg-url> | database migrate postgres-to-sqlite [--target-path <path>]";

async function resolveBackendMigrationRuntime(paths, config) {
  // Backend 运行时 Python 定位与 runtime prepare 一致：优先 prepared venv，
  // 回退到 init 选择的 python。
  const prepared = await readJson(`${paths.runtime}/prepared.json`, null);
  const python = prepared?.python || config.runtime?.python?.command;
  if (!python) {
    throw new CliError("Runtime Python is not prepared; run puddingclaw runtime prepare first", {
      code: "runtime_python_not_prepared",
      exitCode: 1,
    });
  }
  const active = await loadActiveRuntime(paths);
  if (!active) {
    throw new CliError("no PuddingClaw runtime is installed; install a verified runtime bundle first", {
      code: "runtime_not_installed",
      exitCode: 1,
    });
  }
  return { python, cwd: path.join(active.root, "backend") };
}

export async function databaseMigrateCommand(args, flags, paths, config) {
  const [direction] = args;
  if (!["sqlite-to-postgres", "postgres-to-sqlite"].includes(direction)) {
    throw new CliError(MIGRATE_USAGE, { code: "argument_error" });
  }
  if (direction === "sqlite-to-postgres" && !flags.url) {
    throw new CliError("database migrate sqlite-to-postgres requires --url <postgresql-url>", {
      code: "argument_error",
    });
  }
  // 前置检查：Backend 运行中会持有连接并绕过 drain 协议写库，必须先停止。
  const runtimeState = await readJson(paths.runtimeState, null);
  const instance = await probeManagedRuntimeState(paths, runtimeState);
  if (instance.status === "running") {
    throw new CliError("Backend 正在运行；请先执行 puddingclaw stop，再运行数据库迁移", {
      code: "backend_running",
      exitCode: 1,
    });
  }
  const { python, cwd } = await resolveBackendMigrationRuntime(paths, config);
  const moduleArgs = direction === "sqlite-to-postgres"
    ? ["sqlite-to-pg", "--target-url", String(flags.url)]
    : ["pg-to-sqlite", ...(flags.target_path ? ["--target-path", String(flags.target_path)] : [])];
  if (flags.skip_drain) moduleArgs.push("--skip-drain");
  if (flags.drain_timeout !== undefined) moduleArgs.push("--drain-timeout", String(flags.drain_timeout));
  // 输出透传：迁移报告与中文诊断直接写到用户终端。
  const child = spawn(python, ["-m", "catalog_migration", ...moduleArgs], {
    cwd,
    env: { ...process.env, PUDDINGCLAW_HOME: paths.home },
    stdio: "inherit",
  });
  const [code] = await once(child, "close");
  if (code !== 0) {
    throw new CliError(`catalog migration failed (exit code ${code})`, {
      code: "catalog_migration_failed",
      exitCode: Number.isInteger(code) && code > 0 ? code : 1,
    });
  }
  return { status: "migrated", direction, next_command: "puddingclaw start" };
}

export async function databaseCommand(args, flags, paths) {
  const [action = "show"] = args;
  const config = await requireConfig(paths);
  if (action === "show") {
    return { status: "configured", database: publicCatalog(config) };
  }
  if (action === "migrate") {
    return databaseMigrateCommand(args.slice(1), flags, paths, config);
  }
  if (action !== "configure") {
    throw new CliError("usage: database show | database configure | database migrate", {
      code: "argument_error",
    });
  }

  const existingDatabaseUrl = await readSecret(paths.databaseUrl);
  const previousCatalog = structuredClone(config.infrastructure.catalog);
  const previousProvider = catalogProvider(previousCatalog);
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

  // 提供方切换守卫：源库可达且非空时禁止静默切换。空 Catalog 切换必须
  // 显式确认（--confirm-empty-switch 或交互确认），硬拦截在这里而非后端。
  const nextProvider = catalogProvider(discovered.catalog);
  if (nextProvider !== previousProvider
      && await sourceCatalogMightHaveData(previousProvider, existingDatabaseUrl, paths.home)) {
    if (flags.confirm_empty_switch) {
      writeError(`! ${SWITCH_WARNING}（--confirm-empty-switch 已确认）`);
    } else if (!flags.non_interactive && process.stdin.isTTY) {
      output.write(`! ${SWITCH_WARNING}\n`);
      if (!(await confirmProviderSwitch())) {
        throw new CliError("已取消数据库提供方切换；原配置未修改", {
          code: "database_switch_cancelled",
          exitCode: 1,
          details: { previous: publicCatalog(config) },
        });
      }
    } else {
      throw new CliError(`${SWITCH_WARNING}如确认切换，请显式传入 --confirm-empty-switch`, {
        code: "database_switch_requires_confirmation",
        exitCode: 1,
        details: { previous: publicCatalog(config), requires_migration: true },
      });
    }
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
    requires_migration: nextProvider !== previousProvider,
    restart_required: instance.status === "running",
    next_command: instance.status === "running" ? "puddingclaw restart" : "puddingclaw start",
  };
}
