"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen,
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronDown,
  Circle,
  ExternalLink,
  FileText,
  Globe2,
  KeyRound,
  ListChecks,
  Pause,
  Play,
  ShieldCheck,
  SquareTerminal,
  Target,
  Timer,
  X,
} from "lucide-react";
import {
  listSessionPermissions,
  rawKnowledgeFileUrl,
  revokePermissionGrant,
  type HarnessGoal,
  type HarnessRun,
  type PermissionGrant,
  type RubricEvaluationReport,
} from "@/lib/api";
import { useApp, type SourceRecord, type ToolCall } from "@/lib/store";

type TodoStatus = "completed" | "in_progress" | "pending";

export default function SourcesPanel() {
  const {
    messages,
    isStreaming,
    sessionId,
    todos,
    activeSourceId,
    inspectorActiveTab,
    setInspectorActiveTab,
    activeGoal,
    currentRun,
    verificationReport,
    pauseActiveGoal,
    resumeActiveGoal,
    cancelActiveGoal,
  } = useApp();
  const [permissionGrants, setPermissionGrants] = useState<PermissionGrant[]>([]);

  const loadPermissions = React.useCallback(() => {
    if (!sessionId || sessionId === "default") {
      setPermissionGrants([]);
      return;
    }
    listSessionPermissions(sessionId)
      .then(setPermissionGrants)
      .catch(() => setPermissionGrants([]));
  }, [sessionId]);

  useEffect(() => {
    loadPermissions();
  }, [loadPermissions]);

  useEffect(() => {
    const handler = () => loadPermissions();
    window.addEventListener("puddingclaw:permissions-changed", handler);
    return () => window.removeEventListener("puddingclaw:permissions-changed", handler);
  }, [loadPermissions]);

  // When a citation marker in the chat is clicked, activeSourceId is set and the
  // inspector opens. Make sure the drawer shows the Sources tab so the cited
  // source is visible, instead of staying on Progress.
  useEffect(() => {
    if (activeSourceId) {
      setInspectorActiveTab("sources");
    }
  }, [activeSourceId, setInspectorActiveTab]);

  const { cited, retrieved, inferredTodos } = useMemo(() => {
    const lastUserIndex = messages.findLastIndex((message) => message.role === "user");
    const turnMessages = lastUserIndex >= 0 ? messages.slice(lastUserIndex) : [];
    const sourceMap = new Map<string, SourceRecord>();
    const citationIndex = new Map<string, number>();
    const toolByCallId = new Map<string, string>();
    let latestTodos: Array<{ content: string; status: TodoStatus }> = [];
    for (const message of turnMessages) {
      for (const toolCall of message.toolCalls || []) {
        if (toolCall.id) toolByCallId.set(toolCall.id, toolCall.tool);
        const parsedTodos = extractTodosFromToolCall(toolCall);
        if (parsedTodos.length > 0) latestTodos = parsedTodos;
      }
    }
    for (const message of turnMessages) {
      for (const source of message.sources || []) {
        if (isLegacyFalsePositive(source, toolByCallId)) continue;
        sourceMap.set(source.source_id, { ...sourceMap.get(source.source_id), ...source });
      }
      for (const citation of message.citations || []) {
        if (!citationIndex.has(citation.source_id)) {
          citationIndex.set(citation.source_id, citation.display_index);
        }
      }
    }

    // Fallback: some tools emit sources the model cited with [^source_id] markers
    // but the citations_finalized event did not include them (e.g. adapter timing
    // or source_id mismatch). Treat any marker in the rendered content that points
    // to a known source as a cited source so it does not end up under "其他检索结果".
    const markerRe = /\[\^(src_[A-Za-z0-9_-]+)\]/g;
    for (const message of turnMessages) {
      const contents = [
        message.content,
        ...(message.segments?.map((s) => s.content) || []),
      ];
      for (const content of contents) {
        if (!content) continue;
        let match;
        markerRe.lastIndex = 0;
        while ((match = markerRe.exec(content)) !== null) {
          const sourceId = match[1];
          if (sourceMap.has(sourceId) && !citationIndex.has(sourceId)) {
            const nextIndex =
              citationIndex.size > 0
                ? Math.max(...Array.from(citationIndex.values())) + 1
                : 1;
            citationIndex.set(sourceId, nextIndex);
          }
        }
      }
    }

    const citedSources = Array.from(sourceMap.values())
      .filter((source) => citationIndex.has(source.source_id))
      .sort((a, b) => (citationIndex.get(a.source_id) || 0) - (citationIndex.get(b.source_id) || 0))
      .map((source) => ({ source, index: citationIndex.get(source.source_id) }));
    const retrievedSources = Array.from(sourceMap.values())
      .filter((source) => !citationIndex.has(source.source_id))
      .map((source) => ({ source, index: undefined }));
    return { cited: citedSources, retrieved: retrievedSources, inferredTodos: latestTodos };
  }, [messages]);

  // The default Sources list intentionally stays scoped to the latest turn.
  // A citation in an older message can still be activated, though, so resolve
  // that source from the full session history and show it separately instead
  // of expanding every historical source into the normal list.
  const selectedHistoricalSource = useMemo(() => {
    if (!activeSourceId) return null;
    const isInCurrentTurn = [...cited, ...retrieved].some(
      ({ source }) => source.source_id === activeSourceId
    );
    if (isInCurrentTurn) return null;

    const toolByCallId = new Map<string, string>();
    for (const message of messages) {
      for (const toolCall of message.toolCalls || []) {
        if (toolCall.id) toolByCallId.set(toolCall.id, toolCall.tool);
      }
    }

    let source: SourceRecord | undefined;
    let citationIndex: number | undefined;
    for (const message of messages) {
      for (const candidate of message.sources || []) {
        if (candidate.source_id !== activeSourceId) continue;
        if (isLegacyFalsePositive(candidate, toolByCallId)) continue;
        source = { ...source, ...candidate } as SourceRecord;
      }
      for (const citation of message.citations || []) {
        if (citation.source_id === activeSourceId) {
          citationIndex = citation.display_index;
        }
      }
    }

    return source ? { source, index: citationIndex } : null;
  }, [activeSourceId, cited, messages, retrieved]);

  // Prefer persisted todos; fall back to todos inferred from the current turn
  // when persistence has not been populated yet.
  const displayTodos = useMemo(
    () => (todos && todos.length > 0 ? todos : inferredTodos),
    [todos, inferredTodos]
  );

  const total = cited.length + retrieved.length;
  const hasSources = total > 0;
  const hasTodos = displayTodos.length > 0;
  return (
    <div className="h-full overflow-y-auto px-5 py-5">
      <div className="workspace-side-card overflow-hidden rounded-[28px] px-5 py-3">
        {activeGoal && (
          <>
            <GoalCard
              active={inspectorActiveTab === "goal"}
              onActivate={() => setInspectorActiveTab(inspectorActiveTab === "goal" ? null : "goal")}
              goal={activeGoal}
              run={currentRun}
              onPause={pauseActiveGoal}
              onResume={resumeActiveGoal}
              onCancel={cancelActiveGoal}
            />
            <PanelDivider />
          </>
        )}
        {verificationReport && (
          <>
            <VerificationCard
              active={inspectorActiveTab === "verification"}
              onActivate={() =>
                setInspectorActiveTab(inspectorActiveTab === "verification" ? null : "verification")
              }
              report={verificationReport}
              run={currentRun}
            />
            <PanelDivider />
          </>
        )}
        <ProgressCard
          active={inspectorActiveTab === "progress"}
          onActivate={() => setInspectorActiveTab(inspectorActiveTab === "progress" ? null : "progress")}
          todos={displayTodos as Array<{ content: string; status: TodoStatus }>}
        />
        <PanelDivider />
        <PermissionsCard
          active={inspectorActiveTab === "permissions"}
          onActivate={() => setInspectorActiveTab(inspectorActiveTab === "permissions" ? null : "permissions")}
          grants={permissionGrants}
          onRevoke={async (grantId) => {
            await revokePermissionGrant(sessionId, grantId);
            loadPermissions();
            window.dispatchEvent(new CustomEvent("puddingclaw:permissions-changed"));
          }}
        />
        <PanelDivider />
        <SourcesCard
          active={inspectorActiveTab === "sources"}
          onActivate={() => setInspectorActiveTab(inspectorActiveTab === "sources" ? null : "sources")}
          cited={cited}
          retrieved={retrieved}
          selectedHistoricalSource={selectedHistoricalSource}
          isStreaming={isStreaming && hasSources}
        />
      </div>
    </div>
  );
}

