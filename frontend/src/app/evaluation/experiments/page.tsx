"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { CheckCircle2, ChevronDown, ChevronRight, Download, ExternalLink, Loader2, Plus, RefreshCw, Trash2, XCircle } from "lucide-react";
import {
  cancelEvaluationExperiment,
  deleteEvaluationExperiment,
  getEvaluationExperimentResults,
  listEvaluationExperiments,
  retryEvaluationExperiment,
  swebenchPredictionExportUrl,
  syncEvaluationExperiment,
  type EvalExperiment,
  type EvalExperimentResultRow,
} from "@/lib/evaluationApi";
import { experimentIsTerminal, safeRemoteUrl } from "@/lib/evaluationState";

const actionClass =
  "inline-flex h-8 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-3 text-xs font-medium text-gray-700 transition hover:border-gray-300 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40";

const progressStageLabels: Record<string, string> = {
  queued: "等待 Worker",
  preparing: "准备隔离环境",
  candidate_environment: "准备 Agent 依赖环境",
  agent_running: "Agent 执行中",
  case_completed: "Case 已完成",
  official_verifier: "Docker 判卷中",
  scoring: "汇总评分",
  langsmith_projection: "投影 LangSmith",
  cancel_requested: "正在取消",
  completed: "评测完成",
  failed: "执行失败",
};

