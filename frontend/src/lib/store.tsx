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
} from "./api";

// ── Types ──────────────────────────────────────────────────

export interface ToolCall {
  tool: string;
  input?: string;
  output?: string;
  status: "running" | "done";
  summary_source?: string;
}

export interface RetrievalResult {
  text: string;
  score: string;
  source: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  toolCalls?: ToolCall[];
  retrievals?: RetrievalResult[];
  timestamp: number;
}

export interface SessionMeta {
  id: string;
  title: string;
  updated_at: number;
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

interface AppState {
  // Chat
  messages: ChatMessage[];
  isStreaming: boolean;
  sendMessage: (text: string) => Promise<void>;
  stopStreaming: () => void;

  // Sessions
  sessionId: string;
  setSessionId: (id: string) => void;
  sessions: SessionMeta[];
  loadSessions: () => void;
  createSession: () => Promise<void>;
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

  // Right panel tab
  rightTab: "memory" | "skills";
  setRightTab: (tab: "memory" | "skills") => void;

  // Raw messages
  rawMessages: RawMessage[] | null;
  loadRawMessages: () => void;

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

  // Context usage
  contextUsage: ContextUsage;
  setContextUsage: (usage: ContextUsage) => void;
}

const AppContext = createContext<AppState | null>(null);

// ── Helper: parse backend history into ChatMessage[] ────────
function parseHistoryMessages(
  backendMessages: Array<{ role: string; content: string; tool_calls?: Array<{ tool: string; input?: string; output?: string }> }>
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
          tool: tc.tool,
          input: tc.input || "",
          output: tc.output || "",
          status: "done" as const,
        })
      );
      loaded.push({
        id: `hist-asst-${msgIndex++}`,
        role: "assistant",
        content: msg.content,
        toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
        timestamp: Date.now() - (backendMessages.length - msgIndex) * 1000,
      });
    }
  }
  return loaded;
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

