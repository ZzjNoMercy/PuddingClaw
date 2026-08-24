"use client";

import { useEffect, useState } from "react";
import { Database, ExternalLink, Loader2, X } from "lucide-react";

import {
  previewFeishuBitable,
  type FeishuBitableField,
  type FeishuBitablePreview,
  type KnowledgeSource,
  type KnowledgeSourceItem,
} from "@/lib/knowledgeSourcesApi";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

function cellText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try { return JSON.stringify(value); } catch { return String(value); }
}

export default function FeishuBitableDetailModal({ item, source, onClose }: {
  item: KnowledgeSourceItem;
  source: KnowledgeSource;
  onClose: () => void;
}) {
  const [preview, setPreview] = useState<FeishuBitablePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const metadata = item.metadata || {};
  const sourceUrl = item.source_url || String(source.config.source_url || "");
  const tableId = String(metadata.table_id || source.config.table_id || "");
  const viewId = String(metadata.view_id || source.config.view_id || "");
  const savedFields = Array.isArray(metadata.fields) ? metadata.fields as FeishuBitableField[] : [];
  const fields = preview?.fields || savedFields;

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    void previewFeishuBitable(source.id, { url: sourceUrl, table_id: tableId, view_id: viewId, page_size: 10 })
      .then((result) => { if (active) setPreview(result); })
      .catch((nextError) => { if (active) setError(messageOf(nextError)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [source.id, sourceUrl, tableId, viewId]);

  return (
    <div className="fixed inset-0 z-[120] grid place-items-center bg-slate-950/35 p-4 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-label="多维表格详情">
      <div className="flex max-h-[90vh] w-full max-w-6xl flex-col overflow-hidden rounded-[28px] border border-white/70 bg-white shadow-2xl">
        <div className="flex items-start justify-between border-b border-black/[0.06] px-6 py-5">
          <div className="flex min-w-0 items-center gap-3"><span className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-[#002fa7]/[0.07] text-[#002fa7]"><Database className="h-5 w-5" /></span><div className="min-w-0"><h2 className="truncate text-lg font-semibold text-gray-950">{item.title || source.name}</h2><p className="mt-1 text-xs text-gray-400">飞书多维表格 · 实时读取 · 不保存行数据</p></div></div>
          <div className="flex items-center gap-2">{sourceUrl ? <a href={sourceUrl} target="_blank" rel="noreferrer" className="inline-flex h-9 items-center gap-1.5 rounded-xl border border-black/[0.08] px-3 text-xs font-semibold text-gray-600 hover:text-[#002fa7]">在飞书打开 <ExternalLink className="h-3.5 w-3.5" /></a> : null}<button type="button" onClick={onClose} className="grid h-9 w-9 place-items-center rounded-xl bg-gray-50 text-gray-500"><X className="h-4 w-4" /></button></div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-6">
          <section className="rounded-2xl border border-black/[0.06]">
            <div className="border-b border-black/[0.06] px-4 py-3"><h3 className="text-sm font-semibold text-gray-900">字段 Schema</h3><p className="mt-1 text-[11px] text-gray-400">Schema 描述列的名称、类型、ID、主字段和格式规则，不包含任何行数据。</p></div>
            <div className="flex flex-wrap gap-2 p-4">{fields.map((field) => <span key={field.field_id} className="inline-flex items-center gap-1.5 rounded-xl bg-gray-50 px-3 py-2 text-xs"><strong className="font-semibold text-gray-700">{field.field_name}</strong><span className="font-mono text-[10px] text-gray-400">{field.ui_type || field.type || "unknown"}</span>{field.is_primary ? <span className="rounded bg-[#002fa7]/10 px-1.5 py-0.5 text-[9px] font-semibold text-[#002fa7]">主字段</span> : null}</span>)}{!fields.length && !loading ? <span className="text-xs text-gray-400">暂无字段 Schema。</span> : null}</div>
          </section>
          <section className="mt-4 overflow-hidden rounded-2xl border border-black/[0.06]">
            <div className="flex items-center justify-between border-b border-black/[0.06] px-4 py-3"><div><h3 className="text-sm font-semibold text-gray-900">实时数据预览</h3><p className="mt-1 text-[11px] text-gray-400">最多显示 10 行；关闭窗口后不保留本次查询结果。</p></div>{preview ? <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold text-emerald-700">实时连接</span> : null}</div>
            {loading ? <div className="flex items-center justify-center gap-2 py-16 text-xs text-gray-400"><Loader2 className="h-4 w-4 animate-spin" />正在读取飞书…</div> : error ? <div className="m-4 rounded-xl bg-red-50 px-4 py-3 text-xs text-red-700">{error}</div> : (
              <div className="max-h-[42vh] overflow-auto"><table className="min-w-full border-collapse text-left text-xs"><thead className="sticky top-0 bg-gray-50"><tr>{fields.map((field) => <th key={field.field_id} className="whitespace-nowrap border-b border-black/[0.06] px-3 py-2.5 font-semibold text-gray-600">{field.field_name}</th>)}</tr></thead><tbody>{preview?.records.items.map((record, index) => <tr key={record.record_id || index} className="border-b border-black/[0.04] last:border-0">{fields.map((field) => <td key={field.field_id} title={cellText(record.fields?.[field.field_name])} className="max-w-64 truncate whitespace-nowrap px-3 py-2.5 text-gray-600">{cellText(record.fields?.[field.field_name])}</td>)}</tr>)}</tbody></table>{!preview?.records.items.length ? <div className="py-14 text-center text-xs text-gray-400">当前没有可预览的记录。</div> : null}</div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
