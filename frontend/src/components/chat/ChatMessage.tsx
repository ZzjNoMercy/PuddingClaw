"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, FileText, Key, KeyRound, Sparkles } from "lucide-react";
import { denyPermissionRequest, grantExternalFileRead, type PermissionRequest } from "@/lib/api";
import { markdownRemarkPlugins } from "@/lib/markdown";
import { useApp, type ChatMessage as ChatMessageType, type SourceRecord, type TimelineItem } from "@/lib/store";
import ThoughtChain from "./ThoughtChain";
import RetrievalCard from "./RetrievalCard";

interface Props {
  message: ChatMessageType;
  isStreaming?: boolean;
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

/** Detect 401 / API key errors without matching arbitrary numbers like patent IDs. */
function isAuthError(content: string): boolean {
  const lower = content.toLowerCase();
  // Specific HTTP 401 contexts (avoid matching a bare "401" in patent numbers / dates)
  const has401 = /\b401\s*(unauthorized| unauthorised|禁止|认证失败|未授权)\b/i.test(content) ||
    /\b(http\s*401|status\s*401|error\s*401|code\s*401|返回\s*401)\b/i.test(content);
  const hasApiKeyError = /invalid.*api\s*key|api\s*key.*invalid|api\s*key.*missing|api\s*key.*not\s*set|apikey.*invalid/i.test(lower);
  const hasAuthFail = /authentication\s*(fail|error|failed)|认证失败|鉴权失败|未通过认证|授权失败/i.test(content);
  return has401 || hasApiKeyError || hasAuthFail;
}

export default function ChatMessage({ message, isStreaming = false }: Props) {
  const isUser = message.role === "user";
  const hasAuthError = !isUser && isAuthError(message.content);
  const renderedContent = renderCitationMarkers(message);
  const { sessionId, setActiveSourceId, setInspectorOpen } = useApp();
  const pendingPermissionRequests = (message.permissionRequests || []).filter(
    (request) => request.status !== "resolved"
  );

  const citationComponents: Components = {
    a: (props) => (
      <CitationLink
        {...props}
        sources={message.sources}
        onActivate={(sourceId) => {
          setActiveSourceId(sourceId);
          setInspectorOpen(true);
        }}
      />
    ),
  };

  return (
    <div className="animate-fade-in px-7 py-3">
      <div className="mx-auto w-full max-w-[900px]">
        {/* User message — right-aligned bubble */}
        {isUser ? (
          <div className="flex justify-end">
            <div>
              <div className="max-w-xl rounded-2xl rounded-tr-md bg-[#002fa7] px-4 py-2.5 text-[14px] leading-relaxed text-white shadow-sm shadow-blue-950/10">
                {message.content}
              </div>
              <div className="text-[10px] text-gray-400 mt-1 text-right pr-1">
                {formatTime(message.timestamp)}
              </div>
            </div>
          </div>
        ) : (
          /* Assistant message — left-aligned */
          <div>
            <div className="min-w-0">
              {message.segments && message.segments.length > 0 ? (
                /* Multi-segment agent turn: each model invocation is its own block */
                <div className="space-y-4">
                  {pendingPermissionRequests.map((request) => (
                    <ExternalFilePermissionCard
                      key={request.id}
                      request={request}
                      sessionId={sessionId}
                    />
                  ))}
                  {message.segments.map((segment, index) => (
                    <SegmentBlock
                      key={`${message.id}-seg-${index}`}
                      segment={segment}
                      message={message}
                      isStreaming={isStreaming}
                      isLast={index === message.segments!.length - 1}
                    />
                  ))}
                  {message.retrievals && message.retrievals.length > 0 && (
                    <RetrievalCard retrievals={message.retrievals} />
                  )}
                  <div className="text-[10px] text-gray-400 mt-1 pl-1">
                    {formatTime(message.timestamp)}
                  </div>
                </div>
              ) : (
                <>
                  {(() => {
                    const hasTools = message.timeline?.some((item) => item.type === "tool") ?? false;

                    const thoughtChain =
                      message.timeline && message.timeline.length > 0 ? (
                        <ThoughtChain timeline={message.timeline} isStreaming={isStreaming} />
                      ) : message.reasoning ? (
                        <ReasoningBlock
                          content={message.reasoning}
                          defaultOpen={isStreaming && !message.content}
                          isStreaming={isStreaming && !message.content}
                        />
                      ) : null;

                    const contentBlock = hasAuthError ? (
                      <AuthErrorAlert content={message.content} />
                    ) : message.content ? (
                      <div>
                        <div className="px-1 py-1 text-[15px] leading-relaxed">
                          <div className="markdown-content">
                            <ReactMarkdown remarkPlugins={markdownRemarkPlugins} components={citationComponents}>
                              {renderedContent}
                            </ReactMarkdown>
                          </div>
                        </div>
                        {message.retrievals && message.retrievals.length > 0 && (
                          <RetrievalCard retrievals={message.retrievals} />
                        )}
                        <div className="text-[10px] text-gray-400 mt-1 pl-1">
                          {formatTime(message.timestamp)}
                        </div>
                      </div>
                    ) : null;

                    // Pure reasoning precedes the answer; tool chains follow it
                    // so intent and action stay adjacent.
                    return (
                      <>
                        {pendingPermissionRequests.map((request) => (
                          <ExternalFilePermissionCard
                            key={request.id}
                            request={request}
                            sessionId={sessionId}
                          />
                        ))}
                        {!hasTools && thoughtChain}
                        {contentBlock}
                        {hasTools && thoughtChain}
                      </>
                    );
                  })()}
                </>
              )}

              {/* Typing indicator — only when nothing else is visible yet */}
              {isStreaming && !message.content && !message.reasoning && !message.timeline?.length ? (
                <div className="workspace-message-card inline-flex items-center gap-2 rounded-2xl px-4 py-3 text-[12px] text-slate-500">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[#002fa7]" />
                    <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[#002fa7]" />
                    <span className="typing-dot h-1.5 w-1.5 rounded-full bg-[#002fa7]" />
                  </span>
                  <span>Agent 正在处理</span>
                </div>
              ) : null}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function ExternalFilePermissionCard({
  request,
  sessionId,
}: {
  request: PermissionRequest;
  sessionId: string;
}) {
  const [status, setStatus] = useState<"idle" | "loading" | "granted" | "denied" | "error">("idle");
  const [error, setError] = useState("");
  const path = request.path || "";
  const name = path.split("/").filter(Boolean).pop() || "外部文件";

  const grant = async (targetKind: "exact_file" | "all_external_files") => {
    setStatus("loading");
    setError("");
    try {
      await grantExternalFileRead(
        sessionId,
        targetKind,
        targetKind === "exact_file" ? path : undefined,
        request.id
      );
      setStatus("granted");
      window.dispatchEvent(new CustomEvent("puddingclaw:permissions-changed"));
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "授权失败");
    }
  };

  const deny = async () => {
    setStatus("loading");
    setError("");
    try {
      await denyPermissionRequest(sessionId, request.id, "User denied external file read permission.");
      setStatus("denied");
      window.dispatchEvent(new CustomEvent("puddingclaw:permissions-changed"));
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "拒绝失败");
    }
  };

  return (
    <div className="mb-3 max-w-[680px] rounded-2xl border border-black/[0.06] bg-white/75 p-4 shadow-sm shadow-slate-950/[0.04] backdrop-blur">
      <div className="flex gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/10 text-[#002fa7]">
          {status === "granted" ? <CheckCircle2 className="h-5 w-5" /> : <KeyRound className="h-5 w-5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[15px] font-bold text-slate-950">允许读取外部文件</h3>
            {status === "granted" ? (
              <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                已授权
              </span>
            ) : null}
          </div>
          <div className="mt-2 flex items-start gap-2 rounded-xl bg-slate-50 px-3 py-2">
            <FileText className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
            <div className="min-w-0">
              <div className="truncate text-[13px] font-medium text-slate-800">{name}</div>
              <div className="mt-0.5 truncate font-mono text-[11px] text-slate-500">{path}</div>
            </div>
          </div>
          {status !== "granted" && status !== "denied" ? (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={status === "loading"}
                onClick={() => grant("exact_file")}
                className="rounded-full bg-[#002fa7] px-3.5 py-2 text-[12px] font-semibold text-white shadow-sm transition hover:bg-[#00298f] disabled:cursor-default disabled:opacity-60"
              >
                允许此文件
              </button>
              <button
                type="button"
                disabled={status === "loading"}
                onClick={() => grant("all_external_files")}
                className="rounded-full bg-white px-3.5 py-2 text-[12px] font-semibold text-slate-700 shadow-sm ring-1 ring-black/[0.08] transition hover:bg-slate-50 disabled:cursor-default disabled:opacity-60"
              >
                本 session 允许所有外部文件
              </button>
              <button
                type="button"
                disabled={status === "loading"}
                onClick={deny}
                className="rounded-full px-3 py-2 text-[12px] font-semibold text-slate-500 transition hover:bg-slate-100 hover:text-slate-800 disabled:cursor-default disabled:opacity-60"
              >
                拒绝
              </button>
            </div>
          ) : null}
          {status === "loading" ? <div className="mt-2 text-[11px] text-slate-500">处理中...</div> : null}
          {status === "denied" ? <div className="mt-2 text-[11px] text-slate-500">已拒绝</div> : null}
          {status === "error" ? <div className="mt-2 text-[11px] text-rose-600">{error}</div> : null}
        </div>
      </div>
    </div>
  );
}

function SegmentBlock({
  segment,
  message,
  isStreaming,
  isLast,
}: {
  segment: { content: string; reasoning?: string; timeline?: TimelineItem[] };
  message: ChatMessageType;
  isStreaming?: boolean;
  isLast?: boolean;
}) {
  const { setActiveSourceId, setInspectorOpen } = useApp();
  const rendered = renderCitationMarkersForSegment(message, segment.content);
  const citationComponents: Components = {
    a: (props) => (
      <CitationLink
        {...props}
        sources={message.sources}
        onActivate={(sourceId) => {
          setActiveSourceId(sourceId);
          setInspectorOpen(true);
        }}
      />
    ),
  };

  const hasTools = segment.timeline?.some((item) => item.type === "tool") ?? false;

  const thoughtChain =
    segment.timeline && segment.timeline.length > 0 ? (
      <ThoughtChain timeline={segment.timeline} isStreaming={isStreaming && isLast} />
    ) : segment.reasoning ? (
      <ReasoningBlock
        content={segment.reasoning}
        defaultOpen={isStreaming && !segment.content}
        isStreaming={isStreaming && !segment.content}
      />
    ) : null;

  const contentBlock = segment.content ? (
    <div className="px-1 py-1 text-[15px] leading-relaxed">
      <div className="markdown-content">
        <ReactMarkdown remarkPlugins={markdownRemarkPlugins} components={citationComponents}>
          {rendered}
        </ReactMarkdown>
      </div>
    </div>
  ) : null;

  // Keep reasoning and tools together as one thought chain. If the chain only
  // contains reasoning, show it before the statement so it doesn't jump after
  // streaming ends. If it contains tools, show it after the statement so the
  // user can see the intent -> action flow and why tools were called.
  return (
    <div className="space-y-2">
      {!hasTools && thoughtChain}
      {contentBlock}
      {hasTools && thoughtChain}
    </div>
  );
}

function renderCitationMarkersForSegment(
  message: ChatMessageType,
  content: string
): string {
  const indexes = new Map<string, number>();
  message.citations?.forEach((citation) => {
    indexes.set(citation.source_id, citation.display_index);
  });
  const existingIndexes = Array.from(indexes.values());
  let nextIndex = existingIndexes.length > 0 ? Math.max(...existingIndexes) + 1 : 1;
  message.sources?.forEach((source) => {
    if (source.source_id && !indexes.has(source.source_id)) {
      indexes.set(source.source_id, nextIndex++);
    }
  });
  if (indexes.size === 0) return content;
  return content.replace(/\[\^(src_[A-Za-z0-9_-]+)\]/g, (marker, sourceId: string) => {
    const index = indexes.get(sourceId);
    return index ? `[${index}](#source-${sourceId})` : marker;
  });
}

function ReasoningBlock({
  content,
  defaultOpen,
  isStreaming,
}: {
  content: string;
  defaultOpen?: boolean;
  isStreaming?: boolean;
}) {
  const [open, setOpen] = useState(Boolean(defaultOpen));
  const lineCount = content.split("\n").filter(Boolean).length;

  return (
    <div className="mb-2 inline-block max-w-full overflow-hidden rounded-xl border border-black/[0.055] bg-white/58 shadow-sm shadow-slate-950/[0.025]">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex max-w-full items-center gap-2 px-3 py-1.5 text-[12px] text-slate-600 transition-colors hover:bg-white/60"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 text-slate-400" />
        ) : (
          <ChevronRight className="h-3 w-3 text-slate-400" />
        )}
        <div className="flex h-5 w-5 items-center justify-center rounded bg-[#eef2ff] text-[#002fa7]">
          <Sparkles className="h-3 w-3" />
        </div>
        <span className="font-medium">思考过程</span>
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
          {content.length} 字{lineCount > 1 ? ` · ${lineCount} 行` : ""}
        </span>
        {isStreaming && (
          <span className="ml-1 inline-flex items-center gap-1.5 text-[11px] text-[#002fa7]">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#002fa7]" />
            正在推理
          </span>
        )}
      </button>
      {open && (
        <div className="w-[min(720px,calc(100vw-180px))] max-w-full border-t border-black/[0.045] px-3 pb-2 pt-1.5">
          <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg bg-white/58 p-2 text-[11px] leading-relaxed text-slate-500">
            {content}
          </pre>
        </div>
      )}
    </div>
  );
}

function renderCitationMarkers(message: ChatMessageType): string {
  const indexes = new Map<string, number>();

  // Citations carry the authoritative display index.
  message.citations?.forEach((citation) => {
    indexes.set(citation.source_id, citation.display_index);
  });

  // Fallback: assign sequential indexes from sources for markers that were not
  // finalized as citations (e.g. due to truncation or adapter mismatch).
  const existingIndexes = Array.from(indexes.values());
  let nextIndex = existingIndexes.length > 0 ? Math.max(...existingIndexes) + 1 : 1;
  message.sources?.forEach((source) => {
    if (source.source_id && !indexes.has(source.source_id)) {
      indexes.set(source.source_id, nextIndex++);
    }
  });

  if (indexes.size === 0) return message.content;

  return message.content.replace(/\[\^(src_[A-Za-z0-9_-]+)\]/g, (marker, sourceId: string) => {
    const index = indexes.get(sourceId);
    return index ? `[${index}](#source-${sourceId})` : marker;
  });
}

function CitationLink({
  href,
  children,
  sources,
  onActivate,
}: {
  href?: string;
  children?: React.ReactNode;
  sources?: SourceRecord[];
  onActivate?: (sourceId: string) => void;
}) {
  if (!href?.startsWith("#source-")) {
    return <a href={href}>{children}</a>;
  }
  const sourceId = href.replace("#source-", "");
  const source = sources?.find((s) => s.source_id === sourceId);
  const label = typeof children === "string" ? children : "•";

  return (
    <sup className="inline-block mx-0.5">
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          onActivate?.(sourceId);
        }}
        title={source?.title || sourceId}
        className="inline-flex h-4 min-w-4 items-center justify-center rounded bg-[#002fa7]/[0.08] px-1 text-[10px] font-semibold text-[#002fa7] hover:bg-[#002fa7]/[0.15]"
      >
        {label}
      </button>
    </sup>
  );
}

/** Prominent auth error alert with setup guidance */
function AuthErrorAlert({ content }: { content: string }) {
  return (
    <div className="animate-fade-in-scale rounded-xl border border-red-200 bg-red-50/80 px-4 py-3 space-y-2">
      <div className="flex items-center gap-2">
        <AlertTriangle className="w-4 h-4 text-red-500 shrink-0" />
        <span className="text-[13px] font-semibold text-red-700">
          API Key 认证失败
        </span>
      </div>
      <p className="text-[12px] text-red-600/80 leading-relaxed">
        你的 API Key 无效或未配置。请检查 <code className="bg-red-100 px-1 rounded text-red-700">backend/.env</code> 文件中的配置。
      </p>
      <div className="flex items-center gap-3 pt-1">
        <a
          href="http://localhost:8002/"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-[11px] font-medium text-red-600 hover:text-red-800 transition-colors"
        >
          <Key className="w-3 h-3" />
          检查后端状态
        </a>
        <span className="text-[10px] text-red-400">|</span>
        <span className="text-[10px] text-red-500 font-mono">{content.slice(0, 120)}...</span>
      </div>
    </div>
  );
}
