"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Terminal,
  Code,
  Globe,
  FileText,
  Search,
  Loader2,
  CheckCircle2,
  XCircle,
  Pencil,
  Lightbulb,
  FolderOpen,
  KeyRound,
  Play,
  Wrench,
} from "lucide-react";
import type { TimelineItem, ToolCall } from "@/lib/store";

interface Props {
  timeline: TimelineItem[];
  isStreaming?: boolean;
}

const COMMAND_TOOLS = new Set(["bash", "python_repl", "python", "shell", "exec"]);

const TOOL_META: Record<string, { icon: React.ElementType; color: string; bg: string }> = {
  terminal: { icon: Terminal, color: "#6b7280", bg: "#f3f4f6" },
  bash: { icon: Terminal, color: "#374151", bg: "#f3f4f6" },
  python_repl: { icon: Code, color: "#2563eb", bg: "#eff6ff" },
  python: { icon: Code, color: "#2563eb", bg: "#eff6ff" },
  fetch_url: { icon: Globe, color: "#059669", bg: "#ecfdf5" },
  read_file: { icon: FileText, color: "#d97706", bg: "#fffbeb" },
  read_external_file: { icon: KeyRound, color: "#e11d48", bg: "#fff1f2" },
  llamaindex_knowledge_query: { icon: Search, color: "#7c3aed", bg: "#f5f3ff" },
  write_file: { icon: Pencil, color: "#0891b2", bg: "#ecfeff" },
  edit_file: { icon: Pencil, color: "#0891b2", bg: "#ecfeff" },
  glob: { icon: FolderOpen, color: "#ea580c", bg: "#fff7ed" },
  execute_skill: { icon: Play, color: "#16a34a", bg: "#f0fdf4" },
};

function getToolMeta(tool: string) {
  return TOOL_META[tool] || { icon: Wrench, color: "#6b7280", bg: "#f3f4f6" };
}

function getToolLabel(toolCall: ToolCall): string {
  const tool = toolCall.tool;
  const input = toolCall.input || "";
  try {
    const parsed = JSON.parse(input);
    if (tool === "read_file" && parsed.path) {
      return `阅读 ${parsed.path.split("/").pop() || parsed.path}`;
    }
    if (tool === "read_external_file" && parsed.path) {
      return `读取外部文件 ${parsed.path.split("/").pop() || parsed.path}`;
    }
    if (tool === "write_file" && parsed.path) {
      return `写入文件 ${parsed.path.split("/").pop() || parsed.path}`;
    }
    if (tool === "edit_file" && parsed.path) {
      return `编辑文件 ${parsed.path.split("/").pop() || parsed.path}`;
    }
    if (tool === "glob" && parsed.pattern) {
      return `查找 ${parsed.pattern}`;
    }
    if ((tool === "bash" || tool === "python_repl" || tool === "python") && parsed.command) {
      const cmd = parsed.command as string;
      return `运行 ${cmd.length > 60 ? cmd.slice(0, 60) + "..." : cmd}`;
    }
    if (tool === "execute_skill" && parsed.skill_name) {
      return `执行技能 ${parsed.skill_name}`;
    }
    if (tool === "fetch_url" && parsed.url) {
      return `访问 ${parsed.url}`;
    }
  } catch {
    // fall through
  }
  return tool;
}

function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return "<1s";
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m ${seconds}s`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${remainingMinutes}m`;
}

function toolDurationMs(toolCall: ToolCall, now: number): number | null {
  if (!toolCall.startedAt) return null;
  const end = toolCall.endedAt || (toolCall.status === "running" ? now : undefined);
  if (!end) return null;
  return Math.max(0, end - toolCall.startedAt);
}

