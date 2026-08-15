/**
 * API client for PuddingClaw backend.
 * Custom SSE parser for POST requests (native EventSource only supports GET).
 */

const API_BASE = "/api";
const DIRECT_BACKEND_API_BASE =
  process.env.NEXT_PUBLIC_BACKEND_API_BASE ||
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "http://localhost:8888/api";

function apiErrorMessage(text: string, fallback: string): string {
  if (!text) return fallback;
  try {
    const payload = JSON.parse(text) as { detail?: unknown; message?: unknown };
    const detail = payload.detail ?? payload.message;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => {
          if (!item || typeof item !== "object") return "";
          const issue = item as { loc?: unknown; msg?: unknown };
          const location = Array.isArray(issue.loc) ? issue.loc.slice(1).join(".") : "";
          const message = typeof issue.msg === "string" ? issue.msg : "";
          return [location, message].filter(Boolean).join(": ");
        })
        .filter(Boolean);
      if (messages.length) return messages.join("；");
    }
    if (detail && typeof detail === "object" && "message" in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return message;
    }
  } catch {
    // keep raw text below
  }
  return text;
}

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit = {}, timeoutMs = 5000): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, {
      ...init,
      signal: init.signal ?? controller.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("状态刷新超时，后台任务仍会继续处理。");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export type SkillPlanStatus = "prepared" | "committed" | "cancelled" | "expired";

export interface SkillPlan {
  plan_id: string;
  plan_sha256: string;
  action: "install" | "update";
  skill_name: string;
  source: string;
  ref?: string;
  subpath?: string;
  created_at: number;
  expires_at: number;
  status: SkillPlanStatus;
  phase: "awaiting_confirmation" | "installed" | "cancelled" | "expired";
  requires_confirmation: boolean;
  installed: boolean;
  ui_commit_supported: boolean;
  diff?: {
    added?: string[];
    changed?: string[];
    removed?: string[];
    summary?: string;
  };
  staged_metadata?: Record<string, unknown>;
  installed_path?: string;
  installed_sha256?: string;
}

interface SkillPlanResponse {
  session_id: string;
  plan: SkillPlan;
  idempotent?: boolean;
  permission_recorded?: boolean;
}

export async function getSkillPlan(sessionId: string, planId: string): Promise<SkillPlan> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/skill-plans/${encodeURIComponent(planId)}`,
    { cache: "no-store" },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `加载 Skill 计划失败：${response.status}`));
  return (JSON.parse(text) as SkillPlanResponse).plan;
}

export async function commitSkillPlan(
  sessionId: string,
  planId: string,
  planSha256: string,
): Promise<SkillPlan> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/skill-plans/${encodeURIComponent(planId)}/commit`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_sha256: planSha256 }),
    },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `提交 Skill 计划失败：${response.status}`));
  return (JSON.parse(text) as SkillPlanResponse).plan;
}

