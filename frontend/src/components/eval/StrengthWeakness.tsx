import { CheckCircle2, AlertTriangle } from "lucide-react";
import type { EvalStrengthWeakness } from "@/hooks/useEvalStream";

interface StrengthWeaknessProps {
  strengths: EvalStrengthWeakness[];
  weaknesses: EvalStrengthWeakness[];
}

function DimensionTag({
  label,
  variant,
}: {
  label: string;
  variant: "strength" | "weakness";
}) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-medium flex-shrink-0 ${
        variant === "strength"
          ? "bg-emerald-100 text-emerald-600"
          : "bg-amber-100 text-amber-600"
      }`}
    >
      {label}
    </span>
  );
}

export function StrengthWeakness({ strengths, weaknesses }: StrengthWeaknessProps) {
  return (
    <div className="grid grid-cols-2 gap-4">
      {/* ── Left column: Strengths ── */}
      <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-4">
        {/* Column header */}
        <div className="flex items-center gap-2 mb-3">
          <CheckCircle2 className="w-4 h-4 text-emerald-500 flex-shrink-0" strokeWidth={2} />
          <h3 className="text-[13px] font-semibold text-gray-700">优势</h3>
          <span className="ml-auto text-[11px] text-gray-400 font-medium tabular-nums">
            {strengths.length} 条
          </span>
        </div>

        {strengths.length === 0 ? (
          <p className="text-[12px] text-gray-400 py-2 text-center">暂无</p>
        ) : (
          <div className="flex flex-col gap-2.5">
            {strengths.map((s, i) => (
              <div
                key={i}
                className="flex items-start gap-2 border-l-2 border-l-emerald-400 pl-2.5 py-0.5"
              >
                {/* Green dot */}
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 flex-shrink-0 mt-1.5" />
                <div className="flex flex-wrap items-start gap-1.5 min-w-0">
                  <DimensionTag label={s.dimension} variant="strength" />
                  <span className="text-[12px] text-gray-600 leading-relaxed">
                    {s.text}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Right column: Weaknesses ── */}
      <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-4">
        {/* Column header */}
        <div className="flex items-center gap-2 mb-3">
          <AlertTriangle className="w-4 h-4 text-amber-500 flex-shrink-0" strokeWidth={2} />
          <h3 className="text-[13px] font-semibold text-gray-700">问题</h3>
          <span className="ml-auto text-[11px] text-gray-400 font-medium tabular-nums">
            {weaknesses.length} 条
          </span>
        </div>

        {weaknesses.length === 0 ? (
          <p className="text-[12px] text-gray-400 py-2 text-center">暂无</p>
        ) : (
          <div className="flex flex-col gap-2.5">
            {weaknesses.map((w, i) => (
              <div
                key={i}
                className="flex items-start gap-2 border-l-2 border-l-amber-400 pl-2.5 py-0.5"
              >
                {/* Orange dot */}
                <span className="w-1.5 h-1.5 rounded-full bg-amber-500 flex-shrink-0 mt-1.5" />
                <div className="flex flex-wrap items-start gap-1.5 min-w-0">
                  <DimensionTag label={w.dimension} variant="weakness" />
                  <span className="text-[12px] text-gray-600 leading-relaxed">
                    {w.text}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
