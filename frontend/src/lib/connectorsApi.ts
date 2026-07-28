const API_BASE = "/api";

export type ConnectorStatus =
  | "connected"
  | "authorizing"
  | "authorization_required"
  | "repair_required"
  | "revoked"
  | "unconfigured"
  | "environment_unavailable";

export interface ConnectorIdentityStatus {
  status?: string;
  reason?: string;
  updated_at?: number;
}

export interface ConnectorAuthorizationFlow {
  type: "managed_authorization_request";
  flow_id: string;
  revision?: number;
  attempt?: number;
  purpose?: string;
  status: string;
  completed_phase_ids?: string[];
  phase: {
    id: string;
    step: number;
    total: number;
    title: string;
    description: string;
  };
  verification_url?: string;
  user_code?: string;
  expires_at?: number;
  completion_hint?: string;
}

export interface ConnectorInfo {
  connector_id: string;
  provider: string;
  adapter_id: string;
  display_name: string;
  description: string;
  driver_kind: "managed_cli" | "mcp" | "http_api" | "desktop_bridge";
  status: ConnectorStatus;
  environment: {
    health: string;
    runtime: string;
    executable: string;
    package: string;
    version?: string | null;
    availability_scope: string;
    toolchain_revision?: string | null;
  };
  profile?: {
    id: string;
    label: string;
    health: string;
    sharing_policy: string;
    app_identity: ConnectorIdentityStatus;
    user_identity: ConnectorIdentityStatus;
    last_updated_at?: number;
  } | null;
  active_flow?: ConnectorAuthorizationFlow | null;
  capabilities: string[];
  installed_skill_count: number;
}

async function errorMessage(response: Response, fallback: string): Promise<string> {
  const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
  if (response.status === 404 && payload?.detail === "Not Found") {
    return "连接器服务尚未加载，请重启 PuddingClaw Backend 后重试";
  }
  if (typeof payload?.detail === "string") return payload.detail;
  if (payload?.detail && typeof payload.detail === "object") {
    const detail = payload.detail as Record<string, unknown>;
    if (typeof detail.message === "string") return detail.message;
    if (typeof detail.output === "string") return detail.output;
    if (typeof detail.error === "string") return detail.error;
  }
  return fallback;
}

export async function listConnectors(): Promise<ConnectorInfo[]> {
  const response = await fetch(`${API_BASE}/connectors`, { cache: "no-store" });
  if (!response.ok) throw new Error(await errorMessage(response, "连接器加载失败"));
  const payload = await response.json() as { connectors: ConnectorInfo[] };
  return payload.connectors;
}

export async function getConnector(connectorId: string): Promise<ConnectorInfo> {
  const response = await fetch(`${API_BASE}/connectors/${encodeURIComponent(connectorId)}`, {
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await errorMessage(response, "连接器状态加载失败"));
  const payload = await response.json() as { connector: ConnectorInfo };
  return payload.connector;
}

export async function authorizeConnector(
  connectorId: string,
  mode: "user_reauthorize" | "full_replace",
): Promise<Record<string, unknown>> {
  const response = await fetch(`${API_BASE}/connectors/${encodeURIComponent(connectorId)}/authorize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "连接器授权启动失败"));
  return response.json();
}

export async function resumeConnectorAuthorization(connectorId: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${API_BASE}/connectors/${encodeURIComponent(connectorId)}/resume`, {
    method: "POST",
  });
  if (!response.ok) throw new Error(await errorMessage(response, "连接器授权验证失败"));
  return response.json();
}

export async function revokeConnector(connectorId: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${API_BASE}/connectors/${encodeURIComponent(connectorId)}/revoke`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirmed: true }),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "连接器断开失败"));
  return response.json();
}
