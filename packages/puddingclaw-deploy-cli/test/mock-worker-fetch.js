import { createRequire } from "node:module";

const packageVersion = createRequire(import.meta.url)("../package.json").version;

globalThis.fetch = async (url, options) => {
  const body = options?.body ? JSON.parse(options.body) : {};
  if (String(url).endsWith("/api/headless/health")) {
    return new Response(JSON.stringify({
      schema_version: "1",
      agent_id: "puddingclaw",
      cli_version: packageVersion,
      protocol_version: "1",
      configured: true,
      reachable: true,
      server_version: packageVersion,
      project_id: "proj_test",
      workspace_ready: true,
    }), { status: 200 });
  }
  if (String(url).endsWith("/api/headless/models")) {
    return new Response(JSON.stringify({ models: [{ id: "auto-analysis" }] }), { status: 200 });
  }
  if (String(url).includes("/api/headless/runs?stream=true") && body.message === "needs approval") {
    return new Response(`${JSON.stringify({ event: "run_started", data: { run_id: "run-approval" } })}\n${JSON.stringify({ event: "result", data: {
      run_id: "run-approval",
      status: "needs_input",
      outcome: "waiting_hitl",
      continuation_token: "continuation-token-long-enough",
      needs_input: {
        type: "permission_request",
        request_id: "permission-1",
        command: "python task.py",
        options: ["once", "session"],
      },
    } })}\n`, { status: 200 });
  }
  if (String(url).includes("/api/headless/runs?stream=true")) {
    if (process.env.MOCK_LEGACY_STREAM === "1") {
      return new Response(JSON.stringify({
        status: "completed",
        outcome: "completed",
        final_response: "legacy done",
        session_id: body.session_id,
      }), { status: 200, headers: { "content-type": "application/json" } });
    }
    if (body.session_id === "expired-session") {
      return new Response(JSON.stringify({ detail: "Headless Session expired" }), { status: 410 });
    }
    const isStreamFixture = body.message === "stream";
    const events = [
      { event: "run_started", data: { run_id: isStreamFixture ? "run-stream" : "run-default" } },
      { event: "progress", data: { message: "working" } },
      { event: "result", data: {
        status: "completed",
        outcome: "completed",
        reply: isStreamFixture ? "stream done" : "ok",
        final_response: isStreamFixture ? "stream done" : "final ok",
        session_id: body.session_id,
        request_url: String(url),
        authorization_header: options?.headers?.authorization ?? null,
        ...(body.workspace_path ? { workspace_path: body.workspace_path } : {}),
        ...(body.message === "export" ? {
          artifacts: [{ name: "report.txt", path: "report.txt", kind: "text" }],
        } : {}),
      } },
    ];
    return new Response(`${events.map(JSON.stringify).join("\n")}\n`, { status: 200 });
  }
  if (String(url).includes("/api/headless/runs/run-respond/resume")) {
    const events = [
      { event: "permission_resolved", data: { request_id: "permission-1" } },
      { event: "tool_start", data: { id: "tool-1", tool: "search" } },
      { event: "tool_end", data: { id: "tool-1", tool: "search", output: "ok" } },
      { event: "result", data: {
        run_id: "run-respond",
        status: "completed",
        outcome: "completed",
        final_response: "responded",
      } },
    ];
    return new Response(`${events.map(JSON.stringify).join("\n")}\n`, { status: 200 });
  }
  if (String(url).includes("/api/headless/runs/run-cancel/cancel")) {
    return new Response(JSON.stringify({
      run_id: "run-cancel",
      status: "cancelled",
      outcome: "cancelled",
    }), { status: 200 });
  }
  if (body.session_id === "expired-session") {
    return new Response(JSON.stringify({ detail: "Headless Session expired" }), { status: 410 });
  }
  if (body.message === "needs approval") {
    return new Response(JSON.stringify({
      run_id: "run-approval",
      status: "needs_input",
      outcome: "waiting_hitl",
      continuation_token: "continuation-token-long-enough",
      needs_input: {
        type: "permission_request",
        request_id: "permission-1",
        command: "python task.py",
        options: ["once", "session"],
      },
    }), { status: 200 });
  }
  return new Response(JSON.stringify({
    status: "completed",
    outcome: "completed",
    reply: "ok",
    final_response: "final ok",
    request_url: String(url),
    session_id: body.session_id,
    ...(body.message === "export" ? {
      artifacts: [{ name: "report.txt", path: "report.txt", kind: "text" }],
    } : {}),
    ...(body.workspace_path ? { workspace_path: body.workspace_path } : {}),
  }), { status: 200 });
};
