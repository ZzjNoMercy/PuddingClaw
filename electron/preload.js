const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electron', {
  // 选择项目文件夹
  selectProjectFolder: () => ipcRenderer.invoke('select-project-folder'),
  selectKnowledgeFile: () => ipcRenderer.invoke('select-knowledge-file'),
  selectKnowledgeFolder: () => ipcRenderer.invoke('select-knowledge-folder'),

  // Backend 管理
  startBackend: () => ipcRenderer.invoke('start-backend'),
  stopBackend: () => ipcRenderer.invoke('stop-backend'),
  getBackendStatus: () => ipcRenderer.invoke('get-backend-status'),

  // Docker infra 管理
  startInfra: () => ipcRenderer.invoke('start-infra'),
  stopInfra: () => ipcRenderer.invoke('stop-infra'),
  getInfraStatus: () => ipcRenderer.invoke('get-infra-status'),

  // 首次启动模式选择（计划与探测来自 CLI）
  getOnboardingState: () => ipcRenderer.invoke('get-onboarding-state'),
  inspectOnboardingProfile: (profile) => ipcRenderer.invoke('inspect-onboarding-profile', profile),
  applyOnboardingProfile: (profile) => ipcRenderer.invoke('apply-onboarding-profile', profile),

  // 监听后端日志/状态事件
  onBackendLog: (callback) => ipcRenderer.on('backend-log', callback),
  onBackendStatusChange: (callback) => ipcRenderer.on('backend-status-change', callback),
  onInfraStatusChange: (callback) => ipcRenderer.on('infra-status-change', callback),

  // 移除监听器
  removeAllListeners: (channel) => ipcRenderer.removeAllListeners(channel),
});
