"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen,
  CheckCircle2,
  ChevronDown,
  Circle,
  ExternalLink,
  FileText,
  ListChecks,
  Timer,
} from "lucide-react";
import { useApp, type SourceRecord, type ToolCall } from "@/lib/store";

type TodoStatus = "completed" | "in_progress" | "pending";
interface TodoItem {
  content: string;
  status: TodoStatus;
}

export default function SourcesPanel() {
  const { messages, isStreaming } = useApp();
  const { cited, retrieved, todos } = useMemo(() => {
    const lastUserIndex = messages.findLastIndex((message) => message.role === "user");
    const turnMessages = lastUserIndex >= 0 ? messages.slice(lastUserIndex) : [];
    const sourceMap = new Map<string, SourceRecord>();
    const citationIndex = new Map<string, number>();
    const toolByCallId = new Map<string, string>();
    let latestTodos: TodoItem[] = [];
    for (const message of turnMessages) {
      for (const toolCall of message.toolCalls || []) {
        if (toolCall.id) toolByCallId.set(toolCall.id, toolCall.tool);
        const parsedTodos = extractTodosFromToolCall(toolCall);
        if (parsedTodos.length > 0) latestTodos = parsedTodos;
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
    return { cited: citedSources, retrieved: retrievedSources, todos: latestTodos };
  }, [messages]);

  const total = cited.length + retrieved.length;

  return (
    <div className="h-full overflow-y-auto px-5 py-7 space-y-6">
      <ProgressCard todos={todos} />

      {isStreaming && total > 0 && (
        <div className="flex items-center justify-center gap-1.5 text-[11px] text-blue-600">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
          检索中
        </div>
      )}

      {total > 0 && <SourcesCard cited={cited} retrieved={retrieved} />}
    </div>
  );
}

function ProgressCard({ todos }: { todos: TodoItem[] }) {
  const [open, setOpen] = useState(true);
  const completed = todos.filter((todo) => todo.status === "completed").length;
  const hasTodos = todos.length > 0;

  // Auto-expand when todos are detected so the user doesn't have to open the drawer manually.
  useEffect(() => {
    if (hasTodos) {
      setOpen(true);
    }
  }, [hasTodos]);

  return (
    <section className="workspace-side-card rounded-[28px] px-5 py-5">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between text-left"
      >
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-xl bg-black/[0.055] text-slate-800">
            <ListChecks className="h-4 w-4" />
          </div>
          <span className="text-[15px] font-bold text-slate-900">进度</span>
          {hasTodos && (
            <span className="rounded-full bg-black/[0.045] px-2 py-0.5 text-[11px] font-medium text-slate-500">
              {completed}/{todos.length}
            </span>
          )}
        </div>
        <ChevronDown
          className={`h-4 w-4 text-slate-500 transition-transform duration-200 ${!open ? "-rotate-90" : ""}`}
        />
      </button>

      {open && (
        <div className="mt-3 space-y-2.5">
          {hasTodos ? (
            <>
              {todos.map((todo, index) => (
                <div key={`${todo.content}-${index}`} className="flex items-start gap-2.5">
                  <TodoStatusIcon status={todo.status} />
                  <p
                    className={`min-w-0 flex-1 text-[13px] leading-relaxed ${
                      todo.status === "completed"
                        ? "text-slate-500 line-through decoration-slate-500 decoration-[1.5px]"
                        : todo.status === "in_progress"
                        ? "text-slate-900 font-medium"
                        : "text-slate-600"
                    }`}
                  >
                    {todo.content}
                  </p>
                </div>
              ))}
            </>
          ) : (
            <ProgressEmptyState />
          )}
        </div>
      )}
    </section>
  );
}

function ProgressEmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center">
      <div className="relative mb-4 h-20 w-44 opacity-80">
        <div className="absolute left-7 top-2 h-10 w-32 rounded-full border border-black/[0.08] bg-white" />
        <div className="absolute left-12 top-5 h-3 w-20 rounded-full bg-slate-100" />
        <div className="absolute left-10 top-5 h-3 w-3 rounded-full bg-slate-100" />
        <div className="absolute right-5 top-0 flex h-7 w-7 items-center justify-center rounded-full border border-black/[0.08] bg-white text-slate-300">
          <CheckCircle2 className="h-4 w-4" />
        </div>
        <div className="absolute bottom-1 left-1 h-10 w-36 rounded-full border border-black/[0.08] bg-white" />
        <div className="absolute bottom-4 left-14 h-3 w-20 rounded-full bg-slate-100" />
        <div className="absolute bottom-4 left-8 h-3 w-3 rounded-full bg-slate-100" />
      </div>
      <p className="text-[14px] font-medium text-slate-400">任务进度将显示在这里</p>
    </div>
  );
}

function TodoStatusIcon({ status }: { status: TodoStatus }) {
  if (status === "completed") {
    return <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 fill-slate-900 text-white" />;
  }
  if (status === "in_progress") {
    return <Timer className="mt-0.5 h-5 w-5 shrink-0 text-[#002fa7]" />;
  }
  return <Circle className="mt-0.5 h-5 w-5 shrink-0 text-slate-300" />;
}

