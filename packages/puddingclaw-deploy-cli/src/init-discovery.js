import { Writable } from "node:stream";
import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { spawnSync } from "node:child_process";
import { CliError } from "./errors.js";
import { probeHttpHealth, probeProviderEndpoint, probeTcpEndpoint } from "./probes.js";
import {
  installDockerPostgres,
  installNativePostgres,
  nativePostgresInstaller,
  probeDocker,
} from "./postgres-runtime.js";

async function question(prompt, fallback = "") {
  const rl = createInterface({ input, output });
  try {
    const answer = String(await rl.question(`${prompt}${fallback ? ` [${fallback}]` : ""}：`)).trim();
    return answer || fallback;
  } finally {
    rl.close();
  }
}

async function secretQuestion(prompt) {
  let muted = false;
  const hiddenOutput = new Writable({
    write(chunk, encoding, callback) {
      if (!muted) output.write(chunk, encoding);
      callback();
    },
  });
  const rl = createInterface({ input, output: hiddenOutput, terminal: true });
  try {
    output.write(`${prompt}（输入不会回显）：`);
    muted = true;
    const answer = String(await rl.question(""));
    muted = false;
    output.write("\n");
    return answer.trim();
  } finally {
    muted = false;
    rl.close();
  }
}

export function providerPreset(selected) {
  if (selected === "1") {
    return {
      id: "deepseek",
      name: "DeepSeek",
      protocol: "deepseek",
      base_url: "https://api.deepseek.com",
      model: "deepseek-v4-flash",
    };
  }
  if (selected === "2") {
    return {
      id: "dashscope",
      name: "阿里云百炼",
      protocol: "openai_compatible",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      model: "qwen3.7-plus",
    };
  }
  return null;
}

export function multimodalProviderPreset(selected) {
  if (selected === "1" || selected === "dashscope") {
    return {
      id: "dashscope",
      name: "阿里云百炼",
      protocol: "openai_compatible",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      model: "qwen3.7-plus",
    };
  }
  return null;
}

export async function discoverInitialProvider({ flags, nonInteractive }) {
  const configuredByFlags = flags.provider || flags.api_key || process.env.PUDDINGCLAW_INIT_API_KEY;
  if (nonInteractive && !configuredByFlags) {
    return { provider: { status: "unconfigured" }, apiKey: "", probe: { status: "skipped" } };
  }

  let provider;
  if (nonInteractive) {
    const preset = providerPreset(flags.provider === "dashscope" ? "2" : "1");
    provider = {
      ...preset,
      ...(flags.provider === "custom" ? {
        id: String(flags.provider_id || "custom"),
        name: String(flags.provider_name || "Custom Provider"),
        protocol: "openai_compatible",
      } : {}),
      ...(flags.base_url ? { base_url: String(flags.base_url) } : {}),
      ...(flags.model ? { model: String(flags.model) } : {}),
    };
  } else {
    output.write("\n配置初始模型 Provider（完成后进入 GUI 即可对话）：\n\n");
    output.write("[1] DeepSeek\n[2] 阿里云百炼\n[3] 其他 OpenAI-compatible Provider\n[4] 暂不配置\n\n");
    const selected = await question("请选择", "1");
    if (selected === "4") {
      return { provider: { status: "unconfigured" }, apiKey: "", probe: { status: "skipped" } };
    }
    provider = providerPreset(selected);
    if (!provider && selected !== "3") {
      throw new CliError("invalid provider selection", { code: "argument_error" });
    }
    if (!provider) {
      const name = await question("Provider 名称", "Custom Provider");
      provider = {
        id: name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "custom",
        name,
        protocol: "openai_compatible",
        base_url: await question("API Base URL", "https://api.openai.com/v1"),
        model: await question("模型名称"),
      };
    } else {
      provider.base_url = await question("API Base URL", provider.base_url);
      provider.model = await question("模型名称", provider.model);
    }
  }

  const apiKey = String(flags.api_key || process.env.PUDDINGCLAW_INIT_API_KEY || (
    nonInteractive ? "" : await secretQuestion(`${provider.name} API Key`)
  )).trim();
  if (!apiKey) {
    if (nonInteractive) {
      throw new CliError("provider API key is required; use PUDDINGCLAW_INIT_API_KEY", {
        code: "provider_key_required",
      });
    }
    return { provider: { ...provider, status: "needs_action" }, apiKey: "", probe: { status: "needs_action", reason: "API Key 未配置" } };
  }
  output.write("正在验证 Provider 连通性…\n");
  const probe = await probeProviderEndpoint({ baseUrl: provider.base_url, apiKey });
  if (!nonInteractive) {
    output.write(probe.status === "available"
      ? `✓ ${provider.name} 与模型端点可访问\n`
      : `! Provider 尚不可用：${probe.reason}\n`);
  }
  return {
    provider: { ...provider, status: probe.status === "available" ? "configured" : "needs_action" },
    apiKey,
    probe,
  };
}

