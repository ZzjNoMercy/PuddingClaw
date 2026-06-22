import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL || "http://localhost:8888";

/**
 * Stream chat SSE directly to the backend without Next.js gzip buffering.
 *
 * Next.js rewrites enable gzip compression by default, which buffers
 * streaming responses and causes all tokens to arrive in a few large
 * chunks. This route handler pipes the backend response through a
 * ReadableStream and explicitly disables compression.
 */
export async function POST(request: NextRequest) {
  const backendUrl = `${BACKEND_URL}/api/chat`;

  let body: string;
  try {
    body = await request.text();
  } catch {
    return NextResponse.json({ error: "Invalid request body" }, { status: 400 });
  }

  const upstream = await fetch(backendUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body,
    // Disable Next.js fetch cache and keep connection alive for streaming.
    cache: "no-store",
  });

  if (!upstream.ok) {
    const text = await upstream.text().catch(() => "Upstream error");
    return NextResponse.json(
      { error: text },
      { status: upstream.status }
    );
  }

  const responseHeaders = new Headers();
  responseHeaders.set("Content-Type", "text/event-stream; charset=utf-8");
  responseHeaders.set("Cache-Control", "no-cache, no-transform");
  responseHeaders.set("Connection", "keep-alive");
  // Tell upstream proxies (including Next.js) not to buffer this response.
  responseHeaders.set("X-Accel-Buffering", "no");

  const reader = upstream.body?.getReader();
  if (!reader) {
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
              controller.close();
              return;
            }
            controller.enqueue(value);
            pump();
          })
          .catch((err) => {
            controller.error(err);
          });
      }
      pump();
    },
    cancel() {
      r.cancel().catch(() => {});
    },
  });

  return new Response(stream, {
    status: 200,
    headers: responseHeaders,
  });
}
