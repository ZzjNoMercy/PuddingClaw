import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { CliError } from "./errors.js";
import { resolveExecutablePath } from "./probes.js";

export const BUNDLED_POSTGRES_IMAGE = "pgvector/pgvector:pg16";
export const BUNDLED_POSTGRES_CONTAINER = "puddingclaw-postgres";

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export function runProcess(command, args, {
  env = process.env,
  inherit = false,
  input = "",
  timeoutMs = 30_000,
} = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      env,
      stdio: inherit ? "inherit" : ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    if (!inherit) {
      child.stdout?.on("data", (chunk) => { stdout += chunk; });
      child.stderr?.on("data", (chunk) => { stderr += chunk; });
      child.stdin?.end(input || undefined);
    }
    const timer = setTimeout(() => child.kill("SIGTERM"), timeoutMs);
    child.once("error", (error) => {
      clearTimeout(timer);
      resolve({ status: null, stdout, stderr, error });
    });
    child.once("close", (status, signal) => {
      clearTimeout(timer);
      resolve({ status, signal, stdout, stderr });
    });
  });
}

export function nativePostgresInstaller({
  platform = process.platform,
  resolve = resolveExecutablePath,
} = {}) {
  if (platform !== "linux") {
    return {
      available: false,
      platform,
      reason: "当前版本仅支持在 Ubuntu/Debian 上一键安装本机 PostgreSQL",
    };
  }
  const aptGet = resolve("apt-get");
  const aptCache = resolve("apt-cache");
  const sudo = resolve("sudo");
  if (!aptGet || !sudo) {
    return {
      available: false,
      platform,
      reason: "未找到 sudo/apt-get；无法安全地自动安装本机 PostgreSQL",
    };
  }
  return {
    available: true,
    platform,
    package_manager: "apt",
    aptGet,
    aptCache: aptCache || "apt-cache",
    sudo,
    systemctl: resolve("systemctl"),
    service: resolve("service"),
  };
}

export async function probeDocker({ run = runProcess } = {}) {
  const result = await run("docker", ["version", "--format", "{{.Server.Version}}"], {
    timeoutMs: 5_000,
  });
  const version = String(result.stdout || "").trim();
  return result.status === 0 && version
    ? { probe: "runtime.docker", status: "available", required: false, version }
    : {
      probe: "runtime.docker",
      status: "needs_action",
      required: false,
      reason: result.error?.code === "ENOENT"
        ? "Docker 未安装"
        : String(result.stderr || result.error?.message || "Docker daemon 不可用").trim(),
    };
}

function databaseUrl({ username, password, database, port = 5432 }) {
  return `postgresql+asyncpg://${encodeURIComponent(username)}:${encodeURIComponent(password)}`
    + `@127.0.0.1:${port}/${encodeURIComponent(database)}`;
}

function databaseIdentifier(value, label) {
  const normalized = String(value || "").trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,62}$/.test(normalized)) {
    throw new CliError(`${label} 只能包含字母、数字和下划线，且必须以字母或下划线开头`, {
      code: "database_identifier_invalid",
      exitCode: 1,
    });
  }
  return normalized;
}

