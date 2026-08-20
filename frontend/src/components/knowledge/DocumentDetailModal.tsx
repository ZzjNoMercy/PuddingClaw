"use client";

import { useEffect, useState } from "react";
import { Database, ExternalLink, Loader2, X } from "lucide-react";

import FeishuSourceCard, { feishuMetaOf } from "@/components/knowledge/FeishuSourceCard";
import {
  previewKnowledgeFile,
  publishKnowledgeDocumentVector,
  type KnowledgeDocument,
  type KnowledgeFilePreview,
} from "@/lib/api";

export type SourceKind = "feishu" | "local" | "web";

export const KIND_LABEL: Record<SourceKind, string> = { feishu: "飞书", local: "本地上传", web: "网页收藏" };

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function fullTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function docType(doc: KnowledgeDocument): { glyph: string; label: string } {
  const mime = (doc.mime_type || "").toLowerCase();
  const name = (doc.title || doc.source_path || "").toLowerCase();
  if (mime.includes("pdf") || name.endsWith(".pdf")) return { glyph: "PDF", label: "PDF" };
  if (mime.includes("word") || /\.docx?$/.test(name)) return { glyph: "DOC", label: "Docx" };
  if (mime.includes("spreadsheet") || /\.(xlsx?|xlsm)$/.test(name)) return { glyph: "XLS", label: "Excel" };
  if (mime.includes("csv") || name.endsWith(".csv")) return { glyph: "CSV", label: "CSV" };
  if (mime.includes("presentation") || /\.pptx?$/.test(name)) return { glyph: "PPT", label: "PPT" };
  if (mime.startsWith("image/") || /\.(png|jpe?g|gif|webp|svg)$/.test(name)) return { glyph: "IMG", label: "图片" };
  if (mime.includes("html") || doc.source_type === "read_later" || /\.html?$/.test(name)) return { glyph: "网页", label: "网页" };
  if (mime.includes("markdown") || /\.(md|markdown)$/.test(name)) return { glyph: "MD", label: "Markdown" };
  const extension = name.includes(".") ? name.split(".").pop() || "" : "";
  const label = extension && extension.length <= 5 ? extension.toUpperCase() : "文件";
  return { glyph: label.slice(0, 4), label };
}

export function statusView(doc: KnowledgeDocument): { label: string; className: string } {
  const status = doc.status;
  if (status === "processing" || status === "queued" || status === "parsing") return { label: "处理中", className: "bg-amber-50 text-amber-600" };
  if (status === "error" || status === "failed") return { label: "失败", className: "bg-red-50 text-red-600" };
  if (status === "deleted") return { label: "已删除", className: "bg-gray-100 text-gray-400" };
  if (status === "ready" || status === "indexed") {
    const targets = doc.publish_targets || [];
    if (!targets.includes("vector") && !targets.includes("local_vector")) return { label: "已入库", className: "bg-gray-100 text-gray-500" };
    const stamp = (doc.metadata?.vector_index || null) as { refreshed?: boolean } | null;
    if (stamp?.refreshed) return { label: "已索引", className: "bg-emerald-50 text-emerald-600" };
    return { label: "待索引", className: "bg-amber-50 text-amber-600" };
  }
  return { label: status || "未知", className: "bg-gray-100 text-gray-500" };
}

function vectorIndexLabel(doc: KnowledgeDocument): string {
  const targets = doc.publish_targets || [];
  if (!targets.includes("vector") && !targets.includes("local_vector")) return "不入向量库";
  const stamp = (doc.metadata?.vector_index || null) as { refreshed?: boolean; generated_at?: string } | null;
  if (stamp?.refreshed) return stamp.generated_at ? `已索引 · ${fullTime(stamp.generated_at)}` : "已索引";
  return "待索引";
}

export function KindLogo({ kind }: { kind: SourceKind }) {
  const src = kind === "feishu" ? "/brands/feishu-logo.svg" : kind === "local" ? "/brands/local-upload.svg" : "/brands/web-capture.svg";
  const alt = kind === "feishu" ? "飞书" : kind === "local" ? "本地上传" : "网页收藏";
  return <img src={src} alt={alt} className="h-5 w-5 shrink-0 rounded-md border border-black/[0.06] object-cover" />;
}

