"use client";

import { useEffect, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, History, Loader2, RotateCcw, Search, Terminal } from "lucide-react";
import CapabilitiesStatus from "@/components/settings/CapabilitiesStatus";
import SettingsAnchorLayout, { type SettingsAnchorSection } from "@/components/settings/SettingsAnchorLayout";
import {
  listHeadlessActivityLogs,
  type HeadlessActivityLogFilters,
  type HeadlessActivityLogPage,
} from "@/lib/headlessActivityApi";
import type { RuntimeExtensions } from "@/lib/useRuntimeProfile";

const EMPTY_PAGE: HeadlessActivityLogPage = {
  items: [], page: 1, page_size: 10, total: 0, total_pages: 0, source_names: [], timezone: "Asia/Shanghai",
};

const SECTIONS: SettingsAnchorSection[] = [
  { id: "cli", label: "CLI 探测", description: "本机命令行客户端状态", icon: Terminal },
  { id: "logs", label: "调用日志", description: "本机 Headless 请求记录", icon: History },
];

function beijingBoundary(date: string, end = false): number | undefined {
  if (!date) return undefined;
  const value = Date.parse(`${date}T${end ? "23:59:59.999" : "00:00:00.000"}+08:00`);
  return Number.isFinite(value) ? value / 1000 : undefined;
}

