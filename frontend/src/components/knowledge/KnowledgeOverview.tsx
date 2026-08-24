"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertCircle,
  BookOpenCheck,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Cloud,
  Database,
  FileUp,
  HardDrive,
  Link2,
  Loader2,
  RefreshCw,
  Search,
  Settings,
  Sparkles,
  X,
} from "lucide-react";

import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import DocumentDetailModal, {
  docType,
  formatSize,
  KindLogo,
  statusView,
  type SourceKind,
} from "@/components/knowledge/DocumentDetailModal";
import {
  commitKnowledgeImportSource,
  createLlmWikiIngestJob,
  getLlmWikiWorkspaceStatus,
  listReadLaterItems,
  saveReadLaterUrl,
  stageKnowledgeImportSource,
  type DocumentParserStatus,
  type KnowledgeDocument,
  type KnowledgeImportJob,
  type LlmWikiWorkspaceStatus,
  type ReadLaterItem,
  type StagedKnowledgeSource,
} from "@/lib/api";
import { listKnowledgeSources, type KnowledgeSource } from "@/lib/knowledgeSourcesApi";

type Props = {
  documents: KnowledgeDocument[];
  jobs: KnowledgeImportJob[];
  loading: boolean;
  onRefresh: () => void;
};

function isActive(job: KnowledgeImportJob) {
  return job.status === "queued" || job.status === "running";
}

function overviewJobStatus(job: KnowledgeImportJob): string {
  if (job.status === "staged") return "待选择解析器";
  return job.current_step || job.status;
}

function relativeTime(value: string | null | undefined): string {
  if (!value) return "—";
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "—";
  const minutes = Math.max(1, Math.floor((Date.now() - time) / 60000));
  if (minutes < 60) return `${minutes} 分钟前`;
  const date = new Date(value);
  const clock = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  if (minutes < 1440 && date.toDateString() === new Date().toDateString()) return `今天 ${clock}`;
  if (minutes < 2880) return `昨天 ${clock}`;
  return date.toLocaleDateString("zh-CN");
}

function kindOf(doc: KnowledgeDocument, sources: KnowledgeSource[]): SourceKind {
  const source = doc.source_connection_id ? sources.find((item) => item.id === doc.source_connection_id) : undefined;
  if (source?.connector_key === "feishu_wiki" || doc.source_type.startsWith("feishu")) return "feishu";
  if (source?.connector_key === "web_capture" || doc.source_type === "read_later" || doc.source_type === "web") return "web";
  return "local";
}

function readLaterStatus(item: ReadLaterItem): { label: string; className: string } {
  if (item.parse_status === "queued") return { label: "等待解析", className: "bg-amber-50 text-amber-700" };
  if (item.parse_status === "processing") return { label: "解析中", className: "bg-[#002fa7]/10 text-[#002fa7]" };
  if (item.parse_status === "failed") return { label: "解析失败", className: "bg-red-50 text-red-600" };
  if (item.parse_status === "link_only") return { label: "仅保留链接", className: "bg-amber-50 text-amber-700" };
  return { label: "正文就绪", className: "bg-emerald-50 text-emerald-700" };
}