function sqlLiteral(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

async function requireSuccess(result, message, code) {
  if (result.status !== 0) {
    throw new CliError(String(result.stderr || result.error?.message || message).trim(), {
      code,
      exitCode: 1,
    });
  }
  return result;
}

async function aptInstall(installer, packages, run) {
  await requireSuccess(
    await run(installer.sudo, [installer.aptGet, "update"], { inherit: true, timeoutMs: 300_000 }),
    "apt-get update 失败",
    "postgres_package_index_failed",
  );
  await requireSuccess(
    await run(installer.sudo, [installer.aptGet, "install", "-y", ...packages], {
      inherit: true,
      timeoutMs: 600_000,
    }),
    "PostgreSQL 系统包安装失败",
    "postgres_install_failed",
  );
}

async function startPostgresService(installer, run) {
  if (installer.systemctl) {
    const result = await run(installer.sudo, [installer.systemctl, "enable", "--now", "postgresql"], {
      inherit: true,
      timeoutMs: 60_000,
    });
    if (result.status === 0) return;
  }
  if (installer.service) {
    await requireSuccess(
      await run(installer.sudo, [installer.service, "postgresql", "start"], { inherit: true, timeoutMs: 60_000 }),
      "PostgreSQL 服务启动失败",
      "postgres_start_failed",
    );
    return;
  }
  throw new CliError("PostgreSQL 已安装，但未找到 systemctl/service 来启动服务", {
    code: "postgres_start_failed",
    exitCode: 1,
  });
}

async function waitUntilReady(installer, run, wait) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const checked = await run(installer.sudo, ["-u", "postgres", "pg_isready"], { timeoutMs: 5_000 });
    if (checked.status === 0) return;
    await wait(1_000);
  }
  throw new CliError("本机 PostgreSQL 在 60 秒内未就绪", {
    code: "postgres_readiness_timeout",
    exitCode: 1,
  });
}

async function configureDatabase(installer, { username, password, database }, run) {
  const role = databaseIdentifier(username, "数据库用户名");
  const dbName = databaseIdentifier(database, "数据库名");
  const sql = [
    `SELECT 'CREATE ROLE "${role}" LOGIN'`,
    `WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ${sqlLiteral(role)})\\gexec`,
    `ALTER ROLE "${role}" WITH LOGIN PASSWORD ${sqlLiteral(password)};`,
    `SELECT 'CREATE DATABASE "${dbName}" OWNER "${role}"'`,
    `WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = ${sqlLiteral(dbName)})\\gexec`,
    `ALTER DATABASE "${dbName}" OWNER TO "${role}";`,
  ].join("\n");
  await requireSuccess(
    await run(installer.sudo, ["-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "--no-psqlrc"], {
      input: `${sql}\n`,
      timeoutMs: 30_000,
    }),
    "无法创建 PuddingClaw PostgreSQL 用户或数据库",
    "postgres_configure_failed",
  );
}

async function postgresMajor(installer, run) {
  const result = await run(installer.sudo, ["-u", "postgres", "psql", "-Atqc", "show server_version_num"], {
    timeoutMs: 10_000,
  });
  if (result.status !== 0) return null;
  const version = Number.parseInt(String(result.stdout || "").trim(), 10);
  return Number.isInteger(version) && version > 0 ? Math.floor(version / 10_000) : null;
}

async function installPgvector(installer, database, run) {
  const major = await postgresMajor(installer, run);
  if (!major) {
    return { status: "needs_action", reason: "无法识别 PostgreSQL 主版本，未自动安装 pgvector" };
  }
  const packageName = `postgresql-${major}-pgvector`;
  const candidate = await run(installer.aptCache, ["show", packageName], { timeoutMs: 10_000 });
  if (candidate.status !== 0) {
    return { status: "needs_action", package: packageName, reason: `软件源中没有 ${packageName}` };
  }
  const installed = await run(installer.sudo, [installer.aptGet, "install", "-y", packageName], {
    inherit: true,
    timeoutMs: 300_000,
  });
  if (installed.status !== 0) {
    return { status: "needs_action", package: packageName, reason: `${packageName} 安装失败` };
  }
  const created = await run(installer.sudo, [
    "-u", "postgres", "psql", "-d", database, "-v", "ON_ERROR_STOP=1",
    "-c", "CREATE EXTENSION IF NOT EXISTS vector",
  ], { timeoutMs: 30_000 });
  return created.status === 0
    ? { status: "available", package: packageName }
    : { status: "needs_action", package: packageName, reason: "pgvector 扩展初始化失败" };
}