export default function HeadlessActivityPanel({ extensions }: { extensions: RuntimeExtensions | null }) {
  const [logs, setLogs] = useState(EMPTY_PAGE);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [query, setQuery] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [filters, setFilters] = useState<HeadlessActivityLogFilters>({});
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setMessage("");
    void listHeadlessActivityLogs({ ...filters, page })
      .then((result) => { if (!cancelled) setLogs(result); })
      .catch((error) => { if (!cancelled) setMessage(error instanceof Error ? error.message : "加载调用日志失败"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [filters, page, revision]);

  const apply = () => {
    const startAt = beijingBoundary(startDate);
    const endAt = beijingBoundary(endDate, true);
    if (startAt !== undefined && endAt !== undefined && startAt > endAt) {
      setMessage("开始日期不能晚于结束日期");
      return;
    }
    setPage(1);
    setFilters({ sourceName: sourceName || undefined, query: query.trim() || undefined, startAt, endAt });
    setRevision((value) => value + 1);
  };

  const reset = () => {
    setSourceName(""); setQuery(""); setStartDate(""); setEndDate(""); setMessage(""); setPage(1); setFilters({});
    setRevision((value) => value + 1);
  };
  const totalPages = Math.max(1, logs.total_pages);

  return (
    <SettingsAnchorLayout prefix="worker" sections={SECTIONS}>
      <section id="worker-section-cli" className="scroll-mt-6">
        <Card title="本机 CLI" icon={Terminal}>
          <p className="mb-4 text-xs leading-5 text-gray-500">PuddingClaw CLI 仅连接本机回环 Backend，不需要 Worker Token。模型、数据源和工具审批由 PuddingClaw 管理。</p>
          <CapabilitiesStatus refreshIntervalMs={30000} extensions={extensions} includeKeys={["cli"]} showSummary={false} />
        </Card>
      </section>
      <section id="worker-section-logs" className="scroll-mt-6">
        <Card title="调用日志" icon={History}>
          <div className="mb-4 flex items-end justify-between gap-3"><p className="text-xs text-gray-500">记录本机 CLI 发起的 Headless Run，请求时间按北京时间展示。</p><p className="text-[11px] text-gray-400">共 {logs.total} 条 · 每页 10 条</p></div>
          <div className="grid gap-3 rounded-xl bg-gray-50 p-3 md:grid-cols-2 2xl:grid-cols-[150px_150px_180px_1fr_auto_auto]">
            <DateField label="开始日期" value={startDate} onChange={setStartDate} />
            <DateField label="结束日期" value={endDate} onChange={setEndDate} />
            <label className="text-[11px] font-medium text-gray-500"><span className="mb-1.5 block">调用方</span><select value={sourceName} onChange={(event) => setSourceName(event.target.value)} className="form-input !py-2 text-xs"><option value="">全部调用方</option>{logs.source_names.map((name) => <option key={name} value={name}>{name}</option>)}</select></label>
            <label className="text-[11px] font-medium text-gray-500"><span className="mb-1.5 block">Query 关键词</span><input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") apply(); }} placeholder="搜索请求内容" className="form-input !py-2 text-xs" /></label>
            <button type="button" onClick={apply} className="inline-flex h-[34px] items-center justify-center gap-1.5 self-end rounded-lg bg-[#002fa7] px-3 text-xs font-medium text-white"><Search className="h-3.5 w-3.5" />筛选</button>
            <button type="button" onClick={reset} className="inline-flex h-[34px] items-center justify-center gap-1.5 self-end rounded-lg border border-gray-200 bg-white px-3 text-xs font-medium text-gray-600"><RotateCcw className="h-3.5 w-3.5" />重置</button>
          </div>
          {message && <p className="mt-3 text-xs text-rose-600">{message}</p>}
          <div className="mt-4 overflow-hidden rounded-xl border border-gray-200">
            <div className="overflow-x-auto"><table className="w-full min-w-[620px] table-fixed text-left"><colgroup><col className="w-[165px]" /><col className="w-[150px]" /><col /></colgroup><thead className="bg-gray-50 text-[11px] font-semibold text-gray-500"><tr><th className="px-4 py-3">时间（北京时间）</th><th className="px-4 py-3">调用方</th><th className="px-4 py-3">Query</th></tr></thead><tbody className="divide-y divide-gray-100 bg-white text-xs text-gray-700">
              {loading ? <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-400"><span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" />正在加载…</span></td></tr> : logs.items.length === 0 ? <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-400">暂无符合条件的调用日志</td></tr> : logs.items.map((item) => <tr key={item.id} className="align-top hover:bg-gray-50/70"><td className="whitespace-nowrap px-4 py-3 font-mono text-[11px] text-gray-500">{item.created_at_beijing}</td><td className="px-4 py-3 font-medium text-gray-800">{item.source_name}</td><td className="px-4 py-3"><p className="line-clamp-2 whitespace-pre-wrap break-words leading-5" title={item.query}>{item.query}</p></td></tr>)}
            </tbody></table></div>
            <div className="flex min-h-12 items-center justify-between border-t border-gray-100 px-4 py-2"><p className="text-[11px] text-gray-400">第 {logs.total ? logs.page : 0} / {logs.total ? totalPages : 0} 页</p><div className="flex gap-2"><PageButton label="上一页" disabled={loading || page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}><ChevronLeft className="h-4 w-4" /></PageButton><PageButton label="下一页" disabled={loading || !logs.total || page >= totalPages} onClick={() => setPage((value) => value + 1)}><ChevronRight className="h-4 w-4" /></PageButton></div></div>
          </div>
        </Card>
      </section>
    </SettingsAnchorLayout>
  );
}

function Card({ title, icon: Icon, children }: { title: string; icon: React.ElementType; children: React.ReactNode }) { return <section className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm"><div className="mb-5 flex items-center gap-2 text-[15px] font-semibold text-gray-800"><Icon className="h-4 w-4 text-[#002fa7]" />{title}</div>{children}</section>; }
function DateField({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) { return <label className="text-[11px] font-medium text-gray-500"><span className="mb-1.5 flex items-center gap-1"><CalendarDays className="h-3 w-3" />{label}</span><input type="date" value={value} onChange={(event) => onChange(event.target.value)} className="form-input !py-2 text-xs" /></label>; }
function PageButton({ label, disabled, onClick, children }: { label: string; disabled: boolean; onClick: () => void; children: React.ReactNode }) { return <button type="button" aria-label={label} disabled={disabled} onClick={onClick} className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-gray-200 text-gray-600 disabled:opacity-35">{children}</button>; }
