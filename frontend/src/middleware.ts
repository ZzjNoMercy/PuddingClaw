import { NextRequest, NextResponse } from "next/server";

function enabled(name: string): boolean {
  const explicit = process.env[`PUDDINGCLAW_EXTENSION_${name.toUpperCase()}`];
  if (explicit !== undefined) {
    return ["1", "true", "yes", "on"].includes(explicit.trim().toLowerCase());
  }
  try {
    const extensions = JSON.parse(process.env.PUDDINGCLAW_EXTENSIONS || "null") as Record<string, unknown> | null;
    if (extensions && typeof extensions[name] === "boolean") return Boolean(extensions[name]);
  } catch {
    // Preserve full source-checkout navigation when no CLI contract exists.
  }
  return true;
}

export function middleware(request: NextRequest) {
  const pathname = request.nextUrl.pathname;
  if (pathname.startsWith("/knowledge") && !enabled("knowledge")) {
    return NextResponse.redirect(new URL("/extension-disabled?extension=knowledge", request.url));
  }
  if (pathname.startsWith("/analytics") && !enabled("analytics")) {
    return NextResponse.redirect(new URL("/extension-disabled?extension=analytics", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/knowledge/:path*", "/analytics/:path*"],
};
