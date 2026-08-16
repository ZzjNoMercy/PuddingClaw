const { spawn, exec } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { getRepoRoot, getInfraComposePath } = require('./paths');

const REPO_ROOT = getRepoRoot();
const INFRA_COMPOSE = getInfraComposePath();

// Core 默认 SQLite：bundled PostgreSQL 仅在显式选择 PostgreSQL Core 时才需要启动。
// TODO(gbrain)：启用 gbrain(pgvector) 时同样需要 PostgreSQL，但其配置位于
// gbrain runtime home 的 .gbrain/config.json，electron 侧暂时无法可靠定位；
// 目前 gbrain 用户需保证 catalog 选择 bundled PostgreSQL 或自行启动 PostgreSQL。
//
// 双事实源：GUI 写的是 backend 的 $PUDDINGCLAW_HOME/config.json（database 段，
// 优先），CLI 写的是 deploy.json（infrastructure.catalog 段）。两边都无法识别时
// 保持现状（启动 postgres，安全方向）。
function classifyDatabaseSelection(selection) {
  // 返回 true（需要 bundled postgres）/ false（不需要）/ null（无法识别）。
  if (!selection || typeof selection !== 'object') return null;
  const provider = ['sqlite', 'postgresql'].includes(selection.provider)
    ? selection.provider
    : selection.mode === 'postgresql'
      ? 'postgresql'
      : selection.mode === 'sqlite'
        ? 'sqlite'
        : null;
  if (provider === 'sqlite') return false;
  if (provider === 'postgresql') {
    // 外部 PostgreSQL 由用户自己托管，不应启动本地 bundled postgres。
    if (selection.source === 'external') return false;
    return true;
  }
  return null;
}

function needsBundledPostgres() {
  const home = process.env.PUDDINGCLAW_HOME || path.join(os.homedir(), '.puddingclaw');
  // GUI 事实源优先：backend config.json 的 database 段。
  try {
    const config = JSON.parse(fs.readFileSync(path.join(home, 'config.json'), 'utf8'));
    const decision = classifyDatabaseSelection(config && config.database);
    if (decision !== null) return decision;
  } catch {
    // config.json 缺失/非法时回退 deploy.json。
  }
  try {
    const deploy = JSON.parse(fs.readFileSync(path.join(home, 'deploy.json'), 'utf8'));
    const decision = classifyDatabaseSelection(
      deploy && deploy.infrastructure && deploy.infrastructure.catalog,
    );
    if (decision !== null) return decision;
  } catch {
    // deploy.json 缺失/非法时走安全默认。
  }
  // 两个事实源都缺失/无法识别时保持现状（启动 postgres），避免破坏既有桌面端。
  return true;
}

const INFRA_SERVICES = {
  postgres: { type: 'tcp', host: '127.0.0.1', port: Number(process.env.POSTGRES_PORT || 5432), name: 'PostgreSQL' },
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
  if (service.type === 'tcp') {
    return new Promise((resolve) => {
      const net = require('net');
      const socket = net.createConnection({ host: service.host, port: service.port, timeout: 2000 }, () => {
        socket.destroy();
        resolve('running');
      });
      socket.on('error', () => resolve('stopped'));
      socket.on('timeout', () => {
        socket.destroy();
        resolve('stopped');
      });
    });
  }
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
  const postgresRequired = needsBundledPostgres();
  const dockerOk = await checkDockerDaemon();
  if (!dockerOk) {
    dockerStatus = 'stopped';
    dockerError = 'Docker Desktop 未运行';
    return {
      docker: false,
      postgres: postgresRequired ? 'stopped' : 'not_required',
      milvus: 'stopped',
      status: 'stopped',
      error: dockerError,
    };
  }

  const postgres = postgresRequired ? await checkInfraService('postgres') : 'not_required';
  const milvus = await checkInfraService('milvus');

  const postgresOk = !postgresRequired || postgres === 'running';
  const postgresDown = postgresRequired ? postgres === 'stopped' : true;
  if (postgresOk && milvus === 'running') {
    dockerStatus = 'running';
    dockerError = null;
  } else if (postgresDown && milvus === 'stopped') {
    dockerStatus = 'stopped';
  } else {
    dockerStatus = 'partial';
  }

  return {
    docker: true,
    postgres,
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
    // Core 默认 SQLite：仅在配置（config.json / deploy.json）选择 bundled
    // PostgreSQL Core 时才启动 postgres；否则只起 Milvus 栈（milvus 的
    // depends_on 会自动带上 etcd/minio）。
    const services = needsBundledPostgres() ? [] : ['milvus'];
    await runCommand('docker', ['compose', '-f', INFRA_COMPOSE, 'up', '-d', ...services]);

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
