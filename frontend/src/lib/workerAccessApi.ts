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

export interface WorkerAccessLog {
  id: string;
  created_at: number;
  created_at_beijing: string;
  key_id: string;
  key_name: string;
  query: string;
}

export interface WorkerAccessLogPage {
  items: WorkerAccessLog[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  key_names: string[];
  timezone: "Asia/Shanghai";
}

export interface WorkerAccessLogFilters {
  page?: number;
  keyName?: string;
  query?: string;
  startAt?: number;
  endAt?: number;
}

function errorDetailMessage(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (!item || typeof item !== "object") return "";
        const record = item as Record<string, unknown>;
        return typeof record.msg === "string"
          ? record.msg
          : typeof record.message === "string"
            ? record.message
            : "";
      })
      .filter(Boolean);
    return messages.length ? messages.join("；") : null;
  }
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === "string") return record.message;
    if (typeof record.msg === "string") return record.msg;
  }
  return null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = errorDetailMessage(payload.detail) || errorDetailMessage(payload) || `Worker Key 请求失败：${response.status}`;
    throw new Error(message);
  }
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

export async function listWorkerAccessLogs(filters: WorkerAccessLogFilters = {}): Promise<WorkerAccessLogPage> {
  const params = new URLSearchParams({ page: String(filters.page || 1) });
  if (filters.keyName) params.set("key_name", filters.keyName);
  if (filters.query) params.set("query", filters.query);
  if (filters.startAt !== undefined) params.set("start_at", String(filters.startAt));
  if (filters.endAt !== undefined) params.set("end_at", String(filters.endAt));
  return request(`/worker-access-logs?${params.toString()}`);
}
