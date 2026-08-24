"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, Search } from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import Sidebar from "@/components/layout/Sidebar";
import DocumentDetailModal, {
  docType,
  formatSize,
  KindLogo,
  statusView,
  KIND_LABEL,
  type SourceKind,
} from "@/components/knowledge/DocumentDetailModal";
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import FeishuBitableDetailModal from "@/components/knowledge/FeishuBitableDetailModal";
import { listKnowledgeDocuments, listReadLaterItems, type KnowledgeDocument, type ReadLaterItem } from "@/lib/api";
import {
  listKnowledgeSourceItems,
  listKnowledgeSources,
  type KnowledgeSource,
  type KnowledgeSourceItem,
} from "@/lib/knowledgeSourcesApi";
import { useApp } from "@/lib/store";

const PAGE_SIZE = 10;

function readLaterStatus(item: ReadLaterItem): { label: string; className: string } {
  if (item.parse_status === "queued") return { label: "等待解析", className: "bg-amber-50 text-amber-700" };
  if (item.parse_status === "processing") return { label: "解析中", className: "bg-[#002fa7]/10 text-[#002fa7]" };
  if (item.parse_status === "failed") return { label: "失败", className: "bg-red-50 text-red-600" };
  if (item.parse_status === "link_only") return { label: "仅保留链接", className: "bg-amber-50 text-amber-700" };
  return { label: "已入库", className: "bg-emerald-50 text-emerald-700" };
}

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