function SourcesCard({
  cited,
  retrieved,
}: {
  cited: Array<{ source: SourceRecord; index?: number }>;
  retrieved: Array<{ source: SourceRecord; index?: number }>;
}) {
  const [open, setOpen] = useState(true);
  const { activeSourceId } = useApp();
  const activeRef = useRef<HTMLDivElement>(null);
  const total = cited.length + retrieved.length;

  useEffect(() => {
    if (!activeSourceId) return;
    const allSources = [...cited, ...retrieved];
    if (allSources.some(({ source }) => source.source_id === activeSourceId)) {
      setOpen(true);
      window.setTimeout(() => {
        activeRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 50);
    }
  }, [activeSourceId, cited, retrieved]);

  return (
    <section className="workspace-side-card rounded-[28px] px-5 py-5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-left"
      >
        <div className="flex items-center gap-2">
          <div className="flex h-6 w-6 items-center justify-center rounded-lg bg-[#002fa7]/[0.07] text-[#002fa7]">
            <BookOpen className="h-3.5 w-3.5" />
          </div>
          <span className="text-[15px] font-bold text-slate-900">来源</span>
          {total > 0 && (
            <span className="rounded-full bg-black/[0.045] px-2 py-0.5 text-[11px] font-medium text-slate-500">
              {total}
            </span>
          )}
        </div>
        <ChevronDown
          className={`h-4 w-4 text-slate-500 transition-transform duration-200 ${!open ? "-rotate-90" : ""}`}
        />
      </button>

      {open && (
        <div className="mt-4 space-y-5">
          {cited.length > 0 && (
            <SourceSection title="已引用" count={cited.length}>
              {cited.map(({ source, index }) => (
                <SourceItem
                  key={source.source_id}
                  source={source}
                  citationIndex={index}
                  isActive={activeSourceId === source.source_id}
                  ref={activeSourceId === source.source_id ? activeRef : undefined}
                />
              ))}
            </SourceSection>
          )}

          {retrieved.length > 0 && (
            <SourceSection title="其他检索结果" count={retrieved.length}>
              {retrieved.map(({ source }) => (
                <SourceItem
                  key={source.source_id}
                  source={source}
                  isActive={activeSourceId === source.source_id}
                  ref={activeSourceId === source.source_id ? activeRef : undefined}
                />
              ))}
            </SourceSection>
          )}
        </div>
      )}
    </section>
  );
}

function SourceSection({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-2.5 flex items-center gap-2">
        <span className="text-[12px] font-semibold text-slate-700">{title}</span>
        <span className="rounded-full bg-black/[0.045] px-1.5 py-0.5 text-[10px] text-slate-500">
          {count}
        </span>
      </div>
      <div className="space-y-3">{children}</div>
    </div>
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

function extractTodosFromToolCall(toolCall: ToolCall): TodoItem[] {
  if (toolCall.tool !== "write_todos") return [];
  const candidates = [
    parseMaybeJson(toolCall.input),
    parseMaybeJson(toolCall.output),
  ];
  for (const candidate of candidates) {
    const todos = normalizeTodos(candidate);
    if (todos.length > 0) return todos;
  }
  return [];
}

function parseMaybeJson(value: unknown): unknown {
  if (!value || typeof value !== "string") return null;
  const text = value.trim();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    const objectStart = text.indexOf("{");
    const arrayStart = text.indexOf("[");
    const starts = [objectStart, arrayStart].filter((index) => index >= 0);
    if (starts.length === 0) return null;
    try {
      return JSON.parse(text.slice(Math.min(...starts)));
    } catch {
      return null;
    }
  }
}

function normalizeTodos(value: unknown): TodoItem[] {
  const rawItems = Array.isArray(value)
    ? value
    : isRecord(value)
    ? value.todos || value.tasks || value.items || value.todo
    : null;
  if (!Array.isArray(rawItems)) return [];

  return rawItems
    .map((item): TodoItem | null => {
      if (typeof item === "string") {
        const content = item.trim();
        return content ? { content, status: "pending" } : null;
      }
      if (!isRecord(item)) return null;
      const content = String(
        item.content || item.todo || item.task || item.title || item.text || ""
      ).trim();
      if (!content) return null;
      return {
        content,
        status: normalizeTodoStatus(item.status || item.state || item.done),
      };
    })
    .filter((item): item is TodoItem => item !== null);
}

function normalizeTodoStatus(value: unknown): TodoStatus {
  if (value === true) return "completed";
  const status = String(value || "").toLowerCase().replace(/[-\s]/g, "_");
  if (["completed", "complete", "done", "checked", "finished"].includes(status)) {
    return "completed";
  }
  if (["in_progress", "inprogress", "active", "doing", "running"].includes(status)) {
    return "in_progress";
  }
  return "pending";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
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

const SourceItem = React.forwardRef<HTMLDivElement, {
  source: SourceRecord;
  citationIndex?: number;
  isActive?: boolean;
}>(function SourceItem({ source, citationIndex, isActive }, ref) {
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
      ref={ref}
      id={`source-${source.source_id}`}
      className={`rounded-2xl border border-black/[0.10] bg-white p-3 transition-colors hover:border-black/[0.16] ${
        citationIndex ? "border-[#002fa7]/20 bg-[#f8faff]" : ""
      } ${isActive ? "ring-2 ring-[#002fa7]/40 shadow-sm" : ""}`}
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
        <blockquote className="mt-2 line-clamp-3 border-l-2 border-[#002fa7]/10 pl-2 text-[10px] leading-relaxed text-slate-500">
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
});

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
