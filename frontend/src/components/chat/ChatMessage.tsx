"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Database, FileSpreadsheet, FileText, Key, KeyRound, Layers3, PauseCircle, Plus, Sparkles, Trash2 } from "lucide-react";
import { denyPermissionRequest, grantExternalFileRead, openLocalFile, resolveDimensionBuildRuleRequest, type DimensionBuildRuleRequest, type PermissionRequest } from "@/lib/api";
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
                    <ExternalFilePermissionCard
                      key={request.id}
                      request={request}
                      sessionId={sessionId}
                    />
                  ))}
                  {pendingDimensionBuildRuleRequests.map((request) => (
                    <DimensionBuildRuleCard key={request.id} request={request} />
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
                          <ExternalFilePermissionCard
                            key={request.id}
                            request={request}
                            sessionId={sessionId}
                          />
                        ))}
                        {pendingDimensionBuildRuleRequests.map((request) => (
                          <DimensionBuildRuleCard key={request.id} request={request} />
                        ))}
                        {(message.content || thoughtChain || pendingPermissionRequests.length > 0 || pendingDimensionBuildRuleRequests.length > 0) && (
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
