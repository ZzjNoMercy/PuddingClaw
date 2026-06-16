import { Square } from "lucide-react";
import type { EvalStage, EvalDimension } from "@/hooks/useEvalStream";
import { StageTimeline } from "./StageTimeline";
import { DimensionBars } from "./DimensionBars";
import { EvalLog } from "./EvalLog";

interface EvalProgressProps {
  stages: EvalStage[];
  dimensions: EvalDimension[];
  logLines: string[];
  elapsed: number;
  skillName: string;
  onStop: () => void;
}

function formatElapsed(seconds: number): string {
  const mm = Math.floor(seconds / 60)
    .toString()
    .padStart(2, "0");
  const ss = (seconds % 60).toString().padStart(2, "0");
  return `${mm}:${ss}`;
}

export function EvalProgress({
  stages,
  dimensions,
  logLines,
  elapsed,
  skillName,
  onStop,
}: EvalProgressProps) {
  return (
    <div className="flex flex-col gap-4 h-full">
      {/* Top info bar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-[13px] font-semibold text-gray-800 truncate max-w-[240px]">
            {skillName}
          </span>
          {/* Status badge */}
          <div className="flex items-center gap-1.5 bg-emerald-50 border border-emerald-200 rounded-full px-2.5 py-0.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
            <span className="text-[11px] text-emerald-600 font-medium">
              评估中
            </span>
          </div>
        </div>
        {/* Elapsed time */}
        <span className="text-[12px] font-mono text-gray-500 tabular-nums">
          {formatElapsed(elapsed)}
        </span>
      </div>

      {/* Two-column layout */}
      <div className="flex gap-4 flex-1 min-h-0">
        {/* Left column — 60% */}
        <div className="flex flex-col gap-3 w-[60%] min-w-0">
          {/* Stage timeline card */}
          <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-4">
            <h3 className="text-[13px] font-semibold text-gray-700 mb-3">
              评估阶段
            </h3>
            <StageTimeline stages={stages} />
          </div>

          {/* Dimension bars card */}
          <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-4">
            <h3 className="text-[13px] font-semibold text-gray-700 mb-3">
              维度评分
            </h3>
            <DimensionBars dimensions={dimensions} />
          </div>
        </div>

        {/* Right column — 40% */}
        <div className="flex flex-col w-[40%] min-w-0 min-h-0">
          <EvalLog logLines={logLines} />
        </div>
      </div>

      {/* Bottom stop button */}
      <div className="flex justify-center flex-shrink-0">
        <button
          onClick={onStop}
          className="flex items-center gap-2 bg-red-50 hover:bg-red-100 border border-red-200 hover:border-red-300 text-red-600 hover:text-red-700 rounded-lg px-4 py-2 text-[12px] font-medium transition-colors duration-150"
        >
          <Square className="w-3.5 h-3.5" strokeWidth={2.5} />
          停止评估
        </button>
      </div>
    </div>
  );
}
