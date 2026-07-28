export interface LarkAuthorizationDetails {
  url: string;
  qr: string;
}

function managedCliOutput(output?: string): string {
  let value: unknown = String(output || "");
  for (let depth = 0; depth < 2 && typeof value === "string"; depth += 1) {
    const text = value.trim();
    if (!text.startsWith("{")) return String(value);
    try {
      const parsed = JSON.parse(text) as unknown;
      const envelope = parsed && typeof parsed === "object"
        ? parsed as Record<string, unknown>
        : null;
      if (
        envelope?.managed_by === "managed_cli"
        && typeof envelope.output === "string"
      ) {
        value = envelope.output;
        continue;
      }
      return String(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function larkConfigurationUrl(text: string): string | null {
  const candidate = text.match(
    /https:\/\/(?:open\.feishu\.cn|open\.larksuite\.com)\/page\/cli\?[^\s\\"'<>]+/i,
  )?.[0];
  if (!candidate) return null;
  try {
    const url = new URL(candidate);
    if (
      !["open.feishu.cn", "open.larksuite.com"].includes(url.hostname.toLowerCase())
      || url.pathname !== "/page/cli"
      || !url.searchParams.get("user_code")
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

export function larkAuthorizationDetails(output?: string): LarkAuthorizationDetails | null {
  const text = managedCliOutput(output).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
  if (!text.includes("Status: awaiting_user_browser")) return null;
  const url = larkConfigurationUrl(text);
  if (!url) return null;
  const qrLines = text
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => /[█▀▄]/.test(line));
  const qr = qrLines.length >= 10 ? qrLines.join("\n") : "";
  return { url, qr };
}
