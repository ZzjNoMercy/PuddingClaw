"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Archive, CloudUpload, Code2, Download, FileCode2, FileDown, FilePlus2, Loader2, Play, Plus, RefreshCw, Upload, X } from "lucide-react";
import { archiveEvaluationDataset, createEvaluationDataset, evaluationDatasetExportUrl, frozenSWEbenchDatasetExportUrl, importEvaluationDataset, importSWEbenchDataset, listEvaluationDatasets, publishEvaluationDataset, reopenEvaluationDataset, syncEvaluationDataset, type EvalDataset } from "@/lib/evaluationApi";
import { datasetActions } from "@/lib/evaluationState";

const datasetTableGridClass = "grid min-w-[1320px] grid-cols-[minmax(300px,1fr)_72px_160px_72px_minmax(650px,max-content)]";

export default function DatasetsPage() {
  const [items, setItems] = useState<EvalDataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [name, setName] = useState("");
  const [datasetKind, setDatasetKind] = useState<"general" | "coding">("general");
  const [codingSource, setCodingSource] = useState<"blank" | "swebench">("blank");
  const [sweLimit, setSweLimit] = useState(5);
  const [modalError, setModalError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => { try { setItems((await listEvaluationDatasets()).items); setError(""); } catch (e) { setError(e instanceof Error ? e.message : "加载失败"); } finally { setLoading(false); } }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!createModalOpen) return;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && busy !== "create" && busy !== "swebench") {
        setCreateModalOpen(false);
      }
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [createModalOpen, busy]);
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
  const openCreateModal = () => {
    setName("");
    setDatasetKind("general");
    setCodingSource("blank");
    setSweLimit(5);
    setModalError("");
    setCreateModalOpen(true);
  };
  const submitCreate = async () => {
    const trimmedName = name.trim();
    if (!trimmedName) {
      setModalError("请输入评测集名称");
      return;
    }
    const isSWEbench = datasetKind === "coding" && codingSource === "swebench";
    setBusy(isSWEbench ? "swebench" : "create");
    setModalError("");
    try {
      if (isSWEbench) {
        await importSWEbenchDataset({ limit: sweLimit, split: "test", name: trimmedName });
      } else {
        await createEvaluationDataset({
          name: trimmedName,
          default_profile: datasetKind === "coding" ? "coding_agent@1" : "general_agent@1",
          tags: datasetKind === "coding" ? ["coding"] : [],
        });
      }
      setCreateModalOpen(false);
      await load();
    } catch (e) {
      setModalError(e instanceof Error ? e.message : isSWEbench ? "SWE-bench 导入失败" : "创建失败");
    } finally {
      setBusy(null);
    }
  };
  const act = async (key: string, task: () => Promise<unknown>) => { setBusy(key); try { await task(); await load(); } catch (e) { setError(e instanceof Error ? e.message : "操作失败"); } finally { setBusy(null); } };
  const statusPresentation = (status: EvalDataset["status"]) => status === "published"
    ? { label: "已冻结 · 可评测", className: "border-emerald-200 bg-emerald-50 text-emerald-700", dot: "bg-emerald-500" }
    : status === "draft"
      ? { label: "草稿 · 可编辑", className: "border-amber-200 bg-amber-50 text-amber-700", dot: "bg-amber-500" }
      : { label: "已归档", className: "border-slate-200 bg-slate-100 text-slate-500", dot: "bg-slate-400" };
  const datasetKindLabel = (dataset: EvalDataset) => dataset.tags.includes("swebench")
    ? "SWE-bench"
    : dataset.default_profile === "coding_agent@1" || dataset.tags.includes("coding")
      ? "代码评测"
      : "通用";
  const datasetDescription = (dataset: EvalDataset) => {
    if (dataset.tags.includes("swebench")) {
      return "SWE-bench 代码修复任务；不提供标准答案补丁，由官方判卷器统一评分。";
    }
    if (dataset.description) return dataset.description;
    return dataset.default_profile === "coding_agent@1" ? "代码智能体评测集" : "通用智能体评测集";
  };
  const secondaryActionClass = "inline-flex h-9 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-200 bg-white px-2.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-45";
  return <div className="workspace-page-container">
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-xl font-semibold text-gray-900">评测集</h1>
        <p className="mt-1 text-sm text-gray-500">查看和管理本地评测集；LangSmith 仅作为可选投影。</p>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        <button onClick={load} className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-600 transition hover:border-slate-300 hover:bg-slate-50" title="刷新列表"><RefreshCw className="h-4 w-4" /></button>
        <button onClick={downloadCsvTemplate} className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50" title="下载可批量填写的最简 CSV"><FileDown className="h-4 w-4"/>CSV 模板</button>
        <label className="inline-flex h-9 shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-lg border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 transition hover:border-slate-300 hover:bg-slate-50">
          {busy === "import" ? <Loader2 className="h-4 w-4 animate-spin"/> : <Upload className="h-4 w-4"/>}
          {busy === "import" ? "导入中…" : "批量导入"}
          <input type="file" accept=".json,.bundle,.jsonl,.csv" className="hidden" disabled={busy === "import"} onChange={async(e)=>{const file=e.target.files?.[0];if(!file)return;const format=file.name.endsWith(".csv")?"csv":file.name.endsWith(".jsonl")?"jsonl":"bundle";setBusy("import");try{await importEvaluationDataset(await file.text(),format);await load();}catch(error){setError(error instanceof Error?error.message:"导入失败");}finally{setBusy(null);e.target.value="";}}}/>
        </label>
        <button onClick={openCreateModal} className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-[#002fa7] px-3.5 text-sm font-semibold text-white shadow-sm transition hover:bg-[#00257f]"><Plus className="h-4 w-4"/>新建</button>
      </div>
    </div>
    {error && <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
    {loading ? <div className="flex justify-center p-12"><Loader2 className="animate-spin"/></div> : <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="min-w-max"><div className={`${datasetTableGridClass} items-center border-b border-slate-200 bg-slate-50/80 px-5 py-3 text-xs font-medium text-slate-500`}><span>名称</span><span>版本</span><span>状态</span><span>用例数</span><span>操作</span></div>
      {items.map((dataset) => { const allowed = datasetActions(dataset); const key = dataset.dataset_id; const status = statusPresentation(dataset.status); const rowBusy = busy === key; return <div key={key} className={`${datasetTableGridClass} items-center border-b border-slate-100 px-5 py-4 text-sm transition last:border-b-0 hover:bg-slate-50/50`}>
        <Link href={`/evaluation/datasets/${encodeURIComponent(key)}`} className="group min-w-0 pr-5"><div className="truncate whitespace-nowrap font-semibold text-slate-900 transition group-hover:text-[#002fa7]">{dataset.name}</div><div className="mt-1 flex min-w-0 flex-nowrap items-center gap-2 text-xs text-slate-400"><span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 font-medium text-slate-600">{datasetKindLabel(dataset)}</span><span className="truncate whitespace-nowrap" title={datasetDescription(dataset)}>{datasetDescription(dataset)}</span></div></Link>
        <span className="w-fit rounded-md bg-slate-100 px-2 py-1 font-mono text-xs font-semibold text-slate-600">v{dataset.current_version}</span><span className={`inline-flex w-fit items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${status.className}`}><span className={`h-1.5 w-1.5 rounded-full ${status.dot}`}/>{status.label}</span><span className="font-medium text-slate-700">{dataset.cases.length}</span>
        <div className="flex flex-nowrap items-center justify-start gap-2 whitespace-nowrap">
          {allowed.publishable && <button disabled={rowBusy} onClick={() => act(key, () => publishEvaluationDataset(key, dataset.revision))} className="inline-flex h-9 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg bg-[#002fa7] px-3.5 text-xs font-semibold text-white shadow-sm transition hover:bg-[#00257f] disabled:cursor-not-allowed disabled:opacity-45">{rowBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin"/> : <CloudUpload className="h-3.5 w-3.5"/>}冻结并发布</button>}
          {allowed.syncable && <Link href={`/evaluation/experiments/new?dataset=${encodeURIComponent(`${key}@${dataset.current_version}`)}`} className="inline-flex h-9 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg bg-[#002fa7] px-3.5 text-xs font-semibold text-white shadow-sm transition hover:bg-[#00257f]"><Play className="h-3.5 w-3.5 fill-current"/>开始评测</Link>}
          {allowed.reopenable && <button disabled={rowBusy} onClick={() => act(key, () => reopenEvaluationDataset(key, dataset.revision))} className={secondaryActionClass}><FilePlus2 className="h-3.5 w-3.5"/>新建草稿</button>}
          {allowed.syncable && <button disabled={rowBusy} onClick={() => act(key, () => syncEvaluationDataset(key, dataset.current_version))} className={secondaryActionClass}><CloudUpload className="h-3.5 w-3.5"/>同步 LangSmith</button>}
          <a href={evaluationDatasetExportUrl(key)} className={secondaryActionClass} title="导出完整 PuddingClaw 评测集"><Download className="h-3.5 w-3.5"/>导出</a>
          {dataset.tags.includes("swebench") && dataset.current_version > 0 && <a href={frozenSWEbenchDatasetExportUrl(key, dataset.current_version)} className={secondaryActionClass} title="导出官方判卷器使用的冻结数据快照"><FileCode2 className="h-3.5 w-3.5"/>判卷快照</a>}
          {allowed.archivable && <button aria-label="归档评测集" disabled={rowBusy} onClick={() => confirm("归档后评测集将不可编辑，确认归档？") && act(key, () => archiveEvaluationDataset(key, dataset.revision))} className="inline-flex h-9 shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border border-rose-200 bg-white px-2.5 text-xs font-medium text-rose-600 shadow-sm transition hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"><Archive className="h-3.5 w-3.5"/>归档</button>}
        </div>
      </div>; })}
      {!items.length && <div className="p-12 text-center text-sm text-gray-400">还没有评测集，点击右上角“新建”开始创建</div>}
      </div></div>}
    {createModalOpen && (
      <div className="fixed inset-0 z-[120] flex items-center justify-center bg-slate-950/35 px-4 py-6 backdrop-blur-[2px]" onMouseDown={() => busy !== "create" && busy !== "swebench" && setCreateModalOpen(false)}>
        <form className="flex max-h-[90vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl ring-1 ring-black/[0.06]" onMouseDown={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void submitCreate(); }}>
          <div className="flex shrink-0 items-start justify-between gap-4 border-b border-slate-100 px-6 py-5">
            <div>
              <h2 className="text-lg font-semibold text-slate-950">新建评测集</h2>
              <p className="mt-1 text-sm text-slate-500">选择评测类型和数据来源，创建后再进入详情维护用例。</p>
            </div>
            <button type="button" disabled={busy === "create" || busy === "swebench"} onClick={() => setCreateModalOpen(false)} className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-700 disabled:opacity-40" aria-label="关闭新建评测集弹窗"><X className="h-4 w-4"/></button>
          </div>

          <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-6 py-5">
            <label className="block">
              <span className="text-sm font-medium text-slate-800">评测集名称</span>
              <input autoFocus value={name} onChange={(event) => { setName(event.target.value); setModalError(""); }} placeholder={datasetKind === "coding" && codingSource === "swebench" ? "例如：SWE-bench Verified (50)" : "输入一个便于识别的名称"} className="mt-2 h-10 w-full rounded-xl border border-slate-200 px-3 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-[#002fa7] focus:ring-2 focus:ring-blue-100"/>
            </label>

            <fieldset>
              <legend className="text-sm font-medium text-slate-800">评测类型</legend>
              <div className="mt-2 grid gap-3 sm:grid-cols-2">
                <button type="button" onClick={() => { setDatasetKind("general"); setCodingSource("blank"); setModalError(""); }} className={`flex min-h-20 items-start gap-3 rounded-xl border p-3 text-left transition ${datasetKind === "general" ? "border-[#002fa7] bg-blue-50/60 ring-1 ring-[#002fa7]" : "border-slate-200 hover:border-slate-300 hover:bg-slate-50"}`}>
                  <span className={`mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${datasetKind === "general" ? "bg-[#002fa7] text-white" : "bg-slate-100 text-slate-500"}`}><FilePlus2 className="h-4 w-4"/></span>
                  <span><span className="block text-sm font-semibold text-slate-900">通用评测</span><span className="mt-1 block text-xs leading-5 text-slate-500">问答、工具调用和七维通用评估</span></span>
                </button>
                <button type="button" onClick={() => { setDatasetKind("coding"); setModalError(""); }} className={`flex min-h-20 items-start gap-3 rounded-xl border p-3 text-left transition ${datasetKind === "coding" ? "border-[#002fa7] bg-blue-50/60 ring-1 ring-[#002fa7]" : "border-slate-200 hover:border-slate-300 hover:bg-slate-50"}`}>
                  <span className={`mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${datasetKind === "coding" ? "bg-[#002fa7] text-white" : "bg-slate-100 text-slate-500"}`}><Code2 className="h-4 w-4"/></span>
                  <span><span className="block text-sm font-semibold text-slate-900">代码评测</span><span className="mt-1 block text-xs leading-5 text-slate-500">工作区工具、代码补丁与隔离验证</span></span>
                </button>
              </div>
            </fieldset>

            {datasetKind === "coding" && (
              <fieldset>
                <legend className="text-sm font-medium text-slate-800">数据来源</legend>
                <div className="mt-2 overflow-hidden rounded-xl border border-slate-200">
                  <label className={`flex cursor-pointer items-start gap-3 border-b border-slate-100 px-4 py-3 transition ${codingSource === "blank" ? "bg-blue-50/50" : "hover:bg-slate-50"}`}>
                    <input type="radio" name="coding-source" value="blank" checked={codingSource === "blank"} onChange={() => { setCodingSource("blank"); setModalError(""); }} className="mt-1 accent-[#002fa7]"/>
                    <span><span className="block text-sm font-medium text-slate-900">空白代码评测集</span><span className="mt-0.5 block text-xs leading-5 text-slate-500">创建后手动添加任务说明、初始代码和隐藏测试。</span></span>
                  </label>
                  <label className={`flex cursor-pointer items-start gap-3 px-4 py-3 transition ${codingSource === "swebench" ? "bg-blue-50/50" : "hover:bg-slate-50"}`}>
                    <input type="radio" name="coding-source" value="swebench" checked={codingSource === "swebench"} onChange={() => { setCodingSource("swebench"); if (!name.trim()) setName(`SWE-bench Verified (${sweLimit})`); setModalError(""); }} className="mt-1 accent-[#002fa7]"/>
                    <span><span className="block text-sm font-medium text-slate-900">SWE-bench Verified</span><span className="mt-0.5 block text-xs leading-5 text-slate-500">从官方数据集导入；智能体生成补丁后由官方 Docker 判卷器判卷。</span></span>
                  </label>
                </div>
              </fieldset>
            )}

            {datasetKind === "coding" && codingSource === "swebench" && (
              <label className="block">
                <span className="text-sm font-medium text-slate-800">导入用例数量</span>
                <div className="mt-2 flex items-center gap-3">
                  <input type="number" min={1} max={500} value={sweLimit} onChange={(event) => { const nextLimit = Math.max(1, Math.min(500, Number(event.target.value) || 1)); if (/^SWE-bench Verified \(\d+\)$/.test(name)) setName(`SWE-bench Verified (${nextLimit})`); setSweLimit(nextLimit); }} className="h-10 w-28 rounded-xl border border-slate-200 px-3 text-sm outline-none transition focus:border-[#002fa7] focus:ring-2 focus:ring-blue-100"/>
                  <span className="whitespace-nowrap text-sm text-slate-500">个用例，支持 1–500</span>
                </div>
              </label>
            )}

            {modalError && <div className="rounded-xl bg-rose-50 px-3 py-2.5 text-sm text-rose-700">{modalError}</div>}
          </div>

          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-slate-100 bg-slate-50/70 px-6 py-4">
            <button type="button" disabled={busy === "create" || busy === "swebench"} onClick={() => setCreateModalOpen(false)} className="inline-flex h-9 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:opacity-40">取消</button>
            <button type="submit" disabled={busy === "create" || busy === "swebench"} className="inline-flex h-9 min-w-28 items-center justify-center gap-1.5 rounded-lg bg-[#002fa7] px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-[#00257f] disabled:cursor-not-allowed disabled:opacity-50">
              {busy === "create" || busy === "swebench" ? <Loader2 className="h-4 w-4 animate-spin"/> : datasetKind === "coding" && codingSource === "swebench" ? <Download className="h-4 w-4"/> : <Plus className="h-4 w-4"/>}
              {busy === "swebench" ? "导入中…" : busy === "create" ? "创建中…" : datasetKind === "coding" && codingSource === "swebench" ? "导入并创建" : "创建评测集"}
            </button>
          </div>
        </form>
      </div>
    )}
  </div>;
}
