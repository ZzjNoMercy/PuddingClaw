"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Bot,
  CornerDownLeft,
  Loader2,
  MessageSquare,
  Search,
  X,
} from "lucide-react";
import {
  searchSessions,
  type SessionSearchResult,
} from "@/lib/api";

interface SessionSearchDialogProps {
  open: boolean;
  projectNames: Map<string, string>;
  onClose: () => void;
  onSelect: (session: SessionSearchResult) => void;
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const index = normalizedQuery
    ? text.toLocaleLowerCase().indexOf(normalizedQuery)
    : -1;
  if (index < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, index)}
      <mark className="rounded-sm bg-[#002fa7]/10 px-0.5 text-inherit">
        {text.slice(index, index + normalizedQuery.length)}
      </mark>
      {text.slice(index + normalizedQuery.length)}
    </>
  );
}

function formatUpdatedAt(timestamp: number): string {
  const value = timestamp > 1_000_000_000_000 ? timestamp : timestamp * 1000;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
  }).format(new Date(value));
}

export default function SessionSearchDialog({
  open,
  projectNames,
  onClose,
  onSelect,
}: SessionSearchDialogProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const resultRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SessionSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setResults([]);
    setFailed(false);
    setActiveIndex(0);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const normalized = query.trim();
    if (!normalized) {
      setResults([]);
      setLoading(false);
      setFailed(false);
      return;
    }

    setResults([]);
    setActiveIndex(0);
    setLoading(true);
    setFailed(false);
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void searchSessions(normalized, controller.signal)
        .then((items) => {
          setResults(items);
          setActiveIndex(0);
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === "AbortError") return;
          setResults([]);
          setFailed(true);
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 180);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [open, query]);

  useEffect(() => {
    resultRefs.current[activeIndex]?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  const resultCountLabel = useMemo(
    () => query.trim() && !loading && !failed ? `${results.length} 个结果` : "",
    [failed, loading, query, results.length],
  );

  if (!open || typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[10000] flex items-start justify-center bg-slate-950/25 px-4 pt-[11vh] backdrop-blur-[2px]"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label="搜索对话"
        className="flex max-h-[72vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-black/[0.08] bg-white shadow-2xl shadow-slate-950/20"
      >
        <div className="flex items-center gap-3 border-b border-black/[0.06] px-4">
          <Search className="h-5 w-5 shrink-0 text-gray-400" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" && results.length) {
                event.preventDefault();
                setActiveIndex((index) => (index + 1) % results.length);
              } else if (event.key === "ArrowUp" && results.length) {
                event.preventDefault();
                setActiveIndex((index) => (index - 1 + results.length) % results.length);
              } else if (event.key === "Enter" && results[activeIndex]) {
                event.preventDefault();
                onSelect(results[activeIndex]);
              }
            }}
            placeholder="搜索对话标题或内容"
            className="h-14 min-w-0 flex-1 bg-transparent text-[15px] text-gray-900 outline-none placeholder:text-gray-400"
          />
          {loading ? <Loader2 className="h-4 w-4 animate-spin text-[#002fa7]" /> : null}
          {query ? (
            <button
              type="button"
              onClick={() => setQuery("")}
              className="rounded-lg p-1.5 text-gray-400 transition hover:bg-black/[0.05] hover:text-gray-700"
              aria-label="清空搜索"
            >
              <X className="h-4 w-4" />
            </button>
          ) : (
            <kbd className="rounded-md border border-black/[0.08] bg-black/[0.03] px-1.5 py-0.5 text-[10px] text-gray-400">
              ESC
            </kbd>
          )}
        </div>

        <div className="min-h-[260px] overflow-y-auto p-2">
          {!query.trim() ? (
            <div className="flex h-[244px] flex-col items-center justify-center text-center">
              <Search className="mb-3 h-7 w-7 text-gray-300" />
              <p className="text-[13px] font-medium text-gray-600">搜索对话</p>
              <p className="mt-1 text-[12px] text-gray-400">输入关键词匹配 session 标题或会话内容</p>
            </div>
          ) : failed ? (
            <div className="flex h-[244px] flex-col items-center justify-center text-center">
              <p className="text-[13px] font-medium text-gray-600">搜索失败</p>
              <p className="mt-1 text-[12px] text-gray-400">请确认服务已连接后重试</p>
            </div>
          ) : !loading && results.length === 0 ? (
            <div className="flex h-[244px] flex-col items-center justify-center text-center">
              <MessageSquare className="mb-3 h-7 w-7 text-gray-300" />
              <p className="text-[13px] font-medium text-gray-600">没有匹配的对话</p>
              <p className="mt-1 text-[12px] text-gray-400">试试其他关键词</p>
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between px-2 pb-1.5 pt-1">
                <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-400">对话</p>
                <p className="text-[10px] text-gray-400">{resultCountLabel}</p>
              </div>
              <div role="listbox" aria-label="对话搜索结果" className="space-y-0.5">
                {results.map((session, index) => {
                  const projectName = session.project_id
                    ? projectNames.get(session.project_id)
                    : null;
                  return (
                    <button
                      key={session.id}
                      ref={(element) => {
                        resultRefs.current[index] = element;
                      }}
                      type="button"
                      role="option"
                      aria-selected={index === activeIndex}
                      onMouseEnter={() => setActiveIndex(index)}
                      onClick={() => onSelect(session)}
                      className={`group flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition ${
                        index === activeIndex ? "bg-[#002fa7]/[0.07]" : "hover:bg-black/[0.035]"
                      }`}
                    >
                      <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${
                        index === activeIndex ? "bg-[#002fa7] text-white" : "bg-black/[0.04] text-gray-500"
                      }`}>
                        {session.runtime_mode === "agent"
                          ? <Bot className="h-3.5 w-3.5" />
                          : <MessageSquare className="h-3.5 w-3.5" />}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className="truncate text-[13px] font-medium text-gray-800">
                            <HighlightedText text={session.title} query={query} />
                          </span>
                          {session.matched_in === "title" ? (
                            <span className="shrink-0 rounded bg-[#002fa7]/[0.07] px-1.5 py-0.5 text-[9px] font-medium text-[#002fa7]">
                              标题匹配
                            </span>
                          ) : null}
                        </span>
                        {session.snippet ? (
                          <span className="mt-0.5 block truncate text-[12px] leading-5 text-gray-400">
                            <HighlightedText text={session.snippet} query={query} />
                          </span>
                        ) : null}
                      </span>
                      <span className="flex shrink-0 items-center gap-2 pt-0.5 text-[10px] text-gray-400">
                        <span>{projectName || (session.runtime_mode === "agent" ? "Agent" : "Legacy Chat（已停用）")}</span>
                        <span>{formatUpdatedAt(session.updated_at)}</span>
                        {index === activeIndex ? <CornerDownLeft className="h-3 w-3 text-[#002fa7]" /> : null}
                      </span>
                    </button>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </section>
    </div>,
    document.body,
  );
}
