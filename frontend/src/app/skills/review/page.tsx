"use client";

import { useState, useEffect, useCallback } from "react";
import { ClipboardCheck, PlayCircle, Zap, X, Loader2, ChevronDown, ChevronRight } from "lucide-react";
import Navbar from "@/components/layout/Navbar";
import { useEvalStream } from "@/hooks/useEvalStream";
import { EvalProgress } from "@/components/eval/EvalProgress";
import { EvalResult } from "@/components/eval/EvalResult";
import { EvalLog } from "@/components/eval/EvalLog";
import { EvalCompare } from "@/components/eval/EvalCompare";
import { listSkills, type SkillInfo } from "@/lib/skillsApi";
import { listVersions, type VersionInfo } from "@/lib/versionApi";
import {
  getEvalResult,
  listEvalResults,
  type FiveDimEvalResult,
  type EvalResultSummary,
} from "@/lib/evalApi";
import type { EvalVerdict, EvalDimension, EvalStrengthWeakness } from "@/hooks/useEvalStream";

// ── Helper: format timestamp ──────────────────────────────
function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ── Helper: map FiveDimEvalResult → EvalResult props ─────
function mapHistoricalToResultProps(result: FiveDimEvalResult): {
  verdict: EvalVerdict;
  dimensions: EvalDimension[];
  strengths: EvalStrengthWeakness[];
  weaknesses: EvalStrengthWeakness[];
} {
  return {
    verdict: {
      grade: result.grade,
      totalScore: result.total_score,
      note: result.verdict_note,
    },
    dimensions: result.dimensions.map((d) => ({
      name: d.name,
      score: d.score,
      reason: d.reason,
      checks: d.checks,
    })),
    strengths: result.strengths,
    weaknesses: result.weaknesses,
  };
}

// ── Grade badge helpers ───────────────────────────────────
function getGradeEmoji(grade: string): string {
  switch (grade) {
    case "生产级":       return "🟢";
    case "基础扎实":     return "🟡";
    case "可用但不稳定": return "🟠";
    default:             return "🔴";
  }
}

// ── Helper: generate compare Markdown for export ─────────
function buildCompareMarkdown(
  vA: string,
  vB: string,
  rA: FiveDimEvalResult | null,
  rB: FiveDimEvalResult | null
): string {
  const lines: string[] = [];
  lines.push(`# 评估对比报告：${vA} vs ${vB}`);
  lines.push(`\n生成时间：${new Date().toLocaleString("zh-CN")}\n`);
  if (rA) {
    lines.push(`## ${vA}`);
    lines.push(`- 总分：${rA.total_score}/25（${rA.grade}）`);
    rA.dimensions.forEach((d) => lines.push(`  - ${d.name}：${d.score}/5`));
  } else {
    lines.push(`## ${vA}\n- 暂无评估数据`);
  }
  if (rB) {
    lines.push(`\n## ${vB}`);
    lines.push(`- 总分：${rB.total_score}/25（${rB.grade}）`);
    rB.dimensions.forEach((d) => lines.push(`  - ${d.name}：${d.score}/5`));
  } else {
    lines.push(`\n## ${vB}\n- 暂无评估数据`);
  }
  if (rA && rB) {
    lines.push(`\n## 差值`);
    lines.push(`- 总分变化：${rB.total_score - rA.total_score}`);
    rA.dimensions.forEach((dA, idx) => {
      const dB = rB.dimensions[idx];
      if (dB) {
        lines.push(`  - ${dA.name}：${dA.score} → ${dB.score}`);
      }
    });
  }
  return lines.join("\n");
}

