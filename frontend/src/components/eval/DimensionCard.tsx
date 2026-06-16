import { useState } from "react";
import { ChevronDown, ChevronRight, CheckCircle2, XCircle } from "lucide-react";
import type { EvalDimension } from "@/hooks/useEvalStream";

interface DimensionCardProps {
  dimension: EvalDimension;
  index: number;
}

// Returns Tailwind color classes for the dot/circle based on score
function getScoreColors(score: number | null): {
  dot: string;
  text: string;
  border: string;
  bg: string;
} {
  if (score === null)
    return { dot: "bg-gray-300", text: "text-gray-400", border: "border-gray-200", bg: "bg-gray-100" };
  if (score >= 4)
    return { dot: "bg-emerald-500", text: "text-emerald-600", border: "border-emerald-200", bg: "bg-emerald-100" };
  if (score === 3)
    return { dot: "bg-amber-400", text: "text-amber-600", border: "border-amber-200", bg: "bg-amber-100" };
  return { dot: "bg-red-500", text: "text-red-600", border: "border-red-200", bg: "bg-red-100" };
}

// Left border accent colors cycling through 5 distinct hues
const INDEX_BORDER_COLORS = [
  "border-l-emerald-400",
  "border-l-blue-400",
  "border-l-violet-400",
  "border-l-amber-400",
  "border-l-rose-400",
];

// Mini 5-block progress bar (more compact than DimensionBars)
function MiniBar({ score }: { score: number | null }) {
  const s = score ?? 0;
  const filledClass =
    score === null
      ? "bg-gray-200"
      : score >= 4
      ? "bg-emerald-500"
      : score === 3
      ? "bg-amber-400"
      : "bg-red-500";

  return (
    <div className="flex gap-0.5">
      {Array.from({ length: 5 }, (_, i) => (
        <div
          key={i}
          className={`w-4 h-2 rounded-sm transition-colors duration-500 ${
            score !== null && i < s ? filledClass : "bg-gray-200"
          }`}
        />
      ))}
    </div>
  );
}

export function DimensionCard({ dimension, index }: DimensionCardProps) {
  const [expanded, setExpanded] = useState(false);
  const colors = getScoreColors(dimension.score);
  const borderColor = INDEX_BORDER_COLORS[index % INDEX_BORDER_COLORS.length];
  const hasDetail =
    (dimension.reason && dimension.reason.trim().length > 0) ||
    dimension.checks.length > 0;

  return (
    <div
      className={`bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl overflow-hidden border-l-4 ${borderColor}`}
    >
      {/* ── Overview row (always visible, clickable) ── */}
      <button
        className="w-full flex items-center gap-3 px-4 py-3 hover:bg-black/[0.02] transition-colors duration-100 text-left"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {/* Index circle */}
        <div
          className={`w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 text-[10px] font-bold ${colors.bg} ${colors.text} border ${colors.border}`}
        >
          {index + 1}
        </div>

        {/* Dimension name */}
        <span className="text-[13px] font-semibold text-gray-700 flex-1 text-left truncate">
          {dimension.name}
        </span>

        {/* Mini bar */}
        <MiniBar score={dimension.score} />

        {/* Score label */}
        <span
          className={`text-[12px] font-semibold w-8 text-right flex-shrink-0 ${colors.text}`}
        >
          {dimension.score !== null ? `${dimension.score}/5` : "—"}
        </span>

        {/* Expand/collapse chevron */}
        <span className="text-gray-400 flex-shrink-0 ml-1">
          {expanded ? (
            <ChevronDown className="w-4 h-4" strokeWidth={2} />
          ) : (
            <ChevronRight className="w-4 h-4" strokeWidth={2} />
          )}
        </span>
      </button>

      {/* ── Expanded detail ── */}
      {expanded && (
        <div className="px-4 pb-4 flex flex-col gap-3 border-t border-black/[0.04]">
          {hasDetail ? (
            <>
              {/* Reason block */}
              {dimension.reason && dimension.reason.trim() && (
                <div className="bg-gray-50 rounded-lg p-3 mt-3">
                  <p className="text-[12px] text-gray-600 leading-relaxed">
                    {dimension.reason}
                  </p>
                </div>
              )}

              {/* Check items */}
              {dimension.checks.length > 0 && (
                <div className="flex flex-col gap-1.5 mt-1">
                  {dimension.checks.map((check, i) => (
                    <div key={i} className="flex items-start gap-2">
                      {check.passed ? (
                        <CheckCircle2
                          className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0 mt-0.5"
                          strokeWidth={2}
                        />
                      ) : (
                        <XCircle
                          className="w-3.5 h-3.5 text-red-500 flex-shrink-0 mt-0.5"
                          strokeWidth={2}
                        />
                      )}
                      <span className="text-[12px] text-gray-600 leading-relaxed">
                        {check.item}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <p className="text-[12px] text-gray-400 mt-3 text-center py-2">
              暂无详细数据
            </p>
          )}
        </div>
      )}
    </div>
  );
}
