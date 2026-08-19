"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Database, ExternalLink, Loader2, Search, X } from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import FeishuSourceCard, { feishuMetaOf } from "@/components/knowledge/FeishuSourceCard";
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import {
  listKnowledgeDocuments,
  previewKnowledgeFile,
  publishKnowledgeDocumentVector,
  type KnowledgeDocument,
  type KnowledgeFilePreview,
} from "@/lib/api";
import { listKnowledgeSources, type KnowledgeSource } from "@/lib/knowledgeSourcesApi";
import { useApp } from "@/lib/store";

type SourceKind = "feishu" | "local" | "web";

const PAGE_SIZE = 10;

const KIND_LABEL: Record<SourceKind, string> = { feishu: "飞书", local: "本地上传", web: "网页收藏" };

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

function relativeTime(value: string | null | undefined): string {
  if (!value) return "—";
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "—";
  const minutes = Math.max(1, Math.floor((Date.now() - time) / 60000));
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) {
    const date = new Date(value);
    const today = new Date();
    if (date.toDateString() === today.toDateString()) {
      return `今天 ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
    }
    return `${Math.floor(minutes / 60)} 小时前`;
  }
  if (minutes < 2880) {
    const date = new Date(value);
    return `昨天 ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  }
  return `${Math.floor(minutes / 1440)} 天前`;
}

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function fullTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function docType(doc: KnowledgeDocument): { glyph: string; label: string } {
  const mime = (doc.mime_type || "").toLowerCase();
  const name = (doc.title || doc.source_path || "").toLowerCase();
  if (mime.includes("pdf") || name.endsWith(".pdf")) return { glyph: "PDF", label: "PDF" };
  if (mime.includes("word") || /\.docx?$/.test(name)) return { glyph: "DOC", label: "Docx" };
  if (mime.includes("spreadsheet") || /\.(xlsx?|xlsm)$/.test(name)) return { glyph: "XLS", label: "Excel" };
  if (mime.includes("csv") || name.endsWith(".csv")) return { glyph: "CSV", label: "CSV" };
  if (mime.includes("presentation") || /\.pptx?$/.test(name)) return { glyph: "PPT", label: "PPT" };
  if (mime.startsWith("image/") || /\.(png|jpe?g|gif|webp|svg)$/.test(name)) return { glyph: "IMG", label: "图片" };
  if (mime.includes("html") || doc.source_type === "read_later" || /\.html?$/.test(name)) return { glyph: "网页", label: "网页" };
  if (mime.includes("markdown") || /\.(md|markdown)$/.test(name)) return { glyph: "MD", label: "Markdown" };
  const extension = name.includes(".") ? name.split(".").pop() || "" : "";
  const label = extension && extension.length <= 5 ? extension.toUpperCase() : "文件";
  return { glyph: label.slice(0, 4), label };
}

function statusView(doc: KnowledgeDocument): { label: string; className: string } {
  const status = doc.status;
  if (status === "processing" || status === "queued" || status === "parsing") return { label: "处理中", className: "bg-amber-50 text-amber-600" };
  if (status === "error" || status === "failed") return { label: "失败", className: "bg-red-50 text-red-600" };
  if (status === "deleted") return { label: "已删除", className: "bg-gray-100 text-gray-400" };
  if (status === "ready" || status === "indexed") {
    const targets = doc.publish_targets || [];
    if (!targets.includes("vector") && !targets.includes("local_vector")) return { label: "已入库", className: "bg-gray-100 text-gray-500" };
    const stamp = (doc.metadata?.vector_index || null) as { refreshed?: boolean } | null;
    if (stamp?.refreshed) return { label: "已索引", className: "bg-emerald-50 text-emerald-600" };
    return { label: "待索引", className: "bg-amber-50 text-amber-600" };
  }
  return { label: status || "未知", className: "bg-gray-100 text-gray-500" };
}

function vectorIndexLabel(doc: KnowledgeDocument): string {
  const targets = doc.publish_targets || [];
  if (!targets.includes("vector") && !targets.includes("local_vector")) return "不入向量库";
  const stamp = (doc.metadata?.vector_index || null) as { refreshed?: boolean; generated_at?: string } | null;
  if (stamp?.refreshed) return stamp.generated_at ? `已索引 · ${fullTime(stamp.generated_at)}` : "已索引";
  return "待索引";
}

