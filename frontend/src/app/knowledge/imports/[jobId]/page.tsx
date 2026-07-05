"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  AlertCircle,
  ArrowLeft,
  CheckCircle2,
  Clock3,
  Database,
  FileImage,
  FileText,
  Layers3,
  Loader2,
  RefreshCw,
  Search,
  X,
} from "lucide-react";

import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import { useApp } from "@/lib/store";
import {
  getKnowledgeImportJob,
  publishKnowledgeImportJobVector,
  previewKnowledgeFile,
  searchKnowledge,
  type KnowledgeDocument,
  type KnowledgeFilePreview,
  type KnowledgeImportEvent,
  type KnowledgeImportJob,
  type KnowledgeSearchHit,
  type KnowledgeSearchResult,
} from "@/lib/api";

type MainTab = "overview" | "result" | "chunks" | "search";
type ResultTab = "markdown" | "source" | "images" | "structured";
type SearchPoolTab = "text_vector" | "bm25" | "image_vector";
type ImageContext = {
  heading?: string;
  caption?: string;
  snippet?: string;
  lineNumber?: number;
};
type ParsedImageAsset = { label: string; src: string; context?: ImageContext };
type ImagePreview = ParsedImageAsset | null;
type ChunkPreview = {
  index: number;
  title: string;
  level: string;
  preview: string;
  nodeId?: string;
  headerPath?: string;
  virtualPath?: string;
  linkedImages?: string[];
};

const CHUNKS_PER_PAGE = 10;
const EVENTS_PER_PAGE = 10;

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error || "未知错误");
}

function jobStatusLabel(status: string): string {
  if (status === "queued") return "排队中";
  if (status === "running") return "处理中";
  if (status === "succeeded") return "可用";
  if (status === "failed") return "失败";
  if (status === "cancelled") return "已取消";
  return status;
}

function jobStatusClass(status: string): string {
  if (status === "succeeded") return "bg-emerald-50 text-emerald-700 ring-emerald-500/10";
  if (status === "failed") return "bg-red-50 text-red-600 ring-red-500/10";
  if (status === "running") return "bg-[#002fa7]/10 text-[#002fa7] ring-[#002fa7]/10";
  return "bg-gray-100 text-gray-600 ring-black/[0.04]";
}

function isImportJobActive(job: KnowledgeImportJob | null): boolean {
  return job?.status === "queued" || job?.status === "running";
}

function isVectorPublishJob(job: KnowledgeImportJob | null): boolean {
  return job?.metadata?.kind === "vector_publish" || job?.file_type === "vector";
}

function sourceJobId(job: KnowledgeImportJob | null): string {
  const value = job?.metadata?.source_job_id;
  return typeof value === "string" ? value : "";
}

function vectorJobStatus(job: KnowledgeImportJob | null): string {
  const value = job?.metadata?.vector_job_status;
  return typeof value === "string" ? value : "";
}

function vectorJobError(job: KnowledgeImportJob | null): string {
  const value = job?.metadata?.vector_error_message;
  return typeof value === "string" ? value : "";
}

