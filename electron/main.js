const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const backendManager = require('./managers/backend');
const dockerManager = require('./managers/docker');
const { getFrontendStandaloneDir } = require('./managers/paths');

let mainWindow;
let frontendProcess = null;
const FRONTEND_PORT = 3000;

function getFrontendUrl() {
  const isDev = !app.isPackaged;
  const port = isDev ? (process.env.FRONTEND_DEV_PORT || '3000') : String(FRONTEND_PORT);
  return `http://localhost:${port}/app-control`;
}

async function startFrontendServer() {
  const isDev = !app.isPackaged;
  if (isDev) {
    // 开发模式由外部脚本启动 frontend dev server
    return true;
  }

  const standaloneDir = getFrontendStandaloneDir();
  const serverJs = path.join(standaloneDir, 'server.js');
  const fs = require('fs');

  if (!fs.existsSync(serverJs)) {
    throw new Error(`未找到 frontend standalone server: ${serverJs}`);
  }

  // 清理可能残留的 3000 端口
  require('child_process').execSync('lsof -ti:3000 | xargs kill 2>/dev/null || true');

  frontendProcess = spawn('node', ['server.js'], {
    cwd: standaloneDir,
    env: {
      ...process.env,
      PORT: String(FRONTEND_PORT),
      BACKEND_INTERNAL_URL: 'http://localhost:8888',
    },
    stdio: 'pipe',
  });

  frontendProcess.stdout.on('data', (data) => {
    console.log('[frontend stdout]', data.toString().trim());
  });

  frontendProcess.stderr.on('data', (data) => {
    console.error('[frontend stderr]', data.toString().trim());
  });

  // 等待 frontend ready
  const http = require('http');
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    const ready = await new Promise((resolve) => {
      const req = http.get(`http://localhost:${FRONTEND_PORT}/app-control`, { timeout: 2000 }, (res) => {
        resolve(res.statusCode === 200);
      });
      req.on('error', () => resolve(false));
      req.on('timeout', () => { req.destroy(); resolve(false); });
    });
    if (ready) return true;
  }

  throw new Error('frontend server 未在 30 秒内就绪');
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 960,
    titleBarStyle: 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(getFrontendUrl());

  const isDev = !app.isPackaged;
  if (isDev) {
    mainWindow.webContents.openDevTools();
  }
}

app.whenReady().then(async () => {
  try {
    await startFrontendServer();
    createWindow();

    // 开机自检：如果 backend 未运行，自动启动
    const backendStatus = backendManager.getStatus();
    if (backendStatus.status !== 'running') {
      console.log('[auto] backend 未运行，开始自动启动...');
      backendManager.startBackend().catch((err) => {
        console.error('自动启动 backend 失败:', err);
      });
    }
  } catch (err) {
    console.error('启动 frontend server 失败:', err);
    dialog.showErrorBox('启动失败', err.message);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    backendManager.stopBackend();
    dockerManager.stopInfra();
    if (frontendProcess) frontendProcess.kill();
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('before-quit', async () => {
  await backendManager.stopBackend();
  await dockerManager.stopInfra();
  if (frontendProcess) frontendProcess.kill();
});

// IPC: 选择项目文件夹
ipcMain.handle('select-project-folder', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory'],
    title: '选择项目文件夹',
    buttonLabel: '选择',
  });

  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }

  return result.filePaths[0];
});

// IPC: Backend 管理
ipcMain.handle('start-backend', async () => {
  const result = await backendManager.startBackend();
  if (mainWindow) {
    mainWindow.webContents.send('backend-status-change', backendManager.getStatus());
  }
  return result;
});

ipcMain.handle('stop-backend', async () => {
  const result = await backendManager.stopBackend();
  if (mainWindow) {
    mainWindow.webContents.send('backend-status-change', backendManager.getStatus());
  }
  return result;
});

ipcMain.handle('get-backend-status', async () => {
  return backendManager.getStatus();
});

// IPC: Docker infra 管理
ipcMain.handle('start-infra', async () => {
  const result = await dockerManager.startInfra();
  if (mainWindow) {
    mainWindow.webContents.send('infra-status-change', await dockerManager.checkInfraStatus());
  }
  return result;
});

ipcMain.handle('stop-infra', async () => {
  const result = await dockerManager.stopInfra();
  if (mainWindow) {
    mainWindow.webContents.send('infra-status-change', await dockerManager.checkInfraStatus());
  }
  return result;
});

ipcMain.handle('get-infra-status', async () => {
  return dockerManager.checkInfraStatus();
});

// 定时刷新状态
setInterval(async () => {
  if (!mainWindow) return;

  try {
    const backendStatus = backendManager.getStatus();
    mainWindow.webContents.send('backend-status-change', backendStatus);
  } catch (err) {
    console.error('backend status update error:', err);
  }

  try {
    const infraStatus = await dockerManager.checkInfraStatus();
    mainWindow.webContents.send('infra-status-change', infraStatus);
  } catch (err) {
    console.error('infra status update error:', err);
  }
}, 3000);
