"use client";

import { useState } from "react";
import { CircleGauge, Loader2, Play, Plus, X } from "lucide-react";
import { useApp } from "@/lib/store";
import { parseGoalBudgetRounds } from "@/lib/goalControls";
import ConfirmDialog from "@/components/ui/ConfirmDialog";

export default function GoalBudgetRequestCard() {
  const {
    activeGoal,
    extendActiveGoalBudget,
    resumeActiveGoal,
    cancelActiveGoal,
    sendMessage,
    hasActiveRun,
  } = useApp();
  const [additionalRounds, setAdditionalRounds] = useState("2");
  const [pendingAction, setPendingAction] = useState<
    "extend" | "continue" | "cancel" | null
  >(null);
  const [error, setError] = useState("");
  const [cancelConfirmationOpen, setCancelConfirmationOpen] = useState(false);

  if (!activeGoal || activeGoal.status !== "budget_exceeded") return null;

  const submit = async (continueAfterExtension: boolean) => {
    const rounds = parseGoalBudgetRounds(additionalRounds);
    if (rounds === null) {
      setError("请输入 1–100 的整数轮次");
      return;
    }
    setError("");
    setPendingAction(continueAfterExtension ? "continue" : "extend");
    try {
      const extended = await extendActiveGoalBudget(rounds);
      if (!continueAfterExtension) return;
      if (extended.status === "paused" || extended.status === "blocked") {
        await resumeActiveGoal();
      }
      const started = await sendMessage(
        "继续执行当前目标",
        [],
        { goalControlAction: "start", hiddenUserMessage: true },
      );
      if (!started) {
        throw new Error("预算已追加，但当前会话正在处理其他任务；可稍后从 Goal 继续");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "追加预算失败");
      setPendingAction(null);
    }
  };

  const busy = pendingAction !== null || hasActiveRun;

  return (
    <>
      <div
        className="animate-fade-in px-7 py-3"
        role="group"
        aria-label="Goal 预算追加请求"
        data-testid="goal-budget-request"
      >
        <div className="mx-auto w-full max-w-[900px]">
          <div className="max-w-[680px] rounded-2xl border border-amber-200 bg-amber-50/80 p-4 shadow-sm shadow-amber-950/[0.04]">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-amber-100 text-amber-700">
              <CircleGauge className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-[15px] font-bold text-slate-950">
                  Goal 执行预算已用尽
                </h3>
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                  等待你的选择
                </span>
              </div>
              <p className="mt-1.5 text-[12px] leading-5 text-slate-600">
                已完成 {activeGoal.round} / {activeGoal.max_rounds} 轮。进度、Todo、产物和证据均已保留；追加轮次不会丢失当前上下文。
              </p>

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <label className="flex h-9 items-center gap-2 rounded-xl border border-amber-200 bg-white px-3 text-[12px] font-medium text-slate-700">
                  追加
                  <input
                    type="number"
                    min={1}
                    max={100}
                    step={1}
                    inputMode="numeric"
                    value={additionalRounds}
                    onChange={(event) => setAdditionalRounds(event.target.value)}
                    disabled={busy}
                    aria-label="追加 Goal 轮数"
                    className="w-12 bg-transparent text-center text-[13px] font-semibold text-slate-900 outline-none disabled:opacity-50"
                  />
                  轮
                </label>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void submit(false)}
                  className="inline-flex h-9 items-center gap-1.5 rounded-xl border border-amber-300 bg-white px-3 text-[12px] font-semibold text-amber-800 transition hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {pendingAction === "extend"
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <Plus className="h-3.5 w-3.5" />}
                  仅追加
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void submit(true)}
                  className="inline-flex h-9 items-center gap-1.5 rounded-xl bg-amber-600 px-3.5 text-[12px] font-semibold text-white shadow-sm transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {pendingAction === "continue"
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <Play className="h-3.5 w-3.5 fill-current" />}
                  追加并继续
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setCancelConfirmationOpen(true)}
                  className="inline-flex h-9 items-center gap-1.5 rounded-xl px-3 text-[12px] font-medium text-slate-500 transition hover:bg-white/70 hover:text-rose-700 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {pendingAction === "cancel"
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <X className="h-3.5 w-3.5" />}
                  {pendingAction === "cancel" ? "正在结束…" : "结束 Goal"}
                </button>
              </div>
              {hasActiveRun ? (
                <p className="mt-2 text-[11px] text-amber-800">
                  当前会话仍有 Run 在处理，结束后即可追加。
                </p>
              ) : null}
              {error ? (
                <p className="mt-2 text-[11px] font-medium text-rose-700" role="alert">
                  {error}
                </p>
              ) : null}
            </div>
          </div>
        </div>
      </div>
      </div>
      <ConfirmDialog
        open={cancelConfirmationOpen}
        title="结束当前 Goal？"
        description="当前 Goal 将停止，已完成的进度、Todo、产物和证据记录仍会保留。"
        confirmLabel="结束 Goal"
        busy={pendingAction === "cancel"}
        onClose={() => setCancelConfirmationOpen(false)}
        onConfirm={() => {
          setError("");
          setPendingAction("cancel");
          void cancelActiveGoal()
            .then(() => {
              setPendingAction(null);
              setCancelConfirmationOpen(false);
            })
            .catch((cause) => {
              setError(cause instanceof Error ? cause.message : "Goal 取消失败");
              setPendingAction(null);
              setCancelConfirmationOpen(false);
            });
        }}
      />
    </>
  );
}
