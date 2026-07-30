"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  Database,
  FileText,
  BookOpenCheck,
  Loader2,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import { useApp } from "@/lib/store";
import {
  clearKnowledgeImportJobs,
  deleteKnowledgeImportJob,
  listKnowledgeImportJobs,
  retryKnowledgeImportJob,
  type KnowledgeImportJob,
} from "@/lib/api";

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || "未知错误");
}

function jobStatusLabel(status: string): string {
  if (status === "queued") return "排队中";
  if (status === "running") return "处理中";
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "失败";
  if (status === "cancelled") return "已取消";
  return status;
}

function jobStatusClass(status: string): string {
  if (status === "succeeded") return "bg-emerald-50 text-emerald-700 ring-emerald-500/10";
  if (status === "failed") return "bg-red-50 text-red-600 ring-red-500/10";
  if (status === "running") return "bg-[#002fa7]/10 text-[#002fa7] ring-[#002fa7]/10";
  return "bg-gray-100 text-gray-600 ring-black/[0.04]";
}

function JobIcon({ status }: { status: string }) {
  if (status === "succeeded") return <CheckCircle2 className="h-5 w-5 text-emerald-600" />;
  if (status === "failed") return <AlertCircle className="h-5 w-5 text-red-500" />;
  if (status === "running") return <Loader2 className="h-5 w-5 animate-spin text-[#002fa7]" />;
  return <Clock3 className="h-5 w-5 text-gray-400" />;
}

type JobFilter = "all" | "wiki" | "file" | "vector" | "entity";
const JOB_PAGE_SIZE = 10;

function isVectorPublishJob(job: KnowledgeImportJob): boolean {
  return job.metadata?.kind === "vector_publish" || job.file_type === "vector";
}

function isVannaEntityJob(job: KnowledgeImportJob): boolean {
  return job.metadata?.kind === "vanna_entity_import" || job.file_type === "vanna_entity";
}

function isLlmWikiJob(job: KnowledgeImportJob): boolean {
  return job.metadata?.kind === "llm_wiki_ingest" || job.file_type === "llm_wiki";
}

function jobKindLabel(job: KnowledgeImportJob): string {
  if (isLlmWikiJob(job)) return "Wiki 编译";
  if (isVannaEntityJob(job)) return "实体导入";
  return isVectorPublishJob(job) ? "向量导入" : "文件导入";
}

function jobKindClass(job: KnowledgeImportJob): string {
  if (isLlmWikiJob(job)) return "bg-violet-50 text-violet-700";
  if (isVannaEntityJob(job)) return "bg-amber-50 text-amber-700";
  return isVectorPublishJob(job) ? "bg-[#002fa7]/10 text-[#002fa7]" : "bg-emerald-50 text-emerald-700";
}

function filterLabel(filter: JobFilter): string {
  if (filter === "wiki") return "Wiki 编译";
  if (filter === "file") return "文件导入";
  if (filter === "vector") return "向量导入";
  if (filter === "entity") return "实体导入";
  return "全部";
}