function metadataNumber(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function vectorProgress(job: KnowledgeImportJob | null): {
  textDone: number;
  textTotal: number;
  imageDone: number;
  imageTotal: number;
} | null {
  const value = job?.metadata?.vector_progress;
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const textDone = metadataNumber(record.text_done);
  const textTotal = metadataNumber(record.text_total);
  const imageDone = metadataNumber(record.image_done);
  const imageTotal = metadataNumber(record.image_total);
  if (textTotal <= 0 && imageTotal <= 0) return null;
  return { textDone, textTotal, imageDone, imageTotal };
}

function hasActiveVectorJob(job: KnowledgeImportJob | null): boolean {
  const status = vectorJobStatus(job);
  return status === "queued" || status === "running";
}

function vectorStatusMessage(vectorIndex: Record<string, unknown> | null): { tone: "muted" | "error"; text: string } | null {
  if (!vectorIndex) return { tone: "muted", text: "还没有导入向量。" };
  if (vectorIndex.refreshed) return null;
  const reason = String(vectorIndex.error || vectorIndex.reason || "").trim();
  if (!reason || reason === "vector publish not requested") {
    return { tone: "muted", text: "还没有导入向量。" };
  }
  if (reason === "knowledge directory has no markdown") {
    return { tone: "error", text: "知识库里还没有可导入的 Markdown。" };
  }
  return { tone: "error", text: reason };
}

function mainTabLabel(tab: MainTab): string {
  if (tab === "overview") return "概览";
  if (tab === "result") return "解析结果";
  if (tab === "chunks") return "切片预览";
  return "检索测试";
}

function metadataArray(document: KnowledgeDocument | null | undefined, key: string): unknown[] {
  const value = document?.metadata?.[key];
  return Array.isArray(value) ? value : [];
}

function metadataString(document: KnowledgeDocument | null | undefined, key: string): string {
  const value = document?.metadata?.[key];
  return typeof value === "string" ? value : "";
}

function rawKnowledgeFileUrl(virtualPath: string): string {
  if (!virtualPath) return "";
  if (virtualPath.startsWith("/api/knowledge/file/raw?")) return virtualPath;
  if (virtualPath.startsWith("/knowledge/")) {
    return `/api/knowledge/file/raw?virtual_path=${encodeURIComponent(virtualPath)}`;
  }
  return virtualPath;
}

function normalizeMarkdownAssetUrl(value: string): string {
  return value.replace(/\\/g, "/").trim().replace(/^['"]|['"]$/g, "").replace(/^\.?\//, "");
}

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function retrievalChannelLabel(value?: string): string {
  if (value === "text_vector") return "文本语义";
  if (value === "bm25") return "关键词";
  if (value === "image_vector") return "图片向量";
  return value || "检索";
}

function searchPoolLabel(value: SearchPoolTab): string {
  if (value === "text_vector") return "文本语义";
  if (value === "bm25") return "关键词";
  return "图片向量";
}

function formatRelevanceScore(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

function relevanceScoreValue(hit: KnowledgeSearchHit): number | null {
  if (typeof hit.normalized_score === "number" && Number.isFinite(hit.normalized_score)) {
    return hit.normalized_score;
  }
  if (typeof hit.score !== "number" || !Number.isFinite(hit.score)) return null;
  if (hit.score >= 0 && hit.score <= 0.05) {
    return Math.min(1, hit.score * 61);
  }
  return hit.score;
}

function retrievalAssessment(result: KnowledgeSearchResult): string {
  const imageFinalCount = result.hits.filter((hit) => hit.modality === "image").length;
  const imagePoolCount = result.candidate_pools?.image_vector?.length || 0;
  const fusion = result.fusion || {};
  const textWeight = Math.round((fusion.text_vector_weight ?? 0) * 100);
  const bm25Weight = Math.round((fusion.bm25_weight ?? 0) * 100);
  const imageWeight = Math.round((fusion.image_vector_weight ?? 0) * 100);
  const textGroupWeight = Math.round((fusion.text_group_weight ?? Math.max(0, 1 - (fusion.image_vector_weight ?? 0))) * 100);
  if (imagePoolCount > 0 && imageFinalCount === 0) {
    return `图片有候选但没进最终结果。当前配置是：文本内部语义 ${textWeight}% / 关键词 ${bm25Weight}%，最终图文融合文本整体 ${textGroupWeight}% / 图片 ${imageWeight}%，Top-K ${result.top_k}；如果希望图片更容易进最终结果，可以提高图片权重或开启重排。`;
  }
  if (fusion.rerank_enabled) {
    return `已开启重排，最终结果由候选池进入 rerank 后选出 Top-${result.top_k}。`;
  }
  return `当前配置：文本内部语义 ${textWeight}% / 关键词 ${bm25Weight}%，最终图文融合文本整体 ${textGroupWeight}% / 图片 ${imageWeight}%，未开启重排。`;
}

function plainContextLine(line: string): string {
  return compactText(
    line
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(/<img\b[^>]*>/gi, "")
      .replace(/^\s{0,3}#{1,6}\s+/, "")
  );
}

function nearbyContextLines(lines: string[], start: number, step: number, limit = 2): string[] {
  const items: string[] = [];
  for (let index = start; index >= 0 && index < lines.length && items.length < limit; index += step) {
    const text = plainContextLine(lines[index]);
    if (text) items.push(text);
  }
  return step < 0 ? items.reverse() : items;
}

function extractImageContextsFromMarkdown(content: string): Record<string, ImageContext> {
  const contexts: Record<string, ImageContext> = {};
  const lines = content.split(/\r?\n/);
  let heading = "";
  const imagePattern = /!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
  const htmlImagePattern = /<img\b[^>]*?\bsrc=["'](.*?)["'][^>]*>/gi;

  lines.forEach((line, index) => {
    const headingMatch = line.match(/^\s{0,3}#{1,6}\s+(.+?)\s*$/);
    if (headingMatch?.[1]) heading = plainContextLine(headingMatch[1]);

    const before = nearbyContextLines(lines, index - 1, -1);
    const after = nearbyContextLines(lines, index + 1, 1);
    const add = (url: string, caption = "") => {
      const normalized = normalizeMarkdownAssetUrl(url);
      if (!normalized) return;
      const snippet = [heading, ...before, caption, ...after].filter(Boolean).join(" / ").slice(0, 800);
      const context: ImageContext = {
        heading,
        caption,
        snippet,
        lineNumber: index + 1,
      };
      contexts[normalized] = context;
      contexts[rawKnowledgeFileUrl(normalized)] = context;
      contexts[normalized.split("/").pop() || normalized] = context;
    };

    let markdownMatch = imagePattern.exec(line);
    while (markdownMatch) {
      add(markdownMatch[2] || "", markdownMatch[1] || "");
      markdownMatch = imagePattern.exec(line);
    }
    let htmlMatch = htmlImagePattern.exec(line);
    while (htmlMatch) {
      add(htmlMatch[1] || "");
      htmlMatch = htmlImagePattern.exec(line);
    }
  });

  return contexts;
}

function parseAsset(asset: unknown, index: number, markdownContexts: Record<string, ImageContext> = {}): ParsedImageAsset {
  if (typeof asset === "string") {
    const src = rawKnowledgeFileUrl(asset);
    return { label: asset, src, context: markdownContexts[asset] || markdownContexts[src] };
  }
  if (!asset || typeof asset !== "object") {
    return { label: `图片 ${index + 1}`, src: "" };
  }
  const record = asset as Record<string, unknown>;
  const virtualPath = typeof record.virtual_path === "string" ? record.virtual_path : "";
  const relativePath = typeof record.relative_path === "string" ? record.relative_path : "";
  const name = typeof record.name === "string" ? record.name : "";
  const path = typeof record.path === "string" ? record.path : "";
  const browserPath = virtualPath || (path.startsWith("/knowledge/") ? path : "");
  const contextRecord = record.context && typeof record.context === "object" ? (record.context as Record<string, unknown>) : null;
  const before = Array.isArray(contextRecord?.before) ? contextRecord?.before.filter((item): item is string => typeof item === "string") : [];
  const after = Array.isArray(contextRecord?.after) ? contextRecord?.after.filter((item): item is string => typeof item === "string") : [];
  const metadataContext: ImageContext | undefined = contextRecord
    ? {
        heading: typeof contextRecord.heading === "string" ? contextRecord.heading : undefined,
        caption: typeof contextRecord.caption === "string" ? contextRecord.caption : undefined,
        snippet:
          typeof contextRecord.snippet === "string"
            ? contextRecord.snippet
            : [...before, ...after].filter(Boolean).join(" / ") || undefined,
        lineNumber: typeof contextRecord.line_number === "number" ? contextRecord.line_number : undefined,
      }
    : undefined;
  const src = rawKnowledgeFileUrl(browserPath);
  const fallbackContext =
    markdownContexts[browserPath] ||
    markdownContexts[src] ||
    markdownContexts[relativePath] ||
    markdownContexts[name] ||
    markdownContexts[path.split("/").pop() || ""];
  return {
    label: name || relativePath || virtualPath || path || `图片 ${index + 1}`,
    src,
    context: metadataContext || fallbackContext,
  };
}

function repairUtf8Mojibake(text: string): string {
  if (!text) return text;
  const mojibakeHits = (text.match(/[\u0080-\u00ff]/g) || []).length;
  const chineseHits = (text.match(/[\u4e00-\u9fff]/g) || []).length;
  if (mojibakeHits < 8 || chineseHits > mojibakeHits) return text;
  try {
    const bytes = Uint8Array.from(Array.from(text), (char) => char.charCodeAt(0) & 0xff);
    const repaired = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    const repairedChineseHits = (repaired.match(/[\u4e00-\u9fff]/g) || []).length;
    return repairedChineseHits > chineseHits ? repaired : text;
  } catch {
    return text;
  }
}

function MarkdownPreview({ content, truncated }: { content: string; truncated?: boolean }) {
  const imagePattern = /^\s*!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)\s*$/;
  const lines = content.split(/\r?\n/);
  return (
    <div className="max-h-[620px] overflow-auto bg-white px-6 py-6 text-left text-sm leading-7 text-gray-800">
      {lines.map((line, index) => {
        const match = line.match(imagePattern);
        if (match?.[2]) {
          const alt = match[1] || `图片 ${index + 1}`;
          const src = rawKnowledgeFileUrl(match[2]);
          return (
            <figure key={index} className="my-4 overflow-hidden rounded-2xl border border-black/[0.06] bg-black/[0.02] p-3">
              <img
                src={src}
                alt={alt}
                className="max-h-[420px] w-full rounded-xl object-contain"
                loading="lazy"
              />
              {alt ? <figcaption className="mt-2 break-all text-xs text-gray-400">{alt}</figcaption> : null}
            </figure>
          );
        }
        return (
          <div key={index} className="min-h-[1.75rem] whitespace-pre-wrap break-words font-mono">
            {line || "\u00A0"}
          </div>
        );
      })}
      {truncated ? <div className="mt-4 text-gray-400">……文件较大，只显示前面一部分。</div> : null}
    </div>
  );
}

function nestedNumber(value: unknown, key: string): number | null {
  if (!value || typeof value !== "object") return null;
  const next = (value as Record<string, unknown>)[key];
  return typeof next === "number" ? next : null;
}

function buildChunks(content: string): ChunkPreview[] {
  const lines = content.split(/\r?\n/);
  const chunks: ChunkPreview[] = [];
  let currentTitle = "文档开头";
  let currentLevel = "正文";
  let buffer: string[] = [];

  const push = () => {
    const preview = buffer.join("\n").trim();
    if (!preview && chunks.length > 0) return;
    chunks.push({
      index: chunks.length + 1,
      title: currentTitle,
      level: currentLevel,
      preview: preview.slice(0, 260),
    });
    buffer = [];
  };

  lines.forEach((line) => {
    const heading = /^(#{1,6})\s+(.+)$/.exec(line.trim());
    if (heading) {
      push();
      currentLevel = `H${heading[1].length}`;
      currentTitle = heading[2].trim();
      return;
    }
    buffer.push(line);
  });
  push();

  return chunks.filter((chunk) => chunk.title || chunk.preview).slice(0, 80);
}

function parseIndexedChunks(vectorIndex: Record<string, unknown> | null): ChunkPreview[] {
  const multimodal = vectorIndex?.multimodal;
  if (!multimodal || typeof multimodal !== "object") return [];
  const rawChunks = (multimodal as Record<string, unknown>).chunks;
  if (!Array.isArray(rawChunks)) return [];
  return rawChunks
    .map((chunk, fallbackIndex): ChunkPreview | null => {
      if (!chunk || typeof chunk !== "object") return null;
      const record = chunk as Record<string, unknown>;
      const linkedImages = Array.isArray(record.linked_images)
        ? record.linked_images.filter((item): item is string => typeof item === "string")
        : [];
      return {
        index: typeof record.index === "number" ? record.index : fallbackIndex + 1,
        title: typeof record.title === "string" && record.title.trim() ? record.title : "文档片段",
        level: typeof record.level === "string" ? record.level : "正文",
        preview: typeof record.preview === "string" ? record.preview : "",
        nodeId: typeof record.node_id === "string" ? record.node_id : undefined,
        headerPath: typeof record.header_path === "string" ? record.header_path : undefined,
        virtualPath: typeof record.virtual_path === "string" ? record.virtual_path : undefined,
        linkedImages,
      };
    })
    .filter((chunk): chunk is ChunkPreview => Boolean(chunk))
    .slice(0, 120);
}

function parseDocumentLlamaIndexChunks(document: KnowledgeDocument | null): ChunkPreview[] {
  const chunkManifest = document?.metadata?.llamaindex_chunks;
  if (!chunkManifest || typeof chunkManifest !== "object") return [];
  const rawChunks = (chunkManifest as Record<string, unknown>).chunks;
  if (!Array.isArray(rawChunks)) return [];
  return rawChunks
    .map((chunk, fallbackIndex): ChunkPreview | null => {
      if (!chunk || typeof chunk !== "object") return null;
      const record = chunk as Record<string, unknown>;
      const linkedImages = Array.isArray(record.linked_images)
        ? record.linked_images.filter((item): item is string => typeof item === "string")
        : [];
      return {
        index: typeof record.index === "number" ? record.index : fallbackIndex + 1,
        title: typeof record.title === "string" && record.title.trim() ? record.title : "文档片段",
        level: typeof record.level === "string" ? record.level : "正文",
        preview: typeof record.preview === "string" ? record.preview : "",
        nodeId: typeof record.node_id === "string" ? record.node_id : undefined,
        headerPath: typeof record.header_path === "string" ? record.header_path : undefined,
        virtualPath: typeof record.virtual_path === "string" ? record.virtual_path : undefined,
        linkedImages,
      };
    })
    .filter((chunk): chunk is ChunkPreview => Boolean(chunk))
    .slice(0, 120);
}

function InfoRow({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="grid grid-cols-[90px_1fr] gap-3 text-sm">
      <span className="text-gray-400">{label}</span>
      <span className="min-w-0 break-words text-right font-medium text-gray-700" title={title || value}>
        {value}
      </span>
    </div>
  );
}

function EventList({ events }: { events: KnowledgeImportEvent[] }) {
  const [emptyHintReady, setEmptyHintReady] = useState(false);
  const [eventPage, setEventPage] = useState(1);
  const eventPageCount = Math.max(1, Math.ceil(events.length / EVENTS_PER_PAGE));
  const currentEventPage = Math.min(eventPage, eventPageCount);
  const pagedEvents = useMemo(
    () => events.slice((currentEventPage - 1) * EVENTS_PER_PAGE, currentEventPage * EVENTS_PER_PAGE),
    [events, currentEventPage]
  );
  const eventStart = events.length > 0 ? (currentEventPage - 1) * EVENTS_PER_PAGE + 1 : 0;
  const eventEnd = Math.min(currentEventPage * EVENTS_PER_PAGE, events.length);

  useEffect(() => {
    setEventPage(1);
  }, [events.length]);

  useEffect(() => {
    setEmptyHintReady(true);
  }, []);

  if (events.length === 0) {
    return (
      <div className="rounded-2xl bg-black/[0.025] px-4 py-5 text-sm text-gray-400" suppressHydrationWarning>
        {emptyHintReady ? "暂无处理记录。" : ""}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 text-sm text-gray-400">
        <span>
          {eventStart}-{eventEnd} / {events.length}
        </span>
        {eventPageCount > 1 ? <span>第 {currentEventPage} / {eventPageCount} 页</span> : null}
      </div>
      <div className="space-y-2">
        {pagedEvents.map((event) => (
          <div key={event.id} className="flex items-start gap-3 rounded-2xl bg-black/[0.025] px-4 py-3">
            <span
              className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
                event.level === "error" ? "bg-red-500" : event.level === "warning" ? "bg-amber-500" : "bg-[#002fa7]"
              }`}
            />
            <div className="min-w-0 flex-1">
              <p className={`text-sm ${event.level === "error" ? "text-red-600" : "text-gray-700"}`}>{event.message}</p>
              <p className="mt-1 text-xs text-gray-400">{formatTime(event.created_at)}</p>
            </div>
          </div>
        ))}
      </div>
      {eventPageCount > 1 ? (
        <div className="flex flex-col gap-3 rounded-2xl border border-black/[0.06] bg-white px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <span className="text-sm text-gray-400">
            第 {currentEventPage} / {eventPageCount} 页
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setEventPage((page) => Math.max(1, page - 1))}
              disabled={currentEventPage <= 1}
              className="inline-flex h-9 items-center justify-center rounded-xl border border-black/[0.08] bg-white px-3 text-sm font-medium text-gray-600 transition hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
            >
              上一页
            </button>
            <button
              type="button"
              onClick={() => setEventPage((page) => Math.min(eventPageCount, page + 1))}
              disabled={currentEventPage >= eventPageCount}
              className="inline-flex h-9 items-center justify-center rounded-xl bg-[#002fa7] px-3 text-sm font-medium text-white transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function KnowledgeImportJobDetailPage() {
  const params = useParams<{ jobId?: string | string[] }>();
  const router = useRouter();
  const jobId = Array.isArray(params.jobId) ? params.jobId[0] : params.jobId;
  const { sidebarOpen, toggleSidebar, sidebarWidth, setSidebarWidth } = useApp();
  const [mounted, setMounted] = useState(false);
  const [job, setJob] = useState<KnowledgeImportJob | null>(null);
  const [document, setDocument] = useState<KnowledgeDocument | null>(null);
  const [events, setEvents] = useState<KnowledgeImportEvent[]>([]);
  const [preview, setPreview] = useState<KnowledgeFilePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<MainTab>("overview");
  const [resultTab, setResultTab] = useState<ResultTab>("markdown");
  const [loading, setLoading] = useState(true);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [vectorPublishing, setVectorPublishing] = useState(false);
  const [imagePreview, setImagePreview] = useState<ImagePreview>(null);
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchResult, setSearchResult] = useState<KnowledgeSearchResult | null>(null);
  const [searchPoolTab, setSearchPoolTab] = useState<SearchPoolTab>("text_vector");

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const handleSidebarResize = useCallback(
    (delta: number) => {
      setSidebarWidth((prev: number) => Math.max(200, prev + delta));
    },
    [setSidebarWidth]
  );

  const refresh = useCallback(async (options?: { silent?: boolean }) => {
    if (!jobId) return;
    if (!options?.silent) setLoading(true);
    try {
      const detail = await getKnowledgeImportJob(jobId);
      setJob(detail.job);
      setEvents(detail.events);
      setDocument(detail.document ?? null);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!isImportJobActive(job) && !hasActiveVectorJob(job)) return;
    const timer = window.setInterval(() => {
      refresh({ silent: true });
    }, globalThis.document?.visibilityState === "visible" ? 5000 : 15000);
    return () => window.clearInterval(timer);
  }, [job, refresh]);

  const documentVirtualPath = useMemo(() => {
    if (document?.virtual_path) return document.virtual_path;
    const value = job?.metadata?.document_virtual_path;
    return typeof value === "string" ? value : "";
  }, [document, job]);

  const loadPreview = useCallback(async () => {
    if (!documentVirtualPath) return;
    setPreviewLoading(true);
    try {
      const nextPreview = await previewKnowledgeFile(documentVirtualPath);
      setPreview(nextPreview);
      setPreviewError(null);
    } catch (error) {
      setPreview(null);
      const message = errorMessage(error);
      setPreviewError(message.includes("File not found") ? "文件找不到了，可能是旧任务或文件被移动。可以重新导入一次。" : message);
    } finally {
      setPreviewLoading(false);
    }
  }, [documentVirtualPath]);

  useEffect(() => {
    if (!documentVirtualPath || preview?.virtual_path === documentVirtualPath) return;
    loadPreview();
  }, [documentVirtualPath, loadPreview, preview?.virtual_path]);

  const vectorIndex = useMemo(() => {
    const ingestion = job?.metadata?.ingestion;
    if (ingestion && typeof ingestion === "object") {
      const value = (ingestion as Record<string, unknown>).vector_index;
      if (value && typeof value === "object") return value as Record<string, unknown>;
    }
    const value = document?.metadata?.vector_index;
    return value && typeof value === "object" ? (value as Record<string, unknown>) : null;
  }, [document?.metadata, job?.metadata]);
  const previewContent = useMemo(() => repairUtf8Mojibake(preview?.content || ""), [preview?.content]);
  const markdownImageContexts = useMemo(() => extractImageContextsFromMarkdown(previewContent), [previewContent]);
  const indexedChunks = useMemo(() => parseIndexedChunks(vectorIndex), [vectorIndex]);
  const parsedChunks = useMemo(() => parseDocumentLlamaIndexChunks(document), [document]);
  const chunks = useMemo(
    () => (indexedChunks.length > 0 ? indexedChunks : parsedChunks.length > 0 ? parsedChunks : buildChunks(previewContent)),
    [indexedChunks, parsedChunks, previewContent]
  );
  const [chunkPage, setChunkPage] = useState(1);
  const chunkPageCount = Math.max(1, Math.ceil(chunks.length / CHUNKS_PER_PAGE));
  const currentChunkPage = Math.min(chunkPage, chunkPageCount);
  const pagedChunks = useMemo(
    () => chunks.slice((currentChunkPage - 1) * CHUNKS_PER_PAGE, currentChunkPage * CHUNKS_PER_PAGE),
    [chunks, currentChunkPage]
  );
  const chunkStart = chunks.length > 0 ? (currentChunkPage - 1) * CHUNKS_PER_PAGE + 1 : 0;
  const chunkEnd = Math.min(currentChunkPage * CHUNKS_PER_PAGE, chunks.length);
  const assets = metadataArray(document, "assets");
  const multimodal = document?.metadata?.multimodal;
  const imageCount = nestedNumber(multimodal, "image_asset_count") ?? assets.length;
  const originalPath = metadataString(document, "original_path") || job?.source_path || "-";
  const displayName = job?.title || job?.file_name || "导入任务";
  const vectorReady = Boolean(vectorIndex?.refreshed);
  const vectorMessage = vectorStatusMessage(vectorIndex);
  const currentIsVectorJob = isVectorPublishJob(job);
  const relatedSourceJobId = sourceJobId(job);
  const relatedVectorStatus = vectorJobStatus(job);
  const relatedVectorError = vectorJobError(job);
  const vectorJobRunning =
    (currentIsVectorJob && isImportJobActive(job)) || relatedVectorStatus === "queued" || relatedVectorStatus === "running";
  const currentVectorProgress = vectorProgress(job);
  const vectorStatusText = relatedVectorStatus === "failed" ? "失败" : vectorJobRunning ? "导入中" : vectorReady ? "已导入" : "未导入";

  useEffect(() => {
    setChunkPage(1);
  }, [jobId, chunks.length]);

  const publishVector = useCallback(async () => {
    if (!jobId || !job?.document_id) {
      setToast({ type: "error", message: "任务还没有生成知识库文档，暂时不能导入向量。" });
      return;
    }
    setVectorPublishing(true);
    setToast(null);
    try {
      const result = await publishKnowledgeImportJobVector(jobId);
      setToast({ type: "success", message: "向量导入已加入后台任务。" });
      router.push(`/knowledge/imports/${encodeURIComponent(result.job.id)}`);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
    } finally {
      setVectorPublishing(false);
    }
  }, [job?.document_id, jobId, router]);

  const runSearchTest = useCallback(async () => {
    const query = searchQuery.trim();
    if (!query) {
      setToast({ type: "error", message: "先输入一个要测试的问题。" });
      return;
    }
    setSearchLoading(true);
    setToast(null);
    try {
      const result = await searchKnowledge(query);
      setSearchResult(result);
    } catch (error) {
      setToast({ type: "error", message: errorMessage(error) });
      setSearchResult(null);
    } finally {
      setSearchLoading(false);
    }
  }, [searchQuery]);

  return (
    <div className="h-screen app-bg text-gray-900">
      <div className="fixed left-3 top-3 z-[80]">
        <Navbar sidebarOpen={sidebarOpen} toggleSidebar={toggleSidebar} showPanelToggles compact />
      </div>

      <div className="flex h-full overflow-hidden">
        <div
          className="workspace-sidebar-shell shrink-0 panel-transition overflow-hidden"
          style={{ width: sidebarOpen ? sidebarWidth : 0 }}
        >
          <div style={{ width: sidebarWidth, minWidth: 200 }} className="h-full flex flex-col">
            <div className="h-11 shrink-0" />
            <div className="flex-1 min-h-0 overflow-hidden">
              <Sidebar />
            </div>
          </div>
        </div>

        {mounted && sidebarOpen && <ResizeHandle onResize={handleSidebarResize} direction="left" />}

        <main className="workspace-content-frame flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 overflow-y-auto">
            <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-5 py-6">
              <div className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
                <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                  <div className="flex min-w-0 items-start gap-3">
                    <Link
                      href="/knowledge/imports"
                      className="inline-flex h-10 shrink-0 items-center gap-2 rounded-2xl border border-black/[0.08] bg-white px-3 text-sm font-medium text-gray-600 shadow-sm transition hover:text-[#002fa7]"
                    >
                      <ArrowLeft className="h-4 w-4" />
                      返回
                    </Link>
                    <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-[#002fa7]/10 text-[#002fa7]">
                      <FileText className="h-6 w-6" />
                    </div>
                    <div className="min-w-0">
                      <h1 className="truncate text-xl font-semibold tracking-tight text-gray-950" title={displayName}>
                        {displayName}
                      </h1>
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-gray-400">
                        {job ? (
                          <>
                            <span
                              className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${jobStatusClass(job.status)}`}
                            >
                              {job.status === "running" ? (
                                <Loader2 className="h-3 w-3 animate-spin" />
                              ) : job.status === "succeeded" ? (
                                <CheckCircle2 className="h-3 w-3" />
                              ) : job.status === "failed" ? (
                                <AlertCircle className="h-3 w-3" />
                              ) : (
                                <Clock3 className="h-3 w-3" />
                              )}
                              {jobStatusLabel(job.status)}
                            </span>
                            <span>{formatBytes(job.file_size)}</span>
                            <span>·</span>
                            <span>{currentIsVectorJob ? "向量导入" : job.file_type.toUpperCase()}</span>
                            <span>·</span>
                            <span>{formatTime(job.created_at)}</span>
                            {document ? (
                              <>
                                <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700">
                                  Markdown
                                </span>
                                {document.publish_targets.includes("vector") ? (
                                  <span className="rounded-full bg-[#002fa7]/10 px-2.5 py-1 text-xs font-medium text-[#002fa7]">
                                    Milvus 向量
                                  </span>
                                ) : null}
                              </>
                            ) : null}
                          </>
                        ) : (
                          <span>加载中...</span>
                        )}
                      </div>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => refresh()}
                    disabled={loading}
                    className="inline-flex h-10 items-center justify-center gap-2 rounded-full border border-black/[0.08] bg-white px-4 text-sm font-medium text-gray-700 shadow-sm transition hover:bg-gray-50 disabled:cursor-wait disabled:opacity-60"
                  >
                    <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
                    刷新
                  </button>
                </div>

                {job ? (
                  <div className="mt-5 h-2 overflow-hidden rounded-full bg-gray-100">
                    <div
                      className={`h-full rounded-full ${job.status === "failed" ? "bg-red-500" : "bg-[#002fa7]"}`}
                      style={{ width: `${Math.max(0, Math.min(100, job.progress || 0))}%` }}
                    />
                  </div>
                ) : null}
              </div>

              {toast ? (
                <div
                  className={`flex items-start gap-2 rounded-2xl border px-4 py-3 text-sm ${
                    toast.type === "success"
                      ? "border-emerald-500/15 bg-emerald-50 text-emerald-700"
                      : "border-red-500/15 bg-red-50 text-red-600"
                  }`}
                >
                  {toast.type === "success" ? (
                    <CheckCircle2 className="mt-0.5 h-4 w-4" />
                  ) : (
                    <AlertCircle className="mt-0.5 h-4 w-4" />
                  )}
                  <span className="break-all">{toast.message}</span>
                </div>
              ) : null}

              <div className="grid grid-cols-4 overflow-hidden rounded-[24px] border border-black/[0.06] bg-white shadow-sm">
                {(["overview", "result", "chunks", "search"] as MainTab[]).map((tab) => (
                  <button
                    key={tab}
                    type="button"
                    onClick={() => setActiveTab(tab)}
                    className={`flex h-16 flex-col items-center justify-center gap-1 text-sm transition ${
                      activeTab === tab
                        ? "bg-[#002fa7]/[0.06] text-[#002fa7]"
                        : "text-gray-500 hover:bg-black/[0.02]"
                    }`}
                  >
                    {tab === "overview" ? <Clock3 className="h-4 w-4" /> : null}
                    {tab === "result" ? <FileText className="h-4 w-4" /> : null}
                    {tab === "chunks" ? <Layers3 className="h-4 w-4" /> : null}
                    {tab === "search" ? <Search className="h-4 w-4" /> : null}
                    {mainTabLabel(tab)}
                  </button>
                ))}
              </div>

              {activeTab === "overview" ? (
                <section className="grid gap-5 lg:grid-cols-2">
                  <div className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
                    <h2 className="text-base font-semibold text-gray-950">文件信息</h2>
                    <div className="mt-5 space-y-4">
                      <InfoRow label="文件名" value={job?.file_name || "-"} />
                      <InfoRow label="任务类型" value={currentIsVectorJob ? "向量导入" : "文件导入"} />
                      <InfoRow label="文件类型" value={currentIsVectorJob ? "-" : job?.file_type?.toUpperCase() || "-"} />
                      <InfoRow label="文件大小" value={job ? formatBytes(job.file_size) : "-"} />
                      <InfoRow label="当前步骤" value={job?.current_step || "-"} />
                      <InfoRow label="任务编号" value={job?.id || "-"} />
                      {relatedSourceJobId ? <InfoRow label="原任务" value={relatedSourceJobId} /> : null}
                      <InfoRow label="开始时间" value={formatTime(job?.started_at)} />
                      <InfoRow label="完成时间" value={formatTime(job?.finished_at)} />
                      <InfoRow label="原始文件" value={originalPath} title={originalPath} />
                    </div>
                  </div>

                  <div className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
                    <h2 className="text-base font-semibold text-gray-950">解析统计</h2>
                    <div className="mt-5 grid grid-cols-3 gap-3">
                      <div className="rounded-2xl bg-[#002fa7]/[0.06] px-4 py-5 text-center">
                        <p className="text-2xl font-semibold text-[#002fa7]">{job?.progress || 0}%</p>
                        <p className="mt-1 text-xs text-gray-500">进度</p>
                      </div>
                      <div className="rounded-2xl bg-emerald-500/[0.07] px-4 py-5 text-center">
                        <p className="text-2xl font-semibold text-emerald-700">{imageCount}</p>
                        <p className="mt-1 text-xs text-gray-500">图片</p>
                      </div>
                      <div className="rounded-2xl bg-amber-500/[0.08] px-4 py-5 text-center">
                        <p className="text-2xl font-semibold text-amber-700">{chunks.length}</p>
                        <p className="mt-1 text-xs text-gray-500">切片预览</p>
                      </div>
                    </div>

                    <div className="mt-5">
                      <h3 className="text-sm font-semibold text-gray-900">处理方式</h3>
                      <div className="mt-3 grid gap-3">
                        <div className="rounded-2xl border border-[#002fa7]/20 bg-[#002fa7]/[0.04] p-4">
                          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                            <div>
                              <div className="flex items-center gap-2 text-sm font-semibold text-[#002fa7]">
                                <Database className="h-4 w-4" />
                                Milvus 向量
                                <span
                                  className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                                    relatedVectorStatus === "failed"
                                      ? "bg-red-50 text-red-600"
                                      : vectorJobRunning
                                      ? "bg-[#002fa7]/10 text-[#002fa7]"
                                      : vectorReady
                                        ? "bg-emerald-50 text-emerald-700"
                                        : "bg-white text-gray-500"
                                  }`}
                                >
                                  {vectorStatusText}
                                </span>
                              </div>
                              <p className="mt-1 text-xs leading-5 text-gray-500">
                                把当前知识库内容发布到多模态索引，语义问答和图文命中走这里。
                              </p>
                              {currentVectorProgress ? (
                                <div className="mt-3 grid gap-2 text-xs text-gray-500 sm:grid-cols-2">
                                  <div className="rounded-xl bg-white/70 px-3 py-2 ring-1 ring-black/[0.04]">
                                    文本 {currentVectorProgress.textDone}/{currentVectorProgress.textTotal}
                                  </div>
                                  <div className="rounded-xl bg-white/70 px-3 py-2 ring-1 ring-black/[0.04]">
                                    图片 {currentVectorProgress.imageDone}/{currentVectorProgress.imageTotal}
                                  </div>
                                </div>
                              ) : null}
                              {vectorMessage ? (
                                <p
                                  className={`mt-2 line-clamp-2 text-xs ${
                                    vectorMessage.tone === "error" ? "text-red-500" : "text-gray-400"
                                  }`}
                                  title={vectorMessage.text}
                                >
                                  {vectorMessage.text}
                                </p>
                              ) : null}
                              {relatedVectorError ? (
                                <p className="mt-2 line-clamp-2 text-xs text-red-500" title={relatedVectorError}>
                                  {relatedVectorError}
                                </p>
                              ) : null}
                            </div>
                            <button
                              type="button"
                              onClick={publishVector}
                              disabled={vectorPublishing || vectorJobRunning || !job?.document_id}
                              className="inline-flex h-10 shrink-0 items-center justify-center gap-2 rounded-2xl bg-[#002fa7] px-4 text-sm font-semibold text-white transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              {vectorPublishing || vectorJobRunning ? (
                                <Loader2 className="h-4 w-4 animate-spin" />
                              ) : (
                                <Database className="h-4 w-4" />
                              )}
                              {vectorJobRunning ? "导入中" : vectorReady ? "重新导入向量" : "导入向量"}
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="lg:col-span-2 rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
                    <h2 className="text-base font-semibold text-gray-950">处理记录</h2>
                    <div className="mt-4">
                      <EventList events={events} />
                    </div>
                  </div>
                </section>
              ) : null}

              {activeTab === "result" ? (
                <section className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
                  <div className="inline-flex flex-wrap gap-2 rounded-2xl bg-black/[0.025] p-1">
                    {(["markdown", "source", "images", "structured"] as ResultTab[]).map((tab) => (
                      <button
                        key={tab}
                        type="button"
                        onClick={() => setResultTab(tab)}
                        className={`inline-flex h-10 items-center gap-2 rounded-xl px-4 text-sm font-medium transition ${
                          resultTab === tab ? "bg-white text-[#002fa7] shadow-sm" : "text-gray-500 hover:text-gray-900"
                        }`}
                      >
                        {tab === "markdown" ? <FileText className="h-4 w-4" /> : null}
                        {tab === "source" ? <FileText className="h-4 w-4" /> : null}
                        {tab === "images" ? <FileImage className="h-4 w-4" /> : null}
                        {tab === "structured" ? <Layers3 className="h-4 w-4" /> : null}
                        {tab === "markdown" ? "Markdown" : null}
                        {tab === "source" ? "原始文件" : null}
                        {tab === "images" ? `图片 (${imageCount})` : null}
                        {tab === "structured" ? "结构化" : null}
                      </button>
                    ))}
                  </div>

                  <div className="mt-5 overflow-hidden rounded-[24px] border border-black/[0.06] bg-black/[0.018]">
                    {resultTab === "markdown" ? (
                      previewLoading ? (
                        <div className="flex min-h-[360px] items-center justify-center text-sm text-gray-400">
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                          加载 Markdown
                        </div>
                      ) : preview?.preview_type === "text" ? (
                        <MarkdownPreview content={previewContent || "这个 Markdown 是空的。"} truncated={preview.truncated} />
                      ) : (
                        <div className="flex min-h-[360px] flex-col items-center justify-center gap-2 px-6 text-center text-sm text-gray-400">
                          <p>
                            {previewError ||
                              (documentVirtualPath ? "暂时无法预览这个文件。" : "任务完成后会在这里显示 Markdown 结果。")}
                          </p>
                          {documentVirtualPath ? <p className="max-w-lg break-all text-xs text-gray-300">{documentVirtualPath}</p> : null}
                        </div>
                      )
                    ) : null}

                    {resultTab === "source" ? (
                      <div className="min-h-[260px] bg-white px-6 py-6">
                        <h3 className="text-sm font-semibold text-gray-950">原始文件</h3>
                        <p className="mt-3 break-all rounded-2xl bg-black/[0.025] px-4 py-3 text-sm leading-6 text-gray-600">
                          {originalPath}
                        </p>
                        <p className="mt-4 text-sm leading-6 text-gray-400">
                          PDF 原文预览后面可以接浏览器内嵌 PDF viewer；当前先把源文件路径和解析产物关联起来。
                        </p>
                      </div>
                    ) : null}

                    {resultTab === "images" ? (
                      <div className="min-h-[260px] bg-white px-6 py-6">
                        <h3 className="text-sm font-semibold text-gray-950">图片资源</h3>
                        {assets.length > 0 ? (
                          <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                            {assets.slice(0, 30).map((asset, index) => {
                              const image = parseAsset(asset, index, markdownImageContexts);
                              const contextText = compactText(image.context?.snippet || image.context?.caption || image.context?.heading || "");
                              return (
                                <div key={`${index}-${image.label}`} className="rounded-2xl border border-black/[0.06] p-4">
                                  <button
                                    type="button"
                                    onClick={() => image.src && setImagePreview(image)}
                                    disabled={!image.src}
                                    className="flex h-36 w-full items-center justify-center overflow-hidden rounded-2xl bg-black/[0.025] text-gray-300 transition hover:bg-[#002fa7]/[0.04] disabled:cursor-default"
                                    title={image.src ? "点击预览图片" : "图片不可预览"}
                                  >
                                    {image.src ? (
                                      <img
                                        src={image.src}
                                        alt={image.label}
                                        className="h-full w-full object-contain"
                                        loading="lazy"
                                      />
                                    ) : (
                                      <FileImage className="h-8 w-8" />
                                    )}
                                  </button>
                                  <p className="mt-3 break-all text-xs leading-5 text-gray-500">{image.label}</p>
                                  {contextText ? (
                                    <div className="mt-3 rounded-xl bg-[#002fa7]/[0.04] px-3 py-2">
                                      <p className="text-[11px] font-semibold text-[#002fa7]">上下文</p>
                                      <p className="mt-1 line-clamp-3 text-xs leading-5 text-gray-600" title={contextText}>
                                        {contextText}
                                      </p>
                                    </div>
                                  ) : null}
                                </div>
                              );
                            })}
                          </div>
                        ) : (
                          <p className="mt-4 text-sm text-gray-400">暂时没有图片资源。后面 MinerU 图片预览 API 接上后，这里会直接显示缩略图。</p>
                        )}
                      </div>
                    ) : null}

                    {resultTab === "structured" ? (
                      <div className="min-h-[260px] bg-white px-6 py-6">
                        <h3 className="text-sm font-semibold text-gray-950">结构化结果</h3>
                        <p className="mt-3 text-sm leading-6 text-gray-500">
                          这里预留给 MinerU layout block、表格、标题层级和后续 AI 结构化提取结果。
                        </p>
                        {document?.metadata?.mineru ? (
                          <pre className="mt-4 max-h-[360px] overflow-auto rounded-2xl bg-gray-950 px-4 py-4 text-xs leading-5 text-gray-100">
                            {JSON.stringify(document.metadata.mineru, null, 2)}
                          </pre>
                        ) : (
                          <div className="mt-4 rounded-2xl border border-dashed border-black/[0.08] px-4 py-8 text-center text-sm text-gray-400">
                            暂无结构化数据。
                          </div>
                        )}
                      </div>
                    ) : null}
                  </div>
                </section>
              ) : null}

              {activeTab === "chunks" ? (
                <section className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <h2 className="text-base font-semibold text-gray-950">切片预览</h2>
                    </div>
                    <div className="flex items-center gap-3">
                      {chunks.length > CHUNKS_PER_PAGE ? (
                        <span className="text-sm text-gray-400">
                          {chunkStart}-{chunkEnd} / {chunks.length}
                        </span>
                      ) : null}
                      <span className="text-sm text-gray-400">{chunks.length} 个</span>
                    </div>
                  </div>
                  <div className="mt-5 space-y-3">
                    {chunks.length > 0 ? (
                      <>
                        {pagedChunks.map((chunk) => (
                          <div key={chunk.index} className="rounded-2xl border border-black/[0.06] bg-black/[0.018] px-4 py-3">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="rounded-xl bg-[#002fa7]/10 px-2 py-1 text-xs font-semibold text-[#002fa7]">
                                #{chunk.index}
                              </span>
                              <span className="rounded-xl bg-white px-2 py-1 text-xs font-medium text-gray-500 shadow-sm ring-1 ring-black/[0.04]">
                                {chunk.level}
                              </span>
                              <h3 className="text-sm font-semibold text-gray-900">{chunk.title}</h3>
                            </div>
                            {chunk.preview ? (
                              <p className="mt-2 line-clamp-2 text-sm leading-6 text-gray-500">{chunk.preview}</p>
                            ) : null}
                            {chunk.headerPath || chunk.linkedImages?.length ? (
                              <div className="mt-2 flex flex-wrap gap-2 text-xs text-gray-400">
                                {chunk.headerPath ? <span>路径：{chunk.headerPath}</span> : null}
                                {chunk.linkedImages?.length ? <span>关联图片：{chunk.linkedImages.length}</span> : null}
                              </div>
                            ) : null}
                          </div>
                        ))}
                        {chunkPageCount > 1 ? (
                          <div className="flex flex-col gap-3 rounded-2xl border border-black/[0.06] bg-white px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                            <span className="text-sm text-gray-400">
                              第 {currentChunkPage} / {chunkPageCount} 页
                            </span>
                            <div className="flex items-center gap-2">
                              <button
                                type="button"
                                onClick={() => setChunkPage((page) => Math.max(1, page - 1))}
                                disabled={currentChunkPage <= 1}
                                className="inline-flex h-9 items-center justify-center rounded-xl border border-black/[0.08] bg-white px-3 text-sm font-medium text-gray-600 transition hover:text-[#002fa7] disabled:cursor-not-allowed disabled:opacity-40"
                              >
                                上一页
                              </button>
                              <button
                                type="button"
                                onClick={() => setChunkPage((page) => Math.min(chunkPageCount, page + 1))}
                                disabled={currentChunkPage >= chunkPageCount}
                                className="inline-flex h-9 items-center justify-center rounded-xl bg-[#002fa7] px-3 text-sm font-medium text-white transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-40"
                              >
                                下一页
                              </button>
                            </div>
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <div className="rounded-2xl border border-dashed border-black/[0.08] px-4 py-12 text-center text-sm text-gray-400">
                        暂无切片。
                      </div>
                    )}
                  </div>
                </section>
              ) : null}

              {activeTab === "search" ? (
                <section className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
                  <h2 className="text-base font-semibold text-gray-950">检索测试</h2>
                  <p className="mt-2 text-sm leading-6 text-gray-500">
                    输入问题，直接走当前 LlamaIndex 多模态检索配置，查看文本、关键词和图片命中。
                  </p>
                  <div className="mt-5 flex flex-col gap-3 sm:flex-row">
                    <input
                      value={searchQuery}
                      onChange={(event) => setSearchQuery(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          runSearchTest();
                        }
                      }}
                      placeholder="例如：这份报告的核心结论是什么？"
                      className="h-12 min-w-0 flex-1 rounded-2xl border border-black/[0.08] bg-white px-4 text-sm text-gray-700 outline-none transition focus:border-[#002fa7]/40 focus:ring-4 focus:ring-[#002fa7]/10"
                    />
                    <button
                      type="button"
                      onClick={runSearchTest}
                      disabled={searchLoading || !searchQuery.trim()}
                      className="inline-flex h-12 items-center justify-center gap-2 rounded-2xl bg-[#002fa7] px-5 text-sm font-semibold text-white transition hover:bg-[#001f7a] disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {searchLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
                      测试
                    </button>
                  </div>
                  {searchResult ? (
                    <div className="mt-5 flex flex-col gap-4">
                      <div className="order-1 grid gap-3 text-sm sm:grid-cols-4">
                        <div className="rounded-2xl bg-[#002fa7]/[0.06] px-4 py-3">
                          <p className="text-xs text-gray-400">最终结果</p>
                          <p className="mt-1 font-semibold text-[#002fa7]">{searchResult.retrieval.selected}/{searchResult.top_k}</p>
                        </div>
                        <div className="rounded-2xl bg-emerald-500/[0.07] px-4 py-3">
                          <p className="text-xs text-gray-400">文本语义</p>
                          <p className="mt-1 font-semibold text-emerald-700">{searchResult.retrieval.text_vector}</p>
                        </div>
                        <div className="rounded-2xl bg-amber-500/[0.08] px-4 py-3">
                          <p className="text-xs text-gray-400">关键词</p>
                          <p className="mt-1 font-semibold text-amber-700">{searchResult.retrieval.bm25}</p>
                        </div>
                        <div className="rounded-2xl bg-[#002fa7]/[0.06] px-4 py-3">
                          <p className="text-xs text-gray-400">图片向量</p>
                          <p className="mt-1 font-semibold text-[#002fa7]">{searchResult.retrieval.image_vector}</p>
                        </div>
                      </div>
                      <div className="order-2 rounded-2xl border border-[#002fa7]/10 bg-[#002fa7]/[0.035] px-4 py-3 text-sm leading-6 text-gray-600">
                        {retrievalAssessment(searchResult)}
                      </div>
                      {(() => {
                        const pool = searchResult.candidate_pools?.[searchPoolTab] || [];
                        return (
                          <details className="order-4 rounded-3xl border border-black/[0.06] bg-black/[0.015] p-4">
                            <summary className="cursor-pointer list-none">
                              <div className="flex flex-wrap items-center justify-between gap-3">
                                <div>
                                  <p className="text-sm font-semibold text-gray-950">召回过程</p>
                                  <p className="mt-1 text-xs text-gray-400">
                                    展开后查看文本语义、关键词和图片向量各自的候选池。
                                  </p>
                                </div>
                                <span className="rounded-full bg-white px-3 py-1 text-xs font-medium text-gray-500 ring-1 ring-black/[0.06]">
                                  当前：{searchPoolLabel(searchPoolTab)} · {pool.length} 条
                                </span>
                              </div>
                            </summary>
                            <div className="mt-4">
                              <div className="mb-4 inline-flex rounded-2xl bg-white p-1 ring-1 ring-black/[0.06]">
                                {(["text_vector", "bm25", "image_vector"] as SearchPoolTab[]).map((tab) => (
                                  <button
                                    key={tab}
                                    type="button"
                                    onClick={() => setSearchPoolTab(tab)}
                                    className={`rounded-xl px-3 py-2 text-xs font-semibold transition ${
                                      searchPoolTab === tab
                                        ? "bg-[#002fa7] text-white shadow-sm"
                                        : "text-gray-500 hover:bg-black/[0.04] hover:text-gray-900"
                                    }`}
                                  >
                                    {searchPoolLabel(tab)}
                                  </button>
                                ))}
                              </div>
                              <div>
                                <p className="text-sm font-semibold text-gray-950">
                                  {searchPoolLabel(searchPoolTab)}召回池
                                </p>
                                <p className="mt-1 text-xs text-gray-400">
                                  共 {pool.length} 条，点击上面的统计卡切换通道。
                                </p>
                              </div>
                            </div>
                            {pool.length > 0 ? (
                              <div className="mt-4 max-h-[460px] space-y-2 overflow-y-auto pr-1">
                                {pool.map((hit) => {
                                  const source = (hit.source || {}) as Record<string, unknown>;
                                  const metadata = (source.metadata && typeof source.metadata === "object" ? source.metadata : {}) as Record<string, unknown>;
                                  const virtualPath =
                                    hit.image_hit?.virtual_path ||
                                    (typeof metadata.virtual_path === "string" ? metadata.virtual_path : "") ||
                                    "";
                                  const imageSrc = hit.modality === "image" && virtualPath ? rawKnowledgeFileUrl(virtualPath) : "";
                                  return (
                                    <div
                                      key={`${searchPoolTab}-${hit.rank}-${hit.title}-${hit.retrieval_channel}`}
                                      className="rounded-2xl border border-black/[0.05] bg-white px-3 py-3"
                                    >
                                      <div className="flex gap-3">
                                        <span className="mt-0.5 inline-flex h-7 min-w-7 items-center justify-center rounded-xl bg-[#002fa7]/10 px-2 text-xs font-semibold text-[#002fa7]">
                                          #{hit.rank}
                                        </span>
                                        {imageSrc ? (
                                          <button
                                            type="button"
                                            onClick={() =>
                                              setImagePreview({
                                                label: hit.title || `图片候选 ${hit.rank}`,
                                                src: imageSrc,
                                              })
                                            }
                                            className="h-16 w-24 shrink-0 overflow-hidden rounded-xl border border-black/[0.06] bg-black/[0.02]"
                                          >
                                            {/* eslint-disable-next-line @next/next/no-img-element */}
                                            <img src={imageSrc} alt={hit.title || "图片候选"} className="h-full w-full object-contain" />
                                          </button>
                                        ) : null}
                                        <div className="min-w-0 flex-1">
                                          <div className="flex flex-wrap items-center gap-2">
                                            <p className="truncate text-sm font-semibold text-gray-900" title={hit.title}>
                                              {hit.title || `候选 ${hit.rank}`}
                                            </p>
                                            <span
                                              className="shrink-0 text-xs text-gray-400"
                                              title={typeof hit.raw_score === "number" ? `raw score ${hit.raw_score.toFixed(6)}` : undefined}
                                            >
                                              相关度 {formatRelevanceScore(relevanceScoreValue(hit))}
                                            </span>
                                          </div>
                                          <p className="mt-1 line-clamp-2 whitespace-pre-wrap text-xs leading-5 text-gray-500">
                                            {hit.quote || "没有摘要。"}
                                          </p>
                                          {typeof source.uri === "string" && source.uri ? (
                                            <p className="mt-1 truncate text-[11px] text-gray-400" title={source.uri}>
                                              {source.uri}
                                            </p>
                                          ) : null}
                                        </div>
                                      </div>
                                    </div>
                                  );
                                })}
                              </div>
                            ) : (
                              <div className="mt-4 rounded-2xl border border-dashed border-black/[0.08] px-4 py-8 text-center text-sm text-gray-400">
                                这个通道没有候选结果。
                              </div>
                            )}
                          </details>
                        );
                      })()}
                      {searchResult.hits.length > 0 ? (
                        <div className="order-3 space-y-3">
                          <div>
                            <p className="text-sm font-semibold text-gray-950">最终结果</p>
                            <p className="mt-1 text-xs text-gray-400">这是融合 / 重排后真正进入回答上下文的结果。</p>
                          </div>
                          {searchResult.hits.map((hit) => {
                            const source = (hit.source || {}) as Record<string, unknown>;
                            const metadata = (source.metadata && typeof source.metadata === "object" ? source.metadata : {}) as Record<string, unknown>;
                            const virtualPath =
                              hit.image_hit?.virtual_path ||
                              (typeof metadata.virtual_path === "string" ? metadata.virtual_path : "") ||
                              "";
                            const imageSrc = hit.modality === "image" && virtualPath ? rawKnowledgeFileUrl(virtualPath) : "";
                            return (
                              <div key={`${hit.rank}-${hit.title}-${hit.retrieval_channel}`} className="rounded-2xl border border-black/[0.06] bg-black/[0.018] p-4">
                                <div className="flex flex-wrap items-center gap-2">
                                  <span className="rounded-xl bg-[#002fa7]/10 px-2 py-1 text-xs font-semibold text-[#002fa7]">
                                    #{hit.rank}
                                  </span>
                                  <span className={`rounded-xl px-2 py-1 text-xs font-medium ${
                                    hit.modality === "image" ? "bg-[#002fa7]/10 text-[#002fa7]" : "bg-emerald-50 text-emerald-700"
                                  }`}>
                                    {hit.modality === "image" ? "图片" : "文本"}
                                  </span>
                                  <span className="rounded-xl bg-white px-2 py-1 text-xs font-medium text-gray-500 ring-1 ring-black/[0.04]">
                                    {retrievalChannelLabel(hit.retrieval_channel)}
                                  </span>
                                  <span
                                    className="text-xs text-gray-400"
                                    title={typeof hit.raw_score === "number" ? `raw score ${hit.raw_score.toFixed(6)}` : undefined}
                                  >
                                    相关度 {formatRelevanceScore(relevanceScoreValue(hit))}
                                  </span>
                                </div>
                                <div className="mt-3 grid gap-3 md:grid-cols-[160px,1fr]">
                                  {imageSrc ? (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        setImagePreview({
                                          label: hit.title || `图片命中 ${hit.rank}`,
                                          src: imageSrc,
                                        })
                                      }
                                      className="overflow-hidden rounded-2xl border border-black/[0.06] bg-white"
                                    >
                                      {/* eslint-disable-next-line @next/next/no-img-element */}
                                      <img src={imageSrc} alt={hit.title || "图片命中"} className="h-28 w-full object-contain" />
                                    </button>
                                  ) : null}
                                  <div className={imageSrc ? "min-w-0" : "min-w-0 md:col-span-2"}>
                                    <h3 className="truncate text-sm font-semibold text-gray-950" title={hit.title}>
                                      {hit.title || `命中 ${hit.rank}`}
                                    </h3>
                                    <p className="mt-2 line-clamp-4 whitespace-pre-wrap text-sm leading-6 text-gray-500">
                                      {hit.quote || "没有摘要。"}
                                    </p>
                                    {typeof source.uri === "string" && source.uri ? (
                                      <p className="mt-2 truncate text-xs text-gray-400" title={source.uri}>
                                        {source.uri}
                                      </p>
                                    ) : null}
                                  </div>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="rounded-2xl border border-dashed border-black/[0.08] px-4 py-10 text-center text-sm text-gray-400">
                          没有命中。可以放宽问题，或者确认这份文档已经导入向量。
                        </div>
                      )}
                    </div>
                  ) : null}
                </section>
              ) : null}
            </div>
          </div>
        </main>
      </div>

      {imagePreview ? (
        <div
          className="fixed inset-0 z-[120] flex items-center justify-center bg-gray-950/70 px-5 py-6 backdrop-blur-sm"
          onClick={() => setImagePreview(null)}
        >
          <div
            className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-center justify-between gap-3 border-b border-black/[0.06] px-5 py-4">
              <p className="min-w-0 truncate text-sm font-semibold text-gray-900" title={imagePreview.label}>
                {imagePreview.label}
              </p>
              <button
                type="button"
                onClick={() => setImagePreview(null)}
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-black/[0.04] text-gray-500 transition hover:bg-black/[0.08] hover:text-gray-900"
                aria-label="关闭图片预览"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex min-h-[320px] flex-1 items-center justify-center bg-black/[0.02] p-4">
              <img
                src={imagePreview.src}
                alt={imagePreview.label}
                className="max-h-[78vh] max-w-full rounded-2xl object-contain"
              />
            </div>
            {imagePreview.context?.snippet || imagePreview.context?.caption || imagePreview.context?.heading ? (
              <div className="border-t border-black/[0.06] px-5 py-4">
                <p className="text-xs font-semibold text-[#002fa7]">上下文</p>
                {imagePreview.context.heading ? (
                  <p className="mt-1 text-sm font-semibold text-gray-900">{imagePreview.context.heading}</p>
                ) : null}
                <p className="mt-1 text-sm leading-6 text-gray-600">
                  {imagePreview.context.snippet || imagePreview.context.caption || imagePreview.context.heading}
                </p>
                {imagePreview.context.lineNumber ? (
                  <p className="mt-2 text-xs text-gray-400">Markdown 第 {imagePreview.context.lineNumber} 行附近</p>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