export async function installNativePostgres({
  installer = nativePostgresInstaller(),
  requirePgvector = false,
  database = "puddingclaw",
  username = "puddingclaw",
  password = "",
  run = runProcess,
  wait = delay,
} = {}) {
  if (!installer.available) {
    throw new CliError(installer.reason, {
      code: "native_postgres_installer_unavailable",
      exitCode: 1,
      details: installer,
    });
  }
  await aptInstall(installer, ["postgresql", "postgresql-contrib"], run);
  await startPostgresService(installer, run);
  await waitUntilReady(installer, run, wait);
  const selectedDatabase = databaseIdentifier(database, "数据库名");
  const selectedUsername = databaseIdentifier(username, "数据库用户名");
  const selectedPassword = password || crypto.randomBytes(24).toString("base64url");
  await configureDatabase(installer, {
    username: selectedUsername,
    password: selectedPassword,
    database: selectedDatabase,
  }, run);
  const pgvector = requirePgvector
    ? await installPgvector(installer, selectedDatabase, run)
    : { status: "skipped", reason: "当前 Profile 不需要 pgvector" };
  return {
    status: "installed",
    databaseUrl: databaseUrl({ username: selectedUsername, password: selectedPassword, database: selectedDatabase }),
    catalog: {
      mode: "postgresql",
      provider: "postgresql",
      source: "native_apt",
      host: "127.0.0.1",
      port: 5432,
      database: selectedDatabase,
      username: selectedUsername,
      probe_status: "available",
    },
    probe: {
      probe: "database.postgresql.install",
      status: "available",
      required: false,
      source: "native_apt",
      pgvector,
    },
  };
}

export function bundledPostgresRunArgs({
  home,
  password,
  port = 5432,
  database = "puddingclaw",
  username = "puddingclaw",
}) {
  return [
    "run", "--detach",
    "--name", BUNDLED_POSTGRES_CONTAINER,
    "--label", "io.puddingclaw.managed=postgresql",
    "--label", `io.puddingclaw.home=${home}`,
    "--restart", "unless-stopped",
    "--publish", `127.0.0.1:${port}:5432`,
    "--env", `POSTGRES_DB=${database}`,
    "--env", `POSTGRES_USER=${username}`,
    "--env", `POSTGRES_PASSWORD=${password}`,
    "--volume", `${path.join(home, "infrastructure", "postgres")}:/var/lib/postgresql/data`,
    BUNDLED_POSTGRES_IMAGE,
  ];
}

function inspectedEnvironment(inspected) {
  return Object.fromEntries((inspected?.Config?.Env || []).map((item) => {
    const index = String(item).indexOf("=");
    return index < 0 ? [String(item), ""] : [String(item).slice(0, index), String(item).slice(index + 1)];
  }));
}

