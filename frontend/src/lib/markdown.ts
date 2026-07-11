import remarkGfm from "remark-gfm";
import { defaultUrlTransform } from "react-markdown";
import type { PluggableList } from "unified";

export const markdownRemarkPlugins: PluggableList = [[remarkGfm, { singleTilde: false }]];

export function markdownUrlTransform(url: string): string {
  if (url.startsWith("file://")) return url;
  return defaultUrlTransform(url);
}
