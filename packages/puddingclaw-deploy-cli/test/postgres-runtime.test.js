import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  BUNDLED_POSTGRES_CONTAINER,
  bundledPostgresRunArgs,
  installDockerPostgres,
  installNativePostgres,
  nativePostgresInstaller,
} from "../src/postgres-runtime.js";
import { validatePreparedInfrastructure } from "../src/init-discovery.js";

test("missing asyncpg is a Runtime error, not a PostgreSQL fallback signal", () => {
  const probes = validatePreparedInfrastructure({
    python: "/managed/python",
    databaseUrl: "postgresql+asyncpg://puddingclaw:secret@127.0.0.1:5432/puddingclaw",
    milvus: { enabled: false },
    spawn: () => ({
      status: 1,
      stdout: "",
      stderr: "ModuleNotFoundError: No module named 'asyncpg'\n",
    }),
  });

  assert.deepEqual(probes, [{
    probe: "database.connection",
    status: "error",
    required: true,
    code: "runtime_dependency_missing",
    reason: "ModuleNotFoundError: No module named 'asyncpg'",
  }]);
});

test("Harness PostgreSQL validation skips optional pgvector", () => {
  const probes = validatePreparedInfrastructure({
    python: "/managed/python",
    databaseUrl: "postgresql+asyncpg://puddingclaw:secret@127.0.0.1:5432/puddingclaw",
    milvus: { enabled: false },
    requirePgvector: false,
    spawn: () => ({ status: 0, stdout: '{"connected":true,"pgvector":""}\n', stderr: "" }),
  });

  assert.equal(probes[0].status, "available");
  assert.deepEqual(probes[0].pgvector, {
    status: "skipped",
    reason: "当前 Profile 不需要 pgvector",
  });
});

test("Knowledge PostgreSQL validation requires pgvector", () => {
  const probes = validatePreparedInfrastructure({
    python: "/managed/python",
    databaseUrl: "postgresql+asyncpg://puddingclaw:secret@127.0.0.1:5432/puddingclaw",
    milvus: { enabled: false },
    requirePgvector: true,
    spawn: () => ({ status: 0, stdout: '{"connected":true,"pgvector":""}\n', stderr: "" }),
  });

  assert.equal(probes[0].status, "available");
  assert.equal(probes[0].pgvector.status, "needs_action");
});

test("database validation creates a missing database only after explicit authorization", () => {
  let captured;
  const probes = validatePreparedInfrastructure({
    python: "/managed/python",
    databaseUrl: "postgresql+asyncpg://admin:secret@127.0.0.1:5432/new_database",
    createDatabaseIfMissing: true,
    milvus: { enabled: false },
    spawn: (command, args, options) => {
      captured = { command, args, options };
      return { status: 0, stdout: '{"connected":true,"created":true,"pgvector":""}\n', stderr: "" };
    },
  });

  assert.equal(captured.options.env.PUDDINGCLAW_CREATE_DATABASE_IF_MISSING, "1");
  assert.match(captured.args.at(-1), /CREATE DATABASE/);
  assert.equal(probes[0].created, true);
});

test("native PostgreSQL installer is explicitly capability-gated", () => {
  const resolved = new Map([
    ["apt-get", "/usr/bin/apt-get"],
    ["apt-cache", "/usr/bin/apt-cache"],
    ["sudo", "/usr/bin/sudo"],
    ["systemctl", "/usr/bin/systemctl"],
  ]);
  const installer = nativePostgresInstaller({ platform: "linux", resolve: (name) => resolved.get(name) || "" });
  assert.equal(installer.available, true);
  assert.equal(installer.package_manager, "apt");
  assert.equal(nativePostgresInstaller({ platform: "darwin", resolve: () => "" }).available, false);
});

