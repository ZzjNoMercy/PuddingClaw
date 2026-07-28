"use client";

import { useState, useRef, useCallback, useEffect, useMemo } from "react";
import {
  ArrowUp,
  ArrowLeft,
  Check,
  ChevronDown,
  FolderKanban,
  FolderPlus,
  Square,
  XCircle,
  Activity,
  Brain,
  FileArchive,
  FileImage,
  FileSpreadsheet,
  FileText,
  ImagePlus,
  Layers3,
  Paperclip,
  Plus,
  ShieldCheck,
  Target,
  X,
  type LucideIcon,
} from "lucide-react";
import { useApp } from "@/lib/store";
import { isSessionSubmitting } from "@/lib/sessionConcurrency";
import { useProjectFolderPicker } from "@/components/projects/useProjectFolderPicker";
import ConfirmDialog from "@/components/ui/ConfirmDialog";
import {
  listAnalyticsModels,
  listSkills,
  getSessionTokenCount,
  uploadAgentAttachments,
  type AgentAttachment,
  type AnalyticsModelSummary,
  type ApprovalMode,
} from "@/lib/api";

function formatTokens(n: number): string {
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
}

function formatContextPercentage(percentage: number, used: number): string {
  if (used > 0 && percentage < 1) return "<1%";
  return `${percentage.toFixed(0)}%`;
}
import SlashCommandMenu from "./SlashCommandMenu";

type AttachmentKind = AgentAttachment["type"];
type OpenPopover = null | "plus" | "plus-model" | "project" | "approval";
type SelectedSkillHint = { name: string; start: number; end: number };

const terminalRunStatuses = new Set([
  "completed",
  "cancelled",
  "failed",
  "blocked",
  "budget_exceeded",
  "verification_failed",
]);

function formatFileSize(size?: number): string {
  if (!size) return "";
  if (size < 1024) return `${size}B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)}KB`;
  return `${(size / 1024 / 1024).toFixed(size < 10 * 1024 * 1024 ? 1 : 0)}MB`;
}

const attachmentStyles: Record<AttachmentKind, { cls: string; label: string; Icon: LucideIcon }> = {
  image: {
    cls: "border-[#002fa7]/15 bg-[#e8edff] text-[#002fa7]",
    label: "图片",
    Icon: FileImage,
  },
  pdf: {
    cls: "border-rose-500/15 bg-rose-50 text-rose-700",
    label: "PDF",
    Icon: FileText,
  },
  spreadsheet: {
    cls: "border-emerald-500/15 bg-emerald-50 text-emerald-700",
    label: "表格",
    Icon: FileSpreadsheet,
  },
  markdown: {
    cls: "border-violet-500/15 bg-violet-50 text-violet-700",
    label: "MD",
    Icon: FileText,
  },
  text: {
    cls: "border-sky-500/15 bg-sky-50 text-sky-700",
    label: "文本",
    Icon: FileText,
  },
  document: {
    cls: "border-amber-500/15 bg-amber-50 text-amber-700",
    label: "文档",
    Icon: FileText,
  },
  file: {
    cls: "border-slate-500/15 bg-slate-100 text-slate-700",
    label: "文件",
    Icon: FileArchive,
  },
};

