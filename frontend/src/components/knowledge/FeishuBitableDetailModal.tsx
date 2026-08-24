"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, Database, ExternalLink, Link2, Loader2, Plus, Search, Trash2, X } from "lucide-react";

import {
  createFeishuBitableRelation,
  deleteFeishuBitableRelation,
  listFeishuBitableRelations,
  listKnowledgeSourceItems,
  previewFeishuBitable,
  updateFeishuBitableRelation,
  type FeishuBitableCardinality,
  type FeishuBitableDeletePolicy,
  type FeishuBitableField,
  type FeishuBitablePreview,
  type FeishuBitableRelation,
  type FeishuBitableRelationInput,
  type KnowledgeSource,
  type KnowledgeSourceItem,
} from "@/lib/knowledgeSourcesApi";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

function cellText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (["string", "number", "boolean"].includes(typeof value)) return String(value);
  try { return JSON.stringify(value); } catch { return String(value); }
}

function fieldsOf(item: KnowledgeSourceItem | null): FeishuBitableField[] {
  const fields = item?.metadata?.fields;
  return Array.isArray(fields) ? fields as FeishuBitableField[] : [];
}

function tableIdOf(item: KnowledgeSourceItem | null): string {
  return String(item?.metadata?.table_id || "");
}

function viewIdOf(item: KnowledgeSourceItem | null): string {
  return String(item?.metadata?.view_id || "");
}

function relationLabel(cardinality: FeishuBitableCardinality): string {
  return { one_to_one: "1 : 1", one_to_many: "1 : N", many_to_one: "N : 1", many_to_many: "N : N" }[cardinality];
}

function deletePolicyLabel(policy: FeishuBitableDeletePolicy): string {
  return { retain_orphans: "保留来源记录并标记孤儿", restrict: "阻止删除目标记录", cascade: "级联删除来源记录" }[policy];
}

const EMPTY_RELATION: FeishuBitableRelationInput = {
  name: "",
  description: "",
  source_table_id: "",
  source_field_id: "",
  target_table_id: "",
  target_field_id: "",
  cardinality: "many_to_one",
  on_target_delete: "retain_orphans",
};