export default function ThoughtChain({ timeline, isStreaming = false }: Props) {
  const [isExpanded, setIsExpanded] = useState(isStreaming);
  const wasStreamingRef = useRef(isStreaming);
  const [expandedTools, setExpandedTools] = useState<Record<string, boolean>>({});
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (isStreaming && !wasStreamingRef.current) {
      setIsExpanded(true);
    } else if (!isStreaming && wasStreamingRef.current) {
      setIsExpanded(false);
    }
    wasStreamingRef.current = isStreaming;
  }, [isStreaming]);

  useEffect(() => {
    const hasRunningTool = timeline.some(
      (item) => item.type === "tool" && item.toolCall?.status === "running"
    );
    if (!hasRunningTool) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [timeline]);

  const toolItems = timeline.filter((item) => item.type === "tool" && item.toolCall);
  const toolCount = toolItems.length;
  const commandCount = toolItems.filter(
    (item) => item.type === "tool" && COMMAND_TOOLS.has(item.toolCall?.tool || "")
  ).length;

  const hasRunningTool = toolItems.some(
    (item) => item.type === "tool" && item.toolCall?.status === "running"
  );
  const hasRunningVerification = timeline.some(
    (item) => item.type === "activity"
      && item.status === "running"
      && item.label === "正在核对完成质量"
  );
  const hasCompletedVerification = timeline.some(
    (item) => item.type === "activity"
      && (item.status === "satisfied" || item.status === "passed")
      && item.label === "完成质量检查通过"
  );

  const runningDurations = toolItems
    .map((item) => {
      if (item.type !== "tool" || item.toolCall.status !== "running") return null;
      return toolDurationMs(item.toolCall, now);
    })
    .filter((duration): duration is number => duration !== null);
  const runningElapsed = runningDurations.length ? formatDuration(Math.max(...runningDurations)) : "";
  const runningSuffix = hasRunningTool && isStreaming
    ? ` · 运行中${runningElapsed ? ` ${runningElapsed}` : "..."}`
    : "";
  const summaryText = hasRunningVerification
    ? "验收中"
    : hasCompletedVerification
      ? "验收完成"
    : toolCount > 0
      ? `使用了 ${toolCount} 个工具，运行 ${commandCount} 个命令${runningSuffix}`
      : `处理过程${runningSuffix}`;

  const toggleTool = (id: string) => {
    setExpandedTools((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  if (timeline.length === 0) return null;

  return (
    <div className="mb-3">
      <div className="sticky top-0 z-20 -mx-1 bg-white/95 px-1 py-1 backdrop-blur-sm">
        <button
          type="button"
          onClick={() => setIsExpanded((v) => !v)}
          className="inline-flex items-center gap-2 rounded-md px-1 py-0.5 text-[13px] text-gray-600 transition-colors hover:bg-gray-50 hover:text-gray-900"
        >
          {isExpanded ? (
            <ChevronDown className="h-4 w-4 text-gray-400" />
          ) : (
            <ChevronRight className="h-4 w-4 text-gray-400" />
          )}
          <span>{summaryText}</span>
        </button>
      </div>

      {isExpanded && (
        <div className="relative mt-3 pl-3">
          {/* Vertical dashed line */}
          <div className="absolute left-[21px] top-2 bottom-2 border-l border-dashed border-gray-200" />

          <div className="space-y-4">
            {timeline.map((item, idx) => {
              if (item.type === "reasoning") {
                const isLast = idx === timeline.length - 1;
                const isThinking = isLast && isStreaming;
                return (
                  <div key={item.id} className="relative flex items-start gap-3">
                    <div className="relative z-10 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-amber-50 text-amber-500">
                      <Lightbulb className="h-3 w-3" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 pt-0.5 text-[12px] text-gray-500">
                        {isThinking ? (
                          <>
                            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" />
                            <span>正在思考...</span>
                          </>
                        ) : (
                          <span>思考已完成</span>
                        )}
                      </div>
                      <pre className="mt-1 max-h-40 max-w-full overflow-y-auto whitespace-pre-wrap rounded-lg bg-white/58 p-2 text-[11px] leading-relaxed text-slate-500">
                        {item.content}
                      </pre>
                    </div>
                  </div>
                );
              }

              if (item.type === "activity") {
                const passed = ["satisfied", "passed", "completed", "done"].includes(item.status || "");
                const failed = ["failed", "timed_out", "cancelled", "error"].includes(item.status || "");
                const ActivityIcon = passed ? CheckCircle2 : failed ? XCircle : Loader2;
                return (
                  <div key={item.id} className="relative flex items-start gap-3">
                    <div className={`relative z-10 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${passed ? "bg-emerald-50 text-emerald-600" : failed ? "bg-rose-50 text-rose-600" : "bg-blue-50 text-[#002fa7]"}`}>
                      <ActivityIcon className={`h-3 w-3 ${item.status === "running" ? "animate-spin" : ""}`} />
                    </div>
                    <div className="pt-0.5 text-[12px] text-gray-500">{item.label}</div>
                  </div>
                );
              }

              const tc = item.toolCall;
              if (!tc) return null;
              const meta = getToolMeta(tc.tool);
              const Icon = meta.icon;
              const isOpen = expandedTools[item.id] ?? false;
              const isRunning = tc.status === "running";
              const duration = toolDurationMs(tc, now);
              const durationText = duration === null ? "" : formatDuration(duration);

              return (
                <div key={item.id} className="relative flex items-start gap-3">
                  <div
                    className="relative z-10 flex h-5 w-5 shrink-0 items-center justify-center rounded-full"
                    style={{ background: meta.bg, color: meta.color }}
                  >
                    <Icon className="h-3 w-3" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <button
                      type="button"
                      onClick={() => toggleTool(item.id)}
                      className="flex w-full items-center gap-2 text-left"
                    >
                      <span className="text-[13px] text-gray-700">{getToolLabel(tc)}</span>
                      {durationText && (
                        <span className="rounded-full bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                          {isRunning ? durationText : `耗时 ${durationText}`}
                        </span>
                      )}
                      <span className="shrink-0">
                        {isRunning ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin text-amber-500" />
                        ) : tc.is_error ? (
                          <XCircle className="h-3.5 w-3.5 text-red-500" />
                        ) : (
                          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                        )}
                      </span>
                      {isOpen ? (
                        <ChevronDown className="ml-auto h-3.5 w-3.5 shrink-0 text-gray-400" />
                      ) : (
                        <ChevronRight className="ml-auto h-3.5 w-3.5 shrink-0 text-gray-400" />
                      )}
                    </button>

                    {isOpen && (
                      <div className="mt-2 space-y-2 pr-2">
                        {tc.input && (
                          <div>
                            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">
                              Input
                            </span>
                            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded-lg bg-white/58 p-2 font-mono text-[11px] leading-relaxed text-gray-600">
                              {tc.input}
                            </pre>
                          </div>
                        )}
                        {tc.output && (
                          <div>
                            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">
                              Output
                            </span>
                            <pre
                              className={`mt-1 max-h-36 overflow-y-auto overflow-x-auto whitespace-pre-wrap rounded-lg p-2 font-mono text-[11px] leading-relaxed ${
                                tc.is_error
                                  ? "bg-red-50 text-red-700"
                                  : "bg-white/58 text-gray-600"
                              }`}
                            >
                              {tc.output}
                            </pre>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