export async function cancelSkillPlan(
  sessionId: string,
  planId: string,
  planSha256: string,
): Promise<SkillPlan> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/skill-plans/${encodeURIComponent(planId)}/cancel`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_sha256: planSha256 }),
    },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `取消 Skill 计划失败：${response.status}`));
  return (JSON.parse(text) as SkillPlanResponse).plan;
}

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
}

export type GoalStatus =
  | "active"
  | "paused"
  | "blocked"
  | "completed"
  | "cancelled"
  | "budget_exceeded";

export type RunStatus =
  | "preparing"
  | "running"
  | "waiting_hitl"
  | "evaluating"
  | "completed"
  | "cancelled"
  | "failed"
  | "blocked"
  | "budget_exceeded"
  | "verification_failed";

export interface VerificationCriterion {
  id: string;
  statement: string;
  source: string;
  verifier: string;
  required: boolean;
  evidence_scope: "run_only" | "goal_inheritable" | "artifact_bound" | "freshness_bound";
}

export interface RunVerificationContract {
  contract_id: string;
  version: string;
  task_type: string;
  criteria: VerificationCriterion[];
  rubric: string;
  verification_packs?: string[];
  activation_reasons?: Record<string, string[]>;
  browser_e2e_required?: boolean;
  base_contract_id?: string | null;
  created_at: number;
}

export interface RunTaskProfile {
  primary_intent: string;
  intents: string[];
  initial_packs: string[];
  available_context_refs: string[];
  reasons: string[];
}

export interface VerificationActivation {
  activation_id: string;
  run_id: string;
  query_id: string;
  tool_call_id: string;
  tool_name: string;
  pack: string;
  source: string;
  status: string;
  evidence_refs: Array<Record<string, unknown>>;
  created_at: number;
}

export interface CriterionEvaluation {
  criterion_id: string;
  name: string;
  passed: boolean | null;
  verifier: string;
  evidence: Array<Record<string, unknown>>;
  gap?: string | null;
  failure_kind?: "task_gap" | "infrastructure_error" | null;
}

export type VerificationStatus =
  | "not_required"
  | "pending"
  | "evaluating"
  | "satisfied"
  | "needs_revision"
  | "failed"
  | "max_iterations_reached"
  | "verification_incomplete"
  | "grader_error"
  | "infrastructure_error"
  | "budget_exceeded";

export interface RubricEvaluationReport {
  report_id: string;
  run_id: string;
  status: string;
  contract_id?: string | null;
  contract_version?: string | null;
  evaluations: CriterionEvaluation[];
  gaps: string[];
  explanation: string;
  iteration_count: number;
  verification_scope?: "run" | "goal_aggregate";
  supporting_run_ids?: string[];
  goal_revision?: number | null;
  accepted_for_goal_revision?: boolean | null;
  created_at: number;
}

export interface HarnessRun {
  run_id: string;
  query_id: string;
  session_id: string;
  objective: string;
  run_kind?: "goal_execution" | "goal_inspection" | "standalone";
  goal_id?: string | null;
  context_goal_id?: string | null;
  context_goal_revision?: number | null;
  goal_revision?: number | null;
  goal_turn_intent?: "inspect_goal" | "continue_goal" | "revise_goal" | "control_goal" | "standalone_task" | "clarify" | null;
  verification_enabled?: boolean;
  task_profile?: RunTaskProfile;
  status: RunStatus;
  outcome?: string | null;
  declared_verification_contract?: RunVerificationContract | null;
  verification_contract?: RunVerificationContract | null;
  verification_activations?: VerificationActivation[];
  verification_report?: RubricEvaluationReport | null;
  delegation_contracts?: Array<Record<string, unknown>>;
  delegation_results?: Array<Record<string, unknown>>;
  delegation_events?: Array<{
    type?: string;
    status?: string;
    objective?: string;
    tool?: string;
    subagent_run_id?: string;
    timestamp?: number;
  }>;
  model_call_count: number;
  budget_exhaustion_reason?: string | null;
  error?: string | null;
  created_at: number;
  updated_at: number;
  completed_at?: number | null;
}

export interface HarnessGoal {
  goal_id: string;
  session_id: string;
  objective: string;
  objective_revision?: number;
  revisions?: Array<{
    revision: number;
    objective: string;
    contract_id?: string | null;
    created_at: number;
  }>;
  pending_revision?: boolean;
  status: GoalStatus;
  requested_status?: GoalStatus | null;
  current_run_id?: string | null;
  run_ids: string[];
  completion_policy?: "standard" | "rubric";
  latest_completion_request_id?: string | null;
  gaps: string[];
  control_notices?: string[];
  latest_verification_report_id?: string | null;
  latest_goal_decision?: {
    decision_id: string;
    goal_id: string;
    objective_revision: number;
    status: VerificationStatus;
    accepted?: boolean;
    supporting_run_ids: string[];
    criterion_provenance?: Array<Record<string, unknown>>;
    evidence_ref_count: number;
    gaps: string[];
    accepted_run_id?: string | null;
    report_id?: string | null;
    created_at: number;
  } | null;
  round: number;
  max_rounds: number;
  model_call_count: number;
  budget_exhaustion_reason?: string | null;
  created_at: number;
  updated_at: number;
  completed_at?: number | null;
}

export interface SessionHarnessState {
  session_id: string;
  runs: Record<string, HarnessRun>;
  run_order: string[];
  latest_run_id?: string | null;
  goals: Record<string, HarnessGoal>;
  goal_order: string[];
  active_goal_id?: string | null;
}

export interface ToolContextJobStatus {
  id?: string;
  status:
    | "idle"
    | "pending"
    | "running"
    | "completed"
    | "completed_with_errors"
    | "failed"
    | "expired";
  completed_count?: number;
  failed_count?: number;
  error?: string;
  revision?: number;
}

export async function getToolContextJobStatus(sessionId: string): Promise<ToolContextJobStatus> {
  const response = await fetchWithTimeout(
    `${API_BASE}/agent/tool-context/status/${encodeURIComponent(sessionId)}`,
    { cache: "no-store" },
  );
  if (!response.ok) throw new Error(`Failed to get Tool Context status: ${response.status}`);
  return response.json();
}

export interface AgentAttachment {
  type: "image" | "pdf" | "spreadsheet" | "markdown" | "text" | "document" | "file";
  id?: string;
  name?: string;
  mime_type?: string;
  path?: string;
  size?: number;
  source?: "upload" | "paste" | "generated";
  sha256?: string;
  derived_from?: string;
  created_by_run_id?: string;
  created_by_query_id?: string;
  created_by_tool_call_id?: string;
  created_by_goal_id?: string;
  created_by_goal_revision?: number;
  download_url?: string;
  preview_url?: string;
  preview_mime_type?: string;
  width?: number;
  height?: number;
  created_at?: number;
}

export interface KnowledgeDocument {
  id: string;
  knowledge_base_id: string;
  title: string;
  source_type: string;
  source_path: string;
  storage_path: string;
  virtual_path: string;
  mime_type: string;
  content_sha256: string;
  size_bytes: number;
  status: string;
  publish_targets: string[];
  metadata?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface KnowledgeDirectoryFile {
  name: string;
  extension: string;
  virtual_path: string;
  storage_path: string;
  size_bytes: number;
  modified_at: string;
}

export interface KnowledgeTreeNode {
  name: string;
  type: "directory" | "file";
  virtual_path: string;
  storage_path: string;
  extension?: string;
  size_bytes?: number;
  modified_at?: string;
  child_count?: number;
  file_count?: number;
  truncated?: boolean;
  children?: KnowledgeTreeNode[];
}

export interface KnowledgeFilePreview {
  name: string;
  extension: string;
  virtual_path: string;
  storage_path: string;
  size_bytes: number;
  modified_at: string;
  preview_type: "text" | "unsupported";
  content: string;
  truncated: boolean;
  message?: string | null;
  llm_wiki_raw?: {
    available: boolean;
    snapshot?: Record<string, unknown> | null;
    latest_snapshot?: Record<string, unknown> | null;
    changed_since_snapshot: boolean;
    error?: string;
  };
}

export interface KnowledgeStatus {
  enabled: boolean;
  database: {
    configured: boolean;
    provider: string;
    url: string;
    configured_by?: string;
    environment_override?: boolean;
    mode?: string;
    configuration_hint?: string;
    healthy: boolean;
    last_error?: string | null;
  };
  local_markdown: {
    enabled: boolean;
    physical_path: string;
    originals_path?: string;
    configured_by?: string;
    environment_override?: boolean;
    deepagents_virtual_path: string;
  };
  vector: {
    enabled: boolean;
    provider?: string | null;
    note?: string;
    multimodal?: {
      enabled: boolean;
      vector_store: string;
      milvus_uri: string;
      text_collection: string;
      image_collection: string;
      overwrite?: boolean;
    };
  };
  parser: {
    mineru_optional: boolean;
    note?: string;
  };
  markdown_search?: {
    enabled: boolean;
    glob_endpoint: string;
    grep_endpoint: string;
    deepagents_virtual_path: string;
  };
}

export type GbrainPrimitive = "entity" | "media" | "temporal" | "annotation" | "concept";
export type GbrainAggregator = "scalar_brier" | "weighted_brier" | "count_based" | "cluster_summary";
export type GbrainSubtypeField = "subtype" | "legacy_type" | "origin" | "format" | "kind" | "period" | "domain";
export type GbrainResolver =
  | "frontmatter"
  | "body_first_link"
  | "slug"
  | "body_excerpt"
  | { frontmatter_field: string };

export interface GbrainExtractableSpec {
  prompt_template?: string;
  fixture_corpus?: string;
  eval_dimensions: string[];
  benchmark_min_recall?: number;
  verifier_path?: string;
}

export interface GbrainPageSubtype {
  name: string;
  when: {
    path_pattern?: string;
    frontmatter_field?: string;
    frontmatter_value?: string | number | boolean;
  };
}

export interface GbrainPageType {
  name: string;
  primitive: GbrainPrimitive;
  path_prefixes: string[];
  aliases: string[];
  extractable: boolean | GbrainExtractableSpec;
  expert_routing: boolean;
  subtypes?: GbrainPageSubtype[];
}

export interface GbrainLinkType {
  name: string;
  inverse?: string;
  inference?: {
    regex?: string;
    page_type?: string;
    target_type?: string;
  };
}

export interface GbrainFrontmatterLink {
  page_type: string;
  fields: string[];
  link_type: string;
}

export type GbrainMappingRule =
  | {
      kind: "retype";
      from_type: string;
      to_type: string;
      subtype?: string;
      subtype_field: GbrainSubtypeField;
      path_filter?: string;
    }
  | {
      kind: "page_to_link";
      from_type: string;
      link_type: string;
      source_slug_from: GbrainResolver;
      target_slug_from: GbrainResolver;
      inverse?: string;
      preserve_notes?: boolean;
    }
  | {
      kind: "page_to_alias";
      from_type: string;
      canonical_from: GbrainResolver;
      alias_slug_from: GbrainResolver;
      notes_from?: GbrainResolver;
    };

export interface GbrainSchemaPackManifest {
  api_version: "gbrain-schema-pack-v1";
  name: string;
  version: string;
  description: string;
  author?: string;
  license?: string;
  homepage?: string;
  gbrain_min_version: string;
  extends?: string | null;
  borrow_from: Array<{ pack: string; types?: string[]; link_types?: string[] }>;
  page_types: GbrainPageType[];
  link_types: GbrainLinkType[];
  frontmatter_links: GbrainFrontmatterLink[];
  takes_kinds: string[];
  enrichable_types: Array<{ type: string; rubric?: string }>;
  filing_rules: Array<{ kind: string; directory: string; examples: string[]; description?: string }>;
  phases?: string[];
  calibration_domains?: Array<{ name: string; aggregator: GbrainAggregator; page_types: string[] }>;
  migration_from?: { pack: string; version: string };
  mapping_rules?: GbrainMappingRule[];
}

export interface GbrainSchemaCatalogPack {
  name: string;
  version: string;
  description: string;
  gbrain_min_version: string;
  extends?: string | null;
  borrow_from: GbrainSchemaPackManifest["borrow_from"];
  manifest_sha256: string;
  page_type_count: number;
  link_type_count: number;
  manifest: GbrainSchemaPackManifest;
  raw_yaml: string;
  recommended: boolean;
  legacy: boolean;
}

export interface GbrainSchemaCatalog {
  source_dir: string;
  packs: GbrainSchemaCatalogPack[];
  count: number;
}

export interface BrainSchemaBundle {
  initialized: true;
  brain_root: string;
  custom: {
    path: string;
    manifest: GbrainSchemaPackManifest;
    raw_yaml: string;
    manifest_sha256: string;
  };
  parent: {
    name: string;
    version: string;
    manifest_sha256: string;
  } | null;
  brain_schema: {
    path: string;
    document: Record<string, unknown>;
    raw_yaml: string;
    sha256: string;
  };
  agents: {
    path: string;
    raw_markdown: string;
    sha256: string;
  };
  resolved: {
    manifest: GbrainSchemaPackManifest;
    raw_yaml: string;
    sha256: string;
  };
  bundle_hash: string;
}

export interface BrainSchemaPreview {
  valid: true;
  custom: {
    manifest: GbrainSchemaPackManifest;
    raw_yaml: string;
    manifest_sha256: string;
  };
  resolved: BrainSchemaBundle["resolved"];
  gbrain_validation: Array<Record<string, unknown>>;
  validation_mode?: "structural" | "official";
}

export interface LlmWikiWorkspaceStatus {
  brain_root: string;
  bundle_hash: string;
  schema_version: string;
  agents: { path: string; sha256: string; content: string };
  raw: Array<{
    source_id?: string;
    asset_id?: string;
    title?: string;
    snapshot_path: string;
    sha256?: string;
    size_bytes?: number;
    created_at?: string;
    integrity: string;
    compiled: boolean;
    compiled_at?: string | null;
    compiled_pages: string[];
    compiled_job_ids: string[];
  }>;
  wiki: Array<{
    slug: string;
    title: string;
    type: string;
    updated?: string;
    valid: boolean;
    error?: string;
  }>;
  files: { index: boolean; log: boolean };
  embedding?: LlmWikiEmbeddingStatus;
  gbrain: {
    cli_installed: boolean;
    postgres_configured: boolean;
    postgres?: {
      configured: boolean;
      host: string;
      port: number;
      database: string;
      username: string;
    };
    runtime_home: string;
    imports: {
      available: boolean;
      counts: { pages: number; links: number; chunks: number; imports: number };
      records: Array<{
        id: number;
        source_id: string;
        source_type: string;
        pages_updated: string[];
        summary: string;
        created_at: string;
      }>;
    };
    models: {
      configured: boolean;
      embedding: { model_id: string; name: string; provider: string; dimension: number; uses_default_binding: boolean } | null;
      think: { model_id: string; name: string; provider: string; uses_default_binding: boolean } | null;
      error: string;
    };
  };
}

export interface LlmWikiEmbeddingStatus {
  hybrid_enabled: boolean;
  query_mode: "lexical" | "hybrid";
  infrastructure_ready: boolean;
  shared_collection?: boolean;
  profile?: {
    embedding_model_id: string;
    embedding_model: string;
    embedding_provider: string;
    embedding_dimension: number;
    parser: string;
    parser_version: number;
    text_collection: string;
    milvus_uri: string;
  };
  profile_matches?: boolean;
  counts: {
    total: number;
    indexed: number;
    pending: number;
    outdated: number;
    failed: number;
    chunks: number;
    stale: number;
  };
  pages: Array<{
    slug: string;
    virtual_path: string;
    content_sha256: string;
    indexed_content_sha256?: string | null;
    state: "indexed" | "pending" | "outdated" | "failed";
    chunk_count: number;
    indexed_at?: string | null;
    error?: string | null;
  }>;
  stale_pages?: string[];
  last_sync?: {
    completed_at?: string;
    force?: boolean;
    updated?: string[];
    skipped?: string[];
    failed?: Array<{ slug: string; error: string }>;
  } | null;
  error?: string;
}

export interface LlmWikiLintResult {
  ok: boolean;
  errors: Array<{ code: string; path: string; message: string }>;
  warnings: Array<{ code: string; path: string; message: string }>;
  counts: { pages: number; errors: number; warnings: number };
  bundle_hash: string;
}

export interface LlmWikiCompileResult {
  ok: boolean;
  phase: string;
  bundle_hash?: string;
  runtime_home?: string;
  checks?: Array<Record<string, unknown>>;
  import?: Record<string, unknown> | null;
  lint?: LlmWikiLintResult;
}

export interface KnowledgeMarkdownFile {
  name: string;
  path: string;
  virtual_path: string;
  storage_path: string;
  size_bytes: number;
  modified_at: string;
}

export interface KnowledgeMarkdownMatch {
  virtual_path: string;
  path: string;
  storage_path: string;
  line_number: number;
  line: string;
  context: string[];
}

export interface KnowledgeSearchHit {
  rank: number;
  modality: "text" | "image" | string;
  title: string;
  quote: string;
  id?: string;
  result_type?: string;
  uri?: string;
  display_path?: string;
  snippet?: string;
  highlights?: string[];
  matched_by?: string[];
  source_group?: { original?: string | null; imported?: string | null; wiki?: string | null; versions?: string[] };
  preview?: { kind?: string; heading?: string | null; line_number?: number | null };
  score?: number | null;
  raw_score?: number | null;
  normalized_score?: number | null;
  retrieval_channel?: string;
  source?: Record<string, unknown>;
  image_hit?: {
    title?: string;
    file_path?: string;
    virtual_path?: string;
    score?: number | null;
    raw_score?: number | null;
    normalized_score?: number | null;
    linked_markdown?: string;
    linked_markdown_virtual_path?: string;
    context?: Record<string, unknown>;
  } | null;
}

export interface KnowledgeSearchResult {
  query: string;
  top_k: number;
  candidate_top_k: number;
  total?: number;
  took_ms?: number;
  facets?: Record<string, Record<string, number>>;
  fusion?: {
    text_vector_weight?: number;
    bm25_weight?: number;
    image_vector_weight?: number;
    text_group_weight?: number;
    rerank_enabled?: boolean;
    rerank_top_n?: number;
  };
  retrieval: {
    text_vector: number;
    bm25: number;
    image_vector: number;
    selected: number;
    hybrid_enabled: boolean;
    rerank_enabled: boolean;
  };
  hits: KnowledgeSearchHit[];
  candidate_pools?: {
    text_vector?: KnowledgeSearchHit[];
    bm25?: KnowledgeSearchHit[];
    image_vector?: KnowledgeSearchHit[];
  };
  sources: Record<string, unknown>[];
}

export interface KnowledgeImportJob {
  id: string;
  knowledge_base_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | string;
  file_name: string;
  file_type: string;
  file_size: number;
  source_path: string;
  source_sha256: string;
  title?: string | null;
  publish_targets: string[];
  current_step: string;
  progress: number;
  document_id?: string | null;
  error_message?: string | null;
  retry_count: number;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface KnowledgeImportEvent {
  id: string;
  job_id: string;
  level: "info" | "warning" | "error" | string;
  message: string;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
}

export interface KnowledgeImportJobDetail {
  job: KnowledgeImportJob;
  events: KnowledgeImportEvent[];
  document?: KnowledgeDocument | null;
}

export interface TableAssetProfileColumn {
  name: string;
  dtype: string;
  non_null?: number;
  null_count?: number;
  distinct_count?: number;
  distinct_ratio?: number;
  sample_values?: string[];
  semantic_role_hint?: string;
}

export interface TableAssetProfile {
  asset_id: string;
  kind: string;
  source_type: string;
  file_name: string;
  virtual_path: string;
  sheet_name?: string | null;
  size_bytes: number;
  modified_at: string;
  generated_at: string;
  shape?: [number, number];
  columns?: TableAssetProfileColumn[];
  dtypes?: Record<string, string>;
  preview?: Record<string, unknown>[];
}

export interface TableAsset {
  asset_id: string;
  file_name: string;
  source_type: "excel" | "csv" | "tsv" | string;
  virtual_path: string;
  sheet_name?: string | null;
  size_bytes: number;
  modified_at: string;
  profile_status: "ready" | "missing" | string;
  profile_path?: string;
  rows?: number | null;
  columns_count?: number | null;
  columns?: string[];
  reference_status?: string;
  logical_dataset?: {
    kind: "vertical_concat" | string;
    formatter?: string;
    version?: string;
    description?: string;
    tags?: string[];
    materialization?: string;
    schema_mode?: string;
    source_asset_ids?: string[];
    canonical_columns?: string[];
    row_lineage_columns?: string[];
    sources?: Array<{ asset_id?: string; name?: string; sheet_name?: string | null; rows_estimate?: number | null; fields?: string[] }>;
    schema?: { fields?: string[]; lineage_fields?: string[] };
    coverage?: Array<Record<string, unknown>>;
    statistics?: { source_count?: number; rows_estimate?: number | null };
    routing?: { preferred_intents?: string[]; direct_source_allowed?: boolean; direct_source_when?: string[] };
    profile?: {
      status?: "ready" | "partial" | "missing" | "stale" | string;
      generated_at?: string;
      profile_refreshed_at?: string;
      source_count?: number;
      profiled_source_count?: number;
      fresh_source_count?: number;
      note?: string;
    };
    profile_refreshed_at?: string;
    refreshed_at?: string;
  };
  profile?: TableAssetProfile;
}

export interface ConcatDatasetPreviewSource {
  asset_id: string;
  file_name: string;
  sheet_name?: string | null;
  columns: string[];
  missing_from_baseline: string[];
  extra_vs_baseline: string[];
  missing_from_union: string[];
}

export interface ConcatDatasetPreview {
  baseline_columns: string[];
  canonical_columns: string[];
  baseline_asset_id: string;
  baseline_file_name: string;
  has_schema_drift: boolean;
  sources: ConcatDatasetPreviewSource[];
}

export interface TableAssetProfileJob {
  job_id: string;
  asset_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | string;
  created_at: number;
  updated_at: number;
  started_at?: number | null;
  finished_at?: number | null;
  error?: string | null;
  asset?: TableAsset;
}

export interface KnowledgeDatabaseSource {
  id: string;
  type: "postgresql" | string;
  source_type?: "postgresql" | string;
  name: string;
  description?: string;
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  password_configured?: boolean;
  selected_tables: string[];
  builtin?: boolean;
  configured_by?: string;
  environment_override?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export type VannaTrainingType = "sql" | "ddl" | "documentation" | string;

export interface VannaTrainingRecord {
  id: string;
  training_type: VannaTrainingType;
  question?: string | null;
  content: string;
  preview?: string;
}

export interface VannaTrainingData {
  records: VannaTrainingRecord[];
  count: number;
  counts: Record<string, number>;
}

export interface VannaTrainingResult {
  ok: boolean;
  training_type: VannaTrainingType;
  ids: string[];
  count: number;
  message: string;
}

export interface TableEntityCandidate {
  column: string;
  suggested_entity_type: string;
  score: number;
  reasons: string[];
  sample_values: string[];
  table_column?: string | null;
  distinct_count?: number | null;
  distinct_ratio?: number | null;
  dtype?: string | null;
}

export type VannaEntityFilterOperator = "in" | "not_in";

export interface VannaEntityFilter {
  column: string;
  operator: VannaEntityFilterOperator;
  values: string[];
}

export interface VannaEntityImportPreview {
  table_name: string;
  column: string;
  total: number;
  filtered: number;
  excluded: number;
  filters: VannaEntityFilter[];
}

export interface VannaEntityRecord {
  pk?: number | string;
  id?: number | string;
  entity_type: string;
  canonical_name: string;
  aliases?: string[];
  table_column?: string;
}

export interface VannaEntityListResult {
  entities: VannaEntityRecord[];
  count: number;
  limited?: boolean;
  type_counts?: Record<string, number>;
  offset?: number;
  limit?: number;
}

export interface VannaEntityImportResult {
  ok: boolean;
  job_id?: string;
  job?: KnowledgeImportJob;
  source_table?: string;
  table_column?: string;
  entity_type?: string;
  count?: number;
  entities?: Array<{ id: string; canonical_name: string; aliases: string[] }>;
}

export async function getKnowledgeStatus(): Promise<KnowledgeStatus> {
  const response = await fetch(`${API_BASE}/knowledge/status`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to load knowledge status: ${response.status}`);
  }
  return response.json();
}

export async function getBrainSchemaCatalog(): Promise<GbrainSchemaCatalog> {
  const response = await fetch(`${API_BASE}/knowledge/brain/schema/catalog`, { cache: "no-store" });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `加载 gbrain Schema 目录失败：${response.status}`));
  }
  return JSON.parse(text) as GbrainSchemaCatalog;
}

export async function getBrainSchemaBundle(): Promise<BrainSchemaBundle | null> {
  const response = await fetch(`${API_BASE}/knowledge/brain/schema/bundle`, { cache: "no-store" });
  if (response.status === 404) return null;
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `加载 Schema Bundle 失败：${response.status}`));
  }
  return JSON.parse(text) as BrainSchemaBundle;
}

export async function initializeBrainSchema(): Promise<BrainSchemaBundle> {
  const response = await fetch(`${API_BASE}/knowledge/brain/initialize`, { method: "POST" });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `初始化 LLM Wiki Brain 失败：${response.status}`));
  }
  return JSON.parse(text) as BrainSchemaBundle;
}

