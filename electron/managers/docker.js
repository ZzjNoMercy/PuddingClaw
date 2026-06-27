const { spawn, exec } = require('child_process');
const path = require('path');
const http = require('http');
const { getRepoRoot, getInfraComposePath } = require('./paths');

const REPO_ROOT = getRepoRoot();
const INFRA_COMPOSE = getInfraComposePath();

const INFRA_SERVICES = {
  higress: { url: 'http://127.0.0.1:8080/health', name: 'Higress' },
  milvus: { url: 'http://127.0.0.1:19530', name: 'Milvus' },
};

let dockerStatus = 'unknown';
let dockerError = null;

function runCommand(cmd, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, {
      cwd: REPO_ROOT,
      env: { ...process.env },
      stdio: 'pipe',
      ...options,
    });

    let stdout = '';
    let stderr = '';

    child.stdout.on('data', (data) => {
      stdout += data.toString();
      console.log(`[docker ${cmd}]`, data.toString().trim());
    });

    child.stderr.on('data', (data) => {
      stderr += data.toString();
      console.error(`[docker ${cmd}]`, data.toString().trim());
    });

    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) {
        resolve(stdout);
      } else {
        reject(new Error(stderr || `命令退出码: ${code}`));
      }
    });
  });
}

async function checkDockerDaemon() {
  return new Promise((resolve) => {
    exec('docker info', { timeout: 3000 }, (err) => {
      resolve(!err);
    });
  });
}

async function checkInfraService(key) {
  const service = INFRA_SERVICES[key];
  return new Promise((resolve) => {
    const req = http.get(service.url, { timeout: 2000 }, (res) => {
      resolve(res.statusCode < 500 ? 'running' : 'error');
    });
    req.on('error', () => resolve('stopped'));
    req.on('timeout', () => {
      req.destroy();
      resolve('stopped');
    });
  });
}

async function checkInfraStatus() {
  const dockerOk = await checkDockerDaemon();
  if (!dockerOk) {
    dockerStatus = 'stopped';
    dockerError = 'Docker Desktop 未运行';
    return {
      docker: false,
      higress: 'stopped',
      milvus: 'stopped',
      status: 'stopped',
      error: dockerError,
    };
  }

  const higress = await checkInfraService('higress');
  const milvus = await checkInfraService('milvus');

  if (higress === 'running' && milvus === 'running') {
    dockerStatus = 'running';
    dockerError = null;
  } else if (higress === 'stopped' && milvus === 'stopped') {
    dockerStatus = 'stopped';
  } else {
    dockerStatus = 'partial';
  }

  return {
    docker: true,
    higress,
    milvus,
    status: dockerStatus,
    error: dockerError,
  };
}

async function startInfra() {
  const dockerOk = await checkDockerDaemon();
  if (!dockerOk) {
    dockerStatus = 'error';
    dockerError = 'Docker Desktop 未运行，请先启动 Docker Desktop';
    return { status: 'error', message: dockerError };
  }

  try {
    dockerStatus = 'starting';
    await runCommand('docker', ['compose', '-f', INFRA_COMPOSE, 'up', '-d']);

    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const status = await checkInfraStatus();
      if (status.status === 'running') {
        return { status: 'running', message: '基础设施启动成功' };
      }
    }

    dockerStatus = 'error';
    dockerError = '基础设施未在 60 秒内就绪';
    return { status: 'error', message: dockerError };
  } catch (err) {
    dockerStatus = 'error';
    dockerError = err.message;
    return { status: 'error', message: err.message };
  }
}

async function stopInfra() {
  try {
    await runCommand('docker', ['compose', '-f', INFRA_COMPOSE, 'down']);
    dockerStatus = 'stopped';
    dockerError = null;
    return { status: 'stopped', message: '基础设施已停止' };
  } catch (err) {
    dockerStatus = 'error';
    dockerError = err.message;
    return { status: 'error', message: err.message };
  }
}

function getStatus() {
  return {
    status: dockerStatus,
    error: dockerError,
  };
}

module.exports = {
  checkDockerDaemon,
  checkInfraStatus,
  startInfra,
  stopInfra,
  getStatus,
};