export default function FeishuBitableDetailModal({ item, source, onClose }: {
  item: KnowledgeSourceItem;
  source: KnowledgeSource;
  onClose: () => void;
}) {
  const [items, setItems] = useState<KnowledgeSourceItem[]>([]);
  const [selectedId, setSelectedId] = useState(item.id);
  const [preview, setPreview] = useState<FeishuBitablePreview | null>(null);
  const [relations, setRelations] = useState<FeishuBitableRelation[]>([]);
  const [search, setSearch] = useState("");
  const [loadingCatalog, setLoadingCatalog] = useState(true);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [savingRelation, setSavingRelation] = useState(false);
  const [error, setError] = useState("");
  const [relationError, setRelationError] = useState("");
  const [relationsOpen, setRelationsOpen] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState("");
  const [relationForm, setRelationForm] = useState<FeishuBitableRelationInput>(EMPTY_RELATION);

  useEffect(() => {
    let active = true;
    setLoadingCatalog(true);
    setError("");
    void Promise.all([listKnowledgeSourceItems(source.id), listFeishuBitableRelations(source.id)])
      .then(([nextItems, nextRelations]) => {
        if (!active) return;
        const bitableItems = nextItems.filter((candidate) => candidate.external_type === "bitable" && candidate.status !== "deleted" && tableIdOf(candidate));
        setItems(bitableItems);
        setRelations(nextRelations);
        if (!bitableItems.some((candidate) => candidate.id === item.id)) setSelectedId(bitableItems[0]?.id || "");
      })
      .catch((nextError) => { if (active) setError(messageOf(nextError)); })
      .finally(() => { if (active) setLoadingCatalog(false); });
    return () => { active = false; };
  }, [item.id, source.id]);

  const selectedItem = useMemo(() => items.find((candidate) => candidate.id === selectedId) || items[0] || null, [items, selectedId]);
  const selectedTableId = tableIdOf(selectedItem);
  const savedFields = fieldsOf(selectedItem);
  const fields = preview?.fields || savedFields;
  const sourceUrl = selectedItem?.source_url || item.source_url || String(source.config.source_url || "");
  const relationWritable = source.config.source_mode === "bitable";

  useEffect(() => {
    if (!selectedItem || !sourceUrl || !selectedTableId) { setPreview(null); return; }
    let active = true;
    setLoadingPreview(true);
    setError("");
    setPreview(null);
    void previewFeishuBitable(source.id, { url: sourceUrl, table_id: selectedTableId, view_id: viewIdOf(selectedItem), page_size: 10 })
      .then((result) => { if (active) setPreview(result); })
      .catch((nextError) => { if (active) setError(messageOf(nextError)); })
      .finally(() => { if (active) setLoadingPreview(false); });
    return () => { active = false; };
  }, [selectedItem, selectedTableId, source.id, sourceUrl]);

  const tableById = useMemo(() => new Map(items.map((candidate) => [tableIdOf(candidate), candidate])), [items]);
  const visibleItems = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    return keyword ? items.filter((candidate) => candidate.title.toLowerCase().includes(keyword)) : items;
  }, [items, search]);
  const selectedRelations = useMemo(() => relations.filter((relation) => relation.source_table_id === selectedTableId || relation.target_table_id === selectedTableId), [relations, selectedTableId]);
  const relationFieldIds = useMemo(() => new Set(selectedRelations.flatMap((relation) => {
    const ids: string[] = [];
    if (relation.source_table_id === selectedTableId) ids.push(relation.source_field_id);
    if (relation.target_table_id === selectedTableId) ids.push(relation.target_field_id);
    return ids;
  })), [selectedRelations, selectedTableId]);
  const sourceFields = fieldsOf(tableById.get(relationForm.source_table_id) || null);
  const targetFields = fieldsOf(tableById.get(relationForm.target_table_id) || null);

  function resetRelationForm(sourceTableId = selectedTableId) {
    const target = items.find((candidate) => tableIdOf(candidate) !== sourceTableId);
    setEditingId("");
    setRelationError("");
    setRelationForm({ ...EMPTY_RELATION, source_table_id: sourceTableId, target_table_id: tableIdOf(target || null) });
    setFormOpen(true);
  }

  function editRelation(relation: FeishuBitableRelation) {
    setEditingId(relation.id);
    setRelationError("");
    setRelationForm({
      name: relation.name,
      description: relation.description,
      source_table_id: relation.source_table_id,
      source_field_id: relation.source_field_id,
      target_table_id: relation.target_table_id,
      target_field_id: relation.target_field_id,
      cardinality: relation.cardinality,
      on_target_delete: relation.on_target_delete,
    });
    setFormOpen(true);
  }

  async function saveRelation() {
    if (!relationForm.source_table_id || !relationForm.source_field_id || !relationForm.target_table_id || !relationForm.target_field_id) {
      setRelationError("请选择关系两端的数据表和字段。");
      return;
    }
    setSavingRelation(true);
    setRelationError("");
    try {
      const saved = editingId
        ? await updateFeishuBitableRelation(source.id, editingId, relationForm)
        : await createFeishuBitableRelation(source.id, relationForm);
      setRelations((current) => editingId ? current.map((relation) => relation.id === editingId ? saved : relation) : [...current, saved]);
      setFormOpen(false);
      setEditingId("");
    } catch (nextError) {
      setRelationError(messageOf(nextError));
    } finally {
      setSavingRelation(false);
    }
  }

  async function removeRelation(relation: FeishuBitableRelation) {
    if (!window.confirm(`删除关系“${relation.name}”？Agent 将不再把它作为已确认 Join 路径。`)) return;
    setRelationError("");
    try {
      await deleteFeishuBitableRelation(source.id, relation.id);
      setRelations((current) => current.filter((candidate) => candidate.id !== relation.id));
      if (editingId === relation.id) setFormOpen(false);
    } catch (nextError) {
      setRelationError(messageOf(nextError));
    }
  }

  return (
    <div className="fixed inset-0 z-[120] grid place-items-center bg-slate-950/35 p-3 backdrop-blur-[2px]" role="dialog" aria-modal="true" aria-label="多维表格详情">
      <div className="flex h-[min(92vh,880px)] w-full max-w-[1500px] flex-col overflow-hidden rounded-[28px] border border-white/70 bg-white shadow-2xl">
        <div className="flex items-start justify-between border-b border-black/[0.06] px-6 py-4">
          <div className="flex min-w-0 items-center gap-3"><span className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-[#002fa7]/[0.07] text-[#002fa7]"><Database className="h-5 w-5" /></span><div className="min-w-0"><h2 className="truncate text-lg font-semibold text-gray-950">{source.name}</h2><p className="mt-1 text-xs text-gray-400">飞书多维表格 · {items.length || 1} 张数据表 · 实时读取 · 不保存行数据</p></div></div>
          <div className="flex items-center gap-2">{sourceUrl ? <a href={sourceUrl} target="_blank" rel="noreferrer" className="inline-flex h-9 items-center gap-1.5 rounded-xl border border-black/[0.08] px-3 text-xs font-semibold text-gray-600 hover:text-[#002fa7]">在飞书打开 <ExternalLink className="h-3.5 w-3.5" /></a> : null}<button type="button" onClick={onClose} className="grid h-9 w-9 place-items-center rounded-xl bg-gray-50 text-gray-500"><X className="h-4 w-4" /></button></div>
        </div>

        <div className={`grid min-h-0 flex-1 ${relationsOpen ? "grid-cols-[250px_minmax(0,1fr)_380px]" : "grid-cols-[250px_minmax(0,1fr)]"}`}>
          <aside className="flex min-h-0 flex-col border-r border-black/[0.06] bg-gray-50/60">
            <div className="border-b border-black/[0.06] p-3"><label className="flex h-9 items-center gap-2 rounded-xl border border-black/[0.08] bg-white px-3 text-gray-400"><Search className="h-3.5 w-3.5" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索数据表" className="min-w-0 flex-1 bg-transparent text-xs text-gray-700 outline-none" /></label></div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {loadingCatalog ? <div className="grid min-h-48 place-items-center"><Loader2 className="h-5 w-5 animate-spin text-[#002fa7]" /></div> : visibleItems.map((candidate) => {
                const tableId = tableIdOf(candidate);
                const relationCount = relations.filter((relation) => relation.source_table_id === tableId || relation.target_table_id === tableId).length;
                return <button type="button" key={candidate.id} onClick={() => setSelectedId(candidate.id)} className={`mb-1 grid w-full grid-cols-[34px_minmax(0,1fr)_auto] items-center gap-2 rounded-xl px-2.5 py-2.5 text-left ${candidate.id === selectedItem?.id ? "bg-[#002fa7]/[0.07] text-[#002fa7]" : "hover:bg-black/[0.025]"}`}><span className="grid h-8 w-8 place-items-center rounded-lg border border-black/[0.07] bg-white text-[10px] font-semibold">表</span><span className="min-w-0"><strong className="block truncate text-xs font-semibold">{candidate.title || tableId}</strong><span className="mt-1 block text-[10px] text-gray-400">{fieldsOf(candidate).length} 个字段</span></span><span className="text-[10px] text-gray-400">{relationCount ? `${relationCount} 关系` : ""}</span></button>;
              })}
              {!loadingCatalog && !visibleItems.length ? <div className="px-4 py-12 text-center text-xs text-gray-400">没有匹配的数据表。</div> : null}
            </div>
            <div className="border-t border-black/[0.06] px-4 py-3 text-[10px] leading-4 text-gray-400">Sheet 结构和关系定义保存在控制面；行数据始终从飞书实时读取。</div>
          </aside>

          <main className="flex min-w-0 flex-col overflow-hidden">
            <div className="flex min-h-[70px] items-center justify-between gap-4 border-b border-black/[0.06] px-5 py-3"><div className="min-w-0"><h3 className="truncate text-lg font-semibold text-gray-950">{selectedItem?.title || "多维表格"}</h3><p className="mt-1 text-[11px] text-gray-400">{fields.length} 个字段 · 最多显示 10 行样例 · 关闭窗口后丢弃查询结果</p></div>{relationWritable ? <button type="button" onClick={() => setRelationsOpen((value) => !value)} className={`inline-flex h-9 shrink-0 items-center gap-2 rounded-xl border px-3 text-xs font-semibold ${relationsOpen ? "border-[#002fa7]/25 bg-[#002fa7]/[0.06] text-[#002fa7]" : "border-black/[0.08] text-gray-600"}`}><Link2 className="h-3.5 w-3.5" />管理关系{selectedRelations.length ? ` · ${selectedRelations.length}` : ""}</button> : null}</div>
            {error ? <div className="m-4 rounded-xl bg-red-50 px-4 py-3 text-xs text-red-700">{error}</div> : null}
            <div className="flex flex-wrap items-center gap-2 border-b border-black/[0.06] px-4 py-3">{fields.map((field) => <span key={field.field_id} className={`inline-flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-[10px] ${field.is_primary ? "bg-[#002fa7]/[0.07] text-[#002fa7]" : relationFieldIds.has(field.field_id) ? "bg-violet-50 text-violet-700" : "border border-black/[0.07] text-gray-500"}`}><strong>{field.is_primary ? "PK" : relationFieldIds.has(field.field_id) ? "关联" : ""}</strong>{field.field_name}<span className="opacity-60">· {field.ui_type || field.type || "unknown"}</span></span>)}{preview ? <span className="ml-auto rounded-full bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold text-emerald-700">● 实时读取</span> : null}</div>
            <div className="min-h-0 flex-1 overflow-auto">{loadingPreview ? <div className="flex min-h-72 items-center justify-center gap-2 text-xs text-gray-400"><Loader2 className="h-4 w-4 animate-spin" />正在读取飞书…</div> : preview ? <table className="min-w-full border-collapse text-left text-xs"><thead className="sticky top-0 z-10 bg-gray-50"><tr>{fields.map((field) => <th key={field.field_id} className="whitespace-nowrap border-b border-r border-black/[0.06] px-3 py-3 font-semibold text-gray-600">{field.field_name}<span className="ml-1.5 font-mono text-[9px] font-normal text-gray-400">{field.ui_type || field.type || ""}</span></th>)}</tr></thead><tbody>{preview.records.items.map((record, index) => <tr key={record.record_id || index} className="border-b border-black/[0.04] hover:bg-gray-50/60">{fields.map((field) => <td key={field.field_id} title={cellText(record.fields?.[field.field_name])} className={`max-w-72 truncate whitespace-nowrap border-r border-black/[0.05] px-3 py-3 ${relationFieldIds.has(field.field_id) ? "font-medium text-violet-700" : "text-gray-600"}`}>{cellText(record.fields?.[field.field_name])}</td>)}</tr>)}</tbody></table> : !error ? <div className="grid min-h-72 place-items-center text-xs text-gray-400">当前没有可预览的记录。</div> : null}</div>
          </main>

          {relationsOpen ? <aside className="min-h-0 overflow-y-auto border-l border-black/[0.06] bg-white">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-black/[0.06] bg-white px-4 py-4"><div><h3 className="text-sm font-semibold text-gray-900">关系管理</h3><p className="mt-1 text-[10px] text-gray-400">Agent 只使用这里确认的跨表路径</p></div><button type="button" onClick={() => resetRelationForm()} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[#002fa7] px-3 text-[10px] font-semibold text-white"><Plus className="h-3 w-3" />新建</button></div>
            <div className="space-y-3 p-4">
              {relationError && !formOpen ? <div className="rounded-xl bg-red-50 px-3 py-2.5 text-[10px] text-red-700">{relationError}</div> : null}
              {formOpen ? <section className="rounded-2xl border border-[#002fa7]/15 bg-[#002fa7]/[0.025] p-3">
                <div className="flex items-center justify-between"><strong className="text-xs">{editingId ? "编辑关系" : "新建关系"}</strong><button type="button" onClick={() => setFormOpen(false)} className="text-gray-400"><X className="h-3.5 w-3.5" /></button></div>
                <div className="mt-3 grid gap-3">
                  <label className="grid gap-1.5 text-[10px] font-semibold text-gray-600">来源数据表<select value={relationForm.source_table_id} onChange={(event) => setRelationForm((current) => ({ ...current, source_table_id: event.target.value, source_field_id: "" }))} className="h-9 rounded-lg border border-black/10 bg-white px-2.5 text-xs font-normal outline-none">{items.map((candidate) => <option key={candidate.id} value={tableIdOf(candidate)}>{candidate.title}</option>)}</select></label>
                  <label className="grid gap-1.5 text-[10px] font-semibold text-gray-600">来源字段<select value={relationForm.source_field_id} onChange={(event) => setRelationForm((current) => ({ ...current, source_field_id: event.target.value }))} className="h-9 rounded-lg border border-black/10 bg-white px-2.5 text-xs font-normal outline-none"><option value="">选择字段</option>{sourceFields.map((field) => <option key={field.field_id} value={field.field_id}>{field.field_name} · {field.ui_type || field.type || "unknown"}</option>)}</select></label>
                  <div className="flex items-center gap-2 text-[10px] font-semibold text-gray-400"><span className="h-px flex-1 bg-black/[0.07]" /><ArrowRight className="h-3.5 w-3.5" /><span className="h-px flex-1 bg-black/[0.07]" /></div>
                  <label className="grid gap-1.5 text-[10px] font-semibold text-gray-600">目标数据表<select value={relationForm.target_table_id} onChange={(event) => setRelationForm((current) => ({ ...current, target_table_id: event.target.value, target_field_id: "" }))} className="h-9 rounded-lg border border-black/10 bg-white px-2.5 text-xs font-normal outline-none">{items.map((candidate) => <option key={candidate.id} value={tableIdOf(candidate)}>{candidate.title}</option>)}</select></label>
                  <label className="grid gap-1.5 text-[10px] font-semibold text-gray-600">目标字段<select value={relationForm.target_field_id} onChange={(event) => setRelationForm((current) => ({ ...current, target_field_id: event.target.value }))} className="h-9 rounded-lg border border-black/10 bg-white px-2.5 text-xs font-normal outline-none"><option value="">选择字段</option>{targetFields.map((field) => <option key={field.field_id} value={field.field_id}>{field.field_name} · {field.ui_type || field.type || "unknown"}{field.is_primary ? " · 主字段" : ""}</option>)}</select></label>
                  <div className="grid grid-cols-2 gap-2"><label className="grid gap-1.5 text-[10px] font-semibold text-gray-600">基数<select value={relationForm.cardinality} onChange={(event) => setRelationForm((current) => ({ ...current, cardinality: event.target.value as FeishuBitableCardinality }))} className="h-9 rounded-lg border border-black/10 bg-white px-2 text-xs font-normal outline-none"><option value="many_to_one">N : 1</option><option value="one_to_one">1 : 1</option><option value="one_to_many">1 : N</option><option value="many_to_many">N : N</option></select></label><label className="grid gap-1.5 text-[10px] font-semibold text-gray-600">删除策略<select value={relationForm.on_target_delete} onChange={(event) => setRelationForm((current) => ({ ...current, on_target_delete: event.target.value as FeishuBitableDeletePolicy }))} className="h-9 rounded-lg border border-black/10 bg-white px-2 text-xs font-normal outline-none"><option value="retain_orphans">保留并标记</option><option value="restrict">阻止删除</option><option value="cascade">级联删除</option></select></label></div>
                  <label className="grid gap-1.5 text-[10px] font-semibold text-gray-600">关系名称<input value={relationForm.name || ""} onChange={(event) => setRelationForm((current) => ({ ...current, name: event.target.value }))} placeholder="默认使用 表 A → 表 B" className="h-9 rounded-lg border border-black/10 bg-white px-2.5 text-xs font-normal outline-none" /></label>
                </div>
                <div className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-[10px] leading-4 text-amber-800">只校验 Schema、主字段和重复路径；不会为了保存关系扫描或保存整表行数据。</div>
                {relationError ? <div className="mt-2 rounded-xl bg-red-50 px-3 py-2 text-[10px] text-red-700">{relationError}</div> : null}
                <div className="mt-3 flex justify-end gap-2"><button type="button" onClick={() => setFormOpen(false)} className="h-8 rounded-lg border border-black/10 px-3 text-[10px] font-semibold text-gray-600">取消</button><button type="button" disabled={savingRelation} onClick={() => void saveRelation()} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[#002fa7] px-3 text-[10px] font-semibold text-white disabled:opacity-50">{savingRelation ? <Loader2 className="h-3 w-3 animate-spin" /> : null}保存并校验</button></div>
              </section> : null}

              {relations.map((relation) => <section key={relation.id} className="rounded-2xl border border-black/[0.07] p-3">
                <div className="flex items-start justify-between gap-3"><div className="min-w-0"><strong className="block truncate text-xs text-gray-900">{relation.name}</strong><span className="mt-1 inline-flex rounded-md bg-[#002fa7]/[0.06] px-1.5 py-0.5 text-[9px] font-semibold text-[#002fa7]">{relationLabel(relation.cardinality)}</span></div><button type="button" onClick={() => void removeRelation(relation)} className="grid h-7 w-7 shrink-0 place-items-center rounded-lg text-gray-400 hover:bg-red-50 hover:text-red-600"><Trash2 className="h-3.5 w-3.5" /></button></div>
                <button type="button" onClick={() => editRelation(relation)} className="mt-3 w-full rounded-xl bg-gray-50 p-2.5 text-left"><span className="block text-[10px] font-medium text-gray-700">{relation.source_table_name}.{relation.source_field_name}</span><span className="my-1 flex items-center gap-1 text-[9px] text-gray-400"><ArrowRight className="h-3 w-3" />{deletePolicyLabel(relation.on_target_delete)}</span><span className="block text-[10px] font-medium text-gray-700">{relation.target_table_name}.{relation.target_field_name}</span></button>
                <div className={`mt-2 flex items-start gap-1.5 rounded-lg px-2 py-1.5 text-[9px] leading-4 ${relation.validation_status === "schema_valid" ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-800"}`}>{relation.validation_status === "schema_valid" ? <span>✓</span> : <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />}<span>{relation.validation_status === "schema_valid" ? "结构已验证 · 未保存行数据" : relation.validation_warnings?.[0] || "需要检查关系端点"}</span></div>
              </section>)}
              {!relations.length && !formOpen ? <div className="rounded-2xl border border-dashed border-black/10 px-5 py-12 text-center"><Link2 className="mx-auto h-5 w-5 text-gray-300" /><p className="mt-3 text-xs font-medium text-gray-600">还没有已确认关系</p><p className="mt-1 text-[10px] leading-4 text-gray-400">没有关系不影响查看数据；Agent 只会避免跨表猜测。</p></div> : null}
            </div>
          </aside> : null}
        </div>
      </div>
    </div>
  );
}
