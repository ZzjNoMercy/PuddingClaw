import { CheckCircle2, Loader2 } from "lucide-react";
import type { EvalStage } from "@/hooks/useEvalStream";

interface StageTimelineProps {
  stages: EvalStage[];
}

export function StageTimeline({ stages }: StageTimelineProps) {
  return (
    <div className="flex flex-col gap-0">
      {stages.map((stage, index) => {
        const isLast = index === stages.length - 1;
        const lineGreen = stage.status === "done";

        return (
          <div key={stage.id} className="flex items-stretch gap-3">
            {/* Icon column */}
            <div className="flex flex-col items-center">
              {/* Circle / icon */}
              <div className="flex-shrink-0 mt-0.5">
                {stage.status === "done" ? (
                  <CheckCircle2
                    className="w-4 h-4 text-emerald-500"
                    strokeWidth={2.5}
                  />
                ) : stage.status === "running" ? (
                  <div className="w-4 h-4 rounded-full bg-emerald-500 flex items-center justify-center">
                    <Loader2 className="w-2.5 h-2.5 text-white animate-spin" strokeWidth={3} />
                  </div>
                ) : (
                  <div className="w-4 h-4 rounded-full border-2 border-gray-300 bg-white" />
                )}
              </div>
              {/* Connector line */}
              {!isLast && (
                <div
                  className={`w-0.5 flex-1 mt-0.5 mb-0.5 min-h-[16px] transition-colors duration-500 ${
                    lineGreen ? "bg-emerald-400" : "bg-gray-200"
                  }`}
                />
              )}
            </div>

            {/* Label row */}
            <div
              className={`flex-1 flex items-center py-1 px-2 rounded-lg mb-1 transition-colors duration-300 ${
                stage.status === "running" ? "bg-emerald-50/50" : ""
              }`}
            >
              <span
                className={`text-[12px] font-medium transition-colors duration-300 ${
                  stage.status === "done"
                    ? "text-gray-800"
                    : stage.status === "running"
                    ? "text-emerald-600 font-semibold"
                    : "text-gray-400"
                }`}
              >
                {stage.name}
              </span>
              {stage.status === "running" && (
                <span className="ml-2 text-[10px] text-emerald-500 animate-pulse">
                  进行中…
                </span>
              )}
              {stage.status === "done" && (
                <span className="ml-2 text-[10px] text-emerald-400">完成</span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
