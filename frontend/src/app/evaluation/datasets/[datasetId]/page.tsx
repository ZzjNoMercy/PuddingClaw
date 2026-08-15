"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, CheckCircle2, Copy, Loader2, Plus, Save, Trash2 } from "lucide-react";
import {
  addEvaluationCase,
  deleteEvaluationCase,
  getEvaluationDataset,
  publishEvaluationDataset,
  updateEvaluationCase,
  validateEvaluationDataset,
  type DatasetValidation,
  type EvalCase,
  type EvalDataset,
} from "@/lib/evaluationApi";

function newCase(): EvalCase {
  const now = new Date().toISOString();
  const id = crypto.randomUUID().replaceAll("-", "");
  return {
    protocol_version: "1.0", case_id: `case_${id}`, revision_id: `rev_${id}`,
    name: "新用例", description: "", enabled: true, repetitions: 1, dimensions: [],
    input: { message: "", turns: [] },
    setup: { timezone: "Asia/Shanghai", fixtures: [], allow_network: false, allow_side_effects: false, reproducible: true },
    expectations: { contains_all: [], contains_any: [], excludes: [], required_tools: [], forbidden_tools: [], tool_order: [], required_steps: [], forbidden_actions: [], expected_state: {} },
    evaluator_bindings: [], resolved_evaluator_bindings: [], criticality: "normal",
    data_classification: "internal", tags: [], metadata: {}, created_at: now, updated_at: now,
  };
}

function newCodeCase(): EvalCase {
  const base = newCase();
  return {
    ...base,
    name: "新代码用例",
    input: { message: "实现 solution.py 中的 add(a, b)，使隐藏测试通过。", turns: [] },
    dimensions: ["task_completion", "tool_use", "trajectory", "safety", "robustness"],
    tags: ["coding"],
    code: {
      schema_version: "1",
      repository: {
        kind: "inline",
        files: { "solution.py": "def add(a, b):\n    raise NotImplementedError\n" },
        swebench: null,
      },
      verification: {
        mode: "commands",
        commands: [{ command_id: "hidden-tests", command: "hidden_cases.json", runner: "python_callable_json", timeout_seconds: 120, expected_exit_code: 0 }],
        hidden_files: { "hidden_cases.json": "{\n  \"callable\": \"solution:add\",\n  \"cases\": [\n    {\"args\": [2, 3], \"expected\": 5},\n    {\"args\": [-1, 1], \"expected\": 0}\n  ]\n}\n" },
        require_patch: true,
      },
    },
  };
}

function copyCase(source: EvalCase): EvalCase {
  const now = new Date().toISOString();
  const id = crypto.randomUUID().replaceAll("-", "");
  return {
    ...structuredClone(source),
    case_id: `case_${id}`,
    revision_id: `rev_${id}`,
    name: `${source.name} - 副本`,
    resolved_evaluator_bindings: [],
    created_at: now,
    updated_at: now,
  };
}

const split = (value: string) => value.split("\n").map((item) => item.trim()).filter(Boolean);

