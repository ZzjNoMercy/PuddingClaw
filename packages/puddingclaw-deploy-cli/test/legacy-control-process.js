import fs from "node:fs";
import path from "node:path";

const [controlPath, controlToken, instanceId, role] = process.argv.slice(2);
fs.mkdirSync(controlPath, { recursive: true, mode: 0o700 });
fs.writeFileSync(path.join(controlPath, "ready"), "ready\n", { mode: 0o600 });

const timer = setInterval(() => {
  const requestPath = path.join(controlPath, "request.json");
  const responsePath = path.join(controlPath, "response.json");
  try {
    const request = JSON.parse(fs.readFileSync(requestPath, "utf8"));
    if (request.action !== "identify" || request.token !== controlToken) return;
    const temporary = path.join(controlPath, `.legacy-response-${process.pid}.tmp`);
    fs.writeFileSync(temporary, `${JSON.stringify({
      ok: true,
      nonce: request.nonce,
      instance_id: instanceId,
      role,
      pid: process.pid,
    })}\n`, { mode: 0o600 });
    fs.renameSync(temporary, responsePath);
  } catch (error) {
    if (error?.code !== "ENOENT") process.stderr.write(`${error.message}\n`);
  }
}, 50);

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    clearInterval(timer);
    process.exit(0);
  });
}
