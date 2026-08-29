const API_BASE = "/api/config/web-search";

export type WebSearchProviderId = "tavily" | "deepseek" | "grok";
export type WebSearchProviderState = "disabled" | "needs_test" | "ready" | "error";

export interface WebSearchProvider {
  id: WebSearchProviderId;
  name: string;
  description: string;
  website: string;
  docs: string;
  model: string | null;
  base_url: string;
  enabled: boolean;
  state: WebSearchProviderState;
  credential_configured: boolean;
  api_key_masked: string;
  credential_source: "" | "web_search" | "provider_registry" | "environment";
  credential_readable?: boolean;
  credential_error?: string;
  options: {
    max_results: number;
    search_depth?: "basic" | "advanced" | "fast" | "ultra-fast";
    web_search_enabled?: boolean;
    x_search_enabled?: boolean;
  };
  dependencies: {
    status: "already_satisfied" | "preparing" | "error";
    packages: string[];
  };
  last_test: {
    success: boolean;
    latency_ms: number;
    tested_at: number;
  } | null;
  last_error: string;
}

export interface WebSearchConfig {
  version: number;
  default_scope: "domestic" | "global";
  routing: {
    domestic: WebSearchProviderId[];
    global: WebSearchProviderId[];
    fallback_enabled: boolean;
    max_provider_attempts: number;
    cross_check_enabled: boolean;
  };
  providers: WebSearchProvider[];
  ready_providers: WebSearchProviderId[];
  credential_vault?: {
    readable: boolean;
    error: string;
  };
}

export interface WebSearchTestResult {
  success: boolean;
  provider_id: WebSearchProviderId;
  credential_source: string;
  latency_ms: number;
  source_count: number;
  server_tools: string[];
}

async function responseJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || fallback);
  }
  return response.json();
}

export async function getWebSearchConfig(): Promise<WebSearchConfig> {
  return responseJson(await fetch(API_BASE, { cache: "no-store" }), "无法读取联网搜索配置");
}

export async function updateWebSearchRouting(update: Partial<{
  default_scope: "domestic" | "global";
  domestic: WebSearchProviderId[];
  global: WebSearchProviderId[];
  fallback_enabled: boolean;
  max_provider_attempts: number;
  cross_check_enabled: boolean;
}>): Promise<WebSearchConfig> {
  return responseJson(
    await fetch(`${API_BASE}/routing`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(update),
    }),
    "无法保存搜索路由",
  );
}

export async function updateWebSearchProviderOptions(
  providerId: WebSearchProviderId,
  options: Record<string, unknown>,
): Promise<WebSearchConfig> {
  return responseJson(
    await fetch(`${API_BASE}/providers/${providerId}/options`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ options }),
    }),
    "无法保存供应商选项",
  );
}

export async function saveWebSearchCredential(
  providerId: WebSearchProviderId,
  apiKey: string,
): Promise<WebSearchConfig> {
  return responseJson(
    await fetch(`${API_BASE}/providers/${providerId}/credential`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    }),
    "无法保存 API Key",
  );
}

export async function deleteWebSearchCredential(providerId: WebSearchProviderId): Promise<WebSearchConfig> {
  return responseJson(
    await fetch(`${API_BASE}/providers/${providerId}/credential`, { method: "DELETE" }),
    "无法删除 API Key",
  );
}

export async function testWebSearchProvider(providerId: WebSearchProviderId): Promise<WebSearchTestResult> {
  return responseJson(
    await fetch(`${API_BASE}/providers/${providerId}/test`, { method: "POST" }),
    "连接测试失败",
  );
}

export async function enableWebSearchProvider(providerId: WebSearchProviderId): Promise<WebSearchConfig> {
  return responseJson(
    await fetch(`${API_BASE}/providers/${providerId}/enable`, { method: "POST" }),
    "启用供应商失败",
  );
}

export async function disableWebSearchProvider(providerId: WebSearchProviderId): Promise<WebSearchConfig> {
  return responseJson(
    await fetch(`${API_BASE}/providers/${providerId}/disable`, { method: "POST" }),
    "停用供应商失败",
  );
}
