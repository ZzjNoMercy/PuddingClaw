"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Copy,
  History,
  KeyRound,
  Loader2,
  RefreshCw,
  RotateCcw,
  Search,
  Shield,
  Trash2,
} from "lucide-react";
import {
  createWorkerAccessKey,
  listWorkerAccessKeys,
  listWorkerAccessLogs,
  revokeWorkerAccessKey,
  rotateWorkerAccessKey,
  type WorkerAccessKey,
  type WorkerAccessKeySecret,
  type WorkerAccessLogFilters,
  type WorkerAccessLogPage,
} from "@/lib/workerAccessApi";

const EMPTY_LOG_PAGE: WorkerAccessLogPage = {
  items: [],
  page: 1,
  page_size: 10,
  total: 0,
  total_pages: 0,
  key_names: [],
  timezone: "Asia/Shanghai",
};

function beijingDateBoundary(date: string, endOfDay = false): number | undefined {
  if (!date) return undefined;
  const suffix = endOfDay ? "23:59:59.999" : "00:00:00.000";
  const timestamp = Date.parse(`${date}T${suffix}+08:00`);
  return Number.isFinite(timestamp) ? timestamp / 1000 : undefined;
}

export default function WorkerAccessKeysPanel() {
  const [keys, setKeys] = useState<WorkerAccessKey[]>([]);
  const [name, setName] = useState("");
  const [profile, setProfile] = useState("smart");
  const [secret, setSecret] = useState<WorkerAccessKeySecret | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [logs, setLogs] = useState<WorkerAccessLogPage>(EMPTY_LOG_PAGE);
  const [logPage, setLogPage] = useState(1);
  const [logLoading, setLogLoading] = useState(false);
  const [logMessage, setLogMessage] = useState("");
  const [keyNameFilter, setKeyNameFilter] = useState("");
  const [queryFilter, setQueryFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [appliedLogFilters, setAppliedLogFilters] = useState<WorkerAccessLogFilters>({});
  const [logRefreshToken, setLogRefreshToken] = useState(0);
  const nameInputRef = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    setBusy(true);
    setMessage("");
    try {
      setKeys((await listWorkerAccessKeys()).keys);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "加载 Worker Key 失败");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLogLoading(true);
    setLogMessage("");
    void listWorkerAccessLogs({ ...appliedLogFilters, page: logPage })
      .then((result) => {
        if (!cancelled) setLogs(result);
      })
      .catch((error) => {
        if (!cancelled) setLogMessage(error instanceof Error ? error.message : "加载调用日志失败");
      })
      .finally(() => {
        if (!cancelled) setLogLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [appliedLogFilters, logPage, logRefreshToken]);

  const create = async () => {
    const cleanName = name.trim();
    if (!cleanName) {
      setMessage("请输入 Key 名称");
      nameInputRef.current?.focus();
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const created = await createWorkerAccessKey({
        name: cleanName,
        authority_profile: profile,
        scopes: ["worker:health", "worker:models:read", "worker:runs:create", "worker:runs:read", "worker:runs:cancel"],
      });
      setName("");
      setSecret(created);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "创建 Worker Key 失败");
    } finally {
      setBusy(false);
    }
  };

  const rotate = async (keyId: string) => {
    setBusy(true);
    setMessage("");
    try {
      setSecret(await rotateWorkerAccessKey(keyId));
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "轮换 Worker Key 失败");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (keyId: string) => {
    setBusy(true);
    setMessage("");
    try {
      await revokeWorkerAccessKey(keyId);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "吊销 Worker Key 失败");
    } finally {
      setBusy(false);
    }
  };

  const copySecret = async () => {
    if (secret?.token) await navigator.clipboard.writeText(secret.token);
  };

  const applyLogFilters = () => {
    const startAt = beijingDateBoundary(startDate);
    const endAt = beijingDateBoundary(endDate, true);
    if (startAt !== undefined && endAt !== undefined && startAt > endAt) {
      setLogMessage("开始日期不能晚于结束日期");
      return;
    }
    setLogMessage("");
    setLogPage(1);
    setAppliedLogFilters({
      keyName: keyNameFilter || undefined,
      query: queryFilter.trim() || undefined,
      startAt,
      endAt,
    });
    setLogRefreshToken((current) => current + 1);
  };

  const resetLogFilters = () => {
    setKeyNameFilter("");
    setQueryFilter("");
    setStartDate("");
    setEndDate("");
    setLogMessage("");
    setLogPage(1);
    setAppliedLogFilters({});
    setLogRefreshToken((current) => current + 1);
  };

  const totalPages = Math.max(1, logs.total_pages);
  const keyNameOptions = useMemo(
    () => Array.from(new Set([...keys.map((item) => item.name), ...logs.key_names])).filter(Boolean).sort(),
    [keys, logs.key_names],
  );

  return (
    <SettingsCard title="Worker 接入" icon={KeyRound} color="#002fa7">
      <div className="space-y-6">
        <div className="space-y-5">
          <p className="text-xs leading-5 text-gray-500">
            本机通过回环地址管理 Worker Access Key，无需额外管理员 Token。Worker Key 供 PuddingTeams CLI 调用 Headless API；后端只保存哈希，明文只在创建或轮换成功后显示一次。远程部署可通过 PUDDINGCLAW_ADMIN_TOKEN 保护管理接口。
          </p>
          <div className="grid gap-3 md:grid-cols-[1fr_180px_auto]">
            <input
              ref={nameInputRef}
              value={name}
              onChange={(event) => {
                setName(event.target.value);
                if (message === "请输入 Key 名称") setMessage("");
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") void create();
              }}
              placeholder="Key 名称，例如 codex / puddingteams"
              aria-label="Key 名称"
              aria-invalid={message === "请输入 Key 名称"}
              className={`form-input ${message === "请输入 Key 名称" ? "!border-rose-300 !ring-2 !ring-rose-100" : ""}`}
            />
            <select value={profile} onChange={(event) => setProfile(event.target.value)} className="form-input">
              <option value="smart">SMART</option>
              <option value="workspace">FULL_ACCESS · workspace</option>
              <option value="workspace_network">FULL_ACCESS · workspace + network</option>
              <option value="workspace_package_install">FULL_ACCESS · workspace + package</option>
            </select>
            <button type="button" onClick={create} disabled={busy} className="inline-flex items-center justify-center gap-2 rounded-xl bg-[#002fa7] px-4 py-2 text-xs font-medium text-white hover:bg-[#001f7a] disabled:opacity-50">
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
              创建 Key
            </button>
          </div>
          {secret && (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-xs text-amber-950">
              <div className="mb-2 flex items-center gap-2 font-semibold"><Shield className="h-4 w-4" />请立即复制，关闭此页面后不再显示</div>
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 break-all rounded-lg bg-white px-3 py-2">{secret.token}</code>
                <button type="button" onClick={copySecret} className="rounded-lg border border-amber-300 bg-white p-2" title="复制"><Copy className="h-3.5 w-3.5" /></button>
              </div>
            </div>
          )}
          {message && <p className="text-xs text-rose-600">{message}</p>}
          <div className="divide-y rounded-xl border border-gray-200">
            {keys.length === 0 ? <p className="p-4 text-xs text-gray-400">暂无 Worker Key</p> : keys.map((item) => (
              <div key={item.key_id} className="flex flex-wrap items-center gap-3 p-4">
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-gray-800">{item.name}</div>
                  <div className="mt-1 text-[11px] text-gray-500"><code>{item.prefix}…</code> · {item.authority_profile} · {item.revoked_at ? "已吊销" : "有效"}</div>
                </div>
                <button type="button" onClick={() => rotate(item.key_id)} disabled={busy || Boolean(item.revoked_at)} className="inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-[11px] text-gray-600 disabled:opacity-40"><RefreshCw className="h-3 w-3" />轮换</button>
                <button type="button" onClick={() => revoke(item.key_id)} disabled={busy || Boolean(item.revoked_at)} className="inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-[11px] text-rose-600 disabled:opacity-40"><Trash2 className="h-3 w-3" />吊销</button>
              </div>
            ))}
          </div>
        </div>

        <section className="border-t border-gray-200 pt-6" aria-labelledby="worker-access-log-title">
          <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
            <div>
              <div className="flex items-center gap-2 text-sm font-semibold text-gray-800" id="worker-access-log-title"><History className="h-4 w-4 text-[#002fa7]" />调用日志</div>
              <p className="mt-1 text-xs text-gray-500">记录通过 Worker Key 发起的 Headless Run，请求时间统一按北京时间展示。</p>
            </div>
            <p className="text-[11px] text-gray-400">共 {logs.total} 条 · 每页 10 条</p>
          </div>

          <div className="grid gap-3 rounded-xl bg-gray-50 p-3 md:grid-cols-2 2xl:grid-cols-[150px_150px_180px_1fr_auto_auto]">
            <label className="min-w-0 text-[11px] font-medium text-gray-500">
              <span className="mb-1.5 flex items-center gap-1"><CalendarDays className="h-3 w-3" />开始日期</span>
              <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} className="form-input !py-2 text-xs" />
            </label>
            <label className="min-w-0 text-[11px] font-medium text-gray-500">
              <span className="mb-1.5 flex items-center gap-1"><CalendarDays className="h-3 w-3" />结束日期</span>
              <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} className="form-input !py-2 text-xs" />
            </label>
            <label className="min-w-0 text-[11px] font-medium text-gray-500">
              <span className="mb-1.5 block">Key Name</span>
              <select value={keyNameFilter} onChange={(event) => setKeyNameFilter(event.target.value)} className="form-input !py-2 text-xs">
                <option value="">全部 Key</option>
                {keyNameOptions.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <label className="min-w-0 text-[11px] font-medium text-gray-500">
              <span className="mb-1.5 block">Query 关键词</span>
              <input
                value={queryFilter}
                onChange={(event) => setQueryFilter(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") applyLogFilters();
                }}
                placeholder="搜索请求内容"
                className="form-input !py-2 text-xs"
              />
            </label>
            <button type="button" onClick={applyLogFilters} className="inline-flex h-[34px] items-center justify-center gap-1.5 self-end rounded-lg bg-[#002fa7] px-3 text-xs font-medium text-white hover:bg-[#001f7a]"><Search className="h-3.5 w-3.5" />筛选</button>
            <button type="button" onClick={resetLogFilters} className="inline-flex h-[34px] items-center justify-center gap-1.5 self-end rounded-lg border border-gray-200 bg-white px-3 text-xs font-medium text-gray-600 hover:bg-gray-50"><RotateCcw className="h-3.5 w-3.5" />重置</button>
          </div>

          {logMessage && <p className="mt-3 text-xs text-rose-600">{logMessage}</p>}

          <div className="mt-4 overflow-hidden rounded-xl border border-gray-200">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] table-fixed text-left">
                <colgroup><col className="w-[165px]" /><col className="w-[140px]" /><col /></colgroup>
                <thead className="bg-gray-50 text-[11px] font-semibold text-gray-500">
                  <tr><th className="px-4 py-3">时间（北京时间）</th><th className="px-4 py-3">Key Name</th><th className="px-4 py-3">Query</th></tr>
                </thead>
                <tbody className="divide-y divide-gray-100 bg-white text-xs text-gray-700">
                  {logLoading ? (
                    <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-400"><span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin text-[#002fa7]" />正在加载调用日志…</span></td></tr>
                  ) : logs.items.length === 0 ? (
                    <tr><td colSpan={3} className="px-4 py-10 text-center text-gray-400">暂无符合条件的调用日志</td></tr>
                  ) : logs.items.map((item) => (
                    <tr key={item.id} className="align-top hover:bg-gray-50/70">
                      <td className="whitespace-nowrap px-4 py-3 font-mono text-[11px] text-gray-500">{item.created_at_beijing}</td>
                      <td className="px-4 py-3 font-medium text-gray-800">{item.key_name}</td>
                      <td className="px-4 py-3"><p className="line-clamp-2 whitespace-pre-wrap break-words leading-5" title={item.query}>{item.query}</p></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex min-h-12 items-center justify-between gap-3 border-t border-gray-100 bg-white px-4 py-2">
              <p className="text-[11px] text-gray-400">第 {logs.total === 0 ? 0 : logs.page} / {logs.total === 0 ? 0 : totalPages} 页</p>
              <div className="flex items-center gap-2">
                <button type="button" aria-label="上一页" disabled={logLoading || logPage <= 1} onClick={() => setLogPage((current) => Math.max(1, current - 1))} className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-35"><ChevronLeft className="h-4 w-4" /></button>
                <button type="button" aria-label="下一页" disabled={logLoading || logPage >= totalPages || logs.total === 0} onClick={() => setLogPage((current) => current + 1)} className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 disabled:opacity-35"><ChevronRight className="h-4 w-4" /></button>
              </div>
            </div>
          </div>
        </section>
      </div>
    </SettingsCard>
  );
}

function SettingsCard({ title, icon: Icon, color, children }: { title: string; icon: React.ElementType; color: string; children: React.ReactNode }) {
  return <section className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm"><div className="mb-5 flex items-center gap-2 text-[15px] font-semibold text-gray-800"><Icon className="h-4 w-4" style={{ color }} />{title}</div>{children}</section>;
}