export async function previewBrainCustomSchema(
  manifest: GbrainSchemaPackManifest,
): Promise<BrainSchemaPreview> {
  const response = await fetch(`${API_BASE}/knowledge/brain/schema/custom/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ manifest }),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `Schema 校验失败：${response.status}`));
  }
  return JSON.parse(text) as BrainSchemaPreview;
}

export async function saveBrainCustomSchema(
  manifest: GbrainSchemaPackManifest,
  expectedSha256: string,
  expectedBundleHash: string,
): Promise<BrainSchemaBundle> {
  const response = await fetch(`${API_BASE}/knowledge/brain/schema/custom`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      manifest,
      expected_sha256: expectedSha256,
      expected_bundle_hash: expectedBundleHash,
    }),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `保存自定义 Schema 失败：${response.status}`));
  }
  return JSON.parse(text) as BrainSchemaBundle;
}

export async function rebuildLlmWikiAgents(): Promise<BrainSchemaBundle> {
  const response = await fetch(`${API_BASE}/knowledge/brain/agents/rebuild`, { method: "POST" });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `重建 AGENTS.md 失败：${response.status}`));
  }
  return JSON.parse(text) as BrainSchemaBundle;
}

export async function getLlmWikiWorkspaceStatus(): Promise<LlmWikiWorkspaceStatus> {
  const response = await fetch(`${API_BASE}/knowledge/brain/wiki/status`, { cache: "no-store" });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `加载 LLM Wiki 工作区失败：${response.status}`));
  }
  return JSON.parse(text) as LlmWikiWorkspaceStatus;
}

export async function getLlmWikiEmbeddingStatus(): Promise<LlmWikiEmbeddingStatus> {
  const response = await fetch(`${API_BASE}/knowledge/brain/wiki/embedding`, { cache: "no-store" });
  const text = await response.text();
  if (response.status === 404) {
    const workspace = await getLlmWikiWorkspaceStatus();
    if (workspace.embedding) return workspace.embedding;
    throw new Error("当前后端尚未加载 Wiki Embedding 状态接口，请重启后端服务后重新探测。");
  }
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `加载 Wiki Embedding 状态失败：${response.status}`));
  }
  return JSON.parse(text) as LlmWikiEmbeddingStatus;
}

export async function syncLlmWikiEmbeddings(force = false, slugs: string[] = []): Promise<{
  ok: boolean;
  updated: string[];
  skipped: string[];
  failed: Array<{ slug: string; error: string }>;
  status?: LlmWikiEmbeddingStatus;
}> {
  const response = await fetch(`${API_BASE}/knowledge/brain/wiki/embedding/sync`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force, slugs }),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `同步 Wiki Embedding 失败：${response.status}`));
  }
  return JSON.parse(text) as {
    ok: boolean;
    updated: string[];
    skipped: string[];
    failed: Array<{ slug: string; error: string }>;
    status?: LlmWikiEmbeddingStatus;
  };
}

export async function snapshotLlmWikiRaw(payload: {
  source_id: string;
  asset_id: string;
  title: string;
  content: string;
  source_path?: string;
}): Promise<Record<string, unknown>> {
  const response = await fetch(`${API_BASE}/knowledge/brain/wiki/raw`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `导入 Raw 快照失败：${response.status}`));
  }
  return JSON.parse(text) as Record<string, unknown>;
}

export async function lintLlmWiki(): Promise<LlmWikiLintResult> {
  const response = await fetch(`${API_BASE}/knowledge/brain/wiki/lint`, { cache: "no-store" });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `检查 Wiki 失败：${response.status}`));
  }
  return JSON.parse(text) as LlmWikiLintResult;
}

export async function compileLlmWikiGbrain(importPages = false): Promise<LlmWikiCompileResult> {
  const response = await fetch(`${API_BASE}/knowledge/brain/wiki/compile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ import_pages: importPages }),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `运行 gbrain 编译失败：${response.status}`));
  }
  return JSON.parse(text) as LlmWikiCompileResult;
}

export async function initializeLlmWikiGbrain(databaseUrl: string): Promise<{
  ok: boolean;
  runtime_home: string;
  schema_pack: string;
  postgresql: string;
}> {
  const response = await fetch(`${API_BASE}/knowledge/brain/wiki/gbrain/initialize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ database_url: databaseUrl }),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `初始化 gbrain PostgreSQL 运行时失败：${response.status}`));
  }
  return JSON.parse(text) as { ok: boolean; runtime_home: string; schema_pack: string; postgresql: string };
}

export async function listKnowledgeDocuments(): Promise<KnowledgeDocument[]> {
  const response = await fetch(`${API_BASE}/knowledge/documents`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `Failed to load knowledge documents: ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload.documents) ? payload.documents : [];
}

export async function listKnowledgeFiles(): Promise<KnowledgeDirectoryFile[]> {
  const response = await fetch(`${API_BASE}/knowledge/files`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `Failed to load knowledge files: ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload.files) ? payload.files : [];
}

export async function getKnowledgeFileTree(): Promise<KnowledgeTreeNode | null> {
  const response = await fetch(`${API_BASE}/knowledge/tree`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `Failed to load knowledge file tree: ${response.status}`);
  }
  const payload = await response.json();
  return payload.tree ?? null;
}

export async function listKnowledgeDatabaseSources(): Promise<KnowledgeDatabaseSource[]> {
  const response = await fetch(`${API_BASE}/knowledge/database-sources`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load database sources: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.sources) ? payload.sources : [];
}

export async function saveKnowledgeDatabaseSource(
  source: Partial<KnowledgeDatabaseSource>
): Promise<KnowledgeDatabaseSource> {
  const response = await fetch(`${API_BASE}/knowledge/database-sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(source),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to save database source: ${response.status}`));
  }
  const payload = await response.json();
  return payload.source;
}

export async function testKnowledgeDatabaseSource(
  source: Partial<KnowledgeDatabaseSource>
): Promise<{ ok: boolean; message: string }> {
  const response = await fetch(`${API_BASE}/knowledge/database-sources/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(source),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to test database source: ${response.status}`));
  }
  return response.json();
}

export async function listKnowledgeDatabaseSourceTables(sourceId: string): Promise<string[]> {
  const response = await fetch(`${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/tables`, {
    cache: "no-store",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to list database tables: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.tables) ? payload.tables : [];
}

export async function listKnowledgeDatabaseSourceTableColumns(sourceId: string, tableName: string): Promise<string[]> {
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/tables/${encodeURIComponent(tableName)}/columns`,
    { cache: "no-store" }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load database table columns: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.columns) ? payload.columns.map(String) : [];
}

export async function listKnowledgeDatabaseSourceColumnValues(
  sourceId: string,
  payload: { table_name: string; column: string; search?: string; limit?: number }
): Promise<{ values: string[]; has_more: boolean }> {
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/column-values`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load database column values: ${response.status}`));
  }
  const result = await response.json();
  return {
    values: Array.isArray(result.values) ? result.values.map(String) : [],
    has_more: Boolean(result.has_more),
  };
}

export async function deleteKnowledgeDatabaseSource(sourceId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to delete database source: ${response.status}`));
  }
}

export async function listKnowledgeDatabaseSourceVannaTraining(
  sourceId: string,
  tableName?: string
): Promise<VannaTrainingData> {
  const params = new URLSearchParams();
  if (tableName) params.set("table_name", tableName);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/vanna/training-data${suffix}`,
    { cache: "no-store" }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to list Vanna training data: ${response.status}`));
  }
  const payload = await response.json();
  return {
    records: Array.isArray(payload.records) ? payload.records : [],
    count: Number(payload.count || 0),
    counts: payload.counts && typeof payload.counts === "object" ? payload.counts : {},
  };
}

export async function trainKnowledgeDatabaseSourceVanna(
  sourceId: string,
  payload: {
    training_type: "ddl" | "documentation" | "sql";
    table_name?: string;
    table_names?: string[];
    ddl?: string;
    documentation?: string;
    question?: string;
    sql?: string;
  }
): Promise<VannaTrainingResult> {
  const response = await fetch(`${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/vanna/train`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to train Vanna: ${response.status}`));
  }
  return response.json();
}

export async function deleteKnowledgeDatabaseSourceVannaTraining(
  sourceId: string,
  trainingId: string
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/vanna/training-data/${encodeURIComponent(trainingId)}`,
    { method: "DELETE" }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to delete Vanna training data: ${response.status}`));
  }
}

export async function listKnowledgeDatabaseSourceVannaEntityCandidates(
  sourceId: string,
  payload: { table_name: string; max_candidates?: number }
): Promise<TableEntityCandidate[]> {
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/vanna/entities/candidates`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load Vanna entity candidates: ${response.status}`));
  }
  const data = await response.json();
  return Array.isArray(data.candidates) ? data.candidates : [];
}

export async function importKnowledgeDatabaseSourceVannaEntities(
  sourceId: string,
  payload: {
    table_name: string;
    column: string;
    entity_type: string;
    alias_columns?: string[];
    filters?: VannaEntityFilter[];
    max_values?: number;
  }
): Promise<VannaEntityImportResult> {
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/vanna/entities/import`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to import Vanna entities: ${response.status}`));
  }
  return response.json();
}

export async function previewKnowledgeDatabaseSourceVannaEntities(
  sourceId: string,
  payload: { table_name: string; column: string; filters?: VannaEntityFilter[] }
): Promise<VannaEntityImportPreview> {
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/vanna/entities/preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to preview Vanna entities: ${response.status}`));
  }
  const result = await response.json();
  return {
    table_name: String(result.table_name || payload.table_name),
    column: String(result.column || payload.column),
    total: Number(result.total || 0),
    filtered: Number(result.filtered || 0),
    excluded: Number(result.excluded || 0),
    filters: Array.isArray(result.filters) ? result.filters : [],
  };
}

export async function listKnowledgeDatabaseSourceVannaEntities(
  sourceId: string,
  options?: string | {
    tableName?: string;
    entityType?: string;
    search?: string;
    offset?: number;
    limit?: number;
  }
): Promise<VannaEntityListResult> {
  const params = new URLSearchParams();
  const normalizedOptions = typeof options === "string" ? { tableName: options } : options ?? {};
  if (normalizedOptions.tableName) params.set("table_name", normalizedOptions.tableName);
  if (normalizedOptions.entityType && normalizedOptions.entityType !== "all") params.set("entity_type", normalizedOptions.entityType);
  if (normalizedOptions.search?.trim()) params.set("search", normalizedOptions.search.trim());
  if (typeof normalizedOptions.offset === "number") params.set("offset", String(Math.max(0, normalizedOptions.offset)));
  if (typeof normalizedOptions.limit === "number") params.set("limit", String(Math.max(1, normalizedOptions.limit)));
  const query = params.toString();
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/vanna/entities${query ? `?${query}` : ""}`,
    { cache: "no-store" }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to list Vanna entities: ${response.status}`));
  }
  const data = await response.json();
  const entities = Array.isArray(data.entities) ? data.entities : [];
  return {
    entities,
    count: typeof data.count === "number" ? data.count : entities.length,
    limited: Boolean(data.limited),
    type_counts: data.type_counts && typeof data.type_counts === "object" ? data.type_counts : {},
    offset: typeof data.offset === "number" ? data.offset : 0,
    limit: typeof data.limit === "number" ? data.limit : entities.length,
  };
}

export async function deleteKnowledgeDatabaseSourceVannaEntity(sourceId: string, entityId: string): Promise<void> {
  const response = await fetch(
    `${API_BASE}/knowledge/database-sources/${encodeURIComponent(sourceId)}/vanna/entities/${encodeURIComponent(entityId)}`,
    { method: "DELETE" }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to delete Vanna entity: ${response.status}`));
  }
}

export async function previewKnowledgeFile(virtualPath: string): Promise<KnowledgeFilePreview> {
  const params = new URLSearchParams({ virtual_path: virtualPath });
  const response = await fetch(`${API_BASE}/knowledge/file/preview?${params.toString()}`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to preview knowledge file: ${response.status}`));
  }
  const payload = await response.json();
  return payload.file;
}

export async function snapshotKnowledgeFileToLlmWikiRaw(
  virtualPath: string
): Promise<{ ok: boolean; raw: Record<string, unknown> }> {
  const response = await fetch(`${API_BASE}/knowledge/file/llm-wiki-raw`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ virtual_path: virtualPath }),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `加入 LLM Wiki Raw 失败：${response.status}`));
  }
  return JSON.parse(text) as { ok: boolean; raw: Record<string, unknown> };
}

export function rawKnowledgeFileUrl(virtualPath: string): string {
  if (!virtualPath) return "";
  if (virtualPath.startsWith("/api/knowledge/file/raw?")) return virtualPath;
  if (virtualPath.startsWith("/knowledge/")) {
    return `${API_BASE}/knowledge/file/raw?virtual_path=${encodeURIComponent(virtualPath)}`;
  }
  return virtualPath;
}

export async function importLocalMarkdownDocument(
  sourcePath: string,
  title?: string
): Promise<KnowledgeDocument> {
  const response = await fetch(`${API_BASE}/knowledge/documents/import-local-md`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_path: sourcePath, title: title || undefined }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `Failed to import knowledge document: ${response.status}`);
  }
  const payload = await response.json();
  return payload.document;
}

export async function uploadPdfKnowledgeDocument(
  file: File,
  title?: string,
  publishTargets: string[] = ["local_markdown", "vector"]
): Promise<{ document: KnowledgeDocument; ingestion: Record<string, unknown> }> {
  const form = new FormData();
  form.append("file", file, file.name);
  if (title?.trim()) form.append("title", title.trim());
  form.append("publish_targets", publishTargets.join(","));
  const response = await fetch(`${DIRECT_BACKEND_API_BASE}/knowledge/documents/upload-pdf`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to upload PDF knowledge document: ${response.status}`));
  }
  return response.json();
}

export async function uploadKnowledgeDocument(
  file: File,
  title?: string,
  publishTargets: string[] = ["local_markdown", "vector"]
): Promise<{ document: KnowledgeDocument; ingestion: Record<string, unknown>; detected_type?: string }> {
  const form = new FormData();
  form.append("file", file, file.name);
  if (title?.trim()) form.append("title", title.trim());
  form.append("publish_targets", publishTargets.join(","));
  const response = await fetch(`${DIRECT_BACKEND_API_BASE}/knowledge/documents/import`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to import knowledge document: ${response.status}`));
  }
  return response.json();
}

export async function createKnowledgeImportJob(
  file: File,
  title?: string,
  publishTargets: string[] = ["local_markdown"]
): Promise<KnowledgeImportJob> {
  const form = new FormData();
  form.append("file", file, file.name);
  if (title?.trim()) form.append("title", title.trim());
  form.append("publish_targets", publishTargets.join(","));
  const response = await fetch(`${DIRECT_BACKEND_API_BASE}/knowledge/import-jobs`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to create knowledge import job: ${response.status}`));
  }
  const payload = await response.json();
  return payload.job;
}

export async function createLlmWikiIngestJob(
  rawPaths: string[],
  importGbrain = false
): Promise<KnowledgeImportJob> {
  const response = await fetch(`${API_BASE}/knowledge/brain/wiki/ingest-jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ raw_paths: rawPaths, import_gbrain: importGbrain }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to create LLM Wiki ingest job: ${response.status}`));
  }
  const payload = await response.json();
  return payload.job;
}

export type ReadLaterItem = {
  id: string;
  original_url: string;
  canonical_url: string;
  title: string;
  site_name: string;
  author: string;
  description: string;
  image_url: string;
  virtual_path: string;
  content_sha256: string;
  parse_status: "queued" | "processing" | "ready" | "link_only" | "failed";
  reading_status: "unread" | "read" | "archived";
  error_message: string;
  tags: string[];
  note: string;
  document_id: string | null;
  raw_snapshot_path: string;
  wiki_job_id: string;
  fetched_at: string | null;
  read_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  content?: string;
};

