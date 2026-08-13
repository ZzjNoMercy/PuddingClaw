"use client";

import { Children, isValidElement, useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Database, Download, FileSpreadsheet, FileText, FolderOpen, Globe2, HelpCircle, ImageIcon, Key, KeyRound, Layers3, Loader2, Maximize2, PauseCircle, Plus, Sparkles, SquareTerminal, Trash2, XCircle } from "lucide-react";
import { denyPermissionRequest, grantExternalFilePermission, grantShellDirectoryPermission, grantToolActionPermission, resolveDatabaseSqlRevisionRequest, resolveDimensionBuildRuleRequest, resolveKernelFallbackRequest, resolveLogicalDatasetRuleRequest, resolveSkillSecretRequest, resolveUserInputRequest, type AgentAttachment, type DatabaseSqlRevisionRequest, type DimensionBuildRuleRequest, type KernelFallbackRequest, type LogicalDatasetRuleRequest, type PermissionRequest, type SkillSecretRequest, type UserInputAnswer, type UserInputRequest } from "@/lib/api";
import { markdownRemarkPlugins, markdownUrlTransform, normalizeLooseStrongMarkdown } from "@/lib/markdown";
import { useApp, type ChatMessage as ChatMessageType, type SourceRecord, type TimelineItem, type ToolCall } from "@/lib/store";
import { isPreviewableImageAttachment, isQrImageAttachment } from "@/lib/imageAttachments";
import { placeOutputAttachments } from "@/lib/artifactPlacement";
import { splitTimelineAtManagedAuthorizations } from "@/lib/managedAuthorization";
import { parseLightweightHtmlDocument } from "@/lib/lightweightHtml";
import ThoughtChain, { SkillPlanCards } from "./ThoughtChain";
import RetrievalCard from "./RetrievalCard";
import ManagedAuthorizationCards from "./ManagedAuthorizationCard";
import HtmlArtifactCard from "./HtmlArtifactCard";
import LocalFileAttachmentCard from "./LocalFileAttachmentCard";

interface Props {
  message: ChatMessageType;
  sessionSources?: SourceRecord[];
  isStreaming?: boolean;
  showInterruptionNotice?: boolean;
}

const BEIJING_DATE_TIME_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: "Asia/Shanghai",
});

function beijingDateTimeParts(date: Date): Record<string, string> {
  return Object.fromEntries(
    BEIJING_DATE_TIME_FORMATTER.formatToParts(date).map(({ type, value }) => [type, value])
  );
}

function formatTime(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return "";
  const value = beijingDateTimeParts(new Date(ts));
  const today = beijingDateTimeParts(new Date());
  const time = `${value.hour}:${value.minute}`;
  const isToday = value.year === today.year
    && value.month === today.month
    && value.day === today.day;
  return isToday ? time : `${value.year}/${value.month}/${value.day} ${time}`;
}

function stripModelCallLimitNotice(content: string): string {
  return content
    .replace(
      /(?:\r?\n){0,2}Model call limits exceeded:\s*(?:run|thread) limit\s*\(\d+\s*\/\s*\d+\)\.?\s*$/i,
      ""
    )
    .trimEnd();
}

const ScrollableMarkdownTable: Components["table"] = ({ node: _node, ...props }) => (
  <div className="markdown-table-scroll">
    <table {...props} />
  </div>
);

const HtmlAwarePre: NonNullable<Components["pre"]> = ({ node: _node, children, ...props }) => {
  if (Children.count(children) === 1) {
    const child = Children.only(children);
    if (isValidElement<{ className?: string; children?: ReactNode }>(child) && child.type === "code") {
      const code = Children.toArray(child.props.children)
        .filter((value): value is string => typeof value === "string")
        .join("");
      const document = parseLightweightHtmlDocument(child.props.className, code);
      if (document) {
        return <HtmlArtifactCard html={document.html} title={document.title} />;
      }
    }
  }

  return <pre {...props}>{children}</pre>;
};

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

type AttachmentAnalysisMap = Record<string, ToolCall[]>;

function taskAttachmentRefs(toolCall: ToolCall): string[] {
  if (toolCall.tool !== "task" && !toolCall.tool.includes("subagent")) return [];
  let searchable = toolCall.input || "";
  try {
    const parsed = JSON.parse(searchable) as Record<string, unknown>;
    searchable = String(parsed.description || parsed.prompt || searchable);
  } catch {
    // Legacy persisted calls may contain plain-text task input.
  }
  return Array.from(new Set(searchable.match(/att_[a-zA-Z0-9]+/g) || []));
}

function attachmentAnalysisMap(message: ChatMessageType): AttachmentAnalysisMap {
  const attachmentIds = new Set(
    (message.outputAttachments || []).map((attachment) => attachment.id).filter(Boolean),
  );
  const calls = [
    ...(message.toolCalls || []),
    ...(message.timeline || []).flatMap((item) => item.type === "tool" && item.toolCall ? [item.toolCall] : []),
    ...(message.segments || []).flatMap((segment) => [
      ...(segment.toolCalls || []),
      ...(segment.timeline || []).flatMap((item) => item.type === "tool" && item.toolCall ? [item.toolCall] : []),
    ]),
  ];
  const uniqueCalls = Array.from(new Map(calls.map((call) => [call.id, call])).values());
  const result: AttachmentAnalysisMap = {};
  for (const call of uniqueCalls) {
    for (const ref of taskAttachmentRefs(call)) {
      if (!attachmentIds.has(ref)) continue;
      result[ref] = [...(result[ref] || []), call];
    }
  }
  return result;
}

function withoutEmbeddedAttachmentAnalysis(
  timeline: TimelineItem[] = [],
  analysisByAttachmentId: AttachmentAnalysisMap,
): TimelineItem[] {
  const embeddedToolIds = new Set(
    Object.values(analysisByAttachmentId).flatMap((calls) => calls.map((call) => call.id)),
  );
  return timeline.filter(
    (item) => item.type !== "tool" || !item.toolCall || !embeddedToolIds.has(item.toolCall.id),
  );
}