export default function DatasetDetailPage() {
  const id = String(useParams().datasetId || "");
  const [dataset, setDataset] = useState<EvalDataset | null>(null);
  const [selected, setSelected] = useState<EvalCase | null>(null);
  const [validation, setValidation] = useState<DatasetValidation | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await getEvaluationDataset(id);
      setDataset(next);
      setSelected(next.cases[0] || null);
      setDirty(false);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const editable = dataset?.status === "draft";
  const change = (next: EvalCase) => { setSelected(next); setDirty(true); setValidation(null); };
  const choose = (next: EvalCase) => {
    if (dirty && !window.confirm("当前用例有未保存修改，确定放弃吗？")) return;
    setSelected(next); setDirty(false); setValidation(null); setError("");
  };
  const save = async () => {
    if (!dataset || !selected) return;
    setBusy(true);
    try {
      const exists = dataset.cases.some((item) => item.case_id === selected.case_id);
      const next = exists
        ? await updateEvaluationCase(id, selected.case_id, dataset.revision, selected)
        : await addEvaluationCase(id, dataset.revision, selected);
      setDataset(next);
      setSelected(next.cases.find((item) => item.case_id === selected.case_id) || null);
      setDirty(false); setValidation(null); setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  };
  const precheck = async () => {
    if (dirty) { setError("请先保存当前用例，再执行预检查。"); return; }
    setBusy(true);
    try { setValidation(await validateEvaluationDataset(id)); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "预检查失败"); }
    finally { setBusy(false); }
  };
  const publish = async () => {
    if (!dataset) return;
    if (dirty) { setError("请先保存当前用例，再发布版本。"); return; }
    setBusy(true);
    try { await publishEvaluationDataset(id, dataset.revision); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "发布失败"); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!dataset || !selected || !dataset.cases.some((item) => item.case_id === selected.case_id)) return;
    if (!window.confirm("删除这个用例？")) return;
    setBusy(true);
    try { await deleteEvaluationCase(id, selected.case_id, dataset.revision); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : "删除失败"); }
    finally { setBusy(false); }
  };
  const tools = useMemo(() => selected?.expectations.required_tools.join("\n") || "", [selected]);

  if (loading) return <div className="flex h-full items-center justify-center"><Loader2 className="animate-spin" /></div>;
  if (!dataset) return <div className="p-8 text-sm text-red-700">{error || "评测集不存在"}</div>;

  return <div className="grid min-h-full grid-cols-[280px_minmax(0,1fr)]">
    <aside className="border-r bg-gray-50/70 p-4"><Link href="/evaluation/datasets" onClick={(event) => { if (dirty && !window.confirm("当前用例有未保存修改，确定返回评测集吗？")) event.preventDefault(); }} className="mb-4 inline-flex items-center gap-1.5 text-xs font-medium text-[#002fa7] hover:underline"><ArrowLeft className="h-3.5 w-3.5" />返回评测集</Link><div className="mb-3"><h1 className="truncate whitespace-nowrap font-semibold">{dataset.name}</h1><p className="whitespace-nowrap text-xs text-gray-500">{dataset.status === "published" ? "已发布" : dataset.status === "draft" ? "草稿" : "已归档"} · v{dataset.current_version} · 修订 {dataset.revision}</p></div>
      {editable && <div className="mb-3 grid grid-cols-2 gap-2"><button disabled={busy} onClick={() => choose(dataset.default_profile === "coding_agent@1" ? newCodeCase() : newCase())} className="flex items-center justify-center gap-1 rounded-lg border bg-white py-2 text-xs"><Plus className="h-3.5 w-3.5" />{dataset.default_profile === "coding_agent@1" ? "新增代码题" : "新增空白"}</button><button disabled={busy || !selected} onClick={() => selected && choose(copyCase(selected))} className="flex items-center justify-center gap-1 rounded-lg border bg-white py-2 text-xs disabled:opacity-40"><Copy className="h-3.5 w-3.5" />复制当前</button></div>}
      <div className="space-y-1">{dataset.cases.map((item) => <button key={item.case_id} onClick={() => choose(item)} className={`w-full rounded-lg px-3 py-2 text-left text-sm ${selected?.case_id === item.case_id ? "bg-[#002fa7] text-white" : "hover:bg-white"}`}><div className="truncate">{item.name}</div><div className="truncate text-[11px] opacity-60">{item.criticality} · {item.data_classification}</div></button>)}</div>
    </aside>
    <section className="p-6">{error && <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      <div className="mb-5 flex justify-between gap-4"><div className="min-w-0"><h2 className="truncate whitespace-nowrap text-lg font-semibold">{selected?.name || "选择一个用例"}{dirty && <span className="ml-2 text-xs text-amber-600">未保存</span>}</h2><p className="truncate whitespace-nowrap text-xs text-gray-500">{dataset.default_profile === "coding_agent@1" ? "代码用例使用隔离工作区与执行工具；隐藏测试不会暴露给智能体。" : "第一阶段使用隔离工作区核心能力集；已发布版本只读。"}</p></div><div className="flex shrink-0 gap-2"><button disabled={busy} onClick={precheck} className="whitespace-nowrap rounded-lg border px-3 py-2 text-sm disabled:opacity-50">预检查</button>{editable && dataset.cases.length > 0 && <button disabled={busy} onClick={publish} className="whitespace-nowrap rounded-lg bg-emerald-600 px-3 py-2 text-sm text-white disabled:opacity-50">发布版本</button>}</div></div>
      {validation && <div className={`mb-5 rounded-xl border p-4 ${validation.valid ? "border-emerald-200 bg-emerald-50" : "border-red-200 bg-red-50"}`}><div className="flex items-center gap-2 text-sm font-medium"><CheckCircle2 className="h-4 w-4" />{validation.valid ? "可以发布" : "需要修正"}</div>{validation.issues.map((issue, index) => <p key={index} className="mt-1 text-xs">{issue.severity === "error" ? "错误" : issue.severity === "warning" ? "警告" : "提示"}：{issue.message}</p>)}</div>}
      {selected && <><div className="mb-3 max-w-4xl rounded-lg bg-blue-50 px-4 py-3 text-xs text-blue-800">{selected.code ? "填写任务说明、初始源码、隐藏测试和验证命令即可；智能体只能看到初始源码，评分只认隔离验证结果。" : "最少只需要填写“名称、输入、回答必须包含”三项；其余字段可以保持默认。"}</div><div className="grid max-w-4xl grid-cols-2 gap-4 rounded-xl border bg-white p-5">
        <label className="text-xs text-gray-500">名称<input disabled={!editable} value={selected.name} onChange={(e) => change({...selected, name: e.target.value})} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-900 disabled:bg-gray-50" /></label>
        <label className="text-xs text-gray-500">等级<select disabled={!editable} value={selected.criticality} onChange={(e) => change({...selected, criticality: e.target.value as EvalCase["criticality"]})} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-900"><option value="normal">普通</option><option value="high">高</option><option value="critical">关键</option></select></label>
        <div className="col-span-2"><div className="mb-2 flex items-center justify-between"><span className="text-xs text-gray-500">输入</span><select disabled={!editable} value={selected.input.turns.length ? "multi" : "single"} onChange={(e) => change({...selected, input: e.target.value === "multi" ? {message: null, turns: [{role: "user", content: selected.input.message || ""}]} : {message: selected.input.turns.find((turn) => turn.role === "user")?.content || "", turns: []}})} className="rounded-md border px-2 py-1 text-xs"><option value="single">单轮</option><option value="multi">多轮</option></select></div>
          {!selected.input.turns.length ? <textarea disabled={!editable} value={selected.input.message || ""} onChange={(e) => change({...selected, input: {message: e.target.value, turns: []}})} rows={4} className="w-full rounded-lg border px-3 py-2 text-sm text-gray-900 disabled:bg-gray-50" /> : <div className="space-y-2">{selected.input.turns.map((turn, index) => <div key={index} className="grid grid-cols-[110px_minmax(0,1fr)_32px] gap-2"><select disabled={!editable} value={turn.role} onChange={(e) => { const turns = [...selected.input.turns]; turns[index] = {...turn, role: e.target.value as "user" | "assistant"}; change({...selected, input: {message: null, turns}}); }} className="rounded-lg border px-2 text-sm"><option value="user">用户</option><option value="assistant">智能体</option></select><textarea disabled={!editable} value={turn.content} onChange={(e) => { const turns = [...selected.input.turns]; turns[index] = {...turn, content: e.target.value}; change({...selected, input: {message: null, turns}}); }} rows={2} className="rounded-lg border px-3 py-2 text-sm" /><button disabled={!editable || selected.input.turns.length <= 1} onClick={() => change({...selected, input: {message: null, turns: selected.input.turns.filter((_, i) => i !== index)}})} className="text-gray-400">×</button></div>)}{editable && <button onClick={() => change({...selected, input: {message: null, turns: [...selected.input.turns, {role: "user", content: ""}]}})} className="text-xs text-[#002fa7]">+ 添加一轮</button>}</div>}
        </div>
        {!selected.code && <label className="col-span-2 text-xs text-gray-500">回答必须包含（每行一项）<textarea disabled={!editable} value={selected.expectations.contains_all.join("\n")} onChange={(e) => change({...selected, expectations: {...selected.expectations, contains_all: split(e.target.value)}})} rows={3} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-900 disabled:bg-gray-50" /></label>}
        {selected.code?.repository.kind === "inline" && <>
          <label className="col-span-2 text-xs text-gray-500">初始源码（solution.py）<textarea disabled={!editable} value={selected.code.repository.files["solution.py"] || ""} onChange={(e) => change({...selected, code: {...selected.code!, repository: {...selected.code!.repository, files: {...selected.code!.repository.files, "solution.py": e.target.value}}}})} rows={10} className="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm text-gray-900 disabled:bg-gray-50" /></label>
          <label className="col-span-2 text-xs text-gray-500">隐藏调用用例（hidden_cases.json，不进入智能体工作区）<textarea disabled={!editable} value={selected.code.verification.hidden_files["hidden_cases.json"] || ""} onChange={(e) => change({...selected, code: {...selected.code!, verification: {...selected.code!.verification, hidden_files: {...selected.code!.verification.hidden_files, "hidden_cases.json": e.target.value}}}})} rows={10} className="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm text-gray-900 disabled:bg-gray-50" /></label>
          <label className="col-span-2 text-xs text-gray-500">隐藏用例入口（可信父进程比对 expected）<input disabled={!editable} value={selected.code.verification.commands[0]?.command || ""} onChange={(e) => { const current = selected.code!.verification.commands[0] || {command_id: "hidden-tests", runner: "python_callable_json" as const, timeout_seconds: 120, expected_exit_code: 0, command: ""}; change({...selected, code: {...selected.code!, verification: {...selected.code!.verification, commands: [{...current, command: e.target.value}]}}}); }} className="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm text-gray-900 disabled:bg-gray-50" /></label>
        </>}
        {selected.code?.repository.kind === "swebench" && <div className="col-span-2 rounded-lg border bg-gray-50 p-3 text-xs text-gray-600">SWE-bench · {selected.code.repository.swebench?.instance_id} · {selected.code.repository.swebench?.repo}@{selected.code.repository.swebench?.base_commit.slice(0, 12)}。标准答案补丁已剔除；评测会自动使用官方 Docker 判卷器验证智能体生成的补丁。</div>}
        <label className="text-xs text-gray-500">必须调用工具（每行一项）<textarea disabled={!editable} value={tools} onChange={(e) => change({...selected, expectations: {...selected.expectations, required_tools: split(e.target.value)}})} rows={3} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-900 disabled:bg-gray-50" /></label>
        <label className="text-xs text-gray-500">禁止调用工具（每行一项）<textarea disabled={!editable} value={selected.expectations.forbidden_tools.join("\n")} onChange={(e) => change({...selected, expectations: {...selected.expectations, forbidden_tools: split(e.target.value)}})} rows={3} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-900 disabled:bg-gray-50" /></label>
        <label className="text-xs text-gray-500">固定时间（协议预留；第一阶段发布会拒绝）<input disabled={!editable} type="text" placeholder="2026-08-03T09:00:00+08:00" value={selected.setup.clock || ""} onChange={(e) => change({...selected, setup: {...selected.setup, clock: e.target.value || null}})} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-900" /></label>
        <label className="text-xs text-gray-500">数据分级<select disabled={!editable} value={selected.data_classification} onChange={(e) => change({...selected, data_classification: e.target.value as EvalCase["data_classification"]})} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm text-gray-900"><option value="public">公开</option><option value="internal">内部</option><option value="sensitive">敏感</option><option value="restricted">受限</option></select></label>
        {editable && <div className="col-span-2 flex justify-between border-t pt-4"><button disabled={busy} onClick={remove} className="flex items-center gap-2 text-sm text-red-600 disabled:opacity-50"><Trash2 className="h-4 w-4" />删除</button><button disabled={busy || (!selected.input.message?.trim() && !selected.input.turns.some((turn) => turn.role === "user" && turn.content.trim()))} onClick={save} className="flex items-center gap-2 rounded-lg bg-[#002fa7] px-4 py-2 text-sm text-white disabled:opacity-40">{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}保存用例</button></div>}
      </div></>}
    </section>
  </div>;
}
