export const MAX_LIGHTWEIGHT_HTML_BYTES = 256 * 1024;

export interface LightweightHtmlDocument {
  html: string;
  title: string;
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function decodeTitleEntities(value: string): string {
  const entities: Record<string, string> = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    quot: '"',
  };

  return value.replace(/&(#\d+|#x[\da-f]+|amp|apos|gt|lt|quot);/gi, (match, entity: string) => {
    if (entity.startsWith("#x") || entity.startsWith("#X")) {
      const codePoint = Number.parseInt(entity.slice(2), 16);
      return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : match;
    }
    if (entity.startsWith("#")) {
      const codePoint = Number.parseInt(entity.slice(1), 10);
      return Number.isFinite(codePoint) ? String.fromCodePoint(codePoint) : match;
    }
    return entities[entity.toLowerCase()] || match;
  });
}

export function extractLightweightHtmlTitle(html: string): string {
  const rawTitle = html.match(/<title\b[^>]*>([\s\S]*?)<\/title\s*>/i)?.[1] || "";
  const title = decodeTitleEntities(rawTitle.replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim());
  return title.slice(0, 120) || "临时 HTML 预览";
}

export function parseLightweightHtmlDocument(
  languageClass: string | undefined,
  content: string,
): LightweightHtmlDocument | null {
  const language = languageClass?.match(/(?:^|\s)language-([^\s]+)/i)?.[1]?.toLowerCase();
  if (language !== "html") return null;

  const html = content.trim();
  if (!html || utf8ByteLength(html) > MAX_LIGHTWEIGHT_HTML_BYTES) return null;

  const startsAsDocument = /^\s*<!doctype\s+html(?:\s[^>]*)?>/i.test(html)
    || /^\s*<html(?:\s[^>]*)?>/i.test(html);
  if (!startsAsDocument || !/<\/html\s*>\s*$/i.test(html)) return null;

  return {
    html,
    title: extractLightweightHtmlTitle(html),
  };
}
