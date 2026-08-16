export interface ElectronAPI {
  // 文件夹选择
  selectProjectFolder: () => Promise<string | null>;
  selectKnowledgeFile: () => Promise<string | null>;
  selectKnowledgeFolder: () => Promise<string | null>;

  // Backend 管理
  startBackend: () => Promise<{ status: string; message: string }>;
  stopBackend: () => Promise<{ status: string; message: string }>;
  getBackendStatus: () => Promise<{ status: string; error: string | null; url: string }>;

  // Docker infra 管理
  startInfra: () => Promise<{ status: string; message: string }>;
  stopInfra: () => Promise<{ status: string; message: string }>;
  getInfraStatus: () => Promise<{
    docker: boolean;
    postgres: string;
    milvus: string;
    status: string;
    error: string | null;
  }>;

  // 首次启动模式选择
  getOnboardingState: () => Promise<OnboardingState>;
  inspectOnboardingProfile: (profile: OnboardingProfileId) => Promise<ProfileInspection>;
  applyOnboardingProfile: (profile: OnboardingProfileId) => Promise<ProfileApplyResult>;

  // 事件监听
  onBackendLog: (callback: (event: unknown, log: string) => void) => void;
  onBackendStatusChange: (callback: (event: unknown, status: unknown) => void) => void;
  onInfraStatusChange: (callback: (event: unknown, status: unknown) => void) => void;
  removeAllListeners: (channel: string) => void;
}

export type OnboardingProfileId = "harness" | "knowledge" | "full";

export interface OnboardingState {
  available: boolean;
  initialized: boolean;
  profile: string | null;
  extensions: Record<string, { enabled: boolean }> | null;
  home: string;
  error?: string;
}

export interface ProfileDependency {
  id: string;
  label: string;
  group: "core" | "configuration" | "optional" | "knowledge" | "analytics";
  required: boolean;
  status: "available" | "planned" | "needs_action" | "not_configured" | "optional_unavailable";
  detail: string;
  remediation: string[];
  source: "cli";
}

export interface ProfileInspection {
  schema_version: number;
  status: "ready" | "needs_action";
  profile: OnboardingProfileId;
  label: string;
  initialized: boolean;
  current_profile: string | null;
  extensions: Record<string, boolean>;
  dependency_profile: string;
  dependencies: ProfileDependency[];
  blocking: string[];
  actions: { can_apply: boolean; can_prepare: boolean };
}

export interface ProfileApplyResult {
  status: "applied";
  profile: OnboardingProfileId;
  label: string;
  config_path: string;
  extensions: Record<string, { enabled: boolean }>;
  inspection: ProfileInspection;
  backend: { status: string; message: string };
}

declare global {
  interface Window {
    electron?: ElectronAPI;
  }
}

export {};
