const API_BASE = "/api/evaluation";

export type DatasetStatus = "draft" | "published" | "archived";
export type ExperimentStatus = "queued" | "syncing" | "running" | "completed" | "failed" | "cancel_requested" | "cancelled";

export interface EvalTurn { role: "system" | "user" | "assistant" | "tool"; content: string; name?: string | null }
export interface EvalInput { message?: string | null; turns: EvalTurn[] }
export interface EvalExpectations {
  exact_output?: string | null;
  reference_answer?: string | null;
  contains_all: string[];
  contains_any: string[];
  excludes: string[];
  required_tools: string[];
  forbidden_tools: string[];
  tool_order: string[];
  max_tool_calls?: number | null;
  required_steps: string[];
  forbidden_actions: string[];
  expected_state: Record<string, unknown>;
  rubric?: string | null;
}
export interface CodeEvaluationSpec {
  schema_version: "1";
  repository: {
    kind: "inline" | "swebench";
    files: Record<string, string>;
    swebench?: {
      dataset_name: string; split: string; instance_id: string; repo: string;
      base_commit: string; version?: string | null; environment_setup_commit?: string | null;
      test_patch: string; fail_to_pass: string[]; pass_to_pass: string[];
    } | null;
  };
  verification: {
    mode: "commands" | "swebench";
    commands: Array<{ command_id: string; command: string; runner: "python_callable_json"; timeout_seconds: number; expected_exit_code: number }>;
    hidden_files: Record<string, string>;
    require_patch: boolean;
  };
}
export interface EvalCase {
  protocol_version: "1.0";
  case_id: string;
  revision_id: string;
  name: string;
  description: string;
  enabled: boolean;
  repetitions: number;
  dimensions: string[];
  input: EvalInput;
  setup: {
    clock?: string | null;
    timezone: string;
    fixtures: unknown[];
    resource_group?: string | null;
    allow_network: boolean;
    allow_side_effects: boolean;
    reproducible: boolean;
  };
  expectations: EvalExpectations;
  code?: CodeEvaluationSpec | null;
  evaluator_bindings: unknown[];
  resolved_evaluator_bindings: unknown[];
  criticality: "normal" | "high" | "critical";
  data_classification: "public" | "internal" | "sensitive" | "restricted";
  tags: string[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}
export interface EvalDataset {
  protocol_version: "1.0";
  dataset_id: string;
  name: string;
  description: string;
  status: DatasetStatus;
  current_version: number;
  current_version_id?: string | null;
  revision: number;
  default_profile: string;
  tags: string[];
  metadata: Record<string, unknown>;
  cases: EvalCase[];
  created_at: string;
  updated_at: string;
}
export interface DatasetValidation {
  valid: boolean;
  reproducible: boolean;
  issues: Array<{ severity: "error" | "warning"; code: string; message: string; case_id?: string | null; path?: string | null }>;
}
export interface LangSmithSettings {
  enabled: boolean;
  endpoint: string;
  project: string;
  workspace_id?: string | null;
  redaction_profile: string;
  request_timeout_seconds: number;
  max_retries: number;
  trace_finalize_timeout_seconds: number;
  projection_timeout_seconds: number;
  api_key_configured: boolean;
  api_key_masked?: string | null;
}
export interface EvalExperiment {
  experiment_id: string;
  name: string;
  dataset_id: string;
  dataset_version: number;
  dataset_version_id: string;
  dataset_content_hash: string;
  profile_id: string;
  status: ExperimentStatus;
  candidate: { name: string; llm_model_id?: string | null; credential_name?: string | null; fingerprint?: string | null; fingerprint_status: "partial" | "complete" };
  execution: { repetitions: number; max_concurrency: number; timeout_seconds: number; preserve_workspaces: boolean };
  summary: Record<string, unknown>;
  error?: { code: string; message: string } | null;
  remote_url?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}
export interface EvalExperimentResultRow {
  case_id: string;
  repetition: number;
  attempt_id: string;
  attempt_status: "queued" | "running" | "completed" | "failed" | "cancelled";
  error?: { code?: string; message?: string; retryable?: boolean } | null;
  created_at?: string | null;
  updated_at?: string | null;
  latency_ms?: number | null;
  result?: Record<string, unknown> | null;
}

async function request<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { cache: "no-store", ...init, signal });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") message = payload.detail;
      else if (Array.isArray(payload.detail)) {
        message = payload.detail.map((item: { loc?: unknown[]; msg?: string }) => `${item.loc?.slice(1).join(".") || "字段"}: ${item.msg || "无效"}`).join("；");
      }
    } catch { /* use status */ }
    throw new Error(message);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