export function AppProvider({ children }: { children: React.ReactNode }) {
  // ── Per-session state (Map-based, supports parallel sessions) ──
  const messagesMapRef = useRef<Record<string, ChatMessage[]>>({});
  const abortControllersRef = useRef<Map<string, AbortController>>(new Map());
  const assistantIdsRef = useRef<Map<string, string>>(new Map());
  const sessionIdRef = useRef("default"); // tracks current sessionId for SSE callbacks

  // ── UI state (reflects current session) ──
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streamingSessions, setStreamingSessions] = useState<Set<string>>(new Set());
  const [sessionId, setSessionIdRaw] = useState("default");
  const [userId] = useState(() => getOrCreateUserId());
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [inspectorFile, setInspectorFileRaw] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [rightTab, setRightTab] = useState<"memory" | "skills">("memory");
  const [rawMessages, setRawMessages] = useState<RawMessage[] | null>(null);
  const [expandedFile, setExpandedFile] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(260);
  const [inspectorWidth, setInspectorWidth] = useState(360);
  const [isCompressing, setIsCompressing] = useState(false);
  const [ragMode, setRagMode] = useState(false);
  const [contextUsage, setContextUsage] = useState<ContextUsage>({
    used: 0,
    total: 500000,
    percentage: 0,
  });

  // Derived: is the CURRENT session streaming?
  const isStreaming = streamingSessions.has(sessionId);

  // Load RAG mode on mount
  useEffect(() => {
    apiGetRagMode()
      .then((data) => setRagMode(data.rag_mode))
      .catch(() => {});
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

  // ── Session management ─────────────────────────────

  const loadSessions = useCallback(() => {
    apiListSessions()
      .then((list) => setSessions(list))
      .catch(() => {});
  }, []);

  // Load sessions on mount
  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const setSessionId = useCallback(
    (id: string) => {
      // Switch view — do NOT abort any SSE streams (they continue in background)
      sessionIdRef.current = id;
      setSessionIdRaw(id);
      setRawMessages(null);

      // Show cached messages immediately if available
      const cached = messagesMapRef.current[id];
      if (cached && cached.length > 0) {
        setMessages(cached);
        return; // already have messages, no need to fetch
      }

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
    },
    []
  );

  const createSession = useCallback(async () => {
    try {
      const meta = await apiCreateSession();
      setSessions((prev) => [{ id: meta.id, title: meta.title, updated_at: Date.now() / 1000 }, ...prev]);
      setSessionId(meta.id);
    } catch {
      // ignore
    }
  }, [setSessionId]);

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
      if (sessionIdRef.current === sessionId) {
        setMessages([]);
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
    }
  }, [sessionId]);

  // ── Send message ───────────────────────────────────

  const sendMessage = useCallback(
    async (text: string) => {
      // Guard: only check if CURRENT session is streaming (other sessions can be)
      if (!text.trim() || streamingSessions.has(sessionId) || isCompressing) return;

      // Capture the sessionId at send time (stable for entire SSE lifecycle)
      const sendSessionId = sessionId;

      // Slash command processing
      let processedText = text;
      const tokens = text.split(/(\s+)/);
      const skillNames: string[] = [];
      for (const token of tokens) {
        if (token.startsWith("/") && token.length > 1 && !/\s/.test(token)) {
          skillNames.push(token.slice(1));
        }
      }
      if (skillNames.length > 0) {
        await Promise.allSettled(skillNames.map((name) => apiLoadSkill(name)));
        processedText = tokens
          .map((t) => {
            if (t.startsWith("/") && t.length > 1 && !/\s/.test(t)) {
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
        content: text,
        timestamp: Date.now(),
      };

      const firstAssistantId = `assistant-${Date.now()}`;
      const assistantMsg: ChatMessage = {
        id: firstAssistantId,
        role: "assistant",
        content: "",
        toolCalls: [],
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

      try {
        for await (const event of streamChat(processedText, sendSessionId, controller.signal, userId)) {
          if (controller.signal.aborted) break;

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
              case "token":
                msg.content += (event.data.content as string) || "";
                break;

              case "tool_start":
                msg.toolCalls = [
                  ...(msg.toolCalls || []),
                  {
                    tool: event.data.tool as string,
                    input: event.data.input as string,
                    status: "running",
                  },
                ];
                break;

              case "tool_end": {
                const calls = [...(msg.toolCalls || [])];
                for (let i = calls.length - 1; i >= 0; i--) {
                  if (
                    calls[i].tool === event.data.tool &&
                    calls[i].status === "running"
                  ) {
                    calls[i] = {
                      ...calls[i],
                      output: event.data.output as string,
                      status: "done",
                    };
                    break;
                  }
                }
                msg.toolCalls = calls;
                break;
              }

              case "done":
                break;

              case "error":
                msg.content +=
                  `\n\n**Error:** ${event.data.error || "Unknown error"}`;
                break;
            }

            updated[idx] = msg;
            return updated;
          });
        }
      } catch (err) {
        // Don't show error for manual abort (user clicked stop)
        if (err instanceof DOMException && err.name === "AbortError") {
          const targetId = getAssistantId();
          updateMsgs((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === targetId);
            if (idx !== -1 && updated[idx].content) {
              updated[idx] = {
                ...updated[idx],
                content: updated[idx].content + "\n\n*— 已停止生成 —*",
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
        abortControllersRef.current.delete(sendSessionId);
        assistantIdsRef.current.delete(sendSessionId);
        setStreamingSessions((prev) => {
          const next = new Set(prev);
          next.delete(sendSessionId);
          return next;
        });
        loadSessions();
      }
    },
    [streamingSessions, isCompressing, sessionId, loadSessions, updateSessionMessages]
  );

  return (
    <AppContext.Provider
      value={{
        messages,
        isStreaming,
        sendMessage,
        stopStreaming,
        sessionId,
        setSessionId,
        sessions,
        loadSessions,
        createSession,
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
        rightTab,
        setRightTab,
        rawMessages,
        loadRawMessages,
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
        contextUsage,
        setContextUsage,
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
