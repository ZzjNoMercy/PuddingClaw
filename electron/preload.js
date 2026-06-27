const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electron', {
  // 选择项目文件夹
  selectProjectFolder: () => ipcRenderer.invoke('select-project-folder'),

  // Backend 管理
  startBackend: () => ipcRenderer.invoke('start-backend'),
  stopBackend: () => ipcRenderer.invoke('stop-backend'),
  getBackendStatus: () => ipcRenderer.invoke('get-backend-status'),

  // Docker infra 管理
  startInfra: () => ipcRenderer.invoke('start-infra'),
  stopInfra: () => ipcRenderer.invoke('stop-infra'),
  getInfraStatus: () => ipcRenderer.invoke('get-infra-status'),

  // 监听后端日志/状态事件
  onBackendLog: (callback) => ipcRenderer.on('backend-log', callback),
  onBackendStatusChange: (callback) => ipcRenderer.on('backend-status-change', callback),
  onInfraStatusChange: (callback) => ipcRenderer.on('infra-status-change', callback),

  // 移除监听器
  removeAllListeners: (channel) => ipcRenderer.removeAllListeners(channel),
});