// ── Main Page ─────────────────────────────────────────────
export default function SkillsReviewPage() {
  // ── Skill list state ──────────────────────────────────
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loadingSkills, setLoadingSkills] = useState(true);

  // ── Two-level selection state ─────────────────────────
  const [expandedSkill, setExpandedSkill] = useState<string | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<SkillInfo | null>(null);
  const [versions, setVersions] = useState<VersionInfo[]>([]);
  const [evalSummaries, setEvalSummaries] = useState<EvalResultSummary[]>([]);
  const [selectedVersions, setSelectedVersions] = useState<string[]>([]);
  const [loadingVersions, setLoadingVersions] = useState(false);

  // ── Historical result for single-version mode ─────────
  const [historicalResult, setHistoricalResult] = useState<FiveDimEvalResult | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);

  // ── Compare mode: two results ─────────────────────────
  const [compareResultA, setCompareResultA] = useState<FiveDimEvalResult | null>(null);
  const [compareResultB, setCompareResultB] = useState<FiveDimEvalResult | null>(null);
  const [loadingCompare, setLoadingCompare] = useState(false);

  // ── Toast ─────────────────────────────────────────────
  const [toast, setToast] = useState<string | null>(null);

  // ── Log drawer state ──────────────────────────────────
  const [logDrawerOpen, setLogDrawerOpen] = useState(false);

  // ── Eval stream hook ──────────────────────────────────
  const {
    phase,
    stages,
    dimensions,
    verdict,
    strengths,
    weaknesses,
    logLines,
    elapsed,
    startEval,
    stopEval,
    resetEval,
  } = useEvalStream();

  // ── Load skill list on mount ──────────────────────────
  useEffect(() => {
    (async () => {
      try {
        const data = await listSkills();
        setSkills(data);
      } catch {
        // ignore
      } finally {
        setLoadingSkills(false);
      }
    })();
  }, []);

  // ── Expand a skill: load versions + eval summaries ────
  const handleExpandSkill = useCallback(
    async (skill: SkillInfo) => {
      if (expandedSkill === skill.name) {
        setExpandedSkill(null);
        return;
      }
      if (phase === "evaluating") return;
      setExpandedSkill(skill.name);
      setSelectedSkill(skill);
      setSelectedVersions([]);
      setHistoricalResult(null);
      setCompareResultA(null);
      setCompareResultB(null);
      resetEval();
      setLogDrawerOpen(false);
      setLoadingVersions(true);
      const [vers, summaries] = await Promise.all([
        listVersions(skill.name).catch(() => [] as VersionInfo[]),
        listEvalResults(skill.name).catch(() => [] as EvalResultSummary[]),
      ]);
      setVersions(vers);
      setEvalSummaries(summaries);
      setLoadingVersions(false);
    },
    [expandedSkill, phase, resetEval]
  );

  // ── Toggle version checkbox (max 2) ──────────────────
  const handleToggleVersion = useCallback(
    (version: string) => {
      setSelectedVersions((prev) => {
        if (prev.includes(version)) {
          return prev.filter((v) => v !== version);
        }
        if (prev.length >= 2) {
          setToast("最多选择 2 个版本");
          setTimeout(() => setToast(null), 2000);
          return prev;
        }
        return [...prev, version];
      });
    },
    []
  );

  // ── Load single version result ────────────────────────
  const loadSingleVersionResult = useCallback(
    async (version: string) => {
      if (!selectedSkill) return;
      setLoadingHistory(true);
      try {
        const ver = version === "current" ? undefined : version;
        const data = await getEvalResult(selectedSkill.name, ver);
        setHistoricalResult(data);
      } catch {
        setHistoricalResult(null);
      } finally {
        setLoadingHistory(false);
      }
    },
    [selectedSkill]
  );

  // ── Load compare results for two versions ─────────────
  const loadCompareResults = useCallback(
    async (verA: string, verB: string) => {
      if (!selectedSkill) return;
      setLoadingCompare(true);
      const [rA, rB] = await Promise.all([
        getEvalResult(
          selectedSkill.name,
          verA === "current" ? undefined : verA
        ).catch(() => null),
        getEvalResult(
          selectedSkill.name,
          verB === "current" ? undefined : verB
        ).catch(() => null),
      ]);
      setCompareResultA(rA);
      setCompareResultB(rB);
      setLoadingCompare(false);
    },
    [selectedSkill]
  );

  // ── React to selectedVersions change ─────────────────
  useEffect(() => {
    if (selectedVersions.length === 1) {
      resetEval();
      setHistoricalResult(null);
      setCompareResultA(null);
      setCompareResultB(null);
      loadSingleVersionResult(selectedVersions[0]);
    } else if (selectedVersions.length === 2) {
      resetEval();
      setHistoricalResult(null);
      loadCompareResults(selectedVersions[0], selectedVersions[1]);
    } else {
      resetEval();
      setHistoricalResult(null);
      setCompareResultA(null);
      setCompareResultB(null);
    }
  }, [selectedVersions, loadSingleVersionResult, loadCompareResults, resetEval]);

  // ── Start evaluation (single-version) ─────────────────
  const handleStartEval = useCallback(() => {
    if (!selectedSkill || selectedVersions.length !== 1) return;
    const currentVersion = selectedVersions[0];
    setHistoricalResult(null);
    const path =
      currentVersion === "current"
        ? selectedSkill.path
        : `${selectedSkill.path}/versions/${currentVersion}`;
    startEval(selectedSkill.name, path, currentVersion);
  }, [selectedSkill, selectedVersions, startEval]);

  // ── Restart eval ──────────────────────────────────────
  const handleRestart = useCallback(() => {
    if (!selectedSkill || selectedVersions.length !== 1) return;
    const currentVersion = selectedVersions[0];
    setLogDrawerOpen(false);
    const path =
      currentVersion === "current"
        ? selectedSkill.path
        : `${selectedSkill.path}/versions/${currentVersion}`;
    startEval(selectedSkill.name, path, currentVersion);
  }, [selectedSkill, selectedVersions, startEval]);

  // ── Compare mode: "evaluate this version" callback ────
  const handleEvalVersion = useCallback(
    (version: string) => {
      if (phase === "evaluating") return;
      setSelectedVersions([version]);
    },
    [phase]
  );

  // ── Compare mode: export Markdown ────────────────────
  const handleExportCompare = useCallback(() => {
    if (selectedVersions.length !== 2) return;
    const [vA, vB] = selectedVersions;
    const md = buildCompareMarkdown(vA, vB, compareResultA, compareResultB);
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `eval-compare-${vA}-vs-${vB}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }, [selectedVersions, compareResultA, compareResultB]);

  // ── Derived mode flags ────────────────────────────────
  const isCompareMode = selectedVersions.length === 2;
  const isSingleMode = selectedVersions.length === 1;
  const currentVersion = isSingleMode ? selectedVersions[0] : null;

  const showEvaluating = isSingleMode && phase === "evaluating";
  const showCompleted =
    isSingleMode &&
    (phase === "completed" ||
      (phase === "idle" && historicalResult !== null));

  const historicalProps = historicalResult
    ? mapHistoricalToResultProps(historicalResult)
    : null;

  const resolvedVerdict: EvalVerdict | null =
    phase === "completed" && verdict
      ? verdict
      : historicalProps?.verdict ?? null;

  const resolvedDimensions: EvalDimension[] =
    phase === "completed" ? dimensions : historicalProps?.dimensions ?? [];

  const resolvedStrengths: EvalStrengthWeakness[] =
    phase === "completed" ? strengths : historicalProps?.strengths ?? [];

  const resolvedWeaknesses: EvalStrengthWeakness[] =
    phase === "completed" ? weaknesses : historicalProps?.weaknesses ?? [];

  const resolvedElapsed = phase === "completed" ? elapsed : 0;

  // ── Build version list: "current" first, then snapshots
  const allVersions: string[] = ["current", ...versions.map((v) => v.label)];

  // ── Map summaries by version label ───────────────────
  const summaryMap = new Map<string, EvalResultSummary>();
  evalSummaries.forEach((s) => summaryMap.set(s.version, s));

  // ── Render ────────────────────────────────────────────
  return (
    <div className="h-screen flex flex-col app-bg">
      <Navbar />

      <div className="flex-1 flex overflow-hidden">
        {/* ── Left Sidebar ── */}
        <aside className="w-[240px] border-r border-black/[0.06] bg-white/60 backdrop-blur-xl flex flex-col shrink-0">
          {/* Sidebar header */}
          <div className="px-3 pt-3 pb-2 border-b border-black/[0.06]">
            <div className="flex items-center gap-2">
              <div className="w-6 h-6 rounded-md bg-gradient-to-br from-emerald-400 to-emerald-500 flex items-center justify-center shadow-sm shadow-emerald-400/20">
                <ClipboardCheck className="w-3.5 h-3.5 text-white" />
              </div>
              <span className="text-[13px] font-semibold text-gray-800">
                评估审核
              </span>
            </div>
          </div>

          {/* Skill list with version sub-rows */}
          <div className="flex-1 overflow-y-auto p-2">
            <p className="text-[10px] font-medium text-gray-400 uppercase tracking-wide px-1.5 mb-1.5 mt-1">
              Skills
            </p>

            {loadingSkills ? (
              <div className="flex items-center justify-center py-6">
                <Loader2 className="w-4 h-4 animate-spin text-gray-300" />
              </div>
            ) : skills.length === 0 ? (
              <p className="text-[12px] text-gray-400 px-2 py-3">
                暂无 Skill
              </p>
            ) : (
              <div className="space-y-0.5">
                {skills.map((skill) => {
                  const isExpanded = expandedSkill === skill.name;
                  const isDisabled = phase === "evaluating";
                  return (
                    <div key={skill.name}>
                      {/* Skill row */}
                      <button
                        onClick={() => handleExpandSkill(skill)}
                        disabled={isDisabled}
                        className={`w-full text-left px-2.5 py-1.5 rounded-lg text-[12px] font-medium transition-all
                          ${isExpanded
                            ? "bg-emerald-500/10 text-emerald-700"
                            : "text-gray-600 hover:bg-black/[0.04]"
                          }
                          ${isDisabled ? "pointer-events-none opacity-50" : ""}
                        `}
                      >
                        <div className="flex items-center gap-1.5">
                          {isExpanded ? (
                            <ChevronDown className="w-3 h-3 shrink-0 text-emerald-500" />
                          ) : (
                            <ChevronRight className="w-3 h-3 shrink-0 text-gray-400" />
                          )}
                          <Zap
                            className={`w-3 h-3 shrink-0 ${
                              isExpanded ? "text-emerald-500" : "text-gray-400"
                            }`}
                          />
                          <span className="truncate">{skill.name}</span>
                        </div>
                      </button>

                      {/* Version sub-rows */}
                      {isExpanded && (
                        <div className="ml-3 mt-0.5 space-y-0.5">
                          {loadingVersions ? (
                            <div className="flex items-center gap-1.5 px-2.5 py-1.5">
                              <Loader2 className="w-3 h-3 animate-spin text-gray-300" />
                              <span className="text-[11px] text-gray-400">
                                加载中…
                              </span>
                            </div>
                          ) : (
                            allVersions.map((ver) => {
                              const isChecked = selectedVersions.includes(ver);
                              const summary = summaryMap.get(ver);
                              return (
                                <button
                                  key={ver}
                                  onClick={() => handleToggleVersion(ver)}
                                  className={`w-full text-left px-2.5 py-1.5 rounded-lg text-[11px] transition-all flex items-center gap-2
                                    ${isChecked
                                      ? "bg-emerald-500/10 text-emerald-700"
                                      : "text-gray-500 hover:bg-black/[0.04]"
                                    }
                                  `}
                                >
                                  {/* Checkbox visual */}
                                  <span
                                    className={`w-3.5 h-3.5 rounded border flex items-center justify-center shrink-0 transition-colors
                                      ${isChecked
                                        ? "bg-emerald-500 border-emerald-500"
                                        : "border-gray-300 bg-white"
                                      }
                                    `}
                                  >
                                    {isChecked && (
                                      <svg
                                        viewBox="0 0 10 10"
                                        className="w-2 h-2 text-white"
                                        fill="none"
                                        stroke="currentColor"
                                        strokeWidth={2}
                                      >
                                        <path d="M1.5 5l2.5 2.5 4.5-4.5" />
                                      </svg>
                                    )}
                                  </span>

                                  <span className="truncate flex-1 font-medium">
                                    {ver}
                                  </span>

                                  {/* Eval summary badge */}
                                  {summary ? (
                                    <span className="shrink-0 text-[10px] font-semibold">
                                      {getGradeEmoji(summary.grade)}{" "}
                                      <span className="text-gray-500">
                                        {summary.total_score}
                                      </span>
                                    </span>
                                  ) : (
                                    <span className="shrink-0 text-[10px] text-gray-300">
                                      未评估
                                    </span>
                                  )}
                                </button>
                              );
                            })
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Bottom: selected count + clear */}
          {expandedSkill && selectedVersions.length > 0 && (
            <div className="px-3 py-2 border-t border-black/[0.06] flex items-center justify-between">
              <span className="text-[11px] text-gray-500">
                已选{" "}
                <span className="font-semibold text-emerald-600">
                  {selectedVersions.length}
                </span>
                /2
              </span>
              <button
                onClick={() => setSelectedVersions([])}
                className="text-[11px] text-gray-400 hover:text-gray-600 transition-colors"
              >
                清除
              </button>
            </div>
          )}
        </aside>

        {/* ── Main Content Area ── */}
        <main className="flex-1 overflow-y-auto relative">
          {/* ── State: No skill expanded ── */}
          {!expandedSkill && (
            <div className="h-full flex items-center justify-center">
              <div className="text-center">
                <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-emerald-400 to-emerald-500 flex items-center justify-center mx-auto mb-4 shadow-lg shadow-emerald-400/15">
                  <ClipboardCheck className="w-8 h-8 text-white" />
                </div>
                <h1 className="text-xl font-semibold text-gray-800 mb-2">
                  评估审核
                </h1>
                <p className="text-[13px] text-gray-400 max-w-xs mx-auto leading-relaxed">
                  展开左侧的 Skill，选择版本开始评估
                </p>
              </div>
            </div>
          )}

          {/* ── State: Skill expanded, no version selected ── */}
          {expandedSkill && selectedVersions.length === 0 && !loadingVersions && (
            <div className="h-full flex items-center justify-center">
              <div className="text-center">
                <div className="w-12 h-12 rounded-xl bg-emerald-500/10 flex items-center justify-center mx-auto mb-3">
                  <Zap className="w-6 h-6 text-emerald-500" />
                </div>
                <p className="text-[14px] font-semibold text-gray-700 mb-1">
                  {expandedSkill}
                </p>
                <p className="text-[12px] text-gray-400 max-w-[260px] mx-auto leading-relaxed">
                  勾选左侧版本（最多 2 个）查看评估结果或进行对比
                </p>
              </div>
            </div>
          )}

          {/* ── State: Single version, loading history ── */}
          {isSingleMode && phase === "idle" && loadingHistory && (
            <div className="h-full flex items-center justify-center">
              <Loader2 className="w-5 h-5 animate-spin text-emerald-400" />
            </div>
          )}

          {/* ── State: Single version, idle, no result ── */}
          {isSingleMode &&
            phase === "idle" &&
            !loadingHistory &&
            !historicalResult && (
              <div className="h-full flex items-center justify-center">
                <div className="text-center max-w-sm">
                  <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-2xl px-8 py-7 shadow-sm mb-4">
                    <div className="w-12 h-12 rounded-xl bg-emerald-500/10 flex items-center justify-center mx-auto mb-4">
                      <Zap className="w-6 h-6 text-emerald-500" />
                    </div>
                    <h2 className="text-[18px] font-bold text-gray-800 mb-1 truncate max-w-[260px] mx-auto">
                      {selectedSkill?.name}
                    </h2>
                    <p className="text-[12px] text-gray-500 mb-1">
                      版本：
                      <span className="font-semibold text-gray-700">
                        {currentVersion}
                      </span>
                    </p>
                    <p className="text-[11px] text-gray-400 font-mono break-all leading-relaxed mb-6">
                      {currentVersion === "current"
                        ? selectedSkill?.path
                        : `${selectedSkill?.path}/versions/${currentVersion}`}
                    </p>
                    <button
                      onClick={handleStartEval}
                      className="flex items-center gap-2 bg-emerald-500 hover:bg-emerald-600 active:bg-emerald-700 text-white rounded-xl px-6 py-2.5 text-[13px] font-semibold transition-colors duration-150 shadow-sm shadow-emerald-400/20 mx-auto"
                    >
                      <PlayCircle className="w-4 h-4" strokeWidth={2.5} />
                      开始评估
                    </button>
                  </div>
                </div>
              </div>
            )}

          {/* ── State: Evaluating ── */}
          {isSingleMode && showEvaluating && selectedSkill && (
            <div className="h-full p-5">
              <EvalProgress
                stages={stages}
                dimensions={dimensions}
                logLines={logLines}
                elapsed={elapsed}
                skillName={selectedSkill.name}
                onStop={stopEval}
              />
            </div>
          )}

          {/* ── State: Completed (hook or historical) ── */}
          {isSingleMode && showCompleted && resolvedVerdict && selectedSkill && (
            <>
              {/* Top bar: metadata + re-eval button */}
              {phase === "idle" && historicalResult && (
                <div className="flex items-center justify-between px-6 pt-4 pb-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[12px] text-gray-500">
                      版本：
                      <span className="font-semibold text-gray-700 ml-1">
                        {currentVersion}
                      </span>
                      <span className="mx-1 text-gray-300">·</span>
                      上次评估：
                      <span className="font-semibold text-gray-700 ml-1">
                        {historicalResult.total_score} 分
                      </span>
                      <span className="mx-1 text-gray-300">·</span>
                      <span className="text-emerald-600 font-medium">
                        {historicalResult.grade}
                      </span>
                      <span className="mx-1 text-gray-300">·</span>
                      <span>{formatTime(historicalResult.timestamp)}</span>
                    </span>
                  </div>
                  <button
                    onClick={handleStartEval}
                    className="flex items-center gap-1.5 bg-emerald-500 hover:bg-emerald-600 active:bg-emerald-700 text-white rounded-lg px-3 py-1.5 text-[12px] font-semibold transition-colors duration-150 shadow-sm shadow-emerald-400/20"
                  >
                    <PlayCircle className="w-3.5 h-3.5" strokeWidth={2.5} />
                    重新评估
                  </button>
                </div>
              )}

              <EvalResult
                verdict={resolvedVerdict}
                dimensions={resolvedDimensions}
                strengths={resolvedStrengths}
                weaknesses={resolvedWeaknesses}
                elapsed={resolvedElapsed}
                skillName={selectedSkill.name}
                onRestart={handleRestart}
                onViewLog={() => setLogDrawerOpen(true)}
              />
            </>
          )}

          {/* ── State: Compare mode loading ── */}
          {isCompareMode && loadingCompare && (
            <div className="h-full flex items-center justify-center">
              <Loader2 className="w-5 h-5 animate-spin text-emerald-400" />
            </div>
          )}

          {/* ── State: Compare mode ── */}
          {isCompareMode && !loadingCompare && (
            <EvalCompare
              resultA={compareResultA}
              resultB={compareResultB}
              versionA={selectedVersions[0]}
              versionB={selectedVersions[1]}
              onEvalVersion={handleEvalVersion}
              onExportCompare={handleExportCompare}
            />
          )}
        </main>
      </div>

      {/* ── Log Drawer (right slide-in panel) ── */}
      {logDrawerOpen && (
        <>
          {/* Backdrop */}
          <div
            className="fixed inset-0 z-40 bg-black/20 backdrop-blur-[2px]"
            onClick={() => setLogDrawerOpen(false)}
          />

          {/* Panel */}
          <div className="fixed top-0 right-0 h-full w-[400px] z-50 flex flex-col bg-gray-900 shadow-2xl shadow-black/30 animate-slide-in-right">
            {/* Panel header */}
            <div className="flex items-center justify-between bg-gray-800 px-4 py-3 flex-shrink-0 border-b border-gray-700">
              <span className="text-[13px] font-semibold text-gray-200">
                评估日志
              </span>
              <button
                onClick={() => setLogDrawerOpen(false)}
                className="w-7 h-7 flex items-center justify-center rounded-lg text-gray-400 hover:text-gray-200 hover:bg-gray-700 transition-colors"
              >
                <X className="w-4 h-4" strokeWidth={2} />
              </button>
            </div>

            {/* Log content */}
            <div className="flex-1 min-h-0">
              <EvalLog logLines={logLines} />
            </div>
          </div>
        </>
      )}

      {/* ── Toast ── */}
      {toast && (
        <div className="fixed bottom-6 right-6 z-50 bg-amber-500 text-white px-4 py-2.5 rounded-xl text-[13px] font-medium shadow-lg animate-fade-in">
          {toast}
        </div>
      )}
    </div>
  );
}