function KnowledgeLibraryContent() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [mounted, setMounted] = useState(false);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [readLater, setReadLater] = useState<ReadLaterItem[]>([]);
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [bitableItems, setBitableItems] = useState<KnowledgeSourceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [sourceFilter, setSourceFilter] = useState<"all" | SourceKind>(() => {
    const value = searchParams.get("source");
    return value === "feishu" || value === "local" || value === "web" ? value : "all";
  });
  const [statusFilter, setStatusFilter] = useState("all");
  const [page, setPage] = useState(0);
  const [selectedDocId, setSelectedDocId] = useState("");
  const [selectedBitableId, setSelectedBitableId] = useState("");

  useEffect(() => setMounted(true), []);
  useEffect(() => setPage(0), [query, sourceFilter, statusFilter]);

  const refresh = useCallback(async () => {
    try {
      const [nextDocuments, nextSources, nextReadLater] = await Promise.all([
        listKnowledgeDocuments(),
        listKnowledgeSources(),
        listReadLaterItems(),
      ]);
      const bitableSources = nextSources.filter((source) => source.connector_key === "feishu_wiki");
      const nextBitableItems = (await Promise.all(
        bitableSources.map((source) => listKnowledgeSourceItems(source.id)),
      )).flat().filter((item) => item.external_type === "bitable" && item.status !== "deleted");
      setDocuments(nextDocuments);
      setSources(nextSources);
      setReadLater(nextReadLater);
      setBitableItems(nextBitableItems);
      setNotice("");
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    const delay = readLater.some((item) => item.parse_status === "queued" || item.parse_status === "processing") ? 2500 : 10000;
    const timer = window.setInterval(() => { void refresh(); }, delay);
    return () => window.clearInterval(timer);
  }, [readLater, refresh]);

  const sourceById = useMemo(() => new Map(sources.map((source) => [source.id, source])), [sources]);

  const rows = useMemo(() => {
    const documentRows = documents.map((doc) => {
      const source = doc.source_connection_id ? sourceById.get(doc.source_connection_id) : undefined;
      const kind: SourceKind = source
        ? (source.connector_key === "feishu_wiki" ? "feishu" : source.connector_key === "web_capture" ? "web" : "local")
        : doc.source_type.startsWith("feishu") ? "feishu"
          : doc.source_type === "read_later" || doc.source_type === "web" ? "web"
            : "local";
      const location = (doc.virtual_path || doc.source_path || "").replace(/^\/+|\/+$/g, "").split("/").filter(Boolean).join(" / ");
      return {
        id: `document:${doc.id}`,
        doc,
        bitable: null as KnowledgeSourceItem | null,
        title: doc.title,
        secondary: `${docType(doc).label} · ${formatSize(doc.size_bytes)}`,
        updatedAt: doc.updated_at,
        kind,
        sourceName: source?.name || KIND_LABEL[kind],
        location,
        type: docType(doc),
        status: statusView(doc),
      };
    });
    const documentIds = new Set(documents.map((document) => document.id));
    const pendingRows = readLater
      .filter((item) => !item.document_id || !documentIds.has(item.document_id))
      .map((item) => ({
        id: `read-later:${item.id}`,
        doc: null as KnowledgeDocument | null,
        bitable: null as KnowledgeSourceItem | null,
        title: item.title || item.original_url,
        secondary: item.parse_status === "failed"
          ? "网页 · 解析失败"
          : item.parse_status === "link_only"
            ? "网页 · 仅保留链接"
            : item.parse_status === "ready"
              ? "网页 · 正文已生成"
              : "网页 · 正文生成中",
        updatedAt: item.updated_at,
        kind: "web" as SourceKind,
        sourceName: "网页收藏",
        location: item.site_name || new URL(item.canonical_url).hostname,
        type: { glyph: "网页", label: "网页" },
        status: readLaterStatus(item),
      }));
    const bitableRows = bitableItems.map((item) => {
      const source = sourceById.get(item.source_connection_id);
      const fields = Array.isArray(item.metadata.fields) ? item.metadata.fields : [];
      return {
        id: `bitable:${item.id}`,
        doc: null as KnowledgeDocument | null,
        bitable: item,
        title: item.title || source?.name || "飞书多维表格",
        secondary: `多维表格 · ${fields.length} 个字段`,
        updatedAt: item.updated_at,
        kind: "feishu" as SourceKind,
        sourceName: source?.name || "飞书",
        location: "飞书 / 多维表格 / 实时连接",
        type: { glyph: "表", label: "多维表格" },
        status: { label: "实时连接", className: "bg-emerald-50 text-emerald-700" },
      };
    });
    return [...documentRows, ...bitableRows, ...pendingRows].sort(
      (left, right) => Date.parse(right.updatedAt || "") - Date.parse(left.updatedAt || "")
    );
  }, [bitableItems, documents, readLater, sourceById]);

  const visible = useMemo(() => rows.filter((row) => {
    if (sourceFilter !== "all" && row.kind !== sourceFilter) return false;
    if (statusFilter !== "all" && row.status.label !== statusFilter) return false;
    const keyword = query.trim().toLowerCase();
    if (!keyword) return true;
    return row.title.toLowerCase().includes(keyword) || row.location.toLowerCase().includes(keyword) || row.sourceName.toLowerCase().includes(keyword);
  }), [rows, sourceFilter, statusFilter, query]);

  const pageCount = Math.max(1, Math.ceil(visible.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const paged = visible.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

  const selectedRow = useMemo(() => rows.find((row) => row.doc?.id === selectedDocId) || null, [rows, selectedDocId]);
  const selectedBitableRow = useMemo(() => rows.find((row) => row.bitable?.id === selectedBitableId) || null, [rows, selectedBitableId]);
  const selectedBitableSource = selectedBitableRow?.bitable
    ? sourceById.get(selectedBitableRow.bitable.source_connection_id)
    : undefined;

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
                    <option value="等待解析">等待解析</option>
                    <option value="解析中">解析中</option>
                    <option value="处理中">处理中</option>
                    <option value="实时连接">实时连接</option>
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
                      <button type="button" key={row.id} onClick={() => row.doc ? setSelectedDocId(row.doc.id) : row.bitable ? setSelectedBitableId(row.bitable.id) : router.push("/knowledge/read-later")} className="grid w-full cursor-pointer grid-cols-[minmax(0,2.2fr)_minmax(0,1.2fr)_minmax(0,1.2fr)_120px_90px] items-center gap-4 border-b border-black/[0.04] px-5 py-3.5 text-left transition last:border-b-0 hover:bg-[#002fa7]/[0.03]">
                        <div className="flex min-w-0 items-center gap-3">
                          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.06] text-[10px] font-bold text-[#002fa7]">{row.type.glyph}</span>
                          <span className="min-w-0">
                            <span className="block truncate text-sm font-semibold text-gray-900">{row.title}</span>
                            <span className="mt-0.5 block text-[11px] text-gray-400">{row.secondary}</span>
                          </span>
                        </div>
                        <div className="flex min-w-0 items-center gap-2"><KindLogo kind={row.kind} /><span className="truncate text-xs text-gray-600">{row.sourceName}</span></div>
                        <div className="truncate text-xs text-gray-500">{row.location || "—"}</div>
                        <div className="text-xs text-gray-500">{relativeTime(row.updatedAt)}</div>
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
      {selectedRow?.doc ? (
        <DocumentDetailModal
          doc={selectedRow.doc}
          kind={selectedRow.kind}
          sourceName={selectedRow.sourceName}
          onClose={() => setSelectedDocId("")}
        />
      ) : null}
      {selectedBitableRow?.bitable && selectedBitableSource ? (
        <FeishuBitableDetailModal
          item={selectedBitableRow.bitable}
          source={selectedBitableSource}
          onClose={() => setSelectedBitableId("")}
        />
      ) : null}
    </div>
  );
}

export default function KnowledgeLibraryPage() {
  return (
    <Suspense fallback={null}>
      <KnowledgeLibraryContent />
    </Suspense>
  );
}