export default function ChatMessage({ message, sessionSources = [], isStreaming = false, showInterruptionNotice = false }: Props) {
  const isUser = message.role === "user";
  const hasAuthError = !isUser && isAuthError(message.content);
  const renderedContent = renderCitationMarkers(message, sessionSources);
  const availableSources = mergeSources(message.sources, sessionSources);
  const { sessionId, setActiveSourceId, setInspectorOpen, closeAttachmentPreview } = useApp();
  // Persisted turns can carry both a message-level timeline and per-model-call
  // segment timelines.  Neither representation is guaranteed to be a strict
  // superset of the other (large managed-tool results in particular may only
  // survive in one of them), so confirmation UI must inspect both.  De-dupe
  // by timeline/tool id to avoid rendering the same plan batch twice.
  const skillPlanTimeline = Array.from(
    new Map(
      [
        ...(message.timeline || []),
        ...(message.segments?.flatMap((segment) => segment.timeline || []) || []),
      ].map((item, index) => [
        item.type === "tool"
          ? `tool:${item.toolCall?.id || item.id || index}`
          : `${item.type}:${item.id || index}`,
        item,
      ]),
    ).values(),
  );
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
    (request) => (request.status || "pending") === "pending"
  );
  const visibleUserInputRequests = (message.userInputRequests || []).filter(
    (request) => (request.status || "pending") === "pending"
  );
  const visibleSkillSecretRequests = (message.skillSecretRequests || []).filter(
    (request) => (request.status || "pending") === "pending"
  );
  const pendingKernelFallbackRequests = (message.kernelFallbackRequests || []).filter(
    (request) => (request.status || "pending") === "pending"
  );
  const outputAttachmentPlacement = message.segments?.length
    ? placeOutputAttachments(message.outputAttachments, message.segments, message.toolCalls)
    : { bySegment: [], unplaced: message.outputAttachments || [] };
  const analysisByAttachmentId = attachmentAnalysisMap(message);

  const citationComponents: Components = {
    a: (props) => (
      <CitationLink
        {...props}
        sessionId={sessionId}
        sources={availableSources}
        onActivate={(sourceId) => {
          closeAttachmentPreview();
          setActiveSourceId(sourceId);
          setInspectorOpen(true);
        }}
      />
    ),
    img: SafeMarkdownImage,
    table: ScrollableMarkdownTable,
    ...(!isStreaming ? { pre: HtmlAwarePre } : {}),
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
              <details open>
                  <summary className="hidden" />
              {message.segments && message.segments.length > 0 ? (
                /* Multi-segment agent turn: each model invocation is its own block */
                <div className="space-y-4">
                  {message.segments.map((segment, index) => (
                    <SegmentBlock
                      key={`${message.id}-seg-${index}`}
                      segment={segment}
                      message={message}
                      sessionSources={sessionSources}
                      isStreaming={isStreaming}
                      isLast={index === message.segments!.length - 1}
                      verificationSummary={index === message.segments!.length - 1 ? message.verificationSummary : undefined}
                      outputAttachments={outputAttachmentPlacement.bySegment[index]}
                      analysisByAttachmentId={analysisByAttachmentId}
                    />
                  ))}
                  {outputAttachmentPlacement.unplaced.length ? (
                    <AssistantAttachmentList
                      attachments={outputAttachmentPlacement.unplaced}
                      analysisByAttachmentId={analysisByAttachmentId}
                    />
                  ) : null}
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
                  {visibleUserInputRequests.map((request) => (
                    <UserInputRequestCard key={request.id} request={request} sessionId={sessionId} />
                  ))}
                  {visibleSkillSecretRequests.map((request) => (
                    <SkillSecretRequestCard key={request.id} request={request} sessionId={sessionId} />
                  ))}
                  {pendingKernelFallbackRequests.map((request) => (
                    <KernelFallbackRequestCard key={request.id} request={request} sessionId={sessionId} />
                  ))}
                  {/* Skill plans are direct frontend-to-backend actions, not HITL
                      interruptions. Keep them once at the bottom of the whole turn
                      so later model segments can never render below the card. */}
                  <SkillPlanCards timeline={skillPlanTimeline} sessionId={sessionId} />
                  {(message.segments.length > 0 || pendingPermissionRequests.length > 0 || pendingDimensionBuildRuleRequests.length > 0 || pendingLogicalDatasetRuleRequests.length > 0 || pendingDatabaseSqlRevisionRequests.length > 0 || visibleUserInputRequests.length > 0 || visibleSkillSecretRequests.length > 0 || pendingKernelFallbackRequests.length > 0) && (
                    <div className="text-[10px] text-gray-400 mt-1 pl-1">
                      {formatTime(message.timestamp)}
                    </div>
                  )}
                </div>
              ) : (
                <>
                  {(() => {
                    const displayTimeline = withoutEmbeddedAttachmentAnalysis(
                      message.timeline || [],
                      analysisByAttachmentId,
                    );
                    const hasTools = displayTimeline.some((item) => item.type === "tool");

                    const thoughtChain = displayTimeline.length > 0 ? (
                      <TimelineWithManagedAuthorization
                        timeline={displayTimeline}
                        isStreaming={isStreaming}
                      />
                      ) : message.reasoning ? (
                      <ReasoningBlock
                          content={message.reasoning}
                          defaultOpen={isStreaming && !message.content}
                          isStreaming={isStreaming && !message.content}
                        />
                      ) : null;

                    const contentBlock = hasAuthError ? (
                      <AuthErrorAlert content={message.content} />
                    ) : message.content || message.verificationSummary ? (
                      <div>
                        <div className="px-1 py-1 text-[15px] leading-relaxed">
                          <div className="markdown-content">
                            {message.content ? (
                              <ReactMarkdown
                                remarkPlugins={markdownRemarkPlugins}
                                components={citationComponents}
                                urlTransform={markdownUrlTransform}
                              >
                                {renderedContent}
                              </ReactMarkdown>
                            ) : null}
                            <VerificationSummaryText text={message.verificationSummary} />
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
                        {outputAttachmentPlacement.unplaced.length ? (
                          <AssistantAttachmentList
                            attachments={outputAttachmentPlacement.unplaced}
                            analysisByAttachmentId={analysisByAttachmentId}
                          />
                        ) : null}
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
                        {visibleUserInputRequests.map((request) => (
                          <UserInputRequestCard key={request.id} request={request} sessionId={sessionId} />
                        ))}
                        {visibleSkillSecretRequests.map((request) => (
                          <SkillSecretRequestCard key={request.id} request={request} sessionId={sessionId} />
                        ))}
                        {pendingKernelFallbackRequests.map((request) => (
                          <KernelFallbackRequestCard key={request.id} request={request} sessionId={sessionId} />
                        ))}
                        <SkillPlanCards timeline={skillPlanTimeline} sessionId={sessionId} />
                        {((message.content || thoughtChain) || pendingPermissionRequests.length > 0 || pendingDimensionBuildRuleRequests.length > 0 || pendingLogicalDatasetRuleRequests.length > 0 || pendingDatabaseSqlRevisionRequests.length > 0 || visibleUserInputRequests.length > 0 || visibleSkillSecretRequests.length > 0 || pendingKernelFallbackRequests.length > 0) && (
                          <div className="text-[10px] text-gray-400 mt-1 pl-1">
                            {formatTime(message.timestamp)}
                          </div>
                        )}
                      </>
                    );
                  })()}
                </>
              )}
                </details>

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

function SafeMarkdownImage({ alt }: React.ImgHTMLAttributes<HTMLImageElement>) {
  return (
    <span className="my-2 inline-flex max-w-full items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[12px] text-slate-600">
      <ImageIcon className="h-4 w-4 shrink-0" />
      <span className="truncate">{alt || "图片"}（请通过附件预览）</span>
    </span>
  );
}

function InlineImageAttachment({
  attachment,
  align = "left",
  analysisTools = [],
}: {
  attachment: AgentAttachment & { id: string; preview_url: string };
  align?: "left" | "right";
  analysisTools?: ToolCall[];
}) {
  const { openAttachmentPreview } = useApp();
  const [failed, setFailed] = useState(false);
  const isQr = isQrImageAttachment(attachment);
  const label = attachment.name || "图片附件";
  const analysisRunning = analysisTools.some((tool) => tool.status === "running");
  const analysisFailed = analysisTools.some((tool) => Boolean(tool.is_error));
  const AnalysisIcon = analysisRunning ? Loader2 : analysisFailed ? XCircle : CheckCircle2;
  const analysisLabel = analysisRunning
    ? "子代理正在分析图片"
    : analysisFailed
      ? "子代理图片分析未完成"
      : `子代理图片分析已完成${analysisTools.length > 1 ? ` · ${analysisTools.length} 次核对` : ""}`;

  if (failed) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[12px] text-slate-600">
        <ImageIcon className="h-4 w-4" />
        <span className="truncate">{label} · 预览失败</span>
      </div>
    );
  }

  return (
    <button
      type="button"
      data-attachment-id={attachment.id}
      onClick={() => openAttachmentPreview(attachment.id)}
      className={`group relative block overflow-hidden rounded-2xl border border-slate-200 bg-white text-left shadow-sm transition hover:border-[#002fa7]/35 hover:shadow-md focus:outline-none focus:ring-2 focus:ring-[#002fa7]/30 ${
        isQr ? "w-[220px] max-w-full p-3" : "w-full max-w-[560px]"
      } ${align === "right" ? "ml-auto" : ""}`}
      aria-label={`打开图片预览：${label}`}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={attachment.preview_url}
        alt={label}
        onError={() => setFailed(true)}
        className={`block w-full object-contain ${isQr ? "aspect-square bg-white" : "max-h-[360px] bg-slate-50"}`}
      />
      <span className="absolute right-2 top-2 flex h-8 w-8 items-center justify-center rounded-full bg-slate-950/65 text-white opacity-0 shadow-sm backdrop-blur-sm transition group-hover:opacity-100 group-focus:opacity-100">
        <Maximize2 className="h-3.5 w-3.5" />
      </span>
      <span className="block truncate border-t border-slate-100 px-3 py-2 text-[11px] font-medium text-slate-700">
        {label}
      </span>
      {analysisTools.length > 0 ? (
        <span className="flex items-center gap-1.5 border-t border-slate-100 bg-slate-50/80 px-3 py-2 text-[11px] text-slate-600">
          <AnalysisIcon
            className={`h-3.5 w-3.5 shrink-0 ${
              analysisRunning
                ? "animate-spin text-[#002fa7]"
                : analysisFailed
                  ? "text-rose-500"
                  : "text-emerald-600"
            }`}
          />
          <span className="truncate">{analysisLabel}</span>
        </span>
      ) : null}
    </button>
  );
}

