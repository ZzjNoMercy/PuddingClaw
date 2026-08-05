"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertCircle,
  ArrowRight,
  BookOpenCheck,
  CheckCircle2,
  Clock3,
  Database,
  Files,
  FileUp,
  Link2,
  ListChecks,
  Loader2,
  RefreshCw,
  Search,
  Settings,
  Sparkles,
  X,
} from "lucide-react";

import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import {
  createKnowledgeImportJob,
  createLlmWikiIngestJob,
  getLlmWikiWorkspaceStatus,
  listReadLaterItems,
  saveReadLaterUrl,
  type KnowledgeImportJob,
  type LlmWikiWorkspaceStatus,
  type ReadLaterItem,
} from "@/lib/api";

type Props = {
  documentCount: number;
  fileCount: number;
  jobs: KnowledgeImportJob[];
  loading: boolean;
  onRefresh: () => void;
};

function isActive(job: KnowledgeImportJob) {
  return job.status === "queued" || job.status === "running";
}

export default function KnowledgeOverview({ documentCount, fileCount, jobs, loading, onRefresh }: Props) {
  const router = useRouter();
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [readLater, setReadLater] = useState<ReadLaterItem[]>([]);
  const [dialog, setDialog] = useState<"read-later" | "compile" | null>(null);
  const [url, setUrl] = useState("");
  const [workspace, setWorkspace] = useState<LlmWikiWorkspaceStatus | null>(null);
  const [selectedRaw, setSelectedRaw] = useState<Set<string>>(new Set());
  const [importGbrain, setImportGbrain] = useState(false);
  const [busy, setBusy] = useState<"upload" | "read-later" | "compile" | null>(null);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    void listReadLaterItems().then(setReadLater).catch(() => setReadLater([]));
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchInputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  function submitSearch() {
    const query = searchQuery.trim();
    if (query) router.push(`/knowledge/search?q=${encodeURIComponent(query)}`);
  }

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 3000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!dialog) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) setDialog(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, dialog]);

  const unreadCount = readLater.filter((item) => item.reading_status === "unread").length;
  const readyCount = readLater.filter((item) => item.parse_status === "ready").length;
  const activeJobs = jobs.filter(isActive);
  const latestJobs = useMemo(() => jobs.slice(0, 3), [jobs]);
  const pendingRaw = useMemo(() => workspace?.raw.filter((item) => !item.compiled) ?? [], [workspace]);

  async function uploadFile(file: File | undefined) {
    if (!file) return;
    setBusy("upload");
    setActionError("");
    try {
      await createKnowledgeImportJob(file, undefined, ["local_markdown"]);
      setNotice(`已加入导入队列：${file.name}`);
      onRefresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "上传失败");
    } finally {
      setBusy(null);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  }

  function openReadLater() {
    setActionError("");
    setUrl("");
    setDialog("read-later");
  }

  async function saveUrl() {
    if (!url.trim()) return;
    setBusy("read-later");
    setActionError("");
    try {
      const result = await saveReadLaterUrl({ url: url.trim() });
      setReadLater((current) => {
        const rest = current.filter((item) => item.id !== result.item.id);
        return [result.item, ...rest];
      });
      setDialog(null);
      setNotice(result.deduplicated ? "这个链接已经在稍后读中" : "已收藏，后台正在整理正文");
      onRefresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "收藏失败");
    } finally {
      setBusy(null);
    }
  }

  async function openCompile() {
    setDialog("compile");
    setWorkspace(null);
    setSelectedRaw(new Set());
    setImportGbrain(false);
    setActionError("");
    try {
      const result = await getLlmWikiWorkspaceStatus();
      const selectable = result.raw.filter((item) => !item.compiled).map((item) => item.snapshot_path);
      setWorkspace(result);
      setSelectedRaw(new Set(selectable));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "读取 Raw 失败");
    }
  }

  async function compileWiki() {
    if (!selectedRaw.size) return;
    setBusy("compile");
    setActionError("");
    try {
      const job = await createLlmWikiIngestJob(Array.from(selectedRaw), importGbrain);
      setDialog(null);
      router.push(`/knowledge/imports/${job.id}`);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "提交编译失败");
    } finally {
      setBusy(null);
    }
  }

  const workspaces = [
    {
      href: "/knowledge/library",
      title: "资料库",
      description: "上传、预览和管理原始资料；需要时复制 Markdown 到 Wiki Raw。",
      meta: `${fileCount} 个文件 · ${documentCount} 个已登记文档`,
      icon: Files,
      tone: "bg-[#002fa7]/[0.08] text-[#002fa7]",
    },
    {
      href: "/knowledge/read-later",
      title: "稍后读",
      description: "先收藏链接并自动整理正文，阅读后再决定是否进入 Wiki。",
      meta: `${unreadCount} 篇未读 · ${readyCount} 篇正文就绪`,
      icon: BookOpenCheck,
      tone: "bg-cyan-50 text-cyan-700",
    },
    {
      href: "/knowledge/schema",
      title: "LLM Wiki Studio",
      description: "维护 Schema，将 Raw 编译成互联 Wiki，并按需导入 GBrain。",
      meta: "Raw → Wiki → 检查 → GBrain",
      icon: Sparkles,
      tone: "bg-violet-50 text-violet-700",
    },
    {
      href: "/knowledge/imports",
      title: "任务中心",
      description: "统一查看文件解析、稍后读抓取、Wiki 编译和数据库导入。",
      meta: `${activeJobs.length} 个处理中 · ${jobs.length} 条记录`,
      icon: ListChecks,
      tone: "bg-amber-50 text-amber-700",
    },
  ];

  return (
    <>
      <KnowledgeWorkspaceHeader
        section="overview"
        actions={
          <>
          <Link
            href="/settings?category=knowledge"
            className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/[0.07] bg-white px-3.5 text-xs font-semibold text-gray-600 shadow-sm hover:text-[#002fa7]"
          >
            <Settings className="h-3.5 w-3.5" />设置
          </Link>
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/[0.07] bg-white px-3.5 text-xs font-semibold text-gray-600 shadow-sm hover:text-[#002fa7] disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />刷新
          </button>
          </>
        }
      />

      <KnowledgeWorkspaceNav />

      <section className="rounded-[28px] border border-[#002fa7]/10 bg-gradient-to-br from-[#002fa7]/[0.07] via-white to-cyan-50/60 p-5 shadow-sm sm:p-7">
        <div className="mx-auto max-w-4xl">
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-[#002fa7]/70">LOCAL KNOWLEDGE SEARCH</p>
          <h2 className="mt-2 text-xl font-semibold tracking-tight text-gray-950 sm:text-2xl">从你的知识库找到答案所需的原文</h2>
          <form
            className="mt-5 flex items-center gap-2 rounded-2xl border border-black/[0.09] bg-white p-1.5 shadow-lg shadow-[#002fa7]/[0.06] focus-within:border-[#002fa7]/35 focus-within:ring-4 focus-within:ring-[#002fa7]/[0.08]"
            onSubmit={(event) => { event.preventDefault(); submitSearch(); }}
          >
            <Search className="ml-3 h-5 w-5 shrink-0 text-[#002fa7]" />
            <input
              ref={searchInputRef}
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="搜索文章、Wiki、文件和图片……"
              className="h-12 min-w-0 flex-1 bg-transparent px-2 text-sm text-gray-900 outline-none placeholder:text-gray-400"
              aria-label="搜索知识库"
            />
            <button type="submit" disabled={!searchQuery.trim()} className="h-11 rounded-xl bg-[#002fa7] px-5 text-xs font-semibold text-white transition hover:bg-[#00227d] disabled:cursor-not-allowed disabled:opacity-40">搜索</button>
          </form>
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-500">
            <span>Wiki · 文章 · 图片 · 文件</span>
            <Link href="/knowledge/settings/search" className="font-semibold text-[#002fa7] hover:underline">配置搜索范围</Link>
          </div>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {workspaces.map((item) => {
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className="group flex min-h-[210px] flex-col rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm transition hover:-translate-y-0.5 hover:border-[#002fa7]/15 hover:shadow-lg hover:shadow-[#002fa7]/[0.06]"
            >
              <div className="flex items-start justify-between gap-3">
                <span className={`flex h-11 w-11 items-center justify-center rounded-2xl ${item.tone}`}>
                  <Icon className="h-5 w-5" />
                </span>
                <ArrowRight className="h-4 w-4 text-gray-300 transition group-hover:translate-x-0.5 group-hover:text-[#002fa7]" />
              </div>
              <h2 className="mt-5 text-base font-semibold text-gray-950">{item.title}</h2>
              <p className="mt-2 flex-1 text-xs leading-5 text-gray-500">{item.description}</p>
              <p className="mt-4 border-t border-black/[0.05] pt-3 text-[11px] font-medium text-gray-400">{item.meta}</p>
            </Link>
          );
        })}
      </section>

      <section className="grid items-start gap-4 xl:grid-cols-2">
        <div className="rounded-2xl border border-black/[0.06] bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-gray-950">快速开始</h2>
              <p className="mt-1 text-xs text-gray-400">从当前意图直接进入下一步。</p>
            </div>
          </div>
          <div className="mt-3 grid gap-2.5 sm:grid-cols-3">
            <input
              ref={uploadInputRef}
              type="file"
              accept="application/pdf,.pdf,text/markdown,.md,.markdown,.xlsx,.xls,.csv,.tsv,.txt,.docx"
              className="hidden"
              onChange={(event) => void uploadFile(event.target.files?.[0])}
            />
            <button
              type="button"
              onClick={() => uploadInputRef.current?.click()}
              disabled={Boolean(busy)}
              className="group flex min-h-[68px] items-center gap-2.5 rounded-xl border border-[#002fa7]/15 bg-[#002fa7]/[0.035] px-3 py-2.5 text-left transition hover:border-[#002fa7]/30 hover:bg-[#002fa7]/[0.07] disabled:cursor-wait disabled:opacity-60"
            >
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-blue-50 text-[#002fa7]">
                {busy === "upload" ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}
              </span>
              <span className="min-w-0 flex-1"><strong className="block text-sm text-gray-900">上传资料</strong><span className="mt-0.5 block truncate text-[10px] text-gray-400">PDF / Markdown / 表格</span></span>
            </button>
            <button type="button" onClick={openReadLater} disabled={Boolean(busy)} className="group flex min-h-[68px] items-center gap-2.5 rounded-xl border border-black/[0.06] bg-black/[0.018] px-3 py-2.5 text-left transition hover:border-cyan-500/20 hover:bg-cyan-50/60 disabled:opacity-60">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-cyan-50 text-cyan-700"><BookOpenCheck className="h-4 w-4" /></span>
              <span className="min-w-0 flex-1"><strong className="block text-sm text-gray-900">收藏链接</strong><span className="mt-0.5 block truncate text-[10px] text-gray-400">自动抓取并整理正文</span></span>
            </button>
            <button type="button" onClick={() => void openCompile()} disabled={Boolean(busy)} className="group flex min-h-[68px] items-center gap-2.5 rounded-xl border border-black/[0.06] bg-black/[0.018] px-3 py-2.5 text-left transition hover:border-violet-500/20 hover:bg-violet-50/60 disabled:opacity-60">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-violet-50 text-violet-700"><Sparkles className="h-4 w-4" /></span>
              <span className="min-w-0 flex-1"><strong className="block text-sm text-gray-900">编译 Wiki</strong><span className="mt-0.5 block truncate text-[10px] text-gray-400">选择 Raw 并提交后台</span></span>
            </button>
          </div>
          {notice ? <p className="mt-3 text-xs font-medium text-emerald-700">{notice}</p> : null}
          {actionError && !dialog ? <p className="mt-3 text-xs font-medium text-red-600">{actionError}</p> : null}
        </div>

        <div className="rounded-2xl border border-black/[0.06] bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-gray-950">后台动态</h2>
              <p className="mt-1 text-xs text-gray-400">最近任务与处理状态。</p>
            </div>
            <Link href="/knowledge/imports" className="text-xs font-semibold text-[#002fa7]">查看全部</Link>
          </div>
          <div className="mt-4 space-y-2">
            {latestJobs.length ? latestJobs.map((job) => (
              <Link key={job.id} href={`/knowledge/imports/${job.id}`} className="flex items-center gap-3 rounded-2xl bg-black/[0.022] px-3 py-3 transition hover:bg-black/[0.04]">
                <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl ${isActive(job) ? "bg-[#002fa7]/10 text-[#002fa7]" : "bg-emerald-50 text-emerald-700"}`}>
                  {isActive(job) ? <Clock3 className="h-4 w-4" /> : <CheckCircle2 className="h-4 w-4" />}
                </span>
                <span className="min-w-0 flex-1"><strong className="block truncate text-xs text-gray-800">{job.title || job.file_name}</strong><span className="mt-0.5 block text-[10px] text-gray-400">{job.current_step || job.status}</span></span>
              </Link>
            )) : (
              <div className="rounded-2xl border border-dashed border-black/[0.08] px-4 py-7 text-center text-xs text-gray-400">还没有后台任务</div>
            )}
          </div>
        </div>
      </section>

      {dialog ? (
        <div
          className="fixed inset-0 z-[120] flex items-center justify-center bg-slate-950/25 p-4 backdrop-blur-[2px]"
          role="presentation"
          onMouseDown={(event) => {
            if (event.currentTarget === event.target && !busy) setDialog(null);
          }}
        >
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="quick-action-title"
            className="w-full max-w-lg overflow-hidden rounded-[28px] border border-white/70 bg-white shadow-2xl shadow-slate-900/20"
          >
            <div className="flex items-start gap-3 border-b border-black/[0.06] px-6 py-5">
              <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/[0.08] text-[#002fa7]">
                {dialog === "read-later" ? <Link2 className="h-5 w-5" /> : <Sparkles className="h-5 w-5" />}
              </span>
              <div className="min-w-0 flex-1">
                <h2 id="quick-action-title" className="text-base font-semibold text-gray-950">
                  {dialog === "read-later" ? "收藏链接" : "编译 Wiki"}
                </h2>
                <p className="mt-1 text-xs leading-5 text-gray-500">
                  {dialog === "read-later" ? "保存后将在后台抓取并整理正文。" : "选择本次要交给 Wiki Compiler Agent 的 Raw。"}
                </p>
              </div>
              <button type="button" onClick={() => setDialog(null)} disabled={Boolean(busy)} aria-label="关闭" className="flex h-8 w-8 items-center justify-center rounded-xl text-gray-400 hover:bg-black/[0.04] hover:text-gray-700 disabled:opacity-40">
                <X className="h-4 w-4" />
              </button>
            </div>

            {dialog === "read-later" ? (
              <div className="px-6 py-5">
                <label className="block text-xs font-semibold text-gray-600" htmlFor="quick-read-later-url">文章链接</label>
                <div className="mt-2 flex items-center gap-2 rounded-2xl border border-black/[0.08] bg-black/[0.015] px-3 focus-within:border-[#002fa7]/30 focus-within:ring-4 focus-within:ring-[#002fa7]/[0.06]">
                  <Link2 className="h-4 w-4 shrink-0 text-[#002fa7]" />
                  <input
                    id="quick-read-later-url"
                    autoFocus
                    value={url}
                    onChange={(event) => setUrl(event.target.value)}
                    onKeyDown={(event) => { if (event.key === "Enter") void saveUrl(); }}
                    placeholder="https://..."
                    className="h-12 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-gray-400"
                  />
                </div>
              </div>
            ) : (
              <div className="px-6 py-5">
                {!workspace && !actionError ? (
                  <div className="flex min-h-36 items-center justify-center text-sm text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在读取 Raw…</div>
                ) : pendingRaw.length ? (
                  <>
                    <div className="max-h-64 space-y-2 overflow-y-auto pr-1">
                      {pendingRaw.map((item) => {
                        const checked = selectedRaw.has(item.snapshot_path);
                        return (
                          <label key={item.snapshot_path} className={`flex cursor-pointer items-center gap-3 rounded-2xl px-3 py-3 transition ${checked ? "bg-[#002fa7]/[0.07]" : "bg-black/[0.02] hover:bg-black/[0.035]"}`}>
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={(event) => setSelectedRaw((current) => {
                                const next = new Set(current);
                                if (event.target.checked) next.add(item.snapshot_path); else next.delete(item.snapshot_path);
                                return next;
                              })}
                              className="h-4 w-4 accent-[#002fa7]"
                            />
                            <span className="min-w-0 flex-1"><strong className="block truncate text-xs text-gray-800">{item.title || item.snapshot_path.split("/").pop()}</strong><span className="mt-1 block truncate font-mono text-[10px] text-gray-400">{item.snapshot_path}</span></span>
                          </label>
                        );
                      })}
                    </div>
                    <label className="mt-4 flex cursor-pointer items-center gap-3 rounded-2xl border border-black/[0.06] px-3 py-3">
                      <input type="checkbox" checked={importGbrain} onChange={(event) => setImportGbrain(event.target.checked)} className="h-4 w-4 accent-[#002fa7]" />
                      <Database className="h-4 w-4 text-violet-600" />
                      <span><strong className="block text-xs text-gray-800">编译完成后导入 GBrain</strong><span className="mt-0.5 block text-[10px] text-gray-400">不勾选则只发布 Markdown Wiki</span></span>
                    </label>
                  </>
                ) : workspace ? (
                  <div className="rounded-2xl border border-dashed border-black/[0.08] px-5 py-9 text-center"><CheckCircle2 className="mx-auto h-6 w-6 text-emerald-600" /><p className="mt-3 text-sm font-medium text-gray-700">当前没有待编译的 Raw</p><Link href="/knowledge/schema" className="mt-2 inline-block text-xs font-semibold text-[#002fa7]">前往 LLM Wiki Studio</Link></div>
                ) : null}
              </div>
            )}

            {actionError ? <div className="mx-6 mb-4 flex gap-2 rounded-2xl bg-red-50 px-3 py-2.5 text-xs text-red-600"><AlertCircle className="h-4 w-4 shrink-0" />{actionError}</div> : null}
            <div className="flex justify-end gap-2 border-t border-black/[0.06] bg-black/[0.012] px-6 py-4">
              <button type="button" onClick={() => setDialog(null)} disabled={Boolean(busy)} className="h-10 rounded-xl px-4 text-sm font-semibold text-gray-500 hover:bg-black/[0.04] disabled:opacity-40">取消</button>
              <button
                type="button"
                onClick={() => void (dialog === "read-later" ? saveUrl() : compileWiki())}
                disabled={Boolean(busy) || (dialog === "read-later" ? !url.trim() : !selectedRaw.size)}
                className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-semibold text-white shadow-sm hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                {dialog === "read-later" ? "收藏" : importGbrain ? "编译并导入 GBrain" : "提交编译"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </>
  );
}
