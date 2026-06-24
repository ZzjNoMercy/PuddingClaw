/**
 * Settings API client for PuddingClaw backend.
 */

const API_BASE = "/api";

export interface LlmSettings {
  provider: string;
  model: string;
  base_url: string;
  api_key_masked: string;
  temperature: number;
  max_tokens: number;
}

export interface EmbeddingSettings {
  provider: string;
  model: string;
  base_url: string;
  api_key_masked: string;
}

export interface RagSettings {
  enabled: boolean;
  top_k: number;
  similarity_threshold: number;
}

export interface CompressionSettings {
  ratio: number;
}

export interface SystemSettings {
  llm: LlmSettings;
  embedding: EmbeddingSettings;
  rag: RagSettings;
  compression: CompressionSettings;
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

export interface TestConnectionResult {
  success: boolean;
  model: string;
  latency_ms: number;
  response_model?: string;
  dimensions?: number;
}

export async function testConnection(params: {
  type: "llm" | "embedding";
  provider: string;
  model: string;
  base_url: string;
  api_key: string;
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

export interface CapabilityStatus {
  available: boolean;
  reason: string | null;
}

export interface Capabilities {
  ai_gateway: CapabilityStatus;
  milvus: CapabilityStatus;
  mineru: CapabilityStatus;
}

export async function getCapabilities(): Promise<Capabilities> {
  const resp = await fetch(`${API_BASE}/capabilities`);
  if (!resp.ok) throw new Error(`Failed to get capabilities: ${resp.status}`);
  return resp.json();
}