export default function ChatInput() {
  const [attachments, setAttachments] = useState<AgentAttachment[]>([]);
  const {
    sendMessage,
    stopStreaming,
    isStreaming,
    isCompressing,
    sessionHistoryLoading,
    sessionId,
    setSessionId,
    createSession,
    messages,
    contextUsage,
    setContextUsage,
    pendingInput,
    setPendingInput,
    getInputDraft,
    setInputDraft,
    runtimeMode,
    setRuntimeMode,
    currentProjectId,
    setCurrentProjectId,
    projects,
    registerProject,
    thinkingMode,
    setThinkingMode,
    analyticsModelId,
    setAnalyticsModelId,
    goalModeEnabled,
    setGoalModeEnabled,
    activeGoal,
    cancelActiveGoal,
    currentRun,
    hasActiveRun,
    approvalMode,
    approvalModeSaving,
    approvalModeError,
    setApprovalMode,
    setInspectorOpen,
    setInspectorActiveTab,
    activeAttachmentPreview,
    closeAttachmentPreview,
  } = useApp();
  // Initialize from the per-session draft so typed text survives page
  // navigation (the store outlives this component; local state does not).
  const [text, setText] = useState(() => getInputDraft(sessionId));
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const controlsMenuRef = useRef<HTMLDivElement>(null);
  const submitInFlightSessionsRef = useRef<Set<string>>(new Set());
  const currentSessionIdRef = useRef(sessionId);
  const contextUsageRequestRef = useRef(0);
  const [openPopover, setOpenPopover] = useState<OpenPopover>(null);
  const openPopoverRef = useRef<OpenPopover>(null);
  const [submittingSessionIds, setSubmittingSessionIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [uploadingCount, setUploadingCount] = useState(0);
  const [inputError, setInputError] = useState<string | null>(null);
  const [goalCancelPending, setGoalCancelPending] = useState(false);
  const [goalCancelConfirmationOpen, setGoalCancelConfirmationOpen] = useState(false);
  const isUploading = uploadingCount > 0;
  const approvalLocked =
    hasActiveRun ||
    approvalModeSaving ||
    Boolean(currentRun && !terminalRunStatuses.has(currentRun.status));
  const isSubmitting = isSessionSubmitting(submittingSessionIds, sessionId);
  const disabled = sessionHistoryLoading || isStreaming || isCompressing || approvalModeSaving || isSubmitting || isUploading || currentRun?.status === "waiting_hitl";
  const configurationBusy = isSubmitting || isUploading;
  const [analyticsModels, setAnalyticsModels] = useState<AnalyticsModelSummary[]>([]);
  const detectedImagePaths = useMemo(() => {
    const matches = text.match(/(?:~|\/|[A-Za-z]:[\\/])(?:[^\s'"<>]|\\ )+\.(?:png|jpe?g|webp|gif|bmp|tiff?)/gi);
    return Array.from(new Set(matches || [])).slice(0, 4);
  }, [text]);

  useEffect(() => {
    currentSessionIdRef.current = sessionId;
  }, [sessionId]);

  // Attachment drafts are Session-owned. Never carry a selected/uploaded file
  // into a different conversation when the user switches quickly.
  useEffect(() => {
    setAttachments([]);
    setInputError(null);
    if (attachmentInputRef.current) attachmentInputRef.current.value = "";
  }, [sessionId]);

  // Per-session input draft: restore the target session's draft when
  // switching sessions; otherwise persist every text change under the
  // current session id. Cleared on send via setText("").
  const draftSessionRef = useRef<string | null>(null);
  useEffect(() => {
    if (draftSessionRef.current === sessionId) {
      setInputDraft(sessionId, text);
      return;
    }
    draftSessionRef.current = sessionId;
    setText(getInputDraft(sessionId));
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, text]);

  // Fetch token count on mount, when session changes, and after a streaming
  // response finishes (so newly loaded messages are reflected immediately).
  // Keep the same full-context definition as the live context_usage events:
  // system prompt + messages + tool outputs (or the recorded runtime peak).
  const refreshContextUsage = useCallback(() => {
    if (!sessionId) return;
    const requestedSessionId = sessionId;
    const requestId = contextUsageRequestRef.current + 1;
    contextUsageRequestRef.current = requestId;
    getSessionTokenCount(requestedSessionId)
      .then((data) => {
        // Initial page restoration briefly mounts with the default session.
        // Its slower response must never overwrite the token meter after the
        // persisted real session has already been restored. The sequence check
        // also prevents older same-session refreshes from winning a race.
        if (
          currentSessionIdRef.current !== requestedSessionId ||
          contextUsageRequestRef.current !== requestId
        ) {
          return;
        }
        const used = data.total_tokens;
        const total = data.compaction_trigger;
        const percentage = Math.min(100, data.percentage);
        setContextUsage({
          used,
          total,
          percentage,
        });
      })
      .catch(() => {});
  }, [sessionId, setContextUsage]);

  useEffect(() => {
    refreshContextUsage();
  }, [refreshContextUsage]);

  useEffect(() => {
    if (!isStreaming) {
      refreshContextUsage();
    }
  }, [isStreaming, messages.length, refreshContextUsage]);

  // Slash command state
  const [showSlashMenu, setShowSlashMenu] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [selectedMenuIndex, setSelectedMenuIndex] = useState(0);
  const [skills, setSkills] = useState<Array<{ name: string; description: string }>>([]);
  const selectedSkillHintsBySessionRef = useRef<Map<string, SelectedSkillHint[]>>(new Map());
  // Track the position of the `/` that triggered the menu, for replacement on select
  const slashStartPosRef = useRef<number>(-1);
  const thinkingToggleInFlightRef = useRef(false);
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
  const selectedAnalyticsModel = useMemo(
    () => analyticsModels.find((model) => model.id === analyticsModelId) || null,
    [analyticsModelId, analyticsModels]
  );

  useEffect(() => {
    if (runtimeMode !== "agent") return;
    listAnalyticsModels()
      .then((result) => setAnalyticsModels(result.models))
      .catch(() => setAnalyticsModels([]));
  }, [runtimeMode]);

  useEffect(() => {
    openPopoverRef.current = openPopover;
    if (!openPopover) return;
    const handler = (event: PointerEvent) => {
      if (controlsMenuRef.current && !controlsMenuRef.current.contains(event.target as Node)) {
        setOpenPopover(null);
      }
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [openPopover]);

  const togglePopover = useCallback((popover: Exclude<OpenPopover, null>) => {
    setShowSlashMenu(false);
    setOpenPopover((current) => current === popover ? null : popover);
  }, []);

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

  const handleSubmit = useCallback(async () => {
    if (
      (!text.trim() && attachments.length === 0) ||
      disabled ||
      submitInFlightSessionsRef.current.has(sessionId)
    ) return;
    const submittedText = text;
    const submittedAttachments = attachments;
    const submittedSkillHintRecords = (
      selectedSkillHintsBySessionRef.current.get(sessionId) || []
    ).filter((hint) => text.slice(hint.start, hint.end) === `/${hint.name}`);
    const submittedSkillHints = Array.from(
      new Set(submittedSkillHintRecords.map((hint) => hint.name)),
    );
    const submittedSessionId = sessionId;
    submitInFlightSessionsRef.current.add(submittedSessionId);
    setSubmittingSessionIds((current) => new Set(current).add(submittedSessionId));
    setInputError(null);
    setOpenPopover(null);
    setText("");
    selectedSkillHintsBySessionRef.current.delete(submittedSessionId);
    setAttachments([]);
    setPendingInput(null);
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    if (attachmentInputRef.current) attachmentInputRef.current.value = "";
    try {
      const accepted = await sendMessage(submittedText.trim(), submittedAttachments, {
        skillHints: submittedSkillHints,
      });
      if (!accepted && currentSessionIdRef.current === submittedSessionId) {
        setText((current) => current || submittedText);
        setAttachments((current) => current.length > 0 ? current : submittedAttachments);
        selectedSkillHintsBySessionRef.current.set(submittedSessionId, submittedSkillHintRecords);
        setInputError("消息未发出，已恢复输入内容，请重试。");
      }
    } catch (error) {
      if (currentSessionIdRef.current === submittedSessionId) {
        setText((current) => current || submittedText);
        setAttachments((current) => current.length > 0 ? current : submittedAttachments);
        selectedSkillHintsBySessionRef.current.set(submittedSessionId, submittedSkillHintRecords);
        setInputError(error instanceof Error ? error.message : "消息发送失败，已恢复输入内容。");
      }
    } finally {
      submitInFlightSessionsRef.current.delete(submittedSessionId);
      setSubmittingSessionIds((current) => {
        const next = new Set(current);
        next.delete(submittedSessionId);
        return next;
      });
    }
  }, [text, attachments, disabled, sendMessage, setPendingInput, sessionId]);

  const handleAttachmentFiles = useCallback(async (files: FileList | File[] | null, source: "upload" | "paste" = "upload") => {
    if (!files || files.length === 0) return;
    const fileList = Array.from(files).slice(0, 8);
    setUploadingCount((count) => count + 1);
    setInputError(null);
    let targetSessionId = sessionId;
    try {
      targetSessionId = sessionId === "default" ? (await createSession() || "") : sessionId;
      if (!targetSessionId) throw new Error("无法创建会话，附件尚未上传。");
      const next = await uploadAgentAttachments(fileList, targetSessionId, source);
      if (currentSessionIdRef.current !== targetSessionId) return;
      setAttachments((current) => [...current, ...next].slice(0, 8));
    } catch (error) {
      if (currentSessionIdRef.current === targetSessionId) {
        setInputError(error instanceof Error ? error.message : "附件上传失败，请重试。");
      }
    } finally {
      setUploadingCount((count) => Math.max(0, count - 1));
      if (attachmentInputRef.current) attachmentInputRef.current.value = "";
    }
  }, [createSession, sessionId]);

  const handlePaste = useCallback((event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.files || []);
    const itemFiles = Array.from(event.clipboardData.items || [])
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter((file): file is File => Boolean(file));
    const pastedFiles = files.length ? files : itemFiles;
    if (pastedFiles.length === 0) return;
    event.preventDefault();
    void handleAttachmentFiles(pastedFiles, "paste");
  }, [handleAttachmentFiles]);

  const handleToggleThinking = useCallback(async () => {
    if (thinkingToggleInFlightRef.current) return;
    setOpenPopover(null);
    thinkingToggleInFlightRef.current = true;
    const next = !thinkingMode;
    try {
      await setThinkingMode(next);
    } catch (err) {
      console.error("Failed to toggle thinking mode:", err);
    } finally {
      thinkingToggleInFlightRef.current = false;
    }
  }, [thinkingMode, setThinkingMode]);

  const handleProjectPathSelected = useCallback(async (path: string) => {
    const project = await registerProject(path.trim());
    if (!project) {
      return false;
    }
    setRuntimeMode("agent");
    setCurrentProjectId(project.project_id);
    setSessionId("default");
    setOpenPopover(null);
    return true;
  }, [registerProject, setCurrentProjectId, setRuntimeMode, setSessionId]);

  const { openProjectFolderPicker, projectFolderDialog } = useProjectFolderPicker({
    onPathSelected: handleProjectPathSelected,
  });

  const handleRegisterProject = useCallback(async () => {
    await openProjectFolderPicker();
  }, [openProjectFolderPicker]);

  const handleSlashSelect = useCallback((skillName: string) => {
    // Use textarea DOM value as source of truth to avoid stale closure (fixes I-1)
    const currentText = textareaRef.current?.value ?? "";
    const startPos = slashStartPosRef.current;
    let insertedStart = 0;
    let adjustedHints: SelectedSkillHint[] = [];
    if (startPos >= 0) {
      const cursorPos = textareaRef.current?.selectionStart ?? currentText.length;
      const before = currentText.slice(0, startPos);
      const after = currentText.slice(cursorPos);
      const inserted = `/${skillName} `;
      insertedStart = startPos;
      const delta = inserted.length - (cursorPos - startPos);
      adjustedHints = (selectedSkillHintsBySessionRef.current.get(sessionId) || []).flatMap((hint) => {
        if (hint.end <= startPos) return [hint];
        if (hint.start >= cursorPos) {
          return [{ ...hint, start: hint.start + delta, end: hint.end + delta }];
        }
        return [];
      });
      const newText = before + inserted + after;
      setText(newText);
      // Schedule cursor placement after React re-render (fixes I-2)
      pendingCursorRef.current = startPos + inserted.length;
    } else {
      setText(`/${skillName} `);
    }
    setShowSlashMenu(false);
    selectedSkillHintsBySessionRef.current.set(sessionId, [
      ...adjustedHints.filter((hint) => hint.name !== skillName),
      { name: skillName, start: insertedStart, end: insertedStart + skillName.length + 1 },
    ]);
    slashStartPosRef.current = -1;
    textareaRef.current?.focus();
  }, [sessionId]);

  // Escape closes the nearest transient UI before it can stop a Run.
  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (activeAttachmentPreview?.sessionId === currentSessionIdRef.current) {
        e.preventDefault();
        closeAttachmentPreview();
        setInspectorOpen(false);
        return;
      }
      if (openPopoverRef.current) {
        e.preventDefault();
        setOpenPopover(null);
        return;
      }
      if (isStreaming && !showSlashMenuRef.current) {
        e.preventDefault();
        stopStreaming();
      }
    };
    window.addEventListener("keydown", handleGlobalKeyDown);
    return () => window.removeEventListener("keydown", handleGlobalKeyDown);
  }, [activeAttachmentPreview, closeAttachmentPreview, isStreaming, setInspectorOpen, stopStreaming]);

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
    <>
    <div className="relative z-30 px-3 pb-4 pt-2 sm:px-6">
      <div className="glass-input relative mx-auto flex w-full max-w-[900px] flex-col gap-2 rounded-3xl px-3 py-3 transition-shadow hover:shadow-lg sm:px-4">
        <SlashCommandMenu
          visible={showSlashMenu}
          filteredSkills={filteredSkills}
          selectedIndex={selectedMenuIndex}
          onSelect={handleSlashSelect}
          onClose={() => setShowSlashMenu(false)}
        />
        {(attachments.length > 0 || detectedImagePaths.length > 0) && (
          <div className="flex flex-wrap gap-1.5 px-1">
            {attachments.map((item, index) => (
              <AttachmentChip
                key={`${item.id || item.name || "attachment"}-${index}`}
                item={item}
                onRemove={() => setAttachments((current) => current.filter((_, i) => i !== index))}
              />
            ))}
            {detectedImagePaths.map((path) => (
              <span
                key={path}
                className="inline-flex max-w-[260px] items-center gap-1.5 rounded-full border border-emerald-500/10 bg-emerald-50 px-2.5 py-1 text-[11px] text-emerald-700"
                title="后端会识别这个本地图片路径并传给多模态模型"
              >
                <ImagePlus className="h-3 w-3 shrink-0" />
                <span className="truncate">本地图片：{path}</span>
              </span>
            ))}
          </div>
        )}
        {(isUploading || inputError) && (
          <div className="px-1 text-[11px]" aria-live="polite">
            {isUploading && <span className="text-[#002fa7]">正在上传附件，请稍候…</span>}
            {inputError && <span role="alert" className="text-rose-600">{inputError}</span>}
          </div>
        )}
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
              setOpenPopover(null);
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
          onPaste={handlePaste}
          onCompositionStart={() => { isComposingRef.current = true; }}
          onCompositionEnd={() => { isComposingRef.current = false; }}
          placeholder="输入消息，或用 / 调用扩展能力"
          rows={1}
          className="max-h-40 min-h-12 w-full resize-none bg-transparent px-1 py-1 text-[14px] leading-relaxed outline-none placeholder:text-gray-400"
        />

        {runtimeMode === "agent" && (
          <input
            ref={attachmentInputRef}
            type="file"
            accept="image/*,.pdf,.md,.markdown,.txt,.csv,.tsv,.xls,.xlsx,.doc,.docx,.ppt,.pptx,.json,.yaml,.yml"
            multiple
            className="hidden"
            onChange={(event) => handleAttachmentFiles(event.target.files, "upload")}
          />
        )}

        <div
          ref={controlsMenuRef}
          className="relative flex flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3"
        >
          <div className={`flex w-full min-w-0 flex-wrap items-center gap-1.5 sm:w-auto sm:flex-1 sm:gap-2 ${configurationBusy ? "pointer-events-none opacity-60" : ""}`}>
            {runtimeMode === "agent" && (
              <div>
                <button
                  type="button"
                  onClick={() => togglePopover("plus")}
                  aria-expanded={openPopover === "plus" || openPopover === "plus-model"}
                  aria-haspopup="menu"
                  aria-label="添加附件、分析模型或目标"
                  className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full border transition-all ${
                    openPopover === "plus" || openPopover === "plus-model"
                      ? "border-[#002fa7]/15 bg-[#e8edff] text-[#002fa7]"
                      : "border-black/[0.06] bg-white/50 text-gray-600 hover:bg-white/80 hover:text-gray-950"
                  }`}
                >
                  <Plus className="h-4 w-4" />
                </button>

                {(openPopover === "plus" || openPopover === "plus-model") && (
                  <div
                    role="menu"
                    className="absolute bottom-full left-0 z-50 mb-2 w-full max-w-[22rem] rounded-2xl border border-black/[0.10] bg-white p-2 shadow-2xl shadow-slate-900/15 animate-fade-in-scale"
                  >
                    {openPopover === "plus-model" ? (
                      <>
                        <button
                          type="button"
                          onClick={() => setOpenPopover("plus")}
                          className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-[13px] font-medium text-gray-700 hover:bg-black/[0.04]"
                        >
                          <ArrowLeft className="h-4 w-4" />
                          选择分析模型
                        </button>
                        <div className="my-1 h-px bg-black/[0.06]" />
                        <div className="max-h-60 overflow-y-auto py-1">
                          <button
                            type="button"
                            role="menuitemradio"
                            aria-checked={!analyticsModelId}
                            onClick={() => {
                              setAnalyticsModelId(null);
                              setOpenPopover(null);
                            }}
                            className={`flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left transition-colors ${
                              !analyticsModelId
                                ? "bg-[#002fa7]/[0.07] text-[#002fa7]"
                                : "text-gray-700 hover:bg-black/[0.04]"
                            }`}
                          >
                            <Layers3 className="mt-0.5 h-4 w-4 shrink-0" />
                            <span className="min-w-0 flex-1">
                              <span className="block text-[13px] font-medium">不使用分析模型</span>
                              <span className="block text-[11px] text-gray-400">按通用 Agent 上下文执行</span>
                            </span>
                            {!analyticsModelId && <Check className="mt-0.5 h-4 w-4" />}
                          </button>
                          {analyticsModels.map((model) => (
                            <button
                              type="button"
                              role="menuitemradio"
                              aria-checked={analyticsModelId === model.id}
                              key={model.id}
                              onClick={() => {
                                setAnalyticsModelId(model.id);
                                setOpenPopover(null);
                              }}
                              className={`flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left transition-colors ${
                                analyticsModelId === model.id
                                  ? "bg-[#002fa7]/[0.07] text-[#002fa7]"
                                  : "text-gray-700 hover:bg-black/[0.04]"
                              }`}
                            >
                              <Layers3 className="mt-0.5 h-4 w-4 shrink-0" />
                              <span className="min-w-0 flex-1">
                                <span className="block truncate text-[13px] font-medium">{model.name}</span>
                                <span className="block truncate text-[11px] text-gray-400">{model.id}</span>
                              </span>
                              {analyticsModelId === model.id && <Check className="mt-0.5 h-4 w-4" />}
                            </button>
                          ))}
                          {analyticsModels.length === 0 && (
                            <p className="px-3 py-3 text-[12px] text-gray-400">
                              还没有分析模型，可在智能问数工作台创建或导入。
                            </p>
                          )}
                        </div>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          role="menuitem"
                          onClick={() => {
                            setOpenPopover(null);
                            attachmentInputRef.current?.click();
                          }}
                          className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[13px] text-gray-700 hover:bg-black/[0.04]"
                        >
                          <Paperclip className="h-4 w-4" />
                          <span className="flex-1">添加文件和图片</span>
                        </button>
                        <button
                          type="button"
                          role="menuitem"
                          onClick={() => setOpenPopover("plus-model")}
                          className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[13px] text-gray-700 hover:bg-black/[0.04]"
                        >
                          <Layers3 className="h-4 w-4" />
                          <span className="min-w-0 flex-1">
                            <span className="block">分析模型</span>
                            <span className="block truncate text-[11px] text-gray-400">
                              {selectedAnalyticsModel?.name || "未选择"}
                            </span>
                          </span>
                          <ChevronDown className="h-4 w-4 -rotate-90" />
                        </button>
                        <button
                          type="button"
                          role="menuitemcheckbox"
                          aria-checked={Boolean(activeGoal || goalModeEnabled)}
                          onClick={() => {
                            setOpenPopover(null);
                            if (activeGoal) {
                              setInspectorOpen(true);
                              setInspectorActiveTab("goal");
                            } else {
                              setGoalModeEnabled(!goalModeEnabled);
                            }
                          }}
                          className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[13px] hover:bg-black/[0.04] ${
                            activeGoal || goalModeEnabled ? "text-emerald-700" : "text-gray-700"
                          }`}
                        >
                          <Target className="h-4 w-4" />
                          <span className="min-w-0 flex-1">
                            <span className="block">目标</span>
                            <span className="block text-[11px] text-gray-400">
                              {activeGoal
                                ? activeGoal.status === "completed"
                                  ? "目标已完成，点击复盘"
                                  : activeGoal.status === "budget_exceeded"
                                  ? "预算已耗尽，点击追加轮次"
                                  : activeGoal.status === "paused"
                                    ? "目标已暂停，点击查看"
                                    : "目标进行中，点击查看"
                                : goalModeEnabled
                                  ? "下次发送将创建跨 Run Goal"
                                  : "默认关闭；仅对下次发送生效"}
                            </span>
                          </span>
                          {(activeGoal || goalModeEnabled) && <Check className="h-4 w-4" />}
                        </button>
                      </>
                    )}
                  </div>
                )}
              </div>
            )}

            {runtimeMode === "agent" && (
              <div className="min-w-0">
                <button
                  type="button"
                  onClick={() => togglePopover("project")}
                  aria-expanded={openPopover === "project"}
                  aria-haspopup="menu"
                  className={`flex h-8 max-w-[13rem] items-center gap-1.5 rounded-full border px-3 text-[12px] transition-all sm:max-w-[16rem] ${
                    selectedProject
                      ? "border-[#002fa7]/15 bg-[#e8edff] text-[#002fa7] hover:bg-[#dfe7ff]"
                      : "border-black/[0.06] bg-white/42 text-gray-600 hover:bg-white/70 hover:text-gray-900"
                  }`}
                  title={selectedProject?.path || "选择 Agent 工作项目"}
                >
                  <FolderKanban className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">{selectedProject ? selectedProject.name : "项目目录"}</span>
                  <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                </button>
                {openPopover === "project" && (
                  <div role="menu" className="absolute bottom-full left-0 z-50 mb-2 w-full max-w-[22rem] rounded-2xl border border-black/[0.10] bg-white p-2 shadow-2xl shadow-slate-900/15 animate-fade-in-scale">
                    <div className="px-3 pb-2 pt-1">
                      <p className="text-[11px] font-semibold text-gray-500">Agent 工作项目</p>
                      <p className="mt-0.5 text-[11px] leading-relaxed text-gray-400">项目目录是工具读写边界，也会挂载到 Docker 的 /workspace。</p>
                    </div>
                    <div className="max-h-52 overflow-y-auto py-1">
                      {projects.map((project) => (
                        <button
                          type="button"
                          role="menuitemradio"
                          aria-checked={currentProjectId === project.project_id}
                          key={project.project_id}
                          onClick={() => {
                            setRuntimeMode("agent");
                            setCurrentProjectId(project.project_id);
                            setSessionId("default");
                            setOpenPopover(null);
                          }}
                          className={`flex w-full items-start gap-2 rounded-xl px-3 py-2 text-left transition-colors ${
                            currentProjectId === project.project_id
                              ? "bg-[#002fa7]/[0.07] text-[#002fa7]"
                              : "text-gray-700 hover:bg-black/[0.04]"
                          }`}
                        >
                          <FolderKanban className="mt-0.5 h-4 w-4 shrink-0" />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-[13px] font-medium">{project.name}</span>
                            <span className="block truncate text-[11px] text-gray-400">{project.path}</span>
                          </span>
                          {currentProjectId === project.project_id && <Check className="mt-0.5 h-4 w-4" />}
                        </button>
                      ))}
                      {projects.length === 0 && <p className="px-3 py-3 text-[12px] text-gray-400">还没有项目，先登记一个本地文件夹。</p>}
                    </div>
                    <div className="my-1 h-px bg-black/[0.06]" />
                    <button type="button" onClick={handleRegisterProject} className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-[13px] text-gray-700 hover:bg-black/[0.04]">
                      <FolderPlus className="h-4 w-4" />使用现有文件夹…
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setCurrentProjectId(null);
                        setSessionId("default");
                        setOpenPopover(null);
                      }}
                      className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-[13px] text-gray-500 hover:bg-black/[0.04]"
                    >
                      <XCircle className="h-4 w-4" />不使用项目
                    </button>
                  </div>
                )}
              </div>
            )}

            {runtimeMode === "agent" && (
              <div>
                <button
                  type="button"
                  onClick={() => togglePopover("approval")}
                  aria-expanded={openPopover === "approval"}
                  aria-haspopup="menu"
                  className={`flex h-8 items-center gap-1.5 rounded-full border px-3 text-[12px] transition-all ${
                    approvalMode === "smart"
                      ? "border-emerald-600/15 bg-emerald-50 text-emerald-700"
                      : "border-black/[0.06] bg-white/42 text-gray-600 hover:bg-white/70"
                  }`}
                  title="选择本 Session 的授权模式"
                >
                  <ShieldCheck className="h-3.5 w-3.5" />
                  <span>{approvalMode === "smart" ? "智能审批" : "严格审批"}</span>
                  <ChevronDown className="h-3.5 w-3.5" />
                </button>
                {openPopover === "approval" && (
                  <div role="menu" className="absolute bottom-full left-0 z-50 mb-2 w-full max-w-[23rem] rounded-2xl border border-black/[0.10] bg-white p-2 shadow-2xl shadow-slate-900/15 animate-fade-in-scale">
                    <div className="px-3 pb-2 pt-1">
                      <p className="text-[12px] font-semibold text-gray-700">授权模式</p>
                      <p className="mt-1 text-[11px] leading-relaxed text-gray-400">模式属于当前 Session，并在 Run 开始时冻结；Run 进行中不可切换。</p>
                    </div>
                    {([
                      ["strict", "严格审批", "所有需要授权的操作都由你确认。"],
                      ["smart", "智能审批", "受控网页读取与搜索自动联网；Shell、CLI、上传和依赖安装仍按影响授权。"],
                    ] as Array<[ApprovalMode, string, string]>).map(([mode, label, description]) => (
                      <button
                        type="button"
                        role="menuitemradio"
                        aria-checked={approvalMode === mode}
                        disabled={approvalLocked}
                        key={mode}
                        onClick={async () => {
                          if (await setApprovalMode(mode)) setOpenPopover(null);
                        }}
                        className={`flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                          approvalMode === mode ? "bg-[#002fa7]/[0.07] text-[#002fa7]" : "text-gray-700 hover:bg-black/[0.04]"
                        }`}
                      >
                        <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${approvalMode === mode ? "border-[#002fa7]" : "border-gray-300"}`}>
                          {approvalMode === mode && <span className="h-2 w-2 rounded-full bg-[#002fa7]" />}
                        </span>
                        <span className="min-w-0">
                          <span className="block text-[13px] font-medium">{label}</span>
                          <span className="mt-0.5 block text-[11px] leading-relaxed text-gray-400">{description}</span>
                        </span>
                      </button>
                    ))}
                    {approvalModeSaving && <p className="px-3 py-2 text-[11px] text-[#002fa7]">正在保存授权模式…</p>}
                    {approvalModeError && <p role="alert" className="px-3 py-2 text-[11px] text-rose-600">{approvalModeError}</p>}
                  </div>
                )}
              </div>
            )}

            {runtimeMode === "agent"
              && ((activeGoal && activeGoal.status !== "completed") || (!activeGoal && goalModeEnabled))
              && (
              <div className="flex h-8 items-center rounded-full border border-emerald-600/15 bg-emerald-50 text-[12px] text-emerald-700 transition-all hover:bg-emerald-100">
                <button
                  type="button"
                  onClick={() => {
                    if (activeGoal) {
                      setInspectorOpen(true);
                      setInspectorActiveTab("goal");
                    }
                  }}
                  className="flex h-full items-center gap-1.5 rounded-l-full pl-3 pr-1.5"
                  title={activeGoal ? "查看当前目标" : "已为下次发送启用目标"}
                >
                  <Target className="h-3.5 w-3.5" />
                  <span>目标</span>
                </button>
                <button
                  type="button"
                  disabled={goalCancelPending}
                  onClick={() => {
                    if (!activeGoal) {
                      setGoalModeEnabled(false);
                      return;
                    }
                    setGoalCancelConfirmationOpen(true);
                  }}
                  className="mr-1 flex h-6 w-6 items-center justify-center rounded-full text-emerald-700/70 hover:bg-emerald-200/70 hover:text-emerald-900 disabled:cursor-wait disabled:opacity-40"
                  title={activeGoal ? "结束当前 Goal" : "关闭目标模式"}
                  aria-label={activeGoal ? "结束当前 Goal" : "关闭目标模式"}
                >
                  {goalCancelPending
                    ? <Activity className="h-3.5 w-3.5 animate-spin" />
                    : <X className="h-3.5 w-3.5" />}
                </button>
              </div>
            )}
          </div>

          <div
            className="flex w-full shrink-0 items-center justify-end gap-1.5 sm:ml-auto sm:w-auto sm:gap-2"
            onPointerDown={() => setOpenPopover(null)}
          >
            <button
              type="button"
              onClick={handleToggleThinking}
              aria-pressed={thinkingMode}
              title={thinkingMode ? "思考模式已开启" : "思考模式已关闭"}
              className={`flex h-8 items-center gap-1.5 rounded-full border px-2.5 text-[12px] transition-all sm:px-3 ${
                thinkingMode
                  ? "border-[#002fa7]/15 bg-[#e8edff] text-[#002fa7] hover:bg-[#dfe7ff]"
                  : "border-black/[0.06] bg-white/42 text-gray-600 hover:bg-white/70 hover:text-gray-900"
              }`}
            >
              <Brain className="h-3.5 w-3.5 shrink-0" />
              <span className="hidden sm:inline">思考</span>
            </button>
            <ContextUsageTooltip usage={contextUsage} />
            {isStreaming ? (
              <button onClick={stopStreaming} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-red-500 text-white transition-all hover:bg-red-600 active:scale-95" title="停止生成 (Esc)" aria-label="停止生成">
                <Square className="h-3.5 w-3.5 fill-current" />
              </button>
            ) : (
              <button onClick={handleSubmit} disabled={(!text.trim() && attachments.length === 0) || disabled} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#002fa7] text-white transition-all hover:bg-[#001f7a] active:scale-95 disabled:bg-gray-300 disabled:opacity-80" aria-label="发送消息">
                <ArrowUp className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>
      </div>

      <p className="viewport-center-axis mt-1 text-center text-[10px] text-gray-400/45">
        Powered by DeepSeek · PuddingClaw v0.1
      </p>
    </div>
    <ConfirmDialog
      open={goalCancelConfirmationOpen}
      title="结束当前 Goal？"
      description={
        activeGoal?.current_run_id
          ? "当前 Goal 和正在执行的 Run 都将停止，已完成的进度和产物记录仍会保留。"
          : "当前 Goal 将停止，已完成的进度和产物记录仍会保留。"
      }
      confirmLabel="结束 Goal"
      busy={goalCancelPending}
      onClose={() => setGoalCancelConfirmationOpen(false)}
      onConfirm={() => {
        setGoalCancelPending(true);
        setInputError(null);
        void cancelActiveGoal()
          .catch((error) => {
            setInputError(error instanceof Error ? error.message : "Goal 取消失败");
          })
          .finally(() => {
            setGoalCancelPending(false);
            setGoalCancelConfirmationOpen(false);
          });
      }}
    />
    {projectFolderDialog}
    </>
  );
}

function AttachmentChip({ item, onRemove }: { item: AgentAttachment; onRemove: () => void }) {
  const kind = item.type || "file";
  const style = attachmentStyles[kind] || attachmentStyles.file;
  const Icon = style.Icon;
  const size = formatFileSize(item.size);

  return (
    <span
      className={`inline-flex max-w-[260px] items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] ${style.cls}`}
      title={`${style.label}${item.mime_type ? ` · ${item.mime_type}` : ""}${size ? ` · ${size}` : ""}`}
    >
      <Icon className="h-3 w-3 shrink-0" />
      <span className="shrink-0 rounded-full bg-white/60 px-1.5 py-0.5 text-[9px] font-semibold uppercase leading-none">
        {style.label}
      </span>
      <span className="truncate">{item.name || item.id || "attachment"}</span>
      {size && <span className="shrink-0 text-[10px] opacity-65">{size}</span>}
      <button
        type="button"
        onClick={onRemove}
        className="rounded-full p-0.5 hover:bg-black/10"
        aria-label="移除附件"
      >
        <X className="h-3 w-3" />
      </button>
    </span>
  );
}

function ContextUsageTooltip({
  usage,
}: {
  usage: { used: number; total: number; percentage: number };
}) {
  const [open, setOpen] = useState(false);
  const formattedPercentage = formatContextPercentage(usage.percentage, usage.used);
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
        {formattedPercentage}
      </button>
      {open && (
        <div className="absolute bottom-full right-0 mb-2 w-56 rounded-xl bg-[#1f2937] px-3.5 py-2.5 text-[12px] text-white shadow-xl animate-fade-in-scale z-50">
          <p className="font-medium text-gray-200">背景信息窗口</p>
          <p className="mt-1 text-[16px] font-semibold">
            {formattedPercentage} 已用
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