function numberValue(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function elapsedLabel(startedAt?: string | null): string {
  if (!startedAt) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  if (seconds < 60) return `已运行 ${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `已运行 ${minutes} 分 ${seconds % 60} 秒`;
}

function displayExperimentName(name: string): string {
  // Older retries appended this marker to the persisted title. Keep the list
  // readable while the backend now records lineage as metadata instead.
  return name.replace(/(?:\s*\(retry\))+\s*$/i, "").trim() || name;
}

interface AttemptView {
  attemptId: string;
  caseId: string;
  repetition: number;
  status: EvalExperimentResultRow["attempt_status"];
  error?: EvalExperimentResultRow["error"];
  createdAt?: string | null;
  latencyMs?: number | null;
  outcomes: string[];
}

function attemptViews(rows: EvalExperimentResultRow[]): AttemptView[] {
  const attempts = new Map<string, AttemptView>();
  for (const row of rows) {
    const current = attempts.get(row.attempt_id) || {
      attemptId: row.attempt_id,
      caseId: row.case_id,
      repetition: row.repetition,
      status: row.attempt_status,
      error: row.error,
      createdAt: row.created_at,
      latencyMs: row.latency_ms,
      outcomes: [],
    };
    current.status = row.attempt_status;
    current.error = row.error || current.error;
    current.latencyMs = row.latency_ms ?? current.latencyMs;
    const outcome = String(row.result?.outcome || "");
    if (outcome && !current.outcomes.includes(outcome)) current.outcomes.push(outcome);
    attempts.set(row.attempt_id, current);
  }
  return Array.from(attempts.values());
}

function attemptDuration(attempt: AttemptView): string {
  const milliseconds = attempt.latencyMs ?? (
    attempt.status === "running" && attempt.createdAt
      ? Date.now() - new Date(attempt.createdAt).getTime()
      : 0
  );
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "";
  const seconds = Math.max(1, Math.round(milliseconds / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function LiveAttemptResults({ rows }: { rows: EvalExperimentResultRow[] }) {
  const attempts = attemptViews(rows);
  const [open, setOpen] = useState(false);
  if (attempts.length === 0) return null;
  const failures = attempts.filter((attempt) => attempt.status === "failed").length;
  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-slate-200 bg-slate-50/60">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
      >
        <span className="inline-flex items-center gap-2 text-xs font-semibold text-slate-700">
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          实时结果
          <span className="font-normal tabular-nums text-slate-400">{attempts.length} 个 Case</span>
        </span>
        {failures > 0 ? <span className="text-xs font-medium text-rose-600">{failures} 个执行失败</span> : null}
      </button>
      {open ? (
        <div className="divide-y divide-slate-200 border-t border-slate-200">
          {attempts.map((attempt) => {
            const failed = attempt.status === "failed";
            const running = attempt.status === "running" || attempt.status === "queued";
            const duration = attemptDuration(attempt);
            return (
              <div key={attempt.attemptId} className="flex items-start gap-2.5 px-3 py-2.5 text-xs">
                {running ? (
                  <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-[#002fa7]" />
                ) : failed ? (
                  <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-500" />
                ) : (
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="min-w-0 truncate font-medium text-slate-700">{attempt.caseId}</span>
                    <span className={`shrink-0 font-medium ${failed ? "text-rose-600" : running ? "text-[#002fa7]" : "text-emerald-600"}`}>
                      {failed ? "执行失败" : running ? "执行中" : "已完成"}
                    </span>
                    {duration ? <span className="shrink-0 tabular-nums text-slate-400">{duration}</span> : null}
                    {attempt.repetition > 0 ? <span className="shrink-0 text-slate-400">第 {attempt.repetition + 1} 次</span> : null}
                  </div>
                  {attempt.error?.message ? (
                    <p className="mt-1 break-words leading-5 text-rose-600">{attempt.error.message}</p>
                  ) : attempt.outcomes.length > 0 ? (
                    <p className="mt-1 text-slate-500">判定：{attempt.outcomes.join(" / ")}</p>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function ExperimentProgress({ item }: { item: EvalExperiment }) {
  if (experimentIsTerminal(item.status)) return null;
  const progress = (item.summary.progress || {}) as Record<string, unknown>;
  const stage = String(progress.stage || (item.status === "queued" ? "queued" : "preparing"));
  const total = numberValue(progress.total);
  const completed = numberValue(progress.completed);
  const failed = numberValue(progress.failed);
  const percent = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  const currentCase = String(progress.current_case_name || "");
  const message = String(progress.message || "Worker 已启动，等待首个进度事件");
  const elapsed = elapsedLabel(item.started_at);

  return (
    <div className="mt-4 rounded-xl border border-blue-100 bg-blue-50/50 px-4 py-3">
      <div className="flex items-center justify-between gap-4">
        <div className="flex min-w-0 items-center gap-2">
          {!experimentIsTerminal(item.status) && <Loader2 className="h-4 w-4 shrink-0 animate-spin text-[#002fa7]" />}
          <span className="shrink-0 text-sm font-medium text-slate-800">
            {progressStageLabels[stage] || stage}
          </span>
          <span className="truncate text-xs text-slate-500">{message}</span>
        </div>
        <span className="shrink-0 text-xs font-semibold tabular-nums text-[#002fa7]">
          {total > 0 ? `${completed} / ${total}` : "初始化中"}
        </span>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-blue-100">
        {total > 0 ? (
          <div
            className="h-full rounded-full bg-[#002fa7] transition-[width] duration-500 ease-out"
            style={{ width: `${percent}%` }}
          />
        ) : (
          <div className="h-full w-1/3 animate-pulse rounded-full bg-[#002fa7]/70" />
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs text-slate-500">
        <span className="min-w-0 truncate">
          {currentCase ? `当前：${currentCase}` : "正在建立独立 session、workspace 与 memory"}
        </span>
        <div className="flex shrink-0 gap-4 tabular-nums">
          {elapsed && <span>{elapsed}</span>}
          {total > 0 && <span>{percent}%</span>}
          <span>失败 {failed}</span>
        </div>
      </div>
    </div>
  );
}

export default function ExperimentsPage() {
  const [items, setItems] = useState<EvalExperiment[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<EvalExperiment | null>(null);
  const [resultsByExperiment, setResultsByExperiment] = useState<Record<string, EvalExperimentResultRow[]>>({});
  const [error, setError] = useState("");
  const loadingRef = useRef(false);

  const load = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    try {
      const response = await listEvaluationExperiments();
      setItems(response.items);
      const resultTargets = response.items.filter((item, index) =>
        !experimentIsTerminal(item.status) || index < 5
      );
      const resultResponses = await Promise.allSettled(
        resultTargets.map(async (item) => ({
          id: item.experiment_id,
          rows: (await getEvaluationExperimentResults(item.experiment_id)).items,
        })),
      );
      setResultsByExperiment((current) => {
        const next = { ...current };
        for (const result of resultResponses) {
          if (result.status === "fulfilled") next[result.value.id] = result.value.rows;
        }
        return next;
      });
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!items.some((item) => !experimentIsTerminal(item.status))) return;
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 2000);
    return () => clearInterval(timer);
  }, [items, load]);

  const act = async (id: string, action: () => Promise<unknown>) => {
    if (busyId) return;
    setBusyId(id);
    try {
      await action();
      await load();
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusyId(null);
    }
  };

  const remove = (item: EvalExperiment) => {
    setPendingDelete(null);
    void act(item.experiment_id, () => deleteEvaluationExperiment(item.experiment_id));
  };

  return (
    <div className="workspace-page-container">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">Experiments</h1>
          <p className="mt-1 text-sm text-gray-500">本地先落盘评分，再选择性投影到 LangSmith Comparison。</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            disabled={loadingRef.current}
            onClick={load}
            aria-label="刷新评测列表"
            title="刷新"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-600 shadow-sm transition hover:bg-slate-50 disabled:opacity-40"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
          <Link
            href="/evaluation/experiments/new"
            className="inline-flex h-9 items-center gap-1.5 whitespace-nowrap rounded-lg bg-[#002fa7] px-3.5 text-sm font-medium text-white shadow-sm transition hover:bg-[#002583]"
          >
            <Plus className="h-4 w-4" />发起评测
          </Link>
        </div>
      </div>

      {error && <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      {loading ? (
        <div className="flex justify-center p-12"><Loader2 className="animate-spin" /></div>
      ) : (
        <div className="space-y-3">
          {items.map((item) => {
            const url = safeRemoteUrl(item.remote_url);
            const projection = String(item.summary.langsmith_projection || "");
            const busy = busyId === item.experiment_id;
            const liveProgress = (item.summary.progress || {}) as Record<string, unknown>;
            const visibleAttempts = experimentIsTerminal(item.status)
              ? numberValue(item.summary.case_attempts)
              : numberValue(liveProgress.completed);
            const visibleFailures = experimentIsTerminal(item.status)
              ? numberValue(item.summary.failed_attempts)
              : numberValue(liveProgress.failed);
            const terminal = experimentIsTerminal(item.status);
            const showSecondaryActions = Boolean(
              url
              || (item.status === "completed" && item.summary.swebench_predictions_available === true)
              || (item.status === "completed" && projection === "pending")
              || item.error,
            );
            return (
              <div key={item.experiment_id} className="rounded-xl border bg-white p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="font-medium">{displayExperimentName(item.name)}</div>
                    <div className="mt-1 text-xs text-gray-500">
                      Dataset {item.dataset_id} @ v{item.dataset_version} · {item.candidate.name} · {item.profile_id}
                    </div>
                    <div className="mt-2 text-xs text-gray-500">
                      fingerprint {item.candidate.fingerprint || "pending"} · {visibleAttempts} attempts · {visibleFailures} execution failures · {String(item.summary.critical_failures || 0)} critical failures · Comparison {projection || "未配置"} · Agent trace {String(item.summary.agent_trace_export || "未配置")}
                    </div>
                    {Boolean(item.summary.swebench_official_harness) && (
                      <div className="mt-1 text-xs text-emerald-700">
                        SWE-bench Docker: {String((item.summary.swebench_official_harness as Record<string, unknown>).status || "pending")} · {String((item.summary.swebench_official_harness as Record<string, unknown>).resolved || 0)}/{String((item.summary.swebench_official_harness as Record<string, unknown>).total || 0)} resolved
                      </div>
                    )}
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
                    <span className="rounded-full bg-gray-100 px-2.5 py-1 text-xs">{item.status}</span>
                    {terminal ? (
                      <button
                        disabled={busy}
                        onClick={() => act(item.experiment_id, () => retryEvaluationExperiment(item.experiment_id))}
                        className={`${actionClass} border-blue-200 text-[#002fa7] hover:border-blue-300 hover:bg-blue-50`}
                      >
                        <RefreshCw className="h-3.5 w-3.5" />重新评测
                      </button>
                    ) : (
                      <button
                        disabled={busy}
                        onClick={() => act(item.experiment_id, () => cancelEvaluationExperiment(item.experiment_id))}
                        className={`${actionClass} border-red-200 text-red-600 hover:border-red-300 hover:bg-red-50`}
                      >
                        取消
                      </button>
                    )}
                    {terminal && (
                      <button
                        disabled={busy}
                        onClick={() => setPendingDelete(item)}
                        aria-label={`删除评测 ${displayExperimentName(item.name)}`}
                        title="删除"
                        className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-red-200 bg-white text-red-600 transition hover:border-red-300 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                    {busy && <Loader2 className="h-3.5 w-3.5 animate-spin text-gray-400" />}
                  </div>
                </div>

                <ExperimentProgress item={item} />
                <LiveAttemptResults rows={resultsByExperiment[item.experiment_id] || []} />

                {showSecondaryActions && <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-gray-100 pt-3">
                  {url && (
                    <a href={url} target="_blank" rel="noopener noreferrer" className={actionClass}>
                      LangSmith<ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  )}
                  {item.status === "completed" && item.summary.swebench_predictions_available === true && (
                    <a href={swebenchPredictionExportUrl(item.experiment_id)} className={actionClass}>
                      SWE prediction<Download className="h-3.5 w-3.5" />
                    </a>
                  )}
                  {item.status === "completed" && projection === "pending" && (
                    <button
                      disabled={busy}
                      onClick={() => act(item.experiment_id, () => syncEvaluationExperiment(item.experiment_id))}
                      className={`${actionClass} text-[#002fa7]`}
                    >
                      补投 LangSmith
                    </button>
                  )}
                  {item.error && <span className="text-xs text-red-600">{item.error.message}</span>}
                </div>}
              </div>
            );
          })}
          {!items.length && <div className="p-12 text-center text-sm text-gray-400">还没有 Experiment</div>}
        </div>
      )}

      {pendingDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/35 p-4 backdrop-blur-[1px]"
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-experiment-title"
        >
          <div className="w-full max-w-md rounded-2xl border border-gray-200 bg-white p-6 shadow-2xl">
            <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-full bg-red-50 text-red-600">
              <Trash2 className="h-5 w-5" />
            </div>
            <h2 id="delete-experiment-title" className="text-lg font-semibold text-gray-900">删除这次评测？</h2>
            <p className="mt-2 text-sm leading-6 text-gray-600">
              “{displayExperimentName(pendingDelete.name)}”的本地 attempts、评分结果和运行文件会被永久删除，无法恢复。
            </p>
            <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">
              已投影到 LangSmith 的远端数据不会被删除。
            </p>
            <div className="mt-6 flex justify-end gap-2">
              <button onClick={() => setPendingDelete(null)} className={actionClass}>取消</button>
              <button
                onClick={() => remove(pendingDelete)}
                className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-red-600 px-3 text-xs font-medium text-white transition hover:bg-red-700"
              >
                <Trash2 className="h-3.5 w-3.5" />确认删除
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