export default function KnowledgeOverview({ documents, jobs, loading, onRefresh }: Props) {
  const router = useRouter();
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [readLater, setReadLater] = useState<ReadLaterItem[]>([]);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [dialog, setDialog] = useState<"read-later" | "compile" | "parser" | null>(null);
  const [url, setUrl] = useState("");
  const [workspace, setWorkspace] = useState<LlmWikiWorkspaceStatus | null>(null);
  const [selectedRaw, setSelectedRaw] = useState<Set<string>>(new Set());
  const [importGbrain, setImportGbrain] = useState(false);
  const [busy, setBusy] = useState<"upload" | "read-later" | "compile" | null>(null);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [detail, setDetail] = useState<{ doc: KnowledgeDocument; kind: SourceKind } | null>(null);
  const [stagedSource, setStagedSource] = useState<StagedKnowledgeSource | null>(null);
  const [parserChoices, setParserChoices] = useState<DocumentParserStatus[]>([]);
  const [selectedParserId, setSelectedParserId] = useState("");
  const [allowCloudParser, setAllowCloudParser] = useState(false);
  const orderedParserChoices = useMemo(() => [...parserChoices].sort((left, right) => (
    Number(right.selectable) - Number(left.selectable)
    || Number(right.recommended) - Number(left.recommended)
    || left.priority - right.priority
  )), [parserChoices]);

  useEffect(() => {
    void listReadLaterItems().then(setReadLater).catch(() => setReadLater([]));
    void listKnowledgeSources().then(setSources).catch(() => setSources([]));
  }, []);

  useEffect(() => {
    if (!jobs.some((job) => job.metadata?.kind === "read_later_capture" && isActive(job))) return;
    void listReadLaterItems().then(setReadLater).catch(() => undefined);
  }, [jobs]);

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
  const activeJobs = jobs.filter(isActive);
  const latestJobs = useMemo(() => jobs.slice(0, 3), [jobs]);
  const pendingRaw = useMemo(() => workspace?.raw.filter((item) => !item.compiled) ?? [], [workspace]);

  const feishuSourceName = sources.find((item) => item.connector_key === "feishu_wiki")?.name || "飞书";
  const sourceColumns = useMemo(() => ([
    { kind: "local" as SourceKind, title: "本地上传", hint: "PDF、Markdown 与 Office 文件" },
    { kind: "web" as SourceKind, title: "稍后读", hint: `${unreadCount} 篇未读` },
    { kind: "feishu" as SourceKind, title: feishuSourceName, hint: "飞书 Wiki 同步" },
  ]), [feishuSourceName, unreadCount]);

  const sourceGroups = useMemo(() => {
    const groups: Record<SourceKind, KnowledgeDocument[]> = { local: [], web: [], feishu: [] };
    for (const doc of documents) {
      if (doc.status === "deleted") continue;
      groups[kindOf(doc, sources)].push(doc);
    }
    for (const kind of Object.keys(groups) as SourceKind[]) {
      groups[kind].sort((a, b) => Date.parse(b.updated_at || "") - Date.parse(a.updated_at || ""));
    }
    return groups;
  }, [documents, sources]);

  const projectedReadLater = useMemo(() => {
    const documentIds = new Set(documents.map((document) => document.id));
    return readLater.filter((item) => !item.document_id || !documentIds.has(item.document_id));
  }, [documents, readLater]);

  function sourceNameOf(kind: SourceKind): string {
    if (kind === "feishu") return feishuSourceName;
    if (kind === "web") return "稍后读";
    return "本地上传";
  }

  async function uploadFile(file: File | undefined) {
    if (!file) return;
    setBusy("upload");
    setActionError("");
    try {
      const staged = await stageKnowledgeImportSource(file);
      setStagedSource(staged.source);
      setParserChoices(staged.parsers);
      setSelectedParserId(staged.parsers.find((item) => item.recommended && item.selectable)?.id || "");
      setAllowCloudParser(false);
      setDialog("parser");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "上传失败");
    } finally {
      setBusy(null);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  }

  async function commitUpload() {
    if (!stagedSource || !selectedParserId) return;
    const parser = parserChoices.find((item) => item.id === selectedParserId);
    if (!parser?.selectable) return;
    setBusy("upload");
    setActionError("");
    try {
      await commitKnowledgeImportSource(stagedSource.id, {
        parser_id: parser.id,
        publish_targets: ["local_markdown"],
        allow_cloud: parser.location === "cloud" && allowCloudParser,
      });
      setDialog(null);
      setStagedSource(null);
      setParserChoices([]);
      setNotice(`已加入导入队列：${stagedSource.file_name}`);
      onRefresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "提交导入失败");
    } finally {
      setBusy(null);
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

  function renderDocRow(doc: KnowledgeDocument, kind: SourceKind) {
    const type = docType(doc);
    const status = statusView(doc);
    return (
      <button
        key={doc.id}
        type="button"
        onClick={() => setDetail({ doc, kind })}
        className="flex w-full items-center gap-2.5 rounded-2xl px-2.5 py-2.5 text-left transition hover:bg-[#002fa7]/[0.04]"
      >
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.06] text-[9px] font-bold text-[#002fa7]">{type.glyph}</span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-medium text-gray-800">{doc.title}</span>
          <span className="mt-0.5 block text-[10px] text-gray-400">{type.label} · {formatSize(doc.size_bytes)} · {relativeTime(doc.updated_at)}</span>
        </span>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${status.className}`}>{status.label}</span>
      </button>
    );
  }

  function renderReadLaterRow(item: ReadLaterItem) {
    const status = readLaterStatus(item);
    return (
      <button
        key={`read-later-${item.id}`}
        type="button"
        onClick={() => router.push("/knowledge/read-later")}
        className="flex w-full items-center gap-2.5 rounded-2xl px-2.5 py-2.5 text-left transition hover:bg-[#002fa7]/[0.04]"
      >
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.06] text-[9px] font-bold text-[#002fa7]">网页</span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-medium text-gray-800">{item.title || item.original_url}</span>
          <span className="mt-0.5 block truncate text-[10px] text-gray-400">{item.site_name || item.canonical_url} · {relativeTime(item.updated_at)}</span>
        </span>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${status.className}`}>{status.label}</span>
      </button>
    );
  }

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

      <section className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm sm:p-6">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-gray-950">快速开始</h2>
            <p className="mt-1 text-xs text-gray-400">从当前意图直接进入下一步。</p>
          </div>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
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
            className="group flex min-h-[92px] items-center gap-3 rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.035] px-4 py-3 text-left transition hover:border-[#002fa7]/30 hover:bg-[#002fa7]/[0.07] disabled:cursor-wait disabled:opacity-60"
          >
            <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-[#002fa7]">
              {busy === "upload" ? <Loader2 className="h-5 w-5 animate-spin" /> : <FileUp className="h-5 w-5" />}
            </span>
            <span className="min-w-0 flex-1"><strong className="block text-sm text-gray-900">上传资料</strong><span className="mt-1 block text-[11px] leading-4 text-gray-400">PDF / Markdown / 表格，后台自动解析</span></span>
          </button>
          <button type="button" onClick={openReadLater} disabled={Boolean(busy)} className="group flex min-h-[92px] items-center gap-3 rounded-2xl border border-black/[0.06] bg-black/[0.018] px-4 py-3 text-left transition hover:border-cyan-500/20 hover:bg-cyan-50/60 disabled:opacity-60">
            <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-cyan-50 text-cyan-700"><BookOpenCheck className="h-5 w-5" /></span>
            <span className="min-w-0 flex-1"><strong className="block text-sm text-gray-900">收藏链接</strong><span className="mt-1 block text-[11px] leading-4 text-gray-400">自动抓取并整理正文</span></span>
          </button>
          <button type="button" onClick={() => void openCompile()} disabled={Boolean(busy)} className="group flex min-h-[92px] items-center gap-3 rounded-2xl border border-black/[0.06] bg-black/[0.018] px-4 py-3 text-left transition hover:border-violet-500/20 hover:bg-violet-50/60 disabled:opacity-60">
            <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-violet-50 text-violet-700"><Sparkles className="h-5 w-5" /></span>
            <span className="min-w-0 flex-1"><strong className="block text-sm text-gray-900">编译 Wiki</strong><span className="mt-1 block text-[11px] leading-4 text-gray-400">选择 Raw 并提交后台</span></span>
          </button>
        </div>
        {notice ? <p className="mt-3 text-xs font-medium text-emerald-700">{notice}</p> : null}
        {actionError && !dialog ? <p className="mt-3 text-xs font-medium text-red-600">{actionError}</p> : null}
      </section>

      <section className="grid items-start gap-4 lg:grid-cols-3">
        {sourceColumns.map((column) => {
          const docs = sourceGroups[column.kind];
          const entries = [
            ...docs.map((doc) => ({ type: "document" as const, updatedAt: doc.updated_at || "", doc })),
            ...(column.kind === "web"
              ? projectedReadLater.map((item) => ({ type: "read-later" as const, updatedAt: item.updated_at || "", item }))
              : []),
          ].sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
          return (
            <div key={column.kind} className="flex flex-col rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2.5">
                  <KindLogo kind={column.kind} />
                  <div className="min-w-0">
                    <h3 className="truncate text-sm font-semibold text-gray-950">{column.title}</h3>
                    <p className="mt-0.5 text-[11px] text-gray-400">{column.hint} · {entries.length} 项</p>
                  </div>
                </div>
                <Link
                  href={`/knowledge/library?source=${column.kind}`}
                  className="inline-flex shrink-0 items-center gap-0.5 text-xs font-semibold text-[#002fa7] hover:underline"
                >
                  查看更多<ChevronRight className="h-3.5 w-3.5" />
                </Link>
              </div>
              <div className="mt-3 space-y-1">
                {entries.slice(0, 5).map((entry) => entry.type === "document"
                  ? renderDocRow(entry.doc, column.kind)
                  : renderReadLaterRow(entry.item))}
                {!entries.length ? (
                  <div className="rounded-2xl border border-dashed border-black/[0.08] px-4 py-8 text-center text-xs text-gray-400">还没有内容。</div>
                ) : null}
              </div>
            </div>
          );
        })}
      </section>

      <section className="rounded-2xl border border-black/[0.06] bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-gray-950">后台动态</h2>
            <p className="mt-1 text-xs text-gray-400">最近任务与处理状态。</p>
          </div>
          <Link href="/knowledge/imports" className="text-xs font-semibold text-[#002fa7]">查看全部</Link>
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-3">
          {latestJobs.length ? latestJobs.map((job) => (
            <Link key={job.id} href={`/knowledge/imports/${job.id}`} className="group flex items-center gap-3 rounded-2xl bg-black/[0.022] px-3 py-3 transition hover:bg-black/[0.04]">
              <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl ${job.status === "staged" ? "bg-amber-50 text-amber-700" : isActive(job) ? "bg-[#002fa7]/10 text-[#002fa7]" : "bg-emerald-50 text-emerald-700"}`}>
                {job.status === "staged" || isActive(job) ? <Clock3 className="h-4 w-4" /> : <CheckCircle2 className="h-4 w-4" />}
              </span>
              <span className="min-w-0 flex-1"><strong className="block truncate text-xs text-gray-800">{job.title || job.file_name}</strong><span className="mt-0.5 block text-[10px] text-gray-400">{overviewJobStatus(job)}</span></span>
              {job.status === "staged" ? <span className="flex shrink-0 items-center gap-0.5 text-[10px] font-semibold text-[#002fa7]">继续解析<ChevronRight className="h-3 w-3 transition group-hover:translate-x-0.5" /></span> : null}
            </Link>
          )) : (
            <div className="rounded-2xl border border-dashed border-black/[0.08] px-4 py-7 text-center text-xs text-gray-400 sm:col-span-3">还没有后台任务</div>
          )}
        </div>
      </section>

      {detail ? (
        <DocumentDetailModal
          doc={detail.doc}
          kind={detail.kind}
          sourceName={sourceNameOf(detail.kind)}
          onClose={() => setDetail(null)}
        />
      ) : null}

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
                {dialog === "read-later" ? <Link2 className="h-5 w-5" /> : dialog === "parser" ? <FileUp className="h-5 w-5" /> : <Sparkles className="h-5 w-5" />}
              </span>
              <div className="min-w-0 flex-1">
                <h2 id="quick-action-title" className="text-base font-semibold text-gray-950">
                  {dialog === "read-later" ? "收藏链接" : dialog === "parser" ? "选择文档解析器" : "编译 Wiki"}
                </h2>
                <p className="mt-1 text-xs leading-5 text-gray-500">
                  {dialog === "read-later" ? "保存后将在后台抓取并整理正文。" : dialog === "parser" ? "文件已经暂存；确认解析器后才会创建后台任务。" : "选择本次要交给 Wiki Compiler Agent 的 Raw。"}
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
            ) : dialog === "parser" ? (
              <div className="px-6 py-5">
                {stagedSource ? <p className="mb-3 truncate text-xs font-semibold text-gray-700">{stagedSource.file_name} · {Math.max(1, Math.round(stagedSource.size_bytes / 1024))} KB</p> : null}
                <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
                  {orderedParserChoices.map((parser) => {
                    const selected = selectedParserId === parser.id;
                    return (
                      <button key={parser.id} type="button" disabled={!parser.selectable} onClick={() => { setSelectedParserId(parser.id); if (parser.location !== "cloud") setAllowCloudParser(false); }} className={`flex w-full items-start gap-3 rounded-2xl border p-3 text-left transition ${selected ? "border-[#002fa7] bg-[#002fa7]/[0.06]" : parser.selectable ? "border-black/[0.07] hover:border-[#002fa7]/25" : "cursor-not-allowed border-black/[0.05] bg-gray-50 opacity-55"}`}>
                        <span className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${parser.location === "cloud" ? "bg-violet-50 text-violet-600" : "bg-emerald-50 text-emerald-700"}`}>{parser.location === "cloud" ? <Cloud className="h-4 w-4" /> : <HardDrive className="h-4 w-4" />}</span>
                        <span className="min-w-0 flex-1"><span className="flex flex-wrap items-center gap-2"><strong className="text-xs text-gray-900">{parser.name}</strong>{parser.recommended ? <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[9px] font-semibold text-emerald-700">推荐</span> : null}</span><span className="mt-1 block text-[10px] leading-4 text-gray-500">{parser.selectable ? parser.description : parser.enabled ? `已启用，但当前不可选：${parser.health_message || parser.reason}` : `已停用：${parser.health_message || parser.reason}`}</span></span>
                        <span className={`mt-2 h-4 w-4 rounded-full border ${selected ? "border-[5px] border-[#002fa7]" : "border-gray-300"}`} />
                      </button>
                    );
                  })}
                </div>
                {parserChoices.find((item) => item.id === selectedParserId)?.location === "cloud" ? <label className="mt-3 flex cursor-pointer items-start gap-2 rounded-xl bg-amber-50 px-3 py-2.5 text-[10px] leading-4 text-amber-800"><input type="checkbox" checked={allowCloudParser} onChange={(event) => setAllowCloudParser(event.target.checked)} className="mt-0.5 accent-[#002fa7]" /><span>我确认原始文件将发送至第三方云端服务；未勾选时禁止提交。</span></label> : null}
                <Link href="/settings?category=knowledge&section=parsers#knowledge-section-parsers" className="mt-3 inline-block text-[10px] font-semibold text-[#002fa7]">管理解析器与密钥</Link>
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
                onClick={() => void (dialog === "read-later" ? saveUrl() : dialog === "parser" ? commitUpload() : compileWiki())}
                disabled={Boolean(busy) || (dialog === "read-later" ? !url.trim() : dialog === "parser" ? !selectedParserId || (parserChoices.find((item) => item.id === selectedParserId)?.location === "cloud" && !allowCloudParser) : !selectedRaw.size)}
                className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-semibold text-white shadow-sm hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                {dialog === "read-later" ? "收藏" : dialog === "parser" ? "创建后台任务" : importGbrain ? "编译并导入 GBrain" : "提交编译"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </>
  );
}
