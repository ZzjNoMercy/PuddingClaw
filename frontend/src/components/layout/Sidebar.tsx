"use client";

import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import { createPortal } from "react-dom";
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
  Database,
  FolderKanban,
  Bot,
  MessagesSquare,
  Workflow,
  Settings,
  Github,
  ExternalLink,
  Archive,
  Pin,
} from "lucide-react";
import { useApp } from "@/lib/store";
import { openProject } from "@/lib/api";
import { useProjectFolderPicker } from "@/components/projects/useProjectFolderPicker";

export default function Sidebar() {
  const {
    sessionId,
    setSessionId,
    sessions,
    renameSession,
    deleteSession,
    runtimeMode,
    runtimeReady,
    setRuntimeMode,
    currentProjectId,
    setCurrentProjectId,
    projects,
    registerProject,
    updateProject,
    removeProject,
    setWorkspaceView,
  } = useApp();
  const router = useRouter();
  const pathname = usePathname();

  // Sort sessions by most recent activity first
  const sortedSessions = useMemo(
    () => [...sessions].sort((a, b) => b.updated_at - a.updated_at),
    [sessions]
  );
  const isAgentSession = useCallback(
    (session: (typeof sessions)[number]) => session.runtime_mode === "agent",
    []
  );
  const isChatSession = useCallback(
    (session: (typeof sessions)[number]) => session.runtime_mode !== "agent",
    []
  );
  const projectSessions = useMemo(() => {
    const grouped = new Map<string, typeof sessions>();
    for (const session of sortedSessions) {
      if (!isAgentSession(session)) continue;
      if (!session.project_id) continue;
      const list = grouped.get(session.project_id) || [];
      list.push(session);
      grouped.set(session.project_id, list);
    }
    return grouped;
  }, [sortedSessions, isAgentSession]);
  const conversationSessions = useMemo(() => {
    if (runtimeMode === "agent") {
      return sortedSessions.filter((session) => isAgentSession(session) && !session.project_id);
    }
    return sortedSessions.filter((session) => isChatSession(session));
  }, [runtimeMode, sortedSessions, isAgentSession, isChatSession]);

  const handleProjectPathSelected = useCallback(async (path: string) => {
    const project = await registerProject(path.trim());
    if (!project) {
      return false;
    }
    setWorkspaceView("chat");
    setSessionId("default");
    if (pathname !== "/") {
      router.push("/");
    }
    return true;
  }, [pathname, registerProject, router, setSessionId, setWorkspaceView]);

  const { openProjectFolderPicker, projectFolderDialog } = useProjectFolderPicker({
    onPathSelected: handleProjectPathSelected,
  });

  const handleAddProject = useCallback(async () => {
    await openProjectFolderPicker();
  }, [openProjectFolderPicker]);

  return (
    <>
    <aside className="flex flex-col h-full relative bg-transparent text-gray-700">
      {/* Primary actions */}
      <div className="px-2 pt-2 pb-1 space-y-0.5">
        <div className="mb-1 grid grid-cols-2 rounded-xl bg-black/[0.035] p-0.5">
          <button
            type="button"
            onClick={() => {
              setRuntimeMode("agent");
              const latest = sortedSessions.find((s) => s.runtime_mode === "agent");
              setSessionId(latest ? latest.id : "default");
              if (pathname !== "/") router.push("/");
            }}
            className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[12px] transition-all ${
              runtimeReady && runtimeMode === "agent"
                ? "bg-white/85 text-gray-900 shadow-sm"
                : "text-gray-500 hover:text-gray-800"
            }`}
          >
            <Bot className="h-3.5 w-3.5" />
            Agent
          </button>
          <button
            type="button"
            onClick={() => {
              setRuntimeMode("chat");
              const latest = sortedSessions.find((s) => s.runtime_mode !== "agent");
              setSessionId(latest ? latest.id : "default");
              if (pathname !== "/") router.push("/");
            }}
            className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[12px] transition-all ${
              runtimeReady && runtimeMode === "chat"
                ? "bg-white/85 text-gray-900 shadow-sm"
                : "text-gray-500 hover:text-gray-800"
            }`}
          >
            <MessagesSquare className="h-3.5 w-3.5" />
            Chat
          </button>
        </div>
        <button
          onClick={() => {
            // Don't create a session eagerly; only navigate to the chat page.
            // A new session will be created lazily when the user actually sends
            // their first message (handled inside sendMessage).
            setWorkspaceView("chat");
            if (pathname !== "/") {
              router.push("/");
            }
            // Switch to the placeholder "default" session so the next message
            // creates a fresh session instead of appending to the current one.
            setSessionId("default");
          }}
          className="w-full flex items-center gap-2 px-3 py-2 text-[13px] font-medium text-gray-800 hover:bg-white/50 rounded-xl transition-all"
        >
          <Plus className="w-4 h-4" />
          新对话
        </button>
        <SidebarLink icon={Search} label="搜索" muted />
        <Link
          href="/knowledge"
          className={`w-full flex items-center gap-2 px-3 py-2 text-[13px] rounded-xl transition-all ${
            runtimeReady && pathname.startsWith("/knowledge")
              ? "bg-white/72 text-[#002fa7] font-medium shadow-sm"
              : "text-gray-600 hover:text-gray-900 hover:bg-white/50"
          }`}
        >
          <Database className="w-4 h-4" />
          知识库
        </Link>
        <Link
          href="/skills"
          className={`w-full flex items-center gap-2 px-3 py-2 text-[13px] rounded-xl transition-all ${
            runtimeReady && pathname.startsWith("/skills")
              ? "bg-white/72 text-[#002fa7] font-medium shadow-sm"
              : "text-gray-600 hover:text-gray-900 hover:bg-white/50"
          }`}
        >
          <Puzzle className="w-4 h-4" />
          扩展
        </Link>
        <SidebarLink icon={Workflow} label="定时任务" muted />
      </div>

      <div className="mx-4 my-1.5 h-px bg-black/[0.04]" />

      {/* Projects */}
      {runtimeReady && runtimeMode === "agent" && (
        <div className="shrink-0 px-1.5 pb-2">
          <div className="flex items-center justify-between px-3 pt-2 pb-1">
            <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-widest">
              项目
            </p>
            <button
              type="button"
              onClick={handleAddProject}
              className="rounded p-0.5 text-gray-400 hover:bg-black/[0.05] hover:text-gray-700"
              title="添加项目"
            >
              <Plus className="h-3.5 w-3.5" />
            </button>
          </div>
          {projects.length > 0 ? (
            <div className="space-y-1">
              {projects.map((project) => {
                const childSessions = projectSessions.get(project.project_id) || [];
                return (
                  <div key={project.project_id} className="relative">
                    <ProjectItem
                      projectId={project.project_id}
                      name={project.name}
                      path={project.path}
                      pinned={Boolean(project.pinned)}
                      isActive={
                        currentProjectId === project.project_id &&
                        !childSessions.some((s) => s.id === sessionId)
                      }
                      onSelect={() => {
                        setRuntimeMode("agent");
                        setCurrentProjectId(project.project_id);
                      }}
                      onPinToggle={async () => {
                        await updateProject(project.project_id, { pinned: !project.pinned });
                      }}
                      onRename={async (name) => {
                        return Boolean(await updateProject(project.project_id, { name }));
                      }}
                      onRemove={async () => {
                        const removed = await removeProject(project.project_id);
                        if (removed && currentProjectId === project.project_id) {
                          setCurrentProjectId(null);
                          setWorkspaceView("chat");
                          setSessionId("default");
                        }
                        return removed;
                      }}
                    />
                    <div className="relative ml-5 mt-0.5 space-y-px">
                      {childSessions.length > 0 ? (
                        childSessions.slice(0, 5).map((s) => (
                          <SessionItem
                            key={s.id}
                            id={s.id}
                            title={s.title}
                            isActive={sessionId === s.id}
                            onSelect={() => {
                              setRuntimeMode("agent");
                              setCurrentProjectId(project.project_id);
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
                        <p className="px-3 py-1 text-[12px] text-gray-400">暂无对话</p>
                      )}
                      {childSessions.length > 5 && (
                        <p className="px-3 py-1 text-[12px] text-gray-400">展开显示</p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="flex items-center gap-2 px-3 py-2 text-[12px] text-gray-400">
              <FolderKanban className="h-3.5 w-3.5" />
              暂无项目
            </div>
          )}
        </div>
      )}

      {runtimeReady && runtimeMode === "agent" && <div className="mx-4 h-px bg-black/[0.04]" />}

      {/* Regular conversations */}
      <div className="flex-1 overflow-y-auto px-1.5">
        <div className="space-y-px">
          <p className="px-3 pt-2 pb-0.5 text-[10px] font-semibold text-gray-500 uppercase tracking-widest">
            对话
          </p>
          {conversationSessions.length > 0 ? (
            conversationSessions.map((s) => (
              <SessionItem
                key={s.id}
                id={s.id}
                title={s.title}
                isActive={sessionId === s.id}
                onSelect={() => {
                  if (s.runtime_mode === "agent") {
                    setRuntimeMode("agent");
                    setCurrentProjectId(null);
                  } else {
                    setRuntimeMode("chat");
                    setCurrentProjectId(null);
                  }
                  setSessionId(s.id);
                  setWorkspaceView("chat");
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

      <div className="mx-4 h-px bg-black/[0.04]" />

      {/* Footer navigation */}
      <div className="shrink-0 px-2 py-2 space-y-0.5">
        <a
          href="https://github.com/ZzjNoMercy/PuddingClaw"
          target="_blank"
          rel="noopener noreferrer"
          className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-[13px] text-gray-500 transition-all hover:bg-white/50 hover:text-gray-800"
        >
          <Github className="h-4 w-4" />
          GitHub
          <ExternalLink className="ml-auto h-3 w-3" />
        </a>
        <Link
          href="/settings"
          className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-[13px] text-gray-600 transition-all hover:bg-white/50 hover:text-gray-900"
        >
          <Settings className="h-4 w-4" />
          设置
        </Link>
      </div>

    </aside>
    {projectFolderDialog}
    </>
  );
}

function getSystemFileManagerLabel(): string {
  if (typeof navigator === "undefined") return "文件管理器";
  const platform = `${navigator.platform || ""} ${navigator.userAgent || ""}`;
  if (/Mac|iPhone|iPad|iPod/i.test(platform)) return "访达";
  if (/Win/i.test(platform)) return "资源管理器";
  return "文件管理器";
}

function ProjectItem({
  projectId,
  name,
  path,
  pinned,
  isActive,
  onSelect,
  onPinToggle,
  onRename,
  onRemove,
}: {
  projectId: string;
  name: string;
  path: string;
  pinned: boolean;
  isActive: boolean;
  onSelect: () => void;
  onPinToggle: () => Promise<void>;
  onRename: (name: string) => Promise<boolean>;
  onRemove: () => Promise<boolean>;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [opening, setOpening] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(name);
  const [savingRename, setSavingRename] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [pinning, setPinning] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const fileManagerLabel = getSystemFileManagerLabel();

  useEffect(() => {
    setRenameValue(name);
  }, [name]);

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

  const handleOpenProject = useCallback(async () => {
    setOpening(true);
    try {
      await openProject(projectId);
      setMenuOpen(false);
    } catch {
      window.alert(`无法在“${fileManagerLabel}”中打开项目，请确认后端运行在本机且项目路径可访问。`);
    } finally {
      setOpening(false);
    }
  }, [fileManagerLabel, projectId]);

  const handleTogglePin = useCallback(async () => {
    setPinning(true);
    try {
      await onPinToggle();
      setMenuOpen(false);
    } finally {
      setPinning(false);
    }
  }, [onPinToggle]);

  const handleStartRename = useCallback(() => {
    setRenameValue(name);
    setConfirmRemove(false);
    setRenaming(true);
  }, [name]);

  const handleSaveRename = useCallback(async () => {
    const nextName = renameValue.trim();
    if (!nextName) return;
    setSavingRename(true);
    try {
      const ok = await onRename(nextName);
      if (ok) {
        setRenaming(false);
        setMenuOpen(false);
      }
    } finally {
      setSavingRename(false);
    }
  }, [onRename, renameValue]);

  const handleRemove = useCallback(async () => {
    if (!confirmRemove) {
      setRenaming(false);
      setConfirmRemove(true);
      return;
    }
    setRemoving(true);
    try {
      const ok = await onRemove();
      if (ok) {
        setMenuOpen(false);
      }
    } finally {
      setRemoving(false);
    }
  }, [confirmRemove, onRemove]);

  return (
    <div className={`group/project relative flex items-center ${menuOpen ? "z-[70]" : "z-10"}`}>
      <button
        type="button"
        onClick={onSelect}
        className={`flex min-w-0 flex-1 items-center gap-2 rounded-xl px-3 py-1.5 pr-14 text-left text-[12px] transition-all ${
          isActive
            ? "bg-white/68 text-gray-900 font-medium shadow-sm"
            : "text-gray-700 hover:bg-white/48"
        }`}
        title={path}
      >
        <FolderKanban className="h-3.5 w-3.5 shrink-0 text-gray-500" />
        <span className="truncate">{name}</span>
        {pinned && (
          <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-[#002fa7]/10 text-[#002fa7]" title="已置顶">
            <Pin className="h-2.5 w-2.5" />
          </span>
        )}
      </button>

      <div
        className={`absolute right-3 top-1/2 z-20 -translate-y-1/2 ${
          menuOpen ? "z-[80]" : ""
        }`}
        ref={menuRef}
      >
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            setMenuOpen((open) => !open);
          }}
          className={`flex h-7 w-7 items-center justify-center rounded-lg text-gray-400 transition-all hover:bg-black/[0.05] hover:text-gray-700 focus:bg-black/[0.05] focus:text-gray-700 focus:outline-none ${
            menuOpen ? "opacity-100" : "opacity-0 group-hover/project:opacity-100 focus:opacity-100"
          }`}
          title="项目操作"
        >
          <MoreHorizontal className="h-3.5 w-3.5" />
        </button>

        {menuOpen && (
          <div className="absolute right-0 top-full mt-1 w-56 rounded-2xl border border-black/[0.08] bg-white p-1.5 shadow-2xl shadow-slate-900/15 animate-fade-in-scale">
            <button
              type="button"
              onClick={handleTogglePin}
              disabled={pinning}
              className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-[13px] text-gray-700 transition-colors hover:bg-black/[0.04] hover:text-gray-950 disabled:cursor-wait disabled:opacity-60"
            >
              <Pin className="h-4 w-4" />
              {pinned ? "取消置顶" : "置顶项目"}
            </button>
            <button
              type="button"
              onClick={handleOpenProject}
              disabled={opening}
              className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-[13px] text-gray-700 transition-colors hover:bg-black/[0.04] hover:text-gray-950 disabled:cursor-wait disabled:opacity-60"
            >
              <FolderKanban className="h-4 w-4" />
              在“{fileManagerLabel}”中打开
            </button>
            {renaming ? (
              <div className="mt-1 rounded-xl bg-black/[0.025] p-2">
                <input
                  value={renameValue}
                  onChange={(event) => setRenameValue(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void handleSaveRename();
                    if (event.key === "Escape") setRenaming(false);
                  }}
                  className="h-8 w-full rounded-lg border border-black/[0.08] bg-white px-2 text-[12px] outline-none focus:border-[#002fa7]/40 focus:ring-2 focus:ring-[#002fa7]/10"
                  autoFocus
                />
                <div className="mt-2 flex justify-end gap-1.5">
                  <button
                    type="button"
                    onClick={() => setRenaming(false)}
                    className="rounded-lg px-2 py-1 text-[11px] text-gray-500 hover:bg-black/[0.04]"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    onClick={handleSaveRename}
                    disabled={savingRename || !renameValue.trim()}
                    className="rounded-lg bg-[#002fa7] px-2 py-1 text-[11px] font-medium text-white disabled:opacity-50"
                  >
                    保存
                  </button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                onClick={handleStartRename}
                className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-[13px] text-gray-700 transition-colors hover:bg-black/[0.04] hover:text-gray-950"
              >
                <Pencil className="h-4 w-4" />
                重命名项目
              </button>
            )}
            <button
              type="button"
              onClick={handleRemove}
              disabled={removing}
              className={`flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-[13px] transition-colors disabled:cursor-wait disabled:opacity-60 ${
                confirmRemove
                  ? "bg-red-50 text-red-600 hover:bg-red-100"
                  : "text-red-500 hover:bg-red-50 hover:text-red-600"
              }`}
            >
              <Trash2 className="h-4 w-4" />
              {confirmRemove ? "确认移除" : "移除"}
            </button>
            {confirmRemove && (
              <button
                type="button"
                onClick={() => setConfirmRemove(false)}
                className="flex w-full items-center gap-2 rounded-xl px-3 py-1.5 text-left text-[12px] text-gray-400 transition-colors hover:bg-black/[0.04] hover:text-gray-600"
              >
                <X className="h-3.5 w-3.5" />
                取消移除
              </button>
            )}
          </div>
        )}
      </div>
    </div>
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
      className={`w-full flex items-center gap-2 px-3 py-2 text-[13px] rounded-xl transition-all ${
        muted
          ? "text-gray-500 hover:text-gray-700 hover:bg-white/50"
          : "text-gray-600 hover:text-gray-900 hover:bg-white/50"
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
  const [menuPosition, setMenuPosition] = useState<{ left: number; top: number } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        menuRef.current &&
        !menuRef.current.contains(target) &&
        !menuButtonRef.current?.contains(target)
      ) {
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
    if (confirm("确定删除这个对话吗？")) {
      onDelete();
    }
  }, [onDelete]);

  const toggleMenu = useCallback(() => {
    const rect = menuButtonRef.current?.getBoundingClientRect();
    if (rect) {
      const width = 128;
      const height = 88;
      const drawerRect = menuButtonRef.current?.closest("aside")?.getBoundingClientRect();
      const drawerRight = drawerRect?.right ?? window.innerWidth;
      const left = Math.min(drawerRight - width - 12, window.innerWidth - width - 12);
      const top = Math.min(rect.bottom + 6, window.innerHeight - height - 12);
      setMenuPosition({ left: Math.max(12, left), top: Math.max(12, top) });
    }
    setMenuOpen((v) => !v);
  }, []);

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
    <div className={`relative group ${menuOpen ? "z-[90]" : ""}`}>
      <button
        onClick={onSelect}
        className={`w-full flex items-center gap-1.5 px-3 py-1.5 text-[12px] rounded-xl transition-all text-left relative pr-8 ${
          isActive
            ? "bg-white/72 text-[#002fa7] font-medium shadow-sm"
            : "text-gray-600 hover:bg-white/48 hover:text-gray-900"
        }`}
      >
        <MessageSquare className="h-3 w-3 shrink-0 text-gray-500" />
        <span className="truncate">{title}</span>
      </button>

      {/* More button */}
      <div className={`absolute right-1 top-1/2 -translate-y-1/2 ${menuOpen ? "z-[100]" : ""}`}>
        <button
          ref={menuButtonRef}
          onClick={(e) => {
            e.stopPropagation();
            toggleMenu();
          }}
          className="p-1 rounded-md text-gray-400 opacity-0 group-hover:opacity-100 hover:text-gray-700 hover:bg-black/[0.05] transition-all"
        >
          <MoreHorizontal className="w-3.5 h-3.5" />
        </button>

        {menuOpen && menuPosition && typeof document !== "undefined" ? createPortal(
          <div
            ref={menuRef}
            style={{ left: menuPosition.left, top: menuPosition.top }}
            className="fixed z-[9999] w-32 rounded-lg border border-black/[0.08] bg-white py-1 shadow-lg animate-fade-in-scale"
          >
            <button
              onClick={() => {
                setMenuOpen(false);
                setRenameValue(title);
                setRenaming(true);
              }}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] text-gray-600 hover:bg-black/[0.04] transition-colors"
            >
              <Pencil className="w-3 h-3" />
              重命名
            </button>
            <button
              onClick={handleDelete}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] text-red-500 hover:bg-red-50 transition-colors"
            >
              <Trash2 className="w-3 h-3" />
              删除
            </button>
          </div>,
          document.body
        ) : null}
      </div>
    </div>
  );
}
