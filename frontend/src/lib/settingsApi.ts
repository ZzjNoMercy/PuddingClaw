/**
 * Settings API client for PuddingClaw backend.
 */

const API_BASE = "/api";

export interface FallbackLlmSettings {
  provider: string;
  model: string;
  base_url: string;
  api_key_masked: string;
  temperature: number;
  max_tokens: number;
}

export interface GatewayLlmSettings {
  model: string;
}

export interface GatewaySettings {
  enabled: boolean;
  base_url: string;
  health_path: string;
  fallback_to_direct: boolean;
  environment_override: boolean;
  routed_models: string[];
}

export interface FallbackEmbeddingSettings {
  provider: string;
  model: string;
  base_url: string;
  dimension?: number;
  batch_size?: number;
  api_key_masked: string;
}

export interface MultimodalEmbeddingSettings {
  provider: string;
  model: string;
  dimension: number;
  batch_size?: number;
  base_url: string;
  route_path: string;
  prefer_gateway: boolean;
  api_key_masked: string;
  effective_model?: string;
  effective_dimension?: number;
  gateway_route_required?: boolean;
  openai_compatible?: boolean;
}

export interface RagSettings {
  enabled: boolean;
  top_k: number;
  similarity_threshold: number;
  hybrid?: {
    enabled: boolean;
    mode: string;
    text_vector_weight: number;
    image_vector_weight: number;
    bm25_weight: number;
    candidate_top_k: number;
  };
  rerank?: {
    enabled: boolean;
    provider?: string;
    model: string;
    top_n: number;
    candidate_top_k: number;
    base_url?: string;
    api_key_masked?: string;
  };
}

export interface VannaSettings {
  enabled: boolean;
  default_database_source_id: string;
  default_dialect: string;
  query: {
    entity_top_k_default: number;
    entity_top_k_by_type: Record<string, number>;
  };
}

export interface DatabaseQaSettings {
  full_rows_token_budget: number;
  preview_rows_token_budget: number;
  profile_token_budget: number;
  full_rows_hard_row_cap: number;
  full_rows_hard_column_cap: number;
  max_cell_chars_for_llm: number;
  query_timeout_ms: number;
  result_store_enabled: boolean;
  result_store_ttl_hours: number;
  default_page_size: number;
  max_page_size: number;
  export_enabled: boolean;
  profile_enabled: boolean;
}

export interface AnalyticsSettings {
  database_qa: DatabaseQaSettings;
}

export interface KnowledgeSettings {
  root_dir: string;
  configured_by?: string;
  environment_override?: boolean;
  mineru?: {
    base_url: string;
    runtime_output_dir: string;
    keep_runtime_output: boolean;
  };
  multimodal_index: {
    enabled: boolean;
    vector_store: string;
    milvus_uri: string;
    text_collection: string;
    image_collection: string;
    overwrite?: boolean;
  };
}

export interface DatabaseSettings {
  mode: "bundled" | "external";
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  url: string;
  configured_url?: string;
  configured_by?: string;
  environment_override?: boolean;
}

export interface CompressionSettings {
  ratio: number;
}

export interface HarnessSettings {
  model_call_limit: {
    enabled: boolean;
    run_limit: number | null;
    thread_limit: number | null;
    exit_behavior: "end" | "error";
  };
}

export interface SubAgentItem {
  enabled: boolean;
  name: string;
  model: string;
  description: string;
  route_trigger: string;
  tools: {
    mode: "inherit" | "none";
  };
  skills: {
    mode: "inherit" | "custom" | "none";
    paths: string[];
  };
  system_prompt: string;
}

export interface SubAgentSettings {
  items: SubAgentItem[];
}

export interface SystemSettings {
  thinking_mode: boolean;
  ai_gateway: GatewaySettings;
  gateway_llm: GatewayLlmSettings;
  fallback_llm: FallbackLlmSettings;
  fallback_embedding: FallbackEmbeddingSettings;
  multimodal_embedding: MultimodalEmbeddingSettings;
  rag: RagSettings;
  vanna?: VannaSettings;
  analytics?: AnalyticsSettings;
  database: DatabaseSettings;
  knowledge: KnowledgeSettings;
  compression: CompressionSettings;
  harness: HarnessSettings;
  subagents: SubAgentSettings;
  subagent?: SubAgentSettings;
}

export async function getSettings(): Promise<SystemSettings> {
  const resp = await fetch(`${API_BASE}/settings`);
  if (!resp.ok) throw new Error(`Failed to get settings: ${resp.status}`);
  return resp.json();
}

export async function updateSettings(updates: Record<string, unknown>): Promise<void> {
  const resp = await fetch(`${API_BASE}/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to save settings: ${resp.status}`);
  }
}

export interface ResetKnowledgeVectorResult {
  ok: boolean;
  milvus_uri: string;
  dropped: string[];
  missing: string[];
}

export async function resetKnowledgeVectorCollections(): Promise<ResetKnowledgeVectorResult> {
  const resp = await fetch(`${API_BASE}/knowledge/vector/reset`, { method: "POST" });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to reset vector collections: ${resp.status}`);
  }
  return resp.json();
}

export interface TestConnectionResult {
  success: boolean;
  model: string;
  latency_ms: number;
  response_model?: string;
  dimensions?: number;
}

export async function testConnection(params: {
  type: "gateway" | "llm" | "embedding";
  provider?: string;
  model?: string;
  base_url: string;
  api_key?: string;
  health_path?: string;
}): Promise<TestConnectionResult> {
  const resp = await fetch(`${API_BASE}/settings/test-connection`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Connection test failed: ${resp.status}`);
  }
  return resp.json();
}

export interface TestDatabaseConnectionResult {
  success: boolean;
  created: boolean;
  database_missing: boolean;
  can_create: boolean;
  latency_ms: number;
  message: string;
  server_version?: string;
  safe_url?: string;
}

export async function testDatabaseConnection(params: {
  mode: "bundled" | "external";
  host?: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  create_if_missing?: boolean;
}): Promise<TestDatabaseConnectionResult> {
  const resp = await fetch(`${API_BASE}/settings/database/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || `Database connection test failed: ${resp.status}`);
  }
  return data;
}

export interface CapabilityStatus {
  available: boolean;
  reason: string | null;
}

export interface Capabilities {
  database: CapabilityStatus;
  ai_gateway: CapabilityStatus;
  milvus: CapabilityStatus;
  mineru: CapabilityStatus;
}

export async function getCapabilities(): Promise<Capabilities> {
  const resp = await fetch(`${API_BASE}/capabilities`, { cache: "no-store" });
  if (!resp.ok) throw new Error(`Failed to get capabilities: ${resp.status}`);
  return resp.json();
}
