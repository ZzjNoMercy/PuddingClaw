"use client";

import { useMemo } from "react";
import {
  BookOpen,
  ExternalLink,
  FileText,
  Search,
} from "lucide-react";
import { useApp, type SourceRecord } from "@/lib/store";

export default function SourcesPanel() {
  const { messages, isStreaming } = useApp();
  const { cited, retrieved } = useMemo(() => {
    const sourceMap = new Map<string, SourceRecord>();
    const citationIndex = new Map<string, number>();
    const toolByCallId = new Map<string, string>();
    for (const message of messages) {
      for (const toolCall of message.toolCalls || []) {
        if (toolCall.id) toolByCallId.set(toolCall.id, toolCall.tool);
      }
    }
    for (const message of messages) {
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
    <aside className="flex h-full flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex h-11 shrink-0 items-center gap-2 border-b border-slate-100 px-3.5">
        <div className="flex h-6 w-6 items-center justify-center rounded-md bg-blue-50">
          <BookOpen className="h-3.5 w-3.5 text-[#002fa7]" />
        </div>
        <span className="text-[13px] font-semibold text-slate-800">引用来源</span>
        <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
          {total}
        </span>
        {isStreaming && total > 0 && (
          <span className="ml-auto inline-flex items-center gap-1 text-[10px] text-blue-600">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
            检索中
          </span>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-3">
        {total === 0 ? (
          <div className="flex h-full flex-col items-center justify-center px-5 text-center">
            <Search className="mb-3 h-8 w-8 text-slate-300" />
            <p className="text-[12px] font-medium text-slate-500">暂无引用来源</p>
            <p className="mt-1 text-[11px] leading-relaxed text-slate-400">
              Agent 通过知识库工具或 Skill 找到文档后，来源会在这里动态出现。
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {cited.length > 0 && (
              <SourceGroup title="已引用" count={cited.length}>
                {cited.map(({ source, index }) => (
                  <SourceCard key={source.source_id} source={source} citationIndex={index} />
                ))}
              </SourceGroup>
            )}
            {retrieved.length > 0 && (
              <SourceGroup title={cited.length ? "其他检索结果" : "已检索"} count={retrieved.length} muted>
                {retrieved.map(({ source }) => (
                  <SourceCard key={source.source_id} source={source} />
                ))}
              </SourceGroup>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

function isLegacyFalsePositive(
  source: SourceRecord,
  toolByCallId: Map<string, string>
): boolean {
  const adapter = String(source.metadata?.adapter || "");
  if (!adapter || !["markdown_links", "common_json"].includes(adapter)) return false;
  const tool = toolByCallId.get(source.tool_call_id || "");
  return tool === "read_file" || tool === "write_file" || tool === "execute_skill";
}

function SourceGroup({
  title,
  count,
  muted = false,
  children,
}: {
  title: string;
  count: number;
  muted?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className={`mb-2 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider ${muted ? "text-slate-400" : "text-[#002fa7]"}`}>
        <span>{title}</span>
        <span>{count}</span>
      </div>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

function SourceCard({ source, citationIndex }: { source: SourceRecord; citationIndex?: number }) {
  const isExternal = /^https?:\/\//i.test(source.uri || "");
  const locateCitation = () => {
    const marker = document.querySelector<HTMLElement>(`a[href="#source-${source.source_id}"]`);
    marker?.scrollIntoView({ behavior: "smooth", block: "center" });
    marker?.classList.add("citation-marker-active");
    window.setTimeout(() => marker?.classList.remove("citation-marker-active"), 1600);
  };

  return (
    <article
      id={`source-${source.source_id}`}
      className={`rounded-lg border p-2.5 transition-colors ${citationIndex ? "border-blue-200 bg-blue-50/40" : "border-slate-200 bg-slate-50/50"}`}
    >
      <button onClick={locateCitation} className="flex w-full items-start gap-2 text-left">
        <div className={`mt-0.5 flex h-5 min-w-5 items-center justify-center rounded text-[10px] font-semibold ${citationIndex ? "bg-[#002fa7] text-white" : "bg-slate-200 text-slate-500"}`}>
          {citationIndex || <FileText className="h-3 w-3" />}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[12px] font-medium text-slate-700" title={source.title}>
            {source.title}
          </p>
          <p className="mt-0.5 text-[10px] text-slate-400">
            {source.page ? `第 ${source.page} 页 · ` : ""}
            {sourceTypeLabel(source.source_type)}
            {typeof source.score === "number" ? ` · ${Math.round(source.score * 100)}%` : ""}
          </p>
        </div>
      </button>

      {source.quote && (
        <blockquote className="mt-2 line-clamp-4 border-l-2 border-slate-200 pl-2 text-[10px] leading-relaxed text-slate-500">
          {source.quote}
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

function sourceTypeLabel(type: string): string {
  if (type === "web") return "网页";
  if (type === "skill") return "Skill";
  if (type === "file") return "文件";
  if (type === "knowledge_base") return "知识库";
  return type || "来源";
}
