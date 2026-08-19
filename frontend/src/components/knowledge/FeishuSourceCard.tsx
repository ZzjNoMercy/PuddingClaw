import { ExternalLink } from "lucide-react";

import type { KnowledgeDocument } from "@/lib/api";

export type FeishuDocMeta = {
  authorName: string;
  authorId: string;
  publishedAt: string;
  updatedAt: string;
  revision: string;
  wikiPath: string[];
  url: string;
};

/** 读取同步时持久化的飞书文档元数据；非飞书文档返回 null。 */
export function feishuMetaOf(document: KnowledgeDocument | null | undefined): FeishuDocMeta | null {
  if (!document || document.source_type !== "feishu_docx") return null;
  const raw = document.metadata?.feishu;
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const wikiPath = Array.isArray(record.wiki_path)
    ? record.wiki_path.filter((item): item is string => typeof item === "string")
    : [];
  return {
    authorName: typeof record.author_name === "string" ? record.author_name : "",
    authorId: typeof record.author_id === "string" ? record.author_id : "",
    publishedAt: typeof record.published_at === "string" ? record.published_at : "",
    updatedAt: typeof record.updated_at === "string" ? record.updated_at : "",
    revision: typeof record.revision_id === "string" ? record.revision_id : "",
    wikiPath,
    url: document.origin_url || "",
  };
}

function dateOnly(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return value.slice(0, 10);
}

function MetaItem({ label, children, muted }: { label: string; children: React.ReactNode; muted?: boolean }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-[11px] text-gray-400">{label}</span>
      <span className={`flex min-w-0 items-center gap-1.5 text-[13px] ${muted ? "font-medium text-gray-400" : "font-semibold text-gray-900"}`}>
        {children}
      </span>
    </div>
  );
}

function AuthorValue({ meta }: { meta: FeishuDocMeta }) {
  const name = meta.authorName || meta.authorId;
  if (!name) {
    return (
      <>
        <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-black/[0.05] text-[10px] font-bold text-gray-400">?</span>
        <span className="truncate">飞书用户</span>
      </>
    );
  }
  return (
    <>
      <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-[#002fa7]/[0.07] text-[10px] font-bold text-[#002fa7]">
        {name.slice(0, 1)}
      </span>
      <span className="truncate">{name}</span>
    </>
  );
}

function OpenInFeishuLink({ url, className }: { url: string; className?: string }) {
  if (!url) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-xl bg-[#002fa7]/[0.07] px-3 text-xs font-semibold text-[#002fa7] transition hover:bg-[#002fa7] hover:text-white ${className || ""}`}
    >
      在飞书中打开
      <ExternalLink className="h-3.5 w-3.5" />
    </a>
  );
}

function SourceRow({ meta, trailing }: { meta: FeishuDocMeta; trailing?: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2.5">
      <img src="/brands/feishu-logo.svg" alt="飞书" className="h-[22px] w-[22px] shrink-0 rounded-md border border-black/[0.06] object-cover" />
      <span className="text-xs font-semibold text-gray-600">飞书文档</span>
      {meta.wikiPath.length > 0 ? (
        <span className="flex min-w-0 items-center gap-1.5 text-xs text-gray-400">
          {meta.wikiPath.map((node, index) => (
            <span key={`${node}-${index}`} className="flex min-w-0 items-center gap-1.5">
              {index > 0 ? <span className="text-black/[0.18]">/</span> : null}
              <span className={`truncate ${index === meta.wikiPath.length - 1 ? "font-medium text-gray-500" : ""}`}>{node}</span>
            </span>
          ))}
        </span>
      ) : null}
      {trailing}
    </div>
  );
}

/**
 * 飞书文档来源信息卡。两个形态：
 * - panel：概览 Tab 的半宽面板，替换「文件信息」卡；
 * - card：通栏卡片，放在 Markdown 预览或文档弹窗里。
 */
export default function FeishuSourceCard({ meta, variant = "card" }: { meta: FeishuDocMeta; variant?: "card" | "panel" }) {
  const publishedAt = dateOnly(meta.publishedAt);
  const updatedAt = dateOnly(meta.updatedAt);
  const revision = meta.revision ? `v${meta.revision}` : "-";

  if (variant === "panel") {
    return (
      <div className="rounded-[28px] border border-black/[0.06] bg-white p-5 shadow-sm">
        <h2 className="text-base font-semibold text-gray-950">来源信息</h2>
        <div className="mt-4">
          <SourceRow meta={meta} />
        </div>
        <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 border-t border-black/[0.05] pt-3.5">
          <MetaItem label="作者" muted={!meta.authorName && !meta.authorId}><AuthorValue meta={meta} /></MetaItem>
          <MetaItem label="发布时间">{publishedAt}</MetaItem>
          <MetaItem label="最近更新">{updatedAt}</MetaItem>
          <MetaItem label="文档版本">{revision}</MetaItem>
        </div>
        {meta.url ? (
          <div className="mt-4">
            <OpenInFeishuLink url={meta.url} />
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <section className="overflow-hidden rounded-3xl border border-black/[0.06] bg-white shadow-sm">
      <div className="flex flex-col gap-4 px-5 py-4">
        <SourceRow meta={meta} trailing={<span className="ml-auto"><OpenInFeishuLink url={meta.url} /></span>} />
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 border-t border-black/[0.05] pt-3.5 sm:grid-cols-4">
          <MetaItem label="作者" muted={!meta.authorName && !meta.authorId}><AuthorValue meta={meta} /></MetaItem>
          <MetaItem label="发布时间">{publishedAt}</MetaItem>
          <MetaItem label="最近更新">{updatedAt}</MetaItem>
          <MetaItem label="文档版本">{revision}</MetaItem>
        </div>
      </div>
    </section>
  );
}
