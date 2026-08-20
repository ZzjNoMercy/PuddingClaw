import path from "node:path";
import { CliError, assertArgument } from "./errors.js";
import { buildInitPlan } from "./init-schema.js";
import {
  PROFILES,
  defaultConfig,
  loadConfig,
  saveConfig,
} from "./config.js";
import {
  probeHome,
  probeHttpHealth,
  probeNode,
  probePython,
  probeTcpEndpoint,
  probeUv,
} from "./probes.js";
import { probeDocker } from "./postgres-runtime.js";
import { embeddedRuntimeStatus } from "./runtime-commands.js";
import { loadActiveRuntime } from "./runtime-bundle.js";
import { readJson } from "./store.js";

const PROFILE_LABELS = Object.freeze({
  harness: "Harness 模式",
  knowledge: "知识库模式",
  analytics: "问数模式",
  full: "知识库 + 问数模式",
});

const PREPARED_PROFILE_COVERAGE = Object.freeze({
  harness: new Set(["harness"]),
  knowledge: new Set(["harness", "knowledge"]),
  analytics: new Set(["harness", "analytics"]),
  full: new Set(["harness", "knowledge", "analytics", "full"]),
});

function dependency({
  id,
  label,
  group,
  required,
  status,
  detail = "",
  remediation = [],
  source = "cli",
}) {
  return { id, label, group, required, status, detail, remediation, source };
}

function selectedRuntimeProfile(profile) {
  if (profile === "full") return "full";
  if (profile === "knowledge") return "knowledge";
  if (profile === "analytics") return "analytics";
  return "harness";
}

function preparedFor(prepared, requested) {
  if (!prepared?.python || !prepared?.dependency_profile) return false;
  return PREPARED_PROFILE_COVERAGE[prepared.dependency_profile]?.has(requested) || false;
}

function configuredProvider(config, key) {
  const provider = config?.[key];
  return provider?.status === "configured" && Boolean(provider.model);
}

