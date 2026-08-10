"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, Code2, Copy, Download, Ellipsis, ImageDown, Monitor } from "lucide-react";

interface HtmlArtifactCardProps {
  html: string;
  title: string;
}

type PreviewMode = "ui" | "source";

interface PreviewBridgeMessage {
  source?: string;
  token?: string;
  type?: string;
  height?: number;
  dataUrl?: string;
  error?: string;
}

function safeFileName(title: string, extension: string): string {
  const base = title
    .replace(/\.html?$/i, "")
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-")
    .trim()
    .slice(0, 80) || "临时图表";
  return `${base}.${extension}`;
}

function triggerDownload(href: string, name: string): void {
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = name;
  anchor.rel = "noopener noreferrer";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function withPreviewBridge(html: string, token: string): string {
  const bridge = `
<script>
(() => {
  const token = ${JSON.stringify(token)};
  const reply = (payload) => parent.postMessage({ source: "puddingclaw-html-preview", token, ...payload }, "*");
  const reportHeight = () => {
    const body = document.body;
    const root = document.documentElement;
    reply({ type: "height", height: Math.max(body?.scrollHeight || 0, root?.scrollHeight || 0) });
  };
  addEventListener("load", reportHeight);
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(reportHeight).observe(document.documentElement);
  }
  addEventListener("message", async (event) => {
    if (event.source !== parent || event.data?.source !== "puddingclaw-html-host" || event.data?.token !== token) return;
    if (event.data.type !== "capture") return;
    try {
      const backgroundColor = getComputedStyle(document.body).backgroundColor || "#ffffff";
      let nativeDataUrl = "";

      if (window.echarts?.getInstanceByDom) {
        const chartRoots = document.querySelectorAll("[_echarts_instance_]");
        for (const root of chartRoots) {
          const instance = window.echarts.getInstanceByDom(root);
          if (!instance) continue;
          nativeDataUrl = instance.getDataURL({
            type: "png",
            pixelRatio: 2,
            backgroundColor,
          });
          if (nativeDataUrl) break;
        }
      }

      if (!nativeDataUrl && window.Chart?.getChart) {
        for (const canvas of document.querySelectorAll("canvas")) {
          const instance = window.Chart.getChart(canvas);
          if (!instance?.toBase64Image) continue;
          nativeDataUrl = instance.toBase64Image("image/png", 1);
          if (nativeDataUrl) break;
        }
      }

      if (!nativeDataUrl) {
        const canvases = Array.from(document.querySelectorAll("canvas"))
          .filter((canvas) => canvas.width > 0 && canvas.height > 0);
        if (canvases.length === 1) nativeDataUrl = canvases[0].toDataURL("image/png");
      }

      if (nativeDataUrl.startsWith("data:image/png;base64,")) {
        reply({ type: "capture-result", dataUrl: nativeDataUrl });
        return;
      }

      if (!window.html2canvas) {
        await new Promise((resolve, reject) => {
          const script = document.createElement("script");
          script.src = "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";
          script.onload = resolve;
          script.onerror = () => reject(new Error("截图组件加载失败"));
          document.head.appendChild(script);
        });
      }
      const canvas = await window.html2canvas(document.documentElement, {
        backgroundColor,
        logging: false,
        useCORS: true,
      });
      reply({ type: "capture-result", dataUrl: canvas.toDataURL("image/png") });
    } catch (error) {
      reply({ type: "capture-error", error: error instanceof Error ? error.message : "截图失败" });
    }
  });
})();
</script>`;

  return /<\/body\s*>/i.test(html)
    ? html.replace(/<\/body\s*>/i, `${bridge}</body>`)
    : html.replace(/<\/html\s*>/i, `${bridge}</html>`);
}

export default function HtmlArtifactCard({ html, title }: HtmlArtifactCardProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const reactId = useId();
  const bridgeToken = useMemo(() => `html-${reactId.replace(/[^a-z0-9_-]/gi, "")}`, [reactId]);
  const previewHtml = useMemo(() => withPreviewBridge(html, bridgeToken), [bridgeToken, html]);
  const [expanded, setExpanded] = useState(true);
  const [menuOpen, setMenuOpen] = useState(false);
  const [mode, setMode] = useState<PreviewMode>("ui");
  const [height, setHeight] = useState(520);
  const [iframeReady, setIframeReady] = useState(false);
  const [copied, setCopied] = useState(false);
  const [captureState, setCaptureState] = useState<"idle" | "capturing" | "error">("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!menuOpen) return;
    const closeMenu = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", closeMenu);
    return () => document.removeEventListener("mousedown", closeMenu);
  }, [menuOpen]);

  useEffect(() => {
    const handleMessage = (event: MessageEvent<PreviewBridgeMessage>) => {
      if (event.source !== iframeRef.current?.contentWindow) return;
      const message = event.data;
      if (message?.source !== "puddingclaw-html-preview" || message.token !== bridgeToken) return;

      if (message.type === "height" && Number.isFinite(message.height)) {
        setHeight(Math.min(760, Math.max(280, Number(message.height))));
      } else if (captureState === "capturing" && message.type === "capture-result" && message.dataUrl?.startsWith("data:image/png;base64,")) {
        if (message.dataUrl.length > 24 * 1024 * 1024) {
          setCaptureState("error");
          setError("图片过大，暂时无法下载");
          return;
        }
        triggerDownload(message.dataUrl, safeFileName(title, "png"));
        setCaptureState("idle");
      } else if (captureState === "capturing" && message.type === "capture-error") {
        setCaptureState("error");
        setError(message.error || "图片下载失败");
      }
    };
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [bridgeToken, captureState, title]);

  useEffect(() => {
    if (captureState !== "capturing" || mode !== "ui" || !expanded || !iframeReady) return;

    iframeRef.current?.contentWindow?.postMessage({
      source: "puddingclaw-html-host",
      token: bridgeToken,
      type: "capture",
    }, "*");
    const timeout = window.setTimeout(() => {
      setCaptureState("error");
      setError("生成图片超时，请稍后重试");
    }, 20_000);
    return () => window.clearTimeout(timeout);
  }, [bridgeToken, captureState, expanded, iframeReady, mode]);

  useEffect(() => {
    if (!expanded || mode !== "ui") setIframeReady(false);
  }, [expanded, mode]);

  const downloadHtml = () => {
    const url = URL.createObjectURL(new Blob([html], { type: "text/html;charset=utf-8" }));
    triggerDownload(url, safeFileName(title, "html"));
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
    setMenuOpen(false);
  };

  const copyHtml = async () => {
    try {
      await navigator.clipboard.writeText(html);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    } catch {
      setError("复制失败，请检查剪贴板权限");
    }
    setMenuOpen(false);
  };

  const downloadImage = () => {
    setCaptureState("capturing");
    setError("");
    setMode("ui");
    setExpanded(true);
    setMenuOpen(false);
  };

  const showSource = () => {
    setMode((current) => current === "source" ? "ui" : "source");
    setExpanded(true);
    setMenuOpen(false);
  };

  return (
    <section className="my-4 w-full rounded-[22px] border border-slate-200 bg-white shadow-sm">
      <header className={`flex h-[52px] items-center gap-2 rounded-t-[22px] bg-slate-50/95 px-3 ${expanded || error ? "" : "rounded-b-[22px]"}`}>
        <button
          type="button"
          onClick={() => setExpanded((current) => !current)}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-xl px-1.5 py-2 text-left text-[14px] font-semibold text-slate-900 transition hover:bg-slate-100"
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          <span>展示详情</span>
          {mode === "source" ? <span className="text-xs font-normal text-slate-400">· 源码</span> : null}
        </button>
        <div className="relative" ref={menuRef}>
          <button
            type="button"
            onClick={() => setMenuOpen((current) => !current)}
            className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-200/70 text-slate-600 transition hover:bg-slate-200 hover:text-slate-950"
            aria-label="HTML 操作菜单"
            aria-expanded={menuOpen}
          >
            <Ellipsis className="h-5 w-5" />
          </button>
          {menuOpen ? (
            <div className="absolute right-0 top-11 z-20 w-48 overflow-hidden rounded-xl border border-slate-200 bg-white p-1.5 text-[13px] text-slate-800 shadow-xl shadow-slate-950/15">
              <MenuButton icon={<Download className="h-4 w-4" />} label="下载到本地" onClick={downloadHtml} />
              <MenuButton
                icon={<ImageDown className="h-4 w-4" />}
                label={captureState === "capturing" ? "正在生成图片…" : "下载为图片"}
                onClick={downloadImage}
                disabled={captureState === "capturing"}
              />
              <MenuButton icon={copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />} label={copied ? "已复制" : "复制代码"} onClick={() => void copyHtml()} />
              <MenuButton
                icon={mode === "source" ? <Monitor className="h-4 w-4" /> : <Code2 className="h-4 w-4" />}
                label={mode === "source" ? "查看页面" : "查看代码"}
                onClick={showSource}
              />
            </div>
          ) : null}
        </div>
      </header>
      {expanded ? (
        <div className={`overflow-hidden border-t border-slate-100 bg-white ${error ? "" : "rounded-b-[22px]"}`}>
          {mode === "ui" ? (
            <iframe
              ref={iframeRef}
              title={title}
              sandbox="allow-scripts"
              srcDoc={previewHtml}
              referrerPolicy="no-referrer"
              onLoad={() => setIframeReady(true)}
              className="block w-full border-0 bg-white"
              style={{ height }}
            />
          ) : (
            <pre className="html-source-scroll !m-0 h-[min(520px,65vh)] overflow-auto overscroll-contain !rounded-none !bg-[#111827] !p-5 text-[12px] leading-5 text-slate-200 !shadow-none">
              <code>{html}</code>
            </pre>
          )}
        </div>
      ) : null}
      {error ? <p className="!m-0 rounded-b-[22px] border-t border-rose-100 bg-rose-50 px-4 py-2 text-xs text-rose-700">{error}</p> : null}
    </section>
  );
}

function MenuButton({
  icon,
  label,
  onClick,
  disabled = false,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left transition hover:bg-slate-100 disabled:cursor-wait disabled:opacity-50"
    >
      <span className="text-slate-500">{icon}</span>
      <span>{label}</span>
    </button>
  );
}
