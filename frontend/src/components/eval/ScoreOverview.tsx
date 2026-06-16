import type { EvalVerdict, EvalDimension } from "@/hooks/useEvalStream";
import { RadarChart } from "./RadarChart";

interface ScoreOverviewProps {
  verdict: EvalVerdict;
  dimensions: EvalDimension[];
}

interface GradeStyle {
  bg: string;
  text: string;
  border: string;
  emoji: string;
  ring: string; // SVG stroke color class equivalent (hex for inline use)
}

function getGradeStyle(grade: string): GradeStyle {
  switch (grade) {
    case "生产级":
      return {
        bg: "bg-emerald-100",
        text: "text-emerald-700",
        border: "border-emerald-200",
        emoji: "🟢",
        ring: "#10b981",
      };
    case "基础扎实":
      return {
        bg: "bg-amber-100",
        text: "text-amber-700",
        border: "border-amber-200",
        emoji: "🟡",
        ring: "#f59e0b",
      };
    case "可用但不稳定":
      return {
        bg: "bg-orange-100",
        text: "text-orange-700",
        border: "border-orange-200",
        emoji: "🟠",
        ring: "#f97316",
      };
    case "需重做":
    default:
      return {
        bg: "bg-red-100",
        text: "text-red-700",
        border: "border-red-200",
        emoji: "🔴",
        ring: "#ef4444",
      };
  }
}

/** Ring progress SVG — shows totalScore / 25 */
function RingProgress({
  score,
  max,
  color,
}: {
  score: number;
  max: number;
  color: string;
}) {
  const size = 88;
  const strokeWidth = 7;
  const r = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * r;
  const pct = Math.min(1, Math.max(0, score / max));
  const dashOffset = circumference * (1 - pct);

  return (
    <svg width={size} height={size} className="-rotate-90">
      {/* Track */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="#e5e7eb"
        strokeWidth={strokeWidth}
      />
      {/* Progress arc */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={dashOffset}
        className="transition-all duration-700"
      />
    </svg>
  );
}

export function ScoreOverview({ verdict, dimensions }: ScoreOverviewProps) {
  const style = getGradeStyle(verdict.grade);
  const radarData = dimensions.map((d) => ({ name: d.name, score: d.score }));

  return (
    <div className="bg-white/70 backdrop-blur-xl border border-black/[0.06] rounded-xl p-5">
      <div className="grid grid-cols-3 gap-4 items-center">
        {/* ── Left: Total Score with ring ── */}
        <div className="flex flex-col items-center gap-1">
          <div className="relative flex items-center justify-center">
            <RingProgress
              score={verdict.totalScore}
              max={25}
              color={style.ring}
            />
            {/* Centered text inside ring */}
            <div className="absolute flex flex-col items-center">
              <span className="text-3xl font-bold text-gray-800 leading-none">
                {verdict.totalScore}
              </span>
              <span className="text-[10px] text-gray-400 font-medium mt-0.5">
                / 25
              </span>
            </div>
          </div>
          <span className="text-[11px] text-gray-400 font-medium">总分</span>
        </div>

        {/* ── Center: Grade badge + note ── */}
        <div className="flex flex-col items-center gap-3">
          <div
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full border text-[13px] font-semibold ${style.bg} ${style.text} ${style.border}`}
          >
            <span>{style.emoji}</span>
            <span>{verdict.grade}</span>
          </div>
          <p className="text-[11px] text-gray-500 text-center leading-relaxed max-w-[140px]">
            {verdict.note}
          </p>
        </div>

        {/* ── Right: Radar chart ── */}
        <div className="flex justify-center">
          <RadarChart dimensions={radarData} size={160} />
        </div>
      </div>
    </div>
  );
}
