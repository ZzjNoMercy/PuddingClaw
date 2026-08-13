import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { CliError } from "./errors.js";
import { defaultConfig, PROFILES, saveConfig } from "./config.js";
import { integerFlag } from "./args.js";
import { findFreePort, probeHome, probeNode, probePlatform, probePort, probePython, probeUv } from "./probes.js";
import { prepareManagedPython } from "./python-runtime.js";
import { readJson } from "./store.js";
import { buildInitPlan } from "./init-schema.js";
import { embeddedRuntimeStatus } from "./runtime-commands.js";
import { installRuntimeBundle } from "./runtime-bundle.js";
import { prepareRuntimePython } from "./runtime-python.js";
import { bootstrapUv } from "./uv-runtime.js";
import { ensureLocalWorkerToken } from "./local-worker-token.js";
import {
  discoverExtensionInfrastructure,
  discoverInitialProvider,
  validatePreparedInfrastructure,
} from "./init-discovery.js";
import { writeSecret } from "./secrets.js";

const PROFILE_LABELS = {
  harness: "只使用 Agent Harness",
  knowledge: "Agent Harness + 知识库",
  analytics: "Agent Harness + 智能问数",
  full: "完整功能",
};

async function promptProfile() {
  const rl = createInterface({ input, output });
  try {
    output.write("请选择初始化方案：\n\n");
    output.write("[1] 只使用 Agent Harness（推荐首次体验）\n");
    output.write("[2] Agent Harness + 知识库\n");
    output.write("[3] Agent Harness + 智能问数\n");
    output.write("[4] 完整功能\n\n");
    const selected = String(await rl.question("请选择 [1]：")).trim() || "1";
    const profile = { 1: "harness", 2: "knowledge", 3: "analytics", 4: "full" }[selected];
    if (!profile) throw new CliError("invalid profile selection", { code: "argument_error" });
    return profile;
  } finally {
    rl.close();
  }
}

async function confirmSummary(summary) {
  const rl = createInterface({ input, output });
  try {
    output.write("\n即将写入配置：\n");
    output.write(`  Profile:  ${PROFILE_LABELS[summary.profile]}\n`);
    output.write(`  Home:     ${summary.home}\n`);
    output.write(`  Backend:  127.0.0.1:${summary.backend_port}\n`);
    output.write(`  Frontend: 127.0.0.1:${summary.frontend_port}\n`);
    output.write(`  Provider: ${summary.provider?.name || "稍后配置"}${summary.provider?.model ? ` / ${summary.provider.model}` : ""}\n`);
    if (summary.infrastructure?.catalog) {
      output.write(`  Catalog:  ${summary.infrastructure.catalog.mode}\n`);
      output.write(`  Milvus:   ${summary.infrastructure.milvus.enabled ? summary.infrastructure.milvus.uri : "disabled"}\n`);
    }
    const answer = String(await rl.question("\n确认初始化？[Y/n] ")).trim().toLowerCase();
    return answer === "" || answer === "y" || answer === "yes";
  } finally {
    rl.close();
  }
}

async function confirmPythonPreparation() {
  const rl = createInterface({ input, output });
  try {
    output.write("\n未找到兼容的 Python 3.11/3.12。\n");
    output.write("PuddingClaw 可以使用 uv 在独立 Home 中准备 Python 3.12，且不会修改系统 Python。\n");
    const answer = String(await rl.question("现在一键准备？[Y/n] ")).trim().toLowerCase();
    return answer === "" || answer === "y" || answer === "yes";
  } finally {
    rl.close();
  }
}

async function confirmRuntimeInstallation(releaseVersion) {
  const rl = createInterface({ input, output });
  try {
    output.write(`\n安装包包含 PuddingClaw Runtime ${releaseVersion}。\n`);
    output.write("下一步会在独立 Home 中安装 Backend/Web，并使用 uv 下载锁定的 Python 依赖。\n");
    const answer = String(await rl.question("现在安装 Runtime？[Y/n] ")).trim().toLowerCase();
    return answer === "" || answer === "y" || answer === "yes";
  } finally {
    rl.close();
  }
}

async function promptReplacementPort(role, currentPort, probeResult) {
  const suggested = await findFreePort(currentPort + 1);
  const rl = createInterface({ input, output });
  try {
    const owner = probeResult?.owner;
    output.write(`\n${role === "backend" ? "后端" : "前端"}端口 ${currentPort} 已被占用。\n`);
    if (owner) output.write(`占用进程：PID ${owner.pid} (${owner.command})\n`);
    output.write("该进程未通过本 CLI 的实例身份校验，init 不会终止它。\n");
    output.write(`直接回车使用可用端口 ${suggested}；输入其他端口可自行指定；输入 q 退出。\n`);
    while (true) {
      const answer = String(await rl.question(`新端口 [${suggested}]：`)).trim().toLowerCase();
      if (answer === "q" || answer === "quit") {
        throw new CliError("initialization cancelled due to port conflict", {
          code: "cancelled",
          exitCode: 1,
        });
      }
      if (!answer) return suggested;
      let candidate;
      try {
        candidate = integerFlag(answer, "port");
      } catch (error) {
        output.write(`${error.message}\n`);
        continue;
      }
      const candidateProbe = await probePort(candidate);
      if (candidateProbe.status === "available") return candidate;
      output.write(`端口 ${candidate} 仍被占用，请换一个。\n`);
    }
  } finally {
    rl.close();
  }
}

