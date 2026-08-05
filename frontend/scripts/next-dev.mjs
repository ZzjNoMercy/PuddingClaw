#!/usr/bin/env node

import { spawn } from "node:child_process";
import process from "node:process";

const forwarded = process.argv.slice(2);

function requestedPort(args) {
  for (let index = 0; index < args.length; index += 1) {
    const value = args[index];
    if ((value === "-p" || value === "--port") && args[index + 1]) return args[index + 1];
    if (value.startsWith("--port=")) return value.slice("--port=".length);
  }
  return process.env.PORT || "3000";
}

const port = requestedPort(forwarded);
const hasHost = forwarded.some((value) => value === "-H" || value === "--hostname" || value.startsWith("--hostname="));
const hasPort = forwarded.some((value) => value === "-p" || value === "--port" || value.startsWith("--port="));
const args = ["next", "dev"];
if (!hasHost) args.push("-H", "0.0.0.0");
if (!hasPort) args.push("-p", port);
args.push(...forwarded);

const command = process.platform === "win32" ? "npx.cmd" : "/bin/bash";
const commandArgs = process.platform === "win32"
  ? args
  : [
      "-c",
      // Raise a small inherited limit, but never lower a larger one. Lowering
      // a 1M shell limit to 65K caused Watchpack to miss app routes and leave
      // the dev server serving only its generated 404 page.
      'current="$(ulimit -n 2>/dev/null || echo 0)"; target="${PUDDINGCLAW_MAX_OPEN_FILES:-122880}"; case "$current:$target" in *[!0-9:]*|:*) ;; *) if [ "$current" -lt "$target" ]; then ulimit -n "$target" 2>/dev/null || true; fi ;; esac; exec "$@"',
      "puddingclaw-next-dev",
      "npx",
      ...args,
    ];
const child = spawn(command, commandArgs, {
  stdio: "inherit",
  env: {
    ...process.env,
    // Never let two dev servers (or next build) mutate the same chunk graph.
    NEXT_DIST_DIR: process.env.NEXT_DIST_DIR || `.next-dev-${port}`,
  },
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
