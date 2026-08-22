"use client";

import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useRef,
  useEffect,
  useMemo,
} from "react";
import type { AttachmentPreviewSelection } from "./imageAttachments";
import {
  streamAgent,
  streamSessionEvents,
  getSessionTokenCount,
  getToolContextJobStatus,
  listSessions as apiListSessions,
  createSession as apiCreateSession,
  renameSession as apiRenameSession,
  deleteSession as apiDeleteSession,
  getRawMessages as apiGetRawMessages,
  getSessionTraces as apiGetSessionTraces,
  getSessionHistory as apiGetSessionHistory,
  getCurrentSessionTodos as apiGetCurrentSessionTodos,
  compactAgentSession as apiCompactAgentSession,
  clearSession as apiClearSession,
  listMcpServers as apiListMcpServers,
  listProjects as apiListProjects,
  registerProject as apiRegisterProject,
  updateProject as apiUpdateProject,
  setProjectTrust as apiSetProjectTrust,
  removeProject as apiRemoveProject,
  updateSessionAnalyticsModel as apiUpdateSessionAnalyticsModel,
  updateSessionLlmSelection as apiUpdateSessionLlmSelection,
  getSessionApprovalMode as apiGetSessionApprovalMode,
  updateSessionApprovalMode as apiUpdateSessionApprovalMode,
  getSessionHarnessState as apiGetSessionHarnessState,
  pauseGoal as apiPauseGoal,
  resumeGoal as apiResumeGoal,
  cancelGoal as apiCancelGoal,
  extendGoalBudget as apiExtendGoalBudget,
  updateGoalObjective as apiUpdateGoalObjective,
  ProjectMeta,
  TodoItem,
  AgentTrace,
  TraceSpan,
  TraceHookBoundarySnapshot,
  TraceMiddlewareInvocation,
  GraphStructure,
  PermissionRequest,
  DimensionBuildRuleRequest,
  LogicalDatasetRuleRequest,
  DatabaseSqlRevisionRequest,
  UserInputRequest,
  SkillSecretRequest,
  KernelFallbackRequest,
  AgentAttachment,
  HarnessGoal,
  HarnessRun,
  RubricEvaluationReport,
  ApprovalMode,
  AgentCompactResult,
} from "./api";
import { getSubagentActivityIdentity } from "./subagentActivity";
import { goalRemainsVisible } from "./goalControls";
import { shouldApplyTodoSnapshot, TodoAuthority } from "./todoProjection";
import {
  settleRunningVerificationActivities,
  verificationFailureActivity,
} from "./verificationActivity";
import {
  mergeRunningSessionIds,
  releaseOrphanedPlaceholderLock,
  rebindSessionScopedLock,
} from "./sessionConcurrency";
import {
  mergeUsageSummaries,
  normalizeUsageSummary,
  type UsageSummary,
} from "./usageSummary";

// ── Types ──────────────────────────────────────────────────

export interface ToolCall {
  id?: string;
  tool: string;
  input?: string;
  output?: string;
  status: "running" | "done";
  startedAt?: number;
  endedAt?: number;
  summary_source?: string;
  is_error?: boolean;
  permissionRequest?: PermissionRequest;
  progress?: {
    stage: string;
    label: string;
    detail?: string;
    elapsedMs?: number;
    stageTimings?: Record<string, number>;
    status?: string;
    history?: Array<{
      id?: string;
      stage: string;
      label: string;
      detail?: string;
      elapsedMs?: number;
      status: string;
    }>;
  };
}

export type TimelineItem =
  | { type: "reasoning"; content: string; id: string }
  | { type: "tool"; toolCall: ToolCall; id: string }
  | { type: "activity"; label: string; detail?: string; status?: string; id: string };

type InspectorActiveTab = "progress" | "goal" | "verification" | "sources" | "permissions" | "attachments" | null;
const INSPECTOR_ACTIVE_TAB_STORAGE_KEY = "puddingclaw_inspector_active_tab";
const ACTIVE_RUNS_STORAGE_KEY = "puddingclaw_active_runs";
const ACTIVE_RUNS_HEARTBEAT_MS = 5_000;
const ACTIVE_RUNS_STALE_MS = 15_000;

type ActiveRunRegistry = Record<string, { sessions: string[]; updatedAt: number }>;
const TERMINAL_RUN_STATUSES = new Set([
  "completed",
  "cancelled",
  "failed",
  "blocked",
  "budget_exceeded",
  "verification_failed",
]);
const CONTROL_ONLY_VERIFICATION_STATUSES = new Set([
  "not_required",
  "verification_incomplete",
  "grader_error",
  "infrastructure_error",
  "budget_exceeded",
]);

function runIsActive(run: HarnessRun | null | undefined): boolean {
  return Boolean(run && !TERMINAL_RUN_STATUSES.has(run.status));
}

function visibleGoalFromHarness(state: {
  active_goal_id?: string | null;
  goal_order?: string[];
  goals: Record<string, HarnessGoal>;
}): HarnessGoal | null {
  if (
    state.active_goal_id
    && state.goals[state.active_goal_id]
    && goalRemainsVisible(state.goals[state.active_goal_id].status)
  ) {
    return state.goals[state.active_goal_id];
  }
  const goalId = [...(state.goal_order || [])]
    .reverse()
    .find((candidate) => {
      const goal = state.goals[candidate];
      return Boolean(goal && goalRemainsVisible(goal.status));
    });
  return goalId ? state.goals[goalId] : null;
}

