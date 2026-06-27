import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL || "http://localhost:8888";

export async function POST(request: NextRequest) {
  const backendUrl = `${BACKEND_URL}/api/agent`;

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
    cache: "no-store",
  });

  if (!upstream.ok) {
    const text = await upstream.text().catch(() => "Upstream error");
    return NextResponse.json({ error: text }, { status: upstream.status });
  }

  const responseHeaders = new Headers();
  responseHeaders.set("Content-Type", "text/event-stream; charset=utf-8");
  responseHeaders.set("Cache-Control", "no-cache, no-transform");
  responseHeaders.set("Connection", "keep-alive");
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
