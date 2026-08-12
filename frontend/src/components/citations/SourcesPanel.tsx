"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Circle,
  ExternalLink,
  Download,
  FileText,
  FolderOpen,
  Globe2,
  KeyRound,
  Images,
  ListChecks,
  Pause,
  Pencil,
  Play,
  ShieldCheck,
  SquareTerminal,
  Target,
  Timer,
  Trash2,
  X,
} from "lucide-react";
import {
  listSessionPermissions,
  rawKnowledgeFileUrl,
  revokePermissionGrant,
  type HarnessGoal,
  type HarnessRun,
  type PermissionGrant,
  type AgentAttachment,
  type RubricEvaluationReport,
} from "@/lib/api";
import {
  goalControlPresentation,
  goalRevisionApplyPlan,
  goalTodoProgress,
} from "@/lib/goalControls";
import { useApp, type SourceRecord, type ToolCall } from "@/lib/store";
import {
  collectSessionArtifacts,
  isPreviewableImageAttachment,
  isQrImageAttachment,
} from "@/lib/imageAttachments";
import ConfirmDialog from "@/components/ui/ConfirmDialog";

type TodoStatus = "completed" | "in_progress" | "pending" | "cancelled" | "error";

export default function SourcesPanel({
  onAvailabilityChange,
}: {
  onAvailabilityChange?: (available: boolean) => void;
}) {
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
    goalRuns,
    verificationReport,
    pauseActiveGoal,
    resumeActiveGoal,
    cancelActiveGoal,
    extendActiveGoalBudget,
    updateActiveGoal,
    sendMessage,
  } = useApp();
  const [permissionGrants, setPermissionGrants] = useState<PermissionGrant[]>([]);
  const [permissionHistory, setPermissionHistory] = useState<PermissionGrant[]>([]);
  const [permissionSessionId, setPermissionSessionId] = useState<string | null>(null);
  const permissionLoadVersionRef = useRef(0);

  const loadPermissions = React.useCallback(() => {
    const loadVersion = ++permissionLoadVersionRef.current;
    if (!sessionId || sessionId === "default") {
      setPermissionGrants([]);
      setPermissionHistory([]);
      setPermissionSessionId(sessionId || null);
      return;
    }
    listSessionPermissions(sessionId)
      .then(({ grants, history }) => {
        if (permissionLoadVersionRef.current !== loadVersion) return;
        setPermissionGrants(grants);
        setPermissionHistory(history);
        setPermissionSessionId(sessionId);
      })
      .catch(() => {
        if (permissionLoadVersionRef.current !== loadVersion) return;
        setPermissionGrants([]);
        setPermissionHistory([]);
        setPermissionSessionId(sessionId);
      });
  }, [sessionId]);

  useEffect(() => {
    loadPermissions();
  }, [loadPermissions]);

  useEffect(() => {
    const handler = () => loadPermissions();
    window.addEventListener("puddingclaw:permissions-changed", handler);
    return () => window.removeEventListener("puddingclaw:permissions-changed", handler);
  }, [loadPermissions]);

  useEffect(() => {
    if (!isStreaming) loadPermissions();
  }, [isStreaming, loadPermissions]);

  // When a citation marker in the chat is clicked, activeSourceId is set and the
  // inspector opens. Make sure the drawer shows the Sources tab so the cited
  // source is visible, instead of staying on Progress.
  useEffect(() => {
    if (activeSourceId) {
      setInspectorActiveTab("sources");
    }
  }, [activeSourceId, setInspectorActiveTab]);

  const { cited, retrieved } = useMemo(() => {
    const lastUserIndex = messages.findLastIndex((message) => message.role === "user");
    const turnMessages = lastUserIndex >= 0 ? messages.slice(lastUserIndex) : [];
    const sourceMap = new Map<string, SourceRecord>();
    const citationIndex = new Map<string, number>();
    const toolByCallId = new Map<string, string>();
    for (const message of turnMessages) {
      for (const toolCall of message.toolCalls || []) {
        if (toolCall.id) toolByCallId.set(toolCall.id, toolCall.tool);
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
    return { cited: citedSources, retrieved: retrievedSources };
  }, [messages]);
  const sessionArtifacts = useMemo(
    () => collectSessionArtifacts(messages),
    [messages],
  );

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

  // Persisted Todo state is lifecycle-scoped by the backend. An empty list is
  // authoritative after a Run starts or terminal work ends; inferring Todos
  // from historical tool calls would resurrect a completed Goal.
  const displayTodos = todos || [];

  const total = cited.length + retrieved.length;
  const hasSources = total > 0;
  const hasTodos = displayTodos.length > 0;
  const hasProgress = hasTodos;
  const hasPermissions =
    permissionSessionId === sessionId &&
    (permissionGrants.some((grant) => grant.scope !== "once") ||
      permissionHistory.length > 0);
  const showSources = hasSources || selectedHistoricalSource !== null;
  const hasArtifacts = sessionArtifacts.length > 0;
  const hasContent = Boolean(activeGoal || verificationReport || hasProgress || hasPermissions || hasArtifacts || showSources);

  useEffect(() => {
    onAvailabilityChange?.(hasContent);
  }, [hasContent, onAvailabilityChange]);

  const cards: Array<{ key: string; content: React.ReactNode }> = [];
  if (activeGoal) {
    cards.push({
      key: "goal",
      content: (
        <GoalCard
          active={inspectorActiveTab === "goal"}
          onActivate={() => setInspectorActiveTab(inspectorActiveTab === "goal" ? null : "goal")}
          goal={activeGoal}
          run={currentRun}
          runs={goalRuns}
          onPause={pauseActiveGoal}
          onResume={resumeActiveGoal}
          onCancel={cancelActiveGoal}
          onExtendBudget={extendActiveGoalBudget}
          onUpdate={updateActiveGoal}
          onContinue={() =>
            sendMessage(
              "继续执行当前目标",
              [],
              { goalControlAction: "start", hiddenUserMessage: true },
            )
          }
          isStreaming={isStreaming}
        />
      ),
    });
  }
  if (verificationReport) {
    cards.push({
      key: "verification",
      content: (
        <VerificationCard
          active={inspectorActiveTab === "verification"}
          onActivate={() =>
            setInspectorActiveTab(inspectorActiveTab === "verification" ? null : "verification")
          }
          report={verificationReport}
          run={currentRun}
        />
      ),
    });
  }
  if (hasProgress) {
    cards.push({
      key: "progress",
      content: (
        <ProgressCard
          active={inspectorActiveTab === "progress"}
          onActivate={() => setInspectorActiveTab(inspectorActiveTab === "progress" ? null : "progress")}
          todos={displayTodos as Array<{ content: string; status: TodoStatus }>}
        />
      ),
    });
  }
  if (hasPermissions) {
    cards.push({
      key: "permissions",
      content: (
        <PermissionsCard
          active={inspectorActiveTab === "permissions"}
          onActivate={() => setInspectorActiveTab(inspectorActiveTab === "permissions" ? null : "permissions")}
          grants={permissionGrants}
          history={permissionHistory}
          onRevoke={async (grantId) => {
            await revokePermissionGrant(sessionId, grantId);
            loadPermissions();
            window.dispatchEvent(new CustomEvent("puddingclaw:permissions-changed"));
          }}
        />
      ),
    });
  }
  if (hasArtifacts) {
    cards.push({
      key: "attachments",
      content: (
        <ArtifactsCard
          active={inspectorActiveTab === "attachments"}
          onActivate={() => setInspectorActiveTab(inspectorActiveTab === "attachments" ? null : "attachments")}
          artifacts={sessionArtifacts}
        />
      ),
    });
  }
  if (showSources) {
    cards.push({
      key: "sources",
      content: (
        <SourcesCard
          active={inspectorActiveTab === "sources"}
          onActivate={() => setInspectorActiveTab(inspectorActiveTab === "sources" ? null : "sources")}
          cited={cited}
          retrieved={retrieved}
          selectedHistoricalSource={selectedHistoricalSource}
          isStreaming={isStreaming && hasSources}
        />
      ),
    });
  }

  return (
    <div className="h-full overflow-y-auto px-5 py-5">
      {cards.length > 0 ? (
        <h2 className="mb-4 px-1 text-[18px] font-semibold tracking-tight text-slate-900">
          概览
        </h2>
      ) : null}
      {cards.length > 0 && (
        <div className="workspace-side-card overflow-hidden rounded-[28px] px-5 py-3">
          {cards.map((card, index) => (
            <React.Fragment key={card.key}>
              {index > 0 && <PanelDivider />}
              {card.content}
            </React.Fragment>
          ))}
        </div>
      )}
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
  completed: "已完成",
  cancelled: "已取消",
  budget_exceeded: "预算已耗尽",
};

const runStatusLabel: Record<HarnessRun["status"], string> = {
  preparing: "准备中",
  running: "执行中",
  waiting_hitl: "等待授权",
  evaluating: "验收中",
  completed: "已完成",
  cancelled: "已停止",
  failed: "执行失败",
  blocked: "受阻",
  budget_exceeded: "已达本轮上限",
  verification_failed: "待修正",
};

function budgetReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    run_model_call_limit: "本轮模型调用已达上限",
    thread_model_call_limit: "当前会话模型调用已达上限",
    goal_max_runs: "已达到 Goal 最大轮数",
  };
  return labels[reason] || reason;
}

