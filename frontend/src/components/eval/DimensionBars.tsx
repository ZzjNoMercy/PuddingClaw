import type { EvalDimension } from "@/hooks/useEvalStream";

interface DimensionBarsProps {
  dimensions: EvalDimension[];
}

function getBarColor(score: number): string {
  if (score >= 4) return "bg-emerald-500";
  if (score === 3) return "bg-amber-400";
  return "bg-red-500";
}

export function DimensionBars({ dimensions }: DimensionBarsProps) {
  return (
    <div className="flex flex-col gap-2.5">
      {dimensions.map((dim) => {
        const hasScore = dim.score !== null;
        const score = dim.score ?? 0;
        const color = hasScore ? getBarColor(score) : "";

        return (
          <div key={dim.name} className="flex items-center gap-2">
            {/* Dimension name */}
            <span className="text-[11px] text-gray-600 w-[76px] flex-shrink-0 truncate">
              {dim.name}
            </span>

            {/* 5-block progress bar */}
            <div className="flex gap-0.5 flex-1">
              {Array.from({ length: 5 }, (_, i) => {
                const filled = hasScore && i < score;
                return (
                  <div
                    key={i}
                    className={`flex-1 h-3 rounded-sm transition-colors duration-500 ${
                      filled ? color : "bg-gray-200"
                    }`}
                  />
                );
              })}
            </div>

            {/* Score label */}
            <span
              className={`text-[11px] font-semibold w-7 text-right flex-shrink-0 ${
                hasScore
                  ? score >= 4
                    ? "text-emerald-600"
                    : score === 3
                    ? "text-amber-500"
                    : "text-red-500"
                  : "text-gray-400"
              }`}
            >
              {hasScore ? `${score}/5` : "—"}
            </span>
          </div>
        );
      })}
    </div>
  );
}
