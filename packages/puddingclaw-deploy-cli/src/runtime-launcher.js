#!/usr/bin/env node

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const [instanceId, role] = process.argv.slice(2);
const command = process.env.PUDDINGCLAW_LAUNCH_COMMAND;
const cwd = process.env.PUDDINGCLAW_LAUNCH_CWD;
const controlPath = process.env.PUDDINGCLAW_CONTROL_PATH;
const controlToken = process.env.PUDDINGCLAW_CONTROL_TOKEN;

let args;
try {
  args = JSON.parse(process.env.PUDDINGCLAW_LAUNCH_ARGS || "[]");
} catch {
  process.stderr.write("invalid PUDDINGCLAW_LAUNCH_ARGS\n");
  process.exit(127);
}

if (!instanceId || !role || !command || !cwd || !controlPath || !controlToken || !Array.isArray(args)) {
  process.stderr.write("runtime launcher configuration is incomplete\n");
  process.exit(127);
}

const childEnv = { ...process.env };
for (const key of [
  "PUDDINGCLAW_LAUNCH_COMMAND",
  "PUDDINGCLAW_LAUNCH_CWD",
  "PUDDINGCLAW_LAUNCH_ARGS",
  "PUDDINGCLAW_CONTROL_PATH",
  "PUDDINGCLAW_CONTROL_TOKEN",
]) {
  delete childEnv[key];
}

fs.mkdirSync(controlPath, { recursive: true, mode: 0o700 });
const controlTimer = setInterval(() => {
  let requests = [];
  try {
    requests = fs.readdirSync(controlPath).filter((name) => /^request-[a-f0-9-]{36}\.json$/i.test(name));
  } catch (error) {
    if (error?.code !== "ENOENT") process.stderr.write(`runtime control scan failed: ${error.message}\n`);
  }
  for (const fileName of requests) {
    const requestPath = path.join(controlPath, fileName);
    try {
      const request = JSON.parse(fs.readFileSync(requestPath, "utf8"));
      const nonce = fileName.slice("request-".length, -".json".length);
      if (request.action !== "identify" || request.token !== controlToken || request.nonce !== nonce) continue;
      const responsePath = path.join(controlPath, `response-${nonce}.json`);
      const temporary = path.join(controlPath, `.response-${nonce}-${process.pid}.tmp`);
      fs.writeFileSync(temporary, `${JSON.stringify({
        ok: true,
        nonce,
        instance_id: instanceId,
        role,
        pid: process.pid,
      })}\n`, { mode: 0o600 });
      fs.renameSync(temporary, responsePath);
      fs.rmSync(requestPath, { force: true });
    } catch (error) {
      if (error?.code !== "ENOENT") {
        process.stderr.write(`runtime control check failed: ${error.message}\n`);
      }
    }
  }
}, 100);
controlTimer.unref();

const child = spawn(command, args, {
  cwd,
  env: childEnv,
  stdio: "inherit",
});

process.on("SIGTERM", () => {
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
});
process.on("SIGINT", () => {
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGINT");
});

child.once("error", (error) => {
  process.stderr.write(`failed to launch ${role}: ${error.message}\n`);
  process.exit(127);
});

child.once("exit", (code, signal) => {
  clearInterval(controlTimer);
  process.exit(code ?? 1);
});
