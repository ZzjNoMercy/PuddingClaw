const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const { app } = require('electron');
const { getBackendDir } = require('./paths');

const BACKEND_DIR = getBackendDir();
const BACKEND_PORT = 8888;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}/api/capabilities`;

let backendProcess = null;
let backendStatus = 'stopped'; // stopped | starting | running | error
let backendError = null;

function getBackendCommand() {
  const isDev = !app.isPackaged;

  if (isDev) {
    return {
      cmd: 'uv',
      args: [
        'run', '--all-extras', '--group', 'dev', '--group', 'deepagents-test',
        'python', '-m', 'uvicorn', 'app:app',
        '--host', '0.0.0.0',
        '--port', String(BACKEND_PORT),
        '--reload',
        '--reload-dir', 'api',
        '--reload-dir', 'graph',
        '--reload-dir', 'projects',
        '--reload-dir', 'tools',
        '--reload-include', '*.py',
        '--log-level', 'info',
        '--log-config', '../logging.yaml',
      ],
      cwd: BACKEND_DIR,
    };
  }

  // 生产模式：假设 .venv 已存在；首次启动前需要 uv sync
  return {
    cmd: path.join(BACKEND_DIR, '.venv', 'bin', 'python'),
    args: ['-m', 'uvicorn', 'app:app', '--host', '0.0.0.0', '--port', String(BACKEND_PORT)],
    cwd: BACKEND_DIR,
  };
}

async function checkBackendStatus() {
  return new Promise((resolve) => {
    const req = http.get(BACKEND_URL, { timeout: 2000 }, (res) => {
      resolve(res.statusCode === 200 ? 'running' : 'error');
    });
    req.on('error', () => resolve('stopped'));
    req.on('timeout', () => {
      req.destroy();
      resolve('stopped');
    });
  });
}

async function ensureBackendVenv() {
  const venvPython = path.join(BACKEND_DIR, '.venv', 'bin', 'python');
  const fs = require('fs');

  if (fs.existsSync(venvPython)) {
    return true;
  }

  backendStatus = 'starting';
  backendError = 'backend .venv 不存在，正在创建（首次启动需要几分钟）...';

  return new Promise((resolve, reject) => {
    const child = spawn('uv', ['sync', '--all-extras', '--group', 'dev', '--group', 'deepagents-test'], {
      cwd: BACKEND_DIR,
      env: {
        ...process.env,
        VIRTUAL_ENV: '',
      },
      stdio: 'pipe',
    });

    let output = '';
    child.stdout.on('data', (data) => { output += data.toString(); });
    child.stderr.on('data', (data) => { output += data.toString(); });

    child.on('close', (code) => {
      if (code === 0 && fs.existsSync(venvPython)) {
        resolve(true);
      } else {
        backendStatus = 'error';
        backendError = `创建 .venv 失败: ${output.slice(-500)}`;
        reject(new Error(backendError));
      }
    });
  });
}

async function startBackend() {
  const current = await checkBackendStatus();
  if (current === 'running') {
    backendStatus = 'running';
    return { status: 'running', message: 'backend 已经在运行' };
  }

  if (backendProcess) {
    return { status: backendStatus, message: 'backend 正在启动中' };
  }

  backendStatus = 'starting';
  backendError = null;

  try {
    const isDev = !app.isPackaged;
    if (!isDev) {
      await ensureBackendVenv();
    }

    const { cmd, args, cwd } = getBackendCommand();

    backendProcess = spawn(cmd, args, {
      cwd,
      env: {
        ...process.env,
        AI_GATEWAY_URL: process.env.AI_GATEWAY_URL || 'http://localhost:8080/v1',
        MINERU_URL: process.env.MINERU_URL || 'http://localhost:8002',
        VIRTUAL_ENV: '',
      },
      stdio: 'pipe',
    });

    backendProcess.stdout.on('data', (data) => {
      console.log('[backend stdout]', data.toString().trim());
    });

    backendProcess.stderr.on('data', (data) => {
      console.error('[backend stderr]', data.toString().trim());
    });

    backendProcess.on('error', (err) => {
      backendStatus = 'error';
      backendError = err.message;
      backendProcess = null;
    });

    backendProcess.on('exit', (code) => {
      backendStatus = code === 0 ? 'stopped' : 'error';
      if (code !== 0 && code !== null) {
        backendError = `backend 进程退出，退出码: ${code}`;
      }
      backendProcess = null;
    });

    // 等待 backend ready
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const status = await checkBackendStatus();
      if (status === 'running') {
        backendStatus = 'running';
        return { status: 'running', message: 'backend 启动成功' };
      }
    }

    backendStatus = 'error';
    backendError = 'backend 未在 30 秒内就绪';
    return { status: 'error', message: backendError };
  } catch (err) {
    backendStatus = 'error';
    backendError = err.message;
    return { status: 'error', message: err.message };
  }
}

async function stopBackend() {
  if (!backendProcess) {
    backendStatus = 'stopped';
    return { status: 'stopped', message: 'backend 未运行' };
  }

  backendProcess.kill('SIGTERM');

  setTimeout(() => {
    if (backendProcess && !backendProcess.killed) {
      backendProcess.kill('SIGKILL');
    }
  }, 5000);

  backendProcess = null;
  backendStatus = 'stopped';
  backendError = null;
  return { status: 'stopped', message: 'backend 已停止' };
}

function getStatus() {
  return {
    status: backendStatus,
    error: backendError,
    url: `http://127.0.0.1:${BACKEND_PORT}`,
  };
}

module.exports = {
  startBackend,
  stopBackend,
  checkBackendStatus,
  getStatus,
};
