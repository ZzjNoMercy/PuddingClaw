export interface ElectronAPI {
  // 文件夹选择
  selectProjectFolder: () => Promise<string | null>;

  // Backend 管理
  startBackend: () => Promise<{ status: string; message: string }>;
  stopBackend: () => Promise<{ status: string; message: string }>;
  getBackendStatus: () => Promise<{ status: string; error: string | null; url: string }>;

  // Docker infra 管理
  startInfra: () => Promise<{ status: string; message: string }>;
  stopInfra: () => Promise<{ status: string; message: string }>;
  getInfraStatus: () => Promise<{
    docker: boolean;
    higress: string;
    milvus: string;
    status: string;
    error: string | null;
  }>;

  // 事件监听
  onBackendLog: (callback: (event: unknown, log: string) => void) => void;
  onBackendStatusChange: (callback: (event: unknown, status: unknown) => void) => void;
  onInfraStatusChange: (callback: (event: unknown, status: unknown) => void) => void;
  removeAllListeners: (channel: string) => void;
}

declare global {
  interface Window {
    electron?: ElectronAPI;
  }
}

export {};
