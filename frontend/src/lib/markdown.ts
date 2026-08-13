import remarkGfm from "remark-gfm";
import { defaultUrlTransform } from "react-markdown";
import type { PluggableList } from "unified";

export const markdownRemarkPlugins: PluggableList = [[remarkGfm, { singleTilde: false }]];

function normalizeStrongDelimiterPairs(text: string, delimiter: "**" | "__"): string {
  let result = "";
  let cursor = 0;
  while (cursor < text.length) {
    const opening = text.indexOf(delimiter, cursor);
    if (opening < 0) return result + text.slice(cursor);
    const closing = text.indexOf(delimiter, opening + delimiter.length);
    if (closing < 0) return result + text.slice(cursor);
    const body = text.slice(opening + delimiter.length, closing);
    const normalizedBody = /\S[ \t]+$/.test(body) ? body.trimEnd() : body;
    result += text.slice(cursor, opening)
      + delimiter
      + normalizedBody
      + delimiter;
    cursor = closing + delimiter.length;
  }
  return result;
}

function normalizeLooseStrongText(text: string): string {
  // Common model-output typo: whitespace immediately before a closing
  // emphasis delimiter makes CommonMark treat the markers as literal text.
  return normalizeStrongDelimiterPairs(
    normalizeStrongDelimiterPairs(text, "**"),
    "__",
  );
}

function normalizeOutsideInlineCode(line: string): string {
  let result = "";
  let cursor = 0;
  while (cursor < line.length) {
    const opening = line.indexOf("`", cursor);
    if (opening < 0) return result + normalizeLooseStrongText(line.slice(cursor));
    result += normalizeLooseStrongText(line.slice(cursor, opening));
    let runLength = 1;
    while (line[opening + runLength] === "`") runLength += 1;
    const delimiter = "`".repeat(runLength);
    const closing = line.indexOf(delimiter, opening + runLength);
    if (closing < 0) return result + line.slice(opening);
    result += line.slice(opening, closing + runLength);
    cursor = closing + runLength;
  }
  return result;
}

/**
 * Repair whitespace before closing strong delimiters in model-authored prose.
 * Fenced and inline code stay byte-for-byte unchanged.
 */
export function normalizeLooseStrongMarkdown(content: string): string {
  let fence: { marker: string; length: number } | null = null;
  return content.split("\n").map((line) => {
    const fenceMatch = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (fenceMatch) {
      const marker = fenceMatch[1][0];
      const length = fenceMatch[1].length;
      if (!fence) fence = { marker, length };
      else if (fence.marker === marker && length >= fence.length) fence = null;
      return line;
    }
    return fence ? line : normalizeOutsideInlineCode(line);
  }).join("\n");
}

export function markdownUrlTransform(url: string): string {
  if (url.startsWith("file://")) return url;
  return defaultUrlTransform(url);
}
