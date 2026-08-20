const API_BASE = "/api";

export interface HeadlessActivityLog {
  id: string;
  created_at: number;
  created_at_beijing: string;
  source_id: string;
  source_name: string;
  query: string;
}

export interface HeadlessActivityLogPage {
  items: HeadlessActivityLog[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  source_names: string[];
  timezone: "Asia/Shanghai";
}

export interface HeadlessActivityLogFilters {
  page?: number;
  sourceName?: string;
  query?: string;
  startAt?: number;
  endAt?: number;
}

export async function listHeadlessActivityLogs(
  filters: HeadlessActivityLogFilters = {},
): Promise<HeadlessActivityLogPage> {
  const params = new URLSearchParams({ page: String(filters.page || 1) });
  if (filters.sourceName) params.set("source_name", filters.sourceName);
  if (filters.query) params.set("query", filters.query);
  if (filters.startAt !== undefined) params.set("start_at", String(filters.startAt));
  if (filters.endAt !== undefined) params.set("end_at", String(filters.endAt));
  const response = await fetch(`${API_BASE}/headless-activity-logs?${params.toString()}`);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload?.detail === "string" ? payload.detail : `调用日志请求失败：${response.status}`;
    throw new Error(detail);
  }
  return payload as HeadlessActivityLogPage;
}
