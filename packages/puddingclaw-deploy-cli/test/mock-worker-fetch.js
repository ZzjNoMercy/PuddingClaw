globalThis.fetch = async (url, options) => {
  const body = options?.body ? JSON.parse(options.body) : {};
  if (String(url).endsWith("/api/headless/health")) {
    return new Response(JSON.stringify({
      schema_version: "1",
      agent_id: "puddingclaw",
      cli_version: "0.1.2",
      protocol_version: "1",
      configured: true,
      authenticated: true,
      reachable: true,
      server_version: "0.1.2",
      project_id: "proj_test",
      workspace_ready: true,
    }), { status: 200 });
  }
  if (String(url).endsWith("/api/headless/models")) {
    return new Response(JSON.stringify({ models: [{ id: "auto-analysis" }] }), { status: 200 });
  }
  if (String(url).includes("/api/headless/runs?stream=true")) {
    const events = [
      { event: "run_started", data: { run_id: "run-stream" } },
      { event: "progress", data: { message: "working" } },
      { event: "result", data: { status: "completed", outcome: "completed", final_response: "stream done" } },
    ];
    return new Response(`${events.map(JSON.stringify).join("\n")}\n`, { status: 200 });
  }
  if (String(url).includes("/api/headless/runs/run-respond/resume")) {
    return new Response(JSON.stringify({
      run_id: "run-respond",
      status: "completed",
      outcome: "completed",
      final_response: "responded",
    }), { status: 200 });
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
