import { CliError } from "./errors.js";

const SAFE_ERROR = /authorization|bearer|token|secret|password|api[_-]?key/gi;

function exitCodeForError(code) {
  if (code === "cancelled") return 130;
  if (code === "timeout") return 3;
  if (["session_expired", "interaction_expired", "interaction_conflict", "run_expired"].includes(code)) return 1;
  return 2;
}

export class WorkerClientError extends CliError {
  constructor(message, { status = 0, code = "connection_error" } = {}) {
    super(message, {
      code,
      exitCode: exitCodeForError(code),
      details: Number(status) > 0 ? { http_status: Number(status) } : undefined,
    });
    this.name = "WorkerClientError";
    this.status = status;
  }
}

function safeMessage(value) {
  return String(value || "Request failed").replace(SAFE_ERROR, "credential").slice(0, 500);
}

function responseError(path, response, payload) {
  const detail = typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`;
  const code = response.status === 410
    ? path.includes("/resume") ? "interaction_expired" : path.includes("/cancel") ? "run_expired" : "session_expired"
    : response.status === 409 && path.includes("/resume")
      ? "interaction_conflict"
      : response.status === 401 || response.status === 403
        ? "auth_error"
        : "http_error";
  return new WorkerClientError(safeMessage(detail), { status: response.status, code });
}

export class WorkerClient {
  constructor({ endpoint, token, timeoutMs = 600000 }) {
    this.endpoint = endpoint.replace(/\/+$/, "");
    this.token = token;
    this.timeoutMs = timeoutMs;
  }

  async request(requestPath, { method = "GET", body, signal } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("timeout")), this.timeoutMs);
    const abort = () => controller.abort(new Error("cancelled"));
    if (signal) {
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    }
    try {
      const response = await fetch(`${this.endpoint}${requestPath}`, {
        method,
        redirect: "manual",
        signal: controller.signal,
        headers: {
          accept: "application/json",
          ...(body === undefined ? {} : { "content-type": "application/json" }),
          authorization: `Bearer ${this.token}`,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const raw = await response.text();
      let payload = {};
      try { payload = raw ? JSON.parse(raw) : {}; } catch { payload = {}; }
      if (!response.ok) throw responseError(requestPath, response, payload);
      return payload;
    } catch (error) {
      if (error instanceof WorkerClientError) throw error;
      if (signal?.aborted) throw new WorkerClientError("cancelled", { code: "cancelled" });
      if (error?.name === "AbortError" || String(error?.message || "").includes("timeout")) {
        throw new WorkerClientError("Worker request timed out", { code: "timeout" });
      }
      throw new WorkerClientError(safeMessage(error?.message || error), { code: "connection_error" });
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", abort);
    }
  }

  async streamJsonl(requestPath, { method = "POST", body, signal, onEvent } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("timeout")), this.timeoutMs);
    const abort = () => controller.abort(new Error("cancelled"));
    if (signal) {
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    }
    try {
      const response = await fetch(`${this.endpoint}${requestPath}`, {
        method,
        redirect: "manual",
        signal: controller.signal,
        headers: {
          accept: "application/x-ndjson",
          "content-type": "application/json",
          authorization: `Bearer ${this.token}`,
        },
        body: JSON.stringify(body ?? {}),
      });
      if (!response.ok) {
        const raw = await response.text();
        let payload = {};
        try { payload = raw ? JSON.parse(raw) : {}; } catch { payload = {}; }
        throw responseError(requestPath, response, payload);
      }
      const decoder = new TextDecoder();
      let buffer = "";
      let lastResult = null;
      const consume = async (text) => {
        buffer += text;
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          let event;
          try { event = JSON.parse(line); } catch {
            throw new WorkerClientError("Worker returned invalid JSONL", { code: "protocol_error" });
          }
          if (event?.event === "result" && event.data && typeof event.data === "object") {
            lastResult = event.data;
          }
          if (onEvent) await onEvent(event);
        }
      };
      if (response.body?.getReader) {
        const reader = response.body.getReader();
        while (true) {
          const next = await reader.read();
          if (next.done) break;
          await consume(decoder.decode(next.value, { stream: true }));
        }
        await consume(decoder.decode());
      } else {
        await consume(await response.text());
      }
      if (buffer.trim()) await consume("\n");
      return lastResult || {};
    } catch (error) {
      if (error instanceof WorkerClientError) throw error;
      if (signal?.aborted) throw new WorkerClientError("cancelled", { code: "cancelled" });
      if (error?.name === "AbortError" || String(error?.message || "").includes("timeout")) {
        throw new WorkerClientError("Worker request timed out", { code: "timeout" });
      }
      throw new WorkerClientError(safeMessage(error?.message || error), { code: "connection_error" });
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", abort);
    }
  }
}
