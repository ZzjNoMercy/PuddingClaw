"use client";

import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useRef,
  useEffect,
} from "react";
import {
  streamChat,
  streamAgent,
  listSessions as apiListSessions,
  createSession as apiCreateSession,
  renameSession as apiRenameSession,
  deleteSession as apiDeleteSession,
  getRawMessages as apiGetRawMessages,
  getSessionHistory as apiGetSessionHistory,
  compressSession as apiCompressSession,
  clearSession as apiClearSession,
  getRagMode as apiGetRagMode,
  setRagMode as apiSetRagMode,
  loadSkill as apiLoadSkill,
  listMcpServers as apiListMcpServers,
  listProjects as apiListProjects,
  registerProject as apiRegisterProject,
  updateProject as apiUpdateProject,
  removeProject as apiRemoveProject,
  ProjectMeta,
  TodoItem,
  AgentTrace,
  TraceSpan,
  TraceHookBoundarySnapshot,
  TraceMiddlewareInvocation,
  GraphStructure,
  PermissionRequest,
  AgentAttachment,
} from "./api";
import {
  getSettings as apiGetSettings,
  updateSettings as apiUpdateSettings,
} from "./settingsApi";

// ── Types ──────────────────────────────────────────────────

export interface ToolCall {
  id?: string;
  tool: string;
  input?: string;
  output?: string;
  status: "running" | "done";
  summary_source?: string;
  is_error?: boolean;
  permissionRequest?: PermissionRequest;
}

export type TimelineItem =
  | { type: "reasoning"; content: string; id: string }
  | { type: "tool"; toolCall: ToolCall; id: string };

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
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  toolCalls?: ToolCall[];
  timeline?: TimelineItem[];
  segments?: MessageSegment[];
  retrievals?: RetrievalResult[];
  sources?: SourceRecord[];
  citations?: CitationRef[];
  permissionRequests?: PermissionRequest[];
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
}

export interface RawMessage {
  role: string;
  content: string;
}

export interface ContextUsage {
  used: number;
  total: number;
  percentage: number;
}

export interface ContextMaintenanceStatus {
  phase: string;
  message: string;
}

interface AppState {
  // Runtime mode
  runtimeMode: "agent" | "chat";
  runtimeReady: boolean;
  setRuntimeMode: (mode: "agent" | "chat") => void;
  currentProjectId: string | null;
  setCurrentProjectId: (id: string | null) => void;
  projects: ProjectMeta[];
  loadProjects: () => void;
  registerProject: (path: string) => Promise<ProjectMeta | null>;
  updateProject: (projectId: string, update: { name?: string; pinned?: boolean }) => Promise<ProjectMeta | null>;
  removeProject: (projectId: string) => Promise<boolean>;

  // Chat
  messages: ChatMessage[];
  isStreaming: boolean;
  sendMessage: (text: string, attachments?: AgentAttachment[]) => Promise<void>;
  stopStreaming: () => void;

  // Sessions
  sessionId: string;
  setSessionId: (id: string) => void;
  sessions: SessionMeta[];
  loadSessions: () => void;
  createSession: () => Promise<string | null>;
  triggerSkillCreator: () => void;

  // Pending input (prefill from external actions, cleared on send)
  pendingInput: string | null;
  setPendingInput: (text: string | null) => void;

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
  inspectorActiveTab: "progress" | "sources" | "permissions";
  setInspectorActiveTab: (tab: "progress" | "sources" | "permissions") => void;

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
  compressCurrentSession: () => Promise<void>;

  // Clear
  clearCurrentSession: () => Promise<void>;

  // RAG mode
  ragMode: boolean;
  toggleRagMode: () => void;

  // Thinking mode
  thinkingMode: boolean;
  setThinkingMode: (value: boolean) => Promise<void>;

  // Context usage
  contextUsage: ContextUsage;
  setContextUsage: (usage: ContextUsage) => void;

  // Context maintenance
  maintenanceStatus: ContextMaintenanceStatus | null;

  // Active citation source (syncs chat click with right panel)
  activeSourceId: string | null;
  setActiveSourceId: (id: string | null) => void;
}

const AppContext = createContext<AppState | null>(null);

