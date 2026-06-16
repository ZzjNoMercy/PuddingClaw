import type { FiveDimEvalResult } from "@/lib/evalApi";
import { RadarChart } from "@/components/eval/RadarChart";
import { Download } from "lucide-react";

interface EvalCompareProps {
  resultA: FiveDimEvalResult | null;
  resultB: FiveDimEvalResult | null;
  versionA: string;
  versionB: string;
  onEvalVersion: (version: string) => void;
  onExportCompare: () => void;
}

interface GradeBadgeStyle {
  bg: string;
  text: string;
  border: string;
  emoji: string;
}

function getGradeBadgeStyle(grade: string): GradeBadgeStyle {
  switch (grade) {
    case "生产级":
      return { bg: "bg-emerald-100", text: "text-emerald-700", border: "border-emerald-200", emoji: "🟢" };
    case "基础扎实":
      return { bg: "bg-amber-100", text: "text-amber-700", border: "border-amber-200", emoji: "🟡" };
    case "可用但不稳定":
      return { bg: "bg-orange-100", text: "text-orange-700", border: "border-orange-200", emoji: "🟠" };
    case "需重做":
    default:
      return { bg: "bg-red-100", text: "text-red-700", border: "border-red-200", emoji: "🔴" };
  }
}

function getDimScoreColor(score: number): string {
  if (score >= 4) return "text-emerald-600 font-semibold";
  if (score === 3) return "text-amber-600 font-semibold";
  return "text-red-600 font-semibold";
}

function DiffBadge({ diff }: { diff: number }) {
  if (diff > 0) {
    return <span className="text-emerald-600 font-semibold">+{diff} ↑</span>;
  }
  if (diff < 0) {
    return <span className="text-red-600 font-semibold">{diff} ↓</span>;
  }
  return <span className="text-gray-400 font-semibold">0 →</span>;
}

function MissingVersionCard({
  version,
  onEvalVersion,
}: {
  version: string;
  onEvalVersion: (v: string) => void;
}) {
  return (
    <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-5 flex flex-col items-center gap-3 text-center">
      <div className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center">
        <span className="text-[18px]">📭</span>
      </div>
      <div>
        <p className="text-[13px] font-semibold text-gray-700">{version}</p>
        <p className="text-[12px] text-gray-400 mt-0.5">暂无评估数据</p>
      </div>
      <button
        onClick={() => onEvalVersion(version)}
        className="mt-1 px-4 py-1.5 bg-emerald-500 hover:bg-emerald-600 active:bg-emerald-700 text-white text-[12px] font-semibold rounded-lg transition-colors"
      >
        评估此版本
      </button>
    </div>
  );
}