function PanelDivider() {
  return <div className="mx-1 h-px bg-black/[0.06]" />;
}

const goalStatusLabel: Record<HarnessGoal["status"], string> = {
  active: "进行中",
  paused: "已暂停",
  blocked: "受阻",
  achieved: "已完成",
  cancelled: "已取消",
  budget_exceeded: "预算已耗尽",
};

function GoalCard({
  active,
  onActivate,
  goal,
  run,
  onPause,
  onResume,
  onCancel,
}: {
  active: boolean;
  onActivate: () => void;
  goal: HarnessGoal;
  run: HarnessRun | null;
  onPause: () => Promise<void>;
  onResume: () => Promise<void>;
  onCancel: () => Promise<void>;
}) {
  const [actionError, setActionError] = useState("");
  const runIsActive = Boolean(
    run && ![
      "completed",
      "cancelled",
      "failed",
      "blocked",
      "budget_exceeded",
      "verification_failed",
    ].includes(run.status)
  );
  const performAction = async (action: () => Promise<void>) => {
    setActionError("");
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "目标状态更新失败");
    }
  };
  return (
    <section>
      <SectionHeader
        icon={<Target className="h-4 w-4" />}
        title="目标"
        metric={goalStatusLabel[goal.status]}
        open={active}
        onToggle={onActivate}
      />
      {active && (
        <div className="pb-4">
          <p className="text-[13px] leading-6 text-slate-700">{goal.objective}</p>
          <div className="mt-3 grid grid-cols-3 gap-2 text-[11px] text-slate-500">
            <div className="rounded-xl bg-black/[0.035] px-3 py-2">
              Run
              <span className="mt-0.5 block font-semibold text-slate-700">
                {goal.round}/{goal.max_rounds}
              </span>
            </div>
            <div className="rounded-xl bg-black/[0.035] px-3 py-2">
              当前状态
              <span className="mt-0.5 block font-semibold text-slate-700">
                {run?.status || goalStatusLabel[goal.status]}
              </span>
            </div>
            <div className="rounded-xl bg-black/[0.035] px-3 py-2">
              模型调用
              <span className="mt-0.5 block font-semibold text-slate-700">
                {goal.model_call_count || 0}
              </span>
            </div>
          </div>
          {goal.budget_exhaustion_reason && (
            <p className="mt-2 text-[11px] text-amber-700">
              预算原因：{goal.budget_exhaustion_reason}
            </p>
          )}
          {goal.gaps.length > 0 && (
            <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-[11px] font-semibold text-amber-800">
                <AlertTriangle className="h-3.5 w-3.5" />
                尚待补齐
              </div>
              <ul className="mt-1.5 space-y-1 text-[11px] leading-5 text-amber-800/90">
                {goal.gaps.map((gap, index) => <li key={`${gap}-${index}`}>• {gap}</li>)}
              </ul>
            </div>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            {goal.status === "active" && (
              <button
                type="button"
                disabled={runIsActive}
                title={runIsActive ? "请先停止当前 Run" : "暂停目标"}
                onClick={() => void performAction(onPause)}
                className="flex items-center gap-1 rounded-lg border border-black/[0.08] px-2.5 py-1.5 text-[11px] text-slate-600 hover:bg-black/[0.035] disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Pause className="h-3 w-3" />暂停
              </button>
            )}
            {(goal.status === "paused" || goal.status === "blocked") && (
              <button
                type="button"
                onClick={() => void performAction(onResume)}
                className="flex items-center gap-1 rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-[11px] text-emerald-700 hover:bg-emerald-100"
              >
                <Play className="h-3 w-3" />恢复
              </button>
            )}
            {!["achieved", "cancelled", "budget_exceeded"].includes(goal.status) && (
              <button
                type="button"
                disabled={runIsActive}
                title={runIsActive ? "请先停止当前 Run" : "取消目标"}
                onClick={() => void performAction(onCancel)}
                className="flex items-center gap-1 rounded-lg border border-rose-200 px-2.5 py-1.5 text-[11px] text-rose-600 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Ban className="h-3 w-3" />取消目标
              </button>
            )}
          </div>
          {runIsActive && (
            <p className="mt-2 text-[11px] text-slate-400">
              当前 Run 结束或停止后，才能暂停或取消 Goal。
            </p>
          )}
          {actionError && (
            <p className="mt-2 rounded-lg bg-rose-50 px-2.5 py-2 text-[11px] text-rose-700">
              {actionError}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function VerificationCard({
  active,
  onActivate,
  report,
  run,
}: {
  active: boolean;
  onActivate: () => void;
  report: RubricEvaluationReport;
  run: HarnessRun | null;
}) {
  const passed = report.status === "satisfied" || report.status === "not_required";
  const controlError = report.status === "verification_incomplete" || report.status === "grader_error";
  const statusLabel = verificationStatusLabel(report.status);
  const criteriaById = new Map(
    (run?.verification_contract?.criteria || []).map((criterion) => [
      criterion.id,
      criterion,
    ]),
  );
  return (
    <section>
      <SectionHeader
        icon={passed ? <CheckCircle2 className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
        title="验收"
        metric={verificationMetricLabel(report.status)}
        open={active}
        onToggle={onActivate}
      />
      {active && (
        <div className="space-y-3 pb-4">
          <div className="rounded-xl bg-slate-50 px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
              <span>Run {run?.run_id?.slice(-8) || report.run_id.slice(-8)}</span>
              <span className="text-slate-300">·</span>
              <span className={passed ? "font-medium text-emerald-700" : "font-medium text-amber-700"}>
                {statusLabel}
              </span>
              {report.iteration_count > 0 && (
                <>
                  <span className="text-slate-300">·</span>
                  <span>第 {report.iteration_count} 轮验收</span>
                </>
              )}
            </div>
            <p className="mt-1.5 text-[11px] leading-5 text-slate-600">
              {passed
                ? "全部必需验收项均已通过。"
                : controlError
                  ? "验收控制流程没有形成有效终态；这不代表用户任务本身未通过。"
                : `发现 ${Math.max(1, report.gaps.length, report.evaluations.filter((item) => !item.passed).length)} 个待修正问题。`}
            </p>
            {run?.task_profile && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-medium text-slate-500">
                  本轮类型：{taskIntentLabel(run.task_profile.primary_intent)}
                </span>
                {(run.verification_contract?.verification_packs || []).map((pack) => (
                  <span
                    key={pack}
                    className="rounded-full bg-[#002fa7]/[0.06] px-2 py-0.5 text-[10px] font-medium text-[#002fa7]"
                  >
                    {verificationPackLabel(pack)}
                  </span>
                ))}
              </div>
            )}
          </div>
          {!passed && report.gaps.length > 0 && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5">
              <p className="text-[11px] font-semibold text-amber-800">
                {controlError ? "验收流程问题" : "待修正问题"}
              </p>
              <ul className="mt-1.5 space-y-1.5 text-[11px] leading-5 text-amber-900">
                {report.gaps.map((gap, index) => (
                  <li key={`${gap}-${index}`} className="flex gap-1.5">
                    <span className="shrink-0 text-amber-500">{index + 1}.</span>
                    <span className="min-w-0 break-words">{gap}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {report.evaluations.map((evaluation) => {
            const criterion = criteriaById.get(evaluation.criterion_id);
            const presentation = verificationCriterionPresentation(
              evaluation.criterion_id,
              evaluation.name,
              criterion?.statement,
            );
            const evidenceLines = evaluation.evidence.flatMap(formatVerificationEvidence);
            const notEvaluated = evaluation.passed === null;
            return (
              <details
                key={`${evaluation.criterion_id}-${evaluation.name}`}
                open={evaluation.passed !== true}
                className={`group rounded-xl border ${
                  evaluation.passed
                    ? "border-emerald-100 bg-emerald-50/70"
                    : notEvaluated
                      ? "border-slate-200 bg-slate-50"
                    : "border-amber-200 bg-amber-50"
                }`}
              >
                <summary className="flex cursor-pointer list-none items-start gap-2 px-3 py-2.5 [&::-webkit-details-marker]:hidden">
                  {evaluation.passed
                    ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                    : <AlertTriangle className={`mt-0.5 h-4 w-4 shrink-0 ${notEvaluated ? "text-slate-400" : "text-amber-600"}`} />}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[12px] font-semibold text-slate-800">
                        {presentation.title}
                      </span>
                      <div className="flex shrink-0 items-center gap-1.5">
                        <span className={`text-[10px] font-medium ${
                          evaluation.passed
                            ? "text-emerald-700"
                            : notEvaluated
                              ? "text-slate-500"
                              : "text-amber-700"
                        }`}>
                          {evaluation.passed ? "通过" : notEvaluated ? "未执行" : "未通过"}
                        </span>
                        <span className="text-[10px] text-slate-400">查看明细</span>
                      </div>
                    </div>
                    <p className="mt-1 text-[11px] leading-4 text-slate-500">
                      {presentation.description}
                    </p>
                  </div>
                  <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400 transition-transform group-open:rotate-180" />
                </summary>
                <div className="border-t border-black/[0.05] px-3 pb-3 pt-2.5">
                  {criterion?.statement && (
                    <div className="mb-2.5">
                      <p className="text-[10px] font-semibold text-slate-500">验收规则</p>
                      <p className="mt-1 text-[11px] leading-5 text-slate-600">
                        {criterion.statement}
                      </p>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1.5">
                    <span className="rounded-full bg-white/80 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                      {verificationMethodLabel(evaluation.verifier)}
                    </span>
                    {criterion && (
                      <span className="rounded-full bg-white/80 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                        {criterion.required ? "必需项" : "可选项"}
                      </span>
                    )}
                  </div>
                  {evaluation.gap && (
                    <div className="mt-2.5">
                      <p className={`text-[10px] font-semibold ${notEvaluated ? "text-slate-600" : "text-amber-700"}`}>
                        {notEvaluated ? "未执行原因" : "未通过原因"}
                      </p>
                      <p className={`mt-1 text-[11px] leading-5 ${notEvaluated ? "text-slate-600" : "text-amber-800"}`}>
                        {evaluation.gap}
                      </p>
                    </div>
                  )}
                  <div className="mt-2.5">
                    <p className="text-[10px] font-semibold text-slate-500">判定依据</p>
                    {evidenceLines.length > 0 ? (
                      <ul className="mt-1 space-y-1 text-[11px] leading-5 text-slate-600">
                        {evidenceLines.map((line, index) => (
                          <li key={`${line}-${index}`} className="flex gap-1.5">
                            <span className="text-slate-300">•</span>
                            <span className="min-w-0 break-words">{line}</span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-1 text-[11px] leading-5 text-slate-500">
                        {notEvaluated
                          ? "该项尚未进入评审，因此没有判定依据。"
                          : `该项仅由${verificationMethodLabel(evaluation.verifier)}基于本轮上下文判断，当前未附结构化证据，不属于确定性验证。`}
                      </p>
                    )}
                  </div>
                </div>
              </details>
            );
          })}
          {report.explanation && (
            <details className="group rounded-xl border border-black/[0.06] bg-white/70">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2.5 text-[11px] font-semibold text-slate-600 [&::-webkit-details-marker]:hidden">
                <span>模型验收说明</span>
                <ChevronDown className="h-3.5 w-3.5 text-slate-400 transition-transform group-open:rotate-180" />
              </summary>
              <p className="border-t border-black/[0.05] px-3 py-2.5 text-[11px] leading-5 text-slate-600">
                {report.explanation}
              </p>
            </details>
          )}
        </div>
      )}
    </section>
  );
}

const VERIFICATION_CRITERIA: Record<string, { title: string; description: string }> = {
  task_fulfillment: {
    title: "任务完成度",
    description: "是否真正完成本轮用户要求，而不是只给出计划或口头声明。",
  },
  todo_reconciliation: {
    title: "待办收口",
    description: "本轮产生的 Todo 是否全部完成或明确取消。",
  },
  metric_consistency: {
    title: "指标口径一致性",
    description: "指标名称、计算口径、维度和结论是否前后一致。",
  },
  web_evidence_traceability: {
    title: "网页来源可追溯",
    description: "网页结论是否引用了本轮真实检索得到的来源或链接。",
  },
  analytics_evidence_traceability: {
    title: "分析证据可追溯",
    description: "关键数据是否关联本轮查询结果、数据源、结果 ID 或查询轨迹。",
  },
  time_scope: {
    title: "数据时间范围",
    description: "是否明确并遵守用户指定的数据期间。",
  },
  artifact_delivery: {
    title: "产物交付",
    description: "要求生成或更新的文件是否真实存在并提供了可定位路径。",
  },
  code_validation: {
    title: "代码验证",
    description: "是否执行并通过与代码改动相称的测试、构建或静态检查。",
  },
  report_integrity: {
    title: "报告完整性",
    description: "报告结构、标题、图表和正文是否完整，且未破坏既有模板。",
  },
};

function verificationCriterionPresentation(
  criterionId: string,
  rawName: string,
  statement?: string,
): { title: string; description: string } {
  const known = VERIFICATION_CRITERIA[criterionId];
  if (known) return known;
  const technicalName = /^[a-z0-9_:-]+$/i.test(rawName);
  return {
    title: technicalName ? "自定义验收规则" : rawName,
    description: statement || rawName || criterionId,
  };
}

function verificationStatusLabel(status: string): string {
  return (
    {
      not_required: "无需验收",
      pending: "等待验收",
      evaluating: "正在验收",
      satisfied: "验收通过",
      needs_revision: "需要修正",
      failed: "验收失败",
      max_iterations_reached: "已达最大验收轮次",
      verification_incomplete: "验收流程未完成",
      grader_error: "验收器异常",
      budget_exceeded: "验收预算耗尽",
    } as Record<string, string>
  )[status] || status;
}

function verificationMetricLabel(status: string): string {
  if (status === "satisfied") return "通过";
  if (status === "not_required") return "无需验收";
  if (status === "pending" || status === "evaluating") return "进行中";
  if (status === "verification_incomplete" || status === "grader_error") return "异常";
  return "待修正";
}

function verificationMethodLabel(verifier: string): string {
  return (
    {
      deterministic: "确定性检查",
      analytics: "分析验收",
      llm_grader: "模型评审",
    } as Record<string, string>
  )[verifier] || "验收检查";
}

function taskIntentLabel(intent: string): string {
  return (
    {
      general: "通用任务",
      ai_insights: "AI 资讯",
      web_research: "网页研究",
      knowledge_search: "知识检索",
      database_analysis: "数据库分析",
      table_analysis: "表格分析",
      semantic_dimension: "语义维度",
      logical_dataset: "逻辑数据集",
      artifact: "产物生成",
      code: "代码任务",
    } as Record<string, string>
  )[intent] || intent;
}

function verificationPackLabel(pack: string): string {
  return (
    {
      core: "基础验收",
      web_research: "来源验收",
      analytics: "分析验收",
      artifact: "产物验收",
      code: "代码验收",
    } as Record<string, string>
  )[pack] || pack;
}

function formatVerificationEvidence(evidence: Record<string, unknown>): string[] {
  if (evidence.kind === "todo_state") {
    const total = Number(evidence.total || 0);
    const incomplete = Number(evidence.incomplete || 0);
    return [
      total === 0
        ? "本轮未创建 Todo，无待办需要收口。"
        : `本轮共有 ${total} 个 Todo，其中 ${incomplete} 个未完成。`,
    ];
  }
  if (evidence.kind === "workspace_artifact") {
    const mentioned = Array.isArray(evidence.mentioned) ? evidence.mentioned.length : 0;
    const existing = Array.isArray(evidence.existing) ? evidence.existing.length : 0;
    const missing = Array.isArray(evidence.missing) ? evidence.missing.length : 0;
    return [
      `最终回答引用 ${mentioned} 个产物，确认存在 ${existing} 个，缺失 ${missing} 个。`,
    ];
  }
  if (evidence.kind === "tool_execution") {
    const toolName = String(evidence.tool_name || "未知工具");
    const toolCallId = String(evidence.tool_call_id || "");
    const inputPreview = String(evidence.input_preview || "");
    return [
      `${toolName} 已成功执行${toolCallId ? `（${toolCallId}）` : ""}${inputPreview ? `：${inputPreview}` : ""}`,
    ];
  }
  return Object.entries(evidence).map(([key, value]) => {
    const rendered =
      typeof value === "string"
        ? value
        : JSON.stringify(value, null, 0);
    return `${key}：${rendered}`;
  });
}

function SectionHeader({
  icon,
  title,
  metric,
  open,
  onToggle,
}: {
  icon: React.ReactNode;
  title: string;
  metric?: React.ReactNode;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex w-full items-center justify-between gap-3 py-4 text-left"
    >
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-black/[0.045] text-slate-500">
          {icon}
        </div>
        <span className="truncate text-[15px] font-bold text-slate-700">{title}</span>
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-slate-400 transition-transform duration-200 ${open ? "rotate-90" : ""}`}
        />
      </div>
      {metric && (
        <div className="shrink-0 text-[13px] font-semibold text-slate-500">
          {metric}
        </div>
      )}
    </button>
  );
}

function PermissionsCard({
  active,
  onActivate,
  grants,
  onRevoke,
}: {
  active: boolean;
  onActivate: () => void;
  grants: PermissionGrant[];
  onRevoke: (grantId: string) => Promise<void>;
}) {
  const [revoking, setRevoking] = useState<string | null>(null);
  // One-shot grants are consumed immediately when the interrupted Run
  // resumes. Showing the API race window here leaves a stale permission card;
  // durable Session grants belong in this control panel, one-shot decisions
  // remain visible in the message/trace audit trail.
  const visibleGrants = grants.filter((grant) => grant.scope !== "once");

  return (
    <section>
      <SectionHeader
        icon={<ShieldCheck className="h-4 w-4" />}
        title="权限"
        open={active}
        onToggle={onActivate}
        metric={
          visibleGrants.length > 0 ? (
            <span className="text-[#002fa7]">{visibleGrants.length}</span>
          ) : (
            <span className="text-slate-300">0</span>
          )
        }
      />

      {active && visibleGrants.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-9 text-center">
          <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-50 text-slate-300">
            <KeyRound className="h-5 w-5" />
          </div>
          <p className="text-[14px] font-medium text-slate-400">授权信息将显示在这里</p>
        </div>
      ) : active ? (
        <div className="mt-4 space-y-3 pb-5">
          {visibleGrants.map((grant) => {
            const isToolAction =
              grant.type === "tool_action"
              || ["fingerprint", "network_origin", "tool_name"].includes(grant.target_kind);
            const command = String(grant.metadata?.command || "").trim();
            const sessionTarget = String(grant.metadata?.session_target || "").trim();
            const target = isToolAction
              ? sessionTarget
                || command
                || `命令指纹 ${grant.target.slice(0, 20)}…`
              : grant.target_kind === "all_external_files"
                ? "所有外部文件"
                : grant.target;
            const canWrite = grant.capabilities.includes("write") || grant.type === "external_file_write";
            const commandExecutable = extractCommandExecutable(command);
            const risk = String(grant.metadata?.risk || "");
            const riskLabel = risk
              ? (
                  {
                    high: "脚本执行",
                    network: "联网",
                    package_install: "依赖安装",
                    managed_write: "受控写入",
                    critical: "关键风险",
                  } as Record<string, string>
                )[risk] || risk
              : "";
            const name = isToolAction
              ? grant.target_kind === "network_origin"
                ? "网站访问授权"
                : grant.target_kind === "tool_name"
                  ? "联网搜索授权"
                  : grant.target_kind === "capability" && grant.capabilities.includes("package_install")
                    ? "沙箱依赖安装授权"
                  : commandExecutable
                ? `${commandExecutable} 命令授权`
                : "受控命令授权"
              : grant.target_kind === "all_external_files"
                ? `本 session 外部文件${canWrite ? "写入" : "读取"}`
                : grant.target.split("/").filter(Boolean).pop() || "外部文件";
            return (
              <div key={grant.id} className="rounded-2xl border border-black/[0.06] bg-white/70 p-3">
                <div className="flex items-start gap-2.5">
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-[#002fa7]/10 text-[#002fa7]">
                    {isToolAction
                      ? grant.target_kind === "network_origin" || grant.target_kind === "tool_name"
                        ? <Globe2 className="h-4 w-4" />
                        : <SquareTerminal className="h-4 w-4" />
                      : <KeyRound className="h-4 w-4" />}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0 truncate text-[13px] font-semibold text-slate-900">{name}</div>
                      <button
                        type="button"
                        disabled={revoking === grant.id}
                        onClick={async () => {
                          setRevoking(grant.id);
                          try {
                            await onRevoke(grant.id);
                          } finally {
                            setRevoking(null);
                          }
                        }}
                        className="rounded-full p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
                        aria-label="撤销权限"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                    <div
                      className={`mt-1 text-[10.5px] text-slate-500 ${
                        isToolAction
                          ? "line-clamp-2 break-all font-mono leading-4"
                          : "truncate font-mono"
                      }`}
                      title={target}
                    >
                      {target}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
                        {grant.scope === "once" ? "仅本次" : "本 Session"}
                      </span>
                      {isToolAction ? (
                        <>
                          <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700">
                            {grant.target_kind === "network_origin"
                              ? "网站访问"
                              : grant.target_kind === "tool_name"
                                ? "联网搜索"
                                : "命令执行"}
                          </span>
                          {riskLabel && (
                            <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                              {riskLabel}
                            </span>
                          )}
                          {(grant.capabilities.includes("temporary_network") ||
                            grant.capabilities.includes("network_access")) && (
                            <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700">
                              {grant.capabilities.includes("temporary_network") ? "临时联网" : "联网执行"}
                            </span>
                          )}
                          {grant.capabilities.includes("managed_write") && (
                            <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                              写入项目
                            </span>
                          )}
                          {grant.capabilities.includes("package_install") && (
                            <span className="rounded-full bg-violet-50 px-2 py-0.5 text-[10px] font-medium text-violet-700">
                              安装依赖
                            </span>
                          )}
                        </>
                      ) : (
                        <span
                          className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                            canWrite ? "bg-amber-50 text-amber-700" : "bg-slate-100 text-slate-500"
                          }`}
                        >
                          {canWrite ? "Write" : "Read only"}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}

function ProgressCard({
  active,
  onActivate,
  todos,
}: {
  active: boolean;
  onActivate: () => void;
  todos: Array<{ content: string; status: TodoStatus }>;
}) {
  const completed = todos.filter((todo) => todo.status === "completed").length;
  const hasTodos = todos.length > 0;

  return (
    <section>
      <SectionHeader
        icon={<ListChecks className="h-4 w-4" />}
        title="进度"
        open={active}
        onToggle={onActivate}
        metric={
          hasTodos ? (
            <span>
              <span className="text-emerald-500">{completed}</span>
              <span className="text-slate-300">/{todos.length}</span>
            </span>
          ) : (
            <span className="text-slate-300">0</span>
          )
        }
      />

      {active && (
        <div className="pb-4 space-y-2.5">
          {hasTodos ? (
            <>
              {todos.map((todo, index) => (
                <div key={`${todo.content}-${index}`} className="flex items-start gap-2.5">
                  <TodoStatusIcon status={todo.status} />
                  <p
                    className={`min-w-0 flex-1 text-[13px] leading-relaxed ${
                      todo.status === "completed"
                        ? "text-slate-500 line-through decoration-slate-500 decoration-[1.5px]"
                        : todo.status === "in_progress"
                        ? "text-slate-900 font-medium"
                        : "text-slate-600"
                    }`}
                  >
                    {todo.content}
                  </p>
                </div>
              ))}
            </>
          ) : (
            <ProgressEmptyState />
          )}
        </div>
      )}
    </section>
  );
}

function ProgressEmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-8 text-center">
      <div className="relative mb-4 h-20 w-44 opacity-80">
        <div className="absolute left-7 top-2 h-10 w-32 rounded-full border border-black/[0.08] bg-white" />
        <div className="absolute left-12 top-5 h-3 w-20 rounded-full bg-slate-100" />
        <div className="absolute left-10 top-5 h-3 w-3 rounded-full bg-slate-100" />
        <div className="absolute right-5 top-0 flex h-7 w-7 items-center justify-center rounded-full border border-black/[0.08] bg-white text-slate-300">
          <CheckCircle2 className="h-4 w-4" />
        </div>
        <div className="absolute bottom-1 left-1 h-10 w-36 rounded-full border border-black/[0.08] bg-white" />
        <div className="absolute bottom-4 left-14 h-3 w-20 rounded-full bg-slate-100" />
        <div className="absolute bottom-4 left-8 h-3 w-3 rounded-full bg-slate-100" />
      </div>
      <p className="text-[14px] font-medium text-slate-400">任务进度将显示在这里</p>
    </div>
  );
}

function SourcesEmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-black/[0.045]">
        <BookOpen className="h-6 w-6 text-slate-400" />
      </div>
      <p className="text-[14px] font-medium text-slate-400">来源将显示在这里</p>
    </div>
  );
}

function TodoStatusIcon({ status }: { status: TodoStatus }) {
  if (status === "completed") {
    return <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 fill-slate-900 text-white" />;
  }
  if (status === "in_progress") {
    return <Timer className="mt-0.5 h-5 w-5 shrink-0 text-[#002fa7]" />;
  }
  return <Circle className="mt-0.5 h-5 w-5 shrink-0 text-slate-300" />;
}

function SourcesCard({
  active,
  onActivate,
  cited,
  retrieved,
  selectedHistoricalSource,
  isStreaming,
}: {
  active: boolean;
  onActivate: () => void;
  cited: Array<{ source: SourceRecord; index?: number }>;
  retrieved: Array<{ source: SourceRecord; index?: number }>;
  selectedHistoricalSource: { source: SourceRecord; index?: number } | null;
  isStreaming: boolean;
}) {
  const { activeSourceId } = useApp();
  const activeRef = useRef<HTMLDivElement>(null);
  const total = cited.length + retrieved.length;
  const visibleTotal = total + (selectedHistoricalSource ? 1 : 0);

  useEffect(() => {
    if (!activeSourceId) return;
    const allSources = [
      ...(selectedHistoricalSource ? [selectedHistoricalSource] : []),
      ...cited,
      ...retrieved,
    ];
    if (allSources.some(({ source }) => source.source_id === activeSourceId)) {
      window.setTimeout(() => {
        activeRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 50);
    }
  }, [activeSourceId, cited, retrieved, selectedHistoricalSource]);

  return (
    <section>
      <SectionHeader
        icon={<BookOpen className="h-4 w-4" />}
        title="来源"
        open={active}
        onToggle={onActivate}
        metric={
          isStreaming ? (
            <span className="inline-flex items-center gap-1.5 text-[#002fa7]">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#002fa7]" />
              {visibleTotal}
            </span>
          ) : visibleTotal > 0 ? (
            <span>{visibleTotal}</span>
          ) : (
            <span className="text-slate-300">0</span>
          )}
      />

      {active && visibleTotal === 0 ? (
        <SourcesEmptyState />
      ) : active ? (
        <div className="pb-4 space-y-5">
          {selectedHistoricalSource && (
            <SourceSection title="历史引用" count={1}>
              <SourceItem
                source={selectedHistoricalSource.source}
                citationIndex={selectedHistoricalSource.index}
                isActive
                ref={activeRef}
              />
            </SourceSection>
          )}

          {cited.length > 0 && (
            <SourceSection title="已引用" count={cited.length}>
              {cited.map(({ source, index }) => (
                <SourceItem
                  key={source.source_id}
                  source={source}
                  citationIndex={index}
                  isActive={activeSourceId === source.source_id}
                  ref={activeSourceId === source.source_id ? activeRef : undefined}
                />
              ))}
            </SourceSection>
          )}

          {retrieved.length > 0 && (
            <SourceSection title="其他检索结果" count={retrieved.length}>
              {retrieved.map(({ source }) => (
                <SourceItem
                  key={source.source_id}
                  source={source}
                  isActive={activeSourceId === source.source_id}
                />
              ))}
            </SourceSection>
          )}
        </div>
      ) : null}
    </section>
  );
}

function SourceSection({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <div>
      <h4 className="mb-2 flex items-center gap-2 text-[12px] font-semibold uppercase tracking-wide text-slate-500">
        {title}
        <span className="rounded-full bg-black/[0.045] px-1.5 py-0 text-[10px]">{count}</span>
      </h4>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

function extractCommandExecutable(command: string): string {
  const ignored = new Set(["cd", "mkdir", "printf", "export"]);
  const tokens = tokenizeShellWords(command);
  let atSegmentStart = true;
  for (const token of tokens) {
    if (token === "&&" || token === "||" || token === ";" || token === "|") {
      atSegmentStart = true;
      continue;
    }
    const normalized = token.replace(/^\(+/, "");
    if (!normalized) continue;
    if (atSegmentStart && /^[A-Za-z_][A-Za-z0-9_]*=/.test(normalized)) {
      continue;
    }
    const executable = normalized.split("/").filter(Boolean).pop() || normalized;
    if (ignored.has(executable)) {
      atSegmentStart = true;
      continue;
    }
    return executable;
  }
  return "";
}

function tokenizeShellWords(command: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let quote: "'" | '"' | null = null;
  let escaped = false;
  const pushCurrent = () => {
    if (current) {
      tokens.push(current);
      current = "";
    }
  };

  for (let i = 0; i < command.length; i += 1) {
    const ch = command[i];
    const next = command[i + 1];
    if (escaped) {
      current += ch;
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (ch === quote) {
        quote = null;
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      continue;
    }
    if (/\s/.test(ch)) {
      pushCurrent();
      continue;
    }
    if ((ch === "&" && next === "&") || (ch === "|" && next === "|")) {
      pushCurrent();
      tokens.push(`${ch}${next}`);
      i += 1;
      continue;
    }
    if (ch === ";" || ch === "|") {
      pushCurrent();
      tokens.push(ch);
      continue;
    }
    current += ch;
  }
  pushCurrent();
  return tokens;
}

const SourceItem = React.forwardRef<HTMLDivElement, {
  source: SourceRecord;
  citationIndex?: number;
  isActive?: boolean;
}>(function SourceItem({ source, citationIndex, isActive }, ref) {
  const openUrl = sourceOpenUrl(source);
  const isLocalSource = isLocalResourceUri(source.uri);

  return (
    <div
      ref={ref}
      className={`group rounded-xl border p-3 transition-colors ${
        isActive
          ? "border-[#002fa7]/30 bg-[#002fa7]/[0.04]"
          : "border-black/[0.06] bg-white hover:border-black/[0.12]"
      }`}
    >
      <div className="flex items-start gap-2.5">
        <div className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-black/[0.055]">
          <FileText className="h-3 w-3 text-slate-600" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            {citationIndex !== undefined && (
              <span className="inline-flex h-4 min-w-[16px] items-center justify-center rounded bg-[#002fa7]/10 px-1 text-[10px] font-bold text-[#002fa7]">
                {citationIndex}
              </span>
            )}
            <p className="truncate text-[13px] font-medium text-slate-800" title={source.title}>
              {source.title}
            </p>
          </div>
          {source.quote && (
            <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-slate-500">
              {source.quote}
            </p>
          )}
          {openUrl ? (
            <a
              href={openUrl}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-flex items-center gap-1 text-[11px] text-[#002fa7] hover:underline"
            >
              查看来源
              <ExternalLink className="h-3 w-3" />
            </a>
          ) : isLocalSource ? (
            <p className="mt-2 break-all text-[11px] leading-relaxed text-slate-400" title={source.uri}>
              本地知识库资源
            </p>
          ) : null}
        </div>
      </div>
    </div>
  );
});

function metadataString(source: SourceRecord, key: string): string {
  const value = source.metadata?.[key];
  return typeof value === "string" ? value : "";
}

function isHttpUrl(value: string | undefined): boolean {
  return /^https?:\/\//i.test(value || "");
}

function isLocalResourceUri(value: string | undefined): boolean {
  if (!value) return false;
  return value.startsWith("/knowledge/") || value.startsWith("/") || value.startsWith("~");
}

function sourceOpenUrl(source: SourceRecord): string {
  const uri = source.uri || "";
  if (isHttpUrl(uri)) return uri;
  if (uri.startsWith("/knowledge/")) return rawKnowledgeFileUrl(uri);

  const virtualPath =
    metadataString(source, "virtual_path") ||
    metadataString(source, "linked_markdown_virtual_path") ||
    metadataString(source, "browser_path");
  if (virtualPath.startsWith("/knowledge/")) return rawKnowledgeFileUrl(virtualPath);

  return "";
}

function extractTodosFromToolCall(toolCall: ToolCall): Array<{ content: string; status: TodoStatus }> {
  if (toolCall.tool !== "write_todos" || !toolCall.output) return [];
  try {
    const parsed = JSON.parse(toolCall.output);
    const raw = parsed.todos || parsed;
    if (!Array.isArray(raw)) return [];
    return raw
      .filter((item: unknown) => item && typeof item === "object")
      .map((item: any) => ({
        content: item.content || item.text || String(item),
        status: normalizeTodoStatus(item.status),
      }));
  } catch {
    return [];
  }
}

function normalizeTodoStatus(status: unknown): TodoStatus {
  const s = String(status || "pending").toLowerCase();
  if (s === "completed" || s === "done" || s === "finished") return "completed";
  if (s === "in_progress" || s === "doing" || s === "in progress") return "in_progress";
  return "pending";
}

function isLegacyFalsePositive(source: SourceRecord, toolByCallId: Map<string, string>): boolean {
  if (source.source_type !== "skill") return false;
  const toolName = source.tool_call_id ? toolByCallId.get(source.tool_call_id) : undefined;
  return toolName === "execute_skill";
}
