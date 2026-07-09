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

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface AgentAttachment {
  type: "image" | "pdf" | "spreadsheet" | "markdown" | "text" | "document" | "file";
  id?: string;
  name?: string;
  mime_type?: string;
  path?: string;
  size?: number;
  source?: "upload" | "paste";
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
  profile?: TableAssetProfile;
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
  const payload = await response.json();
  return payload.asset;
}

export async function generateTableAssetProfile(assetId: string): Promise<TableAsset> {
  const response = await fetch(`${API_BASE}/analytics/table-assets/${encodeURIComponent(assetId)}/profile`, {
    method: "POST",
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, `Failed to generate table profile: ${response.status}`));
  }
  const payload = await response.json();
  return payload.asset;
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

export type SemanticAssetType = "measure" | "dimension" | "grain";

export interface SemanticAssetSummary {
  id: string;
  name: string;
  type: SemanticAssetType;
  path: string;
  description?: string;
  aliases?: string[];
  tags?: string[];
  formatter?: string;
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

export interface SemanticAssetListResult {
  assets: SemanticAssetSummary[];
  count: number;
  type_counts?: Record<string, number>;
  root_dir?: string;
  last_scanned_at?: string | null;
}

export interface SemanticAssetCreatePayload {
  name: string;
  type: SemanticAssetType;
  description?: string;
  aliases?: string[];
  tags?: string[];
  version?: string;
  slug?: string;
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
  status: "pending" | "in_progress" | "completed" | "error";
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
  scope: "once" | "session" | string;
  target_kind: "exact_file" | "all_external_files" | string;
  target: string;
  capabilities: string[];
  source?: string;
  created_at?: number;
  revoked_at?: number;
}

export interface PermissionRequest {
  id: string;
  type: string;
  session_id: string;
  query_id?: string;
  tool_call_id?: string;
  path?: string;
  target_kind?: string;
  capabilities?: string[];
  status?: string;
}

/**
 * Stream chat messages via POST SSE.
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
  attachments?: AgentAttachment[]
): AsyncGenerator<SSEEvent> {
  const response = await fetch(`${API_BASE}/agent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      user_id: userId || "default_user",
      project_id: projectId || null,
      attachments: attachments || [],
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
  }>
> {
  const resp = await fetch(`${API_BASE}/sessions`);
  if (!resp.ok) throw new Error(`Failed to list sessions: ${resp.status}`);
  const data = await resp.json();
  return data.sessions;
}

export interface ProjectMeta {
  project_id: string;
  name: string;
  path: string;
  created_at: number;
  updated_at: number;
  pinned?: boolean;
}

export interface ProjectContextDocument {
  project_id: string;
  content: string;
  path: string;
  is_project_local: boolean;
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
    body: JSON.stringify({ path, name }),
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

export async function updateProject(
  projectId: string,
  update: { name?: string; pinned?: boolean }
): Promise<ProjectMeta> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  if (!resp.ok) throw new Error(`Failed to update project: ${resp.status}`);
  return resp.json();
}

export async function removeProject(projectId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw new Error(`Failed to remove project: ${resp.status}`);
}

export async function getProjectContext(projectId: string): Promise<ProjectContextDocument> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/context`);
  if (!resp.ok) throw new Error(`Failed to get project context: ${resp.status}`);
  return resp.json();
}

export async function updateProjectContext(
  projectId: string,
  content: string
): Promise<ProjectContextDocument> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/context`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  if (!resp.ok) throw new Error(`Failed to update project context: ${resp.status}`);
  return resp.json();
}

export async function listSessionPermissions(sessionId: string): Promise<PermissionGrant[]> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions`);
  if (!resp.ok) throw new Error(`Failed to list permissions: ${resp.status}`);
  const data = await resp.json();
  return data.grants || [];
}

export async function grantExternalFileRead(
  sessionId: string,
  targetKind: "exact_file" | "all_external_files",
  path?: string,
  permissionRequestId?: string
): Promise<PermissionGrant> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/external-files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_kind: targetKind, path, permission_request_id: permissionRequestId }),
  });
  if (!resp.ok) throw new Error(`Failed to grant external file permission: ${resp.status}`);
  const data = await resp.json();
  return data.grant;
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
export async function createSession(): Promise<{
  id: string;
  title: string;
  created_at?: number;
  updated_at?: number;
  runtime_mode?: "agent" | "chat";
}> {
  const resp = await fetch(`${API_BASE}/sessions`, { method: "POST" });
  if (!resp.ok) throw new Error(`Failed to create session: ${resp.status}`);
  return resp.json();
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
  trace?: AgentTrace | null;
  traces?: Record<string, AgentTrace>;
  latest_query_id?: string;
  latest_trace_id?: string;
  graph?: GraphStructure | null;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/messages`
  );
  if (!resp.ok) throw new Error(`Failed to get raw messages: ${resp.status}`);
  return resp.json();
}

/**
 * Get session conversation history (no system prompt, includes tool_calls).
 */
export async function getSessionHistory(
  sessionId: string
): Promise<{
  session_id: string;
  messages: Array<{
    role: string;
    content: string;
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
  sessionId: string
): Promise<{ system_tokens: number; message_tokens: number; total_tokens: number; compaction_trigger: number; percentage: number }> {
  const resp = await fetch(
    `${API_BASE}/tokens/session/${encodeURIComponent(sessionId)}`
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

/**
 * Compress a session's conversation history.
 */
export async function compressSession(
  sessionId: string
): Promise<{ archived_count: number; remaining_count: number }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/compress`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to compress session: ${resp.status}`);
  return resp.json();
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

/**
 * Get current RAG mode status.
 */
export async function getRagMode(): Promise<{ rag_mode: boolean }> {
  const resp = await fetch(`${API_BASE}/config/rag-mode`);
  if (!resp.ok) throw new Error(`Failed to get RAG mode: ${resp.status}`);
  return resp.json();
}

/**
 * Set RAG mode enabled/disabled.
 */
export async function setRagMode(
  enabled: boolean
): Promise<{ rag_mode: boolean }> {
  const resp = await fetch(`${API_BASE}/config/rag-mode`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!resp.ok) throw new Error(`Failed to set RAG mode: ${resp.status}`);
  return resp.json();
}