export async function installDockerPostgres({
  home,
  port = 5432,
  database = "puddingclaw",
  username = "puddingclaw",
  password = "",
  requirePgvector = false,
  run = runProcess,
  wait = delay,
} = {}) {
  const docker = await probeDocker({ run });
  if (docker.status !== "available") {
    throw new CliError(docker.reason || "Docker 不可用", {
      code: "docker_required",
      exitCode: 1,
      details: docker,
    });
  }
  await fs.mkdir(path.join(home, "infrastructure", "postgres"), { recursive: true, mode: 0o700 });
  const inspectedResult = await run("docker", ["container", "inspect", BUNDLED_POSTGRES_CONTAINER], {
    timeoutMs: 5_000,
  });
  let selectedPort = Number(port || 5432);
  if (!Number.isInteger(selectedPort) || selectedPort < 1 || selectedPort > 65535) {
    throw new CliError("Docker PostgreSQL 端口必须在 1-65535 之间", {
      code: "argument_error",
      exitCode: 1,
    });
  }
  let selectedDatabase = databaseIdentifier(database, "数据库名");
  let selectedUsername = databaseIdentifier(username, "数据库用户名");
  let selectedPassword = password || crypto.randomBytes(24).toString("base64url");
  const requested = {
    port: selectedPort,
    database: selectedDatabase,
    username: selectedUsername,
    password: password || "",
  };
  let reused = false;
  if (inspectedResult.status === 0) {
    let inspected;
    try { inspected = JSON.parse(inspectedResult.stdout)[0]; } catch {
      throw new CliError("无法识别现有 PostgreSQL 容器", { code: "postgres_container_invalid" });
    }
    const labels = inspected?.Config?.Labels || {};
    if (labels["io.puddingclaw.managed"] !== "postgresql" || labels["io.puddingclaw.home"] !== home) {
      throw new CliError(
        `容器 ${BUNDLED_POSTGRES_CONTAINER} 已存在但不属于当前 Home；不会接管或删除它`,
        { code: "postgres_container_conflict", exitCode: 1 },
      );
    }
    const environment = inspectedEnvironment(inspected);
    const actual = {
      password: environment.POSTGRES_PASSWORD || "",
      database: environment.POSTGRES_DB || selectedDatabase,
      username: environment.POSTGRES_USER || selectedUsername,
      port: Number(inspected?.NetworkSettings?.Ports?.["5432/tcp"]?.[0]?.HostPort || selectedPort),
    };
    if (
      requested.port !== actual.port
      || requested.database !== actual.database
      || requested.username !== actual.username
      || (requested.password && requested.password !== actual.password)
    ) {
      throw new CliError(
        "现有 Docker PostgreSQL 的端口、数据库或凭据与本次输入不一致；不会删除或重建现有容器",
        { code: "postgres_container_configuration_conflict", exitCode: 1 },
      );
    }
    selectedPassword = actual.password;
    selectedDatabase = actual.database;
    selectedUsername = actual.username;
    selectedPort = actual.port;
    if (!selectedPassword) {
      throw new CliError("托管 PostgreSQL 容器缺少可恢复凭据；不会修改它", {
        code: "postgres_credentials_unavailable",
        exitCode: 1,
      });
    }
    await requireSuccess(
      await run("docker", ["start", BUNDLED_POSTGRES_CONTAINER], { timeoutMs: 30_000 }),
      "启动 Docker PostgreSQL 失败",
      "postgres_start_failed",
    );
    reused = true;
  } else {
    await requireSuccess(
      await run("docker", bundledPostgresRunArgs({
        home,
        password: selectedPassword,
        port: selectedPort,
        database: selectedDatabase,
        username: selectedUsername,
      }), {
        inherit: true,
        timeoutMs: 300_000,
      }),
      "Docker PostgreSQL 创建失败",
      "postgres_install_failed",
    );
  }
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const ready = await run("docker", [
      "exec", BUNDLED_POSTGRES_CONTAINER, "pg_isready", "-U", selectedUsername, "-d", selectedDatabase,
    ], { timeoutMs: 5_000 });
    if (ready.status === 0) break;
    if (attempt === 59) {
      throw new CliError("Docker PostgreSQL 在 60 秒内未就绪", {
        code: "postgres_readiness_timeout",
        exitCode: 1,
      });
    }
    await wait(1_000);
  }
  let pgvector = { status: "skipped", reason: "当前 Profile 不需要 pgvector" };
  if (requirePgvector) {
    await requireSuccess(
      await run("docker", [
        "exec", "--env", "PGPASSWORD", BUNDLED_POSTGRES_CONTAINER,
        "psql", "-v", "ON_ERROR_STOP=1", "-U", selectedUsername, "-d", selectedDatabase,
        "-c", "CREATE EXTENSION IF NOT EXISTS vector",
      ], { env: { ...process.env, PGPASSWORD: selectedPassword }, timeoutMs: 30_000 }),
      "Docker PostgreSQL 的 pgvector 初始化失败",
      "pgvector_install_failed",
    );
    pgvector = { status: "available" };
  }
  return {
    status: reused ? "reused" : "installed",
    databaseUrl: databaseUrl({
      username: selectedUsername,
      password: selectedPassword,
      database: selectedDatabase,
      port: selectedPort,
    }),
    catalog: {
      mode: "postgresql",
      provider: "postgresql",
      source: "docker",
      host: "127.0.0.1",
      port: selectedPort,
      database: selectedDatabase,
      username: selectedUsername,
      probe_status: "available",
      container_name: BUNDLED_POSTGRES_CONTAINER,
      image: BUNDLED_POSTGRES_IMAGE,
    },
    probe: {
      probe: "database.postgresql.install",
      status: "available",
      required: false,
      source: "docker",
      reused,
      pgvector,
    },
  };
}