function goalGapLabel(gap: string): string {
  const modelLimitMatch = gap.match(
    /模型调用预算已耗尽[：:]\s*(run_model_call_limit|thread_model_call_limit)(?:\s*\((\d+)\/(\d+)\))?[。.]?/i
  );
  if (modelLimitMatch) {
    const [, reason, used, limit] = modelLimitMatch;
    const scope = reason === "run_model_call_limit" ? "本轮" : "当前会话";
    const usage = used && limit ? `（${used}/${limit}）` : "";
    return `${scope}主 Agent 模型调用已达上限${usage}。`;
  }
  return gap;
}

function GoalCard({
  active,
  onActivate,
  goal,
  run,
  runs,
  onPause,
  onResume,
  onCancel,
  onExtendBudget,
  onUpdate,
  onContinue,
  isStreaming,
}: {
  active: boolean;
  onActivate: () => void;
  goal: HarnessGoal;
  run: HarnessRun | null;
  runs: HarnessRun[];
  onPause: () => Promise<HarnessGoal>;
  onResume: () => Promise<HarnessGoal>;
  onCancel: () => Promise<void>;
  onExtendBudget: (additionalRounds: number) => Promise<HarnessGoal>;
  onUpdate: (objective: string) => Promise<HarnessGoal>;
  onContinue: () => Promise<boolean>;
  isStreaming: boolean;
}) {
  const [actionError, setActionError] = useState("");
  const [editing, setEditing] = useState(false);
  const [draftObjective, setDraftObjective] = useState(goal.objective);
  const [saving, setSaving] = useState(false);
  const [additionalRounds, setAdditionalRounds] = useState(2);
  const [editNotice, setEditNotice] = useState("");
  const [cancelConfirmationOpen, setCancelConfirmationOpen] = useState(false);
  const [actionPending, setActionPending] = useState<
    "pause" | "start" | "cancel" | null
  >(null);
  useEffect(() => {
    if (!editing) setDraftObjective(goal.objective);
  }, [editing, goal.objective, goal.objective_revision]);
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
  const executionActive = Boolean(
    goal.current_run_id
    || (
      isStreaming
      && run?.goal_id === goal.goal_id
      && runIsActive
    )
  );
  const controls = goalControlPresentation(
    goal.status,
    goal.requested_status,
    executionActive,
    Boolean(goal.pending_revision),
  );
  const requestedStatus = goal.requested_status;
  const orderedRuns = useMemo(() => {
    const byId = new Map(runs.map((item) => [item.run_id, item]));
    return goal.run_ids.map((runId) => byId.get(runId)).filter((item): item is HarnessRun => Boolean(item));
  }, [goal.run_ids, runs]);
  const performAction = async (
    pending: "pause" | "start" | "cancel",
    action: () => Promise<void>,
  ) => {
    setActionError("");
    setEditNotice("");
    setActionPending(pending);
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "目标状态更新失败");
    } finally {
      setActionPending(null);
    }
  };
  const startGoal = async () => {
    if (goal.status === "paused" || goal.status === "blocked") {
      await onResume();
    }
    const started = await onContinue();
    if (!started) {
      throw new Error("当前会话正在处理其他任务，暂时无法启动目标");
    }
    setEditNotice("目标已启动，正在创建新的 Run");
  };
  const saveRevision = async (continueAfterSave: boolean) => {
    const normalized = draftObjective.trim();
    if (!normalized) {
      setActionError("目标描述不能为空");
      return;
    }
    setSaving(true);
    setActionError("");
    setEditNotice("");
    try {
      const next = await onUpdate(normalized);
      setEditing(false);
      if (!continueAfterSave) {
        setEditNotice(`已保存为第 ${next.objective_revision || 1} 版`);
        return;
      }
      const applyPlan = goalRevisionApplyPlan(next.status, executionActive);
      for (const step of applyPlan) {
        if (step === "pause") await onPause();
        if (step === "resume") await onResume();
        if (step === "start") {
          const started = await onContinue();
          if (!started) {
            throw new Error("目标已保存，但当前会话暂时无法启动新的 Run");
          }
        }
      }
      setEditNotice(
        executionActive
          ? "已停止旧 Run，并按新版本重新启动"
          : "已保存并启动目标"
      );
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "目标更新失败");
    } finally {
      setSaving(false);
    }
  };
  const extendBudget = async (continueAfterExtension: boolean) => {
    const rounds = Math.floor(Number(additionalRounds));
    if (!Number.isFinite(rounds) || rounds < 1 || rounds > 100) {
      setActionError("追加轮数必须是 1–100 的整数");
      return;
    }
    await performAction("start", async () => {
      const extended = await onExtendBudget(rounds);
      if (!continueAfterExtension) {
        setEditNotice(`已追加 ${rounds} 轮，Goal 保持暂停`);
        return;
      }
      if (extended.status === "paused" || extended.status === "blocked") {
        await onResume();
      }
      const started = await onContinue();
      if (!started) {
        throw new Error("预算已追加，但当前会话暂时无法启动新的 Run");
      }
      setEditNotice(`已追加 ${rounds} 轮并继续执行`);
    });
  };
  return (
    <section>
      <SectionHeader
        icon={<Target className="h-4 w-4" />}
        title="目标"
        metric={controls.metric}
        open={active}
        onToggle={onActivate}
        actions={
          !["completed", "cancelled"].includes(goal.status) ? (
            <div className="flex items-center gap-0.5">
              {goal.status !== "budget_exceeded" ? (
                <button
                  type="button"
                  aria-label="编辑目标"
                  title="编辑目标"
                  onClick={() => {
                    if (!active) onActivate();
                    setDraftObjective(goal.objective);
                    setEditing(true);
                    setActionError("");
                    setEditNotice("");
                  }}
                  className="rounded-lg p-1.5 text-slate-400 hover:bg-black/[0.045] hover:text-slate-700"
                >
                  <Pencil className="h-3.5 w-3.5" />
                </button>
              ) : null}
              {controls.primaryAction === "pause" ? (
                <button
                  type="button"
                  aria-label="暂停目标"
                  title={actionPending === "pause" ? "正在暂停" : controls.primaryLabel}
                  disabled={Boolean(requestedStatus) || actionPending !== null}
                  onClick={() => void performAction("pause", async () => {
                    await onPause();
                    setEditNotice("目标已暂停，当前进度已保留");
                  })}
                  className="rounded-lg p-1.5 text-slate-400 hover:bg-black/[0.045] hover:text-slate-700 disabled:cursor-not-allowed disabled:opacity-35"
                >
                  <Pause className="h-3.5 w-3.5" />
                </button>
              ) : controls.primaryAction === "start"
                || controls.primaryAction === "resume_and_start" ? (
                <button
                  type="button"
                  aria-label={controls.primaryLabel}
                  title={actionPending === "start" ? "正在启动" : controls.primaryLabel}
                  disabled={actionPending !== null || Boolean(requestedStatus)}
                  onClick={() => void performAction("start", startGoal)}
                  className="rounded-lg p-1.5 text-emerald-600 hover:bg-emerald-50 hover:text-emerald-700 disabled:cursor-not-allowed disabled:opacity-35"
                >
                  <Play className="h-3.5 w-3.5" />
                </button>
              ) : null}
              <button
                type="button"
                aria-label="取消目标"
                title={actionPending === "cancel" ? "正在取消" : "取消目标"}
                disabled={Boolean(requestedStatus) || actionPending !== null}
                onClick={() => setCancelConfirmationOpen(true)}
                className="rounded-lg p-1.5 text-slate-400 hover:bg-rose-50 hover:text-rose-600 disabled:cursor-not-allowed disabled:opacity-35"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : undefined
        }
      />
      {active && (
        <div className="pb-4">
          <div className="mb-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
            完成方式：{goal.completion_policy === "rubric" ? "Rubric 验收 · 实验性" : "标准验收"}
          </div>
          {goal.status === "budget_exceeded" ? (
            <div className="mb-3 rounded-2xl border border-amber-200 bg-amber-50/70 p-3">
              <div className="text-[12px] font-semibold text-amber-900">
                本 Goal 已用完 {goal.max_rounds} 轮预算
              </div>
              <p className="mt-1 text-[11px] leading-5 text-amber-800">
                进度、Todo、产物和证据均已保留。追加预算不会自动执行，除非选择“追加并继续”。
              </p>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-2 text-[11px] font-medium text-amber-900">
                  追加
                  <input
                    type="number"
                    min={1}
                    max={100}
                    step={1}
                    value={additionalRounds}
                    onChange={(event) => setAdditionalRounds(Number(event.target.value))}
                    disabled={actionPending !== null}
                    className="h-8 w-20 rounded-lg border border-amber-200 bg-white px-2 text-center text-[12px] text-slate-700 outline-none focus:border-amber-400"
                  />
                  轮
                </label>
                <button
                  type="button"
                  disabled={actionPending !== null}
                  onClick={() => void extendBudget(false)}
                  className="h-8 rounded-lg border border-amber-300 bg-white px-3 text-[11px] font-semibold text-amber-800 hover:bg-amber-100 disabled:opacity-40"
                >
                  仅追加
                </button>
                <button
                  type="button"
                  disabled={actionPending !== null}
                  onClick={() => void extendBudget(true)}
                  className="h-8 rounded-lg bg-amber-600 px-3 text-[11px] font-semibold text-white hover:bg-amber-700 disabled:opacity-40"
                >
                  {actionPending === "start" ? "处理中…" : "追加并继续"}
                </button>
              </div>
            </div>
          ) : null}
          {editing ? (
            <div className="rounded-2xl border border-[#002fa7]/20 bg-[#002fa7]/[0.025] p-3">
              <div className="mb-2 flex items-center justify-between gap-3">
                <span className="text-[11px] font-semibold text-slate-600">修改目标描述</span>
                <span className="text-[10px] text-slate-400">
                  当前第 {goal.objective_revision || 1} 版
                </span>
              </div>
              <textarea
                value={draftObjective}
                onChange={(event) => setDraftObjective(event.target.value)}
                rows={6}
                maxLength={20000}
                autoFocus
                className="w-full resize-y rounded-xl border border-black/[0.08] bg-white px-3 py-2 text-[13px] leading-6 text-slate-700 outline-none focus:border-[#002fa7]/40 focus:ring-2 focus:ring-[#002fa7]/10"
              />
              <div className="mt-2 flex flex-wrap justify-end gap-2">
                <button
                  type="button"
                  disabled={saving}
                  onClick={() => setEditing(false)}
                  className="rounded-lg px-2.5 py-1.5 text-[11px] text-slate-500 hover:bg-black/[0.04]"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={saving || !draftObjective.trim() || draftObjective.trim() === goal.objective}
                  onClick={() => void saveRevision(false)}
                  className="rounded-lg border border-black/[0.08] px-2.5 py-1.5 text-[11px] text-slate-600 hover:bg-black/[0.04] disabled:opacity-40"
                >
                  仅保存
                </button>
                <button
                  type="button"
                  disabled={saving || !draftObjective.trim() || draftObjective.trim() === goal.objective}
                  onClick={() => void saveRevision(true)}
                  className="rounded-lg bg-[#002fa7] px-3 py-1.5 text-[11px] font-medium text-white hover:bg-[#002686] disabled:opacity-40"
                >
                  {saving
                    ? "保存中…"
                    : executionActive
                      ? "保存并立即应用"
                      : "保存并启动"}
                </button>
              </div>
            </div>
          ) : (
            <div>
              <p className="text-[13px] leading-6 text-slate-700">{goal.objective}</p>
              {(goal.objective_revision || 1) > 1 && (
                <p className="mt-1 text-[10px] text-slate-400">
                  第 {goal.objective_revision} 版
                  {goal.pending_revision ? " · 等待按新版本执行" : ""}
                </p>
              )}
            </div>
          )}
          <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] text-slate-500">
            <div className="min-w-0 rounded-xl bg-black/[0.035] px-3 py-2">
              Run
              <span className="mt-0.5 block font-semibold text-slate-700">
                {goal.round}/{goal.max_rounds}
              </span>
            </div>
            <div className="min-w-0 rounded-xl bg-black/[0.035] px-3 py-2">
              主 Agent 模型调用
              <span className="mt-0.5 block break-words font-semibold text-slate-700">
                {goal.model_call_count || 0}
              </span>
            </div>
            <div className="col-span-2 min-w-0 rounded-xl bg-black/[0.035] px-3 py-2">
              本轮状态
              <span className="mt-0.5 block break-words font-semibold text-slate-700">
                {run ? runStatusLabel[run.status] : goalStatusLabel[goal.status]}
              </span>
            </div>
          </div>
          {goal.budget_exhaustion_reason && (
            <p className="mt-2 text-[11px] text-amber-700">
              预算原因：{budgetReasonLabel(goal.budget_exhaustion_reason)}
            </p>
          )}
          {orderedRuns.length > 0 && (
            <div className="mt-3 rounded-xl border border-black/[0.06] bg-white/70 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-[11px] font-semibold text-slate-600">
                <Timer className="h-3.5 w-3.5" />
                Run 时间线
              </div>
              <div className="mt-2 space-y-2">
                {orderedRuns.map((item, index) => (
                  <div key={item.run_id} className="flex min-w-0 items-start gap-2 text-[11px]">
                    <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
                      item.status === "completed"
                        ? "bg-emerald-500"
                        : ["failed", "blocked", "verification_failed"].includes(item.status)
                          ? "bg-rose-500"
                          : item.status === "budget_exceeded"
                            ? "bg-amber-500"
                            : "bg-[#002fa7]"
                    }`} />
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 items-center justify-between gap-2">
                        <span className="truncate font-medium text-slate-700">第 {index + 1} 轮</span>
                        <span className="shrink-0 text-slate-500">{runStatusLabel[item.status]}</span>
                      </div>
                      <div className="mt-0.5 flex flex-wrap gap-x-2 text-[10px] text-slate-400">
                        <span>模型调用 {item.model_call_count || 0}</span>
                        {item.budget_exhaustion_reason ? (
                          <span>{budgetReasonLabel(item.budget_exhaustion_reason)}</span>
                        ) : null}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {(goal.control_notices || []).length > 0 && (
            <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5">
              <div className="text-[11px] font-semibold text-slate-600">运行说明</div>
              <ul className="mt-1 min-w-0 space-y-1 text-[11px] leading-5 text-slate-500">
                {(goal.control_notices || []).slice(-3).map((notice, index) => (
                  <li key={`${notice}-${index}`} className="flex min-w-0 gap-1.5">
                    <span className="shrink-0">•</span>
                    <span className="min-w-0 [overflow-wrap:anywhere]">{goalGapLabel(notice)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {goal.gaps.length > 0 && (
            <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-[11px] font-semibold text-amber-800">
                <AlertTriangle className="h-3.5 w-3.5" />
                尚待补齐
              </div>
              <ul className="mt-1.5 min-w-0 space-y-1 text-[11px] leading-5 text-amber-800/90">
                {goal.gaps.map((gap, index) => (
                  <li key={`${gap}-${index}`} className="flex min-w-0 gap-1.5">
                    <span className="shrink-0">•</span>
                    <span className="min-w-0 [overflow-wrap:anywhere]">{goalGapLabel(gap)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {editNotice && <p className="mt-2 text-[11px] text-emerald-700">{editNotice}</p>}
          {actionError && (
            <p className="mt-2 rounded-lg bg-rose-50 px-2.5 py-2 text-[11px] text-rose-700">
              {actionError}
            </p>
          )}
        </div>
      )}
      <ConfirmDialog
        open={cancelConfirmationOpen}
        title="结束当前 Goal？"
        description={
          executionActive
            ? "当前 Goal 和正在执行的 Run 都将停止，执行记录仍会保留用于审计。"
            : "当前 Goal 将结束，执行记录仍会保留用于审计。"
        }
        confirmLabel="结束 Goal"
        busy={actionPending === "cancel"}
        onClose={() => setCancelConfirmationOpen(false)}
        onConfirm={() => {
          void performAction("cancel", onCancel)
            .finally(() => setCancelConfirmationOpen(false));
        }}
      />
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
  const supersededGoalRevision = report.status === "satisfied"
    && report.accepted_for_goal_revision === false;
  const passed = report.status === "not_required"
    || (report.status === "satisfied" && !supersededGoalRevision);
  const controlError = report.status === "verification_incomplete"
    || report.status === "grader_error"
    || report.status === "infrastructure_error";
  const statusLabel = supersededGoalRevision
    ? "旧版目标验收通过（未接纳）"
    : verificationStatusLabel(report.status);
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
        metric={supersededGoalRevision ? "未接纳" : verificationMetricLabel(report.status)}
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
              <span className="text-slate-300">·</span>
              <span>
                {report.verification_scope === "goal_aggregate"
                  ? `Goal 聚合验收 · ${Math.max(1, report.supporting_run_ids?.length || 0)} 个 Run 证据`
                  : "本 Run 验收"}
              </span>
            </div>
            <p className="mt-1.5 text-[11px] leading-5 text-slate-600">
              {supersededGoalRevision
                ? "该验收报告属于已被修改的 Goal 版本，不会作为当前目标的正式完成结果。"
                : passed
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
            const evidenceLines = Array.from(
              new Set(evaluation.evidence.flatMap(formatVerificationEvidence)),
            );
            const notEvaluated = evaluation.passed === null;
            const infrastructureFailure = evaluation.failure_kind === "infrastructure_error";
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
                          {evaluation.passed
                            ? "通过"
                            : notEvaluated
                              ? "未执行"
                              : infrastructureFailure
                                ? "验收异常"
                                : "未通过"}
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
                        {notEvaluated
                          ? "未执行原因"
                          : infrastructureFailure
                            ? "验收异常原因"
                            : "未通过原因"}
                      </p>
                      <p className={`mt-1 text-[11px] leading-5 ${notEvaluated ? "text-slate-600" : "text-amber-800"}`}>
                        {evaluation.gap}
                      </p>
                    </div>
                  )}
                  <div className="mt-2.5">
                    <p className="text-[10px] font-semibold text-slate-500">
                      {notEvaluated ? "为什么未执行" : evaluation.passed ? "为什么通过" : "为什么未通过"}
                    </p>
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
                    {evaluation.evidence.length > 0 && (
                      <details className="group/technical mt-2 rounded-lg border border-black/[0.05] bg-white/55">
                        <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-2.5 py-2 text-[10px] text-slate-400 [&::-webkit-details-marker]:hidden">
                          <span>技术明细（高级）· {evaluation.evidence.length} 条</span>
                          <ChevronDown className="h-3 w-3 transition-transform group-open/technical:rotate-180" />
                        </summary>
                        <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-all border-t border-black/[0.05] px-2.5 py-2 text-[10px] leading-4 text-slate-500">
                          {JSON.stringify(evaluation.evidence, null, 2)}
                        </pre>
                      </details>
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
      max_iterations_reached: "验收流程未完成（兼容状态）",
      verification_incomplete: "验收流程未完成",
      grader_error: "验收器异常",
      infrastructure_error: "验收基础设施异常",
      budget_exceeded: "验收预算耗尽",
    } as Record<string, string>
  )[status] || status;
}

function verificationMetricLabel(status: string): string {
  if (status === "satisfied") return "通过";
  if (status === "not_required") return "无需验收";
  if (status === "pending" || status === "evaluating") return "进行中";
  if (status === "verification_incomplete" || status === "grader_error" || status === "infrastructure_error") return "异常";
  return "待修正";
}

function verificationMethodLabel(verifier: string): string {
  return (
    {
      deterministic: "系统核验",
      analytics: "数据核验",
      llm_grader: "模型复核",
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
  if (evidence.kind === "artifact_registry") {
    const selected = Array.isArray(evidence.selected_artifact_ids)
      ? evidence.selected_artifact_ids.length
      : 0;
    const existing = Array.isArray(evidence.existing) ? evidence.existing.length : 0;
    const missing = Array.isArray(evidence.missing) ? evidence.missing.length : 0;
    const changed = Array.isArray(evidence.changed) ? evidence.changed.length : 0;
    const invalid = Array.isArray(evidence.invalid) ? evidence.invalid.length : 0;
    return [
      `按工具写入收据核对 ${selected} 个目标产物：${existing} 个有效，${missing} 个缺失，${changed} 个写入后发生变化，${invalid} 个权限或映射异常。`,
    ];
  }
  if (evidence.kind === "tool_result") {
    const toolName = String(evidence.tool_name || "");
    const toolLabel = verificationToolLabel(toolName);
    const resultCount = verificationResultCount(evidence.output_preview);
    if (resultCount !== null) {
      return [`本轮${toolLabel}成功返回 ${resultCount} 条结果，已用于核对结论。`];
    }
    return [`本轮${toolLabel}成功返回结果，已用于核对结论。`];
  }
  if (evidence.kind === "source") {
    const title = String(evidence.title || "").trim();
    const uri = String(evidence.uri || "").trim();
    const host = verificationSourceHost(uri);
    const sourceName = title ? `《${title}》` : host || "网页来源";
    return [
      `已核对本轮获取的来源${sourceName}${title && host ? `（${host}）` : ""}。`,
    ];
  }
  if (evidence.kind === "analytics_result") {
    const reference = String(evidence.ref || "").trim();
    return [
      reference
        ? `分析结论可追溯到本轮查询结果 ${reference}。`
        : "分析结论可追溯到本轮查询结果。",
    ];
  }
  if (evidence.kind === "artifact_write") {
    const path = String(evidence.path || "").trim();
    return [path ? `已确认本轮生成或更新产物：${path}` : "已确认本轮完成产物写入。"];
  }
  if (evidence.kind === "tool_execution") {
    const toolName = String(evidence.tool_name || "未知工具");
    return [`本轮已成功使用${verificationToolLabel(toolName)}完成相关操作。`];
  }
  return ["验收器已记录一条可复核的结构化依据。"];
}

function verificationToolLabel(toolName: string): string {
  return (
    {
      fetch_url: "网页抓取",
      tavily_search: "网页搜索",
      llamaindex_knowledge_query: "知识检索",
      pandas_knowledge_query: "表格分析",
      database_schema_inspect: "数据库结构检查",
      database_evidence_search: "数据库证据检索",
      database_sql_generate: "查询生成",
      database_sql_validate_legacy: "旧版查询校验",
      database_sql_validate: "SQL 校验",
      database_sql_execute: "数据查询",
      database_query_trace_inspect: "查询轨迹检查",
      database_query_result_page: "查询结果读取",
      semantic_entity_lookup: "语义实体查询",
      write_file: "文件写入",
      edit_file: "文件修改",
      execute: "命令执行",
      terminal: "命令执行",
    } as Record<string, string>
  )[toolName] || "工具";
}

function verificationResultCount(rawPreview: unknown): number | null {
  if (typeof rawPreview !== "string" || !rawPreview.trim()) return null;
  try {
    const parsed = JSON.parse(rawPreview) as Record<string, unknown>;
    if (typeof parsed.count === "number" && Number.isFinite(parsed.count)) {
      return parsed.count;
    }
    for (const key of ["items", "results", "sources", "data"]) {
      if (Array.isArray(parsed[key])) return parsed[key].length;
    }
  } catch {
    return null;
  }
  return null;
}

function verificationSourceHost(uri: string): string {
  if (!uri) return "";
  try {
    return new URL(uri).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function SectionHeader({
  icon,
  title,
  metric,
  open,
  onToggle,
  actions,
}: {
  icon: React.ReactNode;
  title: string;
  metric?: React.ReactNode;
  open: boolean;
  onToggle: () => void;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex w-full items-center justify-between gap-3 py-4">
      <button
        type="button"
        onClick={onToggle}
        className="flex min-w-0 flex-1 items-center gap-3 text-left"
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
      </button>
      <div className="flex shrink-0 items-center gap-2">
        {metric && (
          <div className="text-[13px] font-semibold text-slate-500">
            {metric}
          </div>
        )}
        {actions}
      </div>
    </div>
  );
}

function PermissionsCard({
  active,
  onActivate,
  grants,
  history,
  onRevoke,
}: {
  active: boolean;
  onActivate: () => void;
  grants: PermissionGrant[];
  history: PermissionGrant[];
  onRevoke: (grantId: string) => Promise<void>;
}) {
  const [revoking, setRevoking] = useState<string | null>(null);
  const semanticGrants = Array.from(
    grants
      .filter((grant) => grant.scope !== "once" && !grant.superseded_at)
      .reduce((byIdentity, grant) => {
        const identity = grant.semantic_key || grant.id;
        const previous = byIdentity.get(identity);
        if (!previous || Number(grant.created_at || 0) >= Number(previous.created_at || 0)) {
          byIdentity.set(identity, grant);
        }
        return byIdentity;
      }, new Map<string, PermissionGrant>())
      .values(),
  );
  const normalizePath = (value: string) => value.replace(/\\/g, "/").replace(/\/+$/, "");
  const directoryGrants = semanticGrants.filter(
    (grant) => grant.target_kind === "exact_directory",
  );
  // An exact-file card remains an audit record, but it is redundant in the
  // active list once an effective directory capability covers the same file.
  const activeGrants = semanticGrants.filter((grant) => {
    if (grant.target_kind !== "exact_file") return true;
    const target = normalizePath(grant.target);
    return !directoryGrants.some((directoryGrant) => {
      const root = normalizePath(directoryGrant.target);
      const coversPath = target.startsWith(`${root}/`);
      const directoryCapabilities = new Set(directoryGrant.capabilities || []);
      const coversCapabilities = (grant.capabilities || []).every(
        (capability) => capability === "external_path" || directoryCapabilities.has(capability),
      );
      return coversPath && coversCapabilities;
    });
  });

  return (
    <section>
      <SectionHeader
        icon={<ShieldCheck className="h-4 w-4" />}
        title="权限"
        open={active}
        onToggle={onActivate}
        metric={
          activeGrants.length > 0 || history.length > 0 ? (
            <span>
              <span className="text-[#002fa7]">{activeGrants.length}</span>
              <span className="text-slate-300"> 有效 · {history.length} 记录</span>
            </span>
          ) : (
            <span className="text-slate-300">0</span>
          )
        }
      />

      {active && activeGrants.length === 0 && history.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-9 text-center">
          <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-50 text-slate-300">
            <KeyRound className="h-5 w-5" />
          </div>
          <p className="text-[14px] font-medium text-slate-400">授权信息将显示在这里</p>
        </div>
      ) : active ? (
        <div className="mt-4 space-y-5 pb-5">
          {activeGrants.length > 0 ? (
            <PermissionGrantGroup title="当前有效" count={activeGrants.length}>
              {activeGrants.map((grant) => (
                <PermissionGrantRow
                  key={grant.id}
                  grant={grant}
                  revoking={revoking === grant.id}
                  onRevoke={async () => {
                    setRevoking(grant.id);
                    try {
                      await onRevoke(grant.id);
                    } finally {
                      setRevoking(null);
                    }
                  }}
                />
              ))}
            </PermissionGrantGroup>
          ) : null}
          {history.length > 0 ? (
            <details className="group/history">
              <summary className="flex cursor-pointer list-none items-center gap-2 text-[11px] font-semibold text-slate-400">
                <ChevronDown className="h-3.5 w-3.5 -rotate-90 transition-transform group-open/history:rotate-0" />
                <span>已消费或撤销</span>
                <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[9px] text-slate-500">{history.length}</span>
              </summary>
              <div className="mt-2 space-y-3">
                {history.map((grant) => (
                  <PermissionGrantRow key={grant.id} grant={grant} historical />
                ))}
              </div>
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function PermissionGrantGroup({
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
      <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold text-slate-400">
        <span>{title}</span>
        <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[9px] text-slate-500">{count}</span>
      </div>
      <div className="space-y-3">{children}</div>
    </div>
  );
}

function PermissionGrantRow({
  grant,
  historical = false,
  revoking = false,
  onRevoke,
}: {
  grant: PermissionGrant;
  historical?: boolean;
  revoking?: boolean;
  onRevoke?: () => Promise<void>;
}) {
  const presentation = permissionGrantPresentation(grant);
  const canWrite = grant.capabilities.includes("write")
    || grant.type === "external_file_write"
    || grant.type === "external_directory_write";
  const isDirectoryResource = grant.target_kind === "exact_directory"
    || grant.type.startsWith("external_directory_");
  const isFileResource = grant.target_kind === "exact_file"
    || grant.target_kind === "all_external_files"
    || grant.type.startsWith("external_file_");
  const timestamp = grant.consumed_at || grant.revoked_at || grant.created_at;
  return (
    <div className="rounded-2xl border border-black/[0.06] bg-white/70 p-3">
      <div className="flex items-start gap-2.5">
        <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl ${
          presentation.isSkill
            ? "bg-cyan-50 text-cyan-700"
            : isDirectoryResource
              ? "bg-amber-50 text-amber-700"
              : isFileResource
                ? "bg-blue-50 text-blue-700"
            : "bg-[#002fa7]/10 text-[#002fa7]"
        }`}>
          {presentation.isSkill
            ? <ShieldCheck className="h-4 w-4" />
            : presentation.isNetwork
              ? <Globe2 className="h-4 w-4" />
              : presentation.isToolAction
                ? <SquareTerminal className="h-4 w-4" />
                : isDirectoryResource
                  ? <FolderOpen className="h-4 w-4" />
                  : isFileResource
                    ? <FileText className="h-4 w-4" />
                    : <KeyRound className="h-4 w-4" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0 truncate text-[13px] font-semibold text-slate-900">
              {presentation.name}
            </div>
            {!historical && onRevoke ? (
              <button
                type="button"
                disabled={revoking}
                onClick={() => void onRevoke()}
                className="rounded-full p-1 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
                aria-label="撤销权限"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            ) : null}
          </div>
          <div className="mt-1 line-clamp-2 break-all text-[10.5px] leading-4 text-slate-500" title={presentation.target}>
            {presentation.target}
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {historical ? (
              <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">
                {grant.consumed_at ? "已消费" : "已撤销"}
              </span>
            ) : null}
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500">
              {grant.scope === "once"
                ? "单次"
                : grant.scope === "run"
                  ? "本 Run"
                  : "本 Session"}
            </span>
            {grant.metadata?.policy_source === "codex_grok_smart_reviewer" ? (
              <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700">
                智能审查
              </span>
            ) : null}
            {!presentation.isSkill && !presentation.isToolAction && isDirectoryResource ? (
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                目录
              </span>
            ) : !presentation.isSkill && !presentation.isToolAction && isFileResource ? (
              <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700">
                文件
              </span>
            ) : null}
            {presentation.isSkill ? (
              <span className="rounded-full bg-cyan-50 px-2 py-0.5 text-[10px] font-medium text-cyan-700">
                Skill 管理
              </span>
            ) : presentation.isToolAction ? (
              <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700">
                {presentation.isNetwork ? "联网访问" : "命令执行"}
              </span>
            ) : (
              <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                canWrite ? "bg-amber-50 text-amber-700" : "bg-slate-100 text-slate-500"
              }`}>
                {canWrite ? "Write" : "Read only"}
              </span>
            )}
            {presentation.changes ? (
              <span className="rounded-full bg-cyan-50 px-2 py-0.5 text-[10px] font-medium text-cyan-700">
                {presentation.changes}
              </span>
            ) : null}
          </div>
          {historical && timestamp ? (
            <div className="mt-2 text-[10px] text-slate-400">
              {new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false })}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function permissionGrantPresentation(grant: PermissionGrant): {
  name: string;
  target: string;
  changes: string;
  isSkill: boolean;
  isNetwork: boolean;
  isToolAction: boolean;
} {
  const isToolAction = grant.type === "tool_action"
    || ["fingerprint", "network_origin", "tool_name"].includes(grant.target_kind);
  const toolName = String(grant.metadata?.tool_name || "");
  const command = String(grant.metadata?.command || "").trim();
  const preview = grant.metadata?.change_preview || {};
  let commandData: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(command);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      commandData = parsed as Record<string, unknown>;
    }
  } catch {
    // Normal shell commands are intentionally not JSON.
  }
  const requestData = commandData.request && typeof commandData.request === "object"
    ? commandData.request as Record<string, unknown>
    : commandData;
  const planData = commandData.verified_plan && typeof commandData.verified_plan === "object"
    ? commandData.verified_plan as Record<string, unknown>
    : {};
  const isSkill = ["prepare_skill_install", "prepare_skill_update", "install_skill", "update_skill"].includes(toolName)
    || grant.capabilities.includes("managed_skill_write");
  if (isSkill) {
    const skillName = String(preview.skill_name || planData.skill_name || requestData.skill_name || "Skill");
    const source = String(preview.source || planData.source || requestData.source || "受管 Skill 目录");
    const changes = String(preview.changes || "");
    const action = toolName.includes("update") ? "更新" : "安装";
    const prepare = toolName.startsWith("prepare_");
    return {
      name: `${prepare ? "检查" : action} ${skillName}${prepare ? ` ${action}` : ""}`,
      target: source,
      changes,
      isSkill: true,
      isNetwork: grant.capabilities.includes("temporary_network"),
      isToolAction: true,
    };
  }
  const isNetwork = grant.target_kind === "network_origin"
    || grant.target_kind === "tool_name"
    || grant.capabilities.includes("temporary_network")
    || grant.capabilities.includes("network_access");
  if (isToolAction) {
    const executable = extractCommandExecutable(command);
    const isSessionNetwork = grant.target_kind === "capability"
      && grant.target === "session_network_access";
    const isNetworkProfile = grant.target_kind === "network_profile";
    const name = isSessionNetwork
      ? "Session 联网授权"
      : grant.target_kind === "network_origin"
      ? "网站访问授权"
      : isNetworkProfile
        ? "联网工具授权"
      : grant.target_kind === "tool_name"
        ? "联网搜索授权"
        : grant.capabilities.includes("package_install")
          ? "沙箱依赖安装授权"
          : executable ? `${executable} 命令授权` : "受控命令授权";
    return {
      name,
      target: isSessionNetwork
        ? "所有网络来源"
        : String(grant.metadata?.session_target || (isNetworkProfile ? grant.target : command) || `命令指纹 ${grant.target.slice(0, 20)}…`),
      changes: "",
      isSkill: false,
      isNetwork,
      isToolAction: true,
    };
  }
  const canWrite = grant.capabilities.includes("write")
    || grant.type === "external_file_write"
    || grant.type === "external_directory_write";
  const canDelete = grant.capabilities.includes("delete")
    || grant.type === "external_file_delete";
  const isDirectory = grant.target_kind === "exact_directory"
    || grant.type.startsWith("external_directory_");
  return {
    name: grant.target_kind === "all_external_files"
      ? `本 Session 外部文件${canWrite ? "写入" : canDelete ? "删除" : "读取"}`
      : `${grant.target.split("/").filter(Boolean).pop() || (isDirectory ? "外部目录" : "外部文件")}${isDirectory ? ` · 目录${canWrite ? "修改" : "读取"}` : canDelete ? " · 文件删除" : ""}`,
    target: grant.target_kind === "all_external_files" ? "所有外部文件" : grant.target,
    changes: "",
    isSkill: false,
    isNetwork: false,
    isToolAction: false,
  };
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
  const visibleTodos = todos.filter((todo) => todo.status !== "cancelled");
  const progress = goalTodoProgress(todos.map((todo) => todo.status));
  const hasTodos = visibleTodos.length > 0;

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
              <span className="text-emerald-500">{progress.completed}</span>
              <span className="text-slate-300">/{progress.total}</span>
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
              {visibleTodos.map((todo, index) => (
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

function ArtifactsCard({
  active,
  onActivate,
  artifacts,
}: {
  active: boolean;
  onActivate: () => void;
  artifacts: Array<AgentAttachment & { id: string }>;
}) {
  const { openAttachmentPreview } = useApp();

  return (
    <section>
      <SectionHeader
        icon={<Images className="h-4 w-4" />}
        title="产物"
        open={active}
        onToggle={onActivate}
        metric={<span>{artifacts.length}</span>}
      />
      {active ? (
        <div className="space-y-1 pb-4">
          {artifacts.map((artifact) => {
            const isImage = isPreviewableImageAttachment(artifact);
            const canPreview = isImage || (
              (artifact.type === "markdown" || artifact.type === "text") &&
              Boolean(artifact.download_url)
            );
            const isQr = isImage && isQrImageAttachment(artifact);
            const label = artifact.name || "未命名产物";
            const mainClassName = "inspector-transient-action flex min-w-0 flex-1 items-center gap-3 rounded-xl px-2.5 py-2.5 text-left transition hover:bg-black/[0.035] focus:bg-black/[0.035]";
            const content = (
              <>
                <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${isImage ? "bg-blue-50 text-[#376ed8]" : "bg-teal-50 text-teal-600"}`}>
                  {isImage ? <Images className="h-4 w-4" /> : <FileText className="h-4 w-4" />}
                </span>
                <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-slate-800">
                  {label}
                </span>
                {isQr ? (
                  <span className="shrink-0 rounded-full bg-slate-100 px-2 py-0.5 text-[9px] font-medium text-slate-500">
                    二维码
                  </span>
                ) : null}
              </>
            );
            return (
              <div key={artifact.id} className="flex min-w-0 items-center gap-1">
                {canPreview ? (
                  <button
                    type="button"
                    data-inspector-attachment-id={artifact.id}
                    onClick={() => openAttachmentPreview(artifact.id)}
                    className={mainClassName}
                    aria-label={`查看 ${label}`}
                  >
                    {content}
                  </button>
                ) : artifact.download_url ? (
                  <a
                    href={artifact.download_url}
                    download={label}
                    className={mainClassName}
                    aria-label={`下载 ${label}`}
                  >
                    {content}
                  </a>
                ) : (
                  <div className={mainClassName}>{content}</div>
                )}
                {artifact.download_url && canPreview ? (
                  <a
                    href={artifact.download_url}
                    download={label}
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-[#002fa7]"
                    aria-label={`下载 ${label}`}
                    title="下载"
                  >
                    <Download className="h-3.5 w-3.5" />
                  </a>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
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
  const displayTitle = sourceDisplayTitle(source);

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
            <p className="truncate text-[13px] font-medium text-slate-800" title={displayTitle}>
              {displayTitle}
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

function sourceDisplayTitle(source: SourceRecord): string {
  const title = String(source.title || "").trim();
  const uri = source.uri || "";
  const genericTitle = /^\[?\d+\]?$/.test(title)
    || ["", "x.com", "twitter.com", "网页来源", "未命名来源"].includes(title.toLowerCase());
  if (!genericTitle || !isHttpUrl(uri)) {
    return title || "未命名来源";
  }
  try {
    const url = new URL(uri);
    const parts = url.pathname.split("/").filter(Boolean);
    if (source.source_type === "x" && parts.length > 0 && parts[0].toLowerCase() !== "i") {
      return parts.slice(1).includes("status")
        ? `@${parts[0]} 的 X 帖子`
        : `@${parts[0]} 的 X 主页`;
    }
    if (source.source_type !== "x") {
      const slug = decodeURIComponent(parts.at(-1) || "").replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ").trim();
      return slug ? `${slug} · ${url.hostname.replace(/^www\./, "")}` : url.hostname.replace(/^www\./, "");
    }
  } catch {
    // Keep a stable generic fallback for malformed historical source URLs.
  }
  return source.source_type === "x" ? "X 帖子" : "网页来源";
}

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

function normalizeTodoStatus(status: unknown): TodoStatus {
  const s = String(status || "pending").toLowerCase();
  if (s === "completed" || s === "done" || s === "finished") return "completed";
  if (s === "in_progress" || s === "doing" || s === "in progress") return "in_progress";
  if (s === "cancelled" || s === "canceled") return "cancelled";
  if (s === "error" || s === "failed") return "error";
  return "pending";
}

function isLegacyFalsePositive(source: SourceRecord, toolByCallId: Map<string, string>): boolean {
  if (source.source_type !== "skill") return false;
  const toolName = source.tool_call_id ? toolByCallId.get(source.tool_call_id) : undefined;
  return toolName === "execute_skill";
}
