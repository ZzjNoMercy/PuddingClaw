"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { CheckCircle2, ChevronDown, ChevronRight, Download, ExternalLink, Loader2, Plus, RefreshCw, Trash2, XCircle } from "lucide-react";
import {
  cancelEvaluationExperiment,
  deleteEvaluationExperiment,
  getEvaluationExperimentResults,
  listEvaluationExperiments,
  rerunSWEbenchVerifier,
  resumeMissingSWEbenchCases,
  retryEvaluationExperiment,
  swebenchPredictionExportUrl,
  syncEvaluationExperiment,
  type EvalExperiment,
  type EvalExperimentResultRow,
} from "@/lib/evaluationApi";
import { experimentIsTerminal, safeRemoteUrl } from "@/lib/evaluationState";

const actionClass =
  "inline-flex h-8 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-gray-200 bg-white px-3 text-xs font-medium text-gray-700 transition hover:border-gray-300 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40";

const progressStageLabels: Record<string, string> = {
  queued: "等待执行进程",
  preparing: "准备隔离环境",
  candidate_environment: "准备智能体依赖环境",
  agent_running: "智能体执行中",
  case_completed: "用例已完成",
  official_verifier: "Docker 判卷中",
  scoring: "汇总评分",
  langsmith_projection: "投影 LangSmith",
  cancel_requested: "正在取消",
  completed: "评测完成",
  failed: "执行失败",
};

const experimentStatusLabels: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancel_requested: "正在取消",
  cancelled: "已取消",
};

const integrationStatusLabels: Record<string, string> = {
  disabled: "已禁用",
  pending: "待投影",
  synced: "已投影",
  completed: "已完成",
  failed: "失败",
  error: "异常",
  not_started: "未开始",
  not_configured: "未配置",
};

const outcomeLabels: Record<string, string> = {
  pass: "通过",
  fail: "未通过",
  error: "异常",
  not_evaluated: "未评估",
  not_applicable: "不适用",
};

function statusLabel(value: unknown, fallback = "未配置"): string {
  const status = String(value || "");
  return experimentStatusLabels[status] || integrationStatusLabels[status] || status || fallback;
}

function harnessStatusLabel(value: unknown): string {
  const status = String(value || "");
  return ({
    pending: "等待中",
    not_started: "未开始",
    running: "判卷中",
    completed: "判卷完成",
    failed: "判卷失败",
    error: "判卷异常",
    cancelled: "已取消",
  } as Record<string, string>)[status] || status || "等待中";
}

function outcomeLabel(value: string): string {
  return outcomeLabels[value] || value;
}

function candidateLabel(value: string): string {
  if (value === "当前 Agent") return "当前智能体";
  return value.replace(/Agent/g, "智能体");
}