export async function saveReadLaterUrl(input: {
  url: string;
  title?: string;
  note?: string;
  tags?: string[];
}): Promise<{ item: ReadLaterItem; job: KnowledgeImportJob | null; deduplicated: boolean }> {
  const response = await fetch(`${API_BASE}/read-later`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `收藏链接失败：${response.status}`));
  return JSON.parse(text);
}

export async function listReadLaterItems(options: {
  readingStatus?: string;
  parseStatus?: string;
  search?: string;
} = {}): Promise<ReadLaterItem[]> {
  const params = new URLSearchParams();
  if (options.readingStatus) params.set("reading_status", options.readingStatus);
  if (options.parseStatus) params.set("parse_status", options.parseStatus);
  if (options.search) params.set("search", options.search);
  const response = await fetch(`${API_BASE}/read-later?${params.toString()}`, { cache: "no-store" });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `读取稍后读失败：${response.status}`));
  return (JSON.parse(text).items || []) as ReadLaterItem[];
}

export async function getReadLaterItem(itemId: string): Promise<ReadLaterItem> {
  const response = await fetch(`${API_BASE}/read-later/${encodeURIComponent(itemId)}`, { cache: "no-store" });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `读取收藏正文失败：${response.status}`));
  return JSON.parse(text).item as ReadLaterItem;
}

export async function updateReadLaterItem(
  itemId: string,
  patch: Partial<Pick<ReadLaterItem, "reading_status" | "title" | "note" | "tags">>
): Promise<ReadLaterItem> {
  const response = await fetch(`${API_BASE}/read-later/${encodeURIComponent(itemId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `更新收藏失败：${response.status}`));
  return JSON.parse(text).item as ReadLaterItem;
}

export async function deleteReadLaterItem(itemId: string): Promise<{
  ok: boolean;
  deleted: Record<string, boolean>;
  preserved: string[];
}> {
  const response = await fetch(`${API_BASE}/read-later/${encodeURIComponent(itemId)}`, { method: "DELETE" });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `删除收藏失败：${response.status}`));
  return JSON.parse(text);
}

export async function retryReadLaterItem(itemId: string): Promise<{ item: ReadLaterItem; job: KnowledgeImportJob }> {
  const response = await fetch(`${API_BASE}/read-later/${encodeURIComponent(itemId)}/retry`, { method: "POST" });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `重新解析失败：${response.status}`));
  return JSON.parse(text);
}

export async function compileReadLaterItems(itemIds: string[], importGbrain = false): Promise<KnowledgeImportJob> {
  const response = await fetch(`${API_BASE}/read-later/compile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item_ids: itemIds, import_gbrain: importGbrain }),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `提交 Wiki 编译失败：${response.status}`));
  return JSON.parse(text).job as KnowledgeImportJob;
}

export async function listKnowledgeImportJobs(limit = 20): Promise<KnowledgeImportJob[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  const response = await fetchWithTimeout(
    `${API_BASE}/knowledge/import-jobs?${params.toString()}`,
    { cache: "no-store" },
    4000
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load knowledge import jobs: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.jobs) ? payload.jobs : [];
}

export async function getKnowledgeImportJob(
  jobId: string,
  includeEvents = true
): Promise<KnowledgeImportJobDetail> {
  const params = new URLSearchParams({ include_events: includeEvents ? "true" : "false" });
  const response = await fetchWithTimeout(
    `${API_BASE}/knowledge/import-jobs/${encodeURIComponent(jobId)}?${params.toString()}`,
    {
      cache: "no-store",
    },
    includeEvents ? 8000 : 3000
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load knowledge import job: ${response.status}`));
  }
  const payload = await response.json();
  return {
    job: payload.job,
    events: Array.isArray(payload.events) ? payload.events : [],
    document: payload.document ?? null,
  };
}

export async function retryKnowledgeImportJob(jobId: string): Promise<KnowledgeImportJob> {
  const response = await fetch(`${API_BASE}/knowledge/import-jobs/${encodeURIComponent(jobId)}/retry`, {
    method: "POST",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to retry knowledge import job: ${response.status}`));
  }
  const payload = await response.json();
  return payload.job;
}

export async function deleteKnowledgeImportJob(jobId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/knowledge/import-jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to delete knowledge import job: ${response.status}`));
  }
}

export async function clearKnowledgeImportJobs(): Promise<number> {
  const response = await fetch(`${API_BASE}/knowledge/import-jobs`, {
    method: "DELETE",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to clear knowledge import jobs: ${response.status}`));
  }
  const payload = await response.json();
  return typeof payload.deleted_count === "number" ? payload.deleted_count : 0;
}

export async function publishKnowledgeImportJobVector(
  jobId: string
): Promise<{ job: KnowledgeImportJob; queued: boolean; source_job_id?: string }> {
  const response = await fetch(`${API_BASE}/knowledge/import-jobs/${encodeURIComponent(jobId)}/publish-vector`, {
    method: "POST",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to publish knowledge vector index: ${response.status}`));
  }
  return response.json();
}

export async function publishKnowledgeDocumentVector(
  documentId: string
): Promise<{ job: KnowledgeImportJob; queued: boolean }> {
  const response = await fetch(
    `${API_BASE}/knowledge/documents/${encodeURIComponent(documentId)}/publish-vector`,
    { method: "POST" }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to rebuild document vector index: ${response.status}`));
  }
  return response.json();
}

export async function globKnowledgeMarkdown(pattern = "**/*.md"): Promise<KnowledgeMarkdownFile[]> {
  const params = new URLSearchParams({ pattern });
  const response = await fetch(`${API_BASE}/knowledge/markdown/glob?${params.toString()}`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `Failed to glob knowledge Markdown: ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload.files) ? payload.files : [];
}

export async function grepKnowledgeMarkdown(
  query: string,
  pattern = "**/*.md"
): Promise<KnowledgeMarkdownMatch[]> {
  const response = await fetch(`${API_BASE}/knowledge/markdown/grep`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, pattern, context_lines: 1, max_matches: 50 }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `Failed to grep knowledge Markdown: ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload.matches) ? payload.matches : [];
}

export async function searchKnowledge(query: string, topK?: number): Promise<KnowledgeSearchResult> {
  const response = await fetch(`${API_BASE}/knowledge/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k: topK }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to search knowledge: ${response.status}`));
  }
  return response.json();
}

export type KnowledgeSearchCategory = "all" | "wiki" | "article" | "image" | "file";

export interface KnowledgeSearchDirectory {
  id: string;
  path: string;
  enabled: boolean;
  recursive: boolean;
  content_types: string[];
  referenced_images_only?: boolean;
  status?: string;
  indexed_documents?: number;
  indexed_images?: number;
}

export interface KnowledgeSearchConfig {
  enabled: boolean;
  directories: KnowledgeSearchDirectory[];
  sources: { read_later?: { enabled: boolean } };
  exclude: string[];
}

export interface KnowledgeSearchIndexStatus {
  enabled: boolean;
  status: string;
  generated_at?: string | null;
  counts: { records: number; documents: number; images: number };
  directories: KnowledgeSearchDirectory[];
}

export async function searchKnowledgePortal(input: {
  query: string;
  category?: KnowledgeSearchCategory;
  offset?: number;
  limit?: number;
  directory_ids?: string[];
}): Promise<KnowledgeSearchResult> {
  const response = await fetch(`${API_BASE}/knowledge/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `搜索知识库失败：${response.status}`));
  return JSON.parse(text) as KnowledgeSearchResult;
}

export async function getKnowledgeSearchConfig(): Promise<KnowledgeSearchConfig> {
  const response = await fetch(`${API_BASE}/knowledge/search/config`, { cache: "no-store" });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `读取搜索配置失败：${response.status}`));
  return (JSON.parse(text) as { config: KnowledgeSearchConfig }).config;
}

export async function updateKnowledgeSearchConfig(config: KnowledgeSearchConfig): Promise<KnowledgeSearchConfig> {
  const response = await fetch(`${API_BASE}/knowledge/search/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `保存搜索配置失败：${response.status}`));
  return (JSON.parse(text) as { config: KnowledgeSearchConfig }).config;
}

export async function getKnowledgeSearchIndexStatus(): Promise<KnowledgeSearchIndexStatus> {
  const response = await fetch(`${API_BASE}/knowledge/search/index-status`, { cache: "no-store" });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `读取关键词目录状态失败：${response.status}`));
  return JSON.parse(text) as KnowledgeSearchIndexStatus;
}

export async function refreshKnowledgeSearchIndex(rebuild = false): Promise<KnowledgeSearchIndexStatus> {
  const response = await fetch(`${API_BASE}/knowledge/search/${rebuild ? "index-rebuild" : "index-refresh"}`, { method: "POST" });
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `更新关键词目录失败：${response.status}`));
  return JSON.parse(text) as KnowledgeSearchIndexStatus;
}

export async function listTableAssets(includeProfile = false): Promise<TableAsset[]> {
  const params = new URLSearchParams({ include_profile: includeProfile ? "true" : "false" });
  const response = await fetch(`${API_BASE}/analytics/table-assets?${params.toString()}`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load table assets: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.assets) ? payload.assets : [];
}

export async function getTableAsset(assetId: string, includeProfile = true): Promise<TableAsset> {
  const response = await fetch(
    `${API_BASE}/analytics/table-assets/${encodeURIComponent(assetId)}?include_profile=${includeProfile ? "true" : "false"}`,
    { cache: "no-store" }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load table asset: ${response.status}`));
  }
  const responsePayload = await response.json();
  return responsePayload.asset;
}

export async function removeTableAsset(assetId: string): Promise<{ asset_id: string; removed_asset_ids: string[]; file_name: string; source_file_preserved: boolean }> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/${encodeURIComponent(assetId)}`, { method: "DELETE" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to remove table asset: ${response.status}`));
  }
  return response.json();
}

export async function previewConcatDataset(sourceAssetIds: string[]): Promise<ConcatDatasetPreview> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/concat-datasets/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_asset_ids: sourceAssetIds }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to inspect logical dataset fields: ${response.status}`));
  }
  return response.json();
}

export async function createConcatDataset(payload: { name: string; description?: string; tags?: string[]; source_asset_ids: string[]; schema_mode?: "strict" | "baseline_fill_missing" | "union_fill_missing"; preferred_intents?: string[]; direct_source_allowed?: boolean }): Promise<TableAsset> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/concat-datasets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to create logical dataset: ${response.status}`));
  }
  return (await response.json()).asset;
}

export async function refreshConcatDataset(assetId: string): Promise<TableAsset> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/${encodeURIComponent(assetId)}/refresh-concat`, { method: "POST" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to refresh logical dataset: ${response.status}`));
  }
  return (await response.json()).asset;
}

export async function appendConcatDatasetSources(
  assetId: string,
  payload: { source_asset_ids: string[]; schema_mode: "strict" | "baseline_fill_missing" | "union_fill_missing" }
): Promise<TableAsset> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/${encodeURIComponent(assetId)}/concat-sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to append logical dataset sources: ${response.status}`));
  }
  return (await response.json()).asset;
}

export async function updateLogicalDatasetDefinition(
  assetId: string,
  payload: { name?: string; description?: string; tags?: string[]; preferred_intents?: string[]; direct_source_allowed?: boolean }
): Promise<TableAsset> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/${encodeURIComponent(assetId)}/logical-definition`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to update logical dataset definition: ${response.status}`));
  }
  return (await response.json()).asset;
}

export async function generateTableAssetProfile(assetId: string): Promise<TableAssetProfileJob> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/${encodeURIComponent(assetId)}/profile`, {
    method: "POST",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to generate table profile: ${response.status}`));
  }
  const payload = await response.json();
  return payload.job;
}

export async function getTableAssetProfileJob(jobId: string): Promise<TableAssetProfileJob> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/profile-jobs/${encodeURIComponent(jobId)}`, {
    cache: "no-store",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load table profile job: ${response.status}`));
  }
  const payload = await response.json();
  return payload.job;
}

export async function refreshTableAssetProfiles(): Promise<{ generated: TableAsset[]; errors: Record<string, string>[]; total: number }> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/refresh-profiles`, { method: "POST" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to refresh table profiles: ${response.status}`));
  }
  return response.json();
}

export async function listTableAssetEntityCandidates(assetId: string, limit = 12): Promise<TableEntityCandidate[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  const response = await fetch(
    `${API_BASE}/analytics/table-assets/${encodeURIComponent(assetId)}/entity-candidates?${params.toString()}`,
    { cache: "no-store" }
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load entity candidates: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.candidates) ? payload.candidates : [];
}

export type SemanticAssetType = "measure" | "dimension" | "grain" | "relation";
export type DimensionResolutionMode = "source_field" | "derived" | "entity_lookup" | "calendar_lookup";
export type AssetRelationType = "dimension_binding" | "direct_join";

export interface AssetRelationDefinition {
  type: AssetRelationType;
  asset?: { ref: string; display_name?: string; key_fields: string[] };
  dimension?: { ref: string; display_name?: string; output_key?: string };
  left?: { ref: string; display_name?: string; key_fields: string[] };
  right?: { ref: string; display_name?: string; key_fields: string[] };
  field_mapping?: { left: string[]; right: string[] };
  cardinality: "one_to_one" | "one_to_many" | "many_to_one" | "many_to_many";
  join_type?: "inner" | "left" | "right" | "full";
  grain?: string[] | { left: string[]; right: string[] };
  use_statuses?: string[];
  rules?: string[];
}

export interface DimensionBindingDefinition {
  asset_ref?: string;
  display_name?: string;
  fields?: Record<string, string>;
}

export interface DimensionDefinition {
  mode: DimensionResolutionMode;
  bindings?: DimensionBindingDefinition[];
  source_fields?: string[];
  expression?: string;
  canonical?: { key?: string; fields?: string[] };
  reference_path?: string;
  date_field?: string;
  week_start_day?: string;
  timezone?: string;
}

export interface SemanticDimensionUpdatePayload {
  name: string;
  description: string;
  aliases: string[];
  tags: string[];
  version: string;
  dimension_definition: DimensionDefinition;
}

export interface SemanticAssetSummary {
  id: string;
  name: string;
  type: SemanticAssetType;
  path: string;
  description?: string;
  aliases?: string[];
  tags?: string[];
  formatter?: string;
  resolution_mode?: string;
  resolution_label?: string;
  relation_type?: AssetRelationType | string;
  relation_definition?: AssetRelationDefinition;
  mtime?: number;
  size_bytes?: number;
}

export interface SemanticAssetFile {
  name: string;
  path: string;
  relative_path: string;
  size_bytes?: number;
  mtime?: number;
  editable?: boolean;
  main?: boolean;
}

export interface SemanticAssetDetail extends SemanticAssetSummary {
  body: string;
  frontmatter: Record<string, unknown>;
  files?: SemanticAssetFile[];
}