// ── Helper: parse backend history into ChatMessage[] ────────
function parseHistoryMessages(
  backendMessages: Array<{
    role: string;
    content: string;
    reasoning_content?: string;
    tool_calls?: Array<{ id?: string; tool: string; input?: string; output?: string; is_error?: boolean }>;
    timeline?: Array<{ type: string; content?: string; tool_call?: ToolCall; id?: string }>;
    segments?: Array<{ content?: string; reasoning_content?: string; tool_calls?: ToolCall[]; timeline?: TimelineItem[] }>;
    sources?: SourceRecord[];
    citations?: CitationRef[];
  }>
): ChatMessage[] {
  const loaded: ChatMessage[] = [];
  let msgIndex = 0;
  for (const msg of backendMessages) {
    if (msg.role === "user") {
      loaded.push({
        id: `hist-user-${msgIndex++}`,
        role: "user",
        content: msg.content,
        timestamp: Date.now() - (backendMessages.length - msgIndex) * 1000,
      });
    } else if (msg.role === "assistant") {
      const toolCalls: ToolCall[] = (msg.tool_calls || []).map(
        (tc) => ({
          id: tc.id,
          tool: tc.tool,
          input: tc.input || "",
          output: tc.output || "",
          status: "done" as const,
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
              is_error: Boolean(tc.is_error),
            }));
            const segTimeline = seg.timeline?.length
              ? normalizeSavedTimeline(seg.timeline, segToolCalls)
              : undefined;
            return {
              content: seg.content || "",
              reasoning: seg.reasoning_content,
              toolCalls: segToolCalls.length > 0 ? segToolCalls : undefined,
              timeline: segTimeline,
            };
          })
        : undefined;
      loaded.push({
        id: `hist-asst-${msgIndex++}`,
        role: "assistant",
        content: msg.content,
        reasoning: msg.reasoning_content,
        toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
        timeline: timeline.length > 0 ? timeline : undefined,
        segments,
        sources: msg.sources,
        citations: msg.citations,
        timestamp: Date.now() - (backendMessages.length - msgIndex) * 1000,
      });
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
  saved: Array<{ type: string; content?: string; tool_call?: ToolCall; id?: string }>,
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
            is_error: full?.is_error ?? Boolean(tc.is_error),
          },
          id: tc.id || `saved-tool-${Date.now()}`,
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

