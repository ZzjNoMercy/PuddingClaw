"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Database, FileSpreadsheet, FileText, Globe2, Key, KeyRound, Layers3, PauseCircle, Plus, Sparkles, SquareTerminal, Trash2 } from "lucide-react";
import { denyPermissionRequest, grantExternalFilePermission, grantToolActionPermission, openLocalFile, resolveDatabaseSqlRevisionRequest, resolveDimensionBuildRuleRequest, resolveLogicalDatasetRuleRequest, type AgentAttachment, type DatabaseSqlRevisionRequest, type DimensionBuildRuleRequest, type LogicalDatasetRuleRequest, type PermissionRequest } from "@/lib/api";
import { markdownRemarkPlugins, markdownUrlTransform } from "@/lib/markdown";
import { useApp, type ChatMessage as ChatMessageType, type SourceRecord, type TimelineItem } from "@/lib/store";
import ThoughtChain from "./ThoughtChain";
import RetrievalCard from "./RetrievalCard";

interface Props {
  message: ChatMessageType;
  isStreaming?: boolean;
  showInterruptionNotice?: boolean;
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

export default function ChatMessage({ message, isStreaming = false, showInterruptionNotice = false }: Props) {
  const isUser = message.role === "user";
  const hasAuthError = !isUser && isAuthError(message.content);
  const renderedContent = renderCitationMarkers(message);
  const { sessionId, setActiveSourceId, setInspectorOpen } = useApp();
  const pendingPermissionRequests = (message.permissionRequests || []).filter(
    (request) => request.status !== "resolved"
  );
  const pendingDimensionBuildRuleRequests = (message.dimensionBuildRuleRequests || []).filter(
    (request) => request.status !== "resolved"
  );
  const pendingLogicalDatasetRuleRequests = (message.logicalDatasetRuleRequests || []).filter(
    (request) => request.status !== "resolved"
  );
  const pendingDatabaseSqlRevisionRequests = (message.databaseSqlRevisionRequests || []).filter(
    (request) => request.status !== "resolved"
  );

  const citationComponents: Components = {
    a: (props) => (
      <CitationLink
        {...props}
        sessionId={sessionId}
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
            <div className="flex max-w-xl flex-col items-end">
              {message.content ? (
                <div className="rounded-2xl rounded-tr-md bg-[#002fa7] px-4 py-2.5 text-[14px] leading-relaxed text-white shadow-sm shadow-blue-950/10">
                  {message.content}
                </div>
              ) : null}
              {message.attachments?.length ? <UserAttachmentList attachments={message.attachments} /> : null}
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
                  {pendingPermissionRequests.map((request) => (
                    <PermissionRequestCard
                      key={request.id}
                      request={request}
                      sessionId={sessionId}
                    />
                  ))}
                  {pendingDimensionBuildRuleRequests.map((request) => (
                    <DimensionBuildRuleCard key={request.id} request={request} />
                  ))}
                  {pendingLogicalDatasetRuleRequests.map((request) => (
                    <LogicalDatasetRuleCard key={request.id} request={request} />
                  ))}
                  {pendingDatabaseSqlRevisionRequests.map((request) => (
                    <DatabaseSqlRevisionCard key={request.id} request={request} />
                  ))}
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
                            <ReactMarkdown
                              remarkPlugins={markdownRemarkPlugins}
                              components={citationComponents}
                              urlTransform={markdownUrlTransform}
                            >
                              {renderedContent}
                            </ReactMarkdown>
                          </div>
                        </div>
                        {message.retrievals && message.retrievals.length > 0 && (
                          <RetrievalCard retrievals={message.retrievals} />
                        )}
                      </div>
                    ) : null;

                    // Pure reasoning precedes the answer; tool chains follow it
                    // so intent and action stay adjacent.
                    return (
                      <>
                        {!hasTools && thoughtChain}
                        {contentBlock}
                        {hasTools && thoughtChain}
                        {pendingPermissionRequests.map((request) => (
                          <PermissionRequestCard
                            key={request.id}
                            request={request}
                            sessionId={sessionId}
                          />
                        ))}
                        {pendingDimensionBuildRuleRequests.map((request) => (
                          <DimensionBuildRuleCard key={request.id} request={request} />
                        ))}
                        {pendingLogicalDatasetRuleRequests.map((request) => (
                          <LogicalDatasetRuleCard key={request.id} request={request} />
                        ))}
                        {pendingDatabaseSqlRevisionRequests.map((request) => (
                          <DatabaseSqlRevisionCard key={request.id} request={request} />
                        ))}
                        {(message.content || thoughtChain || pendingPermissionRequests.length > 0 || pendingDimensionBuildRuleRequests.length > 0 || pendingLogicalDatasetRuleRequests.length > 0 || pendingDatabaseSqlRevisionRequests.length > 0) && (
                          <div className="text-[10px] text-gray-400 mt-1 pl-1">
                            {formatTime(message.timestamp)}
                          </div>
                        )}
                      </>
                    );
                  })()}
                </>
              )}

              {showInterruptionNotice && message.interrupted && message.interruptionNotice ? (
                <InterruptionNotice text={message.interruptionNotice} />
              ) : null}
              {message.errorNotice ? (
                <ErrorNotice text={message.errorNotice} />
              ) : null}

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