export async function discoverInitialMultimodalProvider({
  flags,
  nonInteractive,
  primaryDiscovery,
  probeProvider = probeProviderEndpoint,
}) {
  const requested = String(flags.multimodal_provider || "").trim().toLowerCase();
  const configuredByFlags = requested
    || flags.multimodal_api_key
    || process.env.PUDDINGCLAW_INIT_MULTIMODAL_API_KEY;
  if (nonInteractive && !configuredByFlags) {
    return {
      provider: { status: "unconfigured" },
      apiKey: "",
      probe: { probe: "provider.multimodal_endpoint", status: "skipped", required: true },
    };
  }

  let provider;
  let reusePrimaryCredential = false;
  if (nonInteractive) {
    if (["none", "skip", "unconfigured"].includes(requested)) {
      return {
        provider: { status: "unconfigured" },
        apiKey: "",
        probe: { probe: "provider.multimodal_endpoint", status: "skipped", required: true },
      };
    }
    if (requested === "same") {
      if (!primaryDiscovery?.provider?.base_url || !primaryDiscovery?.provider?.model) {
        throw new CliError("multimodal-provider=same requires a configured primary provider", {
          code: "multimodal_provider_required",
        });
      }
      provider = { ...primaryDiscovery.provider };
      reusePrimaryCredential = true;
    } else {
      provider = {
        ...multimodalProviderPreset("1"),
        ...(requested === "custom" ? {
          id: String(flags.multimodal_provider_id || "custom-multimodal"),
          name: String(flags.multimodal_provider_name || "Custom Multimodal Provider"),
          protocol: "openai_compatible",
        } : {}),
        ...(flags.multimodal_base_url ? { base_url: String(flags.multimodal_base_url) } : {}),
        ...(flags.multimodal_model ? { model: String(flags.multimodal_model) } : {}),
      };
      reusePrimaryCredential = provider.id === primaryDiscovery?.provider?.id
        && provider.base_url === primaryDiscovery?.provider?.base_url;
    }
  } else {
    output.write("\n配置图片分析 SubAgent 的多模态模型（Harness Core）：\n\n");
    output.write("[1] 阿里云百炼 / qwen3.7-plus（推荐）\n");
    if (primaryDiscovery?.provider?.model) {
      output.write(`[2] 复用主模型 / ${primaryDiscovery.provider.model}（需确认支持图片输入）\n`);
    } else {
      output.write("[2] 复用主模型（当前主模型未配置）\n");
    }
    output.write("[3] 其他 OpenAI-compatible 多模态 Provider\n[4] 暂不配置\n\n");
    const selected = await question("请选择", "1");
    if (selected === "4") {
      return {
        provider: { status: "unconfigured" },
        apiKey: "",
        probe: { probe: "provider.multimodal_endpoint", status: "skipped", required: true },
      };
    }
    if (selected === "2") {
      if (!primaryDiscovery?.provider?.base_url || !primaryDiscovery?.provider?.model) {
        throw new CliError("请先配置主模型，或为图片分析选择独立 Provider", {
          code: "multimodal_provider_required",
        });
      }
      provider = { ...primaryDiscovery.provider };
      reusePrimaryCredential = true;
    } else if (selected === "3") {
      const name = await question("Provider 名称", "Custom Multimodal Provider");
      provider = {
        id: name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "custom-multimodal",
        name,
        protocol: "openai_compatible",
        base_url: await question("API Base URL", "https://api.openai.com/v1"),
        model: await question("多模态模型名称"),
      };
    } else {
      provider = multimodalProviderPreset(selected);
      if (!provider) {
        throw new CliError("invalid multimodal provider selection", { code: "argument_error" });
      }
      provider.base_url = await question("多模态 API Base URL", provider.base_url);
      provider.model = await question("多模态模型名称", provider.model);
      reusePrimaryCredential = provider.id === primaryDiscovery?.provider?.id
        && provider.base_url === primaryDiscovery?.provider?.base_url;
    }
  }

  const apiKey = String(
    reusePrimaryCredential
      ? primaryDiscovery?.apiKey || ""
      : flags.multimodal_api_key
        || process.env.PUDDINGCLAW_INIT_MULTIMODAL_API_KEY
        || (nonInteractive ? "" : await secretQuestion(`${provider.name} 多模态 API Key`)),
  ).trim();
  if (!apiKey) {
    if (nonInteractive) {
      throw new CliError(
        "multimodal provider API key is required; use PUDDINGCLAW_INIT_MULTIMODAL_API_KEY",
        { code: "multimodal_provider_key_required" },
      );
    }
    return {
      provider: {
        ...provider,
        status: "needs_action",
        reuse_primary_credential: reusePrimaryCredential,
      },
      apiKey: "",
      probe: {
        probe: "provider.multimodal_endpoint",
        status: "needs_action",
        required: true,
        reason: "多模态 API Key 未配置",
      },
    };
  }

  output.write("正在验证多模态 Provider 连通性…\n");
  const endpointProbe = await probeProvider({ baseUrl: provider.base_url, apiKey });
  const probe = { ...endpointProbe, probe: "provider.multimodal_endpoint" };
  if (!nonInteractive) {
    output.write(probe.status === "available"
      ? `✓ ${provider.name} 多模态模型端点可访问\n`
      : `! 多模态 Provider 尚不可用：${probe.reason}\n`);
  }
  return {
    provider: {
      ...provider,
      status: probe.status === "available" ? "configured" : "needs_action",
      reuse_primary_credential: reusePrimaryCredential,
    },
    apiKey: reusePrimaryCredential ? "" : apiKey,
    probe,
  };
}