export interface SemanticDimensionSourceBinding {
  source_id?: string;
  source_kind?: string;
  source_ref?: string;
  source_name?: string;
  table_or_sheet?: string;
  key_fields?: Record<string, unknown>;
}

export interface SemanticDimensionMatchRow {
  entity_key?: string | null;
  canonical_label?: string;
  canonical?: { entity_key?: string; canonical_brand?: string; canonical_serial_name?: string } | null;
  status?: string;
  binding?: SemanticDimensionSourceBinding | null;
  manual?: boolean;
  override_id?: string;
}

export interface SemanticDimensionSourceRegistryEntry {
  id: string;
  name: string;
  kind?: string;
  table_or_sheet?: string;
  identity_fields?: string[];
  mapping?: Array<Record<string, unknown>>;
}

export interface SemanticDimensionMatchingView {
  dimension_id: string;
  version: string;
  generated_at_display?: string;
  summary: {
    canonical_entities: number;
    manual_overrides: number;
    manual_entity_overrides?: number;
    sources: number;
    status_counts: Record<string, number>;
    published_manual_overrides?: number;
    has_unpublished_changes?: boolean;
  };
  sources: SemanticDimensionSourceRegistryEntry[];
  entity_options: Array<{ entity_key: string; label: string }>;
  rows: SemanticDimensionMatchRow[];
  count: number;
  offset: number;
  limit: number;
}

export interface SemanticDimensionMatchingOverviewRow {
  entity_key: string;
  canonical_label: string;
  status?: string;
  source_cells: Record<string, Array<{
    source_ref: string;
    source_key: Record<string, unknown>;
    manual?: boolean;
  }>>;
}

export interface SemanticDimensionMatchingOverview {
  dimension_id: string;
  version: string;
  has_unpublished_changes?: boolean;
  summary: {
    canonical_entities: number;
    manual_overrides: number;
    manual_entity_overrides?: number;
    published_manual_overrides?: number;
    sources: number;
  };
  sources: SemanticDimensionSourceRegistryEntry[];
  rows: SemanticDimensionMatchingOverviewRow[];
  count: number;
  offset: number;
  limit: number;
}

export interface SemanticDimensionBaselineChange {
  job: SemanticDimensionBuildJob;
  baseline_delta: {
    added?: Array<{ entity_key: string; label?: string }>;
    removed?: Array<{ entity_key: string; label?: string }>;
  };
}

export interface SemanticDimensionOverridePayload {
  source_ref: string;
  source_key: Record<string, unknown>;
  source_id?: string;
  scope?: "source_id" | "source_ref";
  action: "bind" | "exclude";
  target_entity_key?: string;
  reason?: string;
  source_name?: string;
  source_kind?: string;
  table_or_sheet?: string;
}

export interface SemanticDimensionEntityLifecyclePayload {
  entity_key: string;
  action: "active" | "inactive" | "remove";
  reason?: string;
}

export interface SemanticAssetListResult {
  assets: SemanticAssetSummary[];
  count: number;
  type_counts?: Record<string, number>;
  root_dir?: string;
  last_scanned_at?: string | null;
}

export interface TaskNotification {
  id: string;
  category: string;
  subject_type: string;
  subject_id: string;
  title: string;
  body: string;
  payload?: Record<string, unknown>;
  created_at?: string | null;
  read_at?: string | null;
}

export interface SemanticDimensionBuildJob {
  id: string;
  session_id: string;
  query_id: string;
  dimension_id: string;
  adapter: string;
  status: string;
  current_step: string;
  progress: number;
  staging_path: string;
  published_reference_path: string;
  result_summary?: Record<string, unknown>;
  error_message?: string | null;
  retry_count: number;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface TaskJobEvent {
  id: string;
  job_id: string;
  level: string;
  message: string;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
}

export interface SemanticDimensionBuildJobDetail {
  job: SemanticDimensionBuildJob;
  events: TaskJobEvent[];
}

export interface TaskCenterItem {
  task_type: "semantic_dimension_build" | "knowledge_import" | string;
  title: string;
  job: { id?: string; status?: string; current_step?: string; progress?: number; [key: string]: unknown };
  created_at?: string | null;
}

export async function listTaskNotifications(unreadOnly = false, limit = 20): Promise<TaskNotification[]> {
  const params = new URLSearchParams({ unread_only: String(unreadOnly), limit: String(limit) });
  const response = await fetchWithTimeout(`${API_BASE}/analytics/task-notifications?${params.toString()}`, { cache: "no-store" }, 4000);
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load task notifications: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.notifications) ? payload.notifications : [];
}

export async function markTaskNotificationRead(notificationId: string): Promise<TaskNotification> {
  const response = await fetch(`${API_BASE}/analytics/task-notifications/${encodeURIComponent(notificationId)}/read`, { method: "POST" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to mark task notification read: ${response.status}`));
  }
  const payload = await response.json();
  return payload.notification;
}

export async function listTaskCenter(limit = 20): Promise<TaskCenterItem[]> {
  const response = await fetchWithTimeout(`${API_BASE}/analytics/task-center?limit=${limit}`, { cache: "no-store" }, 4000);
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load task center: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.tasks) ? payload.tasks : [];
}

export async function getSemanticDimensionBuildJob(
  jobId: string,
  includeEvents = true
): Promise<SemanticDimensionBuildJobDetail> {
  const params = new URLSearchParams({ include_events: String(includeEvents) });
  const response = await fetchWithTimeout(
    `${API_BASE}/analytics/semantic-dimension-jobs/${encodeURIComponent(jobId)}?${params.toString()}`,
    { cache: "no-store" },
    includeEvents ? 8000 : 3000
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load semantic dimension job: ${response.status}`));
  }
  const payload = await response.json();
  return {
    job: payload.job,
    events: Array.isArray(payload.events) ? payload.events : [],
  };
}

export interface SemanticAssetCreatePayload {
  name: string;
  type: SemanticAssetType;
  description?: string;
  aliases?: string[];
  tags?: string[];
  version?: string;
  slug?: string;
  dimension_definition?: DimensionDefinition;
  relation_definition?: AssetRelationDefinition;
}

export async function listSemanticAssets(): Promise<SemanticAssetListResult> {
  const response = await fetch(`${API_BASE}/analytics/semantic-assets`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load semantic assets: ${response.status}`));
  }
  const payload = await response.json();
  return {
    ...payload,
    assets: Array.isArray(payload.assets) ? payload.assets : [],
    count: typeof payload.count === "number" ? payload.count : 0,
  };
}

export async function refreshSemanticAssets(): Promise<SemanticAssetListResult> {
  const response = await fetch(`${API_BASE}/analytics/semantic-assets/refresh`, { method: "POST" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to refresh semantic assets: ${response.status}`));
  }
  const payload = await response.json();
  return {
    ...payload,
    assets: Array.isArray(payload.assets) ? payload.assets : [],
    count: typeof payload.count === "number" ? payload.count : 0,
  };
}

export async function createSemanticAsset(payload: SemanticAssetCreatePayload): Promise<SemanticAssetDetail> {
  const response = await fetch(`${API_BASE}/analytics/semantic-assets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to create semantic asset: ${response.status}`));
  }
  const data = await response.json();
  return data.asset;
}

export async function getSemanticAsset(assetId: string): Promise<SemanticAssetDetail> {
  const response = await fetch(`${API_BASE}/analytics/semantic-assets/${encodeURIComponent(assetId)}`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load semantic asset: ${response.status}`));
  }
  const payload = await response.json();
  return payload.asset;
}

export async function updateSemanticDimensionDefinition(
  assetId: string,
  payload: SemanticDimensionUpdatePayload
): Promise<SemanticAssetDetail> {
  const response = await fetch(`${API_BASE}/analytics/semantic-assets/${encodeURIComponent(assetId)}/dimension-definition`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to save dimension definition: ${response.status}`));
  }
  const responsePayload = await response.json();
  return responsePayload.asset;
}

export async function updateSemanticRelationDefinition(
  assetId: string,
  payload: {
    name: string;
    description: string;
    aliases: string[];
    tags: string[];
    version: string;
    relation_definition: AssetRelationDefinition;
  }
): Promise<SemanticAssetDetail> {
  const response = await fetch(`${API_BASE}/analytics/semantic-assets/${encodeURIComponent(assetId)}/relation-definition`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to save relation definition: ${response.status}`));
  }
  return (await response.json()).asset;
}

export async function getSemanticDimensionMatching(
  dimensionId: string,
  options: { status?: string; sourceRef?: string; query?: string; offset?: number; limit?: number } = {}
): Promise<SemanticDimensionMatchingView> {
  const params = new URLSearchParams();
  if (options.status) params.set("status", options.status);
  if (options.sourceRef) params.set("source_ref", options.sourceRef);
  if (options.query) params.set("query", options.query);
  params.set("offset", String(options.offset || 0));
  params.set("limit", String(options.limit || 100));
  const response = await fetchWithTimeout(
    `${API_BASE}/analytics/semantic-dimensions/${encodeURIComponent(dimensionId)}/matching?${params.toString()}`,
    { cache: "no-store" },
    10000
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load semantic dimension matching: ${response.status}`));
  }
  return response.json();
}

export async function getSemanticDimensionMatchingOverview(
  dimensionId: string,
  options: { query?: string; offset?: number; limit?: number } = {}
): Promise<SemanticDimensionMatchingOverview> {
  const params = new URLSearchParams();
  if (options.query) params.set("query", options.query);
  params.set("offset", String(options.offset || 0));
  params.set("limit", String(options.limit || 100));
  const response = await fetchWithTimeout(
    `${API_BASE}/analytics/semantic-dimensions/${encodeURIComponent(dimensionId)}/matching/overview?${params.toString()}`,
    { cache: "no-store" },
    10000
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load semantic dimension overview: ${response.status}`));
  }
  return response.json();
}

export async function getSemanticDimensionBaselineChange(dimensionId: string): Promise<SemanticDimensionBaselineChange | null> {
  const response = await fetchWithTimeout(
    `${API_BASE}/analytics/semantic-dimensions/${encodeURIComponent(dimensionId)}/matching/baseline-changes`,
    { cache: "no-store" },
    10000
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load semantic dimension baseline change: ${response.status}`));
  }
  const payload = await response.json();
  return payload.change || null;
}

export async function resolveSemanticDimensionBaselineChange(
  jobId: string,
  action: "inactive" | "remove" | "cancel"
): Promise<void> {
  const response = await fetch(`${API_BASE}/analytics/semantic-dimension-jobs/${encodeURIComponent(jobId)}/baseline-change/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to resolve semantic dimension baseline change: ${response.status}`));
  }
}

export async function publishSemanticDimensionMatching(dimensionId: string): Promise<{ version: string; published_at_display?: string }> {
  const response = await fetch(`${API_BASE}/analytics/semantic-dimensions/${encodeURIComponent(dimensionId)}/matching/publish`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to publish semantic dimension matching: ${response.status}`));
  }
  return response.json();
}

export async function saveSemanticDimensionOverride(
  dimensionId: string,
  payload: SemanticDimensionOverridePayload
): Promise<void> {
  const response = await fetch(`${API_BASE}/analytics/semantic-dimensions/${encodeURIComponent(dimensionId)}/matching/overrides`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to save semantic dimension override: ${response.status}`));
  }
}

export async function deleteSemanticDimensionOverride(dimensionId: string, overrideId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/analytics/semantic-dimensions/${encodeURIComponent(dimensionId)}/matching/overrides/${encodeURIComponent(overrideId)}`, { method: "DELETE" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to delete semantic dimension override: ${response.status}`));
  }
}

export async function saveSemanticDimensionEntityLifecycle(
  dimensionId: string,
  payload: SemanticDimensionEntityLifecyclePayload
): Promise<void> {
  const response = await fetch(`${API_BASE}/analytics/semantic-dimensions/${encodeURIComponent(dimensionId)}/matching/entities/lifecycle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to save semantic dimension lifecycle: ${response.status}`));
  }
}

export async function importSemanticAssets(files: File[]): Promise<SemanticAssetListResult> {
  const form = new FormData();
  files.forEach((file) => {
    const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
    form.append("files", file, relativePath || file.name);
  });
  const response = await fetch(`${API_BASE}/analytics/semantic-assets/import`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to import semantic assets: ${response.status}`));
  }
  const payload = await response.json();
  return {
    ...payload,
    assets: Array.isArray(payload.assets) ? payload.assets : [],
    count: typeof payload.count === "number" ? payload.count : 0,
  };
}

export interface AnalyticsModelFile {
  name: string;
  path: string;
  relative_path: string;
  size_bytes?: number;
  mtime?: number;
  editable?: boolean;
  main?: boolean;
}

export interface AnalyticsModelSummary {
  id: string;
  name: string;
  path: string;
  description?: string;
  version?: string;
  tags?: string[];
  formatter?: string;
  data_assets?: Record<string, unknown>;
  semantic_assets?: Record<string, unknown>;
  asset_relations?: string[];
  guardrails?: string[];
  templates?: Record<string, unknown>;
  default_template?: string | null;
  mtime?: number;
  size_bytes?: number;
}

export interface AnalyticsModelDetail extends AnalyticsModelSummary {
  body: string;
  frontmatter: Record<string, unknown>;
  files?: AnalyticsModelFile[];
}

export interface AnalyticsModelListResult {
  models: AnalyticsModelSummary[];
  count: number;
  root_dir?: string;
  last_scanned_at?: string | null;
}

export interface AnalyticsModelCreatePayload {
  name: string;
  description?: string;
  version?: string;
  tags?: string[];
  slug?: string;
  data_assets?: Record<string, unknown>;
  semantic_assets?: Record<string, unknown>;
  asset_relations?: string[];
  guardrails?: string[];
  templates?: Record<string, unknown>;
  default_template?: string | null;
}

export type AnalyticsProjectDataFileMode = "copy" | "reference";

export interface AnalyticsProjectExportDataAsset {
  ref: string;
  kind: "database_table" | "table_asset" | "logical_dataset";
  status: "ready" | "missing";
  asset_id: string;
  file_name: string;
  source_path: string;
  virtual_path: string;
  sheet_name?: string | null;
  size_bytes: number;
  profile_available: boolean;
  source_asset_ids: string[];
  source_name: string;
  source_type: string;
  host: string;
  port: number;
  database: string;
  schema_name: string;
}

export interface AnalyticsProjectExportPlan {
  format: string;
  model_id: string;
  model_name: string;
  model_version: string;
  package_name: string;
  plan_id: string;
  data_file_mode: AnalyticsProjectDataFileMode;
  semantic_asset_ids: string[];
  relation_ids: string[];
  guardrail_ids: string[];
  data_assets: AnalyticsProjectExportDataAsset[];
  copied_file_count: number;
  copied_bytes: number;
  warnings: string[];
  missing_dependencies: string[];
}

export async function listAnalyticsModels(): Promise<AnalyticsModelListResult> {
  const response = await fetch(`${API_BASE}/analytics/models`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load analytics models: ${response.status}`));
  }
  const payload = await response.json();
  return {
    ...payload,
    models: Array.isArray(payload.models) ? payload.models : [],
    count: typeof payload.count === "number" ? payload.count : 0,
  };
}