function UserAttachmentList({ attachments }: { attachments: AgentAttachment[] }) {
  return (
    <div className="mt-2 flex max-w-xl flex-wrap justify-end gap-2">
      {attachments.map((attachment, index) => {
        if (isPreviewableImageAttachment(attachment)) {
          return (
            <InlineImageAttachment
              key={`${attachment.id}-${index}`}
              attachment={attachment}
              align="right"
            />
          );
        }
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

function formatAttachmentSize(size?: number): string {
  if (!size || size < 1) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function AssistantAttachmentList({
  attachments,
  analysisByAttachmentId = {},
}: {
  attachments: AgentAttachment[];
  analysisByAttachmentId?: AttachmentAnalysisMap;
}) {
  const images = attachments.filter(isPreviewableImageAttachment);
  const files = attachments.filter((attachment) => !isPreviewableImageAttachment(attachment));
  return (
    <div className="mt-3 flex max-w-[680px] flex-col gap-2">
      {images.map((attachment, index) => (
        <InlineImageAttachment
          key={`${attachment.id}-${index}`}
          attachment={attachment}
          analysisTools={analysisByAttachmentId[attachment.id] || []}
        />
      ))}
      {files.map((attachment, index) => {
        const Icon = attachment.type === "spreadsheet" ? FileSpreadsheet : FileText;
        const href = attachment.download_url || "";
        const content = (
          <>
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[#002fa7]/10 text-[#002fa7]">
              <Icon className="h-4.5 w-4.5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-semibold text-slate-900">
                {attachment.name || attachment.id || "生成附件"}
              </div>
              <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10px] text-slate-500">
                <span>已生成附件</span>
                {formatAttachmentSize(attachment.size) ? <span>{formatAttachmentSize(attachment.size)}</span> : null}
                {attachment.derived_from ? <span>源自上传文件</span> : null}
              </div>
            </div>
            <Download className="h-4 w-4 shrink-0 text-[#002fa7]" />
          </>
        );
        const className = "flex w-full items-center gap-3 rounded-2xl border border-[#002fa7]/15 bg-[#f7f9ff] px-3 py-2.5 text-left shadow-sm transition hover:border-[#002fa7]/30 hover:bg-[#f1f5ff]";
        return href ? (
          <a
            key={`${attachment.id || attachment.name || "output"}-${index}`}
            className={className}
            href={href}
            download={attachment.name || true}
          >
            {content}
          </a>
        ) : (
          <div key={`${attachment.id || attachment.name || "output"}-${index}`} className={className}>
            {content}
          </div>
        );
      })}
    </div>
  );
}

function InterruptionNotice({ text }: { text: string }) {
  return (
    <div className="mt-3 flex w-full max-w-[820px] items-start gap-2 rounded-xl border border-amber-200 bg-amber-50/80 px-3 py-2.5 text-[12px] font-medium leading-relaxed text-amber-800 shadow-sm shadow-amber-900/[0.03]">
      <PauseCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
      <span className="min-w-0 break-words">{text}</span>
    </div>
  );
}

function ErrorNotice({ text }: { text: string }) {
  return (
    <div className="mt-3 flex w-full max-w-[820px] items-start gap-2 rounded-xl border border-rose-200 bg-rose-50/85 px-3 py-2.5 text-[12px] font-medium leading-relaxed text-rose-800 shadow-sm shadow-rose-900/[0.03]">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-600" />
      <span className="min-w-0 break-words">{text}</span>
    </div>
  );
}

function KernelFallbackRequestCard({
  request,
  sessionId,
}: {
  request: KernelFallbackRequest;
  sessionId: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resolve = async (action: "switch_project_to_spawn" | "fallback_once" | "reject") => {
    setBusy(true);
    setError(null);
    try {
      await resolveKernelFallbackRequest(sessionId, request.id, request.version, action);
    } catch (err) {
      setError(err instanceof Error ? err.message : "处理回退请求失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="my-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-950">
      <div className="flex items-center gap-2 font-medium">
        <AlertTriangle className="h-4 w-4" /> Kernel 沙箱当前不可用
      </div>
      <p className="mt-2 text-xs leading-relaxed text-amber-900">
        {request.reason}。宿主执行（spawn）没有 OS 沙箱边界，请明确选择回退范围。
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {request.project_id ? (
          <button disabled={busy} onClick={() => void resolve("switch_project_to_spawn")} className="rounded-lg bg-amber-700 px-3 py-2 text-xs font-medium text-white disabled:opacity-50">
            本项目以后使用宿主执行
          </button>
        ) : null}
        <button disabled={busy} onClick={() => void resolve("fallback_once")} className="rounded-lg border border-amber-300 bg-white px-3 py-2 text-xs font-medium disabled:opacity-50">
          仅本次 Run 回退
        </button>
        <button disabled={busy} onClick={() => void resolve("reject")} className="rounded-lg border border-amber-300 px-3 py-2 text-xs disabled:opacity-50">
          拒绝
        </button>
      </div>
      {error ? <p className="mt-2 text-xs text-red-700">{error}</p> : null}
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
  const isDirectory = request.type.startsWith("external_directory_");
  const isWrite = request.type === "external_file_write" || request.type === "external_directory_write";
  const isDelete = request.type === "external_file_delete";

  const grant = async (
    targetKind: "exact_file" | "exact_directory" | "all_external_files",
    scope?: "run" | "session",
  ) => {
    setStatus("loading");
    setError("");
    try {
      await grantExternalFilePermission(
        sessionId,
        targetKind,
        targetKind === "all_external_files" ? undefined : path,
        request.id,
        scope,
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
        `User denied external ${isDirectory ? "directory" : "file"} ${isDelete ? "delete" : isWrite ? "write" : "read"} permission.`
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
          {status === "granted"
            ? <CheckCircle2 className="h-5 w-5" />
            : isDirectory ? <FolderOpen className="h-5 w-5" /> : <KeyRound className="h-5 w-5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[15px] font-bold text-slate-950">
              {isDelete
                ? "允许删除此外部文件"
                : isWrite
                  ? `允许修改外部${isDirectory ? "目录" : "文件"}`
                  : `允许读取外部${isDirectory ? "目录" : "文件"}`}
            </h3>
            {status === "granted" ? (
              <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                已授权
              </span>
            ) : null}
          </div>
          <div className="mt-2 flex items-start gap-2 rounded-xl bg-slate-50 px-3 py-2">
            {isDirectory
              ? <FolderOpen className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
              : <FileText className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />}
            <div className="min-w-0">
              <div className="truncate text-[13px] font-medium text-slate-800">{name}</div>
              <div className="mt-0.5 truncate font-mono text-[11px] text-slate-500">{path}</div>
            </div>
          </div>
          {(isWrite || isDelete) && request.change_preview ? (
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
          {isDirectory ? (
            <div className="mt-2 rounded-xl border border-sky-100 bg-sky-50/70 px-3 py-2 text-[11px] leading-relaxed text-sky-800">
              此授权只开放 HostFileBroker 文件能力，不会把宿主目录挂载进命令容器，也不会授予 shell 访问；
              {isWrite
                ? " 可在此目录内创建、修改和删除普通文件；递归或批量删除仍需单独确认。"
                : " 只允许读取和搜索，不会修改原目录，也不会扩展到父目录或相邻目录。"}
            </div>
          ) : null}
          {status !== "granted" && status !== "denied" ? (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {isDirectory ? (
                <>
                  <button
                    type="button"
                    disabled={status === "loading"}
                    onClick={() => grant("exact_directory", "session")}
                    className="rounded-full bg-[#002fa7] px-3.5 py-2 text-[12px] font-semibold text-white shadow-sm transition hover:bg-[#00298f] disabled:cursor-default disabled:opacity-60"
                  >
                    {isWrite ? "本 Session 允许修改此目录" : "本 Session 允许读取此目录"}
                  </button>
                  <button
                    type="button"
                    disabled={status === "loading"}
                    onClick={() => grant("exact_directory", "run")}
                    className="rounded-full bg-white px-3.5 py-2 text-[12px] font-semibold text-slate-700 shadow-sm ring-1 ring-black/[0.08] transition hover:bg-slate-50 disabled:cursor-default disabled:opacity-60"
                  >
                    仅本次 Run
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  disabled={status === "loading"}
                  onClick={() => grant("exact_file")}
                  className="rounded-full bg-[#002fa7] px-3.5 py-2 text-[12px] font-semibold text-white shadow-sm transition hover:bg-[#00298f] disabled:cursor-default disabled:opacity-60"
                >
                  {isDelete ? "确认删除此文件" : isWrite ? "允许修改此文件" : "允许此文件"}
                </button>
              )}
              {!isWrite && !isDelete && !isDirectory ? (
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
  if (request.type === "shell_directory_access") {
    return <ShellDirectoryPermissionCard request={request} sessionId={sessionId} />;
  }
  return <ExternalFilePermissionCard request={request} sessionId={sessionId} />;
}

function ShellDirectoryPermissionCard({
  request,
  sessionId,
}: {
  request: PermissionRequest;
  sessionId: string;
}) {
  const [status, setStatus] = useState<"idle" | "loading" | "granted" | "denied" | "error">("idle");
  const [error, setError] = useState("");
  const specs = request.grant_specs || [];
  const directories = Array.from(new Set((request.paths || specs.map((spec) => spec.target)).filter(Boolean)));
  const permissionsFor = (target: string) => {
    const targetSpecs = specs.filter((spec) => spec.target === target);
    const writable = targetSpecs.some((spec) => spec.access === "write");
    const deletable = targetSpecs.some((spec) => spec.delete);
    return deletable ? "读取、修改和删除" : writable ? "读取和修改" : "只读";
  };

  const grant = async (scope: "run" | "session") => {
    setStatus("loading");
    setError("");
    try {
      await grantShellDirectoryPermission(sessionId, request.id, scope);
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
      await denyPermissionRequest(sessionId, request.id, "User denied shell directory access.");
      setStatus("denied");
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "拒绝失败");
    }
  };

  return (
    <div className="mb-3 max-w-[680px] rounded-2xl border border-sky-200 bg-white/90 p-4 shadow-sm">
      <div className="flex gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-sky-100 text-sky-700">
          {status === "granted" ? <CheckCircle2 className="h-5 w-5" /> : <FolderOpen className="h-5 w-5" />}
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="text-[15px] font-bold text-slate-950">允许终端访问这些目录</h3>
          <p className="mt-1 text-[11px] leading-relaxed text-slate-600">
            此授权会直接开放给当前沙箱中的标准 shell 命令；只包含下列目录，不会扩展到父目录或其他已授权目录。
          </p>
          {request.command ? (
            <pre className="mt-2 overflow-auto rounded-xl bg-slate-950 px-3 py-2 text-[11px] text-slate-100">{request.command}</pre>
          ) : null}
          <div className="mt-2 space-y-1.5">
            {directories.map((path) => (
              <div key={path} className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2">
                <div className="font-mono text-[11px] text-slate-700">{path}</div>
                <div className="mt-0.5 text-[10px] font-semibold text-sky-700">{permissionsFor(path)}</div>
              </div>
            ))}
          </div>
          {status === "idle" || status === "error" ? (
            <div className="mt-3 flex flex-wrap gap-2">
              <button type="button" onClick={() => grant("run")} className="rounded-full bg-[#002fa7] px-3.5 py-2 text-[12px] font-semibold text-white">
                仅本次 Run
              </button>
              <button type="button" onClick={() => grant("session")} className="rounded-full bg-white px-3.5 py-2 text-[12px] font-semibold text-slate-700 ring-1 ring-black/[0.08]">
                本 Session 允许
              </button>
              <button type="button" onClick={deny} className="rounded-full px-3 py-2 text-[12px] font-semibold text-slate-500">
                拒绝
              </button>
            </div>
          ) : null}
          {status === "loading" ? <div className="mt-2 text-[11px] text-slate-500">处理中...</div> : null}
          {status === "granted" ? <div className="mt-2 text-[11px] text-emerald-700">已授权，命令将继续执行。</div> : null}
          {status === "denied" ? <div className="mt-2 text-[11px] text-slate-500">已拒绝</div> : null}
          {status === "error" ? <div className="mt-2 text-[11px] text-rose-600">{error}</div> : null}
        </div>
      </div>
    </div>
  );
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
  const needsTemporaryNetwork = (request.capabilities || []).includes("temporary_network");
  const needsNetwork = needsTemporaryNetwork || (request.capabilities || []).includes("network_access");
  const installsPackages = (request.capabilities || []).includes("package_install");
  const writesWorkspace = (request.capabilities || []).includes("managed_write");
  const writesSkills = (request.capabilities || []).includes("managed_skill_write");
  const opensSessionScope = (request.options || []).includes("session")
    && Boolean(request.session_target_kind && request.session_target);
  const opensProjectScope = (request.options || []).includes("project")
    && request.session_target_kind === "command_pattern"
    && Boolean(request.session_target);
  const reason = request.reason || "需要人工确认";
  const managesSkills = writesSkills || [
    "prepare_skill_install",
    "prepare_skill_update",
    "install_skill",
    "update_skill",
  ].includes(request.tool_name || "")
    || reason.startsWith("managed_skill_source_download:")
    || request.change_preview?.action === "prepare_install";
  const riskLabel = ({
    high: "脚本执行 · 需确认",
    network: "联网 · 需确认",
    package_install: "安装依赖 · 需确认",
    managed_write: "写入项目 · 需确认",
    destructive_write: "破坏性写入 · 需确认",
    managed_skill_write: "安装或更新 Skill · 需确认",
    critical: "禁止级风险",
  } as Record<string, string>)[request.risk || ""] || request.risk || "受控操作";
  const reasonLabel = request.policy_explanation || (reason.startsWith("arbitrary_interpreter:")
    ? "解释器可执行任意代码；Harness 会另外标明本次是否联网、写入或安装依赖。"
    : reason.startsWith("network_access:")
      ? "该命令需要访问互联网。"
      : reason.startsWith("package_management")
        ? "该操作会下载并安装运行时依赖。"
        : reason.startsWith("skill_source_download") || reason.startsWith("managed_skill_source_download:")
          ? "该操作会联网下载 Skill 到隔离暂存区，并校验文件和来源；不会修改已安装 Skill。"
        : reason.startsWith("managed_skill_write")
          ? "该操作会提交已校验的不可变计划到受管 Skill 目录。授权仅对本次计划有效。"
          : reason.startsWith("managed_workspace_write")
            ? "该命令会修改项目目录。"
          : `Harness 规则：${reason}`);
  const reviewedBySmartPolicy = request.policy_source === "codex_grok_smart_reviewer";
  const title = isFetchUrl
    ? "允许访问网站"
    : isSearch
      ? "允许联网搜索"
      : installsPackages
        ? "允许在沙箱中安装依赖"
        : managesSkills
          ? request.tool_name === "prepare_skill_update"
            ? "允许检查 Skill 更新"
            : request.tool_name === "prepare_skill_install"
              ? "允许准备安装 Skill"
              : request.tool_name === "update_skill" ? "允许更新 Skill" : "允许安装 Skill"
        : needsNetwork
          ? "允许命令联网执行"
        : "允许执行受控命令";

  const grant = async (scope: "once" | "session" | "project") => {
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
            : isNetworkTool || needsNetwork
              ? <Globe2 className="h-5 w-5" />
              : <SquareTerminal className="h-5 w-5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[15px] font-bold text-slate-950">{title}</h3>
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
              {riskLabel}
            </span>
            {reviewedBySmartPolicy ? (
              <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-semibold text-blue-800">
                智能审查后需确认
              </span>
            ) : null}
          </div>
          <p className="mt-1 text-[12px] text-slate-500">
            {reasonLabel}
          </p>
          {needsNetwork ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-semibold text-blue-800">
                {needsTemporaryNetwork ? "临时联网" : "联网执行"}
              </span>
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-600">
                {needsTemporaryNetwork ? "命令结束后自动断开" : "仍受当前 Backend 网络策略约束"}
              </span>
            </div>
          ) : null}
          {writesWorkspace || writesSkills || installsPackages ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {writesWorkspace ? (
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                  写入项目
                </span>
              ) : null}
              {writesSkills ? (
                <span className="rounded-full bg-cyan-100 px-2 py-0.5 text-[10px] font-semibold text-cyan-800">
                  写入受管 Skill
                </span>
              ) : null}
              {installsPackages ? (
                <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold text-violet-800">
                  安装依赖
                </span>
              ) : null}
            </div>
          ) : null}
          {managesSkills && request.change_preview ? (
            <div className="mt-3 rounded-xl border border-cyan-200 bg-white/80 p-3 text-[12px] text-slate-700">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-bold text-slate-950">
                  {request.change_preview.skill_name || "Skill"}
                  {request.change_preview.version ? ` · ${request.change_preview.version}` : ""}
                </span>
                <span className="rounded-full bg-cyan-100 px-2 py-0.5 font-semibold text-cyan-800">
                  {request.change_preview.changes || (writesSkills ? "已校验变更" : "下载并校验")}
                </span>
              </div>
              {request.change_preview.source ? (
                <p className="mt-2 break-all font-mono text-[10.5px] text-slate-500">{request.change_preview.source}</p>
              ) : null}
              {["added", "changed", "removed"].map((key) => request.change_preview?.[key] ? (
                <p key={key} className="mt-1 break-all">
                  <span className="mr-1 font-semibold text-slate-500">
                    {key === "added" ? "新增" : key === "changed" ? "修改" : "删除"}：
                  </span>
                  {request.change_preview[key]}
                </p>
              ) : null)}
            </div>
          ) : null}
          {managesSkills ? (
            <details className="group mt-3 rounded-xl border border-black/[0.06] bg-white/60 px-3 py-2">
              <summary className="cursor-pointer select-none text-[11px] font-semibold text-slate-500 marker:text-slate-300">
                技术详情
              </summary>
              {request.change_preview?.plan_sha256 ? (
                <p className="mt-2 break-all font-mono text-[10px] text-slate-400">
                  Plan SHA-256: {request.change_preview.plan_sha256}
                </p>
              ) : null}
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950 px-3 py-2.5 font-mono text-[11px] leading-5 text-slate-100">
                {request.command || ""}
              </pre>
            </details>
          ) : (
            <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-slate-950 px-3 py-2.5 font-mono text-[12px] leading-5 text-slate-100">
              {request.command || ""}
            </pre>
          )}
          {status === "idle" || status === "error" ? (
            <div className="mt-3 flex flex-wrap gap-2">
              {opensProjectScope ? (
                <button
                  type="button"
                  onClick={() => void grant("project")}
                  className="rounded-full bg-violet-700 px-3.5 py-2 text-[12px] font-semibold text-white hover:bg-violet-800"
                >
                  记住到本项目
                </button>
              ) : null}
              {opensSessionScope ? (
                <button
                  type="button"
                  onClick={() => void grant("session")}
                  className="rounded-full bg-[#002fa7] px-3.5 py-2 text-[12px] font-semibold text-white hover:bg-[#00298f]"
                >
                  {request.session_scope_label || "本 Session 允许联网"}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void grant("once")}
                  className="rounded-full bg-[#002fa7] px-3.5 py-2 text-[12px] font-semibold text-white hover:bg-[#00298f]"
                >
                  仅允许本次
                </button>
              )}
              {!managesSkills && opensSessionScope ? (
                <button
                  type="button"
                  onClick={() => void grant("once")}
                  className="rounded-full bg-white px-3.5 py-2 text-[12px] font-semibold text-slate-700 ring-1 ring-black/[0.08] hover:bg-slate-50"
                >
                  仅允许本次
                </button>
              ) : null}
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

function SkillSecretRequestCard({ request, sessionId }: { request: SkillSecretRequest; sessionId: string }) {
  const [value, setValue] = useState("");
  const [status, setStatus] = useState<"idle" | "loading" | "done" | "cancelled" | "error">("idle");
  const [error, setError] = useState("");
  const submittingRef = useRef(false);

  const resolve = async (action: "configure" | "reuse" | "cancel") => {
    if (submittingRef.current) return;
    if (action === "configure" && !value) {
      setError("请输入凭证值。");
      return;
    }
    submittingRef.current = true;
    setStatus("loading");
    setError("");
    try {
      await resolveSkillSecretRequest(sessionId, request.id, {
        request_version: request.version,
        action,
        ...(action === "configure" ? { secret_value: value } : {}),
      });
      setValue("");
      setStatus(action === "cancel" ? "cancelled" : "done");
    } catch (nextError) {
      submittingRef.current = false;
      setStatus("error");
      setError(nextError instanceof Error ? nextError.message : "配置失败，请重试");
    }
  };

  if (status === "done" || request.decision?.action === "configured") {
    return <div className="mb-3 inline-flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800" role="status"><CheckCircle2 className="h-4 w-4" />已安全配置 {request.env_name}，Agent 将继续执行。</div>;
  }
  if (status === "cancelled" || request.decision?.action === "cancel") {
    return <div className="mb-3 inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-600" role="status"><XCircle className="h-4 w-4" />已取消凭证配置。</div>;
  }

  const reuse = request.mode === "reuse";
  return (
    <form
      className="mb-4 max-w-[820px] rounded-2xl border border-amber-200 bg-white p-5 shadow-sm shadow-amber-950/[0.04]"
      onSubmit={(event) => { event.preventDefault(); void resolve(reuse ? "reuse" : "configure"); }}
    >
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-amber-50 text-amber-700"><KeyRound className="h-5 w-5" /></div>
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-amber-700">安全凭证输入</p>
          <h3 className="mt-1 text-base font-bold text-slate-950">为 {request.skill_id} 配置 {request.env_name}</h3>
          <p className="mt-1 text-sm leading-6 text-slate-500">{request.reason}</p>
          <p className="mt-1 text-xs leading-5 text-slate-400">值不会发送给 Agent，也不会出现在命令、消息或执行日志中。</p>
        </div>
      </div>
      {!reuse ? (
        <label className="mt-4 block text-sm font-semibold text-slate-700">
          {request.env_name}
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={value}
            disabled={status === "loading"}
            onChange={(event) => setValue(event.target.value)}
            className="mt-2 w-full rounded-xl border border-slate-200 px-3 py-2 font-mono text-sm outline-none focus:border-amber-500 focus:ring-2 focus:ring-amber-100"
          />
        </label>
      ) : (
        <p className="mt-4 rounded-xl bg-amber-50 px-3 py-2 text-sm text-amber-800">这个变量已有保存值。确认后只为当前版本的 {request.skill_id} 建立使用绑定，不会向 Agent 展示值。</p>
      )}
      {error ? <p className="mt-2 text-xs text-rose-600">{error}</p> : null}
      <div className="mt-4 flex items-center gap-2">
        <button type="submit" disabled={status === "loading"} className="rounded-full bg-[#002fa7] px-4 py-2 text-xs font-semibold text-white disabled:opacity-50">
          {status === "loading" ? "处理中..." : reuse ? "允许当前 Skill 使用" : "保存并继续"}
        </button>
        <button type="button" disabled={status === "loading"} onClick={() => void resolve("cancel")} className="rounded-full px-3 py-2 text-xs font-semibold text-slate-500 hover:bg-slate-50">取消</button>
      </div>
    </form>
  );
}

function UserInputRequestCard({ request, sessionId }: { request: UserInputRequest; sessionId: string }) {
  type Draft = { optionIds: string[]; text: string };
  const draftStorageKey = `puddingclaw:user-input-draft:${sessionId}:${request.id}:v${request.version}`;
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() => {
    if (typeof window !== "undefined") {
      try {
        const saved = window.sessionStorage.getItem(draftStorageKey);
        if (saved) return JSON.parse(saved) as Record<string, Draft>;
      } catch {
        // Fall through to an empty form if storage is unavailable/corrupt.
      }
    }
    return Object.fromEntries(
      request.questions.map((question) => [question.id, { optionIds: [], text: "" }]),
    );
  });
  const [status, setStatus] = useState<"idle" | "loading" | "submitted" | "cancelled" | "decided" | "error">("idle");
  const [error, setError] = useState("");
  const submittingRef = useRef(false);

  useEffect(() => {
    try {
      if (request.status === "pending") {
        window.sessionStorage.setItem(draftStorageKey, JSON.stringify(drafts));
      } else {
        window.sessionStorage.removeItem(draftStorageKey);
      }
    } catch {
      // Draft persistence is best-effort; server validation remains authoritative.
    }
  }, [draftStorageKey, drafts, request.status]);

  const updateChoice = (questionId: string, optionId: string, multiple: boolean) => {
    setDrafts((current) => {
      const draft = current[questionId] || { optionIds: [], text: "" };
      const optionIds = multiple
        ? draft.optionIds.includes(optionId)
          ? draft.optionIds.filter((item) => item !== optionId)
          : [...draft.optionIds, optionId]
        : [optionId];
      return {
        ...current,
        [questionId]: { ...draft, optionIds, text: multiple ? draft.text : "" },
      };
    });
  };

  if (request.status !== "pending" && request.decision) {
    const action = request.decision.action;
    const answerLabels = (request.decision.answers || []).flatMap((answer) => {
      const question = request.questions.find((item) => item.id === answer.question_id);
      const selected = answer.option_ids.map((optionId) =>
        question?.options?.find((option) => option.id === optionId)?.label || optionId
      );
      return [...selected, ...(answer.text ? [answer.text] : [])];
    });
    const text = action === "cancel"
      ? "已跳过这个问题，Agent 将继续执行。"
      : action === "agent_decide"
        ? "已交由 Agent 按推荐项或稳妥默认值继续。"
        : `已提交：${answerLabels.join("；") || "已确认"}`;
    const cancelled = action === "cancel";
    return <div className={`mb-3 inline-flex items-center gap-2 rounded-xl border px-3 py-2 text-sm ${cancelled ? "border-slate-200 bg-slate-50 text-slate-600" : "border-emerald-200 bg-emerald-50 text-emerald-800"}`} role="status"><CheckCircle2 className="h-4 w-4" />{text}</div>;
  }

  const validate = (): UserInputAnswer[] | null => {
    const answers = request.questions.map((question) => ({
      question_id: question.id,
      option_ids: drafts[question.id]?.optionIds || [],
      text: (drafts[question.id]?.text || "").trim(),
    }));
    for (let index = 0; index < request.questions.length; index += 1) {
      const question = request.questions[index];
      const answer = answers[index];
      const count = answer.option_ids.length + (answer.text ? 1 : 0);
      if (question.required !== false && count === 0) {
        setError(`请回答“${question.prompt}”。`);
        return null;
      }
      if (question.type === "multi_select") {
        const minimum = question.min_selections ?? (question.required === false ? 0 : 1);
        if (count < minimum) {
          setError(`“${question.prompt}”至少选择 ${minimum} 项。`);
          return null;
        }
        if (question.max_selections != null && count > question.max_selections) {
          setError(`“${question.prompt}”最多选择 ${question.max_selections} 项。`);
          return null;
        }
      }
    }
    return answers;
  };

  const resolve = async (action: "submit" | "cancel" | "agent_decide") => {
    if (submittingRef.current) return;
    const answers = action === "submit" ? validate() : [];
    if (action === "submit" && !answers) return;
    submittingRef.current = true;
    setStatus("loading");
    setError("");
    try {
      await resolveUserInputRequest(sessionId, request.id, {
        request_version: request.version,
        action,
        answers: answers || [],
      });
      setStatus(action === "submit" ? "submitted" : action === "cancel" ? "cancelled" : "decided");
    } catch (nextError) {
      submittingRef.current = false;
      setStatus("error");
      setError(nextError instanceof Error ? nextError.message : "提交选择失败，请重试");
    }
  };

  if (["submitted", "cancelled", "decided"].includes(status)) {
    const label = status === "submitted"
      ? "已提交，Agent 将继续执行。"
      : status === "decided"
        ? "已交由 Agent 采用推荐方案继续。"
        : "已跳过这个问题，Agent 将继续执行。";
    const cancelled = status === "cancelled";
    return <div className={`mb-3 inline-flex items-center gap-2 rounded-xl border px-3 py-2 text-sm ${cancelled ? "border-slate-200 bg-slate-50 text-slate-600" : "border-emerald-200 bg-emerald-50 text-emerald-800"}`} role="status"><CheckCircle2 className="h-4 w-4" />{label}</div>;
  }

  return (
    <form
      className="mb-4 max-w-[820px] rounded-2xl border border-blue-200 bg-white p-5 shadow-sm shadow-blue-950/[0.05]"
      onSubmit={(event) => { event.preventDefault(); void resolve("submit"); }}
    >
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-[#002fa7]"><HelpCircle className="h-5 w-5" /></div>
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[#002fa7]">需要你的选择</p>
          <h3 className="mt-1 text-base font-bold text-slate-950">{request.title}</h3>
          <p className="mt-1 text-sm leading-6 text-slate-500">{request.reason}</p>
        </div>
      </div>
      <div className="mt-4 space-y-4">
        {request.questions.map((question) => {
          const draft = drafts[question.id] || { optionIds: [], text: "" };
          return (
            <fieldset key={question.id} className="rounded-xl border border-slate-200 p-4" disabled={status === "loading"}>
              <legend className="px-1 text-sm font-semibold text-slate-800">{question.prompt}{question.required === false ? <span className="ml-1 font-normal text-slate-400">（可选）</span> : null}</legend>
              {question.type === "text" ? (
                <textarea
                  value={draft.text}
                  maxLength={question.max_length || 1000}
                  required={question.required !== false}
                  onChange={(event) => setDrafts((current) => ({ ...current, [question.id]: { ...draft, text: event.target.value } }))}
                  className="mt-2 min-h-24 w-full rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-[#002fa7] focus:ring-2 focus:ring-blue-100"
                />
              ) : (
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  {(question.options || []).map((option) => {
                    const checked = draft.optionIds.includes(option.id);
                    return (
                      <label key={option.id} className={`flex cursor-pointer gap-3 rounded-xl border p-3 transition ${checked ? "border-[#002fa7] bg-blue-50" : "border-slate-200 hover:border-slate-300"}`}>
                        <input
                          type={question.type === "single_select" ? "radio" : "checkbox"}
                          name={`question-${request.id}-${question.id}`}
                          value={option.id}
                          checked={checked}
                          onChange={() => updateChoice(question.id, option.id, question.type === "multi_select")}
                          className="mt-0.5 h-4 w-4 accent-[#002fa7]"
                        />
                        <span><span className="block text-sm font-semibold text-slate-800">{option.label}{option.recommended ? <span className="ml-1.5 rounded-full bg-blue-100 px-1.5 py-0.5 text-[10px] text-[#002fa7]">推荐</span> : null}</span>{option.description ? <span className="mt-0.5 block text-xs leading-5 text-slate-500">{option.description}</span> : null}</span>
                      </label>
                    );
                  })}
                </div>
              )}
              {question.type !== "text" && question.allow_other ? (
                <label className="mt-3 block text-xs font-medium text-slate-600">其他
                  <input
                    value={draft.text}
                    maxLength={question.max_length || 1000}
                    onChange={(event) => setDrafts((current) => ({
                      ...current,
                      [question.id]: {
                        ...draft,
                        optionIds: question.type === "single_select" ? [] : draft.optionIds,
                        text: event.target.value,
                      },
                    }))}
                    className="mt-1.5 h-9 w-full rounded-lg border border-slate-200 px-3 text-sm font-normal outline-none focus:border-[#002fa7]"
                  />
                </label>
              ) : null}
            </fieldset>
          );
        })}
      </div>
      {error ? <p className="mt-3 text-sm text-rose-600" role="alert">{error}</p> : null}
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <button type="button" disabled={status === "loading"} onClick={() => void resolve("cancel")} className="h-9 rounded-xl px-4 text-sm font-semibold text-slate-500 hover:bg-slate-50 disabled:opacity-50">取消</button>
        {request.allow_agent_decide !== false ? <button type="button" disabled={status === "loading"} onClick={() => void resolve("agent_decide")} className="h-9 rounded-xl border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50">由 Agent 决定</button> : null}
        <button type="submit" disabled={status === "loading"} className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-sm font-semibold text-white hover:bg-[#00247f] disabled:opacity-50">{status === "loading" ? <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" /> : <CheckCircle2 className="h-4 w-4" />}确认并继续</button>
      </div>
    </form>
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
  sessionSources,
  isStreaming,
  isLast,
  verificationSummary,
  outputAttachments,
  analysisByAttachmentId,
}: {
  segment: {
    content: string;
    reasoning?: string;
    timeline?: TimelineItem[];
  };
  message: ChatMessageType;
  sessionSources: SourceRecord[];
  isStreaming?: boolean;
  isLast?: boolean;
  verificationSummary?: string;
  outputAttachments?: AgentAttachment[];
  analysisByAttachmentId: AttachmentAnalysisMap;
}) {
  const { sessionId, setActiveSourceId, setInspectorOpen, closeAttachmentPreview } = useApp();
  // Terminal text is withheld by the backend until accepted.  Segments shown
  // here are therefore process activity or the single published response;
  // verification control state must never hide history or tool progress.
  const rendered = renderCitationMarkersForSegment(message, segment.content, sessionSources);
  const availableSources = mergeSources(message.sources, sessionSources);
  const citationComponents: Components = {
    a: (props) => (
      <CitationLink
        {...props}
        sessionId={sessionId}
        sources={availableSources}
        onActivate={(sourceId) => {
          closeAttachmentPreview();
          setActiveSourceId(sourceId);
          setInspectorOpen(true);
        }}
      />
    ),
    img: SafeMarkdownImage,
    table: ScrollableMarkdownTable,
    ...(!(isStreaming && isLast) ? { pre: HtmlAwarePre } : {}),
  };

  const displayTimeline = withoutEmbeddedAttachmentAnalysis(
    segment.timeline || [],
    analysisByAttachmentId,
  );
  const hasTools = displayTimeline.some((item) => item.type === "tool");

  const thoughtChain =
    displayTimeline.length > 0 ? (
      <TimelineWithManagedAuthorization
        timeline={displayTimeline}
        isStreaming={isStreaming && isLast}
      />
    ) : segment.reasoning ? (
      <ReasoningBlock
        content={segment.reasoning}
        defaultOpen={isStreaming && !segment.content}
        isStreaming={isStreaming && !segment.content}
      />
    ) : null;

  const contentBlock = segment.content || verificationSummary ? (
    <div className="px-1 py-1 text-[15px] leading-relaxed">
      <div className="markdown-content">
        {segment.content ? (
          <ReactMarkdown
            remarkPlugins={markdownRemarkPlugins}
            components={citationComponents}
            urlTransform={markdownUrlTransform}
          >
            {rendered}
          </ReactMarkdown>
        ) : null}
        <VerificationSummaryText text={verificationSummary} />
      </div>
    </div>
  ) : null;

  return (
    <div className="space-y-2">
      {!hasTools && thoughtChain}
      {contentBlock}
      {hasTools && thoughtChain}
      {outputAttachments?.length ? (
        <AssistantAttachmentList
          attachments={outputAttachments}
          analysisByAttachmentId={analysisByAttachmentId}
        />
      ) : null}
    </div>
  );
}

function TimelineWithManagedAuthorization({
  timeline,
  isStreaming,
}: {
  timeline: TimelineItem[];
  isStreaming?: boolean;
}) {
  const slices = splitTimelineAtManagedAuthorizations(timeline);
  return (
    <div className="space-y-2">
      {slices.map((slice, index) => (
        <div key={`${slice.timeline[0]?.id || "timeline"}-${index}`} className="space-y-2">
          {slice.timeline.length > 0 ? (
            <ThoughtChain
              timeline={slice.timeline}
              isStreaming={Boolean(isStreaming && index === slices.length - 1)}
            />
          ) : null}
          {slice.authorization ? (
            <ManagedAuthorizationCards timeline={[slice.authorization]} />
          ) : null}
        </div>
      ))}
    </div>
  );
}

function VerificationSummaryText({ text }: { text?: string }) {
  const summary = String(text || "").trim();
  if (!summary || /^验证通过[。.!！]?$/.test(summary)) return null;
  return (
    <div className="mt-5 text-slate-700">
      <ReactMarkdown
        remarkPlugins={markdownRemarkPlugins}
        components={{ img: SafeMarkdownImage, table: ScrollableMarkdownTable }}
        urlTransform={markdownUrlTransform}
      >
        {summary}
      </ReactMarkdown>
    </div>
  );
}

function renderCitationMarkersForSegment(
  message: ChatMessageType,
  content: string,
  sessionSources: SourceRecord[] = []
): string {
  const normalizedContent = sanitizeCitationMarkdown(stripModelCallLimitNotice(content));
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
  const sessionSourceIds = new Set(sessionSources.map((source) => source.source_id));
  const historicalMarkerRe = /\[\^(src_[A-Za-z0-9_-]+)\]/g;
  let historicalMatch: RegExpExecArray | null;
  while ((historicalMatch = historicalMarkerRe.exec(normalizedContent)) !== null) {
    const sourceId = historicalMatch[1];
    if (sessionSourceIds.has(sourceId) && !indexes.has(sourceId)) {
      indexes.set(sourceId, nextIndex++);
    }
  }
  if (indexes.size === 0) return normalizedContent;
  return normalizedContent.replace(/\[\^(src_[A-Za-z0-9_-]+)\]/g, (marker, sourceId: string) => {
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
        <span className="font-medium">处理过程</span>
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

function renderCitationMarkers(message: ChatMessageType, sessionSources: SourceRecord[] = []): string {
  const normalizedContent = sanitizeCitationMarkdown(stripModelCallLimitNotice(message.content));
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
  const sessionSourceIds = new Set(sessionSources.map((source) => source.source_id));
  const historicalMarkerRe = /\[\^(src_[A-Za-z0-9_-]+)\]/g;
  let historicalMatch: RegExpExecArray | null;
  while ((historicalMatch = historicalMarkerRe.exec(normalizedContent)) !== null) {
    const sourceId = historicalMatch[1];
    if (sessionSourceIds.has(sourceId) && !indexes.has(sourceId)) {
      indexes.set(sourceId, nextIndex++);
    }
  }

  if (indexes.size === 0) return normalizedContent;

  return normalizedContent.replace(/\[\^(src_[A-Za-z0-9_-]+)\]/g, (marker, sourceId: string) => {
    const index = indexes.get(sourceId);
    return index ? `[${index}](#source-${sourceId})` : marker;
  });
}

function sanitizeCitationMarkdown(content: string): string {
  return normalizeLooseStrongMarkdown(content)
    // Citation metadata belongs to the structured source cards, never a GFM
    // Footnotes appendix rendered inside the assistant answer.
    .replace(/^[ \t]*\[\^[^\]\n]+\]:[^\n]*(?:\n(?:(?: {2,}|\t)[^\n]*))*(?:\n|$)/gm, "")
    // Only structured source ids are eligible for citation rendering. SQL
    // generation ids and arbitrary model-created footnotes remain plain ids.
    .replace(/\[\^(?!src_[A-Za-z0-9_-]+\])[^\]\n]+\]/g, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function mergeSources(
  primary: SourceRecord[] | undefined,
  fallback: SourceRecord[]
): SourceRecord[] {
  const catalog = new Map<string, SourceRecord>();
  for (const source of fallback) catalog.set(source.source_id, source);
  for (const source of primary || []) {
    catalog.set(source.source_id, { ...catalog.get(source.source_id), ...source });
  }
  return Array.from(catalog.values());
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
      let localPath = href.slice("file://".length);
      try {
        localPath = decodeURIComponent(new URL(href).pathname);
      } catch {
        // Keep the original path for the existing open-file error handling.
      }
      return (
        <LocalFileAttachmentCard
          href={href}
          filePath={localPath}
          sessionId={sessionId}
        />
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
