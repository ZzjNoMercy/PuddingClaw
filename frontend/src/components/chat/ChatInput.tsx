"use client";

import { useState, useRef, useCallback, useEffect, useMemo } from "react";
import {
  ArrowUp,
  Check,
  ChevronDown,
  FolderKanban,
  FolderPlus,
  Square,
  XCircle,
  Activity,
} from "lucide-react";
import { useApp } from "@/lib/store";
import { listSkills, getSessionTokenCount } from "@/lib/api";

function formatTokens(n: number): string {
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
}
import SlashCommandMenu from "./SlashCommandMenu";

export default function ChatInput() {
  const [text, setText] = useState("");
  const {
    sendMessage,
    stopStreaming,
    isStreaming,
    isCompressing,
    sessionId,
    contextUsage,
    setContextUsage,
    pendingInput,
    setPendingInput,
    runtimeMode,
    setRuntimeMode,
    currentProjectId,
    setCurrentProjectId,
    projects,
    registerProject,
  } = useApp();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const projectMenuRef = useRef<HTMLDivElement>(null);
  const disabled = isStreaming || isCompressing;
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);

  // Fetch token count on mount and when session changes
  useEffect(() => {
    getSessionTokenCount(sessionId)
      .then((data) => {
        setContextUsage({
          used: data.total_tokens,
          total: data.compaction_trigger,
          percentage: data.percentage,
        });
      })
      .catch(() => {});
  }, [sessionId, setContextUsage]);

  // Slash command state
  const [showSlashMenu, setShowSlashMenu] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [selectedMenuIndex, setSelectedMenuIndex] = useState(0);
  const [skills, setSkills] = useState<Array<{ name: string; description: string }>>([]);
  // Track the position of the `/` that triggered the menu, for replacement on select
  const slashStartPosRef = useRef<number>(-1);
  // Pending cursor position to set after React re-render (fixes I-2: rAF race)
  const pendingCursorRef = useRef<number | null>(null);

  // Preload skills on mount
  useEffect(() => {
    listSkills().then(setSkills).catch(() => {});
  }, []);

  // Single source of truth for filtered skills (fixes I-1: dedup filter logic)
  const filteredSkills = useMemo(
    () => skills.filter((s) =>
      s.name.toLowerCase().includes(slashQuery) ||
      s.description.toLowerCase().includes(slashQuery)
    ),
    [skills, slashQuery]
  );

  // Ref to let global Escape handler know if slash menu is open (fixes I-2)
  const showSlashMenuRef = useRef(false);
  useEffect(() => { showSlashMenuRef.current = showSlashMenu; }, [showSlashMenu]);

  const selectedProject = useMemo(
    () => projects.find((project) => project.project_id === currentProjectId) || null,
    [projects, currentProjectId]
  );

  useEffect(() => {
    if (!projectMenuOpen) return;
    const handler = (event: MouseEvent) => {
      if (projectMenuRef.current && !projectMenuRef.current.contains(event.target as Node)) {
        setProjectMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [projectMenuOpen]);

  // Track IME composition so Enter to confirm pinyin/hiragana doesn't submit (fixes IME-1)
  const isComposingRef = useRef(false);

  // Prefill input from external actions (e.g. "create skill" button in /skills)
  useEffect(() => {
    if (pendingInput && textareaRef.current) {
      setText(pendingInput);
      setPendingInput(null);
      textareaRef.current.focus();
      // Auto-resize to fit prefilled text
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [pendingInput, setPendingInput]);

  // Apply pending cursor position after React re-renders textarea with new text
  useEffect(() => {
    if (pendingCursorRef.current !== null && textareaRef.current) {
      textareaRef.current.setSelectionRange(pendingCursorRef.current, pendingCursorRef.current);
      pendingCursorRef.current = null;
    }
  }, [text]);

  const handleSubmit = useCallback(() => {
    if (!text.trim() || disabled) return;
    sendMessage(text.trim());
    setText("");
    setPendingInput(null);
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }, [text, disabled, sendMessage, setPendingInput]);

  const handleRegisterProject = useCallback(async () => {
    let path: string | null = null;
    if (window.electron?.selectProjectFolder) {
      path = await window.electron.selectProjectFolder();
    } else {
      path = window.prompt("输入本地项目目录路径");
    }
    if (!path?.trim()) return;
    const project = await registerProject(path.trim());
    if (!project) {
      window.alert("项目目录登记失败，请确认路径存在且是文件夹。");
      return;
    }
    setProjectMenuOpen(false);
  }, [registerProject]);

  const handleSlashSelect = useCallback((skillName: string) => {
    // Use textarea DOM value as source of truth to avoid stale closure (fixes I-1)
    const currentText = textareaRef.current?.value ?? "";
    const startPos = slashStartPosRef.current;
    if (startPos >= 0) {
      const cursorPos = textareaRef.current?.selectionStart ?? currentText.length;
      const before = currentText.slice(0, startPos);
      const after = currentText.slice(cursorPos);
      const inserted = `/${skillName} `;
      const newText = before + inserted + after;
      setText(newText);
      // Schedule cursor placement after React re-render (fixes I-2)
      pendingCursorRef.current = startPos + inserted.length;
    } else {
      setText(`/${skillName} `);
    }
    setShowSlashMenu(false);
    slashStartPosRef.current = -1;
    textareaRef.current?.focus();
  }, []);

  // Escape key to stop streaming (global listener)
  // Skip if slash menu is open — let the local handler close it first (I-2 fix)
  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isStreaming && !showSlashMenuRef.current) {
        e.preventDefault();
        stopStreaming();
      }
    };
    window.addEventListener("keydown", handleGlobalKeyDown);
    return () => window.removeEventListener("keydown", handleGlobalKeyDown);
  }, [isStreaming, stopStreaming]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (showSlashMenu) {
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedMenuIndex((prev) => Math.max(0, prev - 1));
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedMenuIndex((prev) => Math.min(prev + 1, Math.max(0, filteredSkills.length - 1)));
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (filteredSkills.length > 0) {
          const idx = Math.min(selectedMenuIndex, filteredSkills.length - 1);
          handleSlashSelect(filteredSkills[idx].name);
        }
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setShowSlashMenu(false);
        return;
      }
    }
    // Original submit logic — ignore Enter while IME is composing so users can
    // confirm candidate characters (or type English directly) without sending.
    if (e.key === "Enter" && !e.shiftKey && !isComposingRef.current && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleInput = () => {
    const el = textareaRef.current;
    if (el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 160) + "px"; }
  };

  return (
    <div className="px-6 pb-4 pt-2">
      <div className="glass-input relative mx-auto flex w-full max-w-[820px] flex-col gap-2 rounded-3xl px-4 py-3 transition-shadow hover:shadow-lg">
        <SlashCommandMenu
          visible={showSlashMenu}
          filteredSkills={filteredSkills}
          selectedIndex={selectedMenuIndex}
          onSelect={handleSlashSelect}
          onClose={() => setShowSlashMenu(false)}
        />
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => {
            const val = e.target.value;
            const cursorPos = e.target.selectionStart ?? val.length;
            setText(val);
            handleInput();

            // Slash command detection: scan backwards from cursor for `/`
            // Trigger when `/` is at start of text or preceded by a space/newline,
            // and there's no space between `/` and cursor (i.e. still typing the command name)
            let slashPos = -1;
            for (let i = cursorPos - 1; i >= 0; i--) {
              const ch = val[i];
              if (ch === " " || ch === "\n") break; // hit whitespace before finding `/`
              if (ch === "/") {
                // Valid if at start or preceded by space/newline
                if (i === 0 || val[i - 1] === " " || val[i - 1] === "\n") {
                  slashPos = i;
                }
                break;
              }
            }

            if (slashPos >= 0) {
              const query = val.slice(slashPos + 1, cursorPos).toLowerCase();
              setShowSlashMenu(true);
              setSlashQuery(query);
              setSelectedMenuIndex(0);
              slashStartPosRef.current = slashPos;
            } else {
              setShowSlashMenu(false);
              slashStartPosRef.current = -1;
            }
          }}
          onKeyDown={handleKeyDown}
          onCompositionStart={() => { isComposingRef.current = true; }}
          onCompositionEnd={() => { isComposingRef.current = false; }}
          placeholder="输入消息，或用 / 调用扩展能力"
          rows={1}
          className="max-h-40 min-h-12 w-full resize-none bg-transparent px-1 py-1 text-[14px] leading-relaxed outline-none placeholder:text-gray-400"
        />

        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            {runtimeMode === "agent" && (
              <div className="relative" ref={projectMenuRef}>
                <button
                  type="button"
                  onClick={() => setProjectMenuOpen((open) => !open)}
                  className={`flex h-8 max-w-[260px] items-center gap-1.5 rounded-full border px-3 text-[12px] transition-all ${
                    selectedProject
                      ? "border-[#002fa7]/15 bg-[#e8edff] text-[#002fa7] hover:bg-[#dfe7ff]"
                      : "border-black/[0.06] bg-white/42 text-gray-600 hover:bg-white/70 hover:text-gray-900"
                  }`}
                  title={selectedProject?.path || "选择 Agent 工作项目"}
                >
                  <FolderKanban className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">
                    {selectedProject ? selectedProject.name : "进入项目工作"}
                  </span>
                  <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                </button>

                {projectMenuOpen && (
                  <div className="absolute bottom-full left-0 z-50 mb-2 w-80 rounded-2xl border border-black/[0.10] bg-white p-2 shadow-2xl shadow-slate-900/15 animate-fade-in-scale">
                    <div className="px-3 pb-2 pt-1">
                      <p className="text-[11px] font-semibold text-gray-500">Agent 工作项目</p>
                      <p className="mt-0.5 text-[11px] leading-relaxed text-gray-400">
                        项目会作为 DeepAgents 文件工作区；不选择项目时使用隐式会话工作区。
                      </p>
                    </div>

                    <div className="max-h-52 overflow-y-auto py-1">
                      {projects.length > 0 ? (
                        projects.map((project) => (
                          <button
                            type="button"
                            key={project.project_id}
                            onClick={() => {
                              setRuntimeMode("agent");
                              setCurrentProjectId(project.project_id);
                              setProjectMenuOpen(false);
                            }}
                            className={`flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left transition-colors ${
                              currentProjectId === project.project_id
                                ? "bg-[#002fa7]/[0.07] text-[#002fa7]"
                                : "text-gray-700 hover:bg-black/[0.04] hover:text-gray-950"
                            }`}
                          >
                            <FolderKanban className="mt-0.5 h-4 w-4 shrink-0" />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-[13px] font-medium">
                                {project.name}
                              </span>
                              <span className="block truncate text-[11px] text-gray-400">
                                {project.path}
                              </span>
                            </span>
                            {currentProjectId === project.project_id && (
                              <Check className="mt-0.5 h-4 w-4 shrink-0" />
                            )}
                          </button>
                        ))
                      ) : (
                        <p className="px-3 py-3 text-[12px] text-gray-400">
                          还没有项目，先登记一个本地文件夹。
                        </p>
                      )}
                    </div>

                    <div className="my-1 h-px bg-black/[0.06]" />

                    <button
                      type="button"
                      onClick={handleRegisterProject}
                      className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-[13px] text-gray-700 transition-colors hover:bg-black/[0.04] hover:text-gray-950"
                    >
                      <FolderPlus className="h-4 w-4" />
                      使用现有文件夹…
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setCurrentProjectId(null);
                        setProjectMenuOpen(false);
                      }}
                      className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-[13px] text-gray-500 transition-colors hover:bg-black/[0.04] hover:text-gray-800"
                    >
                      <XCircle className="h-4 w-4" />
                      不使用项目，作为 Agent 对话
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <ContextUsageTooltip usage={contextUsage} />

            {isStreaming ? (
              <button
                onClick={stopStreaming}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-red-500 text-white transition-all hover:bg-red-600 active:scale-95"
                title="停止生成 (Esc)"
              >
                <Square className="w-3.5 h-3.5 fill-current" />
              </button>
            ) : (
              <button
                onClick={handleSubmit}
                disabled={!text.trim() || isCompressing}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#002fa7] text-white transition-all hover:bg-[#001f7a] active:scale-95 disabled:bg-gray-300 disabled:opacity-80"
              >
                <ArrowUp className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      </div>

      <p className="viewport-center-axis mt-1 text-center text-[10px] text-gray-400/45">
        Powered by DeepSeek · PuddingClaw v0.1
      </p>
    </div>
  );
}

function ContextUsageTooltip({
  usage,
}: {
  usage: { used: number; total: number; percentage: number };
}) {
  const [open, setOpen] = useState(false);
  const color =
    usage.percentage >= 90 ? "text-red-500" : usage.percentage >= 70 ? "text-amber-500" : "text-gray-400";

  return (
    <div
      className="relative"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      <button
        type="button"
        className={`flex h-7 items-center gap-1 rounded-full border border-black/[0.06] bg-white/50 px-2.5 text-[11px] font-medium transition-colors hover:bg-white/80 ${color}`}
      >
        <Activity className="h-3 w-3" />
        {usage.percentage.toFixed(0)}%
      </button>
      {open && (
        <div className="absolute bottom-full right-0 mb-2 w-56 rounded-xl bg-[#1f2937] px-3.5 py-2.5 text-[12px] text-white shadow-xl animate-fade-in-scale z-50">
          <p className="font-medium text-gray-200">背景信息窗口</p>
          <p className="mt-1 text-[16px] font-semibold">
            {usage.percentage.toFixed(0)}% 已用
          </p>
          <p className="mt-1 text-[11px] text-gray-400">
            已用 {formatTokens(usage.used)}，共 {formatTokens(usage.total)}
          </p>
          <div className="absolute bottom-[-5px] right-4 h-2.5 w-2.5 rotate-45 bg-[#1f2937]" />
        </div>
      )}
    </div>
  );
}
