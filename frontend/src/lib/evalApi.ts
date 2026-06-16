/**
 * Eval API client for PuddingClaw — Skills evaluation review.
 */

const API_BASE =
  typeof window !== "undefined"
    ? `http://${window.location.hostname}:9100/api`
    : "http://localhost:9100/api";

// ── Types ────────────────────────────────────────────────

export interface EvalIteration {
  id: string;
  timestamp: number;
  eval_count: number;
  has_benchmark: boolean;
  overall_pass_rate: number | null;
}

export interface BenchmarkAssertion {
  id?: string;
  description?: string;
  passed?: boolean;
  pass?: boolean;
  evidence?: string;
  expected?: string;
  actual?: string;
}

export interface BenchmarkData {
  pass_rate?: number;
  total_time_s?: number;
  total_tokens?: number;
  baseline_pass_rate?: number;
  baseline_time_s?: number;
  baseline_tokens?: number;
  results?: BenchmarkAssertion[];
  analyst_notes?: string[];
  [key: string]: unknown;
}

export interface GradingData {
  score?: number;
  max_score?: number;
  assertions?: BenchmarkAssertion[];
  notes?: string;
  [key: string]: unknown;
}

export interface FeedbackPayload {
  verdict: "approve" | "reject" | "needs_work";
  notes: string;
  tags: string[];
}

// ── API Functions ────────────────────────────────────────

export async function listEvalIterations(
  skillName: string
): Promise<EvalIteration[]> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/evals`
  );
  if (!resp.ok) throw new Error(`Failed to list evals: ${resp.status}`);
  const data = await resp.json();
  return data.iterations ?? [];
}

export async function getBenchmark(
  skillName: string,
  iterationId: string
): Promise<BenchmarkData> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/evals/${encodeURIComponent(iterationId)}/benchmark`
  );
  if (!resp.ok) {
    if (resp.status === 404) return {};
    throw new Error(`Failed to get benchmark: ${resp.status}`);
  }
  return resp.json();
}

export async function getGrading(
  skillName: string,
  iterationId: string,
  evalId: string
): Promise<GradingData> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/evals/${encodeURIComponent(iterationId)}/${encodeURIComponent(evalId)}/grading`
  );
  if (!resp.ok) {
    if (resp.status === 404) return {};
    throw new Error(`Failed to get grading: ${resp.status}`);
  }
  return resp.json();
}

export async function saveFeedback(
  skillName: string,
  iterationId: string,
  feedback: FeedbackPayload
): Promise<{ success: boolean }> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/evals/${encodeURIComponent(iterationId)}/feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(feedback),
    }
  );
  if (!resp.ok) throw new Error(`Failed to save feedback: ${resp.status}`);
  return resp.json();
}

// ── Five-Dimension Eval Result ────────────────────────────

/** Five-dimension evaluation result persisted to skills/{name}/evals/five-dim-result.json */
export interface FiveDimEvalResult {
  skill_name: string;
  timestamp: number;
  total_score: number;
  grade: string;
  verdict_note: string;
  dimensions: Array<{
    name: string;
    score: number;
    reason: string;
    checks: Array<{ item: string; passed: boolean }>;
  }>;
  strengths: Array<{ dimension: string; text: string }>;
  weaknesses: Array<{ dimension: string; text: string }>;
  session_id: string;
}

/** Save five-dimension evaluation result to the backend. */
export async function saveEvalResult(
  skillName: string,
  result: FiveDimEvalResult,
  version?: string
): Promise<{ success: boolean }> {
  const versionParam = version && version !== "current" ? `?version=${encodeURIComponent(version)}` : "";
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/eval-result${versionParam}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result),
    }
  );
  if (!resp.ok) throw new Error(`Failed to save eval result: ${resp.status}`);
  return resp.json();
}

/** Read five-dimension evaluation result from the backend. Returns null if not found. */
export async function getEvalResult(
  skillName: string,
  version?: string
): Promise<FiveDimEvalResult | null> {
  const versionParam = version && version !== "current" ? `?version=${encodeURIComponent(version)}` : "";
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/eval-result${versionParam}`
  );
  if (!resp.ok) {
    if (resp.status === 404) return null;
    throw new Error(`Failed to get eval result: ${resp.status}`);
  }
  return resp.json();
}

/** Summary of an eval result for a specific version. */
export interface EvalResultSummary {
  version: string;
  total_score: number;
  grade: string;
  timestamp: number;
}

/** List all eval results across current and versioned snapshots. */
export async function listEvalResults(
  skillName: string
): Promise<EvalResultSummary[]> {
  const resp = await fetch(
    `${API_BASE}/skills/${encodeURIComponent(skillName)}/eval-results-list`
  );
  if (!resp.ok) throw new Error(`Failed to list eval results: ${resp.status}`);
  const data = await resp.json();
  return data.results ?? [];
}
