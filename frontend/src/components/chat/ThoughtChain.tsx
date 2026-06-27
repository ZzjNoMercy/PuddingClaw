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
  search_knowledge_base: { icon: Search, color: "#7c3aed", bg: "#f5f3ff" },
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

export default function ThoughtChain({ timeline, isStreaming = false }: Props) {
  const [isExpanded, setIsExpanded] = useState(isStreaming);
  const wasStreamingRef = useRef(isStreaming);
  const [expandedTools, setExpandedTools] = useState<Record<string, boolean>>({});

  useEffect(() => {
    if (isStreaming && !wasStreamingRef.current) {
      setIsExpanded(true);
    } else if (!isStreaming && wasStreamingRef.current) {
      setIsExpanded(false);
    }
    wasStreamingRef.current = isStreaming;
  }, [isStreaming]);

  const toolItems = timeline.filter((item) => item.type === "tool");
  const toolCount = toolItems.length;
  const commandCount = toolItems.filter(
    (item) => item.type === "tool" && COMMAND_TOOLS.has(item.toolCall.tool)
  ).length;

  const hasRunningTool = toolItems.some(
    (item) => item.type === "tool" && item.toolCall.status === "running"
  );

  const runningSuffix = hasRunningTool && isStreaming ? " · 运行中..." : "";
  const summaryText =
    toolCount > 0
      ? `使用了 ${toolCount} 个工具，运行 ${commandCount} 个命令${runningSuffix}`
      : `思考过程${runningSuffix}`;

  const toggleTool = (id: string) => {
    setExpandedTools((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  if (timeline.length === 0) return null;

  return (
    <div className="mb-3">
      <button
        type="button"
        onClick={() => setIsExpanded((v) => !v)}
        className="inline-flex items-center gap-2 text-[13px] text-gray-600 transition-colors hover:text-gray-900"
      >
        {isExpanded ? (
          <ChevronDown className="h-4 w-4 text-gray-400" />
        ) : (
          <ChevronRight className="h-4 w-4 text-gray-400" />
        )}
        <span>{summaryText}</span>
      </button>

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

              const tc = item.toolCall;
              const meta = getToolMeta(tc.tool);
              const Icon = meta.icon;
              const isOpen = expandedTools[item.id] ?? false;
              const isRunning = tc.status === "running";

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
