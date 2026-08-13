import { Writable } from "node:stream";
import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { spawnSync } from "node:child_process";
import { CliError } from "./errors.js";
import { probeHttpHealth, probeProviderEndpoint, probeTcpEndpoint } from "./probes.js";

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

function providerPreset(selected) {
  if (selected === "1") {
    return {
      id: "deepseek",
      name: "DeepSeek",
      protocol: "deepseek",
      base_url: "https://api.deepseek.com",
      model: "deepseek-chat",
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

function safeDatabaseMetadata(raw) {
  const parsed = new URL(raw.replace(/^postgresql\+asyncpg:/, "postgresql:"));
  return {
    host: parsed.hostname,
    port: Number(parsed.port || 5432),
    database: parsed.pathname.replace(/^\//, ""),
  };
}

export async function discoverExtensionInfrastructure({ profile, flags, nonInteractive }) {
  const knowledgeEnabled = profile === "knowledge" || profile === "full";
  const result = {
    catalog: { mode: "sqlite", host: "", port: 0, database: "", probe_status: "skipped" },
    milvus: { enabled: false, uri: "http://127.0.0.1:19530", probe_status: "skipped" },
    embedding: { status: "disabled", provider: "", model: "" },
    mineru: { enabled: false, base_url: "http://127.0.0.1:8002", probe_status: "skipped" },
    embeddingApiKey: "",
    databaseUrl: "",
    probes: [],
  };
  if (!knowledgeEnabled) return result;

  if (!nonInteractive) {
    output.write("\n知识库依赖探索（按依赖顺序执行）：\n");
    output.write("  1. Catalog 存储 → 2. Milvus 向量索引 → 3. Embedding 配置\n\n");
  }
  let localPostgresProbe = null;
  if (!nonInteractive) {
    output.write("正在探测本机 PostgreSQL 127.0.0.1:5432…\n");
    localPostgresProbe = await probeTcpEndpoint({
      probe: "database.postgresql.discovery",
      host: "127.0.0.1",
      port: 5432,
      required: false,
    });
    result.probes.push(localPostgresProbe);
    output.write(localPostgresProbe.status === "available"
      ? "✓ 发现 PostgreSQL 端口；仍需 URL 验证认证和 pgvector\n"
      : "- 未发现本机 PostgreSQL；可选择轻量 SQLite，或填写远程 PostgreSQL\n");
  }
  const databaseMode = String(flags.database_mode || (nonInteractive
    ? "sqlite"
    : await question(
      "Catalog 存储：[1] PostgreSQL（完整） [2] SQLite（轻量）",
      localPostgresProbe?.status === "available" ? "1" : "2",
    )))
    .toLowerCase();
  const usePostgres = databaseMode === "postgresql" || databaseMode === "1";
  if (usePostgres) {
    const databaseUrl = String(flags.database_url || process.env.PUDDINGCLAW_INIT_DATABASE_URL || (
      nonInteractive ? "" : await secretQuestion("PostgreSQL URL（postgresql+asyncpg://...）")
    )).trim();
    if (!databaseUrl) {
      throw new CliError("PostgreSQL URL is required for the selected knowledge catalog", {
        code: "database_url_required",
      });
    }
    let metadata;
    try {
      metadata = safeDatabaseMetadata(databaseUrl);
    } catch {
      throw new CliError("PostgreSQL URL is invalid", { code: "argument_error" });
    }
    const probe = await probeTcpEndpoint({ probe: "database.postgresql", host: metadata.host, port: metadata.port });
    result.catalog = { mode: "postgresql", ...metadata, probe_status: probe.status };
    result.databaseUrl = databaseUrl;
    result.probes.push(probe);
    if (!nonInteractive) {
      output.write(probe.status === "available"
        ? `✓ PostgreSQL ${metadata.host}:${metadata.port} 端口可访问（认证与 pgvector 将在 Runtime 准备后复检）\n`
        : `! PostgreSQL 不可达：${probe.reason}；配置会保存为待修复状态\n`);
    }
  } else if (!nonInteractive) {
    output.write("- PostgreSQL：已按轻量分支跳过，知识目录使用 Home 内 SQLite\n");
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

export function validatePreparedInfrastructure({ python, databaseUrl, milvus }) {
  const probes = [];
  if (databaseUrl) {
    const script = [
      "import asyncio,json,os",
      "import asyncpg",
      "async def main():",
      " url=os.environ['PUDDINGCLAW_PROBE_DATABASE_URL'].replace('postgresql+asyncpg:', 'postgresql:', 1)",
      " conn=await asyncpg.connect(url, timeout=5)",
      " try:",
      "  version=await conn.fetchval(\"select extversion from pg_extension where extname='vector'\")",
      "  print(json.dumps({'connected':True,'pgvector':version or ''}))",
      " finally: await conn.close()",
      "asyncio.run(main())",
    ].join("\n");
    const checked = spawnSync(python, ["-c", script], {
      encoding: "utf8",
      timeout: 10_000,
      env: { ...process.env, PUDDINGCLAW_PROBE_DATABASE_URL: databaseUrl },
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (checked.status === 0) {
      const payload = JSON.parse(String(checked.stdout || "{}").trim() || "{}");
      probes.push({
        probe: "database.connection",
        status: "available",
        required: true,
        pgvector: payload.pgvector
          ? { status: "available", version: payload.pgvector }
          : { status: "needs_action", reason: "pgvector extension is not installed in this database" },
      });
    } else {
      probes.push({
        probe: "database.connection",
        status: "needs_action",
        required: true,
        reason: String(checked.stderr || "PostgreSQL authentication failed").trim().split("\n").at(-1),
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
    const checked = spawnSync(python, ["-c", script], {
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
