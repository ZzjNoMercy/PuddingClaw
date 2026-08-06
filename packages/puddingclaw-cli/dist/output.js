export function writeJson(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

export function writeDiagnostic(message) {
  process.stderr.write(`${String(message).replace(/[\r\n]+/g, " ")}\n`);
}

export function exitCodeForResponse(response) {
  return response?.outcome === "completed" || response?.status === "completed"
    || response?.outcome === "cancelled" || response?.status === "cancelled" ? 0 : 1;
}