export function EvalCompare({
  resultA,
  resultB,
  versionA,
  versionB,
  onEvalVersion,
  onExportCompare,
}: EvalCompareProps) {
  const bothPresent = resultA !== null && resultB !== null;
  const partialMissing = !bothPresent && (resultA !== null || resultB !== null);

  const radarA = resultA?.dimensions.map((d) => ({ name: d.name, score: d.score })) ?? [];
  const radarB = resultB?.dimensions.map((d) => ({ name: d.name, score: d.score })) ?? [];

  const totalDiff = bothPresent ? resultB!.total_score - resultA!.total_score : null;

  return (
    <div className="space-y-4 max-w-4xl mx-auto px-6 py-6">
      {/* ── 对比标题栏 ── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-[15px] font-bold text-gray-800">
            {versionA}
            <span className="text-gray-400 mx-2 font-normal">vs</span>
            {versionB}
          </h2>
          {partialMissing && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-orange-100 text-orange-600 text-[11px] font-semibold rounded-full border border-orange-200">
              ⚠️ 部分版本未评估
            </span>
          )}
        </div>
      </div>

      {/* ── 雷达图叠加区（两个都有结果时） ── */}
      {bothPresent && (
        <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-5 flex justify-center">
          <RadarChart dimensions={radarA} dimensionsB={radarB} size={220} />
        </div>
      )}

      {/* ── 总分对比卡片（两个都有结果时） ── */}
      {bothPresent && (
        <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-5">
          <p className="text-[13px] font-semibold text-gray-700 mb-4">总分对比</p>
          <div className="grid grid-cols-3 gap-4 items-center">
            {/* 版本 A */}
            {(() => {
              const style = getGradeBadgeStyle(resultA!.grade);
              return (
                <div className="flex flex-col items-center gap-2">
                  <p className="text-[12px] text-gray-500 font-medium">{versionA}</p>
                  <p className="text-3xl font-bold text-gray-800 leading-none">
                    {resultA!.total_score}
                    <span className="text-[14px] text-gray-400 font-normal"> / 25</span>
                  </p>
                  <span
                    className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full border text-[11px] font-semibold ${style.bg} ${style.text} ${style.border}`}
                  >
                    {style.emoji} {resultA!.grade}
                  </span>
                </div>
              );
            })()}

            {/* 差值 */}
            <div className="flex flex-col items-center gap-1">
              <p className="text-[11px] text-gray-400">差值</p>
              <p className="text-2xl font-bold">
                <DiffBadge diff={totalDiff!} />
              </p>
              <p className="text-[11px] text-gray-400">总分变化</p>
            </div>

            {/* 版本 B */}
            {(() => {
              const style = getGradeBadgeStyle(resultB!.grade);
              return (
                <div className="flex flex-col items-center gap-2">
                  <p className="text-[12px] text-gray-500 font-medium">{versionB}</p>
                  <p className="text-3xl font-bold text-gray-800 leading-none">
                    {resultB!.total_score}
                    <span className="text-[14px] text-gray-400 font-normal"> / 25</span>
                  </p>
                  <span
                    className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full border text-[11px] font-semibold ${style.bg} ${style.text} ${style.border}`}
                  >
                    {style.emoji} {resultB!.grade}
                  </span>
                </div>
              );
            })()}
          </div>
        </div>
      )}

      {/* ── 五维度逐项对比（两个都有结果时） ── */}
      {bothPresent && (
        <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-5">
          <p className="text-[13px] font-semibold text-gray-700 mb-3">维度对比</p>
          <div className="space-y-2.5">
            {resultA!.dimensions.map((dimA, idx) => {
              const dimB = resultB!.dimensions[idx];
              const diff = dimB ? dimB.score - dimA.score : 0;
              return (
                <div
                  key={dimA.name}
                  className="flex items-center gap-3 py-2 border-b border-black/[0.04] last:border-0"
                >
                  <p className="text-[12px] text-gray-600 w-24 shrink-0">{dimA.name}</p>
                  <div className="flex items-center gap-2 flex-1">
                    <span className={`text-[13px] ${getDimScoreColor(dimA.score)}`}>
                      {dimA.score}
                    </span>
                    <span className="text-gray-300 text-[12px]">→</span>
                    {dimB ? (
                      <span className={`text-[13px] ${getDimScoreColor(dimB.score)}`}>
                        {dimB.score}
                      </span>
                    ) : (
                      <span className="text-[12px] text-gray-400">—</span>
                    )}
                  </div>
                  <div className="text-[12px] w-14 text-right">
                    <DiffBadge diff={diff} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── 未评估版本占位提示 ── */}
      {!bothPresent && (
        <div className="grid grid-cols-2 gap-4">
          {resultA === null ? (
            <MissingVersionCard version={versionA} onEvalVersion={onEvalVersion} />
          ) : (
            <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-5 flex flex-col items-center gap-2">
              {(() => {
                const style = getGradeBadgeStyle(resultA.grade);
                return (
                  <>
                    <p className="text-[12px] text-gray-500 font-medium">{versionA}</p>
                    <p className="text-3xl font-bold text-gray-800 leading-none">
                      {resultA.total_score}
                      <span className="text-[14px] text-gray-400 font-normal"> / 25</span>
                    </p>
                    <span
                      className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full border text-[11px] font-semibold ${style.bg} ${style.text} ${style.border}`}
                    >
                      {style.emoji} {resultA.grade}
                    </span>
                  </>
                );
              })()}
            </div>
          )}
          {resultB === null ? (
            <MissingVersionCard version={versionB} onEvalVersion={onEvalVersion} />
          ) : (
            <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-5 flex flex-col items-center gap-2">
              {(() => {
                const style = getGradeBadgeStyle(resultB.grade);
                return (
                  <>
                    <p className="text-[12px] text-gray-500 font-medium">{versionB}</p>
                    <p className="text-3xl font-bold text-gray-800 leading-none">
                      {resultB.total_score}
                      <span className="text-[14px] text-gray-400 font-normal"> / 25</span>
                    </p>
                    <span
                      className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full border text-[11px] font-semibold ${style.bg} ${style.text} ${style.border}`}
                    >
                      {style.emoji} {resultB.grade}
                    </span>
                  </>
                );
              })()}
            </div>
          )}
        </div>
      )}

      {/* ── 底部操作区 ── */}
      <div className="flex justify-end pt-1">
        <button
          onClick={onExportCompare}
          className="inline-flex items-center gap-2 px-4 py-2 bg-white/80 hover:bg-white border border-black/[0.08] hover:border-black/[0.14] text-gray-700 text-[12px] font-semibold rounded-lg transition-all shadow-sm"
        >
          <Download size={14} />
          导出对比报告
        </button>
      </div>
    </div>
  );
}
