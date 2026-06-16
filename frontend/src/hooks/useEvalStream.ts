/**
 * useEvalStream — Core hook for five-dimension skill quality evaluation.
 *
 * Drives the evaluation state machine:
 *   idle → evaluating → completed
 *
 * Streams the evaluation prompt via streamChat (SSE), parses format markers
 * from the accumulated token buffer, and manages all derived state.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { streamChat } from "@/lib/api";
import { saveEvalResult, type FiveDimEvalResult } from "@/lib/evalApi";

// ── Exported Types ────────────────────────────────────────

export interface EvalStage {
  id: number;
  name: string;
  status: "pending" | "running" | "done";
}

export interface EvalCheck {
  item: string;
  passed: boolean;
}

export interface EvalDimension {
  name: string;
  score: number | null;
  reason: string;
  checks: EvalCheck[];
}

export interface EvalVerdict {
  grade: string;
  totalScore: number;
  note: string;
}

export interface EvalStrengthWeakness {
  dimension: string;
  text: string;
}

export interface UseEvalStreamReturn {
  phase: "idle" | "evaluating" | "completed";
  stages: EvalStage[];
  dimensions: EvalDimension[];
  verdict: EvalVerdict | null;
  strengths: EvalStrengthWeakness[];
  weaknesses: EvalStrengthWeakness[];
  logLines: string[];
  startEval: (skillName: string, skillPath: string, version?: string) => void;
  stopEval: () => void;
  resetEval: () => void;
  elapsed: number;
  error: string | null;
}

// ── Constants ─────────────────────────────────────────────

const INITIAL_STAGES: EvalStage[] = [
  { id: 1, name: "候选检查", status: "pending" },
  { id: 2, name: "结构分析", status: "pending" },
  { id: 3, name: "触发与路由分析", status: "pending" },
  { id: 4, name: "上下文与复用分析", status: "pending" },
  { id: 5, name: "验证与质量分析", status: "pending" },
  { id: 6, name: "综合评分", status: "pending" },
];

const INITIAL_DIMENSIONS: EvalDimension[] = [
  { name: "触发质量", score: null, reason: "", checks: [] },
  { name: "路由清晰度", score: null, reason: "", checks: [] },
  { name: "上下文效率", score: null, reason: "", checks: [] },
  { name: "复用与确定性", score: null, reason: "", checks: [] },
  { name: "验证强度", score: null, reason: "", checks: [] },
];

// ── Format Marker Regexes ──────────────────────────────────

// [STAGE:序号:阶段名:started/done]
const RE_STAGE = /\[STAGE:(\d+):([^:\]]+):(started|done)\]/g;
// [DIM:维度名:分数:理由]
const RE_DIM = /\[DIM:([^:\]]+):(\d+):([^\]]*)\]/g;
// [CHECK:维度名:检查项描述:pass/fail]
const RE_CHECK = /\[CHECK:([^:\]]+):([^:\]]+):(pass|fail)\]/g;
// [STRENGTH:维度名:描述]
const RE_STRENGTH = /\[STRENGTH:([^:\]]+):([^\]]*)\]/g;
// [WEAKNESS:维度名:描述]
const RE_WEAKNESS = /\[WEAKNESS:([^:\]]+):([^\]]*)\]/g;
// [VERDICT:等级:总分:建议]
const RE_VERDICT = /\[VERDICT:([^:\]]+):(\d+):([^\]]*)\]/g;

// ── Prompt Builder ─────────────────────────────────────────

function buildEvalPrompt(skillPath: string): string {
  return `请使用 /skill-benchmark 对以下 Skill 进行五维度质量评估：
Skill 路径：${skillPath}

评估框架（五维度，每维度 1-5 分）：
1. 触发质量 — 边界精确度，是否能精确识别触发请求
2. 路由清晰度 — agent 是否清楚"接下来该做什么"
3. 上下文效率 — SKILL.md 是否精简，detail 是否正确分层
4. 复用与确定性 — 重复工作是否脚本化，执行结果是否一致
5. 验证强度 — 是否有明确的成功标准

请严格按以下格式输出标记（标记之间可以自由输出分析文字）：
- 每个评估阶段开始/结束：[STAGE:序号:阶段名:started/done]
- 每个维度评分：[DIM:维度名:分数:一句话理由]
- 每个检查项：[CHECK:维度名:检查项描述:pass/fail]
- 优势：[STRENGTH:维度名:描述]
- 问题：[WEAKNESS:维度名:描述]
- 最终判定：[VERDICT:等级:总分:一句话建议]

等级标准：22-25=生产级，17-21=基础扎实，12-16=可用但不稳定，<12=需重做

评估阶段（按顺序执行）：
1. 候选检查 — 验证 SKILL.md 是否存在，文件结构是否合规
2. 结构分析 — 扫描 scripts/、references/、assets/ 目录结构
3. 触发与路由分析 — 分析 description、workflow、decision tree
4. 上下文与复用分析 — 检查 SKILL.md 行数、引用分层、脚本覆盖
5. 验证与质量分析 — 检查 validation 章节、成功标准、测试覆盖
6. 综合评分 — 汇总五维度评分，输出判定和改进建议`;
}

// ── Hook Implementation ────────────────────────────────────

export function useEvalStream(): UseEvalStreamReturn {
  const [phase, setPhase] = useState<"idle" | "evaluating" | "completed">("idle");
  const [stages, setStages] = useState<EvalStage[]>(INITIAL_STAGES);
  const [dimensions, setDimensions] = useState<EvalDimension[]>(INITIAL_DIMENSIONS);
  const [verdict, setVerdict] = useState<EvalVerdict | null>(null);
  const [strengths, setStrengths] = useState<EvalStrengthWeakness[]>([]);
  const [weaknesses, setWeaknesses] = useState<EvalStrengthWeakness[]>([]);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Refs that persist across renders without triggering re-renders
  const abortCtrlRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef<number>(0);
  // Accumulated token text buffer for cross-token marker detection
  const textBufferRef = useRef<string>("");
  // Deduplication: track marker positions already processed
  const seenMarkersRef = useRef<Set<string>>(new Set());
  // Track last log extraction position to avoid duplicate log lines
  const logCursorRef = useRef<number>(0);
  // Accumulate partial line until a newline arrives
  const pendingLineRef = useRef<string>("");
  // Refs mirroring state for use in async save callback (avoids nested setState)
  const dimensionsRef = useRef<EvalDimension[]>(INITIAL_DIMENSIONS);
  const verdictRef = useRef<EvalVerdict | null>(null);
  const strengthsRef = useRef<EvalStrengthWeakness[]>([]);
  const weaknessesRef = useRef<EvalStrengthWeakness[]>([]);
  // Snapshot of current state for use inside async callbacks
  const skillNameRef = useRef<string>("");
  const skillPathRef = useRef<string>("");

  // ── Timer management ────────────────────────────────────

  const startTimer = useCallback(() => {
    startTimeRef.current = Date.now();
    setElapsed(0);
    timerRef.current = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startTimeRef.current) / 1000));
    }, 1000);
  }, []);

  const stopTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // Cleanup timer on unmount
  useEffect(() => {
    return () => {
      stopTimer();
      abortCtrlRef.current?.abort();
    };
  }, [stopTimer]);

  // ── Marker parsing ───────────────────────────────────────

  /**
   * Parse the full text buffer for markers. Uses seenMarkersRef for dedup
   * so each marker is only processed once even if the buffer is re-scanned.
   * Log lines are extracted incrementally via logCursorRef.
   */
  const parseAndApplyMarkers = useCallback((fullText: string) => {
    if (!fullText) return;
    // Collect marker ranges (absolute positions in fullText) for log extraction
    const markerRanges: Array<[number, number]> = [];

    // Helper: check if this match at this position was already processed
    const seen = seenMarkersRef.current;
    const isNew = (type: string, pos: number) => {
      const key = `${type}:${pos}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    };

    // STAGE markers
    {
      const re = new RegExp(RE_STAGE.source, "g");
      let m: RegExpExecArray | null;
      while ((m = re.exec(fullText)) !== null) {
        markerRanges.push([m.index, m.index + m[0].length]);
        if (!isNew("STAGE", m.index)) continue;
        const stageId = parseInt(m[1], 10);
        const stageName = m[2];
        const stageState = m[3] as "started" | "done";
        setStages((prev) =>
          prev.map((s) =>
            s.id === stageId || s.name === stageName
              ? { ...s, name: stageName, status: stageState === "started" ? "running" : "done" }
              : s
          )
        );
      }
    }

    // DIM markers
    {
      const re = new RegExp(RE_DIM.source, "g");
      let m: RegExpExecArray | null;
      while ((m = re.exec(fullText)) !== null) {
        markerRanges.push([m.index, m.index + m[0].length]);
        if (!isNew("DIM", m.index)) continue;
        const dimName = m[1];
        const score = parseInt(m[2], 10);
        const reason = m[3];
        const updateDim = (d: EvalDimension) =>
          d.name === dimName ? { ...d, score, reason } : d;
        setDimensions((prev) => prev.map(updateDim));
        dimensionsRef.current = dimensionsRef.current.map(updateDim);
      }
    }

    // CHECK markers
    {
      const re = new RegExp(RE_CHECK.source, "g");
      let m: RegExpExecArray | null;
      while ((m = re.exec(fullText)) !== null) {
        markerRanges.push([m.index, m.index + m[0].length]);
        if (!isNew("CHECK", m.index)) continue;
        const dimName = m[1];
        const item = m[2];
        const passed = m[3] === "pass";
        const addCheck = (d: EvalDimension) =>
          d.name === dimName ? { ...d, checks: [...d.checks, { item, passed }] } : d;
        setDimensions((prev) => prev.map(addCheck));
        dimensionsRef.current = dimensionsRef.current.map(addCheck);
      }
    }

    // STRENGTH markers
    {
      const re = new RegExp(RE_STRENGTH.source, "g");
      let m: RegExpExecArray | null;
      while ((m = re.exec(fullText)) !== null) {
        markerRanges.push([m.index, m.index + m[0].length]);
        if (!isNew("STRENGTH", m.index)) continue;
        const entry = { dimension: m[1], text: m[2] };
        setStrengths((prev) => [...prev, entry]);
        strengthsRef.current = [...strengthsRef.current, entry];
      }
    }

    // WEAKNESS markers
    {
      const re = new RegExp(RE_WEAKNESS.source, "g");
      let m: RegExpExecArray | null;
      while ((m = re.exec(fullText)) !== null) {
        markerRanges.push([m.index, m.index + m[0].length]);
        if (!isNew("WEAKNESS", m.index)) continue;
        const entry = { dimension: m[1], text: m[2] };
        setWeaknesses((prev) => [...prev, entry]);
        weaknessesRef.current = [...weaknessesRef.current, entry];
      }
    }

    // VERDICT markers
    {
      const re = new RegExp(RE_VERDICT.source, "g");
      let m: RegExpExecArray | null;
      while ((m = re.exec(fullText)) !== null) {
        markerRanges.push([m.index, m.index + m[0].length]);
        if (!isNew("VERDICT", m.index)) continue;
        const v: EvalVerdict = {
          grade: m[1],
          totalScore: parseInt(m[2], 10),
          note: m[3],
        };
        setVerdict(v);
        verdictRef.current = v;
        setPhase("completed");
        stopTimer();
      }
    }

    // Extract non-marker text as log lines (only new text since logCursorRef).
    // Accumulate into pendingLineRef and only emit complete lines on \n.
    const logStart = logCursorRef.current;
    const newText = fullText.slice(logStart);
    if (newText) {
      // Remove marker regions from new text
      const relRanges = markerRanges
        .filter(([, e]) => e > logStart)
        .map(([s, e]) => [Math.max(0, s - logStart), e - logStart] as [number, number])
        .sort((a, b) => a[0] - b[0]);

      let cursor = 0;
      const parts: string[] = [];
      for (const [s, e] of relRanges) {
        if (s > cursor) parts.push(newText.slice(cursor, s));
        cursor = Math.max(cursor, e);
      }
      if (cursor < newText.length) parts.push(newText.slice(cursor));

      const cleanText = parts.join("");
      if (cleanText) {
        // Append to pending line buffer, then split on newlines
        const combined = pendingLineRef.current + cleanText;
        const segments = combined.split("\n");
        // Last segment is incomplete (no trailing \n) — keep in pending
        pendingLineRef.current = segments.pop() ?? "";
        // All other segments are complete lines
        const completeLines = segments.filter((l) => l.trim());
        if (completeLines.length > 0) {
          setLogLines((prev) => [...prev, ...completeLines]);
        }
      }
    }
    logCursorRef.current = fullText.length;
  }, [stopTimer]);

  // ── Main evaluation runner ────────────────────────────────

  const startEval = useCallback(
    (skillName: string, skillPath: string, version?: string) => {
      // Abort any in-progress evaluation
      abortCtrlRef.current?.abort();
      stopTimer();

      skillNameRef.current = skillName;
      skillPathRef.current = skillPath;
      textBufferRef.current = "";
      seenMarkersRef.current = new Set();
      logCursorRef.current = 0;
      pendingLineRef.current = "";
      dimensionsRef.current = INITIAL_DIMENSIONS.map((d) => ({ ...d, checks: [] }));
      verdictRef.current = null;
      strengthsRef.current = [];
      weaknessesRef.current = [];

      // Reset state
      setPhase("evaluating");
      setStages(INITIAL_STAGES.map((s) => ({ ...s })));
      setDimensions(INITIAL_DIMENSIONS.map((d) => ({ ...d, checks: [] })));
      setVerdict(null);
      setStrengths([]);
      setWeaknesses([]);
      setLogLines([]);
      setError(null);
      setElapsed(0);

      startTimer();

      const ctrl = new AbortController();
      abortCtrlRef.current = ctrl;

      const sessionId = `eval-${skillName}-${Date.now()}`;
      const prompt = buildEvalPrompt(skillPath);

      // Run the async generator in a self-contained async IIFE
      (async () => {
        try {
          for await (const event of streamChat(prompt, sessionId, ctrl.signal)) {
            if (ctrl.signal.aborted) break;

            if (event.event === "token") {
              // Accumulate token text
              const token =
                typeof event.data.content === "string"
                  ? event.data.content
                  : typeof event.data.token === "string"
                  ? event.data.token
                  : typeof event.data.text === "string"
                  ? event.data.text
                  : "";

              textBufferRef.current += token;

              // Parse full buffer — dedup via seenMarkersRef prevents reprocessing
              parseAndApplyMarkers(textBufferRef.current);
            } else if (event.event === "done") {
              // Final parse pass
              parseAndApplyMarkers(textBufferRef.current);

              // Flush any remaining pending line
              if (pendingLineRef.current.trim()) {
                const lastLine = pendingLineRef.current.trim();
                setLogLines((prev) => [...prev, lastLine]);
                pendingLineRef.current = "";
              }

              // Save eval result using refs (pure, no side effects in setState)
              if (verdictRef.current) {
                const result: FiveDimEvalResult = {
                  skill_name: skillNameRef.current,
                  timestamp: Date.now() / 1000,
                  total_score: verdictRef.current.totalScore,
                  grade: verdictRef.current.grade,
                  verdict_note: verdictRef.current.note,
                  dimensions: dimensionsRef.current.map((d) => ({
                    name: d.name,
                    score: d.score ?? 0,
                    reason: d.reason,
                    checks: d.checks,
                  })),
                  strengths: strengthsRef.current,
                  weaknesses: weaknessesRef.current,
                  session_id: sessionId,
                };
                saveEvalResult(skillNameRef.current, result, version).catch(
                  (err) => console.error("Failed to save eval result:", err)
                );
              }

              stopTimer();
              setPhase((prev) => (prev === "evaluating" ? "completed" : prev));
              break;
            } else if (event.event === "error") {
              const msg =
                typeof event.data.message === "string"
                  ? event.data.message
                  : "Evaluation error";
              setError(msg);
              setPhase("idle");
              stopTimer();
              break;
            }
          }
        } catch (err) {
          if (!ctrl.signal.aborted) {
            const msg = err instanceof Error ? err.message : String(err);
            setError(msg);
            setPhase("idle");
            stopTimer();
          }
        }
      })();
    },
    [startTimer, stopTimer, parseAndApplyMarkers]
  );

  // ── Stop & Reset ──────────────────────────────────────────

  const stopEval = useCallback(() => {
    abortCtrlRef.current?.abort();
    stopTimer();
    setPhase("idle");
  }, [stopTimer]);

  const resetEval = useCallback(() => {
    abortCtrlRef.current?.abort();
    stopTimer();
    textBufferRef.current = "";
    seenMarkersRef.current = new Set();
    logCursorRef.current = 0;
    pendingLineRef.current = "";
    dimensionsRef.current = INITIAL_DIMENSIONS.map((d) => ({ ...d, checks: [] }));
    verdictRef.current = null;
    strengthsRef.current = [];
    weaknessesRef.current = [];
    setPhase("idle");
    setStages(INITIAL_STAGES.map((s) => ({ ...s })));
    setDimensions(INITIAL_DIMENSIONS.map((d) => ({ ...d, checks: [] })));
    setVerdict(null);
    setStrengths([]);
    setWeaknesses([]);
    setLogLines([]);
    setError(null);
    setElapsed(0);
  }, [stopTimer]);

  return {
    phase,
    stages,
    dimensions,
    verdict,
    strengths,
    weaknesses,
    logLines,
    startEval,
    stopEval,
    resetEval,
    elapsed,
    error,
  };
}
