"use client";

import { useMemo, useState } from "react";
import {
  BookOpen,
  ChevronDown,
  ExternalLink,
  FileText,
  Search,
} from "lucide-react";
import { useApp, type SourceRecord } from "@/lib/store";

export default function SourcesPanel() {
  const { messages, isStreaming } = useApp();
  const { cited, retrieved } = useMemo(() => {
    const lastUserIndex = messages.findLastIndex((message) => message.role === "user");
    const turnMessages = lastUserIndex >= 0 ? messages.slice(lastUserIndex) : [];
    const sourceMap = new Map<string, SourceRecord>();
    const citationIndex = new Map<string, number>();
    const toolByCallId = new Map<string, string>();
    for (const message of turnMessages) {
      for (const toolCall of message.toolCalls || []) {
        if (toolCall.id) toolByCallId.set(toolCall.id, toolCall.tool);
      }
    }
    for (const message of turnMessages) {
      for (const source of message.sources || []) {
        if (isLegacyFalsePositive(source, toolByCallId)) continue;
        sourceMap.set(source.source_id, { ...sourceMap.get(source.source_id), ...source });
      }
      for (const citation of message.citations || []) {
        if (!citationIndex.has(citation.source_id)) {
          citationIndex.set(citation.source_id, citation.display_index);
        }
      }
    }
    const citedSources = Array.from(sourceMap.values())
      .filter((source) => citationIndex.has(source.source_id))
      .sort((a, b) => (citationIndex.get(a.source_id) || 0) - (citationIndex.get(b.source_id) || 0))
      .map((source) => ({ source, index: citationIndex.get(source.source_id) }));
    const retrievedSources = Array.from(sourceMap.values())
      .filter((source) => !citationIndex.has(source.source_id))
      .map((source) => ({ source, index: undefined }));
    return { cited: citedSources, retrieved: retrievedSources };
  }, [messages]);

  const total = cited.length + retrieved.length;

  return (
    <div className="h-full overflow-y-auto pr-2 py-2 pl-1 space-y-3">
      {total === 0 ? (
        <div className="flex h-full flex-col items-center justify-center px-5 text-center">
          <Search className="mb-3 h-8 w-8 text-slate-300" />
          <p className="text-[12px] font-medium text-slate-500">暂无引用来源</p>
          <p className="mt-1 text-[11px] leading-relaxed text-slate-400">
            Agent 通过知识库工具或 Skill 找到文档后，来源会在这里动态出现。
          </p>
        </div>
      ) : (
        <>
          <SourceCard
            title="已引用"
            icon={BookOpen}
            count={cited.length}
            defaultOpen
          >
            <div className="space-y-2">
              {cited.map(({ source, index }) => (
                <SourceItem key={source.source_id} source={source} citationIndex={index} />
              ))}
            </div>
          </SourceCard>

          <SourceCard
            title="其他检索结果"
            icon={FileText}
            count={retrieved.length}
            defaultOpen={false}
          >
            <div className="space-y-2">
              {retrieved.map(({ source }) => (
                <SourceItem key={source.source_id} source={source} />
              ))}
            </div>
          </SourceCard>

          {isStreaming && total > 0 && (
            <div className="flex items-center justify-center gap-1.5 py-1 text-[11px] text-blue-600">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
              检索中
            </div>
          )}
        </>
      )}
    </div>
  );
}

function SourceCard({
  title,
  icon: Icon,
  count,
  children,
  defaultOpen = true,
}: {
  title: string;
  icon: React.ElementType;
  count: number;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-3.5 py-2.5 hover:bg-slate-50/60 transition-colors"
      >
        <div className="flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center rounded-md bg-blue-50 text-[#002fa7]">
            <Icon className="h-3.5 w-3.5" />
          </div>
          <span className="text-[13px] font-semibold text-slate-800">{title}</span>
          <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
            {count}
          </span>
        </div>
        <ChevronDown
          className={`h-3.5 w-3.5 text-gray-400 transition-transform duration-200 ${!open ? "-rotate-90" : ""}`}
        />
      </button>
      {open && <div className="px-3.5 pb-3">{children}</div>}
    </section>
  );
}

