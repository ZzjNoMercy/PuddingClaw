"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen,
  CheckCircle2,
  ChevronDown,
  Circle,
  ExternalLink,
  FileText,
  KeyRound,
  ListChecks,
  ShieldCheck,
  Timer,
  X,
} from "lucide-react";
import { listSessionPermissions, revokePermissionGrant, type PermissionGrant } from "@/lib/api";
import { useApp, type SourceRecord, type ToolCall } from "@/lib/store";

type TodoStatus = "completed" | "in_progress" | "pending";

export default function SourcesPanel() {
  const {
    messages,
    isStreaming,
    sessionId,
    todos,
    activeSourceId,
    inspectorActiveTab,
    setInspectorActiveTab,
  } = useApp();
  const [permissionGrants, setPermissionGrants] = useState<PermissionGrant[]>([]);

  const loadPermissions = React.useCallback(() => {
    listSessionPermissions(sessionId)
      .then(setPermissionGrants)
      .catch(() => setPermissionGrants([]));
  }, [sessionId]);

  useEffect(() => {
    loadPermissions();
  }, [loadPermissions]);

  useEffect(() => {
    const handler = () => loadPermissions();
    window.addEventListener("puddingclaw:permissions-changed", handler);
    return () => window.removeEventListener("puddingclaw:permissions-changed", handler);
  }, [loadPermissions]);

  // When a citation marker in the chat is clicked, activeSourceId is set and the
  // inspector opens. Make sure the drawer shows the Sources tab so the cited
  // source is visible, instead of staying on Progress.
  useEffect(() => {
    if (activeSourceId) {
      setInspectorActiveTab("sources");
    }
  }, [activeSourceId, setInspectorActiveTab]);

  const { cited, retrieved, inferredTodos } = useMemo(() => {
    const lastUserIndex = messages.findLastIndex((message) => message.role === "user");
    const turnMessages = lastUserIndex >= 0 ? messages.slice(lastUserIndex) : [];
    const sourceMap = new Map<string, SourceRecord>();
    const citationIndex = new Map<string, number>();
    const toolByCallId = new Map<string, string>();
    let latestTodos: Array<{ content: string; status: TodoStatus }> = [];
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

    // Fallback: some tools emit sources the model cited with [^source_id] markers
    // but the citations_finalized event did not include them (e.g. adapter timing
    // or source_id mismatch). Treat any marker in the rendered content that points
    // to a known source as a cited source so it does not end up under "其他检索结果".
    const markerRe = /\[\^(src_[A-Za-z0-9_-]+)\]/g;
    for (const message of turnMessages) {
      const contents = [
        message.content,
        ...(message.segments?.map((s) => s.content) || []),
      ];
      for (const content of contents) {
        if (!content) continue;
        let match;
        markerRe.lastIndex = 0;
        while ((match = markerRe.exec(content)) !== null) {
          const sourceId = match[1];
          if (sourceMap.has(sourceId) && !citationIndex.has(sourceId)) {
            const nextIndex =
              citationIndex.size > 0
                ? Math.max(...Array.from(citationIndex.values())) + 1
                : 1;
            citationIndex.set(sourceId, nextIndex);
          }
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
    return { cited: citedSources, retrieved: retrievedSources, inferredTodos: latestTodos };
  }, [messages]);

  // Prefer persisted todos; fall back to todos inferred from the current turn
  // when persistence has not been populated yet.
  const displayTodos = useMemo(
    () => (todos && todos.length > 0 ? todos : inferredTodos),
    [todos, inferredTodos]
  );

  const total = cited.length + retrieved.length;
  const hasSources = total > 0;
  const hasTodos = displayTodos.length > 0;
  return (
    <div className="h-full overflow-y-auto px-5 py-5">
      <div className="workspace-side-card overflow-hidden rounded-[28px] px-5 py-3">
        <ProgressCard
          active={inspectorActiveTab === "progress"}
          onActivate={() => setInspectorActiveTab("progress")}
          todos={displayTodos as Array<{ content: string; status: TodoStatus }>}
        />
        <PanelDivider />
        <PermissionsCard
          active={inspectorActiveTab === "permissions"}
          onActivate={() => setInspectorActiveTab("permissions")}
          grants={permissionGrants}
          onRevoke={async (grantId) => {
            await revokePermissionGrant(sessionId, grantId);
            loadPermissions();
            window.dispatchEvent(new CustomEvent("puddingclaw:permissions-changed"));
          }}
        />
        <PanelDivider />
        <SourcesCard
          active={inspectorActiveTab === "sources"}
          onActivate={() => setInspectorActiveTab("sources")}
          cited={cited}
          retrieved={retrieved}
          isStreaming={isStreaming && hasSources}
        />
      </div>
    </div>
  );
}

function PanelDivider() {
  return <div className="mx-1 h-px bg-black/[0.06]" />;
}

function SectionHeader({
  icon,
  title,
  metric,
  open,
  onToggle,
}: {
  icon: React.ReactNode;
  title: string;
  metric?: React.ReactNode;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex w-full items-center justify-between gap-3 py-4 text-left"
    >
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-black/[0.045] text-slate-500">
          {icon}
        </div>
        <span className="truncate text-[15px] font-bold text-slate-700">{title}</span>
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-slate-400 transition-transform duration-200 ${open ? "rotate-90" : ""}`}
        />
      </div>
      {metric && (
        <div className="shrink-0 text-[13px] font-semibold text-slate-500">
          {metric}
        </div>
      )}
    </button>
  );
}

function PermissionsCard({
  active,
  onActivate,
  grants,
  onRevoke,
}: {
  active: boolean;
  onActivate: () => void;
  grants: PermissionGrant[];
  onRevoke: (grantId: string) => Promise<void>;
}) {
  const [revoking, setRevoking] = useState<string | null>(null);

  return (
    <section>
      <SectionHeader
        icon={<ShieldCheck className="h-4 w-4" />}
        title="权限"
        open={active}
        onToggle={onActivate}
        metric={
          grants.length > 0 ? (
            <span className="text-[#002fa7]">{grants.length}</span>
          ) : (
            <span className="text-slate-300">0</span>
          )
        }
      />

      {active && grants.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-9 text-center">
          <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-50 text-slate-300">
            <KeyRound className="h-5 w-5" />
          </div>
          <p className="text-[14px] font-medium text-slate-400">授权信息将显示在这里</p>
        </div>
      ) : active ? (
        <div className="mt-4 space-y-3 pb-5">
          {grants.map((grant) => {
            const target = grant.target_kind === "all_external_files" ? "所有外部文件" : grant.target;
            const name =
              grant.target_kind === "all_external_files"
                ? "本 session 外部文件读取"
                : grant.target.split("/").filter(Boolean).pop() || "外部文件";
            return (
              <div key={grant.id} className="rounded-2xl border border-black/[0.06] bg-white/70 p-3">
                <div className="flex items-start gap-2.5">
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-[#002fa7]/10 text-[#002fa7]">
                    <KeyRound className="h-4 w-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0 truncate text-[13px] font-semibold text-slate-900">{name}</div>
                      <button
                        type="button"
                        disabled={revoking === grant.id}
                        onClick={async () => {
                          setRevoking(grant.id);
                          try {
                            await onRevoke(grant.id);
                          } finally {
                            setRevoking(null);
                          }
                        }}
                        className="rounded-full p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
                        aria-label="撤销权限"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                    <div className="mt-1 truncate font-mono text-[10.5px] text-slate-500">{target}</div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                        Session
                      </span>
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                        Read only
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

function ProgressCard({
  active,
  onActivate,
  todos,
}: {
  active: boolean;
  onActivate: () => void;
  todos: Array<{ content: string; status: TodoStatus }>;
}) {
  const completed = todos.filter((todo) => todo.status === "completed").length;
  const hasTodos = todos.length > 0;

  return (
    <section>
      <SectionHeader
        icon={<ListChecks className="h-4 w-4" />}
        title="进度"
        open={active}
        onToggle={onActivate}
        metric={
          hasTodos ? (
            <span>
              <span className="text-emerald-500">{completed}</span>
              <span className="text-slate-300">/{todos.length}</span>
            </span>
          ) : (
            <span className="text-slate-300">0</span>
          )
        }
      />

      {active && (
        <div className="pb-4 space-y-2.5">
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

function SourcesEmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-black/[0.045]">
        <BookOpen className="h-6 w-6 text-slate-400" />
      </div>
      <p className="text-[14px] font-medium text-slate-400">来源将显示在这里</p>
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
  active,
  onActivate,
  cited,
  retrieved,
  isStreaming,
}: {
  active: boolean;
  onActivate: () => void;
  cited: Array<{ source: SourceRecord; index?: number }>;
  retrieved: Array<{ source: SourceRecord; index?: number }>;
  isStreaming: boolean;
}) {
  const { activeSourceId } = useApp();
  const activeRef = useRef<HTMLDivElement>(null);
  const total = cited.length + retrieved.length;

  useEffect(() => {
    if (!activeSourceId) return;
    const allSources = [...cited, ...retrieved];
    if (allSources.some(({ source }) => source.source_id === activeSourceId)) {
      window.setTimeout(() => {
        activeRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 50);
    }
  }, [activeSourceId, cited, retrieved]);

  return (
    <section>
      <SectionHeader
        icon={<BookOpen className="h-4 w-4" />}
        title="来源"
        open={active}
        onToggle={onActivate}
        metric={
          isStreaming ? (
            <span className="inline-flex items-center gap-1.5 text-[#002fa7]">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#002fa7]" />
              {total}
            </span>
          ) : total > 0 ? (
            <span>{total}</span>
          ) : (
            <span className="text-slate-300">0</span>
          )}
      />

      {active && total === 0 ? (
        <SourcesEmptyState />
      ) : active ? (
        <div className="pb-4 space-y-5">
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
                />
              ))}
            </SourceSection>
          )}
        </div>
      ) : null}
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
      <h4 className="mb-2 flex items-center gap-2 text-[12px] font-semibold uppercase tracking-wide text-slate-500">
        {title}
        <span className="rounded-full bg-black/[0.045] px-1.5 py-0 text-[10px]">{count}</span>
      </h4>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

const SourceItem = React.forwardRef<HTMLDivElement, {
  source: SourceRecord;
  citationIndex?: number;
  isActive?: boolean;
}>(function SourceItem({ source, citationIndex, isActive }, ref) {
  return (
    <div
      ref={ref}
      className={`group rounded-xl border p-3 transition-colors ${
        isActive
          ? "border-[#002fa7]/30 bg-[#002fa7]/[0.04]"
          : "border-black/[0.06] bg-white hover:border-black/[0.12]"
      }`}
    >
      <div className="flex items-start gap-2.5">
        <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-black/[0.055]">
          <FileText className="h-3 w-3 text-slate-600" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            {citationIndex !== undefined && (
              <span className="inline-flex h-4 min-w-[16px] items-center justify-center rounded bg-[#002fa7]/10 px-1 text-[10px] font-bold text-[#002fa7]">
                {citationIndex}
              </span>
            )}
            <p className="truncate text-[13px] font-medium text-slate-800" title={source.title}>
              {source.title}
            </p>
          </div>
          {source.quote && (
            <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-slate-500">
              {source.quote}
            </p>
          )}
          {source.uri && (
            <a
              href={source.uri}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-flex items-center gap-1 text-[11px] text-[#002fa7] hover:underline"
            >
              查看来源
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      </div>
    </div>
  );
});

function extractTodosFromToolCall(toolCall: ToolCall): Array<{ content: string; status: TodoStatus }> {
  if (toolCall.tool !== "write_todos" || !toolCall.output) return [];
  try {
    const parsed = JSON.parse(toolCall.output);
    const raw = parsed.todos || parsed;
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((item: unknown) => item && typeof item === "object")
      .map((item: any) => ({
        content: item.content || item.text || String(item),
        status: normalizeTodoStatus(item.status),
      }));
  } catch {
    return [];
  }
}

function normalizeTodoStatus(status: unknown): TodoStatus {
  const s = String(status || "pending").toLowerCase();
  if (s === "completed" || s === "done" || s === "finished") return "completed";
  if (s === "in_progress" || s === "doing" || s === "in progress") return "in_progress";
  return "pending";
}

function isLegacyFalsePositive(source: SourceRecord, toolByCallId: Map<string, string>): boolean {
  if (source.source_type !== "skill") return false;
  const toolName = source.tool_call_id ? toolByCallId.get(source.tool_call_id) : undefined;
  return toolName === "execute_skill";
}