export function AppProvider({ children }: { children: React.ReactNode }) {
  // ── Per-session state (Map-based, supports parallel sessions) ──
  const messagesMapRef = useRef<Record<string, ChatMessage[]>>({});
  const todosMapRef = useRef<Record<string, TodoItem[]>>({});
  const tracesMapRef = useRef<Record<string, AgentTrace | null>>({});
  const traceHistoriesMapRef = useRef<Record<string, Record<string, AgentTrace>>>({});
  const selectedTraceQueryMapRef = useRef<Record<string, string | null>>({});
  const graphsMapRef = useRef<Record<string, GraphStructure | null>>({});
  const graphActiveNodesRef = useRef<Record<string, string | null>>({});
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());
  const assistantIdsRef = useRef<Map<string, string>>(new Map());
  const sessionIdRef = useRef("default"); // tracks current sessionId for SSE callbacks

  // ── UI state (reflects current session) ──
  const [messages, setMessages] = useState<ChatMessage[]>([]);
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
  const [sessionId, setSessionIdRaw] = useState("default");
  const [userId] = useState(() => getOrCreateUserId());
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [runtimeMode, setRuntimeModeRaw] = useState<"agent" | "chat">("chat");
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [currentProjectId, setCurrentProjectIdRaw] = useState<string | null>(null);
  const [projects, setProjects] = useState<ProjectMeta[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [inspectorFile, setInspectorFileRaw] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorActiveTab, setInspectorActiveTab] = useState<"progress" | "sources" | "permissions">("progress");
  const [rightTab, setRightTab] = useState<"memory" | "skills" | "mcp">("memory");
  const [mcpServers, setMcpServers] = useState<Array<{ key: string; name: string; url: string; transport: string }>>([]);
  const [rawMessages, setRawMessages] = useState<RawMessage[] | null>(null);
  const [expandedFile, setExpandedFile] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(260);
  const [inspectorWidth, setInspectorWidth] = useState(360);
  const [isCompressing, setIsCompressing] = useState(false);
  const [ragMode, setRagMode] = useState(false);
  const [thinkingMode, setThinkingModeRaw] = useState(false);
  const [contextUsage, setContextUsage] = useState<ContextUsage>({
    used: 0,
    total: 500000,
    percentage: 0,
  });
  const [pendingInput, setPendingInput] = useState<string | null>(null);
  const [maintenanceStatus, setMaintenanceStatus] =
    useState<ContextMaintenanceStatus | null>(null);

  const [activeSourceId, setActiveSourceId] = useState<string | null>(null);

  // Derived: is the CURRENT session streaming?
  const isStreaming = streamingSessions.has(sessionId);

  const setRuntimeMode = useCallback((mode: "agent" | "chat") => {
    setRuntimeModeRaw(mode);
    try {
      localStorage.setItem("puddingclaw_runtime_mode", mode);
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
      const savedMode = localStorage.getItem("puddingclaw_runtime_mode");
      if (savedMode === "agent" || savedMode === "chat") {
        setRuntimeModeRaw(savedMode);
      }
      const savedProjectId = localStorage.getItem("puddingclaw_current_project_id");
      if (savedProjectId) setCurrentProjectIdRaw(savedProjectId);
    } catch {
      // ignore storage errors
    } finally {
      setRuntimeReady(true);
    }
  }, []);

  // Load RAG mode on mount
  useEffect(() => {
    apiGetRagMode()
      .then((data) => setRagMode(data.rag_mode))
      .catch(() => {});
  }, []);

  // Load thinking mode on mount
  useEffect(() => {
    apiGetSettings()
      .then((s) => setThinkingModeRaw(Boolean(s.thinking_mode)))
      .catch(() => {});
  }, []);

  const setThinkingMode = useCallback(async (value: boolean) => {
    setThinkingModeRaw(value);
    try {
      await apiUpdateSettings({ thinking_mode: value });
    } catch {
      // Revert on error so UI stays consistent with backend.
      setThinkingModeRaw((prev) => !value);
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
  const updateSessionTodos = useCallback((sid: string, nextTodos: TodoItem[]) => {
    todosMapRef.current[sid] = nextTodos;
    if (sessionIdRef.current === sid) {
      setTodos(nextTodos);
    }
  }, []);

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

  const loadSessions = useCallback(() => {
    apiListSessions()
      .then((list) => setSessions(list))
      .catch(() => {});
  }, []);

  const loadProjects = useCallback(() => {
    apiListProjects()
      .then((list) => setProjects(list))
      .catch(() => setProjects([]));
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

  const setSessionId = useCallback(
    (id: string) => {
      // Switch view — do NOT abort any SSE streams (they continue in background)
      sessionIdRef.current = id;
      // Persist the selected session so refresh returns to it instead of
      // falling back to the latest/new-chat page.
      try {
        sessionStorage.setItem("puddingclaw_session_id", id);
      } catch {
        // ignore storage errors
      }
      setSessionIdRaw(id);
      setRawMessages(null);

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

      // Show cached messages immediately if available
      const cached = messagesMapRef.current[id];
      if (cached && cached.length > 0) {
        setMessages(cached);
      } else {
        // No cache — clear and load from backend
        setMessages([]);
        apiGetSessionHistory(id)
          .then((data) => {
            if (data.messages && data.messages.length > 0) {
              const loaded = parseHistoryMessages(data.messages);
              messagesMapRef.current[id] = loaded;
              // Only update UI if still viewing this session
              if (sessionIdRef.current === id) {
                setMessages(loaded);
              }
            }
          })
          .catch(() => {
            // Session might not exist yet, that's OK
          });
      }

      // Load persisted todos / trace / graph for Agent sessions
      apiGetRawMessages(id)
        .then((data) => {
          if (data.todos) {
            todosMapRef.current[id] = data.todos;
            if (sessionIdRef.current === id) setTodos(data.todos);
          }
          if (data.trace) {
            tracesMapRef.current[id] = data.trace;
            if (sessionIdRef.current === id) setTrace(data.trace);
          }
          if (data.traces) {
            traceHistoriesMapRef.current[id] = data.traces;
            const selected = data.latest_query_id || data.trace?.query_id || null;
            selectedTraceQueryMapRef.current[id] = selected;
            if (sessionIdRef.current === id) {
              setTraceHistory(data.traces);
              setSelectedTraceQueryId(selected);
              if (selected && data.traces[selected]) setTrace(data.traces[selected]);
            }
          }
          if (data.graph) {
            graphsMapRef.current[id] = data.graph as GraphStructure;
            if (sessionIdRef.current === id) setGraph(data.graph as GraphStructure);
          }
        })
        .catch(() => {
          // ignore
        });
    },
    []
  );

  // On mount/refresh, restore the last viewed session from storage.
  const restoredSessionRef = useRef(false);
  useEffect(() => {
    if (sessions.length === 0) return;

    if (!restoredSessionRef.current) {
      restoredSessionRef.current = true;
      try {
        const saved = sessionStorage.getItem("puddingclaw_session_id");
        if (saved && (saved === "default" || sessions.some((s) => s.id === saved))) {
          setSessionId(saved);
          return;
        }
      } catch {
        // ignore storage errors
      }
    }

    // Fallback: don't auto-switch away from the placeholder "default" session;
    // the user may have clicked "New Chat" and expects to start a fresh conversation.
    if (sessionIdRef.current === "default") return;
    // If the current session already exists in the loaded list, keep it.
    if (sessions.some((s) => s.id === sessionIdRef.current)) return;
    const latest = [...sessions].sort((a, b) => b.updated_at - a.updated_at)[0];
    if (latest && latest.id !== sessionIdRef.current) {
      setSessionId(latest.id);
    }
  }, [sessions, setSessionId]);

  const createSession = useCallback(async (): Promise<string | null> => {
    try {
      const meta = await apiCreateSession();
      setSessions((prev) => [
        {
          id: meta.id,
          title: meta.title,
          updated_at: meta.updated_at || Date.now() / 1000,
          runtime_mode: meta.runtime_mode || "chat",
        },
        ...prev,
      ]);
      // Pre-populate the message cache so setSessionId shows the empty state
      // immediately and doesn't overwrite locally-added messages with a later
      // history fetch.
      messagesMapRef.current[meta.id] = [];
      setSessionId(meta.id);
      return meta.id;
    } catch {
      // ignore
      return null;
    }
  }, [setSessionId]);

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
        assistantIdsRef.current.delete(id);
        setStreamingSessions((prev) => {
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
    [setSessionId]
  );

  const loadRawMessages = useCallback(() => {
    if (!sessionId) return;
    apiGetRawMessages(sessionId)
      .then((data) => setRawMessages(data.messages))
      .catch(() => setRawMessages(null));
  }, [sessionId]);

  // ── Compression ──────────────────────────────────────

  const compressCurrentSession = useCallback(async () => {
    if (isCompressing || streamingSessions.has(sessionId)) return;
    setIsCompressing(true);
    try {
      await apiCompressSession(sessionId);
      loadRawMessages();
      const data = await apiGetSessionHistory(sessionId);
      if (data.messages && data.messages.length > 0) {
        const loaded = parseHistoryMessages(data.messages);
        messagesMapRef.current[sessionId] = loaded;
        if (sessionIdRef.current === sessionId) {
          setMessages(loaded);
        }
      } else {
        messagesMapRef.current[sessionId] = [];
        if (sessionIdRef.current === sessionId) {
          setMessages([]);
        }
      }
    } finally {
      setIsCompressing(false);
    }
  }, [isCompressing, sessionId, loadRawMessages]);

  // ── RAG mode ────────────────────────────────────────

  const toggleRagMode = useCallback(() => {
    const newMode = !ragMode;
    setRagMode(newMode);
    apiSetRagMode(newMode).catch(() => setRagMode(ragMode));
  }, [ragMode]);

  // ── Clear session ───────────────────────────────────

  const clearCurrentSession = useCallback(async () => {
    if (isCompressing || streamingSessions.has(sessionId)) return;
    setIsCompressing(true);
    try {
      await apiClearSession(sessionId);
      messagesMapRef.current[sessionId] = [];
      todosMapRef.current[sessionId] = [];
      tracesMapRef.current[sessionId] = null;
      traceHistoriesMapRef.current[sessionId] = {};
      selectedTraceQueryMapRef.current[sessionId] = null;
      if (sessionIdRef.current === sessionId) {
        setMessages([]);
        setTodos([]);
        setTrace(null);
        setTraceHistory({});
        setSelectedTraceQueryId(null);
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
      // Immediately drop the streaming badge so the UI responds even if the
      // SSE reader is slow to terminate.
      setStreamingSessions((prev) => {
        const next = new Set(prev);
        next.delete(sessionId);
        return next;
      });
    }
  }, [sessionId]);

  // ── Send message ───────────────────────────────────

  const sendMessage = useCallback(
    async (text: string, attachments: AgentAttachment[] = []) => {
      // Guard: only check if CURRENT session is streaming (other sessions can be)
      if ((!text.trim() && attachments.length === 0) || streamingSessions.has(sessionId) || isCompressing) return;

      // Lazily create a session only when we are on the placeholder "default"
      // session (e.g. after the user clicked "New Chat" or triggered a skill
      // from another page). Normal follow-up messages in an existing session
      // must stay in that session.
      if (sessionIdRef.current === "default") {
        await createSession();
      }

      // Capture the sessionId at send time (stable for entire SSE lifecycle)
      const sendSessionId = sessionIdRef.current;

      // Slash command processing
      // Only treat tokens like "/skill-name" as skill invocations; ignore
      // absolute file paths such as "/Users/..." or "/home/...".
      const SKILL_NAME_RE = /^\/[^/]+$/;
      let processedText = text.trim() || "请分析这张图片。";
      const tokens = text.split(/(\s+)/);
      const skillNames: string[] = [];
      for (const token of tokens) {
        if (SKILL_NAME_RE.test(token)) {
          skillNames.push(token.slice(1));
        }
      }
      if (skillNames.length > 0) {
        await Promise.allSettled(skillNames.map((name) => apiLoadSkill(name)));
        processedText = tokens
          .map((t) => {
            if (SKILL_NAME_RE.test(t)) {
              return `[使用技能: ${t.slice(1)}]`;
            }
            return t;
          })
          .join("");
        if (!processedText.replace(/\[使用技能:\s*[^\]]+\]/g, "").trim()) {
          processedText += " 请执行该技能的默认操作";
        }
      }

      const userMsg: ChatMessage = {
        id: `user-${Date.now()}`,
        role: "user",
        content: attachments.length
          ? `${text}\n\n[附件]\n${attachments.map((item) => `- ${item.name || item.path || item.id || "attachment"}`).join("\n")}`
          : text,
        timestamp: Date.now(),
      };

      const firstAssistantId = `assistant-${Date.now()}`;
      const assistantMsg: ChatMessage = {
        id: firstAssistantId,
        role: "assistant",
        content: "",
        toolCalls: [],
        timeline: [],
        segments: [{ content: "" }],
        timestamp: Date.now(),
      };

      // Per-session tracking
      assistantIdsRef.current.set(sendSessionId, firstAssistantId);
      updateSessionMessages(sendSessionId, (prev) => [...prev, userMsg, assistantMsg]);

      // Mark this session as streaming
      setStreamingSessions((prev) => new Set(prev).add(sendSessionId));

      const controller = new AbortController();
      abortControllersRef.current.set(sendSessionId, controller);

      // Helper: update messages for this specific session
      const updateMsgs = (updater: (prev: ChatMessage[]) => ChatMessage[]) => {
        updateSessionMessages(sendSessionId, updater);
      };

      // Helper: get current assistant ID for this session
      const getAssistantId = () => assistantIdsRef.current.get(sendSessionId) || "";

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
        const eventStream = runtimeMode === "agent"
          ? streamAgent(processedText, sendSessionId, currentProjectId, controller.signal, userId, attachments)
          : streamChat(processedText, sendSessionId, controller.signal, userId);

        for await (const event of eventStream) {
          if (controller.signal.aborted) break;

          if (event.event === "token") {
            setMaintenanceStatus((current) =>
              current?.phase === "reasoning" ? null : current
            );
            queueToken((event.data.content as string) || "");
            continue;
          }

          if (event.event === "reasoning") {
            // Reasoning is rendered inline as a collapsible block on the
            // current assistant message, so we do not duplicate it with the
            // global maintenance badge.
            queueReasoning((event.data.content as string) || "");
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
                const segments = updated[idx].segments
                  ? [...updated[idx].segments, { content: "" }]
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
            setContextUsage({
              used: usage.used_tokens,
              total: usage.total_tokens,
              percentage: usage.percentage,
            });
            continue;
          }

          // Handle context maintenance event (history tool summarization, compaction, etc.)
          if (event.event === "context_maintenance") {
            const payload = event.data as {
              status?: "start" | "done" | "error";
              phase?: string;
              message?: string;
            };
            if (payload.status === "start") {
              setMaintenanceStatus({
                phase: payload.phase || "context",
                message: payload.message || "正在维护上下文...",
              });
            } else {
              setMaintenanceStatus(null);
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

          if (event.event === "citations_finalized") {
            const targetId = getAssistantId();
            const citations = (event.data.citations || []) as unknown as CitationRef[];
            updateMsgs((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === targetId);
              if (idx === -1) return prev;
              updated[idx] = { ...updated[idx], citations };
              return updated;
            });
            continue;
          }

          // Agent white-box state: todo list updates
          if (event.event === "todos_updated") {
            const nextTodos = (event.data.todos || []) as TodoItem[];
            updateSessionTodos(sendSessionId, nextTodos);
            if (workspaceView === "chat" && nextTodos.length > 0) {
              setInspectorOpen(true);
              setInspectorActiveTab("progress");
            }
            continue;
          }

          if (event.event === "permission_required") {
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
              const output = `Permission required\nPath: ${permissionRequest.path || ""}`;
              const calls = [...(msg.toolCalls || [])];
              let callIdx = toolCallId ? calls.findIndex((call) => call.id === toolCallId) : -1;
              if (callIdx === -1) {
                callIdx = calls.findIndex((call) => call.tool === "read_external_file" && call.status === "running");
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
              msg.permissionRequests = existingRequests.some((request) => request.id === permissionRequest.id)
                ? existingRequests.map((request) => request.id === permissionRequest.id ? permissionRequest : request)
                : [...existingRequests, permissionRequest];
              const timeline = msg.timeline ? [...msg.timeline] : [];
              updateToolInTimeline(timeline, toolCallId, "read_external_file", {
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
                updateToolInTimeline(segTimeline, toolCallId, "read_external_file", {
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
                segments: [{ content: "" }],
                timestamp: Date.now(),
              },
            ]);
            continue;
          }

          const targetId = getAssistantId();

          updateMsgs((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === targetId);
            if (idx === -1) return prev;
            const msg = { ...updated[idx] };

            switch (event.event) {
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
                  output: event.data.output as string,
                  status: "done",
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
                // Also update the current segment's timeline.
                const segments = msg.segments ? [...msg.segments] : undefined;
                if (segments) {
                  const lastSegIdx = segments.length - 1;
                  const segTimeline = segments[lastSegIdx].timeline
                    ? [...segments[lastSegIdx].timeline]
                    : [];
                  updateToolInTimeline(segTimeline, tcId, event.data.tool as string, updates);
                  segments[lastSegIdx] = { ...segments[lastSegIdx], timeline: segTimeline };
                  msg.segments = segments;
                }
                break;
              }

              case "done":
                break;

              case "error":
                {
                  const message =
                    (event.data.message as string) ||
                    (event.data.error as string) ||
                    "Agent 运行失败，请查看后端日志。";
                  msg.content = msg.content
                    ? `${msg.content}\n\n**Agent error:** ${message}`
                    : `**Agent error:** ${message}`;
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
              // If no token arrived yet, replace the empty placeholder so the
              // typing indicator disappears; otherwise append the stop marker.
              const marker = "*— 已停止生成 —*";
              updated[idx] = {
                ...updated[idx],
                content: updated[idx].content
                  ? updated[idx].content + "\n\n" + marker
                  : marker,
              };
            }
            return updated;
          });
        } else {
          const targetId = getAssistantId();
          updateMsgs((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === targetId);
            if (idx !== -1) {
              updated[idx] = {
                ...updated[idx],
                content:
                  updated[idx].content +
                  `\n\n**Connection error:** ${err instanceof Error ? err.message : "Unknown"}`,
              };
            }
            return updated;
          });
        }
      } finally {
        flushPendingTokens();
        flushPendingReasoning();
        abortControllersRef.current.delete(sendSessionId);
        assistantIdsRef.current.delete(sendSessionId);
        if (sessionIdRef.current === sendSessionId) {
          setMaintenanceStatus(null);
        }
        setStreamingSessions((prev) => {
          const next = new Set(prev);
          next.delete(sendSessionId);
          return next;
        });
        loadSessions();
      }
    },
    [
      streamingSessions,
      isCompressing,
      sessionId,
      createSession,
      loadSessions,
      updateSessionMessages,
      runtimeMode,
      currentProjectId,
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
        projects,
        loadProjects,
        registerProject,
        updateProject,
        removeProject,
        messages,
        isStreaming,
        sendMessage,
        stopStreaming,
        sessionId,
        setSessionId,
        sessions,
        loadSessions,
        createSession,
        triggerSkillCreator,
        pendingInput,
        setPendingInput,
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
        compressCurrentSession,
        clearCurrentSession,
        ragMode,
        toggleRagMode,
        thinkingMode,
        setThinkingMode,
        contextUsage,
        setContextUsage,
        maintenanceStatus,
        activeSourceId,
        setActiveSourceId,
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
