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
  context_window?: number;
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
  result_materialization_row_cap: number;
  query_timeout_ms: number;
  sql_generation_timeout_ms: number;
  result_store_enabled: boolean;
  result_store_ttl_hours: number;
  default_page_size: number;
  max_page_size: number;
  export_enabled: boolean;
  profile_enabled: boolean;
  database_agent_sql_path_enabled: boolean;
  database_agent_sql_path_rollout_percentage: number;
  database_agent_sql_fallback_enabled: boolean;
  database_agent_sql_shadow_compare_enabled: boolean;
}

export interface AnalyticsSettings {
  database_qa: DatabaseQaSettings;
}

export interface KnowledgeSettings {
  root_dir: string;
  configured_by?: string;
  environment_override?: boolean;
  llm_wiki?: {
    compiler_agent?: {
      model_id: string;
    };
    retrieval?: {
      hybrid_enabled: boolean;
    };
    gbrain?: {
      embedding_model_id: string;
      think_model_id: string;
    };
  };
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
    legacy_text_collection?: string;
    image_collection: string;
    bm25_enabled?: boolean;
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
  deepagents?: {
    summarization?: {
      enabled?: boolean;
      model_id?: string;
      trigger_tokens?: number;
      keep_messages?: number;
    };
    tool_context?: {
      enabled?: boolean;
      immediate_compaction_enabled?: boolean;
      single_tool_trigger_tokens?: number;
      background_min_result_tokens?: number;
      keep_recent_tool_results?: number;
    };
  };
}

export interface HarnessSettings {
  prompt_cache?: {
    trace_part_diagnostics?: boolean;
    ordered_system_sections?: boolean;
    tail_routing_message?: boolean;
    deterministic_session_projection?: boolean;
    stable_tool_schema?: boolean;
  };
  model_call_limit: {
    enabled: boolean;
    run_limit: number | null;
    thread_limit: number | null;
    exit_behavior: "end" | "error";
  };
  completion?: {
    rubric?: {
      enabled?: boolean;
      model?: string;
      max_iterations?: number;
      max_stagnant_repairs?: number;
      custom_rules_enabled?: boolean;
      custom_rules?: Array<{
        id: string;
        enabled: boolean;
        statement: string;
        required: boolean;
        verifier: "analytics" | "llm_grader";
      }>;
    };
  };
  goals?: {
    enabled?: boolean;
    activation?: "explicit_user_only";
    default_enabled?: false;
    auto_promote_from_run?: false;
    max_rounds?: number;
  };
  terminal?: {
    sandbox_mode?: "auto" | "kernel" | "docker";
    docker_enabled?: boolean;
    on_unavailable?: "fallback" | "deny";
    default_timeout_seconds?: number;
    docker?: {
      connection?: string;
      context?: string;
      image?: string;
      cpu_limit?: string;
      memory_limit_mb?: number;
      pids_limit?: number;
      network_enabled?: boolean;
      dependency_setup_enabled?: boolean;
      dependency_setup_opt_in_version?: number;
      lifecycle?: "project";
      idle_stop_minutes?: number;
    };
  };
}

