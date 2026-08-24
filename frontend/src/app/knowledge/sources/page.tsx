"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  ChevronRight,
  Database,
  ExternalLink,
  FileText,
  FileUp,
  Globe2,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  Unplug,
  X,
} from "lucide-react";

import FeishuConnectionWizard from "@/components/knowledge/FeishuConnectionWizard";
import FeishuBitableDetailModal from "@/components/knowledge/FeishuBitableDetailModal";
import DocumentDetailModal, { type SourceKind } from "@/components/knowledge/DocumentDetailModal";
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import {
  commitKnowledgeImportSource,
  listKnowledgeDocuments,
  listReadLaterItems,
  saveReadLaterUrl,
  stageKnowledgeImportSource,
  type DocumentParserStatus,
  type KnowledgeDocument,
  type ReadLaterItem,
  type StagedKnowledgeSource,
} from "@/lib/api";
import {
  listKnowledgeSourceItems,
  listKnowledgeSourceRuns,
  listKnowledgeSources,
  cancelKnowledgeSourceSync,
  startKnowledgeSourceSync,
  type KnowledgeSource,
  type KnowledgeSourceItem,
  type KnowledgeSyncRun,
} from "@/lib/knowledgeSourcesApi";
import { useApp } from "@/lib/store";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

function relativeTime(value: string | null | undefined): string {
  if (!value) return "尚未同步";
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "尚未同步";
  const minutes = Math.max(1, Math.floor((Date.now() - time) / 60000));
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 1440)} 天前`;
}

function statusView(status: string): { label: string; className: string } {
  if (status === "ready" || status === "succeeded" || status === "indexed") return { label: "可用", className: "bg-emerald-500" };
  if (status === "syncing" || status === "running" || status === "queued" || status === "processing") return { label: "同步中", className: "bg-amber-500 animate-pulse" };
  if (status === "pending_auth") return { label: "待授权", className: "bg-amber-500" };
  if (status === "needs_reauth") return { label: "需重新授权", className: "bg-red-500" };
  if (status === "error" || status === "succeeded_with_errors") return { label: "有错误", className: "bg-red-500" };
  if (status === "disabled") return { label: "已停用", className: "bg-gray-400" };
  return { label: status || "未知", className: "bg-red-500" };
}

function sourceItemStatusView(status: string): { label: string; className: string } {
  if (status === "staged") return { label: "待解析", className: "bg-amber-50 text-amber-700" };
  if (status === "indexed") return { label: "已索引", className: "bg-emerald-50 text-emerald-700" };
  if (["queued", "processing", "running"].includes(status)) {
    return { label: "处理中", className: "bg-blue-50 text-[#002fa7]" };
  }
  if (["failed", "error"].includes(status)) return { label: "失败", className: "bg-red-50 text-red-600" };
  if (status === "linked") return { label: "实时连接", className: "bg-violet-50 text-violet-700" };
  if (status === "ready" || status === "succeeded") return { label: "可用", className: "bg-emerald-50 text-emerald-700" };
  return { label: status || "未知", className: "bg-gray-100 text-gray-600" };
}

function importJobIdOf(item: KnowledgeSourceItem): string {
  for (const key of ["import_job_id", "job_id"]) {
    const value = item.metadata?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  const prefix = "import-job:";
  return item.external_id.startsWith(prefix) ? item.external_id.slice(prefix.length) : "";
}

function SourceMark({ kind }: { kind: KnowledgeSource["connector_key"] }) {
  const src = kind === "local_upload" ? "/brands/local-upload.svg" : kind === "web_capture" ? "/brands/web-capture.svg" : "/brands/feishu-logo.svg";
  const alt = kind === "local_upload" ? "本地上传" : kind === "web_capture" ? "网页收藏" : "飞书";
  return <img src={src} alt={alt} className="h-11 w-11 rounded-2xl border border-black/[0.06] bg-white object-cover shadow-sm" />;
}

function SourceList({ sources, selectedId, onSelect, onAdd }: {
  sources: KnowledgeSource[];
  selectedId: string;
  onSelect: (id: string) => void;
  onAdd: () => void;
}) {
  return (
    <section className="overflow-hidden rounded-3xl border border-black/[0.06] bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-black/[0.055] px-5 py-4">
        <div><h2 className="text-sm font-semibold text-gray-950">已连接来源</h2><p className="mt-0.5 text-[11px] text-gray-400">{sources.length} 个来源</p></div>
        <button type="button" onClick={onAdd} className="grid h-8 w-8 place-items-center rounded-xl bg-[#002fa7]/[0.06] text-[#002fa7] hover:bg-[#002fa7]/10" aria-label="添加来源"><Plus className="h-4 w-4" /></button>
      </div>
      <div className="space-y-1 p-2">
        {sources.map((source) => {
          const status = statusView(source.status);
          const subtitle = source.connector_key === "local_upload" ? "PDF、Markdown 与 Office 文件" : source.connector_key === "web_capture" ? "稍后读与网页正文" : `${source.auth_type === "user" ? "用户身份" : "应用身份"} · ${source.item_count || 0} 项`;
          return (
            <button type="button" key={source.id} onClick={() => onSelect(source.id)} className={`flex w-full items-center gap-3 rounded-2xl border px-3 py-3 text-left transition ${selectedId === source.id ? "border-[#002fa7]/15 bg-[#002fa7]/[0.055]" : "border-transparent hover:bg-gray-50"}`}>
              <SourceMark kind={source.connector_key} />
              <span className="min-w-0 flex-1"><span className="block truncate text-sm font-semibold text-gray-900">{source.name}</span><span className="mt-0.5 block truncate text-[11px] text-gray-400">{subtitle}</span></span>
              <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${status.className}`} title={status.label} />
            </button>
          );
        })}
      </div>
    </section>
  );
}

