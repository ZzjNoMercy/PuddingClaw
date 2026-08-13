"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  createEvaluationExperiment,
  listEvaluationDatasets,
  listEvaluationDatasetVersions,
  type EvalDataset,
} from "@/lib/evaluationApi";
import { estimateCaseRuns } from "@/lib/evaluationState";
import { getProviders, type ProviderRegistry } from "@/lib/settingsApi";

export default function NewExperimentPage() {
  const router = useRouter();
  const [datasets, setDatasets] = useState<EvalDataset[]>([]);
  const [providerRegistry, setProviderRegistry] = useState<ProviderRegistry | null>(null);
  const [datasetKey, setDatasetKey] = useState("");
  const [name, setName] = useState("");
  const [candidate, setCandidate] = useState("当前 Agent");
  const [model, setModel] = useState("");
  const [credentialName, setCredentialName] = useState("");
  const [repetitions, setRepetitions] = useState(1);
  const [timeout, setTimeoutSeconds] = useState(300);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([
      listEvaluationDatasets().then(async ({ items }) => (
        await Promise.all(items.map((item) => listEvaluationDatasetVersions(item.dataset_id)))
      ).flatMap((result) => result.items)),
      getProviders(),
    ]).then(([versions, registry]) => {
      setDatasets(versions);
      setDatasetKey(versions[0] ? `${versions[0].dataset_id}@${versions[0].current_version}` : "");
      setProviderRegistry(registry);
    }).catch((cause) => setError(cause instanceof Error ? cause.message : "加载配置失败"));
  }, []);

  const selected = useMemo(
    () => datasets.find((item) => `${item.dataset_id}@${item.current_version}` === datasetKey),
    [datasets, datasetKey],
  );
  const isSWEbench = Boolean(selected?.tags.includes("swebench"));
  useEffect(() => {
    if (isSWEbench) setRepetitions(1);
  }, [isSWEbench]);
  const models = useMemo(() => providerRegistry?.providers.flatMap((provider) => (
    provider.models
      .filter((item) => item.capability === "llm"
        && item.categories?.includes("llm")
        && provider.api_keys.some((key) => key.is_default && key.credential_configured))
      .map((item) => ({ provider, model: item }))
  )) || [], [providerRegistry]);
  const selectedModel = models.find((item) => item.model.id === model) || null;
  const selectableKeys = useMemo(
    () => selectedModel?.provider.api_keys.filter((item) => item.credential_configured) || [],
    [selectedModel],
  );

  useEffect(() => {
    if (selectableKeys.length <= 1) {
      setCredentialName("");
    } else if (credentialName && !selectableKeys.some((item) => item.name === credentialName)) {
      setCredentialName("");
    }
  }, [credentialName, selectableKeys]);

  const submit = async () => {
    if (!selected || busy) return;
    setBusy(true);
    setError("");
    try {
      await createEvaluationExperiment({
        name: name || `${selected.name} 回归`,
        dataset_id: selected.dataset_id,
        dataset_version: selected.current_version,
        candidate_request: {
          name: candidate,
          llm_model_id: model || null,
          credential_name: credentialName || null,
          tool_allowlist: [],
          config: {},
        },
        profile_id: selected.default_profile,
        execution: {
          repetitions,
          max_concurrency: 1,
          timeout_seconds: timeout,
          preserve_workspaces: false,
        },
      });
      router.push("/evaluation/experiments");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "创建失败");
      setBusy(false);
    }
  };

  const inputClass = "mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-900";
  return (
    <div className="mx-auto max-w-2xl p-6">
      <h1 className="mb-1 text-xl font-semibold">发起 Experiment</h1>
      <p className="mb-6 text-sm text-gray-500">固定串行执行；Worker 使用独立 session、workspace、memory，并禁用 MCP。Coding Profile 额外开放 kernel 隔离的 execute。</p>
      {error && <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      <div className="space-y-4 rounded-xl border bg-white p-5">
        <label className="block text-xs text-gray-500">Experiment 名称<input value={name} onChange={(event) => setName(event.target.value)} className={inputClass} /></label>
        <label className="block text-xs text-gray-500">Published Dataset Version<select value={datasetKey} onChange={(event) => setDatasetKey(event.target.value)} className={inputClass}>{datasets.map((item) => <option key={`${item.dataset_id}@${item.current_version}`} value={`${item.dataset_id}@${item.current_version}`}>{item.name} · v{item.current_version}</option>)}</select></label>
        <label className="block text-xs text-gray-500">Candidate 名称<input value={candidate} onChange={(event) => setCandidate(event.target.value)} className={inputClass} /></label>
        <label className="block text-xs text-gray-500">LLM 模型（留空使用当前默认）<select value={model} onChange={(event) => { setModel(event.target.value); setCredentialName(""); }} className={inputClass}><option value="">当前默认模型</option>{models.map(({ provider, model: item }) => <option key={item.id} value={item.id}>{provider.name} · {item.name}</option>)}</select></label>
        {selectableKeys.length > 1 && <label className="block text-xs text-gray-500">评测 API Key（不选使用 default）<select value={credentialName} onChange={(event) => setCredentialName(event.target.value)} className={inputClass}><option value="">default</option>{selectableKeys.filter((item) => !item.is_default).map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label>}
        <div className="grid grid-cols-2 gap-4">
          <label className="text-xs text-gray-500">重复次数<input disabled={isSWEbench} type="number" min={1} max={20} value={repetitions} onChange={(event) => setRepetitions(Number(event.target.value))} className={`${inputClass} disabled:bg-gray-50`} /></label>
          <label className="text-xs text-gray-500">单 Case 超时（秒）<input type="number" min={1} max={3600} value={timeout} onChange={(event) => setTimeoutSeconds(Number(event.target.value))} className={inputClass} /></label>
        </div>
        <div className="rounded-lg bg-amber-50 p-3 text-xs text-amber-800">预计执行 {estimateCaseRuns(selected?.cases.length || 0, repetitions)} 个 Case Run。自定义生产 Tool 与 MCP 默认禁用；{isSWEbench ? "Agent 完成后平台会自动启动官方 SWE-bench Docker Harness。首次运行需要拉取/构建镜像，耗时和磁盘占用明显高于普通评测。" : selected?.default_profile === "coding_agent@1" ? "代码题可使用隔离 execute，评分以可信隐藏验证结果为准。" : "workspace 内建文件工具仍可用于 fixture 场景。"}</div>
        <button disabled={busy || !selected} onClick={submit} className="w-full rounded-lg bg-[#002fa7] py-2.5 text-sm text-white disabled:opacity-40">{busy ? "正在创建…" : "创建并运行"}</button>
      </div>
    </div>
  );
}
