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
  PackageCheck,
  ShieldCheck,
  Ban,
} from "lucide-react";
import {
  cancelSkillPlan,
  commitSkillPlan,
  getSkillPlan,
  type SkillPlan,
} from "@/lib/api";
import type { TimelineItem, ToolCall } from "@/lib/store";
import { skillPlanGroupsFromToolCall } from "@/lib/skillPlanProjection";
import { getSubagentToolLabel } from "@/lib/subagentActivity";

interface Props {
  timeline: TimelineItem[];
  isStreaming?: boolean;
}

const COMMAND_TOOLS = new Set([
  "execute",
  "terminal",
  "bash",
  "python_repl",
  "python",
  "shell",
  "exec",
]);

const TOOL_META: Record<string, { icon: React.ElementType; color: string; bg: string }> = {
  execute: { icon: Terminal, color: "#6b7280", bg: "#f3f4f6" },
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
  if (tool === "task" || tool.includes("subagent")) {
    return getSubagentToolLabel(toolCall.status, Boolean(toolCall.is_error));
  }
  if (tool === "load_skill_context") {
    return "加载 Skill 上下文";
  }
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
    if (COMMAND_TOOLS.has(tool) && parsed.command) {
      return "运行命令";
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
                      <ReasoningScrollBlock content={item.content} isThinking={isThinking} />
                    </div>
                  </div>
                );
              }

              if (item.type === "activity") {
                const historicalVerificationContinuation =
                  !isStreaming
                  && item.status === "running"
                  && item.id.startsWith("verification-");
                const passed = historicalVerificationContinuation
                  || ["satisfied", "passed", "completed", "done"].includes(item.status || "");
                const failed = [
                  "failed",
                  "timed_out",
                  "cancelled",
                  "error",
                  "infrastructure_error",
                  "verification_failed",
                ].includes(item.status || "");
                const ActivityIcon = passed ? CheckCircle2 : failed ? XCircle : Loader2;
                const activityLabel = historicalVerificationContinuation
                  ? "已进入后续修复轮次"
                  : item.label;
                const activityDetail = historicalVerificationContinuation
                  ? "该阶段已结束；以后续验收结果为准。"
                  : item.detail;
                return (
                  <div key={item.id} className="relative flex items-start gap-3">
                    <div className={`relative z-10 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${passed ? "bg-emerald-50 text-emerald-600" : failed ? "bg-rose-50 text-rose-600" : "bg-blue-50 text-[#002fa7]"}`}>
                      <ActivityIcon className={`h-3 w-3 ${item.status === "running" && isStreaming ? "animate-spin" : ""}`} />
                    </div>
                    <div className="min-w-0 flex-1 pt-0.5">
                      <div className="text-[12px] font-medium text-gray-600">{activityLabel}</div>
                      {activityDetail ? (
                        <div className="mt-1 max-w-[680px] text-[11px] leading-relaxed text-gray-500">
                          {activityDetail}
                        </div>
                      ) : null}
                    </div>
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

export function SkillPlanCards({
  timeline,
  sessionId,
}: {
  timeline: TimelineItem[];
  sessionId: string;
}) {
  const groups = Array.from(
    new Map(
      timeline
        .flatMap((item) => (
          item.type === "tool" && item.toolCall ? skillPlanGroupsFromToolCall(item.toolCall) : []
        ))
        .map((group) => [group.id, group]),
    ).values(),
  );
  if (groups.length === 0) return null;
  return (
    <div className="mt-3 space-y-3">
      {groups.map((group) => group.plans.length > 1 ? (
        <SkillPlanBatchCard
          key={group.id}
          sessionId={sessionId}
          initialPlans={group.plans}
          source={group.source}
          errorCount={group.errorCount}
        />
      ) : (
        <SkillPlanCard
          key={group.plans[0].plan_id}
          sessionId={sessionId}
          initialPlan={group.plans[0]}
        />
      ))}
    </div>
  );
}

function SkillPlanBatchCard({
  sessionId,
  initialPlans,
  source,
  errorCount = 0,
}: {
  sessionId: string;
  initialPlans: SkillPlan[];
  source?: string;
  errorCount?: number;
}) {
  const [plans, setPlans] = useState(initialPlans);
  const [pendingAction, setPendingAction] = useState<"commit" | "cancel" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const planIds = initialPlans.map((plan) => plan.plan_id).join(",");
  const initialPlansRef = useRef(initialPlans);
  initialPlansRef.current = initialPlans;

  useEffect(() => {
    let active = true;
    void Promise.all(initialPlansRef.current.map((plan) => getSkillPlan(sessionId, plan.plan_id)))
      .then((fresh) => {
        if (active) setPlans(fresh);
      })
      .catch((cause) => {
        if (active) setError(cause instanceof Error ? cause.message : "无法刷新 Skill 批次状态");
      });
    return () => {
      active = false;
    };
  }, [planIds, sessionId]);

  const runBatch = async (action: "commit" | "cancel") => {
    setPendingAction(action);
    setError(null);
    const next = [...plans];
    const failures: string[] = [];
    for (let index = 0; index < next.length; index += 1) {
      const plan = next[index];
      if (plan.status !== "prepared") continue;
      try {
        next[index] = action === "commit"
          ? await commitSkillPlan(sessionId, plan.plan_id, plan.plan_sha256)
          : await cancelSkillPlan(sessionId, plan.plan_id, plan.plan_sha256);
      } catch (cause) {
        failures.push(`${plan.skill_name}: ${cause instanceof Error ? cause.message : "操作失败"}`);
        try {
          next[index] = await getSkillPlan(sessionId, plan.plan_id);
        } catch {
          // Keep the last known plan state and report the original failure.
        }
      }
      setPlans([...next]);
    }
    if (failures.length > 0) setError(failures.join("；"));
    setPendingAction(null);
  };

  const prepared = plans.filter((plan) => plan.status === "prepared").length;
  const committed = plans.filter((plan) => plan.status === "committed").length;
  const cancelled = plans.filter((plan) => plan.status === "cancelled").length;
  const expired = plans.filter((plan) => plan.status === "expired").length;
  const allCommitted = committed === plans.length;
  const allInactive = prepared === 0;
  const title = allCommitted
    ? `${plans.length} 个 Skills 安装完成`
    : allInactive
      ? `${plans.length} 个 Skills 批次已结束`
      : `${plans.length} 个 Skills 待确认`;
  const description = allCommitted
    ? "整批计划已提交，后续 Agent 运行可使用这些 Skills。"
    : allInactive
      ? `已安装 ${committed} 个，已取消 ${cancelled} 个，已过期 ${expired} 个。`
      : `已分别完成来源校验和文件校验；一次确认将提交剩余 ${prepared} 个计划。${errorCount > 0 ? `另有 ${errorCount} 个未能生成计划。` : ""}`;
  const names = plans.map((plan) => plan.skill_name);
  const tone = allCommitted
    ? "border-emerald-200 bg-emerald-50/70"
    : allInactive
      ? "border-slate-200 bg-slate-50"
      : "border-blue-200 bg-blue-50/60";
  const Icon = allCommitted ? ShieldCheck : allInactive ? Ban : PackageCheck;

  return (
    <section className={`rounded-2xl border p-4 ${tone}`} data-skill-plan-batch={planIds}>
      <div className="flex items-start gap-3">
        <div className={`mt-0.5 rounded-xl p-2 ${allCommitted ? "bg-emerald-100 text-emerald-700" : prepared > 0 ? "bg-blue-100 text-[#002fa7]" : "bg-slate-200 text-slate-600"}`}>
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
          <p className="mt-1 text-xs leading-5 text-slate-600">{description}</p>
          <div className="mt-2 text-[11px] text-slate-500">
            来源：<span className="break-all font-mono">{source || plans[0]?.source}</span>
          </div>
          <details className="mt-2 text-[11px] text-slate-500">
            <summary className="cursor-pointer select-none hover:text-slate-700">
              查看 {names.length} 个 Skill 名称
            </summary>
            <div className="mt-2 flex max-h-28 flex-wrap gap-1.5 overflow-y-auto">
              {names.map((name) => (
                <span key={name} className="rounded-full bg-white/80 px-2 py-1 font-mono">{name}</span>
              ))}
            </div>
          </details>
          {error ? <p className="mt-2 text-xs text-rose-600">{error}</p> : null}
          {prepared > 0 ? (
            <div className="mt-3 flex flex-wrap justify-end gap-2">
              <button
                type="button"
                disabled={pendingAction !== null}
                onClick={() => void runBatch("cancel")}
                className="h-9 rounded-xl px-3 text-sm font-semibold text-slate-500 hover:bg-white/70 disabled:opacity-50"
              >
                {pendingAction === "cancel" ? "正在取消…" : "取消整批"}
              </button>
              <button
                type="button"
                disabled={pendingAction !== null}
                onClick={() => void runBatch("commit")}
                className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-sm font-semibold text-white shadow-sm disabled:opacity-50"
              >
                {pendingAction === "commit" ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                {pendingAction === "commit" ? "正在提交整批…" : `确认并安装/更新 ${prepared} 个 Skills`}
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function SkillPlanCard({
  sessionId,
  initialPlan,
}: {
  sessionId: string;
  initialPlan: SkillPlan;
}) {
  const [plan, setPlan] = useState(initialPlan);
  const [pendingAction, setPendingAction] = useState<"commit" | "cancel" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void getSkillPlan(sessionId, initialPlan.plan_id)
      .then((fresh) => {
        if (active) setPlan(fresh);
      })
      .catch((cause) => {
        if (active) setError(cause instanceof Error ? cause.message : "无法刷新 Skill 计划状态");
      });
    return () => {
      active = false;
    };
  }, [initialPlan.plan_id, sessionId]);

  const commit = async () => {
    setPendingAction("commit");
    setError(null);
    try {
      setPlan(await commitSkillPlan(sessionId, plan.plan_id, plan.plan_sha256));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "安装失败");
      try {
        const fresh = await getSkillPlan(sessionId, plan.plan_id);
        setPlan(fresh);
        if (fresh.status !== "prepared") setError(null);
      } catch {
        // Preserve the actionable commit error.
      }
    } finally {
      setPendingAction(null);
    }
  };

  const cancel = async () => {
    setPendingAction("cancel");
    setError(null);
    try {
      setPlan(await cancelSkillPlan(sessionId, plan.plan_id, plan.plan_sha256));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "取消失败");
      try {
        const fresh = await getSkillPlan(sessionId, plan.plan_id);
        setPlan(fresh);
        if (fresh.status !== "prepared") setError(null);
      } catch {
        // Preserve the actionable cancellation error.
      }
    } finally {
      setPendingAction(null);
    }
  };

  const isPrepared = plan.status === "prepared";
  const isCommitted = plan.status === "committed";
  const isCancelled = plan.status === "cancelled";
  const title = isCommitted
    ? `${plan.skill_name} ${plan.action === "install" ? "安装完成" : "更新完成"}`
    : isCancelled
      ? `${plan.skill_name} 已取消`
      : plan.status === "expired"
        ? `${plan.skill_name} 计划已过期`
        : `${plan.skill_name} 待${plan.action === "install" ? "安装" : "更新"}`;
  const description = isCommitted
    ? "已按已确认的不可变计划提交，后续 Agent 运行可使用该 Skill。"
    : isCancelled
      ? "已取消，Skills 目录未被修改。"
      : plan.status === "expired"
        ? "暂存内容已清理，Skills 目录未被修改。请重新发起准备。"
        : "源文件已暂存并通过校验，尚未安装。确认后才会修改 Skills 目录。";
  const Icon = isCommitted ? ShieldCheck : isCancelled || plan.status === "expired" ? Ban : PackageCheck;
  const tone = isCommitted
    ? "border-emerald-200 bg-emerald-50/70"
    : isCancelled || plan.status === "expired"
      ? "border-slate-200 bg-slate-50"
      : "border-blue-200 bg-blue-50/60";

  return (
    <section className={`rounded-2xl border p-4 ${tone}`} data-skill-plan-id={plan.plan_id}>
      <div className="flex items-start gap-3">
        <div className={`mt-0.5 rounded-xl p-2 ${isCommitted ? "bg-emerald-100 text-emerald-700" : isPrepared ? "bg-blue-100 text-[#002fa7]" : "bg-slate-200 text-slate-600"}`}>
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
            {plan.diff?.summary ? (
              <span className="rounded-full border border-black/[0.06] bg-white/80 px-2 py-0.5 font-mono text-[10px] text-slate-500">
                {plan.diff.summary}
              </span>
            ) : null}
          </div>
          <p className="mt-1 text-xs leading-5 text-slate-600">{description}</p>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
            <span>来源：<span className="break-all font-mono">{plan.source}</span></span>
            {plan.ref ? <span>版本：{plan.ref}</span> : null}
          </div>
          {plan.diff && (
            <details className="mt-2 text-[11px] text-slate-500">
              <summary className="cursor-pointer select-none hover:text-slate-700">查看文件变更</summary>
              <div className="mt-2 grid gap-2 sm:grid-cols-3">
                <SkillDiffList label="新增" items={plan.diff.added} />
                <SkillDiffList label="修改" items={plan.diff.changed} />
                <SkillDiffList label="删除" items={plan.diff.removed} />
              </div>
            </details>
          )}
          {error ? <p className="mt-2 text-xs text-rose-600">{error}</p> : null}
          {isPrepared ? (
            <div className="mt-3 flex flex-wrap justify-end gap-2">
              <button
                type="button"
                disabled={pendingAction !== null}
                onClick={() => void cancel()}
                className="h-9 rounded-xl px-3 text-sm font-semibold text-slate-500 hover:bg-white/70 disabled:opacity-50"
              >
                {pendingAction === "cancel" ? "正在取消…" : "取消"}
              </button>
              <button
                type="button"
                disabled={pendingAction !== null}
                onClick={() => void commit()}
                className="inline-flex h-9 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-sm font-semibold text-white shadow-sm disabled:opacity-50"
              >
                {pendingAction === "commit" ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                {pendingAction === "commit" ? "正在提交…" : `确认并${plan.action === "install" ? "安装" : "更新"}`}
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function SkillDiffList({ label, items }: { label: string; items?: string[] }) {
  return (
    <div className="rounded-lg bg-white/70 p-2">
      <div className="font-semibold text-slate-600">{label} {items?.length || 0}</div>
      {items?.length ? (
        <div className="mt-1 max-h-24 space-y-0.5 overflow-y-auto font-mono">
          {items.slice(0, 30).map((item) => <div key={item} className="break-all">{item}</div>)}
          {items.length > 30 ? <div>还有 {items.length - 30} 项…</div> : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Streaming reasoning block: follows the newest content while the model is
 * thinking, but stops following once the user scrolls up to read earlier
 * text (resumes when they scroll back near the bottom).
 */
function ReasoningScrollBlock({
  content,
  isThinking,
}: {
  content: string;
  isThinking: boolean;
}) {
  const preRef = useRef<HTMLPreElement>(null);
  const pinnedToBottomRef = useRef(true);

  useEffect(() => {
    const el = preRef.current;
    if (!el || !isThinking || !pinnedToBottomRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [content, isThinking]);

  return (
    <pre
      ref={preRef}
      onScroll={(event) => {
        const el = event.currentTarget;
        pinnedToBottomRef.current =
          el.scrollHeight - el.scrollTop - el.clientHeight < 32;
      }}
      className="mt-1 max-h-40 max-w-full overflow-y-auto whitespace-pre-wrap rounded-lg bg-white/58 p-2 text-[11px] leading-relaxed text-slate-500"
    >
      {content}
    </pre>
  );
}