export async function refreshAnalyticsModels(): Promise<AnalyticsModelListResult> {
  const response = await fetch(`${API_BASE}/analytics/models/refresh`, { method: "POST" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to refresh analytics models: ${response.status}`));
  }
  const payload = await response.json();
  return {
    ...payload,
    models: Array.isArray(payload.models) ? payload.models : [],
    count: typeof payload.count === "number" ? payload.count : 0,
  };
}

export async function createAnalyticsModel(payload: AnalyticsModelCreatePayload): Promise<AnalyticsModelDetail> {
  const response = await fetch(`${API_BASE}/analytics/models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to create analytics model: ${response.status}`));
  }
  const data = await response.json();
  return data.model;
}

export async function getAnalyticsModel(modelId: string): Promise<AnalyticsModelDetail> {
  const response = await fetch(`${API_BASE}/analytics/models/${encodeURIComponent(modelId)}`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load analytics model: ${response.status}`));
  }
  const payload = await response.json();
  return payload.model;
}

export async function importAnalyticsModels(files: File[]): Promise<AnalyticsModelListResult> {
  const form = new FormData();
  files.forEach((file) => {
    const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
    form.append("files", file, relativePath || file.name);
  });
  const response = await fetch(`${API_BASE}/analytics/models/import`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to import analytics models: ${response.status}`));
  }
  const payload = await response.json();
  return {
    ...payload,
    models: Array.isArray(payload.models) ? payload.models : [],
    count: typeof payload.count === "number" ? payload.count : 0,
  };
}

export async function planAnalyticsProjectExport(
  modelId: string,
  dataFileMode: AnalyticsProjectDataFileMode
): Promise<{ plan: AnalyticsProjectExportPlan; ready: boolean }> {
  const response = await fetch(`${API_BASE}/analytics/models/export-plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_id: modelId, data_file_mode: dataFileMode }),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to plan analytics project export: ${response.status}`));
  }
  return response.json();
}

export function analyticsProjectExportDownloadUrl(
  modelId: string,
  dataFileMode: AnalyticsProjectDataFileMode,
  expectedPlanId?: string
): string {
  const params = new URLSearchParams({ model_id: modelId, data_file_mode: dataFileMode });
  if (expectedPlanId) params.set("expected_plan_id", expectedPlanId);
  return `${API_BASE}/analytics/models/export.zip?${params.toString()}`;
}

export type SqlGuardrailActionType = "rewrite" | "block" | "warn";

export interface SqlGuardrailScope {
  table_scope: {
    mode: "any" | "all";
    values: string[];
  };
  semantic_assets: string[];
}

export interface SqlGuardrailAction {
  type: SqlGuardrailActionType;
  message: string;
}

export interface SqlGuardrailRule {
  id: string;
  name: string;
  enabled: boolean;
  type: string;
  scope: SqlGuardrailScope;
  params: Record<string, unknown>;
  action: SqlGuardrailAction;
  document_path?: string;
  document_body?: string;
  document_content?: string;
}

export interface SqlGuardrailFieldDefinition {
  path: string;
  label: string;
  type: "string" | "string_array" | "number" | string;
  required?: boolean;
}

export interface SqlGuardrailTypeDefinition {
  label: string;
  description: string;
  fields: SqlGuardrailFieldDefinition[];
}

export interface SqlGuardrailRulesResult {
  guardrails: SqlGuardrailRule[];
}

export async function listSqlGuardrailTypes(): Promise<Record<string, SqlGuardrailTypeDefinition>> {
  const response = await fetch(`${API_BASE}/analytics/sql-guardrail-types`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load SQL guardrail types: ${response.status}`));
  }
  const payload = await response.json();
  return payload.types || {};
}

export async function listSqlGuardrails(): Promise<SqlGuardrailRule[]> {
  const response = await fetch(`${API_BASE}/analytics/sql-guardrails`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load SQL guardrails: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.guardrails) ? payload.guardrails : [];
}

export async function saveSqlGuardrail(rule: SqlGuardrailRule): Promise<SqlGuardrailRule> {
  const response = await fetch(`${API_BASE}/analytics/sql-guardrails`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(rule),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to save SQL guardrail: ${response.status}`));
  }
  const payload = await response.json();
  return payload.rule;
}

export async function deleteSqlGuardrail(ruleId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/analytics/sql-guardrails/${encodeURIComponent(ruleId)}`, { method: "DELETE" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to delete SQL guardrail: ${response.status}`));
  }
}

export async function resetSqlGuardrails(): Promise<SqlGuardrailRule[]> {
  const response = await fetch(`${API_BASE}/analytics/sql-guardrails/reset`, { method: "POST" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to reset SQL guardrails: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.guardrails) ? payload.guardrails : [];
}

export interface DatabaseQueryResultSummary {
  result_id: string;
  session_id?: string;
  tool_call_id?: string;
  question: string;
  sql: string;
  columns: string[];
  row_count: number;
  profile?: Record<string, unknown>;
  artifact_path: string;
  storage_path?: string;
  artifact_format: string;
  status: string;
  expired: boolean;
  artifact_exists: boolean;
  export_enabled?: boolean;
  created_at: string;
  expires_at: string;
}

export interface DatabaseQueryResultPage {
  result_id: string;
  expired: boolean;
  status: string;
  row_count: number;
  columns: string[];
  profile?: Record<string, unknown>;
  export_enabled?: boolean;
  page: number;
  page_size: number;
  has_next?: boolean;
  has_previous?: boolean;
  rows: Record<string, unknown>[];
  expires_at: string;
  message?: string;
}

export async function listDatabaseQueryResults(limit = 50): Promise<DatabaseQueryResultSummary[]> {
  const params = new URLSearchParams({ limit: String(limit), include_expired: "true" });
  const response = await fetch(`${API_BASE}/analytics/query-results?${params.toString()}`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load query results: ${response.status}`));
  }
  const payload = await response.json();
  return Array.isArray(payload.items) ? payload.items : [];
}

export async function getDatabaseQueryResultPage(
  resultId: string,
  page = 1,
  pageSize = 100
): Promise<DatabaseQueryResultPage> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  const response = await fetch(`${API_BASE}/analytics/query-results/${encodeURIComponent(resultId)}?${params.toString()}`, {
    cache: "no-store",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to load query result page: ${response.status}`));
  }
  return response.json();
}

export function databaseQueryResultExportCsvUrl(resultId: string): string {
  return `${API_BASE}/analytics/query-results/${encodeURIComponent(resultId)}/export.csv`;
}

export async function uploadAgentAttachments(
  files: File[],
  sessionId: string,
  source: "upload" | "paste" = "upload"
): Promise<AgentAttachment[]> {
  const form = new FormData();
  form.append("session_id", sessionId);
  form.append("source", source);
  files.forEach((file) => form.append("files", file, file.name));
  const response = await fetch(`${API_BASE}/attachments`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    throw new Error(`Attachment upload failed: ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload.attachments) ? payload.attachments : [];
}

export interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled" | "error";
  position?: number;
  parent_id?: string | null;
  created_at?: number;
  updated_at?: number;
  metadata?: Record<string, unknown>;
}

export interface TraceSpan {
  id: string;
  parent_id: string | null;
  type:
    | "root"
    | "llm"
    | "model_input"
    | "tool"
    | "reasoning"
    | "todo"
    | "custom"
    | "rag"
    | "graph"
    | "middleware"
    | "memory"
    | "skill"
    | "subagent"
    | "permission";
  name: string;
  started_at: number;
  completed_at: number | null;
  status: "running" | "completed" | "error";
  input: unknown;
  output: unknown;
  metadata?: Record<string, unknown>;
  children?: TraceSpan[];
}

export interface TraceRuntimeMiddlewareEntry {
  name: string;
  order?: number;
  stack_order?: number;
  execution_order?: number;
  source?: string;
  hooks?: string[];
  note?: string;
}

export interface TraceRuntimeInventory {
  middleware?: {
    stack?: TraceRuntimeMiddlewareEntry[];
    hooks?: Record<string, TraceRuntimeMiddlewareEntry[]>;
    order_rule?: Record<string, string>;
  };
  filesystem?: {
    mounts?: Array<{
      virtual_path: string;
      root_dir?: string;
      exists?: boolean;
      role?: string;
    }>;
  };
  tools?: Array<{
    name: string;
    source?: string;
    description?: string;
  }>;
  skills?: Array<{
    name: string;
    description?: string;
    location?: string;
    system_prompt_source?: string;
    in_system_prompt?: boolean;
    href?: string;
  }>;
  subagents?: Array<{
    name: string;
    enabled?: boolean;
    model?: string;
    description?: string;
    route_trigger?: string;
    tools_mode?: string;
    skills_mode?: string;
    href?: string;
  }>;
  package_versions?: Record<string, string>;
}

export interface TraceMiddlewareEffect {
  id: string;
  category: string;
  title: string;
  hook?: string | null;
  middleware?: string[];
  before?: unknown;
  after?: unknown;
  diff?: Record<string, unknown>;
  evidence?: string[];
  metadata?: Record<string, unknown>;
  created_at?: number;
}

export interface TraceMiddlewareInvocation {
  id: string;
  hook: string;
  middleware?: string[];
  category?: string | null;
  title: string;
  invocation_index: number;
  sequence: number;
  status: "changed" | "read" | "noop" | "error" | string;
  evidence?: string[];
  before?: unknown;
  after?: unknown;
  diff?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  flow_ref?: Record<string, unknown>;
  created_at?: number;
}

export interface TraceHookBoundarySnapshot {
  id: string;
  hook: string;
  phase: "before" | "after" | string;
  title: string;
  snapshot?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  evidence?: string[];
  created_at?: number;
  sequence?: number;
}

export interface AgentTrace {
  trace_id: string;
  query_id?: string;
  session_id: string;
  started_at: number;
  completed_at: number | null;
  status: "running" | "completed" | "error";
  runtime_inventory?: TraceRuntimeInventory;
  middleware_effects?: TraceMiddlewareEffect[];
  middleware_invocations?: TraceMiddlewareInvocation[];
  hook_boundary_snapshots?: TraceHookBoundarySnapshot[];
  spans: TraceSpan[];
}

export interface GraphNode {
  id: string;
  type?: string;
  data?: unknown;
}

export interface GraphEdge {
  source: string;
  target: string;
}

export interface GraphStructure {
  nodes: GraphNode[];
  edges: GraphEdge[];
  mermaid?: string;
  mermaid_png_data_url?: string;
}

export interface PermissionGrant {
  id: string;
  type: string;
  scope: "once" | "session" | "project" | string;
  target_kind: "exact_file" | "all_external_files" | string;
  target: string;
  capabilities: string[];
  source?: string;
  created_at?: number;
  revoked_at?: number;
  consumed_at?: number;
  binding_schema_version?: number;
  semantic_key?: string;
  stable_bindings?: Record<string, unknown>;
  runtime_observations?: Record<string, unknown>;
  superseded_at?: number;
  superseded_by?: string;
  supersede_reason?: string;
  metadata?: {
    tool_name?: string;
    command?: string;
    reason?: string;
    risk?: string;
    policy_source?: string;
    policy_explanation?: string;
    control_descriptor?: Record<string, string>;
    session_scope_label?: string;
    session_target?: string;
    run_id?: string;
    change_preview?: Record<string, string>;
  };
}

export interface SessionPermissionState {
  grants: PermissionGrant[];
  history: PermissionGrant[];
}

export interface PermissionRequest {
  id: string;
  type: string;
  session_id: string;
  query_id?: string;
  tool_call_id?: string;
  path?: string;
  paths?: string[];
  authority_plane?: "shell" | string;
  grant_specs?: Array<{
    target: string;
    access: "read" | "write" | string;
    delete?: boolean;
    capabilities?: string[];
  }>;
  target_kind?: string;
  capabilities?: string[];
  operation?: string;
  tool_name?: string;
  command?: string;
  reason?: string;
  risk?: string;
  policy_source?: string;
  policy_explanation?: string;
  control_descriptor?: Record<string, string>;
  fingerprint?: string;
  semantic_key?: string;
  session_target_kind?: string;
  session_target?: string;
  session_scope_label?: string;
  options?: string[];
  change_preview?: Record<string, string>;
  status?: string;
}

export interface KernelFallbackRequest {
  id: string;
  request_id?: string;
  version: number;
  type: "kernel_fallback" | string;
  session_id: string;
  run_id: string;
  query_id?: string;
  project_id?: string | null;
  configured_mode: "kernel";
  fallback_runner: "spawn";
  platform?: string;
  availability_class: "stable" | "transient";
  reason_code: string;
  reason: string;
  probe_fingerprint: string;
  options?: Array<"switch_project_to_spawn" | "fallback_once" | "reject" | string>;
  status?: string;
}

export async function resolveKernelFallbackRequest(
  sessionId: string,
  requestId: string,
  requestVersion: number,
  action: "switch_project_to_spawn" | "fallback_once" | "reject",
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/kernel-fallback-requests/${encodeURIComponent(requestId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_version: requestVersion, action }),
    },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `处理 Kernel 回退请求失败：${response.status}`));
}

export interface DimensionBuildInputCandidate {
  id: string;
  display_name: string;
  input: {
    kind: "attachment" | "table_asset" | "database_table" | string;
    attachment_id?: string;
    asset_id?: string;
    source_id?: string;
    table?: string;
  };
  fields: string[];
  suggested_key_fields?: string[];
  suggested_output_fields?: string[];
  suggested_source_id?: string;
  suggested_source_name?: string;
}

export interface DimensionBuildRuleRequest {
  id: string;
  type: string;
  session_id: string;
  query_id?: string;
  tool_call_id?: string;
  status?: string;
  dimension_id: string;
  title: string;
  reason: string;
  operation: string;
  locked_canonical_candidate_id?: string;
  candidates: DimensionBuildInputCandidate[];
  registered_sources?: Array<{ id: string; name: string; identity_fields?: string[] }>;
  rule_template?: {
    dimension_id?: string;
    adapter?: string;
    reference_path?: string;
  };
}

