"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, FileText, Image as ImageIcon, Loader2, Search, Settings } from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import KnowledgeWorkspaceHeader from "@/components/knowledge/KnowledgeWorkspaceHeader";
import KnowledgeWorkspaceNav from "@/components/knowledge/KnowledgeWorkspaceNav";
import { rawKnowledgeFileUrl, searchKnowledgePortal, type KnowledgeSearchCategory, type KnowledgeSearchHit, type KnowledgeSearchResult } from "@/lib/api";
import { useApp } from "@/lib/store";

const tabs: Array<{ key: KnowledgeSearchCategory; label: string }> = [
  { key: "all", label: "全部" },
  { key: "wiki", label: "Wiki" },
  { key: "article", label: "文章" },
  { key: "image", label: "图片" },
  { key: "file", label: "文件" },
];

function resultTypeLabel(hit: KnowledgeSearchHit): string {
  if (hit.result_type === "wiki") return "LLM Wiki";
  if (hit.result_type === "image") return "图片";
  if (hit.result_type === "file") return "文件";
  return "文章";
}

function matchedByLabel(value: string): string {
  return ({
    title: "标题",
    path: "路径",
    content: "正文",
    image_context: "图片上下文",
    text_vector: "文本语义",
    image_vector: "图片语义",
    bm25: "关键词",
  } as Record<string, string>)[value] || value;
}

