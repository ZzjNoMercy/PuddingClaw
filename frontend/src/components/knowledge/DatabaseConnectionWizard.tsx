"use client";

import { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Database, Loader2, RefreshCw, X } from "lucide-react";

import {
  listKnowledgeDatabaseSourceTables,
  saveKnowledgeDatabaseSource,
  testKnowledgeDatabaseSource,
  type KnowledgeDatabaseSource,
} from "@/lib/api";

export type DatabaseConnectorType = "postgresql" | "mysql";

function typeOf(source: Partial<KnowledgeDatabaseSource>): DatabaseConnectorType {
  return (source.source_type || source.type) === "mysql" ? "mysql" : "postgresql";
}

function defaults(sourceType: DatabaseConnectorType): KnowledgeDatabaseSource {
  const mysql = sourceType === "mysql";
  return {
    id: "",
    type: sourceType,
    source_type: sourceType,
    name: mysql ? "MySQL 数据源" : "PostgreSQL 数据源",
    description: "",
    host: "127.0.0.1",
    port: mysql ? 3306 : 5432,
    database: "",
    username: "",
    password: "",
    selected_tables: [],
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "未知错误");
}

export default function DatabaseConnectionWizard({
  open,
  sourceType = "postgresql",
  existingSource = null,
  onClose,
  onSaved,
}: {
  open: boolean;
  sourceType?: DatabaseConnectorType;
  existingSource?: KnowledgeDatabaseSource | null;
  onClose: () => void;
  onSaved: (source: KnowledgeDatabaseSource) => void | Promise<void>;
}) {
  const [draft, setDraft] = useState<KnowledgeDatabaseSource>(() => defaults(sourceType));
  const [tables, setTables] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [status, setStatus] = useState<{ kind: "success" | "error" | "info"; message: string } | null>(null);

  useEffect(() => {
    if (!open) return;
    const next = existingSource ? { ...existingSource, password: "" } : defaults(sourceType);
    setDraft(next);
    setTables(existingSource?.selected_tables || []);
    setStatus(null);
  }, [existingSource, open, sourceType]);

  if (!open) return null;

  const connectorType = typeOf(draft);
  const label = connectorType === "mysql" ? "MySQL" : "PostgreSQL";
  const isProjectDefault = draft.id === "project_postgres";

  function update(updates: Partial<KnowledgeDatabaseSource>) {
    setDraft((current) => ({ ...current, ...updates }));
  }

  function changeType(nextType: DatabaseConnectorType) {
    if (isProjectDefault || draft.id) return;
    const nextDefaults = defaults(nextType);
    setDraft((current) => ({
      ...current,
      type: nextType,
      source_type: nextType,
      port: current.port === 5432 || current.port === 3306 ? nextDefaults.port : current.port,
      name: ["PostgreSQL 数据源", "MySQL 数据源"].includes(current.name) ? nextDefaults.name : current.name,
    }));
  }

  async function save({ close = true }: { close?: boolean } = {}) {
    setBusy("save");
    setStatus(null);
    try {
      const saved = await saveKnowledgeDatabaseSource({
        ...draft,
        type: connectorType,
        source_type: connectorType,
      });
      setDraft({ ...saved, password: "" });
      setStatus({ kind: "success", message: `已保存 ${label} 连接。` });
      await onSaved(saved);
      if (close) onClose();
      return saved;
    } catch (error) {
      setStatus({ kind: "error", message: errorMessage(error) });
      return null;
    } finally {
      setBusy("");
    }
  }

  async function loadTables() {
    setBusy("tables");
    setStatus(null);
    try {
      let current = draft;
      if (!current.id) {
        const saved = await saveKnowledgeDatabaseSource({
          ...current,
          type: connectorType,
          source_type: connectorType,
        });
        current = { ...saved, password: "" };
        setDraft(current);
        await onSaved(saved);
      }
      const nextTables = await listKnowledgeDatabaseSourceTables(current.id);
      setTables(nextTables);
      setStatus({ kind: "success", message: `读取到 ${nextTables.length} 张表；勾选后保存即可授权给智能问数。` });
    } catch (error) {
      setStatus({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy("");
    }
  }

  async function test() {
    setBusy("test");
    setStatus(null);
    try {
      const result = await testKnowledgeDatabaseSource({
        ...draft,
        type: connectorType,
        source_type: connectorType,
      });
      setStatus({ kind: result.ok ? "success" : "error", message: result.message || (result.ok ? "连接成功" : "连接失败") });
    } catch (error) {
      setStatus({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-slate-950/35 px-4 py-6 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label={`${label} 数据源连接`}>
      <div className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-[28px] bg-white shadow-2xl ring-1 ring-black/[0.08]">
        <header className="flex items-start justify-between border-b border-black/[0.06] px-6 py-5">
          <div>
            <div className="flex items-center gap-2"><Database className="h-5 w-5 text-[#002fa7]" /><h2 className="text-lg font-semibold text-gray-950">{existingSource ? "编辑" : "连接"} {label}</h2></div>
            <p className="mt-1 text-xs text-gray-400">数据库只提供实时数据；平台仅保存加密凭据、连接元数据和已授权表。</p>
          </div>
          <button type="button" onClick={onClose} disabled={!!busy} className="grid h-9 w-9 place-items-center rounded-xl bg-gray-50 text-gray-500 hover:bg-gray-100 disabled:opacity-50" aria-label="关闭"><X className="h-4 w-4" /></button>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          <div className="grid gap-4 md:grid-cols-2">
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-gray-500">Connector</span>
              <select value={connectorType} disabled={isProjectDefault || Boolean(draft.id)} onChange={(event) => changeType(event.target.value as DatabaseConnectorType)} className="h-11 w-full rounded-2xl border border-black/[0.08] bg-white px-4 text-sm outline-none disabled:bg-gray-50 disabled:text-gray-400">
                <option value="postgresql">PostgreSQL</option>
                <option value="mysql">MySQL</option>
              </select>
            </label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-gray-500">显示名称</span><input value={draft.name} onChange={(event) => update({ name: event.target.value })} className="h-11 w-full rounded-2xl border border-black/[0.08] px-4 text-sm outline-none focus:border-[#002fa7]/40" /></label>
            <label className="space-y-1.5 md:col-span-2"><span className="text-xs font-semibold text-gray-500">用途说明</span><input value={draft.description || ""} onChange={(event) => update({ description: event.target.value })} placeholder="例如：订单与客户经营分析" className="h-11 w-full rounded-2xl border border-black/[0.08] px-4 text-sm outline-none focus:border-[#002fa7]/40" /></label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-gray-500">Host</span><input value={draft.host} disabled={isProjectDefault} onChange={(event) => update({ host: event.target.value })} className="h-11 w-full rounded-2xl border border-black/[0.08] px-4 text-sm outline-none disabled:bg-gray-50 disabled:text-gray-400" /></label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-gray-500">端口</span><input type="number" value={draft.port} disabled={isProjectDefault} onChange={(event) => update({ port: Number(event.target.value) || (connectorType === "mysql" ? 3306 : 5432) })} className="h-11 w-full rounded-2xl border border-black/[0.08] px-4 text-sm outline-none disabled:bg-gray-50 disabled:text-gray-400" /></label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-gray-500">数据库名</span><input value={draft.database} disabled={isProjectDefault} onChange={(event) => update({ database: event.target.value })} className="h-11 w-full rounded-2xl border border-black/[0.08] px-4 text-sm outline-none disabled:bg-gray-50 disabled:text-gray-400" /></label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-gray-500">用户名</span><input value={draft.username} disabled={isProjectDefault} onChange={(event) => update({ username: event.target.value })} className="h-11 w-full rounded-2xl border border-black/[0.08] px-4 text-sm outline-none disabled:bg-gray-50 disabled:text-gray-400" /></label>
            <label className="space-y-1.5 md:col-span-2"><span className="text-xs font-semibold text-gray-500">密码</span><input type="password" value={draft.password || ""} disabled={isProjectDefault} onChange={(event) => update({ password: event.target.value })} placeholder={draft.password_configured ? "已加密保存，留空表示不修改" : "请输入数据库密码"} className="h-11 w-full rounded-2xl border border-black/[0.08] px-4 text-sm outline-none disabled:bg-gray-50 disabled:text-gray-400" /></label>
          </div>

          {draft.password_readable === false ? <div className="mt-4 rounded-2xl bg-amber-50 px-4 py-3 text-xs text-amber-800">已保存密码无法解密，请重新录入并保存。</div> : null}
          {status ? <div className={`mt-4 flex items-start gap-2 rounded-2xl px-4 py-3 text-xs ${status.kind === "success" ? "bg-emerald-50 text-emerald-700" : status.kind === "error" ? "bg-red-50 text-red-700" : "bg-blue-50 text-[#002fa7]"}`}>{status.kind === "success" ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /> : <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />}<span>{status.message}</span></div> : null}

          <section className="mt-5 rounded-3xl border border-black/[0.06] bg-gray-50/60 p-4">
            <div className="flex items-center justify-between gap-3"><div><h3 className="text-sm font-semibold text-gray-950">授权数据表</h3><p className="mt-1 text-xs text-gray-400">只登记表名与字段结构，不复制数据库行数据。</p></div><button type="button" onClick={() => void loadTables()} disabled={!!busy || !draft.name.trim() || !draft.database.trim()} className="inline-flex h-9 items-center gap-2 rounded-xl bg-white px-3 text-xs font-semibold text-[#002fa7] ring-1 ring-black/[0.06] disabled:opacity-40">{busy === "tables" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}读取表</button></div>
            <div className="mt-3 max-h-56 overflow-y-auto rounded-2xl bg-white p-2">
              {tables.length ? <div className="grid gap-1 sm:grid-cols-2">{tables.map((table) => { const checked = draft.selected_tables.includes(table); return <label key={table} className={`flex cursor-pointer items-center gap-2 rounded-xl px-3 py-2 text-xs ${checked ? "bg-[#002fa7]/10 text-[#002fa7]" : "text-gray-600 hover:bg-gray-50"}`}><input type="checkbox" checked={checked} onChange={(event) => update({ selected_tables: event.target.checked ? [...draft.selected_tables, table] : draft.selected_tables.filter((item) => item !== table) })} className="accent-[#002fa7]" /><span className="truncate" title={table}>{table}</span></label>; })}</div> : <p className="px-3 py-7 text-center text-xs text-gray-400">保存连接并读取后，可在这里选择 Agent 能访问的表。</p>}
            </div>
          </section>
        </div>

        <footer className="flex flex-wrap justify-end gap-3 border-t border-black/[0.06] px-6 py-4">
          <button type="button" onClick={() => void test()} disabled={!!busy || !draft.database.trim()} className="inline-flex h-10 items-center gap-2 rounded-2xl border border-black/[0.08] px-4 text-sm font-semibold text-gray-700 disabled:opacity-45">{busy === "test" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}测试连接</button>
          <button type="button" onClick={onClose} disabled={!!busy} className="h-10 rounded-2xl px-4 text-sm font-semibold text-gray-500">取消</button>
          <button type="button" onClick={() => void save()} disabled={!!busy || !draft.name.trim() || !draft.database.trim()} className="inline-flex h-10 items-center gap-2 rounded-2xl bg-[#002fa7] px-5 text-sm font-semibold text-white disabled:opacity-45">{busy === "save" ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}保存数据源</button>
        </footer>
      </div>
    </div>
  );
}