test("native PostgreSQL installation configures an isolated role and optional pgvector", async () => {
  const calls = [];
  const installer = {
    available: true,
    platform: "linux",
    package_manager: "apt",
    aptGet: "/usr/bin/apt-get",
    aptCache: "/usr/bin/apt-cache",
    sudo: "/usr/bin/sudo",
    systemctl: "/usr/bin/systemctl",
    service: "",
  };
  const run = async (command, args, options = {}) => {
    calls.push({ command, args, input: options.input || "" });
    if (args.some((item) => String(item).includes("server_version_num"))) {
      return { status: 0, stdout: "160009\n", stderr: "" };
    }
    return { status: 0, stdout: "ok\n", stderr: "" };
  };
  const result = await installNativePostgres({ installer, requirePgvector: true, run, wait: async () => {} });
  assert.equal(result.catalog.source, "native_apt");
  assert.equal(result.probe.pgvector.package, "postgresql-16-pgvector");
  assert.match(result.databaseUrl, /^postgresql\+asyncpg:\/\/puddingclaw:/);
  assert.ok(calls.some((call) => call.args.includes("postgresql-contrib")));
  assert.ok(calls.some((call) => call.args.includes("postgresql-16-pgvector")));
  assert.ok(calls.some((call) => call.input.includes('CREATE ROLE "puddingclaw" LOGIN')));
});

test("native PostgreSQL uses the confirmed database, username, and password", async () => {
  const calls = [];
  const installer = {
    available: true,
    platform: "linux",
    package_manager: "apt",
    aptGet: "/usr/bin/apt-get",
    aptCache: "/usr/bin/apt-cache",
    sudo: "/usr/bin/sudo",
    systemctl: "/usr/bin/systemctl",
    service: "",
  };
  const run = async (command, args, options = {}) => {
    calls.push({ command, args, input: options.input || "" });
    return { status: 0, stdout: "ok\n", stderr: "" };
  };
  const result = await installNativePostgres({
    installer,
    database: "agent_data",
    username: "agent_user",
    password: "quoted'password",
    run,
    wait: async () => {},
  });
  assert.equal(result.catalog.database, "agent_data");
  assert.equal(result.catalog.username, "agent_user");
  assert.match(result.databaseUrl, /agent_user:quoted'password@127\.0\.0\.1:5432\/agent_data$/);
  assert.ok(calls.some((call) => call.input.includes('CREATE DATABASE "agent_data" OWNER "agent_user"')));
  assert.ok(calls.some((call) => call.input.includes("PASSWORD 'quoted''password'")));
});

test("Docker PostgreSQL is an explicit alternative and labels its ownership", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "puddingclaw-postgres-runtime-"));
  const calls = [];
  try {
    const run = async (command, args) => {
      calls.push({ command, args });
      if (args[0] === "version") return { status: 0, stdout: "26.1.0\n", stderr: "" };
      if (args[0] === "container") return { status: 1, stdout: "", stderr: "not found" };
      return { status: 0, stdout: "ok\n", stderr: "" };
    };
    const result = await installDockerPostgres({ home, run, wait: async () => {} });
    assert.equal(result.catalog.source, "docker");
    assert.equal(result.catalog.container_name, BUNDLED_POSTGRES_CONTAINER);
    const dockerRun = calls.find((call) => call.args[0] === "run");
    assert.ok(dockerRun.args.includes("io.puddingclaw.managed=postgresql"));
    assert.ok(dockerRun.args.includes(`io.puddingclaw.home=${home}`));
    assert.ok(dockerRun.args.includes("127.0.0.1:5432:5432"));
    assert.deepEqual(
      bundledPostgresRunArgs({ home, password: "secret" }).slice(0, 4),
      ["run", "--detach", "--name", BUNDLED_POSTGRES_CONTAINER],
    );
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});

test("Docker PostgreSQL applies confirmed port and database fields", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "puddingclaw-postgres-custom-"));
  const calls = [];
  try {
    const run = async (command, args) => {
      calls.push({ command, args });
      if (args[0] === "version") return { status: 0, stdout: "26.1.0\n", stderr: "" };
      if (args[0] === "container") return { status: 1, stdout: "", stderr: "not found" };
      return { status: 0, stdout: "ok\n", stderr: "" };
    };
    const result = await installDockerPostgres({
      home,
      port: 15432,
      database: "agent_data",
      username: "agent_user",
      password: "secret",
      run,
      wait: async () => {},
    });
    assert.equal(result.catalog.port, 15432);
    assert.equal(result.catalog.database, "agent_data");
    assert.equal(result.catalog.username, "agent_user");
    const dockerRun = calls.find((call) => call.args[0] === "run");
    assert.ok(dockerRun.args.includes("127.0.0.1:15432:5432"));
    assert.ok(dockerRun.args.includes("POSTGRES_DB=agent_data"));
    assert.ok(dockerRun.args.includes("POSTGRES_USER=agent_user"));
    assert.equal(result.probe.pgvector.status, "skipped");
  } finally {
    await rm(home, { recursive: true, force: true });
  }
});
