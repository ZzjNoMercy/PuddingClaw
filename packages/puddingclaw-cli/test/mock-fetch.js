globalThis.fetch = async (url, options) => {
  if (String(url).endsWith("/api/headless/health")) {
    return new Response(JSON.stringify({
      schema_version: "1",
      agent_id: "puddingclaw",
      cli_version: "0.1.0",
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
        version: "0.1.0",
        required_version: "0.1.0",
        version_mismatch: false,
        node: { available: true, path: "/opt/homebrew/bin/node", version: "26.5.0" },
        npm: { available: true, path: "/opt/homebrew/bin/npm", version: "11.17.0" },
        install_policy: "auto",
      },
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  const body = options?.body ? JSON.parse(options.body) : {};
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
    analytics_model_id: "auto-analysis",
    analytics_model_match: { status: "matched", selected_id: "auto-analysis", strategy: "semantic" },
    session_id: body.session_id,
  }), { status: 200, headers: { "content-type": "application/json" } });
};