export async function resolveDimensionBuildRuleRequest(
  requestId: string,
  payload: {
    action: "confirm" | "cancel";
    canonical_candidate_id?: string;
    bindings?: Array<{ candidate_id: string; key_fields: string[]; output_fields: string[]; source_id?: string; source_name?: string; source_mode?: "new" | "append" }>;
    conflict_policy?: string;
  }
): Promise<{ request_id: string; decision: Record<string, unknown>; resumed: boolean }> {
  const response = await fetch(`${API_BASE}/analytics/dimension-build-requests/${encodeURIComponent(requestId)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, "Failed to resolve dimension build rule request"));
  }
  return response.json();
}

export interface LogicalDatasetRuleRequest {
  id: string;
  type: string;
  session_id: string;
  status?: string;
  title: string;
  reason: string;
  suggested_name?: string;
  operation: "create" | "append" | string;
  target_asset_id?: string;
  target?: { asset_id: string; display_name: string; fields: string[]; rows?: number | null; sheet_name?: string | null } | null;
  candidates: Array<{ asset_id: string; display_name: string; fields: string[]; rows?: number | null; sheet_name?: string | null }>;
}

export async function resolveLogicalDatasetRuleRequest(
  requestId: string,
  payload: { action: "confirm" | "cancel"; name?: string; description?: string; tags?: string[]; baseline_asset_id?: string; source_asset_ids?: string[]; schema_mode?: "strict" | "baseline_fill_missing" | "union_fill_missing"; preferred_intents?: string[]; direct_source_allowed?: boolean }
): Promise<{ request_id: string; decision: Record<string, unknown>; resumed: boolean }> {
  const response = await fetch(`${API_BASE}/analytics/logical-dataset-requests/${encodeURIComponent(requestId)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, "Failed to resolve logical dataset rule request"));
  }
  return response.json();
}

export interface DatabaseSqlRevisionRequest {
  id: string;
  type: string;
  session_id: string;
  query_id?: string;
  tool_call_id?: string;
  status?: string;
  generation_id: string;
  original_question: string;
  original_sql: string;
  proposed_revision_instruction: string;
  semantic_assets?: {
    matched?: Array<Record<string, unknown>>;
    references?: Array<Record<string, unknown>>;
  };
}

export interface UserInputOption {
  id: string;
  label: string;
  description?: string;
  recommended?: boolean;
}

export interface UserInputQuestion {
  id: string;
  prompt: string;
  type: "single_select" | "multi_select" | "text";
  options?: UserInputOption[];
  required?: boolean;
  allow_other?: boolean;
  min_selections?: number;
  max_selections?: number | null;
  max_length?: number;
}

export interface UserInputRequest {
  id: string;
  version: number;
  type: "user_input" | string;
  session_id: string;
  query_id: string;
  run_id: string;
  goal_id?: string | null;
  goal_revision?: number | null;
  tool_call_id?: string;
  status: "pending" | "resolved" | "cancelled" | string;
  title: string;
  reason: string;
  questions: UserInputQuestion[];
  allow_agent_decide?: boolean;
  decision?: {
    action?: "submit" | "cancel" | "agent_decide" | string;
    answers?: UserInputAnswer[];
  };
}

export interface UserInputAnswer {
  question_id: string;
  option_ids: string[];
  text: string;
}

export async function resolveUserInputRequest(
  sessionId: string,
  requestId: string,
  payload: {
    request_version: number;
    action: "submit" | "cancel" | "agent_decide";
    answers?: UserInputAnswer[];
  },
): Promise<{ request_id: string; decision: Record<string, unknown>; resumed: boolean }> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/user-input-requests/${encodeURIComponent(requestId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, "Failed to resolve user input request"));
  }
  return response.json();
}

export interface SkillSecretRequest {
  id: string;
  version: number;
  type: "skill_secret" | string;
  session_id: string;
  query_id: string;
  run_id: string;
  status: "pending" | "resolved" | "cancelled" | string;
  skill_id: string;
  skill_version: string;
  env_name: string;
  reason: string;
  mode: "enter" | "reuse";
  decision?: { action?: "configured" | "cancel" | string; env_name?: string };
}

export async function resolveSkillSecretRequest(
  sessionId: string,
  requestId: string,
  payload: {
    request_version: number;
    action: "configure" | "reuse" | "cancel";
    secret_value?: string;
  },
): Promise<{ request_id: string; decision: Record<string, unknown>; resumed: boolean }> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/skill-secret-requests/${encodeURIComponent(requestId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, "Failed to configure Skill Secret"));
  }
  return response.json();
}

export async function resolveDatabaseSqlRevisionRequest(
  requestId: string,
  payload: { action: "agree" | "reject" | "modify"; revision_instruction?: string }
): Promise<{ request_id: string; decision: Record<string, unknown>; resumed: boolean }> {
  const response = await fetch(`${API_BASE}/analytics/database-sql-revision-requests/${encodeURIComponent(requestId)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, "Failed to resolve database SQL revision request"));
  }
  return response.json();
}

/**
 * LEGACY: Stream messages through the retired Chat runtime via POST SSE.
 * This compatibility client is no longer used by the main conversation UI
 * and is not maintained. New product flows must use streamAgent.
 *
 * @deprecated Use streamAgent instead.
 * Yields parsed SSE events as they arrive.
 */
export async function* streamChat(
  message: string,
  sessionId: string,
  signal?: AbortSignal,
  userId?: string
): AsyncGenerator<SSEEvent> {
  const response = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      user_id: userId || "default_user",
      stream: true
    }),
    signal,
  });

  if (!response.ok) {
    throw new Error(`Chat API error: ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    // SSE uses an empty line as the event boundary. Parsing complete frames
    // keeps event/data association correct even when a network chunk splits
    // between the two lines.
    buffer = buffer.replace(/\r\n/g, "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";

    const parsedFrames = frames
      .map((frame) => parseSSEFrame(frame))
      .filter((event): event is SSEEvent => event !== null);

    for (const parsed of parsedFrames) {

      if (parsed.event === "token" && typeof parsed.data.content === "string") {
        // Consume upstream chunks immediately. The HTTP trace proves the proxy
        // already delivers data incrementally; adding rAF/timer pacing here can
        // only create a client-side queue and delayed "replay" on long runs.
        yield parsed;
        continue;
      }

      yield parsed;
    }
  }
}

/**
 * Stream Agent-mode messages via POST SSE.
 */
export async function* streamAgent(
  message: string,
  sessionId: string,
  projectId?: string | null,
  signal?: AbortSignal,
  userId?: string,
  attachments?: AgentAttachment[],
  analyticsModelId?: string | null,
  goalMode = false,
  goalId?: string | null,
  contextGoalId?: string | null,
  goalControlAction?: "start" | null,
  skillHints?: string[],
  llmModelId?: string | null,
  thinkingLevel?: "low" | "high" | "max" | null,
  credentialName?: string | null,
): AsyncGenerator<SSEEvent> {
  const response = await fetch(`${API_BASE}/agent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      user_id: userId || "default_user",
      project_id: projectId || null,
      analytics_model_id: analyticsModelId || null,
      attachments: attachments || [],
      skill_hints: skillHints ?? null,
      llm_model_id: llmModelId || null,
      thinking_level: thinkingLevel || null,
      credential_name: credentialName || null,
      goal_mode: goalMode,
      goal_id: goalMode ? goalId || null : null,
      context_goal_id: contextGoalId || null,
      goal_control_action: goalControlAction || null,
      stream: true
    }),
    signal,
  });

  if (!response.ok) {
    throw new Error(`Agent API error: ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";

    const parsedFrames = frames
      .map((frame) => parseSSEFrame(frame))
      .filter((event): event is SSEEvent => event !== null);

    for (const parsed of parsedFrames) {
      yield parsed;
    }
  }
}

export async function getSessionHarnessState(
  sessionId: string,
): Promise<SessionHarnessState> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/harness`,
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new Error(`Failed to get Harness state: ${response.status}`);
  }
  return response.json();
}

async function transitionGoal(
  sessionId: string,
  goalId: string,
  action: "pause" | "resume" | "cancel",
): Promise<HarnessGoal> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/goals/${encodeURIComponent(goalId)}/${action}`,
    { method: "POST" },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(apiErrorMessage(text, `Failed to ${action} Goal: ${response.status}`));
  }
  return response.json();
}

export const pauseGoal = (sessionId: string, goalId: string) =>
  transitionGoal(sessionId, goalId, "pause");

export const resumeGoal = (sessionId: string, goalId: string) =>
  transitionGoal(sessionId, goalId, "resume");

export const cancelGoal = (sessionId: string, goalId: string) =>
  transitionGoal(sessionId, goalId, "cancel");

export async function extendGoalBudget(
  sessionId: string,
  goalId: string,
  additionalRounds: number,
): Promise<HarnessGoal> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/goals/${encodeURIComponent(goalId)}/extend-budget`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ additional_rounds: additionalRounds }),
    },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(apiErrorMessage(text, `Failed to extend Goal budget: ${response.status}`));
  }
  return response.json();
}

export async function updateGoalObjective(
  sessionId: string,
  goalId: string,
  objective: string,
  expectedRevision: number,
): Promise<HarnessGoal> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/goals/${encodeURIComponent(goalId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        objective,
        expected_revision: expectedRevision,
      }),
    },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(apiErrorMessage(text, `Failed to update Goal: ${response.status}`));
  }
  return response.json();
}

function parseSSEFrame(frame: string): SSEEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  try {
    const data = JSON.parse(dataLines.join("\n"));
    return { event, data };
  } catch {
    return null;
  }
}

/**
 * Read a file from the backend.
 */
export async function readFile(path: string): Promise<string> {
  const resp = await fetch(`${API_BASE}/files?path=${encodeURIComponent(path)}`);
  if (!resp.ok) throw new Error(`Failed to read file: ${resp.status}`);
  const data = await resp.json();
  return data.content;
}

/**
 * Save a file to the backend.
 */
export async function saveFile(path: string, content: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content }),
  });
  if (!resp.ok) throw new Error(`Failed to save file: ${resp.status}`);
}

/**
 * List all sessions.
 */
export async function listSessions(): Promise<
  Array<{
    id: string;
    title: string;
    updated_at: number;
    runtime_mode?: "agent" | "chat";
    project_id?: string | null;
    project_path?: string | null;
    workspace_type?: string;
    workspace_path?: string;
    analytics_model_id?: string | null;
    llm_model_id?: string | null;
    thinking_level?: "low" | "high" | "max" | null;
    credential_name?: string | null;
    approval_mode?: ApprovalMode;
    policy_epoch?: number;
    policy_version?: string;
  }>
> {
  const resp = await fetch(`${API_BASE}/sessions`);
  if (!resp.ok) throw new Error(`Failed to list sessions: ${resp.status}`);
  const data = await resp.json();
  return data.sessions;
}

export interface SessionSearchResult {
  id: string;
  title: string;
  updated_at: number;
  runtime_mode?: "agent" | "chat";
  project_id?: string | null;
  project_path?: string | null;
  snippet: string;
  matched_in: "title" | "content";
}

/** Search session titles and visible conversation content. */
export async function searchSessions(
  query: string,
  signal?: AbortSignal,
): Promise<SessionSearchResult[]> {
  const params = new URLSearchParams({
    q: query.trim(),
    limit: "50",
  });
  const resp = await fetch(`${API_BASE}/sessions/search?${params.toString()}`, {
    cache: "no-store",
    signal,
  });
  if (!resp.ok) throw new Error(`Failed to search sessions: ${resp.status}`);
  const data = await resp.json() as { results?: SessionSearchResult[] };
  return Array.isArray(data.results) ? data.results : [];
}

export interface ProjectMeta {
  project_id: string;
  name: string;
  path: string;
  created_at: number;
  updated_at: number;
  pinned?: boolean;
  execution_mode?: "spawn" | "kernel" | null;
  trust_state: "pending" | "trusted" | "denied";
  identity_digest?: string;
}

export async function listProjects(): Promise<ProjectMeta[]> {
  const resp = await fetch(`${API_BASE}/projects`);
  if (!resp.ok) throw new Error(`Failed to list projects: ${resp.status}`);
  const data = await resp.json();
  return data.projects;
}

export async function registerProject(path: string, name?: string): Promise<ProjectMeta> {
  const resp = await fetch(`${API_BASE}/projects/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // Selecting or pasting a local directory in PuddingClaw is the user's
    // explicit authorization of that exact workspace identity.
    body: JSON.stringify({ path, name, authorize: true }),
  });
  if (!resp.ok) throw new Error(`Failed to register project: ${resp.status}`);
  return resp.json();
}

export async function openProject(projectId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/open`, {
    method: "POST",
  });
  if (!resp.ok) throw new Error(`Failed to open project: ${resp.status}`);
}

export async function openLocalFile(path: string, sessionId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/local-files/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, session_id: sessionId }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(apiErrorMessage(text, `Failed to open file: ${resp.status}`));
  }
}

export async function updateProject(
  projectId: string,
  update: { name?: string; pinned?: boolean; execution_mode?: "spawn" | "kernel" }
): Promise<ProjectMeta> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  if (!resp.ok) throw new Error(`Failed to update project: ${resp.status}`);
  return resp.json();
}

export async function setProjectTrust(
  projectId: string,
  state: "pending" | "trusted" | "denied",
): Promise<ProjectMeta> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/trust`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state }),
  });
  if (!resp.ok) {
    const responseText = await resp.text();
    throw new Error(apiErrorMessage(responseText, `Failed to update project trust: ${resp.status}`));
  }
  return resp.json();
}

export async function removeProject(projectId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw new Error(`Failed to remove project: ${resp.status}`);
}

export async function listSessionPermissions(sessionId: string): Promise<SessionPermissionState> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions`);
  if (!resp.ok) throw new Error(`Failed to list permissions: ${resp.status}`);
  const data = await resp.json();
  return {
    grants: Array.isArray(data.grants) ? data.grants : [],
    history: Array.isArray(data.history) ? data.history : [],
  };
}

export async function grantExternalFilePermission(
  sessionId: string,
  targetKind: "exact_file" | "exact_directory" | "all_external_files",
  path?: string,
  permissionRequestId?: string,
  scope?: "run" | "session",
): Promise<PermissionGrant> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/external-files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target_kind: targetKind,
      path,
      permission_request_id: permissionRequestId,
      scope,
    }),
  });
  if (!resp.ok) throw new Error(`Failed to grant external file permission: ${resp.status}`);
  const data = await resp.json();
  return data.grant;
}

export async function grantToolActionPermission(
  sessionId: string,
  permissionRequestId: string,
  scope: "once" | "session" | "project",
): Promise<PermissionGrant> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/tool-actions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        permission_request_id: permissionRequestId,
        scope,
      }),
    },
  );
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(apiErrorMessage(text, `Failed to grant Tool permission: ${resp.status}`));
  }
  const data = await resp.json();
  return data.grant;
}

export async function grantShellDirectoryPermission(
  sessionId: string,
  permissionRequestId: string,
  scope: "run" | "session",
): Promise<PermissionGrant[]> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/shell-directories`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        permission_request_id: permissionRequestId,
        scope,
      }),
    },
  );
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(apiErrorMessage(text, `Failed to grant shell directory permission: ${resp.status}`));
  }
  const data = await resp.json();
  return Array.isArray(data.grants) ? data.grants : [];
}