function safeDatabaseMetadata(raw) {
  const parsed = new URL(raw.replace(/^postgresql\+asyncpg:/, "postgresql:"));
  return {
    host: parsed.hostname,
    port: Number(parsed.port || 5432),
    database: decodeURIComponent(parsed.pathname.replace(/^\//, "")),
    username: decodeURIComponent(parsed.username || ""),
  };
}

function databaseUrlWithName(raw, database) {
  const normalized = String(database || "").trim();
  if (!normalized) throw new CliError("数据库名不能为空", { code: "database_name_required" });
  const parsed = new URL(raw.replace(/^postgresql\+asyncpg:/, "postgresql:"));
  parsed.pathname = `/${encodeURIComponent(normalized)}`;
  return parsed.toString().replace(/^postgresql:/, "postgresql+asyncpg:");
}

async function confirmCreateMissingDatabase() {
  const answer = String(await question("如果数据库不存在，是否创建", "Y")).trim().toLowerCase();
  return answer === "" || answer === "y" || answer === "yes";
}

async function promptInstalledDatabase({ docker = false, defaults = {}, allowedOccupiedPort = 0 } = {}) {
  let port = Number(defaults.port || 5432);
  if (docker) {
    port = Number.parseInt(await question("宿主机映射端口", String(port)), 10);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new CliError("Docker PostgreSQL 端口必须在 1-65535 之间", { code: "argument_error" });
    }
    let occupied = await probeTcpEndpoint({
      probe: "database.docker.port",
      host: "127.0.0.1",
      port,
      required: false,
    });
    while (occupied.status === "available" && port !== allowedOccupiedPort) {
      output.write(`! 端口 ${port} 已被占用；不会终止占用进程\n`);
      port = Number.parseInt(await question("请选择其他宿主机端口", String(port + 1)), 10);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new CliError("Docker PostgreSQL 端口必须在 1-65535 之间", { code: "argument_error" });
      }
      occupied = await probeTcpEndpoint({
        probe: "database.docker.port",
        host: "127.0.0.1",
        port,
        required: false,
      });
    }
  }
  const database = await question("数据库名", defaults.database || "puddingclaw");
  const username = await question("用户名", defaults.username || "puddingclaw");
  const password = await secretQuestion("密码（留空则安全随机生成）");
  return { port, database, username, password };
}

