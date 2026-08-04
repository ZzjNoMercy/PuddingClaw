const SAFE_ERROR = /authorization|bearer|token|secret|password|api[_-]?key/gi;

export class WorkerClientError extends Error {
  constructor(message, { status = 0, code = "connection_error" } = {}) {
    super(message);
    this.name = "WorkerClientError";
    this.status = status;
    this.code = code;
  }
}

function safeMessage(value) {
  return String(value || "Request failed").replace(SAFE_ERROR, "credential").slice(0, 500);
}

export class WorkerClient {
  constructor({ endpoint, token, timeoutMs = 600000 }) {
    this.endpoint = endpoint.replace(/\/+$/, "");
    this.token = token;
    this.timeoutMs = timeoutMs;
  }

  async request(path, { method = "GET", body, signal } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("timeout")), this.timeoutMs);
    const abort = () => controller.abort(new Error("cancelled"));
    if (signal) {
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    }
    try {
      const response = await fetch(`${this.endpoint}${path}`, {
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
      if (!response.ok) {
        const detail = typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`;
        throw new WorkerClientError(safeMessage(detail), {
          status: response.status,
          code: response.status === 410
            ? "session_expired"
            : response.status === 401 || response.status === 403
              ? "auth_error"
              : "http_error",
        });
      }
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
}