async function waitForGoalState(
  sessionId: string,
  goalId: string,
  predicate: (goal: HarnessGoal) => boolean,
  timeoutMs = 15_000,
): Promise<HarnessGoal> {
  const deadline = Date.now() + timeoutMs;
  let latest: HarnessGoal | null = null;
  while (Date.now() < deadline) {
    const harness = await apiGetSessionHarnessState(sessionId);
    latest = harness.goals[goalId] || null;
    if (latest && predicate(latest)) return latest;
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  throw new Error(
    latest?.requested_status
      ? `目标仍在处理“${latest.requested_status}”请求，请稍后重试`
      : "目标状态更新超时，请刷新后重试",
  );
}

async function waitForLatestRunToSettle(
  sessionId: string,
  timeoutMs = 10_000,
): Promise<{
  state: Awaited<ReturnType<typeof apiGetSessionHarnessState>> | null;
  settled: boolean;
}> {
  const deadline = Date.now() + timeoutMs;
  let state: Awaited<ReturnType<typeof apiGetSessionHarnessState>> | null = null;
  while (Date.now() < deadline) {
    state = await apiGetSessionHarnessState(sessionId);
    const latestRun = state.latest_run_id ? state.runs[state.latest_run_id] || null : null;
    if (!runIsActive(latestRun)) return { state, settled: true };
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  return { state, settled: false };
}

function readActiveRunRegistry(): ActiveRunRegistry {
  try {
    const parsed = JSON.parse(localStorage.getItem(ACTIVE_RUNS_STORAGE_KEY) || "{}") as ActiveRunRegistry;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function activeRunSessions(registry: ActiveRunRegistry): Set<string> {
  return new Set(Object.values(registry).flatMap((entry) => entry.sessions));
}

export interface RetrievalResult {
  text: string;
  score: string;
  source: string;
}

export interface SourceRecord {
  source_id: string;
  title: string;
  uri?: string;
  document_id?: string;
  chunk_id?: string;
  source_type: "knowledge_base" | "web" | "file" | "skill" | string;
  page?: number | string;
  quote?: string;
  score?: number;
  tool_call_id?: string;
  metadata?: Record<string, unknown>;
}

export interface CitationRef {
  citation_id: string;
  source_id: string;
  display_index: number;
  start?: number;
  end?: number;
  status: "pending" | "verified" | "invalid";
}

export interface MessageSegment {
  content: string;
  reasoning?: string;
  toolCalls?: ToolCall[];
  timeline?: TimelineItem[];
  runId?: string;
  goalId?: string;
}

export interface RunBoundaryNotice {
  reason: "run_model_call_limit" | "thread_model_call_limit" | "verification_failed" | string;
  message: string;
  modelCallCount?: number;
  limit?: number | null;
  completedRound?: number;
  nextRound?: number;
  maxRounds?: number;
  autoContinued?: boolean;
}

export interface ChatMessage {
  id: string;
  queryId?: string;
  role: "user" | "assistant";
  content: string;
  attachments?: AgentAttachment[];
  outputAttachments?: AgentAttachment[];
  reasoning?: string;
  toolCalls?: ToolCall[];
  timeline?: TimelineItem[];
  segments?: MessageSegment[];
  retrievals?: RetrievalResult[];
  sources?: SourceRecord[];
  citations?: CitationRef[];
  permissionRequests?: PermissionRequest[];
  dimensionBuildRuleRequests?: DimensionBuildRuleRequest[];
  logicalDatasetRuleRequests?: LogicalDatasetRuleRequest[];
  databaseSqlRevisionRequests?: DatabaseSqlRevisionRequest[];
  userInputRequests?: UserInputRequest[];
  skillSecretRequests?: SkillSecretRequest[];
  kernelFallbackRequests?: KernelFallbackRequest[];
  interrupted?: boolean;
  interruptionNotice?: string;
  errorNotice?: string;
  runBoundaryNotice?: RunBoundaryNotice;
  verificationSummary?: string;
  usageSummary?: UsageSummary;
  timestamp: number;
}

export interface SessionMeta {
  id: string;
  title: string;
  updated_at: number;
  runtime_mode?: "agent" | "chat";
  project_id?: string | null;
  project_path?: string | null;
  workspace_type?: string;
  workspace_path?: string;
  session_source?: string;
  analytics_model_id?: string | null;
  llm_model_id?: string | null;
  thinking_level?: "low" | "high" | "max" | null;
  credential_name?: string | null;
  approval_mode?: ApprovalMode;
  policy_epoch?: number;
  policy_version?: string;
}

export interface RawMessage {
  role: string;
  content: string;
}

export interface ContextUsage {
  used: number;
  total: number;
  percentage: number;
  measured: boolean;
}

export interface ContextMaintenanceStatus {
  phase: string;
  message: string;
  usedTokensBefore?: number;
  triggerTokens?: number;
  startedAt?: number;
}

export interface RunActivityStatus {
  phase:
    | "running"
    | "reasoning"
    | "subagent"
    | "reading"
    | "querying"
    | "writing"
    | "command"
    | "tool"
    | "verification"
    | "revision"
    | "permission"
    | "hitl"
    | "continuing";
  label: string;
  detail?: string;
}

export interface SendMessageOptions {
  goalControlAction?: "start";
  hiddenUserMessage?: boolean;
  skillHints?: string[];
  onSessionResolved?: (sessionId: string) => void;
}

interface AppState {
  // Runtime mode
  runtimeMode: "agent";
  runtimeReady: boolean;
  setRuntimeMode: (mode: "agent") => void;
  currentProjectId: string | null;
  setCurrentProjectId: (id: string | null) => void;
  analyticsModelId: string | null;
  setAnalyticsModelId: (id: string | null) => void;
  llmModelId: string | null;
  thinkingLevel: "low" | "high" | "max" | null;
  credentialName: string | null;
  setLlmSelection: (
    modelId: string,
    thinkingLevel: "low" | "high" | "max" | null,
    credentialName?: string | null,
  ) => void;
  projects: ProjectMeta[];
  loadProjects: () => void;
  registerProject: (path: string) => Promise<ProjectMeta | null>;
  updateProject: (projectId: string, update: { name?: string; pinned?: boolean }) => Promise<ProjectMeta | null>;
  trustProject: (projectId: string, state: "pending" | "trusted" | "denied") => Promise<ProjectMeta>;
  removeProject: (projectId: string) => Promise<boolean>;

  // Chat
  messages: ChatMessage[];
  sessionHistoryLoading: boolean;
  isStreaming: boolean;
  hasActiveRun: boolean;
  runningSessionIds: ReadonlySet<string>;
  sendMessage: (
    text: string,
    attachments?: AgentAttachment[],
    options?: SendMessageOptions,
  ) => Promise<boolean>;
  stopStreaming: () => void;
  goalModeEnabled: boolean;
  setGoalModeEnabled: (enabled: boolean) => void;
  approvalMode: ApprovalMode;
  approvalModeSaving: boolean;
  approvalModeError: string | null;
  setApprovalMode: (mode: ApprovalMode) => Promise<boolean>;
  activeGoal: HarnessGoal | null;
  currentRun: HarnessRun | null;
  goalRuns: HarnessRun[];
  verificationReport: RubricEvaluationReport | null;
  pauseActiveGoal: () => Promise<HarnessGoal>;
  resumeActiveGoal: () => Promise<HarnessGoal>;
  cancelActiveGoal: () => Promise<void>;
  extendActiveGoalBudget: (additionalRounds: number) => Promise<HarnessGoal>;
  updateActiveGoal: (objective: string) => Promise<HarnessGoal>;

  // Sessions
  sessionId: string;
  setSessionId: (id: string) => void;
  sessions: SessionMeta[];
  sessionsLoaded: boolean;
  projectsLoaded: boolean;
  loadSessions: () => void;
  createSession: () => Promise<string | null>;
  triggerSkillCreator: () => void;

  // Pending input (prefill from external actions, cleared on send)
  pendingInput: string | null;
  setPendingInput: (text: string | null) => void;

  // Per-session input drafts (survive page navigation; cleared on send)
  getInputDraft: (sessionId: string) => string;
  setInputDraft: (sessionId: string, draft: string) => void;

  // Lightweight transient notice (toast), rendered centered in the chat
  // interaction area so it stays correct when side panels open/resize.
  notice: string | null;
  showNotice: (message: string) => void;

  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;

  // Sidebar
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  toggleSidebar: () => void;

  // Inspector (Monaco editor)
  inspectorFile: string | null;
  setInspectorFile: (path: string | null) => void;
  inspectorOpen: boolean;
  setInspectorOpen: (open: boolean) => void;
  toggleInspector: () => void;
  inspectorActiveTab: InspectorActiveTab;
  setInspectorActiveTab: (tab: InspectorActiveTab) => void;

  // Right panel tab
  rightTab: "memory" | "skills" | "mcp";
  setRightTab: (tab: "memory" | "skills" | "mcp") => void;

  // MCP servers
  mcpServers: Array<{ key: string; name: string; url: string; transport: string }>;
  loadMcpServers: () => void;

  // Raw messages
  rawMessages: RawMessage[] | null;
  loadRawMessages: () => void;

  // Agent white-box state
  todos: TodoItem[];
  trace: AgentTrace | null;
  traceHistory: Record<string, AgentTrace>;
  selectedTraceQueryId: string | null;
  selectTraceQuery: (queryId: string) => void;

  // LangGraph execution graph for Agent mode
  graph: GraphStructure | null;
  activeGraphNode: string | null;

  // Main workspace view
  workspaceView: "chat" | "trace";
  setWorkspaceView: (view: "chat" | "trace") => void;

  // Expanded file (editor full-panel mode)
  expandedFile: boolean;
  setExpandedFile: (v: boolean) => void;

  // Panel widths
  sidebarWidth: number;
  setSidebarWidth: (w: number | ((prev: number) => number)) => void;
  inspectorWidth: number;
  setInspectorWidth: (w: number | ((prev: number) => number)) => void;

  // Compression
  isCompressing: boolean;
  compactCurrentAgentSession: (focus?: string) => Promise<AgentCompactResult>;

  // Clear
  clearCurrentSession: () => Promise<void>;

  // Context usage
  contextUsage: ContextUsage;
  setContextUsage: (usage: ContextUsage) => void;

  // Context maintenance
  maintenanceStatus: ContextMaintenanceStatus | null;

  // Current streaming phase, rendered next to the composer rather than in chat.
  runActivityStatus: RunActivityStatus | null;

  // Active citation source (syncs chat click with right panel)
  activeSourceId: string | null;
  setActiveSourceId: (id: string | null) => void;

  // Image preview selection is an identity only. The panel resolves the
  // attachment from the current Session's messages before rendering it.
  activeAttachmentPreview: AttachmentPreviewSelection | null;
  openAttachmentPreview: (attachmentId: string) => void;
  closeAttachmentPreview: () => void;
}

const AppContext = createContext<AppState | null>(null);

// ── Helper: parse backend history into ChatMessage[] ────────
function splitPersistedUserMessage(content: string): { content: string; attachments?: AgentAttachment[] } {
  const marker = "\n\n[附件]\n";
  const markerIndex = content.lastIndexOf(marker);
  if (markerIndex < 0) return { content };

  const attachments = content
    .slice(markerIndex + marker.length)
    .split("\n")
    .map((line) => line.replace(/^-\s*/, "").trim())
    .filter(Boolean)
    .map((name) => ({ type: "file" as const, name }));

  return {
    content: content.slice(0, markerIndex),
    attachments: attachments.length ? attachments : undefined,
  };
}

function stripPersistedModelCallLimitNotice(content: string): string {
  return content
    .replace(
      /(?:\r?\n){0,2}Model call limits exceeded:\s*(?:run|thread) limit\s*\(\d+\s*\/\s*\d+\)\.?\s*$/i,
      ""
    )
    .trimEnd();
}

function persistedTimestampMilliseconds(value: number | undefined): number {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return 0;
  return timestamp >= 1_000_000_000_000 ? timestamp : timestamp * 1000;
}

function parseHistoryMessages(
  backendMessages: Array<{
    role: string;
    content: string;
    created_at?: number;
    query_id?: string;
    attachments?: AgentAttachment[];
    output_attachments?: AgentAttachment[];
    reasoning_content?: string;
    tool_calls?: Array<{
      id?: string;
      tool: string;
      input?: string;
      output?: string;
      is_error?: boolean;
      startedAt?: number;
      endedAt?: number;
    }>;
    timeline?: Array<{
      type: string;
      content?: string;
      tool_call?: ToolCall;
      label?: string;
      detail?: string;
      status?: string;
      id?: string;
    }>;
    status?: string;
    segments?: Array<{ content?: string; reasoning_content?: string; tool_calls?: ToolCall[]; timeline?: TimelineItem[]; run_id?: string; goal_id?: string }>;
    sources?: SourceRecord[];
    citations?: CitationRef[];
    interrupted?: boolean;
    interruption_notice?: string;
    error_notice?: string;
    verification_summary?: string;
    usage_summary?: Record<string, unknown>;
    run_boundary_notice?: {
      reason?: string;
      message?: string;
      model_call_count?: number;
      limit?: number | null;
      completed_round?: number;
      next_round?: number;
      max_rounds?: number;
      auto_continued?: boolean;
    };
  }>
): ChatMessage[] {
  const loaded: ChatMessage[] = [];
  let msgIndex = 0;
  for (const msg of backendMessages) {
    if (msg.role === "user") {
      const userMessage = splitPersistedUserMessage(msg.content);
      loaded.push({
        id: `hist-user-${msgIndex++}`,
        queryId: msg.query_id,
        role: "user",
        content: userMessage.content,
        attachments: msg.attachments?.length ? msg.attachments : userMessage.attachments,
        timestamp: persistedTimestampMilliseconds(msg.created_at),
      });
    } else if (msg.role === "assistant") {
      const toolCalls: ToolCall[] = (msg.tool_calls || []).map(
        (tc) => ({
          id: tc.id,
          tool: tc.tool,
          input: tc.input || "",
          output: tc.output || "",
          status: "done" as const,
          startedAt: tc.startedAt,
          endedAt: tc.endedAt,
          is_error: Boolean(tc.is_error),
        })
      );
      const timeline = msg.timeline?.length
        ? normalizeSavedTimeline(msg.timeline, toolCalls)
        : buildHistoryTimeline(msg.reasoning_content, toolCalls);
      const segments: MessageSegment[] | undefined = msg.segments?.length
        ? msg.segments.map((seg) => {
            const segToolCalls: ToolCall[] = (seg.tool_calls || []).map((tc) => ({
              id: tc.id || "",
              tool: tc.tool,
              input: tc.input || "",
              output: tc.output || "",
              status: "done" as const,
              startedAt: tc.startedAt,
              endedAt: tc.endedAt,
              is_error: Boolean(tc.is_error),
            }));
            const segTimeline = seg.timeline?.length
              ? normalizeSavedTimeline(seg.timeline, segToolCalls)
              : undefined;
            return {
              content: stripPersistedModelCallLimitNotice(seg.content || ""),
              reasoning: seg.reasoning_content,
              toolCalls: segToolCalls.length > 0 ? segToolCalls : undefined,
              timeline: segTimeline,
              runId: seg.run_id,
              goalId: seg.goal_id,
            };
          })
        : undefined;
      const restored: ChatMessage = {
        id: `hist-asst-${msgIndex++}`,
        queryId: msg.query_id,
        role: "assistant",
        content: stripPersistedModelCallLimitNotice(msg.content),
        reasoning: msg.reasoning_content,
        toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
        timeline: timeline.length > 0 ? timeline : undefined,
        segments,
        outputAttachments: msg.output_attachments,
        sources: msg.sources,
        citations: msg.citations,
        interrupted: Boolean(msg.interrupted),
        interruptionNotice: msg.interruption_notice,
        errorNotice: msg.error_notice,
        verificationSummary: msg.verification_summary,
        usageSummary: normalizeUsageSummary(msg.usage_summary),
        // Run boundaries are Harness control-plane state. They remain in the
        // persisted audit record, but are intentionally not rendered as chat
        // messages; the Goal drawer owns the cross-Run timeline.
        runBoundaryNotice: undefined,
        timestamp: persistedTimestampMilliseconds(msg.created_at),
      };
      const previous = loaded[loaded.length - 1];
      const restoredGoalId = segments?.find((segment) => segment.goalId)?.goalId;
      const previousGoalId = previous?.segments?.find((segment) => segment.goalId)?.goalId;
      if (
        restoredGoalId
        && previous?.role === "assistant"
        && previousGoalId === restoredGoalId
      ) {
        previous.content = [previous.content, restored.content].filter(Boolean).join("\n\n");
        previous.segments = [...(previous.segments || []), ...(restored.segments || [])];
        previous.toolCalls = [...(previous.toolCalls || []), ...(restored.toolCalls || [])];
        previous.timeline = [...(previous.timeline || []), ...(restored.timeline || [])];
        previous.verificationSummary = restored.verificationSummary || previous.verificationSummary;
        previous.usageSummary = mergeUsageSummaries(
          previous.usageSummary,
          restored.usageSummary,
        );
        previous.sources = [...(previous.sources || []), ...(restored.sources || [])];
        previous.citations = [...(previous.citations || []), ...(restored.citations || [])];
        previous.outputAttachments = [
          ...(previous.outputAttachments || []),
          ...(restored.outputAttachments || []).filter(
            (item) => !(previous.outputAttachments || []).some((current) => current.id === item.id)
          ),
        ];
        previous.timestamp = restored.timestamp;
      } else {
        loaded.push(restored);
      }
    }
  }
  return loaded;
}

// ── Timeline helpers ───────────────────────────────────────
// Build a live timeline that interleaves reasoning and tool calls.

function appendReasoningToTimeline(timeline: TimelineItem[], content: string): void {
  if (!content) return;
  const last = timeline[timeline.length - 1];
  if (last?.type === "reasoning") {
    last.content += content;
  } else {
    timeline.push({
      type: "reasoning",
      content,
      id: `reasoning-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    });
  }
}

function addToolToTimeline(timeline: TimelineItem[], toolCall: ToolCall): void {
  timeline.push({
    type: "tool",
    toolCall,
    id: toolCall.id || `tool-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
  });
}

function updateToolInTimeline(
  timeline: TimelineItem[],
  id: string,
  toolName: string,
  updates: Partial<ToolCall>
): void {
  for (let i = timeline.length - 1; i >= 0; i--) {
    const item = timeline[i];
    if (item.type === "tool") {
      const tc = item.toolCall;
      if ((id && tc.id === id) || (!id && tc.tool === toolName && tc.status === "running")) {
        item.toolCall = { ...tc, ...updates };
        return;
      }
    }
  }
}

function finalizeRunningToolCall(toolCall: ToolCall, output: string): ToolCall {
  if (toolCall.status !== "running") return toolCall;
  return {
    ...toolCall,
    status: "done",
    endedAt: toolCall.endedAt || Date.now(),
    output: toolCall.output || output,
    is_error: true,
    summary_source: toolCall.summary_source || "stream_cancelled",
  };
}

function finalizeRunningToolsInTimeline(timeline: TimelineItem[] | undefined, output: string): TimelineItem[] | undefined {
  if (!timeline) return timeline;
  let changed = false;
  const next = timeline.map((item) => {
    if (item.type !== "tool" || item.toolCall.status !== "running") return item;
    changed = true;
    return { ...item, toolCall: finalizeRunningToolCall(item.toolCall, output) };
  });
  return changed ? next : timeline;
}

function finalizeRunningToolsInMessage(message: ChatMessage, output: string): ChatMessage {
  let changed = false;
  const toolCalls = message.toolCalls?.map((toolCall) => {
    const next = finalizeRunningToolCall(toolCall, output);
    if (next !== toolCall) changed = true;
    return next;
  });
  const timeline = finalizeRunningToolsInTimeline(message.timeline, output);
  if (timeline !== message.timeline) changed = true;
  const segments = message.segments?.map((segment) => {
    let segmentChanged = false;
    const segmentToolCalls = segment.toolCalls?.map((toolCall) => {
      const next = finalizeRunningToolCall(toolCall, output);
      if (next !== toolCall) segmentChanged = true;
      return next;
    });
    const segmentTimeline = finalizeRunningToolsInTimeline(segment.timeline, output);
    if (segmentTimeline !== segment.timeline) segmentChanged = true;
    if (!segmentChanged) return segment;
    changed = true;
    return {
      ...segment,
      toolCalls: segmentToolCalls,
      timeline: segmentTimeline,
    };
  });
  if (!changed) return message;
  return {
    ...message,
    toolCalls,
    timeline,
    segments,
  };
}

function markMessageInterrupted(message: ChatMessage): ChatMessage {
  return {
    ...message,
    interrupted: true,
    interruptionNotice: message.interruptionNotice || "本轮已被用户停止，以上为中断前已完成的部分结果。",
  };
}

function markMessageError(message: ChatMessage, notice: string): ChatMessage {
  return {
    ...message,
    errorNotice: notice,
  };
}

function buildHistoryTimeline(
  reasoningContent: string | undefined,
  toolCalls: ToolCall[]
): TimelineItem[] {
  const timeline: TimelineItem[] = [];
  if (reasoningContent) {
    // After the session ends we only have the final reasoning_content string.
    // Split it at paragraph boundaries so the history timeline isn't one huge
    // wall of text; this approximates the multiple reasoning chunks seen while
    // streaming.
    const chunks = reasoningContent
      .split(/\n{2,}/)
      .map((chunk) => chunk.trim())
      .filter(Boolean);
    chunks.forEach((chunk, idx) => {
      timeline.push({
        type: "reasoning",
        content: chunk,
        id: `hist-reasoning-${Date.now()}-${idx}`,
      });
    });
  }
  toolCalls.forEach((tc, idx) => {
    timeline.push({
      type: "tool",
      toolCall: tc,
      id: tc.id || `hist-tool-${Date.now()}-${idx}`,
    });
  });
  return timeline;
}

function normalizeSavedTimeline(
  saved: Array<{ type: string; content?: string; tool_call?: ToolCall; label?: string; detail?: string; status?: string; id?: string }>,
  toolCalls: ToolCall[]
): TimelineItem[] {
  // Prefer the persisted tool_call from the timeline, but supplement with the
  // full saved tool_calls list (status/output) when the timeline entry is partial.
  const toolById = new Map(toolCalls.map((tc) => [tc.id, tc]));
  return saved
    .map((item): TimelineItem | null => {
      if (item.type === "reasoning" && typeof item.content === "string") {
        return { type: "reasoning", content: item.content, id: item.id || `saved-reasoning-${Date.now()}` };
      }
      if (item.type === "tool") {
        const tc = item.tool_call;
        if (!tc) return null;
        const full = tc.id ? toolById.get(tc.id) : undefined;
        return {
          type: "tool",
          toolCall: {
            id: tc.id || `saved-tool-${Date.now()}`,
            tool: tc.tool,
            input: tc.input || "",
            output: full?.output ?? tc.output ?? "",
            status: full?.status ?? (tc.status === "running" ? "running" : "done"),
            startedAt: full?.startedAt ?? tc.startedAt,
            endedAt: full?.endedAt ?? tc.endedAt,
            is_error: full?.is_error ?? Boolean(tc.is_error),
          },
          id: tc.id || `saved-tool-${Date.now()}`,
        };
      }
      if (item.type === "activity" && item.label) {
        return {
          type: "activity",
          label: item.label,
          detail: item.detail,
          status: item.status,
          id: item.id || `saved-activity-${Date.now()}`,
        };
      }
      return null;
    })
    .filter((item): item is TimelineItem => item !== null);
}

function getOrCreateUserId(): string {
  if (typeof window === "undefined") return "default_user";
  const key = "puddingclaw-user-id";
  let id = localStorage.getItem(key);
  if (!id) {
    id = `user-${crypto.randomUUID()}`;
    localStorage.setItem(key, id);
  }
  return id;
}

function sortProjects(projects: ProjectMeta[]): ProjectMeta[] {
  return [...projects].sort((a, b) => {
    if (Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1;
    return b.updated_at - a.updated_at;
  });
}

function toolActivityStatus(toolName: string, rawInput?: string): RunActivityStatus {
  const tool = toolName.trim().toLowerCase();
  if (tool === "task" || tool.includes("subagent")) {
    let detail = "";
    try {
      const parsed = JSON.parse(rawInput || "{}") as Record<string, unknown>;
      detail = String(parsed.description || parsed.task || parsed.prompt || "").trim();
    } catch {
      // Tool input is optional UI detail; never expose an unparsed payload.
    }
    return {
      phase: "subagent",
      label: "子代理处理中",
      detail: detail ? detail.slice(0, 72) : undefined,
    };
  }
  if (
    tool.startsWith("database_") ||
    tool.includes("knowledge_search") ||
    tool.includes("web_search") ||
    tool.includes("tavily") ||
    tool === "fetch_url"
  ) {
    return { phase: "querying", label: "正在查询与整理数据" };
  }
  if (
    tool === "write_file" ||
    tool === "patch_file" ||
    tool === "edit_file" ||
    tool.includes("commit_external") ||
    tool.includes("publish_attachment")
  ) {
    return { phase: "writing", label: "正在更新文件" };
  }
  if (
    tool === "read_file" ||
    tool === "read_resource" ||
    tool === "inspect_file_version" ||
    tool.includes("stage_external")
  ) {
    return { phase: "reading", label: "正在读取与核对资料" };
  }
  if (tool === "execute" || tool === "terminal" || tool === "shell") {
    return { phase: "command", label: "正在运行命令" };
  }
  return { phase: "tool", label: "正在调用工具" };
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  // ── Per-session state (Map-based, supports parallel sessions) ──
  const messagesMapRef = useRef<Record<string, ChatMessage[]>>({});
  const todosMapRef = useRef<Record<string, TodoItem[]>>({});
  const todoLedgerRevisionsMapRef = useRef<Record<string, number>>({});
  const todoAuthoritiesMapRef = useRef<Record<string, TodoAuthority>>({});
  const tracesMapRef = useRef<Record<string, AgentTrace | null>>({});
  const traceHistoriesMapRef = useRef<Record<string, Record<string, AgentTrace>>>({});
  const selectedTraceQueryMapRef = useRef<Record<string, string | null>>({});
  const graphsMapRef = useRef<Record<string, GraphStructure | null>>({});
  const graphActiveNodesRef = useRef<Record<string, string | null>>({});
  const analyticsModelIdsMapRef = useRef<Record<string, string | null>>({});
  const llmSelectionsMapRef = useRef<Record<string, {
    modelId: string | null;
    thinkingLevel: "low" | "high" | "max" | null;
    credentialName: string | null;
  }>>({});
  const llmSelectionSaveChainsRef = useRef<Record<string, Promise<void>>>({});
  const approvalModesMapRef = useRef<Record<string, ApprovalMode>>({ default: "smart" });
  const approvalPolicyEpochsMapRef = useRef<Record<string, number>>({ default: 1 });
  const nextRunGoalModeMapRef = useRef<Record<string, boolean>>({ default: false });
  const createSessionPromisesRef = useRef<Map<string, Promise<string | null>>>(new Map());
  const sendReservationsRef = useRef<Set<string>>(new Set());
  const approvalModeSavingSessionsRef = useRef<Set<string>>(new Set());
  const approvalModeErrorsMapRef = useRef<Record<string, string | null>>({});
  const activeGoalsMapRef = useRef<Record<string, HarnessGoal | null>>({});
  const currentRunsMapRef = useRef<Record<string, HarnessRun | null>>({});
  const goalRunsMapRef = useRef<Record<string, HarnessRun[]>>({});
  const verificationReportsMapRef = useRef<Record<string, RubricEvaluationReport | null>>({});
  const runActivityStatusesMapRef = useRef<Record<string, RunActivityStatus | null>>({});
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());
  const assistantIdsRef = useRef<Map<string, string>>(new Map());
  const sessionIdRef = useRef("default"); // tracks current sessionId for SSE callbacks

  // ── UI state (reflects current session) ──
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // Starts true: on a fresh provider mount the restore effect below still has
  // to decide which session to show (saved id or latest). Until that decision
  // resolves, ChatPanel must show the loading state instead of flashing the
  // empty "default" workbench (which carries the localStorage-restored project
  // chip) before jumping to the target session's history.
  const [sessionHistoryLoading, setSessionHistoryLoading] = useState(true);
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [trace, setTrace] = useState<AgentTrace | null>(null);
  const [traceHistory, setTraceHistory] = useState<Record<string, AgentTrace>>({});
  const [selectedTraceQueryId, setSelectedTraceQueryId] = useState<string | null>(null);
  const [graph, setGraph] = useState<GraphStructure | null>(null);
  const [activeGraphNode, setActiveGraphNode] = useState<string | null>(null);
  const [workspaceView, setWorkspaceViewRaw] = useState<"chat" | "trace">("chat");

  const setWorkspaceView = useCallback(
    (view: "chat" | "trace") => {
      try {
        sessionStorage.setItem("puddingclaw_workspace_view", view);
      } catch {
        // ignore storage errors
      }
      setWorkspaceViewRaw(view);
    },
    []
  );

  // Restore workspace view from sessionStorage on the client to avoid SSR
  // hydration mismatches (sessionStorage is not available during server render).
  useEffect(() => {
    try {
      const saved = sessionStorage.getItem("puddingclaw_workspace_view");
      if (saved === "chat" || saved === "trace") {
        setWorkspaceViewRaw(saved);
      }
    } catch {
      // ignore storage errors
    }
  }, []);
  const [streamingSessions, setStreamingSessions] = useState<Set<string>>(new Set());
  const streamingSessionsRef = useRef<Set<string>>(new Set());
  const updateStreamingSessions = useCallback(
    (update: (current: Set<string>) => Set<string>) => {
      const next = update(streamingSessionsRef.current);
      streamingSessionsRef.current = next;
      setStreamingSessions(next);
    },
    [],
  );
  const [sharedStreamingSessions, setSharedStreamingSessions] = useState<Set<string>>(new Set());
  // Runs started by another client (Worker CLI, PuddingTeams, or another
  // browser tab) do not own this tab's POST /agent stream.  Keep a small
  // remote-run projection so the selected Session still shows live activity
  // without requiring a manual refresh.
  const [remoteRunningSessions, setRemoteRunningSessions] = useState<Set<string>>(new Set());
  const remoteRunningSessionsRef = useRef<Set<string>>(new Set());
  const headlessObserverSequencesRef = useRef<Record<string, number>>({});
  const headlessObserverRunIdsRef = useRef<Record<string, string>>({});
  const [sessionId, setSessionIdRaw] = useState("default");
  const [userId] = useState(() => getOrCreateUserId());
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const sessionsRef = useRef<SessionMeta[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const runtimeMode = "agent" as const;
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [currentProjectId, setCurrentProjectIdRaw] = useState<string | null>(null);
  const [analyticsModelId, setAnalyticsModelIdRaw] = useState<string | null>(null);
  const [llmModelId, setLlmModelIdRaw] = useState<string | null>(null);
  const [thinkingLevel, setThinkingLevelRaw] = useState<"low" | "high" | "max" | null>(null);
  const [credentialName, setCredentialNameRaw] = useState<string | null>(null);
  const [goalModeEnabled, setGoalModeEnabledRaw] = useState(false);
  const [approvalMode, setApprovalModeRaw] = useState<ApprovalMode>("smart");
  const [approvalModeSaving, setApprovalModeSaving] = useState(false);
  const [approvalModeError, setApprovalModeError] = useState<string | null>(null);
  const [activeGoal, setActiveGoal] = useState<HarnessGoal | null>(null);
  const [currentRun, setCurrentRun] = useState<HarnessRun | null>(null);
  const [goalRuns, setGoalRuns] = useState<HarnessRun[]>([]);
  const [verificationReport, setVerificationReport] = useState<RubricEvaluationReport | null>(null);
  const [projects, setProjects] = useState<ProjectMeta[]>([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [inspectorFile, setInspectorFileRaw] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorActiveTab, setInspectorActiveTabRaw] = useState<InspectorActiveTab>("progress");
  const setInspectorActiveTab = useCallback((tab: InspectorActiveTab) => {
    try {
      localStorage.setItem(INSPECTOR_ACTIVE_TAB_STORAGE_KEY, tab || "collapsed");
    } catch {
      // Keep the panel usable when browser storage is unavailable.
    }
    setInspectorActiveTabRaw(tab);
  }, []);
  useEffect(() => {
    try {
      const saved = localStorage.getItem(INSPECTOR_ACTIVE_TAB_STORAGE_KEY);
      if (
        saved === "progress" ||
        saved === "goal" ||
        saved === "verification" ||
        saved === "sources" ||
        saved === "permissions" ||
        saved === "attachments"
      ) {
        setInspectorActiveTabRaw(saved);
      } else if (saved === "collapsed") {
        setInspectorActiveTabRaw(null);
      }
    } catch {
      // Default to Progress when browser storage is unavailable.
    }
  }, []);

  // Each browser window has its own React store. Share a small heartbeat
  // registry so the current session can reflect work started in another one.
  useEffect(() => {
    let windowId = "";
    try {
      windowId = sessionStorage.getItem("puddingclaw_window_id") || crypto.randomUUID();
      sessionStorage.setItem("puddingclaw_window_id", windowId);
    } catch {
      windowId = `window-${Math.random().toString(36).slice(2)}`;
    }

    const sync = (removeCurrentWindow = false) => {
      const now = Date.now();
      const registry = readActiveRunRegistry();
      const freshRegistry = Object.fromEntries(
        Object.entries(registry).filter(([, entry]) =>
          entry && Array.isArray(entry.sessions) && now - entry.updatedAt < ACTIVE_RUNS_STALE_MS
        )
      ) as ActiveRunRegistry;

      if (removeCurrentWindow || streamingSessions.size === 0) {
        delete freshRegistry[windowId];
      } else {
        freshRegistry[windowId] = {
          sessions: Array.from(streamingSessions),
          updatedAt: now,
        };
      }

      try {
        localStorage.setItem(ACTIVE_RUNS_STORAGE_KEY, JSON.stringify(freshRegistry));
      } catch {
        // Keep the current window indicator working even if storage is blocked.
      }
      setSharedStreamingSessions(activeRunSessions(freshRegistry));
    };

    const handleStorage = (event: StorageEvent) => {
      if (event.key !== ACTIVE_RUNS_STORAGE_KEY) return;
      setSharedStreamingSessions(activeRunSessions(readActiveRunRegistry()));
    };
    const handleBeforeUnload = () => sync(true);

    sync();
    const heartbeat = window.setInterval(sync, ACTIVE_RUNS_HEARTBEAT_MS);
    window.addEventListener("storage", handleStorage);
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => {
      window.clearInterval(heartbeat);
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener("beforeunload", handleBeforeUnload);
      sync(true);
    };
  }, [streamingSessions]);
  const [rightTab, setRightTab] = useState<"memory" | "skills" | "mcp">("memory");
  const [mcpServers, setMcpServers] = useState<Array<{ key: string; name: string; url: string; transport: string }>>([]);
  const [rawMessages, setRawMessages] = useState<RawMessage[] | null>(null);
  const [expandedFile, setExpandedFile] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(260);
  const [inspectorWidth, setInspectorWidth] = useState(360);
  const [isCompressing, setIsCompressing] = useState(false);
  const [contextUsage, setContextUsage] = useState<ContextUsage>({
    used: 0,
    total: 200000,
    percentage: 0,
    measured: false,
  });
  const [pendingInput, setPendingInput] = useState<string | null>(null);
  // Per-session input drafts. Keyed by session id ("default" for an unsent
  // new chat) so typed text survives page navigation; cleared on send.
  // Stored in a ref: drafts change on every keystroke and must not trigger
  // provider-level re-renders.
  const inputDraftsRef = useRef<Map<string, string>>(new Map());
  const getInputDraft = useCallback((sid: string): string => {
    return inputDraftsRef.current.get(sid) ?? "";
  }, []);
  const setInputDraft = useCallback((sid: string, draft: string) => {
    if (draft) {
      inputDraftsRef.current.set(sid, draft);
    } else {
      inputDraftsRef.current.delete(sid);
    }
  }, []);
  // Transient UI notice (toast). Auto-dismisses; re-triggering resets the timer.
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showNotice = useCallback((message: string) => {
    setNotice(message);
    if (noticeTimerRef.current) clearTimeout(noticeTimerRef.current);
    noticeTimerRef.current = setTimeout(() => setNotice(null), 2000);
  }, []);
  const [maintenanceStatus, setMaintenanceStatus] =
    useState<ContextMaintenanceStatus | null>(null);
  const [runActivityStatus, setRunActivityStatus] =
    useState<RunActivityStatus | null>(null);
  const toolContextPollingJobsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const handleProviderBindingChange = (event: Event) => {
      const detail = (event as CustomEvent<{ binding?: string }>).detail;
      if (detail?.binding !== "agent") return;

      // The unsent placeholder conversation inherits the global Agent binding.
      // Never turn that inherited default into a durable per-session override.
      llmSelectionsMapRef.current.default = {
        modelId: null,
        thinkingLevel: null,
        credentialName: null,
      };
      if (sessionIdRef.current === "default") {
        setLlmModelIdRaw(null);
        setThinkingLevelRaw(null);
        setCredentialNameRaw(null);
      }
    };
    window.addEventListener(
      "puddingclaw:provider-bindings-changed",
      handleProviderBindingChange,
    );
    return () => {
      window.removeEventListener(
        "puddingclaw:provider-bindings-changed",
        handleProviderBindingChange,
      );
    };
  }, []);

  const [activeSourceId, setActiveSourceId] = useState<string | null>(null);
  const [activeAttachmentPreview, setActiveAttachmentPreview] =
    useState<AttachmentPreviewSelection | null>(null);
  const openAttachmentPreview = useCallback((attachmentId: string) => {
    setActiveSourceId(null);
    setInspectorActiveTab("attachments");
    setActiveAttachmentPreview({
      sessionId: sessionIdRef.current,
      attachmentId,
    });
    setInspectorOpen(true);
  }, [setInspectorActiveTab]);
  const closeAttachmentPreview = useCallback(() => {
    setActiveAttachmentPreview(null);
  }, []);

  const setAnalyticsModelId = useCallback((id: string | null) => {
    const sid = sessionIdRef.current;
    analyticsModelIdsMapRef.current[sid] = id;
    setAnalyticsModelIdRaw(id);

    if (sid === "default") return;
    setSessions((prev) =>
      prev.map((session) =>
        session.id === sid ? { ...session, analytics_model_id: id } : session
      )
    );
    apiUpdateSessionAnalyticsModel(sid, id).catch(() => {
      // Keep the optimistic session-local selection. A subsequent Agent turn
      // also persists the same value through its request metadata.
    });
  }, []);

  const setLlmSelection = useCallback((
    modelId: string,
    nextThinkingLevel: "low" | "high" | "max" | null,
    nextCredentialName: string | null = null,
  ) => {
    const sid = sessionIdRef.current;
    llmSelectionsMapRef.current[sid] = {
      modelId,
      thinkingLevel: nextThinkingLevel,
      credentialName: nextCredentialName,
    };
    setLlmModelIdRaw(modelId);
    setThinkingLevelRaw(nextThinkingLevel);
    setCredentialNameRaw(nextCredentialName);

    if (sid === "default") return;
    setSessions((current) => current.map((session) =>
      session.id === sid
        ? {
            ...session,
            llm_model_id: modelId,
            thinking_level: nextThinkingLevel,
            credential_name: nextCredentialName,
          }
        : session
    ));
    const previousSave = llmSelectionSaveChainsRef.current[sid] || Promise.resolve();
    const nextSave = previousSave
      .catch(() => undefined)
      .then(() => apiUpdateSessionLlmSelection(sid, modelId, nextThinkingLevel, nextCredentialName))
      .then(() => undefined)
      .catch(() => {
        // The next Agent request validates and persists the same frozen values.
      });
    llmSelectionSaveChainsRef.current[sid] = nextSave;
    void nextSave.finally(() => {
      if (llmSelectionSaveChainsRef.current[sid] === nextSave) {
        delete llmSelectionSaveChainsRef.current[sid];
      }
    });
  }, []);

  const setGoalModeEnabled = useCallback((enabled: boolean) => {
    const sid = sessionIdRef.current;
    nextRunGoalModeMapRef.current[sid] = enabled;
    setGoalModeEnabledRaw(enabled);
  }, []);

  const setApprovalMode = useCallback(async (mode: ApprovalMode): Promise<boolean> => {
    const sid = sessionIdRef.current;
    approvalModeErrorsMapRef.current[sid] = null;
    setApprovalModeError(null);
    if (sid === "default") {
      approvalModesMapRef.current.default = mode;
      setApprovalModeRaw(mode);
      return true;
    }
    if (approvalModeSavingSessionsRef.current.has(sid) || runIsActive(currentRunsMapRef.current[sid])) {
      const message = "当前 Run 进行中，完成后才能切换授权模式。";
      approvalModeErrorsMapRef.current[sid] = message;
      setApprovalModeError(message);
      return false;
    }
    const expectedEpoch = approvalPolicyEpochsMapRef.current[sid];
    if (!expectedEpoch) {
      const message = "授权策略仍在加载，请稍后重试。";
      approvalModeErrorsMapRef.current[sid] = message;
      setApprovalModeError(message);
      return false;
    }
    approvalModeSavingSessionsRef.current.add(sid);
    setApprovalModeSaving(true);
    try {
      const result = await apiUpdateSessionApprovalMode(
        sid,
        mode,
        expectedEpoch,
      );
      approvalModesMapRef.current[sid] = result.approval_mode;
      approvalPolicyEpochsMapRef.current[sid] = result.policy_epoch;
      setSessions((current) => current.map((session) =>
        session.id === sid
          ? { ...session, approval_mode: result.approval_mode }
          : session
      ));
      if (sessionIdRef.current === sid) {
        setApprovalModeRaw(result.approval_mode);
      }
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : "授权模式更新失败";
      approvalModeErrorsMapRef.current[sid] = message;
      if (sessionIdRef.current === sid) setApprovalModeError(message);
      return false;
    } finally {
      approvalModeSavingSessionsRef.current.delete(sid);
      if (sessionIdRef.current === sid) setApprovalModeSaving(false);
    }
  }, []);

  // Derived: is the CURRENT session streaming?
  const isStreaming = streamingSessions.has(sessionId);
  const runningSessionIds = useMemo(
    () => {
      const merged = mergeRunningSessionIds(streamingSessions, sharedStreamingSessions);
      remoteRunningSessions.forEach((sid) => merged.add(sid));
      return merged;
    },
    [remoteRunningSessions, sharedStreamingSessions, streamingSessions],
  );
  // The navbar is global, so it must also reflect work that continues after
  // switching away from the session that initiated it.
  const hasActiveRun = runningSessionIds.has(sessionId);

  const setRuntimeMode = useCallback((_mode: "agent") => {
    try {
      localStorage.setItem("puddingclaw_runtime_mode", "agent");
    } catch {
      // ignore storage errors
    }
  }, []);

  const setCurrentProjectId = useCallback((id: string | null) => {
    setCurrentProjectIdRaw(id);
    try {
      if (id) localStorage.setItem("puddingclaw_current_project_id", id);
      else localStorage.removeItem("puddingclaw_current_project_id");
    } catch {
      // ignore storage errors
    }
  }, []);

  useEffect(() => {
    try {
      // Chat mode was retired. Overwrite older persisted selections so every
      // product conversation now enters the maintained Agent runtime.
      localStorage.setItem("puddingclaw_runtime_mode", "agent");
      const savedProjectId = localStorage.getItem("puddingclaw_current_project_id");
      if (savedProjectId) setCurrentProjectIdRaw(savedProjectId);
    } catch {
      // ignore storage errors
    } finally {
      setRuntimeReady(true);
    }
  }, []);

  const toggleSidebar = useCallback(() => setSidebarOpen((v) => !v), []);
  const toggleInspector = useCallback(() => setInspectorOpen((v) => !v), []);

  // When a file is selected, auto-open the inspector
  const setInspectorFile = useCallback((path: string | null) => {
    setInspectorFileRaw(path);
    if (path) setInspectorOpen(true);
  }, []);

  // ── Helper: update messages for a session ──────────────
  // Updates the map, and if it's the currently viewed session, also updates UI state
  const updateSessionMessages = useCallback(
    (sid: string, updater: (prev: ChatMessage[]) => ChatMessage[]) => {
      const prev = messagesMapRef.current[sid] || [];
      const next = updater(prev);
      messagesMapRef.current[sid] = next;
      // Only trigger re-render if this is the currently displayed session
      if (sessionIdRef.current === sid) {
        setMessages(next);
      }
    },
    []
  );

  // ── Helper: update Agent white-box state for a session ──────────────
  const updateSessionTodos = useCallback((
    sid: string,
    nextTodos: TodoItem[],
    authority?: TodoAuthority | null,
    ledgerRevision?: number | null,
  ) => {
    const previousAuthority = todoAuthoritiesMapRef.current[sid];
    const previousRevision = todoLedgerRevisionsMapRef.current[sid] ?? 0;
    if (!shouldApplyTodoSnapshot(
      previousAuthority,
      previousRevision,
      authority,
      ledgerRevision,
    )) {
      return;
    }
    const orderedTodos = nextTodos
      .map((todo, arrivalIndex) => ({ todo, arrivalIndex }))
      .sort((left, right) =>
        (left.todo.position ?? left.arrivalIndex) - (right.todo.position ?? right.arrivalIndex)
      )
      .map(({ todo }) => todo);
    todosMapRef.current[sid] = orderedTodos;
    if (authority) todoAuthoritiesMapRef.current[sid] = authority;
    if (typeof ledgerRevision === "number") {
      todoLedgerRevisionsMapRef.current[sid] = ledgerRevision;
    }
    if (sessionIdRef.current === sid) {
      setTodos(orderedTodos);
    }
  }, []);

  const updateSessionRunActivity = useCallback(
    (sid: string, status: RunActivityStatus | null) => {
      const previous = runActivityStatusesMapRef.current[sid] ?? null;
      if (
        previous?.phase === status?.phase &&
        previous?.label === status?.label &&
        previous?.detail === status?.detail
      ) {
        return;
      }
      runActivityStatusesMapRef.current[sid] = status;
      if (sessionIdRef.current === sid) {
        setRunActivityStatus(status);
      }
    },
    []
  );

  const updateSessionTrace = useCallback((sid: string, nextTrace: AgentTrace | null) => {
    tracesMapRef.current[sid] = nextTrace;
    if (nextTrace?.query_id) {
      const history = {
        ...(traceHistoriesMapRef.current[sid] || {}),
        [nextTrace.query_id]: nextTrace,
      };
      traceHistoriesMapRef.current[sid] = history;
      selectedTraceQueryMapRef.current[sid] = nextTrace.query_id;
      if (sessionIdRef.current === sid) {
        setTraceHistory(history);
        setSelectedTraceQueryId(nextTrace.query_id || null);
      }
    }
    if (sessionIdRef.current === sid) {
      setTrace(nextTrace);
    }
  }, []);

  const selectTraceQuery = useCallback((queryId: string) => {
    const sid = sessionIdRef.current;
    const history = traceHistoriesMapRef.current[sid] || {};
    const nextTrace = history[queryId];
    if (!nextTrace) return;
    selectedTraceQueryMapRef.current[sid] = queryId;
    tracesMapRef.current[sid] = nextTrace;
    setSelectedTraceQueryId(queryId);
    setTrace(nextTrace);
  }, []);

  const updateSessionGraph = useCallback((sid: string, nextGraph: GraphStructure | null) => {
    graphsMapRef.current[sid] = nextGraph;
    if (sessionIdRef.current === sid) {
      setGraph(nextGraph);
    }
  }, []);

  const updateSessionActiveGraphNode = useCallback((sid: string, node: string | null) => {
    graphActiveNodesRef.current[sid] = node;
    if (sessionIdRef.current === sid) {
      setActiveGraphNode(node);
    }
  }, []);

  const applyMiddlewareInvocationEvent = useCallback(
    (
      sid: string,
      invocation: TraceMiddlewareInvocation,
      traceMeta?: { trace_id?: string; query_id?: string }
    ) => {
      const current = tracesMapRef.current[sid];
      const base: AgentTrace = current || {
        trace_id: traceMeta?.trace_id || `trace-${sid}`,
        query_id: traceMeta?.query_id,
        session_id: sid,
        started_at: invocation.created_at || Date.now() / 1000,
        completed_at: null,
        status: "running",
        spans: [],
      };
      const existing = base.middleware_invocations || [];
      const nextInvocations = existing.some((item) => item.id === invocation.id)
        ? existing.map((item) => (item.id === invocation.id ? invocation : item))
        : [...existing, invocation];
      updateSessionTrace(sid, {
        ...base,
        trace_id: traceMeta?.trace_id || base.trace_id,
        query_id: traceMeta?.query_id || base.query_id,
        middleware_invocations: nextInvocations,
      });
    },
    [updateSessionTrace]
  );

  const applyHookBoundarySnapshotEvent = useCallback(
    (
      sid: string,
      snapshot: TraceHookBoundarySnapshot,
      traceMeta?: { trace_id?: string; query_id?: string }
    ) => {
      const current = tracesMapRef.current[sid];
      const base: AgentTrace = current || {
        trace_id: traceMeta?.trace_id || `trace-${sid}`,
        query_id: traceMeta?.query_id,
        session_id: sid,
        started_at: snapshot.created_at || Date.now() / 1000,
        completed_at: null,
        status: "running",
        spans: [],
      };
      const existing = base.hook_boundary_snapshots || [];
      const nextSnapshots = existing.some((item) => item.id === snapshot.id)
        ? existing.map((item) => (item.id === snapshot.id ? snapshot : item))
        : [...existing, snapshot];
      updateSessionTrace(sid, {
        ...base,
        trace_id: traceMeta?.trace_id || base.trace_id,
        query_id: traceMeta?.query_id || base.query_id,
        hook_boundary_snapshots: nextSnapshots,
      });
    },
    [updateSessionTrace]
  );

  // Apply a trace_span_start / trace_span_end event to the in-memory trace.
  // The backend sends flattened span dictionaries; we rebuild the tree on the fly.
  const applyTraceSpanEvent = useCallback(
    (
      sid: string,
      span: TraceSpan,
      isEnd: boolean,
      traceMeta?: { trace_id?: string; query_id?: string }
    ) => {
      const current = tracesMapRef.current[sid];
      const base: AgentTrace = current || {
        trace_id: traceMeta?.trace_id || `trace-${sid}`,
        query_id: traceMeta?.query_id,
        session_id: sid,
        started_at: span.started_at,
        completed_at: null,
        status: "running",
        spans: [],
      };

      const spansById = new Map(base.spans.map((s) => [s.id, s]));
      if (!spansById.has(span.id)) {
        spansById.set(span.id, { ...span, children: [] });
      } else {
        const existing = spansById.get(span.id)!;
        spansById.set(span.id, {
          ...existing,
          ...span,
          children: existing.children || [],
        });
      }

      // Rebuild parent -> children links for all spans.
      const childrenByParent = new Map<string | null, TraceSpan[]>();
      Array.from(spansById.values()).forEach((s) => {
        const parentId = s.parent_id;
        if (!childrenByParent.has(parentId)) {
          childrenByParent.set(parentId, []);
        }
        childrenByParent.get(parentId)!.push(s);
      });

      // Reconstruct flattened list with correct children pointers.
      const visited = new Set<string>();
      const walk = (spanId: string): TraceSpan[] => {
        if (visited.has(spanId)) return [];
        visited.add(spanId);
        const s = spansById.get(spanId);
        if (!s) return [];
        const children = (childrenByParent.get(spanId) || [])
          .flatMap((child) => walk(child.id))
          .sort((a: TraceSpan, b: TraceSpan) => a.started_at - b.started_at);
        return [{ ...s, children }];
      };

      // Find the root span (parent_id == null). If missing, use the earliest span.
      let rootId: string | null = null;
      Array.from(spansById.values()).forEach((s) => {
        if (rootId === null && s.parent_id === null) {
          rootId = s.id;
        }
      });
      if (!rootId && spansById.size > 0) {
        rootId = Array.from(spansById.values())
          .sort((a: TraceSpan, b: TraceSpan) => a.started_at - b.started_at)[0].id;
      }

      const nextSpans = rootId ? walk(rootId) : [];
      const nextTrace: AgentTrace = {
        ...base,
        trace_id: traceMeta?.trace_id || base.trace_id,
        query_id: traceMeta?.query_id || base.query_id,
        spans: nextSpans,
      };

      updateSessionTrace(sid, nextTrace);
    },
    [updateSessionTrace]
  );

  // ── Session management ─────────────────────────────

  // Tracks whether the initial session-restore decision has been made. The
  // decision runs inside loadSessions (same commit as the list) — see below.
  const restoredSessionRef = useRef(false);

  const loadSessions = useCallback(() => {
    // Initial restore decision (runs once per provider mount). It must be
    // issued in the same React batch as `setSessions`/`setSessionsLoaded` so
    // the list, the selected session and the loaded flag commit in a single
    // frame. Done via an effect instead, the "sessionsLoaded but sessionId
    // still 'default'" window paints an intermediate frame (empty default
    // workbench, sidebar project row flashing selected).
    const finishInitialRestore = (list: SessionMeta[]) => {
      if (restoredSessionRef.current) return;
      restoredSessionRef.current = true;
      let target: string | null = null;
      try {
        const saved = sessionStorage.getItem("puddingclaw_session_id");
        if (
          saved
          && saved !== "default"
          && list.some((s) => s.id === saved && s.runtime_mode === "agent")
        ) {
          target = saved;
        }
      } catch {
        // ignore storage errors
      }
      if (!target) {
        const latest = [...list]
          .filter((session) => session.runtime_mode === "agent")
          .sort((a, b) => b.updated_at - a.updated_at)[0];
        if (latest && latest.id !== sessionIdRef.current) target = latest.id;
      }
      if (target) {
        // setSessionId is declared below; the closure captures the binding,
        // which is initialized by the time loadSessions is ever invoked, and
        // its logic reads only refs/stable setters, so the first-render
        // closure is safe to call.
        setSessionId(target);
      } else if (sessionIdRef.current === "default") {
        // Staying on the "default" workbench — end the initial loading gate
        // (sessionHistoryLoading starts true). If another setSessionId call
        // is already in flight it owns the loading state instead.
        setSessionHistoryLoading(false);
      }
    };
    apiListSessions()
      .then((list) => {
        sessionsRef.current = list;
        for (const session of list) {
          if (!Object.prototype.hasOwnProperty.call(analyticsModelIdsMapRef.current, session.id)) {
            analyticsModelIdsMapRef.current[session.id] = session.analytics_model_id ?? null;
          }
          if (!Object.prototype.hasOwnProperty.call(llmSelectionsMapRef.current, session.id)) {
            llmSelectionsMapRef.current[session.id] = {
              modelId: session.llm_model_id ?? null,
              thinkingLevel: session.thinking_level ?? null,
              credentialName: session.credential_name ?? null,
            };
          }
          approvalModesMapRef.current[session.id] = session.approval_mode || "smart";
          if (session.policy_epoch) {
            approvalPolicyEpochsMapRef.current[session.id] = session.policy_epoch;
          }
        }
        setSessions(list);
        finishInitialRestore(list);
        setSessionsLoaded(true);
      })
      .catch(() => {
        finishInitialRestore(sessionsRef.current);
        setSessionsLoaded(true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- setSessionId is declared below and stable in practice (refs + stable setters only)
  }, []);

  const loadProjects = useCallback(() => {
    apiListProjects()
      .then((list) => setProjects(list))
      .catch(() => setProjects([]))
      .finally(() => setProjectsLoaded(true));
  }, []);

  const registerProject = useCallback(async (path: string) => {
    try {
      const project = await apiRegisterProject(path);
      setProjects((prev) => {
        const others = prev.filter((item) => item.project_id !== project.project_id);
        return sortProjects([project, ...others]);
      });
      setCurrentProjectId(project.project_id);
      setRuntimeMode("agent");
      return project;
    } catch {
      return null;
    }
  }, [setCurrentProjectId, setRuntimeMode]);

  const updateProject = useCallback(
    async (projectId: string, update: { name?: string; pinned?: boolean }): Promise<ProjectMeta | null> => {
      try {
        const project = await apiUpdateProject(projectId, update);
        setProjects((prev) =>
          sortProjects(prev.map((item) => (item.project_id === project.project_id ? project : item)))
        );
        return project;
      } catch {
        return null;
      }
    },
    []
  );

  const trustProject = useCallback(
    async (projectId: string, state: "pending" | "trusted" | "denied"): Promise<ProjectMeta> => {
      const project = await apiSetProjectTrust(projectId, state);
      setProjects((prev) =>
        sortProjects(prev.map((item) => (item.project_id === project.project_id ? project : item)))
      );
      return project;
    },
    []
  );

  const removeProject = useCallback(
    async (projectId: string): Promise<boolean> => {
      try {
        await apiRemoveProject(projectId);
        setProjects((prev) => prev.filter((item) => item.project_id !== projectId));
        if (sessionIdRef.current === "default" && currentProjectId === projectId) {
          setCurrentProjectId(null);
        }
        return true;
      } catch {
        return false;
      }
    },
    [currentProjectId, setCurrentProjectId]
  );

  const loadMcpServers = useCallback(() => {
    apiListMcpServers()
      .then((list) => setMcpServers(list))
      .catch(() => setMcpServers([]));
  }, []);

  // Load sessions and MCP servers on mount
  useEffect(() => {
    loadSessions();
    loadProjects();
    loadMcpServers();
  }, [loadSessions, loadProjects, loadMcpServers]);

  // Headless CLI/PuddingTeams can create Sessions without this browser being
  // the initiating client. Refresh only the lightweight Session index so a
  // new Worker conversation appears in the sidebar without a page reload; do
  // not auto-switch away from the user's current conversation.
  useEffect(() => {
    const timer = window.setInterval(loadSessions, 3000);
    return () => window.clearInterval(timer);
  }, [loadSessions]);

  const setSessionId = useCallback(
    (id: string) => {
      // Switch view — do NOT abort any SSE streams (they continue in background)
      sessionIdRef.current = id;
      setActiveAttachmentPreview(null);
      setActiveSourceId(null);
      // Persist the selected session so refresh returns to it instead of
      // falling back to the latest/new-chat page.
      try {
        sessionStorage.setItem("puddingclaw_session_id", id);
      } catch {
        // ignore storage errors
      }
      setSessionIdRaw(id);
      setRawMessages(null);
      setRunActivityStatus(runActivityStatusesMapRef.current[id] ?? null);

      if (id !== "default") {
        const targetSession = sessionsRef.current.find((session) => session.id === id);
        setCurrentProjectId(targetSession?.project_id ?? null);
      }

      if (id === "default") {
        setAnalyticsModelIdRaw(analyticsModelIdsMapRef.current.default ?? null);
        setLlmModelIdRaw(llmSelectionsMapRef.current.default?.modelId ?? null);
        setThinkingLevelRaw(llmSelectionsMapRef.current.default?.thinkingLevel ?? null);
        setCredentialNameRaw(llmSelectionsMapRef.current.default?.credentialName ?? null);
        setGoalModeEnabledRaw(nextRunGoalModeMapRef.current.default ?? false);
        setApprovalModeRaw(approvalModesMapRef.current.default || "smart");
        setApprovalModeSaving(approvalModeSavingSessionsRef.current.has("default"));
        setApprovalModeError(approvalModeErrorsMapRef.current.default ?? null);
        setActiveGoal(null);
        setCurrentRun(null);
        setGoalRuns([]);
        setVerificationReport(null);
      } else {
        setAnalyticsModelIdRaw(analyticsModelIdsMapRef.current[id] ?? null);
        setLlmModelIdRaw(llmSelectionsMapRef.current[id]?.modelId ?? null);
        setThinkingLevelRaw(llmSelectionsMapRef.current[id]?.thinkingLevel ?? null);
        setCredentialNameRaw(llmSelectionsMapRef.current[id]?.credentialName ?? null);
        setApprovalModeRaw(approvalModesMapRef.current[id] || "smart");
        setApprovalModeSaving(approvalModeSavingSessionsRef.current.has(id));
        setApprovalModeError(approvalModeErrorsMapRef.current[id] ?? null);
        const cachedGoal = activeGoalsMapRef.current[id] ?? null;
        setActiveGoal(cachedGoal);
        setGoalModeEnabledRaw(Boolean(nextRunGoalModeMapRef.current[id]));
        setCurrentRun(currentRunsMapRef.current[id] ?? null);
        setGoalRuns(goalRunsMapRef.current[id] ?? []);
        setVerificationReport(verificationReportsMapRef.current[id] ?? null);
        apiGetSessionHarnessState(id)
          .then((data) => {
            const loadedGoal = visibleGoalFromHarness(data);
            const loadedRun =
              data.latest_run_id && data.runs[data.latest_run_id]
                ? data.runs[data.latest_run_id]
                : null;
            const loadedReport = loadedRun?.verification_report
              && !CONTROL_ONLY_VERIFICATION_STATUSES.has(loadedRun.verification_report.status)
                ? loadedRun.verification_report
                : null;
            const loadedGoalRuns = loadedGoal
              ? loadedGoal.run_ids
                  .map((runId) => data.runs[runId])
                  .filter((candidate): candidate is HarnessRun => Boolean(candidate))
              : [];
            activeGoalsMapRef.current[id] = loadedGoal;
            currentRunsMapRef.current[id] = loadedRun;
            goalRunsMapRef.current[id] = loadedGoalRuns;
            verificationReportsMapRef.current[id] = loadedReport;
            if (!runIsActive(loadedRun)) {
              updateSessionRunActivity(id, null);
            } else if (!runActivityStatusesMapRef.current[id]) {
              const latestDelegationEvent = [...(loadedRun?.delegation_events || [])]
                .sort((left, right) => Number(left.timestamp || 0) - Number(right.timestamp || 0))
                .at(-1);
              if (latestDelegationEvent) {
                const restoredLabels: Record<string, string> = {
                  subagent_started: "子代理执行中",
                  subagent_waiting_for_permission: "子代理等待授权",
                  context_mounted: "上下文已挂载",
                  subagent_stage_changed: "子代理正在分析与规划",
                  subagent_tool_started: latestDelegationEvent.tool
                    ? `子代理正在执行：${latestDelegationEvent.tool}`
                    : "子代理正在执行工具",
                  subagent_tool_completed: latestDelegationEvent.tool
                    ? `子代理工具已返回：${latestDelegationEvent.tool}`
                    : "子代理工具已返回",
                  subagent_completed: "子代理已完成，主 Agent 正在接收结果",
                  subagent_blocked: "子代理需要主 Agent 决策",
                  subagent_timed_out: "子代理执行超时",
                  subagent_failed: "子代理执行未完成",
                  subagent_fallback_to_parent: "主 Agent 正在接管剩余任务",
                };
                const restoredLabel = restoredLabels[String(latestDelegationEvent.type || "")];
                if (restoredLabel) {
                  const restoredTerminalTypes = new Set([
                    "subagent_completed",
                    "subagent_blocked",
                    "subagent_timed_out",
                    "subagent_failed",
                    "subagent_fallback_to_parent",
                  ]);
                  updateSessionRunActivity(id, {
                    phase: restoredTerminalTypes.has(String(latestDelegationEvent.type || ""))
                      ? "continuing"
                      : "subagent",
                    label: restoredLabel,
                    detail: latestDelegationEvent.objective?.slice(0, 96),
                  });
                }
              }
            }
            if (sessionIdRef.current === id) {
              setActiveGoal(loadedGoal);
              setGoalModeEnabledRaw(Boolean(nextRunGoalModeMapRef.current[id]));
              setCurrentRun(loadedRun);
              setGoalRuns(loadedGoalRuns);
              setVerificationReport(loadedReport);
            }
          })
          .catch(() => {});
        apiGetSessionApprovalMode(id)
          .then((policy) => {
            approvalModesMapRef.current[id] = policy.approval_mode;
            approvalPolicyEpochsMapRef.current[id] = policy.policy_epoch;
            if (sessionIdRef.current === id) {
              setApprovalModeRaw(policy.approval_mode);
            }
          })
          .catch(() => {});
      }

      // Restore cached Agent white-box state if available
      const cachedTodos = todosMapRef.current[id];
      const cachedTrace = tracesMapRef.current[id];
      const cachedTraceHistory = traceHistoriesMapRef.current[id];
      const cachedSelectedTraceQuery = selectedTraceQueryMapRef.current[id];
      const cachedGraph = graphsMapRef.current[id];
      const cachedActiveNode = graphActiveNodesRef.current[id];
      if (cachedTodos) setTodos(cachedTodos);
      else setTodos([]);
      if (cachedTrace !== undefined) setTrace(cachedTrace);
      else setTrace(null);
      if (cachedTraceHistory !== undefined) setTraceHistory(cachedTraceHistory);
      else setTraceHistory({});
      if (cachedSelectedTraceQuery !== undefined) setSelectedTraceQueryId(cachedSelectedTraceQuery);
      else setSelectedTraceQueryId(null);
      if (cachedGraph !== undefined) setGraph(cachedGraph);
      else setGraph(null);
      if (cachedActiveNode !== undefined) setActiveGraphNode(cachedActiveNode);
      else setActiveGraphNode(null);

      // Show cached messages immediately. Only uncached persisted sessions
      // need a loading state while their history is fetched.
      const hasCachedMessages = Object.prototype.hasOwnProperty.call(messagesMapRef.current, id);
      if (hasCachedMessages) {
        setSessionHistoryLoading(false);
        setMessages(messagesMapRef.current[id]);
      } else if (id === "default") {
        setSessionHistoryLoading(false);
        setMessages([]);
      } else {
        // No cache — clear and load from backend
        setMessages([]);
        setSessionHistoryLoading(true);
        apiGetSessionHistory(id)
          .then((data) => {
            if (Array.isArray(data.todos)) {
              updateSessionTodos(
                id,
                data.todos,
                data.todos_authority as TodoAuthority | undefined,
                data.todo_ledger_revision,
              );
            }
            if (data.graph !== undefined) {
              updateSessionGraph(id, data.graph || null);
            }
            const loaded = data.messages?.length ? parseHistoryMessages(data.messages) : [];
            const externalPending = data.headless_pending_input;
            const externalRequests = externalPending?.status === "pending"
              && Array.isArray(externalPending.requests)
              ? externalPending.requests.filter((request) => request?.id)
              : [];
            if (externalRequests.length > 0) {
              let target = [...loaded].reverse().find((message) => message.role === "assistant");
              if (!target) {
                target = {
                  id: `headless-pending-${externalPending?.run_id || id}`,
                  queryId: externalPending?.query_id || undefined,
                  role: "assistant",
                  content: "",
                  timestamp: Number(externalPending?.updated_at || Date.now() / 1000) * 1000,
                };
                loaded.push(target);
              }
              target.permissionRequests = externalRequests;
            }
            messagesMapRef.current[id] = loaded;
            // Only update UI if still viewing this session
            if (sessionIdRef.current === id) {
              setMessages(loaded);
            }
          })
          .catch(() => {
            // Session might not exist yet, that's OK
          })
          .finally(() => {
            if (sessionIdRef.current === id) {
              setSessionHistoryLoading(false);
            }
          });
      }

    },
    [setCurrentProjectId, updateSessionGraph, updateSessionRunActivity, updateSessionTodos]
  );

  // Reconcile Runs created outside this browser tab.  The durable Harness
  // state is intentionally the source of truth here: it works for a local
  // Worker CLI and for PuddingTeams without requiring either client to share
  // an in-memory SSE connection with this page.
  useEffect(() => {
    if (!sessionId || sessionId === "default") return;
    let stopped = false;
    let previousActive = remoteRunningSessionsRef.current.has(sessionId);

    const refreshExternalRun = async () => {
      try {
        const data = await apiGetSessionHarnessState(sessionId);
        if (stopped) return;
        const latestRun = data.latest_run_id && data.runs[data.latest_run_id]
          ? data.runs[data.latest_run_id]
          : null;
        const active = runIsActive(latestRun);
        const nextRemote = new Set(remoteRunningSessionsRef.current);
        if (active) nextRemote.add(sessionId);
        else nextRemote.delete(sessionId);
        remoteRunningSessionsRef.current = nextRemote;
        setRemoteRunningSessions(nextRemote);
        currentRunsMapRef.current[sessionId] = latestRun;
        if (sessionIdRef.current === sessionId) setCurrentRun(latestRun);

        if (active) {
          const status = String(latestRun?.status || "running");
          const waiting = status === "waiting_hitl";
          updateSessionRunActivity(sessionId, {
            phase: waiting ? "permission" : status === "evaluating" ? "verification" : "running",
            label: waiting
              ? "等待人工审批"
              : status === "preparing"
                ? "正在准备任务上下文"
                : status === "evaluating"
                  ? "正在整理最终结果"
                  : "Agent 正在执行",
            detail: latestRun?.objective?.slice(0, 120),
          });
        } else if (previousActive) {
          updateSessionRunActivity(sessionId, null);
          // A remote Run persists its authoritative assistant message only at
          // segment/terminal boundaries. Refresh once on the active→terminal
          // transition so the answer appears without a page reload.
          const history = await apiGetSessionHistory(sessionId);
          if (stopped) return;
          const loaded = history.messages?.length ? parseHistoryMessages(history.messages) : [];
          messagesMapRef.current[sessionId] = loaded;
          if (sessionIdRef.current === sessionId) setMessages(loaded);
        }
        previousActive = active;
      } catch {
        // A transient refresh failure must not clear the visible Run state;
        // the next heartbeat retries and the Worker itself remains unaffected.
      }
    };

    void refreshExternalRun();
    const timer = window.setInterval(() => void refreshExternalRun(), 1500);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [sessionId, updateSessionRunActivity]);

  const selectedSessionHasRemoteRun = remoteRunningSessions.has(sessionId);

  // A Headless Run is owned by the Harness, not by the CLI connection that
  // started it. Observe the retained/broadcast event log so this browser can
  // render the same visible content, tools and HITL boundary in real time.
  // Harness polling above remains the discovery and disconnect-recovery path.
  useEffect(() => {
    if (
      !sessionId
      || sessionId === "default"
      || !selectedSessionHasRemoteRun
      || streamingSessionsRef.current.has(sessionId)
    ) {
      return;
    }

    const latestRun = currentRunsMapRef.current[sessionId];
    const runId = String(latestRun?.run_id || "");
    const queryId = String(latestRun?.query_id || "");
    if (!runId) return;

    if (headlessObserverRunIdsRef.current[sessionId] !== runId) {
      headlessObserverRunIdsRef.current[sessionId] = runId;
      headlessObserverSequencesRef.current[sessionId] = 0;
    }

    const controller = new AbortController();
    let stopped = false;
    let targetAssistantId = "";
    let pendingTokens = "";
    let tokenTimer: number | null = null;
    let reconnectTimer: number | null = null;

    const updateTarget = (updater: (message: ChatMessage) => ChatMessage) => {
      if (!targetAssistantId) return;
      updateSessionMessages(sessionId, (previous) => previous.map((message) =>
        message.id === targetAssistantId ? updater(message) : message
      ));
    };

    const flushTokens = () => {
      if (tokenTimer !== null) {
        window.clearTimeout(tokenTimer);
        tokenTimer = null;
      }
      const content = pendingTokens;
      pendingTokens = "";
      if (!content) return;
      updateTarget((message) => {
        const segments = message.segments?.length
          ? [...message.segments]
          : [{ content: "", runId }];
        const last = segments.length - 1;
        segments[last] = {
          ...segments[last],
          runId: segments[last].runId || runId,
          content: `${segments[last].content || ""}${content}`,
        };
        return {
          ...message,
          content: segments.map((segment) => segment.content).filter(Boolean).join("\n\n"),
          segments,
        };
      });
    };

    const queueToken = (content: string) => {
      if (!content) return;
      pendingTokens += content;
      if (tokenTimer === null) {
        tokenTimer = window.setTimeout(flushTokens, 32);
      }
    };

    const loadAuthoritativeHistory = async (resetCurrentRun: boolean) => {
      const history = await apiGetSessionHistory(sessionId);
      if (stopped) return;
      const loaded = history.messages?.length ? parseHistoryMessages(history.messages) : [];
      let targetIndex = -1;
      for (let index = loaded.length - 1; index >= 0; index -= 1) {
        const message = loaded[index];
        if (
          message.role === "assistant"
          && (
            (queryId && message.queryId === queryId)
            || message.segments?.some((segment) => segment.runId === runId)
          )
        ) {
          targetIndex = index;
          break;
        }
      }
      if (targetIndex === -1 && !resetCurrentRun) {
        for (let index = loaded.length - 1; index >= 0; index -= 1) {
          if (loaded[index].role === "assistant") {
            targetIndex = index;
            break;
          }
        }
      }
      if (targetIndex === -1) {
        targetAssistantId = `headless-live-${runId}`;
        loaded.push({
          id: targetAssistantId,
          queryId: queryId || undefined,
          role: "assistant",
          content: "",
          toolCalls: [],
          timeline: [],
          segments: [{ content: "", runId }],
          timestamp: Date.now(),
        });
      } else {
        targetAssistantId = loaded[targetIndex].id;
        if (resetCurrentRun) {
          loaded[targetIndex] = {
            ...loaded[targetIndex],
            queryId: queryId || loaded[targetIndex].queryId,
            content: "",
            reasoning: undefined,
            toolCalls: [],
            timeline: [],
            segments: [{ content: "", runId }],
            permissionRequests: [],
            errorNotice: undefined,
          };
        }
      }
      assistantIdsRef.current.set(sessionId, targetAssistantId);
      messagesMapRef.current[sessionId] = loaded;
      if (sessionIdRef.current === sessionId) setMessages(loaded);
    };

    const updateTool = (eventName: "tool_start" | "tool_end", data: Record<string, unknown>) => {
      updateTarget((message) => {
        const tool = String(data.tool || "");
        const toolCallId = String(data.id || "");
        const calls = [...(message.toolCalls || [])];
        const timeline = [...(message.timeline || [])];
        const segments = message.segments?.length
          ? [...message.segments]
          : [{ content: "", runId }];
        if (eventName === "tool_start") {
          if (!toolCallId || !calls.some((call) => call.id === toolCallId)) {
            const call: ToolCall = {
              id: toolCallId,
              tool,
              input: String(data.input || ""),
              status: "running",
              startedAt: Date.now(),
            };
            calls.push(call);
            addToolToTimeline(timeline, call);
            const last = segments.length - 1;
            const segmentTimeline = [...(segments[last].timeline || [])];
            addToolToTimeline(segmentTimeline, call);
            segments[last] = { ...segments[last], runId: segments[last].runId || runId, timeline: segmentTimeline };
          }
        } else {
          let callIndex = toolCallId ? calls.findIndex((call) => call.id === toolCallId) : -1;
          if (callIndex === -1) {
            for (let index = calls.length - 1; index >= 0; index -= 1) {
              if (calls[index].tool === tool && calls[index].status === "running") {
                callIndex = index;
                break;
              }
            }
          }
          const updates: Partial<ToolCall> = {
            tool,
            output: String(data.output || ""),
            status: "done",
            endedAt: Date.now(),
            summary_source: data.summary_source as string | undefined,
            is_error: Boolean(data.is_error),
          };
          if (callIndex !== -1) calls[callIndex] = { ...calls[callIndex], ...updates };
          updateToolInTimeline(timeline, toolCallId, tool, updates);
          for (let index = segments.length - 1; index >= 0; index -= 1) {
            const segmentTimeline = [...(segments[index].timeline || [])];
            const ownsCall = segmentTimeline.some((item) =>
              item.type === "tool"
              && ((toolCallId && item.toolCall.id === toolCallId)
                || (!toolCallId && item.toolCall.tool === tool && item.toolCall.status === "running"))
            );
            if (!ownsCall) continue;
            updateToolInTimeline(segmentTimeline, toolCallId, tool, updates);
            segments[index] = { ...segments[index], timeline: segmentTimeline };
            break;
          }
        }
        return { ...message, toolCalls: calls, timeline, segments };
      });
    };

    const scheduleReconnect = () => {
      if (stopped || reconnectTimer !== null) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        if (!stopped) void observe(true);
      }, 750);
    };

    const observe = async (reconnecting = false) => {
      let sawTerminalEvent = false;
      try {
        const after = headlessObserverSequencesRef.current[sessionId] || 0;
        if (!reconnecting) {
          await loadAuthoritativeHistory(after === 0);
        }
        if (stopped) return;

        for await (const event of streamSessionEvents(sessionId, controller.signal, after)) {
          if (stopped) break;
          const sequence = Number(event.id || 0);
          if (Number.isFinite(sequence) && sequence > 0) {
            headlessObserverSequencesRef.current[sessionId] = sequence;
          }

          if (event.event === "token") {
            queueToken(String(event.data.content || ""));
            continue;
          }
          flushTokens();

          if (event.event === "run_starting") {
            updateSessionRunActivity(sessionId, { phase: "running", label: "Worker 已接收任务" });
          } else if (event.event === "task_preflight_started") {
            updateSessionRunActivity(sessionId, { phase: "running", label: "正在准备任务上下文" });
          } else if (event.event === "run_started") {
            const run = event.data.run as unknown as HarnessRun;
            if (run?.run_id) {
              currentRunsMapRef.current[sessionId] = run;
              if (sessionIdRef.current === sessionId) setCurrentRun(run);
            }
            updateSessionRunActivity(sessionId, { phase: "running", label: "Agent 正在处理" });
          } else if (event.event === "segment_break") {
            updateTarget((message) => ({
              ...message,
              segments: [...(message.segments || [{ content: "", runId }]), { content: "", runId }],
            }));
          } else if (event.event === "segment_content_replaced") {
            const replacement = String(event.data.content || "");
            updateTarget((message) => {
              const segments = message.segments?.length ? [...message.segments] : [{ content: "", runId }];
              const last = segments.length - 1;
              segments[last] = { ...segments[last], content: replacement, runId: segments[last].runId || runId };
              return {
                ...message,
                content: segments.map((segment) => segment.content).filter(Boolean).join("\n\n"),
                segments,
              };
            });
          } else if (event.event === "tool_start" || event.event === "tool_end") {
            updateTool(event.event, event.data);
            updateSessionRunActivity(sessionId, {
              phase: "running",
              label: event.event === "tool_start"
                ? `正在执行：${String(event.data.tool || "工具")}`
                : "Agent 正在继续处理",
            });
          } else if (event.event === "permission_required") {
            const request = event.data as unknown as PermissionRequest;
            updateSessionRunActivity(sessionId, { phase: "permission", label: "等待你的授权" });
            updateTarget((message) => {
              const requests = message.permissionRequests || [];
              const matches = (item: PermissionRequest) => item.id === request.id
                || Boolean(request.semantic_key && item.semantic_key === request.semantic_key);
              return {
                ...message,
                permissionRequests: requests.some(matches)
                  ? requests.map((item) => matches(item) ? request : item)
                  : [...requests, request],
              };
            });
          } else if (event.event === "permission_resolved") {
            const requestId = String(event.data.request_id || "");
            updateTarget((message) => ({
              ...message,
              permissionRequests: (message.permissionRequests || []).map((request) =>
                request.id === requestId ? { ...request, status: "resolved" } : request
              ),
            }));
            updateSessionRunActivity(sessionId, { phase: "continuing", label: "授权已处理，Agent 正在继续" });
          } else if (event.event === "final_response") {
            const finalContent = String(event.data.content || "");
            updateTarget((message) => {
              const segments = message.segments?.length ? [...message.segments] : [{ content: "", runId }];
              const currentContent = segments.map((segment) => segment.content).filter(Boolean).join("\n\n");
              if (finalContent && !currentContent) {
                const last = segments.length - 1;
                segments[last] = { ...segments[last], content: finalContent, runId: segments[last].runId || runId };
              }
              return {
                ...message,
                queryId: String(event.data.query_id || message.queryId || "") || undefined,
                content: currentContent || finalContent || message.content,
                segments,
                verificationSummary: String(event.data.verification_summary || "") || message.verificationSummary,
              };
            });
            updateSessionRunActivity(sessionId, { phase: "verification", label: "正在整理最终结果" });
          } else if (event.event === "stream_reset_required") {
            await loadAuthoritativeHistory(false);
          } else if (event.event === "error") {
            const message = String(event.data.message || event.data.error || "Agent 运行失败");
            updateTarget((current) => markMessageError(current, message));
            updateSessionRunActivity(sessionId, null);
          } else if (event.event === "done") {
            sawTerminalEvent = true;
            updateSessionRunActivity(sessionId, null);
          }
        }

        flushTokens();
        if (stopped) return;
        if (sawTerminalEvent || !runIsActive(currentRunsMapRef.current[sessionId])) {
          await loadAuthoritativeHistory(false);
        } else {
          scheduleReconnect();
        }
      } catch (error) {
        flushTokens();
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          // Harness polling and Session History remain authoritative fallbacks.
          updateSessionRunActivity(sessionId, { phase: "running", label: "正在恢复运行状态" });
          scheduleReconnect();
        }
      }
    };

    void observe();
    return () => {
      stopped = true;
      controller.abort();
      if (tokenTimer !== null) window.clearTimeout(tokenTimer);
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
    };
  }, [
    selectedSessionHasRemoteRun,
    sessionId,
    updateSessionMessages,
    updateSessionRunActivity,
  ]);

  // Trace history is intentionally lazy: switching conversations reads only
  // messages. The large sidecar is fetched when the user opens Trace 看板.
  useEffect(() => {
    if (workspaceView !== "trace" || !sessionId) return;
    if (traceHistoriesMapRef.current[sessionId] !== undefined) return;
    apiGetSessionTraces(sessionId)
      .then((data) => {
        const histories = data.traces || {};
        const selected = data.latest_query_id || data.trace?.query_id || null;
        traceHistoriesMapRef.current[sessionId] = histories;
        selectedTraceQueryMapRef.current[sessionId] = selected;
        tracesMapRef.current[sessionId] = data.trace || (selected ? histories[selected] : null) || null;
        if (data.todos) {
          updateSessionTodos(
            sessionId,
            data.todos,
            data.todos_authority as TodoAuthority | undefined,
            data.todo_ledger_revision,
          );
        }
        if (data.graph) graphsMapRef.current[sessionId] = data.graph;
        if (sessionIdRef.current === sessionId) {
          setTraceHistory(histories);
          setSelectedTraceQueryId(selected);
          setTrace(tracesMapRef.current[sessionId]);
          if (data.todos) setTodos(todosMapRef.current[sessionId] || data.todos);
          if (data.graph) setGraph(data.graph);
        }
      })
      .catch(() => {});
  }, [workspaceView, sessionId, updateSessionTodos]);

  // The initial restore decision is made synchronously inside loadSessions so
  // it commits in the same frame as the sessions list. This effect only
  // handles later sessions-list changes: if the current session vanished
  // (e.g. deleted elsewhere), fall back to the latest — but never auto-switch
  // away from the placeholder "default" session, since the user may have
  // clicked "New Chat" and expects to start a fresh conversation.
  useEffect(() => {
    if (!sessionsLoaded || !restoredSessionRef.current) return;
    if (sessionIdRef.current === "default") return;
    if (sessions.some((s) => s.id === sessionIdRef.current)) return;
    const latest = [...sessions]
      .filter((session) => session.runtime_mode === "agent")
      .sort((a, b) => b.updated_at - a.updated_at)[0];
    if (latest && latest.id !== sessionIdRef.current) {
      setSessionId(latest.id);
    }
  }, [sessions, sessionsLoaded, setSessionId]);

  const createSession = useCallback(async (): Promise<string | null> => {
    const originSessionId = sessionIdRef.current;
    const existing = createSessionPromisesRef.current.get(originSessionId);
    if (existing) return existing;
    const snapshot = {
      analyticsModelId: analyticsModelIdsMapRef.current[originSessionId] ?? null,
      llmSelection: llmSelectionsMapRef.current[originSessionId] || {
        modelId: null,
        thinkingLevel: null,
        credentialName: null,
      },
      approvalMode: (approvalModesMapRef.current[originSessionId] || "smart") as ApprovalMode,
      goalModeEnabled: nextRunGoalModeMapRef.current[originSessionId] ?? false,
      runtimeMode,
      projectId: currentProjectId,
    };
    const creation = (async (): Promise<string | null> => {
      try {
        const meta = await apiCreateSession({
          analytics_model_id: snapshot.analyticsModelId,
          llm_model_id: snapshot.llmSelection.modelId,
          thinking_level: snapshot.llmSelection.thinkingLevel,
          credential_name: snapshot.llmSelection.credentialName,
          approval_mode: snapshot.approvalMode,
          runtime_mode: snapshot.runtimeMode,
          project_id: snapshot.projectId,
        });
        analyticsModelIdsMapRef.current[meta.id] = snapshot.analyticsModelId;
        llmSelectionsMapRef.current[meta.id] = { ...snapshot.llmSelection };
        approvalModesMapRef.current[meta.id] = meta.approval_mode;
        approvalPolicyEpochsMapRef.current[meta.id] = meta.policy_epoch;
        nextRunGoalModeMapRef.current[meta.id] = snapshot.goalModeEnabled;
        setSessions((prev) => {
          const next = [
            {
              id: meta.id,
              title: meta.title,
              updated_at: meta.updated_at || Date.now() / 1000,
              runtime_mode: meta.runtime_mode || snapshot.runtimeMode,
              project_id: meta.project_id ?? snapshot.projectId,
              analytics_model_id: snapshot.analyticsModelId,
              llm_model_id: snapshot.llmSelection.modelId,
              thinking_level: snapshot.llmSelection.thinkingLevel,
              credential_name: snapshot.llmSelection.credentialName,
              approval_mode: meta.approval_mode,
              policy_epoch: meta.policy_epoch,
              policy_version: meta.policy_version,
            },
            ...prev,
          ];
          sessionsRef.current = next;
          return next;
        });
        // Pre-populate the message cache so setSessionId shows the empty state
        // immediately and doesn't overwrite locally-added messages with a later
        // history fetch.
        messagesMapRef.current[meta.id] = [];
        if (sessionIdRef.current === originSessionId) {
          setSessionId(meta.id);
        }
        if (originSessionId === "default") {
          analyticsModelIdsMapRef.current.default = null;
          llmSelectionsMapRef.current.default = { modelId: null, thinkingLevel: null, credentialName: null };
          approvalModesMapRef.current.default = "smart";
          approvalPolicyEpochsMapRef.current.default = 1;
          nextRunGoalModeMapRef.current.default = false;
        }
        return meta.id;
      } catch {
        return null;
      } finally {
        createSessionPromisesRef.current.delete(originSessionId);
      }
    })();
    createSessionPromisesRef.current.set(originSessionId, creation);
    return creation;
  }, [setSessionId, runtimeMode, currentProjectId]);

  // ── Ensure a real session exists before sending ────────
  const ensureSession = useCallback(async () => {
    // If we're on the placeholder "default" session, or the current session
    // isn't in the loaded list, create a fresh one lazily.
    if (sessionIdRef.current === "default" || !sessions.some((s) => s.id === sessionIdRef.current)) {
      await createSession();
    }
  }, [sessions, createSession]);

  const renameSessionFn = useCallback(async (id: string, title: string) => {
    try {
      await apiRenameSession(id, title);
      setSessions((prev) =>
        prev.map((s) => (s.id === id ? { ...s, title } : s))
      );
    } catch {
      // ignore
    }
  }, []);

  const deleteSessionFn = useCallback(
    async (id: string) => {
      try {
        // Abort if this session is streaming
        const controller = abortControllersRef.current.get(id);
        if (controller) {
          controller.abort();
          abortControllersRef.current.delete(id);
        }
        // Clean up map entries
        delete messagesMapRef.current[id];
        delete analyticsModelIdsMapRef.current[id];
        delete llmSelectionsMapRef.current[id];
        delete llmSelectionSaveChainsRef.current[id];
        assistantIdsRef.current.delete(id);
        updateStreamingSessions((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });

        await apiDeleteSession(id);
        setSessions((prev) => prev.filter((s) => s.id !== id));
        if (sessionIdRef.current === id) {
          setSessionId("default");
        }
      } catch {
        // ignore
      }
    },
    [setSessionId, updateStreamingSessions]
  );

  const loadRawMessages = useCallback(() => {
    if (!sessionId) return;
    apiGetRawMessages(sessionId)
      .then((data) => setRawMessages(data.messages))
      .catch(() => setRawMessages(null));
  }, [sessionId]);

  // ── Compression ──────────────────────────────────────

  const compactCurrentAgentSession = useCallback(async (focus = "") => {
    if (runtimeMode !== "agent") {
      throw new Error("/compact 只适用于 Agent Session。");
    }
    if (!sessionId || sessionId === "default") {
      throw new Error("当前还没有可压缩的已完成 Agent Session。");
    }
    if (isCompressing || streamingSessions.has(sessionId)) {
      throw new Error("当前 Session 正在运行或维护，请完成后再执行 /compact。");
    }
    setIsCompressing(true);
    const compactionStartedAt = Date.now();
    setMaintenanceStatus({
      phase: "manual_compaction",
      message: "正在压缩上下文…",
      usedTokensBefore: contextUsage.used,
      triggerTokens: contextUsage.total,
      startedAt: compactionStartedAt,
    });
    try {
      const result = await apiCompactAgentSession(sessionId, focus.trim());
      if (sessionIdRef.current === sessionId) {
        const total = contextUsage.total;
        setContextUsage({
          used: result.tokens_after,
          total,
          percentage: total > 0 ? Math.min(100, result.tokens_after / total * 100) : 0,
          measured: true,
        });
        setMaintenanceStatus({
          phase: "manual_compaction_done",
          message: `Agent 上下文已压缩：${result.tokens_before.toLocaleString()} → ${result.tokens_after.toLocaleString()} tokens（减少 ${result.reduction_percentage}%）。`,
          startedAt: compactionStartedAt,
        });
        window.setTimeout(() => {
          if (sessionIdRef.current === sessionId) {
            setMaintenanceStatus((current) =>
              current?.phase === "manual_compaction_done" ? null : current,
            );
          }
        }, 5000);
      }
      return result;
    } catch (error) {
      if (sessionIdRef.current === sessionId) setMaintenanceStatus(null);
      throw error;
    } finally {
      setIsCompressing(false);
    }
  }, [contextUsage.total, contextUsage.used, isCompressing, runtimeMode, sessionId, streamingSessions]);

  // ── RAG mode ────────────────────────────────────────

  // ── Clear session ───────────────────────────────────

  const clearCurrentSession = useCallback(async () => {
    if (isCompressing || streamingSessions.has(sessionId)) return;
    setIsCompressing(true);
    try {
      await apiClearSession(sessionId);
      messagesMapRef.current[sessionId] = [];
      todosMapRef.current[sessionId] = [];
      todoLedgerRevisionsMapRef.current[sessionId] = 0;
      delete todoAuthoritiesMapRef.current[sessionId];
      tracesMapRef.current[sessionId] = null;
      traceHistoriesMapRef.current[sessionId] = {};
      selectedTraceQueryMapRef.current[sessionId] = null;
      activeGoalsMapRef.current[sessionId] = null;
      currentRunsMapRef.current[sessionId] = null;
      verificationReportsMapRef.current[sessionId] = null;
      nextRunGoalModeMapRef.current[sessionId] = false;
      if (sessionIdRef.current === sessionId) {
        setMessages([]);
        setTodos([]);
        setTrace(null);
        setTraceHistory({});
        setSelectedTraceQueryId(null);
        setGoalModeEnabledRaw(false);
        setActiveGoal(null);
        setCurrentRun(null);
        setVerificationReport(null);
      }
      setRawMessages(null);
    } catch {
      // ignore
    } finally {
      setIsCompressing(false);
    }
  }, [isCompressing, sessionId]);

  // ── Stop streaming (current session only) ───────────

  const stopStreaming = useCallback(() => {
    const controller = abortControllersRef.current.get(sessionId);
    if (controller) {
      controller.abort();
      abortControllersRef.current.delete(sessionId);
      const targetAssistantId = assistantIdsRef.current.get(sessionId);
      updateSessionMessages(sessionId, (prev) =>
        prev.map((message) =>
          message.role === "assistant" && (!targetAssistantId || message.id === targetAssistantId)
            ? markMessageInterrupted(
                finalizeRunningToolsInMessage(message, "Stream cancelled before this tool returned a result.")
              )
            : message
        )
      );
      // Keep this Session locked until the Backend has durably moved the Run
      // into a terminal state. Releasing it here allows an immediate follow-up
      // to race the cancellation commit and fail with "already has active Run".
      updateSessionRunActivity(sessionId, {
        phase: "running",
        label: "正在停止",
      });
    }
  }, [sessionId, updateSessionMessages, updateSessionRunActivity]);

  const pauseActiveGoal = useCallback(async () => {
    if (!activeGoal) throw new Error("当前没有可暂停的目标");
    const next = await apiPauseGoal(sessionId, activeGoal.goal_id);
    activeGoalsMapRef.current[sessionId] = next;
    if (sessionIdRef.current === sessionId) setActiveGoal(next);
    if (next.requested_status) {
      stopStreaming();
      const settled = await waitForGoalState(
        sessionId,
        activeGoal.goal_id,
        (goal) =>
          goal.status === "paused"
          && !goal.requested_status
          && !goal.current_run_id,
      );
      activeGoalsMapRef.current[sessionId] = settled;
      if (sessionIdRef.current === sessionId) setActiveGoal(settled);
      return settled;
    }
    return next;
  }, [activeGoal, sessionId, stopStreaming]);

  const resumeActiveGoal = useCallback(async () => {
    if (!activeGoal) throw new Error("当前没有可恢复的目标");
    const next = await apiResumeGoal(sessionId, activeGoal.goal_id);
    activeGoalsMapRef.current[sessionId] = next;
    if (sessionIdRef.current === sessionId) setActiveGoal(next);
    return next;
  }, [activeGoal, sessionId]);

  const cancelActiveGoal = useCallback(async () => {
    if (!activeGoal) return;
    const next = await apiCancelGoal(sessionId, activeGoal.goal_id);
    if (!next.requested_status && next.status === "cancelled") {
      activeGoalsMapRef.current[sessionId] = null;
      if (sessionIdRef.current === sessionId) setActiveGoal(null);
      return;
    }
    activeGoalsMapRef.current[sessionId] = next;
    if (sessionIdRef.current === sessionId) setActiveGoal(next);
    stopStreaming();
    await waitForGoalState(
      sessionId,
      activeGoal.goal_id,
      (goal) => goal.status === "cancelled",
    );
    activeGoalsMapRef.current[sessionId] = null;
    if (sessionIdRef.current === sessionId) setActiveGoal(null);
  }, [activeGoal, sessionId, stopStreaming]);

  const extendActiveGoalBudget = useCallback(async (additionalRounds: number) => {
    if (!activeGoal) throw new Error("当前没有可追加预算的目标");
    const next = await apiExtendGoalBudget(
      sessionId,
      activeGoal.goal_id,
      additionalRounds,
    );
    activeGoalsMapRef.current[sessionId] = next;
    if (sessionIdRef.current === sessionId) setActiveGoal(next);
    return next;
  }, [activeGoal, sessionId]);

  const updateActiveGoal = useCallback(async (objective: string) => {
    if (!activeGoal) throw new Error("当前没有可编辑的目标");
    const next = await apiUpdateGoalObjective(
      sessionId,
      activeGoal.goal_id,
      objective,
      activeGoal.objective_revision || 1,
    );
    activeGoalsMapRef.current[sessionId] = next;
    setActiveGoal(next);
    return next;
  }, [activeGoal, sessionId]);

  // ── Send message ───────────────────────────────────

  const sendMessage = useCallback(
    async (
      text: string,
      attachments: AgentAttachment[] = [],
      options: SendMessageOptions = {},
    ): Promise<boolean> => {
      // Guard: only check if CURRENT session is streaming (other sessions can be)
      const originSendSessionId = sessionIdRef.current;
      let reservationSessionId = originSendSessionId;
      if (originSendSessionId === "default") {
        // Fast Refresh or a frontend deployment can leave an older in-flight
        // closure holding the placeholder reservation after its actual stream
        // has already moved to a durable session id. Reclaim only a provably
        // orphaned placeholder; a live create/stream still keeps double-submit
        // protection.
        sendReservationsRef.current = releaseOrphanedPlaceholderLock(
          sendReservationsRef.current,
          originSendSessionId,
          {
            creationPending: createSessionPromisesRef.current.has(originSendSessionId),
            streaming: streamingSessionsRef.current.has(originSendSessionId),
          },
        );
      }
      if (
        (!text.trim() && attachments.length === 0) ||
        streamingSessionsRef.current.has(originSendSessionId) ||
        isCompressing ||
        sendReservationsRef.current.has(originSendSessionId)
      ) {
        return false;
      }
      sendReservationsRef.current.add(originSendSessionId);

      try {

      // Lazily create a session only when we are on the placeholder "default"
      // session (e.g. after the user clicked "New Chat" or triggered a skill
      // from another page). Normal follow-up messages in an existing session
      // must stay in that session.
      let sendSessionId = sessionIdRef.current;
      // Freeze the options the user saw at submit time. Session creation,
      // attachment upload and React renders may finish later, but this Run must
      // not silently inherit a newer draft selection.
      const runOptions = {
        runtimeMode,
        projectId: currentProjectId,
        analyticsModelId:
          analyticsModelIdsMapRef.current[sendSessionId] ?? analyticsModelId ?? null,
        llmSelection: llmSelectionsMapRef.current[sendSessionId] || {
          modelId: llmModelId,
          thinkingLevel,
          credentialName,
        },
        requestedGoalMode:
          options.goalControlAction === "start"
            ? true
            : nextRunGoalModeMapRef.current[sendSessionId] ?? goalModeEnabled,
      };
      if (sendSessionId === "default") {
        const createdSessionId = await createSession();
        if (!createdSessionId) return false;
        sendSessionId = createdSessionId;
      }

      if (reservationSessionId !== sendSessionId) {
        sendReservationsRef.current = rebindSessionScopedLock(
          sendReservationsRef.current,
          reservationSessionId,
          sendSessionId,
        );
        reservationSessionId = sendSessionId;
      }
      options.onSessionResolved?.(sendSessionId);

      // Capture the sessionId at send time (stable for entire SSE lifecycle)

      // Keep slash Skill hints in the user's original message. The backend
      // treats `/skill-name` as one high-confidence routing signal and still
      // performs semantic matching for every other Skill the task may need.
      // Rewriting it into an internal marker made the chat and Goal objective
      // disagree and incorrectly suggested an exclusive Skill selection.
      const processedText = text.trim() || "请分析这张图片。";

      const userMsg: ChatMessage = {
        id: `user-${Date.now()}`,
        role: "user",
        content: text.trim(),
        attachments: attachments.length ? attachments : undefined,
        timestamp: Date.now(),
      };

      const goalForRun =
        activeGoalsMapRef.current[sendSessionId] ||
        (sessionIdRef.current === sendSessionId ? activeGoal : null);
      if (options.goalControlAction === "start" && !goalForRun) {
        return false;
      }
      const goalModeForRun =
        runOptions.runtimeMode === "agent" &&
        runOptions.requestedGoalMode;
      const contextGoalIdForRun =
        runOptions.runtimeMode === "agent" && goalForRun?.status === "active"
          ? goalForRun.goal_id
          : null;

      const firstAssistantId = `assistant-${Date.now()}`;
      const assistantMsg: ChatMessage = {
        id: firstAssistantId,
        role: "assistant",
        content: "",
        toolCalls: [],
        timeline: [],
        segments: [{
          content: "",
        }],
        timestamp: Date.now(),
      };

      // Per-session tracking
      assistantIdsRef.current.set(sendSessionId, firstAssistantId);
      updateSessionMessages(
        sendSessionId,
        (prev) => [
          ...prev,
          ...(options.hiddenUserMessage ? [] : [userMsg]),
          assistantMsg,
        ],
      );

      // Mark this session as streaming
      updateStreamingSessions((prev) => new Set(prev).add(sendSessionId));
      updateSessionRunActivity(sendSessionId, {
        phase: "running",
        label: runOptions.runtimeMode === "agent" ? "Agent 正在处理" : "正在生成回复",
      });

      const controller = new AbortController();
      abortControllersRef.current.set(sendSessionId, controller);

      // Helper: update messages for this specific session
      const updateMsgs = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
        updateSessionMessages(sendSessionId, updater);
      };

      // Helper: get current assistant ID for this session
      const getAssistantId = () => assistantIdsRef.current.get(sendSessionId) || "";
      const appendLifecycleActivity = (
        label: string,
        status = "",
        activityId = "",
        detail = "",
      ) => {
        const targetId = getAssistantId();
        updateMsgs((prev) => prev.map((message) => {
          if (message.id !== targetId) return message;
          const segments = message.segments?.length
            ? [...message.segments]
            : [{ content: "" } as MessageSegment];
          const last = segments.length - 1;
          const timeline = [...(segments[last].timeline || [])];
          const id = activityId || `activity-${Date.now()}-${timeline.length}`;
          const nextActivity = {
            type: "activity" as const,
            label,
            detail: detail || undefined,
            status,
            id,
          };
          const existingIndex = activityId
            ? timeline.findIndex((item) => item.type === "activity" && item.id === activityId)
            : -1;
          if (existingIndex >= 0) timeline[existingIndex] = nextActivity;
          else timeline.push(nextActivity);
          segments[last] = {
            ...segments[last],
            timeline,
          };
          return { ...message, segments };
        }));
      };
      const settleLifecycleActivities = (activityPrefix: string, status: string) => {
        const targetId = getAssistantId();
        updateMsgs((prev) => prev.map((message) => {
          if (message.id !== targetId) return message;
          const settleTimeline = (timeline: TimelineItem[] | undefined) => {
            if (activityPrefix === "verification-") {
              return settleRunningVerificationActivities(timeline || [], status);
            }
            return (timeline || []).map((item) => {
            if (item.type !== "activity"
              || !item.id.startsWith(activityPrefix)
              || item.status !== "running") {
              return item;
            }
            return { ...item, status };
          });
          };
          const segments = (message.segments || []).map((segment) => ({
            ...segment,
            timeline: settleTimeline(segment.timeline),
          }));
          return {
            ...message,
            timeline: settleTimeline(message.timeline),
            segments,
          };
        }));
      };
      // Keep network consumption independent from React rendering. SSE frames
      // are drained immediately into this buffer, while the UI receives one
      // immutable state update roughly every 32ms. This prevents both React
      // auto-batching an entire burst and client-side backpressure/replay.
      let pendingTokenContent = "";
      let tokenFlushTimer: number | null = null;
      const flushPendingTokens = () => {
        if (tokenFlushTimer !== null) {
          window.clearTimeout(tokenFlushTimer);
          tokenFlushTimer = null;
        }
        if (!pendingTokenContent) return;
        const content = pendingTokenContent;
        pendingTokenContent = "";
        const targetId = getAssistantId();
        updateMsgs((prev) => {
          const updated = [...prev];
          const idx = updated.findIndex((m) => m.id === targetId);
          if (idx === -1) return prev;
          const segments = updated[idx].segments
            ? [...updated[idx].segments]
            : [{ content: updated[idx].content }];
          const lastSegIdx = segments.length - 1;
          segments[lastSegIdx] = {
            ...segments[lastSegIdx],
            content: segments[lastSegIdx].content + content,
          };
          updated[idx] = {
            ...updated[idx],
            content: updated[idx].content + content,
            segments,
          };
          return updated;
        });
      };
      const queueToken = (content: string) => {
        if (!content) return;
        pendingTokenContent += content;
        if (tokenFlushTimer === null) {
          tokenFlushTimer = window.setTimeout(flushPendingTokens, 32);
        }
      };
      let pendingReasoningContent = "";
      let reasoningFlushTimer: number | null = null;
      const activeToolActivities = new Map<string, RunActivityStatus & { tool: string }>();
      let anonymousToolSequence = 0;
      let correctingVerificationGap = false;
      const refreshActivityAfterTool = () => {
        const active = Array.from(activeToolActivities.values()).at(-1);
        updateSessionRunActivity(
          sendSessionId,
          active || (correctingVerificationGap
            ? { phase: "revision", label: "Agent 正在处理" }
            : {
                phase: "running",
                label: runOptions.runtimeMode === "agent" ? "Agent 正在处理" : "正在生成回复",
              })
        );
      };
      const flushPendingReasoning = () => {
        if (reasoningFlushTimer !== null) {
          window.clearTimeout(reasoningFlushTimer);
          reasoningFlushTimer = null;
        }
        if (!pendingReasoningContent) return;
        const content = pendingReasoningContent;
        pendingReasoningContent = "";
        const targetId = getAssistantId();
        updateMsgs((prev) => {
          const updated = [...prev];
          const idx = updated.findIndex((m) => m.id === targetId);
          if (idx === -1) return prev;
          const timeline = updated[idx].timeline
            ? [...updated[idx].timeline]
            : [];
          appendReasoningToTimeline(timeline, content);
          const segments = updated[idx].segments
            ? [...updated[idx].segments]
            : undefined;
          if (segments) {
            const lastSegIdx = segments.length - 1;
            const segTimeline = segments[lastSegIdx].timeline
              ? [...segments[lastSegIdx].timeline]
              : [];
            appendReasoningToTimeline(segTimeline, content);
            segments[lastSegIdx] = {
              ...segments[lastSegIdx],
              reasoning: `${segments[lastSegIdx].reasoning || ""}${content}`,
              timeline: segTimeline,
            };
          }
          updated[idx] = {
            ...updated[idx],
            reasoning: `${updated[idx].reasoning || ""}${content}`,
            timeline,
            segments,
          };
          return updated;
        });
      };
      const queueReasoning = (content: string) => {
        if (!content) return;
        pendingReasoningContent += content;
        if (reasoningFlushTimer === null) {
          reasoningFlushTimer = window.setTimeout(flushPendingReasoning, 80);
        }
      };

      try {
        const eventStream = streamAgent(
          processedText,
          sendSessionId,
          runOptions.projectId,
          controller.signal,
          userId,
          attachments,
          runOptions.analyticsModelId,
          goalModeForRun,
          options.goalControlAction === "start" ? goalForRun?.goal_id || null : null,
          contextGoalIdForRun,
          options.goalControlAction || null,
          options.skillHints,
          runOptions.llmSelection.modelId,
          runOptions.llmSelection.thinkingLevel,
          runOptions.llmSelection.credentialName,
        );

        for await (const event of eventStream) {
          if (controller.signal.aborted) break;

          if (event.event === "model_stream_preview") {
            // The provider chunk itself now streams through the ordinary
            // token/reasoning events. Keep previews in Trace only; rendering
            // them here would duplicate the authoritative token stream.
            continue;
          }

          if (event.event === "model_transport_interrupted") {
            const retrying = event.data.next_action === "retry_same_model_node";
            appendLifecycleActivity(
              retrying ? "模型连接中断，正在重试" : "模型连接中断",
              retrying ? "running" : "error",
              "model-transport-recovery",
            );
            updateSessionRunActivity(sendSessionId, {
              phase: "running",
              label: retrying ? "模型连接中断，正在重试" : "模型连接中断",
            });
            continue;
          }

          if (event.event === "model_response_recovery_started") {
            appendLifecycleActivity(
              "模型回答不完整，正在自动恢复",
              "running",
              "model-response-recovery",
              "保留已完成的工具结果和 Todo，从当前 Run 原地继续一次。",
            );
            updateSessionRunActivity(sendSessionId, {
              phase: "running",
              label: "模型回答不完整，正在自动恢复",
            });
            continue;
          }

          if (event.event === "model_response_incomplete") {
            appendLifecycleActivity(
              "模型未形成完整回答",
              "error",
              "model-response-recovery",
              "自动恢复已用尽；当前进度已保留，可从同一会话继续。",
            );
            continue;
          }

          if (event.event === "model_stream_attempt") {
            if (event.data.status === "completed") {
              refreshActivityAfterTool();
            }
            continue;
          }

          if (event.event === "usage_summary") {
            const usageSummary = normalizeUsageSummary(event.data);
            if (usageSummary) {
              const targetId = getAssistantId();
              updateMsgs((previous) => previous.map((message) =>
                message.id === targetId ? { ...message, usageSummary } : message
              ));
            }
            continue;
          }

          if (event.event === "token") {
            setMaintenanceStatus((current) =>
              current?.phase === "reasoning" ? null : current
            );
            queueToken((event.data.content as string) || "");
            continue;
          }

          if (event.event === "final_response") {
            setMaintenanceStatus(null);
            // `token` events own normal visible output. `final_response` is a
            // terminal marker and a fallback only for verification-gated Runs
            // that intentionally published no text tokens. Material stream
            // corrections use the dedicated `segment_content_replaced` event.
            flushPendingTokens();
            const targetId = getAssistantId();
            const finalContent = String(event.data.content || "");
            const verificationSummary = String(event.data.verification_summary || "").trim();
            updateMsgs((prev) => prev.map((message) => {
              if (message.id !== targetId) return message;
              const segments = message.segments?.length
                ? [...message.segments]
                : [{ content: "" } as MessageSegment];
              const currentContent = segments
                .map((segment) => segment.content)
                .filter(Boolean)
                .join("\n\n");
              if (finalContent && !currentContent) {
                const last = segments.length - 1;
                segments[last] = { ...segments[last], content: finalContent };
              }
              return {
                ...message,
                queryId: String(event.data.query_id || message.queryId || "") || undefined,
                content: currentContent ? message.content : finalContent || message.content,
                segments,
                verificationSummary: verificationSummary || message.verificationSummary,
                usageSummary:
                  normalizeUsageSummary(event.data.usage_summary) || message.usageSummary,
              };
            }));
            updateSessionRunActivity(sendSessionId, {
              phase: "verification",
              label: String(event.data.verification_summary || "正在整理最终结果"),
            });
            continue;
          }

          if (event.event === "segment_content_replaced") {
            flushPendingTokens();
            const targetId = getAssistantId();
            const replacement = String(event.data.content || "");
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === targetId);
              if (idx === -1) return prev;
              const segments = updated[idx].segments?.length
                ? [...updated[idx].segments]
                : [{ content: "" }];
              const last = segments.length - 1;
              segments[last] = { ...segments[last], content: replacement };
              updated[idx] = {
                ...updated[idx],
                segments,
                content: segments.map((segment) => segment.content).filter(Boolean).join("\n\n"),
              };
              return updated;
            });
            continue;
          }

          if (event.event === "reasoning") {
            // Reasoning is rendered inline as a collapsible block on the
            // current assistant message, so we do not duplicate it with the
            // global maintenance badge.
            queueReasoning((event.data.content as string) || "");
            if (activeToolActivities.size === 0) {
              updateSessionRunActivity(sendSessionId, correctingVerificationGap
                ? { phase: "revision", label: "Agent 正在处理" }
                : { phase: "reasoning", label: "正在思考与规划" });
            }
            continue;
          }

          if (event.event === "segment_break") {
            // The model was re-invoked after tool calls. Start a new message
            // segment so the UI can render each model invocation + its tools
            // as a separate block.
            flushPendingTokens();
            flushPendingReasoning();
            const targetId = getAssistantId();
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === targetId);
              if (idx !== -1) {
                const current = currentRunsMapRef.current[sendSessionId];
                const segments = updated[idx].segments
                  ? [
                      ...updated[idx].segments,
                      {
                        content: "",
                        runId: current?.run_id,
                      },
                    ]
                  : [{ content: "" }];
                updated[idx] = { ...updated[idx], segments };
              }
              return updated;
            });
            continue;
          }

          // Preserve protocol ordering: all text preceding a structural event
          // must be visible on the current assistant message first.
          flushPendingTokens();
          flushPendingReasoning();

          // Handle context_usage event
          if (event.event === "context_usage") {
            const usage = event.data as {
              used_tokens: number;
              total_tokens: number;
              percentage: number;
            };
            // Streams continue in the background when the user switches
            // conversations. Never let one Session overwrite another
            // Session's composer meter.
            if (sessionIdRef.current === sendSessionId) {
              setContextUsage({
                used: usage.used_tokens,
                total: usage.total_tokens,
                percentage: usage.percentage,
                measured: true,
              });
            }
            continue;
          }

          // Handle context maintenance event (history tool summarization, compaction, etc.)
          if (event.event === "context_maintenance") {
            const payload = event.data as {
              status?: "start" | "done" | "error";
              phase?: string;
              message?: string;
              used_tokens_before?: number;
              trigger_tokens?: number;
            };
            const isSummarizationPhase = [
              "global_summarization",
              "deepagents_summarization",
            ].includes(payload.phase || "");
            if (payload.status === "start") {
              if (
                payload.phase === "global_summarization" &&
                Number.isFinite(payload.used_tokens_before) &&
                Number.isFinite(payload.trigger_tokens) &&
                Number(payload.trigger_tokens) > 0
              ) {
                const used = Number(payload.used_tokens_before);
                const total = Number(payload.trigger_tokens);
                setContextUsage({
                  used,
                  total,
                  percentage: Math.round((used / total) * 1000) / 10,
                  measured: true,
                });
              }
              setMaintenanceStatus((current) => ({
                phase: isSummarizationPhase
                  ? "global_summarization"
                  : payload.phase || "context",
                message: isSummarizationPhase
                  ? "正在压缩上下文…"
                  : payload.message || "正在维护上下文...",
                usedTokensBefore: payload.used_tokens_before ?? current?.usedTokensBefore,
                triggerTokens: payload.trigger_tokens ?? current?.triggerTokens,
                startedAt: isSummarizationPhase
                  ? current?.phase === "global_summarization"
                    ? current.startedAt
                    : Date.now()
                  : undefined,
              }));
              updateSessionRunActivity(sendSessionId, {
                phase: "running",
                label: isSummarizationPhase
                  ? "正在压缩上下文"
                  : "正在优化工具上下文",
                detail: isSummarizationPhase ? undefined : payload.message,
              });
              const jobId = String(event.data.job_id || "");
              const maintenanceSessionId = String(event.data.session_id || sendSessionId);
              if (
                payload.phase === "tool_context_compaction" &&
                jobId &&
                !toolContextPollingJobsRef.current.has(jobId)
              ) {
                toolContextPollingJobsRef.current.add(jobId);
                void (async () => {
                  let reachedTerminalState = false;
                  try {
                    // The worker may process several bounded batches. Keep the
                    // lightweight status poll alive for up to five minutes.
                    for (let attempt = 0; attempt < 600; attempt += 1) {
                      await new Promise((resolve) => setTimeout(resolve, 500));
                      const status = await getToolContextJobStatus(maintenanceSessionId);
                      if (!status.id || status.id !== jobId) continue;
                      if (["completed", "completed_with_errors", "failed", "expired"].includes(status.status)) {
                        reachedTerminalState = true;
                        if (sessionIdRef.current === maintenanceSessionId) {
                          if (status.status === "failed" || status.status === "expired") {
                            setMaintenanceStatus(null);
                          } else {
                            try {
                              const usage = await getSessionTokenCount(maintenanceSessionId);
                              if (sessionIdRef.current === maintenanceSessionId) {
                                setContextUsage({
                                  used: usage.total_tokens,
                                  total: usage.compaction_trigger,
                                  percentage: Math.min(100, usage.percentage),
                                  measured: usage.measured,
                                });
                              }
                            } catch {
                              // The status itself is authoritative; a meter refresh
                              // failure must not turn maintenance into a chat error.
                            }
                            setMaintenanceStatus({
                              phase: "tool_context_compaction_done",
                              message: "上下文已优化，后续对话将继续使用精简上下文。",
                            });
                            setTimeout(() => {
                              if (sessionIdRef.current === maintenanceSessionId) {
                                setMaintenanceStatus(null);
                              }
                            }, 1400);
                          }
                        }
                        break;
                      }
                    }
                  } catch {
                    if (sessionIdRef.current === maintenanceSessionId) {
                      setMaintenanceStatus(null);
                    }
                  } finally {
                    if (!reachedTerminalState && sessionIdRef.current === maintenanceSessionId) {
                      setMaintenanceStatus(null);
                    }
                    toolContextPollingJobsRef.current.delete(jobId);
                  }
                })();
              }
            } else if (
              payload.status === "done" &&
              ["global_summarization_done", "deepagents_summarization"].includes(
                payload.phase || "",
              )
            ) {
              setMaintenanceStatus((current) => ({
                phase: "global_summarization_done",
                message: payload.message || "上下文压缩完成，Harness 状态已保留。",
                startedAt: current?.startedAt,
              }));
              updateSessionRunActivity(sendSessionId, null);
              window.setTimeout(() => {
                if (sessionIdRef.current === sendSessionId) {
                  setMaintenanceStatus((current) =>
                    current?.phase === "global_summarization_done" ? null : current,
                  );
                }
              }, 3000);
            } else {
              setMaintenanceStatus(null);
              refreshActivityAfterTool();
            }
            continue;
          }

          // Handle retrieval event (RAG mode)
          if (event.event === "retrieval") {
            const targetId = getAssistantId();
            const retrievalData = event.data as {
              query: string;
              results: Array<{ text: string; score: string; source: string }>;
            };
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === targetId);
              if (idx === -1) return prev;
              updated[idx] = {
                ...updated[idx],
                retrievals: retrievalData.results,
              };
              return updated;
            });
            continue;
          }

          if (event.event === "source_found") {
            const targetId = getAssistantId();
            const source = event.data.source as unknown as SourceRecord;
            if (source?.source_id) {
              setInspectorOpen(true);
              setActiveSourceId(source.source_id);
              updateMsgs((prev) => {
                const updated = [...prev];
                const idx = updated.findIndex((m) => m.id === targetId);
                if (idx === -1) return prev;
                const existing = updated[idx].sources || [];
                updated[idx] = {
                  ...updated[idx],
                  sources: existing.some((item) => item.source_id === source.source_id)
                    ? existing.map((item) => item.source_id === source.source_id ? { ...item, ...source } : item)
                    : [...existing, source],
                };
                return updated;
              });
            }
            continue;
          }

          if (event.event === "attachment_published") {
            const targetId = getAssistantId();
            const attachment = event.data.attachment as unknown as AgentAttachment;
            if (attachment?.id) {
              const toolCallId = String(
                event.data.tool_call_id || attachment.created_by_tool_call_id || ""
              );
              const attributedAttachment = toolCallId
                ? { ...attachment, created_by_tool_call_id: toolCallId }
                : attachment;
              updateMsgs((prev) => {
                const updated = [...prev];
                const idx = updated.findIndex((m) => m.id === targetId);
                if (idx === -1) return prev;
                const existing = updated[idx].outputAttachments || [];
                updated[idx] = {
                  ...updated[idx],
                  queryId: String(event.data.query_id || updated[idx].queryId || "") || undefined,
                  outputAttachments: existing.some((item) => item.id === attachment.id)
                    ? existing.map((item) => item.id === attachment.id ? { ...item, ...attributedAttachment } : item)
                    : [...existing, attributedAttachment],
                };
                return updated;
              });
            }
            continue;
          }

          if (event.event === "citations_finalized") {
            const targetId = getAssistantId();
            const citations = (event.data.citations || []) as unknown as CitationRef[];
            const sources = (event.data.sources || []) as unknown as SourceRecord[];
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === targetId);
              if (idx === -1) return prev;
              const existingSources = updated[idx].sources || [];
              const mergedSources = [...existingSources];
              for (const source of sources) {
                const sourceIndex = mergedSources.findIndex(
                  (item) => item.source_id === source.source_id
                );
                if (sourceIndex >= 0) {
                  mergedSources[sourceIndex] = { ...mergedSources[sourceIndex], ...source };
                } else {
                  mergedSources.push(source);
                }
              }
              updated[idx] = {
                ...updated[idx],
                citations,
                sources: mergedSources.length > 0 ? mergedSources : updated[idx].sources,
              };
              return updated;
            });
            continue;
          }

          // Agent white-box state: todo list updates
          if (event.event === "todos_updated") {
            const nextTodos = (event.data.todos || []) as TodoItem[];
            updateSessionTodos(
              sendSessionId,
              nextTodos,
              event.data.authority as TodoAuthority | undefined,
              typeof event.data.ledger_revision === "number"
                ? event.data.ledger_revision
                : undefined,
            );
            if (workspaceView === "chat" && nextTodos.length > 0) {
              setInspectorOpen(true);
              setInspectorActiveTab("progress");
            }
            continue;
          }

          if (event.event === "task_preflight_started") {
            continue;
          }

          if (event.event === "task_preflight_completed") {
            continue;
          }

          if (event.event === "skill_install_required") {
            const skillIds = Array.isArray(event.data.skill_ids)
              ? event.data.skill_ids.map(String).filter(Boolean)
              : [];
            updateSessionRunActivity(sendSessionId, {
              phase: "running",
              label: "Agent 正在准备 Skill 安装恢复方案",
              detail: skillIds.length > 0 ? `尚未安装：${skillIds.join("、")}` : undefined,
            });
            continue;
          }

          if (event.event === "skill_loading") {
            const skillId = String(event.data.skill_id || "").trim();
            updateSessionRunActivity(sendSessionId, {
              phase: "running",
              label: skillId
                ? `正在加载 ${skillId} 能力`
                : "正在加载所需能力",
              detail: "复用当前 Session 中经过哈希校验的 Skill 上下文",
            });
            continue;
          }

          if (
            event.event === "skill_activated"
            && String(event.data.source || "").startsWith("session_cache")
          ) {
            const skillId = String(event.data.skill_id || "").trim();
            updateSessionRunActivity(sendSessionId, {
              phase: "running",
              label: skillId
                ? `${skillId} 能力已加载，Agent 正在继续`
                : "所需能力已加载，Agent 正在继续",
            });
            continue;
          }

          if (event.event === "rubric_profile_started") {
            updateSessionRunActivity(sendSessionId, {
              phase: "running",
              label: "Rubric 验收正在生成任务画像",
              detail: "实验性模式会在执行前冻结本次 Goal 的验收契约",
            });
            continue;
          }

          if (event.event === "rubric_profile_completed") {
            updateSessionRunActivity(sendSessionId, {
              phase: "running",
              label: String(event.data.label || "Rubric 验收画像已冻结"),
              detail: "Agent 即将按冻结后的验收契约开始执行",
            });
            continue;
          }

          if (event.event.startsWith("subagent_") || event.event === "context_mounted") {
            const objective = String(event.data.objective || "").trim();
            const tool = String(event.data.tool || "").trim();
            const labels: Record<string, { label: string; status: string }> = {
              subagent_started: { label: "子代理执行中", status: "running" },
              subagent_waiting_for_permission: {
                label: "子代理等待授权",
                status: "waiting_for_permission",
              },
              context_mounted: { label: "上下文已挂载", status: "completed" },
              subagent_stage_changed: { label: "子代理正在分析与规划", status: "running" },
              subagent_tool_started: {
                label: tool ? `子代理正在执行：${tool}` : "子代理正在执行工具",
                status: "running",
              },
              subagent_tool_completed: {
                label: tool ? `子代理工具已返回：${tool}` : "子代理工具已返回",
                status: "completed",
              },
              subagent_tool_failed: {
                label: tool ? `子代理工具失败：${tool}` : "子代理工具执行失败",
                status: "failed",
              },
              subagent_completed: { label: "子代理已完成，主 Agent 正在接收结果", status: "completed" },
              subagent_blocked: { label: "子代理需要主 Agent 决策", status: "blocked" },
              subagent_timed_out: { label: "子代理执行超时", status: "timed_out" },
              subagent_failed: { label: "子代理执行未完成", status: "failed" },
              subagent_cancelled: { label: "子代理已随本轮任务取消", status: "cancelled" },
              subagent_fallback_to_parent: { label: "主 Agent 正在接管剩余任务", status: "running" },
            };
            const presentation = labels[event.event];
            if (presentation) {
              const identity = getSubagentActivityIdentity(event.event, event.data);
              const activityStatus = identity.statusOverride || presentation.status;
              if (identity.terminal) {
                // Close every outstanding stage/tool spinner for this
                // sub-run before recording its terminal lifecycle state.
                settleLifecycleActivities(identity.settlePrefix, activityStatus);
              }
              // The running `task` tool row already says "子代理执行中".
              // Keep only the useful mounted-context signal instead of
              // rendering a redundant "子代理已启动" row underneath it.
              if (event.event !== "subagent_started") {
                appendLifecycleActivity(
                  presentation.label,
                  activityStatus,
                  identity.activityId,
                );
              }
              const terminal = identity.terminal;
              if (!terminal) {
                updateSessionRunActivity(sendSessionId, {
                  phase: "subagent",
                  label: presentation.label,
                  detail: objective ? objective.slice(0, 96) : undefined,
                });
              } else {
                updateSessionRunActivity(sendSessionId, {
                  phase: "continuing",
                  label: presentation.label,
                  detail: objective ? objective.slice(0, 96) : undefined,
                });
              }
            }
          }

          if (event.event === "run_started") {
            const run = event.data.run as unknown as HarnessRun;
            correctingVerificationGap = false;
            if (Array.isArray(event.data.todos)) {
              updateSessionTodos(
                sendSessionId,
                event.data.todos as TodoItem[],
                event.data.todos_authority as TodoAuthority | undefined,
                typeof event.data.todo_ledger_revision === "number"
                  ? event.data.todo_ledger_revision
                  : undefined,
              );
            }
            updateSessionRunActivity(sendSessionId, {
              phase: "running",
              label: run?.run_kind === "goal_inspection"
                ? "正在读取 Goal 进度"
                : run?.run_kind === "goal_execution"
                  ? "正在执行 Goal"
                  : "Agent 正在处理",
            });
            if (run?.run_id) {
              currentRunsMapRef.current[sendSessionId] = run;
              const existingRuns = goalRunsMapRef.current[sendSessionId] || [];
              if (run.goal_id) {
                goalRunsMapRef.current[sendSessionId] = [
                  ...existingRuns.filter((item) => item.run_id !== run.run_id),
                  run,
                ];
              }
              verificationReportsMapRef.current[sendSessionId] = null;
              if (sessionIdRef.current === sendSessionId) {
                setCurrentRun(run);
                setGoalRuns(goalRunsMapRef.current[sendSessionId]);
                setVerificationReport(null);
              }
              const targetId = getAssistantId();
              updateMsgs((prev) => prev.map((message) => {
                if (message.id !== targetId || !message.segments?.length) return message;
                const segments = [...message.segments];
                const last = segments.length - 1;
                segments[last] = {
                  ...segments[last],
                  runId: run.run_id,
                };
                return { ...message, segments };
              }));
            }
            continue;
          }

          if (
            event.event === "goal_created" ||
            event.event === "goal_updated" ||
            event.event === "goal_status_changed"
          ) {
            const goal = event.data.goal as unknown as HarnessGoal;
            if (goal?.goal_id) {
              if (event.event === "goal_created" && runOptions.requestedGoalMode) {
                nextRunGoalModeMapRef.current[sendSessionId] = false;
              }
              const nextActiveGoal = goalRemainsVisible(goal.status) ? goal : null;
              activeGoalsMapRef.current[sendSessionId] = nextActiveGoal;
              if (sessionIdRef.current === sendSessionId) {
                setActiveGoal(nextActiveGoal);
                setGoalModeEnabledRaw(
                  nextRunGoalModeMapRef.current[sendSessionId] ?? false
                );
                if (nextActiveGoal) {
                  setInspectorOpen(true);
                  setInspectorActiveTab("goal");
                }
              }
            }
            continue;
          }

          if (event.event === "verification_started") {
            correctingVerificationGap = false;
            updateSessionRunActivity(sendSessionId, {
              phase: "verification",
              label: "Agent 正在处理",
            });
            const gradingRunId = String(event.data.grading_run_id || event.data.iteration || "current");
            appendLifecycleActivity(
              "正在核对完成质量",
              "running",
              `verification-quality-${gradingRunId}`,
            );
            const current = currentRunsMapRef.current[sendSessionId];
            if (current) {
              const next = { ...current, status: "evaluating" as const };
              currentRunsMapRef.current[sendSessionId] = next;
              goalRunsMapRef.current[sendSessionId] = (goalRunsMapRef.current[sendSessionId] || []).map(
                (item) => item.run_id === next.run_id ? next : item
              );
              if (sessionIdRef.current === sendSessionId) setCurrentRun(next);
              if (sessionIdRef.current === sendSessionId) setGoalRuns(goalRunsMapRef.current[sendSessionId]);
            }
            continue;
          }

          if (
            event.event === "rubric_evaluation_end" ||
            event.event === "deterministic_checks_completed"
          ) {
            const result = String(
              event.event === "rubric_evaluation_end"
                ? event.data.result || ""
                : event.data.status || ""
            );
            const verificationActivityId = event.event === "rubric_evaluation_end"
              ? `verification-quality-${String(event.data.grading_run_id || event.data.iteration || "current")}`
              : `verification-completion-${String(event.data.run_id || currentRunsMapRef.current[sendSessionId]?.run_id || "current")}`;
            if (
              result === "needs_revision" ||
              result === "failed" ||
              result === "infrastructure_error"
            ) {
              const failureActivity = verificationFailureActivity(
                event.event,
                result,
                Boolean(event.data.will_continue),
                Boolean(event.data.goal_id),
                event.event === "rubric_evaluation_end"
                  ? event.data.criteria
                  : event.data.evaluations,
              );
              const { willContinue } = failureActivity;
              correctingVerificationGap = willContinue;
              if (!willContinue) {
                settleLifecycleActivities("verification-", failureActivity.displayStatus);
              }
              updateSessionRunActivity(sendSessionId, {
                phase: willContinue ? "revision" : "verification",
                label: "Agent 正在处理",
              });
              appendLifecycleActivity(
                failureActivity.label,
                failureActivity.displayStatus,
                verificationActivityId,
                failureActivity.detail,
              );
              if (failureActivity.summary) {
                const targetId = getAssistantId();
                updateMsgs((prev) => prev.map((message) => (
                  message.id === targetId
                    ? { ...message, verificationSummary: failureActivity.summary }
                    : message
                )));
              }
            } else if (result === "satisfied" || result === "passed") {
              correctingVerificationGap = false;
              settleLifecycleActivities("verification-", result);
              updateSessionRunActivity(sendSessionId, {
                phase: "verification",
                label: "Agent 正在处理",
              });
              appendLifecycleActivity(
                event.event === "deterministic_checks_completed"
                  ? "完成条件检查通过"
                  : "完成质量检查通过",
                result,
                verificationActivityId,
              );
            }
            continue;
          }

          if (event.event === "run_status_changed") {
            const current = currentRunsMapRef.current[sendSessionId];
            if (current && event.data.status) {
              const next = {
                ...current,
                status: event.data.status as HarnessRun["status"],
              };
              currentRunsMapRef.current[sendSessionId] = next;
              goalRunsMapRef.current[sendSessionId] = (goalRunsMapRef.current[sendSessionId] || []).map(
                (item) => item.run_id === next.run_id ? next : item
              );
              if (sessionIdRef.current === sendSessionId) setCurrentRun(next);
              if (sessionIdRef.current === sendSessionId) setGoalRuns(goalRunsMapRef.current[sendSessionId]);
            }
            continue;
          }

          if (event.event === "verification_contract_updated") {
            const contract = event.data.contract as unknown as HarnessRun["verification_contract"];
            const current = currentRunsMapRef.current[sendSessionId];
            if (current && contract) {
              const next = {
                ...current,
                verification_contract: contract,
              };
              currentRunsMapRef.current[sendSessionId] = next;
              goalRunsMapRef.current[sendSessionId] = (goalRunsMapRef.current[sendSessionId] || []).map(
                (item) => item.run_id === next.run_id ? next : item
              );
              if (sessionIdRef.current === sendSessionId) {
                setCurrentRun(next);
                setGoalRuns(goalRunsMapRef.current[sendSessionId]);
              }
            }
            continue;
          }

          if (event.event === "verification_report") {
            const report = event.data.report as unknown as RubricEvaluationReport;
            if (report?.report_id) {
              const controlOnly = CONTROL_ONLY_VERIFICATION_STATUSES.has(report.status);
              const supersededGoalRun =
                report.status === "satisfied" && report.accepted_for_goal_revision === false;
              if (controlOnly || supersededGoalRun) {
                verificationReportsMapRef.current[sendSessionId] = null;
                if (sessionIdRef.current === sendSessionId) {
                  setVerificationReport(null);
                }
              } else {
                verificationReportsMapRef.current[sendSessionId] = report;
              }
              const current = currentRunsMapRef.current[sendSessionId];
              if (current) {
                const next = {
                  ...current,
                  verification_report: report,
                };
                currentRunsMapRef.current[sendSessionId] = next;
                goalRunsMapRef.current[sendSessionId] = (goalRunsMapRef.current[sendSessionId] || []).map(
                  (item) => item.run_id === next.run_id ? next : item
                );
                if (sessionIdRef.current === sendSessionId) {
                  setCurrentRun(next);
                  setGoalRuns(goalRunsMapRef.current[sendSessionId]);
                }
              }
              if (!controlOnly && sessionIdRef.current === sendSessionId) {
                setVerificationReport(report);
              }
            }
            continue;
          }

          if (event.event === "run_outcome") {
            const outcomeStatus = String(event.data.status || event.data.outcome || "");
            updateSessionRunActivity(sendSessionId, outcomeStatus === "satisfied" || outcomeStatus === "completed"
              ? {
                  phase: "verification",
                  label: "Agent 正在处理",
                }
              : {
                  phase: "continuing",
                  label: "正在准备后续处理",
                });
            const current = currentRunsMapRef.current[sendSessionId];
            if (current) {
              const next: HarnessRun = {
                ...current,
                status: (event.data.status as HarnessRun["status"]) || current.status,
                outcome: (event.data.outcome as string) || current.outcome,
                error: (event.data.error as string) || current.error,
                budget_exhaustion_reason:
                  (event.data.budget_exhaustion_reason as string) ||
                  current.budget_exhaustion_reason,
                model_call_count:
                  event.data.model_call_count == null
                    ? current.model_call_count
                    : Number(event.data.model_call_count),
              };
              currentRunsMapRef.current[sendSessionId] = next;
              goalRunsMapRef.current[sendSessionId] = (goalRunsMapRef.current[sendSessionId] || []).map(
                (item) => item.run_id === next.run_id ? next : item
              );
              if (sessionIdRef.current === sendSessionId) setCurrentRun(next);
              if (sessionIdRef.current === sendSessionId) setGoalRuns(goalRunsMapRef.current[sendSessionId]);
            }
            continue;
          }

          if (event.event === "run_limit_reached") {
            // This is a Run control boundary, not assistant content. The Run
            // outcome/timeline in the Goal drawer carries the user-visible state.
            continue;
          }

          if (event.event === "goal_run_continued") {
            correctingVerificationGap = false;
            updateSessionRunActivity(sendSessionId, {
              phase: "continuing",
              label: "正在进入下一轮",
              detail: "已保留 Goal、Todo、产物与当前进度",
            });
            const targetId = getAssistantId();
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === targetId);
              if (idx !== -1) {
                const previous = finalizeRunningToolsInMessage(
                  updated[idx],
                  "本轮结束前，该工具未返回结果。"
                );
                updated[idx] = {
                  ...previous,
                  // Keep one continuous assistant response across internal Runs,
                  // while separating segment-local tool/reasoning state.
                  segments: [...(previous.segments || []), { content: "" }],
                };
              }
              return updated;
            });
            continue;
          }

          if (event.event === "permission_required") {
            updateSessionRunActivity(sendSessionId, {
              phase: "permission",
              label: "等待你的授权",
            });
            const targetId = getAssistantId();
            const permissionRequest = event.data as unknown as PermissionRequest;
            const toolCallId = permissionRequest.tool_call_id || "";
            setInspectorOpen(true);
            setInspectorActiveTab("permissions");
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === targetId);
              if (idx === -1) return prev;
              const msg = { ...updated[idx] };
              const permissionTarget =
                permissionRequest.type === "tool_action"
                  ? `Command: ${permissionRequest.command || ""}`
                  : `Path: ${permissionRequest.path || ""}`;
              const output = `Permission required\n${permissionTarget}`;
              const calls = [...(msg.toolCalls || [])];
              let callIdx = toolCallId ? calls.findIndex((call) => call.id === toolCallId) : -1;
              if (callIdx === -1) {
                callIdx = calls.findIndex(
                  (call) =>
                    call.tool ===
                      (permissionRequest.tool_name ||
                        permissionRequest.operation ||
                        "read_external_file") &&
                    call.status === "running"
                );
              }
              if (callIdx !== -1) {
                calls[callIdx] = {
                  ...calls[callIdx],
                  output,
                  permissionRequest,
                };
              }
              msg.toolCalls = calls;
              const existingRequests = msg.permissionRequests || [];
              const samePermissionRequest = (request: PermissionRequest) =>
                request.id === permissionRequest.id || Boolean(
                  permissionRequest.semantic_key
                    && request.semantic_key === permissionRequest.semantic_key
                );
              msg.permissionRequests = existingRequests.some(samePermissionRequest)
                ? existingRequests.map((request) => samePermissionRequest(request) ? permissionRequest : request)
                : [...existingRequests, permissionRequest];
              const timeline = msg.timeline ? [...msg.timeline] : [];
              const permissionTool =
                permissionRequest.tool_name ||
                permissionRequest.operation ||
                "read_external_file";
              updateToolInTimeline(timeline, toolCallId, permissionTool, {
                output,
                permissionRequest,
              });
              msg.timeline = timeline;
              const segments = msg.segments ? [...msg.segments] : undefined;
              if (segments) {
                const lastSegIdx = segments.length - 1;
                const segTimeline = segments[lastSegIdx].timeline
                  ? [...segments[lastSegIdx].timeline]
                  : [];
                updateToolInTimeline(segTimeline, toolCallId, permissionTool, {
                  output,
                  permissionRequest,
                });
                segments[lastSegIdx] = { ...segments[lastSegIdx], timeline: segTimeline };
                msg.segments = segments;
              }
              updated[idx] = msg;
              return updated;
            });
            continue;
          }

          if (event.event === "permission_resolved") {
            refreshActivityAfterTool();
            const targetId = getAssistantId();
            const requestId = (event.data.request_id as string) || "";
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === targetId);
              if (idx === -1 || !requestId) return prev;
              const msg = { ...updated[idx] };
              msg.permissionRequests = (msg.permissionRequests || []).map((request) =>
                request.id === requestId ? { ...request, status: "resolved" } : request
              );
              updated[idx] = msg;
              return updated;
            });
            continue;
          }

          if (event.event === "dimension_build_rule_required") {
            updateSessionRunActivity(sendSessionId, {
              phase: "permission",
              label: "等待你确认数据口径",
            });
            const targetId = getAssistantId();
            const request = event.data as unknown as DimensionBuildRuleRequest;
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1) return prev;
              const message = { ...updated[idx] };
              const existing = message.dimensionBuildRuleRequests || [];
              message.dimensionBuildRuleRequests = existing.some((item) => item.id === request.id)
                ? existing.map((item) => item.id === request.id ? request : item)
                : [...existing, request];
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "logical_dataset_rule_required") {
            updateSessionRunActivity(sendSessionId, {
              phase: "permission",
              label: "等待你确认数据规则",
            });
            const targetId = getAssistantId();
            const request = event.data as unknown as LogicalDatasetRuleRequest;
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1) return prev;
              const message = { ...updated[idx] };
              const existing = message.logicalDatasetRuleRequests || [];
              message.logicalDatasetRuleRequests = existing.some((item) => item.id === request.id)
                ? existing.map((item) => item.id === request.id ? request : item)
                : [...existing, request];
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "database_sql_revision_required") {
            updateSessionRunActivity(sendSessionId, {
              phase: "permission",
              label: "等待你确认 SQL 口径",
            });
            const targetId = getAssistantId();
            const request = event.data as unknown as DatabaseSqlRevisionRequest;
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1) return prev;
              const message = { ...updated[idx] };
              const existing = message.databaseSqlRevisionRequests || [];
              message.databaseSqlRevisionRequests = existing.some((item) => item.id === request.id)
                ? existing.map((item) => item.id === request.id ? request : item)
                : [...existing, request];
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "user_input_required") {
            updateSessionRunActivity(sendSessionId, {
              phase: "hitl",
              label: "等待你的选择",
            });
            const targetId = getAssistantId();
            const request = event.data as unknown as UserInputRequest;
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1) return prev;
              const message = { ...updated[idx] };
              const existing = message.userInputRequests || [];
              message.userInputRequests = existing.some((item) => item.id === request.id)
                ? existing.map((item) => item.id === request.id ? request : item)
                : [...existing, request];
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "kernel_fallback_required") {
            updateSessionRunActivity(sendSessionId, {
              phase: "hitl",
              label: "Kernel 沙箱不可用，等待你的回退选择",
            });
            const targetId = getAssistantId();
            const request = event.data as unknown as KernelFallbackRequest;
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1) return prev;
              const message = { ...updated[idx] };
              const existing = message.kernelFallbackRequests || [];
              message.kernelFallbackRequests = existing.some((item) => item.id === request.id)
                ? existing.map((item) => item.id === request.id ? request : item)
                : [...existing, request];
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "skill_secret_required") {
            updateSessionRunActivity(sendSessionId, {
              phase: "hitl",
              label: "等待你安全填写凭证",
            });
            const targetId = getAssistantId();
            const request = event.data as unknown as SkillSecretRequest;
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1) return prev;
              const message = { ...updated[idx] };
              const existing = message.skillSecretRequests || [];
              message.skillSecretRequests = existing.some((item) => item.id === request.id)
                ? existing.map((item) => item.id === request.id ? request : item)
                : [...existing, request];
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "dimension_build_rule_resolved") {
            const targetId = getAssistantId();
            const requestId = String(event.data.request_id || "");
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1 || !requestId) return prev;
              const message = { ...updated[idx] };
              message.dimensionBuildRuleRequests = (message.dimensionBuildRuleRequests || []).map((item) =>
                item.id === requestId ? { ...item, status: "resolved" } : item
              );
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "logical_dataset_rule_resolved") {
            const targetId = getAssistantId();
            const requestId = String(event.data.request_id || "");
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1 || !requestId) return prev;
              const message = { ...updated[idx] };
              message.logicalDatasetRuleRequests = (message.logicalDatasetRuleRequests || []).map((request) =>
                request.id === requestId ? { ...request, status: "resolved" } : request
              );
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "database_sql_revision_resolved") {
            const targetId = getAssistantId();
            const requestId = String(event.data.request_id || "");
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1 || !requestId) return prev;
              const message = { ...updated[idx] };
              message.databaseSqlRevisionRequests = (message.databaseSqlRevisionRequests || []).map((request) =>
                request.id === requestId ? { ...request, status: "resolved" } : request
              );
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "user_input_resolved") {
            updateSessionRunActivity(sendSessionId, {
              phase: "continuing",
              label: "Agent 正在继续执行",
            });
            const targetId = getAssistantId();
            const requestId = String(event.data.request_id || "");
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1 || !requestId) return prev;
              const message = { ...updated[idx] };
              message.userInputRequests = (message.userInputRequests || []).map((request) =>
                request.id === requestId
                  ? {
                      ...request,
                      status: "resolved",
                      decision: event.data.decision as UserInputRequest["decision"],
                    }
                  : request
              );
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "kernel_fallback_resolved") {
            updateSessionRunActivity(sendSessionId, {
              phase: "continuing",
              label: "已确认执行模式，Agent 正在继续",
            });
            const targetId = getAssistantId();
            const requestId = String(event.data.request_id || "");
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1 || !requestId) return prev;
              const message = { ...updated[idx] };
              message.kernelFallbackRequests = (message.kernelFallbackRequests || []).map((request) =>
                request.id === requestId ? { ...request, status: "resolved" } : request
              );
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          if (event.event === "skill_secret_resolved") {
            updateSessionRunActivity(sendSessionId, {
              phase: "continuing",
              label: "Agent 正在继续执行",
            });
            const targetId = getAssistantId();
            const requestId = String(event.data.request_id || "");
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((message) => message.id === targetId);
              if (idx === -1 || !requestId) return prev;
              const message = { ...updated[idx] };
              message.skillSecretRequests = (message.skillSecretRequests || []).map((request) =>
                request.id === requestId
                  ? {
                      ...request,
                      status: "resolved",
                      decision: event.data.decision as SkillSecretRequest["decision"],
                    }
                  : request
              );
              updated[idx] = message;
              return updated;
            });
            continue;
          }

          // Agent white-box state: execution trace update
          if (event.event === "trace_updated") {
            const nextTrace = (event.data.trace || null) as AgentTrace | null;
            updateSessionTrace(sendSessionId, nextTrace);
            updateSessionActiveGraphNode(sendSessionId, null);
            continue;
          }

          // Real-time trace span events
          if (event.event === "trace_span_start") {
            const span = event.data.span as TraceSpan;
            applyTraceSpanEvent(sendSessionId, span, false, {
              trace_id: event.data.trace_id as string | undefined,
              query_id: event.data.query_id as string | undefined,
            });
            continue;
          }
          if (event.event === "trace_span_end") {
            const span = event.data.span as TraceSpan;
            applyTraceSpanEvent(sendSessionId, span, true, {
              trace_id: event.data.trace_id as string | undefined,
              query_id: event.data.query_id as string | undefined,
            });
            continue;
          }
          if (event.event === "middleware_invocation") {
            const invocation = event.data.invocation as TraceMiddlewareInvocation;
            if (invocation?.id) {
              applyMiddlewareInvocationEvent(sendSessionId, invocation, {
                trace_id: event.data.trace_id as string | undefined,
                query_id: event.data.query_id as string | undefined,
              });
            }
            continue;
          }
          if (event.event === "hook_boundary_snapshot") {
            const snapshot = event.data.snapshot as TraceHookBoundarySnapshot;
            if (snapshot?.id) {
              applyHookBoundarySnapshotEvent(sendSessionId, snapshot, {
                trace_id: event.data.trace_id as string | undefined,
                query_id: event.data.query_id as string | undefined,
              });
            }
            continue;
          }

          // LangGraph structure and active node
          if (event.event === "graph_structure") {
            const nextGraph = event.data as unknown as GraphStructure;
            updateSessionGraph(sendSessionId, nextGraph);
            continue;
          }
          if (event.event === "graph_node_active") {
            const node = (event.data.node as string) || null;
            updateSessionActiveGraphNode(sendSessionId, node);
            continue;
          }

          // Handle title event (auto-generated after first message)
          if (event.event === "title") {
            const titleData = event.data as { session_id: string; title: string };
            setSessions((prev) =>
              prev.map((s) =>
                s.id === titleData.session_id
                  ? { ...s, title: titleData.title }
                  : s
              )
            );
            continue;
          }

          // Handle compressed event (auto-compression triggered)
          if (event.event === "compressed") {
            apiGetSessionHistory(sendSessionId)
              .then((data) => {
                if (data.messages && data.messages.length > 0) {
                  const loaded = parseHistoryMessages(data.messages);
                  messagesMapRef.current[sendSessionId] = loaded;
                  if (sessionIdRef.current === sendSessionId) {
                    setMessages(loaded);
                  }
                }
              })
              .catch(() => {});
            continue;
          }

          // Handle new_response — create a new assistant bubble
          if (event.event === "new_response") {
            const newId = `assistant-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
            assistantIdsRef.current.set(sendSessionId, newId);
            updateMsgs((prev) => [
              ...prev,
              {
                id: newId,
                role: "assistant",
                content: "",
                toolCalls: [],
                timeline: [],
                segments: [{
                  content: "",
                }],
                timestamp: Date.now(),
              },
            ]);
            continue;
          }

          if (event.event === "tool_start") {
            const tool = String(event.data.tool || "");
            const rawId = String(event.data.id || "");
            const activityId = rawId || `anonymous-${tool}-${anonymousToolSequence++}`;
            activeToolActivities.set(activityId, {
              ...toolActivityStatus(tool, String(event.data.input || "")),
              tool,
            });
            refreshActivityAfterTool();
          } else if (event.event === "tool_end") {
            const rawId = String(event.data.id || "");
            if (rawId) {
              activeToolActivities.delete(rawId);
            } else {
              const tool = String(event.data.tool || "");
              const matchingId = Array.from(activeToolActivities.entries())
                .reverse()
                .find(([, activity]) => activity.tool === tool)?.[0];
              if (matchingId) activeToolActivities.delete(matchingId);
            }
            refreshActivityAfterTool();
          }

          const targetId = getAssistantId();

          updateMsgs((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === targetId);
            if (idx === -1) return prev;
            const msg = { ...updated[idx] };

            switch (event.event) {
              case "database_sql_generation_progress": {
                const calls = [...(msg.toolCalls || [])];
                const tcId = String(event.data.tool_call_id || "");
                let callIdx = tcId ? calls.findIndex((call) => call.id === tcId) : -1;
                if (callIdx === -1) {
                  for (let i = calls.length - 1; i >= 0; i--) {
                    if (["database_sql_generate", "database_evidence_search", "database_sql_validate"].includes(calls[i].tool) && calls[i].status === "running") {
                      callIdx = i;
                      break;
                    }
                  }
                }
                if (callIdx !== -1) {
                  const stage = String(event.data.stage || "sql_generation");
                  const labels: Record<string, string> = {
                    routing: "正在确定数据源与表",
                    vanna_retrieval: "正在召回结构与实体证据",
                    candidate_generation: "正在生成 SQL 草案",
                    entity_inspection: "正在核对 EAV 字段与值分布",
                    semantic_refinement: "模型正在结合探测值生成最终 SQL",
                    deterministic_validation: "正在执行只读与语义预检",
                    sql_parse_repair: "正在修复 SQL 结构（1/1）",
                    eav_evidence_repair: "正在修复实体证据映射（1/1）",
                    semantic_guardrail_repair: "正在修复业务口径冲突（1/1）",
                    deterministic_repair: "正在修复 SQL 技术错误（1/1）",
                    repair: "正在修复已识别的问题（1/1）",
                    completed: "SQL 已生成",
                    sql_generation: "正在完成 SQL 生成",
                  };
                  const completedLabels: Record<string, string> = {
                    routing: "数据源与表已确定",
                    vanna_retrieval: "结构与实体证据已召回",
                    entity_inspection: "EAV 字段与原始值已探测",
                    candidate_generation: "SQL 草案已生成",
                    semantic_refinement: "模型已结合探测值生成最终 SQL",
                    deterministic_validation: "只读与语义预检已完成",
                    sql_parse_repair: "SQL 结构已修复",
                    eav_evidence_repair: "实体证据映射已修复",
                    semantic_guardrail_repair: "业务口径冲突已修复",
                    deterministic_repair: "SQL 技术错误已修复",
                    repair: "已识别问题已修复",
                    completed: "SQL 已生成",
                  };
                  const elapsedMs = Number(event.data.elapsed_ms || 0);
                  const status = String(event.data.status || "running");
                  const isActive = status === "running"
                    || status === "heartbeat"
                    || status === "soft_timeout";
                  const stageTimings = event.data.stage_timings as Record<string, number> | undefined;
                  const timingSummary = stageTimings
                    ? [
                        ["路由", stageTimings.router_ms],
                        ["召回", stageTimings.vanna_references_ms],
                        ["候选", stageTimings.sql_candidate_generation_ms],
                        ["实体画像", stageTimings.eav_value_profile_ms],
                      ]
                        .filter((item): item is [string, number] => typeof item[1] === "number")
                        .map(([name, value]) => `${name} ${(value / 1000).toFixed(1)}s`)
                        .join(" / ")
                    : "";
                  const detail = status === "failed"
                    ? String(event.data.message || "SQL 生成已停止；系统不会自动循环重试")
                    : status === "soft_timeout"
                      ? String(event.data.detail || "已进入确定性收尾窗口")
                    : isActive && elapsedMs >= 120_000
                      ? "耗时异常，可停止后重试"
                      : timingSummary || String(event.data.detail || "") || undefined;
                  const stageLabel = status === "failed"
                    ? "SQL 未生成"
                    : status === "completed"
                      ? completedLabels[stage] || labels[stage] || "阶段已完成"
                      : labels[stage] || "正在生成 SQL";
                  const history = [...(calls[callIdx].progress?.history || [])];
                  if (status === "completed" || status === "failed") {
                    const historyEventId = String(
                      event.data.event_sha256
                      || `${stage}:${status}:${event.data.timestamp || elapsedMs}:${history.length}`
                    );
                    const historyItem = {
                      id: historyEventId,
                      stage,
                      label: stageLabel,
                      detail,
                      elapsedMs,
                      status,
                    };
                    const existingHistoryIndex = history.findIndex(
                      (item) => item.id === historyEventId
                    );
                    if (existingHistoryIndex === -1) {
                      history.push(historyItem);
                    } else {
                      history[existingHistoryIndex] = historyItem;
                    }
                  }
                  const updates: Partial<ToolCall> = {
                    progress: {
                      stage,
                      label: stageLabel,
                      detail,
                      elapsedMs,
                      stageTimings,
                      status,
                      history,
                    },
                  };
                  calls[callIdx] = { ...calls[callIdx], ...updates };
                  msg.toolCalls = calls;
                  const timeline = msg.timeline ? [...msg.timeline] : [];
                  updateToolInTimeline(timeline, tcId, "database_sql_generate", updates);
                  msg.timeline = timeline;
                  const segments = msg.segments ? [...msg.segments] : undefined;
                  if (segments) {
                    let segmentIdx = tcId
                      ? segments.findIndex((segment) =>
                          segment.timeline?.some(
                            (item) => item.type === "tool" && item.toolCall.id === tcId
                          )
                        )
                      : -1;
                    if (segmentIdx === -1) {
                      for (let i = segments.length - 1; i >= 0; i--) {
                        if (segments[i].timeline?.some(
                          (item) => item.type === "tool"
                            && item.toolCall.tool === "database_sql_generate"
                            && item.toolCall.status === "running"
                        )) {
                          segmentIdx = i;
                          break;
                        }
                      }
                    }
                    if (segmentIdx !== -1) {
                      const segmentTimeline = [...(segments[segmentIdx].timeline || [])];
                      updateToolInTimeline(
                        segmentTimeline,
                        tcId,
                        "database_sql_generate",
                        updates,
                      );
                      segments[segmentIdx] = {
                        ...segments[segmentIdx],
                        timeline: segmentTimeline,
                      };
                      msg.segments = segments;
                    }
                  }
                }
                break;
              }

              case "tool_start": {
                const tcId = (event.data.id as string) || "";
                // Defensive deduplication: skip if a running/done call with the
                // same id already exists (backend may replay events).
                const existing = (msg.toolCalls || []).find(
                  (c) => tcId && c.id === tcId
                );
                if (!existing) {
                  const newToolCall: ToolCall = {
                    id: tcId,
                    tool: event.data.tool as string,
                    input: event.data.input as string,
                    status: "running",
                    startedAt: Date.now(),
                  };
                  msg.toolCalls = [...(msg.toolCalls || []), newToolCall];
                  const timeline = msg.timeline ? [...msg.timeline] : [];
                  addToolToTimeline(timeline, newToolCall);
                  msg.timeline = timeline;
                  // Also add to the current segment's timeline.
                  const segments = msg.segments ? [...msg.segments] : undefined;
                  if (segments) {
                    const lastSegIdx = segments.length - 1;
                    const segTimeline = segments[lastSegIdx].timeline
                      ? [...segments[lastSegIdx].timeline]
                      : [];
                    addToolToTimeline(segTimeline, newToolCall);
                    segments[lastSegIdx] = { ...segments[lastSegIdx], timeline: segTimeline };
                    msg.segments = segments;
                  }
                }
                break;
              }

              case "tool_end": {
                const calls = [...(msg.toolCalls || [])];
                const tcId = (event.data.id as string) || "";
                // Prefer matching by id; fall back to last running call with the same tool name.
                let callIdx = -1;
                if (tcId) {
                  callIdx = calls.findIndex((c) => c.id === tcId);
                }
                if (callIdx === -1) {
                  for (let i = calls.length - 1; i >= 0; i--) {
                    if (
                      calls[i].tool === event.data.tool &&
                      calls[i].status === "running"
                    ) {
                      callIdx = i;
                      break;
                    }
                  }
                }
                const updates: Partial<ToolCall> = {
                  // Reflect backend execution routing in the visible tool card.
                  tool: event.data.tool as string,
                  output: event.data.output as string,
                  status: "done",
                  endedAt: Date.now(),
                  summary_source: event.data.summary_source as string | undefined,
                  is_error: Boolean(event.data.is_error),
                };
                if (callIdx !== -1) {
                  calls[callIdx] = { ...calls[callIdx], ...updates };
                }
                msg.toolCalls = calls;
                const timeline = msg.timeline ? [...msg.timeline] : [];
                updateToolInTimeline(timeline, tcId, event.data.tool as string, updates);
                msg.timeline = timeline;
                // A HITL interrupt can insert a segment break before the resumed
                // tool emits its tool_end event. Update the segment that owns the
                // tool call instead of assuming it is still the last segment;
                // otherwise the top-level call finishes while the visible
                // per-segment row remains stuck in "running".
                const segments = msg.segments ? [...msg.segments] : undefined;
                if (segments) {
                  let segmentIdx = tcId
                    ? segments.findIndex((segment) =>
                        segment.timeline?.some(
                          (item) => item.type === "tool" && item.toolCall.id === tcId
                        )
                      )
                    : -1;
                  if (segmentIdx === -1) {
                    for (let i = segments.length - 1; i >= 0; i--) {
                      if (
                        segments[i].timeline?.some(
                          (item) =>
                            item.type === "tool" &&
                            item.toolCall.tool === event.data.tool &&
                            item.toolCall.status === "running"
                        )
                      ) {
                        segmentIdx = i;
                        break;
                      }
                    }
                  }
                  if (segmentIdx !== -1) {
                    const segment = segments[segmentIdx];
                    const segTimeline = segment.timeline ? [...segment.timeline] : [];
                    updateToolInTimeline(segTimeline, tcId, event.data.tool as string, updates);
                    segments[segmentIdx] = { ...segment, timeline: segTimeline };
                  }
                  msg.segments = segments;
                }
                break;
              }

              case "done":
                {
                  const verificationSummary = String(
                    event.data.verification_summary || ""
                  ).trim();
                  if (verificationSummary) {
                    msg.verificationSummary = verificationSummary;
                  }
                  msg.usageSummary =
                    normalizeUsageSummary(event.data.usage_summary) || msg.usageSummary;
                }
                break;

              case "error":
                {
                  const message =
                    (event.data.message as string) ||
                    (event.data.error as string) ||
                    "Agent 运行失败，请查看后端日志。";
                  Object.assign(msg, markMessageError(msg, message));
                }
                break;
            }

            updated[idx] = msg;
            return updated;
          });
        }
      } catch (err) {
        flushPendingTokens();
        flushPendingReasoning();
        // Don't show error for manual abort (user clicked stop)
        if (err instanceof DOMException && err.name === "AbortError") {
          const targetId = getAssistantId();
          updateMsgs((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === targetId);
            if (idx !== -1) {
              const interrupted = markMessageInterrupted(
                finalizeRunningToolsInMessage(
                  updated[idx],
                  "Stream cancelled before this tool returned a result."
                )
              );
              interrupted.userInputRequests = (interrupted.userInputRequests || []).map((request) =>
                request.status === "pending"
                  ? {
                      ...request,
                      status: "cancelled",
                      decision: { action: "cancel", answers: [] },
                    }
                  : request
              );
              interrupted.skillSecretRequests = (interrupted.skillSecretRequests || []).map((request) =>
                request.status === "pending"
                  ? { ...request, status: "cancelled", decision: { action: "cancel" } }
                  : request
              );
              updated[idx] = interrupted;
            }
            return updated;
          });
        } else {
          const targetId = getAssistantId();
          updateMsgs((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === targetId);
            if (idx !== -1) {
              updated[idx] = markMessageError(
                finalizeRunningToolsInMessage(
                  updated[idx],
                  "Connection closed before this tool returned a result."
                ),
                `连接中断：${err instanceof Error ? err.message : "Unknown"}。已保留中断前完成的内容。`
              );
            }
            return updated;
          });
        }
      } finally {
        flushPendingTokens();
        flushPendingReasoning();
        let reconciledHarness: Awaited<ReturnType<typeof apiGetSessionHarnessState>> | null = null;
        let releaseStreamingLock = true;
        if (controller.signal.aborted) {
          try {
            const cancellation = await waitForLatestRunToSettle(sendSessionId);
            reconciledHarness = cancellation.state;
            releaseStreamingLock = cancellation.settled;
          } catch {
            releaseStreamingLock = false;
          }
        }
        // A closed SSE stream cannot own a live verification spinner. Normal
        // terminal events already settle these rows; this is the safety net
        // for disconnects and budget/control-plane termination.
        settleLifecycleActivities("verification-", "error");
        const targetId = getAssistantId();
        updateMsgs((prev) => {
          const updated = [...prev];
          const idx = updated.findIndex((m) => m.id === targetId);
          if (idx === -1) return prev;
          const finalized = finalizeRunningToolsInMessage(
            updated[idx],
            "Stream ended before this tool returned a result."
          );
          finalized.userInputRequests = (finalized.userInputRequests || []).map((request) =>
            request.status === "pending"
              ? {
                  ...request,
                  status: "cancelled",
                  decision: { action: "cancel", answers: [] },
                }
              : request
          );
          finalized.skillSecretRequests = (finalized.skillSecretRequests || []).map((request) =>
            request.status === "pending"
              ? { ...request, status: "cancelled", decision: { action: "cancel" } }
              : request
          );
          updated[idx] = finalized;
          return updated;
        });
        abortControllersRef.current.delete(sendSessionId);
        assistantIdsRef.current.delete(sendSessionId);
        if (sessionIdRef.current === sendSessionId) {
          setMaintenanceStatus(null);
        }
        updateSessionRunActivity(
          sendSessionId,
          releaseStreamingLock
            ? null
            : { phase: "running", label: "后端仍在停止，请稍候" },
        );
        if (releaseStreamingLock) {
          updateStreamingSessions((prev) => {
            const next = new Set(prev);
            next.delete(sendSessionId);
            return next;
          });
        }
        (reconciledHarness
          ? Promise.resolve(reconciledHarness)
          : apiGetSessionHarnessState(sendSessionId))
          .then((state) => {
            const reconciledGoal = visibleGoalFromHarness(state);
            const latestRun = state.latest_run_id
              ? state.runs[state.latest_run_id] || null
              : null;
            currentRunsMapRef.current[sendSessionId] = latestRun;
            if (reconciledGoal) {
              nextRunGoalModeMapRef.current[sendSessionId] = false;
            }
            activeGoalsMapRef.current[sendSessionId] = reconciledGoal;
            if (sessionIdRef.current === sendSessionId) {
              setCurrentRun(latestRun);
              setActiveGoal(reconciledGoal);
              setGoalModeEnabledRaw(
                nextRunGoalModeMapRef.current[sendSessionId] ?? false
              );
            }
          })
          .catch(() => {});
        apiGetCurrentSessionTodos(sendSessionId)
          .then((snapshot) => {
            updateSessionTodos(
              sendSessionId,
              snapshot.todos,
              snapshot.authority,
              snapshot.ledger_revision,
            );
          })
          .catch(() => {});
        loadSessions();
      }
        return true;
      } finally {
        sendReservationsRef.current.delete(reservationSessionId);
      }
    },
    [
      isCompressing,
      sessionId,
      createSession,
      loadSessions,
      updateSessionMessages,
      updateSessionTodos,
      runtimeMode,
      currentProjectId,
      analyticsModelId,
      llmModelId,
      thinkingLevel,
      credentialName,
      goalModeEnabled,
      activeGoal,
      updateSessionRunActivity,
      updateStreamingSessions,
    ]
  );

  // ── Prefill skill-creator prompt without auto-sending ─
  const triggerSkillCreator = useCallback(() => {
    setPendingInput("/skill-creator 帮我创建一个新的 Skill");
    // Switch to the placeholder session so the next message creates a fresh
    // chat instead of appending to the current conversation.
    setSessionId("default");
  }, [setSessionId]);

  return (
    <AppContext.Provider
      value={{
        runtimeMode,
        runtimeReady,
        setRuntimeMode,
        currentProjectId,
        setCurrentProjectId,
        analyticsModelId,
        setAnalyticsModelId,
        llmModelId,
        thinkingLevel,
        credentialName,
        setLlmSelection,
        projects,
        loadProjects,
        registerProject,
        updateProject,
        trustProject,
        removeProject,
        messages,
        sessionHistoryLoading,
        isStreaming,
        hasActiveRun,
        runningSessionIds,
        sendMessage,
        stopStreaming,
        goalModeEnabled,
        setGoalModeEnabled,
        approvalMode,
        approvalModeSaving,
        approvalModeError,
        setApprovalMode,
        activeGoal,
        currentRun,
        goalRuns,
        verificationReport,
        pauseActiveGoal,
        resumeActiveGoal,
        cancelActiveGoal,
        extendActiveGoalBudget,
        updateActiveGoal,
        sessionId,
        setSessionId,
        sessions,
        sessionsLoaded,
        projectsLoaded,
        loadSessions,
        createSession,
        triggerSkillCreator,
        pendingInput,
        setPendingInput,
        getInputDraft,
        setInputDraft,
        notice,
        showNotice,
        renameSession: renameSessionFn,
        deleteSession: deleteSessionFn,
        sidebarOpen,
        setSidebarOpen,
        toggleSidebar,
        inspectorFile,
        setInspectorFile,
        inspectorOpen,
        setInspectorOpen,
        toggleInspector,
        inspectorActiveTab,
        setInspectorActiveTab,
        rightTab,
        setRightTab,
        mcpServers,
        loadMcpServers,
        rawMessages,
        loadRawMessages,
        todos,
        trace,
        traceHistory,
        selectedTraceQueryId,
        selectTraceQuery,
        graph,
        activeGraphNode,
        workspaceView,
        setWorkspaceView,
        expandedFile,
        setExpandedFile,
        sidebarWidth,
        setSidebarWidth,
        inspectorWidth,
        setInspectorWidth,
        isCompressing,
        compactCurrentAgentSession,
        clearCurrentSession,
        contextUsage,
        setContextUsage,
        maintenanceStatus,
        runActivityStatus,
        activeSourceId,
        setActiveSourceId,
        activeAttachmentPreview,
        openAttachmentPreview,
        closeAttachmentPreview,
      }}
    >
      {children}
    </AppContext.Provider>
  );
}

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used within AppProvider");
  return ctx;
}