function UserAttachmentList({ attachments }: { attachments: AgentAttachment[] }) {
  return (
    <div className="mt-2 flex max-w-xl flex-wrap justify-end gap-2">
      {attachments.map((attachment, index) => {
        const Icon = attachment.type === "spreadsheet" ? FileSpreadsheet : FileText;
        return (
          <div
            key={`${attachment.id || attachment.name || "attachment"}-${index}`}
            className="inline-flex max-w-full items-center gap-2 rounded-xl border border-[#002fa7]/15 bg-[#f7f9ff] px-3 py-2 text-left text-[12px] text-gray-700 shadow-sm"
            title={attachment.name || attachment.path || "附件"}
          >
            <Icon className="h-4 w-4 shrink-0 text-[#002fa7]" />
            <span className="min-w-0 truncate font-medium">{attachment.name || attachment.path || attachment.id || "附件"}</span>
          </div>
        );
      })}
    </div>
  );
}

function InterruptionNotice({ text }: { text: string }) {
  return (
    <div className="mt-3 inline-flex max-w-full items-start gap-2 rounded-xl border border-amber-200 bg-amber-50/80 px-3 py-1.5 text-[12px] font-medium leading-relaxed text-amber-800 shadow-sm shadow-amber-900/[0.03]">
      <PauseCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
      <span className="min-w-0 break-words">{text}</span>
    </div>
  );
}

