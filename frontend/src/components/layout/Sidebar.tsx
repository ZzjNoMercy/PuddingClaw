"use client";

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import {
  MessageSquare,
  Plus,
  MoreHorizontal,
  Pencil,
  Trash2,
  ChevronDown,
  ChevronRight,
  FileCode,
  Check,
  X,
  Maximize2,
  Minimize2,
  RefreshCw,
  Database,
} from "lucide-react";
import { useApp } from "@/lib/store";
import { getSessionTokenCount } from "@/lib/api";

export default function Sidebar() {
  const {
    sessionId,
    setSessionId,
    sessions,
    createSession,
    renameSession,
    deleteSession,
    rawMessages,
    loadRawMessages,
    ragMode,
    toggleRagMode,
  } = useApp();

  const [rawOpen, setRawOpen] = useState(false);
  const [rawExpanded, setRawExpanded] = useState(false);
  const [sessionTokens, setSessionTokens] = useState<number | null>(null);

  // Sort sessions by most recent activity first
  const sortedSessions = useMemo(
    () => [...sessions].sort((a, b) => b.updated_at - a.updated_at),
    [sessions]
  );

  // Load raw messages when section is opened or session changes
  useEffect(() => {
    if (rawOpen) {
      loadRawMessages();
      getSessionTokenCount(sessionId)
        .then((data) => {
          setSessionTokens(data.total_tokens);
        })
        .catch(() => setSessionTokens(null));
    }
  }, [rawOpen, sessionId, loadRawMessages]);

  return (
    <aside className="flex flex-col h-full relative">
      {/* New Chat button */}
      <div className="p-2 pb-0">
        <button
          onClick={createSession}
          className="w-full flex items-center gap-2 px-3 py-2 text-[13px] font-medium text-[#002fa7] hover:bg-[#002fa7]/[0.05] rounded-lg transition-all"
        >
          <Plus className="w-4 h-4" />
          New Chat
        </button>
      </div>

      <div className="mx-3 my-1.5 h-px bg-black/[0.04]" />

      {/* Session list */}
      <div className="flex-1 overflow-y-auto px-1.5">
        {sessions.length > 0 && (
          <div className="space-y-0.5">
            <p className="px-3 pt-1 pb-1 text-[10px] font-semibold text-gray-400 uppercase tracking-widest">
              Recent
            </p>
            {sortedSessions.map((s) => (
              <SessionItem
                key={s.id}
                id={s.id}
                title={s.title}
                isActive={sessionId === s.id}
                onSelect={() => setSessionId(s.id)}
                onRename={(title) => renameSession(s.id, title)}
                onDelete={() => deleteSession(s.id)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Divider */}
      <div className="mx-3 h-px bg-black/[0.04]" />

      {/* Raw Messages section */}
      <div className="shrink-0">
        <div className="flex items-center">
          <button
            onClick={() => setRawOpen((v) => !v)}
            className="flex-1 flex items-center gap-2 px-4 py-2 text-[11px] font-semibold text-gray-500 hover:text-gray-700 transition-colors"
          >
            {rawOpen ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
            <FileCode className="w-3.5 h-3.5" />
            Raw Messages
            {rawOpen && sessionTokens !== null && (
              <span className="text-[10px] text-gray-400 font-normal ml-1">
                ~{sessionTokens.toLocaleString()} tokens
              </span>
            )}
          </button>
          {rawOpen && (
            <div className="flex items-center mr-2 gap-0.5">
              <button
                onClick={() => toggleRagMode()}
                className={`p-1 rounded-md transition-colors ${
                  ragMode
                    ? "text-white bg-[#002fa7] hover:bg-[#001f7a]"
                    : "text-gray-400 hover:text-gray-600 hover:bg-black/[0.04]"
                }`}
                title={ragMode ? "RAG Mode ON" : "RAG Mode OFF"}
              >
                <Database className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={loadRawMessages}
                className="p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-black/[0.04] transition-colors"
                title="Refresh"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={() => setRawExpanded(true)}
                className="p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-black/[0.04] transition-colors"
                title="Expand"
              >
                <Maximize2 className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>

        {rawOpen && !rawExpanded && (
          <div className="px-2 pb-2 max-h-[40vh] overflow-y-auto">
            <RawMessageViewer messages={rawMessages} truncate />
          </div>
        )}
      </div>

      {/* Full-screen Raw Messages overlay */}
      {rawExpanded && (
        <div className="fixed inset-0 z-[100] bg-black/50 flex items-center justify-center p-6 animate-fade-in">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-4xl h-[85vh] flex flex-col overflow-hidden">
            {/* Header */}
            <div className="flex items-center justify-between px-5 py-3 border-b border-black/[0.06]">
              <div className="flex items-center gap-2">
                <FileCode className="w-4 h-4 text-gray-500" />
                <span className="text-[14px] font-semibold text-gray-800">Raw Messages</span>
                <span className="text-[11px] text-gray-400 ml-1">
                  {rawMessages ? `${rawMessages.length} messages` : ""}
                </span>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={loadRawMessages}
                  className="p-1.5 rounded-md text-gray-400 hover:text-gray-600 hover:bg-black/[0.04] transition-colors"
                  title="Refresh"
                >
                  <RefreshCw className="w-4 h-4" />
                </button>
                <button
                  onClick={() => setRawExpanded(false)}
                  className="p-1.5 rounded-md text-gray-400 hover:text-gray-600 hover:bg-black/[0.04] transition-colors"
                  title="Close"
                >
                  <Minimize2 className="w-4 h-4" />
                </button>
              </div>
            </div>
            {/* Body */}
            <div className="flex-1 overflow-y-auto p-5">
              <RawMessageViewer messages={rawMessages} truncate={false} />
            </div>
          </div>
        </div>
      )}

    </aside>
  );
}

// ── Session Item ────────────────────────────────────────

function SessionItem({
  id,
  title,
  isActive,
  onSelect,
  onRename,
  onDelete,
}: {
  id: string;
  title: string;
  isActive: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuOpen]);

  // Focus input when renaming
  useEffect(() => {
    if (renaming && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [renaming]);

  const handleRenameSubmit = useCallback(() => {
    const trimmed = renameValue.trim();
    if (trimmed && trimmed !== title) {
      onRename(trimmed);
    }
    setRenaming(false);
  }, [renameValue, title, onRename]);

  const handleDelete = useCallback(() => {
    setMenuOpen(false);
    if (confirm("Delete this session?")) {
      onDelete();
    }
  }, [onDelete]);

  if (renaming) {
    return (
      <div className="flex items-center gap-1 px-2 py-1">
        <input
          ref={inputRef}
          className="flex-1 px-2 py-1 text-[13px] rounded-md border border-[#002fa7]/30 bg-white outline-none focus:border-[#002fa7]"
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleRenameSubmit();
            if (e.key === "Escape") setRenaming(false);
          }}
          onBlur={handleRenameSubmit}
        />
        <button
          onClick={handleRenameSubmit}
          className="p-1 text-green-600 hover:bg-green-50 rounded"
        >
          <Check className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={() => setRenaming(false)}
          className="p-1 text-gray-400 hover:bg-gray-100 rounded"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
    );
  }

  return (
    <div className="relative group">
      <button
        onClick={onSelect}
        className={`w-full flex items-center gap-2 px-3 py-2 text-[13px] rounded-lg transition-all text-left relative pr-8 ${
          isActive
            ? "bg-white/70 text-gray-800 font-medium shadow-sm"
            : "text-gray-500 hover:bg-white/40"
        }`}
      >
        {isActive && (
          <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 bg-[#002fa7] rounded-r-full" />
        )}
        <MessageSquare className="w-3.5 h-3.5 shrink-0 text-gray-400" />
        <span className="truncate">{title}</span>
      </button>

      {/* More button */}
      <div className={`absolute right-1 top-1/2 -translate-y-1/2 ${menuOpen ? "z-[60]" : ""}`} ref={menuRef}>
        <button
          onClick={(e) => {
            e.stopPropagation();
            setMenuOpen((v) => !v);
          }}
          className="p-1 rounded-md text-gray-300 opacity-0 group-hover:opacity-100 hover:text-gray-600 hover:bg-black/[0.04] transition-all"
        >
          <MoreHorizontal className="w-3.5 h-3.5" />
        </button>

        {menuOpen && (
          <div className="absolute right-0 top-full mt-1 w-32 bg-white rounded-lg shadow-lg border border-black/[0.06] py-1 z-50 animate-fade-in-scale">
            <button
              onClick={() => {
                setMenuOpen(false);
                setRenameValue(title);
                setRenaming(true);
              }}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] text-gray-600 hover:bg-gray-50 transition-colors"
            >
              <Pencil className="w-3 h-3" />
              Rename
            </button>
            <button
              onClick={handleDelete}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] text-red-500 hover:bg-red-50 transition-colors"
            >
              <Trash2 className="w-3 h-3" />
              Delete
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Raw Message Viewer ──────────────────────────────────

function RawMessageViewer({
  messages,
  truncate = true,
}: {
  messages: Array<{ role: string; content: string }> | null;
  truncate?: boolean;
}) {
  if (!messages) {
    return (
      <div className="text-[11px] text-gray-400 text-center py-4">
        No messages yet
      </div>
    );
  }

  return (
    <div className="raw-message-viewer space-y-1">
      {messages.map((msg, i) => {
        const roleClass =
          msg.role === "system"
            ? "msg-system"
            : msg.role === "user"
            ? "msg-user"
            : "msg-assistant";
        const displayContent =
          truncate && msg.content.length > 500
            ? msg.content.slice(0, 500) + "..."
            : msg.content;
        return (
          <div key={i} className={roleClass}>
            <div className="msg-role">{msg.role}</div>
            <div className={`msg-content ${truncate ? "" : "!max-h-none"}`}>
              {displayContent}
            </div>
          </div>
        );
      })}
    </div>
  );
}