function ConnectorPicker({ open, onClose, onPickFeishu }: { open: boolean; onClose: () => void; onPickFeishu: () => void }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[110] grid place-items-center bg-slate-950/30 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-label="添加资料来源">
      <div className="w-full max-w-lg rounded-[28px] border border-white/70 bg-white shadow-2xl shadow-slate-950/15">
        <div className="flex items-start justify-between border-b border-black/[0.06] px-6 py-5">
          <div><h2 className="text-lg font-semibold tracking-tight text-gray-950">添加资料来源</h2><p className="mt-1 text-xs text-gray-400">选择要连接的 Connector 类型</p></div>
          <button type="button" onClick={onClose} aria-label="关闭" className="grid h-9 w-9 place-items-center rounded-xl bg-gray-50 text-gray-500 hover:bg-gray-100"><X className="h-4 w-4" /></button>
        </div>
        <div className="space-y-3 p-6">
          <button type="button" onClick={onPickFeishu} className="flex w-full items-center gap-4 rounded-2xl border border-black/[0.07] p-4 text-left transition hover:border-[#002fa7]/25 hover:bg-[#002fa7]/[0.03]">
            <img src="/brands/feishu-logo.svg" alt="飞书" className="h-11 w-11 shrink-0 rounded-2xl border border-black/[0.06] object-cover" />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold text-gray-900">飞书 Wiki / 多维表格</span>
              <span className="mt-0.5 block text-xs leading-5 text-gray-500">同步 Wiki 文档，或登记 Bitable 供 Agent 实时只读查询。</span>
            </span>
            <ChevronRight className="h-4 w-4 shrink-0 text-gray-300" />
          </button>
          <div className="rounded-2xl border border-dashed border-black/[0.08] px-4 py-5 text-center text-xs text-gray-400">更多 Connector（Notion、语雀等）即将上线</div>
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return <div className="rounded-2xl border border-black/[0.055] bg-gray-50/70 p-4"><div className="text-[11px] font-medium text-gray-400">{label}</div><div className="mt-2 text-lg font-semibold tracking-tight text-gray-900">{value}</div>{hint ? <div className="mt-1 text-[10px] text-gray-400">{hint}</div> : null}</div>;
}