export async function inspectProfile(profile, paths, {
  packaged = process.env.PUDDINGCLAW_DESKTOP_PACKAGED === "1",
} = {}) {
  if (!Object.hasOwn(PROFILES, profile)) {
    throw new CliError(`unknown profile: ${profile}`, { code: "argument_error" });
  }

  const config = await loadConfig(paths.config);
  const plan = buildInitPlan(profile);
  const node = probeNode();
  let python = probePython(config?.runtime?.python?.base_command || config?.runtime?.python?.command || "");
  if (python.status !== "available") python = probePython();
  let uv = probeUv(config?.runtime?.uv?.command || "");
  if (uv.status !== "available") uv = probeUv();
  const rawHome = await probeHome(paths.home, { create: false });
  const home = rawHome.code === "ENOENT"
    ? { ...rawHome, status: "planned", reason: "确认模式后自动创建" }
    : rawHome;
  const [activeRuntime, embeddedRuntime, prepared, docker] = await Promise.all([
    loadActiveRuntime(paths).catch(() => null),
    embeddedRuntimeStatus(),
    readJson(path.join(paths.runtime, "prepared.json"), null),
    probeDocker(),
  ]);

  const runtimeAvailable = Boolean(activeRuntime || embeddedRuntime.available || !packaged);
  const requestedDependencyProfile = selectedRuntimeProfile(profile);
  const runtimePrepared = preparedFor(prepared, requestedDependencyProfile);
  const dependencies = [
    dependency({
      id: "runtime.cli",
      label: "PuddingClaw 客户端组件",
      group: "core",
      required: true,
      status: "available",
      detail: "桌面客户端运行环境已就绪",
    }),
    dependency({
      id: "runtime.node",
      label: "Node.js 20+ Runtime",
      group: "core",
      required: true,
      status: node.status,
      detail: node.status === "available" ? `Node.js ${node.version}` : "当前 Node.js 版本不受支持",
      remediation: node.status === "available" ? [] : ["请重新安装最新版 PuddingClaw 客户端"],
    }),
    dependency({
      id: "runtime.home",
      label: "PuddingClaw 用户目录",
      group: "core",
      required: true,
      status: home.status,
      detail: home.path || paths.home,
      remediation: home.status === "failed" ? ["检查目录读写权限"] : [],
    }),
    dependency({
      id: "runtime.python",
      label: "Python 3.11 / 3.12",
      group: "core",
      required: true,
      status: python.status,
      detail: python.selected ? `${python.selected.version} · ${python.selected.command}` : "尚未准备兼容的 Python 运行环境",
      remediation: python.selected ? [] : ["确认模式后由客户端自动准备"],
    }),
    dependency({
      id: "runtime.uv",
      label: "uv 依赖管理器",
      group: "core",
      required: true,
      status: uv.status,
      detail: uv.selected ? `uv ${uv.selected.version}` : "依赖管理组件尚未准备",
      remediation: uv.selected ? [] : ["确认模式后由客户端自动准备"],
    }),
    dependency({
      id: "runtime.bundle",
      label: "PuddingClaw Runtime Bundle",
      group: "core",
      required: packaged,
      status: runtimeAvailable ? "available" : "needs_action",
      detail: activeRuntime
        ? `已安装 ${activeRuntime.manifest.release_version}`
        : embeddedRuntime.available
          ? `安装包内置 ${embeddedRuntime.release_version}`
          : packaged ? "安装包未包含 Runtime Bundle" : "源码开发模式直接使用仓库 Runtime",
      remediation: runtimeAvailable ? [] : ["重新安装包含 Runtime Bundle 的桌面发行包"],
    }),
    dependency({
      id: "runtime.dependencies",
      label: `${PROFILE_LABELS[profile]} Python 依赖`,
      group: "core",
      required: true,
      status: runtimePrepared || !packaged ? "available" : "needs_action",
      detail: runtimePrepared
        ? `已准备 ${prepared.dependency_profile} 依赖集`
        : packaged ? "当前模式的运行环境尚未准备" : "源码开发环境由 backend/.venv 提供",
      remediation: runtimePrepared || !packaged ? [] : ["确认模式后由客户端自动准备"],
    }),
    dependency({
      id: "catalog.sqlite",
      label: "SQLite Core Catalog",
      group: "core",
      required: true,
      status: ["available", "planned"].includes(home.status) ? "available" : "needs_action",
      detail: "默认本地数据库；不要求 PostgreSQL 或 Docker",
    }),
    dependency({
      id: "provider.agent",
      label: "Agent 模型 Provider",
      group: "configuration",
      required: false,
      status: configuredProvider(config, "provider") ? "available" : "not_configured",
      detail: configuredProvider(config, "provider") ? config.provider.model : "进入设置后绑定模型与凭据",
      remediation: configuredProvider(config, "provider") ? [] : ["在模型服务设置中完成绑定"],
    }),
    dependency({
      id: "runtime.docker",
      label: "Docker 沙箱",
      group: "optional",
      required: false,
      status: docker.status === "available" ? "available" : "optional_unavailable",
      detail: docker.status === "available" ? "Docker daemon 可用" : (docker.reason || "不可用时回退到内核沙箱"),
      remediation: docker.status === "available" ? [] : ["需要容器隔离时启动 Docker Desktop"],
    }),
  ];

  if (PROFILES[profile].knowledge) {
    const [milvus, mineru] = await Promise.all([
      probeTcpEndpoint({
        probe: "milvus.connection",
        host: "127.0.0.1",
        port: 19530,
        required: false,
      }),
      probeHttpHealth({
        probe: "mineru.health",
        baseUrl: config?.infrastructure?.mineru?.base_url || "http://127.0.0.1:8002",
      }),
    ]);
    dependencies.push(
      dependency({
        id: "provider.multimodal",
        label: "Embedding / 多模态模型",
        group: "knowledge",
        required: false,
        status: configuredProvider(config, "multimodal_provider") ? "available" : "not_configured",
        detail: configuredProvider(config, "multimodal_provider")
          ? config.multimodal_provider.model
          : "启用图文向量检索时配置；本地精确检索不依赖它",
        remediation: configuredProvider(config, "multimodal_provider") ? [] : ["需要语义检索时在模型服务中绑定"],
      }),
      dependency({
        id: "knowledge.milvus",
        label: "Milvus 向量库",
        group: "knowledge",
        required: false,
        status: milvus.status === "available" ? "available" : "optional_unavailable",
        detail: milvus.status === "available" ? "127.0.0.1:19530 可达" : "可选；不可用时仍可使用文件与精确检索",
        remediation: milvus.status === "available" ? [] : ["如需向量检索，可稍后在知识库设置中启用"],
      }),
      dependency({
        id: "knowledge.mineru",
        label: "MinerU 富文档解析",
        group: "knowledge",
        required: false,
        status: mineru.status === "available" ? "available" : "optional_unavailable",
        detail: mineru.status === "available" ? "MinerU 健康检查通过" : "可选；仅影响 PDF/Office 高质量解析",
        remediation: mineru.status === "available" ? [] : ["需要富文档解析时启动 MinerU"],
      }),
      dependency({
        id: "knowledge.gbrain",
        label: "PostgreSQL + pgvector（gbrain）",
        group: "knowledge",
        required: false,
        status: "not_configured",
        detail: "仅 LLM Wiki / gbrain 路径需要；普通知识库不要求",
        remediation: ["启用 gbrain 时再配置 PostgreSQL 与 pgvector"],
      }),
    );
  }

  if (PROFILES[profile].analytics) {
    dependencies.push(
      dependency({
        id: "analytics.datasource",
        label: "问数数据源",
        group: "analytics",
        required: false,
        status: "not_configured",
        detail: "支持 Excel / CSV / TSV；数据库连接可在进入应用后添加",
        remediation: ["在智能问数中导入文件或配置只读数据库连接"],
      }),
      dependency({
        id: "analytics.postgres_driver",
        label: "PostgreSQL 数据源驱动",
        group: "analytics",
        required: false,
        status: runtimePrepared && ["analytics", "full"].includes(prepared.dependency_profile)
          ? "available"
          : "not_configured",
        detail: "只有连接 PostgreSQL 业务数据源时需要；文件问数不依赖",
        remediation: ["连接 PostgreSQL 数据源时由客户端按需准备"],
      }),
    );
  }

  const blocking = dependencies.filter((item) => item.required
    && !["available", "planned"].includes(item.status));
  return {
    schema_version: 1,
    status: blocking.length ? "needs_action" : "ready",
    profile,
    label: PROFILE_LABELS[profile],
    initialized: Boolean(config?.initialized),
    current_profile: config?.profile || null,
    extensions: plan.extensions,
    dependency_profile: requestedDependencyProfile,
    plan,
    dependencies,
    blocking: blocking.map((item) => item.id),
    actions: {
      can_apply: true,
      can_prepare: Boolean(embeddedRuntime.available || activeRuntime),
    },
  };
}