export async function probeHarnessDocker(input: {
  connection?: string;
  context?: string;
}): Promise<{ available: boolean; detail: string }> {
  const resp = await fetch(`${API_BASE}/settings/harness/docker/probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      connection: input.connection || "",
      context: input.context || "",
    }),
  });
  if (!resp.ok) throw new Error(`Failed to probe Docker: ${resp.status}`);
  return resp.json();
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
  provider_registry?: ProviderRegistry;
}

export type ProviderCapability = "llm" | "text_embedding" | "multimodal_embedding" | "rerank";
export type ProviderModelCategory = "llm" | "multimodal_llm" | "text_embedding" | "multimodal_embedding" | "rerank";
export type ThinkingLevel = "low" | "high" | "max";

export interface ThinkingProfile {
  kind: "qwen_fixed" | "kimi_bailian_fixed" | "kimi_levels" | "deepseek_levels" | "none";
  thinking_enabled: boolean;
  strength_control: "levels" | "disabled" | "hidden";
  levels: ThinkingLevel[];
  default_level: ThinkingLevel | null;
  disabled_label: string;
}

export interface ProviderEndpoint {
  id: string;
  protocol: string;
  base_url: string;
  route_path?: string;
  capabilities: ProviderCapability[];
  credential_configured: boolean;
  api_key_masked: string;
  credential_source: "" | "environment" | "local_file";
}

export interface ProviderApiKey {
  name: string;
  is_default: boolean;
  credential_configured: boolean;
  api_key_masked: string;
  credential_source: "" | "environment" | "local_file";
}

export interface ProviderModel {
  id: string;
  name: string;
  endpoint_id: string;
  capability: ProviderCapability;
  categories?: ProviderModelCategory[];
  dimension?: number;
  batch_size?: number;
  concurrency?: number;
  thinking_profile?: ThinkingProfile;
}

export interface ProviderService {
  id: string;
  name: string;
  enabled: boolean;
  website?: string;
  credential_scope?: "provider" | "endpoint";
  default_credential_name: string;
  api_keys: ProviderApiKey[];
  endpoints: ProviderEndpoint[];
  models: ProviderModel[];
}

export interface ProviderRegistry {
  version: number;
  providers: ProviderService[];
  bindings: Record<string, string>;
  migration: { state: string };
}

export async function getSettings(): Promise<SystemSettings> {
  const resp = await fetch(`${API_BASE}/settings`, { cache: "no-store" });
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

export async function getProviders(): Promise<ProviderRegistry> {
  const resp = await fetch(`${API_BASE}/providers`);
  if (!resp.ok) throw new Error(`Failed to get providers: ${resp.status}`);
  return resp.json();
}

export async function revealProviderCredential(providerId: string, credentialName: string): Promise<string> {
  const resp = await fetch(
    `${API_BASE}/providers/${encodeURIComponent(providerId)}/credentials/${encodeURIComponent(credentialName)}/reveal`,
    { method: "POST", cache: "no-store" },
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to reveal provider credential: ${resp.status}`);
  }
  return (await resp.json()).value;
}

export async function updateProvider(providerId: string, update: {
  name?: string;
  enabled?: boolean;
  endpoints?: Array<{ id: string; base_url?: string; route_path?: string; api_key?: string }>;
  credentials?: Array<{ name: string; value: string }>;
}): Promise<ProviderRegistry> {
  const resp = await fetch(`${API_BASE}/providers/${providerId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to update provider: ${resp.status}`);
  }
  return resp.json();
}

export async function bindProviderModel(binding: string, modelId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/providers/bindings/${binding}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_id: modelId }),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to bind model: ${resp.status}`);
  }
}

export async function discoverProviderModels(providerId: string, endpointId: string): Promise<Array<{ id: string; name: string }>> {
  const resp = await fetch(`${API_BASE}/providers/${providerId}/endpoints/${endpointId}/discover-models`, { method: "POST" });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to discover models: ${resp.status}`);
  }
  return (await resp.json()).models;
}

export interface ProviderConnectionTestResult {
  success: boolean;
  reachable: boolean;
  status_code: number;
  latency_ms: number;
}

export async function testProviderConnection(
  providerId: string,
  endpointId: string,
  params: { base_url?: string; api_key?: string; credential_name?: string },
): Promise<ProviderConnectionTestResult> {
  const resp = await fetch(`${API_BASE}/providers/${providerId}/endpoints/${endpointId}/test-connection`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Provider connection test failed: ${resp.status}`);
  }
  return resp.json();
}

export async function addProviderModel(providerId: string, model: {
  endpoint_id: string;
  capability: ProviderCapability;
  name: string;
  categories: ProviderModelCategory[];
  dimension?: number;
  batch_size?: number;
  concurrency?: number;
}): Promise<void> {
  const resp = await fetch(`${API_BASE}/providers/${providerId}/models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(model),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to add model: ${resp.status}`);
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
  pgvector?: {
    required: boolean;
    available: boolean;
    installed: boolean;
    version: string;
    server_major?: number | null;
    install_command: string;
  };
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
  details?: Record<string, string>;
}

export interface Capabilities {
  database: CapabilityStatus;
  pgvector: CapabilityStatus;
  docker: CapabilityStatus;
  milvus: CapabilityStatus;
  mineru: CapabilityStatus;
  cli: CapabilityStatus;
}

export async function getCapabilities(): Promise<Capabilities> {
  const resp = await fetch(`${API_BASE}/capabilities`, { cache: "no-store" });
  if (!resp.ok) throw new Error(`Failed to get capabilities: ${resp.status}`);
  return resp.json();
}