function LocalUploadPanel({ source, items, onChanged, onOpenDoc }: { source: KnowledgeSource; items: KnowledgeSourceItem[]; onChanged: () => void | Promise<void>; onOpenDoc: (item: KnowledgeSourceItem) => void }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [stagedSource, setStagedSource] = useState<StagedKnowledgeSource | null>(null);
  const [parsers, setParsers] = useState<DocumentParserStatus[]>([]);
  const [selectedParserId, setSelectedParserId] = useState("");
  const [allowCloud, setAllowCloud] = useState(false);
  const pendingParseCount = items.filter((item) => item.status === "staged").length;
  const indexedCount = items.filter((item) => item.status === "indexed").length;
  const processingCount = items.filter((item) => ["queued", "processing", "running"].includes(item.status)).length;

  async function stageNext(file: File) {
    const staged = await stageKnowledgeImportSource(file);
    setStagedSource(staged.source);
    setParsers(staged.parsers);
    setSelectedParserId(staged.parsers.find((item) => item.recommended && item.selectable)?.id || "");
    setAllowCloud(false);
  }

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setNotice("");
    try {
      const queue = Array.from(files);
      setPendingFiles(queue);
      await stageNext(queue[0]);
      setNotice(`已暂存 ${queue.length} 个文件中的第 1 个，请选择解析器。`);
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function commitCurrent() {
    if (!stagedSource || !selectedParserId) return;
    const selected = parsers.find((item) => item.id === selectedParserId);
    if (!selected?.selectable) return;
    setUploading(true);
    setNotice("");
    try {
      await commitKnowledgeImportSource(stagedSource.id, {
        parser_id: selectedParserId,
        publish_targets: ["local_markdown", "vector"],
        allow_cloud: selected.location === "cloud" && allowCloud,
      });
      const rest = pendingFiles.slice(1);
      if (rest.length) {
        setPendingFiles(rest);
        await stageNext(rest[0]);
        setNotice(`当前文件已提交；还剩 ${rest.length} 个文件，请继续选择解析器。`);
      } else {
        setPendingFiles([]);
        setStagedSource(null);
        setParsers([]);
        setSelectedParserId("");
        setNotice("全部文件已提交，后台将继续解析和索引。");
        await onChanged();
      }
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-5 rounded-3xl border border-[#002fa7]/10 bg-[#002fa7]/[0.035] p-6 sm:flex-row sm:items-center sm:justify-between">
        <div><div className="text-[10px] font-bold uppercase tracking-[0.14em] text-[#002fa7]">内置来源 · 无需连接</div><h3 className="mt-2 text-xl font-semibold tracking-tight text-gray-950">把文件直接放进统一资料库</h3><p className="mt-2 max-w-xl text-sm leading-6 text-gray-500">PDF 交给 MinerU 解析；Markdown、Office 与表格沿用当前导入任务。上传后即使离开页面，后台任务仍会继续。</p></div>
        <button type="button" onClick={() => inputRef.current?.click()} disabled={uploading} className="inline-flex h-11 shrink-0 items-center justify-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-semibold text-white shadow-sm disabled:opacity-50">{uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}选择文件</button>
        <input ref={inputRef} type="file" multiple className="hidden" onChange={(event) => void upload(event.target.files)} accept=".pdf,.md,.markdown,.txt,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.csv,.tsv,image/*" />
      </div>
      {stagedSource ? (
        <div className="rounded-3xl border border-[#002fa7]/15 bg-white p-5 shadow-sm">
          <div><p className="text-sm font-semibold text-gray-950">选择解析器 · {stagedSource.file_name}</p><p className="mt-1 text-xs text-gray-400">原始文件已暂存，不会再次上传。</p></div>
          <div className="mt-3 grid gap-2 md:grid-cols-2">
            {parsers.map((parser) => <button key={parser.id} type="button" disabled={!parser.selectable} onClick={() => { setSelectedParserId(parser.id); if (parser.location !== "cloud") setAllowCloud(false); }} className={`rounded-2xl border p-3 text-left ${selectedParserId === parser.id ? "border-[#002fa7] bg-[#002fa7]/[0.05]" : parser.selectable ? "border-black/[0.07]" : "cursor-not-allowed border-black/[0.05] bg-gray-50 opacity-55"}`}><span className="text-xs font-semibold text-gray-900">{parser.name}</span><span className="mt-1 block text-[10px] leading-4 text-gray-500">{parser.selectable ? parser.description : parser.health_message}</span></button>)}
          </div>
          {parsers.find((item) => item.id === selectedParserId)?.location === "cloud" ? <label className="mt-3 flex items-start gap-2 rounded-xl bg-amber-50 px-3 py-2 text-[10px] text-amber-800"><input type="checkbox" checked={allowCloud} onChange={(event) => setAllowCloud(event.target.checked)} className="mt-0.5 accent-[#002fa7]" />允许把当前文件发送至第三方云端解析服务</label> : null}
          <div className="mt-4 flex justify-end"><button type="button" onClick={() => void commitCurrent()} disabled={uploading || !selectedParserId || (parsers.find((item) => item.id === selectedParserId)?.location === "cloud" && !allowCloud)} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-xs font-semibold text-white disabled:opacity-45">{uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : null}创建后台任务</button></div>
        </div>
      ) : null}
      {notice ? <div className={`rounded-xl px-4 py-3 text-xs ${notice.includes("已提交") ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{notice}</div> : null}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Metric label="文件总数" value={source.item_count ?? items.length} />
        <Metric label="待解析" value={pendingParseCount} />
        <Metric label="已索引" value={indexedCount} />
        <Metric label="处理中" value={processingCount} />
        <Metric label="最近导入" value={relativeTime(items[0]?.updated_at)} />
      </div>
      <RecentItems items={items} empty="还没有上传文件。" onOpen={onOpenDoc} />
    </div>
  );
}

function WebCapturePanel({ source, onChanged, onOpenDoc }: { source: KnowledgeSource; onChanged: () => void | Promise<void>; onOpenDoc: (item: ReadLaterItem) => void }) {
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [items, setItems] = useState<ReadLaterItem[]>([]);
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => setItems(await listReadLaterItems({ search: query.trim() })), [query]);
  useEffect(() => { void refresh().catch((error) => setNotice(messageOf(error))); }, [refresh]);

  async function collect() {
    if (!url.trim()) return;
    setSaving(true);
    setNotice("");
    try {
      const result = await saveReadLaterUrl({ url: url.trim() });
      setUrl("");
      setNotice(result.deduplicated ? "这个链接已经在网页收藏中。" : "已收藏，后台正在提取正文。 ");
      await Promise.all([refresh(), onChanged()]);
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex gap-2 rounded-2xl border border-black/[0.06] bg-white p-2 shadow-sm">
        <div className="flex min-w-0 flex-1 items-center gap-2 px-3"><Globe2 className="h-4 w-4 shrink-0 text-[#002fa7]" /><input value={url} onChange={(event) => setUrl(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void collect(); }} placeholder="粘贴文章链接，例如 https://..." className="h-10 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-gray-400" /></div>
        <button type="button" disabled={saving || !url.trim()} onClick={() => void collect()} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-xs font-semibold text-white disabled:opacity-40">{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : null}收藏并解析</button>
      </div>
      {notice ? <div className={`rounded-xl px-4 py-3 text-xs ${notice.includes("失败") || notice.includes("错误") ? "bg-red-50 text-red-700" : "bg-emerald-50 text-emerald-700"}`}>{notice}</div> : null}
      <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-900">网页收藏</h3><p className="mt-1 text-xs text-gray-400">正文进入统一资料库，阅读状态仍保留。</p></div><div className="flex items-center gap-2"><label className="flex h-9 items-center gap-2 rounded-xl border border-black/[0.07] px-3"><Search className="h-3.5 w-3.5 text-gray-400" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索收藏" className="w-28 bg-transparent text-xs outline-none" /></label><Link href="/knowledge/read-later" className="inline-flex h-9 items-center gap-1.5 rounded-xl bg-gray-100 px-3 text-xs font-semibold text-gray-600 hover:bg-gray-200">进入阅读器 <ExternalLink className="h-3.5 w-3.5" /></Link></div></div>
      <div className="overflow-hidden rounded-2xl border border-black/[0.06]">
        {items.length ? items.slice(0, 8).map((item) => {
          const clickable = Boolean(item.document_id);
          return (
            <button type="button" key={item.id} disabled={!clickable} onClick={() => clickable && onOpenDoc(item)} className={`flex w-full items-center gap-3 border-b border-black/[0.05] px-4 py-3 text-left last:border-0 ${clickable ? "cursor-pointer transition hover:bg-[#002fa7]/[0.03]" : "cursor-default"}`}><span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.055] text-[#002fa7]"><Globe2 className="h-4 w-4" /></span><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium text-gray-800">{item.title || item.original_url}</span><span className="mt-0.5 block truncate text-[10px] text-gray-400">{item.site_name || item.canonical_url} · {relativeTime(item.updated_at)}</span></span><span className={`rounded-full px-2 py-1 text-[10px] font-semibold ${item.parse_status === "ready" ? "bg-emerald-50 text-emerald-700" : item.parse_status === "failed" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-700"}`}>{item.parse_status === "ready" ? "正文就绪" : item.parse_status === "failed" ? "解析失败" : "解析中"}</span></button>
          );
        }) : <div className="px-5 py-12 text-center text-xs text-gray-400">还没有网页收藏。</div>}
      </div>
      <div className="hidden">{source.item_count}</div>
    </div>
  );
}

function RecentItems({ items, empty, onOpen, onOpenBitable }: { items: KnowledgeSourceItem[]; empty: string; onOpen?: (item: KnowledgeSourceItem) => void; onOpenBitable?: (item: KnowledgeSourceItem) => void }) {
  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">最近内容</h3>
        <span className="text-[11px] text-gray-400">最近更新优先</span>
      </div>
      <div className="overflow-hidden rounded-2xl border border-black/[0.06]">
        {items.length ? items.slice(0, 8).map((item) => {
          const importJobId = item.status === "staged" ? importJobIdOf(item) : "";
          const canOpenDocument = Boolean(onOpen && item.document_id);
          const status = sourceItemStatusView(item.status);
          const content = (
            <>
              <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ${item.status === "staged" ? "bg-amber-50 text-amber-700" : "bg-[#002fa7]/[0.05] text-[#002fa7]"}`}>
                {item.external_type === "bitable" ? <Database className="h-4 w-4" /> : <FileText className="h-4 w-4" />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-gray-800">{item.title || item.external_id}</span>
                <span className="mt-0.5 block truncate text-[10px] text-gray-400">
                  {item.external_type === "bitable" ? `${Array.isArray(item.metadata.fields) ? item.metadata.fields.length : 0} 个字段 · 不保存行数据` : item.path?.join(" / ") || item.external_type} · {relativeTime(item.updated_at)}
                </span>
              </span>
              <span className={`rounded-full px-2.5 py-1 text-[10px] font-semibold ${status.className}`}>{status.label}</span>
              {importJobId || canOpenDocument ? <ChevronRight className="h-4 w-4 shrink-0 text-gray-300 transition group-hover:translate-x-0.5 group-hover:text-[#002fa7]" /> : null}
            </>
          );
          if (importJobId) {
            return (
              <Link
                key={item.id}
                href={`/knowledge/imports/${encodeURIComponent(importJobId)}`}
                className="group flex w-full items-center gap-3 border-b border-black/[0.05] px-4 py-3 text-left transition last:border-0 hover:bg-amber-50/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#002fa7]/30"
              >
                {content}
              </Link>
            );
          }
          if (item.external_type === "bitable") {
            return (
              <button
                type="button"
                key={item.id}
                onClick={() => onOpenBitable?.(item)}
                className="group flex w-full items-center gap-3 border-b border-black/[0.05] px-4 py-3 text-left transition last:border-0 hover:bg-[#002fa7]/[0.03] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#002fa7]/30"
              >
                {content}<ChevronRight className="h-4 w-4 shrink-0 text-gray-300 transition group-hover:translate-x-0.5 group-hover:text-[#002fa7]" />
              </button>
            );
          }
          return (
            <button
              type="button"
              key={item.id}
              disabled={!canOpenDocument}
              onClick={() => canOpenDocument && onOpen?.(item)}
              className={`group flex w-full items-center gap-3 border-b border-black/[0.05] px-4 py-3 text-left last:border-0 ${canOpenDocument ? "cursor-pointer transition hover:bg-[#002fa7]/[0.03]" : "cursor-default"}`}
            >
              {content}
            </button>
          );
        }) : <div className="px-5 py-12 text-center text-xs text-gray-400">{empty}</div>}
      </div>
    </div>
  );
}

function FeishuPanel({ source, items, runs, onChanged, onReconnect, onOpenDoc, onOpenBitable }: { source: KnowledgeSource; items: KnowledgeSourceItem[]; runs: KnowledgeSyncRun[]; onChanged: () => void | Promise<void>; onReconnect: () => void; onOpenDoc: (item: KnowledgeSourceItem) => void; onOpenBitable: (item: KnowledgeSourceItem) => void }) {
  const [busyMode, setBusyMode] = useState("");
  const [notice, setNotice] = useState("");
  const status = statusView(source.status);
  const isBitable = source.config.source_mode === "bitable";

  async function sync(mode: "incremental" | "full_scan" | "reindex") {
    setBusyMode(mode);
    setNotice("");
    try {
      await startKnowledgeSourceSync(source.id, mode);
      setNotice(isBitable ? "已提交字段 Schema 刷新；不会读取或保存行数据。" : mode === "full_scan" ? "已提交完整扫描；缺失的远端条目会在扫描结束后标记删除。" : mode === "reindex" ? "已提交重建索引；文档将重新规范化并写入索引。" : "已提交增量同步。 ");
      await onChanged();
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setBusyMode("");
    }
  }

  async function cancel() {
    if (!lastRun) return;
    setBusyMode("cancel");
    setNotice("");
    try {
      await cancelKnowledgeSourceSync(source.id, lastRun.id);
      setNotice("已请求取消同步。当前文档处理完成后任务会停止。 ");
      await onChanged();
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setBusyMode("");
    }
  }

  const lastRun = runs[0];
  const scopeLabel = isBitable
    ? `多维表格实时读取 · ${String(source.config.table_name || source.config.table_id || "尚未选择数据表")}`
    : typeof source.config.space_id === "string" ? `${source.config.space_id}${source.config.root_node_token ? " / 指定根节点" : " / 整个空间"}` : "尚未选择同步范围";
  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between"><div><div className="flex items-center gap-2"><span className={`h-2.5 w-2.5 rounded-full ${status.className}`} /><span className="text-xs font-semibold text-gray-600">{status.label}</span><span className="rounded-full bg-blue-50 px-2 py-1 text-[10px] font-semibold text-blue-700">{source.auth_type === "user" ? "用户身份" : "应用身份"}</span>{isBitable ? <span className="rounded-full bg-violet-50 px-2 py-1 text-[10px] font-semibold text-violet-700">不保存行数据</span> : null}</div><p className="mt-2 text-xs leading-5 text-gray-400">{scopeLabel}</p></div><div className="flex flex-wrap gap-2"><button type="button" onClick={onReconnect} className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/10 px-3 text-xs font-semibold text-gray-700"><Settings2 className="h-3.5 w-3.5" />编辑连接</button>{lastRun && ["queued", "running"].includes(lastRun.status) ? <button type="button" disabled={!!busyMode} onClick={() => void cancel()} className="inline-flex h-9 items-center gap-2 rounded-xl border border-red-200 px-3 text-xs font-semibold text-red-600 disabled:opacity-40">取消同步</button> : isBitable ? <button type="button" disabled={!!busyMode || !["ready", "error"].includes(source.status)} onClick={() => void sync("incremental")} className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-3.5 text-xs font-semibold text-white disabled:opacity-40"><RefreshCw className={`h-3.5 w-3.5 ${busyMode === "incremental" ? "animate-spin" : ""}`} />刷新字段 Schema</button> : <><button type="button" disabled={!!busyMode || !["ready", "error"].includes(source.status)} onClick={() => void sync("reindex")} className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/10 px-3 text-xs font-semibold text-gray-700 disabled:opacity-40">重建索引</button><button type="button" disabled={!!busyMode || !["ready", "error"].includes(source.status)} onClick={() => void sync("full_scan")} className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/10 px-3 text-xs font-semibold text-gray-700 disabled:opacity-40"><RotateCcw className={`h-3.5 w-3.5 ${busyMode === "full_scan" ? "animate-spin" : ""}`} />完整扫描</button><button type="button" disabled={!!busyMode || !["ready", "error"].includes(source.status)} onClick={() => void sync("incremental")} className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-3.5 text-xs font-semibold text-white disabled:opacity-40"><RefreshCw className={`h-3.5 w-3.5 ${busyMode === "incremental" ? "animate-spin" : ""}`} />立即同步</button></>}</div></div>
      {notice ? <div className={`rounded-xl px-4 py-3 text-xs ${notice.includes("已提交") ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{notice}</div> : null}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4"><Metric label={isBitable ? "已登记数据表" : "内容条目"} value={source.item_count || items.length} /><Metric label={isBitable ? "Schema 状态" : "同步状态"} value={lastRun ? statusView(lastRun.status).label : "尚未运行"} hint={lastRun?.current_step} /><Metric label={isBitable ? "上次探测" : "上次同步"} value={relativeTime(source.last_synced_at)} /><Metric label="失败条目" value={lastRun?.stats.failed || 0} /></div>
      {lastRun ? <div className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-black/[0.06] bg-black/[0.06] sm:grid-cols-5">{(isBitable ? [["数据表", lastRun.stats.discovered || 0], ["实时关联", lastRun.stats.linked || 0], ["Schema 更新", lastRun.stats.schema_changed || 0], ["行数据落库", 0], ["失败", lastRun.stats.failed || 0]] : [["发现", lastRun.stats.discovered || 0], ["更新", lastRun.stats.changed || 0], ["跳过", lastRun.stats.unchanged || 0], ["删除", lastRun.stats.deleted || 0], ["失败", lastRun.stats.failed || 0]]).map(([label, value]) => <div key={String(label)} className="bg-white px-4 py-3"><div className="text-[10px] text-gray-400">{label}</div><div className="mt-1 text-base font-semibold text-gray-900">{value}</div></div>)}</div> : null}
      {source.last_error && Object.keys(source.last_error).length ? <div className="flex gap-3 rounded-2xl bg-red-50 p-4 text-xs leading-5 text-red-700"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /><span>{String(source.last_error.message || source.last_error.detail || "最近一次同步出现错误。")}</span></div> : null}
      <RecentItems items={items} empty={isBitable ? "保存连接后会登记数据表与字段 Schema，不会复制记录正文。" : "完成首次同步后，飞书文档会出现在这里。"} onOpen={onOpenDoc} onOpenBitable={onOpenBitable} />
    </div>
  );
}

export default function KnowledgeSourcesPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [items, setItems] = useState<KnowledgeSourceItem[]>([]);
  const [runs, setRuns] = useState<KnowledgeSyncRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [reconnectSource, setReconnectSource] = useState<KnowledgeSource | null>(null);
  const [notice, setNotice] = useState("");
  const [detail, setDetail] = useState<{ doc: KnowledgeDocument; kind: SourceKind; sourceName: string } | null>(null);
  const [bitableDetail, setBitableDetail] = useState<{ item: KnowledgeSourceItem; source: KnowledgeSource } | null>(null);

  useEffect(() => setMounted(true), []);
  const selected = useMemo(() => sources.find((source) => source.id === selectedId) || sources[0] || null, [selectedId, sources]);
  const docById = useMemo(() => new Map(documents.map((doc) => [doc.id, doc])), [documents]);

  const openDocument = useCallback((source: KnowledgeSource, documentId: string | null) => {
    if (!documentId) return;
    const doc = docById.get(documentId);
    if (!doc) return;
    const kind: SourceKind = source.connector_key === "feishu_wiki" ? "feishu" : source.connector_key === "web_capture" ? "web" : "local";
    setDetail({ doc, kind, sourceName: source.name });
  }, [docById]);

  const refreshSources = useCallback(async () => {
    try {
      const [next, nextDocuments] = await Promise.all([listKnowledgeSources(), listKnowledgeDocuments()]);
      setSources(next);
      setDocuments(nextDocuments);
      setSelectedId((current) => next.some((source) => source.id === current) ? current : next[0]?.id || "");
      setNotice("");
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshSelected = useCallback(async () => {
    if (!selectedId) return;
    try {
      const [nextItems, nextRuns] = await Promise.all([listKnowledgeSourceItems(selectedId), listKnowledgeSourceRuns(selectedId)]);
      setItems(nextItems);
      setRuns(nextRuns);
    } catch (error) {
      setNotice(messageOf(error));
    }
  }, [selectedId]);

  useEffect(() => { void refreshSources(); }, [refreshSources]);
  useEffect(() => { setItems([]); setRuns([]); void refreshSelected(); }, [refreshSelected]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void Promise.all([refreshSources(), refreshSelected()]);
    }, 6000);
    return () => window.clearInterval(timer);
  }, [refreshSelected, refreshSources]);

  const readyCount = sources.filter((source) => source.status === "ready").length;
  const totalItems = sources.reduce((sum, source) => sum + (source.item_count || 0), 0);
  const lastSynced = sources.map((source) => source.last_synced_at).filter(Boolean).sort().at(-1) || null;

  async function changed() {
    await Promise.all([refreshSources(), refreshSelected()]);
  }

  return (
    <div className="h-screen app-bg text-gray-950">
      <div className="fixed left-3 top-3 z-[80]"><Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact /></div>
      <div className="flex h-full overflow-hidden">
        <div className="workspace-sidebar-shell shrink-0 overflow-hidden transition-[width]" style={{ width: sidebarOpen ? sidebarWidth : 0 }}><div style={{ width: sidebarWidth, minWidth: 200 }} className="flex h-full flex-col"><div className="h-11 shrink-0" /><div className="min-h-0 flex-1 overflow-hidden"><Sidebar /></div></div></div>
        {mounted && sidebarOpen ? <ResizeHandle onResize={(delta) => setSidebarWidth((value: number) => Math.max(200, value + delta))} direction="left" /> : null}
        <main className="workspace-content-frame min-w-0 flex-1 overflow-y-auto">
          <div className="workspace-page-container flex flex-col gap-5">
            <KnowledgeWorkspaceHeader section="sources" actions={<button type="button" onClick={() => setPickerOpen(true)} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-xs font-semibold text-white shadow-sm shadow-[#002fa7]/15"><Plus className="h-4 w-4" />添加来源</button>} />
            <KnowledgeWorkspaceNav />
            <section className="grid grid-cols-2 gap-3 lg:grid-cols-4"><Metric label="资料来源" value={sources.length} hint={`${readyCount} 个可用`} /><Metric label="知识条目" value={totalItems} hint="跨来源统一计数" /><Metric label="内置来源" value={2} hint="本地上传与网页收藏" /><Metric label="最近同步" value={relativeTime(lastSynced)} /></section>
            {notice ? <div className="rounded-xl bg-red-50 px-4 py-3 text-xs text-red-700">{notice}</div> : null}
            {loading ? <div className="grid min-h-[360px] place-items-center"><Loader2 className="h-6 w-6 animate-spin text-[#002fa7]" /></div> : (
              <section className="grid min-w-0 gap-5 lg:grid-cols-[300px_minmax(0,1fr)]">
                <SourceList sources={sources} selectedId={selected?.id || ""} onSelect={setSelectedId} onAdd={() => setPickerOpen(true)} />
                <div className="min-w-0 rounded-3xl border border-black/[0.06] bg-white p-5 shadow-sm sm:p-6">
                  {selected && !selected.builtin ? <div className="mb-6 flex items-center gap-3 border-b border-black/[0.055] pb-5"><SourceMark kind={selected.connector_key} /><div className="min-w-0"><h2 className="truncate text-lg font-semibold tracking-tight text-gray-950">{selected.name}</h2><div className="mt-1 flex items-center gap-2 text-[11px] text-gray-400"><span>可同步 Connector</span><span>·</span><span>{selected.item_count || 0} 项</span></div></div></div> : null}
                  {!selected ? <div className="grid min-h-[300px] place-items-center text-sm text-gray-400"><div className="text-center"><Unplug className="mx-auto mb-3 h-7 w-7" />还没有资料来源</div></div> : selected.connector_key === "local_upload" ? <LocalUploadPanel source={selected} items={items} onChanged={changed} onOpenDoc={(item) => openDocument(selected, item.document_id)} /> : selected.connector_key === "web_capture" ? <WebCapturePanel source={selected} onChanged={changed} onOpenDoc={(item) => openDocument(selected, item.document_id)} /> : <FeishuPanel source={selected} items={items} runs={runs} onChanged={changed} onReconnect={() => { setReconnectSource(selected); setWizardOpen(true); }} onOpenDoc={(item) => openDocument(selected, item.document_id)} onOpenBitable={(item) => setBitableDetail({ item, source: selected })} />}
                </div>
              </section>
            )}
          </div>
        </main>
      </div>
      <ConnectorPicker open={pickerOpen} onClose={() => setPickerOpen(false)} onPickFeishu={() => { setPickerOpen(false); setReconnectSource(null); setWizardOpen(true); }} />
      <FeishuConnectionWizard open={wizardOpen} existingSource={reconnectSource} onClose={() => setWizardOpen(false)} onConnected={async (next) => { await refreshSources(); setSelectedId(next.id); }} />
      {detail ? <DocumentDetailModal doc={detail.doc} kind={detail.kind} sourceName={detail.sourceName} onClose={() => setDetail(null)} /> : null}
      {bitableDetail ? <FeishuBitableDetailModal item={bitableDetail.item} source={bitableDetail.source} onClose={() => setBitableDetail(null)} /> : null}
    </div>
  );
}