async function selectInitPorts({ backendPort, frontendPort, automatic, nonInteractive }) {
  let selectedBackend = backendPort;
  let selectedFrontend = frontendPort;
  while (true) {
    try {
      return await selectPorts({
        backendPort: selectedBackend,
        frontendPort: selectedFrontend,
        automatic,
      });
    } catch (error) {
      if (error?.code !== "port_occupied" || nonInteractive || automatic) throw error;
      const backendConflict = error.message.startsWith("backend");
      if (backendConflict) {
        selectedBackend = await promptReplacementPort("backend", selectedBackend, error.details);
      } else {
        selectedFrontend = await promptReplacementPort("frontend", selectedFrontend, error.details);
      }
    }
  }
}

export async function runInit({ flags, paths, interactive = process.stdin.isTTY && process.stdout.isTTY }) {
  const nonInteractive = Boolean(flags.non_interactive) || !interactive;
  let profile = String(flags.profile || "").trim();
  if (!profile) {
    if (nonInteractive) {
      throw new CliError("--profile is required in non-interactive mode", { code: "argument_error" });
    }
    profile = await promptProfile();
  }
  if (!Object.hasOwn(PROFILES, profile)) {
    throw new CliError(`unknown profile: ${profile}`, { code: "argument_error" });
  }
  const settingsPlan = buildInitPlan(profile);
  if (flags.plan) return { status: "plan", ...settingsPlan };

  const existing = await readJson(paths.config, null);
  if (existing && !flags.force) {
    throw new CliError("PuddingClaw Deploy CLI is already initialized; pass --force to replace non-secret config", {
      code: "already_initialized",
      exitCode: 1,
    });
  }

  const homeProbe = await probeHome(paths.home, { create: true });
  if (homeProbe.status !== "available") {
    throw new CliError(homeProbe.reason || "deploy home is unavailable", {
      code: "home_unavailable",
      exitCode: 1,
      details: homeProbe,
    });
  }

  const providerDiscovery = await discoverInitialProvider({ flags, nonInteractive });
  const infrastructureDiscovery = await discoverExtensionInfrastructure({
    profile,
    flags,
    nonInteractive,
  });

  let pythonProbe = probePython(flags.python);
  let uvProbe = probeUv(flags.uv);
  const shouldPreparePython = pythonProbe.status !== "available" && (
    flags.prepare_python
    || (!nonInteractive && await confirmPythonPreparation())
  );
  if (shouldPreparePython) {
    if (uvProbe.status !== "available") {
      const bootstrapped = await bootstrapUv(paths.home);
      uvProbe = { ...probeUv(bootstrapped.selected.command), selected: bootstrapped.selected };
    }
    const prepared = await prepareManagedPython(paths.home, { uvCommand: uvProbe.selected.command });
    pythonProbe = {
      probe: "runtime.python",
      status: "available",
      required: true,
      selected: prepared.selected,
      interpreters: [prepared.selected],
      remediation: [],
    };
  }
  if (pythonProbe.status !== "available") {
    throw new CliError("compatible Python 3.11/3.12 was not found", {
      code: "python_required",
      exitCode: 1,
      details: { python: pythonProbe, uv: uvProbe },
    });
  }

  const requestedBackendPort = flags.backend_port ? integerFlag(flags.backend_port, "--backend-port") : 8888;
  const requestedFrontendPort = flags.frontend_port ? integerFlag(flags.frontend_port, "--frontend-port") : 3000;
  const selectedPorts = await selectInitPorts({
    backendPort: requestedBackendPort,
    frontendPort: requestedFrontendPort,
    automatic: flags.port === "auto",
    nonInteractive,
  });
  const { backendPort, frontendPort, initialBackendProbe, initialFrontendProbe } = selectedPorts;

  const summary = {
    profile,
    home: paths.home,
    provider: providerDiscovery.provider,
    infrastructure: {
      catalog: infrastructureDiscovery.catalog,
      milvus: infrastructureDiscovery.milvus,
      embedding: infrastructureDiscovery.embedding,
      mineru: infrastructureDiscovery.mineru,
    },
    backend_port: backendPort,
    frontend_port: frontendPort,
    probes: {
      platform: await probePlatform(),
      node: probeNode(),
      python: pythonProbe,
      uv: uvProbe,
      home: homeProbe,
      requested_backend_port: initialBackendProbe,
      requested_frontend_port: initialFrontendProbe,
      provider: providerDiscovery.probe,
      extensions: infrastructureDiscovery.probes,
    },
    settings_plan: {
      field_count: settingsPlan.field_count,
      probe_count: settingsPlan.probe_count,
      selected_steps: settingsPlan.steps.filter((step) => step.status === "selected").map((step) => step.id),
      disabled_steps: settingsPlan.steps.filter((step) => step.status === "disabled").map((step) => step.id),
    },
  };

  if (!nonInteractive && !flags.yes && !(await confirmSummary(summary))) {
    throw new CliError("initialization cancelled", { code: "cancelled", exitCode: 1 });
  }

  const config = defaultConfig({ profile, backendPort, frontendPort });
  config.initialized = true;
  config.initialized_at = new Date().toISOString();
  config.runtime = {
    python: pythonProbe.selected,
    uv: uvProbe.selected,
  };
  config.provider = {
    ...config.provider,
    ...providerDiscovery.provider,
  };
  config.infrastructure = {
    catalog: infrastructureDiscovery.catalog,
    milvus: infrastructureDiscovery.milvus,
    embedding: infrastructureDiscovery.embedding,
    mineru: infrastructureDiscovery.mineru,
  };
  const embedded = await embeddedRuntimeStatus();
  if (flags.install_runtime && !embedded.available) {
    throw new CliError("this npm package does not contain an embedded runtime", {
      code: "runtime_not_bundled",
      exitCode: 1,
    });
  }
  await ensureLocalWorkerToken(paths);
  if (providerDiscovery.apiKey) await writeSecret(paths.providerApiKey, providerDiscovery.apiKey);
  if (infrastructureDiscovery.embeddingApiKey) {
    await writeSecret(paths.embeddingApiKey, infrastructureDiscovery.embeddingApiKey);
  }
  if (infrastructureDiscovery.databaseUrl) {
    await writeSecret(paths.databaseUrl, infrastructureDiscovery.databaseUrl);
  }
  await saveConfig(paths.config, config);
  const installRequested = embedded.available && (
    flags.install_runtime
    || (!nonInteractive && await confirmRuntimeInstallation(embedded.release_version))
  );
  let runtime = { status: embedded.available ? "available" : "not_bundled" };
  if (installRequested) {
    try {
      const installed = await installRuntimeBundle(embedded.path, paths);
      const prepared = await prepareRuntimePython(paths, { allowUvBootstrap: true });
      const validation = validatePreparedInfrastructure({
        python: prepared.python,
        databaseUrl: infrastructureDiscovery.databaseUrl,
        milvus: infrastructureDiscovery.milvus,
      });
      runtime = { status: "prepared", installed, prepared, infrastructure_validation: validation };
    } catch (error) {
      if (error?.code !== "runtime_already_installed") throw error;
      const prepared = await prepareRuntimePython(paths, { allowUvBootstrap: true });
      const validation = validatePreparedInfrastructure({
        python: prepared.python,
        databaseUrl: infrastructureDiscovery.databaseUrl,
        milvus: infrastructureDiscovery.milvus,
      });
      runtime = {
        status: "prepared",
        installed: { status: "already_installed" },
        prepared,
        infrastructure_validation: validation,
      };
    }
  }
  return { schema_version: 1, status: "initialized", ...summary, config_path: paths.config, runtime };
}