function isLegacyFalsePositive(
  source: SourceRecord,
  toolByCallId: Map<string, string>
): boolean {
  const adapter = String(source.metadata?.adapter || "");
  if (adapter === "fetch_url" && looksLikeRejectedFetch(source.quote || "")) {
    return true;
  }
  if (!adapter || !["markdown_links", "common_json"].includes(adapter)) return false;
  const tool = toolByCallId.get(source.tool_call_id || "");
  return tool === "read_file" || tool === "write_file" || tool === "execute_skill";
}

function looksLikeRejectedFetch(quote: string): boolean {
  const text = quote.toLowerCase();
  const markers = [
    "please click here if you are not redirected",
    "trouble accessing google search",
    "enablejs",
    "网络不给力",
    "请稍后重试",
  ];
  if (markers.some((marker) => text.includes(marker))) return true;
  return ["ç½", "è¯", "å", "é¡", "ï¼"].filter((marker) => text.includes(marker)).length >= 2;
}

function SourceItem({
  source,
  citationIndex,
}: {
  source: SourceRecord;
  citationIndex?: number;
}) {
  const isExternal = /^https?:\/\//i.test(source.uri || "");
  const displayTitle = sourceDisplayTitle(source);
  const displayQuote = looksLikeRejectedFetch(source.quote || "") ? "" : source.quote;
  const locateCitation = () => {
    const marker = document.querySelector<HTMLElement>(`a[href="#source-${source.source_id}"]`);
    marker?.scrollIntoView({ behavior: "smooth", block: "center" });
    marker?.classList.add("citation-marker-active");
    window.setTimeout(() => marker?.classList.remove("citation-marker-active"), 1600);
  };

  return (
    <article
      id={`source-${source.source_id}`}
      className={`rounded-lg border p-2.5 transition-colors ${
        citationIndex ? "border-blue-200 bg-blue-50/40" : "border-slate-200 bg-slate-50/50"
      }`}
    >
      <button onClick={locateCitation} className="flex w-full items-start gap-2 text-left">
        <div
          className={`mt-0.5 flex h-5 min-w-5 items-center justify-center rounded text-[10px] font-semibold ${
            citationIndex ? "bg-[#002fa7] text-white" : "bg-slate-200 text-slate-500"
          }`}
        >
          {citationIndex || <FileText className="h-3 w-3" />}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[12px] font-medium text-slate-700" title={displayTitle}>
            {displayTitle}
          </p>
          <p className="mt-0.5 text-[10px] text-slate-400">
            {source.page ? `第 ${source.page} 页 · ` : ""}
            {sourceTypeLabel(source.source_type)}
            {typeof source.score === "number" ? ` · ${Math.round(source.score * 100)}%` : ""}
          </p>
        </div>
      </button>

      {displayQuote && (
        <blockquote className="mt-2 line-clamp-3 border-l-2 border-slate-200 pl-2 text-[10px] leading-relaxed text-slate-500">
          {displayQuote}
        </blockquote>
      )}

      {isExternal && (
        <a
          href={source.uri}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-2 inline-flex items-center gap-1 text-[10px] font-medium text-blue-600 hover:text-blue-800"
        >
          <ExternalLink className="h-3 w-3" />
          打开原文
        </a>
      )}
    </article>
  );
}

function sourceDisplayTitle(source: SourceRecord): string {
  const title = (source.title || "").trim();
  if (title && !title.startsWith("[]()") && !title.startsWith("[ ](")) return title;
  try {
    return source.uri ? new URL(source.uri).hostname : "未命名来源";
  } catch {
    return title || "未命名来源";
  }
}

function sourceTypeLabel(type: string): string {
  if (type === "web") return "网页";
  if (type === "skill") return "Skill";
  if (type === "file") return "文件";
  if (type === "knowledge_base") return "知识库";
  return type || "来源";
}