async function confirmDatabaseProvisioning(label, connection) {
  output.write(`\n${label} 配置：\n`);
  output.write(`  地址:     127.0.0.1:${connection.port}\n`);
  output.write(`  数据库名: ${connection.database}\n`);
  output.write(`  用户名:   ${connection.username}\n`);
  output.write(`  密码:     ${connection.password ? "使用输入的密码" : "安全随机生成"}\n`);
  const answer = String(await question("确认安装/配置并创建上述数据库", "Y")).trim().toLowerCase();
  return answer === "" || answer === "y" || answer === "yes";
}

function sqliteFallback(reason = "", selectionExplicit = false) {
  return {
    catalog: {
      mode: "sqlite",
      provider: "sqlite",
      source: selectionExplicit ? "local_file" : "fallback",
      host: "",
      port: 0,
      database: "",
      probe_status: "available",
      selection_explicit: selectionExplicit,
      ...(reason ? { fallback_reason: reason } : {}),
    },
    databaseUrl: "",
  };
}

function defaultSqliteCatalog() {
  return {
    catalog: {
      mode: "sqlite",
      provider: "sqlite",
      source: "local_file",
      host: "",
      port: 0,
      database: "",
      probe_status: "skipped",
    },
    databaseUrl: "",
  };
}

async function externalPostgres({
  rawUrl,
  nonInteractive,
  probes,
  createDatabaseIfMissing = false,
  source = "external",
}) {
  const databaseUrl = String(rawUrl || "").trim();
  if (!databaseUrl) {
    if (nonInteractive) {
      throw new CliError("PostgreSQL URL is required for database_mode=postgresql", {
        code: "database_url_required",
      });
    }
    return sqliteFallback("未提供 PostgreSQL URL");
  }
  let metadata;
  try { metadata = safeDatabaseMetadata(databaseUrl); } catch {
    if (nonInteractive) throw new CliError("PostgreSQL URL is invalid", { code: "argument_error" });
    return sqliteFallback("PostgreSQL URL 无效");
  }
  const probe = await probeTcpEndpoint({
    probe: "database.postgresql",
    host: metadata.host,
    port: metadata.port,
    required: false,
  });
  probes.push(probe);
  if (probe.status !== "available") {
    return sqliteFallback(`PostgreSQL ${metadata.host}:${metadata.port} 不可达`);
  }
  return {
    catalog: {
      mode: "postgresql",
      provider: "postgresql",
      source,
      ...metadata,
      create_if_missing: createDatabaseIfMissing,
      probe_status: probe.status,
    },
    databaseUrl,
    createDatabaseIfMissing,
  };
}

async function promptExistingPostgres({ configuredUrl, nonInteractive, probes, source, label }) {
  const rawUrl = configuredUrl || await secretQuestion(`${label} URL（postgresql+asyncpg://...）`);
  const metadata = safeDatabaseMetadata(rawUrl);
  const database = await question("数据库名", metadata.database || "puddingclaw");
  const targetUrl = databaseUrlWithName(rawUrl, database);
  const createDatabaseIfMissing = await confirmCreateMissingDatabase();
  return externalPostgres({
    rawUrl: targetUrl,
    nonInteractive,
    probes,
    createDatabaseIfMissing,
    source,
  });
}