function profileLabel(value: string): string {
  if (value === "coding_agent@1") return "代码智能体";
  if (value === "general_agent@1") return "通用智能体";
  return value;
}

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
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function LiveAttemptResults({
  rows,
  effectiveAttemptIds,
}: {
  rows: EvalExperimentResultRow[];
  effectiveAttemptIds?: string[];
}) {
  const effectiveSet = new Set(effectiveAttemptIds || []);
  const visibleRows = effectiveSet.size > 0
    ? rows.filter((row) => effectiveSet.has(row.attempt_id))
    : rows;
  const attempts = attemptViews(visibleRows);
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
          <span className="whitespace-nowrap font-normal tabular-nums text-slate-400">{attempts.length} 个用例</span>
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
                  <div className="flex flex-nowrap items-center gap-x-2 overflow-hidden">
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
                    <p className="mt-1 whitespace-nowrap text-slate-500">判定：{attempt.outcomes.map(outcomeLabel).join(" / ")}</p>
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
  const message = String(progress.message || "执行进程已启动，等待首个进度事件");
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
          {currentCase ? `当前：${currentCase}` : "正在建立独立会话、工作区与记忆空间"}
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
          <h1 className="text-xl font-semibold">评测实验</h1>
          <p className="mt-1 text-sm text-gray-500">评分结果先保存到本地，再按需投影到 LangSmith 对比实验。</p>
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
            const verifierReplay = item.summary.execution_mode === "official_verifier_replay";
            const caseResume = item.summary.execution_mode === "swebench_missing_case_resume";
            const visibleAttempts = experimentIsTerminal(item.status) || verifierReplay || caseResume
              ? numberValue(item.summary.case_attempts)
              : numberValue(liveProgress.completed);
            const visibleFailures = experimentIsTerminal(item.status) || verifierReplay || caseResume
              ? numberValue(item.summary.failed_attempts)
              : numberValue(liveProgress.failed);
            const terminal = experimentIsTerminal(item.status);
            const officialHarness = item.summary.swebench_official_harness as Record<string, unknown> | undefined;
            const dockerArchitecture = String(officialHarness?.docker_architecture || "");
            const missingPredictions = numberValue(item.summary.swebench_missing_predictions);
            const persistedPredictions = item.summary.swebench_predictions_available === true
              ? numberValue(item.summary.case_attempts)
              : Math.max(0, numberValue(item.summary.case_attempts) - missingPredictions);
            const canRerunVerifier = item.summary.swebench_predictions_available === true
              || (missingPredictions > 0 && persistedPredictions > 0);
            const effectiveAttemptIds = Array.isArray(item.summary.effective_attempt_ids)
              ? item.summary.effective_attempt_ids.map(String)
              : undefined;
            const showSecondaryActions = Boolean(
              url
              || (item.status === "completed" && item.summary.swebench_predictions_available === true)
              || (item.status === "completed" && projection === "pending")
              || item.error,
            );
            return (
              <div key={item.experiment_id} className="overflow-hidden rounded-xl border bg-white p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="truncate whitespace-nowrap font-medium" title={displayExperimentName(item.name)}>{displayExperimentName(item.name)}</div>
                  </div>
                  <div className="flex shrink-0 flex-nowrap items-center justify-end gap-2 whitespace-nowrap">
                    <span className="rounded-full bg-gray-100 px-2.5 py-1 text-xs">{statusLabel(item.status)}</span>
                    {terminal ? (
                      <>
                        {missingPredictions > 0 && (
                          <button
                            disabled={busy}
                            onClick={() => act(item.experiment_id, () => resumeMissingSWEbenchCases(item.experiment_id))}
                            title={`只让智能体补跑缺少补丁的 ${missingPredictions} 个用例；已有 ${persistedPredictions} 个补丁保留，随后自动统一判卷`}
                            className={`${actionClass} !border-[#002fa7] !bg-[#002fa7] !text-white hover:!border-[#00247f] hover:!bg-[#00247f]`}
                          >
                            <RefreshCw className="h-3.5 w-3.5" />
                            补跑失败用例 ({missingPredictions})
                          </button>
                        )}
                        {canRerunVerifier && (
                          <button
                            disabled={busy}
                            onClick={() => act(item.experiment_id, () => rerunSWEbenchVerifier(item.experiment_id))}
                            title={missingPredictions > 0
                              ? `复用已有 ${persistedPredictions} 份补丁判卷；缺失 ${missingPredictions} 份保持未评估`
                              : "复用已有智能体补丁，只重新执行官方 Docker 判卷"}
                            className={`${actionClass} border-slate-200 text-slate-600 hover:border-slate-300 hover:bg-slate-50`}
                          >
                            <RefreshCw className="h-3.5 w-3.5" />
                            {missingPredictions > 0
                              ? `判卷已有补丁 (${persistedPredictions}/${persistedPredictions + missingPredictions})`
                              : "重新判卷"}
                          </button>
                        )}
                        <button
                          disabled={busy}
                          onClick={() => act(item.experiment_id, () => retryEvaluationExperiment(item.experiment_id))}
                          title="重新运行智能体并生成新的补丁"
                          className={`${actionClass} border-blue-200 text-[#002fa7] hover:border-blue-300 hover:bg-blue-50`}
                        >
                          <RefreshCw className="h-3.5 w-3.5" />重新评测
                        </button>
                      </>
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

                <div className="mt-2 overflow-x-auto pb-0.5 text-xs text-gray-500">
                  <div className="whitespace-nowrap">
                    评测集 {item.dataset_id} · v{item.dataset_version} · {candidateLabel(item.candidate.name)} · {profileLabel(item.profile_id)}
                  </div>
                  <div className="mt-1 whitespace-nowrap">
                    指纹 {item.candidate.fingerprint || "待生成"} · {visibleAttempts} 次执行 · {visibleFailures} 个执行失败 · {String(item.summary.critical_failures || 0)} 个严重失败 · 对比投影 {statusLabel(projection)} · 智能体追踪 {statusLabel(item.summary.agent_trace_export)}
                  </div>
                  {officialHarness && (
                    <div className="mt-1 whitespace-nowrap text-emerald-700">
                      SWE-bench Docker{dockerArchitecture ? `（${dockerArchitecture.toUpperCase()}）` : ""}：{harnessStatusLabel(officialHarness.status)} · 通过 {String(officialHarness.resolved || 0)}/{String(officialHarness.total || 0)}
                    </div>
                  )}
                </div>

                <ExperimentProgress item={item} />
                <LiveAttemptResults
                  rows={resultsByExperiment[item.experiment_id] || []}
                  effectiveAttemptIds={effectiveAttemptIds}
                />

                {showSecondaryActions && <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-gray-100 pt-3">
                  {url && (
                    <a href={url} target="_blank" rel="noopener noreferrer" className={actionClass}>
                      LangSmith<ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  )}
                  {item.status === "completed" && item.summary.swebench_predictions_available === true && (
                    <a href={swebenchPredictionExportUrl(item.experiment_id)} className={actionClass}>
                      下载 SWE 预测结果<Download className="h-3.5 w-3.5" />
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
          {!items.length && <div className="p-12 text-center text-sm text-gray-400">还没有评测实验</div>}
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
              “{displayExperimentName(pendingDelete.name)}”的本地执行记录、评分结果和运行文件会被永久删除，无法恢复。
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
