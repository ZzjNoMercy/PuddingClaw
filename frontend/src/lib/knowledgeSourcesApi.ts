const API_BASE = "/api/knowledge";

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    ...init,
    headers: init.body instanceof FormData
      ? init.headers
      : { "Content-Type": "application/json", ...(init.headers || {}) },
  });
  const text = await response.text();
  if (!response.ok) {
    let message = "";
    try {
      const payload = JSON.parse(text) as { detail?: unknown; message?: unknown };
      const detail = payload.detail ?? payload.message;
      if (typeof detail === "string") message = detail;
    } catch {
      // Fall back to the raw response below.
    }
    throw new Error(message || text || `请求失败：${response.status}`);
  }
  return (text ? JSON.parse(text) : {}) as T;
}

export type KnowledgeConnectorKey = "local_upload" | "web_capture" | "feishu_wiki";
export type KnowledgeAuthType = "builtin" | "tenant" | "user";

export interface KnowledgeConnector {
  key: KnowledgeConnectorKey;
  name: string;
  description: string;
  auth_types: KnowledgeAuthType[];
  capabilities: string[];
  builtin: boolean;
}

export interface KnowledgeSource {
  id: string;
  knowledge_base_id: string;
  connector_key: KnowledgeConnectorKey;
  name: string;
  status: string;
  auth_type: KnowledgeAuthType;
  credential_configured: boolean;
  config: Record<string, unknown>;
  schedule: Record<string, unknown>;
  last_sync_run_id: string | null;
  last_synced_at: string | null;
  last_error: Record<string, unknown>;
  builtin: boolean;
  item_count?: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface KnowledgeSourceItem {
  id: string;
  source_connection_id: string;
  external_id: string;
  external_type: string;
  title: string;
  source_url: string | null;
  path: string[];
  revision: string | null;
  document_id: string | null;
  status: string;
  metadata: Record<string, unknown>;
  updated_at: string | null;
}

export interface KnowledgeSyncRun {
  id: string;
  source_connection_id: string;
  mode: "incremental" | "full_scan" | "reindex";
  status: string;
  current_step: string;
  progress: number;
  stats: Record<string, number>;
  error: Record<string, unknown>;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  source_name?: string;
  connector_key?: string;
}

export interface FeishuAppCredential {
  id: string;
  app_id_masked: string;
  app_name: string;
  api_base_url: string;
  tenant_key: string;
  status: string;
  credential_configured: boolean;
  credential_readable?: boolean;
  credential_error?: string;
  validated_at: string | null;
  rotated_at: string | null;
  created_at: string | null;
}

export interface FeishuSpace {
  space_id: string;
  name: string;
  description?: string;
  visibility?: string;
}

export interface FeishuWikiNode {
  space_id?: string;
  node_token: string;
  parent_node_token?: string;
  obj_token?: string;
  obj_type?: string;
  title: string;
  has_child?: boolean;
}

export interface FeishuBitableReference {
  original_url: string;
  entry_kind: "direct_bitable" | "wiki_bitable";
  node_token: string;
  app_token: string;
  table_id: string;
  view_id: string;
}

export interface FeishuBitableTable {
  table_id: string;
  name?: string;
  revision?: number;
}

export interface FeishuBitableField {
  field_id: string;
  field_name: string;
  type?: number;
  ui_type?: string;
  is_primary?: boolean;
  property?: Record<string, unknown> | null;
}

export interface FeishuBitablePreview {
  live: true;
  row_storage: false;
  reference: FeishuBitableReference;
  table: FeishuBitableTable;
  fields: FeishuBitableField[];
  records: {
    items: Array<{ record_id?: string; fields?: Record<string, unknown> }>;
    has_more: boolean;
    page_token: string;
    total?: number | null;
  };
}

export type FeishuBitableCardinality = "one_to_one" | "one_to_many" | "many_to_one" | "many_to_many";
export type FeishuBitableDeletePolicy = "retain_orphans" | "restrict" | "cascade";

export interface FeishuBitableRelation {
  id: string;
  name: string;
  description: string;
  source_table_id: string;
  source_table_name: string;
  source_field_id: string;
  source_field_name: string;
  target_table_id: string;
  target_table_name: string;
  target_field_id: string;
  target_field_name: string;
  cardinality: FeishuBitableCardinality;
  on_target_delete: FeishuBitableDeletePolicy;
  validation_status: "schema_valid" | "needs_review" | "stale_endpoint" | string;
  validation_scope: "schema_only" | string;
  validation_warnings: string[];
  row_values_stored: false;
  created_at: string;
  updated_at: string;
}

export interface FeishuBitableRelationInput {
  name?: string;
  description?: string;
  source_table_id: string;
  source_field_id: string;
  target_table_id: string;
  target_field_id: string;
  cardinality: FeishuBitableCardinality;
  on_target_delete: FeishuBitableDeletePolicy;
}

export async function listKnowledgeConnectors(): Promise<KnowledgeConnector[]> {
  return (await requestJson<{ connectors: KnowledgeConnector[] }>("/connectors")).connectors;
}

export async function listKnowledgeSources(): Promise<KnowledgeSource[]> {
  return (await requestJson<{ sources: KnowledgeSource[] }>("/sources")).sources;
}

export async function createKnowledgeSource(input: {
  connector_key: "feishu_wiki";
  name: string;
  auth_type: "tenant" | "user";
  config?: Record<string, unknown>;
  schedule?: Record<string, unknown>;
}): Promise<KnowledgeSource> {
  return (await requestJson<{ source: KnowledgeSource }>("/sources", {
    method: "POST",
    body: JSON.stringify(input),
  })).source;
}

export async function updateKnowledgeSource(
  sourceId: string,
  patch: Partial<Pick<KnowledgeSource, "name" | "status" | "config" | "schedule">>,
): Promise<KnowledgeSource> {
  return (await requestJson<{ source: KnowledgeSource }>(`/sources/${encodeURIComponent(sourceId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  })).source;
}

export async function disableKnowledgeSource(sourceId: string): Promise<void> {
  await requestJson(`/sources/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
}

export async function listKnowledgeSourceItems(sourceId: string): Promise<KnowledgeSourceItem[]> {
  return (await requestJson<{ items: KnowledgeSourceItem[] }>(
    `/sources/${encodeURIComponent(sourceId)}/items?limit=200`,
  )).items;
}

export async function listKnowledgeSourceRuns(sourceId: string): Promise<KnowledgeSyncRun[]> {
  return (await requestJson<{ runs: KnowledgeSyncRun[] }>(
    `/sources/${encodeURIComponent(sourceId)}/runs?limit=20`,
  )).runs;
}

export async function listKnowledgeSyncRuns(limit = 50): Promise<KnowledgeSyncRun[]> {
  return (await requestJson<{ runs: KnowledgeSyncRun[] }>(`/sync-runs?limit=${limit}`)).runs;
}

export async function startKnowledgeSourceSync(
  sourceId: string,
  mode: "incremental" | "full_scan" | "reindex" = "incremental",
): Promise<KnowledgeSyncRun> {
  return (await requestJson<{ run: KnowledgeSyncRun }>(`/sources/${encodeURIComponent(sourceId)}/sync`, {
    method: "POST",
    body: JSON.stringify({ mode }),
  })).run;
}

export async function cancelKnowledgeSourceSync(sourceId: string, runId: string): Promise<KnowledgeSyncRun> {
  return (await requestJson<{ run: KnowledgeSyncRun }>(
    `/sources/${encodeURIComponent(sourceId)}/runs/${encodeURIComponent(runId)}/cancel`,
    { method: "POST" },
  )).run;
}

export async function listFeishuApps(): Promise<FeishuAppCredential[]> {
  return (await requestJson<{ apps: FeishuAppCredential[] }>("/feishu/apps")).apps;
}

export async function createFeishuApp(input: {
  app_id: string;
  app_secret: string;
  app_name?: string;
  api_base_url?: string;
}): Promise<FeishuAppCredential> {
  return (await requestJson<{ app: FeishuAppCredential }>("/feishu/apps", {
    method: "POST",
    body: JSON.stringify(input),
  })).app;
}

export async function rotateFeishuApp(
  credentialId: string,
  input: { app_id: string; app_secret: string },
): Promise<FeishuAppCredential> {
  return (await requestJson<{ app: FeishuAppCredential }>(
    `/feishu/apps/${encodeURIComponent(credentialId)}`,
    { method: "PUT", body: JSON.stringify(input) },
  )).app;
}

export async function testFeishuApp(appId: string): Promise<FeishuAppCredential> {
  return (await requestJson<{ app: FeishuAppCredential }>(`/feishu/apps/${encodeURIComponent(appId)}/test`, {
    method: "POST",
  })).app;
}

export async function bindFeishuTenantAuth(sourceId: string, appCredentialId: string): Promise<KnowledgeSource> {
  return (await requestJson<{ source: KnowledgeSource }>(
    `/feishu/sources/${encodeURIComponent(sourceId)}/tenant-auth`,
    { method: "POST", body: JSON.stringify({ app_credential_id: appCredentialId }) },
  )).source;
}

export async function startFeishuUserOAuth(
  sourceId: string,
  appCredentialId: string,
  redirectUri: string,
): Promise<{ authorization_url: string; expires_at: string; scopes: string[] }> {
  return requestJson(`/feishu/sources/${encodeURIComponent(sourceId)}/oauth/start`, {
    method: "POST",
    body: JSON.stringify({ app_credential_id: appCredentialId, redirect_uri: redirectUri }),
  });
}

export async function completeFeishuUserOAuth(state: string, code: string): Promise<void> {
  await requestJson("/feishu/oauth/callback", {
    method: "POST",
    body: JSON.stringify({ state, code }),
  });
}

export async function listFeishuSpaces(sourceId: string): Promise<FeishuSpace[]> {
  return (await requestJson<{ spaces: FeishuSpace[] }>(
    `/feishu/sources/${encodeURIComponent(sourceId)}/spaces`,
  )).spaces;
}

export async function listFeishuNodes(
  sourceId: string,
  spaceId: string,
  parentNodeToken = "",
): Promise<FeishuWikiNode[]> {
  const params = new URLSearchParams({ space_id: spaceId });
  if (parentNodeToken) params.set("parent_node_token", parentNodeToken);
  return (await requestJson<{ nodes: FeishuWikiNode[] }>(
    `/feishu/sources/${encodeURIComponent(sourceId)}/nodes?${params.toString()}`,
  )).nodes;
}

export async function configureFeishuScope(sourceId: string, input: {
  space_id: string;
  root_node_token?: string;
  tenant_domain?: string;
  publish_vector?: boolean;
  interval_minutes?: number;
}): Promise<KnowledgeSource> {
  return (await requestJson<{ source: KnowledgeSource }>(
    `/feishu/sources/${encodeURIComponent(sourceId)}/scope`,
    { method: "PUT", body: JSON.stringify(input) },
  )).source;
}

export async function resolveFeishuBitable(
  sourceId: string,
  url: string,
): Promise<{ reference: FeishuBitableReference; tables: FeishuBitableTable[] }> {
  return requestJson(`/feishu/sources/${encodeURIComponent(sourceId)}/bitable/resolve`, {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export async function previewFeishuBitable(sourceId: string, input: {
  url: string;
  table_id: string;
  view_id?: string;
  page_size?: number;
}): Promise<FeishuBitablePreview> {
  return requestJson(`/feishu/sources/${encodeURIComponent(sourceId)}/bitable/preview`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function configureFeishuBitableScope(sourceId: string, input: {
  url: string;
  table_id: string;
  table_ids: string[];
  view_id?: string;
  monitor_changes?: boolean;
  interval_minutes?: number;
}): Promise<KnowledgeSource> {
  return (await requestJson<{ source: KnowledgeSource }>(
    `/feishu/sources/${encodeURIComponent(sourceId)}/bitable/scope`,
    { method: "PUT", body: JSON.stringify(input) },
  )).source;
}

export async function listFeishuBitableRelations(sourceId: string): Promise<FeishuBitableRelation[]> {
  return (await requestJson<{ relations: FeishuBitableRelation[] }>(
    `/feishu/sources/${encodeURIComponent(sourceId)}/bitable/relations`,
  )).relations;
}

export async function createFeishuBitableRelation(
  sourceId: string,
  input: FeishuBitableRelationInput,
): Promise<FeishuBitableRelation> {
  return (await requestJson<{ relation: FeishuBitableRelation }>(
    `/feishu/sources/${encodeURIComponent(sourceId)}/bitable/relations`,
    { method: "POST", body: JSON.stringify(input) },
  )).relation;
}

export async function updateFeishuBitableRelation(
  sourceId: string,
  relationId: string,
  input: FeishuBitableRelationInput,
): Promise<FeishuBitableRelation> {
  return (await requestJson<{ relation: FeishuBitableRelation }>(
    `/feishu/sources/${encodeURIComponent(sourceId)}/bitable/relations/${encodeURIComponent(relationId)}`,
    { method: "PUT", body: JSON.stringify(input) },
  )).relation;
}

export async function deleteFeishuBitableRelation(sourceId: string, relationId: string): Promise<void> {
  await requestJson(
    `/feishu/sources/${encodeURIComponent(sourceId)}/bitable/relations/${encodeURIComponent(relationId)}`,
    { method: "DELETE" },
  );
}
