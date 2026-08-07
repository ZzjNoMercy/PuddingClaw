import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL || "http://localhost:8888";

export async function POST(request: NextRequest) {
  const backendUrl = `${BACKEND_URL}/api/agent`;
  const upstreamController = new AbortController();
  const abortUpstream = () => {
    if (!upstreamController.signal.aborted) upstreamController.abort();
  };

  let body: string;
  try {
    body = await request.text();
  } catch {
    return NextResponse.json({ error: "Invalid request body" }, { status: 400 });
  }

  if (request.signal.aborted) abortUpstream();
  else request.signal.addEventListener("abort", abortUpstream, { once: true });

  let upstream: Response;
  try {
    upstream = await fetch(backendUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body,
      cache: "no-store",
      signal: upstreamController.signal,
    });
  } catch (error) {
    request.signal.removeEventListener("abort", abortUpstream);
    if (upstreamController.signal.aborted) {
      return new Response(null, { status: 499 });
    }
    throw error;
  }

  if (!upstream.ok) {
    const text = await upstream.text().catch(() => "Upstream error");
    request.signal.removeEventListener("abort", abortUpstream);
    return NextResponse.json({ error: text }, { status: upstream.status });
  }

  const responseHeaders = new Headers();
  responseHeaders.set("Content-Type", "text/event-stream; charset=utf-8");
  responseHeaders.set("Cache-Control", "no-cache, no-transform");
  responseHeaders.set("Connection", "keep-alive");
  responseHeaders.set("X-Accel-Buffering", "no");

  const reader = upstream.body?.getReader();
  if (!reader) {
    request.signal.removeEventListener("abort", abortUpstream);
    return NextResponse.json({ error: "No response body" }, { status: 502 });
  }

  const r = reader;
  const stream = new ReadableStream({
    start(controller) {
      function pump() {
        r
          .read()
          .then(({ done, value }) => {
            if (done) {
              request.signal.removeEventListener("abort", abortUpstream);
              controller.close();
              return;
            }
            controller.enqueue(value);
            pump();
          })
          .catch((err) => {
            request.signal.removeEventListener("abort", abortUpstream);
            if (!upstreamController.signal.aborted) controller.error(err);
          });
      }
      pump();
    },
    cancel() {
      request.signal.removeEventListener("abort", abortUpstream);
      abortUpstream();
      return r.cancel().catch(() => {});
    },
  });

  return new Response(stream, {
    status: 200,
    headers: responseHeaders,
  });
}