export async function discoverCoreDatabase({
  profile,
  flags,
  nonInteractive,
  home,
  existingDatabaseUrl = "",
  existingCatalog = null,
  reuseExistingDatabaseUrl = true,
  promptWhenUnspecified = true,
}) {
  const probes = [];
  const explicitMode = String(flags.database_mode || "").trim().toLowerCase();
  const explicitlyConfiguredUrl = String(
    flags.database_url || process.env.PUDDINGCLAW_INIT_DATABASE_URL || "",
  ).trim();
  const configuredUrl = String(
    explicitlyConfiguredUrl
      || (reuseExistingDatabaseUrl ? existingDatabaseUrl : "")
      || "",
  ).trim();

  // `puddingclaw init` has a zero-config SQLite path. It must not prompt for,
  // probe, install or validate PostgreSQL unless the user explicitly passed a
  // database option. `puddingclaw database configure` keeps the interactive
  // selector by leaving promptWhenUnspecified enabled.
  if (!promptWhenUnspecified && !explicitMode && !explicitlyConfiguredUrl) {
    if (existingCatalog) {
      const provider = existingCatalog.provider === "postgresql"
        || existingCatalog.mode === "postgresql" ? "postgresql" : "sqlite";
      return {
        catalog: structuredClone(existingCatalog),
        databaseUrl: provider === "postgresql" ? existingDatabaseUrl : "",
        createDatabaseIfMissing: Boolean(existingCatalog.create_if_missing),
        probes,
      };
    }
    return { ...defaultSqliteCatalog(), probes };
  }

  if (nonInteractive) {
    if (["postgresql", "external"].includes(explicitMode) || (!explicitMode && configuredUrl)) {
      return {
        ...await externalPostgres({
          rawUrl: configuredUrl,
          nonInteractive,
          probes,
          createDatabaseIfMissing: Boolean(flags.database_create_if_missing),
        }),
        probes,
      };
    }
    if (explicitMode === "native") {
      try {
        const installed = await installNativePostgres({
          requirePgvector: profile === "knowledge" || profile === "full",
          database: flags.database_name || "puddingclaw",
          username: flags.database_username || "puddingclaw",
          password: process.env.PUDDINGCLAW_INIT_DATABASE_PASSWORD || "",
        });
        return { catalog: installed.catalog, databaseUrl: installed.databaseUrl, probes: [...probes, installed.probe] };
      } catch (error) {
        const reason = error?.message || String(error);
        probes.push({ probe: "database.postgresql.install", status: "needs_action", required: false, reason });
        return { ...sqliteFallback(reason), probes };
      }
    }
    if (explicitMode === "docker") {
      try {
        const installed = await installDockerPostgres({
          home,
          requirePgvector: profile === "knowledge" || profile === "full",
          port: flags.database_port || 5432,
          database: flags.database_name || "puddingclaw",
          username: flags.database_username || "puddingclaw",
          password: process.env.PUDDINGCLAW_INIT_DATABASE_PASSWORD || "",
        });
        return { catalog: installed.catalog, databaseUrl: installed.databaseUrl, probes: [...probes, installed.probe] };
      } catch (error) {
        const reason = error?.message || String(error);
        probes.push({ probe: "database.postgresql.install", status: "needs_action", required: false, reason });
        return { ...sqliteFallback(reason), probes };
      }
    }
    // SQLite 本地默认：未显式选择 PostgreSQL 时不探测 5432、不安装任何数据库服务。
    return {
      ...sqliteFallback(
        explicitMode === "sqlite" ? "用户选择 SQLite" : "非交互模式默认 SQLite（未显式选择 PostgreSQL）",
        explicitMode === "sqlite",
      ),
      probes,
    };
  }

  // SQLite 本地默认 / PostgreSQL 服务端可选：只有显式选择 PostgreSQL 路径
  // 时才探测或安装 PostgreSQL。
  output.write("\n核心数据库（SQLite 本地默认 / PostgreSQL 服务端可选）：\n");
  output.write("[1] SQLite（本地默认，Home 内单文件，无需外部服务）\n");
  output.write("[2] 本机 PostgreSQL（已运行则填写连接信息，否则可安装/配置）\n");
  output.write("[3] Docker PostgreSQL\n");
  output.write("[4] 外部 PostgreSQL（提供连接 URL）\n");
  const selected = explicitMode
    || (explicitlyConfiguredUrl ? "external" : await question("请选择数据库方案", "1"));
  const normalized = selected.toLowerCase();

  if (["1", "sqlite"].includes(normalized)) {
    output.write("- 已选择 SQLite 本地默认；不会探测或修改任何 PostgreSQL 进程\n");
    return { ...sqliteFallback("用户选择 SQLite", true), probes };
  }

  try {
    if (["2", "local", "native", "postgresql"].includes(normalized)) {
      output.write("正在探测本机 PostgreSQL 127.0.0.1:5432…\n");
      const localProbe = await probeTcpEndpoint({
        probe: "database.postgresql.discovery",
        host: "127.0.0.1",
        port: 5432,
        required: false,
      });
      probes.push(localProbe);
      if (localProbe.status === "available") {
        output.write("✓ 发现本机 5432 端口；仍需认证并验证目标数据库\n");
        const discovered = await promptExistingPostgres({
          configuredUrl: reuseExistingDatabaseUrl && ["local", "native_apt"].includes(existingCatalog?.source)
            ? configuredUrl
            : "",
          nonInteractive,
          probes,
          source: "local",
          label: "本机 PostgreSQL",
        });
        if (discovered.catalog.provider === "postgresql") {
          output.write("✓ PostgreSQL 端口可访问；认证将在 Runtime 准备后复检\n");
        } else {
          output.write(`! ${discovered.catalog.fallback_reason}，将回退 SQLite\n`);
        }
        return { ...discovered, probes };
      }
      output.write("- 未发现本机 PostgreSQL\n");
      const native = nativePostgresInstaller();
      if (!native.available) throw new CliError(native.reason, { code: "native_postgres_installer_unavailable" });
      output.write("CLI 将通过 sudo apt 安装/配置 PostgreSQL 并写入连接信息；初始化完成后，服务生命周期仍归用户和操作系统管理。\n");
      const connection = await promptInstalledDatabase();
      if (!(await confirmDatabaseProvisioning("本机 PostgreSQL", connection))) {
        output.write("- 已取消本机 PostgreSQL 安装/配置，将使用 SQLite\n");
        return { ...sqliteFallback("用户取消本机 PostgreSQL 配置"), probes };
      }
      const installed = await installNativePostgres({
        requirePgvector: profile === "knowledge" || profile === "full",
        ...connection,
      });
      output.write("✓ 本机 PostgreSQL 已准备完成\n");
      return { catalog: installed.catalog, databaseUrl: installed.databaseUrl, probes: [...probes, installed.probe] };
    }
    if (["3", "docker"].includes(normalized)) {
      const docker = await probeDocker();
      probes.push(docker);
      if (docker.status !== "available") throw new CliError(docker.reason, { code: "docker_required" });
      output.write("CLI 将创建 Docker PostgreSQL 并写入连接信息；初始化完成后，服务生命周期由 Docker 管理。\n");
      const currentDocker = existingCatalog?.source === "docker" ? existingCatalog : {};
      const connection = await promptInstalledDatabase({
        docker: true,
        defaults: currentDocker,
        allowedOccupiedPort: Number(currentDocker.port || 0),
      });
      if (!(await confirmDatabaseProvisioning("Docker PostgreSQL", connection))) {
        output.write("- 已取消 Docker PostgreSQL 配置，将使用 SQLite\n");
        return { ...sqliteFallback("用户取消 Docker PostgreSQL 配置"), probes };
      }
      const installed = await installDockerPostgres({
        home,
        requirePgvector: profile === "knowledge" || profile === "full",
        ...connection,
      });
      output.write("✓ Docker PostgreSQL 已准备完成\n");
      return { catalog: installed.catalog, databaseUrl: installed.databaseUrl, probes: [...probes, installed.probe] };
    }
    if (["4", "external"].includes(normalized)) {
      const discovered = await promptExistingPostgres({
        configuredUrl: reuseExistingDatabaseUrl && existingCatalog?.source === "external" ? configuredUrl : "",
        nonInteractive,
        probes,
        source: "external",
        label: "外部 PostgreSQL",
      });
      if (discovered.catalog.provider === "postgresql") {
        output.write("✓ 外部 PostgreSQL 端口可访问；认证将在 Runtime 准备后复检\n");
      } else {
        output.write(`! ${discovered.catalog.fallback_reason}，将使用 SQLite\n`);
      }
      return { ...discovered, probes };
    }
    throw new CliError(`unknown database selection: ${selected}`, { code: "argument_error" });
  } catch (error) {
    const reason = error?.message || String(error);
    probes.push({
      probe: "database.postgresql.install",
      status: "needs_action",
      required: false,
      reason,
    });
    output.write(`! PostgreSQL 配置未完成：${reason}\n`);
    output.write("- 已回退 SQLite，PuddingClaw 仍可继续初始化\n");
    return { ...sqliteFallback(reason), probes };
  }
}

