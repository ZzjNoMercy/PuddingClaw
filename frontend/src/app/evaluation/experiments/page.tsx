"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Download, ExternalLink, Loader2, Plus, RefreshCw } from "lucide-react";
import {
  cancelEvaluationExperiment,
  listEvaluationExperiments,
  retryEvaluationExperiment,
  swebenchPredictionExportUrl,
  syncEvaluationExperiment,
  type EvalExperiment,
} from "@/lib/evaluationApi";
import { experimentIsTerminal, safeRemoteUrl } from "@/lib/evaluationState";

export default function ExperimentsPage() {
  const [items, setItems] = useState<EvalExperiment[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const loadingRef = useRef(false);
  const load = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    try { setItems((await listEvaluationExperiments()).items); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "加载失败"); }
    finally { loadingRef.current = false; setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!items.some((item) => !experimentIsTerminal(item.status))) return;
    const timer = setInterval(() => { if (document.visibilityState === "visible") load(); }, 3000);
    return () => clearInterval(timer);
  }, [items, load]);
  const act = async (id: string, action: () => Promise<unknown>) => {
    if (busyId) return;
    setBusyId(id);
    try { await action(); await load(); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "操作失败"); }
    finally { setBusyId(null); }
  };

  return <div className="workspace-page-container"><div className="mb-6 flex justify-between"><div><h1 className="text-xl font-semibold">Experiments</h1><p className="mt-1 text-sm text-gray-500">本地先落盘评分，再选择性投影到 LangSmith Comparison。</p></div><div className="flex gap-2"><button disabled={loadingRef.current} onClick={load} className="rounded-lg border bg-white p-2"><RefreshCw className="h-4 w-4" /></button><Link href="/evaluation/experiments/new" className="flex items-center gap-2 rounded-lg bg-[#002fa7] px-4 py-2 text-sm text-white"><Plus className="h-4 w-4" />发起评测</Link></div></div>
    {error && <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
    {loading ? <div className="flex justify-center p-12"><Loader2 className="animate-spin" /></div> : <div className="space-y-3">{items.map((item) => {
      const url = safeRemoteUrl(item.remote_url);
      const projection = String(item.summary.langsmith_projection || "");
      return <div key={item.experiment_id} className="rounded-xl border bg-white p-4"><div className="flex items-start justify-between"><div><div className="font-medium">{item.name}</div><div className="mt-1 text-xs text-gray-500">Dataset {item.dataset_id} @ v{item.dataset_version} · {item.candidate.name} · {item.profile_id}</div><div className="mt-2 text-xs text-gray-500">fingerprint {item.candidate.fingerprint || "pending"} · {String(item.summary.case_attempts || 0)} attempts · {String(item.summary.failed_attempts || 0)} execution failures · {String(item.summary.critical_failures || 0)} critical failures · Comparison {projection || "未配置"} · Agent trace {String(item.summary.agent_trace_export || "未配置")}</div>{Boolean(item.summary.swebench_official_harness) && <div className="mt-1 text-xs text-emerald-700">SWE-bench Docker: {String((item.summary.swebench_official_harness as Record<string, unknown>).status || "pending")} · {String((item.summary.swebench_official_harness as Record<string, unknown>).resolved || 0)}/{String((item.summary.swebench_official_harness as Record<string, unknown>).total || 0)} resolved</div>}</div><span className="rounded-full bg-gray-100 px-2.5 py-1 text-xs">{item.status}</span></div><div className="mt-3 flex gap-3 text-xs">{url && <a href={url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1 text-[#002fa7]">LangSmith<ExternalLink className="h-3 w-3" /></a>}{item.status === "completed" && item.summary.swebench_predictions_available === true && <a href={swebenchPredictionExportUrl(item.experiment_id)} className="flex items-center gap-1 text-[#002fa7]">SWE prediction<Download className="h-3 w-3" /></a>}{!experimentIsTerminal(item.status) && <button disabled={busyId === item.experiment_id} onClick={() => act(item.experiment_id, () => cancelEvaluationExperiment(item.experiment_id))} className="text-red-600 disabled:opacity-40">取消</button>}{["failed", "cancelled"].includes(item.status) && <button disabled={busyId === item.experiment_id} onClick={() => act(item.experiment_id, () => retryEvaluationExperiment(item.experiment_id))} className="text-[#002fa7] disabled:opacity-40">重试</button>}{item.status === "completed" && projection === "pending" && <button disabled={busyId === item.experiment_id} onClick={() => act(item.experiment_id, () => syncEvaluationExperiment(item.experiment_id))} className="text-[#002fa7] disabled:opacity-40">补投 LangSmith</button>}{busyId === item.experiment_id && <Loader2 className="h-3 w-3 animate-spin" />}{item.error && <span className="text-red-600">{item.error.message}</span>}</div></div>;
    })}{!items.length && <div className="p-12 text-center text-sm text-gray-400">还没有 Experiment</div>}</div>}
  </div>;
}
