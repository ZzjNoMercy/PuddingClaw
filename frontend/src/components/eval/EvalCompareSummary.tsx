import type { FiveDimEvalResult } from "@/lib/evalApi";

interface EvalCompareSummaryProps {
  resultA: FiveDimEvalResult | null;
  resultB: FiveDimEvalResult | null;
  versionA: string;
  versionB: string;
  onViewFull?: () => void;
}

function getGradeEmoji(grade: string): string {
  switch (grade) {
    case "生产级":        return "🟢";
    case "基础扎实":      return "🟡";
    case "可用但不稳定":  return "🟠";
    case "需重做":
    default:             return "🔴";
  }
}

function InlineDiff({ diff }: { diff: number }) {
  if (diff > 0) {
    return <span className="text-emerald-600 font-semibold">+{diff}</span>;
  }
  if (diff < 0) {
    return <span className="text-red-600 font-semibold">{diff}</span>;
  }
  return <span className="text-gray-400">0</span>;
}

export function EvalCompareSummary({
  resultA,
  resultB,
  versionA,
  versionB,
  onViewFull,
}: EvalCompareSummaryProps) {
  // 两个都无结果：不渲染
  if (resultA === null && resultB === null) return null;

  const bothPresent = resultA !== null && resultB !== null;

  return (
    <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-4">
      {/* 标题栏 */}
      <div className="flex items-center gap-1.5 mb-3">
        <span className="text-[14px]">📊</span>
        <p className="text-[13px] font-semibold text-gray-700">评估对比摘要</p>
      </div>

      {bothPresent ? (
        <div className="space-y-2.5">
          {/* 第一行：总分对比 */}
          <div className="flex items-center gap-2 flex-wrap text-[12px]">
            <span className="text-gray-600 font-medium">{versionA}:</span>
            <span className="font-bold text-gray-800">
              {resultA!.total_score}/25
            </span>
            <span>{getGradeEmoji(resultA!.grade)}</span>
            <span className="text-gray-300 mx-1">→</span>
            <span className="text-gray-600 font-medium">{versionB}:</span>
            <span className="font-bold text-gray-800">
              {resultB!.total_score}/25
            </span>
            <span>{getGradeEmoji(resultB!.grade)}</span>
            <span className="ml-1 text-[12px] font-semibold">
              (
              <InlineDiff diff={resultB!.total_score - resultA!.total_score} />
              )
            </span>
          </div>

          {/* 第二行：五维度差值 */}
          <div className="flex items-center flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-500">
            {resultA!.dimensions.map((dimA, idx) => {
              const dimB = resultB!.dimensions[idx];
              const scoreA = dimA.score;
              const scoreB = dimB?.score ?? scoreA;
              const diff = scoreB - scoreA;
              return (
                <span key={dimA.name} className="whitespace-nowrap">
                  {dimA.name}{" "}
                  <span className="text-gray-600">{scoreA}→{scoreB}</span>
                  (
                  <InlineDiff diff={diff} />
                  )
                </span>
              );
            })}
          </div>

          {/* 第三行：查看完整对比链接 */}
          {onViewFull && (
            <div className="pt-0.5">
              <button
                onClick={onViewFull}
                className="text-[12px] text-emerald-600 hover:text-emerald-700 font-semibold transition-colors"
              >
                查看完整评估对比 →
              </button>
            </div>
          )}
        </div>
      ) : (
        <p className="text-[12px] text-gray-400">
          暂无评估数据，请先评估两个版本
        </p>
      )}
    </div>
  );
}
