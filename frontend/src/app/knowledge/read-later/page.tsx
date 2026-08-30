"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Archive,
  BookOpen,
  Check,
  CheckCircle2,
  Clock3,
  Database,
  ExternalLink,
  ImageOff,
  Inbox,
  Link2,
  Loader2,
  Maximize2,
  Minimize2,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
} from "lucide-react";
import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import { useApp } from "@/lib/store";
import {
  compileReadLaterItems,
  deleteReadLaterItem,
  getReadLaterItem,
  listReadLaterItems,
  rawKnowledgeFileUrl,
  saveReadLaterUrl,
  retryReadLaterItem,
  updateReadLaterItem,
  type ReadLaterItem,
} from "@/lib/api";

type Filter = "all" | "unread" | "read" | "link_only" | "archived";

const parseLabels: Record<string, string> = {
  queued: "等待解析",
  processing: "解析中",
  ready: "正文就绪",
  link_only: "仅链接",
  failed: "解析失败",
};

function hostname(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function relativeTime(value: string | null) {
  if (!value) return "";
  const time = new Date(value).getTime();
  const minutes = Math.max(1, Math.floor((Date.now() - time) / 60000));
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 1440)} 天前`;
}

function ReadLaterCover({ src, title }: { src: string; title: string }) {
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [src]);

  if (!src || failed) return null;
  return (
    <span className="h-20 w-20 shrink-0 overflow-hidden rounded-xl bg-black/[0.035] ring-1 ring-black/[0.05]">
      {/* Local knowledge assets are served through the authenticated raw-file endpoint. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={rawKnowledgeFileUrl(src)}
        alt={`${title}封面`}
        loading="lazy"
        className="h-full w-full object-cover"
        onError={() => setFailed(true)}
      />
    </span>
  );
}

export default function ReadLaterPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [items, setItems] = useState<ReadLaterItem[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<ReadLaterItem | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [sourceOptions, setSourceOptions] = useState<string[]>([]);
  const [url, setUrl] = useState("");
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [importGbrain, setImportGbrain] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [compiling, setCompiling] = useState(false);
  const [readerFullscreen, setReaderFullscreen] = useState(false);
  const [deleteCandidate, setDeleteCandidate] = useState<ReadLaterItem | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const refreshVersion = useRef(0);

  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current;
    try {
      const result = await listReadLaterItems({ source, search: query.trim() });
      if (version !== refreshVersion.current) return;
      setItems(result);
      if (!source && !query.trim()) {
        setSourceOptions(Array.from(new Set(
          result.map((item) => item.site_name || hostname(item.original_url)).filter(Boolean),
        )).sort((left, right) => left.localeCompare(right, "zh-CN")));
      }
      setSelectedId((current) => result.some((item) => item.id === current) ? current : result[0]?.id || "");
    } catch (error) {
      if (version !== refreshVersion.current) return;
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "读取稍后读失败" });
    } finally {
      if (version === refreshVersion.current) setLoading(false);
    }
  }, [query, source]);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    const debounce = window.setTimeout(() => void refresh(), query.trim() ? 250 : 0);
    const timer = query.trim() || source ? null : window.setInterval(() => void refresh(), 4000);
    return () => {
      window.clearTimeout(debounce);
      if (timer !== null) window.clearInterval(timer);
    };
  }, [query, source, refresh]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    void getReadLaterItem(selectedId).then(setDetail).catch(() => setDetail(null));
  }, [selectedId, items]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 3000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!readerFullscreen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setReaderFullscreen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [readerFullscreen]);

  const counts = useMemo(() => ({
    all: items.filter((item) => item.reading_status !== "archived").length,
    unread: items.filter((item) => item.reading_status === "unread").length,
    read: items.filter((item) => item.reading_status === "read").length,
    link_only: items.filter((item) => item.parse_status === "link_only" && item.reading_status !== "archived").length,
    archived: items.filter((item) => item.reading_status === "archived").length,
  }), [items]);

  const visible = useMemo(() => items.filter((item) => {
    if (filter === "link_only" && (item.parse_status !== "link_only" || item.reading_status === "archived")) return false;
    if (filter !== "all" && filter !== "link_only" && item.reading_status !== filter) return false;
    if (filter === "all" && item.reading_status === "archived") return false;
    return true;
  }), [filter, items]);

  const selectedReady = Array.from(checked).filter((id) => items.find((item) => item.id === id)?.parse_status === "ready");

  async function saveUrl() {
    if (!url.trim()) return;
    setSaving(true);
    try {
      const result = await saveReadLaterUrl({ url: url.trim() });
      setUrl("");
      setSelectedId(result.item.id);
      setNotice({ tone: "ok", text: result.deduplicated ? "这个链接已经在稍后读中" : "已收藏，后台正在解析正文" });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "收藏失败" });
    } finally {
      setSaving(false);
    }
  }

  async function updateStatus(itemId: string, readingStatus: "unread" | "read" | "archived") {
    await updateReadLaterItem(itemId, { reading_status: readingStatus });
    await refresh();
  }

  async function retryCapture(itemId: string) {
    try {
      await retryReadLaterItem(itemId);
      setNotice({ tone: "ok", text: "已重新加入解析队列" });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "重新解析失败" });
    }
  }

  async function deleteBookmark() {
    if (!deleteCandidate) return;
    setDeleting(true);
    try {
      await deleteReadLaterItem(deleteCandidate.id);
      const remaining = items.filter((item) => item.id !== deleteCandidate.id);
      setItems(remaining);
      setChecked((current) => {
        const next = new Set(current);
        next.delete(deleteCandidate.id);
        return next;
      });
      setSelectedId(remaining[0]?.id || "");
      setDetail(null);
      setReaderFullscreen(false);
      setDeleteCandidate(null);
      setNotice({ tone: "ok", text: "收藏及本地正文副本已删除" });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "删除失败" });
    } finally {
      setDeleting(false);
    }
  }

  async function compileSelected() {
    if (!selectedReady.length) return;
    setCompiling(true);
    try {
      const job = await compileReadLaterItems(selectedReady, importGbrain);
      setNotice({ tone: "ok", text: importGbrain ? "已提交 Wiki 编译，完成后将进入 GBrain" : "已提交 Wiki 编译任务" });
      setChecked(new Set());
      await refresh();
      window.location.href = `/knowledge/imports/${job.id}`;
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "提交编译失败" });
    } finally {
      setCompiling(false);
    }
  }

  const filters: Array<{ key: Filter; label: string; icon: typeof Inbox }> = [
    { key: "all", label: "收件箱", icon: Inbox },
    { key: "unread", label: "未读", icon: Clock3 },
    { key: "read", label: "已读", icon: CheckCircle2 },
    { key: "link_only", label: "仅链接", icon: Link2 },
    { key: "archived", label: "归档", icon: Archive },
  ];

  return (
    <div className="h-screen app-bg text-gray-950">
      <div className="fixed left-3 top-3 z-[80]"><Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact /></div>
      <div className="flex h-full overflow-hidden">
        <div className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden" style={{ width: sidebarOpen ? sidebarWidth : 0 }}>
          <div style={{ width: sidebarWidth, minWidth: 200 }} className="flex h-full flex-col">
            <div className="h-11 shrink-0" /><div className="min-h-0 flex-1 overflow-hidden"><Sidebar /></div>
          </div>
        </div>
        {mounted && sidebarOpen && <ResizeHandle onResize={(delta) => setSidebarWidth((value: number) => Math.max(200, value + delta))} direction="left" />}
        <main className="workspace-content-frame min-w-0 flex-1 overflow-y-auto">
        <div className="workspace-page-container flex flex-col gap-5">
          <KnowledgeWorkspaceHeader section="readLater" />
          <KnowledgeWorkspaceNav />

      <section className="min-w-0">
        <div className="mb-4 flex gap-2 rounded-2xl border border-black/[0.06] bg-white p-2 shadow-sm">
          <div className="flex min-w-0 flex-1 items-center gap-2 px-3">
            <Link2 className="h-4 w-4 shrink-0 text-[#002fa7]" />
            <input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") void saveUrl(); }}
              placeholder="粘贴文章链接，例如 https://..."
              className="h-10 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-gray-400"
            />
          </div>
          <button onClick={() => void saveUrl()} disabled={saving || !url.trim()} className="flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-40">
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}收藏
          </button>
        </div>

        {notice && <div className={`mb-4 rounded-xl px-4 py-3 text-sm ${notice.tone === "ok" ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{notice.text}</div>}

        <div className="grid min-h-[calc(100vh-190px)] overflow-hidden rounded-[24px] border border-black/[0.06] bg-white shadow-sm lg:grid-cols-[210px_minmax(360px,480px)_minmax(0,1fr)]">
          <aside className="border-b border-black/[0.06] bg-[#fafbfe] p-4 lg:border-b-0 lg:border-r">
            <p className="mb-3 px-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-gray-400">阅读清单</p>
            <nav className="grid grid-cols-2 gap-1 sm:grid-cols-5 lg:grid-cols-1">
              {filters.map(({ key, label, icon: Icon }) => (
                <button key={key} onClick={() => setFilter(key)} className={`flex items-center gap-2 rounded-xl px-3 py-2.5 text-left text-sm transition ${filter === key ? "bg-[#002fa7] font-medium text-white" : "text-gray-600 hover:bg-black/[0.04] hover:text-gray-950"}`}>
                  <Icon className="h-4 w-4" /><span className="flex-1">{label}</span><span className={filter === key ? "text-white/70" : "text-gray-400"}>{counts[key]}</span>
                </button>
              ))}
            </nav>
          </aside>

          <section className="border-b border-black/[0.06] lg:border-b-0 lg:border-r">
            <div className="flex h-16 items-center gap-2 border-b border-black/[0.06] px-4">
              <select
                value={source}
                onChange={(event) => setSource(event.target.value)}
                aria-label="按来源过滤"
                className="h-10 max-w-32 rounded-xl border border-black/[0.06] bg-white px-2.5 text-xs font-medium text-gray-600 outline-none focus:border-[#002fa7]/30"
              >
                <option value="">全部来源</option>
                {sourceOptions.map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
              <div className="flex min-w-0 flex-1 items-center gap-2 rounded-xl bg-black/[0.035] px-3">
                <Search className="h-4 w-4 text-gray-400" />
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、平台或正文" className="h-10 min-w-0 flex-1 bg-transparent text-sm outline-none" />
              </div>
              <button onClick={() => void refresh()} className="rounded-xl p-2.5 text-gray-400 hover:bg-black/[0.04] hover:text-gray-800"><RefreshCw className="h-4 w-4" /></button>
            </div>

            {checked.size > 0 && (
              <div className="border-b border-[#002fa7]/10 bg-[#002fa7]/[0.035] p-3">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-medium text-[#002fa7]">已选 {checked.size} 条</span>
                  <label className="ml-auto flex cursor-pointer items-center gap-2 text-gray-600">
                    <input type="checkbox" checked={importGbrain} onChange={(event) => setImportGbrain(event.target.checked)} className="accent-[#002fa7]" />同时进入 GBrain
                  </label>
                  <button onClick={() => void compileSelected()} disabled={compiling || selectedReady.length === 0} className="flex items-center gap-1.5 rounded-lg bg-[#002fa7] px-3 py-2 font-medium text-white disabled:opacity-40">
                    {compiling ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Database className="h-3.5 w-3.5" />}编译 Wiki
                  </button>
                </div>
                {selectedReady.length !== checked.size && <p className="mt-2 text-[11px] text-amber-700">等待解析或仅链接的收藏不会进入编译。</p>}
              </div>
            )}

            <div className="max-h-[calc(100vh-255px)] overflow-y-auto p-2">
              {loading ? <div className="flex justify-center py-24"><Loader2 className="h-5 w-5 animate-spin text-[#002fa7]" /></div> : visible.length === 0 ? (
                <div className="px-6 py-24 text-center text-sm text-gray-400">这里还没有收藏。</div>
              ) : visible.map((item) => (
                <article key={item.id} onClick={() => setSelectedId(item.id)} className={`group mb-1 cursor-pointer rounded-2xl border p-3.5 transition ${selectedId === item.id ? "border-[#002fa7]/25 bg-[#002fa7]/[0.035]" : "border-transparent hover:bg-black/[0.025]"}`}>
                  <div className="flex gap-3">
                    <button onClick={(event) => { event.stopPropagation(); setChecked((current) => { const next = new Set(current); next.has(item.id) ? next.delete(item.id) : next.add(item.id); return next; }); }} className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${checked.has(item.id) ? "border-[#002fa7] bg-[#002fa7] text-white" : "border-gray-300 bg-white text-transparent"}`}><Check className="h-3.5 w-3.5" /></button>
                    <div className="flex min-w-0 flex-1 gap-3">
                      <div className="min-w-0 flex-1">
                        <h2 className="line-clamp-2 text-sm font-semibold leading-5 text-gray-900">{item.title || hostname(item.original_url)}</h2>
                        {item.description && <p className="mt-1 line-clamp-2 text-xs leading-5 text-gray-500">{item.description}</p>}
                        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px] text-gray-400">
                          <span>{item.site_name || hostname(item.original_url)}</span><span>·</span><span>{relativeTime(item.created_at)}</span>
                          <span className={`rounded-md px-1.5 py-0.5 ${item.parse_status === "ready" ? "bg-emerald-50 text-emerald-700" : item.parse_status === "link_only" ? "bg-amber-50 text-amber-700" : "bg-blue-50 text-blue-700"}`}>{parseLabels[item.parse_status] || item.parse_status}</span>
                        </div>
                      </div>
                      <ReadLaterCover src={item.image_url} title={item.title || hostname(item.original_url)} />
                    </div>
                  </div>
                </article>
              ))}
            </div>
          </section>

          <article className={readerFullscreen ? "fixed inset-0 z-[140] min-w-0 bg-white" : "min-w-0 bg-white"}>
            {!detail ? <div className="flex h-full min-h-[420px] flex-col items-center justify-center text-gray-400"><BookOpen className="mb-3 h-8 w-8" /><p className="text-sm">选择一篇收藏开始阅读</p></div> : (
              <>
                <div className="sticky top-0 z-10 flex min-h-16 items-center gap-2 border-b border-black/[0.06] bg-white/95 px-5 backdrop-blur">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs text-gray-400">{detail.site_name || hostname(detail.original_url)}</p>
                    {readerFullscreen ? <p className="mt-0.5 truncate text-sm font-semibold text-gray-900">{detail.title}</p> : null}
                  </div>
                  <button onClick={() => setReaderFullscreen((current) => !current)} className="rounded-xl p-2 text-gray-500 hover:bg-black/[0.04] hover:text-[#002fa7]" title={readerFullscreen ? "退出全屏（Esc）" : "全屏阅读"}>
                    {readerFullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
                  </button>
                  <a href={detail.original_url} target="_blank" rel="noreferrer" className="rounded-xl p-2 text-gray-500 hover:bg-black/[0.04] hover:text-[#002fa7]" title="打开原文"><ExternalLink className="h-4 w-4" /></a>
                  <button onClick={() => void retryCapture(detail.id)} disabled={detail.parse_status === "queued" || detail.parse_status === "processing"} className="rounded-xl p-2 text-gray-500 hover:bg-black/[0.04] hover:text-[#002fa7] disabled:cursor-wait disabled:opacity-40" title="重新解析正文与图片"><RefreshCw className={`h-4 w-4 ${detail.parse_status === "queued" || detail.parse_status === "processing" ? "animate-spin" : ""}`} /></button>
                  <button onClick={() => void updateStatus(detail.id, detail.reading_status === "read" ? "unread" : "read")} className="rounded-xl p-2 text-gray-500 hover:bg-black/[0.04] hover:text-[#002fa7]" title={detail.reading_status === "read" ? "标为未读" : "标为已读"}><CheckCircle2 className="h-4 w-4" /></button>
                  <button onClick={() => void updateStatus(detail.id, "archived")} className="rounded-xl p-2 text-gray-500 hover:bg-black/[0.04] hover:text-[#002fa7]" title="归档"><Archive className="h-4 w-4" /></button>
                  <button onClick={() => setDeleteCandidate(detail)} className="rounded-xl p-2 text-gray-400 hover:bg-red-50 hover:text-red-600" title="删除收藏"><Trash2 className="h-4 w-4" /></button>
                </div>
                <div className={readerFullscreen ? "h-[calc(100vh-64px)] overflow-y-auto px-6 py-10" : "max-h-[calc(100vh-255px)] overflow-y-auto px-6 py-8 xl:px-10"}>
                  <div className={readerFullscreen ? "mx-auto max-w-4xl" : ""}>
                  <h1 className="text-2xl font-semibold leading-tight tracking-tight text-gray-950 xl:text-3xl">{detail.title || hostname(detail.original_url)}</h1>
                  <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-gray-400"><span>{detail.author || detail.site_name || hostname(detail.original_url)}</span><span>·</span><span>{relativeTime(detail.created_at)}</span></div>
                  {detail.parse_status === "ready" && detail.content ? (
                    <div className="markdown-content read-later-content mt-8 max-w-none">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          img: ({ src, alt }) => {
                            if (!src || /^https?:\/\//i.test(src)) {
                              return (
                                <span className="my-5 flex items-center gap-2 rounded-2xl bg-black/[0.025] px-4 py-3 text-xs text-gray-400">
                                  <ImageOff className="h-4 w-4" />远程图片未缓存，可点击右上角重新解析
                                </span>
                              );
                            }
                            // eslint-disable-next-line @next/next/no-img-element
                            return <img src={rawKnowledgeFileUrl(src)} alt={alt || "文章图片"} loading="lazy" />;
                          },
                        }}
                      >
                        {detail.content}
                      </ReactMarkdown>
                    </div>
                  ) : (
                    <div className="mt-8 rounded-2xl border border-amber-200 bg-amber-50 p-5">
                      <p className="font-medium text-amber-900">正文暂未提取</p>
                      <p className="mt-2 text-sm leading-6 text-amber-800">{detail.error_message || "后台正在解析；即使解析失败，原始链接也会保留。"}</p>
                      <a href={detail.original_url} target="_blank" rel="noreferrer" className="mt-4 inline-flex items-center gap-1.5 rounded-xl bg-white px-3 py-2 text-sm font-medium text-amber-900 shadow-sm">打开原文<ExternalLink className="h-3.5 w-3.5" /></a>
                      {detail.parse_status !== "queued" && detail.parse_status !== "processing" && <button onClick={() => void retryCapture(detail.id)} className="ml-2 mt-4 inline-flex items-center gap-1.5 rounded-xl bg-white px-3 py-2 text-sm font-medium text-amber-900 shadow-sm"><RefreshCw className="h-3.5 w-3.5" />重新解析</button>}
                    </div>
                  )}
                  </div>
                </div>
              </>
            )}
          </article>
        </div>
      </section>
        </div>
        </main>
      </div>

      {deleteCandidate ? (
        <div className="fixed inset-0 z-[180] flex items-center justify-center bg-slate-950/25 p-4 backdrop-blur-[2px]" onMouseDown={(event) => { if (event.currentTarget === event.target && !deleting) setDeleteCandidate(null); }}>
          <section role="alertdialog" aria-modal="true" aria-labelledby="delete-bookmark-title" className="w-full max-w-md overflow-hidden rounded-[26px] border border-white/70 bg-white shadow-2xl shadow-slate-900/20">
            <div className="px-6 py-6">
              <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-red-50 text-red-600"><Trash2 className="h-5 w-5" /></span>
              <h2 id="delete-bookmark-title" className="mt-4 text-lg font-semibold text-gray-950">删除这条收藏？</h2>
              <p className="mt-2 line-clamp-2 text-sm font-medium text-gray-700">{deleteCandidate.title || hostname(deleteCandidate.original_url)}</p>
              <p className="mt-3 text-xs leading-5 text-gray-500">
                将删除收藏记录、稍后读 Markdown 正文和本地图片。已经生成的 Raw、Wiki 页面、GBrain 数据和历史任务不会被删除。
              </p>
            </div>
            <div className="flex justify-end gap-2 border-t border-black/[0.06] bg-black/[0.012] px-6 py-4">
              <button type="button" onClick={() => setDeleteCandidate(null)} disabled={deleting} className="h-10 rounded-xl px-4 text-sm font-semibold text-gray-500 hover:bg-black/[0.04] disabled:opacity-40">取消</button>
              <button type="button" onClick={() => void deleteBookmark()} disabled={deleting} className="inline-flex h-10 items-center gap-2 rounded-xl bg-red-600 px-5 text-sm font-semibold text-white shadow-sm hover:bg-red-700 disabled:cursor-wait disabled:opacity-50">
                {deleting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                确认删除
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
