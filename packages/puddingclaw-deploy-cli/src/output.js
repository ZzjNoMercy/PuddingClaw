export function writeJson(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

export function writeHuman(value) {
  process.stdout.write(`${value}\n`);
}

export function writeError(value) {
  process.stderr.write(`${value}\n`);
}

export function mark(status) {
  if (status === "ok" || status === "available") return "✓";
  if (status === "disabled" || status === "skipped") return "○";
  if (status === "warning" || status === "needs_action") return "!";
  return "✗";
}
