import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import { formatUsageSummary, mergeUsageSummaries, normalizeUsageSummary } from "./usageSummary.ts";

test("normalizes and formats the provider-backed footer without cost", () => {
  const summary = normalizeUsageSummary({
    run_id: "run-1",
    rounds: 2,
    tool_calls: 6,
    steps: 8,
    last_model_duration_ms: 1600,
    last_model_tokens_per_second: 361,
    input_tokens: 48_100,
    output_tokens: 2_700,
    total_tokens: 50_800,
    cache_read_tokens: 38_480,
    cache_hit_rate: 80,
    observed_calls: 2,
    measured_calls: 2,
    measured: true,
    partial: false,
  });

  assert.ok(summary);
  assert.equal(
    formatUsageSummary(summary),
    "2 次模型调用 · 8 步  |  上轮 1.6s · 361 tok/s  |  缓存命中 80%  |  输入 48.1K · 输出 2.7K",
  );
  assert.equal(formatUsageSummary(summary).includes("$"), false);
});

test("merges multiple Runs that the Goal history renders as one assistant turn", () => {
  const first = normalizeUsageSummary({
    rounds: 1,
    tool_calls: 1,
    steps: 2,
    input_tokens: 100,
    output_tokens: 20,
    cache_read_tokens: 80,
    total_tokens: 120,
    measured: true,
  });
  const second = normalizeUsageSummary({
    rounds: 2,
    tool_calls: 2,
    steps: 4,
    input_tokens: 100,
    output_tokens: 30,
    cache_read_tokens: 20,
    total_tokens: 130,
    measured: true,
  });

  const merged = mergeUsageSummaries(first, second);
  assert.ok(merged);
  assert.equal(merged.rounds, 3);
  assert.equal(merged.steps, 6);
  assert.equal(merged.cacheHitRate, 50);
});

test("formats observed calls instead of an internal Goal retry ceiling", () => {
  const summary = normalizeUsageSummary({
    rounds: 20,
    tool_calls: 19,
    steps: 39,
    observed_calls: 11,
    measured: false,
  });

  assert.ok(summary);
  assert.equal(formatUsageSummary(summary), "11 次模型调用 · 30 步");
});