export async function revokePermissionGrant(sessionId: string, grantId: string): Promise<void> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/${encodeURIComponent(grantId)}/revoke`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to revoke permission: ${resp.status}`);
}

export async function denyPermissionRequest(
  sessionId: string,
  permissionRequestId: string,
  message?: string
): Promise<void> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/deny`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ permission_request_id: permissionRequestId, message }),
  });
  if (!resp.ok) throw new Error(`Failed to deny permission: ${resp.status}`);
}

/**
 * Create a new session.
 */
export type ApprovalMode = "strict" | "smart";

export interface CreateSessionOptions {
  analytics_model_id?: string | null;
  llm_model_id?: string | null;
  thinking_level?: "low" | "high" | "max" | null;
  credential_name?: string | null;
  approval_mode?: ApprovalMode;
  runtime_mode?: "agent" | "chat";
  project_id?: string | null;
}

export interface PermissionModeState {
  session_id: string;
  approval_mode: ApprovalMode;
  policy_epoch: number;
  policy_version: string;
}

export async function createSession(options: CreateSessionOptions = {}): Promise<{
  id: string;
  title: string;
  created_at?: number;
  updated_at?: number;
  runtime_mode?: "agent" | "chat";
  project_id?: string | null;
  analytics_model_id?: string | null;
  llm_model_id?: string | null;
  thinking_level?: "low" | "high" | "max" | null;
  credential_name?: string | null;
  approval_mode: ApprovalMode;
  policy_epoch: number;
  policy_version: string;
}> {
  const resp = await fetch(`${API_BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(options),
  });
  if (!resp.ok) throw new Error(`Failed to create session: ${resp.status}`);
  return resp.json();
}

export async function updateSessionLlmSelection(
  sessionId: string,
  llmModelId: string,
  thinkingLevel: "low" | "high" | "max" | null,
  credentialName: string | null = null,
): Promise<{
  id: string;
  llm_model_id?: string | null;
  thinking_level?: "low" | "high" | "max" | null;
  credential_name?: string | null;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/llm-selection`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        llm_model_id: llmModelId,
        thinking_level: thinkingLevel,
        credential_name: credentialName,
      }),
    },
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to update conversation model: ${resp.status}`);
  }
  return resp.json();
}

export async function getSessionApprovalMode(
  sessionId: string,
): Promise<PermissionModeState> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/mode`,
    { cache: "no-store" },
  );
  if (!resp.ok) throw new Error(`Failed to get approval mode: ${resp.status}`);
  return resp.json();
}

export async function updateSessionApprovalMode(
  sessionId: string,
  approvalMode: ApprovalMode,
  expectedEpoch?: number,
): Promise<PermissionModeState> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/mode`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approval_mode: approvalMode,
        expected_epoch: expectedEpoch,
      }),
    },
  );
  if (!resp.ok) {
    const payload = await resp.json().catch(() => null) as { detail?: unknown } | null;
    const detail = typeof payload?.detail === "string" ? payload.detail : "";
    const localized = resp.status === 409
      ? detail.toLowerCase().includes("active run")
        ? "当前 Run 仍在进行，完成后才能切换授权模式。"
        : "授权模式已在其他位置更新，请刷新后重试。"
      : resp.status === 404
        ? "当前会话已不存在，请新建会话后重试。"
        : "授权模式更新失败，请稍后重试。";
    throw new Error(localized);
  }
  return resp.json();
}


/** Persist or clear the analytics model selected for a session. */
export async function updateSessionAnalyticsModel(
  sessionId: string,
  analyticsModelId: string | null
): Promise<void> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/analytics-model`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ analytics_model_id: analyticsModelId }),
    }
  );
  if (!resp.ok) throw new Error(`Failed to update session analytics model: ${resp.status}`);
}

/**
 * Rename a session.
 */
export async function renameSession(id: string, title: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!resp.ok) throw new Error(`Failed to rename session: ${resp.status}`);
}

/**
 * Delete a session.
 */
export async function deleteSession(id: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw new Error(`Failed to delete session: ${resp.status}`);
}

/**
 * Get raw messages for a session (including system prompt).
 */
export async function getRawMessages(
  sessionId: string
): Promise<{
  session_id: string;
  title: string;
  messages: Array<{ role: string; content: string }>;
  todos?: TodoItem[];
  todos_authority?: { kind: "legacy" | "none" | "goal" | "run"; goal_id?: string; goal_revision?: number; run_id?: string };
  todo_ledger_revision?: number;
  graph?: GraphStructure | null;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/messages`
  );
  if (!resp.ok) throw new Error(`Failed to get raw messages: ${resp.status}`);
  return resp.json();
}

/** Load heavyweight Agent traces on demand, separately from chat history. */
export async function getSessionTraces(
  sessionId: string
): Promise<{
  session_id: string;
  trace?: AgentTrace | null;
  traces: Record<string, AgentTrace>;
  latest_query_id?: string | null;
  latest_trace_id?: string | null;
  todos?: TodoItem[];
  todos_authority?: { kind: "legacy" | "none" | "goal" | "run"; goal_id?: string; goal_revision?: number; run_id?: string };
  todo_ledger_revision?: number;
  graph?: GraphStructure | null;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/traces`
  );
  if (!resp.ok) throw new Error(`Failed to get session traces: ${resp.status}`);
  return resp.json();
}

/**
 * Get session conversation history (no system prompt, includes tool_calls).
 */
export async function getSessionHistory(
  sessionId: string
): Promise<{
  session_id: string;
  todos?: TodoItem[];
  todos_authority?: { kind: "legacy" | "none" | "goal" | "run"; goal_id?: string; goal_revision?: number; run_id?: string };
  todo_ledger_revision?: number;
  graph?: GraphStructure | null;
  headless_pending_input?: {
    status?: string;
    run_id?: string | null;
    query_id?: string | null;
    requests?: PermissionRequest[];
    updated_at?: number;
  };
  messages: Array<{
    role: string;
    content: string;
    created_at?: number;
    query_id?: string;
    attachments?: AgentAttachment[];
    output_attachments?: AgentAttachment[];
    reasoning_content?: string;
    tool_calls?: Array<{ tool: string; input?: string; output?: string }>;
  }>;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/history`
  );
  if (!resp.ok) throw new Error(`Failed to get session history: ${resp.status}`);
  return resp.json();
}

export interface CurrentTodosSnapshot {
  session_id: string;
  todos: TodoItem[];
  authority: { kind: "legacy" | "none" | "goal" | "run"; goal_id?: string; goal_revision?: number; run_id?: string };
  ledger_revision: number;
}

export async function getCurrentSessionTodos(sessionId: string): Promise<CurrentTodosSnapshot> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/todos/current`,
    { cache: "no-store" },
  );
  if (!resp.ok) throw new Error(`Failed to get current Todos: ${resp.status}`);
  return resp.json();
}

/**
 * List available skills.
 */
export async function listSkills(): Promise<
  Array<{ name: string; path: string; description: string }>
> {
  const resp = await fetch(`${API_BASE}/skills`);
  if (!resp.ok) throw new Error(`Failed to list skills: ${resp.status}`);
  const data = await resp.json();
  return data.skills;
}

/**
 * List enabled MCP servers.
 */
export async function listMcpServers(): Promise<
  Array<{ key: string; name: string; url: string; transport: string }>
> {
  const resp = await fetch(`${API_BASE}/mcp/servers`);
  if (!resp.ok) throw new Error(`Failed to list MCP servers: ${resp.status}`);
  const data = await resp.json();
  return data.servers;
}

export interface McpServerConfig {
  name?: string;
  transport: "stdio" | "sse" | "streamable-http";
  url?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  headers?: Record<string, string>;
  timeout?: number;
}

export interface McpConfig {
  enabled: string[];
  servers: Record<string, McpServerConfig>;
}

export interface McpConfigPayload {
  path: string;
  config: McpConfig;
}

export async function getMcpConfig(): Promise<McpConfigPayload> {
  const resp = await fetch(`${API_BASE}/mcp/config`, { cache: "no-store" });
  if (!resp.ok) throw new Error(`Failed to load MCP config: ${resp.status}`);
  return resp.json() as Promise<McpConfigPayload>;
}

export async function updateMcpConfig(config: McpConfig): Promise<McpConfigPayload> {
  const resp = await fetch(`${API_BASE}/mcp/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
  const data = await resp.json().catch(() => null) as { detail?: string } | null;
  if (!resp.ok) throw new Error(data?.detail || `Failed to save MCP config: ${resp.status}`);
  mcpStatusProbeInFlight = null;
  return data as McpConfigPayload;
}

export interface McpServerStatus {
  key: string;
  name: string;
  url: string;
  transport: string;
  enabled: boolean;
  auto_enabled: boolean;
  ready: boolean;
  loaded: boolean;
  status: "ready" | "loaded" | "not_ready" | "error";
  reason: string;
  tools: string[];
  tool_count: number;
}

export interface McpServersStatus {
  servers: Array<{ key: string; name: string; url: string; transport: string }>;
  catalog: McpServerStatus[];
  gbrain: {
    configured: boolean;
    ready: boolean;
    reason: string;
    home?: string;
    binary?: string;
    config_exists?: boolean;
    pack_exists?: boolean;
    models?: {
      embedding?: { name: string; provider: string; dimension: number };
      think?: { name: string; provider: string };
    } | null;
  };
}

let mcpStatusProbeInFlight: Promise<McpServersStatus> | null = null;

export async function getMcpServersStatus(probe = true): Promise<McpServersStatus> {
  if (probe && mcpStatusProbeInFlight) return mcpStatusProbeInFlight;
  const request = (async () => {
    const resp = await fetch(`${API_BASE}/mcp/servers?probe=${probe ? "true" : "false"}`, {
      cache: "no-store",
    });
    if (!resp.ok) throw new Error(`Failed to inspect MCP servers: ${resp.status}`);
    return resp.json() as Promise<McpServersStatus>;
  })();
  if (!probe) return request;
  mcpStatusProbeInFlight = request;
  try {
    return await request;
  } finally {
    if (mcpStatusProbeInFlight === request) mcpStatusProbeInFlight = null;
  }
}

/**
 * Load a skill into the current session.
 */
export async function loadSkill(skillName: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/skills/load`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skill_name: skillName }),
  });
  if (!resp.ok) throw new Error(`Failed to load skill: ${resp.status}`);
}

/**
 * Generate a title for a session using AI.
 */
export async function generateTitle(
  sessionId: string
): Promise<{ title: string }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/generate-title`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to generate title: ${resp.status}`);
  return resp.json();
}

/**
 * Get token count for a session (system + messages).
 */
export async function getSessionTokenCount(
  sessionId: string,
  runtimeMode?: "agent" | "chat",
): Promise<{
  system_tokens: number;
  message_tokens: number;
  total_tokens: number;
  compaction_trigger: number;
  percentage: number;
  measured: boolean;
}> {
  const params = new URLSearchParams();
  if (runtimeMode) params.set("runtime_mode", runtimeMode);
  const query = params.toString();
  const resp = await fetch(
    `${API_BASE}/tokens/session/${encodeURIComponent(sessionId)}${query ? `?${query}` : ""}`
  );
  if (!resp.ok) throw new Error(`Failed to get token count: ${resp.status}`);
  return resp.json();
}

/**
 * Get token counts for a list of files.
 */
export async function getFileTokenCounts(
  paths: string[]
): Promise<{ files: Array<{ path: string; tokens: number }> }> {
  const resp = await fetch(`${API_BASE}/tokens/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
  if (!resp.ok) throw new Error(`Failed to get file token counts: ${resp.status}`);
  return resp.json();
}

export interface AgentCompactResult {
  status: "completed";
  session_id: string;
  operation_id: string;
  trigger: "manual";
  tokens_before: number;
  tokens_after: number;
  tokens_reduced: number;
  reduction_percentage: number;
  summarized_message_count: number;
  kept_recent_message_count: number;
  source_query_id: string;
  source_run_id: string;
  projection_version: number;
  summary_model: string;
}

interface AgentCompactOperation extends Omit<Partial<AgentCompactResult>, "status" | "session_id" | "operation_id"> {
  status: "running" | "completed" | "failed" | "expired";
  session_id: string;
  operation_id: string;
  started_at?: number;
  completed_at?: number;
  error?: string;
}

/**
 * Manually compact an idle Agent Session's model context projection.
 * The visible/raw transcript and control-plane ledgers are not modified.
 */
export async function compactAgentSession(
  sessionId: string,
  focus = "",
): Promise<AgentCompactResult> {
  const resp = await fetch(
    `${API_BASE}/agent/sessions/${encodeURIComponent(sessionId)}/compact`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ focus }),
    },
  );
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(apiErrorMessage(text, `Agent 上下文压缩失败：${resp.status}`));
  }
  const operation = JSON.parse(text) as AgentCompactOperation;
  if (!operation.operation_id) {
    throw new Error("Agent 上下文压缩未返回 operation_id。");
  }

  const deadline = Date.now() + 16 * 60 * 1000;
  let consecutivePollFailures = 0;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 750));
    let terminalFailure: Error | null = null;
    try {
      const statusResp = await fetch(
        `${API_BASE}/agent/sessions/${encodeURIComponent(sessionId)}/compact/${encodeURIComponent(operation.operation_id)}`,
        { cache: "no-store" },
      );
      const statusText = await statusResp.text();
      if (!statusResp.ok) {
        throw new Error(apiErrorMessage(statusText, `读取压缩状态失败：${statusResp.status}`));
      }
      consecutivePollFailures = 0;
      const status = JSON.parse(statusText) as AgentCompactOperation;
      if (status.status === "completed") return status as AgentCompactResult;
      if (status.status === "failed" || status.status === "expired") {
        terminalFailure = new Error(
          status.error || `Agent 上下文压缩已${status.status === "expired" ? "超时" : "失败"}。`,
        );
      }
    } catch (error) {
      consecutivePollFailures += 1;
      if (consecutivePollFailures >= 5) throw error;
    }
    if (terminalFailure) throw terminalFailure;
  }
  throw new Error("等待 Agent 上下文压缩结果超时；可稍后重试或刷新 Session 状态。");
}

/**
 * Clear all messages in a session (like Claude Code /clear).
 */
export async function clearSession(
  sessionId: string
): Promise<{ status: string; session_id: string }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/clear`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to clear session: ${resp.status}`);
  return resp.json();
}