export async function discoverExtensionInfrastructure({
  profile,
  flags,
  nonInteractive,
  home,
  existingDatabaseUrl = "",
  existingCatalog = null,
}) {
  const knowledgeEnabled = profile === "knowledge" || profile === "full";
  const coreDatabase = await discoverCoreDatabase({
    profile,
    flags,
    nonInteractive,
    home,
    existingDatabaseUrl,
    existingCatalog,
    promptWhenUnspecified: false,
  });
  const result = {
    catalog: coreDatabase.catalog,
    milvus: { enabled: false, uri: "http://127.0.0.1:19530", probe_status: "skipped" },
    embedding: { status: "disabled", provider: "", model: "" },
    mineru: { enabled: false, base_url: "http://127.0.0.1:8002", probe_status: "skipped" },
    embeddingApiKey: "",
    databaseUrl: coreDatabase.databaseUrl,
    createDatabaseIfMissing: Boolean(coreDatabase.createDatabaseIfMissing),
    probes: [...coreDatabase.probes],
  };
  if (!knowledgeEnabled) return result;

  if (!nonInteractive) {
    output.write("\n知识库依赖探索（按依赖顺序执行）：\n");
    output.write("  1. 核心数据库已决策 → 2. Milvus 向量索引 → 3. Embedding 配置\n\n");
  }

  let localMilvusProbe = null;
  if (!nonInteractive) {
    output.write("正在探测本机 Milvus 127.0.0.1:19530…\n");
    localMilvusProbe = await probeTcpEndpoint({
      probe: "milvus.discovery",
      host: "127.0.0.1",
      port: 19530,
      required: false,
    });
    result.probes.push(localMilvusProbe);
    output.write(localMilvusProbe.status === "available"
      ? "✓ 发现 Milvus 端口；选择启用后将继续验证 Collection API\n"
      : "- 未发现本机 Milvus；可以跳过向量索引或填写远程 URI\n");
  }
  const milvusChoice = String(flags.milvus === undefined
    ? (nonInteractive ? "off" : await question(
      "启用 Milvus 向量索引？[Y/n]",
      localMilvusProbe?.status === "available" ? "Y" : "n",
    ))
    : flags.milvus).toLowerCase();
  const milvusEnabled = !["off", "0", "false", "n", "no"].includes(milvusChoice);
  if (milvusEnabled) {
    const uri = String(flags.milvus_uri || (nonInteractive
      ? "http://127.0.0.1:19530"
      : await question("Milvus URI", "http://127.0.0.1:19530")));
    let parsed;
    try {
      parsed = new URL(uri);
    } catch {
      throw new CliError("Milvus URI is invalid", { code: "argument_error" });
    }
    const probe = await probeTcpEndpoint({
      probe: "milvus.connection",
      host: parsed.hostname,
      port: Number(parsed.port || 19530),
    });
    result.milvus = { enabled: true, uri, probe_status: probe.status };
    result.probes.push(probe);
    if (!nonInteractive) {
      output.write(probe.status === "available"
        ? `✓ Milvus ${parsed.hostname}:${parsed.port || 19530} 端口可访问（Collection 将在 Runtime 准备后复检）\n`
        : `! Milvus 不可达：${probe.reason}；向量索引标记为待修复\n`);
    }
    const embeddingKey = String(flags.embedding_api_key || process.env.PUDDINGCLAW_INIT_EMBEDDING_API_KEY || (
      nonInteractive ? "" : await secretQuestion("阿里云百炼 Embedding API Key（text-embedding-v4）")
    )).trim();
    if (embeddingKey) {
      const embeddingProbe = await probeProviderEndpoint({
        baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
        apiKey: embeddingKey,
      });
      result.embedding = {
        status: embeddingProbe.status === "available" ? "configured" : "needs_action",
        provider: "dashscope",
        model: "text-embedding-v4",
      };
      result.embeddingApiKey = embeddingKey;
      result.probes.push({ ...embeddingProbe, probe: "provider.embedding_models" });
      if (!nonInteractive) {
        output.write(embeddingProbe.status === "available"
          ? "✓ Embedding Provider 可访问\n"
          : `! Embedding Provider 尚不可用：${embeddingProbe.reason}\n`);
      }
    } else {
      result.embedding = { status: "needs_action", provider: "dashscope", model: "text-embedding-v4" };
      result.probes.push({
        probe: "provider.embedding_models",
        status: "needs_action",
        required: true,
        reason: "Milvus 已启用，但 Embedding API Key 尚未配置",
      });
      if (!nonInteractive) output.write("! 未配置 Embedding；Milvus 分支暂不激活索引任务\n");
    }
  } else if (!nonInteractive) {
    output.write("- Milvus：已禁用；知识目录仍可用，但语义/多模态检索不会激活\n");
  }
  if (!nonInteractive) {
    output.write("正在探测可选 MinerU 127.0.0.1:8002/health…\n");
    const mineruProbe = await probeHttpHealth({
      probe: "mineru.health",
      baseUrl: "http://127.0.0.1:8002",
    });
    result.probes.push(mineruProbe);
    result.mineru = {
      enabled: mineruProbe.status === "available",
      base_url: "http://127.0.0.1:8002",
      probe_status: mineruProbe.status,
    };
    output.write(mineruProbe.status === "available"
      ? "✓ MinerU 可用，富文档解析将激活\n"
      : "- MinerU 未运行；PDF/Office 富解析保持可选，不阻断知识库\n");
  }
  return result;
}