function ErrorNotice({ text }: { text: string }) {
  return (
    <div className="mt-3 inline-flex max-w-full items-start gap-2 rounded-xl border border-rose-200 bg-rose-50/85 px-3 py-1.5 text-[12px] font-medium leading-relaxed text-rose-800 shadow-sm shadow-rose-900/[0.03]">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-600" />
      <span className="min-w-0 break-words">{text}</span>
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
  const isWrite = request.type === "external_file_write";

  const grant = async (targetKind: "exact_file" | "all_external_files") => {
    setStatus("loading");
    setError("");
    try {
      await grantExternalFilePermission(
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
      await denyPermissionRequest(
        sessionId,
        request.id,
        isWrite ? "User denied external file write permission." : "User denied external file read permission."
      );
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
            <h3 className="text-[15px] font-bold text-slate-950">
              {isWrite ? "允许修改外部文件" : "允许读取外部文件"}
            </h3>
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
          {isWrite && request.change_preview ? (
            <div className="mt-2 space-y-2 rounded-xl border border-amber-100 bg-amber-50/70 px-3 py-2">
              {Object.entries(request.change_preview).map(([key, value]) => (
                <div key={key}>
                  <div className="text-[10px] font-semibold uppercase tracking-wide text-amber-700">{key}</div>
                  <pre className="mt-1 max-h-28 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-slate-700">
                    {value}
                  </pre>
                </div>
              ))}
            </div>
          ) : null}
          {status !== "granted" && status !== "denied" ? (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={status === "loading"}
                onClick={() => grant("exact_file")}
                className="rounded-full bg-[#002fa7] px-3.5 py-2 text-[12px] font-semibold text-white shadow-sm transition hover:bg-[#00298f] disabled:cursor-default disabled:opacity-60"
              >
                {isWrite ? "允许修改此文件" : "允许此文件"}
              </button>
              {!isWrite ? (
                <button
                  type="button"
                  disabled={status === "loading"}
                  onClick={() => grant("all_external_files")}
                  className="rounded-full bg-white px-3.5 py-2 text-[12px] font-semibold text-slate-700 shadow-sm ring-1 ring-black/[0.08] transition hover:bg-slate-50 disabled:cursor-default disabled:opacity-60"
                >
                  本 session 允许所有外部文件
                </button>
              ) : null}
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

function PermissionRequestCard({
  request,
  sessionId,
}: {
  request: PermissionRequest;
  sessionId: string;
}) {
  if (request.type === "tool_action") {
    return <ToolActionPermissionCard request={request} sessionId={sessionId} />;
  }
  return <ExternalFilePermissionCard request={request} sessionId={sessionId} />;
}

function ToolActionPermissionCard({
  request,
  sessionId,
}: {
  request: PermissionRequest;
  sessionId: string;
}) {
  const [status, setStatus] = useState<"idle" | "loading" | "granted" | "denied" | "error">("idle");
  const [error, setError] = useState("");
  const isFetchUrl = request.tool_name === "fetch_url";
  const isSearch = request.tool_name === "tavily_search";
  const isNetworkTool = isFetchUrl || isSearch;
  const title = isFetchUrl
    ? "允许访问网站"
    : isSearch
      ? "允许联网搜索"
      : "允许执行受控命令";

  const grant = async (scope: "once" | "session") => {
    setStatus("loading");
    setError("");
    try {
      await grantToolActionPermission(sessionId, request.id, scope);
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
      await denyPermissionRequest(
        sessionId,
        request.id,
        "User denied managed Tool execution.",
      );
      setStatus("denied");
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "拒绝失败");
    }
  };

  return (
    <div className="mb-3 max-w-[760px] rounded-2xl border border-amber-200 bg-amber-50/75 p-4 shadow-sm shadow-amber-950/[0.04]">
      <div className="flex gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-amber-100 text-amber-700">
          {status === "granted"
            ? <CheckCircle2 className="h-5 w-5" />
            : isNetworkTool
              ? <Globe2 className="h-5 w-5" />
              : <SquareTerminal className="h-5 w-5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[15px] font-bold text-slate-950">{title}</h3>
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
              {request.risk || "managed"}
            </span>
          </div>
          <p className="mt-1 text-[12px] text-slate-500">
            Harness 规则：{request.reason || "需要人工确认"}
          </p>
          <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-slate-950 px-3 py-2.5 font-mono text-[12px] leading-5 text-slate-100">
            {request.command || ""}
          </pre>
          {status === "idle" || status === "error" ? (
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => void grant("once")}
                className="rounded-full bg-[#002fa7] px-3.5 py-2 text-[12px] font-semibold text-white hover:bg-[#00298f]"
              >
                仅允许本次
              </button>
              <button
                type="button"
                onClick={() => void grant("session")}
                className="rounded-full bg-white px-3.5 py-2 text-[12px] font-semibold text-slate-700 ring-1 ring-black/[0.08] hover:bg-slate-50"
              >
                {request.session_scope_label || "本 Session 允许相同命令"}
              </button>
              <button
                type="button"
                onClick={() => void deny()}
                className="rounded-full px-3 py-2 text-[12px] font-semibold text-slate-500 hover:bg-white/70"
              >
                拒绝
              </button>
            </div>
          ) : null}
          {status === "loading" ? <p className="mt-2 text-[11px] text-slate-500">处理中...</p> : null}
          {status === "granted" ? <p className="mt-2 text-[11px] text-emerald-700">已授权，Agent 将继续执行。</p> : null}
          {status === "denied" ? <p className="mt-2 text-[11px] text-slate-500">已拒绝。</p> : null}
          {status === "error" ? <p className="mt-2 text-[11px] text-rose-600">{error}</p> : null}
        </div>
      </div>
    </div>
  );
}

function DatabaseSqlRevisionCard({ request }: { request: DatabaseSqlRevisionRequest }) {
  const [modifiedInstruction, setModifiedInstruction] = useState(request.proposed_revision_instruction);
  const [status, setStatus] = useState<"idle" | "loading" | "agreed" | "rejected" | "modified" | "error">("idle");
  const [error, setError] = useState("");
  const semanticIds = [
    ...(request.semantic_assets?.references || []),
    ...(request.semantic_assets?.matched || []),
  ].map((item) => String(item.id || item.name || "")).filter(Boolean);

  const resolve = async (action: "agree" | "reject" | "modify") => {
    const instruction = modifiedInstruction.trim();
    if (action === "modify" && !instruction) {
      setError("请填写修改后的自然语言口径。");
      return;
    }
    setStatus("loading");
    setError("");
    try {
      await resolveDatabaseSqlRevisionRequest(
        request.id,
        action === "modify" ? { action, revision_instruction: instruction } : { action },
      );
      setStatus(action === "agree" ? "agreed" : action === "reject" ? "rejected" : "modified");
    } catch (nextError) {
      setStatus("error");
      setError(nextError instanceof Error ? nextError.message : "处理 SQL 口径修改失败");
    }
  };

  if (["agreed", "rejected", "modified"].includes(status)) {
    const text = status === "rejected"
      ? "已拒绝修改，将继续使用原 database_sql_generate 结果。"
      : status === "agreed"
        ? "已同意，正在把 Agent 的自然语言补充重新交给 database_sql_generate。"
        : "已提交修改后的自然语言补充，正在重新调用 database_sql_generate。";
    return <div className="mb-3 inline-flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"><CheckCircle2 className="h-4 w-4" />{text}</div>;
  }

  return <section className="mb-4 max-w-[820px] rounded-2xl border border-amber-200 bg-white p-5 shadow-sm shadow-amber-950/[0.04]">
    <div className="flex items-start gap-3">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-amber-100 text-amber-700"><Database className="h-5 w-5" /></div>
      <div><p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-amber-700">SQL 口径变更确认</p><h3 className="mt-1 text-base font-bold text-slate-950">Agent 想改变生成器采用的业务口径</h3><p className="mt-1 text-sm leading-6 text-slate-500">审批内容仅为自然语言。无论同意还是修改，SQL 都会由 database_sql_generate 重新生成。</p></div>
    </div>
    <div className="mt-4 grid gap-3 sm:grid-cols-2">
      <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs font-semibold text-slate-500">原问题</p><p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-800">{request.original_question}</p></div>
      <div className="rounded-xl bg-amber-50 p-3"><p className="text-xs font-semibold text-amber-700">Agent 建议的口径补充</p><p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-800">{request.proposed_revision_instruction}</p></div>
    </div>
    {semanticIds.length ? <p className="mt-3 text-xs text-slate-500">当前生成器语义依据：{semanticIds.join("、")}</p> : null}
    <details className="mt-3 rounded-xl border border-black/[0.08] bg-slate-950/[0.02]">
      <summary className="cursor-pointer select-none px-3 py-2.5 text-sm font-semibold text-slate-700">查看原 database_sql_generate SQL</summary>
      <pre className="max-h-80 overflow-auto border-t border-black/[0.07] bg-slate-950 p-3 text-xs leading-5 text-slate-100"><code>{request.original_sql}</code></pre>
    </details>
    <label className="mt-4 block text-sm font-semibold text-slate-700">如需修改，请填写最终的自然语言补充<textarea value={modifiedInstruction} onChange={(event) => setModifiedInstruction(event.target.value)} className="mt-1.5 min-h-20 w-full rounded-xl border border-black/[0.1] px-3 py-2 text-sm font-normal outline-none focus:border-[#002fa7]" /></label>
    {error ? <p className="mt-3 text-sm text-rose-600">{error}</p> : null}
    <div className="mt-4 flex flex-wrap justify-end gap-2">
      <button type="button" disabled={status === "loading"} onClick={() => void resolve("reject")} className="h-9 rounded-xl bg-rose-600 px-4 text-sm font-semibold text-white transition hover:bg-rose-700 disabled:opacity-50">拒绝（使用原 SQL）</button>
      <button type="button" disabled={status === "loading"} onClick={() => void resolve("agree")} className="inline-flex h-9 items-center gap-2 rounded-xl bg-emerald-600 px-4 text-sm font-semibold text-white transition hover:bg-emerald-700 disabled:opacity-50">{status === "loading" ? <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" /> : <CheckCircle2 className="h-4 w-4" />}同意并重新生成</button>
      <button type="button" disabled={status === "loading" || !modifiedInstruction.trim()} onClick={() => void resolve("modify")} className="h-9 rounded-xl border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-700 transition hover:border-slate-400 hover:bg-slate-50 disabled:opacity-50">修改后重新生成</button>
    </div>
  </section>;
}

function DimensionBuildRuleCard({ request }: { request: DimensionBuildRuleRequest }) {
  const lockedCanonicalId = request.locked_canonical_candidate_id || "";
  const [canonicalId, setCanonicalId] = useState(() => lockedCanonicalId || request.candidates[0]?.id || "");
  const defaultSourceProfile = (candidate: DimensionBuildRuleRequest["candidates"][number]) => {
    const suggested = candidate.suggested_source_id || candidate.id;
    return request.registered_sources?.some((source) => source.id === suggested) ? `append:${suggested}` : `new:${suggested}`;
  };
  const [sourceProfileByCandidate, setSourceProfileByCandidate] = useState<Record<string, string>>(() => Object.fromEntries(
    request.candidates.map((candidate) => [candidate.id, defaultSourceProfile(candidate)])
  ));
  const [keySlots, setKeySlots] = useState<Array<Record<string, string>>>(() => {
    const slots = Math.max(1, ...request.candidates.map((candidate) => candidate.suggested_key_fields?.length || 1));
    return Array.from({ length: slots }, (_, index) => Object.fromEntries(request.candidates.map((candidate) => [
      candidate.id,
      candidate.suggested_key_fields?.[index] || candidate.fields[index] || "",
    ])));
  });
  const [status, setStatus] = useState<"idle" | "loading" | "confirmed" | "cancelled" | "error">("idle");
  const [error, setError] = useState("");

  const sourceIcon = (kind: string) => kind === "active_crosswalk"
    ? <Layers3 className="h-4 w-4" />
    : kind === "database_table"
    ? <Database className="h-4 w-4" />
    : <FileSpreadsheet className="h-4 w-4" />;

  const resolve = async (action: "confirm" | "cancel") => {
    setStatus("loading");
    setError("");
    try {
      if (action === "cancel") {
        await resolveDimensionBuildRuleRequest(request.id, { action });
        setStatus("cancelled");
        return;
      }
      if (!canonicalId || keySlots.length === 0) {
        throw new Error("请选择基准输入及至少一个键字段。");
      }
      const bindings = request.candidates.map((candidate) => {
        const fields = keySlots.map((slot) => slot[candidate.id] || "").filter(Boolean);
        const outputFields = candidate.suggested_output_fields?.length === fields.length
          ? candidate.suggested_output_fields
          : fields.map((field, index) => candidate.id === canonicalId ? `canonical_${field}` : `source_${index + 1}`);
        const sourceProfile = sourceProfileByCandidate[candidate.id] || defaultSourceProfile(candidate);
        const [sourceMode, sourceId] = sourceProfile.split(":", 2) as ["new" | "append", string];
        const registered = request.registered_sources?.find((source) => source.id === sourceId);
        return {
          candidate_id: candidate.id,
          key_fields: fields,
          output_fields: outputFields,
          source_mode: candidate.id === canonicalId ? "new" : sourceMode,
          source_id: candidate.id === canonicalId ? candidate.id : sourceId,
          source_name: candidate.id === canonicalId ? candidate.display_name : (registered?.name || candidate.suggested_source_name || candidate.display_name),
        };
      });
      if (bindings.some((binding) => binding.key_fields.length === 0)) {
        throw new Error("每个输入都需要至少选择一个键字段。");
      }
      if (keySlots.some((slot) => request.candidates.some((candidate) => !slot[candidate.id]))) {
        throw new Error("每一行键位都要为所有输入选择字段。");
      }
      await resolveDimensionBuildRuleRequest(request.id, {
        action,
        canonical_candidate_id: canonicalId,
        bindings,
        conflict_policy: "candidate",
      });
      setStatus("confirmed");
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "确认构建规则失败");
    }
  };

  if (status === "confirmed" || status === "cancelled") {
    return (
      <div className="mb-3 inline-flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
        <CheckCircle2 className="h-4 w-4" />
        {status === "confirmed" ? "维度构建规则已确认，Agent 将创建后台任务。" : "已取消本次维度构建。"}
      </div>
    );
  }

  return (
    <section className="mb-4 max-w-[820px] rounded-2xl border border-[#002fa7]/20 bg-white p-5 shadow-sm shadow-blue-950/[0.05]">
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[#002fa7]/10 text-[#002fa7]"><Sparkles className="h-5 w-5" /></div>
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[#002fa7]">构建维度</p>
          <h3 className="mt-1 text-base font-bold text-slate-950">{request.title || `构建 ${request.dimension_id}`}</h3>
          <p className="mt-1 text-sm leading-6 text-slate-500">{request.reason}</p>
        </div>
      </div>
      <div className="mt-5 rounded-2xl border border-black/[0.07] p-4">
        <div className="flex items-center justify-between gap-3">
          <div><h4 className="text-sm font-semibold text-slate-900">输入角色</h4><p className="mt-1 text-xs text-slate-500">{lockedCanonicalId ? "当前规范基准已锁定；本次只向它追加来源匹配列。" : "选择定义规范实体的基准输入，其他输入仅追加可匹配的来源键。"}</p></div>
          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-medium text-slate-500">{request.candidates.length} 个输入</span>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {request.candidates.map((candidate) => (
            <label key={candidate.id} className={`flex items-center gap-3 rounded-xl border p-3 transition ${canonicalId === candidate.id ? "border-[#002fa7] bg-[#002fa7]/[0.03]" : "border-black/[0.07] hover:border-[#002fa7]/30"} ${lockedCanonicalId && candidate.id !== lockedCanonicalId ? "opacity-70" : ""}`}>
              <input type="radio" name={`canonical-${request.id}`} checked={canonicalId === candidate.id} disabled={Boolean(lockedCanonicalId) || candidate.input.kind === "active_crosswalk"} onChange={() => setCanonicalId(candidate.id)} className="h-4 w-4 accent-[#002fa7] disabled:cursor-not-allowed" />
              <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[#002fa7]/10 text-[#002fa7]">{sourceIcon(candidate.input.kind)}</span>
              <span className="min-w-0"><span className="block truncate text-sm font-semibold text-slate-800">{candidate.display_name}</span><span className="mt-0.5 block text-xs text-slate-400">{canonicalId === candidate.id ? (lockedCanonicalId ? "当前规范实体基准（固定）" : "规范实体基准") : "待匹配来源"}</span></span>
            </label>
          ))}
        </div>
      </div>
      <div className="mt-4 rounded-2xl border border-black/[0.07] p-4">
        <div className="flex items-center justify-between gap-3">
          <div><h4 className="text-sm font-semibold text-slate-900">字段映射</h4><p className="mt-1 text-xs text-slate-500">同一行字段按位置精确匹配。只选择真实字段，不输入自由文本。</p></div>
          <button type="button" onClick={() => setKeySlots((current) => [...current, Object.fromEntries(request.candidates.map((candidate) => [candidate.id, ""]))])} className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-[#002fa7]/20 px-2.5 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7]/[0.04]"><Plus className="h-3.5 w-3.5" />添加键位</button>
        </div>
        <div className="mt-3 space-y-2">
          {keySlots.map((slot, slotIndex) => (
            <div key={slotIndex} className="grid gap-2 rounded-xl bg-slate-50 p-2.5 sm:grid-cols-[68px_minmax(0,1fr)_auto]">
              <div className="flex items-center text-xs font-semibold text-slate-500">键位 {slotIndex + 1}</div>
              <div className={`grid gap-2 ${request.candidates.length > 1 ? "sm:grid-cols-2" : ""}`}>
                {request.candidates.map((candidate) => (
                  <label key={candidate.id} className="min-w-0">
                    <span className="mb-1 block truncate text-[11px] font-medium text-slate-500">{candidate.display_name}</span>
                    <select value={slot[candidate.id] || ""} onChange={(event) => {
                      const value = event.currentTarget.value;
                      setKeySlots((current) => current.map((item, index) => index === slotIndex ? { ...item, [candidate.id]: value } : item));
                    }} className="h-9 w-full rounded-lg border border-black/[0.1] bg-white px-2 text-sm text-slate-800 outline-none focus:border-[#002fa7]">
                      <option value="">选择字段</option>
                      {candidate.fields.map((field) => <option key={field} value={field}>{field}</option>)}
                    </select>
                  </label>
                ))}
              </div>
              <button type="button" disabled={keySlots.length === 1} onClick={() => setKeySlots((current) => current.filter((_, index) => index !== slotIndex))} className="mt-auto inline-flex h-9 w-9 items-center justify-center rounded-lg text-slate-400 transition hover:bg-rose-50 hover:text-rose-600 disabled:cursor-not-allowed disabled:opacity-30" title="删除键位" aria-label="删除键位"><Trash2 className="h-4 w-4" /></button>
            </div>
          ))}
        </div>
      </div>
      {request.candidates.some((candidate) => candidate.id !== canonicalId && candidate.input.kind !== "active_crosswalk") ? <div className="mt-4 rounded-2xl border border-black/[0.07] p-4">
        <div><h4 className="text-sm font-semibold text-slate-900">来源接入</h4><p className="mt-1 text-xs text-slate-500">字段契约相同的新表可追加已有来源，并复用已确认的来源键映射；订单等新业务表注册为独立来源。两者都映射到当前维度，不会新增规范字段。</p></div>
        <div className="mt-3 space-y-2">
          {request.candidates.filter((candidate) => candidate.id !== canonicalId && candidate.input.kind !== "active_crosswalk").map((candidate) => {
            const selected = sourceProfileByCandidate[candidate.id] || defaultSourceProfile(candidate);
            return <label key={candidate.id} className="grid gap-2 rounded-xl bg-slate-50 p-3 sm:grid-cols-[minmax(0,1fr)_260px] sm:items-center"><span className="min-w-0"><span className="block truncate text-sm font-semibold text-slate-800">{candidate.display_name}</span><span className="mt-0.5 block text-xs text-slate-500">选择它是追加哪个已登记来源，或注册为新的来源。</span></span><select value={selected} onChange={(event) => setSourceProfileByCandidate((current) => ({ ...current, [candidate.id]: event.target.value }))} className="h-9 rounded-lg border border-black/[0.1] bg-white px-2 text-sm text-slate-800 outline-none focus:border-[#002fa7]"><option value={`new:${candidate.suggested_source_id || candidate.id}`}>新建来源：{candidate.suggested_source_name || candidate.display_name}</option>{request.registered_sources?.map((source) => <option key={source.id} value={`append:${source.id}`}>追加：{source.name}</option>)}</select></label>;
          })}
        </div>
      </div> : null}
      {error ? <p className="mt-3 text-sm text-rose-600">{error}</p> : null}
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <button type="button" disabled={status === "loading"} onClick={() => void resolve("cancel")} className="h-9 rounded-xl px-3 text-sm font-semibold text-slate-500 transition hover:bg-slate-100 disabled:opacity-50">取消</button>
        <button type="button" disabled={status === "loading"} onClick={() => void resolve("confirm")} className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-sm font-semibold text-white transition hover:bg-[#001f7a] disabled:opacity-50">
          {status === "loading" ? <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" /> : <CheckCircle2 className="h-4 w-4" />}
          确认构建规则
        </button>
      </div>
    </section>
  );
}

function LogicalDatasetRuleCard({ request }: { request: LogicalDatasetRuleRequest }) {
  const [name, setName] = useState(request.suggested_name || "");
  const [description, setDescription] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [preferredIntents, setPreferredIntents] = useState<string[]>(["trend", "period_comparison"]);
  const [directSourceAllowed, setDirectSourceAllowed] = useState(true);
  const [selectedIds, setSelectedIds] = useState(() => request.candidates.map((candidate) => candidate.asset_id));
  const [baselineAssetId, setBaselineAssetId] = useState(() => request.candidates[0]?.asset_id || "");
  const [schemaMode, setSchemaMode] = useState<"strict" | "baseline_fill_missing" | "union_fill_missing">("strict");
  const [status, setStatus] = useState<"idle" | "loading" | "confirmed" | "cancelled" | "error">("idle");
  const [error, setError] = useState("");
  const isAppend = request.operation === "append";
  const selected = request.candidates.filter((candidate) => selectedIds.includes(candidate.asset_id));
  const baseline = selected.find((candidate) => candidate.asset_id === baselineAssetId) || selected[0];
  const comparisonBaseline = isAppend ? request.target || null : baseline;
  const effectiveBaselineAssetId = isAppend ? request.target_asset_id || "" : baseline?.asset_id || "";
  const hasDrift = Boolean(comparisonBaseline && comparisonBaseline.fields.length > 0 && selected.some((candidate) => candidate.fields.length !== comparisonBaseline.fields.length || candidate.fields.some((field) => !comparisonBaseline.fields.includes(field))));
  const toggle = (assetId: string) => setSelectedIds((current) => {
    const next = current.includes(assetId) ? current.filter((id) => id !== assetId) : [...current, assetId];
    if (assetId === effectiveBaselineAssetId && !next.includes(assetId)) setBaselineAssetId(next[0] || "");
    return next;
  });
  const resolve = async (action: "confirm" | "cancel") => {
    setStatus("loading");
    setError("");
    try {
      await resolveLogicalDatasetRuleRequest(request.id, action === "cancel" ? { action } : {
        action,
        name,
        description,
        tags: tagsText.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
        baseline_asset_id: effectiveBaselineAssetId,
        source_asset_ids: selectedIds,
        schema_mode: hasDrift ? schemaMode : "strict",
        preferred_intents: preferredIntents,
        direct_source_allowed: directSourceAllowed,
      });
      setStatus(action === "confirm" ? "confirmed" : "cancelled");
    } catch (nextError) {
      setStatus("error");
      setError(nextError instanceof Error ? nextError.message : "确认逻辑数据集规则失败");
    }
  };
  if (status === "confirmed" || status === "cancelled") return <div className="mb-3 inline-flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"><CheckCircle2 className="h-4 w-4" />{status === "confirmed" ? "逻辑数据集规则已确认，Agent 将执行合并。" : "已取消本次逻辑数据集合并。"}</div>;
  return <section className="mb-4 max-w-[820px] rounded-2xl border border-[#002fa7]/20 bg-white p-5 shadow-sm shadow-blue-950/[0.05]">
    <div className="flex items-start gap-3"><div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[#002fa7]/10 text-[#002fa7]"><Layers3 className="h-5 w-5" /></div><div><p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[#002fa7]">逻辑数据集</p><h3 className="mt-1 text-base font-bold text-slate-950">{request.title}</h3><p className="mt-1 text-sm leading-6 text-slate-500">{request.reason}</p></div></div>
    {!isAppend ? <><div className="mt-4 grid gap-3 sm:grid-cols-2"><label className="block text-sm font-semibold text-slate-700">数据集名称<input value={name} onChange={(event) => setName(event.target.value)} className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] px-3 text-sm font-normal outline-none focus:border-[#002fa7]" /></label><label className="block text-sm font-semibold text-slate-700">标签<input value={tagsText} onChange={(event) => setTagsText(event.target.value)} placeholder="销量, 月度" className="mt-1.5 h-10 w-full rounded-xl border border-black/[0.1] px-3 text-sm font-normal outline-none focus:border-[#002fa7]" /></label></div><label className="mt-3 block text-sm font-semibold text-slate-700">业务描述<textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="说明业务口径和适用范围" className="mt-1.5 min-h-20 w-full rounded-xl border border-black/[0.1] px-3 py-2 text-sm font-normal outline-none focus:border-[#002fa7]" /></label></> : null}
    <div className="mt-4 rounded-2xl border border-black/[0.07] p-4">
      <div className="flex items-start justify-between gap-3"><div><h4 className="text-sm font-semibold text-slate-900">{isAppend ? "选择要追加的来源" : "选择参与来源与基准表"}</h4><p className="mt-1 text-xs text-slate-500">{isAppend ? "已有逻辑数据集固定作为字段基准；下方只选择本次新增的原始表，不会把目标数据集重复作为来源。" : "先勾选参与合并的来源，再从中选择一张作为基准表。基准表决定“丢弃多余字段”时保留的字段集合。"}</p></div><span className="shrink-0 text-xs text-slate-500">已选 {selected.length} 张</span></div>
      {isAppend ? <div className="mt-3 rounded-xl border border-[#002fa7]/15 bg-[#002fa7]/[0.035] px-3 py-2"><span className="text-[11px] font-semibold text-[#002fa7]">追加目标 / 字段基准</span><p className="mt-0.5 truncate text-sm font-semibold text-slate-800">{request.target?.display_name || request.target_asset_id}</p></div> : <div className="mt-3 hidden grid-cols-[minmax(0,1fr)_112px] gap-3 px-3 text-[11px] font-semibold text-slate-400 sm:grid"><span>参与来源</span><span>基准表</span></div>}
      <div className="mt-2 space-y-2">{request.candidates.map((candidate) => {
        const isSelected = selectedIds.includes(candidate.asset_id);
        const isBaseline = effectiveBaselineAssetId === candidate.asset_id;
        return <div key={candidate.asset_id} className={`grid gap-3 rounded-xl border p-3 ${isAppend ? "" : "sm:grid-cols-[minmax(0,1fr)_112px] sm:items-center"} ${isBaseline ? "border-[#002fa7]/35 bg-[#002fa7]/[0.035]" : "border-transparent bg-slate-50"}`}>
          <label className="flex min-w-0 cursor-pointer items-start gap-3"><input type="checkbox" checked={isSelected} onChange={() => toggle(candidate.asset_id)} className="mt-0.5 h-4 w-4 shrink-0 accent-[#002fa7]" /><span className="min-w-0"><span className="block truncate text-sm font-semibold text-slate-800">{candidate.display_name}</span><span className="mt-0.5 block truncate text-xs text-slate-400">{candidate.fields.join("、")}</span></span></label>
          {!isAppend ? <label className={`inline-flex h-8 items-center justify-center gap-2 rounded-lg border px-2 text-xs font-semibold ${isSelected ? "cursor-pointer border-[#002fa7]/20 bg-white text-[#00246f]" : "cursor-not-allowed border-black/[0.07] bg-slate-100 text-slate-400"}`} title={isSelected ? "设为基准表" : "请先选择为参与来源"}><input type="radio" name={`concat-baseline-${request.id}`} checked={isBaseline} disabled={!isSelected} onChange={() => setBaselineAssetId(candidate.asset_id)} className="h-3.5 w-3.5 accent-[#002fa7]" />{isBaseline ? "基准表" : "设为基准"}</label> : null}
        </div>;
      })}</div>
    </div>
    {hasDrift ? <div className="mt-4 rounded-2xl border border-amber-200 bg-amber-50 p-4"><p className="text-sm font-semibold text-amber-900">字段存在差异</p><p className="mt-1 text-xs leading-5 text-amber-800">缺少的保留字段都会置空。请选择如何处理多余字段。</p><div className="mt-3 grid gap-2 sm:grid-cols-2"><button type="button" onClick={() => setSchemaMode("union_fill_missing")} className={`rounded-xl border p-3 text-left ${schemaMode === "union_fill_missing" ? "border-[#002fa7] bg-white text-[#00246f]" : "border-amber-200 bg-white/60 text-slate-600"}`}><span className="block text-sm font-semibold">保留多余字段并补空</span><span className="mt-1 block text-xs">所有字段进入结果。</span></button><button type="button" onClick={() => setSchemaMode("baseline_fill_missing")} className={`rounded-xl border p-3 text-left ${schemaMode === "baseline_fill_missing" ? "border-[#002fa7] bg-white text-[#00246f]" : "border-amber-200 bg-white/60 text-slate-600"}`}><span className="block text-sm font-semibold">丢弃多余字段</span><span className="mt-1 block text-xs">只保留基准表字段。</span></button></div></div> : <div className="mt-4 rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">字段一致，可使用严格合并。</div>}
    <div className="mt-4 rounded-2xl border border-black/[0.08] p-4"><p className="text-sm font-semibold text-slate-800">分析路由</p><label className="mt-2 flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={directSourceAllowed} onChange={(event) => setDirectSourceAllowed(event.target.checked)} />允许指定原始文件或单期明细时直接使用来源表</label></div>
    {error ? <p className="mt-3 text-sm text-rose-600">{error}</p> : null}
    <div className="mt-4 flex justify-end gap-2"><button type="button" disabled={status === "loading"} onClick={() => void resolve("cancel")} className="h-9 rounded-xl px-3 text-sm font-semibold text-slate-500 hover:bg-slate-100">取消</button><button type="button" disabled={status === "loading" || !name.trim() || selected.length < (isAppend ? 1 : 2) || !effectiveBaselineAssetId} onClick={() => void resolve("confirm")} className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-sm font-semibold text-white disabled:opacity-50">{status === "loading" ? <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" /> : <CheckCircle2 className="h-4 w-4" />}{isAppend ? "确认追加来源" : "确认合并规则"}</button></div>
  </section>;
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
  const { sessionId, setActiveSourceId, setInspectorOpen } = useApp();
  const rendered = renderCitationMarkersForSegment(message, segment.content);
  const citationComponents: Components = {
    a: (props) => (
      <CitationLink
        {...props}
        sessionId={sessionId}
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
        <ReactMarkdown
          remarkPlugins={markdownRemarkPlugins}
          components={citationComponents}
          urlTransform={markdownUrlTransform}
        >
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
  sessionId,
  sources,
  onActivate,
}: {
  href?: string;
  children?: React.ReactNode;
  sessionId: string;
  sources?: SourceRecord[];
  onActivate?: (sourceId: string) => void;
}) {
  if (!href?.startsWith("#source-")) {
    if (href?.startsWith("file://")) {
      return (
        <a
          href={href}
          className="not-prose my-1 inline-flex items-center gap-2 rounded-xl border border-[#002fa7]/15 bg-[#002fa7]/[0.06] px-3 py-2 text-[13px] font-semibold text-[#002fa7] shadow-sm shadow-blue-950/[0.03] transition hover:border-[#002fa7]/30 hover:bg-[#002fa7]/[0.1]"
          onClick={async (event) => {
            event.preventDefault();
            try {
              const url = new URL(href);
              await openLocalFile(decodeURIComponent(url.pathname), sessionId);
            } catch (error) {
              window.alert(error instanceof Error ? error.message : "打开本地文件失败");
            }
          }}
        >
          <FileText className="h-4 w-4 shrink-0" />
          {children}
        </a>
      );
    }
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
