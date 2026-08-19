"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  ChevronRight,
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
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import {
  createKnowledgeImportJob,
  listReadLaterItems,
  saveReadLaterUrl,
  type ReadLaterItem,
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

function SourceMark({ kind }: { kind: KnowledgeSource["connector_key"] }) {
  if (kind === "local_upload") return <span className="grid h-11 w-11 place-items-center rounded-2xl border border-black/[0.08] bg-white text-lg font-bold text-[#002fa7] shadow-sm">本</span>;
  if (kind === "web_capture") return <span className="grid h-11 w-11 place-items-center rounded-2xl border border-black/[0.08] bg-white text-lg font-bold text-[#002fa7] shadow-sm">网</span>;
  return <img src="/brands/feishu-logo.svg" alt="飞书" className="h-11 w-11 rounded-2xl border border-black/[0.06] bg-white object-cover shadow-sm" />;
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
              <span className="block text-sm font-semibold text-gray-900">飞书知识库</span>
              <span className="mt-0.5 block text-xs leading-5 text-gray-500">同步飞书 Wiki 空间文档，支持应用身份与用户身份（OAuth）授权。</span>
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

function LocalUploadPanel({ source, items, onChanged }: { source: KnowledgeSource; items: KnowledgeSourceItem[]; onChanged: () => void | Promise<void> }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState("");

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setNotice("");
    try {
      for (const file of Array.from(files)) await createKnowledgeImportJob(file, undefined, ["local_markdown", "vector"]);
      setNotice(`已提交 ${files.length} 个文件，后台将继续解析和索引。`);
      await onChanged();
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-5 rounded-3xl border border-[#002fa7]/10 bg-[#002fa7]/[0.035] p-6 sm:flex-row sm:items-center sm:justify-between">
        <div><div className="text-[10px] font-bold uppercase tracking-[0.14em] text-[#002fa7]">内置来源 · 无需连接</div><h3 className="mt-2 text-xl font-semibold tracking-tight text-gray-950">把文件直接放进统一资料库</h3><p className="mt-2 max-w-xl text-sm leading-6 text-gray-500">PDF 交给 MinerU 解析；Markdown、Office 与表格沿用当前导入任务。上传后即使离开页面，后台任务仍会继续。</p></div>
        <button type="button" onClick={() => inputRef.current?.click()} disabled={uploading} className="inline-flex h-11 shrink-0 items-center justify-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-semibold text-white shadow-sm disabled:opacity-50">{uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}选择文件</button>
        <input ref={inputRef} type="file" multiple className="hidden" onChange={(event) => void upload(event.target.files)} accept=".pdf,.md,.markdown,.txt,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.csv,.tsv,image/*" />
      </div>
      {notice ? <div className={`rounded-xl px-4 py-3 text-xs ${notice.includes("已提交") ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{notice}</div> : null}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4"><Metric label="文件数量" value={source.item_count || items.length} /><Metric label="已索引" value={items.filter((item) => item.status === "indexed").length} /><Metric label="处理中" value={items.filter((item) => ["queued", "processing"].includes(item.status)).length} /><Metric label="最近导入" value={relativeTime(items[0]?.updated_at)} /></div>
      <RecentItems items={items} empty="还没有上传文件。" />
    </div>
  );
}

function WebCapturePanel({ source, onChanged }: { source: KnowledgeSource; onChanged: () => void | Promise<void> }) {
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
        {items.length ? items.slice(0, 8).map((item) => (
          <div key={item.id} className="flex items-center gap-3 border-b border-black/[0.05] px-4 py-3 last:border-0"><span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.055] text-[#002fa7]"><Globe2 className="h-4 w-4" /></span><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium text-gray-800">{item.title || item.original_url}</span><span className="mt-0.5 block truncate text-[10px] text-gray-400">{item.site_name || item.canonical_url} · {relativeTime(item.updated_at)}</span></span><span className={`rounded-full px-2 py-1 text-[10px] font-semibold ${item.parse_status === "ready" ? "bg-emerald-50 text-emerald-700" : item.parse_status === "failed" ? "bg-red-50 text-red-600" : "bg-amber-50 text-amber-700"}`}>{item.parse_status === "ready" ? "正文就绪" : item.parse_status === "failed" ? "解析失败" : "解析中"}</span></div>
        )) : <div className="px-5 py-12 text-center text-xs text-gray-400">还没有网页收藏。</div>}
      </div>
      <div className="hidden">{source.item_count}</div>
    </div>
  );
}

function RecentItems({ items, empty }: { items: KnowledgeSourceItem[]; empty: string }) {
  return (
    <div><div className="mb-3 flex items-center justify-between"><h3 className="text-sm font-semibold text-gray-900">最近内容</h3><span className="text-[11px] text-gray-400">最近更新优先</span></div><div className="overflow-hidden rounded-2xl border border-black/[0.06]">{items.length ? items.slice(0, 8).map((item) => <div key={item.id} className="flex items-center gap-3 border-b border-black/[0.05] px-4 py-3 last:border-0"><span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.05] text-[#002fa7]"><FileText className="h-4 w-4" /></span><span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium text-gray-800">{item.title || item.external_id}</span><span className="mt-0.5 block truncate text-[10px] text-gray-400">{item.path?.join(" / ") || item.external_type} · {relativeTime(item.updated_at)}</span></span><span className="rounded-full bg-gray-100 px-2 py-1 text-[10px] font-semibold text-gray-600">{item.status}</span></div>) : <div className="px-5 py-12 text-center text-xs text-gray-400">{empty}</div>}</div></div>
  );
}

function FeishuPanel({ source, items, runs, onChanged, onReconnect }: { source: KnowledgeSource; items: KnowledgeSourceItem[]; runs: KnowledgeSyncRun[]; onChanged: () => void | Promise<void>; onReconnect: () => void }) {
  const [busyMode, setBusyMode] = useState("");
  const [notice, setNotice] = useState("");
  const status = statusView(source.status);

  async function sync(mode: "incremental" | "full_scan" | "reindex") {
    setBusyMode(mode);
    setNotice("");
    try {
      await startKnowledgeSourceSync(source.id, mode);
      setNotice(mode === "full_scan" ? "已提交完整扫描；缺失的远端条目会在扫描结束后标记删除。" : mode === "reindex" ? "已提交重建索引；文档将重新规范化并写入索引。" : "已提交增量同步。 ");
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
  const scopeLabel = typeof source.config.space_id === "string" ? `${source.config.space_id}${source.config.root_node_token ? " / 指定根节点" : " / 整个空间"}` : "尚未选择同步范围";
  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between"><div><div className="flex items-center gap-2"><span className={`h-2.5 w-2.5 rounded-full ${status.className}`} /><span className="text-xs font-semibold text-gray-600">{status.label}</span><span className="rounded-full bg-blue-50 px-2 py-1 text-[10px] font-semibold text-blue-700">{source.auth_type === "user" ? "用户身份" : "应用身份"}</span></div><p className="mt-2 text-xs leading-5 text-gray-400">{scopeLabel}</p></div><div className="flex flex-wrap gap-2"><button type="button" onClick={onReconnect} className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/10 px-3 text-xs font-semibold text-gray-700"><Settings2 className="h-3.5 w-3.5" />编辑连接</button>{lastRun && ["queued", "running"].includes(lastRun.status) ? <button type="button" disabled={!!busyMode} onClick={() => void cancel()} className="inline-flex h-9 items-center gap-2 rounded-xl border border-red-200 px-3 text-xs font-semibold text-red-600 disabled:opacity-40">取消同步</button> : <><button type="button" disabled={!!busyMode || !["ready", "error"].includes(source.status)} onClick={() => void sync("reindex")} className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/10 px-3 text-xs font-semibold text-gray-700 disabled:opacity-40">重建索引</button><button type="button" disabled={!!busyMode || !["ready", "error"].includes(source.status)} onClick={() => void sync("full_scan")} className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/10 px-3 text-xs font-semibold text-gray-700 disabled:opacity-40"><RotateCcw className={`h-3.5 w-3.5 ${busyMode === "full_scan" ? "animate-spin" : ""}`} />完整扫描</button><button type="button" disabled={!!busyMode || !["ready", "error"].includes(source.status)} onClick={() => void sync("incremental")} className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-3.5 text-xs font-semibold text-white disabled:opacity-40"><RefreshCw className={`h-3.5 w-3.5 ${busyMode === "incremental" ? "animate-spin" : ""}`} />立即同步</button></>}</div></div>
      {notice ? <div className={`rounded-xl px-4 py-3 text-xs ${notice.includes("已提交") ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{notice}</div> : null}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4"><Metric label="内容条目" value={source.item_count || items.length} /><Metric label="同步状态" value={lastRun ? statusView(lastRun.status).label : "尚未运行"} hint={lastRun?.current_step} /><Metric label="上次同步" value={relativeTime(source.last_synced_at)} /><Metric label="失败条目" value={lastRun?.stats.failed || 0} /></div>
      {lastRun ? <div className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-black/[0.06] bg-black/[0.06] sm:grid-cols-5">{[["发现", lastRun.stats.discovered || 0], ["更新", lastRun.stats.changed || 0], ["跳过", lastRun.stats.unchanged || 0], ["删除", lastRun.stats.deleted || 0], ["失败", lastRun.stats.failed || 0]].map(([label, value]) => <div key={String(label)} className="bg-white px-4 py-3"><div className="text-[10px] text-gray-400">{label}</div><div className="mt-1 text-base font-semibold text-gray-900">{value}</div></div>)}</div> : null}
      {source.last_error && Object.keys(source.last_error).length ? <div className="flex gap-3 rounded-2xl bg-red-50 p-4 text-xs leading-5 text-red-700"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /><span>{String(source.last_error.message || source.last_error.detail || "最近一次同步出现错误。")}</span></div> : null}
      <RecentItems items={items} empty="完成首次同步后，飞书文档会出现在这里。" />
    </div>
  );
}

export default function KnowledgeSourcesPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [items, setItems] = useState<KnowledgeSourceItem[]>([]);
  const [runs, setRuns] = useState<KnowledgeSyncRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [reconnectSource, setReconnectSource] = useState<KnowledgeSource | null>(null);
  const [notice, setNotice] = useState("");

  useEffect(() => setMounted(true), []);
  const selected = useMemo(() => sources.find((source) => source.id === selectedId) || sources[0] || null, [selectedId, sources]);

  const refreshSources = useCallback(async () => {
    try {
      const next = await listKnowledgeSources();
      setSources(next);
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
                  {selected ? <div className="mb-6 flex items-center gap-3 border-b border-black/[0.055] pb-5"><SourceMark kind={selected.connector_key} /><div className="min-w-0"><h2 className="truncate text-lg font-semibold tracking-tight text-gray-950">{selected.name}</h2><div className="mt-1 flex items-center gap-2 text-[11px] text-gray-400"><span>{selected.builtin ? "内置来源" : "可同步 Connector"}</span><span>·</span><span>{selected.item_count || 0} 项</span></div></div></div> : null}
                  {!selected ? <div className="grid min-h-[300px] place-items-center text-sm text-gray-400"><div className="text-center"><Unplug className="mx-auto mb-3 h-7 w-7" />还没有资料来源</div></div> : selected.connector_key === "local_upload" ? <LocalUploadPanel source={selected} items={items} onChanged={changed} /> : selected.connector_key === "web_capture" ? <WebCapturePanel source={selected} onChanged={changed} /> : <FeishuPanel source={selected} items={items} runs={runs} onChanged={changed} onReconnect={() => { setReconnectSource(selected); setWizardOpen(true); }} />}
                </div>
              </section>
            )}
          </div>
        </main>
      </div>
      <ConnectorPicker open={pickerOpen} onClose={() => setPickerOpen(false)} onPickFeishu={() => { setPickerOpen(false); setReconnectSource(null); setWizardOpen(true); }} />
      <FeishuConnectionWizard open={wizardOpen} existingSource={reconnectSource} onClose={() => setWizardOpen(false)} onConnected={async (next) => { await refreshSources(); setSelectedId(next.id); }} />
    </div>
  );
}
