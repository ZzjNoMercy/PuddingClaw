const { app } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { getRepoRoot, getBackendDir } = require('./paths');

function getPuddingClawHome() {
  return process.env.PUDDINGCLAW_HOME || path.join(os.homedir(), '.puddingclaw');
}

function getCliEntry() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'cli', 'src', 'cli.js');
  }
  return path.join(getRepoRoot(), 'packages', 'puddingclaw-deploy-cli', 'src', 'cli.js');
}

function getCliEnvironment() {
  const environment = {
    ...process.env,
    ELECTRON_RUN_AS_NODE: '1',
    PUDDINGCLAW_HOME: getPuddingClawHome(),
    PUDDINGCLAW_DESKTOP_PACKAGED: app.isPackaged ? '1' : '0',
  };
  if (!app.isPackaged && !environment.PUDDINGCLAW_DEPLOY_PYTHON) {
    const python = process.platform === 'win32'
      ? path.join(getBackendDir(), '.venv', 'Scripts', 'python.exe')
      : path.join(getBackendDir(), '.venv', 'bin', 'python');
    if (fs.existsSync(python)) environment.PUDDINGCLAW_DEPLOY_PYTHON = python;
  }
  return environment;
}

function runCli(args, { timeoutMs = 20_000 } = {}) {
  const cliEntry = getCliEntry();
  return new Promise((resolve, reject) => {
    if (!fs.existsSync(cliEntry)) {
      reject(new Error(`未找到 PuddingClaw CLI: ${cliEntry}`));
      return;
    }
    const child = spawn(process.execPath, [cliEntry, ...args, '--json'], {
      cwd: path.dirname(cliEntry),
      env: getCliEnvironment(),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      child.kill('SIGTERM');
      reject(new Error('CLI 探测超时'));
    }, timeoutMs);
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once('close', (code) => {
      clearTimeout(timer);
      let payload;
      try {
        payload = JSON.parse(stdout.trim() || '{}');
      } catch {
        reject(new Error(`CLI 返回了无效 JSON: ${(stderr || stdout).slice(-500)}`));
        return;
      }
      if (code !== 0 || payload.status === 'error') {
        reject(new Error(payload.error || stderr.trim() || `CLI 退出码 ${code}`));
        return;
      }
      resolve(payload);
    });
  });
}

async function getOnboardingState() {
  try {
    const status = await runCli(['status']);
    return {
      available: true,
      initialized: Boolean(status.initialized),
      profile: status.profile || null,
      extensions: status.extensions || null,
      home: status.home || getPuddingClawHome(),
    };
  } catch (error) {
    return {
      available: false,
      initialized: false,
      profile: null,
      extensions: null,
      home: getPuddingClawHome(),
      error: error.message,
    };
  }
}

function inspectProfile(profile) {
  return runCli(['profile', 'inspect', profile], { timeoutMs: 30_000 });
}

function applyProfile(profile) {
  return runCli(['profile', 'apply', profile], { timeoutMs: 30_000 });
}

async function ensurePreparedRuntime() {
  const inspected = await runCli(['runtime', 'inspect']);
  let installed = inspected;
  if (inspected.status !== 'installed') {
    installed = await runCli(['runtime', 'install', 'bundled'], { timeoutMs: 120_000 });
  }
  const prepared = await runCli(['runtime', 'prepare'], { timeoutMs: 20 * 60_000 });
  return { installed, prepared };
}

function getPreparedBackendCommand() {
  try {
    const home = getPuddingClawHome();
    const config = JSON.parse(fs.readFileSync(path.join(home, 'deploy.json'), 'utf8'));
    const active = JSON.parse(fs.readFileSync(path.join(home, 'runtime', 'active.json'), 'utf8'));
    const python = String(config.runtime?.python?.command || '');
    const root = String(active.path || '');
    if (!path.isAbsolute(python) || !fs.existsSync(python) || !path.isAbsolute(root)) return null;
    return {
      cmd: python,
      args: ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', '8888'],
      cwd: path.join(root, 'backend'),
    };
  } catch {
    return null;
  }
}

function readSelectedProfile() {
  try {
    const config = JSON.parse(fs.readFileSync(path.join(getPuddingClawHome(), 'deploy.json'), 'utf8'));
    const profile = String(config.profile || '');
    const extensions = config.extensions || {};
    return {
      profile,
      extensions: {
        knowledge: Boolean(extensions.knowledge?.enabled),
        analytics: Boolean(extensions.analytics?.enabled),
        headless_worker: extensions.headless_worker?.enabled !== false,
      },
    };
  } catch {
    return null;
  }
}

module.exports = {
  applyProfile,
  ensurePreparedRuntime,
  getPreparedBackendCommand,
  getOnboardingState,
  getPuddingClawHome,
  inspectProfile,
  readSelectedProfile,
  runCli,
};