export default function KnowledgeImportJobsPage() {
  const router = useRouter();
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [jobs, setJobs] = useState<KnowledgeImportJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [retryingJobId, setRetryingJobId] = useState<string | null>(null);
  const [deletingJobId, setDeletingJobId] = useState<string | null>(null);
  const [clearingJobs, setClearingJobs] = useState(false);
  const [jobFilter, setJobFilter] = useState<JobFilter>("all");
  const [jobPage, setJobPage] = useState(1);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);

  useEffect(() => {
    setMounted(true);
    if (new URLSearchParams(window.location.search).get("filter") === "wiki") setJobFilter("wiki");
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const handleSidebarResize = useCallback(
    (delta: number) => {
      setSidebarWidth((prev: number) => Math.max(200, prev + delta));
    },
    [setSidebarWidth]
  );

  const refresh = useCallback(async (options?: { silent?: boolean }) => {
    if (!options?.silent) setLoading(true);
    try {
      const nextJobs = await listKnowledgeImportJobs(100);
      setJobs(nextJobs);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      if (!options?.silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!jobs.some((job) => job.status === "queued" || job.status === "running")) return;
    const timer = window.setInterval(() => {
      if (globalThis.document?.visibilityState !== "visible") return;
      refresh({ silent: true });
    }, 10000);
    return () => window.clearInterval(timer);
  }, [jobs, refresh]);

  const retryJob = useCallback(
    async (jobId: string) => {
      setRetryingJobId(jobId);
      setToast(null);
      try {
        await retryKnowledgeImportJob(jobId);
        setToast({ type: "success", message: "任务已重新排队" });
        await refresh();
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setRetryingJobId(null);
      }
    },
    [refresh]
  );

  const deleteJob = useCallback(
    async (jobId: string) => {
      setDeletingJobId(jobId);
      setToast(null);
      try {
        await deleteKnowledgeImportJob(jobId);
        setToast({ type: "success", message: "任务已删除" });
        await refresh();
      } catch (error) {
        setToast({ type: "error", message: errorMessage(error) });
      } finally {
        setDeletingJobId(null);
      }
    },
    [refresh]
  );

  const clearJobs = useCallback(async () => {
    if (jobs.length === 0) return;
    if (!window.confirm("只清空任务记录，不会删除知识库文件。确认清空吗？")) return;
    setClearingJobs(true);
    setToast(null);
    try {
      const count = await clearKnowledgeImportJobs();
      setToast({ type: "success", message: count > 0 ? `已清空 ${count} 条任务` : "没有可清空的任务" });
      await refresh();
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setClearingJobs(false);
    }
  }, [jobs.length, refresh]);

  const vectorJobCount = useMemo(() => jobs.filter(isVectorPublishJob).length, [jobs]);
  const entityJobCount = useMemo(() => jobs.filter(isVannaEntityJob).length, [jobs]);
  const wikiJobCount = useMemo(() => jobs.filter(isLlmWikiJob).length, [jobs]);
  const fileJobCount = Math.max(0, jobs.length - vectorJobCount - entityJobCount - wikiJobCount);
  const visibleJobs = useMemo(() => {
    if (jobFilter === "wiki") return jobs.filter(isLlmWikiJob);
    if (jobFilter === "vector") return jobs.filter(isVectorPublishJob);
    if (jobFilter === "entity") return jobs.filter(isVannaEntityJob);
    if (jobFilter === "file") return jobs.filter((job) => !isVectorPublishJob(job) && !isVannaEntityJob(job) && !isLlmWikiJob(job));
    return jobs;
  }, [jobFilter, jobs]);
  const jobPageCount = Math.max(1, Math.ceil(visibleJobs.length / JOB_PAGE_SIZE));
  const pagedJobs = useMemo(
    () => visibleJobs.slice((jobPage - 1) * JOB_PAGE_SIZE, jobPage * JOB_PAGE_SIZE),
    [jobPage, visibleJobs]
  );

  useEffect(() => {
    setJobPage(1);
  }, [jobFilter]);

  useEffect(() => {
    setJobPage((page) => Math.min(page, jobPageCount));
  }, [jobPageCount]);

  return (
    <div className="h-screen app-bg text-gray-900">
      <div className="fixed left-3 top-3 z-[80]">
        <Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact />
      </div>

      <div className="flex h-full overflow-hidden">
        <div
          className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden"
          style={{ width: sidebarOpen ? sidebarWidth : 0 }}
        >
          <div style={{ width: sidebarWidth, minWidth: 200 }} className="h-full flex flex-col">
            <div className="h-11 shrink-0" />
            <div className="flex-1 min-h-0 overflow-hidden">
              <Sidebar />
            </div>
          </div>
        </div>

        {mounted && sidebarOpen && <ResizeHandle onResize={handleSidebarResize} direction="left" />}

        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto">
            <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 px-5 py-6">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h1 className="text-2xl font-semibold tracking-tight text-gray-950">任务中心</h1>
                  <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-500">
                    统一查看 Wiki 编译、文件解析、向量与实体导入。离开页面后，后台仍会继续处理。
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    onClick={clearJobs}
                    disabled={clearingJobs || jobs.length === 0}
                    className="inline-flex h-10 items-center gap-2 rounded-full border border-red-500/15 bg-red-50 px-4 text-sm font-medium text-red-600 shadow-sm transition hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {clearingJobs ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                    清空任务
                  </button>
                  <button
                    type="button"
                    onClick={() => refresh()}
                    disabled={loading}
                    className="inline-flex h-10 items-center gap-2 rounded-full border border-black/[0.08] bg-white px-4 text-sm font-medium text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
                  >
                    <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
                    刷新
                  </button>
                </div>
              </div>

              {toast ? (
                <div
                  className={`flex items-start gap-2 rounded-2xl border px-4 py-3 text-sm ${
                    toast.type === "success"
                      ? "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                      : "border-red-500/15 bg-red-50 text-red-600"
                  }`}
                >
                  {toast.type === "success" ? (
                    <CheckCircle2 className="mt-0.5 h-4 w-4" />
                  ) : (
                    <AlertCircle className="mt-0.5 h-4 w-4" />
                  )}
                  <span className="break-all">{toast.message}</span>
                </div>
              ) : null}

              <section className="rounded-[32px] border border-black/[0.06] bg-white p-5 shadow-sm">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <h2 className="text-base font-semibold text-gray-950">任务队列</h2>
                    <p className="mt-1 text-xs text-gray-400">
                      Wiki 编译 {wikiJobCount} 条 · 文件导入 {fileJobCount} 条 · 向量导入 {vectorJobCount} 条 · 实体导入 {entityJobCount} 条
                    </p>
                  </div>
                  <div className="inline-flex rounded-2xl bg-black/[0.035] p-1">
                    {(["all", "wiki", "file", "vector", "entity"] as JobFilter[]).map((filter) => (
                      <button
                        key={filter}
                        type="button"
                        onClick={() => setJobFilter(filter)}
                        className={`h-9 rounded-xl px-3 text-xs font-semibold transition ${
                          jobFilter === filter ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500 hover:text-gray-900"
                        }`}
                      >
                        {filterLabel(filter)}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="mt-4 space-y-3">
                  {visibleJobs.length > 0 ? (
                    pagedJobs.map((job) => (
                      <div
                        key={job.id}
                        className="group rounded-[24px] border border-black/[0.06] bg-black/[0.018] p-4 transition hover:border-[#002fa7]/20 hover:bg-[#002fa7]/[0.025]"
                      >
                        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                          <Link href={`/knowledge/imports/${job.id}`} className="flex min-w-0 flex-1 items-start gap-3">
                            <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-white text-[#002fa7] shadow-sm ring-1 ring-black/[0.04]">
                              {isLlmWikiJob(job) ? <BookOpenCheck className="h-5 w-5 text-violet-600" /> : isVectorPublishJob(job) ? <Database className="h-5 w-5 text-[#002fa7]" /> : <JobIcon status={job.status} />}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="flex flex-wrap items-center gap-2">
                                <p className="truncate text-sm font-semibold text-gray-950" title={job.file_name}>
                                  {job.title || job.file_name}
                                </p>
                                <span className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${jobKindClass(job)}`}>
                                  {jobKindLabel(job)}
                                </span>
                                <span
                                  className={`rounded-full px-2.5 py-1 text-[11px] font-medium ring-1 ${jobStatusClass(job.status)}`}
                                >
                                  {jobStatusLabel(job.status)}
                                </span>
                              </div>
                              <p className="mt-1 truncate text-xs text-gray-500" title={job.file_name}>
                                {job.file_name} · {formatBytes(job.file_size)} · {formatTime(job.created_at)}
                              </p>
                              <p className="mt-1 text-xs text-gray-400">
                                当前步骤：{job.current_step || job.status}
                              </p>
                            </div>
                          </Link>

                          <div className="flex shrink-0 items-center gap-3">
                            <div className="w-40">
                              <div className="h-2 overflow-hidden rounded-full bg-gray-100">
                                <div
                                  className={`h-full rounded-full ${
                                    job.status === "failed" ? "bg-red-500" : "bg-[#002fa7]"
                                  }`}
                                  style={{ width: `${Math.max(0, Math.min(100, job.progress || 0))}%` }}
                                />
                              </div>
                              <p className="mt-1 text-right text-[11px] text-gray-400">{job.progress || 0}%</p>
                            </div>
                            {job.status === "failed" ? (
                              <button
                                type="button"
                                onClick={() => retryJob(job.id)}
                                disabled={retryingJobId === job.id}
                                className="inline-flex h-9 items-center gap-2 rounded-full bg-red-50 px-3 text-xs font-semibold text-red-600 transition hover:bg-red-100 disabled:cursor-wait disabled:opacity-60"
                              >
                                {retryingJobId === job.id ? (
                                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                ) : (
                                  <RotateCcw className="h-3.5 w-3.5" />
                                )}
                                重试
                              </button>
                            ) : null}
                            <button
                              type="button"
                              onClick={() => deleteJob(job.id)}
                              disabled={deletingJobId === job.id || job.status === "running"}
                              title={job.status === "running" ? "处理中任务不能删除" : "删除任务记录"}
                              className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-black/[0.06] bg-white text-gray-400 shadow-sm transition hover:border-red-500/20 hover:bg-red-50 hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-40"
                            >
                              {deletingJobId === job.id ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              ) : (
                                <Trash2 className="h-3.5 w-3.5" />
                              )}
                            </button>
                          </div>
                        </div>
                        {job.error_message ? (
                          <p className="mt-3 rounded-2xl bg-red-50 px-3 py-2 text-xs leading-5 text-red-600">
                            {job.error_message}
                          </p>
                        ) : null}
                      </div>
                    ))
                  ) : (
                    <div className="flex min-h-[220px] flex-col items-center justify-center rounded-[24px] border border-dashed border-black/[0.08] bg-black/[0.015] px-6 text-center">
                      <FileText className="h-8 w-8 text-gray-300" />
                      <p className="mt-3 text-sm font-medium text-gray-500">
                        {jobs.length === 0 ? "还没有后台任务" : `没有${filterLabel(jobFilter)}任务`}
                      </p>
                      <button
                        type="button"
                        onClick={() => router.push(jobFilter === "wiki" ? "/knowledge/schema" : "/knowledge")}
                        className="mt-4 inline-flex h-10 items-center rounded-full bg-[#002fa7] px-4 text-sm font-semibold text-white transition hover:bg-[#001f7a]"
                      >
                        {jobFilter === "wiki" ? "去 LLM Wiki Studio" : "去上传文件"}
                      </button>
                    </div>
                  )}
                </div>
                {visibleJobs.length > JOB_PAGE_SIZE ? (
                  <div className="mt-5 flex flex-col gap-3 border-t border-black/[0.05] pt-4 text-xs text-gray-400 sm:flex-row sm:items-center sm:justify-between">
                    <span>
                      第 {jobPage} / {jobPageCount} 页 · 当前筛选 {visibleJobs.length} 条
                    </span>
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => setJobPage((page) => Math.max(1, page - 1))}
                        disabled={jobPage <= 1}
                        className="h-9 rounded-full border border-black/[0.06] bg-white px-3 font-medium text-gray-600 shadow-sm transition hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        上一页
                      </button>
                      <button
                        type="button"
                        onClick={() => setJobPage((page) => Math.min(jobPageCount, page + 1))}
                        disabled={jobPage >= jobPageCount}
                        className="h-9 rounded-full border border-black/[0.06] bg-white px-3 font-medium text-gray-600 shadow-sm transition hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        下一页
                      </button>
                    </div>
                  </div>
                ) : null}
              </section>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
