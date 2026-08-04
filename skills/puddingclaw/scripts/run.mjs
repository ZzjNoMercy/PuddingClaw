#!/usr/bin/env node

import { spawn } from "node:child_process";
import { lstatSync, readFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const skillDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const envFile = path.join(skillDir, ".env");

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(2);
}

function unquote(value) {
  const trimmed = String(value || "").trim();
  if (trimmed.length >= 2) {
    const first = trimmed[0];
    const last = trimmed[trimmed.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return trimmed.slice(1, -1);
    }
  }
  return trimmed;
}

function tokenFromSkillEnv() {
  let status;
  try {
    status = lstatSync(envFile);
  } catch (error) {
    if (error?.code === "ENOENT") {
      fail("PuddingClaw Skill is not configured. Copy .env.example to .env and add PUDDINGCLAW_TOKEN.");
    }
    fail("PuddingClaw Skill .env could not be inspected.");
  }
  if (!status.isFile() || status.isSymbolicLink() || status.size > 16 * 1024) {
    fail("PuddingClaw Skill .env is invalid.");
  }
  if (process.platform !== "win32" && (status.mode & 0o077) !== 0) {
    fail("PuddingClaw Skill .env must have mode 0600.");
  }
  let token = "";
  for (const line of readFileSync(envFile, "utf8").split(/\r?\n/)) {
    const match = line.match(/^\s*PUDDINGCLAW_TOKEN\s*=\s*(.*)$/);
    if (!match) continue;
    if (token) fail("PuddingClaw Skill .env contains duplicate PUDDINGCLAW_TOKEN entries.");
    token = unquote(match[1]);
  }
  if (!token) fail("PuddingClaw Skill .env does not contain PUDDINGCLAW_TOKEN.");
  return token;
}

const injectedToken = String(process.env.PUDDINGCLAW_TOKEN || "").trim() || tokenFromSkillEnv();
const executable = String(process.env.PUDDINGCLAW_CLI_BIN || "puddingclaw");
const childEnv = { ...process.env, PUDDINGCLAW_TOKEN: injectedToken };
delete childEnv.PUDDINGCLAW_CLI_BIN;

const child = spawn(executable, process.argv.slice(2), {
  env: childEnv,
  stdio: "inherit",
  shell: false,
});

child.once("error", () => fail("Could not start the puddingclaw CLI."));
child.once("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exitCode = code ?? 1;
});