function KindLogo({ kind }: { kind: SourceKind }) {
  if (kind === "feishu") return <img src="/brands/feishu-logo.svg" alt="飞书" className="h-5 w-5 shrink-0 rounded-md border border-black/[0.06] object-cover" />;
  return (
    <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md border border-black/[0.08] bg-white text-[10px] font-bold text-[#002fa7]">
      {kind === "local" ? "本" : "网"}
    </span>
  );
}

export default function KnowledgeLibraryPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState<"all" | SourceKind>("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [page, setPage] = useState(0);
  const [selectedDocId, setSelectedDocId] = useState("");
  const [preview, setPreview] = useState<KnowledgeFilePreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [toast, setToast] = useState("");

  useEffect(() => setMounted(true), []);
  useEffect(() => setPage(0), [query, sourceFilter, statusFilter]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 4000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const refresh = useCallback(async () => {
    try {
      const [nextDocuments, nextSources] = await Promise.all([listKnowledgeDocuments(), listKnowledgeSources()]);
      setDocuments(nextDocuments);
      setSources(nextSources);
      setNotice("");
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const sourceById = useMemo(() => new Map(sources.map((source) => [source.id, source])), [sources]);

  const rows = useMemo(() => documents.map((doc) => {
    const source = doc.source_connection_id ? sourceById.get(doc.source_connection_id) : undefined;
    const kind: SourceKind = source
      ? (source.connector_key === "feishu_wiki" ? "feishu" : source.connector_key === "web_capture" ? "web" : "local")
      : doc.source_type.startsWith("feishu") ? "feishu"
        : doc.source_type === "read_later" || doc.source_type === "web" ? "web"
          : "local";
    const location = (doc.virtual_path || doc.source_path || "").replace(/^\/+|\/+$/g, "").split("/").filter(Boolean).join(" / ");
    return {
      doc,
      kind,
      sourceName: source?.name || KIND_LABEL[kind],
      location,
      type: docType(doc),
      status: statusView(doc),
    };
  }), [documents, sourceById]);

  const visible = useMemo(() => rows.filter((row) => {
    if (sourceFilter !== "all" && row.kind !== sourceFilter) return false;
    if (statusFilter !== "all" && row.status.label !== statusFilter) return false;
    const keyword = query.trim().toLowerCase();
    if (!keyword) return true;
    return row.doc.title.toLowerCase().includes(keyword) || row.location.toLowerCase().includes(keyword);
  }), [rows, sourceFilter, statusFilter, query]);

  const pageCount = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const paged = visible.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

  const selectedRow = useMemo(() => rows.find((row) => row.doc.id === selectedDocId) || null, [rows, selectedDocId]);
  const selectedFeishuMeta = selectedRow ? feishuMetaOf(selectedRow.doc) : null;

  useEffect(() => {
    if (!selectedRow) {
      setPreview(null);
      setPreviewError("");
      return;
    }
    let cancelled = false;
    setPreview(null);
    setPreviewError("");
    setPreviewLoading(true);
    previewKnowledgeFile(selectedRow.doc.virtual_path || selectedRow.doc.source_path)
      .then((result) => { if (!cancelled) setPreview(result); })
      .catch((error) => { if (!cancelled) setPreviewError(messageOf(error)); })
      .finally(() => { if (!cancelled) setPreviewLoading(false); });
    return () => { cancelled = true; };
  }, [selectedRow]);

  async function publishVector() {
    if (!selectedRow || actionBusy) return;
    setActionBusy(true);
    try {
      await publishKnowledgeDocumentVector(selectedRow.doc.id);
      setToast("已排队重建该文档的向量索引，进度可在任务中心查看。");
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setActionBusy(false);
    }
  }

  return (
    <div className="h-screen app-bg text-gray-950">
      <div className="fixed left-3 top-3 z-[80]"><Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact /></div>
      <div className="flex h-full overflow-hidden">
        <div className="workspace-sidebar-shell shrink-0 overflow-hidden transition-[width]" style={{ width: sidebarOpen ? sidebarWidth : 0 }}><div style={{ width: sidebarWidth, minWidth: 200 }} className="flex h-full flex-col"><div className="h-11 shrink-0" /><div className="min-h-0 flex-1 overflow-hidden"><Sidebar /></div></div></div>
        {mounted && sidebarOpen ? <ResizeHandle onResize={(delta) => setSidebarWidth((value: number) => Math.max(200, value + delta))} direction="left" /> : null}
        <main className="workspace-content-frame min-w-0 flex-1 overflow-y-auto">
          <div className="workspace-page-container flex flex-col gap-5">
            <KnowledgeWorkspaceHeader section="library" />
            <KnowledgeWorkspaceNav />
            {notice ? <div className="rounded-xl bg-red-50 px-4 py-3 text-xs text-red-700">{notice}</div> : null}
            {toast ? <div className="rounded-xl bg-emerald-50 px-4 py-3 text-xs text-emerald-700">{toast}</div> : null}
            {loading ? <div className="grid min-h-[360px] place-items-center"><Loader2 className="h-6 w-6 animate-spin text-[#002fa7]" /></div> : (
              <section className="overflow-hidden rounded-3xl border border-black/[0.06] bg-white shadow-sm">
                <div className="flex flex-wrap items-center gap-3 px-5 py-4">
                  <label className="flex h-10 min-w-[220px] flex-1 items-center gap-2 rounded-xl border border-black/[0.08] px-3.5 sm:max-w-xs">
                    <Search className="h-4 w-4 shrink-0 text-gray-400" />
                    <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题或目录" className="w-full bg-transparent text-sm outline-none placeholder:text-gray-400" />
                  </label>
                  <select value={sourceFilter} onChange={(event) => { setSourceFilter(event.target.value as "all" | SourceKind); event.currentTarget.blur(); }} className="h-10 rounded-xl border border-black/[0.08] bg-white px-3 text-sm text-gray-700 outline-none focus:border-[#002fa7]/40">
                    <option value="all">全部来源</option>
                    <option value="feishu">飞书</option>
                    <option value="local">本地上传</option>
                    <option value="web">网页收藏</option>
                  </select>
                  <select value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); event.currentTarget.blur(); }} className="h-10 rounded-xl border border-black/[0.08] bg-white px-3 text-sm text-gray-700 outline-none focus:border-[#002fa7]/40">
                    <option value="all">全部状态</option>
                    <option value="已索引">已索引</option>
                    <option value="待索引">待索引</option>
                    <option value="已入库">已入库</option>
                    <option value="处理中">处理中</option>
                    <option value="失败">失败</option>
                  </select>
                  <span className="ml-auto text-[11px] text-gray-400">{visible.length} 项 · 来源已统一但可追踪</span>
                </div>
                <div className="overflow-x-auto">
                  <div className="min-w-[860px]">
                    <div className="grid grid-cols-[minmax(0,2.2fr)_minmax(0,1.2fr)_minmax(0,1.2fr)_120px_90px] gap-4 border-y border-black/[0.055] bg-gray-50/60 px-5 py-2.5 text-[11px] font-semibold text-gray-400">
                      <div>名称</div><div>来源</div><div>所在位置</div><div>更新时间</div><div>状态</div>
                    </div>
                    {paged.map((row) => (
                      <button type="button" key={row.doc.id} onClick={() => setSelectedDocId(row.doc.id)} className="grid w-full cursor-pointer grid-cols-[minmax(0,2.2fr)_minmax(0,1.2fr)_minmax(0,1.2fr)_120px_90px] items-center gap-4 border-b border-black/[0.04] px-5 py-3.5 text-left transition last:border-b-0 hover:bg-[#002fa7]/[0.03]">
                        <div className="flex min-w-0 items-center gap-3">
                          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.06] text-[10px] font-bold text-[#002fa7]">{row.type.glyph}</span>
                          <span className="min-w-0">
                            <span className="block truncate text-sm font-semibold text-gray-900">{row.doc.title}</span>
                            <span className="mt-0.5 block text-[11px] text-gray-400">{row.type.label} · {formatSize(row.doc.size_bytes)}</span>
                          </span>
                        </div>
                        <div className="flex min-w-0 items-center gap-2"><KindLogo kind={row.kind} /><span className="truncate text-xs text-gray-600">{row.sourceName}</span></div>
                        <div className="truncate text-xs text-gray-500">{row.location || "—"}</div>
                        <div className="text-xs text-gray-500">{relativeTime(row.doc.updated_at)}</div>
                        <div><span className={`inline-flex rounded-full px-2.5 py-1 text-[11px] font-semibold ${row.status.className}`}>{row.status.label}</span></div>
                      </button>
                    ))}
                    {!visible.length ? <div className="grid place-items-center px-5 py-16 text-sm text-gray-400">没有符合条件的资料。</div> : null}
                  </div>
                </div>
                <div className="flex items-center justify-between border-t border-black/[0.055] px-5 py-3 text-xs text-gray-500">
                  <span>第 {currentPage + 1} / {pageCount} 页 · 共 {visible.length} 项</span>
                  <div className="flex items-center gap-2">
                    <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)} className="inline-flex h-8 items-center rounded-lg border border-black/[0.08] px-3 font-medium text-gray-600 transition hover:border-[#002fa7]/30 hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-black/[0.08] disabled:hover:text-gray-600">上一页</button>
                    <button type="button" disabled={currentPage >= pageCount - 1} onClick={() => setPage(currentPage + 1)} className="inline-flex h-8 items-center rounded-lg border border-black/[0.08] px-3 font-medium text-gray-600 transition hover:border-[#002fa7]/30 hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-black/[0.08] disabled:hover:text-gray-600">下一页</button>
                  </div>
                </div>
              </section>
            )}
          </div>
        </main>
      </div>
      {selectedRow ? (
        <div className="fixed inset-0 z-[110] grid place-items-center bg-slate-950/30 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-label="文档详情" onClick={() => setSelectedDocId("")}>
          <section className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-[24px] bg-white shadow-2xl ring-1 ring-black/[0.08]" onClick={(event) => event.stopPropagation()}>
            <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
              <div className="flex min-w-0 items-center gap-3">
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.06] text-[11px] font-bold text-[#002fa7]">{selectedRow.type.glyph}</span>
                <div className="min-w-0">
                  <h2 className="truncate text-base font-semibold text-gray-950">{selectedRow.doc.title}</h2>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-gray-400">
                    <span>{selectedRow.type.label} · {formatSize(selectedRow.doc.size_bytes)}</span>
                    <span className={`inline-flex rounded-full px-2 py-0.5 font-semibold ${selectedRow.status.className}`}>{selectedRow.status.label}</span>
                  </div>
                </div>
              </div>
              <button type="button" onClick={() => setSelectedDocId("")} aria-label="关闭" className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-gray-50 text-gray-500 hover:bg-gray-100"><X className="h-4 w-4" /></button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
              {selectedFeishuMeta ? (
                <div className="mb-5">
                  <FeishuSourceCard meta={selectedFeishuMeta} variant="card" />
                </div>
              ) : null}
              <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-xs">
                {selectedFeishuMeta ? null : (
                  <div><dt className="text-gray-400">来源</dt><dd className="mt-1 flex items-center gap-2 font-medium text-gray-700"><KindLogo kind={selectedRow.kind} />{selectedRow.sourceName}</dd></div>
                )}
                <div><dt className="text-gray-400">更新时间</dt><dd className="mt-1 font-medium text-gray-700">{fullTime(selectedRow.doc.updated_at)}</dd></div>
                <div className="col-span-2"><dt className="text-gray-400">所在位置</dt><dd className="mt-1 break-all font-medium text-gray-700">{selectedRow.location || "—"}</dd></div>
                <div><dt className="text-gray-400">创建时间</dt><dd className="mt-1 font-medium text-gray-700">{fullTime(selectedRow.doc.created_at)}</dd></div>
                <div><dt className="text-gray-400">发布目标</dt><dd className="mt-1 font-medium text-gray-700">{(selectedRow.doc.publish_targets || []).join("、") || "—"}</dd></div>
                <div><dt className="text-gray-400">向量索引</dt><dd className="mt-1 font-medium text-gray-700">{vectorIndexLabel(selectedRow.doc)}</dd></div>
                {!selectedFeishuMeta && selectedRow.doc.origin_url ? (
                  <div className="col-span-2"><dt className="text-gray-400">原文链接</dt><dd className="mt-1"><a href={selectedRow.doc.origin_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 break-all font-medium text-[#002fa7] hover:underline">{selectedRow.doc.origin_url}<ExternalLink className="h-3 w-3 shrink-0" /></a></dd></div>
                ) : null}
              </dl>
              <div className="mt-5 border-t border-black/[0.06] pt-4">
                <div className="text-xs font-semibold text-gray-700">内容预览</div>
                {previewLoading ? <div className="grid place-items-center py-10"><Loader2 className="h-5 w-5 animate-spin text-[#002fa7]" /></div>
                  : previewError ? <div className="mt-2 rounded-xl bg-gray-50 px-4 py-3 text-xs text-gray-500">暂不支持预览该文件（{previewError}）</div>
                    : preview ? (
                      <>
                        <pre className="mt-2 max-h-[46vh] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-gray-50 p-4 font-mono text-[11px] leading-5 text-gray-700">{preview.content || "（空文档）"}</pre>
                        {preview.truncated ? <p className="mt-2 text-[11px] text-gray-400">内容较长，仅显示前一部分。</p> : null}
                      </>
                    ) : null}
              </div>
            </div>
            <div className="flex items-center gap-2 border-t border-black/[0.06] px-6 py-4">
              <button type="button" disabled={actionBusy || selectedRow.doc.status !== "ready"} onClick={() => void publishVector()} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-xs font-semibold text-white disabled:opacity-40">
                {actionBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Database className="h-3.5 w-3.5" />}重建向量索引
              </button>
              <span className="text-[11px] text-gray-400">只重建这一篇，不影响其他文档。</span>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
