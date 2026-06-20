"use client";

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import Link from "next/link";
import { useRouter, usePathname } from "next/navigation";
import {
  MessageSquare,
  Plus,
  MoreHorizontal,
  Pencil,
  Trash2,
  Check,
  X,
  Search,
  Puzzle,
  FolderKanban,
  Workflow,
  Settings,
  Github,
  ExternalLink,
} from "lucide-react";
import { useApp } from "@/lib/store";

export default function Sidebar() {
  const {
    sessionId,
    setSessionId,
    sessions,
    renameSession,
    deleteSession,
  } = useApp();
  const router = useRouter();
  const pathname = usePathname();

  // Sort sessions by most recent activity first
  const sortedSessions = useMemo(
    () => [...sessions].sort((a, b) => b.updated_at - a.updated_at),
    [sessions]
  );

  return (
    <aside className="flex flex-col h-full relative bg-white text-gray-700">
      {/* Primary actions */}
      <div className="px-2 pt-2 pb-1 space-y-0.5">
        <button
          onClick={() => {
            // Don't create a session eagerly; only navigate to the chat page.
            // A new session will be created lazily when the user actually sends
            // their first message (handled inside sendMessage).
            if (pathname !== "/") {
              router.push("/");
            }
            // Switch to the placeholder "default" session so the next message
            // creates a fresh session instead of appending to the current one.
            setSessionId("default");
          }}
          className="w-full flex items-center gap-2 px-3 py-2 text-[13px] font-medium text-gray-800 hover:bg-black/[0.04] rounded-lg transition-all"
        >
          <Plus className="w-4 h-4" />
          新对话
        </button>
        <SidebarLink icon={Search} label="搜索" muted />
        <Link
          href="/skills"
          className="w-full flex items-center gap-2 px-3 py-2 text-[13px] text-gray-600 hover:text-gray-900 hover:bg-black/[0.04] rounded-lg transition-all"
        >
          <Puzzle className="w-4 h-4" />
          扩展
        </Link>
        <SidebarLink icon={Workflow} label="定时任务" muted />
      </div>

      <div className="mx-3 my-1.5 h-px bg-black/[0.06]" />

      {/* Projects */}
      <div className="shrink-0 px-1.5 pb-2">
        <p className="px-3 pt-2 pb-1 text-[10px] font-semibold text-gray-500 uppercase tracking-widest">
          项目
        </p>
        <div className="flex items-center gap-2 px-3 py-2 text-[12px] text-gray-400">
          <FolderKanban className="h-3.5 w-3.5" />
          暂无项目
        </div>
      </div>

      <div className="mx-3 h-px bg-black/[0.06]" />

      {/* Regular conversations */}
      <div className="flex-1 overflow-y-auto px-1.5">
        <div className="space-y-px">
          <p className="px-3 pt-2 pb-0.5 text-[10px] font-semibold text-gray-500 uppercase tracking-widest">
            对话
          </p>
          {sessions.length > 0 ? (
            sortedSessions.map((s) => (
              <SessionItem
                key={s.id}
                id={s.id}
                title={s.title}
                isActive={sessionId === s.id}
                onSelect={() => {
                  setSessionId(s.id);
                  if (pathname !== "/") {
                    router.push("/");
                  }
                }}
                onRename={(title) => renameSession(s.id, title)}
                onDelete={() => deleteSession(s.id)}
              />
            ))
          ) : (
            <p className="px-3 py-2 text-[12px] text-gray-400">暂无对话</p>
          )}
        </div>
      </div>

      <div className="mx-3 h-px bg-black/[0.06]" />

      {/* Footer navigation */}
      <div className="shrink-0 px-2 py-2 space-y-0.5">
        <a
          href="https://github.com/ZzjNoMercy/PuddingClaw"
          target="_blank"
          rel="noopener noreferrer"
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-[13px] text-gray-500 transition-all hover:bg-black/[0.04] hover:text-gray-800"
        >
          <Github className="h-4 w-4" />
          GitHub
          <ExternalLink className="ml-auto h-3 w-3" />
        </a>
        <Link
          href="/settings"
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-[13px] text-gray-600 transition-all hover:bg-black/[0.04] hover:text-gray-900"
        >
          <Settings className="h-4 w-4" />
          设置
        </Link>
      </div>

    </aside>
  );
}

function SidebarLink({
  icon: Icon,
  label,
  muted = false,
}: {
  icon: React.ElementType;
  label: string;
  muted?: boolean;
}) {
  return (
    <button
      className={`w-full flex items-center gap-2 px-3 py-2 text-[13px] rounded-lg transition-all ${
        muted
          ? "text-gray-500 hover:text-gray-700 hover:bg-black/[0.04]"
          : "text-gray-600 hover:text-gray-900 hover:bg-black/[0.04]"
      }`}
      type="button"
    >
      <Icon className="w-4 h-4" />
      {label}
    </button>
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
        className={`w-full flex items-center gap-1.5 px-3 py-1.5 text-[12px] rounded-md transition-all text-left relative pr-8 ${
          isActive
            ? "bg-[#002fa7]/[0.08] text-[#002fa7] font-medium"
            : "text-gray-600 hover:bg-black/[0.04] hover:text-gray-900"
        }`}
      >
        {isActive && (
          <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 bg-[#002fa7] rounded-r-full" />
        )}
        <MessageSquare className="h-3 w-3 shrink-0 text-gray-500" />
        <span className="truncate">{title}</span>
      </button>

      {/* More button */}
      <div className={`absolute right-1 top-1/2 -translate-y-1/2 ${menuOpen ? "z-[60]" : ""}`} ref={menuRef}>
        <button
          onClick={(e) => {
            e.stopPropagation();
            setMenuOpen((v) => !v);
          }}
          className="p-1 rounded-md text-gray-400 opacity-0 group-hover:opacity-100 hover:text-gray-700 hover:bg-black/[0.05] transition-all"
        >
          <MoreHorizontal className="w-3.5 h-3.5" />
        </button>

        {menuOpen && (
          <div className="absolute right-0 top-full mt-1 w-32 bg-white rounded-lg shadow-lg border border-black/[0.08] py-1 z-50 animate-fade-in-scale">
            <button
              onClick={() => {
                setMenuOpen(false);
                setRenameValue(title);
                setRenaming(true);
              }}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] text-gray-600 hover:bg-black/[0.04] transition-colors"
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
