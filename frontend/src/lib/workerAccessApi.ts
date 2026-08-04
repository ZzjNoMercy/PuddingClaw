const API_BASE = "/api";

export interface WorkerAccessKey {
  key_id: string;
  prefix: string;
  name: string;
  scopes: string[];
  allowed_analytics_models: string[];
  authority_profile: string;
  expires_at?: number | null;
  last_used_at?: number | null;
  revoked_at?: number | null;
  created_at?: number;
}

export interface WorkerAccessKeySecret extends WorkerAccessKey { token: string }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Worker Key 请求失败：${response.status}`);
  return payload as T;
}

export async function listWorkerAccessKeys(): Promise<{ keys: WorkerAccessKey[] }> {
  return request("/worker-access-keys");
}

export async function createWorkerAccessKey(body: Record<string, unknown>): Promise<WorkerAccessKeySecret> {
  return request("/worker-access-keys", { method: "POST", body: JSON.stringify(body) });
}

export async function rotateWorkerAccessKey(keyId: string): Promise<WorkerAccessKeySecret> {
  return request(`/worker-access-keys/${encodeURIComponent(keyId)}/rotate`, { method: "POST" });
}

export async function revokeWorkerAccessKey(keyId: string): Promise<void> {
  await request(`/worker-access-keys/${encodeURIComponent(keyId)}`, { method: "DELETE" });
}