function ResultItem({ hit }: { hit: KnowledgeSearchHit }) {
  const uri = String(hit.uri || hit.source?.uri || "");
  const isImage = hit.result_type === "image";
  return (
    <article className="group border-b border-black/[0.06] px-5 py-5 last:border-b-0 sm:px-6">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[#002fa7]/[0.07] text-[#002fa7]">
          {isImage ? <ImageIcon className="h-4 w-4" /> : <FileText className="h-4 w-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-gray-400">
            <span className="font-medium text-[#002fa7]">{resultTypeLabel(hit)}</span>
            <span>›</span>
            <span className="truncate">{hit.display_path || uri.replace("/knowledge/", "")}</span>
          </div>
          <a href={rawKnowledgeFileUrl(uri)} target="_blank" rel="noreferrer" className="mt-1 block text-base font-semibold leading-6 text-gray-950 transition group-hover:text-[#002fa7]">
            {hit.title}
            <ArrowUpRight className="ml-1 inline h-3.5 w-3.5 opacity-0 transition group-hover:opacity-100" />
          </a>
          {isImage ? (
            <div className="mt-3 flex gap-3">
              <img src={rawKnowledgeFileUrl(uri)} alt={hit.title} className="h-20 w-28 rounded-xl border border-black/[0.08] object-cover" />
              <p className="text-sm leading-6 text-gray-600">{hit.snippet || hit.quote || "命中图片上下文"}</p>
            </div>
          ) : (
            <p className="mt-1.5 max-w-3xl text-sm leading-6 text-gray-600">{hit.snippet || hit.quote || "命中文档"}</p>
          )}
          <div className="mt-2 flex flex-wrap gap-1.5 text-[10px] text-gray-400">
            {(hit.matched_by || []).map((value) => <span key={value} className="rounded-lg bg-black/[0.035] px-2 py-1">命中 {matchedByLabel(value)}</span>)}
            {hit.source_group?.versions?.length ? <span className="rounded-lg bg-black/[0.035] px-2 py-1">同源版本 {hit.source_group.versions.length}</span> : null}
          </div>
        </div>
      </div>
    </article>
  );
}

export default function KnowledgeSearchPage() {
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [query, setQuery] = useState("");
  const [input, setInput] = useState("");
  const [category, setCategory] = useState<KnowledgeSearchCategory>("all");
  const [result, setResult] = useState<KnowledgeSearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [mounted, setMounted] = useState(false);

  const handleSidebarResize = useCallback((delta: number) => {
    setSidebarWidth((value) => Math.max(200, value + delta));
  }, [setSidebarWidth]);

  async function runSearch(nextQuery = input) {
    const value = nextQuery.trim();
    if (!value) return;
    setLoading(true);
    setError("");
    setQuery(value);
    try {
      // Categories are local views over one fused/reranked candidate pool.
      // Fetch once so tab switches never regenerate embeddings or query Milvus.
      setResult(await searchKnowledgePortal({ query: value, category: "all", limit: 50 }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "搜索失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    setMounted(true);
    const initial = new URLSearchParams(window.location.search).get("q") || "";
    setInput(initial);
    if (initial) void runSearch(initial);
    // The URL is the input contract for this page; it should run once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = input.trim();
    if (value) {
      window.history.replaceState(null, "", `/knowledge/search?q=${encodeURIComponent(value)}`);
      void runSearch(value);
    }
  }

  function chooseCategory(next: KnowledgeSearchCategory) {
    setCategory(next);
  }

  const visibleHits = (result?.hits || []).filter((hit) => {
    if (category === "all") return true;
    return hit.result_type === category;
  }).slice(0, 20);
  const visibleTotal = category === "all"
    ? (result?.total ?? 0)
    : (result?.facets?.categories?.[category] ?? visibleHits.length);

  return (
    <div className="h-screen app-bg text-gray-900">
      <div className="fixed left-3 top-3 z-[80]"><Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact /></div>
      <div className="flex h-full overflow-hidden">
        <div className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden" style={{ width: sidebarOpen ? sidebarWidth : 0 }}>
          <div style={{ width: sidebarWidth, minWidth: 200 }} className="flex h-full flex-col"><div className="h-11 shrink-0" /><div className="min-h-0 flex-1 overflow-hidden"><Sidebar /></div></div>
        </div>
        {mounted && sidebarOpen ? <ResizeHandle onResize={handleSidebarResize} direction="left" /> : null}
        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto">
            <div className="workspace-page-container flex flex-col gap-5">
              <KnowledgeWorkspaceHeader
                section="search"
                actions={
                  <Link href="/knowledge/settings/search" className="inline-flex h-9 items-center gap-2 rounded-xl border border-black/[0.07] bg-white px-3.5 text-xs font-semibold text-gray-600 shadow-sm transition hover:text-[#002fa7]">
                    <Settings className="h-3.5 w-3.5" />搜索设置
                  </Link>
                }
              />
              <KnowledgeWorkspaceNav />

              <section className="rounded-2xl border border-black/[0.06] bg-white p-4 shadow-sm sm:p-5">
                <form onSubmit={submit} className="flex items-center gap-2 rounded-2xl border border-black/[0.08] bg-black/[0.015] p-1.5 transition focus-within:border-[#002fa7]/35 focus-within:bg-white focus-within:ring-4 focus-within:ring-[#002fa7]/[0.07]">
                  <Search className="ml-3 h-5 w-5 shrink-0 text-[#002fa7]" />
                  <input
                    value={input}
                    onChange={(event) => setInput(event.target.value)}
                    className="h-11 min-w-0 flex-1 bg-transparent px-2 text-sm text-gray-900 outline-none placeholder:text-gray-400"
                    placeholder="搜索文章、Wiki、文件和图片……"
                    aria-label="搜索知识库"
                  />
                  <button type="submit" disabled={!input.trim() || loading} className="inline-flex h-10 shrink-0 items-center gap-2 rounded-xl bg-[#002fa7] px-5 text-xs font-semibold text-white transition hover:bg-[#00227d] disabled:cursor-not-allowed disabled:opacity-40">
                    {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                    搜索
                  </button>
                </form>

                <div className="mt-4 flex items-center gap-1 overflow-x-auto rounded-2xl bg-black/[0.03] p-1">
                  {tabs.map((tab) => {
                    const count = tab.key === "all"
                      ? (result?.total ?? 0)
                      : (result?.facets?.categories?.[tab.key] ?? 0);
                    const active = category === tab.key;
                    return (
                      <button
                        key={tab.key}
                        type="button"
                        onClick={() => chooseCategory(tab.key)}
                        className={`inline-flex h-9 shrink-0 items-center gap-1.5 rounded-xl px-4 text-xs font-semibold transition ${active ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500 hover:bg-white/70 hover:text-gray-900"}`}
                      >
                        {tab.label}
                        {count > 0 ? <span className={active ? "text-[#002fa7]/55" : "text-gray-400"}>{count}</span> : null}
                      </button>
                    );
                  })}
                </div>
              </section>

              <section className="overflow-hidden rounded-2xl border border-black/[0.06] bg-white shadow-sm">
                <header className="flex min-h-14 items-center justify-between gap-4 border-b border-black/[0.06] px-5 py-3 text-xs text-gray-400 sm:px-6">
                  <span>{query ? `${visibleTotal} 个结果` : "输入内容开始搜索"}</span>
                  <span className="flex items-center gap-2">
                    {result ? <span className={`rounded-full px-2 py-1 text-[10px] font-medium ${result.retrieval?.hybrid_enabled ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>{result.retrieval?.hybrid_enabled ? "语义 + 关键词" : "仅关键词"}</span> : null}
                    {result?.took_ms != null ? <span>{result.took_ms} ms</span> : null}
                  </span>
                </header>

                {loading ? (
                  <div className="flex min-h-56 items-center justify-center text-sm text-gray-400"><Loader2 className="mr-2 h-4 w-4 animate-spin" />正在搜索知识库…</div>
                ) : error ? (
                  <div className="m-5 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 sm:m-6">{error}</div>
                ) : visibleHits.length ? (
                  <div>{visibleHits.map((hit) => <ResultItem key={hit.id || `${hit.uri}-${hit.rank}`} hit={hit} />)}</div>
                ) : (
                  <div className="flex min-h-56 flex-col items-center justify-center px-6 text-center">
                    <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-[#002fa7]/[0.06] text-[#002fa7]"><Search className="h-5 w-5" /></span>
                    <p className="mt-3 text-sm font-medium text-gray-600">{query ? "没有找到匹配内容" : "搜索本地知识库"}</p>
                    <p className="mt-1 text-xs text-gray-400">{query ? "换一个关键词或内容类型再试试。" : "输入关键词，查找文章、Wiki、文件和图片。"}</p>
                  </div>
                )}
              </section>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