/** 统一的文档详情弹窗：资源库列表、来源面板列表点击后都走这里。 */
export default function DocumentDetailModal({ doc, kind, sourceName, onClose }: {
  doc: KnowledgeDocument;
  kind: SourceKind;
  sourceName: string;
  onClose: () => void;
}) {
  const [preview, setPreview] = useState<KnowledgeFilePreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [note, setNote] = useState("");

  const type = docType(doc);
  const status = statusView(doc);
  const feishuMeta = feishuMetaOf(doc);
  const location = (doc.virtual_path || doc.source_path || "").replace(/^\/+|\/+$/g, "").split("/").filter(Boolean).join(" / ");

  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setPreviewError("");
    setPreviewLoading(true);
    previewKnowledgeFile(doc.virtual_path || doc.source_path)
      .then((result) => { if (!cancelled) setPreview(result); })
      .catch((error) => { if (!cancelled) setPreviewError(messageOf(error)); })
      .finally(() => { if (!cancelled) setPreviewLoading(false); });
    return () => { cancelled = true; };
  }, [doc]);

  async function publishVector() {
    if (actionBusy) return;
    setActionBusy(true);
    setNote("");
    try {
      await publishKnowledgeDocumentVector(doc.id);
      setNote("已排队重建该文档的向量索引，进度可在任务中心查看。");
    } catch (error) {
      setNote(messageOf(error));
    } finally {
      setActionBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[110] grid place-items-center bg-slate-950/30 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-label="文档详情" onClick={onClose}>
      <section className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-[24px] bg-white shadow-2xl ring-1 ring-black/[0.08]" onClick={(event) => event.stopPropagation()}>
        <div className="flex items-start justify-between gap-4 border-b border-black/[0.06] px-6 py-5">
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-[#002fa7]/[0.06] text-[11px] font-bold text-[#002fa7]">{type.glyph}</span>
            <div className="min-w-0">
              <h2 className="truncate text-base font-semibold text-gray-950">{doc.title}</h2>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-gray-400">
                <span>{type.label} · {formatSize(doc.size_bytes)}</span>
                <span className={`inline-flex rounded-full px-2 py-0.5 font-semibold ${status.className}`}>{status.label}</span>
              </div>
            </div>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭" className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-gray-50 text-gray-500 hover:bg-gray-100"><X className="h-4 w-4" /></button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {feishuMeta ? (
            <div className="mb-5">
              <FeishuSourceCard meta={feishuMeta} variant="card" />
            </div>
          ) : null}
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-xs">
            {feishuMeta ? null : (
              <div><dt className="text-gray-400">来源</dt><dd className="mt-1 flex items-center gap-2 font-medium text-gray-700"><KindLogo kind={kind} />{sourceName}</dd></div>
            )}
            <div><dt className="text-gray-400">更新时间</dt><dd className="mt-1 font-medium text-gray-700">{fullTime(doc.updated_at)}</dd></div>
            <div className="col-span-2"><dt className="text-gray-400">所在位置</dt><dd className="mt-1 break-all font-medium text-gray-700">{location || "—"}</dd></div>
            <div><dt className="text-gray-400">创建时间</dt><dd className="mt-1 font-medium text-gray-700">{fullTime(doc.created_at)}</dd></div>
            <div><dt className="text-gray-400">发布目标</dt><dd className="mt-1 font-medium text-gray-700">{(doc.publish_targets || []).join("、") || "—"}</dd></div>
            <div><dt className="text-gray-400">向量索引</dt><dd className="mt-1 font-medium text-gray-700">{vectorIndexLabel(doc)}</dd></div>
            {!feishuMeta && doc.origin_url ? (
              <div className="col-span-2"><dt className="text-gray-400">原文链接</dt><dd className="mt-1"><a href={doc.origin_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 break-all font-medium text-[#002fa7] hover:underline">{doc.origin_url}<ExternalLink className="h-3 w-3 shrink-0" /></a></dd></div>
            ) : null}
          </dl>
          <div className="mt-5 border-t border-black/[0.06] pt-4">
            <div className="text-xs font-semibold text-gray-700">内容预览</div>
            {previewLoading ? <div className="grid place-items-center py-10"><Loader2 className="h-5 w-5 animate-spin text-[#002fa7]" /></div>
              : previewError ? <div className="mt-2 rounded-xl bg-gray-50 px-4 py-3 text-xs text-gray-500">暂不支持预览该文件（{previewError}）</div>
                : preview ? (
                  <>
                    <pre className="mt-2 max-h-[46vh] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-gray-50 p-4 font-mono text-[11px] leading-5 text-gray-700">{preview.content || "（空文档）"}</pre>
                    {preview.truncated ? <p className="mt-2 text-[11px] text-gray-400">内容较长，仅显示前一部分。</p> : null}
                  </>
                ) : null}
          </div>
        </div>
        <div className="flex items-center gap-2 border-t border-black/[0.06] px-6 py-4">
          <button type="button" disabled={actionBusy || doc.status !== "ready"} onClick={() => void publishVector()} className="inline-flex h-10 items-center gap-2 rounded-xl bg-[#002fa7] px-4 text-xs font-semibold text-white disabled:opacity-40">
            {actionBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Database className="h-3.5 w-3.5" />}重建向量索引
          </button>
          {note ? <span className="text-[11px] text-gray-500">{note}</span> : <span className="text-[11px] text-gray-400">只重建这一篇，不影响其他文档。</span>}
        </div>
      </section>
    </div>
  );
}
