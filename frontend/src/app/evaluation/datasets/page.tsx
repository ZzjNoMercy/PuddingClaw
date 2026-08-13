"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Archive, CloudUpload, Code2, Download, FileCode2, FileDown, FilePlus2, Loader2, Play, Plus, RefreshCw, Upload } from "lucide-react";
import { archiveEvaluationDataset, createEvaluationDataset, evaluationDatasetExportUrl, frozenSWEbenchDatasetExportUrl, importEvaluationDataset, importSWEbenchDataset, listEvaluationDatasets, publishEvaluationDataset, reopenEvaluationDataset, syncEvaluationDataset, type EvalDataset } from "@/lib/evaluationApi";
import { datasetActions } from "@/lib/evaluationState";

export default function DatasetsPage() {
  const [items, setItems] = useState<EvalDataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [name, setName] = useState("");
  const [datasetKind, setDatasetKind] = useState<"general" | "coding">("general");
  const [sweLimit, setSweLimit] = useState(5);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => { try { setItems((await listEvaluationDatasets()).items); setError(""); } catch (e) { setError(e instanceof Error ? e.message : "加载失败"); } finally { setLoading(false); } }, []);
  useEffect(() => { load(); }, [load]);
  const downloadCsvTemplate = () => {
    const csv = [
      "question,answer,case_type,expected_tool,name,criticality",
      '"请只回答项目名称","PuddingClaw","smoke","","项目名称回答","normal"',
      '"读取 report.md 并总结","第一点|第二点|第三点","tool-use","read_file","文件总结","high"',
    ].join("\n");
    const url = URL.createObjectURL(new Blob([`\uFEFF${csv}\n`], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "puddingclaw-evaluation-template.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  };
  const create = async () => { if (!name.trim()) return; setBusy("create"); try { await createEvaluationDataset({ name: name.trim(), default_profile: datasetKind === "coding" ? "coding_agent@1" : "general_agent@1", tags: datasetKind === "coding" ? ["coding"] : [] }); setName(""); await load(); } catch (e) { setError(e instanceof Error ? e.message : "创建失败"); } finally { setBusy(null); } };
  const importSwe = async () => { setBusy("swebench"); try { await importSWEbenchDataset({ limit: sweLimit, split: "test", name: `SWE-bench Verified (${sweLimit})` }); await load(); } catch (e) { setError(e instanceof Error ? e.message : "SWE-bench 导入失败"); } finally { setBusy(null); } };
  const act = async (key: string, task: () => Promise<unknown>) => { setBusy(key); try { await task(); await load(); } catch (e) { setError(e instanceof Error ? e.message : "操作失败"); } finally { setBusy(null); } };
  const statusPresentation = (status: EvalDataset["status"]) => status === "published"
    ? { label: "已冻结 · 可评测", className: "border-emerald-200 bg-emerald-50 text-emerald-700", dot: "bg-emerald-500" }
    : status === "draft"
      ? { label: "草稿 · 可编辑", className: "border-amber-200 bg-amber-50 text-amber-700", dot: "bg-amber-500" }
      : { label: "已归档", className: "border-slate-200 bg-slate-100 text-slate-500", dot: "bg-slate-400" };
  const visibleItems = items.filter((dataset) => datasetKind === "coding"
    ? dataset.default_profile === "coding_agent@1" || dataset.tags.includes("coding") || dataset.tags.includes("swebench")
    : dataset.default_profile !== "coding_agent@1" && !dataset.tags.includes("coding") && !dataset.tags.includes("swebench"));
  const secondaryActionClass = "inline-flex h-9 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-45";
  return <div className="workspace-page-container">
    <div className="mb-6 flex items-end justify-between"><div><h1 className="text-xl font-semibold text-gray-900">评测集</h1><p className="mt-1 text-sm text-gray-500">本地 Dataset 是权威源；LangSmith 是可选投影。</p></div><button onClick={load} className="rounded-lg border bg-white p-2"><RefreshCw className="h-4 w-4" /></button></div>
    <div className="mb-5 rounded-xl border bg-white p-3">
      <div className="flex flex-wrap gap-2">
        <select value={datasetKind} onChange={(e) => setDatasetKind(e.target.value as "general" | "coding")} className="h-9 rounded-lg border px-3 text-sm"><option value="general">通用评测</option><option value="coding">Coding 评测</option></select>
        <input value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} placeholder="新 Dataset 名称" className="h-9 min-w-[220px] flex-1 rounded-lg border px-3 text-sm"/>
        <button onClick={downloadCsvTemplate} className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 text-sm" title="下载可批量填写的最简 CSV"><FileDown className="h-4 w-4"/>CSV 模板</button>
        <label className="inline-flex h-9 shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-lg border px-3 text-sm"><Upload className="h-4 w-4"/>批量导入<input type="file" accept=".json,.bundle,.jsonl,.csv" className="hidden" onChange={async(e)=>{const file=e.target.files?.[0];if(!file)return;const format=file.name.endsWith(".csv")?"csv":file.name.endsWith(".jsonl")?"jsonl":"bundle";setBusy("import");try{await importEvaluationDataset(await file.text(),format);await load();}catch(error){setError(error instanceof Error?error.message:"导入失败");}finally{setBusy(null);e.target.value="";}}}/></label>
        <button onClick={create} disabled={busy === "create"} className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-[#002fa7] px-3.5 text-sm text-white"><Plus className="h-4 w-4"/>新建</button>
      </div>
      {datasetKind === "coding" && (
        <div className="mt-3 border-t pt-3">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-2">
            <Code2 className="h-4 w-4 shrink-0 text-gray-500"/>
            <span className="shrink-0 whitespace-nowrap text-xs font-medium text-gray-600">SWE-bench Verified</span>
            <div className="flex shrink-0 items-center gap-2">
              <input type="number" min={1} max={500} value={sweLimit} onChange={(e) => setSweLimit(Math.max(1, Math.min(500, Number(e.target.value) || 1)))} className="h-8 w-20 rounded-lg border px-2 text-sm"/>
              <span className="text-xs text-gray-400">Cases</span>
            </div>
            <button disabled={busy === "swebench"} onClick={importSwe} className="inline-flex h-8 shrink-0 items-center whitespace-nowrap rounded-lg border border-slate-200 px-3 text-xs font-medium text-[#002fa7] transition hover:bg-blue-50 disabled:opacity-50">{busy === "swebench" ? "导入中…" : "从官方数据集导入"}</button>
            <span className="min-w-[280px] flex-1 text-xs leading-5 text-gray-400">Case 像输入框消息一样进入 PuddingClaw Harness；SWE-bench 的 execute 使用官方依赖环境，patch 再由干净的官方 Docker Harness 判卷。</span>
          </div>
        </div>
      )}
      <p className="mt-2 text-xs leading-5 text-gray-400">{datasetKind === "coding" ? "Coding Dataset 使用生产 Harness 的工作区工具协议；自建题使用隐藏用例，SWE-bench 使用独立的官方依赖环境与托管 Verifier。" : "通用 Case 可使用 CSV 批量创建，不包含 Coding/SWE-bench 配置。"}</p>
    </div>
    {error && <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
    {loading ? <div className="flex justify-center p-12"><Loader2 className="animate-spin"/></div> : <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="min-w-[950px]"><div className="grid grid-cols-[minmax(220px,1fr)_60px_140px_60px_minmax(430px,auto)] border-b border-slate-200 bg-slate-50/80 px-5 py-3 text-xs font-medium text-slate-500"><span>名称</span><span>版本</span><span>状态</span><span>Cases</span><span>操作</span></div>
      {visibleItems.map((dataset) => { const allowed = datasetActions(dataset); const key = dataset.dataset_id; const status = statusPresentation(dataset.status); const rowBusy = busy === key; return <div key={key} className="grid grid-cols-[minmax(220px,1fr)_60px_140px_60px_minmax(430px,auto)] items-center border-b border-slate-100 px-5 py-4 text-sm transition last:border-b-0 hover:bg-slate-50/50">
        <Link href={`/evaluation/datasets/${encodeURIComponent(key)}`} className="group min-w-0 pr-5"><div className="truncate font-semibold text-slate-900 transition group-hover:text-[#002fa7]">{dataset.name}</div><div className="mt-1 truncate text-xs text-slate-400">{dataset.description || dataset.default_profile}</div></Link>
        <span className="w-fit rounded-md bg-slate-100 px-2 py-1 font-mono text-xs font-semibold text-slate-600">v{dataset.current_version}</span><span className={`inline-flex w-fit items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${status.className}`}><span className={`h-1.5 w-1.5 rounded-full ${status.dot}`}/>{status.label}</span><span className="font-medium text-slate-700">{dataset.cases.length}</span>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {allowed.publishable && <button disabled={rowBusy} onClick={() => act(key, () => publishEvaluationDataset(key, dataset.revision))} className="inline-flex h-9 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg bg-[#002fa7] px-3.5 text-xs font-semibold text-white shadow-sm transition hover:bg-[#00257f] disabled:cursor-not-allowed disabled:opacity-45">{rowBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin"/> : <CloudUpload className="h-3.5 w-3.5"/>}冻结并发布</button>}
          {allowed.syncable && <Link href={`/evaluation/experiments/new?dataset=${encodeURIComponent(`${key}@${dataset.current_version}`)}`} className="inline-flex h-9 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg bg-[#002fa7] px-3.5 text-xs font-semibold text-white shadow-sm transition hover:bg-[#00257f]"><Play className="h-3.5 w-3.5 fill-current"/>开始评测</Link>}
          {allowed.reopenable && <button disabled={rowBusy} onClick={() => act(key, () => reopenEvaluationDataset(key, dataset.revision))} className={secondaryActionClass}><FilePlus2 className="h-3.5 w-3.5"/>新建草稿</button>}
          {allowed.syncable && <button disabled={rowBusy} onClick={() => act(key, () => syncEvaluationDataset(key, dataset.current_version))} className={secondaryActionClass}><CloudUpload className="h-3.5 w-3.5"/>同步 LangSmith</button>}
          <a href={evaluationDatasetExportUrl(key)} className={secondaryActionClass} title="导出完整 PuddingClaw Dataset"><Download className="h-3.5 w-3.5"/>导出</a>
          {dataset.tags.includes("swebench") && dataset.current_version > 0 && <a href={frozenSWEbenchDatasetExportUrl(key, dataset.current_version)} className={secondaryActionClass} title="导出官方 Harness 使用的冻结数据快照"><FileCode2 className="h-3.5 w-3.5"/>SWE Fixture</a>}
          {allowed.archivable && <button aria-label="归档 Dataset" disabled={rowBusy} onClick={() => confirm("归档后 Dataset 将不可编辑，确认归档？") && act(key, () => archiveEvaluationDataset(key, dataset.revision))} className="inline-flex h-9 shrink-0 items-center justify-center gap-1.5 rounded-lg border border-rose-200 bg-white px-2.5 text-xs font-medium text-rose-600 shadow-sm transition hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"><Archive className="h-3.5 w-3.5"/>归档</button>}
        </div>
      </div>; })}
      {!visibleItems.length && <div className="p-12 text-center text-sm text-gray-400">{datasetKind === "coding" ? "还没有 Coding Dataset" : "还没有通用 Dataset"}</div>}
      </div></div>}
  </div>;
}
