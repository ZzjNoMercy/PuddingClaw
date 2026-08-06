globalThis.fetch = async (url, options) => {
  if (String(url).endsWith("/api/headless/health")) {
    return new Response(JSON.stringify({
      schema_version: "1",
      agent_id: "puddingclaw",
      cli_version: "0.2.0",
      protocol_version: "1",
      configured: true,
      authenticated: true,
      reachable: true,
      server_version: "0.1.0",
      project_id: "proj_test",
      workspace_ready: true,
      capabilities: ["data.query", "data.analysis"],
      key_id: "wak_test",
      cli: {
        command: "puddingclaw",
        installed: true,
        version: "0.2.0",
        required_version: "0.2.0",
        version_mismatch: false,
        node: { available: true, path: "/opt/homebrew/bin/node", version: "26.5.0" },
        npm: { available: true, path: "/opt/homebrew/bin/npm", version: "11.17.0" },
        install_policy: "auto",
      },
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  const body = options?.body ? JSON.parse(options.body) : {};
  if (String(url).includes("/api/headless/runs?stream=true")) {
    const events = [
      { event: "run_started", data: { run_id: "run-stream", session_id: "worker-session-stream" } },
      { event: "progress", data: { message: "正在处理" } },
      { event: "result", data: { schema_version: "1", run_id: "run-stream", status: "completed", outcome: "completed", final_response: "stream done", artifacts: [] } },
    ];
    return new Response(`${events.map((item) => JSON.stringify(item)).join("\n")}\n`, { status: 200, headers: { "content-type": "application/x-ndjson" } });
  }
  if (String(url).includes("/api/headless/runs/run-respond/resume")) {
    return new Response(JSON.stringify({
      schema_version: "1",
      run_id: "run-respond",
      session_id: "worker-session-respond",
      status: "completed",
      outcome: "completed",
      reply: "responded",
      final_response: "responded",
      artifacts: [],
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (String(url).includes("/api/headless/runs/run-cancel/cancel")) {
    return new Response(JSON.stringify({
      schema_version: "1",
      run_id: "run-cancel",
      status: "cancelled",
      outcome: "cancelled",
      artifacts: [],
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (String(url).includes("/api/headless/runs/run-approval/resume")) {
    return new Response(JSON.stringify({
      schema_version: "1",
      run_id: "run-approval",
      session_id: "worker-session-approval",
      status: "completed",
      outcome: "completed",
      reply: "approved",
      final_response: "approved",
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (body.message === "需要授权") {
    return new Response(JSON.stringify({
      schema_version: "1",
      run_id: "run-approval",
      session_id: "worker-session-approval",
      status: "needs_input",
      outcome: "waiting_hitl",
      continuation_token: "continuation-token-long-enough",
      needs_input: {
        type: "permission_request",
        request_id: "perm-req-test",
        tool_name: "execute",
        command: "python3 /skills/test/run.py",
        options: ["once", "session"],
      },
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (body.session_id === "worker-session-expired") {
    return new Response(JSON.stringify({ detail: "Headless Session expired after its configured inactivity TTL" }), {
      status: 410,
      headers: { "content-type": "application/json" },
    });
  }
  return new Response(JSON.stringify({
    schema_version: "1",
    status: "completed",
    outcome: "completed",
    reply: "ok",
    final_response: "final ok",
    ...(body.message === "导出测试" ? { artifacts: [{ name: "report.csv", path: "report.csv", kind: "data", size: 4, origin: "push" }] } : {}),
    analytics_model_id: "auto-analysis",
    analytics_model_match: { status: "matched", selected_id: "auto-analysis", strategy: "semantic" },
    session_id: body.session_id,
  }), { status: 200, headers: { "content-type": "application/json" } });
};
