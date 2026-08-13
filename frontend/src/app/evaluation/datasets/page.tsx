"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Archive, CloudUpload, Code2, Download, FileDown, Loader2, Plus, RefreshCw, Upload } from "lucide-react";
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
  return <div className="workspace-page-container">
    <div className="mb-6 flex items-end justify-between"><div><h1 className="text-xl font-semibold text-gray-900">评测集</h1><p className="mt-1 text-sm text-gray-500">本地 Dataset 是权威源；LangSmith 是可选投影。</p></div><button onClick={load} className="rounded-lg border bg-white p-2"><RefreshCw className="h-4 w-4" /></button></div>
    <div className="mb-5 rounded-xl border bg-white p-3"><div className="flex gap-2"><select value={datasetKind} onChange={(e) => setDatasetKind(e.target.value as "general" | "coding")} className="rounded-lg border px-3 text-sm"><option value="general">通用评测</option><option value="coding">Coding 评测</option></select><input value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} placeholder="新 Dataset 名称" className="min-w-0 flex-1 rounded-lg border px-3 py-2 text-sm"/><button onClick={downloadCsvTemplate} className="flex items-center gap-2 rounded-lg border px-3 text-sm" title="下载可批量填写的最简 CSV"><FileDown className="h-4 w-4"/>CSV 模板</button><label className="flex cursor-pointer items-center gap-2 rounded-lg border px-4 text-sm"><Upload className="h-4 w-4"/>批量导入<input type="file" accept=".json,.bundle,.jsonl,.csv" className="hidden" onChange={async(e)=>{const file=e.target.files?.[0];if(!file)return;const format=file.name.endsWith(".csv")?"csv":file.name.endsWith(".jsonl")?"jsonl":"bundle";setBusy("import");try{await importEvaluationDataset(await file.text(),format);await load();}catch(error){setError(error instanceof Error?error.message:"导入失败");}finally{setBusy(null);e.target.value="";}}}/></label><button onClick={create} disabled={busy === "create"} className="flex items-center gap-2 rounded-lg bg-[#002fa7] px-4 text-sm text-white"><Plus className="h-4 w-4"/>新建</button></div>{datasetKind === "coding" && <div className="mt-3 flex items-center gap-2 border-t pt-3"><Code2 className="h-4 w-4 text-gray-500"/><span className="text-xs text-gray-500">SWE-bench Verified</span><input type="number" min={1} max={500} value={sweLimit} onChange={(e) => setSweLimit(Math.max(1, Math.min(500, Number(e.target.value) || 1)))} className="w-20 rounded-lg border px-2 py-1 text-sm"/><span className="text-xs text-gray-400">Cases</span><button disabled={busy === "swebench"} onClick={importSwe} className="rounded-lg border px-3 py-1.5 text-xs text-[#002fa7] disabled:opacity-50">{busy === "swebench" ? "导入中…" : "从官方数据集导入"}</button><span className="text-xs text-gray-400">Agent 由 PuddingClaw Harness 执行，生成的 patch 由平台自动调用官方 Docker Harness 判卷。</span></div>}<p className="mt-2 text-xs text-gray-400">{datasetKind === "coding" ? "Coding Dataset 会开放隔离 execute；自建题使用隐藏用例，SWE-bench 使用托管官方 Docker Verifier。" : "通用 Case 可使用 CSV 批量创建，不包含 Coding/SWE-bench 配置。"}</p></div>
    {error && <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
    {loading ? <div className="flex justify-center p-12"><Loader2 className="animate-spin"/></div> : <div className="overflow-hidden rounded-xl border bg-white">
      <div className="grid grid-cols-[minmax(0,1fr)_100px_100px_110px_280px] border-b bg-gray-50 px-4 py-2 text-xs text-gray-500"><span>名称</span><span>版本</span><span>状态</span><span>Cases</span><span>操作</span></div>
      {items.map((dataset) => { const allowed = datasetActions(dataset); const key = dataset.dataset_id; return <div key={key} className="grid grid-cols-[minmax(0,1fr)_100px_100px_110px_280px] items-center border-b px-4 py-3 text-sm last:border-b-0">
        <Link href={`/evaluation/datasets/${encodeURIComponent(key)}`} className="min-w-0"><div className="truncate font-medium text-gray-900">{dataset.name}</div><div className="truncate text-xs text-gray-400">{dataset.description || dataset.default_profile}</div></Link>
        <span>v{dataset.current_version}</span><span className="text-xs">{dataset.status}</span><span>{dataset.cases.length}</span>
        <div className="flex items-center gap-1">
          {allowed.publishable && <button disabled={busy === key} onClick={() => act(key, () => publishEvaluationDataset(key, dataset.revision))} className="rounded p-2 text-[#002fa7]" title="发布"><CloudUpload className="h-4 w-4"/></button>}
          {allowed.reopenable && <button disabled={busy === key} onClick={() => act(key, () => reopenEvaluationDataset(key, dataset.revision))} className="rounded px-2 py-1 text-xs">新草稿</button>}
          {allowed.syncable && <button disabled={busy === key} onClick={() => act(key, () => syncEvaluationDataset(key, dataset.current_version))} className="rounded px-2 py-1 text-xs">同步</button>}
          <a href={evaluationDatasetExportUrl(key)} className="rounded p-2" title="导出"><Download className="h-4 w-4"/></a>
          {dataset.tags.includes("swebench") && dataset.current_version > 0 && <a href={frozenSWEbenchDatasetExportUrl(key, dataset.current_version)} className="rounded px-2 py-1 text-xs" title="官方 Harness 使用的冻结数据快照">SWE fixture</a>}
          {allowed.archivable && <button disabled={busy === key} onClick={() => confirm("归档后不可编辑，确认？") && act(key, () => archiveEvaluationDataset(key, dataset.revision))} className="rounded p-2 text-gray-400" title="归档"><Archive className="h-4 w-4"/></button>}
        </div>
      </div>; })}
      {!items.length && <div className="p-12 text-center text-sm text-gray-400">还没有 Dataset</div>}
    </div>}
  </div>;
}
