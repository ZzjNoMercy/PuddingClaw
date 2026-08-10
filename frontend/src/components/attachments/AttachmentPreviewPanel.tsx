"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Download,
  ImageOff,
  FileText,
  Minimize2,
  ZoomIn,
  X,
} from "lucide-react";
import { useApp } from "@/lib/store";
import {
  collectPreviewableImageAttachments,
  isPreviewableImageAttachment,
  isQrImageAttachment,
  resolveActiveArtifact,
} from "@/lib/imageAttachments";
import { markdownRemarkPlugins, markdownUrlTransform } from "@/lib/markdown";

const MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024;

export default function AttachmentPreviewPanel() {
  const {
    messages,
    sessionId,
    activeAttachmentPreview,
    openAttachmentPreview,
    closeAttachmentPreview,
    setInspectorOpen,
  } = useApp();
  const [actualSize, setActualSize] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [textPreview, setTextPreview] = useState("");
  const [textLoading, setTextLoading] = useState(false);
  const [textError, setTextError] = useState("");
  const [portalReady, setPortalReady] = useState(false);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const images = useMemo(
    () => collectPreviewableImageAttachments(messages),
    [messages],
  );
  const active = useMemo(
    () => resolveActiveArtifact(messages, activeAttachmentPreview, sessionId),
    [activeAttachmentPreview, messages, sessionId],
  );
  const activeImage = active && isPreviewableImageAttachment(active) ? active : null;
  const activeId = active?.id;
  const activeIndex = activeImage
    ? images.findIndex((attachment) => attachment.id === activeImage.id)
    : -1;

  useEffect(() => setPortalReady(true), []);

  useEffect(() => {
    if (activeAttachmentPreview?.sessionId === sessionId && !active) {
      closeAttachmentPreview();
    }
  }, [active, activeAttachmentPreview, closeAttachmentPreview, sessionId]);

  useEffect(() => {
    setActualSize(false);
    setLoadFailed(false);
    setTextPreview("");
    setTextLoading(false);
    setTextError("");
  }, [activeId]);

  useEffect(() => {
    if (!activeId || !portalReady) return;
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeAttachmentPreview();
      setInspectorOpen(false);
    };
    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("keydown", handleEscape);
      document.body.style.overflow = previousBodyOverflow;
    };
  }, [activeId, closeAttachmentPreview, portalReady, setInspectorOpen]);

  useEffect(() => {
    if (!active || activeImage) return;
    if (active.type !== "markdown" && active.type !== "text") return;
    if (!active.download_url) return;
    if ((active.size || 0) > MAX_TEXT_PREVIEW_BYTES) {
      setTextError("文件超过 2 MB，请下载后查看。");
      return;
    }
    const controller = new AbortController();
    setTextLoading(true);
    fetch(active.download_url, { cache: "no-store", signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then((content) => setTextPreview(content))
      .catch((error) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setTextError("文件预览加载失败，请下载后查看。");
      })
      .finally(() => {
        if (!controller.signal.aborted) setTextLoading(false);
      });
    return () => controller.abort();
  }, [active, activeImage]);

  const selectAt = (index: number) => {
    if (images.length === 0) return;
    const normalized = (index + images.length) % images.length;
    openAttachmentPreview(images[normalized].id);
  };

  useEffect(() => {
    const handleArrowKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target?.matches("input, textarea, select") ||
        target?.isContentEditable
      ) return;
      if (event.key === "ArrowLeft" && images.length > 1) {
        event.preventDefault();
        selectAt(activeIndex - 1);
      } else if (event.key === "ArrowRight" && images.length > 1) {
        event.preventDefault();
        selectAt(activeIndex + 1);
      }
    };
    window.addEventListener("keydown", handleArrowKey);
    return () => window.removeEventListener("keydown", handleArrowKey);
  });

  if (!active || !portalReady) return null;

  const close = () => {
    const attachmentId = active.id;
    closeAttachmentPreview();
    setInspectorOpen(false);
    requestAnimationFrame(() => {
      document
        .querySelector<HTMLElement>(`[data-attachment-id="${CSS.escape(attachmentId)}"]`)
        ?.focus();
    });
  };
  const backToCollection = () => {
    const attachmentId = active.id;
    closeAttachmentPreview();
    requestAnimationFrame(() => {
      document
        .querySelector<HTMLElement>(`[data-inspector-attachment-id="${CSS.escape(attachmentId)}"]`)
        ?.focus();
    });
  };
  const isQr = activeImage ? isQrImageAttachment(activeImage) : false;
  const dimensions = activeImage?.width && activeImage.height
    ? `${activeImage.width} × ${activeImage.height}`
    : active.type === "markdown"
      ? "Markdown"
      : active.type === "text"
        ? "文本"
        : "文件";

  const preview = (
    <section
      className="fixed inset-0 z-[150] flex h-dvh min-h-0 flex-col bg-white"
      role="dialog"
      aria-modal="true"
      aria-label="附件预览"
    >
      <header className="flex min-h-16 shrink-0 items-center gap-2 border-b border-slate-200 bg-white px-4 sm:px-6">
        <button
          type="button"
          onClick={backToCollection}
          className="inspector-transient-action flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border-0 text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 focus:bg-slate-100 focus:text-slate-900"
          aria-label="返回产物列表"
          title="返回产物列表"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-[13px] font-semibold text-slate-900">
            {active.name || "图片附件"}
          </h2>
          <p className="mt-0.5 text-[10px] text-slate-500">
            {dimensions}{isQr ? " · 二维码" : ""}
          </p>
        </div>
        {activeImage ? (
          <button
            type="button"
            onClick={() => setActualSize((value) => !value)}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 transition hover:bg-slate-100 hover:text-slate-900"
            aria-label={actualSize ? "适应窗口" : "查看原始尺寸"}
            title={actualSize ? "适应窗口" : "原始尺寸"}
          >
            {actualSize ? <Minimize2 className="h-4 w-4" /> : <ZoomIn className="h-4 w-4" />}
          </button>
        ) : null}
        {active.download_url ? (
          <a
            href={active.download_url}
            download={active.name || true}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 transition hover:bg-slate-100 hover:text-slate-900"
            aria-label="下载原图"
            title="下载原图"
          >
            <Download className="h-4 w-4" />
          </a>
        ) : null}
        <button
          ref={closeButtonRef}
          type="button"
          onClick={close}
          className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 transition hover:bg-slate-100 hover:text-slate-900 focus:outline-none focus:ring-2 focus:ring-[#002fa7]/30"
          aria-label="关闭图片预览"
          title="关闭"
        >
          <X className="h-4 w-4" />
        </button>
      </header>

      <div className={`relative min-h-0 flex-1 overflow-auto p-5 sm:p-7 ${activeImage ? "bg-[linear-gradient(45deg,#f4f6f9_25%,transparent_25%),linear-gradient(-45deg,#f4f6f9_25%,transparent_25%),linear-gradient(45deg,transparent_75%,#f4f6f9_75%),linear-gradient(-45deg,transparent_75%,#f4f6f9_75%)] bg-[length:20px_20px] bg-[position:0_0,0_10px,10px_-10px,-10px_0px]" : "bg-white"}`}>
        <div className={`flex min-w-full items-center justify-center ${activeImage ? "h-full" : "min-h-full"}`}>
          {activeImage && loadFailed ? (
            <div className="rounded-2xl border border-slate-200 bg-white px-6 py-8 text-center shadow-sm">
              <ImageOff className="mx-auto h-8 w-8 text-slate-400" />
              <p className="mt-3 text-[12px] font-medium text-slate-700">图片预览加载失败</p>
              {active.download_url ? (
                <a href={active.download_url} download className="mt-2 inline-block text-[12px] text-[#002fa7] hover:underline">
                  下载原文件
                </a>
              ) : null}
            </div>
          ) : activeImage ? (
            <div className={`${isQr ? "rounded-2xl bg-white p-5 shadow-sm" : ""} ${actualSize ? "shrink-0" : "flex h-full w-full items-center justify-center"}`}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={activeImage.preview_url}
                alt={active.name || "图片附件"}
                onError={() => setLoadFailed(true)}
                className={actualSize
                  ? "block max-w-none"
                  : "block max-h-full max-w-full object-contain"
                }
              />
            </div>
          ) : textLoading ? (
            <div className="text-[12px] text-slate-400">正在加载预览…</div>
          ) : textError ? (
            <div className="rounded-2xl border border-slate-200 bg-slate-50 px-6 py-8 text-center">
              <FileText className="mx-auto h-8 w-8 text-slate-400" />
              <p className="mt-3 text-[12px] text-slate-600">{textError}</p>
            </div>
          ) : active.type === "markdown" ? (
            <article className="markdown-content min-h-full w-full max-w-none self-start text-[14px] leading-relaxed text-slate-800">
              <ReactMarkdown
                remarkPlugins={markdownRemarkPlugins}
                urlTransform={markdownUrlTransform}
                components={{
                  img: ({ alt }) => (
                    <span className="my-2 inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[12px] text-slate-500">
                      <ImageOff className="h-4 w-4" />
                      {alt || "Markdown 图片"}（未自动加载）
                    </span>
                  ),
                }}
              >
                {textPreview}
              </ReactMarkdown>
            </article>
          ) : active.type === "text" ? (
            <pre className="min-h-full w-full self-start whitespace-pre-wrap break-words rounded-xl bg-slate-50 p-4 font-mono text-[12px] leading-relaxed text-slate-700">
              {textPreview}
            </pre>
          ) : (
            <div className="rounded-2xl border border-slate-200 bg-slate-50 px-6 py-8 text-center">
              <FileText className="mx-auto h-8 w-8 text-slate-400" />
              <p className="mt-3 text-[12px] text-slate-600">此文件类型暂不支持内嵌预览</p>
            </div>
          )}
        </div>

        {activeImage && images.length > 1 ? (
          <>
            <button
              type="button"
              onClick={() => selectAt(activeIndex - 1)}
              className="absolute left-2 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-slate-200 bg-white/95 text-slate-700 shadow-md transition hover:bg-white"
              aria-label="上一张图片"
            >
              <ChevronLeft className="h-5 w-5" />
            </button>
            <button
              type="button"
              onClick={() => selectAt(activeIndex + 1)}
              className="absolute right-2 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-full border border-slate-200 bg-white/95 text-slate-700 shadow-md transition hover:bg-white"
              aria-label="下一张图片"
            >
              <ChevronRight className="h-5 w-5" />
            </button>
          </>
        ) : null}
      </div>

      {activeImage && images.length > 1 ? (
        <footer className="flex shrink-0 gap-2 overflow-x-auto border-t border-slate-200 bg-white p-3">
          {images.map((image) => (
            <button
              key={image.id}
              type="button"
              onClick={() => openAttachmentPreview(image.id)}
              className={`h-14 w-14 shrink-0 overflow-hidden rounded-xl border-2 bg-slate-50 p-0.5 transition ${
                image.id === active.id ? "border-[#002fa7]" : "border-transparent hover:border-slate-300"
              }`}
              aria-label={`查看 ${image.name || "图片"}`}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={image.preview_url} alt="" className="h-full w-full rounded-lg bg-white object-contain" />
            </button>
          ))}
        </footer>
      ) : null}
    </section>
  );
  return createPortal(preview, document.body);
}
