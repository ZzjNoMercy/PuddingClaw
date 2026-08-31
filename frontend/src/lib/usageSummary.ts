export interface UsageSummary {
  runId?: string;
  queryId?: string;
  rounds: number;
  toolCalls: number;
  steps: number;
  runDurationMs: number;
  lastModelDurationMs?: number;
  lastModelTokensPerSecond?: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  reasoningTokens: number;
  cacheHitRate?: number;
  observedCalls: number;
  measuredCalls: number;
  measured: boolean;
  partial: boolean;
}

type UsageWireValue = Record<string, unknown>;

function nonNegativeNumber(value: unknown): number {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

function optionalNonNegativeNumber(value: unknown): number | undefined {
  if (value === null || value === undefined || value === "") return undefined;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : undefined;
}

/** Convert the backend's snake_case JSON contract into the UI type. */
export function normalizeUsageSummary(value: unknown): UsageSummary | undefined {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as UsageWireValue;
  return {
    runId: String(raw.run_id || raw.runId || "") || undefined,
    queryId: String(raw.query_id || raw.queryId || "") || undefined,
    rounds: nonNegativeNumber(raw.rounds),
    toolCalls: nonNegativeNumber(raw.tool_calls ?? raw.toolCalls),
    steps: nonNegativeNumber(raw.steps),
    runDurationMs: nonNegativeNumber(raw.run_duration_ms ?? raw.runDurationMs),
    lastModelDurationMs: optionalNonNegativeNumber(
      raw.last_model_duration_ms ?? raw.lastModelDurationMs,
    ),
    lastModelTokensPerSecond: optionalNonNegativeNumber(
      raw.last_model_tokens_per_second ?? raw.lastModelTokensPerSecond,
    ),
    inputTokens: nonNegativeNumber(raw.input_tokens ?? raw.inputTokens),
    outputTokens: nonNegativeNumber(raw.output_tokens ?? raw.outputTokens),
    totalTokens: nonNegativeNumber(raw.total_tokens ?? raw.totalTokens),
    cacheReadTokens: nonNegativeNumber(raw.cache_read_tokens ?? raw.cacheReadTokens),
    cacheCreationTokens: nonNegativeNumber(
      raw.cache_creation_tokens ?? raw.cacheCreationTokens,
    ),
    reasoningTokens: nonNegativeNumber(raw.reasoning_tokens ?? raw.reasoningTokens),
    cacheHitRate: optionalNonNegativeNumber(raw.cache_hit_rate ?? raw.cacheHitRate),
    observedCalls: nonNegativeNumber(raw.observed_calls ?? raw.observedCalls),
    measuredCalls: nonNegativeNumber(raw.measured_calls ?? raw.measuredCalls),
    measured: Boolean(raw.measured),
    partial: Boolean(raw.partial),
  };
}

/** Goal history can visually merge multiple Runs; merge their footer facts too. */
export function mergeUsageSummaries(
  previous: UsageSummary | undefined,
  next: UsageSummary | undefined,
): UsageSummary | undefined {
  if (!previous) return next;
  if (!next) return previous;
  const inputTokens = previous.inputTokens + next.inputTokens;
  const cacheReadTokens = previous.cacheReadTokens + next.cacheReadTokens;
  return {
    runId: next.runId || previous.runId,
    queryId: next.queryId || previous.queryId,
    rounds: previous.rounds + next.rounds,
    toolCalls: previous.toolCalls + next.toolCalls,
    steps: previous.steps + next.steps,
    runDurationMs: previous.runDurationMs + next.runDurationMs,
    lastModelDurationMs: next.lastModelDurationMs ?? previous.lastModelDurationMs,
    lastModelTokensPerSecond:
      next.lastModelTokensPerSecond ?? previous.lastModelTokensPerSecond,
    inputTokens,
    outputTokens: previous.outputTokens + next.outputTokens,
    totalTokens: previous.totalTokens + next.totalTokens,
    cacheReadTokens,
    cacheCreationTokens: previous.cacheCreationTokens + next.cacheCreationTokens,
    reasoningTokens: previous.reasoningTokens + next.reasoningTokens,
    cacheHitRate: inputTokens > 0 ? (cacheReadTokens / inputTokens) * 100 : undefined,
    observedCalls: previous.observedCalls + next.observedCalls,
    measuredCalls: previous.measuredCalls + next.measuredCalls,
    measured: previous.measured || next.measured,
    partial: previous.partial || next.partial,
  };
}

function compactNumber(value: number): string {
  if (value < 1000) return String(Math.round(value));
  const units = ["K", "M", "B"];
  let scaled = value;
  let unit = units[0];
  for (const candidate of units) {
    scaled /= 1000;
    unit = candidate;
    if (scaled < 1000) break;
  }
  const precision = scaled >= 100 ? 0 : 1;
  return `${scaled.toFixed(precision).replace(/\.0$/, "")}${unit}`;
}

function durationSeconds(milliseconds: number): string {
  const seconds = milliseconds / 1000;
  return `${seconds >= 10 ? seconds.toFixed(0) : seconds.toFixed(1).replace(/\.0$/, "")}s`;
}

export function formatUsageSummary(summary: UsageSummary): string {
  const groups: string[] = [];
  // `rounds` can contain an internal Goal retry ceiling.  The footer reports
  // observed model calls and actual tool work instead of surfacing that limit
  // as if it were executed work.
  const modelCalls = summary.observedCalls || summary.rounds;
  const observedSteps = modelCalls + summary.toolCalls;
  groups.push(
    `${compactNumber(modelCalls)} 次模型调用 · ${compactNumber(observedSteps)} 步`,
  );
  if (summary.lastModelDurationMs !== undefined) {
    let lastModel = `上轮 ${durationSeconds(summary.lastModelDurationMs)}`;
    if (summary.lastModelTokensPerSecond !== undefined) {
      lastModel += ` · ${compactNumber(summary.lastModelTokensPerSecond)} tok/s`;
    }
    groups.push(lastModel);
  }
  if (summary.measured && summary.cacheHitRate !== undefined) {
    groups.push(`缓存命中 ${Math.round(summary.cacheHitRate)}%`);
  }
  if (summary.measured) {
    groups.push(
      `输入 ${compactNumber(summary.inputTokens)} · 输出 ${compactNumber(summary.outputTokens)}`,
    );
  }
  if (summary.partial) groups.push("部分用量未返回");
  return groups.join("  |  ");
}
