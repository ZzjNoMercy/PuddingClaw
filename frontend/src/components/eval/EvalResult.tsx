import { RefreshCw, Download, FileText } from "lucide-react";
import type { EvalVerdict, EvalDimension, EvalStrengthWeakness } from "@/hooks/useEvalStream";
import { ScoreOverview } from "./ScoreOverview";
import { DimensionCard } from "./DimensionCard";
import { StrengthWeakness } from "./StrengthWeakness";

interface EvalResultProps {
  verdict: EvalVerdict;
  dimensions: EvalDimension[];
  strengths: EvalStrengthWeakness[];
  weaknesses: EvalStrengthWeakness[];
  elapsed: number;
  skillName: string;
  onRestart: () => void;
  onViewLog: () => void;
}

function formatElapsed(seconds: number): string {
  const mm = Math.floor(seconds / 60).toString().padStart(2, "0");
  const ss = (seconds % 60).toString().padStart(2, "0");
  return `${mm}:${ss}`;
}

function exportMarkdown(
  skillName: string,
  verdict: EvalVerdict,
  dimensions: EvalDimension[],
  strengths: EvalStrengthWeakness[],
  weaknesses: EvalStrengthWeakness[],
  elapsed: number
): void {
  const lines: string[] = [
    `# Skill 评估报告：${skillName}`,
    "",
    `**评估时间**：${new Date().toLocaleString("zh-CN")}`,
    `**耗时**：${formatElapsed(elapsed)}`,
    "",
    "## 综合评分",
    "",
    `- **总分**：${verdict.totalScore} / 25`,
    `- **质量等级**：${verdict.grade}`,
    `- **建议**：${verdict.note}`,
    "",
    "## 五维度评分",
    "",
  ];

  for (const d of dimensions) {
    lines.push(`### ${d.name}：${d.score ?? "—"}/5`);
    if (d.reason) lines.push(`> ${d.reason}`);
    if (d.checks.length > 0) {
      lines.push("");
      for (const c of d.checks) {
        lines.push(`- ${c.passed ? "✅" : "❌"} ${c.item}`);
      }
    }
    lines.push("");
  }

  if (strengths.length > 0) {
    lines.push("## 优势", "");
    for (const s of strengths) {
      lines.push(`- **[${s.dimension}]** ${s.text}`);
    }
    lines.push("");
  }

  if (weaknesses.length > 0) {
    lines.push("## 问题", "");
    for (const w of weaknesses) {
      lines.push(`- **[${w.dimension}]** ${w.text}`);
    }
    lines.push("");
  }

  const md = lines.join("\n");
  console.log("[EvalResult] Markdown export:\n", md);

  // Trigger browser download
  try {
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const safeName = skillName.replace(/[^a-zA-Z0-9_\-\u4e00-\u9fa5]/g, "-");
    a.download = `eval-${safeName}-${Date.now()}.md`;
    a.click();
    URL.revokeObjectURL(url);
  } catch {
    // Silently ignore download errors (e.g. in test environments)
  }
}

export function EvalResult({
  verdict,
  dimensions,
  strengths,
  weaknesses,
  elapsed,
  skillName,
  onRestart,
  onViewLog,
}: EvalResultProps) {
  return (
    <div className="max-w-4xl mx-auto px-6 py-6 space-y-4">
      {/* ── Top completion banner ── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-emerald-500 text-[15px]">✅</span>
          <span className="text-[13px] font-semibold text-gray-700">评估完成</span>
          <span className="text-[11px] text-gray-400 font-medium">
            · 技能：
            <span className="text-gray-600 font-semibold">{skillName}</span>
          </span>
        </div>
        <span className="text-[12px] font-mono text-gray-500 tabular-nums">
          用时 {formatElapsed(elapsed)}
        </span>
      </div>

      {/* ── 1. Score overview (ring + grade + radar) ── */}
      <ScoreOverview verdict={verdict} dimensions={dimensions} />

      {/* ── 2. Five dimension cards ── */}
      {dimensions.map((dim, i) => (
        <DimensionCard key={dim.name} dimension={dim} index={i} />
      ))}

      {/* ── 3. Strengths / weaknesses ── */}
      <StrengthWeakness strengths={strengths} weaknesses={weaknesses} />

      {/* ── 4. Bottom action bar ── */}
      <div className="flex items-center justify-center gap-3 pt-2">
        {/* Primary: restart */}
        <button
          onClick={onRestart}
          className="flex items-center gap-2 bg-emerald-500 hover:bg-emerald-600 active:bg-emerald-700 text-white rounded-lg px-4 py-2 text-[12px] font-medium transition-colors duration-150 shadow-sm"
        >
          <RefreshCw className="w-3.5 h-3.5" strokeWidth={2.5} />
          重新评估
        </button>

        {/* Secondary: export */}
        <button
          onClick={() =>
            exportMarkdown(skillName, verdict, dimensions, strengths, weaknesses, elapsed)
          }
          className="flex items-center gap-2 bg-white hover:bg-gray-50 active:bg-gray-100 border border-black/[0.12] text-gray-600 hover:text-gray-800 rounded-lg px-4 py-2 text-[12px] font-medium transition-colors duration-150"
        >
          <Download className="w-3.5 h-3.5" strokeWidth={2} />
          导出报告
        </button>

        {/* Secondary: view log */}
        <button
          onClick={onViewLog}
          className="flex items-center gap-2 bg-white hover:bg-gray-50 active:bg-gray-100 border border-black/[0.12] text-gray-600 hover:text-gray-800 rounded-lg px-4 py-2 text-[12px] font-medium transition-colors duration-150"
        >
          <FileText className="w-3.5 h-3.5" strokeWidth={2} />
          查看评估日志
        </button>
      </div>
    </div>
  );
}