const json = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export async function listEvaluationDatasets(signal?: AbortSignal) {
  return request<{ items: EvalDataset[]; total: number }>("/datasets", undefined, signal);
}
export async function getEvaluationDataset(id: string, signal?: AbortSignal) {
  return request<EvalDataset>(`/datasets/${encodeURIComponent(id)}`, undefined, signal);
}
export async function listEvaluationDatasetVersions(id: string, signal?: AbortSignal) {
  return request<{ items: EvalDataset[]; total: number }>(`/datasets/${encodeURIComponent(id)}/versions`, undefined, signal);
}
export async function createEvaluationDataset(body: { name: string; description?: string; default_profile?: string; tags?: string[] }) {
  return request<EvalDataset>("/datasets", json(body));
}
export async function importSWEbenchDataset(body: { dataset_name?: string; split?: string; offset?: number; limit?: number; name?: string; content?: string }) {
  return request<EvalDataset>("/datasets/import/swebench", json(body));
}
export async function importEvaluationDataset(content: string, format: "bundle" | "jsonl" | "csv", name?: string) {
  return request<EvalDataset>("/datasets/import", json({ content, format, name }));
}
export async function updateEvaluationDataset(id: string, body: Record<string, unknown>) {
  return request<EvalDataset>(`/datasets/${encodeURIComponent(id)}`, { ...json(body), method: "PATCH" });
}
export async function addEvaluationCase(datasetId: string, revision: number, evalCase: EvalCase) {
  return request<EvalDataset>(`/datasets/${encodeURIComponent(datasetId)}/cases`, json({ expected_revision: revision, case: evalCase }));
}
export async function updateEvaluationCase(datasetId: string, caseId: string, revision: number, evalCase: EvalCase) {
  return request<EvalDataset>(`/datasets/${encodeURIComponent(datasetId)}/cases/${encodeURIComponent(caseId)}`, { ...json({ expected_revision: revision, case: evalCase }), method: "PATCH" });
}
export async function deleteEvaluationCase(datasetId: string, caseId: string, revision: number) {
  return request<void>(`/datasets/${encodeURIComponent(datasetId)}/cases/${encodeURIComponent(caseId)}?expected_revision=${revision}`, { method: "DELETE" });
}
export async function validateEvaluationDataset(id: string) {
  return request<DatasetValidation>(`/datasets/${encodeURIComponent(id)}/validate`, { method: "POST" });
}
export async function publishEvaluationDataset(id: string, revision: number) {
  return request<{ dataset: EvalDataset; version_id: string; checksum: string }>(`/datasets/${encodeURIComponent(id)}/publish`, json({ expected_revision: revision }));
}
export async function reopenEvaluationDataset(id: string, revision: number) {
  return request<EvalDataset>(`/datasets/${encodeURIComponent(id)}/versions`, json({ expected_revision: revision }));
}
export async function archiveEvaluationDataset(id: string, revision: number) {
  return request<EvalDataset>(`/datasets/${encodeURIComponent(id)}/archive`, json({ expected_revision: revision }));
}
export async function syncEvaluationDataset(id: string, version?: number) {
  const suffix = version ? `?version=${version}` : "";
  return request<Record<string, unknown>>(`/datasets/${encodeURIComponent(id)}/sync/langsmith${suffix}`, { method: "POST" });
}
export function evaluationDatasetExportUrl(id: string, format: "bundle" | "jsonl" | "csv" = "bundle") {
  return `${API_BASE}/datasets/${encodeURIComponent(id)}/export?format=${format}`;
}
export function frozenSWEbenchDatasetExportUrl(id: string, version?: number) {
  return `${API_BASE}/datasets/${encodeURIComponent(id)}/export/swebench${version ? `?version=${version}` : ""}`;
}
export async function getLangSmithSettings(signal?: AbortSignal) {
  return request<LangSmithSettings>("/settings/langsmith", undefined, signal);
}
export async function saveLangSmithSettings(body: Record<string, unknown>) {
  return request<LangSmithSettings>("/settings/langsmith", { ...json(body), method: "PUT" });
}
export async function testLangSmithConnection() {
  return request<Record<string, unknown>>("/settings/langsmith/test", { method: "POST" });
}
export async function listEvaluationExperiments(signal?: AbortSignal) {
  return request<{ items: EvalExperiment[]; total: number }>("/experiments", undefined, signal);
}
export async function getEvaluationExperimentResults(id: string, signal?: AbortSignal) {
  return request<{ items: EvalExperimentResultRow[]; total: number }>(
    `/experiments/${encodeURIComponent(id)}/results`,
    undefined,
    signal,
  );
}
export async function createEvaluationExperiment(body: Record<string, unknown>) {
  return request<EvalExperiment>("/experiments", json(body));
}
export async function cancelEvaluationExperiment(id: string) {
  return request<EvalExperiment>(`/experiments/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}
export async function deleteEvaluationExperiment(id: string) {
  return request<void>(`/experiments/${encodeURIComponent(id)}`, { method: "DELETE" });
}
export async function retryEvaluationExperiment(id: string) {
  return request<EvalExperiment>(`/experiments/${encodeURIComponent(id)}/retry`, { method: "POST" });
}
export async function rerunSWEbenchVerifier(id: string) {
  return request<EvalExperiment>(`/experiments/${encodeURIComponent(id)}/verify/swebench`, { method: "POST" });
}
export async function resumeMissingSWEbenchCases(id: string) {
  return request<EvalExperiment>(`/experiments/${encodeURIComponent(id)}/resume/swebench`, { method: "POST" });
}
export async function syncEvaluationExperiment(id: string) {
  return request<EvalExperiment>(`/experiments/${encodeURIComponent(id)}/sync/langsmith`, { method: "POST" });
}
export function swebenchPredictionExportUrl(id: string) {
  return `${API_BASE}/experiments/${encodeURIComponent(id)}/export/swebench`;
}
