globalThis.fetch = async (_url, options) => {
  const body = options?.body ? JSON.parse(options.body) : {};
  return new Response(JSON.stringify({
    schema_version: "1",
    status: "completed",
    outcome: "completed",
    reply: "ok",
    analytics_model_id: body.analytics_model_id,
  }), { status: 200, headers: { "content-type": "application/json" } });
};
