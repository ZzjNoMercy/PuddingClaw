import remarkGfm from "remark-gfm";
import type { PluggableList } from "unified";

export const markdownRemarkPlugins: PluggableList = [[remarkGfm, { singleTilde: false }]];
