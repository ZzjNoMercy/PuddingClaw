/**
 * Monaco Editor 本地加载配置。
 * 避免从 jsDelivr CDN 加载（国内网络常超时导致 "Loading..." 卡死）。
 * 静态资源位于 public/monaco-editor/min/vs（从 node_modules 复制）。
 */
import { loader } from "@monaco-editor/react";

loader.config({
  paths: {
    vs: "/monaco-editor/min/vs",
  },
});