export async function selectPorts({
  backendPort,
  frontendPort,
  automatic,
  probe = probePort,
  findFree = findFreePort,
}) {
  const initialBackendProbe = await probe(backendPort);
  const initialFrontendProbe = await probe(frontendPort);
  let selectedBackend = backendPort;
  let selectedFrontend = frontendPort;
  if (initialBackendProbe.status !== "available") {
    if (!automatic) {
      throw new CliError(`backend port ${backendPort} is occupied`, {
        code: "port_occupied",
        exitCode: 1,
        details: initialBackendProbe,
      });
    }
    selectedBackend = await findFree(backendPort + 1);
  }
  if (initialFrontendProbe.status !== "available" || selectedFrontend === selectedBackend) {
    if (!automatic) {
      throw new CliError(`frontend port ${frontendPort} is occupied`, {
        code: "port_occupied",
        exitCode: 1,
        details: selectedFrontend === selectedBackend
          ? { ...initialFrontendProbe, status: "conflict", reason: "frontend and backend ports must differ" }
          : initialFrontendProbe,
      });
    }
    selectedFrontend = await findFree(Math.max(frontendPort + 1, selectedBackend + 1));
  }
  return {
    backendPort: selectedBackend,
    frontendPort: selectedFrontend,
    initialBackendProbe,
    initialFrontendProbe,
  };
}