export async function applyProfile(profile, paths) {
  if (!Object.hasOwn(PROFILES, profile)) {
    throw new CliError(`unknown profile: ${profile}`, { code: "argument_error" });
  }
  const home = await probeHome(paths.home, { create: true });
  if (home.status !== "available") {
    throw new CliError(home.reason || "deploy home is unavailable", {
      code: "home_unavailable",
      details: home,
    });
  }
  const existing = await loadConfig(paths.config);
  const config = existing || defaultConfig({ profile });
  config.profile = profile;
  for (const [name, enabled] of Object.entries(PROFILES[profile])) {
    config.extensions[name] = { ...config.extensions[name], enabled };
  }
  config.initialized = true;
  config.initialized_at ||= new Date().toISOString();
  const python = probePython(config.runtime?.python?.base_command || config.runtime?.python?.command || "");
  const uv = probeUv(config.runtime?.uv?.command || "");
  config.runtime = {
    ...config.runtime,
    ...(python.selected ? { python: { ...config.runtime?.python, ...python.selected } } : {}),
    ...(uv.selected ? { uv: { ...config.runtime?.uv, ...uv.selected } } : {}),
  };
  await saveConfig(paths.config, config);
  return {
    status: "applied",
    profile,
    label: PROFILE_LABELS[profile],
    config_path: paths.config,
    extensions: config.extensions,
    inspection: await inspectProfile(profile, paths),
  };
}

export async function profileCommand(args, paths) {
  const [action, profile] = args;
  assertArgument(action === "inspect" || action === "apply", "usage: profile inspect|apply <profile>");
  assertArgument(Boolean(profile), `usage: profile ${action} <profile>`);
  if (action === "inspect") return inspectProfile(profile, paths);
  return applyProfile(profile, paths);
}