export function validatePreparedInfrastructure({
  python,
  databaseUrl,
  createDatabaseIfMissing = false,
  milvus,
  requirePgvector = false,
  spawn = spawnSync,
}) {
  const probes = [];
  if (databaseUrl) {
    const script = [
      "import asyncio,json,os",
      "from urllib.parse import unquote,urlsplit,urlunsplit",
      "import asyncpg",
      "async def main():",
      " url=os.environ['PUDDINGCLAW_PROBE_DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:', 1)",
      " allow_create=os.environ.get('PUDDINGCLAW_CREATE_DATABASE_IF_MISSING')=='1'",
      " created=False",
      " try:",
      "  conn=await asyncpg.connect(url, timeout=5)",
      " except asyncpg.InvalidCatalogNameError:",
      "  if not allow_create: raise",
      "  parts=urlsplit(url)",
      "  database=unquote(parts.path.lstrip('/'))",
      "  if not database: raise RuntimeError('target database name is empty')",
      "  maintenance=urlunsplit((parts.scheme,parts.netloc,'/postgres',parts.query,parts.fragment))",
      "  admin=await asyncpg.connect(maintenance, timeout=5)",
      "  try:",
      "   exists=await admin.fetchval('select 1 from pg_database where datname=$1',database)",
      "   if not exists:",
      "    identifier='\"'+database.replace('\"','\"\"')+'\"'",
      "    await admin.execute('CREATE DATABASE '+identifier)",
      "    created=True",
      "  finally: await admin.close()",
      "  conn=await asyncpg.connect(url, timeout=5)",
      " try:",
      "  version=await conn.fetchval(\"select extversion from pg_extension where extname='vector'\")",
      "  print(json.dumps({'connected':True,'created':created,'pgvector':version or ''}))",
      " finally: await conn.close()",
      "asyncio.run(main())",
    ].join("\n");
    const checked = spawn(python, ["-c", script], {
      encoding: "utf8",
      timeout: 10_000,
      env: {
        ...process.env,
        PUDDINGCLAW_PROBE_DATABASE_URL: databaseUrl,
        PUDDINGCLAW_CREATE_DATABASE_IF_MISSING: createDatabaseIfMissing ? "1" : "0",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (checked.status === 0) {
      const payload = JSON.parse(String(checked.stdout || "{}").trim() || "{}");
      probes.push({
        probe: "database.connection",
        status: "available",
        required: true,
        created: Boolean(payload.created),
        pgvector: payload.pgvector
          ? { status: "available", version: payload.pgvector }
          : requirePgvector
            ? { status: "needs_action", reason: "pgvector extension is not installed in this database" }
            : { status: "skipped", reason: "当前 Profile 不需要 pgvector" },
      });
    } else {
      const stderr = String(checked.stderr || "PostgreSQL authentication failed").trim();
      const missingDependency = /ModuleNotFoundError: No module named ['\"]asyncpg['\"]/.test(stderr);
      probes.push({
        probe: "database.connection",
        status: missingDependency ? "error" : "needs_action",
        required: true,
        ...(missingDependency ? { code: "runtime_dependency_missing" } : {}),
        reason: stderr.split("\n").at(-1),
      });
    }
  }
  if (milvus?.enabled) {
    const script = [
      "import json,os",
      "from pymilvus import MilvusClient",
      "client=MilvusClient(uri=os.environ['PUDDINGCLAW_PROBE_MILVUS_URI'], timeout=5)",
      "print(json.dumps({'collections':client.list_collections()}))",
    ].join("\n");
    const checked = spawn(python, ["-c", script], {
      encoding: "utf8",
      timeout: 10_000,
      env: { ...process.env, PUDDINGCLAW_PROBE_MILVUS_URI: milvus.uri },
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (checked.status === 0) {
      const payload = JSON.parse(String(checked.stdout || "{}").trim() || "{}");
      probes.push({
        probe: "milvus.collections",
        status: "available",
        required: true,
        collection_count: Array.isArray(payload.collections) ? payload.collections.length : 0,
      });
    } else {
      probes.push({
        probe: "milvus.collections",
        status: "needs_action",
        required: true,
        reason: String(checked.stderr || "Milvus handshake failed").trim().split("\n").at(-1),
      });
    }
  }
  return probes;
}
